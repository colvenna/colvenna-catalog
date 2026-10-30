"""Real engines for the cases that need them (UAT-CAT-01, 02, 03): Flink 1.20, Spark 3.5 and Doris 4.1.5
from their official images, pinned by digest, with the Paimon jars of the compatibility matrix's
cells (tests/compat/cells.json: flink-1.20-paimon-2.0.0, spark-3.5-paimon-2.0.0 and doris-4.1.5),
fetched from Maven Central and checked against their SHA-256 by an init container, never built into
an image. The flows are this repository's (tests/uat/flows, and the matrix's for its fixture and its
Doris reader, tests/compat). Everything runs in one namespace of the run's own, labelled as a client
of the install, admitting ingress only from its own pods (a Flink session cluster's TaskManagers and
JobManager talk to each other), with no AWS identity of its own: the pods' default service account,
no service account token, so that only the catalog's per-table credentials reach the tables' files.

  /jars    the cell's Paimon jars          /uat     the flows and the matrix's engine scripts
  /secret  the tokens the pod needs        /ca      the install's CA (EKS form)
  /work    trust stores (the JDK's CAs plus the install's CA)"""

import base64
import json
import subprocess
import sys
import time

from . import kube
from .install import REPO
from .kube import KubeError
from .probe import Probe

sys.path.insert(0, str(REPO / "tests" / "compat"))
from compatlib import config as compat_config  # noqa: E402

FLOWS = REPO / "tests" / "uat" / "flows"
COMPAT_FLOWS = REPO / "tests" / "compat" / "flows"
COMPAT_ENGINE = REPO / "tests" / "compat" / "engine"
FLINK_CELL, SPARK_CELL, DORIS_CELL = "flink-1.20-paimon-2.0.0", "spark-3.5-paimon-2.0.0", "doris-4.1.5"
JARS = {"uat-flink.jar": FLOWS / "flink" / "target" / "uat-flink.jar",
        "uat-spark.jar": FLOWS / "spark" / "target" / "uat-spark.jar",
        "compat-flink.jar": COMPAT_FLOWS / "flink" / "target" / "compat-flink.jar"}
SCRIPTS = ("truststore.sh", "log4j2.properties", "flink.sh", "reader.py", "doris-fe.sh", "doris-be.sh")
FLINK_UID, SPARK_UID = 9999, 185
JM_PORTS = {"rpc": 6123, "blob": 6124, "rest": 8081}


