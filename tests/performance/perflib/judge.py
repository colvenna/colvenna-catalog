"""Each scenario's metrics, computed from what its run collected and nothing else, and judged against
terms.md's targets (perflib/terms.py). These are pure functions of the records, so that the judging
is tested offline (test_perf.py).

Times are epoch milliseconds; the load tool's durations are microseconds; latencies are reported in
milliseconds."""

from . import stats, terms

US = 1000.0


def ms(us):
    return None if us is None else us / US


def in_window(t, window):
    return window[0] <= t < window[1]


def counts(items, key):
    out = {}
    for i in items:
        k = str(i.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def latency_row(name, values_ms, limits=None, min_samples=stats.MIN_SAMPLES, judged=True):
    """A latency metric: judged against limits, or observed (its summary only)."""
    if limits and judged:
        return stats.judge_latency(name, values_ms, limits, min_samples)
    return stats.observed(name, stats.summary(values_ms))


def attempt_statuses(lines, access):
    """The server's status of each request line (`unknown` when its id is not in the server's access
    log: an answer the gateway made, or a line without an id)."""
    out = {}
    for line in lines:
        status = access.get(line["id"], {}).get("status")
        key = str(status) if status is not None else "unknown"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


# --- P1a, P1b, and P5's writers ------------------------------------------------------------------------

def commit_window(spec):
    start = spec["start"]
    warmup_end = start + spec["warmup"] * 1000
    return warmup_end, warmup_end + spec["duration"] * 1000


def offered(spec):
    """The commits due in the window: writer w's commit k is due at start + w * interval / writers +
    k * interval (Writers.java)."""
    lo, hi = commit_window(spec)
    interval, writers, n = spec["interval"], spec["writers"], 0
    for w in range(writers):
        offset = interval * w // max(1, writers)
        k = 0
        while True:
            due = spec["start"] + offset + k * interval
            if due >= hi:
                break
            if due >= lo:
                n += 1
            k += 1
    return n


def p1a(form, spec, commits, rest, access, conflicts=None):
    """P1a's metrics: commits per second, the commit request's client-observed latency, errors; the
    server-side commit latency and a whole commit's latency observed."""
    window = commit_window(spec)
    seconds = spec["duration"]
    inside = [c for c in commits if not c.get("warmup") and in_window(c["due"], window)]
    ok = [c for c in inside if c.get("ok")]
    lines = [r for r in rest if r["kind"] == "commit" and in_window(r["t"], window)]
    t = terms.target("P1", "P1a commits per second").values[form]
    lat = terms.target("P1", "P1a commit request p95 / p99").values[form]
    server_ms = [access[r["id"]]["ms"] for r in lines if r["id"] in access]
    metrics = [
        stats.judge_at_least("P1a commits per second", len(ok) / seconds if seconds else None, t,
                             f"{len(ok)} commits in {seconds} s"),
        latency_row("P1a commit request (client-observed) p95 / p99", [r["ms"] for r in lines], lat),
        stats.judge_zero("P1a errors", len(inside) - len(ok), f"of {len(inside)} commits; by status "
                         f"{counts([c for c in inside if not c.get('ok')], 'status')}"),
        latency_row("server-side commit latency", server_ms, judged=False),
        latency_row("whole commit (the commit call)", [ms(c["commitUs"]) for c in ok], judged=False),
        latency_row("writing the data file and preparing the commit", [ms(c["prepUs"]) for c in ok], judged=False),
        stats.observed("commit request attempts by server status", attempt_statuses(lines, access)),
    ]
    if conflicts is not None:
        metrics.append(stats.observed("conflicts (stale-base refusals, retried by the client)", {"count": conflicts}))
    return metrics


def p1b(form, spec, commits, rest, access, conflicts=None):
    """P1b's metrics: commits done of those offered (none failed), a whole commit's latency, conflict
    retries included; conflicts and errors observed besides."""
    window = commit_window(spec)
    inside = [c for c in commits if not c.get("warmup") and in_window(c["due"], window)]
    ok = [c for c in inside if c.get("ok")]
    failed = len(inside) - len(ok)
    n_offered = offered(spec)
    share = len(ok) / n_offered if n_offered else None
    t = terms.target("P1", "P1b commits done of those offered").values[form]
    lat = terms.target("P1", "P1b whole commit p95 / p99").values[form]
    done = stats.judge_at_least("P1b commits done of those offered", share, t,
                                f"{len(ok)} done of {n_offered} offered, {failed} failed")
    if failed and done["verdict"] == stats.PASS:
        done = dict(done, verdict=stats.FAIL, detail=done["detail"] + ": none may fail")
    lines = [r for r in rest if r["kind"] == "commit" and in_window(r["t"], window)]
    metrics = [
        done,
        latency_row("P1b whole commit p95 / p99", [ms(c["commitUs"]) for c in ok], lat),
        stats.observed("errors", {"count": failed, "byStatus": counts([c for c in inside if not c.get("ok")], "status")}),
        latency_row("commit request (client-observed)", [r["ms"] for r in lines], judged=False),
        latency_row("late start (previous commit still running)", [ms(c["lateUs"]) for c in ok], judged=False),
        stats.observed("commit request attempts by server status", attempt_statuses(lines, access)),
    ]
    if conflicts is not None:
        metrics.append(stats.observed("conflicts (stale-base refusals, retried by the client)", {"count": conflicts}))
    return metrics


def conflicts(audit_events, window_ms):
    """COMMIT audit events refused on a stale base (outcome FAILED, answered 200) in the window."""
    from datetime import datetime

    n = 0
    for e in audit_events:
        # A stale-base refusal is answered 200 with success false and audited FAILED; a commit that
        # failed otherwise (503, 409) is answered with that status.
        if e.get("action") != "COMMIT" or e.get("outcome") != "FAILED" or e.get("httpStatus") not in (200, None):
            continue
        t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).timestamp() * 1000
        if in_window(t, window_ms):
            n += 1
    return n


