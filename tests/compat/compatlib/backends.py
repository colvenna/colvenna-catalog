"""Where a cell's containers run: Docker on the host (any host that reaches the catalog) or pods
in a Kubernetes cluster (the EKS form's own cluster, say). Both run the same Pod: init containers
one after the other, then containers sharing a network (the first one's), the same mounts, and a
main container whose end ends the pod:

  /jars    the cell's Paimon jars, checked against their SHA-256 (read-only)
  /compat  the flows, the engine scripts and their log configuration (read-only)
  /secret  only the secret files the pod needs: a token, the engine's password, StarRocks's own
           S3 properties (read-only)
  /ca      the catalog's CA certificate, when the run has one (read-only)
  /work    scratch space shared by the pod's containers (trust stores)

Nothing a pod runs listens beyond it: Docker containers join a bridge network by default, with no
port published, and the Kubernetes namespace admits no ingress at all.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LABEL = "app.kubernetes.io/part-of"
PART_OF = "colvenna-compat"


@dataclass
class Container:
    name: str
    image: str
    command: list
    env: dict = field(default_factory=dict)
    user: str = ""  # uid:gid, or "" for the image's
    cpu: str = "500m"  # Kubernetes request
    memory: str = "2Gi"  # Kubernetes request and limit; Docker's limit
    main: bool = False


@dataclass
class Pod:
    name: str
    containers: list
    jars: list = field(default_factory=list)
    secrets: list = field(default_factory=list)  # file names in the run's secret directory
    init: list = field(default_factory=list)  # containers that run to their end first, in order
    deadline: int = 1800  # seconds the main container may run


@dataclass
class PodResult:
    name: str
    exit_code: int  # the main container's; -1 when it never ended (deadline) or never started
    logs: dict  # container name -> its output
    started: datetime
    finished: datetime
    problem: str = ""  # why the pod did not run as it should, when it did not


def now():
    return datetime.now(timezone.utc)


def run(args, input=None, check=True, timeout=None, env=None):
    p = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       env=env, universal_newlines=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} ... failed ({p.returncode}): {p.stderr.strip()[-2000:]}")
    return p


def fetch_jar(cache, maven, name, jar):
    """The jar in the cache, downloaded from the Maven repository first if needed; refused unless
    its SHA-256 is the pinned one."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / name
    if not path.exists() or sha256(path) != jar["sha256"]:
        tmp = path.with_suffix(".part")
        try:
            # Whatever the repository's scheme, the jar is used only if its SHA-256 is the pinned one.
            with urllib.request.urlopen(f"{maven}/{jar['path']}", timeout=300) as response, open(tmp, "wb") as out:  # nosec B310
                shutil.copyfileobj(response, out)
            if sha256(tmp) != jar["sha256"]:
                raise RuntimeError(f"{name}: the download's SHA-256 is not the pinned {jar['sha256']}")
            os.chmod(tmp, 0o644)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_root(user):
    return user.split(":")[0] == "0"


