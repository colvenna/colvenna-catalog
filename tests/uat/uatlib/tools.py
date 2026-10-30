"""The Paimon tools (tests/uat/paimon: CommitStress, MaintenanceLoop, the upgrade's loops, state dumps,
an outside writer) in pods of the run's own, on the pinned Flink 1.20.5 image with the Paimon 2.0.0
jars the compatibility matrix pins (tests/compat/cells.json's cell flink-1.20-paimon-2.0.0), fetched
by an init container and checked against their SHA-256; nothing is built into an image.

A tool pod holds the jars and the tools' jar and waits: short tools run in it through kubectl exec,
the token on stdin. A job pod runs one long tool as its main process (a writer, the maintenance loop,
a loop of the upgrade), its token in a Secret of its own, its output read back from its log. Every
tool's facts are lines "UAT <json>" (tests/uat/paimon Out). In the EKS form the JVM trusts the JDK's
CAs plus the install's; the pods have no AWS identity of their own (no service account token, no
IRSA role): every table file is read and written with the per-table credentials the catalog vends."""

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

from .install import REPO
from .kube import hardened

PAIMON = REPO / "tests" / "uat" / "paimon"
CELLS = REPO / "tests" / "compat" / "cells.json"
CELL = "flink-1.20-paimon-2.0.0"
PROXY = Path(__file__).resolve().parent.parent / "probe" / "proxy.py"
PROXY_PORT = 18181
TOOL = "com.colvenna.catalog.uat.paimon.Tool"

# The tool's launcher: the trust store (the JDK's CAs plus the catalog's, when there is one), then
# the JVM, sized to the container (the local cluster sizes its pools from the CPU count).
LAUNCH = """set -eu
store=/work/cacerts
if [ -f /ca/ca.crt ] && [ ! -f "$store" ]; then
  cp "$JAVA_HOME/lib/security/cacerts" "$store.tmp" && chmod 600 "$store.tmp"
  keytool -importcert -noprompt -alias colvenna-catalog -file /ca/ca.crt -keystore "$store.tmp" -storepass changeit >/dev/null
  mv "$store.tmp" "$store"
fi
trust=""
[ -f "$store" ] && trust="-Djavax.net.ssl.trustStore=$store -Djavax.net.ssl.trustStorePassword=changeit"
# shellcheck disable=SC2086
exec java -Xmx${UAT_HEAP:-1g} -XX:ActiveProcessorCount=2 $trust -Dlog4j2.formatMsgNoLookups=true \\
  -cp "/uat/uat-paimon.jar:/jars/*:/opt/flink/lib/*" com.colvenna.catalog.uat.paimon.Tool "$@"
"""