# --- P2, and P5's reads ---------------------------------------------------------------------------------

READ_OPS = ("getTable", "snapshot", "listTables")


def read_limits(form, op):
    if op == "listTables":
        return terms.target("P2", "`listTables` p95 / p99, at the target rate").values[form]
    return terms.target("P2", "`getTable`, load snapshot p95 / p99, at the target rate").values[form]


def step_report(form, reads, label, rate, seconds, on_time_ms=terms.ON_TIME_MS):
    """One step's figures: per operation the latency from the time each request was due, errors,
    requests sent on time, and whether the step is within the targets."""
    rows = [r for r in reads if str(r.get("step")) == str(label)]
    expected = round(rate * seconds)
    errors = [r for r in rows if not r.get("ok")]
    unfinished = max(0, expected - len(rows))
    on_time = sum(1 for r in rows if r.get("lagUs", 0) <= on_time_ms * 1000)
    ops = {}
    within = True
    for op in READ_OPS:
        # The latency of the requests answered; those that failed are errors, counted apart.
        values = [ms(r["latUs"]) for r in rows if r.get("op") == op and r.get("ok")]
        s = stats.summary(values)
        limits = read_limits(form, op)
        ok = s["n"] > 0 and all(s[k] is not None and s[k] <= v for k, v in limits.items())
        within &= ok
        ops[op] = dict(s, within=ok, limits=limits)
    error_share = (len(errors) + unfinished) / expected if expected else 1
    on_time_share = on_time / expected if expected else 0
    sustained = within and error_share < 0.001 and on_time_share >= 0.99
    return {"step": label, "rate": rate, "expected": expected, "sent": len(rows), "unfinished": unfinished,
            "errors": len(errors), "errorsByStatus": counts(errors, "status"), "errorShare": error_share,
            "onTime": on_time, "onTimeShare": on_time_share, "ops": ops, "sustained": sustained}


