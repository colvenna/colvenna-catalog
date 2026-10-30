"""One run of one scenario: its id, evidence directory and log, the test environment's cluster, the
installs under load (one, or both forms for P5), each with its clients' namespace, probe pod and admin
API, and its clean-up, verified.

  <out>/<scenario>[-<n>]/run.log          what the run did, in order
  <out>/<scenario>[-<n>]/result.json      the judged result (perflib/scenarios.py), the environment,
                                          the load's parameters, the clean-up and its verification
  <out>/<scenario>[-<n>]/summary.md       the result as the performance document's tables
  <out>/<scenario>[-<n>]/<form>/...       the raw data of each install (README.md lists them)

Everything kept goes through the compatibility matrix's sanitizer, with the run's tokens: no token,
password, account ID, ARN, address or bucket is kept. A rerun is <scenario>-2, -3, ...; an earlier
run's evidence is never written over."""

import json
import os
import secrets
import shutil
import signal
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import REPO

from uatlib.admin import Admin  # noqa: E402
from uatlib.aws import Aws  # noqa: E402
from uatlib.install import Install, TestEnvironment  # noqa: E402
from uatlib.kube import Kube  # noqa: E402
from uatlib.probe import Probe  # noqa: E402
from uatlib.run import RunSanitizer, evidence_dir, git_head  # noqa: E402

PART_OF = ("app.kubernetes.io/part-of", "colvenna-perf")
RUN_LABEL = "colvenna-perf/run"
PURPOSE = "performance acceptance (docs/performance/terms.md)"
APPROVER = "perf-operator@example.com"
SHORT = {"eks": "ek", "serverless": "sl"}

# The pinned kubectl and Helm of scripts/install-tools.sh, before any other on PATH.
if (REPO / ".tools").is_dir():
    os.environ["PATH"] = f"{REPO / '.tools'}{os.pathsep}{os.environ.get('PATH', '')}"


