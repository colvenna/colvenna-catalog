"""UAT-CAT-01 Table operations through the catalog (Colvenna Lake's FlinkCatalogFlow and
SparkCatalogFlow, run-flink-deployment.sh and run-spark-flow.sh).

Principals uat-flink-<run> and uat-spark-<run>, each with MANAGE on the name flow_<run> before the
database exists. A standalone Flink session cluster (one JobManager, two TaskManagers, the official
Flink 1.20.5 image with the Paimon 2.0.0 jars) runs FlinkCatalogFlow submitted at parallelism 2; its
streaming step writes its bounded input at parallelism 1 (colvenna-lake #297). The TaskManagers fetch
their own per-table credentials. Then Spark 3.5.9 (local[4]) runs SparkCatalogFlow on what Flink
wrote. Afterwards the dropped table's location is looked at, and the catalog's access log of the
window searched for requests to routes the catalog lacks. Synthetic data only; the database is dropped
through the catalog at the end. The judging functions take what was collected and nothing else
(test_cat01.py)."""

import sys
import time
from datetime import datetime, timezone

from uatlib import engines, purges
from uatlib.catalog import access_log
from uatlib.install import REPO

sys.path.insert(0, str(REPO / "tests" / "compat"))
from compatlib import accesslog  # noqa: E402

ID = "UAT-CAT-01"
FORMS = ("eks", "serverless")
FLINK_CHECKS = ("create-database", "create-primary-key-table", "create-append-table", "batch-insert",
                "upsert-update-delete", "alter-add-column", "streaming-append-commits", "catalog-partitions",
                "consumer-id-stream-read", "tag-create-and-read", "time-travel-by-snapshot", "branch-write-and-read",
                "compact-procedure", "expire-snapshots", "rollback-on-scratch-table", "view", "catalog-function",
                "rename-and-drop")
SPARK_CHECKS = ("spark-read-flink-tables", "spark-time-travel", "spark-read-branch", "spark-system-tables",
                "spark-create-insert", "spark-update-delete-merge", "spark-alter", "spark-procedures",
                "spark-writes-flink-table")
FLINK_FLOW = "com.colvenna.catalog.uat.flink.FlinkCatalogFlow"
SPARK_FLOW = "com.colvenna.catalog.uat.spark.SparkCatalogFlow"


def checks(form, args=None):
    return list(FLINK_CHECKS) + list(SPARK_CHECKS) + ["no-missing-endpoint"]


# --- judging ------------------------------------------------------------------------------------------

def judge_steps(names, output):
    """{check: (ok, detail)} of the flow's STEP lines; a check the flow never reached is absent (the
    case's result then names it missing)."""
    found = engines.steps(output)
    return {n: found[n] for n in names if n in found}


def judge_rename_and_drop(step, objects_left):
    """step: the flow's own (ok, detail): the rename kept the files, the drop removed the table from
    the catalog; objects_left: what is still under the dropped table's location (None: not looked)."""
    ok, detail = step
    return ok and objects_left == 0, (f"{detail}; objects left where the dropped table was: "
                                      f"{objects_left if objects_left is not None else 'not looked for'}")


def judge_endpoints(requests):
    """requests: the access log's requests of the case's principals (compatlib.accesslog.Request)."""
    unserved = [f"{r.describe()}: {r.unserved()}" for r in requests if r.unserved()]
    return bool(requests) and not unserved, (
        f"{len(requests)} requests of the two principals; to routes the catalog lacks or answered 501: "
        + ("; ".join(unserved[:5]) if unserved else "none"))


# --- the run ------------------------------------------------------------------------------------------

def objects_under(ctx, path):
    """How many objects are under a dropped table's location (s3://bucket/key/), by the operator's
    identity, once its files are purged: in the serverless form the purge function deletes them (#69),
    which the case runs now rather than waiting for its schedule."""
    purges.now(ctx)
    return purges.objects_under(ctx, path)


def run(ctx):
    db = f"flow_{ctx.run}"
    flink, spark = f"uat-flink-{ctx.run}", f"uat-spark-{ctx.run}"
    admin = ctx.admin
    started = time.time()
    with ctx.preparing("principals, grants and the Flink session cluster") as notes:
        for who in (flink, spark):
            admin.principal(who, ID)
            admin.issue(who, "PT4H", key=who)
            # MANAGE on the name first: Flink makes the database.
            admin.grant(who, "DATABASE", "MANAGE", database=db)
        ctx.on_cleanup(f"database {db} dropped through the catalog", lambda: ctx.rest.drop_database(db) and "dropped")
        eng = engines.Engines(ctx, "engines")
        eng.tokens({flink: admin.tokens[flink], spark: admin.tokens[spark]})
        cluster = ctx.name("flink")[:40]
        eng.session_cluster(cluster, flink, task_managers=2, slots=2)
        notes.append(f"{flink}, {spark}: MANAGE on {db}; a JobManager and 2 TaskManagers")

    code, out, _ = eng.run(eng.flink_client("flink-client", cluster, FLINK_FLOW, [db], flink, parallelism=2,
                                            deadline=3600), 3900, keep="flink-client.log")
    eng.cluster_logs(cluster, "flink-cluster.log")
    if code != 0:
        ctx.log(f"the Flink flow exited {code}")
    for name, (ok, detail) in judge_steps([c for c in FLINK_CHECKS if c != "rename-and-drop"], out).items():
        ctx.check(name, ok, detail)
    step = judge_steps(["rename-and-drop"], out).get("rename-and-drop")
    if step:
        path = next((line.split(" ", 2)[2] for line in out.splitlines() if line.startswith("PATH scratch3 ")), "")
        ctx.check("rename-and-drop", *judge_rename_and_drop(step, objects_under(ctx, path) if step[0] else None))

    if "FLOW PASS " + db in out:
        code, out, _ = eng.run(eng.spark("spark-flow", SPARK_FLOW, [db], spark, deadline=3600, cores=4, memory="3Gi",
                                         heap="2g"), 3900, keep="spark-flow.log")
        for name, (ok, detail) in judge_steps(SPARK_CHECKS, out).items():
            ctx.check(name, ok, detail)
    else:
        ctx.log("the Spark flow did not run: the Flink flow did not pass")

    text = "\n".join(access_log(ctx, started).values())
    since = datetime.fromtimestamp(started - 5, timezone.utc)
    requests = accesslog.window(accesslog.parse(text), since, datetime.now(timezone.utc), [flink, spark])
    ctx.keep("access-log-requests.txt", "\n".join(r.describe() for r in requests) + "\n")
    ctx.check("no-missing-endpoint", *judge_endpoints(requests))
