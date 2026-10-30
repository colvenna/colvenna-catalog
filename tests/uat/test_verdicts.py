"""A case's result, from its verdicts, as the cases document's section 3.3 says."""

import tempfile
import unittest
from pathlib import Path

from uatlib import verdicts
from uatlib.verdicts import Blocked, Verdicts, judge, read


class Written(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "verdicts.tsv"

    def test_one_line_per_check_with_time_status_and_detail(self):
        v = Verdicts(self.path)
        v.check("get-table/owner", True, "200 {}")
        v.check("get-table/denied", False, "expected 403:\tgot\n200")
        v.close()
        lines = read(self.path)
        self.assertEqual([(c, s) for _, c, s, _ in lines], [("get-table/owner", "PASS"), ("get-table/denied", "FAIL")])
        self.assertEqual(lines[1][3], "expected 403: got 200")
        self.assertRegex(lines[0][0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(self.path.read_text().splitlines()[0], "time\tcheck\tstatus\tdetail")

    def test_details_are_sanitized(self):
        class Upper:
            def text(self, s):
                return s.replace("secret", "<REDACTED>")

        v = Verdicts(self.path, Upper())
        v.check("x", True, "a secret here")
        v.close()
        self.assertEqual(read(self.path)[0][3], "a <REDACTED> here")

    def test_a_failed_precondition_blocks(self):
        v = Verdicts(self.path)
        with self.assertRaises(Blocked):
            v.precondition("tables", False, "no database")
        v.close()
        self.assertEqual(judge(read(self.path), ["a"], 1)[0], "BLOCKED")

    def test_a_check_cannot_pass_for_a_precondition(self):
        v = Verdicts(self.path)
        with self.assertRaises(ValueError):
            v.check("precondition:x", True)
        v.close()


class Judged(unittest.TestCase):
    def lines(self, *pairs):
        return [("t", c, s, "") for c, s in pairs]

    def test_pass_needs_every_check_present_and_passing_and_exit_0(self):
        self.assertEqual(judge(self.lines(("a", "PASS"), ("b", "PASS")), ["a", "b"], 0), ("PASS", []))

    def test_a_missing_check_fails(self):
        result, reasons = judge(self.lines(("a", "PASS")), ["a", "b"], 0)
        self.assertEqual(result, "FAIL")
        self.assertEqual(reasons, ["b: missing"])

    def test_an_only_on_failure_check_may_be_absent(self):
        self.assertEqual(judge(self.lines(("a", "PASS")), ["a", "b"], 0, optional=("b",))[0], "PASS")

    def test_a_failed_check_fails_even_if_it_passed_once(self):
        self.assertEqual(judge(self.lines(("a", "PASS"), ("a", "FAIL")), ["a"], 0)[0], "FAIL")
        self.assertEqual(judge(self.lines(("a", "FAIL"), ("a", "PASS")), ["a"], 0)[0], "FAIL")

    def test_a_nonzero_exit_fails(self):
        result, reasons = judge(self.lines(("a", "PASS")), ["a"], 1)
        self.assertEqual(result, "FAIL")
        self.assertIn("the run exited 1", reasons)
        self.assertIn("the run did not end", judge(self.lines(("a", "PASS")), ["a"], None)[1])

    def test_a_check_the_case_does_not_list_fails(self):
        self.assertEqual(judge(self.lines(("a", "PASS"), ("z", "PASS")), ["a"], 0),
                         ("FAIL", ["z: not a check of this case"]))

    def test_a_case_that_did_not_start_is_not_run(self):
        self.assertEqual(judge([], ["a"], None, ran=False)[0], "NOT_RUN")

    def test_a_case_that_stopped_before_any_check_fails(self):
        result, reasons = judge([], ["a"], 1)
        self.assertEqual(result, "FAIL")
        self.assertIn("a: missing", reasons)

    def test_blocked_wins_over_failed_checks(self):
        lines = self.lines(("a", "FAIL"), (verdicts.PRECONDITION + "db", "FAIL"))
        self.assertEqual(judge(lines, ["a"], 1)[0], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
