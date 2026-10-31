"""The matrix page says what was declared and what happened, and never hides a failure."""

import json
import tempfile
import unittest
from pathlib import Path

from compatlib import config, page

OPS = config.OPERATIONS


def cell(cid, engine, version, paimon, statuses, declared=None):
    results = {}
    for t in config.TABLE_TYPES:
        results[t] = {}
        for o in OPS:
            d, reason = (declared or {}).get((t, o), ("supported", ""))
            v = {"status": statuses.get((t, o), "PASS"), "detail": f"{t} {o} detail", "declared": d}
            if reason:
                v["reason"] = reason
            results[t][o] = v
    failed = [1 for t in results for o in results[t]
              if results[t][o]["declared"] == "supported" and results[t][o]["status"] != "PASS"]
    return {"cell": cid, "engine": engine, "engineVersion": version, "paimon": paimon,
            "outcome": "fail" if failed else "pass", "results": results}


class Page(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "20261028T000000Z-ab12"
        (self.dir / "cells").mkdir(parents=True)
        meta = {"run": "20261028T000000Z-ab12", "form": "eks", "backend": "kubernetes", "region": "us-west-2",
                "started": "2026-10-28T08:00:00+00:00", "matrixCommit": "0123456789abcdef", "matrixTreeDirty": False,
                "cells": ["spark-3.5-paimon-2.0.0", "doris-4.1.5"],
                "cleanup": {"status": "PASS", "dropped": ["compat_x_fixture"]}}
        (self.dir / "run.json").write_text(json.dumps(meta))
        cells = [cell("spark-3.5-paimon-2.0.0", "Spark", "3.5.9", "2.0.0", {("append", "endpoints"): "FAIL"}),
                 cell("doris-4.1.5", "Doris", "4.1.5", "1.4.2 (bundled)", {("pk-dv", "read"): "FAIL"},
                      {("pk-dv", "read"): ("limitation", "Doris bundles Paimon 1.4.2")})]
        for c in cells:
            (self.dir / "cells" / f"{c['cell']}.json").write_text(json.dumps(c))
        self.text = page.render([self.dir], "runs", declared={})

    def test_a_row_for_every_cell_and_table_type(self):
        rows = [line for line in self.text.splitlines()
                if line.startswith("| Spark 3.5.9 | 2.0.0 | ") and ("primary key" in line or "append" in line)]
        self.assertEqual(len(rows), 3)

    def test_failures_are_listed(self):
        self.assertIn("Declared supported but not passed:", self.text)
        self.assertIn("Spark 3.5.9 with Paimon 2.0.0, append, endpoints: append endpoints detail", self.text)
        self.assertIn("| Spark 3.5.9 | 2.0.0 | 14 of 15 | 0 | **fail** |", self.text)

    def test_limitations_carry_their_reason_and_what_was_seen(self):
        self.assertIn("limitation [1]: **FAIL**", self.text)
        self.assertIn("1. Doris bundles Paimon 1.4.2.", self.text)
        self.assertIn("Doris 4.1.5, primary key, deletion vectors, read: **FAIL**: pk-dv read detail", self.text)
        self.assertIn("| Doris 4.1.5 | 1.4.2 (bundled) | 14 of 14 | 1 | pass |", self.text)

    def test_the_run_is_linked(self):
        self.assertIn("[20261028T000000Z-ab12](runs/20261028T000000Z-ab12/)", self.text)
        self.assertIn("region us-west-2", self.text)
        self.assertIn("## EKS form, engines as Kubernetes pods: run 20261028T000000Z-ab12", self.text)
        self.assertIn("Clean-up: no database of the run is left (compat_x_fixture).", self.text)

    def test_a_limitations_reason_is_the_current_declarations(self):
        declared = {("eks", "doris-4.1.5", "pk-dv", "read"): ("limitation", "it misses rows in level 0")}
        text = page.render([self.dir], "runs", declared=declared)
        self.assertIn("1. It misses rows in level 0.", text)
        self.assertNotIn("Doris bundles Paimon 1.4.2.", text)

    def test_a_declaration_changed_since_the_run_is_named_with_its_reason(self):
        declared = {("eks", "doris-4.1.5", "pk", "read"): ("limitation", "its JNI reader fails its first read"),
                    ("eks", "doris-4.1.5", "pk-dv", "read"): ("limitation", "Doris bundles Paimon 1.4.2"),
                    ("eks", "spark-3.5-paimon-2.0.0", "pk", "read"): ("supported", "")}
        text = page.render([self.dir], "runs", declared=declared)
        self.assertIn("**`declared.tsv`'s declarations have changed since this run.**", text)
        self.assertIn("- Doris 4.1.5, primary key, read: declared supported for the run, now a limitation: "
                      "its JNI reader fails its first read.", text)
        self.assertNotIn("Spark 3.5.9, primary key, read: declared", text)
        # The verdicts stay the run's, judged by its own declarations.
        self.assertIn("| Doris 4.1.5 | 1.4.2 (bundled) | 14 of 14 | 1 | pass |", text)

    def test_a_limitation_now_supported_is_named_too(self):
        declared = {("eks", "doris-4.1.5", "pk-dv", "read"): ("supported", "")}
        text = page.render([self.dir], "runs", declared=declared)
        self.assertIn("- Doris 4.1.5, primary key, deletion vectors, read: declared a limitation for the run, "
                      "now supported.", text)

    def test_unchanged_declarations_name_no_change(self):
        declared = {("eks", "doris-4.1.5", "pk-dv", "read"): ("limitation", "it misses rows in level 0")}
        text = page.render([self.dir], "runs", declared=declared)
        self.assertNotIn("have changed since this run", text)

    def test_a_run_judged_again_says_so(self):
        doris = json.loads((self.dir / "cells" / "doris-4.1.5.json").read_text())
        doris["rejudged"] = {"matrixCommit": "abcdef1234567890", "changed": ["pk-dv credentials: FAIL -> PASS"]}  # pragma: allowlist secret - a made-up commit id
        (self.dir / "cells" / "doris-4.1.5.json").write_text(json.dumps(doris))
        text = page.render([self.dir], "runs", declared={})
        self.assertIn("by `matrix.py rejudge` at `abcdef123456`: Doris 4.1.5 pk-dv credentials: FAIL -> PASS.", text)

    def test_a_stopped_run_says_so(self):
        meta = json.loads((self.dir / "run.json").read_text())
        meta.update(completed=False, cells=meta["cells"] + ["spark-4.1-paimon-2.1.0"],
                    cleanup={"status": "FAIL", "remaining": ["compat_x_spark41_p210"]})
        (self.dir / "run.json").write_text(json.dumps(meta))
        text = page.render([self.dir], "runs", declared={})
        self.assertIn("**This run was stopped before it ended**", text)
        self.assertIn("**No result** for spark-4.1-paimon-2.1.0", text)
        self.assertIn("Clean-up: **FAILED**: left compat_x_spark41_p210.", text)


if __name__ == "__main__":
    unittest.main()