class Docker:
    """Containers on this host's Docker. Every container of a pod joins the first one's network
    namespace; the first one joins `network` (default bridge: no port of the engines is reachable
    from beyond the host, and none is published). add_hosts are name:address entries for every
    container, so that a catalog on the host (a port-forward bound to the bridge's gateway) is
    reached by a name its certificate holds."""

    kind = "docker"
    identity = "Docker containers with no AWS variables and the instance metadata service disabled"

    def __init__(self, cells_config, run_id, files_dir, secret_dir, ca_file, jar_cache, network="bridge",
                 volumes=(), user=None, add_hosts=()):
        """volumes: extra host:container mounts for every container (a local warehouse, say); user:
        a uid:gid the writers run as instead of their image's (for such a warehouse's files)."""
        self.config = cells_config
        self.run_id = run_id
        self.files_dir, self.secret_dir, self.ca_file = files_dir, secret_dir, ca_file
        self.jar_cache = jar_cache
        self.network = network
        self.volumes = list(volumes)
        self.user = user
        self.add_hosts = list(add_hosts)

    def prepare(self):
        run(["docker", "info"], timeout=60)

    def cleanup(self):
        # Whatever a failed or stopped pod left, by the run's label.
        left = run(["docker", "ps", "-aq", "--filter", f"label=colvenna-compat={self.run_id}"], check=False).stdout.split()
        if left:
            run(["docker", "rm", "-f", *left], check=False)

    def args(self, pod, c, network, mounts):
        args = ["docker", "run", "-d", "--name", f"{pod.name}-{c.name}", "--network", network,
                "--label", f"colvenna-compat={self.run_id}",
                "--memory", c.memory.replace("Gi", "g").replace("Mi", "m"), "--entrypoint", c.command[0]]
        user = self.user if self.user and not is_root(c.user) else c.user
        if user:
            args += ["--user", user]
        if not network.startswith("container:"):
            for host in self.add_hosts:
                args += ["--add-host", host]
        # No AWS identity of the host's: no AWS variables are passed, and the SDKs are told not to
        # ask the instance metadata service, which a container on the host's network would reach.
        for k, v in dict(c.env, AWS_EC2_METADATA_DISABLED="true").items():
            args += ["-e", f"{k}={v}"]
        return args + mounts + [c.image] + c.command[1:]

    def run_pod(self, pod):
        started = now()
        every = pod.init + pod.containers
        names = {c.name: f"{pod.name}-{c.name}" for c in every}
        for name in names.values():
            run(["docker", "rm", "-f", name], check=False)
        logs, exit_code, problem = {}, -1, ""
        # Inside the runner's 0700 scratch directory, which no other user of this host can enter;
        # writable by the pod's containers, whose users differ, each keeping its own files.
        work = tempfile.mkdtemp(prefix="work-", dir=self.files_dir.parent)
        os.chmod(work, 0o1777)  # nosec B103
        try:
            mounts = []
            for jar in pod.jars:
                path = fetch_jar(self.jar_cache, self.config["mavenRepository"], jar, self.config["jars"][jar])
                mounts += ["-v", f"{path}:/jars/{jar}:ro"]
            mounts += ["-v", f"{self.files_dir}:/compat:ro"]
            for secret in pod.secrets:
                mounts += ["-v", f"{self.secret_dir / secret}:/secret/{secret}:ro"]
            if self.ca_file:
                mounts += ["-v", f"{self.ca_file}:/ca/ca.crt:ro"]
            mounts += ["-v", f"{work}:/work"]
            for volume in self.volumes:
                mounts += ["-v", volume]
            for c in pod.init:
                run(self.args(pod, c, self.network, mounts), timeout=1800)
                run(["docker", "wait", names[c.name]], timeout=pod.deadline)
            first = names[pod.containers[0].name]
            for i, c in enumerate(pod.containers):
                run(self.args(pod, c, self.network if i == 0 else f"container:{first}", mounts), timeout=1800)
            main = next(c for c in pod.containers if c.main)
            try:
                p = run(["docker", "wait", names[main.name]], timeout=pod.deadline)
                exit_code = int(p.stdout.strip() or -1)
            except subprocess.TimeoutExpired:
                problem = f"the main container ran past its {pod.deadline}s deadline"
                run(["docker", "kill", names[main.name]], check=False)
        except Exception as e:  # noqa: BLE001 - the cell records why it could not run
            problem = problem or str(e)
        finally:
            for c in every:
                p = run(["docker", "logs", names[c.name]], check=False)
                if p.returncode == 0 or p.stdout or p.stderr:
                    logs[c.name] = (p.stdout or "") + (p.stderr or "")
                run(["docker", "rm", "-f", names[c.name]], check=False)
            shutil.rmtree(work, ignore_errors=True)
        return PodResult(pod.name, exit_code, logs, started, now(), problem)


