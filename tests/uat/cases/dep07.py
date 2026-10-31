"""UAT-DEP-07 Going back to the previous release.

After UAT-DEP-06 the install runs the candidate. Which way back runs depends on whether the candidate
added migrations to the previous release's (Migrations.VERSIONS of the two checkouts):

  same schema: a rollback while the loops of UAT-DEP-06 keep writing, reading and asking credentials.
    EKS: helm rollback of the release to the previous release's last revision, as the deployment
    guide says; serverless: the previous release's install.sh from a clean checkout of it, with the
    configuration as it takes it (keys it does not know left out).
  schema moved forward: going back as above must be refused (EKS: its pods never ready; serverless:
    its migrate refusing) with nothing written; then the pre-upgrade snapshot (UAT-DEP-06's, tagged)
    is restored as the user manual says, and the previous release installed on it.

Then the loops stop, the state is recorded and compared, and the install is upgraded to the
candidate again (forward-again), with UAT-DEP-06's checks for it. The pre-upgrade snapshot is
deleted at the end. The judging functions take what was collected and nothing else (test_dep07.py)."""

import json
import re
import subprocess
import time

from cases import dep06
from uatlib import faults, installer, lifecycle
from uatlib.install import parse_env
from uatlib.sql import Sql
from uatlib.tools import Tools

ID = "UAT-DEP-07"
FORMS = ("eks", "serverless")
AFTER = 300
SAME = ["same-schema-rolled-back", "same-schema-uninterrupted", "same-schema-data-kept"]
NEWER = ["newer-schema-refused", "newer-schema-nothing-written", "newer-schema-restored"]
TABLES = ["orders", "events"]


def add_arguments(p):
    p.add_argument("--candidate", required=True, help="the candidate's clean checkout (as UAT-DEP-06's)")
    p.add_argument("--previous", required=True, help="the previous release's clean checkout: its tag, or its commit")
    p.add_argument("--dep06-run", help="the run id of UAT-DEP-06 whose database and snapshot this case uses "
                                       "(default: the newest pre-upgrade snapshot of the install's instance)")


def branch(args):
    """same-schema or newer-schema, from the two checkouts; None when they are not given."""
    if not args or not getattr(args, "candidate", None) or not getattr(args, "previous", None):
        return None
    newer = installer.Checkout(args.candidate).migrations()
    older = installer.Checkout(args.previous).migrations()
    return "same-schema" if newer == older else "newer-schema"


def checks(form, args=None):
    b = branch(args)
    own = SAME if b == "same-schema" else NEWER if b == "newer-schema" else SAME + NEWER
    return own + ["forward-again"]


# --- judging ------------------------------------------------------------------------------------------

def judge_previous_serves(form, served, manifest, head):
    """What serves is the previous release: its image by digest (a candidate), or its commit (from
    source: EKS, the pods' source-sha; serverless, the image's tags)."""
    if form == "eks":
        if manifest:
            wrong = [p for p, image, _, _ in served if not str(image).endswith(manifest["image"]["digest"])]
        else:
            wrong = [p for p, _, _, sha in served if sha != head]
        return bool(served) and not wrong, f"{len(served)} pods; not the previous release's: {wrong}"
    want = manifest["image"]["digest"] if manifest else head
    wrong = [v for v, image, tags in served if not (str(image).endswith(want) or want in tags)]
    return bool(served) and not wrong, f"live {[v for v, _, _ in served]}; not the previous release's: {wrong}"


def judge_rolled_back(code, how, previous_serves):
    ok, detail = previous_serves
    return code == 0 and ok, f"{how}: exit {code}; {detail}"


def judge_uninterrupted(verdicts):
    """UAT-DEP-06's writes-, reads- and tokens-uninterrupted through the rollback."""
    return all(ok for ok, _ in verdicts.values()), "; ".join(f"{k}: {d}" for k, (_, d) in sorted(verdicts.items()))


def judge_data_kept(data, identities):
    return data[0] and identities[0], f"{data[1]}; {identities[1]}"


def names_versions(text, versions):
    """Whether a line of the text names the schema's version and the server's (newer, older): "schema
    version 3 is newer than this server (2)", "schema is at version 3, this server needs 2"."""
    newer, older = versions
    return bool(re.search(rf"version\s+{newer}\b[^\n]*?\b{older}\b", text or ""))


