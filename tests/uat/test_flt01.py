"""UAT-FLT-01's judging, offline, with answers that pass and answers that do not."""

import unittest

from cases import flt01
from uatlib import faults

REPLAY = flt01.REPLAY


def event(user, ident, detail, status=200, outcome="SUCCEEDED", committed="true", time="2026-10-29T05:00:00Z"):
    return {"commitUser": user, "commitIdentifier": str(ident), "detail": detail, "status": status, "outcome": outcome,
            "committed": committed, "time": time}


def dropped(user="stress-w3", ident=10, first=200, again=200, body='{"success": true}', first_body='{"success": true}'):
    return {"commitUser": user, "commitIdentifier": ident, "first": first, "firstBody": first_body, "again": again,
            "againBody": body}


class Faults(unittest.TestCase):
    def test_every_fault_of_the_form_with_its_effect(self):
        eks = [(n, True, True) for n in "12345"]
        self.assertTrue(flt01.judge_faults_hit(eks, "eks", 12)[0])
        serverless = [(n, True, True) for n in "1235"]
        self.assertTrue(flt01.judge_faults_hit(serverless, "serverless", 3)[0])

    def test_a_fault_after_the_writers_or_without_its_effect_fails(self):
        self.assertFalse(flt01.judge_faults_hit([("1", True, True), ("2", False, True), ("3", True, True),
                                                 ("4", True, True), ("5", True, True)], "eks", 1)[0])
        self.assertFalse(flt01.judge_faults_hit([("1", True, True), ("2", True, False), ("3", True, True),
                                                 ("5", True, True)], "serverless", 0)[0])
        self.assertFalse(flt01.judge_faults_hit([(n, True, True) for n in "1235"], "eks", 1)[0])


