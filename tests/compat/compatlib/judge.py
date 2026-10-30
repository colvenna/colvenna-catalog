"""From a cell's raw output and the catalog's access log to its verdicts: one per table type and
operation, PASS, FAIL or NOT_RUN, each with what it rests on. Nothing passes without its evidence:
a missing result is a FAIL, never a PASS."""

import json
import re

from . import accesslog
from .config import OPERATIONS, TABLE_TYPES

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
TABLES = {"pk": "t_pk", "pk-dv": "t_pk_dv", "append": "t_append"}
# The writes of each table type (Dataset in the flows): what the catalog must have committed.
WRITES = {"pk": 3, "pk-dv": 4, "append": 2}


def lines(text, kind):
    """The JSON bodies of the output's `<kind> {...}` lines."""
    out = []
    for line in text.splitlines():
        if line.startswith(kind + " "):
            try:
                out.append(json.loads(line[len(kind) + 1:]))
            except ValueError:
                pass
    return out


def flow_results(text):
    """{(table, op): (status, detail)} from a flow's COMPAT-RESULT lines."""
    return {(r["table"], r["op"]): (r["status"], r.get("detail", "")) for r in lines(text, "COMPAT-RESULT")}


def verdict(status, detail, **evidence):
    v = {"status": status, "detail": detail}
    if evidence:
        v["evidence"] = evidence
    return v


def endpoints(requests, shared):
    """The endpoint coverage check of one table type: its requests and the cell's shared ones. A
    log without a single request of the engine on the table is an incomplete log, not a pass."""
    if not requests:
        return verdict(FAIL, "the access log holds no request of the engine on this table: the log is "
                             "incomplete (a replica unread, another principal, or outside the cell's window)")
    every = requests + shared
    unserved = sorted({f"{r.describe()}: {r.unserved()}" for r in every if r.unserved()})
    footprint = accesslog.summary(every)
    if unserved:
        return verdict(FAIL, "the engine called endpoints the catalog does not serve: " + "; ".join(unserved),
                       requests=footprint)
    declined = sorted({r.action for r in every if r.status == 501})
    note = f"; 501 by design: {', '.join(declined)}" if declined else ""
    return verdict(PASS, f"{len(every)} requests, every one to an endpoint the catalog serves{note}",
                   requests=footprint)


def writer(text, problem, requests, database, run_prefix):
    """A writer cell's verdicts. requests: the access log's requests of the cell's time window, or
    None without a log."""
    fatal = lines(text, "COMPAT-FATAL")
    results = flow_results(text)
    by_table, shared = accesslog.attribute(requests or [], database, list(TABLES.values()), run_prefix)
    out = {}
    for table in TABLE_TYPES:
        name = TABLES[table]
        mine = by_table[name]
        v = {}
        for op in ("ddl", "write", "read", "credentials"):
            status, detail = results.get((table, op), (FAIL, ""))
            if (table, op) not in results:
                detail = (f"the flow could not run: {fatal[0]['detail']}" if fatal
                          else problem or "the flow printed no result for it")
            v[op] = verdict(status, detail)
        if requests is not None:
            commits = accesslog.count(mine, "COMMIT")
            if v["write"]["status"] == PASS:
                if commits < WRITES[table]:
                    v["write"] = verdict(FAIL, f"{v['write']['detail']}; but only {commits} commits reached the "
                                               "catalog", commits=commits)
                else:
                    v["write"] = verdict(PASS, f"{v['write']['detail']}; {commits} commits through the catalog",
                                         commits=commits)
            tokens = accesslog.count(mine, "ISSUE_DATA_TOKEN")
            if v["credentials"]["status"] == PASS:
                v["credentials"] = credentials(v, tokens)
            v["endpoints"] = endpoints(mine, shared)
        else:
            if v["credentials"]["status"] == PASS:
                v["credentials"] = credentials(v, None)
            v["endpoints"] = verdict(NOT_RUN, "the catalog's access log was not read (none given, or unreadable: see the raw output)")
        out[table] = v
    return out


def credentials(v, tokens):
    """Per-table credentials pass when the engine's own identity is refused the table's files (the
    flow's probe), the engine wrote and read the table all the same, and (with the log) the catalog
    issued it the table's credentials."""
    if v["write"]["status"] != PASS or v["read"]["status"] != PASS:
        return verdict(FAIL, "the probe passed, but the table was not both written and read")
    if tokens is None:
        return verdict(PASS, v["credentials"]["detail"] + " (the credentials issued were not counted: no access log)")
    if tokens == 0:
        return verdict(FAIL, "the engine's own identity is refused, but the catalog issued no credentials for "
                             "the table: what did it read and write with?", tokens=0)
    return verdict(PASS, f"{v['credentials']['detail']}; the catalog issued the table's credentials "
                         f"{tokens} times", tokens=tokens)