def judge_refused(form, code, ready, text, versions, live_before, live_after):
    """The previous version refused the newer schema, naming the versions (newer, older): EKS, none of
    its pods ever ready (ready: how many were) and their log naming both; serverless, its install.sh
    failing with both named, and the server function as it was (live: the alias live's version and the
    function's published versions; its migrate refused before the server function changed, and published
    none, #63)."""
    named = names_versions(text, versions)
    if form == "eks":
        return ready == 0 and named, (f"the previous release's pods ready: {ready}; the versions {versions} named: "
                                      f"{named}; the rollback exited {code}")
    same = live_before is not None and live_before == live_after
    return code != 0 and named and same, (f"the previous release's install.sh exited {code}; the versions {versions} "
                                          f"named: {named}; the alias live and the published versions {live_before} "
                                          f"then {live_after}")


def judge_nothing_written(before, after):
    """The schema version rows, the tables' pointers and the commit intents, unchanged by the refused
    version (database_state leaves out what the loops' writer did meanwhile through the candidate)."""
    return before == after, f"unchanged: {before == after}" + ("" if before == after else f"; {before} then {after}")


def judge_as_at_snapshot(at, point, tables):
    """After the restore every table lists and holds, snapshot by snapshot, what it did when UAT-DEP-06
    took the snapshot (point: its record from then; None when not found)."""
    if point is None:
        return False, "no record of UAT-DEP-06's from the snapshot's time"
    same = {t: lifecycle.same_snapshots(at, point, t) for t in tables}
    listed = sorted(at.get("listing") or []) == sorted(point.get("listing") or [])
    return listed and all(same.values()), f"listed as then: {listed}; snapshot by snapshot as then: {same}"


def judge_restored(code, previous_serves, as_at_snapshot, identities, consistent, seconds):
    ok = (code == 0 and previous_serves[0] and as_at_snapshot[0] and identities[0] and bool(consistent)
          and all(consistent))
    return ok, (f"the previous release installed on the restored instance: exit {code}; {previous_serves[1]}; "
                f"{as_at_snapshot[1]}; {identities[1]}; tables consistent once reconciled: {consistent}; "
                f"interruption {seconds:.0f} s (recorded, not judged)")


def judge_forward_again(code, steps, candidate, schema_rows, expected, loops, data, identities, token, audit):
    """The upgrade back to the candidate succeeds, and UAT-DEP-06's checks hold for it: the steps, what
    serves, the migrations, the loops, the data, the identities, a new token, the audit."""
    parts = (steps, candidate, loops, data, identities, token, audit)
    ok = code == 0 and len(schema_rows) == len(expected) and all(part[0] for part in parts)
    return ok, (f"install.sh exit {code}; {steps[1]}; runs the candidate: {candidate[1]}; schema {schema_rows} "
                f"(the candidate's {len(expected)}); {loops[1]}; {data[1]}; {identities[1]}; new token: {token[1]}; "
                f"audit: {audit[1]}")


# --- collecting ---------------------------------------------------------------------------------------

def previous_config(ctx, checkout):
    """The install's configuration as the previous release takes it: the keys its example.env lists."""
    mine = parse_env(ctx.install.config_path.read_text())
    known = known_keys(checkout)
    return lifecycle.write_config(ctx, ctx.scratch / f"{ctx.install.env}-previous.env",
                                  {k: v for k, v in mine.items() if k in known},
                                  "The install's configuration, as the previous release takes it")


def known_keys(checkout):
    return set(parse_env((checkout.path / "deploy" / "config" / "example.env").read_text()))


def live_version(ctx):
    """Serverless: (the version the server function's alias live is on, the function's published versions)."""
    fn = ctx.install.output("Serverless", "ServerFunctionName")
    alias = ctx.aws("lambda", "get-alias", "--function-name", fn, "--name", "live")["FunctionVersion"]
    published = ctx.aws("lambda", "list-versions-by-function", "--function-name", fn, "--query", "Versions[].Version") or []
    return alias, sorted(v for v in published if v != "$LATEST")


