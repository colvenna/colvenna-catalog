"""The catalog's access log, which records every request: one line per request from the
colvenna.access logger, "<time> INFO [<thread>] colvenna.access - <request id> <method> <path>
<status> <ms>ms principal=<name> action=<ACTION>". The endpoint coverage check reads it: a request
no route serves (action "-" from an authenticated principal) or an action answered 501 means the
engine called an endpoint the catalog does not serve."""

import re
import urllib.parse
from datetime import datetime, timezone

LINE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}(:?\d{2})?)?)\s+\S+\s+\[[^\]]*\]\s+"
    r"colvenna\.access - (?P<id>\S+) (?P<method>[A-Z]+) (?P<path>\S+) (?P<status>\d{3}) (?P<ms>\d+)ms"
    r" principal=(?P<principal>\S+) action=(?P<action>\S+)\s*$"
)
# 501s the catalog answers by design, each with why a client may meet it.
DECLINED_BY_DESIGN = {
    "LIST_PARTITIONS": "partition listing of a table whose partitions live in its files: the Paimon "
    "client falls back to the file system (LLD, endpoint scope)",
}
TABLE_PATH = re.compile(r"^/v1/[^/]+/databases/(?P<db>[^/]+)/tables/(?P<table>[^/]+)(/(?P<rest>.*))?$")
DATABASE_PATH = re.compile(r"^/v1/[^/]+/databases/(?P<db>[^/]+)(/.*)?$")


class Request:
    __slots__ = ("time", "id", "method", "path", "status", "ms", "principal", "action")

    def __init__(self, time, id, method, path, status, ms, principal, action):
        self.time, self.id, self.method, self.path = time, id, method, path
        self.status, self.ms, self.principal, self.action = status, ms, principal, action

    @property
    def database(self):
        m = DATABASE_PATH.match(self.path)
        return urllib.parse.unquote(m.group("db")) if m else None

    @property
    def table(self):
        """The table's name as the path holds it, decoded: t_pk$snapshots, say."""
        m = TABLE_PATH.match(self.path)
        return urllib.parse.unquote(m.group("table")) if m else None

    def unserved(self):
        """Why this request is one the catalog does not serve, or None."""
        if self.principal == "-":
            return None  # unauthenticated: refused before any route is resolved
        if self.action == "-" and self.status == 404:
            return "no such endpoint"
        if self.status == 501 and self.action not in DECLINED_BY_DESIGN:
            return "not implemented"
        return None

    def describe(self):
        return f"{self.method} {self.path} -> {self.status} ({self.action})"


def parse_time(s):
    """A logback timestamp (pattern X: Z, +08, +0800 or +08:00) as an aware datetime."""
    s = s.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    elif re.search(r"[+-]\d{2}$", s):
        s += ":00"
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse(text):
    """Every access log line in the text (other lines are skipped), in order."""
    out = []
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        try:
            when = parse_time(m.group("time"))
        except ValueError:
            continue
        out.append(Request(when, m.group("id"), m.group("method"), m.group("path"),
                           int(m.group("status")), int(m.group("ms")), m.group("principal"), m.group("action")))
    out.sort(key=lambda r: r.time)
    return out


def window(requests, since, until, principals=None):
    """The requests made in [since, until], by one of the principals if any are given."""
    return [r for r in requests if since <= r.time <= until and (not principals or r.principal in principals)]


# A database of any run of the matrix (runner.Run's prefix and a cell's slug).
MATRIX_DATABASE = re.compile(r"^compat_\d{14}_[0-9a-f]{4}_")


def attribute(requests, database, tables, run_prefix):
    """Splits a cell's requests: {table: [...]} for each of its tables, and the rest (the catalog's
    config, database listings and lookups). Requests on another database of the run (its prefix),
    or of another run of the matrix, are another cell's and are dropped, so that a cell is never
    judged on what ran beside it."""
    by_table = {t: [] for t in tables}
    shared = []
    for r in requests:
        db = r.database
        if db is not None and db != database and (db.startswith(run_prefix) or MATRIX_DATABASE.match(db)):
            continue
        t = r.table
        if db == database and t is not None:
            base = t.split("$", 1)[0]
            if base in by_table:
                by_table[base].append(r)
                continue
        shared.append(r)
    return by_table, shared


def summary(requests):
    """{"<METHOD> <ACTION> <status>": count}, the engine's footprint on the catalog."""
    counts = {}
    for r in requests:
        key = f"{r.method} {r.action} {r.status}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def count(requests, action, status=200):
    return sum(1 for r in requests if r.action == action and r.status == status)
