"""UAT-CAT-03 Per-table credential renewal (Colvenna Lake's run-renewal.sh, FlinkRenewalFlow and
SparkRenewalFlow).

The install issues credentials that live 90 minutes (COLVENNA_DATA_TOKEN_DURATION=PT1H30M, the
shortest the catalog allows: an earlier run of install.sh). The matrix's fixture writer writes its
tables once, in renew_<run>_fixture; Flink and Spark write tables of their own in renew_<run>. Then,
at once and past the first credentials' expiry:

- Flink: a session cluster (one JobManager, two TaskManagers) runs a streaming INSERT of a
  rate-limited, bounded datagen lasting 100 minutes into its own append table, with checkpoints
  every 30 s and no restart allowed, at parallelism 1 (colvenna-lake #297: the input is bounded);
  the TaskManagers fetch their own credentials;
- Spark: one session writes and reads its table, waits 95 minutes, and does it again, also through
  a table object it obtained before the wait;
- Doris 4.1.5: the matrix's reader checks on the fixture's primary-key and append tables, then the
  same checks 95 minutes later, in the same catalog.

The audit then shows the issuances to Flink's principal that were not cache hits; and install.sh
runs again with the default duration (--reinstall), after which the install issues 3-hour
credentials again. About 1 hour 50 minutes; the install to itself."""

import json
import secrets
import shlex
import subprocess
import time
from datetime import datetime, timedelta, timezone

from uatlib import engines
from uatlib.install import REPO

ID = "UAT-CAT-03"
FORMS = ("eks", "serverless")
TOKEN_MINUTES = 90
FLINK_FLOW = "com.colvenna.catalog.uat.flink.FlinkRenewalFlow"
SPARK_FLOW = "com.colvenna.catalog.uat.spark.SparkRenewalFlow"


def add_arguments(p):
    p.add_argument("--reinstall", required=True,
                   help="install.sh's source arguments for step 4, the run that restores the default duration: "
                        "'--from-source' or '--release <candidate>', as the install was made")
    p.add_argument("--flink-minutes", type=int, default=100, help="how long Flink writes (default 100)")
    p.add_argument("--wait-minutes", type=int, default=95, help="how long Spark and Doris wait (default 95)")


def checks(form, args=None):
    return ["flink-writes-across-expiry", "spark-after-the-wait", "doris-after-the-wait", "renewed-before-expiry",
            "duration-restored"]


def token_minutes(ctx, db, table):
    """How long the credentials the admin gets for the table last, in minutes (a fresh table: no cache)."""
    r = ctx.probe.run([{"name": "duration", "url": f"{ctx.install.rest_base}/databases/{db}/tables/{table}/token",
                        "token": "admin"}], tokens=ctx.admin.tokens, keep="durations.jsonl")[0]
    expires = (r.get("body") or {}).get("expiresAtMillis")
    return (expires / 1000 - time.time()) / 60 if r.get("status") == 200 and expires else None


def doris_rounds(output):
    """{(table, check): {round: (rc, output)}} from the reader's COMPAT-READ lines."""
    out = {}
    for line in output.splitlines():
        if line.startswith("COMPAT-READ "):
            r = json.loads(line[len("COMPAT-READ "):])
            if r.get("check") in ("create-catalog", "list-tables") or "round" not in r:
                continue
            out.setdefault((r["table"], r["check"]), {})[r["round"]] = (r["rc"], r.get("output"))
    return out


def judge_doris(rounds):
    """(ok, detail): every check of both rounds answered, the second as the first."""
    if not rounds:
        return False, "no read of the reader's two rounds"
    wrong = []
    for key, by_round in sorted(rounds.items()):
        first, second = by_round.get(1), by_round.get(2)
        if not first or not second or first[0] != 0 or second[0] != 0 or first[1] != second[1]:
            wrong.append(f"{key[0]}/{key[1]}: {first} then {second}")
    return not wrong, (f"{len(rounds)} checks, the second round as the first" if not wrong
                       else f"{len(wrong)} of {len(rounds)} differ: " + "; ".join(wrong[:4]))


