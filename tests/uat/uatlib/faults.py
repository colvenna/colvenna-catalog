"""What the fault cases (UAT-FLT-01, 02) share: the catalog's replicas and their nodes (EKS), its
execution environments (serverless: a new version of the server function, the alias moved to it,
as a deploy does, and moved back), the table's published snapshot and consistency report, and the
writers and maintenance of tests/uat/paimon run as job pods."""

import json
import time
import urllib.parse

from . import purges
from .lifecycle import stamp
from .tools import checks as tool_checks

SELECTOR = "app.kubernetes.io/name=colvenna-catalog"




class Timeline:
    """The case's faults, each with its time, in timeline.txt and the run's log."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.events = []

    def __call__(self, what, **facts):
        t = time.time()
        self.events.append(dict({"time": stamp(t), "at": t, "what": what}, **facts))
        self.ctx.log(what + (f" {json.dumps(facts, sort_keys=True)}" if facts else ""))
        with open(self.ctx.evidence / "timeline.txt", "a", encoding="utf-8") as f:
            f.write(self.ctx.sanitizer.text(f"{stamp(t)} {what} {json.dumps(facts, sort_keys=True) if facts else ''}\n"))
        return t


# --- the table ---------------------------------------------------------------------------------------

def published(ctx, db, table):
    """The table's snapshot as the catalog serves it (LOAD_SNAPSHOT), or None."""
    path = f"/databases/{urllib.parse.quote(db)}/tables/{urllib.parse.quote(table)}/snapshot"
    r = ctx.rest.call("GET", path, expect=None)
    if r.get("status") != 200:
        return None
    return snapshot_id(r.get("body"))


def snapshot_id(body):
    """The snapshot id of a LOAD_SNAPSHOT answer: Paimon's TableSnapshot, {"snapshot": {..., "snapshot":
    {"id": ...}}}."""
    outer = (body or {}).get("snapshot") or {}
    inner = outer.get("snapshot") if isinstance(outer.get("snapshot"), dict) else outer
    return inner.get("id")


