"""A run's result (result.json) and its summary as the performance document's tables (summary.md,
docs/performance/template.md's section 5): per form, each metric's samples, p50, p95, p99, max,
target and verdict."""

import json

from . import stats, terms
from .context import iso, now


def result(scenario, run, sc, environment, cleanup, error):
    """The run's result. A scenario that is never judged (terms.OBSERVED_SCENARIOS: P1c) is OBSERVED when it
    completed and cleaned up, FAIL when not; any other needs a judged metric to pass (stats.verdict)."""
    observed_only = scenario in terms.OBSERVED_SCENARIOS
    forms = {}
    for form, metrics in sc.results.items():
        forms[form] = {"verdict": stats.OBSERVED if observed_only else stats.verdict(metrics), "metrics": metrics,
                       "load": sc.load.get(form)}
    reasons = []
    if error:
        verdict = stats.FAIL
        reasons.append(f"the run did not complete: {error.splitlines()[-1][:300]}")
    elif not forms:
        verdict = stats.FAIL
        reasons.append("no result")
    elif observed_only:
        verdict = stats.OBSERVED
    else:
        verdict = stats.verdict([{"verdict": f["verdict"]} for f in forms.values()])
        for form, f in forms.items():
            for m in f["metrics"]:
                if m["verdict"] in (stats.FAIL, stats.NOT_JUDGED):
                    reasons.append(f"{form}: {m['metric']} {m['verdict']}")
    if cleanup["status"] != "PASS":
        verdict = stats.FAIL
        reasons.append("the clean-up failed: something of the run is left")
    reduced = sc.reduced()
    unmet = [k for k, v in ((environment or {}).get("conditions") or {}).items() if not v.get("ok")]
    for k in unmet:
        reasons.append(f"not the terms' default size: {k}")
    return {
        "scenario": scenario, "run": run.id, "verdict": verdict, "reasons": reasons, "reduced": reduced,
        "official": not reduced and not run.harness_dirty and not unmet and "conditions" in (environment or {}),
        "note": "a reduced run (durations or rounds below the terms'): a trial of the harness, never an acceptance"
        if reduced else "", "durationScale": sc.scale, "started": iso(run.started), "finished": iso(now()),
        "forms": forms, "extra": sc.extra, "environment": environment, "cleanup": cleanup, "error": error,
        "termsCheck": terms.check(),
    }


def number(v, digits=1):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, digits)
        return f"{v:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:,.0f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def target_text(m):
    t = m.get("target")
    if not t:
        return "observed"
    if "min" in t:
        return f"≥ {number(t['min'], 3)}"
    if "max" in t and len(t) == 1:
        return f"≤ {number(t['max'])}"
    return " / ".join(f"{k} ≤ {number(v)} ms" for k, v in t.items())


def summary(body):
    """summary.md: the run's verdict and, per form, its metrics as the document's tables."""
    lines = [f"# {body['scenario']} run `{body['run']}`: {body['verdict']}", ""]
    if body.get("reduced"):
        lines += [f"> {body['note']} (duration scale {body['durationScale']}; the load's parameters are in "
                  f"result.json).", ""]
    for r in body.get("reasons", []):
        lines.append(f"- {r}")
    if body.get("reasons"):
        lines.append("")
    env = body.get("environment") or {}
    lines += [f"Started {body['started']}, finished {body['finished']}; harness `{env.get('harnessCommit')}` "
              f"(tree {'dirty' if env.get('harnessTreeDirty') else 'clean'}); region `{env.get('region')}`.", ""]
    for form, f in body.get("forms", {}).items():
        inst = (env.get("installs") or {}).get(form, {})
        lines += [f"## {form}: {f['verdict']}", "",
                  f"Install `{inst.get('env')}`, source {', '.join(inst.get('sourceSha') or []) or '—'}"
                  f"{', release ' + ', '.join(inst.get('release')) if inst.get('release') else ''}.", "",
                  "| Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        notes = []
        for m in f["metrics"]:
            meas = m.get("measured") or {}
            if isinstance(meas, dict) and "p95" in meas:
                lines.append(f"| {m['metric']} | {number(meas.get('n'))} | {number(meas.get('p50'))} | "
                             f"{number(meas.get('p95'))} | {number(meas.get('p99'))} | {number(meas.get('max'))} | "
                             f"{target_text(m)} | {m['verdict']} |")
            elif isinstance(meas, dict) and "value" in meas:
                lines.append(f"| {m['metric']} | | | | | | {target_text(m)} | {m['verdict']} "
                             f"({number(meas.get('value'), 3)}) |")
            else:
                notes.append(m)
        lines.append("")
        lines.append("Latencies in milliseconds. Details:")
        lines.append("")
        for m in f["metrics"]:
            if m.get("detail"):
                lines.append(f"- {m['metric']}: {m['detail']}")
        for m in notes:
            text = json.dumps(m.get("measured"), sort_keys=True, default=str)
            lines.append(f"- {m['metric']} (observed): `{text[:1500]}`")
        lines.append("")
    if body.get("extra"):
        lines += ["## Besides", "", "```json", json.dumps(body["extra"], indent=1, sort_keys=True, default=str)[:6000],
                  "```", ""]
    lines += ["## Clean-up: " + body["cleanup"]["status"], ""]
    for s in body["cleanup"]["steps"]:
        lines.append(f"- {s['status']} {s['what']}: {s['detail']}")
    return "\n".join(lines) + "\n"
