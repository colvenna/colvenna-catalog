"""The scenarios of docs/performance/terms.md (section 4) as the harness runs them: each one's load,
durations and targets per form. `check()` holds the targets to the document's own tables, cell by
cell, so that the harness judges by the confirmed terms and nothing else; a change to the terms is a
change here too, and the check fails until both agree.

Durations can be scaled down for a trial of the harness (`--duration-scale`); a run at any other
scale than 1 is marked `reduced` and is never a candidate's acceptance."""

import re
from dataclasses import dataclass, field

from . import REPO

TERMS = REPO / "docs" / "performance" / "terms.md"
FORMS = ("eks", "serverless")
SCENARIOS = ("P1a", "P1b", "P2", "P3", "P4a", "P4b", "P5")

# The Paimon client's timeouts and retries, part of the method (terms.md section 4, P4; B6): read in
# paimon-api 2.0.0's HttpClientUtils and ExponentialHttpRequestRetryStrategy. The load tool sets
# none of them: it runs the client as it is.
CLIENT = {
    "paimon": "2.0.0",
    "responseTimeout": "3 minutes",
    "connectionRequestTimeout": "3 minutes",
    "connectTimeout": "3 minutes (HttpClient 5.5.1's default)",
    "retries": "ExponentialHttpRequestRetryStrategy(5): 429 and 503 retried whatever the method, after 1, 2, 4, 8 "
               "and 16 s (+ up to 10 % jitter, or Retry-After), about 31 s in all; I/O errors only for idempotent "
               "methods; never a timeout, a refused connection, an unknown host, a TLS error or a 504",
    "commitMaxRetries": "Paimon's default commit.max-retries (10)",
}


@dataclass
class Target:
    """One row of a scenario's targets table: its label as the document writes it, the cell of each
    column (a form, or P4a / P4b), and the values the harness judges with."""
    row: str
    cells: dict
    values: dict = field(default_factory=dict)


# Per scenario: the targets table's rows. Latencies in milliseconds.
TARGETS = {
    "P1": [
        Target("P1a commits per second", {"EKS": "≥ 10", "Serverless": "≥ 5"},
               {"eks": 10, "serverless": 5}),
        Target("P1a commit request p95 / p99", {"EKS": "≤ 500 ms / ≤ 1,000 ms", "Serverless": "≤ 1,000 ms / ≤ 2,000 ms"},
               {"eks": {"p95": 500, "p99": 1000}, "serverless": {"p95": 1000, "p99": 2000}}),
        Target("P1a errors", {"EKS": "0", "Serverless": "0"}, {"eks": 0, "serverless": 0}),
        Target("P1b commits done of those offered", {"EKS": "≥ 99 %, none failed", "Serverless": "≥ 99 %, none failed"},
               {"eks": 0.99, "serverless": 0.99}),
        Target("P1b whole commit p95 / p99", {"EKS": "≤ 3 s / ≤ 6 s", "Serverless": "≤ 5 s / ≤ 10 s"},
               {"eks": {"p95": 3000, "p99": 6000}, "serverless": {"p95": 5000, "p99": 10000}}),
    ],
    "P2": [
        Target("`getTable`, load snapshot p95 / p99, at the target rate",
               {"EKS": "≤ 50 ms / ≤ 100 ms", "Serverless": "≤ 150 ms / ≤ 300 ms"},
               {"eks": {"p95": 50, "p99": 100}, "serverless": {"p95": 150, "p99": 300}}),
        Target("`listTables` p95 / p99, at the target rate",
               {"EKS": "≤ 100 ms / ≤ 250 ms", "Serverless": "≤ 250 ms / ≤ 500 ms"},
               {"eks": {"p95": 100, "p99": 250}, "serverless": {"p95": 250, "p99": 500}}),
        Target("Sustainable rate", {"EKS": "≥ 200 requests per second",
                                    "Serverless": "≥ 50 requests per second at concurrency 10"},
               {"eks": 200, "serverless": 50}),
    ],
    "P3": [
        Target("Miss p95 / p99", {"EKS": "≤ 500 ms / ≤ 1,000 ms", "Serverless": "≤ 1,000 ms / ≤ 2,000 ms"},
               {"eks": {"p95": 500, "p99": 1000}, "serverless": {"p95": 1000, "p99": 2000}}),
        Target("Hit p95 / p99", {"EKS": "≤ 50 ms / ≤ 100 ms", "Serverless": "≤ 150 ms / ≤ 300 ms"},
               {"eks": {"p95": 50, "p99": 100}, "serverless": {"p95": 150, "p99": 300}}),
        Target("Errors", {"EKS": "0", "Serverless": "0"}, {"eks": 0, "serverless": 0}),
    ],
    "P4": [
        Target("Errors: init timeouts, 5xx, 504", {"P4a on-demand": "0 in every sample", "P4b SnapStart": "0 in every sample"},
               {"P4a": 0, "P4b": 0}),
        Target("Server `Init Duration` (P4a) or `Restore Duration` (P4b) p95 / max",
               {"P4a on-demand": "≤ 4 s / ≤ 6 s", "P4b SnapStart": "≤ 1.5 s / ≤ 2 s"},
               {"P4a": {"p95": 4000, "max": 6000}, "P4b": {"p95": 1500, "max": 2000}}),
        Target("First request, client-observed, server and authorizer cold, p95 / max",
               {"P4a on-demand": "≤ 7 s / ≤ 10 s", "P4b SnapStart": "≤ 5 s / ≤ 8 s"},
               {"P4a": {"p95": 7000, "max": 10000}, "P4b": {"p95": 5000, "max": 8000}}),
    ],
    "P5": [
        Target("Errors", {"Target": "0 in both forms"}),
        Target("Each form's latencies at this load",
               {"Target": "Within that form's targets of P1a (commit request), P2 (reads) and P3 (credentials)"}),
        Target("The difference between the forms", {"Target": "Reported, not judged"}),
    ],
}


