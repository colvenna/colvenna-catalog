"""UAT-CAT-04 Authorization and audit (from Colvenna Lake's verify-authorization.sh).

Six principals of the run's own, each with a two-hour token, make their requests from a client pod
against database authz_<run> (tables orders and ledger, made through the REST API): owner (MANAGE on
the database), writer (WRITE on orders), reader (READ on orders), columns (READ on orders, column id
only), denied (READ on the database, DENY READ on orders) and outsider (no grant). Commits and
alterations carry the body {}, so that 400 means the request passed authorization and nothing is
written. Then each request's audit event is found by the request id the catalog answered with, a
deleted grant and an expiring one are seen to take effect, and the grants' own events are read.
The judging functions below take what was collected and nothing else (test_cat04.py)."""

import time
import urllib.parse
from dataclasses import dataclass

from uatlib.probe import by_name, describe
from uatlib.rest import table_body

ID = "UAT-CAT-04"
FORMS = ("eks", "serverless")
ROLES = ("owner", "writer", "reader", "columns", "denied", "outsider")
# The authentication cache's lifetime (COLVENNA_AUTH_CACHE_TTL, the server's default) and 3 s more.
CACHE_WAIT = 2 + 3
SCRATCH = "scratch"  # a call whose body is the create-table request of the run's scratch table


@dataclass(frozen=True)
class Call:
    role: str
    method: str
    path: object  # below the database's tables ("" for the listing), or None for the database listing
    body: object
    status: int


CALLS = {
    "list-databases/owner": Call("owner", "GET", None, None, 200),
    "list-databases/reader": Call("reader", "GET", None, None, 200),
    "list-databases/outsider": Call("outsider", "GET", None, None, 200),
    "get-table/owner": Call("owner", "GET", "/orders", None, 200),
    "get-table/writer": Call("writer", "GET", "/orders", None, 200),
    "get-table/reader": Call("reader", "GET", "/orders", None, 200),
    "get-table/denied": Call("denied", "GET", "/orders", None, 403),
    "get-table/outsider": Call("outsider", "GET", "/orders", None, 403),
    "list-tables/reader": Call("reader", "GET", "", None, 200),
    "list-tables/outsider": Call("outsider", "GET", "", None, 403),
    "data-token/writer": Call("writer", "GET", "/orders/token", None, 200),
    "data-token/reader": Call("reader", "GET", "/orders/token", None, 200),
    "data-token/denied": Call("denied", "GET", "/orders/token", None, 403),
    "data-token/outsider": Call("outsider", "GET", "/orders/token", None, 403),
    "column-auth/allowed": Call("columns", "POST", "/orders/auth", {"select": ["id"]}, 200),
    "column-auth/denied": Call("columns", "POST", "/orders/auth", {"select": ["id", "amount"]}, 403),
    "commit/owner": Call("owner", "POST", "/orders/commit", {}, 400),
    "commit/writer": Call("writer", "POST", "/orders/commit", {}, 400),
    "commit/reader": Call("reader", "POST", "/orders/commit", {}, 403),
    "commit/outsider": Call("outsider", "POST", "/orders/commit", {}, 403),
    "alter-table/owner": Call("owner", "POST", "/orders", {}, 400),
    "alter-table/writer": Call("writer", "POST", "/orders", {}, 403),
    "create-table/writer": Call("writer", "POST", "", SCRATCH, 403),
    # A WRITE grant does not reach DDL on its own table: aimed at orders itself.
    "drop-table/writer": Call("writer", "DELETE", "/orders", None, 403),
}
OWNER_DDL = ("create-table/owner", "drop-table/owner")
BY_ID = "get-table-by-id/outsider"
# The credentials' access each data-token call must have been issued with (its audit event's detail).
ACCESS = {"data-token/writer": "READ_WRITE", "data-token/reader": "READ"}


def audited_calls():
    """Every call whose audit event is checked: the calls above, the owner's DDL and the lookup by id."""
    return list(CALLS) + list(OWNER_DDL) + [BY_ID]


def checks(form, args=None):
    names = list(CALLS) + [BY_ID, "create-drop-table/owner", "grant-deleted-takes-effect", "grant-expiry-takes-effect"]
    names += [f"audit-{c}" for c in audited_calls()]
    return names + ["audit-no-failure-as-success", "audit-grants"]