def judge_renewal(events, resource, gap=timedelta(minutes=15)):
    """(ok, detail): a fresh (not cached) issuance for the table at least 15 minutes after an earlier
    fresh one, and before that one expired."""
    fresh = []
    for e in events:
        d = e.get("detail") or {}
        if e.get("outcome") != "SUCCEEDED" or e.get("resource") != resource or d.get("cached") is not False:
            continue
        expires = d.get("expiresAt")
        fresh.append((_time(e["time"]), _time(expires) if expires is not None else None))
    fresh.sort()
    for i, (a, a_expires) in enumerate(fresh):
        for b, _ in fresh[i + 1:]:
            if b >= a + gap and a_expires is not None and b < a_expires:
                return True, (f"{len(fresh)} fresh issuances for {resource}; one at {b:%H:%M:%S}Z, "
                              f"{int((b - a).total_seconds() // 60)} min after one that expired at {a_expires:%H:%M:%S}Z")
    return False, f"{len(fresh)} fresh issuances for {resource}, none renewing an earlier one before its expiry"


def judge_flink(step, restarts):
    """step: the flow's own (ok, detail) for flink-writes-across-expiry, or None; restarts: the session
    cluster's pods that restarted."""
    if step is None:
        return False, "the Flink flow printed no verdict"
    ok, detail = step
    return ok and not restarts, detail + (f"; pods restarted: {restarts}" if restarts else "")


def judge_spark(step):
    if step is None:
        return False, "Spark's second round did not run"
    return step


def judge_duration(code, minutes, want=180):
    return code == 0 and minutes is not None and abs(minutes - want) < 5, (
        f"install.sh with the default duration: exit {code}; credentials now last {minutes and round(minutes)} min")


def _time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def reinstall(ctx, args):
    """install.sh again with the install's configuration but the default credential duration."""
    lines = [line for line in ctx.install.config_path.read_text().splitlines()
             if not line.startswith("COLVENNA_DATA_TOKEN_DURATION=")]
    config = ctx.scratch / f"{ctx.install.env}.env"
    config.write_text("\n".join(lines) + "\n")
    command = [str(REPO / "install.sh"), "--config", str(config), *shlex.split(args.reinstall), "--yes"]
    ctx.log("step 4: " + " ".join(command[1:]))
    p = subprocess.run(command, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=3600)
    ctx.keep("install-default-duration.log", p.stdout)
    return p.returncode


