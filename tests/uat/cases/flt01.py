"""UAT-FLT-01 Concurrent writers under faults (from Colvenna Lake's run-commit-faults.sh).

Six writers (tests/uat/paimon CommitStress, each its own commit user) in three pods commit 120 times
500 rows, 3 s apart, to flt_<run>.events, readers in each pod checking that they only ever see whole
commits; a maintenance loop compacts every 20 s with fast retries; two of the pods reach the catalog
through a proxy (probe/proxy.py) that drops the catalog's answer to every tenth commit and sends the
commit again at once, as a client whose response was lost does. While they commit, the faults of
the form, at least 30 s apart:

  EKS: (1) one replica killed, 30 s later the other; (2) the catalog's PostgreSQL sessions ended
  every 3 s for 30 s; (3) the catalog's database closed to connections for 40 s, open sessions
  ended; (4) both replicas killed at once; (5) the branch row lock of events held for 35 s, past
  the 30 s lock timeout.
  Serverless: (1) every execution environment replaced (a new version, the alias moved, as a deploy
  does); (2) and (3) as for EKS; (5) the row lock held for 5 s, past the 3 s lock timeout.

Then the writers' own checks, the catalog's audit of the commits, the commit intents, the
maintenance rounds after the faults and the table's consistency report, read from the database. The
judging functions take what was collected and nothing else (test_flt01.py)."""

import json
import time

from uatlib import faults
from uatlib.probe import Background
from uatlib.sql import Sql
from uatlib.tools import Tools

ID = "UAT-FLT-01"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5
GAP = 30
DROP_EVERY = 10
REPLAY = "replay of a committed request"


def checks(form, args=None):
    return ["faults-hit-live-writers", "every-commit-once", "every-outcome-known", "readers-see-whole-commits",
            "snapshot-ids-contiguous", "failed-at-first-then-resolved", "lock-timeout-retryable",
            "lost-responses-applied-once", "stale-base-refused-and-retried", "intents-published-once",
            "maintenance-after-the-faults", "consistent", "no-failure-as-success"]


# --- judging ------------------------------------------------------------------------------------------

def judge_faults_hit(hit, form, ended):
    """Every fault of the form began while the writers committed, with its effect: sessions ended (2),
    the catalog without its database (3: no ready replica, or answers 503), the lock held (5).
    hit: [(fault, writers committing, effect seen)]; ended: sessions fault 2 ended."""
    want = ["1", "2", "3", "4", "5"] if form == "eks" else ["1", "2", "3", "5"]
    missed = [n for n, writing, _ in hit if not writing]
    no_effect = [n for n, _, effect in hit if not effect]
    ok = [n for n, _, _ in hit] == want and not missed and not no_effect
    return ok, (f"faults {[n for n, _, _ in hit]} (want {want}); begun with the writers done: {missed}; without "
                f"their effect: {no_effect}; sessions ended by fault 2: {ended}")


def judge_retried(retried):
    """At least one commit failed at first and was resolved by its writer's retry."""
    outcomes = sorted({r.get("outcome") for r in retried})
    return bool(retried), f"{len(retried)} commits failed at first and were resolved by their writer: {outcomes}"


def judge_lock(events, since, until, applied):
    """Commits answered 503 while the branch row lock was held, applied once after their clients retried.
    The catalog audits a lock timeout without the commit's user and identifier, so "applied once" is the
    writers' every-commit-once (applied: its (ok, detail))."""
    locked = [e for e in events if e["status"] == 503 and since <= e["time"] <= until]
    return bool(locked) and applied[0], (f"{len(locked)} commits answered 503 while the lock was held; applied once "
                                         f"after the retry (every-commit-once): {applied[0]}")