def target(section, row):
    return next(t for t in TARGETS[section] if t.row == row)


# The load of each scenario at the terms' durations (seconds), before any scaling.
LOAD = {
    "P1a": {"writers": 8, "rows": 100, "interval": 0, "shared": False, "warmup": 60, "duration": 600},
    "P1b": {"writers": 4, "rows": 100, "interval": 2000, "shared": True, "warmup": 60, "duration": 600},
    "P2": {"databases": 20, "tables": 50, "mix": "getTable:60,snapshot:30,listTables:10", "page": 100,
           "steps": {"eks": [50, 100, 200, 400, 800], "serverless": [25, 50, 100, 150]},
           "target_rate": {"eks": 200, "serverless": 50}, "step": 300, "warmup": 120},
    # P3a: 3 a second for 6 minutes, each a pair never issued: 540 tables x 2 principals = 1,080 pairs.
    # P3b: 20 a second for 5 minutes over 20 tables issued before (each pair asked once, then a
    # minute of warm-up at the rate, so that every replica or execution environment holds it).
    "P3": {"miss": {"rate": 3, "duration": 360, "tables": 540}, "hit": {"rate": 20, "duration": 300, "tables": 20,
                                                                         "warmup": 60}},
    "P4": {"rounds": 8, "clients": 5, "idle_samples": 6, "idle_minutes": 30},
    # P5: the terms give no warm-up length: P2's two minutes.
    "P5": {"writers": 4, "interval": 2000, "rows": 100, "read_rate": 50, "token_rate": 2, "miss_share": 0.1,
           "hit_tables": 20, "warmup": 120, "duration": 1800},
}

# What "sent on time" means for an open-loop request (P2's sustainable rate): it started within this
# long of the time it was due. The terms ask for 99 % on time and leave the bound to the method.
ON_TIME_MS = 100


def scaled(seconds, scale, floor=10):
    """Seconds at the scale, at least floor (a reduced trial still runs each phase)."""
    return int(seconds) if scale == 1 else max(floor, int(round(seconds * scale)))


# --- holding the targets to the document ----------------------------------------------------------

ROW = re.compile(r"^\|(.+)\|\s*$")


def document_tables(text):
    """{section: {row label: {column: cell}}} of every targets table in terms.md's section 4: the
    tables after a "**Targets (confirmed ...):**" line, under "### P1." ... "### P5."."""
    out, section, header, in_targets = {}, None, None, False
    for line in text.splitlines():
        m = re.match(r"^### (P\d)\.", line)
        if m:
            section, header, in_targets = m.group(1), None, False
            continue
        if line.startswith("## ") and not line.startswith("## 4."):
            section = None
        if section and line.startswith("**Targets"):
            in_targets, header = True, None
            continue
        if not (section and in_targets):
            continue
        m = ROW.match(line.strip())
        if not m:
            if header is not None and line.strip() == "":
                in_targets = False
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if header is None:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        out.setdefault(section, {})[cells[0]] = dict(zip(header[1:], cells[1:]))
    return out


def check(text=None):
    """Problems between the harness's targets and terms.md's tables; [] when they agree exactly."""
    text = TERMS.read_text(encoding="utf-8") if text is None else text
    tables = document_tables(text)
    problems = []
    for section, targets in TARGETS.items():
        rows = tables.get(section, {})
        if not rows:
            problems.append(f"{section}: terms.md has no targets table")
            continue
        for t in targets:
            if t.row not in rows:
                problems.append(f"{section}: terms.md has no row {t.row!r}")
            elif rows[t.row] != t.cells:
                problems.append(f"{section}: {t.row!r} is {rows[t.row]} in terms.md, {t.cells} here")
        for row in rows:
            if row not in [t.row for t in targets]:
                problems.append(f"{section}: terms.md's row {row!r} is not judged here")
    if "confirmed" not in text.split("\n## ", 1)[0]:
        problems.append("terms.md does not say its targets are confirmed")
    return problems