class Commits(unittest.TestCase):
    def test_retried_and_stale(self):
        self.assertTrue(flt01.judge_retried([{"outcome": "FAILED_THEN_FOUND_COMMITTED"}])[0])
        self.assertFalse(flt01.judge_retried([])[0])
        self.assertTrue(flt01.judge_stale([event("u", 1, "base snapshot a is not the latest b", outcome="FAILED",
                                                 committed="false")])[0])
        self.assertFalse(flt01.judge_stale([event("u", 1, "published")])[0])

    def test_lock_timeouts_inside_the_window_applied_once(self):
        events = [event(None, None, "error", status=503, outcome="FAILED", time="2026-10-29T05:00:10Z")]
        once = (True, "")
        self.assertTrue(flt01.judge_lock(events, "2026-10-29T05:00:00Z", "2026-10-29T05:01:00Z", once)[0])
        self.assertFalse(flt01.judge_lock(events, "2026-10-29T05:00:20Z", "2026-10-29T05:01:00Z", once)[0])
        self.assertFalse(flt01.judge_lock(events, "2026-10-29T05:00:00Z", "2026-10-29T05:01:00Z", (False, ""))[0])

    def test_a_dropped_answer_published_once_and_replayed(self):
        events = [event("stress-w3", 10, "published"), event("stress-w3", 10, REPLAY)]
        self.assertTrue(flt01.judge_dropped([dropped()], events)[0])

    def test_a_dropped_answer_not_replayed_twice_published_or_none_fails(self):
        self.assertFalse(flt01.judge_dropped([dropped(body='{"success":false}')],
                                             [event("stress-w3", 10, "published")])[0])
        self.assertFalse(flt01.judge_dropped([dropped()], [event("stress-w3", 10, "published"),
                                                           event("stress-w3", 10, "published"),
                                                           event("stress-w3", 10, REPLAY)])[0])
        self.assertFalse(flt01.judge_dropped([dropped()], [event("stress-w3", 10, "published")])[0])
        self.assertFalse(flt01.judge_dropped([], [])[0])

    def test_a_refusal_dropped_is_refused_again_and_its_writer_publishes_it_once(self):
        refused = dropped(ident=11, first_body='{"success":false}', body='{"success":false}')
        events = [event("stress-w3", 10, "published"), event("stress-w3", 10, REPLAY), event("stress-w3", 11, "published")]
        self.assertTrue(flt01.judge_dropped([dropped(), refused], events)[0])
        errored = dropped(ident=11, first=503, first_body='{"code":503}', body='{"success":false}')
        self.assertTrue(flt01.judge_dropped([dropped(), errored], events)[0])
        applied = dropped(ident=11, first_body='{"success":false}', body='{"success":true}')
        self.assertFalse(flt01.judge_dropped([dropped(), applied], events)[0])
        conflict = dropped(ident=11, first=409, first_body='{"code":409}', body='{"success":true}')
        self.assertFalse(flt01.judge_dropped([dropped(), conflict], events)[0])
        # Refusals alone show nothing of a replay.
        self.assertFalse(flt01.judge_dropped([refused], events)[0])

    def test_a_retryable_refusal_dropped_may_be_applied_when_sent_again_once(self):
        # #39: the first answer 503 (a lock timeout, its changes rolled back); the resend, the lock gone, applied.
        locked = dropped(ident=11, first=503, first_body='{"code":503}', body='{"success":true}')
        once = [event("stress-w3", 10, "published"), event("stress-w3", 10, REPLAY), event("stress-w3", 11, "published")]
        self.assertTrue(flt01.judge_dropped([dropped(), locked], once)[0])
        self.assertFalse(flt01.judge_dropped([locked], once[2:])[0], "no dropped commit, no replay: nothing shown")
        # A commit in doubt (its session lost): answered 503, rolled forward by the resend, a replay.
        in_doubt = once[:2] + [event("stress-w3", 11, REPLAY)]
        self.assertTrue(flt01.judge_dropped([dropped(), locked], in_doubt)[0])
        twice = once + [event("stress-w3", 11, "published")]
        self.assertFalse(flt01.judge_dropped([dropped(), locked], twice)[0])
        self.assertFalse(flt01.judge_dropped([dropped(), locked], once[:2])[0], "success, but neither published nor replayed")
        throttled = dropped(ident=11, first=429, first_body='{"message":"x"}', body='{"success":true}')
        self.assertTrue(flt01.judge_dropped([dropped(), throttled], once)[0])

    def test_intents(self):
        self.assertTrue(flt01.judge_intents([("PUBLISHED", "1"), ("PUBLISHED", "2"), ("ABORTED", "2")], 2)[0])
        self.assertFalse(flt01.judge_intents([("PUBLISHED", "1"), ("PUBLISHED", "3")], 3)[0])
        self.assertFalse(flt01.judge_intents([("PUBLISHED", "1"), ("PUBLISHED", "1")], 1)[0])
        self.assertFalse(flt01.judge_intents([("PUBLISHED", "1"), ("PENDING", "2")], 1)[0])
        self.assertFalse(flt01.judge_intents([], None)[0])


class Afterwards(unittest.TestCase):
    def test_maintenance_after_the_faults(self):
        rounds = [{"ok": False, "began": "2026-10-29T05:00:00Z", "error": "x"},
                  {"ok": True, "began": "2026-10-29T05:10:00Z"}]
        self.assertTrue(flt01.judge_maintenance(rounds, "2026-10-29T05:05:00Z")[0])
        self.assertFalse(flt01.judge_maintenance(rounds, "2026-10-29T04:00:00Z")[0])
        self.assertFalse(flt01.judge_maintenance(rounds, "2026-10-29T06:00:00Z")[0])

    def test_consistency_and_failures_as_success(self):
        self.assertTrue(faults.judge_consistent({"consistent": True, "problems": []})[0])
        self.assertFalse(faults.judge_consistent({"consistent": False, "problems": ["p"]})[0])
        self.assertTrue(flt01.judge_no_failure_as_success([])[0])
        self.assertFalse(flt01.judge_no_failure_as_success([["COMMIT", "409", "1"]])[0])


if __name__ == "__main__":
    unittest.main()