# --- judging ------------------------------------------------------------------------------------------

def judge_call(name, result, db, listed=None):
    """(ok, detail) of one call of CALLS. listed: for list-tables/reader, every table name the pages named."""
    call = CALLS[name]
    ok = result is not None and result.get("status") == call.status
    body = (result or {}).get("body") if isinstance((result or {}).get("body"), dict) else {}
    detail = f"expected {call.status}: {describe(result)}"
    if ok and name in ("list-databases/owner", "list-databases/reader"):
        ok = db in (body.get("databases") or [])
    elif ok and name == "list-databases/outsider":
        names = body.get("databases") or []
        ok = db not in names and "default" in names
    elif ok and name == "list-tables/reader":
        ok = listed == ["orders"]
        detail += f"; every page named {listed}"
    elif ok and name == "column-auth/denied":
        ok = "amount" in str(body)
    return ok, detail


def judge_by_id(result, table_id, db):
    named = "orders" in str((result or {}).get("body")) or db in str((result or {}).get("body"))
    ok = result is not None and result.get("status") == 404 and table_id is not None and not named
    return ok, f"table id {'known' if table_id else 'unknown'}; expected 404 naming no table: {describe(result)}"


def expected_audit(call, db, scratch):
    """(action, resource) the call's audit event must have; resource None: not judged."""
    orders = f"{db}.orders"
    table = {"get-table": "GET_TABLE", "data-token": "ISSUE_DATA_TOKEN", "column-auth": "AUTH_TABLE_QUERY",
             "commit": "COMMIT", "alter-table": "ALTER_TABLE", "drop-table": "DROP_TABLE"}
    kind = call.split("/")[0]
    if kind == "list-databases":
        return "LIST_DATABASES", None
    if kind == "list-tables":
        return "LIST_TABLES", db
    if call == "create-table/writer":
        return "CREATE_TABLE", db  # refused on the database, before the request names its table
    if call in OWNER_DDL:
        return ("CREATE_TABLE" if call.startswith("create") else "DROP_TABLE"), f"{db}.{scratch}"
    if call == BY_ID:
        return "GET_TABLE_BY_ID", None
    return table[kind], orders


def outcome_of(status):
    return "SUCCEEDED" if status < 400 else "DENIED" if status in (401, 403) else "FAILED"


def expected_outcome(call, status):
    """The outcome the call's event must have: the lookup by id is refused as not found (404), and
    recorded as the denial it is."""
    return "DENIED" if call == BY_ID and status == 404 else outcome_of(status)


def judge_audit(call, result, event, db, scratch, principal, table_id=None):
    """(ok, detail) for audit-<call>: the event with the request's id names the caller, the action, the
    resource, the outcome its status means, the status and the time; a denial carries the policy
    version and the level needed; the refused lookup by id is under the table's id (table_id) and names
    no table; a data token's event, the access it was issued with."""
    if result is None or "status" not in result:
        return False, f"the call made no request: {describe(result)}"
    if event is None:
        return False, f"no audit event with request id {result.get('requestId')}"
    action, resource = expected_audit(call, db, scratch)
    status = result["status"]
    want = {"action": action, "outcome": expected_outcome(call, status), "httpStatus": status, "principal": principal}
    wrong = [f"{k}={event.get(k)!r} (want {v!r})" for k, v in want.items() if event.get(k) != v]
    if resource is not None and event.get("resource") != resource:
        wrong.append(f"resource={event.get('resource')!r} (want {resource!r})")
    if call == BY_ID and event.get("resource") != table_id:
        wrong.append(f"resource={event.get('resource')!r} (want the table id {table_id!r})")
    if call == BY_ID and ("orders" in str(event) or db in str(event)):
        wrong.append(f"the refused lookup by id names the table: {event}")
    for key in ("time", "requestId"):
        if not event.get(key):
            wrong.append(f"no {key}")
    detail = event.get("detail") or {}
    if want["outcome"] == "DENIED" and ("policyVersion" not in detail or not detail.get("privilege")):
        wrong.append(f"a denial without its policy version and the level needed: {detail}")
    if call in ACCESS and detail.get("access") != ACCESS[call]:
        wrong.append(f"access {detail.get('access')!r} (want {ACCESS[call]!r})")
    return not wrong, "; ".join(wrong) or (f"{event['principal']} {action} {event.get('resource')} "
                                           f"{event['outcome']} {status}")