def build(log):
    """The flow jars: tests/uat/flows and the matrix's flows (its fixture writer), with Maven."""
    for pom in (FLOWS / "pom.xml", COMPAT_FLOWS / "pom.xml"):
        log(f"building {pom.relative_to(REPO)}")
        p = subprocess.run(["mvn", "-B", "-f", str(pom), "package", "-DskipTests"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        # The exit status alone is not the verdict: the build must say BUILD SUCCESS.
        if p.returncode != 0 or "[INFO] BUILD SUCCESS" not in p.stdout.splitlines():
            raise RuntimeError(f"{pom} did not build:\n" + p.stdout[-3000:])
    return dict(JARS)


def hardened(uid, gid=None):
    """kube.hardened, with a writable root file system: the engines' images write beside themselves
    (Flink's entrypoint its configuration, Spark its work directory)."""
    return kube.hardened(uid, gid, read_only=False)


class Engines:
    """The engines' namespace and what its pods share: the flows, the scripts, the tokens, the CA."""

    def __init__(self, ctx, role="engines", security="restricted"):
        self.ctx = ctx
        self.kube = ctx.kube
        self.cells = compat_config.load_cells()
        self.ca = ctx.install.ca() if ctx.install.form == "eks" else None
        self.namespace = ctx.namespace(role, client=True, security=security, ingress="namespace")
        self.secrets = {}
        self._probe = None
        jars = build(ctx.log)
        binary = {name: base64.b64encode(path.read_bytes()).decode() for name, path in jars.items()}
        data = {name: (COMPAT_ENGINE / name).read_text() for name in SCRIPTS}
        self.kube.apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "uat-files", "namespace": self.namespace},
                         "data": data, "binaryData": binary})
        if self.ca:
            self.kube.configmap(self.namespace, "uat-ca", {"ca.crt": self.ca})

    def cell(self, cid):
        return compat_config.cell_by_id(self.cells, cid)

    def image(self, name):
        return self.cells["images"][name]

    def tokens(self, files):
        """The Secret of the namespace's tokens (and passwords): {key: value}, added to what is there."""
        self.secrets.update(files)
        self.kube.secret(self.namespace, "uat-secret", self.secrets)

    # --- the parts of a pod -----------------------------------------------------------------------------

    def volumes(self, secrets):
        vols = [{"name": "jars", "emptyDir": {}}, {"name": "work", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                {"name": "uat", "configMap": {"name": "uat-files", "defaultMode": 0o555}},
                {"name": "secret", "secret": {"secretName": "uat-secret", "defaultMode": 0o444,  # pragma: allowlist secret - the name of the Kubernetes Secret the pod mounts, not a value
                                              "items": [{"key": s, "path": s} for s in secrets]}}]
        if self.ca:
            vols.append({"name": "ca", "configMap": {"name": "uat-ca"}})
        return vols

    def mounts(self, extra=()):
        m = [{"name": "jars", "mountPath": "/jars", "readOnly": True}, {"name": "work", "mountPath": "/work"},
             {"name": "tmp", "mountPath": "/tmp"},  # nosec B108 - the pod's own /tmp, an emptyDir
             {"name": "uat", "mountPath": "/uat", "readOnly": True},
             # The matrix's scripts read each other from /compat.
             {"name": "uat", "mountPath": "/compat", "readOnly": True},
             {"name": "secret", "mountPath": "/secret", "readOnly": True}]
        if self.ca:
            m.append({"name": "ca", "mountPath": "/ca", "readOnly": True})
        return m + list(extra)

    def fetch(self, cid):
        """The init container that fetches the cell's Paimon jars and checks their SHA-256."""
        cell = self.cell(cid)
        maven = self.cells["mavenRepository"]
        script = ["set -eu", "cd /jars"]
        for jar in cell.get("jars", []):
            spec = self.cells["jars"][jar]
            script.append(f"curl -fsSL --retry 5 --retry-delay 3 -o '{jar}' '{maven}/{spec['path']}'")
            script.append(f"echo '{spec['sha256']}  {jar}' | sha256sum -c -")
        return {"name": "jars", "image": self.image("curl"), "command": ["sh", "-c", "\n".join(script)],
                "securityContext": hardened(100, 101),
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                "volumeMounts": [{"name": "jars", "mountPath": "/jars"}]}

    def trust(self, image, uid, name):
        """The init container that makes /work/<name>-cacerts: the JDK's CAs plus the install's."""
        return {"name": "trust", "image": image, "securityContext": hardened(uid),
                "command": ["sh", "-c", f"trust_name={name}; . /uat/truststore.sh"],
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                "volumeMounts": self.mounts()}

    def trust_opts(self, name):
        return (f"-Djavax.net.ssl.trustStore=/work/{name}-cacerts -Djavax.net.ssl.trustStorePassword=changeit"
                if self.ca else "")

    def flink_lib(self):
        """The init container that puts the image's Flink jars and the cell's Paimon jars in one lib."""
        return {"name": "lib", "image": self.image("flink-1.20"), "securityContext": hardened(FLINK_UID),
                "command": ["sh", "-c", "cp /opt/flink/lib/* /flinklib/ && cp /jars/*.jar /flinklib/"],
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                "volumeMounts": self.mounts([{"name": "flinklib", "mountPath": "/flinklib"}])}

    def env(self, token):
        install = self.ctx.install
        return [{"name": "UAT_URI", "value": install.uri}, {"name": "UAT_TOKEN_FILE", "value": f"/secret/{token}"},
                {"name": "UAT_WAREHOUSE", "value": install.warehouse}, {"name": "UAT_S3_REGION", "value": install.region},
                {"name": "HOME", "value": "/tmp"}]  # nosec B108 - the container's own /tmp

    def pod(self, name, containers, init, secrets, deadline, extra_volumes=(), labels=None):
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "namespace": self.namespace, "labels": dict({"app": name}, **(labels or {}))},
                "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                         "activeDeadlineSeconds": deadline, "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                         "initContainers": init, "containers": containers,
                         "volumes": self.volumes(secrets) + list(extra_volumes)}}

    # --- running pods -----------------------------------------------------------------------------------

    def start(self, manifest):
        self.kube.kubectl("delete", "pod", manifest["metadata"]["name"], "-n", self.namespace, "--ignore-not-found",
                          "--wait=true", check=False)
        self.kube.apply(manifest)
        return manifest["metadata"]["name"]

    def wait(self, name, timeout, keep=None):
        """Waits until the pod's containers end (or timeout); returns (exit code of the first container, its
        output, every container's output). The pod is deleted afterwards; its output kept as evidence."""
        end = time.time() + timeout
        code = -1
        while time.time() < end:
            p = self.kube.kubectl("get", "pod", name, "-n", self.namespace, "-o", "json", check=False)
            if p.returncode != 0:
                break
            pod = json.loads(p.stdout)
            status = pod.get("status", {})
            failed_init = [s for s in status.get("initContainerStatuses", [])
                           if s.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0]
            # The pod's first container is its main one: its end is the pod's (Doris's BE, beside its FE,
            # never ends by itself). Kubernetes lists the containers' states by name, not in order.
            main = pod["spec"]["containers"][0]["name"]
            term = next((c.get("state", {}).get("terminated") for c in status.get("containerStatuses", [])
                         if c["name"] == main), None)
            if term or status.get("phase") in ("Succeeded", "Failed") or failed_init:
                code = (term or {}).get("exitCode", -1)
                break
            time.sleep(10)
        manifest = self.kube.json("get", "pod", name, "-n", self.namespace) if self.kube.kubectl(
            "get", "pod", name, "-n", self.namespace, check=False).returncode == 0 else {"spec": {}}
        logs = {}
        for c in manifest["spec"].get("initContainers", []) + manifest["spec"].get("containers", []):
            logs[c["name"]] = self.kube.logs(self.namespace, name, c["name"])
        self.kube.kubectl("delete", "pod", name, "-n", self.namespace, "--ignore-not-found", "--wait=false",
                          "--grace-period=5", check=False)
        if keep:
            self.ctx.keep(keep, "".join(f"===== {c}\n{t}\n" for c, t in logs.items()))
        main = manifest["spec"].get("containers", [{}])[0].get("name")
        return code, logs.get(main, ""), logs

    def run(self, manifest, timeout, keep=None):
        return self.wait(self.start(manifest), timeout, keep)

    # --- Flink ------------------------------------------------------------------------------------------

    def flink_properties(self, cluster, slots, jm_mb=1024, tm_mb=1536):
        lines = [f"jobmanager.rpc.address: {cluster}", f"taskmanager.numberOfTaskSlots: {slots}",
                 "restart-strategy.type: none", "classloader.check-leaked-classloader: false",
                 f"jobmanager.memory.process.size: {jm_mb}m", f"taskmanager.memory.process.size: {tm_mb}m",
                 f"blob.server.port: {JM_PORTS['blob']}", "taskmanager.rpc.port: 6122",
                 "web.submit.enable: false", "web.cancel.enable: false"]
        return "\n".join(lines)

    def flink_container(self, name, args, env=(), cpu="250m", memory="1Gi"):
        # The trust store through JAVA_TOOL_OPTIONS: Flink's env.java.opts.all and FLINK_ENV_JAVA_OPTS
        # would replace the JDK 17 module options its configuration carries there.
        trust = [{"name": "JAVA_TOOL_OPTIONS", "value": self.trust_opts("flink")}] if self.ca else []
        return {"name": name, "image": self.image("flink-1.20"), "args": args, "securityContext": hardened(FLINK_UID),
                "env": list(env) + trust, "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": memory}},
                "volumeMounts": self.mounts([{"name": "flinklib", "mountPath": "/opt/flink/lib"}])}

    def session_cluster(self, cluster, token, task_managers=2, slots=2, timeout=900, jm_mb=1024, tm_mb=1536, cpu="250m"):
        """A standalone Flink session cluster: a JobManager and its TaskManagers as Deployments and the
        JobManager's Service, the TaskManagers with the catalog's token (they fetch their own data
        tokens); no Flink operator. Returns once every TaskManager has registered."""
        props = {"name": "FLINK_PROPERTIES", "value": self.flink_properties(cluster, slots, jm_mb, tm_mb)}
        labels = {"app": cluster}
        self.kube.apply({"apiVersion": "v1", "kind": "Service", "metadata": {"name": cluster, "namespace": self.namespace},
                         "spec": {"selector": dict(labels, component="jobmanager"),
                                  "ports": [{"name": k, "port": v} for k, v in JM_PORTS.items()]}})
        for component, replicas, args, memory in (("jobmanager", 1, ["jobmanager"], f"{jm_mb}Mi"),
                                                   ("taskmanager", task_managers, ["taskmanager"], f"{tm_mb}Mi")):
            init = [self.fetch(FLINK_CELL), self.flink_lib()] + ([self.trust(self.image("flink-1.20"), FLINK_UID, "flink")]
                                                                  if self.ca else [])
            spec = self.pod(f"{cluster}-{component}", [self.flink_container(component, args, [props] + self.env(token),
                                                                            cpu=cpu, memory=memory)],
                            init, [token], 86400, [{"name": "flinklib", "emptyDir": {}}])["spec"]
            spec.pop("activeDeadlineSeconds")
            spec["restartPolicy"] = "Always"
            self.kube.apply({"apiVersion": "apps/v1", "kind": "Deployment",
                             "metadata": {"name": f"{cluster}-{component}", "namespace": self.namespace},
                             "spec": {"replicas": replicas,
                                      "selector": {"matchLabels": dict(labels, component=component)},
                                      "template": {"metadata": {"labels": dict(labels, component=component)}, "spec": spec}}})
        end = time.time() + timeout
        while time.time() < end:
            ready = self._registered(cluster)
            if ready >= task_managers:
                self.ctx.log(f"Flink session cluster {cluster}: {ready} TaskManagers registered")
                return
            time.sleep(10)
        raise KubeError(f"the Flink session cluster {cluster} has no {task_managers} TaskManagers after {timeout}s")

    @property
    def probe(self):
        """A probe pod in the engines' namespace, which alone reaches the engines' ports."""
        if self._probe is None:
            self._probe = Probe(self.kube, self.namespace, name="engines-probe", evidence=self.ctx.evidence,
                                sanitizer=self.ctx.sanitizer).start()
        return self._probe

    def _registered(self, cluster):
        """TaskManagers the JobManager knows, from its REST API."""
        r = self.probe.run([{"name": "taskmanagers", "url": f"http://{cluster}:{JM_PORTS['rest']}/taskmanagers",
                             "timeout": 5}])[0]
        body = r.get("body") if isinstance(r.get("body"), dict) else {}
        return len(body.get("taskmanagers", []))

    def jobs(self, cluster):
        """The cluster's jobs and their states, from the JobManager's REST API."""
        r = self.probe.run([{"name": "jobs", "url": f"http://{cluster}:{JM_PORTS['rest']}/jobs/overview", "timeout": 10}])[0]
        body = r.get("body") if isinstance(r.get("body"), dict) else {}
        return body.get("jobs", [])

    def cluster_logs(self, cluster, keep):
        out = []
        for pod in self.kube.json("get", "pods", "-n", self.namespace, "-l", f"app={cluster}")["items"]:
            name = pod["metadata"]["name"]
            restarts = sum(s.get("restartCount", 0) for s in pod["status"].get("containerStatuses", []))
            out.append(f"===== {name} (restarts {restarts})\n" + self.kube.logs(self.namespace, name))
        self.ctx.keep(keep, "\n".join(out))
        return out

    def flink_client(self, name, cluster, main, args, token, parallelism=2, deadline=3600, memory="1Gi", cpu="250m"):
        """`flink run` of a flow class on the session cluster: the flow's main method runs in this pod,
        its jobs on the cluster."""
        env = self.env(token)
        command = ["/opt/flink/bin/flink", "run", "-m", f"{cluster}:{JM_PORTS['rest']}", "-p", str(parallelism),
                   "-c", main, "/uat/uat-flink.jar", *args]
        c = self.flink_container("client", [], env, cpu=cpu, memory=memory)
        c["command"] = command
        init = [self.fetch(FLINK_CELL), self.flink_lib()] + ([self.trust(self.image("flink-1.20"), FLINK_UID, "flink")]
                                                              if self.ca else [])
        return self.pod(name, [c], init, [token], deadline, [{"name": "flinklib", "emptyDir": {}}])

    def java(self, name, main, args, token, jar="/uat/uat-flink.jar", deadline=1800, env=(), memory="1Gi"):
        """A flow class in one JVM on the Flink image (a local Flink, or the Paimon Java API alone)."""
        trust = self.trust_opts("flink")
        script = (f"exec java -Xmx768m -XX:ActiveProcessorCount=2 {trust} "
                  f"-Dlog4j.configurationFile=/uat/log4j2.properties -cp '{jar}:/jars/*:/opt/flink/lib/*' {main} "
                  + " ".join(f"'{a}'" for a in args))
        c = {"name": "java", "image": self.image("flink-1.20"), "command": ["sh", "-c", script],
             "securityContext": hardened(FLINK_UID), "env": self.env(token) + list(env),
             "resources": {"requests": {"cpu": "250m", "memory": memory}, "limits": {"memory": memory}},
             "volumeMounts": self.mounts()}
        init = [self.fetch(FLINK_CELL)] + ([self.trust(self.image("flink-1.20"), FLINK_UID, "flink")] if self.ca else [])
        return self.pod(name, [c], init, [token], deadline)

    def compat_fixture(self, name, database, token, deadline=1800):
        """The matrix's fixture writer (compat FlinkFlow, fixture mode, on the flink-1.20-paimon-2.0.0
        cell): the tables a reader cell reads, and the checks it prints for them (COMPAT-FIXTURE)."""
        env = [{"name": "COMPAT_URI", "value": self.ctx.install.uri}, {"name": "COMPAT_TOKEN_FILE", "value": f"/secret/{token}"},
               {"name": "COMPAT_WAREHOUSE", "value": self.ctx.install.warehouse}, {"name": "COMPAT_DATA_TOKENS", "value": "true"},
               {"name": "COMPAT_HEAP", "value": "2g"}, {"name": "COMPAT_S3_REGION", "value": self.ctx.install.region},
               {"name": "HOME", "value": "/tmp"}]  # nosec B108 - the container's own /tmp
        # flink.sh runs /compat/compat-flink.jar and /jars/*: the matrix's own way.
        c = {"name": "flow", "image": self.image("flink-1.20"), "command": ["sh", "/compat/flink.sh", "fixture", database],
             "securityContext": hardened(FLINK_UID), "env": env,
             # The matrix's own sizes for a writer (compatlib.cells.flow_container): a local Flink.
             "resources": {"requests": {"cpu": "500m", "memory": "3Gi"}, "limits": {"memory": "3Gi"}},
             "volumeMounts": self.mounts()}
        return self.pod(name, [c], [self.fetch(FLINK_CELL)], [token], deadline)

    # --- Spark ------------------------------------------------------------------------------------------

    def spark(self, name, main, args, token, deadline=3600, cores=2, memory="2Gi", heap="1g", cpu="500m"):
        trust = self.trust_opts("spark")
        script = ("trust_name=spark; . /uat/truststore.sh; "
                  f"jars=$(find /jars -name '*.jar' | sort | paste -sd, -); "
                  f"exec /opt/spark/bin/spark-submit --master 'local[{cores}]' --driver-memory {heap} "
                  "--conf spark.ui.enabled=false --conf spark.local.dir=/tmp/spark "
                  f"--conf 'spark.driver.extraJavaOptions={trust} -Dlog4j.configurationFile=/uat/log4j2.properties' "
                  f"--jars \"$jars\" --class {main} /uat/uat-spark.jar " + " ".join(f"'{a}'" for a in args))
        c = {"name": "spark", "image": self.image("spark-3.5"), "command": ["bash", "-c", script],
             "securityContext": hardened(SPARK_UID), "env": self.env(token) + [{"name": "SPARK_USER", "value": "spark"}],
             "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": memory}},
             "volumeMounts": self.mounts()}
        return self.pod(name, [c], [self.fetch(SPARK_CELL)], [token], deadline)

    # --- Doris ------------------------------------------------------------------------------------------

    def doris(self, name, plan, token, password_key, rounds=1, round_seconds=0, deadline=3600, fe_memory="2Gi",
              be_memory="2560Mi", cpu="500m", request=None):
        """The matrix's Doris reader (its FE and BE images, engine/reader.py), on the plan's tables,
        rounds times in the same catalog. Doris runs as root in its images: the namespace must be at
        the baseline pod security level. request: the memory each container asks the scheduler for, when
        less than its limit (beside other engines on small nodes)."""
        env = [{"name": "COMPAT_READER_PLAN", "value": json.dumps(plan, sort_keys=True)},
               {"name": "COMPAT_TOKEN_FILE", "value": f"/secret/{token}"},
               {"name": "COMPAT_ENGINE_PASSWORD_FILE", "value": f"/secret/{password_key}"},
               {"name": "COMPAT_READER_ROUNDS", "value": str(rounds)},
               {"name": "COMPAT_READER_ROUND_SECONDS", "value": str(round_seconds)}]
        root = {"runAsUser": 0, "runAsGroup": 0}
        fe, be = (self.image(i) for i in self.cell(DORIS_CELL)["images"])
        containers = [
            {"name": "fe", "image": fe, "command": ["bash", "/compat/doris-fe.sh"], "env": env, "securityContext": root,
             "resources": {"requests": {"cpu": cpu, "memory": request or fe_memory}, "limits": {"memory": fe_memory}},
             "volumeMounts": self.mounts()},
            {"name": "be", "image": be, "command": ["bash", "/compat/doris-be.sh"], "env": [], "securityContext": root,
             "resources": {"requests": {"cpu": cpu, "memory": request or be_memory}, "limits": {"memory": be_memory}},
             "volumeMounts": self.mounts()}]
        return self.pod(name, containers, [], [token, password_key], deadline)


def steps(text):
    """{check: (ok, detail)} from a flow's STEP lines; the last line of a check counts."""
    out = {}
    for line in text.splitlines():
        if line.startswith("STEP "):
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                out[parts[1]] = (parts[2] == "OK", parts[3] if len(parts) > 3 else "")
    return out
