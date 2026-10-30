"""UAT-CAT-05 Admin API.

Every operation of the admin API once, from a client pod with the admin's token, on objects of the
case's own (principal adm-<run>, its tokens and grants, database cat05_<run> with table t), then the
invalid variants; every answer held to the OpenAPI description (docs/api/admin-v1.yaml); every
operation refused to a service principal; and the user manual's examples (docs/manual/admin-api.md)
run as written, on this host (EKS) or relayed to the client pod (serverless; uatlib/manual.py). The
judging functions take what was collected and nothing else (test_cat05.py)."""

import os
import signal
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from uatlib import manual
from uatlib.admin import APPROVER, PURPOSE, in_hours
from uatlib.openapi import Description
from uatlib.probe import describe

ID = "UAT-CAT-05"
FORMS = ("eks", "serverless")
CACHE_WAIT = 5
AUDIT_LIMIT = 1000


def checks(form, args=None):
    return ["principals-create-list", "token-issue", "token-ttl-too-long", "tokens-list", "token-revoke",
            "principal-disable", "grant-create-list-delete", "grant-needs-approval", "audit-filters", "consistency",
            "reconcile", "unregister", "manual-examples", "openapi-matches", "service-identity-refused"]


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def body_of(result):
    body = (result or {}).get("body")
    return body if isinstance(body, dict) else {}


# --- judging ------------------------------------------------------------------------------------------

def judge_create_list(created, listed, name):
    names = [p.get("name") for p in body_of(listed).get("principals", [])]
    return (created or {}).get("status") == 200 and name in names, f"create {describe(created)[:80]}; listed: {name in names}"


def judge_issue(issued, listing, now):
    """200 with a colvenna_ token, never shown again in the listing; without ttl it expires in 90 days."""
    token = body_of(issued).get("token", "")
    expires = body_of(issued).get("expiresAt")
    days = (epoch(expires) - now) / 86400 if expires is not None else None
    again = bool(token) and token in str((listing or {}).get("body"))
    ok = (issued or {}).get("status") == 200 and token.startswith("colvenna_") and days is not None and abs(days - 90) < 0.1 \
        and not again
    return ok, (f"{(issued or {}).get('status')}; a colvenna_ token: {token.startswith('colvenna_')}; expires in "
                f"{days and round(days, 2)} days; shown again in the listing: {again}")


def judge_tokens_list(listing, token):
    tokens = body_of(listing).get("tokens", [])
    keys = {k for t in tokens for k in t}
    hashed = [k for k in keys if "hash" in k.lower() or k.lower() == "token"]
    shown = bool(token) and token in str((listing or {}).get("body"))
    ok = (listing or {}).get("status") == 200 and bool(tokens) and {"id", "expiresAt", "revokedAt"} <= keys \
        and not hashed and not shown
    return ok, f"{len(tokens)} tokens with {sorted(keys)}; fields holding a token or its hash: {hashed}; the token: {shown}"


def judge_revoke(before, revoked, after):
    ok = (before or {}).get("status") == 200 and (revoked or {}).get("status") in (200, 204) \
        and (after or {}).get("status") == 401
    return ok, f"before {(before or {}).get('status')}; revoke {describe(revoked)[:60]}; after {(after or {}).get('status')}"


def judge_disable(disabled, statuses):
    ok = (disabled or {}).get("status") == 200 and statuses and all(s == 401 for s in statuses)
    return ok, f"disable {describe(disabled)[:60]}; its tokens: {statuses}"


def judge_grant_cycle(made, listed, deleted, after):
    gid = body_of(made).get("id")
    before_ids = [g.get("id") for g in body_of(listed).get("grants", [])]
    after_ids = [g.get("id") for g in body_of(after).get("grants", [])]
    ok = (made or {}).get("status") == 200 and gid is not None and gid in before_ids \
        and (deleted or {}).get("status") in (200, 204) and gid not in after_ids
    return ok, (f"create {(made or {}).get('status')} (id {gid}); listed {gid in before_ids}; delete "
                f"{(deleted or {}).get('status')}; listed after {gid in after_ids}")


