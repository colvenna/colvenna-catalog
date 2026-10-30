"""The raw data of a run, read back: the load tool's records (JSON lines), the Paimon client's request
log, the server's access log and Lambda's REPORT lines, and the joins terms.md section 3 makes.

- Client request lines, as the load tool's log4j2 configuration writes them: `<epoch ms> <thread>
  [rest] requestId:<id> method:<m> url:<u> duration:<ms>ms`, one per attempt; the id is the
  server's (its X-Request-Id answer), or `unknown` when the answer had none (one the gateway made).
- Access lines: `colvenna.access - <id> <method> <path> <status> <ms>ms principal=<p> action=<a>`.
- An operation of the load tool ran on one thread between its start and end: the request lines of
  that thread in that span are its attempts, so each operation gets its request ids, and through
  them the server's status and its audit event."""

import bisect
import json
import re

REST = re.compile(r"^(\d{13}) (\S+) \[rest\] requestId:(\S+) method:(\S+) url:(\S+) duration:(\d+)ms")
ACCESS = re.compile(r"colvenna\.access - (\S+) (\S+) (\S+) (\d{3}) (\d+)ms principal=(\S+) action=(\S+)")
# The REST API's paths below /v1/<warehouse>/ (Paimon's ResourcePaths) that the load uses.
KINDS = [
    ("commit", "POST", re.compile(r"/databases/[^/]+/tables/[^/]+/commit$")),
    ("snapshot", "GET", re.compile(r"/databases/[^/]+/tables/[^/]+/snapshot$")),
    ("token", "GET", re.compile(r"/databases/[^/]+/tables/[^/]+/token$")),
    ("getTable", "GET", re.compile(r"/databases/[^/]+/tables/[^/]+$")),
    ("listTables", "GET", re.compile(r"/databases/[^/]+/tables$")),
]


def kind(method, url):
    """The request's kind: config, commit, snapshot, token, getTable, listTables, or METHOD:other."""
    path = re.sub(r"^https?://[^/]+", "", url).split("?", 1)[0]
    path = re.sub(r"^/[^/]+(?=/v1/)", "", path)  # a stage prefix (the serverless form's API)
    if path.endswith("/v1/config"):
        return "config"
    for name, m, pattern in KINDS:
        if method == m and pattern.search(path):
            return name
    return f"{method}:other"


def records(text):
    """The load tool's records: one JSON object a line."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def facts(text):
    """The load tool's facts: every `PERF <json>` line of its output."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith("PERF {"):
            try:
                out.append(json.loads(line[5:]))
            except ValueError:
                continue
    return out


def rest_lines(text):
    out = []
    for line in (text or "").splitlines():
        m = REST.match(line)
        if m:
            t, thread, rid, method, url, ms = m.groups()
            out.append({"t": int(t), "thread": thread, "id": rid, "method": method, "kind": kind(method, url),
                        "ms": int(ms)})
    return out


def access_lines(text):
    """{request id: access entry}; the last line of an id wins (there is one)."""
    out = {}
    for line in (text or "").splitlines():
        m = ACCESS.search(line)
        if m:
            rid, method, path, status, ms, principal, action = m.groups()
            out[rid] = {"id": rid, "method": method, "path": path, "status": int(status), "ms": int(ms),
                        "principal": principal, "action": action}
    return out


class ThreadIndex:
    """The request lines of each thread, by time, for joining operations to their attempts."""

    def __init__(self, lines):
        self.by_thread = {}
        for line in lines:
            self.by_thread.setdefault(line["thread"], []).append(line)
        for items in self.by_thread.values():
            items.sort(key=lambda x: x["t"])
        self.times = {k: [x["t"] for x in v] for k, v in self.by_thread.items()}

    def attempts(self, thread, start, end, kinds=None):
        """The request lines of the thread from start to end (epoch ms, inclusive, plus a millisecond
        for the clocks' rounding), of the kinds given (all if None)."""
        items, times = self.by_thread.get(thread, []), self.times.get(thread, [])
        lo, hi = bisect.bisect_left(times, start), bisect.bisect_right(times, end + 1)
        return [x for x in items[lo:hi] if kinds is None or x["kind"] in kinds]


def join(ops, index, kinds, access=None, start_key="start", end_key="end"):
    """Each operation with its attempts (`attempts`: [{id, ms, status}]) and the server's status of
    its last attempt (`serverStatus`), in place; returns the operations."""
    for op in ops:
        found = index.attempts(op.get("thread"), op[start_key], op[end_key], kinds)
        op["attempts"] = [{"id": a["id"], "ms": a["ms"],
                           "status": (access or {}).get(a["id"], {}).get("status")} for a in found]
        op["serverStatus"] = op["attempts"][-1]["status"] if op["attempts"] else None
    return ops


# --- Lambda's REPORT lines --------------------------------------------------------------------------

def _number(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def environments(events):
    """Per execution environment (log stream) of a function's log events (filter-log-events'
    `events`): its init or restore duration, its first invocation's duration and time, the number
    of invocations, and whether an init timed out or failed."""
    streams = {}
    for e in events:
        streams.setdefault(e["logStreamName"], []).append(e)
    out = []
    for stream, items in streams.items():
        items.sort(key=lambda e: e["timestamp"])
        text = "\n".join(e["message"] for e in items)
        reports = [e for e in items if e["message"].startswith("REPORT")]
        out.append({
            "stream": stream,
            "initDurationMs": _number(r"Init Duration: ([\d.]+) ms", text),
            "restoreDurationMs": _number(r"Restore Duration: ([\d.]+) ms", text),
            "firstInvokeDurationMs": _number(r"\tDuration: ([\d.]+) ms", reports[0]["message"]) if reports else None,
            "firstInvokeAt": reports[0]["timestamp"] if reports else None,
            "firstEventAt": items[0]["timestamp"],
            "invocations": len(reports),
            "initTimedOut": bool(re.search(r"(INIT_REPORT|RESTORE_REPORT).*Status: timeout", text)),
            "initFailed": bool(re.search(r"(INIT_REPORT|RESTORE_REPORT).*Status: error", text)),
            "maxMemoryUsedMb": _number(r"Max Memory Used: (\d+) MB", text),
        })
    return out
