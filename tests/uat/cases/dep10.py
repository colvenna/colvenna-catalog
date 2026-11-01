"""UAT-DEP-10 Restore the metadata database from a snapshot (from Colvenna Lake's run-rds-restore.sh),
by the user manual's procedure (docs/manual/user.md, sections 6.1 to 6.3).

Database rst_<run>: t1 (10 commits), t2 (5) and gone (3) are written and recorded, snapshot by
snapshot; a manual RDS snapshot is the restore point; after it t1 takes one commit more, t3 is made
(5 commits), gone is dropped, and a principal and a grant are made. The restore: a new instance from
the snapshot with the running instance's settings, and install.sh run again (from the install's own
checkout, --install) with the configuration pointed at it (COLVENNA_DB=existing, its endpoint, the
storage stack's admin secret and bucket); the configuration so changed is kept as
<config dir>/<env>-restored.env, which the install's later runs and its uninstall must use. Before
that run, the same configuration without COLVENNA_BUCKET, and after it the install's own (with
COLVENNA_DB=create), must each be refused, changing nothing. Then the consistency check of every
table, reconcile of t1, register of t3, unregister of gone, and the reads. When every check has
passed, the restore point's snapshot and the old instance are deleted as the manual says (the old
one with a final snapshot, which is deleted too); the install goes on on the restored instance."""

import json
import re
import time

from uatlib import faults, installer, lifecycle
from uatlib.install import parse_env
from uatlib.tools import Tools

ID = "UAT-DEP-10"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5


def add_arguments(p):
    p.add_argument("--install", required=True, dest="install_checkout",
                   help="the clean checkout that installed the catalog (its tag, or its commit), from which "
                        "install.sh runs again onto the restored instance")
    p.add_argument("--keep-old-instance", action="store_true",
                   help="keep the old instance and the restore point's snapshot (default: deleted once every check passed)")


def checks(form, args=None):
    return ["repointed", "repoint-guarded", "lists-as-at-point", "reads-as-at-point", "history-as-at-point", "check-t1-behind", "check-t2-consistent",
            "check-gone-missing", "all-in-line", "history-as-before-restore", "takes-writes", "identities-as-at-point"]


# --- judging ------------------------------------------------------------------------------------------



def judge_repointed(code, serving, endpoint, signed_in):
    """install.sh onto the restored instance exits 0; the catalog runs on it (EKS: the release's
    database.url; serverless: the Serverless stack's DatabaseEndpoint); its users sign in to it."""
    ok, detail = signed_in
    return (code == 0 and serving == endpoint and ok,
            f"install.sh exit {code}; the catalog runs on the restored instance: {serving == endpoint}; {detail}")


def judge_signed_in(form, user, names):
    """EKS: psql signed in as the catalog's user (names: [current_user]); serverless: the driver signed
    in with an IAM token, and the catalog's user and the authorizer's hold rds_iam (names: who holds it)."""
    if form == "eks":
        return names == [user], f"signed in as {names}"
    return sorted(names) == sorted([user, f"{user}_authz"]), f"signed in with an IAM token; rds_iam held by {sorted(names)}"


def judge_guarded(runs, stack_db):
    """Each refused run: exit 3, naming the Storage stack's database, and nothing changed (the stacks,
    and the release or the live alias, as before it)."""
    out, ok = [], bool(runs)
    for label, code, output, before, after in runs:
        named = stack_db in output
        same = before == after
        ok = ok and code == 3 and named and same
        out.append(f"{label}: exit {code}, names the Storage stack's database {named}, nothing changed {same}")
    return ok, "; ".join(out)


def judge_lists(code, listing):
    """After the restore the catalog lists what it did at the point: gone, t1, t2, no t3."""
    return code == 0 and sorted(listing or []) == ["gone", "t1", "t2"], f"install.sh exit {code}; listed {sorted(listing or [])}"


def not_compared(a, b):
    """The reason two records are not compared (one of them incomplete, #64), or None."""
    missing = lifecycle.incomplete(a, b)
    return f"not compared, a record is incomplete ({'; '.join(missing)})" if missing else None


def judge_reads(at, point):
    """t1 and t2 read as at the point (rows and checksum)."""
    if not_compared(at, point):
        return False, not_compared(at, point)
    pairs = {t: (at["tables"].get(t), point["tables"].get(t)) for t in ("t1", "t2")}
    ok = all(a is not None and a == b for a, b in pairs.values())
    return ok, "; ".join(f"{t}: {(a or {}).get('rows')} rows, at the point {(b or {}).get('rows')}" for t, (a, b) in pairs.items())


