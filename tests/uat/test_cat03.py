"""UAT-CAT-03's judging, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cases import cat03

FLINK = Path(__file__).resolve().parent / "flows" / "flink" / "src" / "main" / "java" / "com" / "colvenna" / "catalog" \
    / "uat" / "flink"


def read(table, check, round_, output, rc=0):
    return "COMPAT-READ " + json.dumps({"table": table, "check": check, "round": round_, "rc": rc, "output": output})


def event(minutes, cached=False, expires_after=90, resource="renew_u1.renewal"):
    t = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return {"time": t.isoformat().replace("+00:00", "Z"), "outcome": "SUCCEEDED", "resource": resource,
            "detail": {"cached": cached, "expiresAt": (t + timedelta(minutes=expires_after)).isoformat().replace("+00:00", "Z")}}


class Flow(unittest.TestCase):
    def test_the_renewal_flow_writes_a_bounded_input_at_parallelism_1_without_restart(self):
        text = (FLINK / "FlinkRenewalFlow.java").read_text()
        self.assertIn("env.setParallelism(1);", text)  # colvenna-lake #297
        self.assertIn("RestartStrategies.noRestart()", text)
        self.assertIn("enableCheckpointing(30_000)", text)
        self.assertIn("'number-of-rows'", text)
        self.assertIn("#297", text)


class Judged(unittest.TestCase):
    def test_doris_gives_the_first_rounds_answers(self):
        out = "\n".join([read("pk", "count", 1, "115"), read("pk", "count", 2, "115"),
                         read("append", "sum", 1, "7"), read("append", "sum", 2, "7")])
        self.assertTrue(cat03.judge_doris(cat03.doris_rounds(out))[0])

    def test_doris_differing_failing_or_missing(self):
        for out in ("\n".join([read("pk", "count", 1, "115"), read("pk", "count", 2, "110")]),
                    "\n".join([read("pk", "count", 1, "115"), read("pk", "count", 2, "", rc=1)]),
                    read("pk", "count", 1, "115"), ""):
            self.assertFalse(cat03.judge_doris(cat03.doris_rounds(out))[0], out)

    def test_a_fresh_issuance_before_the_earlier_one_expired_is_a_renewal(self):
        self.assertTrue(cat03.judge_renewal([event(0), event(31)], "renew_u1.renewal")[0])

    def test_no_renewal(self):
        for events in ([event(0)], [event(0), event(10)], [event(0), event(95)], [event(0), event(31, cached=True)],
                       [event(0), event(31, resource="renew_u1.other")]):
            self.assertFalse(cat03.judge_renewal(events, "renew_u1.renewal")[0], events)

    def test_flink_spark_and_duration(self):
        self.assertTrue(cat03.judge_flink((True, "FINISHED"), [])[0])
        self.assertFalse(cat03.judge_flink((True, "FINISHED"), ["===== tm (restarts 1)"])[0])
        self.assertFalse(cat03.judge_flink(None, [])[0])
        self.assertFalse(cat03.judge_spark(None)[0])
        self.assertTrue(cat03.judge_spark((True, "x"))[0])
        self.assertTrue(cat03.judge_duration(0, 179.5)[0])
        self.assertFalse(cat03.judge_duration(0, 90)[0])
        self.assertFalse(cat03.judge_duration(1, 180)[0])


if __name__ == "__main__":
    unittest.main()
