"""UAT-FLT-03's judging, offline."""

import unittest

from cases import flt03


class Fence(unittest.TestCase):
    def test_the_writers_commits_are_among_the_tables_events(self):
        events = [{"action": "COMMIT", "principal": "w", "resource": "d.t", "httpStatus": 409},
                  {"action": "FENCE_TABLE", "principal": "w", "resource": "d.t"},
                  {"action": "COMMIT", "principal": "other", "resource": "d.t", "httpStatus": 200}]
        self.assertEqual(flt03.writer_commits(events, "w"), events[:1])

    def test_fenced(self):
        fence = {"action": "FENCE_TABLE", "httpStatus": 409}
        refused = {"failed": True, "retries": 2,
                   "message": "Commit failed after 9223372036854775807 millis with 2 retries, there maybe exist commit "
                              "conflicts between multiple jobs.",
                   "error": "AlreadyExistsException: Table d.t is fenced and accepts no commits: Snapshot 3 ..."}
        three = [{"action": "COMMIT", "httpStatus": 409}] * 3
        self.assertTrue(flt03.judge_fenced(3, refused, three, [fence])[0])
        # The client gave up before its retries, or made more requests than it had tries.
        self.assertFalse(flt03.judge_fenced(3, refused, three[:1], [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, refused, three + three[:1], [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, dict(refused, message="Exception occurs when committing"), three, [fence])[0])
        # A commit accepted, another refusal, no fence, no outside snapshot, no failure.
        self.assertFalse(flt03.judge_fenced(3, refused, three[:2] + [{"action": "COMMIT", "httpStatus": 200}], [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, refused, three[:2] + [{"action": "COMMIT", "httpStatus": 500}], [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, dict(refused, error="AlreadyExistsException: Table d.t already exists"),
                                            three, [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, refused, three, [])[0])
        self.assertFalse(flt03.judge_fenced(None, refused, three, [fence])[0])
        self.assertFalse(flt03.judge_fenced(3, {"failed": False}, [], [fence])[0])

    def test_reads_hold(self):
        self.assertTrue(flt03.judge_reads({"snapshot": 2, "rows": 20})[0])
        self.assertFalse(flt03.judge_reads({"snapshot": 3, "rows": 25})[0])

    def test_reconcile_releases(self):
        body = {"stateBefore": "FENCED", "branches": [{"branch": "main", "before": 2, "after": 3}]}
        self.assertTrue(flt03.judge_reconcile(200, body, 2, 3)[0])
        self.assertFalse(flt03.judge_reconcile(200, dict(body, stateBefore="ACTIVE"), 2, 3)[0])
        self.assertFalse(flt03.judge_reconcile(500, body, 2, 3)[0])
        self.assertFalse(flt03.judge_reconcile(200, dict(body, branches=[{"branch": "main", "before": 2, "after": 2}]), 2, 3)[0])

    def test_commits_again(self):
        self.assertTrue(flt03.judge_commits_again(0, [4], {"consistent": True})[0])
        self.assertFalse(flt03.judge_commits_again(1, [], {"consistent": True})[0])
        self.assertFalse(flt03.judge_commits_again(0, [4], {"consistent": False})[0])


if __name__ == "__main__":
    unittest.main()
