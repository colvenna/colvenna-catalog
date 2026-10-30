"""UAT-CAT-02 Per-table credential scope (Colvenna Lake's verify-data-tokens.sh).

Database cred_<run> with tables orders and orders_v2 (a name sharing orders' prefix), database
cred_<run>_other with table secrets, each seeded with synthetic rows through the catalog (Seed).
Principals cred-writer-<run> (WRITE on orders), cred-reader-<run> (READ on orders) and
cred-outsider-<run> ask for orders' credentials; each credential alone then calls S3 from this host
on the table and on everything around it. A pod of a client namespace, with no IRSA role, tries the
same files with its own identity, the node's (Probe). The credentials' session is read back (STS)
and found in CloudTrail; a table at a location the client chose outside the warehouse, and the
registration of a path outside it, are refused, so that no external table exists whose credentials
could be asked for (#63); the issuances in the audit; the clients' configuration searched for S3 keys.
The judging functions take what was collected and nothing else (test_cat02.py)."""

import base64
import json
import re
import time

from uatlib import engines
from uatlib.probe import by_name, describe

ID = "UAT-CAT-02"
FORMS = ("eks", "serverless")
NEGATIVES = ("sibling-table", "bare-prefix-list", "database-directory-list", "other-database", "whole-bucket-list",
             "outside-the-warehouse")
KEY_PATTERN = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b|(fs\.s3a\.|s3\.)(access|secret)[-.]key|session[-.]token")
DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")
# What an issuance's audit event records (the catalog's names): session name, access key id, cache hit,
# level, prefix and expiry.
ISSUANCE_FIELDS = ("roleSessionName", "accessKeyId", "cached", "access", "prefix", "expiresAt")
SEED = "com.colvenna.catalog.uat.flink.Seed"
PROBE = "com.colvenna.catalog.uat.flink.Probe"


def checks(form, args=None):
    return ["writer-gets-read-write", "reader-gets-read-only", "outsider-gets-none", "own-table-read-write-list-delete",
            "reader-reads", "reader-writes-consumer-progress", "reader-cannot-write-or-delete-data", *NEGATIVES,
            "pod-identity-refused", "issued-without-role-chaining", "client-location-refused", "register-outside-refused",
            "issuance-audited", "no-s3-keys-in-clients"]


def minutes(duration):
    m = DURATION.match(duration or "")
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) if m else None


def credentials(body):
    t = (body or {}).get("token") or {}
    return {"accessKeyId": t.get("s3.access-key-id") or t.get("fs.s3a.access.key"),
            "secretAccessKey": t.get("s3.secret-access-key") or t.get("fs.s3a.secret.key"),
            "sessionToken": t.get("s3.session-token") or t.get("fs.s3a.session.token")}


def classify(p):
    """allow, deny or missing, from an s3api call's process; anything else is an error."""
    if p.returncode == 0:
        return "allow"
    if re.search(r"AccessDenied|Forbidden|\(403\)", p.stderr):
        return "deny"
    if re.search(r"Not Found|\(404\)|NoSuchKey", p.stderr):
        return "missing"
    return "error: " + " ".join(p.stderr.split())[:200]


# --- judging ------------------------------------------------------------------------------------------

def judge_issued(answer, left_minutes, duration_minutes, session_arn, data_role, principal):
    """A principal's credentials: 200, of the data role's session colvenna-<principal>, valid for the
    configured duration (within 5 minutes)."""
    role_ok = f":assumed-role/{data_role}/colvenna-{principal}" in (session_arn or "")
    ok = (answer.get("status") == 200 and left_minutes is not None and duration_minutes is not None
          and abs(left_minutes - duration_minutes) < 5 and role_ok)
    return ok, (f"{answer.get('status')}; valid {left_minutes and round(left_minutes)} min "
                f"(COLVENNA_DATA_TOKEN_DURATION {duration_minutes} min); the data role's session colvenna-{principal}: "
                f"{role_ok}")


def judge_refused(answer):
    return answer.get("status") == 403, describe(answer)


def judge_own_table(got):
    want = {"list": "allow", "read": "allow", "missing": "missing", "write": "allow", "delete": "allow"}
    return got == want, f"the writer under the table's root: {got} (a missing key: missing, not deny)"


def judge_all(got, want):
    """got: {name: outcome}; each must be want."""
    return bool(got) and all(v == want for v in got.values()), f"{got}"


def judge_negative(got):
    """got: {principal: [outcome of each call]}; every call refused."""
    return bool(got) and all(v == "deny" for vs in got.values() for v in vs), f"{got}"