def previous_pods(ctx, previous):
    """EKS: how many of the previous release's pods are ready, and their logs (this run's and the one
    before, for a container that exits on refusing)."""
    ns, release = ctx.install.namespace, ctx.install.release
    pods = ctx.kube.json("get", "pods", "-n", ns, "-l",
                         f"app.kubernetes.io/name=colvenna-catalog,app.kubernetes.io/instance={release}")["items"]
    digest = (previous.manifest() or {}).get("image", {}).get("digest")
    ready, logs = 0, []
    for p in pods:
        ann = p["metadata"].get("annotations", {})
        image = next((c.get("imageID") or "" for c in p["status"].get("containerStatuses", [])
                      if c["name"] == "colvenna-catalog"), "")
        if not ((digest and image.endswith(digest)) or ann.get("colvenna-catalog/source-sha") == previous.head
                or (previous.candidate and ann.get("colvenna-catalog/release") == previous.candidate)):
            continue
        if any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", [])):
            ready += 1
        for extra in ((), ("--previous",)):
            logs.append(ctx.kube.kubectl("logs", p["metadata"]["name"], "-n", ns, "-c", "colvenna-catalog",
                                         "--tail", "300", *extra, check=False).stdout)
    return ready, "\n".join(logs)


def dep06_point(ctx, run_of):
    """UAT-DEP-06's record from just before it took the snapshot (its run's first), found beside this
    run's evidence; None when there is none."""
    for result in sorted(ctx.evidence.parent.glob("UAT-DEP-06*/result.json")):
        try:
            found = json.loads(result.read_text()).get("run") == run_of
        except ValueError:
            continue
        records = sorted(result.parent.glob("record-*.json")) if found else []
        if records:
            return lifecycle.load_record(records[0])
    return None


