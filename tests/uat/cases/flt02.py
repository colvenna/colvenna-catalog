"""UAT-FLT-02 A replica or execution environment lost (from Colvenna Lake's run-recovery.sh, part A).

Writers (tests/uat/paimon CommitStress, each its own commit user) commit to lost_<run>.t1 while what
the catalog runs on is lost. EKS: the node of one replica is drained (only the catalog's pod
evicted), the other replica serving throughout (the PodDisruptionBudget) and the evicted one never
placed beside it; the node is then made schedulable again, and the replica that kept serving is
killed. Serverless: every PostgreSQL session of the server's user is ended (as retired environments'
are) and every execution environment replaced (a new version, the alias moved), then twice as many
writers as the reserved concurrency allows run for 5 minutes. Then the writers' own checks and the
table's consistency report. The judging functions take what was collected and nothing else
(test_flt02.py)."""

import subprocess
import time

from uatlib import faults
from uatlib.catalog import LOG_ARRIVAL
from uatlib.probe import Background
from uatlib.sql import Sql
from uatlib.tools import Tools

ID = "UAT-FLT-02"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5
OVER = 300


def checks(form, args=None):
    own = (["eks-never-unavailable", "eks-evicted-replica-placed"] if form == "eks"
           else ["serverless-over-concurrency-no-effect"])
    return own + ["every-commit-once", "every-outcome-known", "snapshot-ids-contiguous", "consistent"]


def apart(listing):
    """Whether no two running replicas share a node. listing: [(pod, node, ready, phase)]."""
    nodes = [node for _, node, _, phase in listing if phase == "Running" and node]
    return len(nodes) == len(set(nodes))


# --- judging ------------------------------------------------------------------------------------------

def judge_never_unavailable(drain_code, samples):
    """At least one ready replica at every moment of the drain (the PDB held), the two replicas never on
    one node. samples: [(ready replicas, listing)] watched from the drain to the end of the faults."""
    lowest = min((r for r, _ in samples), default=0)
    together = [listing for _, listing in samples if not apart(listing)]
    ok = drain_code == 0 and bool(samples) and lowest >= 1 and not together
    return ok, (f"drain exit {drain_code}; {len(samples)} samples; fewest ready replicas {lowest}; "
                f"two replicas on one node in {len(together)} samples")


def judge_evicted_placed(samples, drained, serving_node):
    """The evicted replica waited for a node (pending, none given) or ran on another node, never
    beside the other replica."""
    waited = any(phase == "Pending" and not node for _, listing in samples for _, node, _, phase in listing)
    elsewhere = any(phase == "Running" and node not in (drained, serving_node)
                    for _, listing in samples for _, node, _, phase in listing)
    together = any(not apart(listing) for _, listing in samples)
    return (waited or elsewhere) and not together, (f"the evicted replica waited for a node: {waited}; ran on another "
                                                     f"node: {elsewhere}; beside the other: {together}")


# What Paimon 2.0.0's client sends again by itself (ExponentialHttpRequestRetryStrategy): 429 and 503.
RETRYABLE = ("429", "503")


def judge_over_concurrency(throttles, gateway, errors, seconds, refused):
    """Requests over what the install serves at once were refused before the server ran: by Lambda's
    throttle (throttles, counted; the gateway answers them 503) or by the gateway's own (gateway: its
    429s, from the access log). The server function failed on none (errors: its Errors), so no 503 was
    a function's failure, which the gateway answers 503 as well. Every answer other than 200 a watching
    client got (refused: {status or error: count}) is one the Paimon client sends again, 429 or 503:
    never 500, nor anything else (#80). Their effect is the writers' checks' to judge (each commit once,
    each outcome known)."""
    unretried = {k: v for k, v in refused.items() if k not in RETRYABLE}
    ok = (throttles > 0 or gateway > 0) and errors == 0 and not unretried
    return ok, (f"{throttles} requests refused over the reserved concurrency in {seconds} s (Lambda's Throttles), "
                f"{gateway} refused by the gateway's own throttle (429), {errors} server function errors; a watching "
                f"client's answers other than 200 by status {refused}"
                + (f", not retryable: {unretried}" if unretried else ", each retryable")
                + "; their effect: see every-commit-once and every-outcome-known")