def run(ctx):
    args, admin, rest, install = ctx.args, ctx.admin, ctx.rest, ctx.install
    db, fixture_db = f"renew_{ctx.run}", f"renew_{ctx.run}_fixture"
    names = {k: f"renew-{k}-{ctx.run}" for k in ("flink", "spark", "doris", "fixture")}
    started = datetime.now(timezone.utc)
    with ctx.preparing("90-minute credentials, the fixture, the principals and the session cluster") as notes:
        if install.get("COLVENNA_DATA_TOKEN_DURATION") != "PT1H30M":
            raise RuntimeError(f"the install's COLVENNA_DATA_TOKEN_DURATION is {install.get('COLVENNA_DATA_TOKEN_DURATION')}, "
                               "not PT1H30M: run install.sh with it first")
        if args.flink_minutes < TOKEN_MINUTES + 5 or args.wait_minutes < TOKEN_MINUTES + 1:
            raise RuntimeError("Flink must write, and Spark and Doris wait, past the first credentials' expiry")
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, "renewal", [("id", "BIGINT"), ("payload", "STRING")], options={"bucket": "-1"})
        rest.create_table(db, "spark_renewal", [("id", "BIGINT"), ("at", "STRING")], options={"bucket": "-1"})
        rest.create_table(db, "duration_check", [("id", "INT")])
        minutes = token_minutes(ctx, db, "duration_check")
        if minutes is None or abs(minutes - TOKEN_MINUTES) > 3:
            raise RuntimeError(f"the install issues credentials for {minutes and round(minutes)} minutes, not 90")
        for role, name in names.items():
            admin.principal(name, f"{ID} {role}")
            admin.issue(name, "PT4H", key=name)
        admin.grant(names["flink"], "TABLE", "WRITE", database=db, obj="renewal")
        admin.grant(names["spark"], "TABLE", "WRITE", database=db, obj="spark_renewal")
        # The matrix's fixture writer makes its database itself, and holds MANAGE on the catalog, as the
        # matrix's writers do (tests/compat/README.md); the case's own principal, its grant deleted after.
        ctx.on_cleanup(f"database {fixture_db} dropped", lambda: rest.drop_database(fixture_db) and "dropped")
        admin.grant(names["doris"], "DATABASE", "READ", database=fixture_db)
        admin.grant(names["fixture"], "CATALOG", "MANAGE")
        # Doris runs as root in its images: the namespace is at the baseline pod security level.
        eng = engines.Engines(ctx, "engines", security="baseline")
        password = secrets.token_hex(16)
        ctx.sanitizer.secret_sources.append(lambda: [password])
        eng.tokens({**{n: admin.tokens[n] for n in names.values()}, "engine-password": password})
        code, out, _ = eng.run(eng.compat_fixture("fixture", fixture_db, names["fixture"]), 1800, keep="fixture.log")
        fixture = [json.loads(line[len("COMPAT-FIXTURE "):]) for line in out.splitlines()
                   if line.startswith("COMPAT-FIXTURE ")]
        tables = [t for t in fixture if t["table"] in ("pk", "append")]
        if code != 0 or len(tables) != 2:
            raise RuntimeError(f"the fixture writer did not write the primary-key and append tables (exit {code})")
        plan = {"uri": install.uri, "warehouse": install.warehouse, "database": fixture_db, "dataTokens": True,
                "tables": tables}
        cluster = ctx.name("flink")[:40]
        eng.session_cluster(cluster, names["flink"], task_managers=2, slots=1, jm_mb=768, tm_mb=1280, cpu="100m")
        notes.append(f"{round(minutes)}-minute credentials; {fixture_db}: {', '.join(t['name'] for t in tables)}; "
                     f"{db}: renewal, spark_renewal; a JobManager and 2 TaskManagers")

    # --- step 2: the three at once, beside the other clients on the test environment's clients' nodes: small
    # CPU requests (the pods may use more), so that both forms' runs find room for Doris -------------------
    flink = eng.start(eng.flink_client("flink-renewal", cluster, FLINK_FLOW,
                                       [db, "renewal", str(args.flink_minutes), "2", str(TOKEN_MINUTES)],
                                       names["flink"], parallelism=1, deadline=(args.flink_minutes + 40) * 60,
                                       memory="768Mi", cpu="100m"))
    spark = eng.start(eng.spark("spark-renewal", SPARK_FLOW, [db, "spark_renewal", str(args.wait_minutes)],
                                names["spark"], deadline=(args.wait_minutes + 40) * 60, cores=2, memory="1536Mi",
                                heap="768m", cpu="200m"))
    doris = eng.start(eng.doris("doris-renewal", plan, names["doris"], "engine-password", rounds=2,
                                round_seconds=args.wait_minutes * 60, deadline=(args.wait_minutes + 40) * 60,
                                fe_memory="1536Mi", be_memory="2Gi", cpu="200m", request="1Gi"))
    ctx.log(f"Flink writes for {args.flink_minutes} min; Spark and Doris wait {args.wait_minutes} min")
    _, spark_out, _ = eng.wait(spark, (args.wait_minutes + 45) * 60, keep="spark-renewal.log")
    _, doris_out, _ = eng.wait(doris, (args.wait_minutes + 45) * 60, keep="doris-renewal.log")
    _, flink_out, _ = eng.wait(flink, (args.flink_minutes + 45) * 60, keep="flink-renewal.log")
    restarts = [line.split("\n", 1)[0] for line in eng.cluster_logs(cluster, "flink-cluster.log")
                if not line.split("\n", 1)[0].endswith("(restarts 0)")]
    ctx.check("flink-writes-across-expiry", *judge_flink(engines.steps(flink_out).get("flink-writes-across-expiry"),
                                                         restarts))
    ctx.check("spark-after-the-wait", *judge_spark(engines.steps(spark_out).get("spark-after-the-wait")))
    ctx.check("doris-after-the-wait", *judge_doris(doris_rounds(doris_out)))

    # --- step 3: the issuances that were not cache hits -----------------------------------------------
    events = admin.audit(action="ISSUE_DATA_TOKEN", principal=names["flink"],
                         since=started.strftime("%Y-%m-%dT%H:%M:%SZ"))
    ctx.keep("audit-flink-issuances.json", events)
    ctx.check("renewed-before-expiry", *judge_renewal(events, f"{db}.renewal"))

    # --- step 4: the default duration again -----------------------------------------------------------
    code = reinstall(ctx, args)
    rest.create_table(db, "duration_restored", [("id", "INT")])
    minutes = token_minutes(ctx, db, "duration_restored")
    ctx.check("duration-restored", *judge_duration(code, minutes))
    if code != 0:
        ctx.log("install.sh with the default duration failed: run it by hand before any other case")