def p2(form, spec, reads, rest=()):
    """P2's metrics: each operation's p95 / p99 at the target rate (from the time each request was
    due, answered requests only), and the sustainable rate; each attempt's client-observed latency
    (the request log line) at the target rate, observed."""
    steps = [step_report(form, reads, str(rate), rate, spec["step"]) for rate in spec["steps"]]
    target_rate = terms.target("P2", "Sustainable rate").values[form]
    at_target = next((s for s in steps if s["rate"] == target_rate), None)
    metrics = []
    rows = [r for r in reads if str(r.get("step")) == str(target_rate)]
    for op in READ_OPS:
        name = ("P2 listTables p95 / p99 at the target rate" if op == "listTables"
                else f"P2 {op} p95 / p99 at the target rate")
        metrics.append(latency_row(f"{name} ({target_rate}/s)", [ms(r["latUs"]) for r in rows
                                                                 if r.get("op") == op and r.get("ok")],
                                   read_limits(form, op)))
    if rows and rest:
        lo, hi = min(r["start"] for r in rows), max(r["end"] for r in rows)
        for op in READ_OPS:
            metrics.append(latency_row(f"{op} at {target_rate}/s, per attempt (the client's request line)",
                                       [x["ms"] for x in rest if x["kind"] == op and lo <= x["t"] <= hi + 1],
                                       judged=False))
    sustained = [s["rate"] for s in steps if s["sustained"]]
    highest = max(sustained) if sustained else None
    m = stats.judge_at_least("P2 sustainable rate (requests per second)", highest if highest is not None else 0,
                             target_rate, f"highest step within every target: {highest}; steps "
                             + ", ".join(f"{s['rate']}: {'yes' if s['sustained'] else 'no'}" for s in steps))
    if at_target is not None and any(at_target["ops"][op]["n"] < stats.MIN_SAMPLES for op in READ_OPS) \
            and m["verdict"] == stats.PASS:
        m = dict(m, verdict=stats.NOT_JUDGED, detail=m["detail"] + "; an operation at the target rate had fewer "
                 f"than {stats.MIN_SAMPLES} samples")
    metrics.append(m)
    metrics.append(stats.observed("steps", steps))
    return metrics


# --- P3, and P5's credential requests ------------------------------------------------------------------

def classify(tokens, audit_by_id):
    """Each credential request's class as its ISSUE_DATA_TOKEN audit event says (detail.cached):
    `miss`, `hit`, or `unclassified` (no event joined: a failed request, or none found)."""
    for t in tokens:
        cls = "unclassified"
        for a in reversed(t.get("attempts") or []):
            event = audit_by_id.get(a["id"])
            if event is not None:
                cached = (event.get("detail") or {}).get("cached")
                cls = "hit" if cached is True else "miss" if cached is False else "unclassified"
                break
        t["class"] = cls
    return tokens


def p3(form, miss_spec, misses, hit_spec, hits):
    """P3's metrics: misses' and hits' latency (from due), errors; requests whose class was not the
    intended one reported apart."""
    lim_miss = terms.target("P3", "Miss p95 / p99").values[form]
    lim_hit = terms.target("P3", "Hit p95 / p99").values[form]
    a = [t for t in misses if t.get("step") == "window"]
    b = [t for t in hits if t.get("step") == "window"]
    errors = [t for t in a + b if not t.get("ok")]
    unfinished = max(0, round(miss_spec["rate"] * miss_spec["duration"]) - len(a)) + \
        max(0, round(hit_spec["rate"] * hit_spec["duration"]) - len(b))
    # Each request is judged as what the server says it was, whatever it was meant to be: a P3b
    # request the server missed (a replica or environment that had not issued that pair) is a miss.
    miss_ms = [ms(t["latUs"]) for t in a + b if t.get("ok") and t.get("class") == "miss"]
    hit_ms = [ms(t["latUs"]) for t in a + b if t.get("ok") and t.get("class") == "hit"]
    return [
        latency_row("P3 misses p95 / p99 (P3a's, and P3b's the server missed)", miss_ms, lim_miss),
        latency_row("P3 hits p95 / p99 (P3b's, as the audit says)", hit_ms, lim_hit),
        stats.judge_zero("P3 errors", len(errors) + unfinished,
                         f"of {len(a) + len(b)} requests ({unfinished} unfinished); by status {counts(errors, 'status')}"),
        stats.observed("P3a requests by class", counts(a, "class")),
        stats.observed("P3b requests by class", counts(b, "class")),
        latency_row("P3b requests intended as hits that the server missed", [ms(t["latUs"]) for t in b
                    if t.get("ok") and t.get("class") == "miss"], judged=False),
        latency_row("P3b sent late (from due to start)", [ms(t["lagUs"]) for t in b], judged=False),
        latency_row("misses, per attempt (the client's request line)", [x["ms"] for t in a + b if t.get("class") == "miss"
                                                                        for x in t.get("attempts") or [] if "ms" in x], judged=False),
        latency_row("hits, per attempt (the client's request line)", [x["ms"] for t in a + b if t.get("class") == "hit"
                                                                      for x in t.get("attempts") or [] if "ms" in x], judged=False),
    ]


