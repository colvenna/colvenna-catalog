"""Percentiles and verdicts as docs/performance/terms.md fixes them (sections 1 and 3).

- A percentile is nearest-rank over every sample of the window: the value at rank ceil(p / 100 * n)
  of the sorted samples.
- A judged percentile needs at least 1,000 samples (P4: 40); with fewer it is observed only, and its
  verdict NOT_JUDGED.
- A scenario's, and a candidate's, verdict: FAIL if any judged metric failed or is missing,
  NOT_JUDGED if none failed but one had too few samples, PASS otherwise."""

import math

PASS, FAIL, NOT_JUDGED, OBSERVED = "PASS", "FAIL", "NOT_JUDGED", "OBSERVED"
MIN_SAMPLES = 1000
MIN_SAMPLES_COLD = 40


def nearest_rank(values, p):
    """The p-th percentile (0 < p <= 100) of values by nearest rank; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summary(values):
    """n, p50, p95, p99, max and mean of the values (None for each when there are none)."""
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    ordered = sorted(values)
    return {"n": len(ordered), "p50": nearest_rank(ordered, 50), "p95": nearest_rank(ordered, 95),
            "p99": nearest_rank(ordered, 99), "max": ordered[-1], "mean": sum(ordered) / len(ordered)}


def rounded(value, digits=1):
    return None if value is None else round(value, digits)


def judge_latency(name, values, limits, min_samples=MIN_SAMPLES, unit="ms"):
    """A latency metric judged against {percentile name: limit} (p95, p99, max), in the values' unit.
    Too few samples: NOT_JUDGED, with what was measured."""
    s = summary(values)
    measured = {k: rounded(s[k]) for k in limits}
    detail = ", ".join(f"{k} {measured[k]} {unit} (limit {v:g})" for k, v in limits.items())
    if s["n"] == 0:
        return metric(name, FAIL, s, limits, f"no samples: {detail}")
    if s["n"] < min_samples:
        return metric(name, NOT_JUDGED, s, limits, f"{s['n']} samples, fewer than {min_samples}: {detail}")
    failed = [k for k, v in limits.items() if s[k] is None or s[k] > v]
    return metric(name, FAIL if failed else PASS, s, limits, f"n {s['n']}: {detail}")


def judge_at_least(name, value, limit, detail=""):
    if value is None:
        return metric(name, FAIL, {"value": None}, {"min": limit}, f"not measured {detail}".strip())
    return metric(name, PASS if value >= limit else FAIL, {"value": value}, {"min": limit},
                  f"{rounded(value, 3)} (at least {limit:g}){' ' + detail if detail else ''}")


def judge_zero(name, count, detail=""):
    if count is None:
        return metric(name, FAIL, {"value": None}, {"max": 0}, f"not measured {detail}".strip())
    return metric(name, PASS if count == 0 else FAIL, {"value": count}, {"max": 0},
                  f"{count}{' ' + detail if detail else ''}")


def observed(name, measured, detail=""):
    return metric(name, OBSERVED, measured, None, detail)


def metric(name, verdict, measured, target, detail):
    return {"metric": name, "verdict": verdict, "measured": measured, "target": target, "detail": detail}


def verdict(metrics):
    """The verdict of a list of metrics (observed ones never count)."""
    judged = [m["verdict"] for m in metrics if m["verdict"] != OBSERVED]
    if not judged:
        return FAIL
    if FAIL in judged:
        return FAIL
    if NOT_JUDGED in judged:
        return NOT_JUDGED
    return PASS