def judge_probe(lines, expected):
    """The pod's own identity: PROBE lines, every read and listing of every table refused."""
    refused = [line for line in lines if line.split(" ", 4)[3:4] == ["denied"]]
    return len(lines) == expected and len(refused) == expected, (
        f"{len(refused)} of {len(lines)} (of {expected}) reads and listings by the pod's own identity refused: "
        + "; ".join(" ".join(line.split(" ", 4)[1:4]) for line in lines))


def judge_web_identity(record, form, principal):
    """The AssumeRoleWithWebIdentity event that issued the credentials: the catalog's own web identity
    (EKS: the cluster's OIDC provider, the service account's token; serverless: Cognito's developer
    identity), session colvenna-<principal>; None: no such event (a role assumed by another role's
    session would be AssumeRole, never this)."""
    if record is None:
        return False, "no AssumeRoleWithWebIdentity event in CloudTrail issued the credentials"
    provider = (record.get("userIdentity") or {}).get("identityProvider", "")
    session = (record.get("requestParameters") or {}).get("roleSessionName", "")
    want = "oidc.eks" if form == "eks" else "cognito-identity.amazonaws.com"
    ok = want in provider and session == f"colvenna-{principal}"
    return ok, (f"AssumeRoleWithWebIdentity by {(record.get('userIdentity') or {}).get('type')} of "
                f"{'the cluster OIDC provider' if 'oidc.eks' in provider else provider}, session {session}")


def judge_no_table(answer, words, after):
    """A request the install refuses: 400 naming why (words), and no table made by it. answer: the
    request's; after: the table's GET afterwards (404 when there is none)."""
    message = describe(answer)
    ok = answer.get("status") == 400 and words in message and after.get("status") == 404
    return ok, f"{message[:300]}; the table afterwards: {after.get('status')}"


def judge_client_location(made, after):
    """A table at a location the client chose outside the warehouse (path): 400, the install allowing
    no client-chosen location (COLVENNA_EXTERNAL_LOCATION_ROOTS is no install setting)."""
    return judge_no_table(made, "allows no client-chosen locations", after)


def judge_register_outside(registered, after):
    """A path outside the warehouse registered: 400, register taking only the warehouse's directories."""
    return judge_no_table(registered, "inside the warehouse", after)


def judge_issuances(events_by_id, asked, secrets):
    """One event per issuance (the second, a cache hit, too), each with every field and no secret.
    asked: {who: the probe's answer}, events_by_id: {the catalog's request id: event}."""
    problems = []
    for who, answer in asked.items():
        e = events_by_id.get(answer.get("serverRequestId"))
        if e is None:
            problems.append(f"{who}: no event")
            continue
        detail = e.get("detail") or {}
        missing = [f for f in ISSUANCE_FIELDS if f not in detail]
        leaked = [k for k, v in detail.items() if isinstance(v, str) and any(s and s in v for s in secrets)]
        if missing or leaked:
            problems.append(f"{who}: missing {missing}, a secret in {leaked}")
    # The writer asked again, several times: each replica keeps its own cache, so a later request on a
    # replica that issued before is a cache hit, and its event must say so.
    again = [(events_by_id.get(a.get("serverRequestId")) or {}).get("detail") or {} for w, a in asked.items()
             if w.startswith("writer-again")]
    if not any(d.get("cached") is True for d in again):
        problems.append(f"no cache hit recorded among the writer's {len(again)} later issuances: "
                        f"{[d.get('cached') for d in again]}")
    return not problems, "; ".join(problems) or (
        f"{len(asked)} issuances, each its own event, the cache hit included, with {', '.join(ISSUANCE_FIELDS)}, no secret")


def judge_keys(hits, namespaces):
    return not hits, f"namespaces {', '.join(namespaces)}: " + (", ".join(hits) or "no S3 key")


# --- the run ------------------------------------------------------------------------------------------

def key_of(path):
    m = re.match(r"^s3a?://([^/]+)/(.+?)/?$", path)
    return m.group(1), m.group(2)


def cloudtrail_record(ctx, access_key, since, wait=1200):
    """The AssumeRoleWithWebIdentity record whose response issued the access key, once CloudTrail has it."""
    end = time.time() + wait
    while time.time() < end:
        out = ctx.aws("cloudtrail", "lookup-events", "--lookup-attributes",
                      "AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity",
                      "--start-time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 300)),
                      "--max-items", "500", timeout=300)
        for event in (out or {}).get("Events", []):
            record = json.loads(event.get("CloudTrailEvent", "{}"))
            if ((record.get("responseElements") or {}).get("credentials") or {}).get("accessKeyId") == access_key:
                return record
        time.sleep(60)
    return None