def run_id():
    """Letters and digits only (database names, principals and namespaces take it), unique per run."""
    return "p" + datetime.now(timezone.utc).strftime("%y%m%d%H%M") + secrets.token_hex(2)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rounded(value, digits=6):
    """The value with every float rounded to `digits` decimals. The sanitizer takes twelve digits
    standing alone for an account ID, and a float's fraction can be exactly twelve digits long
    (910.123456789012): unrounded, the evidence's JSON would get a placeholder for a number."""
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: rounded(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [rounded(v, digits) for v in value]
    return value


def dumps(value):
    """Evidence as JSON: floats rounded (rounded()), keys sorted unless their types are mixed."""
    value = rounded(value)
    try:
        return json.dumps(value, indent=1, sort_keys=True, default=str)
    except TypeError:  # keys of mixed types cannot be sorted: kept as they are
        return json.dumps(value, indent=1, default=str)


class PerfKube(Kube):
    """The UAT harness's kubectl, with namespaces labelled as the performance run's own."""

    part_of = PART_OF
    run_label = RUN_LABEL
    policy = "perf-ingress"


class PerfAdmin(Admin):
    """The admin API, with grants that name the performance acceptance as their purpose, and the
    audit read back whole."""

    purpose = PURPOSE
    approver = APPROVER

    def audit_all(self, since, until, **query):
        """Every audit event of the query from since to until (datetimes), oldest first: the API
        answers at most 1,000 events, newest first, so the window is read back in pieces, each
        ending at the oldest event of the one before (events at that instant are read twice and
        kept once)."""
        events, seen, upper = [], set(), until
        for _ in range(1000):
            batch = self.audit(since=iso(since), until=iso(upper), limit=1000, **query)
            fresh = [e for e in batch if e.get("id") not in seen]
            for e in fresh:
                seen.add(e.get("id"))
            events += fresh
            if len(batch) < 1000 or not fresh:
                break
            oldest = min(datetime.fromisoformat(e["time"].replace("Z", "+00:00")) for e in batch)
            upper = oldest + timedelta(milliseconds=1)
        return sorted(events, key=lambda e: (e.get("time", ""), e.get("id", 0)))


class Target:
    """One install under load, with what the UAT harness's library asks of a case's context (the
    install, kubectl, AWS, the sanitizer, a scratch directory, a log, namespaces of the run), so that
    its SQL helper and the serverless test driver work as they do there."""

    def __init__(self, run, config):
        self.runner = run
        self.run = run.id
        self.id = run.scenario
        self.aws, self.kube, self.sanitizer, self.scratch = run.aws, run.kube, run.sanitizer, run.scratch
        self.install = Install(config, run.aws, run.kube)
        self.form = self.install.form
        self.short = SHORT[self.form]
        self.evidence = run.evidence / self.form
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.install.refresh()
        run.sanitizer.values.update({self.install.uri: f"<CATALOG_URI_{self.short.upper()}>",
                                     self.install.host: f"<CATALOG_HOST_{self.short.upper()}>"})
        self._probe = None
        self._admin = None
        self._driver = None
        self.monitor = None  # a probe with the monitoring label (EKS), made when the metrics are read
        self.databases = []  # the run's databases in this install, dropped at the clean-up
        self.principals = {}  # role -> name

    # --- what uatlib asks of a context ---------------------------------------------------------------

    def log(self, message):
        self.runner.log(f"[{self.form}] {message}")

    def keep(self, name, text):
        return self.runner.keep(f"{self.form}/{name}", text)

    def name(self, role):
        return f"perf-{self.run}-{self.short}-{role}".lower()

    def namespace(self, role, client=True, monitoring=False, security="restricted", ingress="none", labels=None):
        all_labels = dict(labels or {})
        if self.form == "eks":
            if client:
                key, value = self.install.client_label
                all_labels[key] = value
            if monitoring:
                key, value = self.install.metrics_label
                all_labels[key] = value
        return self.kube.namespace(self.name(role), self.run, all_labels, security, ingress)

    @property
    def driver(self):
        if self._driver is None:
            from uatlib.driver import Driver

            self._driver = Driver(self)
        return self._driver

    @property
    def probe(self):
        if self._probe is None:
            ns = self.namespace("client")
            self._probe = Probe(self.kube, ns, base=self.install.uri, ca=self.install.ca(), evidence=self.evidence,
                                sanitizer=self.sanitizer).start()
        return self._probe

    @property
    def admin(self):
        if self._admin is None:
            self._admin = PerfAdmin(self.probe, self.install.admin_base, self.install.admin_token(), self.id)
            self.sanitizer.secret_sources.append(lambda: list(self._admin.tokens.values()))
        return self._admin

    # --- principals and databases of the run --------------------------------------------------------

    def principal(self, role, grants=(), ttl="PT6H"):
        """A principal of the run (pf<role>-<run>) with a token held under the key role, and grants
        [(scope, privilege, database)]."""
        name = f"pf{role}-{self.run}"
        self.admin.principal(name, description=f"performance run {self.run}: {role}")
        for scope, privilege, database in grants:
            self.admin.grant(name, scope, privilege, database=database)
        self.admin.issue(name, ttl=ttl, key=role)
        self.principals[role] = name
        return name

    def database(self, label):
        name = f"{label}_{self.run}"
        self.databases.append(name)
        return name

    def describe(self):
        return self.install.describe()


class Run:
    """One run of one scenario on one or two installs."""

    def __init__(self, scenario, args):
        self.scenario = scenario
        self.args = args
        self.id = args.run or run_id()
        self.started = now()
        self.aws = Aws(args.region)
        self.scratch = Path(tempfile.mkdtemp(prefix="colvenna-perf-"))
        os.chmod(self.scratch, 0o700)
        self.evidence = evidence_dir(args.out, scenario)
        self.log_file = open(self.evidence / "run.log", "a", encoding="utf-8")
        self.sanitizer = RunSanitizer({})
        self.testenv = TestEnvironment(args.test_env, self.aws)
        self.kube = PerfKube(self.testenv.kubeconfig(self.scratch / "kubeconfig"), log=self.log,
                             placement=self.testenv.client_placement())
        self.targets = [Target(self, c) for c in args.config]
        self.cleanups = []  # (what, function returning (ok, detail)), run last first
        self.harness_commit, self.harness_dirty = git_head()

    def target(self, form):
        return next(t for t in self.targets if t.form == form)

    # --- recording ------------------------------------------------------------------------------------

    def log(self, message):
        line = self.sanitizer.text(f"{now().strftime('%H:%M:%SZ')} {message}")
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def keep(self, name, text):
        path = self.evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(text, str):
            text = dumps(text)
        path.write_text(self.sanitizer.text(text))
        return path

    def on_cleanup(self, what, fn):
        """fn() -> (ok, detail): a step of the clean-up, also its verification."""
        self.cleanups.append((what, fn))

    # --- the end --------------------------------------------------------------------------------------

    def cleanup(self):
        """Every clean-up step, last registered first, then each install's grants and principals, then
        the namespaces; each is tried whatever the others did, and each says whether what it removed
        is gone. Signals meanwhile are noted, not obeyed."""
        steps, noted = [], []
        previous = {s: signal.signal(s, lambda n, f: noted.append(n)) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

        def step(what, fn):
            try:
                ok, detail = fn()
            except BaseException as e:  # noqa: BLE001 - every clean-up is tried
                ok, detail = False, f"{type(e).__name__}: {e}"
            steps.append({"what": what, "status": "PASS" if ok else "FAIL", "detail": self.sanitizer.text(str(detail))})

        try:
            for what, fn in reversed(self.cleanups):
                step(what, fn)
            for t in self.targets:
                if t._admin is not None:
                    step(f"[{t.form}] grants deleted, principals disabled", lambda t=t: admin_cleanup(t))
            step("namespaces deleted", self._namespaces)
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
        if noted:
            steps.append({"what": "signals during the clean-up", "status": "PASS", "detail": f"noted, not obeyed: {noted}"})
        return {"status": "FAIL" if any(s["status"] == "FAIL" for s in steps) else "PASS", "steps": steps}

    def _namespaces(self):
        left = self.kube.delete_namespaces()
        p = self.kube.kubectl("get", "namespaces", "-l", f"{RUN_LABEL}={self.id}", "-o", "name", check=False)
        still = [n for n in p.stdout.split() if n]
        ok = not left and not still and p.returncode == 0
        return ok, f"left: {left + still}" if not ok else "none of the run's namespaces is left"

    def close(self):
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.log_file.close()


def admin_cleanup(target):
    """Deletes the run's grants and disables its principals, then reads both back."""
    admin = target.admin
    problems = admin.cleanup()
    listed = {p.get("name"): p for p in admin.call("GET", "/principals", full=True)["body"].get("principals", [])}
    for name in admin.made_principals:
        left = admin.call("GET", f"/grants?principal={urllib.parse.quote(name)}")["body"].get("grants", [])
        mine = [g for g in left if g.get("id") in admin.granted]
        if mine:
            problems.append(f"{name}: grants left {[g.get('id') for g in mine]}")
        if (listed.get(name) or {}).get("disabledAt") is None:
            problems.append(f"{name}: not disabled")
    return not problems, "; ".join(problems) or (f"{len(admin.granted)} grants deleted, "
                                                  f"{len(admin.made_principals)} principals disabled, read back")


def meta(obj, key):
    """A pod's label or annotation (the chart sets the source commit and the candidate as annotations)."""
    m = obj.get("metadata", {})
    return (m.get("annotations") or {}).get(key) or (m.get("labels") or {}).get(key)


def install_source(target):
    """What the install runs: the commit (and candidate) it was installed from, and its image."""
    inst = target.install
    out = {"env": inst.env, "form": inst.form}
    if inst.form == "eks":
        selector = f"app.kubernetes.io/name=colvenna-catalog,app.kubernetes.io/instance={inst.release}"
        pods = target.kube.json("get", "pods", "-n", inst.namespace, "-l", selector)["items"]
        out["pods"] = [{"name": p["metadata"]["name"], "node": p["spec"].get("nodeName"),
                        "sourceSha": meta(p, "colvenna-catalog/source-sha"),
                        "release": meta(p, "colvenna-catalog/release"),
                        "image": next((c.get("imageID") for c in p["status"].get("containerStatuses", [])
                                       if c["name"] == "colvenna-catalog"), None),
                        "resources": next((c.get("resources") for c in p["spec"]["containers"]
                                           if c["name"] == "colvenna-catalog"), None)} for p in pods]
        out["sourceSha"] = sorted({p["sourceSha"] for p in out["pods"] if p["sourceSha"]})
        out["release"] = sorted({p["release"] for p in out["pods"] if p["release"]})
        deployment = target.kube.json("get", "deployment", inst.release, "-n", inst.namespace)
        config = target.kube.json("get", "configmap", f"{inst.release}-config", "-n", inst.namespace).get("data", {})
        out["replicas"] = deployment["spec"].get("replicas")
        out["jdbcPoolSize"] = config.get("COLVENNA_JDBC_POOL_SIZE")
        out["jdbcAuxPoolSize"] = config.get("COLVENNA_JDBC_AUX_POOL_SIZE")
    else:
        outputs = inst.outputs("Serverless")
        out["sourceSha"] = [outputs.get("SourceSha")]
        out["image"] = outputs.get("ServerImage", "").split("@")[-1]
        fn = outputs.get("ServerFunctionName")
        conf = target.aws("lambda", "get-function-configuration", "--function-name", fn, "--qualifier", "live")
        out["server"] = {"memoryMb": conf.get("MemorySize"), "architectures": conf.get("Architectures"),
                         "version": conf.get("Version"), "snapStart": (conf.get("SnapStart") or {}).get("ApplyOn"),
                         "timeoutS": conf.get("Timeout")}
        conc = target.aws("lambda", "get-function-concurrency", "--function-name", fn) or {}
        out["server"]["reservedConcurrency"] = conc.get("ReservedConcurrentExecutions")
    stacks = target.aws("cloudformation", "describe-stacks", "--stack-name", inst.stack("Storage"))["Stacks"][0]
    out["stackTags"] = {t["Key"]: t["Value"] for t in stacks.get("Tags", []) if t["Key"] in ("SourceSha", "Release")}
    return out
