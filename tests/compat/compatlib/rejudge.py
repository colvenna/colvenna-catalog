"""Verdicts again, from a run's kept raw output, by the judge as it stands: for a rule that
changed after the run. Nothing runs again: the inputs are the run's own (each container's output,
the access log lines the run judged, its fixture's writes), and the cell files record that, and
by which commit, they were judged again."""

import json
import re
from pathlib import Path

from . import accesslog, config, judge
from .runner import git_head

MAIN = {"doris": "fe", "starrocks": "starrocks"}


def read(path):
    path = Path(path)
    return path.read_text() if path.exists() else ""


def rejudge(run_dir, declared, cells_config, sanitizer=None):
    """Writes each cell's verdicts again; returns {cell: (before, after)} of what changed."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text())
    prefix, form = meta["databasePrefix"], meta["form"]
    fixture_text = read(run_dir / "raw" / "fixture" / "flow.log")
    fixture = judge.lines(fixture_text, "COMPAT-FIXTURE")
    planned = {t["table"]: [c["name"] for c in t["checks"]] for t in fixture}
    fixture_writes = {t: v[1] for (t, op), v in judge.flow_results(fixture_text).items() if op == "write"}
    commit, dirty = git_head()
    changed, outcomes = {}, {}
    for path in sorted((run_dir / "cells").glob("*.json")):
        body = json.loads(path.read_text())
        cell = config.cell_by_id(cells_config, body["cell"])
        raw = run_dir / "raw" / cell["id"]
        problem = json.loads(read(raw / "pod.json") or "{}").get("problem", "")
        access = raw / "catalog-access.log"
        requests = accesslog.parse(access.read_text()) if access.exists() else None
        if cell["kind"] in config.WRITERS:
            verdicts = judge.writer(read(raw / "flow.log"), problem, requests, body["database"], prefix)
        else:
            probe = {t: v for (t, op), v in judge.flow_results(read(raw / "probe.log")).items() if op == "credentials"}
            stated = re.search(r"not the catalog's \((.+?)\); the catalog issued",
                               body["results"]["pk"]["credentials"]["detail"])
            verdicts = judge.reader(cell["kind"], read(raw / f"{MAIN[cell['kind']]}.log"), problem, requests,
                                    body["database"], prefix, probe, stated.group(1) if stated else "", planned)
            judge.what_the_tables_hold(verdicts, fixture_writes)
        failed = judge.declare(verdicts, declared, form, cell["id"])
        # Against the run's own verdicts, however often the cell has been judged again.
        original = body.get("rejudged", {}).get("original") or {
            f"{t} {o}": body["results"][t][o]["status"] for t in config.TABLE_TYPES for o in config.OPERATIONS}
        before = {tuple(k.split(" ")): v for k, v in original.items()}
        after = {(t, o): verdicts[t][o]["status"] for t in config.TABLE_TYPES for o in config.OPERATIONS}
        diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if diff:
            changed[cell["id"]] = diff
        body.update(results=verdicts, outcome="pass" if not failed else "fail", failedSupported=failed,
                    rejudged={"matrixCommit": commit, "matrixTreeDirty": dirty, "original": original,
                              "changed": [f"{t} {o}: {b} -> {a}" for (t, o), (b, a) in sorted(diff.items())]})
        if sanitizer:
            body = sanitizer.data(body)
        path.write_text(json.dumps(body, indent=2) + "\n")
        outcomes[cell["id"]] = body["outcome"]
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["outcomes"] = {c: outcomes.get(c, o) for c, o in summary["outcomes"].items()}
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return changed
