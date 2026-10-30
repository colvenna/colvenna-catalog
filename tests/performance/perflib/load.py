"""The load tool (tests/performance/load) in pods of the run's own, on the test environment's
clients' nodes: the pinned Flink 1.20.5 image with the Paimon 2.0.0 jars of the compatibility
matrix's cell flink-1.20-paimon-2.0.0, fetched by an init container and checked against their
SHA-256 (the UAT harness's Paimon tools run the same way); the tool's jar, built on this host, its
launcher and its log4j2 configuration in a ConfigMap. In the EKS form the JVM trusts the JDK's CAs
plus the install's; the pods have no AWS identity of their own: table files are written with the
per-table credentials the catalog vends.

A pod runs one command as a job (`start`), its tokens in a Secret of its own (files, never the
environment), its spec in a ConfigMap, its records, request log and output in /work/out, and then
waits so that the harness can copy /work/out out (`fetch`) before it deletes it. A tool pod
(`tools`) waits instead, and runs short commands through kubectl exec, their spec on stdin."""

import base64
import hashlib
import io
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from . import REPO

from uatlib.kube import hardened  # noqa: E402
from uatlib.tools import cell  # noqa: E402

LOAD = REPO / "tests" / "performance" / "load"
JAR = LOAD / "target" / "perf-load.jar"
LAUNCH = """set -eu
store=/work/cacerts
if [ -f /ca/ca.crt ] && [ ! -f "$store" ]; then
  cp "$JAVA_HOME/lib/security/cacerts" "$store.tmp" && chmod 600 "$store.tmp"
  keytool -importcert -noprompt -alias colvenna-catalog -file /ca/ca.crt -keystore "$store.tmp" -storepass changeit >/dev/null
  mv "$store.tmp" "$store"
fi
trust=""
[ -f "$store" ] && trust="-Djavax.net.ssl.trustStore=$store -Djavax.net.ssl.trustStorePassword=changeit"
mkdir -p "$PERF_OUT"
# shellcheck disable=SC2086
exec java -Xmx${PERF_HEAP:-2g} $trust -Dlog4j2.formatMsgNoLookups=true -Dlog4j2.configurationFile=/perf/log4j2.properties \\
  -cp "/perf/perf-load.jar:/jars/*:/opt/flink/lib/*" com.colvenna.catalog.perf.Load "$@"
"""
# A job: the command, its exit status in /work/exit, then a wait for the harness to fetch /work/out.
JOB = 'mkdir -p "$PERF_OUT"; sh /perf/launch.sh "$@" >"$PERF_OUT/tool.log" 2>&1; echo $? >/work/exit; exec sleep 86400'
_built = {}


