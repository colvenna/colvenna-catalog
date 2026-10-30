"""UAT-CAT-08 Request and response size limits (from the serverless spike's driver.py, its sizes and
big-response actions, and request-sizes/).

Each form's limits, from a client pod: in the EKS form a commit body over the server's 64 MiB limit;
in the serverless form commits of 41,000 and 42,000 partitions (the Paimon 2.0.0 client's commit,
spikes' template, about 5.6 and 5.7 MB: under and over the Lambda event's 6 MB), table-details
pages of 16 wide tables, which must each stay under the 4 MiB response limit
(COLVENNA_MAX_RESPONSE_BYTES), and one table too large for any page by itself. Before and after
each refused request, the table's pointer, its snapshots and its commit intents are read: a
refusal changes nothing."""

import copy
import json
import time
import uuid
from pathlib import Path

from uatlib.probe import Probe, describe
from uatlib.sql import Sql

ID = "UAT-CAT-08"
FORMS = ("eks", "serverless")
TEMPLATE = Path(__file__).resolve().parent / "data" / "commit-template.json"
EKS_LIMIT = 64 * 1024 * 1024
OVER_BY = 1024
RESPONSE_LIMIT = 4 * 1024 * 1024
UNDER, OVER = 41_000, 42_000


def checks(form, args=None):
    if form == "eks":
        return ["eks-body-over-the-limit"]
    return ["serverless-commit-under-the-limit", "serverless-commit-over-the-limit", "serverless-pages-under-4-mib",
            "serverless-item-over-the-limit"]


def commit_body(table_id, base_uuid, snapshot_id, partitions):
    """A commit as the Paimon 2.0.0 client sends it, with one partition statistic per partition."""
    body = json.loads(TEMPLATE.read_text())
    body["tableId"] = table_id
    body["baseSnapshotUuid"] = base_uuid
    snapshot = body["snapshot"]
    snapshot.update(uuid=str(uuid.uuid4()), id=snapshot_id, commitUser=str(uuid.uuid4()),
                    timeMillis=int(time.time() * 1000))
    entry = body["statistics"][0]
    body["statistics"] = []
    for p in range(partitions):
        e = copy.deepcopy(entry)
        e["spec"] = {"dt": "2026-%05d" % p}
        e["totalBuckets"] = -1
        body["statistics"].append(e)
    return json.dumps(body, separators=(",", ":"))


class State:
    """A table's pointer, snapshots and commit intents, as the catalog keeps them: its snapshot and
    snapshot list through the REST API, its pointer, newest snapshot file and pending intents through
    the consistency check, and, where SQL reaches the database (the EKS form), every intent."""

    def __init__(self, ctx, db, table):
        self.ctx, self.db, self.table = ctx, db, table
        self.path = f"/databases/{db}/tables/{table}"

    def read(self):
        rest = self.ctx.rest
        loaded = rest.call("GET", self.path + "/snapshot", expect=(200, 404))
        snap = ((loaded.get("body") or {}).get("snapshot") or {}).get("snapshot") or {}
        listed = rest.call("GET", self.path + "/snapshots", expect=(200, 404))
        snapshots = len((listed.get("body") or {}).get("snapshots") or [])
        report = self.ctx.admin.consistency(self.db, self.table)
        main = next((b for b in report.get("branches", []) if b.get("branch") == "main"), {})
        state = {"pointer": snap.get("id"), "uuid": snap.get("uuid"), "snapshots": snapshots,
                 "check": {k: main.get(k) for k in ("pointerSnapshot", "highestSnapshotFile", "pendingIntents")}}
        if self.ctx.form == "eks":
            state["intents"] = Sql(self.ctx).query(
                "SELECT i.snapshot_id, i.state FROM commit_intents i JOIN catalog_tables t ON t.id = i.table_id "  # nosec B608 - the harness's own names, no outside input
                f"WHERE t.database_name = '{self.db}' AND t.name = '{self.table}' ORDER BY i.id")
        return state


# --- judging ------------------------------------------------------------------------------------------

def judge_body_over(answer, before, after, limit=EKS_LIMIT):
    """A body over the server's limit: 400 naming the limit, and the table as it was."""
    body = str(answer.get("body"))
    named = str(limit) in body or "64 MiB" in body
    return answer.get("status") == 400 and named and before == after, (
        f"{describe(answer)[:200]}; state before {before}, after {after}")


def judge_commit_under(answer, before, after, size):
    ok = (answer.get("status") == 200 and after["snapshots"] == before["snapshots"] + 1
          and after["pointer"] == (before["pointer"] or 0) + 1)
    return ok, (f"{UNDER} partitions, {size} bytes: {describe(answer)[:120]}; snapshots {before['snapshots']} -> "
                f"{after['snapshots']}, pointer {before['pointer']} -> {after['pointer']}")


def judge_commit_over(answer, before, after, size):
    return answer.get("status") == 413 and after == before, (
        f"{OVER} partitions, {size} bytes: {describe(answer)[:120]}; state before {before}, after {after}")


def judge_pages(pages, names, problems, tables):
    """pages: each page's bytes; names: the tables they listed; each page under the response limit,
    none empty, more than one, every table once."""
    too_big = [b for b in pages if b >= RESPONSE_LIMIT]
    ok = not problems and not too_big and len(pages) > 1 and sorted(names) == sorted(tables)
    return ok, (f"{len(pages)} pages of {pages} bytes for {len(names)} of {len(tables)} tables; over 4 MiB: {too_big}"
                + ("; " + "; ".join(problems) if problems else ""))


