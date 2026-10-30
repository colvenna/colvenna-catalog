"""UAT-CAT-01's judging, offline: the flows make every check of the case, and the verdicts."""

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cases import cat01
from uatlib import engines
from uatlib.install import REPO

sys.path.insert(0, str(REPO / "tests" / "compat"))
from compatlib import accesslog  # noqa: E402

FLOWS = Path(__file__).resolve().parent / "flows"
FLINK = FLOWS / "flink" / "src" / "main" / "java" / "com" / "colvenna" / "catalog" / "uat" / "flink"
SPARK = FLOWS / "spark" / "src" / "main" / "java" / "com" / "colvenna" / "catalog" / "uat" / "spark"


def flow_steps(path):
    """The check names a flow's source passes to step(...), in order."""
    return re.findall(r'step\(\s*"([a-z0-9-]+)"', path.read_text())


def line(path, status, action, principal="uat-flink-u1"):
    return (f"2026-10-29T05:00:00.000Z INFO  [v-1] colvenna.access - 9c8f8f5e-0000-4000-8000-000000000001 GET {path} "
            f"{status} 3ms principal={principal} action={action}")


class Flows(unittest.TestCase):
    def test_the_flink_flow_makes_every_flink_check(self):
        self.assertEqual(flow_steps(FLINK / "FlinkCatalogFlow.java"), list(cat01.FLINK_CHECKS))

    def test_the_spark_flow_makes_every_spark_check(self):
        self.assertEqual(flow_steps(SPARK / "SparkCatalogFlow.java"), list(cat01.SPARK_CHECKS))

    def test_the_bounded_streaming_write_is_at_parallelism_1(self):
        # colvenna-lake #297: Paimon 2.0.0 can drop a sink subtask's last data of a bounded input in
        # streaming mode at a parallelism above 1.
        text = (FLINK / "FlinkCatalogFlow.java").read_text()
        body = text[text.index("private String streamingAppend()"):text.index("private String consumerRead()")]
        self.assertIn("env.setParallelism(1);", body)
        self.assertIn("'number-of-rows'='3000'", body)
        self.assertIn("#297", text)


class Judged(unittest.TestCase):
    def test_steps(self):
        out = "noise\nSTEP batch-insert OK 120ms 100 rows\nSTEP view FAIL java.lang.AssertionError: x\n"
        self.assertEqual(cat01.judge_steps(["batch-insert", "view", "never"], out),
                         {"batch-insert": (True, "120ms 100 rows"), "view": (False, "java.lang.AssertionError: x")})
        self.assertEqual(engines.steps("STEP a OK\n"), {"a": (True, "")})

    def test_rename_and_drop_needs_the_files_gone(self):
        self.assertTrue(cat01.judge_rename_and_drop((True, "ok"), 0)[0])
        self.assertFalse(cat01.judge_rename_and_drop((True, "ok"), 2)[0])
        self.assertFalse(cat01.judge_rename_and_drop((True, "ok"), None)[0])
        self.assertFalse(cat01.judge_rename_and_drop((False, "moved"), 0)[0])

    def test_endpoints(self):
        served = accesslog.parse("\n".join([line("/v1/colvenna/databases/flow_u1/tables/orders", 200, "GET_TABLE"),
                                            line("/v1/colvenna/databases/flow_u1/tables/events/partitions", 501,
                                                 "LIST_PARTITIONS")]))
        self.assertTrue(cat01.judge_endpoints(served)[0])
        lacking = accesslog.parse(line("/v1/colvenna/no-such", 404, "-"))
        self.assertFalse(cat01.judge_endpoints(lacking)[0])
        declined = accesslog.parse(line("/v1/colvenna/databases/d/tables/t/x", 501, "SOMETHING"))
        self.assertFalse(cat01.judge_endpoints(declined)[0])
        self.assertFalse(cat01.judge_endpoints([])[0])
        self.assertTrue(datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
