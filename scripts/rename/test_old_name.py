"""Offline tests of old_name.py, the check that the project's old name stays gone (#31)."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import old_name  # noqa: E402


class HitsTest(unittest.TestCase):
    def test_the_name_and_its_identifiers_are_found(self):
        for text in ("Cove Catalog", "cove-catalog-uat/cove-catalog", "CoveCatalogUatEks", "covelake",
                     "COVE_ENV=uat", "package com.covelake.catalog;", "GET /cove/admin/v1/grants", "cove_catalog",
                     "cove-lake", "产品原名Cove Catalog"):
            self.assertTrue(old_name.hits(text), text)

    def test_an_identifier_that_a_capital_starts_inside_another_is_found(self):
        # Names a capital starts inside another: a test class, Colvenna Lake's former names, a string escape.
        for text in ("PaimonCoveCatalogContractTest", "CoveTaskFailed", "coveResnapshot", "isCoveTable",
                     r"\nCoveCatalog"):
            self.assertTrue(old_name.hits(text), text)

    def test_words_that_only_hold_or_start_like_it_are_not(self):
        for text in ("recover the table", "discover", "cover", "Cover letter", "COVERAGE", "coverage report",
                     "covenant", "Coventry", "covet", "covey", "colvenna-catalog", "colvenna-lake", "Recover",
                     "COLVENNA_ENV", "com.colvenna.catalog", "tests/deploy/test_helm_upgrade.py: the Helm recovery"):
            self.assertEqual(old_name.hits(text), [], text)

    def test_a_hexadecimal_literal_that_spells_it_is_found(self):
        # "covecatL" in ASCII, as Java writes a long; and the same bytes the other way round.
        self.assertEqual(old_name.hits("long KEY = 0x636f76656361744cL;"), ["0x636f76656361744c"])
        self.assertEqual(old_name.hits("0x4c74616365766f63"), ["0x4c74616365766f63"])

    def test_other_hexadecimal_literals_are_not(self):
        for text in ("0x636f6c766361744cL", "0xdeadbeef", "0x636f766", "sha256 0x0123456789abcdef"):
            self.assertEqual(old_name.hits(text), [], text)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_a_line_with_the_name_fails(self):
        path = self.write("docs/development.md", "fine\nset COVE_ENV first\n")
        self.assertEqual(old_name.check(path, [], self.root), ["docs/development.md:2: COVE: set COVE_ENV first"])

    def test_an_exception_allows_its_line_only(self):
        path = self.write("README.md", "The product was called Cove Catalog until 2026-10-25.\nRun cove-catalog now.\n")
        rules = [("README.md", "The product was called Cove Catalog", "naming history")]
        problems = old_name.check(path, rules, self.root)
        self.assertEqual([p.split(": ")[0] for p in problems], ["README.md:2"])

    def test_a_glob_with_any_line_allows_the_whole_file(self):
        path = self.write("docs/history/report.md", "cove-catalog\nCoveCatalogUatEks\n")
        self.assertEqual(old_name.check(path, [("docs/history/*", "*", "history")], self.root), [])
        other = self.write("docs/design/x.md", "cove-catalog\n")
        self.assertEqual(len(old_name.check(other, [("docs/history/*", "*", "history")], self.root)), 1)

    def test_the_name_in_a_path_fails(self):
        path = self.write("deploy/helm/cove-catalog/Chart.yaml", "name: x\n")
        self.assertEqual(old_name.check(path, [], self.root),
                         ["deploy/helm/cove-catalog/Chart.yaml: the old name in the path"])

    def test_a_binary_file_is_judged_by_its_path_alone(self):
        full = os.path.join(self.root, "assets/logo.png")
        os.makedirs(os.path.dirname(full))
        with open(full, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\xff\xfecove")
        self.assertEqual(old_name.check("assets/logo.png", [], self.root), [])

    def test_a_malformed_exception_stops_the_check(self):
        path = self.write("exceptions.tsv", "README.md\tonly two fields\n")
        with self.assertRaises(SystemExit) as stop:
            old_name.load_exceptions(os.path.join(self.root, path))
        self.assertIn("want <path glob> TAB <text> TAB <reason>", str(stop.exception))


class RepositoryTest(unittest.TestCase):
    def test_the_repository_holds_the_old_name_only_where_an_exception_allows_it(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = old_name.main(["old_name.py", "--all"])
        self.assertEqual(status, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