def judge_dropped(dropped, events):
    """Each commit whose answer the proxy dropped was applied exactly once, and the same request sent
    again was answered as the first was. A commit (the first answer success): published once, and the
    resend answered as a replay (success, the catalog's audit naming it a replay). A refusal (an error
    status, or success false): the resend not applied either; the writer commits it again, as
    every-commit-once judges (under another identifier, when Paimon gives up on the first)."""
    published, replays = {}, {}
    for e in events:
        key = (e["commitUser"], str(e["commitIdentifier"]))
        if e["detail"] == "published":
            published[key] = published.get(key, 0) + 1
        elif e["detail"] == REPLAY:
            replays[key] = replays.get(key, 0) + 1
    problems, committed = [], 0
    for d in dropped:
        key = (d.get("commitUser"), str(d.get("commitIdentifier")))
        first = (d.get("firstBody") or "").replace(" ", "")
        answer = (d.get("againBody") or "").replace(" ", "")
        if d.get("first") == 200 and '"success":true' in first:
            committed += 1
            if published.get(key, 0) != 1:
                problems.append(f"{key[0]}/{key[1]} published {published.get(key, 0)} times")
            if not (d.get("again") == 200 and '"success":true' in answer and replays.get(key)):
                problems.append(f"{key[0]}/{key[1]}: sent again, answered {d.get('again')} {answer[:60]}, "
                                f"{replays.get(key, 0)} replays audited")
        elif d.get("again") == 200 and '"success":true' in answer:
            problems.append(f"{key[0]}/{key[1]}: refused at first ({d.get('first')}), then applied when sent again")
    return bool(committed) and not problems, (f"{len(dropped)} answers dropped, {committed} of them commits; "
                                              f"problems {problems[:5]}")


def judge_stale(events):
    """Commits refused on a stale base (success false, audited FAILED); their writers' commits all
    landed once (every-commit-once)."""
    stale = [e for e in events if e["committed"] == "false" and e["outcome"] == "FAILED"]
    return bool(stale), f"{len(stale)} commits refused on a stale base and committed again by their writers"


def judge_intents(rows, pointer):
    """The published intents number the snapshots 1..pointer, each once; none pending."""
    states, published = {}, []
    for state, snapshot in rows:
        states[state] = states.get(state, 0) + 1
        if state == "PUBLISHED":
            published.append(int(snapshot))
    published.sort()
    ok = pointer is not None and published == list(range(1, pointer + 1)) and not states.get("PENDING")
    return ok, (f"intents by state {states}; published 1..{pointer} each once: "
                f"{pointer is not None and published == list(range(1, pointer + 1))}")


def judge_maintenance(rounds, after):
    """Every maintenance round that began from 60 s after the last fault (after: its time) succeeded."""
    late = [r for r in rounds if r.get("began", "") > after]
    failed = [r.get("error") for r in late if not r.get("ok")]
    return bool(late) and not failed, f"{len(rounds)} rounds, {len(late)} from 60 s after the last fault; failed {failed[:3]}"