def judge_needs_approval(missing):
    """missing: {field: the answer to the grant without it}."""
    found = {k: ((r or {}).get("status"), k in str((r or {}).get("body"))) for k, r in missing.items()}
    ok = len(found) == 4 and all(s == 400 and named for s, named in found.values())
    return ok, "; ".join(f"without {k}: {s}{' naming it' if n else ''}" for k, (s, n) in found.items())


def judge_audit_filters(queries, window, since, until, over, total):
    """queries: [(parameter, field, value, answer)]; window: the since/until answer; over: limit over 1000
    when the audit holds more than 1000 events (total)."""
    problems = []
    for key, field, value, r in queries:
        events = body_of(r).get("events", [])
        wrong = [e for e in events if e.get(field) != value]
        times = [epoch(e["time"]) for e in events]
        if (r or {}).get("status") != 200 or not events or wrong or times != sorted(times, reverse=True):
            problems.append(f"{key}={value}: {(r or {}).get('status')}, {len(events)} events, {len(wrong)} not matching, "
                            f"newest first {times == sorted(times, reverse=True)}")
    events = body_of(window).get("events", [])
    times = [epoch(e["time"]) for e in events]
    outside = [t for t in times if not (epoch(since) <= t < epoch(until))]
    if (window or {}).get("status") != 200 or not events or outside or times != sorted(times, reverse=True):
        problems.append(f"since/until: {len(events)} events, {len(outside)} outside the range")
    n = len(body_of(over).get("events", []))
    if total <= AUDIT_LIMIT:
        problems.append(f"only {total} events in the audit: limit over {AUDIT_LIMIT} cannot be judged")
    elif (over or {}).get("status") != 200 or n != AUDIT_LIMIT:
        problems.append(f"limit over {AUDIT_LIMIT}: {(over or {}).get('status')}, {n} events of {total}")
    return not problems, "; ".join(problems) or (f"{len(queries)} filters and since/until each gave only matching events, "
                                                 f"newest first; limit over {AUDIT_LIMIT} gave {n} of {total}")


def judge_consistency(report):
    body = body_of(report)
    ok = (report or {}).get("status") == 200 and body.get("consistent") is True and not body.get("problems")
    return ok, f"{(report or {}).get('status')}: consistent {body.get('consistent')}, problems {body.get('problems')}"


KEPT = ("id", "schemaId", "path")


def judge_reconcile(reconciled, before, after):
    same = {k: before.get(k) for k in KEPT} == {k: after.get(k) for k in KEPT}
    return (reconciled or {}).get("status") == 200 and same, f"{describe(reconciled)[:200]}; the table as before: {same}"


def judge_unregister(unregistered, listed_after, registered, before, back):
    same = bool(back) and all(back.get(k) == before.get(k) for k in ("schemaId", "path"))
    ok = (unregistered or {}).get("status") in (200, 204) and not listed_after and (registered or {}).get("status") == 200 \
        and same
    return ok, (f"unregister {(unregistered or {}).get('status')}; listed after {listed_after}; registered again "
                f"{(registered or {}).get('status')} with the same files and schema: {same}")


def judge_service_refused(refusals):
    wrong = [f"{op}: {s}" for op, s in refusals if s != 403]
    return bool(refusals) and not wrong, (f"{len(refusals)} operations; not 403: {wrong}" if wrong
                                          else f"{len(refusals)} operations, each 403")


def judge_manual(returncode, output, script):
    """The examples ran to their end, every block of them, and the unauthenticated request got 401."""
    ran = output.count("### manual block ")
    expected = script.count("echo '### manual block ")
    lines = output.splitlines()
    unauth = any(line.startswith("### manual block") and lines[i + 1].strip() == "401"
                 for i, line in enumerate(lines[:-1]))
    ok = returncode == 0 and ran == expected and expected > 0 and unauth
    return ok, (f"{ran} of {expected} blocks ran, exit {returncode}; the unauthenticated request got "
                f"{'401' if unauth else 'something else'}" + ("" if returncode == 0 else f"; last: {lines[-3:]}"))