def build(log=print):
    """The tools' jar, built from tests/uat/paimon with this host's Maven; its build must say BUILD SUCCESS."""
    if not shutil.which("mvn"):
        raise RuntimeError("Maven (with JDK 21) is needed to build tests/uat/paimon")
    p = subprocess.run(["mvn", "-B", "-f", str(PAIMON / "pom.xml"), "package", "-DskipTests"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    if p.returncode != 0 or "[INFO] BUILD SUCCESS" not in p.stdout.splitlines():
        raise RuntimeError("tests/uat/paimon did not build:\n" + p.stdout[-3000:])
    jar = PAIMON / "target" / "uat-paimon.jar"
    log(f"built {jar.name} ({jar.stat().st_size} bytes)")
    return jar


def cell():
    config = json.loads(CELLS.read_text())
    c = next(c for c in config["cells"] if c["id"] == CELL)
    return config["images"][c["images"][0]], config["images"]["curl"], [
        (name, config["mavenRepository"] + "/" + config["jars"][name]["path"], config["jars"][name]["sha256"])
        for name in c["jars"]]


def half(memory):
    """Half a Kubernetes memory quantity in Mi ("1536Mi" -> "768Mi", "2Gi" -> "1024Mi")."""
    n = int(memory[:-2]) * (1024 if memory.endswith("Gi") else 1)
    return f"{n // 2}Mi"


def lines(text):
    """The tool's facts: every "UAT <json>" line of its output."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith("UAT {"):
            try:
                out.append(json.loads(line[4:]))
            except ValueError:
                continue
    return out


def checks(facts):
    """{check: (ok, detail)} of a tool's "check" lines (a failed line wins over a passed one)."""
    out = {}
    for f in facts:
        if f.get("kind") == "check":
            ok = bool(f.get("ok")) and out.get(f["check"], (True, ""))[0]
            out[f["check"]] = (ok, f.get("detail", ""))
    return out


class Tools:
    """The tools of one run: their jar (built once), a ConfigMap of it and the launcher, the CA."""

    def __init__(self, ctx, namespace=None):
        self.ctx = ctx
        self.kube = ctx.kube
        self.namespace = namespace or ctx.namespace("tools", ingress="none")
        self.image, self.curl, self.jars = cell()
        jar = build(ctx.log)
        self.kube.apply({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": "uat-tools", "namespace": self.namespace},
                         "data": {"launch.sh": LAUNCH, "proxy.py": PROXY.read_text()},
                         "binaryData": {"uat-paimon.jar": base64.b64encode(jar.read_bytes()).decode()}})
        ca = ctx.install.ca()
        if ca:
            self.kube.configmap(self.namespace, "uat-ca", {"ca.crt": ca})
        self.has_ca = bool(ca)
        self.pod = None

    def uri(self, proxied=False):
        """The catalog's URI as a tool reaches it: directly, or through the pod's proxy on loopback."""
        uri = self.ctx.install.uri
        if not proxied:
            return uri
        path = uri.split("://", 1)[1]
        path = path[path.index("/"):] if "/" in path else ""
        return f"http://127.0.0.1:{PROXY_PORT}{path}"

    def _spec(self, name, command, env, token_secret=None, proxy=None, memory="1536Mi", cpu="100m"):
        fetch = ["set -eu", "cd /jars"]
        for jar, url, sha in self.jars:
            fetch.append(f"curl -fsSL --retry 5 --retry-delay 3 -o '{jar}' '{url}'")
            fetch.append(f"echo '{sha}  {jar}' | sha256sum -c -")
        mounts = [{"name": "jars", "mountPath": "/jars", "readOnly": True},
                  {"name": "uat", "mountPath": "/uat", "readOnly": True},
                  {"name": "work", "mountPath": "/work"}]
        volumes = [{"name": "jars", "emptyDir": {}}, {"name": "work", "emptyDir": {}},
                   {"name": "uat", "configMap": {"name": "uat-tools", "defaultMode": 0o555}}]
        if self.has_ca:
            mounts.append({"name": "ca", "mountPath": "/ca", "readOnly": True})
            volumes.append({"name": "ca", "configMap": {"name": "uat-ca"}})
        if token_secret:
            mounts.append({"name": "token", "mountPath": "/secret", "readOnly": True})
            volumes.append({"name": "token", "secret": {"secretName": token_secret, "defaultMode": 0o444}})
        base_env = {"UAT_WAREHOUSE": self.ctx.install.warehouse, "HOME": "/work", "UAT_HEAP": "1g"}
        base_env.update(env)
        containers = [{
            "name": "tool", "image": self.image, "command": command,
            "env": [{"name": k, "value": str(v)} for k, v in base_env.items()],
            "securityContext": hardened(9999, read_only=False),
            # Requested at half its limit: the JVMs (-Xmx1g) seldom need it all, and the test
            # environment's clients' nodes hold every client of the run's installs and these.
            "resources": {"requests": {"cpu": cpu, "memory": half(memory)}, "limits": {"memory": memory}},
            "volumeMounts": mounts}]
        sidecars = []
        if proxy:
            from .probe import PYTHON_IMAGE

            proxy_env = {"UAT_UPSTREAM": self.ctx.install.uri, "UAT_PROXY_PORT": str(PROXY_PORT),
                         "UAT_DROP_EVERY": str(proxy)}
            # A native sidecar (#38): an init container that keeps running, which the kubelet starts, and waits
            # for until it listens, before the tool starts. As the tool's second container it could start
            # later than the tool's first request, while its image was still being pulled on a new node.
            sidecars.append({
                "name": "proxy", "image": PYTHON_IMAGE, "command": ["python3", "-u", "/uat/proxy.py"],
                "restartPolicy": "Always",
                # The proxy listens on 127.0.0.1 alone, and a tcpSocket probe would connect from the node to the
                # pod's address: an exec probe connects inside the pod.
                "startupProbe": {"exec": {"command": ["python3", "-c", "import socket; socket.create_connection("
                                                     f"('127.0.0.1', {PROXY_PORT}), 1).close()"]},
                                 "periodSeconds": 1, "timeoutSeconds": 2, "failureThreshold": 300},
                "env": [{"name": k, "value": v} for k, v in proxy_env.items()],
                "securityContext": hardened(65534),
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                "volumeMounts": [m for m in mounts if m["name"] in ("uat", "ca")] + [{"name": "tmp", "mountPath": "/tmp"}]})  # nosec B108 - the pod's own /tmp, an emptyDir
            volumes.append({"name": "tmp", "emptyDir": {}})
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": self.namespace, "labels": {"app": name}},
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": [{
                    "name": "jars", "image": self.curl, "command": ["sh", "-c", "\n".join(fetch)],
                    "securityContext": hardened(100, 101),
                    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                    "volumeMounts": [{"name": "jars", "mountPath": "/jars"}]}] + sidecars,
                "containers": containers, "volumes": volumes}}

    # --- a pod for short tools -------------------------------------------------------------------------

    def start(self, name="tools"):
        self.kube.apply(self._spec(name, ["sleep", "86400"], {"UAT_URI": self.uri()}))
        self.kube.wait_ready(self.namespace, name, timeout=900)
        self.pod = name
        return self

    def run(self, token_key, *args, keep=None, timeout=1800, uri=None, more=None):
        """Runs a tool in the tool pod with the token held under token_key, on stdin (and, after it, the
        line more: credentials a tool takes, never an argument); returns (exit code, facts)."""
        if self.pod is None:
            self.start()
        env = ["env", f"UAT_URI={uri or self.uri()}", "UAT_TOKEN_FILE=-"]
        stdin = self.ctx.admin.tokens[token_key] + "\n" + (more + "\n" if more else "")
        p = self.kube.exec(self.namespace, self.pod, env + ["sh", "/uat/launch.sh", *args], input=stdin,
                           timeout=timeout, check=False, container="tool")
        text = p.stdout + ("\n" + p.stderr if p.returncode else "")
        if keep:
            with open(self.ctx.evidence / keep, "a", encoding="utf-8") as f:
                f.write(self.ctx.sanitizer.text(f"$ {' '.join(args)}\n{text}\n"))
        return p.returncode, lines(p.stdout)

    # --- job pods for long tools ---------------------------------------------------------------------

    def job(self, name, token_key, *args, proxy=None, memory="1536Mi", cpu="200m"):
        """Starts a pod whose main process is the tool; proxy: the proxy's drop period (every n-th
        commit's response dropped), with the tool reaching the catalog through it."""
        secret = f"{name}-token"
        self.kube.secret(self.namespace, secret, {"token": self.ctx.admin.tokens[token_key]})
        env = {"UAT_URI": self.uri(proxied=bool(proxy)), "UAT_TOKEN_FILE": "/secret/token"}
        spec = self._spec(name, ["sh", "/uat/launch.sh", *args], env, token_secret=secret, proxy=proxy,
                          memory=memory, cpu=cpu)
        self.kube.apply(spec)
        return Job(self, name)