def judge_history(at, point):
    """t1's and t2's snapshots, one by one, read as at the point."""
    if not_compared(at, point):
        return False, not_compared(at, point)
    t1, t2 = lifecycle.same_snapshots(at, point, "t1"), lifecycle.same_snapshots(at, point, "t2")
    return t1 and t2, f"snapshot by snapshot as at the point: t1 {t1}, t2 {t2}"


def judge_check(report):
    """What a consistency report says: 'consistent', 'behind' (files ahead of the pointer), 'missing';
    an answer that is no report (an error status) is none of them."""
    if "status" in report:
        return f"no report: {report['status']} {str(report.get('error'))[:200]}"
    if report.get("consistent") is True:
        return "consistent"
    text = " ".join(str(p) for p in report.get("problems", []))
    if re.search(r"(ahead|beyond|behind|newer).*(pointer|snapshot)|snapshot-\d+ .*not published", text, re.I):
        return "behind"
    if re.search(r"missing|has no file|no schema file|cannot read|does not exist|not found|no such", text, re.I):
        return "missing"
    return "other: " + text[:200]


def judge_found(report, want, table):
    found = judge_check(report)
    return found == want, f"{table}: {found} (want {want})"


def judge_in_line(final):
    """After the repairs every table of the database is consistent: t1, t2, t3."""
    ok = sorted(final) == ["t1", "t2", "t3"] and all(r.get("consistent") is True for r in final.values())
    return ok, f"tables {sorted(final)}; consistent {[(t, r.get('consistent')) for t, r in sorted(final.items())]}"


def judge_history_before(now, before):
    """t1 (its later commit included) and t3 read, snapshot by snapshot, as before the restore."""
    if not_compared(now, before):
        return False, not_compared(now, before)
    t1, t3 = lifecycle.same_snapshots(now, before, "t1"), lifecycle.same_snapshots(now, before, "t3")
    return t1 and t3, f"as before the restore: t1 {t1}, t3 {t3}"


def judge_takes_writes(t1, t3):
    return t1 == 0 and t3 == 0, f"a commit to t1: exit {t1}; to t3: exit {t3}"


def judge_identities(principals, grants, late, late_grant):
    """The principal and the grant made after the point are gone."""
    return late not in principals and late_grant not in grants, (
        f"the principal made after the point listed: {late in principals}; its grant: {late_grant in grants}")


# --- the run ------------------------------------------------------------------------------------------

def consistency(ctx, db, table):
    """The table's consistency report, its pages followed (#69); an answer other than 200 as {status,
    error}, so that the case goes on to the repairs and the other checks."""
    r = ctx.admin.consistency_report(db, table, timeout=90)
    if r.get("status") != 200:
        body = r.get("body")
        return {"status": r.get("status", r.get("error")),
                "error": body.get("message") if isinstance(body, dict) else body, "requestId": r.get("serverRequestId")}
    return r["body"]


def create_config(ctx, path):
    """The configuration with COLVENNA_DB=create, as the install had before it was pointed at a
    restored instance: no endpoint, port or admin secret, and no COLVENNA_BUCKET if a restore added it
    (the Storage stack's own bucket). install.sh must refuse it; in scratch."""
    cfg = parse_env(path.read_text())
    cfg["COLVENNA_DB"] = "create"
    for key in ("COLVENNA_DB_ENDPOINT", "COLVENNA_DB_PORT", "COLVENNA_DB_ADMIN_SECRET_ARN"):
        cfg.pop(key, None)
    if cfg.get("COLVENNA_BUCKET") == ctx.install.output("Storage", "BucketName"):
        cfg.pop("COLVENNA_BUCKET")
    return lifecycle.write_config(ctx, ctx.scratch / f"{ctx.install.env}-db-create.env", cfg,
                                  "The install's configuration with COLVENNA_DB=create (UAT-DEP-10, repoint-guarded)")


