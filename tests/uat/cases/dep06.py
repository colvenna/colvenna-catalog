"""UAT-DEP-06 Online upgrade from the previous release.

The install runs the previous release (installed from that release's own checkout) and is in use:
database upg_<run> with a primary-key table and an append table, three principals of the case's own
(a writer, a reader, and one used only after the upgrade) with tokens issued before it, and their
grants. The case records the install's state, takes a manual snapshot of the metadata database as the
deployment guide says to (kept, tagged, for UAT-DEP-07), starts a writer committing every 2 s, a reader
reading every table every second and a credential loop asking each table's credentials every 5 s and
reading with them (tests/uat/paimon Loops, all with the tokens from before), and upgrades: install.sh
--yes from the candidate's checkout (--candidate), with the install's configuration. The loops go on
5 minutes after it exits; a new token of the third principal is then issued and used, and the state
recorded again and compared. Once the upgrade ran, the database, principals and grants stay with the
snapshot for UAT-DEP-07, which goes back with them and removes them."""

import time

from uatlib import faults, installer, lifecycle
from uatlib.probe import describe
from uatlib.tools import Tools

ID = "UAT-DEP-06"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5
AFTER = 300
SNAPSHOT_TAG = "colvenna-uat/pre-upgrade"


def add_arguments(p):
    p.add_argument("--candidate", required=True,
                   help="a clean checkout of the candidate to upgrade to: its tag (--release), or a commit (--from-source)")
    p.add_argument("--previous", help="the previous release's checkout, recorded with the run (default: not checked)")


def checks(form, args=None):
    return ["install-exit", "every-step-done", "runs-the-candidate", "writes-uninterrupted", "reads-uninterrupted",
            "tokens-uninterrupted", "migration-chain-forward", "data-unchanged", "grants-and-identities-unchanged",
            "audit-continuous", "new-token-works"]


def prepare(ctx, db, principals, upgraded):
    """The case's database, tables, principals, tokens and grants, as the previous release serves them.
    Once the upgrade ran (upgraded not empty), they are UAT-DEP-07's: it goes back with them, and
    removes them."""
    admin, rest = ctx.admin, ctx.rest
    rest.create_database(db)
    ctx.on_cleanup(f"database {db} dropped, unless the upgrade ran (then UAT-DEP-07's)",
                   lambda: "kept for UAT-DEP-07" if upgraded else faults.drop_database(ctx, db))
    for role, name in principals.items():
        admin.principal(name, f"{ctx.id} {role}")
        admin.issue(name, "P1D", key=role)
    admin.grant(principals["writer"], "DATABASE", "MANAGE", database=db)
    admin.grant(principals["reader"], "DATABASE", "READ", database=db)
    admin.grant(principals["late"], "DATABASE", "READ", database=db)
    time.sleep(CACHE_WAIT)
    tools = Tools(ctx)
    for table, kind, commits in (("orders", "pk", 3), ("events", "append", 3)):
        code, facts = tools.run("writer", "write", db, table, kind, str(commits), "20", keep="tools.log")
        if code != 0:
            raise RuntimeError(f"writing {table}: exit {code}")
    return tools


# --- judging ------------------------------------------------------------------------------------------

def judge_install_exit(code):
    return code == 0, f"install.sh exited {code}"


def judge_runs_candidate(form, served, candidate, manifest, head):
    """What serves is the candidate, by digest. EKS: served is [(pod, image, release annotation,
    source-sha annotation)]; serverless: [(the alias's version, its image)] for the server function. A
    source checkout (no manifest) is no candidate, so never passes."""
    if not manifest:
        return False, (f"installed from source at {head[:12]}: no candidate's manifest to judge by "
                       f"(served: {[s[0] for s in served]})")
    digest, built = manifest["image"]["digest"], manifest["source"]["built_from"]
    if form == "eks":
        wrong = [p for p, image, rel, sha in served if not str(image).endswith(digest) or rel != candidate or sha != built]
    else:
        wrong = [v for v, image in served if not str(image).endswith(digest)]
    return bool(served) and not wrong, f"{len(served)} serving; not the candidate's {digest[:19]}: {wrong}"


def judge_new_token(result):
    return result.get("status") == 200, f"a token issued after the upgrade: {describe(result)[:120]}"


def judge_audit(kept, latest, window):
    """The latest event from before is still there; events of the window are recorded."""
    return kept and window > 0, f"the latest event before ({latest}) still there: {kept}; {window} events in the window"


# --- collecting ---------------------------------------------------------------------------------------