def build(scratch, log=print):
    """The tool's jar, built with this host's Maven (BUILD SUCCESS) from a copy of tests/performance/load
    in the run's scratch directory, so that runs at once never build in the same directory."""
    if "jar" in _built:
        return _built["jar"]
    if not shutil.which("mvn"):
        raise RuntimeError("Maven (with JDK 21) is needed to build tests/performance/load")
    source = Path(scratch) / "load"
    shutil.copytree(LOAD, source, ignore=shutil.ignore_patterns("target"))
    p = subprocess.run(["mvn", "-B", "-f", str(source / "pom.xml"), "package", "-DskipTests"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    if p.returncode != 0 or "[INFO] BUILD SUCCESS" not in p.stdout.splitlines():
        raise RuntimeError("tests/performance/load did not build:\n" + p.stdout[-3000:])
    jar = source / "target" / JAR.name
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    log(f"built {jar.name} ({jar.stat().st_size} bytes, sha256 {digest})")
    _built["jar"] = (jar, digest)
    return _built["jar"]


def properties(spec):
    """A spec as a properties file: key=value lines (lists comma-separated)."""
    lines = []
    for k, v in sorted(spec.items()):
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            v = "true" if v else "false"
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


class Loads:
    """The load tool of one install: a namespace of the run (labelled as the install's client in the
    EKS form), the tool's ConfigMap, the install's CA."""

    def __init__(self, target):
        self.target = target
        self.kube = target.kube
        self.namespace = target.namespace("load", ingress="none")
        self.image, self.curl, self.jars = cell()
        jar, self.jar_sha256 = build(target.scratch, target.log)
        self.kube.apply({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": "perf-tool", "namespace": self.namespace},
                         "data": {"launch.sh": LAUNCH, "log4j2.properties": (LOAD / "log4j2.properties").read_text()},
                         "binaryData": {"perf-load.jar": base64.b64encode(jar.read_bytes()).decode()}})
        ca = target.install.ca()
        if ca:
            self.kube.configmap(self.namespace, "perf-ca", {"ca.crt": ca})
        self.has_ca = bool(ca)
        self._tools = None

    def describe(self):
        return {"image": self.image, "jars": [{"name": n, "sha256": s} for n, _, s in self.jars],
                "toolJarSha256": self.jar_sha256}

    def _pod(self, name, command, keys, spec_text=None, memory="3Gi", cpu="500m"):
        secret = f"{name}-tokens"
        tokens = self.target.admin.tokens
        self.kube.secret(self.namespace, secret, {k: tokens[k] for k in keys})
        volumes = [{"name": "jars", "emptyDir": {}}, {"name": "work", "emptyDir": {}},
                   {"name": "perf", "configMap": {"name": "perf-tool", "defaultMode": 0o555}},
                   {"name": "tokens", "secret": {"secretName": secret, "defaultMode": 0o444}}]
        mounts = [{"name": "jars", "mountPath": "/jars", "readOnly": True},
                  {"name": "perf", "mountPath": "/perf", "readOnly": True},
                  {"name": "work", "mountPath": "/work"},
                  {"name": "tokens", "mountPath": "/secret", "readOnly": True}]
        if spec_text is not None:
            self.kube.configmap(self.namespace, f"{name}-spec", {"spec.properties": spec_text})
            volumes.append({"name": "spec", "configMap": {"name": f"{name}-spec"}})
            mounts.append({"name": "spec", "mountPath": "/spec", "readOnly": True})
        if self.has_ca:
            volumes.append({"name": "ca", "configMap": {"name": "perf-ca"}})
            mounts.append({"name": "ca", "mountPath": "/ca", "readOnly": True})
        fetch = ["set -eu", "cd /jars"]
        for jar, url, sha in self.jars:
            fetch.append(f"curl -fsSL --retry 5 --retry-delay 3 -o '{jar}' '{url}'")
            fetch.append(f"echo '{sha}  {jar}' | sha256sum -c -")
        env = {"PERF_URI": self.target.install.uri, "PERF_WAREHOUSE": self.target.install.warehouse,
               "PERF_TOKENS": "/secret", "PERF_OUT": "/work/out", "PERF_HEAP": "2g", "HOME": "/work"}
        manifest = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": self.namespace, "labels": {"app": name}},
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": [{
                    "name": "jars", "image": self.curl, "command": ["sh", "-c", "\n".join(fetch)],
                    "securityContext": hardened(100, 101),
                    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                    "volumeMounts": [{"name": "jars", "mountPath": "/jars"}]}],
                "containers": [{
                    "name": "load", "image": self.image, "command": command,
                    "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                    "securityContext": hardened(9999, read_only=False),
                    "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": memory}},
                    "volumeMounts": mounts}],
                "volumes": volumes}}
        self.kube.apply(manifest)
        return LoadPod(self, name)

    def start(self, name, command, spec, keys):
        """A job pod running `command` with spec ({key: value}) and the tokens of keys."""
        pod = self._pod(name, ["sh", "-c", JOB, "--", command, "/spec/spec.properties"], keys, properties(spec))
        pod.wait_running()
        self.target.log(f"load pod {name} ({command}) running on {pod.node()}")
        return pod

    def tools(self, keys):
        """The pod for short commands (seed, drop, cold), with the tokens of keys; made once."""
        if self._tools is None:
            self._tools = self._pod("tools", ["sh", "-c", "mkdir -p /work/out && exec sleep 86400"], keys,
                                    memory="3Gi", cpu="250m")
            self._tools.keys = set(keys)
            self._tools.wait_running()
        elif not set(keys) <= self._tools.keys:
            raise RuntimeError(f"the tools pod was made without the tokens {sorted(set(keys) - self._tools.keys)}")
        return self._tools

    def tools_gone(self):
        """Whether the tools pod was made and is no longer running (its JVM over its memory, say);
        if so it is deleted, and the next tools() makes another."""
        if self._tools is None or self._tools.status().get("phase") == "Running":
            return False
        self._tools.delete()
        self._tools = None
        return True


