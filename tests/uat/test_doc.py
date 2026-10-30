"""The harness and the cases document agree: every case the harness runs writes exactly the
document's checks for each form it runs in, and the document's cases parse."""

import argparse
import re
import unittest

from cases import CASES
from uatlib import doc


class Document(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = doc.load()
        text = doc.DOC.read_text()
        start, end = text.index("<!-- required-cases:start -->"), text.index("<!-- required-cases:end -->")
        cls.required = re.findall(r"^\| (UAT-[A-Z]{3}-\d{2}) \|", text[start:end], re.M)

    def test_every_required_case_has_checks_or_shares_the_matrix_ones(self):
        self.assertEqual(len(self.required), 34)
        for case_id in self.required:
            self.assertIn(case_id, self.cases)
            if not case_id.startswith("UAT-CMP-"):
                self.assertTrue(self.cases[case_id].checks, case_id)

    def test_forms_and_negatives_are_read(self):
        cat07 = {c.name: c for c in self.cases["UAT-CAT-07"].checks}
        self.assertEqual(cat07["eks-replicas"].form, "eks")
        self.assertEqual(cat07["serverless-no-secret-in-functions"].form, "serverless")
        self.assertTrue(cat07["serverless-no-secret-in-functions"].negative)
        self.assertEqual(cat07["schema-version"].form, "")
        dep07 = {c.name: c for c in self.cases["UAT-DEP-07"].checks}
        self.assertEqual(dep07["same-schema-rolled-back"].branch, "same-schema")
        self.assertEqual(dep07["newer-schema-refused"].branch, "newer-schema")
        self.assertEqual(dep07["forward-again"].branch, "")

    def test_templates_match_their_names(self):
        sec01 = {c.name: c for c in self.cases["UAT-SEC-01"].checks}
        t = sec01["<entry>-<route>-<credential>"]
        self.assertTrue(t.matches("service-config-none"))
        self.assertFalse(t.matches("service-config"))
        args = {c.name: c for c in self.cases["UAT-DEP-05"].checks}["args-*"]
        self.assertTrue(args.matches("args-no-config"))
        self.assertFalse(args.matches("config-unknown-key"))


class Harness(unittest.TestCase):
    def test_each_case_writes_exactly_the_documents_checks(self):
        cases = doc.load()
        for case_id, module in CASES.items():
            self.assertIn(case_id, cases)
            for form in module.FORMS:
                mine = module.checks(form, argparse.Namespace(config=None))
                self.assertEqual(len(mine), len(set(mine)), f"{case_id}: a check named twice")
                taken, stray, ambiguous = doc.assign(cases[case_id].for_form(form), mine)
                for check, names in taken.items():
                    self.assertTrue(names, f"{case_id} ({form}): {check} is not checked")
                self.assertEqual(stray, [], f"{case_id} ({form}): not the document's")
                self.assertEqual(ambiguous, [], f"{case_id} ({form}): more than one template")

    def test_a_template_is_not_filled_in_by_another_check(self):
        sec01 = doc.load()["UAT-SEC-01"].for_form("eks")
        taken, _, _ = doc.assign(sec01, ["eks-plain-http", "admin-control"])
        self.assertEqual(taken["<entry>-<route>-<credential>"], [])

    def test_every_case_but_the_matrixs_has_its_harness_here(self):
        # #52's acceptance: no case is left "to be written"; the matrix's cells are tests/compat's.
        for case_id, case in doc.load().items():
            self.assertFalse(doc.to_be_written(case), case_id)
            if not case_id.startswith("UAT-CMP-"):
                self.assertIn(case_id, CASES)

    def test_the_document_names_the_harness_of_every_case_it_runs(self):
        cases = doc.load()
        for case_id in CASES:
            self.assertIn("tests/uat/uat.py run " + case_id, cases[case_id].harness, case_id)


if __name__ == "__main__":
    unittest.main()