def placement(selector=None, toleration=None):
    """The engines' pods' nodeSelector (from "key=value") and tolerations (from "key=value:Effect"), as
    much as is given; {} for neither."""
    out = {}
    if selector:
        key, sep, value = selector.partition("=")
        if not (key and sep and value):
            raise ValueError(f"--node-selector: key=value, not {selector!r}")
        out["nodeSelector"] = {key: value}
    if toleration:
        key, sep, rest = toleration.partition("=")
        value, colon, effect = rest.partition(":")
        if not (key and sep and value and colon and effect in ("NoSchedule", "PreferNoSchedule", "NoExecute")):
            raise ValueError(f"--toleration: key=value:NoSchedule (or PreferNoSchedule, NoExecute), not {toleration!r}")
        out["tolerations"] = [{"key": key, "operator": "Equal", "value": value, "effect": effect}]
    return out


class Kubernetes:
    """Pods in a namespace of their own in the cluster kubectl reaches: the namespace carries the
    label the catalog's NetworkPolicy admits, admits no ingress itself, and its pods have no AWS
    identity of their own (the default service account, no token). The jars come from the Maven
    repository through an init container that checks each one's SHA-256. The runner makes the
    namespace and deletes it; a namespace it did not make is refused."""

    kind = "kubernetes"
    identity = ("pods of the namespace's default service account (no IRSA role, no service account token): "
                "the node's own role is all they could reach")

    def __init__(self, cells_config, files_dir, secret_dir, ca_file, namespace, client_label, kubeconfig=None,
                 keep=False, placement=None):
        self.config = cells_config
        self.files_dir, self.secret_dir, self.ca_file = files_dir, secret_dir, ca_file
        self.namespace, self.client_label = namespace, client_label
        self.keep = keep
        self.placement = placement or {}  # the pods' nodeSelector and tolerations (placement())
        self.made = False
        self.env = dict(os.environ)
        if kubeconfig:
            self.env["KUBECONFIG"] = str(kubeconfig)

    def kubectl(self, *args, input=None, check=True, timeout=300):
        return run(["kubectl", *args], input=input, check=check, timeout=timeout, env=self.env)

    def apply(self, manifest):
        self.kubectl("apply", "-f", "-", input=json.dumps(manifest))

    def prepare(self):
        existing = self.kubectl("get", "namespace", self.namespace, "-o", "json", check=False)
        if existing.returncode == 0:
            labels = json.loads(existing.stdout).get("metadata", {}).get("labels", {})
            if labels.get(LABEL) != PART_OF:
                raise RuntimeError(f"namespace {self.namespace} exists and is not the matrix's ({LABEL}={PART_OF}): "
                                   "refusing to use it, and so to delete it")
        key, _, value = self.client_label.partition("=")
        # Doris and StarRocks run as root in their official images: baseline, not restricted.
        self.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace, "labels": {
            key: value, "pod-security.kubernetes.io/enforce": "baseline", LABEL: PART_OF}}})
        self.made = True
        # The engines open ports (Doris's and StarRocks's frontends and backends among them) that
        # nothing outside their own pod has any business reaching.
        self.apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": {"name": "no-ingress", "namespace": self.namespace},
                    "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}})
        self.update_files()
        secret = self.kubectl("create", "secret", "generic", "compat-secret", "-n", self.namespace,
                              f"--from-file={self.secret_dir}", "--dry-run=client", "-o", "json").stdout
        self.kubectl("apply", "-f", "-", input=secret)
        if self.ca_file:
            ca = self.kubectl("create", "configmap", "compat-ca", "-n", self.namespace,
                              f"--from-file=ca.crt={self.ca_file}", "--dry-run=client", "-o", "json").stdout
            self.kubectl("apply", "-f", "-", input=ca)

    def update_files(self):
        files = self.kubectl("create", "configmap", "compat-files", "-n", self.namespace,
                             f"--from-file={self.files_dir}", "--dry-run=client", "-o", "json").stdout
        self.kubectl("apply", "-f", "-", input=files)

    def cleanup(self):
        if self.made and not self.keep:
            self.kubectl("delete", "namespace", self.namespace, "--ignore-not-found", "--wait=true",
                         "--timeout=10m", check=False, timeout=900)

    def container(self, c, mounts):
        hardened = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
        spec = {"name": c.name, "image": c.image, "command": c.command,
                "env": [{"name": k, "value": v} for k, v in c.env.items()],
                "resources": {"requests": {"cpu": c.cpu, "memory": c.memory}, "limits": {"memory": c.memory}},
                "volumeMounts": mounts}
        if c.user:
            uid, _, gid = c.user.partition(":")
            if is_root(c.user):
                # Doris and StarRocks: their images' scripts run as root.
                spec["securityContext"] = {"runAsUser": 0, "runAsGroup": int(gid or 0)}
            else:
                spec["securityContext"] = dict(hardened, runAsUser=int(uid), runAsGroup=int(gid or uid),
                                               runAsNonRoot=True)
        return spec

    def manifest(self, pod):
        maven = self.config["mavenRepository"]
        fetch = ["set -eu", "cd /jars"]
        for jar in pod.jars:
            spec = self.config["jars"][jar]
            fetch.append(f"curl -fsSL --retry 5 --retry-delay 3 -o '{jar}' '{maven}/{spec['path']}'")
            fetch.append(f"echo '{spec['sha256']}  {jar}' | sha256sum -c -")
        hardened = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
        mounts = [{"name": "jars", "mountPath": "/jars", "readOnly": True},
                  {"name": "compat", "mountPath": "/compat", "readOnly": True},
                  {"name": "secret", "mountPath": "/secret", "readOnly": True},
                  {"name": "work", "mountPath": "/work"}]
        volumes = [{"name": "jars", "emptyDir": {}},
                   {"name": "compat", "configMap": {"name": "compat-files", "defaultMode": 0o555}},
                   # Only the files this pod needs, of the run's secrets.
                   {"name": "secret", "secret": {"secretName": "compat-secret", "defaultMode": 0o444,  # pragma: allowlist secret - the name of the Kubernetes Secret the pod mounts, not a value
                                                 "items": [{"key": s, "path": s} for s in pod.secrets]}},
                   {"name": "work", "emptyDir": {}}]
        if self.ca_file:
            mounts.append({"name": "ca", "mountPath": "/ca", "readOnly": True})
            volumes.append({"name": "ca", "configMap": {"name": "compat-ca"}})
        init = [{
            "name": "jars", "image": self.config["images"]["curl"], "command": ["sh", "-c", "\n".join(fetch)],
            "securityContext": dict(hardened, runAsUser=100, runAsGroup=101, runAsNonRoot=True),
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "volumeMounts": [{"name": "jars", "mountPath": "/jars"}]}]
        init += [self.container(c, mounts) for c in pod.init]
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod.name, "namespace": self.namespace, "labels": {LABEL: PART_OF}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": pod.deadline + 900,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": init,
                "containers": [self.container(c, mounts) for c in pod.containers],
                "volumes": volumes,
                **self.placement,
            },
        }

    def run_pod(self, pod):
        started = now()
        self.kubectl("delete", "pod", pod.name, "-n", self.namespace, "--ignore-not-found", "--wait=true", check=False)
        main = next(c for c in pod.containers if c.main)
        exit_code, problem, logs = -1, "", {}
        try:
            self.apply(self.manifest(pod))
            # Image pulls and the jars' download come before the main container's deadline.
            deadline = time.time() + pod.deadline + 900
            last = ""
            while time.time() < deadline:
                status = json.loads(self.kubectl("get", "pod", pod.name, "-n", self.namespace, "-o", "json").stdout)
                phase = status.get("status", {}).get("phase", "")
                states = {s["name"]: s.get("state", {}) for s in status.get("status", {}).get("containerStatuses", [])}
                term = states.get(main.name, {}).get("terminated")
                if term:
                    exit_code = term.get("exitCode", -1)
                    break
                init = {s["name"]: s.get("state", {}) for s in status.get("status", {}).get("initContainerStatuses", [])}
                failed = [n for n, s in init.items() if s.get("terminated", {}).get("exitCode", 0) != 0]
                if failed:
                    problem = f"the init container {failed[0]} failed" + (
                        ": the jars could not be fetched or did not match their SHA-256" if failed[0] == "jars" else "")
                    break
                if phase in ("Failed", "Succeeded"):
                    problem = f"the pod ended ({phase}) before its main container did"
                    break
                waiting = [f"{n}: {s['waiting'].get('reason')}" for n, s in {**init, **states}.items()
                           if s.get("waiting")]
                conditions = [c.get("message", "") for c in status.get("status", {}).get("conditions", [])
                              if c.get("status") == "False" and c.get("message")]
                last = "; ".join(waiting + conditions) or phase
                if any("ErrImagePull" in w or "ImagePullBackOff" in w or "CreateContainerConfigError" in w
                       for w in waiting) and time.time() - started.timestamp() > 900:
                    problem = f"the pod's containers did not start: {last}"
                    break
                time.sleep(5)
            else:
                problem = f"the main container did not end within {pod.deadline + 900}s (last: {last})"
        except Exception as e:  # noqa: BLE001 - the cell records why it could not run
            problem = problem or str(e)
        finally:
            for name in ["jars"] + [c.name for c in pod.init + pod.containers]:
                p = self.kubectl("logs", pod.name, "-n", self.namespace, "-c", name, check=False, timeout=120)
                logs[name] = p.stdout if p.returncode == 0 else p.stderr
            self.kubectl("delete", "pod", pod.name, "-n", self.namespace, "--ignore-not-found", "--wait=true",
                         "--timeout=5m", check=False, timeout=400)
        return PodResult(pod.name, exit_code, logs, started, now(), problem)


