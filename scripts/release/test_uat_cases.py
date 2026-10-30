"""Offline checks of the UAT cases document (docs/uat/cases.md, #19) that a candidate's manifest pins:
its required case set parses as manifest.py reads it and names only cases the document defines; each
case is defined once, with the fields design 9.1 asks of every case; and the compatibility matrix's
cases name each cell of tests/compat/cells.json exactly once, when the matrix is there."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import manifest  # noqa: E402

CASES = os.path.join(REPO, manifest.CASES_PATH)
CELLS = os.path.join(REPO, "tests", "compat", "cells.json")
HEADING = re.compile(r"^#### (" + manifest.CASE.pattern + r") \S", re.MULTILINE)
# What design 9.1 asks of every case: requirement, configuration, preconditions, steps, input,
# expected result and clean-up; and what runs it.
FIELDS = ("Requirement", "Configuration", "Preconditions", "Input", "Steps", "Expected", "Cleanup", "Harness")
# The matrix's cases share one definition of these fields and name their cell.
SHARED = "#### What UAT-CMP-01 to 08 share"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


NEXT = re.compile(r"^#{2,4} ", re.MULTILINE)


def sections(text):
    """Each case's ID and its text, up to the next heading of its level or above, in order."""
    out = []
    for m in HEADING.finditer(text):
        rest = NEXT.search(text, m.end())
        out.append((m.group(1), text[m.start():rest.start() if rest else len(text)]))
    return out


class UatCases(unittest.TestCase):
    def setUp(self):
        self.text = read(CASES)
        self.cases = sections(self.text)
        self.ids = [case for case, _ in self.cases]

    def test_each_case_is_defined_once(self):
        self.assertTrue(self.ids)
        self.assertEqual(len(self.ids), len(set(self.ids)), [c for c in self.ids if self.ids.count(c) > 1])

    def test_the_required_cases_parse_and_are_defined(self):
        required = manifest.required_cases(self.text)
        self.assertTrue(required)
        self.assertEqual([c for c in required if c not in self.ids], [])

    def test_every_case_has_its_fields(self):
        shared = self.text[self.text.index(SHARED):]
        shared = shared[:re.compile(r"^#### UAT-", re.MULTILINE).search(shared).start()]
        for field in FIELDS:
            self.assertIn(f"**{field}**", shared, f"the matrix's shared definition lacks {field}")
        for case, body in self.cases:
            if case.startswith("UAT-CMP-"):
                self.assertRegex(body, r"Cell `[a-z0-9.-]+`", f"{case} names no cell")
                continue
            missing = [field for field in FIELDS if f"**{field}**" not in body]
            self.assertEqual(missing, [], f"{case} lacks {missing}")

    @unittest.skipUnless(os.path.exists(CELLS), "the compatibility matrix (#10) is not in this tree")
    def test_the_matrix_cases_name_every_cell_once(self):
        cells = [cell["id"] for cell in json.loads(read(CELLS))["cells"]]
        named = [re.search(r"Cell `([a-z0-9.-]+)`", body).group(1)
                 for case, body in self.cases if case.startswith("UAT-CMP-")]
        self.assertEqual(sorted(named), sorted(cells))
        self.assertEqual(len(named), len(set(named)))


if __name__ == "__main__":
    unittest.main()