def judge_no_failure_as_success(events):
    wrong = [e for e in events if e.get("outcome") == "SUCCEEDED" and e.get("httpStatus", 0) >= 400]
    return bool(events) and not wrong, (f"{len(events)} events of the run's principals; {len(wrong)} failures "
                                        f"recorded as success: " + "; ".join(f"{e['action']} {e['httpStatus']}"
                                                                             for e in wrong[:5]))


GRANT_FIELDS = ("grantId", "principal", "scope", "database", "object", "privilege", "effect", "columns", "purpose",
                "approvedBy", "approval", "expiresAt", "policyVersion")


def judge_grants(granted, events, principals):
    """Every grant the run made has a GRANT and a REVOKE_GRANT event, each with the whole grant."""
    mine = [e for e in events if (e.get("detail") or {}).get("principal") in principals]
    made = {e["detail"].get("grantId") for e in mine if e["action"] == "GRANT" and e["outcome"] == "SUCCEEDED"}
    revoked = {e["detail"].get("grantId") for e in mine if e["action"] == "REVOKE_GRANT" and e["outcome"] == "SUCCEEDED"}
    incomplete = [e["id"] for e in mine if any(f not in (e.get("detail") or {}) for f in GRANT_FIELDS)]
    granted = set(granted)
    ok = bool(granted) and granted <= made and granted <= revoked and not incomplete
    return ok, (f"{len(granted)} grants made and deleted by the run; in the audit: {len(granted & made)} made, "
                f"{len(granted & revoked)} deleted; events without every field: {incomplete[:5]}")


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    db, scratch = f"authz_{ctx.run}", f"authz_probe_{ctx.run}"
    principals = {role: f"authz-{role}-{ctx.run}" for role in ROLES}
    admin, rest = ctx.admin, ctx.rest
    base = f"{ctx.install.rest_base}/databases/{db}/tables"
    with ctx.preparing("database, tables, principals and grants") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, "orders", [("id", "INT"), ("amount", "INT"), ("note", "STRING")])
        rest.create_table(db, "ledger", [("id", "INT"), ("entry", "STRING")])
        for role, name in principals.items():
            admin.principal(name, f"{ID} {role}")
            admin.issue(name, "PT2H", key=role)
        grants = {
            "owner": admin.grant(principals["owner"], "DATABASE", "MANAGE", database=db),
            "writer": admin.grant(principals["writer"], "TABLE", "WRITE", database=db, obj="orders"),
            "reader": admin.grant(principals["reader"], "TABLE", "READ", database=db, obj="orders"),
            "columns": admin.grant(principals["columns"], "TABLE", "READ", database=db, obj="orders", columns=["id"]),
            "denied-db": admin.grant(principals["denied"], "DATABASE", "READ", database=db),
            "denied-deny": admin.grant(principals["denied"], "TABLE", "READ", database=db, obj="orders", effect="DENY"),
        }
        notes.append(f"{db}: orders, ledger; {len(ROLES)} principals, {len(grants)} grants")
    # A grant counts once the authentication cache has let go of the principal's earlier decisions.
    time.sleep(CACHE_WAIT)

    # --- step 1: every principal's requests ----------------------------------------------------------
    steps = []
    for name, call in CALLS.items():
        url = f"{ctx.install.rest_base}/databases" if call.path is None else f"{base}{call.path}"
        step = {"name": name, "method": call.method, "url": url, "token": call.role}
        body = table_body(db, scratch, [("id", "INT")]) if call.body == SCRATCH else call.body
        if body is not None:
            step["body"] = body
        steps.append(step)
    steps += [{"name": "create-table/owner", "method": "POST", "url": base, "token": "owner",
               "body": table_body(db, scratch, [("id", "INT")])},
              {"name": "drop-table/owner", "method": "DELETE", "url": f"{base}/{scratch}", "token": "owner"}]
    results = by_name(ctx.probe.run(steps, tokens=admin.tokens, keep="requests.jsonl"))
    listed = pages(ctx, f"{base}?maxResults=1", "reader")
    table_id = ((results.get("get-table/owner") or {}).get("body") or {}).get("id")
    by_id = {"name": BY_ID, "method": "GET", "token": "outsider",
             "url": f"{ctx.install.rest_base}/tables/id/{urllib.parse.quote(str(table_id or 'unknown'), safe='')}"}
    results.update(by_name(ctx.probe.run([by_id], tokens=admin.tokens, keep="requests.jsonl")))
    for name in CALLS:
        ctx.check(name, *judge_call(name, results.get(name), db, listed if name == "list-tables/reader" else None))
    ctx.check(BY_ID, *judge_by_id(results.get(BY_ID), table_id, db))
    created, dropped = results.get("create-table/owner"), results.get("drop-table/owner")
    ctx.check("create-drop-table/owner", (created or {}).get("status") == 200 and (dropped or {}).get("status") in (200, 204),
              f"create {describe(created)}; drop {describe(dropped)}")

    # --- step 3: a deleted grant and an expiring one take effect --------------------------------------
    admin.delete_grant(grants["reader"])
    time.sleep(CACHE_WAIT)
    after = by_name(ctx.probe.run([
        {"name": "table", "url": f"{base}/orders", "token": "reader"},
        {"name": "credentials", "url": f"{base}/orders/token", "token": "reader"},
    ], tokens=admin.tokens, keep="step3.jsonl"))
    ctx.check("grant-deleted-takes-effect", after["table"].get("status") == 403 and after["credentials"].get("status") == 403,
              f"table {describe(after['table'])}; credentials {describe(after['credentials'])}")
    expiry = time.time() + 120
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry))
    admin.grant(principals["reader"], "TABLE", "READ", database=db, obj="orders", expires=expires_at)
    time.sleep(CACHE_WAIT)
    before = ctx.probe.run([{"name": "before-expiry", "url": f"{base}/orders", "token": "reader"}],
                           tokens=admin.tokens, keep="step3.jsonl")[0]
    time.sleep(max(0, expiry - time.time()) + CACHE_WAIT)
    later = ctx.probe.run([{"name": "after-expiry", "url": f"{base}/orders", "token": "reader"}],
                          tokens=admin.tokens, keep="step3.jsonl")[0]
    ctx.check("grant-expiry-takes-effect", before.get("status") == 200 and later.get("status") == 403,
              f"grant until {expires_at}: before {describe(before)}; after {describe(later)}")

    # --- step 2: the audit, by the request ids the catalog answered with -------------------------------
    events = {}
    for name in principals.values():
        for e in admin.audit(principal=name):
            events[e["requestId"]] = e
    ctx.keep("audit-events.json", list(events.values()))
    for call in audited_calls():
        r = results.get(call)
        role = CALLS[call].role if call in CALLS else "owner" if call in OWNER_DDL else "outsider"
        ctx.check(f"audit-{call}", *judge_audit(call, r, events.get((r or {}).get("requestId")), db, scratch,
                                                principals[role], table_id))
    ctx.check("audit-no-failure-as-success", *judge_no_failure_as_success(list(events.values())))

    # The grants' own events: each creation and each deletion, with the whole grant.
    admin.delete_grants()
    grant_events = admin.audit(resourceType="GRANT", since=ctx.started.strftime("%Y-%m-%dT%H:%M:%SZ"))
    ctx.keep("audit-grant-events.json", grant_events)
    ctx.check("audit-grants", *judge_grants(admin.granted, grant_events, set(principals.values())))


def pages(ctx, url, token):
    """Every table name the listing's pages name, page by page (at most 50 pages)."""
    names, page_token = [], None
    for _ in range(50):
        step = {"name": "page", "url": url + (f"&pageToken={urllib.parse.quote(page_token)}" if page_token else ""),
                "token": token}
        r = ctx.probe.run([step], tokens=ctx.admin.tokens, keep="pages.jsonl")[0]
        body = r.get("body") if isinstance(r.get("body"), dict) else {}
        names += body.get("tables") or []
        page_token = body.get("nextPageToken")
        if r.get("status") != 200 or not page_token:
            break
    return names