class ServerLog:
    """Where the catalog's access log is read from: kubectl:<namespace>:<label selector> (the
    catalog's pods, every replica), command:<shell command> (given COMPAT_SINCE and COMPAT_UNTIL in
    its environment, prints the log lines: CloudWatch Logs for the serverless form, say), or none.
    A log that cannot be read in full raises: a partial log is no evidence."""

    def __init__(self, spec, kubeconfig=None, delay=0):
        self.spec = spec or "none"
        self.delay = delay
        self.env = dict(os.environ)
        if kubeconfig:
            self.env["KUBECONFIG"] = str(kubeconfig)

    @property
    def available(self):
        return self.spec != "none"

    def read(self, since, until):
        if self.spec == "none":
            return None
        if self.delay:
            time.sleep(self.delay)  # a log service's ingestion lag, say
        if self.spec.startswith("kubectl:"):
            _, namespace, selector = self.spec.split(":", 2)
            pods = run(["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o",
                        "jsonpath={.items[*].metadata.name}"], env=self.env, timeout=120).stdout.split()
            if not pods:
                raise RuntimeError(f"no catalog pod matches {selector} in {namespace}")
            text = []
            for pod in pods:
                # Every replica's, all of it since the cell began: one unread replica fails the read.
                p = run(["kubectl", "logs", pod, "-n", namespace, "--all-containers", "--since-time",
                         (since.replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%SZ")], env=self.env, timeout=300)
                text.append(p.stdout)
            return "\n".join(text)
        if self.spec.startswith("command:"):
            env = dict(self.env, COMPAT_SINCE=since.isoformat(), COMPAT_UNTIL=until.isoformat())
            # --server-log command:<command>: the operator's own shell command, run as the operator.
            return subprocess.run(self.spec[len("command:"):], shell=True, env=env, stdout=subprocess.PIPE,  # nosec B602
                                  universal_newlines=True, timeout=600, check=True).stdout
        raise ValueError(f"--server-log: kubectl:<namespace>:<selector>, command:<command> or none, not {self.spec}")


def write_mounted(path, text):
    """A file a container's user can read once mounted (0644). Secrets go only into a directory
    inside one that only this user can enter (the runner's 0700 scratch directory)."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o644)