def settled(ctx):
    """What a refused install.sh must leave as it was: the install's stacks (status, last update) and
    the release's revision (EKS) or the live alias' version (serverless)."""
    stacks = {}
    for part in ("Storage", "Eks", "Network", "Images", "Serverless"):
        p = ctx.aws.raw("cloudformation", "describe-stacks", "--stack-name", ctx.install.stack(part), "--output", "json",
                        check=False)
        if p.returncode == 0:
            st = json.loads(p.stdout)["Stacks"][0]
            stacks[part] = (st["StackStatus"], st.get("LastUpdatedTime") or st.get("CreationTime"))
    if ctx.form == "eks":
        history = lifecycle.helm_json(ctx, "history", ctx.install.release)
        return {"stacks": stacks, "revision": max(h["revision"] for h in history)}
    fn = ctx.install.output("Serverless", "ServerFunctionName")
    return {"stacks": stacks, "live": ctx.aws("lambda", "get-alias", "--function-name", fn, "--name", "live")["FunctionVersion"]}


def serving(ctx):
    """The database host the catalog runs on: EKS, its release's database.url; serverless, the
    Serverless stack's output DatabaseEndpoint."""
    if ctx.form == "eks":
        values = lifecycle.helm_json(ctx, "get", "values", ctx.install.release)
        m = re.match(r"^jdbc:postgresql://([^:/?]+)", (values.get("database") or {}).get("url") or "")
        return m.group(1) if m else None
    return ctx.install.output("Serverless", "DatabaseEndpoint")


def signed_in(ctx, sql):
    """What shows the catalog's users signing in to the restored instance. EKS: the user psql signed
    in as, with the password the chart has (the Eks stack's secret's). Serverless: the driver signed in
    as the catalog's user with an IAM token, and which of the two users hold rds_iam."""
    user = ctx.install.get("COLVENNA_DB_USER")
    if ctx.form == "eks":
        return [r[0] for r in sql.query("SELECT current_user", role="catalog")]
    return [r[0] for r in sql.query("SELECT m.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member "  # nosec B608 - the harness's own names, no outside input
                                    "JOIN pg_roles r ON r.oid = a.roleid WHERE r.rolname = 'rds_iam' "
                                    f"AND m.rolname IN ('{user}', '{user}_authz') ORDER BY 1", role="catalog")]


