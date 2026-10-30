"""A case run's verdicts and its result, as the UAT cases document says (docs/uat/cases.md, sections
3.2 and 3.3).

Every check a case lists is one line its harness writes to verdicts.tsv, when it is decided: the
time (UTC, ISO 8601), the check's name, PASS or FAIL, and a detail. A precondition the case depends
on is a line too, named "precondition:<what>"; one that failed blocks the case. The case's result is
judged from those lines, the checks the case lists for the form, and the run's exit status:

  PASS     the run exited 0 and every check it lists is there and PASS (a check marked "only on
           failure" may be absent);
  FAIL     a check is FAIL, or a check that should be there is not;
  BLOCKED  a precondition failed, so the case could not run or its result cannot be trusted;
  NOT_RUN  it did not run (no verdicts at all).
"""

import csv
import io
import os
from datetime import datetime, timezone
from pathlib import Path

PASS, FAIL = "PASS", "FAIL"
RESULTS = ("PASS", "FAIL", "BLOCKED", "NOT_RUN")
PRECONDITION = "precondition:"
HEADER = ("time", "check", "status", "detail")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean(text):
    """One line of a TSV field: no tab, no newline."""
    return " ".join(str(text).replace("\t", " ").split())


class Blocked(Exception):
    """A precondition failed: the case stops, and its result is BLOCKED."""


class Verdicts:
    """verdicts.tsv of one case run. Each line is written and flushed when the check is decided, so
    that a run killed half-way still shows what it decided. Details go through the sanitizer first:
    no token, account ID, ARN, address or bucket is kept."""

    def __init__(self, path, sanitizer=None, log=None):
        self.path = Path(path)
        self.sanitizer = sanitizer
        self.log = log or (lambda line: None)
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.file = os.fdopen(fd, "a", encoding="utf-8")
        if new:
            self.file.write("\t".join(HEADER) + "\n")
            self.file.flush()
        self.lines = []

    def close(self):
        self.file.close()

    def write(self, check, ok, detail=""):
        status = PASS if ok else FAIL
        detail = clean(detail)
        if self.sanitizer:
            detail = self.sanitizer.text(detail)
        line = (now(), check, status, detail)
        self.file.write("\t".join(line) + "\n")
        self.file.flush()
        self.lines.append(line)
        self.log(f"{status} {check}" + (f": {detail}" if detail else ""))
        return ok

    def check(self, check, ok, detail=""):
        """Records the check; returns whether it passed."""
        if check.startswith(PRECONDITION):
            raise ValueError(f"{check}: a check, not a precondition")
        return self.write(check, bool(ok), detail)

    def precondition(self, what, ok, detail=""):
        """Records a precondition; one that failed raises Blocked."""
        self.write(PRECONDITION + what, bool(ok), detail)
        if not ok:
            raise Blocked(f"{what}: {detail}")
        return True


def read(path):
    """The lines of a verdicts.tsv as (time, check, status, detail) tuples; [] when there is none."""
    path = Path(path)
    if not path.exists():
        return []
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE))
    if rows and tuple(rows[0]) == HEADER:
        rows = rows[1:]
    return [tuple((r + [""] * 4)[:4]) for r in rows if r]


def judge(lines, expected, exit_code, optional=(), ran=True):
    """The case's result and why: (result, [reasons]). lines: read()'s tuples; expected: the check
    names the case lists for the form; optional: those of them marked "only on failure"; exit_code:
    the run's, or None when it never ended (killed); ran: whether the case began at all (False when
    the harness could not even start it: NOT_RUN)."""
    if not ran:
        return "NOT_RUN", ["the case did not run"]
    status = {}
    for _, check, value, _ in lines:
        # A check passes only if every line of it does.
        status[check] = FAIL if FAIL in (value, status.get(check)) else value
    blocked = [c[len(PRECONDITION):] for c, v in status.items() if c.startswith(PRECONDITION) and v != PASS]
    if blocked:
        return "BLOCKED", [f"precondition failed: {b}" for b in blocked]
    reasons = []
    for check in expected:
        if check not in status:
            if check not in optional:
                reasons.append(f"{check}: missing")
        elif status[check] != PASS:
            reasons.append(f"{check}: {status[check]}")
    unknown = sorted(c for c in status if not c.startswith(PRECONDITION) and c not in expected)
    reasons += [f"{c}: not a check of this case" for c in unknown]
    if exit_code != 0:
        reasons.append(f"the run exited {exit_code}" if exit_code is not None else "the run did not end")
    return ("PASS" if not reasons else "FAIL"), reasons
