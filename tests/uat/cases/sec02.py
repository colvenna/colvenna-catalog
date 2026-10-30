"""UAT-SEC-02 Authenticated requests without permission are refused at every entry.

Two principals of the run's own, none-<run> (no grant) and read-<run> (READ on one table), ask the
admin API, the table, its credentials and its commit from a client pod, at the form's entry (the
EKS form's Service, the serverless form's private API): each refusal must be 403, and read-<run>'s
read 200, so that the refusals are the grants', not an outage. UAT-CAT-04 checks the whole model."""

import time

from uatlib.probe import by_name, describe

ID = "UAT-SEC-02"
FORMS = ("eks", "serverless")
ENTRY = {"eks": "service", "serverless": "api"}
CACHE_WAIT = 5


def checks(form, args=None):
    e = ENTRY[form]
    return [f"{e}-admin-api-by-a-service-principal", f"{e}-table-without-a-grant",
            f"{e}-credentials-without-a-grant", f"{e}-commit-with-read-only", f"{e}-read-with-read"]


def run(ctx):
    e = ENTRY[ctx.form]
    db, table = f"sec2_{ctx.run}", "t"
    admin, rest = ctx.admin, ctx.rest
    none, read = f"none-{ctx.run}", f"read-{ctx.run}"
    with ctx.preparing("database, table, principals and the grant") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT")])
        for name in (none, read):
            admin.principal(name, ID)
            admin.issue(name, "PT2H", key=name)
        admin.grant(read, "TABLE", "READ", database=db, obj=table)
        notes.append(f"{db}.{table}")
    time.sleep(CACHE_WAIT)
    t = f"{ctx.install.rest_base}/databases/{db}/tables/{table}"
    plan = {
        f"{e}-admin-api-by-a-service-principal": (read, "GET", f"{ctx.install.admin_base}/principals", None, 403),
        f"{e}-table-without-a-grant": (none, "GET", t, None, 403),
        f"{e}-credentials-without-a-grant": (none, "GET", f"{t}/token", None, 403),
        f"{e}-commit-with-read-only": (read, "POST", f"{t}/commit", {}, 403),
        f"{e}-read-with-read": (read, "GET", t, None, 200),
    }
    steps = []
    for name, (who, method, url, body, _) in plan.items():
        step = {"name": name, "method": method, "url": url, "token": who}
        if body is not None:
            step["body"] = body
        steps.append(step)
    results = by_name(ctx.probe.run(steps, tokens=admin.tokens, keep="requests.jsonl"))
    for name, (*_, status) in plan.items():
        ctx.check(name, *judge(results.get(name), status))


def judge(result, status):
    """The expected status, as an explicit answer (a timeout or no answer is no refusal)."""
    return result is not None and result.get("status") == status, f"expected {status}: {describe(result)}"