def statuses(answers):
    """{status: count} of the answers other than 200 (an error's name where no answer came)."""
    out = {}
    for r in answers:
        key = str(r.get("status", r.get("error")))
        if key != "200":
            out[key] = out.get(key, 0) + 1
    return out


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    db, table = f"lost_{ctx.run}", "t1"
    who = f"lost-writer-{ctx.run}"
    admin, rest = ctx.admin, ctx.rest
    timeline = faults.Timeline(ctx)
    what = "writers committing to the table" + (", the replicas apart" if ctx.form == "eks" else "")
    with ctx.preparing(what) as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: faults.drop_database(ctx, db))
        admin.principal(who, ID)
        admin.issue(who, "PT4H", key="writer")
        admin.grant(who, "DATABASE", "MANAGE", database=db)
        time.sleep(CACHE_WAIT)
        tools = Tools(ctx)
        if ctx.form == "eks":
            listing = faults.replicas(ctx)
            if len(listing) < 2 or not apart(listing):
                raise RuntimeError(f"the replicas are not two on two nodes: {[(p, n) for p, n, _, _ in listing]}")
            # 6 writers as in UAT-FLT-01: 120 commits of 500 rows each, 3 s apart.
            writers = [tools.job("stress-a", "writer", "stress", db, table, "1", "3", "120", "500", "3000"),
                       tools.job("stress-b", "writer", "stress", db, table, "4", "3", "120", "500", "3000")]
        else:
            n = 2 * int(ctx.install.get("COLVENNA_LAMBDA_CONCURRENCY"))
            # Twice the reserved concurrency, in pods of 5 writers: 60 commits of 200 rows each, 500 ms apart,
            # enough to keep them over the concurrency for the 5 minutes after the faults (twenty writers on
            # one table commit a few times a minute each, refused on stale bases in turn).
            writers = [tools.job(f"stress-{i}", "writer", "stress", db, table, str(1 + 5 * i), "5", "60", "200", "500",
                                 cpu="100m")
                       for i in range((n + 4) // 5)]
        if not all(w.wait_running() for w in writers):
            for job in writers:
                job.keep_start(ctx)
            raise RuntimeError("the writers did not start")
        if faults.await_published(ctx, db, table, 5) is None:
            raise RuntimeError("the writers published no five snapshots")
        envs = faults.Environments(ctx) if ctx.form == "serverless" else None
        notes.append(f"{db}.{table}: {len(writers)} writer pods")

    if ctx.form == "eks":
        eks(ctx, timeline, writers)
    else:
        serverless(ctx, timeline, writers, envs, db)

    codes = [w.wait(3600) for w in writers]
    timeline("writers finished", exitCodes=codes)
    for w in writers:
        w.keep(ctx)
    faults.stress_verdicts(ctx, writers, ("every-commit-once", "every-outcome-known", "snapshot-ids-contiguous"))
    ctx.check("consistent", *faults.judge_consistent(faults.consistent(ctx, db, table, keep="consistency.json")))


def watch(ctx, samples, seconds, until=None):
    """Samples the ready replicas and where they run, every 2 s, for the seconds or until until()."""
    end = time.time() + seconds
    while time.time() < end:
        samples.append((faults.ready_replicas(ctx), faults.replicas(ctx)))
        if until and until(samples[-1][1]):
            return
        time.sleep(2)


def eks(ctx, timeline, writers):
    listing = faults.replicas(ctx)
    serving, serving_node = listing[0][0], listing[0][1]
    node = next(n for _, n, _, _ in listing if n != serving_node)
    ctx.on_cleanup(f"node {node} schedulable", lambda: ctx.kube.kubectl("uncordon", node) and f"{node} uncordoned")
    timeline("drain the node of the other replica (catalog pods only)", node=node, serving=serving,
             writing=all(w.running() for w in writers))
    drain_samples, later = [], []
    # Sampled while the drain runs, not after: the PodDisruptionBudget holds during the eviction.
    drain = subprocess.Popen(["kubectl", "drain", node, "--pod-selector", faults.SELECTOR, "--ignore-daemonsets",
                              "--delete-emptydir-data", "--timeout=5m"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True, env=ctx.kube.env)
    watch(ctx, drain_samples, 400, until=lambda now: drain.poll() is not None)
    ctx.keep("drain.txt", drain.communicate(timeout=60)[0])

    def placed(now):
        return any(phase == "Pending" and not n for _, n, _, phase in now) or len(now) >= 2

    watch(ctx, drain_samples, 120, until=placed)
    watch(ctx, drain_samples, 20)
    timeline("during the drain", samples=len(drain_samples))
    ctx.kube.kubectl("uncordon", node)
    faults.await_ready(ctx)
    timeline("node schedulable again", replicas=[(p, n) for p, n, _, _ in faults.replicas(ctx)])
    time.sleep(30)
    timeline("kill the replica that kept serving", replica=serving, writing=all(w.running() for w in writers))
    faults.force_delete(ctx, serving)
    watch(ctx, later, 60)
    faults.await_ready(ctx)
    ctx.keep("replica-samples.json", {"drain": drain_samples, "afterTheKill": later})
    # The PodDisruptionBudget's promise is the drain's; after the kill, apart is all that is asked.
    ctx.check("eks-never-unavailable", *judge_never_unavailable(drain.returncode, drain_samples))
    ctx.check("eks-evicted-replica-placed", *judge_evicted_placed(drain_samples + later, node, serving_node))


def serverless(ctx, timeline, writers, envs, db):
    sql = Sql(ctx)
    user = ctx.install.get("COLVENNA_DB_USER")
    rows = sql.query(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = '{user}' "  # nosec B608 - the harness's own names, no outside input
                     "AND pid <> pg_backend_pid()", role="catalog")
    timeline("end every session of the server's user", ended=rows[0][0] if rows else None,
             writing=all(w.running() for w in writers))
    timeline("every execution environment replaced", version=envs.replace(1))
    started = time.time()
    # A client of the case's own beside the writers: the status a refused request gets.
    url = f"{ctx.install.rest_base}/databases/{db}/tables/t1"
    watcher = Background(ctx.probe, "over-watch", [{"name": "over", "url": url, "token": "admin",
                                                     "repeat": {"every": 1, "for": OVER}}], ctx.admin.tokens)
    time.sleep(OVER)
    answers = watcher.stop(keep="over-watch.jsonl")
    throttles = throttled(ctx, started)
    errors = throttled(ctx, started, "Errors")
    gateway = gateway_refusals(ctx, started)
    timeline(f"{OVER} s over the reserved concurrency", throttles=throttles, gateway=gateway, errors=errors)
    ctx.check("serverless-over-concurrency-no-effect",
              *judge_over_concurrency(throttles, gateway, errors, OVER, statuses(answers)))


def gateway_refusals(ctx, since):
    """How many requests since the time the gateway's own throttle refused (429; THROTTLED in the API's
    access log, #80), once the log has arrived."""
    time.sleep(LOG_ARRIVAL)
    out = ctx.aws("logs", "filter-log-events", "--log-group-name", f"/aws/apigateway/colvenna-catalog-{ctx.install.env}-api",
                  "--start-time", str(int((since - 60) * 1000)), "--filter-pattern", '{ $.errorType = "THROTTLED" }',
                  timeout=900) or {}
    return len(out.get("events", []))


def throttled(ctx, since, metric="Throttles"):
    """The server function's throttles since the time (CloudWatch's Throttles metric, summed), or another
    of its metrics (Errors)."""
    function = ctx.install.output("Serverless", "ServerFunctionName")
    out = ctx.aws("cloudwatch", "get-metric-statistics", "--namespace", "AWS/Lambda", "--metric-name", metric,
                  "--dimensions", f"Name=FunctionName,Value={function}", "--start-time", faults.stamp(since - 60),
                  "--end-time", faults.stamp(), "--period", "60", "--statistics", "Sum")
    return int(sum(p.get("Sum", 0) for p in (out or {}).get("Datapoints", [])))