def run(ctx):
    db = f"rst_{ctx.run}"
    who, late = f"rst-writer-{ctx.run}", f"rst-late-{ctx.run}"
    admin, rest = ctx.admin, ctx.rest
    checkout = installer.Checkout(ctx.args.install_checkout)
    tables = ["t1", "t2", "gone", "t3"]
    with ctx.preparing("the tables, the restore point and the changes after it") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: faults.drop_database(ctx, db))
        admin.principal(who, ID)
        admin.issue(who, "PT6H", key="writer")
        admin.grant(who, "DATABASE", "MANAGE", database=db)
        time.sleep(CACHE_WAIT)
        tools = Tools(ctx)
        for table, commits in (("t1", 10), ("t2", 5), ("gone", 3)):
            code, _ = tools.run("writer", "write", db, table, "append", str(commits), "10", keep="tools.log")
            if code != 0:
                raise RuntimeError(f"writing {table}: exit {code}")
        point = lifecycle.record(ctx, tools, "writer", db, tables, [who])
        old_instance, snap = lifecycle.snapshot(ctx, f"uat-{ctx.run}-restore-point",
                                                {"Purpose": "uat-restore-point", "colvenna-uat/run": ctx.run})
        passed = []
        # The old instance and the restore point are the way back: deleted only once every check passed.
        ctx.on_cleanup(f"the restore point's snapshot {snap}, once every check passed",
                       lambda: lifecycle.delete_snapshot(ctx, snap) if passed else "kept: not every check passed")
        for table, commits in (("t1", 1), ("t3", 5)):
            code, _ = tools.run("writer", "write", db, table, "append", str(commits), "10", keep="tools.log")
            if code != 0:
                raise RuntimeError(f"writing {table} after the point: exit {code}")
        rest.drop_table(db, "gone")
        admin.principal(late, f"{ID}: made after the restore point")
        late_grant = admin.grant(late, "DATABASE", "READ", database=db)
        before_restore = lifecycle.record(ctx, tools, "writer", db, tables, [who])
        notes.append(f"{db}: t1 10 + 1 commits, t2 5, gone 3 (dropped), t3 5; the point: snapshot {snap} of {old_instance}")

    # --- the restore (user manual 6.2) -------------------------------------------------------------
    started = time.time()
    new_instance = f"colvenna-{ctx.install.env}-restored-{ctx.run[-6:]}"
    endpoint = lifecycle.restore(ctx, snap, new_instance)
    stack_db = ctx.install.output("Storage", "DbEndpoint")
    original = ctx.install.config_path
    config = lifecycle.restored_config(ctx, endpoint, original, original.parent / f"{ctx.install.env}-restored.env")
    guarded = []
    before = settled(ctx)
    without = lifecycle.restored_config(ctx, endpoint, original,
                                        ctx.scratch / f"{ctx.install.env}-restored-without-bucket.env", bucket=False)
    c, out = installer.run(ctx, checkout, without, "restore-without-bucket.log")
    guarded.append(("without COLVENNA_BUCKET", c, out, before, settled(ctx)))
    code, output = installer.run(ctx, checkout, config, "restore-install.log")
    ctx.log(f"install.sh onto the restored instance {new_instance}: exit {code}; "
            f"{time.time() - started:.0f} s from the restore's start (recorded, not judged)")
    ctx.install.use(config)
    # What the restored database does not know is not the case's to clean up.
    admin.forget_principal(late)
    admin.forget_grant(late_grant)
    sql = lifecycle.fresh_sql(ctx)
    before = settled(ctx)
    c, out = installer.run(ctx, checkout, create_config(ctx, config), "restore-db-create.log")
    guarded.append(("COLVENNA_DB=create", c, out, before, settled(ctx)))
    ctx.check("repointed", *judge_repointed(code, serving(ctx), endpoint, judge_signed_in(
        ctx.form, ctx.install.get("COLVENNA_DB_USER"), signed_in(ctx, sql))))
    ctx.check("repoint-guarded", *judge_guarded(guarded, stack_db))
    # The case's admin token may be unknown to the restored database: the one the install holds now.
    admin.tokens["admin"] = ctx.install.admin_token()
    at = lifecycle.record(ctx, tools, "writer", db, tables, [who])
    ctx.check("lists-as-at-point", *judge_lists(code, at["listing"]))
    ctx.check("reads-as-at-point", *judge_reads(at, point))
    ctx.check("history-as-at-point", *judge_history(at, point))

    # --- check and repair (user manual 6.3) ---------------------------------------------------------
    reports = {t: consistency(ctx, db, t) for t in ("t1", "t2", "gone")}
    ctx.keep("consistency-after-restore.json", reports)
    ctx.check("check-t1-behind", *judge_found(reports["t1"], "behind", "t1"))
    ctx.check("check-t2-consistent", *judge_found(reports["t2"], "consistent", "t2"))
    ctx.check("check-gone-missing", *judge_found(reports["gone"], "missing", "gone"))
    reconciled = admin.call("POST", f"/tables/{db}/t1/reconcile", expect=None)
    path = f"s3://{ctx.install.bucket}/{ctx.install.prefix}/{db}.db/t3"
    registered = rest.call("POST", f"/databases/{db}/register",
                           {"identifier": {"database": db, "object": "t3"}, "path": path}, expect=None)
    unregistered = admin.call("POST", f"/tables/{db}/gone/unregister", expect=None)
    ctx.log(f"reconcile t1 {reconciled.get('status')}; register t3 {registered.get('status')}; "
            f"unregister gone {unregistered.get('status')}")
    final = {t: consistency(ctx, db, t) for t in rest.tables(db)}
    ctx.keep("consistency-in-line.json", final)
    ctx.check("all-in-line", *judge_in_line(final))
    now = lifecycle.record(ctx, tools, "writer", db, tables, [who])
    ctx.check("history-as-before-restore", *judge_history_before(now, before_restore))
    w1, _ = tools.run("writer", "write", db, "t1", "append", "1", "10", keep="tools.log")
    w3, _ = tools.run("writer", "write", db, "t3", "append", "1", "10", keep="tools.log")
    ctx.check("takes-writes", *judge_takes_writes(w1, w3))
    principals = {p["name"] for p in admin.call("GET", "/principals")["body"].get("principals", [])}
    grants = [g["id"] for g in admin.grants()]
    ctx.check("identities-as-at-point", *judge_identities(principals, grants, late, late_grant))

    failed = any(status == "FAIL" for _, _, status, _ in ctx.verdicts.lines)
    if ctx.args.keep_old_instance or failed:
        ctx.log(f"kept ({'a check failed' if failed else 'as asked'}): the old instance {old_instance} and the "
                f"snapshot {snap}")
        return
    passed.append(True)
    final_snapshot = f"uat-{ctx.run}-old-final"
    ctx.on_cleanup(f"the old instance {old_instance} deleted, with its final snapshot",
                   lambda: lifecycle.delete_instance(ctx, old_instance, final_snapshot) + "; "
                   + lifecycle.delete_snapshot(ctx, final_snapshot))
