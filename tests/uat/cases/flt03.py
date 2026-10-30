"""UAT-FLT-03 A table written outside the catalog is fenced, and reconcile releases it.

A table of the case's own (flt3_<run>.t) takes two commits through the catalog. A writer that
bypasses the catalog then commits one snapshot straight to the table's files: a Paimon filesystem
table on the same path (tests/uat/paimon Fence), with the credentials of an IAM role of the case's
own that may write the table's prefix and nothing else, as a writer bypassing the catalog would
have; the role is deleted at the end. The next commit through the catalog must be refused (409, on
every try: the client's first request and each of its retries, after which it fails; the table
fenced and audited FENCE_TABLE) while the catalog keeps serving its own last
snapshot; once the outside writer has stopped, reconcile publishes the outside snapshot and lifts
the fence, and commits go through again."""

import json
import re
import time

from uatlib import faults
from uatlib.tools import Tools

ID = "UAT-FLT-03"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5
ROWS = 10  # per commit through the catalog


def checks(form, args=None):
    return ["fenced", "catalog-reads-hold", "reconcile-releases", "commits-again"]


def fact(facts, kind):
    found = [f for f in facts if f.get("kind") == kind]
    return found[-1] if found else {}


def writer_commits(events, writer):
    """The writer's COMMIT events among the table's own (found by resource=<db>.<table>): a refused
    commit's event names the table with its database, as every event on a table does (#90)."""
    return [e for e in events if e.get("action") == "COMMIT" and e.get("principal") == writer]


# --- the judges ---------------------------------------------------------------------------------------

def judge_fenced(outside, refused, commits, events):
    """The outside snapshot written; the catalog's commit refused with 409 on every try: the Paimon
    client sends its commit request once and then once per retry (commit.max-retries, the tool's
    "retries"), each answered 409, none accepted; the client fails after its retries, the catalog's
    "fenced and accepts no commits" its innermost cause (#66); the fence audited (FENCE_TABLE).
    outside: the outside writer's snapshot id; refused: the fenced-commit tool's line; commits: the
    writer's COMMIT events after the outside write; events: the table's audit events then."""
    retries = refused.get("retries")
    statuses = [e.get("httpStatus") for e in commits]
    fences = [e for e in events if e.get("action") == "FENCE_TABLE"]
    message, error = str(refused.get("message", "")), str(refused.get("error", ""))
    ok = (outside == 3 and refused.get("failed") is True and isinstance(retries, int)
          and statuses == [409] * (retries + 1) and f"with {retries} retries" in message
          and "fenced and accepts no commits" in error and bool(fences))
    return ok, (f"outside snapshot {outside}; the catalog's commit failed: {refused.get('failed')} "
                f"({message[:100]}; {error[:160]}); {len(statuses)} commit requests answered {sorted(set(statuses))} "
                f"for {retries} retries; {len(fences)} FENCE_TABLE events")


def judge_reads(reads):
    """loadSnapshot and a batch read stay at the catalog's last snapshot (2, of 2 commits)."""
    ok = reads.get("snapshot") == 2 and reads.get("rows") == 2 * ROWS
    return ok, (f"loadSnapshot {reads.get('snapshot')} (the catalog's last: 2), a batch read {reads.get('rows')} rows "
                f"({2 * ROWS})")


def judge_reconcile(status, body, before, after):
    """Reconcile's report: the state FENCED, main from the catalog's last snapshot to the outside one."""
    branches = {b.get("branch"): b for b in (body or {}).get("branches", [])}
    main = branches.get("main", {})
    ok = (status == 200 and (body or {}).get("stateBefore") == "FENCED" and main.get("before") == before
          and main.get("after") == after)
    return ok, (f"{status}: state before {(body or {}).get('stateBefore')}; main {main.get('before')} -> "
                f"{main.get('after')} (want {before} -> {after}); branches {sorted(branches)}")


def judge_commits_again(code, made, consistency):
    """The commit after reconcile went through (snapshot 4), and the table is consistent."""
    ok = code == 0 and made == [4] and consistency.get("consistent") is True
    return ok, (f"commit exit {code}, snapshots {made}; consistent {consistency.get('consistent')}, "
                f"problems {consistency.get('problems')}")


# --- the outside writer's identity ------------------------------------------------------------------

def caller_role(ctx):
    """The operator's role (an assumed role's ARN turned into its role's), whom the outside role trusts."""
    arn = ctx.aws("sts", "get-caller-identity")["Arn"]
    m = re.match(r"^arn:(aws[a-z-]*):sts::(\d{12}):assumed-role/([^/]+)/", arn)
    if not m:
        return arn
    return ctx.aws("iam", "get-role", "--role-name", m.group(3))["Role"]["Arn"]