# --- P4 ---------------------------------------------------------------------------------------------------

def p4(variant, rounds, server_envs, authorizer_envs, gateway, cold_rows, rest, idle=None):
    """P4a's or P4b's metrics. rounds: [{round, requestAt (epoch ms), clients}]; server_envs and
    authorizer_envs: raw.environments() of the functions' REPORT lines over the rounds; gateway: the
    API's access log entries over the rounds; cold_rows: the load tool's cold records; rest: its
    request lines. An environment belongs to the round whose requests came just before its first
    invocation."""
    key = "restoreDurationMs" if variant == "P4b" else "initDurationMs"
    limits = terms.target("P4", "Server `Init Duration` (P4a) or `Restore Duration` (P4b) p95 / max").values[variant]
    first = terms.target("P4", "First request, client-observed, server and authorizer cold, p95 / max").values[variant]

    def round_of(at):
        best = None
        for r in rounds:
            if r["requestAt"] - 2000 <= at <= r["requestAt"] + 60000:
                best = r["round"]
        return best

    cold = [e for e in server_envs if e.get(key) is not None and round_of(e.get("firstInvokeAt") or 0) is not None]
    wrong_kind = [e for e in server_envs if round_of(e.get("firstInvokeAt") or 0) is not None
                  and e.get("initDurationMs" if variant == "P4b" else "restoreDurationMs") is not None]
    timeouts = sum(1 for e in server_envs + authorizer_envs if e.get("initTimedOut") or e.get("initFailed"))
    in_rounds = [g for g in gateway if any(r["requestAt"] - 2000 <= g.get("_t", 0) <= r["requestAt"] + 120000
                                           for r in rounds)]
    bad = [g for g in in_rounds if str(g.get("status", "")).startswith("5")]
    clients_failed = [c for c in cold_rows if not c.get("configOk") or not c.get("getTableOk")]
    # The first request of each client: its GET /v1/config's first attempt, as the client logged it.
    by_thread = {}
    for line in sorted((x for x in rest if x["kind"] == "config"), key=lambda x: x["t"]):
        by_thread.setdefault(line["thread"], []).append(line)
    first_ms, operation_ms = [], []
    for c in cold_rows:
        lines = [x for x in by_thread.get(c.get("thread"), []) if c["start"] <= x["t"] <= c["start"] + c["configUs"] / 1000 + 1]
        if lines:
            first_ms.append(lines[0]["ms"])
        operation_ms.append(ms(c["configUs"]))
    auth_cold = [e for e in authorizer_envs if e.get("initDurationMs") is not None
                 and round_of(e.get("firstInvokeAt") or 0) is not None]
    metrics = [
        stats.judge_zero(f"{variant} errors: init timeouts, 5xx, 504", timeouts + len(bad) + len(clients_failed),
                         f"init timeouts or failures {timeouts}; gateway 5xx {len(bad)} of {len(in_rounds)} requests "
                         f"(by status {counts(in_rounds, 'status')}); clients failed {len(clients_failed)}"),
        stats.judge_latency(f"{variant} server {'Restore' if variant == 'P4b' else 'Init'} Duration p95 / max",
                            [e[key] for e in cold], limits, stats.MIN_SAMPLES_COLD),
        stats.judge_latency(f"{variant} first request (GET /v1/config), client-observed, p95 / max", first_ms, first,
                            stats.MIN_SAMPLES_COLD),
        latency_row("first request, the whole call (client retries included)", operation_ms, judged=False),
        latency_row("server's first invocation Duration", [e["firstInvokeDurationMs"] for e in cold
                                                           if e.get("firstInvokeDurationMs") is not None], judged=False),
        latency_row("authorizer's Init Duration", [e["initDurationMs"] for e in auth_cold], judged=False),
        latency_row("authorizer's first invocation Duration", [e["firstInvokeDurationMs"] for e in auth_cold
                                                               if e.get("firstInvokeDurationMs") is not None],
                    judged=False),
        latency_row("getTable after the first request, client-observed (whole call)",
                    [ms(c["getTableUs"]) for c in cold_rows if c.get("getTableUs") is not None], judged=False),
        stats.observed("cold server environments per round", counts(
            [{"round": round_of(e.get("firstInvokeAt") or 0)} for e in cold], "round")),
        stats.observed("environments of the other kind in the rounds", {"count": len(wrong_kind)}),
        stats.observed("cold requests that also waited on a lock or on S3", {
            "count": 0, "why": "the cold requests are GET /v1/config and getTable: the server answers both from its "
                               "database, with no commit lock and no S3 call"}),
    ]
    if idle is not None:
        metrics.append(stats.observed("idle requests (after at least the idle wait without traffic)", idle))
    return metrics


