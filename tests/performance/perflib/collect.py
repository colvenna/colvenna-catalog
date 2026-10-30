"""What a run records besides the load tool's own data (terms.md section 3, "Raw data"): the
server's access log, the audit export, Lambda's REPORT lines and the gateway's access log, the
database's CloudWatch metrics, the catalog's own metrics, and the environment record.

The EKS form's access log is read from the catalog pods while the load runs (`AccessLog`): the
kubelet rotates a container's log at `containerLogMaxSize`, 10 MiB by default (the kubelet's
configuration reference, KubeletConfiguration), and `kubectl logs` reads the current file only, so
the log is followed every 20 seconds, never read once at the end. The serverless form's is read
from the server function's log group afterwards."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from . import raw
from .context import iso

from uatlib.catalog import catalog_pods, db_instance  # noqa: E402
from uatlib.install import METRICS_PORT  # noqa: E402

# The REST API's stage (deploy/aws/src/serverless-stack.ts, STAGE).
STAGE = "catalog"
# A design choice of the harness: how long it waits for the functions' log lines to reach CloudWatch
# Logs before it reads them (UAT's uatlib/catalog.py waits 60 s).
LOG_ARRIVAL = 90


class AccessLog:
    """The EKS form's catalog logs, followed from the start of a window to its end: every line of
    every catalog pod, prefixed by the pod's name, with the kubelet's timestamp."""

    def __init__(self, target, every=20):
        self.target = target
        self.every = every
        self.lines = {}  # pod -> {timestamped line}
        self.order = {}  # pod -> [lines]
        self.since = {}
        self._stop = threading.Event()
        self._thread = None
        self.errors = []

    def start(self, since=None):
        if self.target.form != "eks":
            return self
        start = since or datetime.now(timezone.utc) - timedelta(seconds=5)
        for p in catalog_pods(self.target):
            self.since[p["metadata"]["name"]] = start
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _poll(self):
        kube, inst = self.target.kube, self.target.install
        for p in catalog_pods(self.target):
            name = p["metadata"]["name"]
            since = self.since.get(name) or datetime.now(timezone.utc) - timedelta(seconds=60)
            out = kube.kubectl("logs", name, "-n", inst.namespace, "-c", "colvenna-catalog", "--timestamps",
                               "--since-time", since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), check=False, timeout=300)
            if out.returncode != 0:
                self.errors.append(f"{name}: {out.stderr.strip()[-200:]}")
                continue
            seen = self.lines.setdefault(name, set())
            order = self.order.setdefault(name, [])
            last = since
            for line in out.stdout.splitlines():
                if line and line not in seen:
                    seen.add(line)
                    order.append(line)
                stamp = line.split(" ", 1)[0]
                try:
                    last = max(last, datetime.fromisoformat(stamp[:26].rstrip("Z") + "+00:00"))
                except ValueError:
                    pass
            # The next read starts a second back: lines of that second are read again and kept once.
            self.since[name] = last - timedelta(seconds=1)

    def _loop(self):
        while not self._stop.wait(self.every):
            try:
                self._poll()
            except Exception as e:  # noqa: BLE001 - recorded; the next poll tries again
                self.errors.append(f"{type(e).__name__}: {e}")

    def stop(self):
        """Reads once more and stops; returns the text: `<pod> <timestamp> <line>` lines."""
        if self.target.form != "eks":
            return ""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=600)
        try:
            self._poll()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{type(e).__name__}: {e}")
        return "\n".join(f"{pod} {line}" for pod, lines in self.order.items() for line in lines) + "\n"


def log_events(target, group, since, until, pattern=None):
    """A log group's events from since to until (epoch ms), every page (the CLI follows them)."""
    args = ["logs", "filter-log-events", "--log-group-name", group, "--start-time", str(int(since)),
            "--end-time", str(int(until))]
    if pattern:
        args += ["--filter-pattern", pattern]
    p = target.aws.raw(*args, "--output", "json", timeout=1800, check=False)
    if p.returncode != 0:
        if "ResourceNotFoundException" in p.stderr:
            return []
        raise RuntimeError(f"filter-log-events {group}: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout or "{}").get("events", [])


def function_name(target, part):
    """The serverless install's function of the part: Server, Authorizer, Purge, Ops."""
    return target.install.output("Serverless", f"{part}FunctionName")


def api_name(target):
    """The serverless install's REST API (deploy/aws/src/serverless-stack.ts: named('-api'))."""
    return f"colvenna-catalog-{target.install.env}-api"


def driver_name(target):
    """The serverless test driver's function (tests/serverless/run.sh)."""
    return f"colvenna-catalog-{target.install.env}-test-driver"


def server_log(target, since, until, wait=True):
    """The serverless form's server function's access lines from since to until (epoch ms)."""
    if wait:
        time.sleep(max(0, LOG_ARRIVAL - (time.time() * 1000 - until) / 1000))
    events = log_events(target, f"/aws/lambda/{function_name(target, 'Server')}", since, until, '"colvenna.access"')
    return "\n".join(e["message"].rstrip("\n") for e in events) + "\n"


def reports(target, part, since, until):
    """REPORT, INIT_REPORT and RESTORE_REPORT lines of a function (Server or Authorizer)."""
    return log_events(target, f"/aws/lambda/{function_name(target, part)}", since, until,
                      "?REPORT ?INIT_REPORT ?RESTORE_REPORT")


def gateway_log(target, since, until):
    """The REST API's access log (JSON lines, one per request the gateway answered)."""
    group = f"/aws/apigateway/{api_name(target)}"
    out = []
    for e in log_events(target, group, since, until):
        try:
            entry = json.loads(e["message"])
        except ValueError:
            continue
        entry["_t"] = e["timestamp"]
        out.append(entry)
    return out


def audit(target, since, until, principals, action=None):
    """The audit events of the principals from since to until (datetimes), oldest first."""
    events = []
    for principal in principals:
        query = {"principal": principal}
        if action:
            query["action"] = action
        events += target.admin.audit_all(since, until, **query)
    return events


# --- CloudWatch metrics --------------------------------------------------------------------------

def _metric(target, namespace, name, dimensions, since, until, stats=("Average", "Maximum"), period=60):
    out = target.aws("cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", name,
                     "--dimensions", *[f"Name={k},Value={v}" for k, v in dimensions.items()],
                     "--start-time", iso(since), "--end-time", iso(until), "--period", str(period),
                     "--statistics", *stats)
    points = sorted(out.get("Datapoints", []), key=lambda d: d["Timestamp"])
    return [{"t": d["Timestamp"], **{s: d.get(s) for s in stats}} for d in points]


def database_metrics(target, since, until):
    """The metadata database's CPU, burst credits, connections and IO latency through the window
    (one-minute points), and its class: recorded for every run (terms.md section 2)."""
    db = db_instance(target)
    if db is None:
        return {"error": "no RDS instance at the install's database endpoint"}
    dims = {"DBInstanceIdentifier": db["DBInstanceIdentifier"]}
    since, until = since - timedelta(minutes=2), until + timedelta(minutes=2)
    out = {"class": db.get("DBInstanceClass"), "engine": f"{db.get('Engine')} {db.get('EngineVersion')}",
           "multiAz": db.get("MultiAZ"), "storage": f"{db.get('StorageType')} {db.get('AllocatedStorage')} GB"}
    for name in ("CPUUtilization", "CPUCreditBalance", "CPUCreditUsage", "DatabaseConnections", "ReadLatency",
                 "WriteLatency", "FreeableMemory"):
        out[name] = _metric(target, "AWS/RDS", name, dims, since, until)
    return out


def database(target):
    """The metadata database's class, engine, availability and storage (RDS)."""
    db = db_instance(target)
    if db is None:
        return {"error": "no RDS instance at the install's database endpoint"}
    return {"class": db.get("DBInstanceClass"), "engine": f"{db.get('Engine')} {db.get('EngineVersion')}",
            "multiAz": db.get("MultiAZ"), "storage": f"{db.get('StorageType')} {db.get('AllocatedStorage')} GB"}


def peak(series, stat="Maximum"):
    values = [p.get(stat) for p in series or [] if p.get(stat) is not None]
    return max(values) if values else None


def lowest(series, stat="Average"):
    values = [p.get(stat) for p in series or [] if p.get(stat) is not None]
    return min(values) if values else None


def function_metrics(target, since, until):
    """The serverless form's server function (its alias live) and REST API, from CloudWatch."""
    fn = function_name(target, "Server")
    since, until = since - timedelta(minutes=1), until + timedelta(minutes=1)
    out = {}
    for name, stats in (("Invocations", ("Sum",)), ("Duration", ("Average", "Maximum")),
                        ("ConcurrentExecutions", ("Maximum",)), ("Throttles", ("Sum",)), ("Errors", ("Sum",))):
        out[f"server.{name}"] = _metric(target, "AWS/Lambda", name, {"FunctionName": fn, "Resource": f"{fn}:live"},
                                        since, until, stats)
    api = api_name(target)
    for name, stats in (("Count", ("Sum",)), ("Latency", ("Average", "Maximum")),
                        ("IntegrationLatency", ("Average", "Maximum")), ("4XXError", ("Sum",)), ("5XXError", ("Sum",))):
        out[f"api.{name}"] = _metric(target, "AWS/ApiGateway", name, {"ApiName": api, "Stage": STAGE}, since,
                                     until, stats)
    return out


def total(series, stat="Sum"):
    return sum(p.get(stat) or 0 for p in series or [])


# --- the catalog's own metrics (EKS) ---------------------------------------------------------------

def catalog_metrics(target, monitor):
    """Each catalog pod's /metrics text, from a probe in a namespace with the monitoring label."""
    if target.form != "eks":
        return {}
    steps = [{"name": p["metadata"]["name"], "url": f"http://{p['status']['podIP']}:{METRICS_PORT}/metrics",
              "full": True} for p in catalog_pods(target)]
    results = monitor.run(steps)
    return {r["name"]: r.get("body") if isinstance(r.get("body"), str) else json.dumps(r.get("body"))
            for r in results}


def counters(text, prefix):
    """{metric line's name and labels: value} of the metrics text's lines starting with prefix."""
    out = {}
    for line in (text or "").splitlines():
        if line.startswith(prefix):
            key, _, value = line.rpartition(" ")
            try:
                out[key] = float(value)
            except ValueError:
                continue
    return out


def metrics_delta(before, after, prefix):
    """{counter: after - before} summed over the pods, of the counters starting with prefix."""
    out = {}
    for pod, text in after.items():
        b = counters(before.get(pod, ""), prefix)
        for k, v in counters(text, prefix).items():
            out[k] = out.get(k, 0) + v - b.get(k, 0)
    return {k: v for k, v in sorted(out.items()) if v}


# --- the environment record --------------------------------------------------------------------------

def nodes(kube):
    """The cluster's nodes: name, node group, instance type, zone, kubelet and OS, capacity."""
    out = []
    for n in kube.json("get", "nodes")["items"]:
        labels = n["metadata"].get("labels", {})
        info = n["status"].get("nodeInfo", {})
        out.append({"name": n["metadata"]["name"], "pool": labels.get("colvenna-catalog/test-pool"),
                    "nodegroup": labels.get("eks.amazonaws.com/nodegroup"),
                    "instanceType": labels.get("node.kubernetes.io/instance-type"),
                    "zone": labels.get("topology.kubernetes.io/zone"), "kubelet": info.get("kubeletVersion"),
                    "os": info.get("osImage"), "cpu": n["status"].get("capacity", {}).get("cpu"),
                    "memory": n["status"].get("capacity", {}).get("memory"),
                    "allocatableCpu": n["status"].get("allocatable", {}).get("cpu")})
    return out


def cluster(target):
    name = target.runner.testenv.cluster
    c = target.aws("eks", "describe-cluster", "--name", name)["cluster"]
    return {"version": c.get("version"), "platformVersion": c.get("platformVersion")}
