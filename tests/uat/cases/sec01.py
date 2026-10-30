"""UAT-SEC-01 Unauthenticated requests are refused at every entry (from Colvenna Lake's
verify-catalog.sh).

From a client pod, every route below is asked with every credential below; each answer must be 401,
the unknown route's too, so that no answer tells whether a route exists. The entry is the EKS form's
Service, or the serverless form's private API, where the gateway itself must refuse and the server
function never see the request. The same GET requests with the admin's token answer 200: the
refusals are not an outage. The internal NLB's are UAT-DEP-11's (requests() serves it too)."""

import secrets
import time
import uuid

from uatlib.catalog import access_log
from uatlib.probe import by_name, describe

ID = "UAT-SEC-01"
FORMS = ("eks", "serverless")
ENTRY = {"eks": "service", "serverless": "api"}
ROUTES = ("config", "table", "commit", "admin", "unknown")
CREDENTIALS = ("none", "forged", "expired", "revoked", "disabled", "basic", "empty-bearer", "lowercase-bearer")
EXPIRED_TTL, EXPIRED_WAIT = "PT1M", 70


def checks(form, args=None):
    names = [f"{ENTRY[form]}-{r}-{c}" for r in ROUTES for c in CREDENTIALS]
    if form == "eks":
        names.append("eks-plain-http")
    else:
        names.append("serverless-refused-before-the-server")
    return names + ["admin-control"]


def routes(rest_base, admin_base, db, table):
    """{route: (method, url, body)}"""
    t = f"{rest_base}/databases/{db}/tables/{table}"
    warehouse = rest_base.rsplit("/", 1)[1]
    return {
        "config": ("GET", f"{rest_base.rsplit('/v1/', 1)[0]}/v1/config?warehouse={warehouse}", None),
        "table": ("GET", t, None),
        "commit": ("POST", f"{t}/commit", {}),
        "admin": ("GET", f"{admin_base}/principals", None),
        "unknown": ("GET", f"{rest_base.rsplit('/v1/', 1)[0]}/v1/{warehouse}/no-such-route-{uuid.uuid4().hex[:8]}", None),
    }


def judge_refused(result):
    """(negative) 401, an explicit answer: a timeout or no answer is no refusal."""
    return result is not None and result.get("status") == 401, f"expected 401: {describe(result)}"


def judge_plain(result):
    """(negative) a connection was made to the TLS port, and no HTTP response came back on it."""
    ok = bool(result) and result.get("connected") is True and result.get("answer") == "none"
    return ok, (f"plain HTTP: connected {bool((result or {}).get('connected'))}, answer {(result or {}).get('answer')} "
                f"{(result or {}).get('first') or (result or {}).get('error', '')}")


def judge_before_the_server(results, lines):
    """(negative) no answer came from the server (an answer of it carries its own request id), and its
    access log has no line of the case's own paths."""
    from_server = [name for name, r in results.items() if (r or {}).get("serverRequestId")]
    ok = bool(results) and not from_server and not lines
    return ok, (f"{len(results)} refused requests; {len(from_server)} answered by the server; {len(lines)} lines of the "
                f"case's paths in the server function's access log")


def judge_control(answers):
    ok = bool(answers) and all((r or {}).get("status") == 200 for r in answers.values())
    return ok, "; ".join(f"{n} {describe(r)[:60]}" for n, r in answers.items())


def credential_step(credential):
    """The step's fields for a credential: a token by name, or a raw Authorization header."""
    return {"none": {}, "forged": {"token": "forged"}, "expired": {"token": "expired"},
            "revoked": {"token": "revoked"}, "disabled": {"token": "disabled"},
            "basic": {"authorization": "Basic dWF0OnVhdA=="}, "empty-bearer": {"authorization": "Bearer "},
            # A valid token, its scheme in lowercase: the scheme must be "Bearer" exactly.
            "lowercase-bearer": {"lowercase": True}}[credential]


def requests(run, entry, urls, tokens):
    """The steps of every route with every credential, each with its own request id."""
    steps = []
    for route, (method, url, body) in urls.items():
        for credential in CREDENTIALS:
            step = {"name": f"{entry}-{route}-{credential}", "method": method, "url": url}
            fields = credential_step(credential)
            if fields.get("lowercase"):
                step["authorization"] = "bearer " + tokens["lowercase"]
            else:
                step.update(fields)
            if body is not None:
                step["body"] = body
            steps.append(step)
    return steps


def prepare_tokens(ctx, admin, db):
    """The expired, revoked and disabled principals' tokens, never used before they are refused (so that
    no authorizer has a cached answer for them), a forged one, and a valid one for the lowercase scheme."""
    names = {k: f"sec-{k}-{ctx.run}" for k in ("expired", "revoked", "disabled", "lowercase")}
    for kind, name in names.items():
        admin.principal(name, f"{ID} {kind}")
        admin.grant(name, "DATABASE", "READ", database=db)
    admin.issue(names["expired"], EXPIRED_TTL, key="expired")
    revoked_id = admin.issue(names["revoked"], "PT2H", key="revoked")
    admin.revoke(revoked_id)
    admin.issue(names["disabled"], "PT2H", key="disabled")
    admin.disable(names["disabled"])
    admin.issue(names["lowercase"], "PT2H", key="lowercase")
    admin.tokens["forged"] = "colvenna_" + secrets.token_urlsafe(32)
    return time.time()


def run(ctx):
    form, entry = ctx.form, ENTRY[ctx.form]
    db, table = f"sec_{ctx.run}", "t"
    admin, rest = ctx.admin, ctx.rest
    with ctx.preparing("database, table and the expired, revoked and disabled tokens") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT")])
        issued = prepare_tokens(ctx, admin, db)
        notes.append(f"{db}.{table}")
    time.sleep(max(0, issued + EXPIRED_WAIT - time.time()))  # the one-minute token has expired

    started = time.time()
    urls = routes(ctx.install.rest_base, ctx.install.admin_base, db, table)
    steps = requests(ctx.run, entry, urls, admin.tokens)
    results = by_name(ctx.probe.run(steps, tokens=admin.tokens, keep="requests.jsonl"))
    for step in steps:
        ctx.check(step["name"], *judge_refused(results.get(step["name"])))

    if form == "eks":
        r = ctx.probe.run([{"name": "plain-http", "kind": "plain", "host": ctx.install.host, "port": 8181, "timeout": 5}],
                          keep="plain-http.jsonl")[0]
        ctx.check("eks-plain-http", *judge_plain(r))
    else:
        marks = [f"/databases/{db}/", urls["unknown"][1].rsplit("/", 1)[1]]
        ctx.check("serverless-refused-before-the-server",
                  *judge_before_the_server({s["name"]: results.get(s["name"]) for s in steps},
                                           server_log_lines(ctx, started, marks)))

    control = [{"name": f"control-{route}", "method": "GET", "url": urls[route][1], "token": "admin"}
               for route in ("config", "table", "admin")]
    ctx.check("admin-control", *judge_control(by_name(ctx.probe.run(control, tokens=admin.tokens, keep="control.jsonl"))))


def server_log_lines(ctx, since, marks):
    """The serverless server function's access log lines since the time that name any of the marks."""
    return [line for text in access_log(ctx, since).values() for line in text.splitlines() if any(m in line for m in marks)]