def outside_role(ctx, table_path):
    """An IAM role of the case's own that may read and write the table's prefix, and nothing else;
    its credentials for 1 hour. Deleted at the clean-up."""
    m = re.match(r"^s3a?://([^/]+)/(.+?)/?$", table_path)
    bucket, prefix = m.group(1), m.group(2)
    name = f"colvenna-uat-outside-{ctx.run}"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": caller_role(ctx)},
                                                       "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*", prefix]}}}]}
    ctx.aws("iam", "create-role", "--role-name", name, "--assume-role-policy-document", json.dumps(trust),
            "--description", f"{ID} {ctx.run}: a writer bypassing the catalog, on one table's prefix",
            "--tags", "Key=Purpose,Value=uat", f"Key=colvenna-uat/run,Value={ctx.run}")

    def delete():
        ctx.aws.raw("iam", "delete-role-policy", "--role-name", name, "--policy-name", "table-prefix", check=False)
        ctx.aws("iam", "delete-role", "--role-name", name)
        return f"role {name} deleted"

    ctx.on_cleanup(f"the outside writer's role {name}", delete)
    ctx.aws("iam", "put-role-policy", "--role-name", name, "--policy-name", "table-prefix",
            "--policy-document", json.dumps(policy))
    role = ctx.aws("iam", "get-role", "--role-name", name)["Role"]["Arn"]
    for attempt in range(24):  # a new role takes a little while to be assumable
        p = ctx.aws.raw("sts", "assume-role", "--role-arn", role, "--role-session-name", f"uat-outside-{ctx.run}",
                        "--duration-seconds", "3600", "--output", "json", check=False)
        if p.returncode == 0:
            creds = json.loads(p.stdout)["Credentials"]
            out = {"accessKeyId": creds["AccessKeyId"], "secretAccessKey": creds["SecretAccessKey"],
                   "sessionToken": creds["SessionToken"], "region": ctx.install.region}
            ctx.sanitizer.secret_sources.append(lambda c=out: [c["secretAccessKey"], c["sessionToken"]])
            return out
        time.sleep(5)
    raise RuntimeError(f"the role {name} could not be assumed: {p.stderr[-300:]}")


def run(ctx):
    db, table = f"flt3_{ctx.run}", "t"
    writer = f"flt3-writer-{ctx.run}"
    admin, rest = ctx.admin, ctx.rest
    with ctx.preparing("a table with commits through the catalog, and the outside writer's identity") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: faults.drop_database(ctx, db))
        admin.principal(writer, f"{ID}: the catalog's writer")
        admin.issue(writer, "PT2H", key="writer")
        admin.grant(writer, "DATABASE", "MANAGE", database=db)
        time.sleep(CACHE_WAIT)
        tools = Tools(ctx)
        code, facts = tools.run("writer", "write", db, table, "append", "2", str(ROWS), keep="tools.log")
        made = fact(facts, "written").get("snapshots", [])
        if code != 0 or made != [1, 2]:
            raise RuntimeError(f"the table's two commits: exit {code}, snapshots {made}")
        path = rest.get_table(db, table)["path"]
        creds = outside_role(ctx, path)
        notes.append(f"{db}.{table}: snapshots 1, 2; an IAM role of the case's own on its prefix")
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))

    # --- step 1: the outside writer's snapshot -------------------------------------------------------
    code, facts = tools.run("writer", "outside", db, table, keep="tools.log", more=json.dumps(creds))
    outside = fact(facts, "outside").get("snapshot")
    ctx.log(f"the outside writer: exit {code}, snapshot {outside}")

    # --- step 2: a commit through the catalog, and reads --------------------------------------------
    code, facts = tools.run("writer", "fenced-commit", db, table, keep="tools.log")
    refused = fact(facts, "fenced-commit")
    events = admin.audit(resource=f"{db}.{table}", since=started)
    commits = writer_commits(events, writer)
    ctx.keep("audit-after-the-fence.json", {"table": events, "commits": commits})
    ctx.check("fenced", *judge_fenced(outside, refused, commits, events))
    code, facts = tools.run("writer", "reads", db, table, keep="tools.log")
    ctx.check("catalog-reads-hold", *judge_reads(fact(facts, "reads")))

    # --- step 3: the outside writer stopped (it made one commit and ended); reconcile; commit again ---
    report = admin.call("POST", f"/tables/{db}/{table}/reconcile", expect=None)
    ctx.keep("reconcile.json", report)
    ctx.check("reconcile-releases", *judge_reconcile(report.get("status"), report.get("body"), 2, 3))
    code, facts = tools.run("writer", "write", db, table, "append", "1", str(ROWS), keep="tools.log")
    consistency = admin.consistency(db, table)
    ctx.keep("consistency.json", consistency)
    ctx.check("commits-again", *judge_commits_again(code, fact(facts, "written").get("snapshots", []), consistency))