def judge_item_over(made, answer, failed_events, table):
    """One table too large for a page: 500 naming the table and the limit, one failed audit event, and
    no empty page instead."""
    text = str(answer.get("body"))
    ok = (made.get("status") == 200 and answer.get("status") == 500 and table in text
          and (str(RESPONSE_LIMIT) in text or "4 MiB" in text) and failed_events == 1)
    return ok, (f"the table made: {made.get('status')}; its page: {describe(answer)[:200]}; failed audit events: "
                f"{failed_events}")


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    db = f"cat08_{ctx.run}"
    rest = ctx.rest
    with ctx.preparing("the case's database, its partitioned table and a client for big bodies") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, "parts", [("id", "INT"), ("payload", "STRING"), ("dt", "STRING")], partition_keys=["dt"],
                          options={"bucket": "-1"})
        table = rest.get_table(db, "parts")
        big = Probe(ctx.kube, ctx.namespace("client"), name="big-probe", base=ctx.install.uri, ca=ctx.install.ca(),
                    evidence=ctx.evidence, sanitizer=ctx.sanitizer, memory="1536Mi").start()
        notes.append(f"{db}.parts")
    state = State(ctx, db, "parts")
    commit_url = f"{ctx.install.rest_base}/databases/{db}/tables/parts/commit"
    if ctx.form == "eks":
        before = state.read()
        r = big.run([{"name": "over-64-mib", "kind": "stream", "method": "POST", "url": commit_url, "token": "admin",
                      # 1 KiB over: the server reads up to its limit and drains what is left of a body that
                      # small, so that its answer arrives whole; much more, and it resets the connection.
                      "bytes": EKS_LIMIT + OVER_BY, "timeout": 300}], tokens=ctx.admin.tokens, keep="requests.jsonl")[0]
        after = state.read()
        ctx.check("eks-body-over-the-limit", *judge_body_over(r, before, after))
        return

    # --- serverless: the event limit on commits ------------------------------------------------------
    before = state.read()
    body = commit_body(table["id"], before["uuid"], (before["pointer"] or 0) + 1, UNDER)
    r = big.run([{"name": f"commit-{UNDER}", "method": "POST", "url": commit_url, "token": "admin", "text": body,
                  "timeout": 120}], tokens=ctx.admin.tokens, keep="requests.jsonl")[0]
    after = state.read()
    ctx.check("serverless-commit-under-the-limit", *judge_commit_under(r, before, after, len(body)))
    before = after
    body = commit_body(table["id"], before["uuid"], (before["pointer"] or 0) + 1, OVER)
    r = big.run([{"name": f"commit-{OVER}", "method": "POST", "url": commit_url, "token": "admin", "text": body,
                  "timeout": 120}], tokens=ctx.admin.tokens, keep="requests.jsonl")[0]
    after = state.read()
    ctx.check("serverless-commit-over-the-limit", *judge_commit_over(r, before, after, len(body)))

    # --- serverless: pages under the response limit --------------------------------------------------
    wide = f"cat08_{ctx.run}_wide"
    rest.create_database(wide)
    ctx.on_cleanup(f"database {wide} dropped", lambda: rest.drop_database(wide) and "dropped")
    columns = [("c%05d_%s" % (i, "x" * 90), "STRING") for i in range(3000)]
    for t in range(16):
        rest.create_table(wide, "t%02d" % t, columns)
    pages, names, token, problems = [], [], None, []
    for _ in range(40):
        url = f"{ctx.install.rest_base}/databases/{wide}/table-details?maxResults=100" + (f"&pageToken={token}" if token else "")
        r = big.run([{"name": "page", "url": url, "token": "admin", "full": True, "timeout": 60}],
                    tokens=ctx.admin.tokens, keep="pages.jsonl")[0]
        if r.get("status") != 200:
            problems.append(f"page {len(pages) + 1}: {describe(r)[:120]}")
            break
        pages.append(r.get("bytes", 0))
        details = (r.get("body") or {}).get("tableDetails") or []
        names += [d.get("name") for d in details]
        if not details:
            problems.append(f"page {len(pages)} is empty")
        token = (r.get("body") or {}).get("nextPageToken")
        if not token:
            break
    ctx.check("serverless-pages-under-4-mib", *judge_pages(pages, names, problems, ["t%02d" % t for t in range(16)]))

    # --- serverless: one table too large for any page -------------------------------------------------
    huge_db = f"cat08_{ctx.run}_huge"
    rest.create_database(huge_db)
    ctx.on_cleanup(f"database {huge_db} dropped", lambda: rest.drop_database(huge_db) and "dropped")
    columns = [("c%05d_%s" % (i, "y" * 200), "STRING") for i in range(20_000)]
    made = rest.call("POST", f"/databases/{huge_db}/tables",
                     {"identifier": {"database": huge_db, "object": "huge"},
                      "schema": {"fields": [{"id": i, "name": n, "type": k} for i, (n, k) in enumerate(columns)],
                                 "partitionKeys": [], "primaryKeys": [], "options": {}}}, expect=None)
    r = big.run([{"name": "huge-page", "url": f"{ctx.install.rest_base}/databases/{huge_db}/table-details?maxResults=100",
                  "token": "admin", "timeout": 60}], tokens=ctx.admin.tokens, keep="huge.jsonl")[0]
    time.sleep(5)
    failed = [e for e in ctx.admin.audit(action="LIST_TABLE_DETAILS", outcome="FAILED")
              if e.get("requestId") == r.get("serverRequestId")]
    ctx.check("serverless-item-over-the-limit", *judge_item_over(made, r, len(failed), "huge"))