# --- P5 ----------------------------------------------------------------------------------------------------

def p5(form, spec, commits, rest, reads, tokens, cold_ids=()):
    """P5's metrics on one form: no error, and each part within its form's targets (P1a's commit
    request, P2's reads, P3's credentials). Requests served by a cold execution environment are
    reported apart (cold_ids: their request ids)."""
    cold_ids = set(cold_ids)
    w = spec["writers"]
    window = commit_window(w)
    inside = [c for c in commits if not c.get("warmup") and in_window(c["due"], window)]
    lines = [r for r in rest if r["kind"] == "commit" and in_window(r["t"], window) and r["id"] not in cold_ids]
    rd = [r for r in reads if r.get("step") == "window"]
    tk = [t for t in tokens if t.get("step") == "window"]

    def warm(op):
        return not any(a["id"] in cold_ids for a in op.get("attempts") or [])

    errors = [c for c in inside if not c.get("ok")] + [r for r in rd if not r.get("ok")] + \
        [t for t in tk if not t.get("ok")]
    expected = round(spec["reads"]["rate"] * spec["reads"]["duration"]) + \
        round(spec["tokens"]["rate"] * spec["tokens"]["duration"])
    unfinished = max(0, expected - len(rd) - len(tk))
    metrics = [stats.judge_zero("P5 errors", len(errors) + unfinished,
                                f"commits {sum(1 for c in inside if not c.get('ok'))}, reads "
                                f"{sum(1 for r in rd if not r.get('ok'))}, credentials "
                                f"{sum(1 for t in tk if not t.get('ok'))}, unfinished {unfinished}")]
    metrics.append(latency_row("P5 commit request p95 / p99 (P1a's target)", [r["ms"] for r in lines],
                               terms.target("P1", "P1a commit request p95 / p99").values[form]))
    for op in READ_OPS:
        metrics.append(latency_row(f"P5 {op} p95 / p99 (P2's target)",
                                   [ms(r["latUs"]) for r in rd if r.get("op") == op and r.get("ok") and warm(r)],
                                   read_limits(form, op)))
    metrics.append(latency_row("P5 credential hit p95 / p99 (P3's target)",
                               [ms(t["latUs"]) for t in tk if t.get("ok") and t.get("class") == "hit" and warm(t)],
                               terms.target("P3", "Hit p95 / p99").values[form]))
    metrics.append(latency_row("P5 credential miss p95 / p99 (P3's target)",
                               [ms(t["latUs"]) for t in tk if t.get("ok") and t.get("class") == "miss" and warm(t)],
                               terms.target("P3", "Miss p95 / p99").values[form]))
    apart = [r for r in rd + tk if not warm(r)]
    metrics.append(latency_row("P5 requests served by a cold execution environment (apart)",
                               [ms(r["latUs"]) for r in apart], judged=False))
    metrics.append(stats.observed("P5 credential requests by class", counts(tk, "class")))
    return metrics


def ratios(eks_metrics, serverless_metrics):
    """Serverless / EKS of each latency metric's p50, p95 and p99 (observed)."""
    out = {}
    by_name = {m["metric"]: m for m in eks_metrics}
    for m in serverless_metrics:
        e = by_name.get(m["metric"])
        if not e or not isinstance(m.get("measured"), dict) or "p95" not in m["measured"]:
            continue
        row = {}
        for k in ("p50", "p95", "p99"):
            a, b = e["measured"].get(k), m["measured"].get(k)
            row[k] = round(b / a, 2) if a and b is not None else None
        out[m["metric"]] = row
    return out