def reader(kind, text, problem, requests, database, run_prefix, probe, own_credentials="", planned=None):
    """A reader cell's verdicts. probe: {table: (status, detail)} of the probe that ran in the
    reader's own pod, of the pod's own identity; own_credentials: what StarRocks's own S3
    credentials are, as the run states them; planned: {table: [check names]}, every check the
    reader had to run (a check that never ran fails the read)."""
    reads = lines(text, "COMPAT-READ")
    done = bool(lines(text, "COMPAT-READ-DONE"))
    fatal = lines(text, "COMPAT-READ-FATAL")
    by_check = {(r["table"], r["check"]): r for r in reads}
    created = by_check.get(("-", "create-catalog"))
    by_table, shared = accesslog.attribute(requests or [], database, list(TABLES.values()), run_prefix)
    out = {}
    for table in TABLE_TYPES:
        v = {op: verdict(NOT_RUN, "read-only engine: the matrix does not run its writes") for op in ("ddl", "write")}
        checks = [r for r in reads if r["table"] == table]
        if fatal or created is None:
            why = fatal[0]["detail"] if fatal else problem or "the reader printed nothing"
            v["read"] = verdict(FAIL, f"the reader did not run: {why}")
        elif created["rc"] != 0:
            v["read"] = verdict(FAIL, f"the catalog could not be created: {first(created['error'])}")
        elif not checks:
            v["read"] = verdict(FAIL, problem or "no check of this table ran")
        else:
            wrong = [f"{c['check']}: " + (first(c["error"]) if c["rc"] != 0 else
                                         f"expected {c['expected']}, got {c['output'] or 'nothing'}")
                     for c in checks if c["rc"] != 0 or c["output"].strip() != str(c["expected"])]
            ran = {c["check"] for c in checks}
            wrong += [f"{name}: never ran" for name in (planned or {}).get(table, []) if name not in ran]
            if not done:
                wrong.append("the reader did not finish" + (f" ({problem})" if problem else ""))
            v["read"] = (verdict(FAIL, "; ".join(wrong)) if wrong else
                         verdict(PASS, ", ".join(f"{c['check']}={c['expected']}" for c in checks)))
        plan = next((p for p in lines(text, "COMPAT-READ-PLAN") if p.get("table") == table), None)
        if plan and plan.get("splits"):
            m = re.search(r"paimonNativeReadSplits=(\d+)/(\d+)", plan["splits"][0])
            if m:
                native, every = int(m.group(1)), int(m.group(2))
                v["read"]["detail"] += f"; Doris's native reader took {native} of {every} splits" + (
                    f", the Paimon SDK (JNI) {every - native}" if native < every else "")
        jni = [r for r in lines(text, "COMPAT-READ-JNI") if r["table"] == table]
        if jni:
            wrong = [f"{r['check']}: " + (first(r["error"]) if r["rc"] != 0 else f"got {r['output'] or 'nothing'}")
                     for r in jni if r["rc"] != 0 or r["output"].strip() != str(r["expected"])]
            v["read"]["detail"] += ("; with force_jni_scanner (the Paimon SDK alone): " +
                                    ("; ".join(wrong) if wrong else "the same values"))
        mine = by_table[TABLES[table]]
        tokens = accesslog.count(mine, "ISSUE_DATA_TOKEN") if requests is not None else None
        if kind == "starrocks":
            v["credentials"] = verdict(NOT_RUN, "StarRocks read with read-only S3 credentials of its own, not the "
                                                "catalog's" + (f" ({own_credentials})" if own_credentials else "")
                                       + (f"; the catalog issued it {tokens} table credentials"
                                          if tokens is not None else ""), tokens=tokens)
        else:
            probe_result = probe.get(table, (FAIL, "the reader's pod did not probe its own identity"))
            # The table's files were read when its queries answered at all, right values or not:
            # credentials are about how the files were reached, the read about what came back.
            answered = any(c["rc"] == 0 for c in checks)
            wrong_values = v["read"]["status"] != PASS and answered
            if v["read"]["status"] != PASS and not answered:
                v["credentials"] = verdict(FAIL if v["read"]["status"] == FAIL else NOT_RUN,
                                           "not judged: no query of the table answered", tokens=tokens)
            elif probe_result[0] != PASS:
                v["credentials"] = verdict(FAIL, f"the pod's own identity was not shown refused: {probe_result[1]}")
            elif tokens is None:
                v["credentials"] = verdict(PASS, "read with data-token.enabled=true and no S3 keys of its own, "
                                                 "where its pod's own identity is refused the table's files")
            elif tokens == 0:
                v["credentials"] = verdict(FAIL, "the catalog issued it no credentials for the table", tokens=0)
            else:
                v["credentials"] = verdict(PASS, "read with the table's credentials, which the catalog issued it "
                                                 f"{tokens} times; its pod's own identity is refused the table's "
                                                 "files", tokens=tokens)
            if wrong_values and v["credentials"]["status"] == PASS:
                v["credentials"]["detail"] += " (its queries answered, with the values the read entry shows)"
        v["endpoints"] = (endpoints(mine, shared) if requests is not None else
                          verdict(NOT_RUN, "the catalog's access log was not read (none given, or unreadable: see the raw output)"))
        out[table] = v
    return out


def what_the_tables_hold(verdicts, fixture_writes):
    """A reader's read verdicts say what its tables held, from their writer's write results:
    deletion vectors and uncompacted level-0 files in pk-dv's."""
    for table, v in verdicts.items():
        vectors = re.search(r"(\d+) deletion vector index files, (\d+) level-0 files", fixture_writes.get(table, ""))
        if vectors and v["read"]["status"] != NOT_RUN:
            v["read"]["detail"] += (f" (the table holds {vectors.group(1)} deletion vector index files, and "
                                    f"ids 111..120 uncompacted in {vectors.group(2)} level-0 files)")


def first(text):
    """An error's first meaningful line, at most 400 characters."""
    for line in (text or "").splitlines():
        if line.strip() and not line.startswith("Picked up JAVA_TOOL_OPTIONS"):
            return line.strip()[:400]
    return "(no message)"


def declare(results, declared, form, cell_id):
    """Each verdict with its declaration, and the cell's outcome: every declared-supported verdict
    passed (pass), or not (fail)."""
    failed = []
    for table in TABLE_TYPES:
        for op in OPERATIONS:
            value, reason = declared[(form, cell_id, table, op)]
            v = results[table][op]
            v["declared"] = value
            if reason:
                v["reason"] = reason
            if value == "supported" and v["status"] != PASS:
                failed.append(f"{table} {op}")
    return failed