def helm_rollback(ctx, previous):
    """EKS: the release rolled back to the previous release's last revision (deployment guide 6.3): the
    revision whose chart is the previous candidate's, or else the one before the newest."""
    ns, release = ctx.install.namespace, ctx.install.release
    out = subprocess.run(["helm", "history", release, "--namespace", ns, "-o", "json"], stdout=subprocess.PIPE,
                         universal_newlines=True, env=ctx.kube.env, timeout=120)
    entries = json.loads(out.stdout or "[]")
    ctx.keep("helm-history.json", entries)
    target = None
    if previous.candidate:
        named = [e for e in entries if e.get("chart") == f"colvenna-catalog-{previous.candidate.lstrip('v')}"]
        target = named[-1]["revision"] if named else None
    if target is None and len(entries) >= 2:
        target = entries[-2]["revision"]
    if target is None:
        return 1, None
    p = subprocess.run(["helm", "rollback", release, str(target), "--namespace", ns, "--wait", "--timeout", "10m"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=ctx.kube.env,
                       timeout=900)
    ctx.keep("helm-rollback.log", p.stdout)
    return p.returncode, target


def served(ctx):
    """EKS: the running pods; serverless: [(the alias live's version, its image, the image's tags)]."""
    if ctx.form == "eks":
        return installer.running(ctx, ctx.install.namespace, ctx.install.release)
    (version, image), = dep06.served(ctx)
    tags = ctx.aws("ecr", "describe-images", "--repository-name", ctx.install.output("Images", "RepositoryName"),
                   "--image-ids", f"imageDigest={image.split('@')[-1]}")["imageDetails"][0].get("imageTags", [])
    return [(version, image, tags)]


def go_back(ctx, previous):
    """Rolls back as the form does; returns (exit code, how, output)."""
    if ctx.form == "eks":
        code, target = helm_rollback(ctx, previous)
        return code, f"helm rollback to revision {target}", ""
    code, output = installer.run(ctx, previous, previous_config(ctx, previous), "rollback.log")
    return code, "the previous release's install.sh", output


def database_state(ctx, db):
    """What a refused version must leave as it was: the schema version rows; each table's pointer less
    the loops' writer's published commits (the candidate goes on serving the loops: each of their commits
    moves both by one); the commit intents of anyone but the loops' writer, by state."""
    sql = Sql(ctx)
    w = lifecycle.LOOP_WRITER
    return (sql.query("SELECT version, script FROM colvenna_schema_version ORDER BY version"),
            sql.query("SELECT t.name, b.branch, b.snapshot_id - (SELECT count(*) FROM commit_intents i WHERE "  # nosec B608 - the harness's own names, no outside input
                      f"i.table_id = t.id AND i.branch = b.branch AND i.state = 'PUBLISHED' AND i.commit_user = '{w}') "
                      f"FROM table_branches b JOIN catalog_tables t ON t.id = b.table_id WHERE t.database_name = '{db}' "
                      "ORDER BY 1, 2"),
            sql.query(f"SELECT state, count(*) FROM commit_intents WHERE commit_user IS DISTINCT FROM '{w}' "  # nosec B608 - the harness's own names, no outside input
                      "GROUP BY 1 ORDER BY 1"))


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    candidate, previous = installer.Checkout(ctx.args.candidate), installer.Checkout(ctx.args.previous)
    b = branch(ctx.args)
    with ctx.preparing("the candidate upgraded by UAT-DEP-06, its snapshot, the loops running") as notes:
        lifecycle.reach_previous_release(ctx)
        run_of = ctx.args.dep06_run
        snaps = lifecycle.tagged_snapshots(ctx, dep06.SNAPSHOT_TAG, run_of) if run_of else \
            lifecycle.tagged_snapshots(ctx, "Purpose", "uat-pre-upgrade")
        if not snaps:
            raise RuntimeError("no pre-upgrade snapshot of UAT-DEP-06")
        snap = snaps[0]["DBSnapshotIdentifier"]
        run_of = run_of or {t["Key"]: t["Value"] for t in snaps[0].get("TagList", [])}.get(dep06.SNAPSHOT_TAG)
        db = f"upg_{run_of}"
        principals = {r: f"upg-{r}-{run_of}" for r in ("writer", "reader", "late")}
        ctx.on_cleanup(f"the pre-upgrade snapshot {snap} deleted", lambda: lifecycle.delete_snapshot(ctx, snap))
        ctx.on_cleanup(f"UAT-DEP-06's database {db} dropped", lambda: faults.drop_database(ctx, db))
        for role, name in principals.items():
            ctx.admin.adopt_principal(name)
            for g in ctx.admin.grants(name):
                ctx.admin.adopt_grant(g["id"])
            ctx.admin.issue(name, "PT4H", key=role)
        tools = Tools(ctx)
        time.sleep(5)
        before = lifecycle.record(ctx, tools, "reader", db, TABLES, principals.values())
        loops = lifecycle.Loops(ctx, tools, "writer", "reader", db, "events", TABLES, "back").start()
        notes.append(f"{b}: UAT-DEP-06's database {db} and snapshot {snap}; previous {previous.migrations()}, "
                     f"candidate {candidate.migrations()}")

    if b == "same-schema":
        code, how, _ = go_back(ctx, previous)
        ctx.check("same-schema-rolled-back", *judge_rolled_back(
            code, how, judge_previous_serves(ctx.form, served(ctx), previous.manifest(), previous.head)))
        time.sleep(AFTER)
        facts = loops.stop()
        ctx.check("same-schema-uninterrupted", *judge_uninterrupted(
            lifecycle.judge_loops(facts, loops.started, loops.stopped - lifecycle.SETTLE)))
        last = lifecycle.record(ctx, tools, "reader", db, TABLES, principals.values())
        ctx.check("same-schema-data-kept", *judge_data_kept(lifecycle.data_unchanged(before, last, writer_table="events"),
                                                            lifecycle.identities_unchanged(before, last)))
    else:
        last = newer_schema(ctx, previous, candidate, snap, loops, tools, db, principals, run_of)

    # --- forward again: the upgrade to the candidate, with UAT-DEP-06's checks ---------------------
    loops = lifecycle.Loops(ctx, tools, "writer", "reader", db, "events", TABLES, "fwd").start()
    began = time.time()
    code, output = installer.run(ctx, candidate, ctx.install.config_path, "forward-again.log")
    ended = time.time()
    time.sleep(AFTER)
    facts = loops.stop()
    rows = [r[0] for r in Sql(ctx).query("SELECT version FROM colvenna_schema_version ORDER BY version")]
    new_id, token = dep06.new_token(ctx, principals["late"], db, "forward-again-new-token.jsonl")
    after = lifecycle.record(ctx, tools, "reader", db, TABLES, principals.values())
    ctx.check("forward-again", *judge_forward_again(
        code, installer.steps_done(output),
        dep06.judge_runs_candidate(ctx.form, dep06.served(ctx), candidate.candidate, candidate.manifest(), candidate.head),
        rows, candidate.migrations(),
        judge_uninterrupted(lifecycle.judge_loops(facts, loops.started, loops.stopped - lifecycle.SETTLE)),
        lifecycle.data_unchanged(last, after, writer_table="events"),
        lifecycle.identities_unchanged(last, after, new_tokens=(new_id,)),
        dep06.judge_new_token(token), dep06.audit_continuous(ctx, last, began, ended + AFTER)))


def newer_schema(ctx, previous, candidate, snap, loops, tools, db, principals, run_of):
    """The schema moved forward: the previous release refused, then the pre-upgrade snapshot restored
    (UAT-DEP-10's procedure) and the previous release installed on it; the install then runs on the
    restored instance (its configuration <env>-restored.env beside the original). Returns the record
    after the restore."""
    versions = (len(candidate.migrations()), len(previous.migrations()))
    state = database_state(ctx, db)
    live_before = live_version(ctx) if ctx.form == "serverless" else None
    code, how, output = go_back(ctx, previous)
    if ctx.form == "eks":
        ready, text = previous_pods(ctx, previous)
        ctx.keep("refused-logs.txt", text)
        live_after = None
    else:
        ready, text, live_after = None, output, live_version(ctx)
    ctx.check("newer-schema-refused", *judge_refused(ctx.form, code, ready, text, versions, live_before, live_after))
    if ctx.form == "eks":
        # The release back on the candidate's pods, which went on serving (maxUnavailable 0).
        ctx.kube.kubectl("rollout", "undo", f"deployment/{ctx.install.release}", "-n", ctx.install.namespace, check=False)
        ctx.kube.kubectl("rollout", "status", f"deployment/{ctx.install.release}", "-n", ctx.install.namespace,
                         "--timeout=600s", check=False, timeout=660)
    ctx.check("newer-schema-nothing-written", *judge_nothing_written(state, database_state(ctx, db)))
    loops.stop()

    started = time.time()
    old_instance = lifecycle.instance(ctx)
    endpoint = lifecycle.restore(ctx, snap, f"colvenna-{ctx.install.env}-back-{ctx.run[-6:]}")
    original = ctx.install.config_path
    code, _ = installer.run(ctx, previous, lifecycle.restored_config(
        ctx, endpoint, original, ctx.scratch / f"{ctx.install.env}-previous-restored.env", known=known_keys(previous)),
        "restored-install.log")
    serves = judge_previous_serves(ctx.form, served(ctx), previous.manifest(), previous.head)
    ctx.install.use(lifecycle.restored_config(ctx, endpoint, original, original.parent / f"{ctx.install.env}-restored.env"))
    lifecycle.fresh_sql(ctx)
    # What the restored database knows: the install's admin token, the principals of the snapshot's time.
    ctx.admin.tokens["admin"] = ctx.install.admin_token()
    new_ids = tuple(ctx.admin.issue(principals[r], "PT4H", key=r) for r in ("writer", "reader"))
    time.sleep(5)
    at = lifecycle.record(ctx, tools, "reader", db, TABLES, principals.values())
    point = dep06_point(ctx, run_of)
    identities = lifecycle.identities_unchanged(point, at, new_tokens=new_ids) if point else (False, "no record")
    consistent = []
    for table in TABLES:
        ctx.admin.call("POST", f"/tables/{db}/{table}/reconcile", expect=None)
        consistent.append(faults.consistency(ctx, db, table).get("consistent") is True)
    ctx.check("newer-schema-restored", *judge_restored(code, serves, judge_as_at_snapshot(at, point, TABLES), identities,
                                                       consistent, time.time() - started))
    ctx.log(f"the install runs on the restored instance now; {old_instance}, the one before it, is left to UAT-DEP-09")
    return lifecycle.record(ctx, tools, "reader", db, TABLES, principals.values())