def served(ctx):
    """What serves now: EKS, the running pods; serverless, the alias live's version and its image."""
    if ctx.form == "eks":
        return installer.running(ctx, ctx.install.namespace, ctx.install.release)
    fn = ctx.install.output("Serverless", "ServerFunctionName")
    alias = ctx.aws("lambda", "get-alias", "--function-name", fn, "--name", "live")
    conf = ctx.aws("lambda", "get-function", "--function-name", fn, "--qualifier", alias["FunctionVersion"])
    return [(alias["FunctionVersion"], conf.get("Code", {}).get("ResolvedImageUri", ""))]


def upgrade_checks(ctx, code, output, checkout):
    ctx.check("install-exit", *judge_install_exit(code))
    ctx.check("every-step-done", *installer.steps_done(output))
    ctx.check("runs-the-candidate", *judge_runs_candidate(ctx.form, served(ctx), checkout.candidate, checkout.manifest(),
                                                          checkout.head))


def new_token(ctx, principal, db, keep):
    """A token issued to the principal now, and the probe's answer to a table read with it: (id, result)."""
    new_id = ctx.admin.issue(principal, "PT1H", key="late-new")
    time.sleep(CACHE_WAIT)
    t = ctx.probe.run([{"name": "new-token", "url": f"{ctx.install.rest_base}/databases/{db}/tables/orders",
                        "token": "late-new"}], tokens=ctx.admin.tokens, keep=keep)[0]
    return new_id, t


def audit_continuous(ctx, before, since, until):
    # The events up to a second after the latest one recorded (the record takes minutes, and its reads
    # are audited: the latest event is from its end).
    old = ctx.admin.audit(until=lifecycle.stamp(lifecycle.parse(before["auditLatestTime"]) + 1), limit=1000)
    kept = any(e["id"] == before["auditLatest"] for e in old)
    window = ctx.admin.audit(since=lifecycle.stamp(since), until=lifecycle.stamp(until), limit=1000)
    return judge_audit(kept, before["auditLatest"], len(window))


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    # #49: the previous release's private CA may have no keyUsage (v1.0.0-rc.1's has none, until the
    # candidate's install.sh issues it again with its extensions): the probe reaches it with Python's strict
    # X.509 checks off, the chain and the names still verified.
    ctx.probe.strict = False
    db = f"upg_{ctx.run}"
    principals = {r: f"upg-{r}-{ctx.run}" for r in ("writer", "reader", "late")}
    candidate = installer.Checkout(ctx.args.candidate)
    upgraded = []
    with ctx.preparing("the previous release in use, recorded, its snapshot taken, the loops running") as notes:
        tools = prepare(ctx, db, principals, upgraded)
        before = lifecycle.record(ctx, tools, "reader", db, ["orders", "events"], principals.values())
        name = f"uat-{ctx.run}-pre-upgrade"
        inst, snap = lifecycle.snapshot(ctx, name, {SNAPSHOT_TAG: ctx.run, "Purpose": "uat-pre-upgrade"})
        # The snapshot is UAT-DEP-07's once the upgrade ran; before that, nothing will use it.
        ctx.on_cleanup(f"the pre-upgrade snapshot {snap}, unless the upgrade ran (then UAT-DEP-07's)",
                       lambda: "kept for UAT-DEP-07" if upgraded else lifecycle.delete_snapshot(ctx, snap))
        loops = lifecycle.Loops(ctx, tools, "writer", "reader", db, "events", ["orders", "events"], "up").start()
        notes.append(f"{db}: orders (pk), events (append); snapshot {snap} of {inst}, kept for UAT-DEP-07")

    began = time.time()
    upgraded.append(began)
    # From here the database, principals and grants are UAT-DEP-07's (it adopts them).
    for name in principals.values():
        ctx.admin.forget_principal(name)
    for gid in list(ctx.admin.made_grants):
        ctx.admin.forget_grant(gid)
    code, output = installer.run(ctx, candidate, ctx.install.config_path, "upgrade.log")
    ended = time.time()
    ctx.log(f"the upgrade exited {code} after {ended - began:.0f} s; the loops go on {AFTER} s")
    time.sleep(AFTER)
    facts = loops.stop()
    for check, verdict in lifecycle.judge_loops(facts, loops.started, loops.stopped - lifecycle.SETTLE).items():
        ctx.check(check, *verdict)
    upgrade_checks(ctx, code, output, candidate)

    new_id, t = new_token(ctx, principals["late"], db, "new-token.jsonl")
    ctx.check("new-token-works", *judge_new_token(t))

    after = lifecycle.record(ctx, tools, "reader", db, ["orders", "events"], principals.values())
    ctx.check("migration-chain-forward", *lifecycle.migration_forward(before, after, candidate.migrations()))
    ctx.check("data-unchanged", *lifecycle.data_unchanged(before, after, writer_table="events"))
    ctx.check("grants-and-identities-unchanged", *lifecycle.identities_unchanged(before, after, new_tokens=(new_id,)))
    ctx.check("audit-continuous", *audit_continuous(ctx, before, began, ended + AFTER))