class Job:
    def __init__(self, tools, name):
        self.tools, self.name = tools, name
        self.kube, self.namespace = tools.kube, tools.namespace

    def status(self):
        p = self.kube.kubectl("get", "pod", self.name, "-n", self.namespace, "-o", "json", check=False)
        if p.returncode != 0:
            return {"phase": "Missing"}
        status = json.loads(p.stdout).get("status", {})
        tool = next((s for s in status.get("containerStatuses", []) if s["name"] == "tool"), {})
        return {"phase": status.get("phase"), "state": tool.get("state", {})}

    def running(self):
        state = self.status()
        return "running" in state.get("state", {})

    def ended(self):
        state = self.status()
        return "terminated" in state.get("state", {}) or state.get("phase") in ("Succeeded", "Failed", "Missing")

    def wait_running(self, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            if self.running() or self.ended():
                return self.running()
            time.sleep(5)
        return False

    def wait(self, timeout):
        """Waits until the tool ended; returns its exit code (-1 if it did not end in time)."""
        end = time.time() + timeout
        while time.time() < end:
            term = self.status().get("state", {}).get("terminated")
            if term:
                return term.get("exitCode", -1)
            if self.status().get("phase") == "Missing":
                return -1
            time.sleep(10)
        return -1

    def stop(self):
        """Asks a loop to end (the file /work/stop)."""
        self.kube.exec(self.namespace, self.name, ["touch", "/work/stop"], check=False, container="tool")

    def logs(self, container="tool"):
        return self.kube.logs(self.namespace, self.name, container)

    def keep_start(self, ctx):
        """What a job that did not start left (#38): its pod's status and its containers' logs, kept before the
        clean-up deletes them."""
        p = self.kube.kubectl("get", "pod", self.name, "-n", self.namespace, "-o", "json", check=False)
        status = json.loads(p.stdout).get("status", {}) if p.returncode == 0 else {"phase": "Missing"}
        logs = {}
        for c in ("jars", "proxy", "tool"):
            p = self.kube.kubectl("logs", self.name, "-n", self.namespace, "-c", c, "--tail=200", check=False)
            logs[c] = p.stdout + (p.stderr if p.returncode else "")
        events = self.kube.kubectl("describe", "pod", self.name, "-n", self.namespace, check=False).stdout
        ctx.keep(f"{self.name}-not-started.json", {"status": status, "logs": logs, "describe": events[-6000:]})

    def facts(self):
        return lines(self.logs())

    def keep(self, ctx, name=None):
        name = name or self.name
        ctx.keep(f"{name}.log", self.logs())
        proxy = self.kube.logs(self.namespace, self.name, "proxy")
        if proxy and "No such container" not in proxy and "not found" not in proxy:
            ctx.keep(f"{name}-proxy.log", proxy)
