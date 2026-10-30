"""UAT-CAT-06 Token rotation and revocation (from Colvenna Lake's verify-catalog.sh).

Principal rot-<run> may READ a table of the case's own; a reader pod reads it every 5 s. The
manual's rotation order (issue B, switch the reader to it, revoke A) must not interrupt it; A is then
refused from 5 s after its revocation, every second for 90 s (in the serverless form first by the
server, while the authorizer's cached answer lasts, then by the gateway itself); C, a one-minute
token, is refused once expired; once the principal is disabled, B is refused for the API and for
credentials within 5 s, while the S3 credentials B got before still read until they expire (as the
manual says: recorded, not a defect). The refusals the server made are in the audit. The judging
functions take what was collected and nothing else (test_cat06.py)."""

import re
import time
from datetime import datetime, timezone

from uatlib.catalog import access_log, answered_by
from uatlib.probe import Background, by_name, describe

ID = "UAT-CAT-06"
FORMS = ("eks", "serverless")
READ_EVERY, REFUSE_FOR, GRACE = 5, 90, 5
AUTHORIZER_CACHE = 60


def checks(form, args=None):
    return ["rotation-uninterrupted", "revoked-refused-by-the-server", "expired-refused", "disabled-refused",
            "issued-credentials-scope", "refusals-audited"]


