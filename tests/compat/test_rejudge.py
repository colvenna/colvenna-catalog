"""Judging a run again from its kept raw output: the same verdicts where the rules did not change."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from compatlib import config
from compatlib.rejudge import rejudge

RUNS = config.HERE.parent.parent / "docs" / "compat" / "runs"


class Rejudge(unittest.TestCase):
    def test_a_committed_run_judged_again(self):
        source = RUNS / "20261028T120119Z-c93a"
        if not source.exists():
            self.skipTest("no committed run")
        run = Path(tempfile.mkdtemp()) / source.name
        shutil.copytree(source, run)
        self.addCleanup(shutil.rmtree, run.parent)
        cells = config.load_cells()
        before = {p.name: json.loads(p.read_text()) for p in (run / "cells").glob("*.json")}
        rejudge(run, config.load_declarations(cells), cells)
        after = {p.name: json.loads(p.read_text()) for p in (run / "cells").glob("*.json")}
        for name, cell in before.items():
            original = cell.get("rejudged", {}).get("original") or {
                f"{t} {o}": cell["results"][t][o]["status"] for t in cell["results"] for o in cell["results"][t]}
            self.assertEqual(after[name]["rejudged"]["original"], original)
            if config.cell_by_id(cells, cell["cell"])["kind"] in config.WRITERS:
                # The writers' rules did not change after the run: every verdict is the run's.
                self.assertEqual(after[name]["results"], cell["results"], name)
                self.assertEqual(after[name]["rejudged"]["changed"], [])


if __name__ == "__main__":
    unittest.main()