def run(ctx):
    admin, rest, install = ctx.admin, ctx.rest, ctx.install
    db, other = f"cred_{ctx.run}", f"cred_{ctx.run}_other"
    names = {k: f"cred-{k}-{ctx.run}" for k in ("writer", "reader", "outsider", "seed")}
    started = time.time()
    with ctx.preparing("databases, tables with data, principals and grants") as notes:
        for d in (db, other):
            rest.create_database(d)
            ctx.on_cleanup(f"database {d} dropped", lambda d=d: rest.drop_database(d) and "dropped")
        for d, t in ((db, "orders"), (db, "orders_v2"), (other, "secrets")):
            rest.create_table(d, t, [("id", "INT"), ("v", "STRING")])
        for role, name in names.items():
            admin.principal(name, f"{ID} {role}")
            admin.issue(name, "PT2H", key=name)
        admin.grant(names["writer"], "TABLE", "WRITE", database=db, obj="orders")
        admin.grant(names["reader"], "TABLE", "READ", database=db, obj="orders")
        for d, t in ((db, "orders"), (db, "orders_v2"), (other, "secrets")):
            admin.grant(names["seed"], "TABLE", "WRITE", database=d, obj=t)
        time.sleep(5)
        eng = engines.Engines(ctx, "engines")
        eng.tokens({names["seed"]: admin.tokens[names["seed"]]})
        for d, tables in ((db, ["orders", "orders_v2"]), (other, ["secrets"])):
            code, out, _ = eng.run(eng.java(f"seed-{d.replace('_', '-')[-24:]}", SEED, [d, "10", *tables], names["seed"]),
                                   1200, keep=f"seed-{d}.log")
            if code != 0 or out.count("SEEDED ") != len(tables):
                raise RuntimeError(f"the seed of {d} failed (exit {code}): {out[-300:]}")
        paths = {t: rest.get_table(d, t)["path"] for d, t in ((db, "orders"), (db, "orders_v2"), (other, "secrets"))}
        notes.append(f"{db}: orders, orders_v2; {other}: secrets; 10 rows each")

    # --- step 1: each principal asks for orders' credentials ------------------------------------------
    token_url = f"{install.rest_base}/databases/{db}/tables/orders/token"
    steps = [{"name": who, "url": token_url, "token": names[who], "full": True} for who in ("writer", "reader", "outsider")]
    steps += [{"name": f"writer-again-{i}", "url": token_url, "token": names["writer"], "full": True} for i in range(1, 5)]
    asked = by_name(ctx.probe.run(steps, tokens=admin.tokens))
    creds = {who: credentials(asked[who].get("body")) for who in ("writer", "reader")}
    # Registered before anything that holds them is written.
    ctx.sanitizer.secret_sources.append(lambda: [c.get(k) for c in creds.values() for k in ("secretAccessKey", "sessionToken")])
    ctx.keep("token-requests.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in asked.values()))
    duration = minutes(install.get("COLVENNA_DATA_TOKEN_DURATION"))
    data_role = install.output("Eks" if install.form == "eks" else "Serverless", "DataRoleArn").rsplit("/", 1)[1]
    sessions = {}
    for who in ("writer", "reader"):
        expires = (asked[who].get("body") or {}).get("expiresAtMillis")
        left = (expires / 1000 - time.time()) / 60 if expires else None
        identity = ctx.aws.raw("sts", "get-caller-identity", "--output", "json", credentials=creds[who], check=False)
        sessions[who] = json.loads(identity.stdout)["Arn"] if identity.returncode == 0 else ""
        ctx.check(f"{who}-gets-{'read-write' if who == 'writer' else 'read-only'}",
                  *judge_issued(asked[who], left, duration, sessions[who], data_role, names[who]))
    ctx.check("outsider-gets-none", *judge_refused(asked["outsider"]))

    # --- step 2: S3 with each credential alone ---------------------------------------------------------
    bucket, orders = key_of(paths["orders"])
    _, sibling = key_of(paths["orders_v2"])
    _, secrets = key_of(paths["secrets"])
    database_dir = orders.rsplit("/", 1)[0]
    empty = ctx.scratch / "empty"
    empty.write_bytes(b"")

    def s3(who, *args):
        return classify(ctx.aws.raw("s3api", *args, credentials=creds[who], check=False))

    def lst(who, prefix):
        return s3(who, "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--max-items", "1")

    def put(who, key):
        return s3(who, "put-object", "--bucket", bucket, "--key", key, "--body", str(empty))

    def head(who, key):
        return s3(who, "head-object", "--bucket", bucket, "--key", key)

    ctx.check("own-table-read-write-list-delete", *judge_own_table({
        "list": lst("writer", orders + "/"), "read": head("writer", f"{orders}/schema/schema-0"),
        "missing": head("writer", f"{orders}/snapshot/snapshot-999999"),
        "write": put("writer", f"{orders}/uat-probe-{ctx.run}"),
        "delete": s3("writer", "delete-object", "--bucket", bucket, "--key", f"{orders}/uat-probe-{ctx.run}")}))
    ctx.check("reader-reads", *judge_all({"read": head("reader", f"{orders}/schema/schema-0"),
                                          "list": lst("reader", orders + "/")}, "allow"))
    progress = f"{orders}/consumer/consumer-uat-{ctx.run}"
    ctx.check("reader-writes-consumer-progress", *judge_all({
        "write": put("reader", progress),
        "remove": s3("reader", "delete-object", "--bucket", bucket, "--key", progress)}, "allow"))
    ctx.check("reader-cannot-write-or-delete-data", *judge_all({
        "write": put("reader", f"{orders}/uat-reader-{ctx.run}"),
        "delete": s3("reader", "delete-object", "--bucket", bucket, "--key", f"{orders}/schema/schema-0")}, "deny"))
    negatives = {
        "sibling-table": lambda who: [lst(who, sibling + "/"), head(who, f"{sibling}/schema/schema-0"),
                                      put(who, f"{sibling}/uat-{ctx.run}")],
        "bare-prefix-list": lambda who: [lst(who, orders)],
        "database-directory-list": lambda who: [lst(who, database_dir + "/")],
        "other-database": lambda who: [head(who, f"{secrets}/schema/schema-0"), lst(who, secrets + "/")],
        "whole-bucket-list": lambda who: [s3(who, "list-objects-v2", "--bucket", bucket, "--max-items", "1")],
        "outside-the-warehouse": lambda who: [put(who, f"uat-outside-{ctx.run}"),
                                              head(who, f"{install.prefix}-outside/{ctx.run}")],
    }
    for name, calls in negatives.items():
        ctx.check(name, *judge_negative({who: calls(who) for who in ("writer", "reader")}))

    # --- step 3: the pods' own identity ----------------------------------------------------------------
    lines = []
    for d, tables in ((db, ["orders", "orders_v2"]), (other, ["secrets"])):
        _, out, _ = eng.run(eng.java(f"probe-{d.replace('_', '-')[-24:]}", PROBE, [d, *tables], names["seed"]), 1200,
                            keep=f"probe-own-identity-{d}.log")
        lines += [line for line in out.splitlines() if line.startswith("PROBE ")]
    ctx.check("pod-identity-refused", *judge_probe(lines, 6))

    # --- the session in CloudTrail --------------------------------------------------------------------
    record = cloudtrail_record(ctx, creds["writer"]["accessKeyId"], started)
    ctx.keep("cloudtrail-issuance.json", record or {})
    ctx.check("issued-without-role-chaining", *judge_web_identity(record, install.form, names["writer"]))

    # --- no external table: client-chosen locations and registrations outside the warehouse refused ------
    # An install allows no client-chosen location, so no external table exists to ask credentials of
    # (#63); the issuer's refusal of one (400) is the server's unit tests' (AwsStsTokenIssuerTest).
    outside = f"s3://{bucket}/uat-external-{ctx.run}/t"
    made = rest.call("POST", f"/databases/{db}/tables",
                     {"identifier": {"database": db, "object": "external_t"},
                      "schema": {"fields": [{"id": 0, "name": "id", "type": "INT"}], "partitionKeys": [],
                                 "primaryKeys": [], "options": {"path": outside}}},
                     expect=None)
    after_made = rest.call("GET", f"/databases/{db}/tables/external_t", expect=None)
    ctx.check("client-location-refused", *judge_client_location(made, after_made))
    registered = rest.call("POST", f"/databases/{db}/register",
                           {"identifier": {"database": db, "object": "outside_t"}, "path": outside}, expect=None)
    after_registered = rest.call("GET", f"/databases/{db}/tables/outside_t", expect=None)
    ctx.check("register-outside-refused", *judge_register_outside(registered, after_registered))

    # --- the audit -------------------------------------------------------------------------------------
    events = admin.audit(action="ISSUE_DATA_TOKEN", since=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)))
    ctx.keep("audit-issuances.json", events)
    held = [c.get(k) for c in creds.values() for k in ("secretAccessKey", "sessionToken")]
    ctx.check("issuance-audited", *judge_issuances({e["requestId"]: e for e in events},
                                                   {k: v for k, v in asked.items() if k != "outsider"}, held))

    # --- no S3 key in any client ----------------------------------------------------------------------
    hits = []
    for ns in list(ctx.kube.made):
        for kind in ("configmaps", "secrets", "pods"):
            for item in ctx.kube.json("get", kind, "-n", ns)["items"]:
                text = json.dumps(item)
                if kind == "secrets":
                    text += " ".join(base64.b64decode(v).decode("utf-8", "replace") for v in (item.get("data") or {}).values())
                if KEY_PATTERN.search(text):
                    hits.append(f"{ns}/{kind}/{item['metadata']['name']}")
    ctx.check("no-s3-keys-in-clients", *judge_keys(hits, list(ctx.kube.made)))