def at(result):
    """A result's time as seconds since the epoch."""
    return datetime.strptime(result["time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def statuses(results):
    return sorted({str(r.get("status", r.get("error"))) for r in results})


# --- judging ------------------------------------------------------------------------------------------

def judge_rotation(reads_a, reads_b, disabled):
    """Every read through the rotation succeeded: A's until its reader stopped, B's until the disable."""
    through = reads_a + [r for r in reads_b if at(r) < disabled]
    ok = bool(reads_a) and bool(reads_b) and all(r.get("status") == 200 for r in through)
    return ok, f"{len(reads_a)} reads with A, then {len(through) - len(reads_a)} with B: {statuses(through)}"


def judge_revoked(form, refused, revoked, answered):
    """Every request with A from GRACE s after the revocation is 401. EKS: both replicas answered some.
    Serverless: the server answered (its own request id in the answer, its access log naming it) only
    until the authorizer's cached answer could last, AUTHORIZER_CACHE s; then the gateway alone."""
    late = [r for r in refused if at(r) >= revoked + GRACE]
    ok = bool(late) and all(r.get("status") == 401 for r in late)
    detail = f"{len(late)} requests with A from {GRACE} s after the revocation: {statuses(late)}; "
    in_log = set().union(*answered.values()) if answered else set()
    if form == "eks":
        by_replica = {k: len(v) for k, v in answered.items()}
        ok = ok and len(by_replica) == 2 and all(by_replica.values())
        return ok, detail + f"answered by the replicas {by_replica}"
    by_server = [r for r in refused if r.get("serverRequestId")]
    after_cache = [r for r in refused if at(r) >= revoked + AUTHORIZER_CACHE + GRACE]
    leaked = [r for r in after_cache if r.get("serverRequestId")]
    logged = {r["serverRequestId"] for r in by_server} <= in_log
    ok = ok and not leaked and all(r.get("status") == 401 for r in by_server) and logged
    return ok, (detail + f"{len(by_server)} refused by the server, each in its access log: {logged}; "
                f"{len(after_cache)} sent {AUTHORIZER_CACHE + GRACE} s after it, of which {len(leaked)} reached the server")


def judge_disabled(api, credentials):
    ok = (api or {}).get("status") == 401 and (credentials or {}).get("status") == 401
    return ok, f"{GRACE} s after the disable: API {describe(api)}; credentials {describe(credentials)}"


def judge_scope(credentials, read, used_after):
    """Recorded, not judged: what the S3 credentials B got before the disable still do; fails only when
    there were none to try."""
    if not credentials.get("accessKeyId"):
        return False, "B got no credentials before the disable"
    ok_read, why = read
    expires = credentials.get("expiresAtMillis")
    return True, (f"the credentials B got before the disable, used {used_after} s after it: "
                  f"{'still read' if ok_read else 'refused: ' + why} (they expire at "
                  f"{time.strftime('%H:%M:%SZ', time.gmtime(expires / 1000)) if expires else '?'}); recorded, not judged")


def judge_audited(refusals, audited):
    """Every refusal the server made (its own request ids) is an audit event."""
    missing = sorted(i for i in refusals if i not in audited)
    return bool(refusals) and not missing, (f"{len(refusals)} refusals the server made (with A, C and the disabled "
                                            f"B); {len(missing)} not in the audit: {missing[:3]}")


def credentials_of(body):
    token = (body or {}).get("token") or {}
    return {"accessKeyId": token.get("fs.s3a.access-key") or token.get("fs.s3a.access.key"),
            "secretAccessKey": token.get("fs.s3a.secret-key") or token.get("fs.s3a.secret.key"),
            "sessionToken": token.get("fs.s3a.session-token") or token.get("fs.s3a.session.token"),
            "expiresAtMillis": (body or {}).get("expiresAtMillis")}


# --- the run ------------------------------------------------------------------------------------------

def reader(ctx, name, key, url):
    return Background(ctx.probe, name, [{"name": name, "url": url, "token": key, "repeat": {"every": READ_EVERY, "for": 3600}}],
                      ctx.admin.tokens)


def s3_read(ctx, credentials, path):
    """Whether the table's schema file reads with these credentials alone (a GetObject from this host)."""
    m = re.match(r"^s3a?://([^/]+)/(.+?)/?$", path)
    p = ctx.aws.raw("s3api", "get-object", "--bucket", m.group(1), "--key", f"{m.group(2)}/schema/schema-0", "/dev/null",
                    credentials=credentials, check=False)
    return p.returncode == 0, (p.stderr.strip()[-200:] or "read")


def run(ctx):
    db, table, who = f"cat06_{ctx.run}", "t", f"rot-{ctx.run}"
    admin, rest = ctx.admin, ctx.rest
    url = f"{ctx.install.rest_base}/databases/{db}/tables/{table}"
    with ctx.preparing("the table, the principal, tokens A and C, and a reader with A") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT")])
        admin.principal(who, ID)
        admin.grant(who, "TABLE", "READ", database=db, obj=table)
        a = admin.issue(who, "P1D", key="A")
        admin.issue(who, "PT1M", key="C")
        c_issued = time.time()
        time.sleep(5)
        reader_a = reader(ctx, "reader-a", "A", url)
        notes.append(f"{db}.{table}, {who}")
    time.sleep(3 * READ_EVERY)

    # --- step 1: the manual's rotation order -------------------------------------------------------
    admin.issue(who, "P1D", key="B")
    with ctx.preparing("the reader switched to B"):
        reader_b = reader(ctx, "reader-b", "B", url)
    time.sleep(READ_EVERY)
    reads_a = reader_a.stop(keep="reader-a.jsonl")
    admin.revoke(a)
    revoked = time.time()

    # --- step 2: A, every second for 90 s, from 1 s after the revocation ----------------------------
    time.sleep(1)
    refused = ctx.probe.run([{"name": "A", "url": url, "token": "A", "repeat": {"every": 1, "for": REFUSE_FOR}}],
                            tokens=admin.tokens, keep="token-a.jsonl")

    # --- step 3: C, a minute after it was issued, and 10 s more --------------------------------------
    time.sleep(max(0, c_issued + 60 + 10 - time.time()))
    c = ctx.probe.run([{"name": "C", "url": url, "token": "C"}], tokens=admin.tokens, keep="token-c.jsonl")[0]
    ctx.check("expired-refused", c.get("status") == 401, f"C {describe(c)}")

    # --- step 4: B's credentials, then the principal disabled ---------------------------------------
    issued = ctx.probe.run([{"name": "B-token", "url": f"{url}/token", "token": "B", "full": True},
                            {"name": "B-table", "url": url, "token": "B"}], tokens=admin.tokens, keep="token-b.jsonl")
    creds = credentials_of(issued[0].get("body"))
    ctx.sanitizer.secret_sources.append(lambda: [creds.get("secretAccessKey"), creds.get("sessionToken")])
    path = (issued[1].get("body") or {}).get("path", "")
    admin.disable(who)
    disabled = time.time()
    time.sleep(GRACE)
    after = by_name(ctx.probe.run([{"name": "B-api", "url": url, "token": "B"},
                                   {"name": "B-credentials", "url": f"{url}/token", "token": "B"}],
                                  tokens=admin.tokens, keep="token-b.jsonl"))
    reads_b = reader_b.stop(keep="reader-b.jsonl")
    ctx.check("rotation-uninterrupted", *judge_rotation(reads_a, reads_b, disabled))
    ctx.check("disabled-refused", *judge_disabled(after["B-api"], after["B-credentials"]))
    read = s3_read(ctx, creds, path) if creds.get("accessKeyId") and path else (False, "no credentials")
    ctx.check("issued-credentials-scope", *judge_scope(creds, read, int(time.time() - disabled)))

    # --- what the server answered, by its own request ids; and the audit ----------------------------
    refusals = [r for r in refused + [c, after["B-api"], after["B-credentials"]] if r.get("serverRequestId")]
    ids = {r["serverRequestId"] for r in refusals}
    answered = answered_by(access_log(ctx, revoked), {r["serverRequestId"] for r in refused if r.get("serverRequestId")})
    ctx.check("revoked-refused-by-the-server", *judge_revoked(ctx.form, refused, revoked, answered))
    events = admin.audit(outcome="DENIED", since=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(revoked - 5)))
    ctx.keep("audit-denied.json", events)
    ctx.check("refusals-audited", *judge_audited(ids, {e["requestId"] for e in events}))