def judge_openapi(spec, seen):
    problems = [p for method, path, status, body in seen for p in spec.check(method, path, status, body)]
    return bool(seen) and not problems, (f"{len(seen)} answers; {len(problems)} not as described"
                                         + (f": {problems[:3]}" if problems else "")), problems


# --- the run ------------------------------------------------------------------------------------------

class Calls:
    """The case's requests to the admin API (and others), each admin answer kept for the OpenAPI check."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.admin_base = ctx.install.admin_base
        self.seen = []  # (method, path, status, body) of the admin API's answers

    def __call__(self, method, path, body=None, token="admin", base=None):
        step = {"name": f"{method} {path}", "method": method, "url": (base or self.admin_base) + path, "token": token,
                "full": True}
        if body is not None:
            step["body"] = body
        r = self.ctx.probe.run([step], tokens=self.ctx.admin.tokens, keep="calls.jsonl")[0]
        if base is None and "status" in r:
            self.seen.append((method, path, r["status"], r.get("body")))
        return r


def run(ctx):
    admin, rest = ctx.admin, ctx.rest
    call = Calls(ctx)
    db, table = f"cat05_{ctx.run}", "t"
    adm, svc, dis = f"adm-{ctx.run}", f"adm-svc-{ctx.run}", f"adm-dis-{ctx.run}"
    with ctx.preparing("database, table and a service principal") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT"), ("v", "STRING")])
        admin.principal(svc, f"{ID} service principal")
        admin.issue(svc, "PT2H", key="svc")
        notes.append(f"{db}.{table}, {svc}")
    started = time.time()
    config = f"/v1/config?warehouse={ctx.install.warehouse}"

    # --- principals and tokens ----------------------------------------------------------------------
    created = call("POST", "/principals", {"name": adm, "description": ID})
    admin.adopt_principal(adm)
    ctx.check("principals-create-list", *judge_create_list(created, call("GET", "/principals"), adm))
    issued = call("POST", f"/principals/{adm}/tokens", {})
    if body_of(issued).get("token"):
        admin.tokens["adm-1"] = body_of(issued)["token"]
    listing = call("GET", f"/principals/{adm}/tokens")
    ctx.check("token-issue", *judge_issue(issued, listing, time.time()))
    too_long = call("POST", f"/principals/{adm}/tokens", {"ttl": "P401D"})
    ctx.check("token-ttl-too-long", too_long.get("status") == 400, f"ttl P401D: {describe(too_long)}")
    ctx.check("tokens-list", *judge_tokens_list(listing, admin.tokens.get("adm-1")))

    second = call("POST", f"/principals/{adm}/tokens", {"ttl": "PT1H", "description": "to be revoked"})
    second_id = body_of(second).get("id")
    admin.tokens["adm-2"] = body_of(second).get("token", "")
    before = call("GET", config, token="adm-2", base=ctx.install.uri)
    revoked = call("DELETE", f"/tokens/{urllib.parse.quote(str(second_id))}")
    time.sleep(CACHE_WAIT)
    after = call("GET", config, token="adm-2", base=ctx.install.uri)
    ctx.check("token-revoke", *judge_revoke(before, revoked, after))

    call("POST", "/principals", {"name": dis, "description": f"{ID} disabled"})
    admin.adopt_principal(dis)
    for k in ("dis-1", "dis-2"):
        admin.tokens[k] = body_of(call("POST", f"/principals/{dis}/tokens", {"ttl": "PT1H"})).get("token", "")
    disabled = call("POST", f"/principals/{dis}/disable")
    time.sleep(CACHE_WAIT)
    statuses = [call("GET", config, token=k, base=ctx.install.uri).get("status") for k in ("dis-1", "dis-2")]
    ctx.check("principal-disable", *judge_disable(disabled, statuses))

    # --- grants -------------------------------------------------------------------------------------
    grant = {"principal": adm, "scope": "TABLE", "database": db, "object": table, "privilege": "READ",
             "purpose": f"{PURPOSE}: {ID}", "approvedBy": APPROVER, "approval": ID, "expiresAt": in_hours(24)}
    made = call("POST", "/grants", grant)
    gid = body_of(made).get("id")
    if gid is not None:
        admin.adopt_grant(gid)
    listed = call("GET", f"/grants?principal={adm}")
    deleted = call("DELETE", f"/grants/{gid}")
    if deleted.get("status") in (200, 204):
        admin.forget_grant(gid)
    ctx.check("grant-create-list-delete", *judge_grant_cycle(made, listed, deleted, call("GET", f"/grants?principal={adm}")))
    missing = {}
    for name in ("purpose", "approvedBy", "approval", "expiresAt"):
        missing[name] = call("POST", "/grants", {k: v for k, v in grant.items() if k != name})
        if missing[name].get("status") == 200 and body_of(missing[name]).get("id") is not None:
            admin.adopt_grant(body_of(missing[name])["id"])
    ctx.check("grant-needs-approval", *judge_needs_approval(missing))

    # --- the audit: filters, the time range, newest first, and the limit -----------------------------
    reader_grant = admin.grant(adm, "TABLE", "READ", database=db, obj=table)
    total = audit_total(ctx, call)
    if total <= AUDIT_LIMIT:
        # Enough audited reads of the case's own table that a limit over 1000 has more to cut.
        ctx.probe.run([{"name": "audited-read", "url": f"{ctx.install.rest_base}/databases/{db}/tables/{table}",
                        "token": "adm-1", "repeat": {"every": 0, "for": 60}}], tokens=admin.tokens, keep="audited-reads.jsonl",
                      timeout=600)
        total = audit_total(ctx, call)
    admin.delete_grant(reader_grant)
    since = datetime.fromtimestamp(started - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.fromtimestamp(time.time() + 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    queries = []
    for key, field, value in (("principal", "principal", "admin"), ("action", "action", "CREATE_PRINCIPAL"),
                              ("resourceType", "resourceType", "GRANT"), ("resource", "resource", f"{adm}@TABLE:{db}.{table}"),
                              ("outcome", "outcome", "FAILED")):
        queries.append((key, field, value, call("GET", f"/audit?{key}={urllib.parse.quote(value)}&since={since}&limit=1000")))
    window = call("GET", f"/audit?since={since}&until={until}&limit=1000")
    over = call("GET", f"/audit?limit={AUDIT_LIMIT * 5}")
    ctx.check("audit-filters", *judge_audit_filters(queries, window, since, until, over, total))

    # --- consistency, reconcile, unregister -----------------------------------------------------------
    ctx.check("consistency", *judge_consistency(call("GET", f"/consistency/{db}/{table}")))
    before = rest.get_table(db, table)
    reconciled = call("POST", f"/tables/{db}/{table}/reconcile")
    ctx.check("reconcile", *judge_reconcile(reconciled, before, rest.get_table(db, table)))
    unregistered = call("POST", f"/tables/{db}/{table}/unregister")
    listed_after = table in rest.tables(db)
    registered = rest.call("POST", f"/databases/{db}/register",
                           {"identifier": {"database": db, "object": table}, "path": before.get("path")}, expect=None)
    if registered.get("status") != 200:
        # The files of an unregistered table are the catalog's no longer: the case removes its own.
        ctx.on_cleanup(f"the unregistered table's files under {db}.db/{table}",
                       lambda: remove_files(ctx, before.get("path", "")))
    back = rest.get_table(db, table) if registered.get("status") == 200 else {}
    ctx.check("unregister", *judge_unregister(unregistered, listed_after, registered, before, back))

    # --- every operation refused to a service principal, on the run's own objects ---------------------
    spec = Description()
    fill = {"{name}": adm, "{id}": None, "{database}": db, "{table}": table}
    refusals = []
    for method, template, action in spec.operations():
        ident = str(second_id if template.startswith("/tokens/") else gid)
        path = template
        for k, v in fill.items():
            path = path.replace(k, v if v is not None else urllib.parse.quote(ident))
        refusals.append((f"{method} {template}", call(method, path, {} if method in ("POST", "PUT") else None,
                                                      token="svc").get("status")))
    ctx.check("service-identity-refused", *judge_service_refused(refusals))

    run_manual(ctx)

    ok, detail, problems = judge_openapi(spec, call.seen)
    ctx.keep("openapi-problems.txt", "\n".join(problems) + "\n")
    ctx.check("openapi-matches", ok, detail)


def audit_total(ctx, call):
    """How many events the audit holds, up to a little over its limit (two pages of the newest)."""
    first = body_of(call("GET", f"/audit?limit={AUDIT_LIMIT}")).get("events", [])
    if len(first) < AUDIT_LIMIT:
        return len(first)
    oldest = first[-1]["time"]
    more = body_of(call("GET", f"/audit?until={urllib.parse.quote(oldest)}&limit=10")).get("events", [])
    return len(first) + len(more)


def remove_files(ctx, path):
    if not path.startswith("s3://") or f"/{ctx.install.prefix}/cat05_{ctx.run}.db/" not in path:
        raise RuntimeError(f"not the case's own table path: {path}")
    ctx.aws.raw("s3", "rm", "--recursive", path.rstrip("/") + "/")
    return "removed"


def run_manual(ctx):
    """Runs the manual's examples, and judges them."""
    names = manual.names(ctx.run)
    home = ctx.scratch / "home"
    home.mkdir()
    fill = {}
    # A home of the run's own (the examples write ~/.colvenna-admin), with the operator's AWS
    # configuration still found: kubectl authenticates through the profile.
    real = Path.home() / ".aws"
    env = dict(os.environ, HOME=str(home), KUBECONFIG=str(ctx.scratch / "kubeconfig"),
               AWS_CONFIG_FILE=os.environ.get("AWS_CONFIG_FILE", str(real / "config")),
               AWS_SHARED_CREDENTIALS_FILE=os.environ.get("AWS_SHARED_CREDENTIALS_FILE", str(real / "credentials")))
    if ctx.form == "serverless":
        fill = {"<region>": ctx.install.region, "<env>": ctx.install.env, "<api-id>": ctx.install.output("Serverless", "ApiId")}
        env.update(UAT_PROBE_NAMESPACE=ctx.probe.namespace, UAT_PROBE_POD=ctx.probe.name)
    text = manual.script(ctx.run, ctx.form, fill)
    path = ctx.scratch / "manual.sh"
    path.write_text(text)
    ctx.keep("manual-script.sh", text)

    def clean():
        done = []
        for p in names["principals"]:
            for g in ctx.admin.grants(p):
                ctx.admin.delete_grant(g["id"])
            ctx.admin.disable(p)
        done.append("the examples' principals disabled, their grants deleted")
        if not ctx.rest.drop_database(names["database"]):
            raise RuntimeError(f"database {names['database']} left")
        done.append("their database dropped")
        if ctx.form == "eks" and not ctx.kube.delete_namespace(names["namespace"], ctx.run):
            raise RuntimeError(f"namespace {names['namespace']} left")
        return "; ".join(done)

    ctx.on_cleanup("the manual examples' objects", clean)
    if ctx.form == "eks":
        busy = subprocess.run(["ss", "-ltn"], stdout=subprocess.PIPE, universal_newlines=True).stdout
        if ":8181 " in busy:
            ctx.check("manual-examples", False, "port 8181 of this host is in use: the manual's port-forward needs it")
            return
    # In a process group of its own: a timeout ends the examples' port-forward too.
    p = subprocess.Popen(["bash", str(path)], cwd=str(home), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, start_new_session=True)
    try:
        output, _ = p.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        output, _ = p.communicate()
    finally:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    ctx.keep("manual-output.txt", output)
    ctx.check("manual-examples", *judge_manual(p.returncode, output, text))