class LoadPod:
    def __init__(self, loads, name):
        self.loads, self.name = loads, name
        self.kube, self.namespace = loads.kube, loads.namespace

    def status(self):
        p = self.kube.kubectl("get", "pod", self.name, "-n", self.namespace, "-o", "json", check=False)
        if p.returncode != 0:
            return {"phase": "Missing"}
        import json

        return json.loads(p.stdout).get("status", {})

    def node(self):
        p = self.kube.kubectl("get", "pod", self.name, "-n", self.namespace, "-o", "jsonpath={.spec.nodeName}",
                              check=False)
        return p.stdout.strip()

    def wait_running(self, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            st = self.status()
            if st.get("phase") == "Running":
                return
            if st.get("phase") in ("Failed", "Succeeded", "Missing"):
                raise RuntimeError(f"load pod {self.name} ended ({st.get('phase')}) before it ran")
            time.sleep(3)
        raise RuntimeError(f"load pod {self.name} not running after {timeout}s")

    def exit_code(self):
        p = self.kube.exec(self.namespace, self.name, ["cat", "/work/exit"], check=False, container="load", timeout=60)
        text = p.stdout.strip()
        return int(text) if p.returncode == 0 and text.lstrip("-").isdigit() else None

    def wait(self, timeout):
        """Waits until the job's command ended; its exit status (-1 if it did not end in time, or the
        pod went away)."""
        end = time.time() + timeout
        while time.time() < end:
            code = self.exit_code()
            if code is not None:
                return code
            if self.status().get("phase") in ("Failed", "Succeeded", "Missing"):
                return -1
            time.sleep(10)
        return -1

    def run(self, command, spec, timeout=1800):
        """Runs a short command in a tool pod, its spec on stdin; returns (exit code, output text)."""
        args = ["sh", "-c", 'f=/work/spec-$$.properties; cat >"$f"; sh /perf/launch.sh "$1" "$f"', "--", command]
        p = self.kube.exec(self.namespace, self.name, args, input=properties(spec), timeout=timeout, check=False,
                           container="load")
        return p.returncode, p.stdout + ("\n" + p.stderr if p.returncode else "")

    def fetch(self, dest):
        """Copies /work/out of the pod into dest (made if missing); returns dest."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        args = ["kubectl", "exec", "-i", self.name, "-n", self.namespace, "-c", "load", "--",
                "tar", "-C", "/work/out", "-czf", "-", "."]
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.kube.env, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError(f"could not copy {self.name}'s output: {p.stderr.decode(errors='replace')[-500:]}")
        with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or ".." in Path(member.name).parts or member.name.startswith("/"):
                    continue
                data = tar.extractfile(member).read()
                (dest / Path(member.name).name).write_bytes(data)
        return dest

    def delete(self):
        self.kube.kubectl("delete", "pod", self.name, "-n", self.namespace, "--ignore-not-found", "--wait=true",
                          "--grace-period=1", check=False, timeout=300)
        gone = self.kube.kubectl("get", "pod", self.name, "-n", self.namespace, check=False).returncode != 0
        return gone