def judge_no_failure_as_success(rows):
    return not rows, f"events with a status of 400 or more recorded as SUCCEEDED: {rows}"


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    db, table = f"flt_{ctx.run}", "events"
    who = f"flt-writer-{ctx.run}"
    admin, rest = ctx.admin, ctx.rest
    sql = Sql(ctx)
    timeline = faults.Timeline(ctx)
    hit = []
    database, user = ctx.install.get("COLVENNA_DB_NAME"), ctx.install.get("COLVENNA_DB_USER")
    with ctx.preparing("writers, readers and maintenance committing; the table consistent") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: faults.drop_database(ctx, db))
        admin.principal(who, ID)
        admin.issue(who, "PT4H", key="writer")
        admin.grant(who, "DATABASE", "MANAGE", database=db)
        time.sleep(CACHE_WAIT)
        started = faults.stamp(time.time() - 1)
        tools = Tools(ctx)
        writers = [tools.job("stress-a", "writer", "stress", db, table, "1", "2", "120", "500", "3000"),
                   tools.job("stress-b", "writer", "stress", db, table, "3", "2", "120", "500", "3000", proxy=DROP_EVERY),
                   tools.job("stress-c", "writer", "stress", db, table, "5", "2", "120", "500", "3000", proxy=DROP_EVERY)]
        maintenance = tools.job("maintenance", "writer", "maintenance", db, table, "30", "20")
        if not all(w.wait_running() for w in writers + [maintenance]):
            raise RuntimeError("the writers or the maintenance loop did not start")
        if faults.await_published(ctx, db, table, 5) is None:
            raise RuntimeError("the writers published no five snapshots")
        baseline = faults.consistent(ctx, db, table, keep="consistency-before.json")
        if baseline.get("consistent") is not True:
            raise RuntimeError(f"the table was not consistent before the faults: {baseline.get('problems')}")
        envs = faults.Environments(ctx) if ctx.form == "serverless" else None
        notes.append(f"{db}.{table}: 6 writers in 3 pods (2 through the proxy), maintenance")

    def live():
        return all(w.running() for w in writers)

    def reopen():
        sql.query(f'ALTER DATABASE "{database}" WITH ALLOW_CONNECTIONS true', role="catalog", database="postgres")
        return "the catalog's database open"

    ctx.on_cleanup("the catalog's database open to connections", reopen)

    # (1) replicas or execution environments replaced
    w = live()
    if ctx.form == "eks":
        pods = [p for p, _, _, _ in faults.replicas(ctx)]
        timeline("fault 1: kill a replica", replica=pods[0], writing=w)
        faults.force_delete(ctx, pods[0])
        time.sleep(30)
        timeline("fault 1: kill the other original replica", replica=pods[1])
        faults.force_delete(ctx, pods[1])
        faults.await_ready(ctx)
        hit.append(("1", w, True))
    else:
        timeline("fault 1: replace every execution environment", writing=w)
        hit.append(("1", w, bool(envs.replace(1))))
    time.sleep(GAP)

    # (2) the catalog's sessions ended every 3 s for 30 s
    w = live()
    timeline("fault 2: end the catalog's sessions every 3 s for 30 s", writing=w)
    ended = 0
    for _ in range(10):
        rows = sql.query(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = '{user}' "  # nosec B608 - the harness's own names, no outside input
                         "AND pid <> pg_backend_pid()", role="catalog")
        ended += int(rows[0][0]) if rows else 0
        time.sleep(3)
    timeline("fault 2: done", ended=ended)
    hit.append(("2", w, ended > 0))
    time.sleep(GAP)

    # (3) the database closed to connections for 40 s
    w = live()
    timeline("fault 3: close the catalog's database for 40 s", writing=w)
    watcher = None
    if ctx.form == "serverless":
        url = f"{ctx.install.rest_base}/databases/{db}/tables/{table}"
        watcher = Background(ctx.probe, "closed-watch", [{"name": "closed", "url": url, "token": "admin",
                                                          "repeat": {"every": 2, "for": 70}}], admin.tokens)
    sql.query(f'ALTER DATABASE "{database}" WITH ALLOW_CONNECTIONS false', role="catalog", database="postgres")
    sql.query(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '{database}' "  # nosec B608 - the harness's own names, no outside input
              f"AND usename = '{user}' AND pid <> pg_backend_pid()", role="catalog", database="postgres")
    lowest, end = 2, time.time() + 40
    while time.time() < end:
        if ctx.form == "eks":
            lowest = min(lowest, faults.ready_replicas(ctx))
        time.sleep(4)
    reopen()
    if ctx.form == "eks":
        timeline("fault 3: the database open again", fewestReady=lowest)
        faults.await_ready(ctx)
        hit.append(("3", w, lowest == 0))
    else:
        time.sleep(30)
        answers = watcher.stop(keep="closed-watch.jsonl")
        timeline("fault 3: the database open again", statuses=sorted({str(r.get("status", r.get("error"))) for r in answers}))
        hit.append(("3", w, any(r.get("status") == 503 for r in answers)))
    time.sleep(GAP)

    # (4) both replicas at once (EKS)
    if ctx.form == "eks":
        w = live()
        timeline("fault 4: kill both replicas", writing=w)
        faults.force_delete(ctx, *[p for p, _, _, _ in faults.replicas(ctx)])
        faults.await_ready(ctx)
        hit.append(("4", w, True))
        time.sleep(GAP)

    # (5) the branch row lock held past the lock timeout
    hold = 35 if ctx.form == "eks" else 5
    w = live()
    lock_from = faults.stamp(time.time() - 1)
    timeline(f"fault 5: hold the branch row lock of {table} for {hold} s", writing=w)
    rows = sql.query(
        "BEGIN; SELECT 'locked:' || b.branch FROM table_branches b JOIN catalog_tables t ON t.id = b.table_id "  # nosec B608 - the harness's own names, no outside input
        f"WHERE t.database_name = '{db}' AND t.name = '{table}' AND b.branch = 'main' FOR UPDATE OF b; "
        f"SELECT pg_sleep({hold}); COMMIT;", role="catalog", timeout=hold + 120)
    lock_until = faults.stamp(time.time() + 60)
    held = any(r and r[0] == "locked:main" for r in rows)
    timeline("fault 5: lock released", held=held)
    hit.append(("5", w, held))
    faults_over = time.time()

    # --- the writers' end and their checks -----------------------------------------------------------
    codes = [wr.wait(3600) for wr in writers]
    timeline("writers finished", exitCodes=codes)
    time.sleep(45)
    maintenance.stop()
    maintenance.wait(300)
    for job in writers + [maintenance]:
        job.keep(ctx)
    ctx.check("faults-hit-live-writers", *judge_faults_hit(hit, ctx.form, ended))
    results = faults.stress_results(writers)
    for name in faults.STRESS_CHECKS:
        ctx.check(name, *faults.judge_stress(results, name))
    retried = faults.retried(results)
    ctx.check("failed-at-first-then-resolved", *judge_retried(retried))

    # --- the catalog's records ------------------------------------------------------------------------
    events = [
        {"commitUser": r[0], "commitIdentifier": r[1], "detail": r[2], "status": int(r[3]), "outcome": r[4], "time": r[5],
         "committed": r[6]}
        for r in sql.query(
            "SELECT detail->>'commitUser', detail->>'commitIdentifier', coalesce(detail->>'detail', detail->>'error', ''), "  # nosec B608 - the harness's own names, no outside input
            "http_status, outcome, to_char(event_time AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
            "coalesce(detail->>'committed', '') FROM audit_events "
            f"WHERE action = 'COMMIT' AND resource = '{db}.{table}' AND event_time >= '{started}'", role="catalog")]
    ctx.keep("audit-commits.json", events)
    ctx.check("lock-timeout-retryable", *judge_lock(events, lock_from, lock_until, faults.judge_stress(results, "every-commit-once")))
    dropped = [json.loads(line) for job in writers[1:] for line in job.logs("proxy").splitlines()
               if line.startswith("{") and '"dropped"' in line]
    ctx.keep("dropped.json", dropped)
    ctx.check("lost-responses-applied-once", *judge_dropped(dropped, events))
    ctx.check("stale-base-refused-and-retried", *judge_stale(events))
    report = faults.consistent(ctx, db, table, keep="consistency.json")
    pointer = next((b.get("pointerSnapshot") for b in report.get("branches", []) if b.get("branch") == "main"), None)
    intents = sql.query("SELECT i.state, i.snapshot_id FROM commit_intents i JOIN catalog_tables t ON t.id = i.table_id "  # nosec B608 - the harness's own names, no outside input
                        f"WHERE t.database_name = '{db}' AND t.name = '{table}' AND i.branch = 'main'", role="catalog")
    ctx.check("intents-published-once", *judge_intents(intents, pointer))
    rounds = [f for f in maintenance.facts() if f.get("kind") == "round"]
    ctx.check("maintenance-after-the-faults", *judge_maintenance(rounds, faults.stamp(faults_over + 60)))
    ctx.check("consistent", *faults.judge_consistent(report))
    wrong = sql.query("SELECT action, http_status, count(*) FROM audit_events WHERE outcome = 'SUCCEEDED' "  # nosec B608 - the harness's own names, no outside input
                      f"AND http_status >= 400 AND event_time >= '{started}' GROUP BY 1, 2", role="catalog")
    ctx.check("no-failure-as-success", *judge_no_failure_as_success(wrong))