def await_published(ctx, db, table, at_least, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        n = published(ctx, db, table)
        if n is not None and n >= at_least:
            return n
        time.sleep(10)
    return None


def consistency(ctx, db, table):
    """The table's consistency report, its pages followed (the serverless form answers a large table's
    check in pages of 10 s, #69), each waited for longer than the probe's default. An answer other than
    200 (a gateway that gave up, say) is {status, error}, which no judge takes for consistent."""
    r = ctx.admin.consistency_report(db, table, timeout=90)
    if r.get("status") != 200:
        body = r.get("body")
        return {"status": r.get("status", r.get("error")), "requestId": r.get("serverRequestId"),
                "error": body.get("message") if isinstance(body, dict) else body}
    return r["body"]


def consistent(ctx, db, table, tries=15, keep=None):
    """The table's consistency report, asked again between commits until it is consistent."""
    report = {}
    for _ in range(tries):
        report = consistency(ctx, db, table)
        if report.get("consistent") is True:
            break
        time.sleep(2)
    if keep:
        ctx.keep(keep, report)
    return report


def drop_database(ctx, db, tries=8, wait=15):
    """Drops the case's database, asking again for a while: an answer of 500 or 503 just after a fault
    (the alias moved back, a replica restarting) is not the clean-up's end. Each table's drop is timed:
    it answers once the table is gone from the catalog, whatever its files (#69). Then its files are
    looked for, once purged (the serverless form's purge function, run now): any left fail the
    clean-up."""
    last = None
    rest = ctx.rest
    paths, drops = {}, []
    for _ in range(tries):
        try:
            for table in rest.tables(db) if db in rest.databases() else []:
                paths.setdefault(table, (rest.get_table(db, table) or {}).get("path"))
                r = rest.call("DELETE", f"/databases/{urllib.parse.quote(db)}/tables/{urllib.parse.quote(table)}",
                              expect=None, timeout=90)
                drops.append(f"{table} {r.get('status', r.get('error'))} in {r.get('ms')} ms")
            if rest.drop_database(db):
                break
        except Exception as e:  # noqa: BLE001 - asked again
            last = e
        time.sleep(wait)
    else:
        raise RuntimeError(f"database {db} still there: {last}")
    run = purges.now(ctx)
    left = {t: purges.objects_under(ctx, p) for t, p in paths.items()}
    detail = (f"{db} dropped: {'; '.join(drops) or 'no table'}"
              + (f"; purge run {run}" if run is not None else "")
              + f"; objects left under the dropped tables' roots: {left}")
    if any(n for n in left.values()):
        raise RuntimeError(detail)
    return detail


# --- EKS: replicas --------------------------------------------------------------------------------

def replicas(ctx):
    """[(pod, node, ready, phase)] of the catalog's replicas (terminating ones excluded)."""
    pods = ctx.kube.json("get", "pods", "-n", ctx.install.namespace, "-l",
                         f"{SELECTOR},app.kubernetes.io/instance={ctx.install.release}")["items"]
    out = []
    for p in pods:
        if p["metadata"].get("deletionTimestamp"):
            continue
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
        out.append((p["metadata"]["name"], p["spec"].get("nodeName"), ready, p["status"].get("phase")))
    return out


def ready_replicas(ctx):
    d = ctx.kube.json("get", "deployment", ctx.install.release, "-n", ctx.install.namespace)
    return d.get("status", {}).get("readyReplicas") or 0


def await_ready(ctx, timeout=600):
    ctx.kube.kubectl("rollout", "status", f"deployment/{ctx.install.release}", "-n", ctx.install.namespace,
                     f"--timeout={timeout}s", timeout=timeout + 30)
    end = time.time() + timeout
    while time.time() < end:
        if ready_replicas(ctx) >= 2:
            return True
        time.sleep(3)
    return False


def force_delete(ctx, *pods):
    ctx.kube.kubectl("delete", "pod", *pods, "-n", ctx.install.namespace, "--grace-period=0", "--force", check=False)


# --- serverless: execution environments -----------------------------------------------------------

class Environments:
    """The server function's execution environments, replaced as a deploy replaces them: its
    configuration changed (a variable of the case's own, UAT_ROUND), a version published, the alias
    live moved to it. restore() moves the alias back, restores the configuration and deletes the
    case's versions (CloudFormation would not: the template has not changed)."""

    def __init__(self, ctx):
        self.ctx, self.aws = ctx, ctx.aws
        self.function = ctx.install.output("Serverless", "ServerFunctionName")
        self.alias = self.aws("lambda", "get-alias", "--function-name", self.function, "--name", "live")
        self.installed = self.alias["FunctionVersion"]
        conf = self.aws("lambda", "get-function-configuration", "--function-name", self.function)
        self.variables = (conf.get("Environment") or {}).get("Variables") or {}
        self.made = []
        ctx.on_cleanup("the server function's alias live back on the installed version", self.restore)

    def _wait(self):
        for _ in range(120):
            c = self.aws("lambda", "get-function-configuration", "--function-name", self.function)
            if c.get("LastUpdateStatus") in ("Successful", None) and c.get("State") == "Active":
                return c
            time.sleep(5)
        raise RuntimeError("the server function's update did not finish")

    def replace(self, round_):
        variables = dict(self.variables, UAT_ROUND=str(round_))
        self.aws("lambda", "update-function-configuration", "--function-name", self.function,
                 "--environment", json.dumps({"Variables": variables}))
        self._wait()
        version = self.aws("lambda", "publish-version", "--function-name", self.function,
                           "--description", f"{self.ctx.id} {self.ctx.run} round {round_}")["Version"]
        self.made.append(version)
        for _ in range(120):
            v = self.aws("lambda", "get-function-configuration", "--function-name", self.function, "--qualifier", version)
            if v.get("State") == "Active":
                break
            time.sleep(5)
        self.aws("lambda", "update-alias", "--function-name", self.function, "--name", "live", "--function-version", version)
        return version

    def restore(self):
        self.aws("lambda", "update-alias", "--function-name", self.function, "--name", "live",
                 "--function-version", self.installed)
        self.aws("lambda", "update-function-configuration", "--function-name", self.function,
                 "--environment", json.dumps({"Variables": self.variables}))
        self._wait()
        for version in self.made:
            self.aws.raw("lambda", "delete-function", "--function-name", self.function, "--qualifier", version,
                         check=False)
        live = self.aws("lambda", "get-alias", "--function-name", self.function, "--name", "live")["FunctionVersion"]
        if live != self.installed:
            raise RuntimeError(f"the alias live is on {live}, not the installed {self.installed}")
        return f"alias live on {live}; {len(self.made)} versions of the case deleted"


# --- the writers' own checks --------------------------------------------------------------------------

STRESS_CHECKS = ("every-commit-once", "every-outcome-known", "readers-see-whole-commits", "snapshot-ids-contiguous")


def stress_results(jobs):
    """{pod: (its checks {name: (ok, detail)}, its commits that failed at first)} of the writer pods."""
    out = {}
    for job in jobs:
        facts = job.facts()
        out[job.name] = (tool_checks(facts), [f for f in facts if f.get("kind") == "retried"])
    return out


def judge_stress(results, name):
    """One of the writers' own checks over every writer pod: passed only if every pod passed it (a pod
    without the verdict did not finish)."""
    per = [(pod, *checks.get(name, (False, "no verdict: the writer did not finish"))) for pod, (checks, _) in
           sorted(results.items())]
    return bool(per) and all(ok for _, ok, _ in per), "; ".join(f"{pod}: {detail}" for pod, _, detail in per)


def retried(results):
    return [r for _, (_, rs) in sorted(results.items()) for r in rs]


def stress_verdicts(ctx, jobs, names=STRESS_CHECKS):
    """Writes the writers' checks; returns the commits that failed at first."""
    results = stress_results(jobs)
    for name in names:
        ctx.check(name, *judge_stress(results, name))
    return retried(results)


def judge_consistent(report):
    """The table's consistency report: consistent, no problem (UAT-FLT-01, 02); an error answer is none."""
    if "status" in report:
        return False, f"no report: {report['status']} {str(report.get('error'))[:200]}"
    return report.get("consistent") is True, f"consistent {report.get('consistent')}, problems {report.get('problems')}"
