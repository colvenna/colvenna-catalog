"""What the UAT cases document (docs/uat/cases.md) says of each case: its checks (the first column of
its Expected table), the form each check is for, and its Harness line. The harness's cases are held
to it (test_doc.py): a case's checks are exactly the document's, so neither can drift from the other.

A check's name may be a template: "<entry>-<route>-<credential>" or "audit-<call>" stand for every
name a case makes by filling the placeholders in, and "args-*" for every name that starts with
"args-". A check is for one form when its expectation begins "(EKS" or "(serverless"; for one branch
of a case when it begins "(Same schema" or "(Schema moved forward"."""

import re
from dataclasses import dataclass, field
from pathlib import Path

DOC = Path(__file__).resolve().parents[3] / "docs" / "uat" / "cases.md"
HEADING = re.compile(r"^#### (?P<id>UAT-[A-Z]{3}-\d{2}) (?P<title>.+)$")
ROW = re.compile(r"^\| (?P<checks>`[^|]+`) \| (?P<expected>.*) \|$")
NAME = re.compile(r"`([^`]+)`")
FORMS = ("eks", "serverless")


@dataclass
class Check:
    name: str
    form: str = ""  # "eks", "serverless", or "" for both
    branch: str = ""  # "same-schema", "newer-schema", or ""
    negative: bool = False
    expected: str = ""

    @property
    def template(self):
        return "<" in self.name or self.name.endswith("*")

    def matches(self, concrete):
        """Whether a concrete check name is this one, or one this template stands for."""
        if not self.template:
            return concrete == self.name
        pattern = re.escape(self.name)
        pattern = re.sub(r"\\<[^>]*\\>|<[^>]*>", r".+", pattern).replace(r"\*", ".+")
        return re.fullmatch(pattern, concrete) is not None


@dataclass
class Case:
    id: str
    title: str
    checks: list = field(default_factory=list)
    harness: str = ""

    def for_form(self, form):
        return [c for c in self.checks if c.form in ("", form)]


def _qualifiers(expected):
    m = re.match(r"^\(([^)]*)\)", expected.strip())
    words = [w.strip().lower() for w in m.group(1).split(",")] if m else []
    form = "eks" if "eks" in words else "serverless" if "serverless" in words else ""
    branch = ("same-schema" if "same schema" in words else
              "newer-schema" if "schema moved forward" in words else "")
    negative = "negative" in words or "(negative)" in expected
    return form, branch, negative


def parse(text):
    """{case id: Case} from the document's text."""
    cases, current, table = {}, None, False
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            current = Case(m.group("id"), m.group("title"))
            cases[current.id] = current
            table = False
            continue
        if current is None:
            continue
        if line.startswith("**Harness**:"):
            current.harness = line[len("**Harness**:"):].strip()
            continue
        if line.startswith("| Check |"):
            table = True
            continue
        if table:
            row = ROW.match(line)
            if row:
                form, branch, negative = _qualifiers(row.group("expected"))
                for name in NAME.findall(row.group("checks")):
                    current.checks.append(Check(name, form, branch, negative, row.group("expected")))
            elif not line.startswith("| ---"):
                table = False
    return cases


def load(path=DOC):
    return parse(Path(path).read_text(encoding="utf-8"))


def assign(checks, names):
    """Each concrete name to the document's check it is: the check of that very name, else the one
    template it fills in. Returns ({check name: [names]}, [names no check takes], [names more than one
    template takes]): a template counts only the names no exact check took, so that a case cannot
    stand in for a template's names with another check's."""
    exact = {c.name: c for c in checks if not c.template}
    templates = [c for c in checks if c.template]
    taken = {c.name: [] for c in checks}
    stray, ambiguous = [], []
    for n in names:
        if n in exact:
            taken[n].append(n)
            continue
        matching = [t for t in templates if t.matches(n)]
        if len(matching) == 1:
            taken[matching[0].name].append(n)
        elif not matching:
            stray.append(n)
        else:
            ambiguous.append(n)
    return taken, stray, ambiguous


def to_be_written(case):
    """Whether the document says the case's harness, or part of it, is still to be written."""
    return "to be written" in case.harness
