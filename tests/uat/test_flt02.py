"""UAT-FLT-02's judging, offline."""

import unittest

from cases import flt02
from uatlib import faults


def listing(*nodes):
    return [(f"p{i}", n, n is not None, "Running" if n else "Pending") for i, n in enumerate(nodes)]


class Replicas(unittest.TestCase):
    def test_apart(self):
        self.assertTrue(flt02.apart(listing("n1", "n2")))
        self.assertFalse(flt02.apart(listing("n1", "n1")))
        self.assertTrue(flt02.apart(listing("n1", None)))

    def test_never_unavailable_while_draining(self):
        samples = [(2, listing("n1", "n2")), (1, listing("n1", None)), (1, listing("n1", None))]
        self.assertTrue(flt02.judge_never_unavailable(0, samples)[0])
        self.assertFalse(flt02.judge_never_unavailable(0, samples + [(0, listing(None, None))])[0])
        self.assertFalse(flt02.judge_never_unavailable(0, samples + [(2, listing("n1", "n1"))])[0])
        self.assertFalse(flt02.judge_never_unavailable(1, samples)[0])

    def test_the_evicted_replica_waits_or_runs_elsewhere(self):
        waiting = [(1, listing("n1", None))]
        self.assertTrue(flt02.judge_evicted_placed(waiting, "n2", "n1")[0])
        elsewhere = [(2, listing("n1", "n3"))]
        self.assertTrue(flt02.judge_evicted_placed(elsewhere, "n2", "n1")[0])
        beside = [(2, listing("n1", "n1"))]
        self.assertFalse(flt02.judge_evicted_placed(beside, "n2", "n1")[0])
        self.assertFalse(flt02.judge_evicted_placed([(2, listing("n1", "n2"))], "n2", "n1")[0])

    def test_over_concurrency_needs_refusals_each_retryable(self):
        self.assertTrue(flt02.judge_over_concurrency(12, 0, 0, 300, {"503": 3})[0])
        self.assertIn("503", flt02.judge_over_concurrency(12, 0, 0, 300, {"503": 3})[1])
        # Refused by the gateway alone: its 429s.
        self.assertTrue(flt02.judge_over_concurrency(0, 7, 0, 300, {"429": 2})[0])
        self.assertTrue(flt02.judge_over_concurrency(5, 2, 0, 300, {})[0])
        self.assertFalse(flt02.judge_over_concurrency(0, 0, 0, 300, {})[0])
        # #80: a throttled request answered 500, which the Paimon client does not retry.
        found = flt02.judge_over_concurrency(45, 0, 0, 300, {"500": 6})
        self.assertFalse(found[0])
        self.assertIn("not retryable: {'500': 6}", found[1])
        self.assertFalse(flt02.judge_over_concurrency(45, 0, 0, 300, {"503": 4, "timeout": 1})[0])
        # A function's failure is a 503 too: none may be among them.
        self.assertFalse(flt02.judge_over_concurrency(45, 0, 2, 300, {"503": 4})[0])
        self.assertEqual(flt02.statuses([{"status": 200}, {"status": 429}, {"status": 429}, {"error": "timeout"}]),
                         {"429": 2, "timeout": 1})

    def test_consistent(self):
        self.assertTrue(faults.judge_consistent({"consistent": True, "problems": []})[0])
        self.assertFalse(faults.judge_consistent({"consistent": False, "problems": ["p"]})[0])
        found = faults.judge_consistent({"status": 504, "error": "Endpoint request timed out"})
        self.assertFalse(found[0])
        self.assertIn("504", found[1])


class Snapshot(unittest.TestCase):
    def test_the_id_in_a_load_snapshot_answer(self):
        self.assertEqual(faults.snapshot_id({"snapshot": {"recordCount": 5, "snapshot": {"id": 7}}}), 7)
        self.assertIsNone(faults.snapshot_id({}))


class Writers(unittest.TestCase):
    def test_every_pod_must_pass_each_check(self):
        results = {"stress-a": ({"every-commit-once": (True, "a")}, []),
                   "stress-b": ({"every-commit-once": (True, "b")}, [{"outcome": "FAILED_THEN_RETRIED"}])}
        self.assertTrue(faults.judge_stress(results, "every-commit-once")[0])
        self.assertEqual(len(faults.retried(results)), 1)
        results["stress-b"] = ({"every-commit-once": (False, "b")}, [])
        self.assertFalse(faults.judge_stress(results, "every-commit-once")[0])
        self.assertFalse(faults.judge_stress({"stress-a": ({}, [])}, "every-commit-once")[0])


if __name__ == "__main__":
    unittest.main()
