"""The performance harness offline: its percentiles and verdicts, its targets against terms.md, the
parsing and joining of the raw data, and each scenario's judging, with data that should pass and
data that should not."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perflib import judge, raw, report, scenarios, stats, terms  # noqa: E402

T0 = 1_761_768_000_000  # epoch ms


class StatsTest(unittest.TestCase):
    def test_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(stats.nearest_rank(values, 50), 50)
        self.assertEqual(stats.nearest_rank(values, 95), 95)
        self.assertEqual(stats.nearest_rank(values, 99), 99)
        self.assertEqual(stats.nearest_rank([7], 99), 7)
        self.assertEqual(stats.nearest_rank([3, 1, 2], 50), 2)
        self.assertIsNone(stats.nearest_rank([], 50))

    def test_judge_latency(self):
        ok = stats.judge_latency("x", [10.0] * 1000, {"p95": 50, "p99": 100})
        self.assertEqual(ok["verdict"], stats.PASS)
        slow = stats.judge_latency("x", [10.0] * 980 + [200.0] * 20, {"p95": 50, "p99": 100})
        self.assertEqual(slow["verdict"], stats.FAIL)
        few = stats.judge_latency("x", [10.0] * 999, {"p95": 50, "p99": 100})
        self.assertEqual(few["verdict"], stats.NOT_JUDGED)
        cold = stats.judge_latency("x", [1000.0] * 40, {"p95": 4000, "max": 6000}, stats.MIN_SAMPLES_COLD)
        self.assertEqual(cold["verdict"], stats.PASS)
        self.assertEqual(stats.judge_latency("x", [], {"p95": 1})["verdict"], stats.FAIL)

    def test_verdicts(self):
        p, f, n, o = ({"verdict": v} for v in (stats.PASS, stats.FAIL, stats.NOT_JUDGED, stats.OBSERVED))
        self.assertEqual(stats.verdict([p, o]), stats.PASS)
        self.assertEqual(stats.verdict([p, n]), stats.NOT_JUDGED)
        self.assertEqual(stats.verdict([n, f]), stats.FAIL)
        self.assertEqual(stats.verdict([o]), stats.FAIL)  # nothing judged is a missing result


class TermsTest(unittest.TestCase):
    def test_the_harness_judges_by_terms_md(self):
        self.assertEqual(terms.check(), [])

    def test_a_changed_target_is_found(self):
        text = terms.TERMS.read_text(encoding="utf-8").replace("| P1a commits per second | ≥ 10 | ≥ 5 |",
                                                                  "| P1a commits per second | ≥ 12 | ≥ 5 |")
        problems = terms.check(text)
        self.assertTrue(any("P1a commits per second" in p for p in problems), problems)

    def test_a_new_row_is_found(self):
        text = terms.TERMS.read_text(encoding="utf-8").replace(
            "| P1a errors | 0 | 0 |", "| P1a errors | 0 | 0 |\n| P1a something new | 1 | 1 |")
        self.assertTrue(any("something new" in p for p in terms.check(text)))

    def test_scaling(self):
        self.assertEqual(terms.scaled(600, 1), 600)
        self.assertEqual(terms.scaled(600, 0.1), 60)
        self.assertEqual(terms.scaled(60, 0.01), 10)


class RawTest(unittest.TestCase):
    def test_request_lines(self):
        text = "\n".join([
            f"{T0} pool-1-thread-3 [rest] requestId:abc method:POST "
            "url:https://h:8181/v1/colvenna/databases/d/tables/t000/commit duration:120ms",
            f"{T0 + 1} pool-1-thread-3 [rest] requestId:def method:GET "
            "url:https://x.execute-api.us-west-2.amazonaws.com/v1/v1/colvenna/databases/d/tables/t000/snapshot duration:7ms",
            f"{T0 + 2} t [rest] requestId:unknown method:GET url:https://h/v1/config?warehouse=colvenna duration:9ms",
            f"{T0 + 3} t [rest] requestId:g method:GET url:https://h/v1/colvenna/databases/d/tables?maxResults=100 duration:5ms",
            f"{T0 + 4} t [rest] requestId:h method:GET url:https://h/v1/colvenna/databases/d/tables/t001 duration:4ms",
            f"{T0 + 5} t [rest] requestId:i method:GET url:https://h/v1/colvenna/databases/d/tables/t001/token duration:40ms",
            "a line that is not one",
        ])
        lines = raw.rest_lines(text)
        self.assertEqual([x["kind"] for x in lines], ["commit", "snapshot", "config", "listTables", "getTable", "token"])
        self.assertEqual(lines[0]["ms"], 120)
        self.assertEqual(lines[0]["thread"], "pool-1-thread-3")

    def test_access_lines(self):
        text = ("pod-a 2026-10-29T20:00:00.1Z 20:00:00.100 [x] INFO  colvenna.access - abc POST "
                "/v1/colvenna/databases/d/tables/t/commit 200 98ms principal=pfwriter-p1 action=COMMIT\n")
        a = raw.access_lines(text)
        self.assertEqual(a["abc"]["status"], 200)
        self.assertEqual(a["abc"]["ms"], 98)
        self.assertEqual(a["abc"]["action"], "COMMIT")

    def test_join_by_thread_and_time(self):
        lines = [{"t": T0 + 10, "thread": "a", "id": "1", "kind": "token", "ms": 5},
                 {"t": T0 + 30, "thread": "a", "id": "2", "kind": "token", "ms": 6},
                 {"t": T0 + 12, "thread": "b", "id": "3", "kind": "token", "ms": 7}]
        ops = [{"thread": "a", "start": T0, "end": T0 + 20}, {"thread": "a", "start": T0 + 25, "end": T0 + 31}]
        raw.join(ops, raw.ThreadIndex(lines), {"token"}, {"1": {"status": 200}})
        self.assertEqual([a["id"] for a in ops[0]["attempts"]], ["1"])
        self.assertEqual(ops[0]["serverStatus"], 200)
        self.assertEqual([a["id"] for a in ops[1]["attempts"]], ["2"])

    def test_environments(self):
        events = [
            {"logStreamName": "s1", "timestamp": 1, "message": "INIT_REPORT Init Duration: 2100.5 ms\tPhase: init\tStatus: success"},
            {"logStreamName": "s1", "timestamp": 2, "message": "REPORT RequestId: x\tDuration: 1800.0 ms\tBilled Duration: 1801 ms"},
            {"logStreamName": "s1", "timestamp": 3, "message": "REPORT RequestId: y\tDuration: 30.0 ms\tBilled Duration: 31 ms"},
            {"logStreamName": "s2", "timestamp": 4, "message": "RESTORE_REPORT Restore Duration: 600.0 ms\tStatus: timeout"},
        ]
        envs = {e["stream"]: e for e in raw.environments(events)}
        self.assertEqual(envs["s1"]["initDurationMs"], 2100.5)
        self.assertEqual(envs["s1"]["firstInvokeDurationMs"], 1800.0)
        self.assertEqual(envs["s1"]["invocations"], 2)
        self.assertTrue(envs["s2"]["initTimedOut"])
        self.assertEqual(envs["s2"]["restoreDurationMs"], 600.0)

    def test_records_and_facts(self):
        self.assertEqual(raw.records('{"a": 1}\nnoise\n{"b": 2}'), [{"a": 1}, {"b": 2}])
        self.assertEqual(raw.facts('PERF {"kind": "schedule", "start": 5}\nother'), [{"kind": "schedule", "start": 5}])


def commits(n_writers, per_writer, interval, start, warmup_s, ok=True, commit_ms=100, late=0):
    out = []
    for w in range(n_writers):
        offset = interval * w // n_writers
        for k in range(per_writer):
            due = start + offset + k * (interval or 100)
            out.append({"w": w, "k": k, "due": due, "start": due + late, "warmup": due < start + warmup_s * 1000,
                        "ok": ok, "commitUs": commit_ms * 1000, "prepUs": 20_000, "lateUs": late * 1000,
                        "wholeUs": (commit_ms + 20) * 1000, "status": None if ok else "503"})
    return out


class P1Test(unittest.TestCase):
    spec_a = {"start": T0, "warmup": 60, "duration": 600, "writers": 8, "interval": 0}

    def p1a_data(self, n, ms_each):
        cs = [{"due": T0 + 60_000 + i * 50, "warmup": False, "ok": True, "commitUs": 150_000, "prepUs": 50_000,
               "status": None} for i in range(n)]
        rest = [{"t": T0 + 60_000 + i * 50, "thread": "x", "id": f"r{i}", "kind": "commit", "ms": ms_each}
                for i in range(n)]
        access = {f"r{i}": {"status": 200, "ms": ms_each - 5} for i in range(n)}
        return cs, rest, access

    def test_p1a_passes(self):
        cs, rest, access = self.p1a_data(7200, 120)  # 12 a second
        m = {x["metric"]: x for x in judge.p1a("eks", self.spec_a, cs, rest, access)}
        self.assertEqual(m["P1a commits per second"]["verdict"], stats.PASS)
        self.assertEqual(m["P1a commit request (client-observed) p95 / p99"]["verdict"], stats.PASS)
        self.assertEqual(m["P1a errors"]["verdict"], stats.PASS)
        self.assertEqual(stats.verdict(list(m.values())), stats.PASS)

    def test_p1a_too_slow_too_few(self):
        cs, rest, access = self.p1a_data(3000, 700)  # 5 a second, 700 ms each
        m = {x["metric"]: x for x in judge.p1a("eks", self.spec_a, cs, rest, access)}
        self.assertEqual(m["P1a commits per second"]["verdict"], stats.FAIL)
        self.assertEqual(m["P1a commit request (client-observed) p95 / p99"]["verdict"], stats.FAIL)
        # The serverless form's targets are wider: 5 a second and 1,000 / 2,000 ms.
        m = {x["metric"]: x for x in judge.p1a("serverless", self.spec_a, cs, rest, access)}
        self.assertEqual(m["P1a commits per second"]["verdict"], stats.PASS)
        self.assertEqual(m["P1a commit request (client-observed) p95 / p99"]["verdict"], stats.PASS)

    def test_p1a_an_error_fails(self):
        cs, rest, access = self.p1a_data(7200, 120)
        cs[100] = dict(cs[100], ok=False, status="503")
        m = {x["metric"]: x for x in judge.p1a("eks", self.spec_a, cs, rest, access)}
        self.assertEqual(m["P1a errors"]["verdict"], stats.FAIL)

    def test_p1b_offered_and_done(self):
        spec = {"start": T0, "warmup": 60, "duration": 600, "writers": 4, "interval": 2000}
        self.assertEqual(judge.offered(spec), 1200)
        cs = commits(4, 330, 2000, T0, 60)
        m = {x["metric"]: x for x in judge.p1b("eks", spec, cs, [], {})}
        self.assertEqual(m["P1b commits done of those offered"]["verdict"], stats.PASS)
        self.assertEqual(m["P1b whole commit p95 / p99"]["verdict"], stats.PASS)
        # One failed commit fails it, however many were done.
        cs[200] = dict(cs[200], ok=False)
        m = {x["metric"]: x for x in judge.p1b("eks", spec, cs, [], {})}
        self.assertEqual(m["P1b commits done of those offered"]["verdict"], stats.FAIL)

    def test_p1b_too_slow(self):
        spec = {"start": T0, "warmup": 60, "duration": 600, "writers": 4, "interval": 2000}
        cs = commits(4, 330, 2000, T0, 60, commit_ms=7000)
        m = {x["metric"]: x for x in judge.p1b("eks", spec, cs, [], {})}
        self.assertEqual(m["P1b whole commit p95 / p99"]["verdict"], stats.FAIL)
        # Done of offered counts commits made: those not started by the window's end are not done.
        m = {x["metric"]: x for x in judge.p1b("eks", spec, cs[:600], [], {})}
        self.assertEqual(m["P1b commits done of those offered"]["verdict"], stats.FAIL)

    def test_p1b_load_of_the_terms(self):
        """#83: P1b at the load the requester confirmed on 2026-11-01: 4 writers on one table every 4 s on the EKS form
        (1 commit/s) for 20 minutes, every 8 s on the serverless form (0.5/s) for 40 minutes, so that each window
        offers at least the 1,000 commits a judged percentile needs; the targets unchanged."""
        p = terms.LOAD["P1b"]
        self.assertEqual((p["writers"], p["shared"], p["warmup"]), (4, True, 60))
        self.assertEqual(p["interval"], {"eks": 4000, "serverless": 8000})
        self.assertEqual(p["duration"], {"eks": 1200, "serverless": 2400})
        for form in terms.FORMS:
            spec = {"start": T0, "warmup": p["warmup"], "duration": terms.per_form(p["duration"], form),
                    "writers": p["writers"], "interval": terms.per_form(p["interval"], form)}
            with self.subTest(form=form):
                self.assertEqual(judge.offered(spec), 1200)
                self.assertGreaterEqual(judge.offered(spec), stats.MIN_SAMPLES)
                per_writer = (spec["warmup"] + spec["duration"]) * 1000 // spec["interval"] + 1
                cs = commits(4, per_writer, spec["interval"], T0, 60, commit_ms=800)
                m = {x["metric"]: x for x in judge.p1b(form, spec, cs, [], {})}
                self.assertEqual(m["P1b commits done of those offered"]["verdict"], stats.PASS)
                self.assertEqual(m["P1b whole commit p95 / p99"]["verdict"], stats.PASS)
                self.assertEqual(m["P1b whole commit p95 / p99"]["measured"]["n"], 1200)
        self.assertEqual(terms.target("P1", "P1b commits done of those offered").values, {"eks": 0.99, "serverless": 0.99})
        self.assertEqual(terms.target("P1", "P1b whole commit p95 / p99").values,
                         {"eks": {"p95": 3000, "p99": 6000}, "serverless": {"p95": 5000, "p99": 10000}})

    def test_p1c_one_tables_capacity_is_observed(self):
        """#83: the earlier P1b load, 4 writers every 2 s (2 commits/s offered), stays as one table's capacity: reported
        (commits done per second, done of offered, failed, latencies, conflicts), never judged."""
        p = terms.LOAD["P1c"]
        self.assertEqual((p["writers"], p["interval"], p["shared"], p["warmup"], p["duration"]), (4, 2000, True, 60, 600))
        self.assertIn("P1c", terms.SCENARIOS)
        self.assertIn("P1c", terms.OBSERVED_SCENARIOS)
        spec = {"start": T0, "warmup": 60, "duration": 600, "writers": 4, "interval": 2000}
        cs = commits(4, 330, 2000, T0, 60, commit_ms=2500)
        cs[300] = dict(cs[300], ok=False, status="RuntimeException")
        metrics = judge.p1c("eks", spec, cs, [], {}, conflicts=900)
        self.assertTrue(all(m["verdict"] == stats.OBSERVED for m in metrics), metrics)
        m = {x["metric"]: x for x in metrics}
        self.assertEqual(m["P1c commits done per second (one table's capacity)"]["measured"]["value"], round(1199 / 600, 3))
        self.assertEqual(m["P1c commits done of those offered"]["measured"], {"done": 1199, "offered": 1200, "failed": 1,
                                                                              "share": round(1199 / 1200, 4)})
        self.assertEqual(m["conflicts (stale-base refusals, retried by the client)"]["measured"],
                         {"count": 900, "perCommitDone": round(900 / 1199, 2)})
        self.assertIn("whole commit (the commit call)", m)
        self.assertEqual(stats.verdict(metrics), stats.FAIL)  # nothing judged: never a PASS by itself

    def test_an_observed_scenario_s_result(self):
        """#83: P1c's result is OBSERVED when it ran and cleaned up, FAIL when it did not complete or left something."""
        class Sc:
            results = {"eks": [stats.observed("x", {"value": 1})]}
            load, extra = {}, {}
            scale = 1

            def reduced(self):
                return False

        class Run:
            id, started, harness_dirty = "p1", report.now(), False

        ok = report.result("P1c", Run(), Sc(), {"conditions": {}}, {"status": "PASS"}, "")
        self.assertEqual((ok["verdict"], ok["forms"]["eks"]["verdict"], ok["reasons"]), (stats.OBSERVED, stats.OBSERVED, []))
        self.assertEqual(report.result("P1c", Run(), Sc(), {"conditions": {}}, {"status": "FAIL"}, "")["verdict"], stats.FAIL)
        self.assertEqual(report.result("P1c", Run(), Sc(), {"conditions": {}}, {"status": "PASS"}, "boom")["verdict"], stats.FAIL)
        # A judged scenario with only observed rows is still no PASS.
        self.assertEqual(report.result("P1b", Run(), Sc(), {"conditions": {}}, {"status": "PASS"}, "")["verdict"], stats.FAIL)

    def test_conflicts(self):
        events = [{"action": "COMMIT", "outcome": "FAILED", "httpStatus": 200, "time": "2025-10-29T20:00:30Z"},
                  {"action": "COMMIT", "outcome": "FAILED", "httpStatus": 503, "time": "2025-10-29T20:00:30Z"},
                  {"action": "COMMIT", "outcome": "SUCCEEDED", "httpStatus": 200, "time": "2025-10-29T20:00:30Z"}]
        from datetime import datetime

        t = datetime.fromisoformat("2025-10-29T20:00:30+00:00").timestamp() * 1000
        self.assertEqual(judge.conflicts(events, (t - 1, t + 1)), 1)


def reads(rate, seconds, label, lat_ms, ok=True, lag_ms=1, ops=("getTable",) * 6 + ("snapshot",) * 3 + ("listTables",)):
    n = round(rate * seconds)
    return [{"step": label, "op": ops[i % len(ops)], "latUs": lat_ms * 1000, "lagUs": lag_ms * 1000, "ok": ok,
             "status": None if ok else "503"} for i in range(n)]


class P2Test(unittest.TestCase):
    def test_sustainable_rate(self):
        data = reads(50, 300, "50", 10) + reads(100, 300, "100", 10) + reads(200, 300, "200", 20) + \
            reads(400, 300, "400", 60) + reads(800, 300, "800", 300)
        m = {x["metric"]: x for x in judge.p2("eks", {"steps": [50, 100, 200, 400, 800], "step": 300}, data)}
        self.assertEqual(m["P2 getTable p95 / p99 at the target rate (200/s)"]["verdict"], stats.PASS)
        self.assertEqual(m["P2 listTables p95 / p99 at the target rate (200/s)"]["verdict"], stats.PASS)
        rate = m["P2 sustainable rate (requests per second)"]
        self.assertEqual(rate["verdict"], stats.PASS)
        self.assertEqual(rate["measured"]["value"], 200)

    def test_late_or_failing_steps_are_not_sustained(self):
        late = reads(200, 300, "200", 20, lag_ms=500)  # the load tool could not keep up
        failing = reads(200, 300, "200", 20)
        for r in failing[:100]:
            r["ok"] = False
        for data in (late, failing):
            steps = judge.step_report("eks", data, "200", 200, 300)
            self.assertFalse(steps["sustained"])
        m = {x["metric"]: x for x in judge.p2("eks", {"steps": [200], "step": 300}, late)}
        self.assertEqual(m["P2 sustainable rate (requests per second)"]["verdict"], stats.FAIL)

    def test_failed_requests_are_errors_not_latency(self):
        data = reads(200, 300, "200", 20)
        for r in data[:3]:
            r["ok"], r["latUs"] = False, 31_000_000
        step = judge.step_report("eks", data, "200", 200, 300)
        self.assertEqual(step["errors"], 3)
        self.assertEqual(step["ops"]["getTable"]["max"], 20.0)
        self.assertTrue(step["sustained"])  # 3 of 60,000 is under 0.1 %

    def test_missing_requests_count_as_errors(self):
        data = reads(200, 300, "200", 20)[:-100]
        self.assertEqual(judge.step_report("eks", data, "200", 200, 300)["unfinished"], 100)


class P3Test(unittest.TestCase):
    def ops(self, n, step, cls, lat_ms):
        return [{"step": step, "ok": True, "latUs": lat_ms * 1000, "lagUs": 0, "attempts": [{"id": f"{step}{cls}{i}"}],
                 "class": None, "_cls": cls} for i in range(n)]

    def test_classified_by_the_audit(self):
        misses = self.ops(1080, "window", "miss", 200)
        hits = self.ops(6000, "window", "hit", 10)
        audit = {o["attempts"][0]["id"]: {"detail": {"cached": o["_cls"] == "hit"}} for o in misses + hits}
        judge.classify(misses, audit)
        judge.classify(hits, audit)
        miss_spec = {"rate": 3, "duration": 360}
        hit_spec = {"rate": 20, "duration": 300}
        m = {x["metric"]: x for x in judge.p3("eks", miss_spec, misses, hit_spec, hits)}
        self.assertEqual(m["P3 misses p95 / p99 (P3a's, and P3b's the server missed)"]["verdict"], stats.PASS)
        self.assertEqual(m["P3 hits p95 / p99 (P3b's, as the audit says)"]["verdict"], stats.PASS)
        self.assertEqual(m["P3 errors"]["verdict"], stats.PASS)
        # A hit the server missed is a miss, judged as one, not hidden among the hits.
        audit[hits[0]["attempts"][0]["id"]] = {"detail": {"cached": False}}
        judge.classify(hits, audit)
        self.assertEqual(hits[0]["class"], "miss")

    def test_slow_hits_fail(self):
        misses = self.ops(1080, "window", "miss", 200)
        hits = self.ops(6000, "window", "hit", 80)
        audit = {o["attempts"][0]["id"]: {"detail": {"cached": o["_cls"] == "hit"}} for o in misses + hits}
        judge.classify(misses, audit)
        judge.classify(hits, audit)
        m = {x["metric"]: x for x in judge.p3("eks", {"rate": 3, "duration": 360}, misses, {"rate": 20, "duration": 300}, hits)}
        self.assertEqual(m["P3 hits p95 / p99 (P3b's, as the audit says)"]["verdict"], stats.FAIL)
        self.assertEqual(m["P3 hits p95 / p99 (P3b's, as the audit says)"]["verdict"], stats.FAIL)


class P4Test(unittest.TestCase):
    def data(self, n_rounds, init_ms, first_ms, status=200, timeout=False):
        rounds = [{"round": r, "requestAt": T0 + r * 120_000} for r in range(n_rounds)]
        envs, rows, rest, gateway = [], [], [], []
        for r in rounds:
            for i in range(5):
                envs.append({"stream": f"{r['round']}-{i}", "initDurationMs": init_ms, "restoreDurationMs": None,
                             "firstInvokeDurationMs": 1800.0, "firstInvokeAt": r["requestAt"] + 4000,
                             "initTimedOut": timeout and i == 0, "initFailed": False})
                thread = f"pool-{r['round']}-thread-{i}"
                rows.append({"round": r["round"], "start": r["requestAt"], "configUs": first_ms * 1000,
                             "configOk": True, "getTableOk": True, "getTableUs": 30_000, "thread": thread})
                rest.append({"t": r["requestAt"] + first_ms, "thread": thread, "id": "x", "kind": "config",
                             "ms": first_ms})
                gateway.append({"_t": r["requestAt"] + first_ms, "status": status})
        auth = [{"stream": "a", "initDurationMs": 150.0, "firstInvokeDurationMs": 500.0,
                 "firstInvokeAt": T0 + 1000, "initTimedOut": False, "initFailed": False}]
        return rounds, envs, auth, gateway, rows, rest

    def test_p4a_passes(self):
        m = {x["metric"]: x for x in judge.p4("P4a", *self.data(8, 2200.0, 4500))}
        self.assertEqual(m["P4a errors: init timeouts, 5xx, 504"]["verdict"], stats.PASS)
        self.assertEqual(m["P4a server Init Duration p95 / max"]["verdict"], stats.PASS)
        self.assertEqual(m["P4a first request (GET /v1/config), client-observed, p95 / max"]["verdict"], stats.PASS)

    def test_p4a_fails_and_too_few(self):
        m = {x["metric"]: x for x in judge.p4("P4a", *self.data(8, 6500.0, 11000, status=504, timeout=True))}
        self.assertEqual(m["P4a errors: init timeouts, 5xx, 504"]["verdict"], stats.FAIL)
        self.assertEqual(m["P4a server Init Duration p95 / max"]["verdict"], stats.FAIL)
        self.assertEqual(m["P4a first request (GET /v1/config), client-observed, p95 / max"]["verdict"], stats.FAIL)
        m = {x["metric"]: x for x in judge.p4("P4a", *self.data(2, 2200.0, 4500))}
        self.assertEqual(m["P4a server Init Duration p95 / max"]["verdict"], stats.NOT_JUDGED)

    def test_p4b_takes_restores(self):
        rounds, envs, auth, gateway, rows, rest = self.data(8, 2200.0, 3000)
        for e in envs:
            e["restoreDurationMs"], e["initDurationMs"] = 600.0, None
        m = {x["metric"]: x for x in judge.p4("P4b", rounds, envs, auth, gateway, rows, rest)}
        self.assertEqual(m["P4b server Restore Duration p95 / max"]["verdict"], stats.PASS)
        self.assertEqual(m["P4b server Restore Duration p95 / max"]["measured"]["n"], 40)


class P5Test(unittest.TestCase):
    def test_both_parts_judged_cold_ones_apart(self):
        start, warmup, duration = T0, 120, 1800
        spec = {"writers": {"start": start, "warmup": warmup, "duration": duration, "interval": 2000, "writers": 4},
                "reads": {"rate": 50, "duration": duration}, "tokens": {"rate": 2, "duration": duration}}
        cs = commits(4, 960, 2000, start, warmup)
        rest = [{"t": start + warmup * 1000 + i * 500, "thread": "w", "id": f"c{i}", "kind": "commit", "ms": 150}
                for i in range(3600)]
        rd = [dict(r, attempts=[{"id": f"r{i}"}]) for i, r in enumerate(reads(50, duration, "window", 10))]
        rd[0]["latUs"] = 3_000_000  # served by a new execution environment
        tk = [{"step": "window", "ok": True, "latUs": 20_000, "class": "hit" if i % 10 else "miss",
               "attempts": [{"id": f"t{i}"}]} for i in range(3600)]
        m = {x["metric"]: x for x in judge.p5("eks", spec, cs, rest, rd, tk, cold_ids={"r0"})}
        self.assertEqual(m["P5 errors"]["verdict"], stats.PASS)
        self.assertEqual(m["P5 commit request p95 / p99 (P1a's target)"]["verdict"], stats.PASS)
        self.assertEqual(m["P5 getTable p95 / p99 (P2's target)"]["verdict"], stats.PASS)
        self.assertEqual(m["P5 requests served by a cold execution environment (apart)"]["measured"]["n"], 1)
        # Fewer than 1,000 misses at 10 % of 2 a second for 30 minutes: observed only.
        self.assertEqual(m["P5 credential miss p95 / p99 (P3's target)"]["verdict"], stats.NOT_JUDGED)

    def test_ratios(self):
        e = [{"metric": "m", "measured": {"p50": 10, "p95": 20, "p99": 40}}]
        s = [{"metric": "m", "measured": {"p50": 30, "p95": 50, "p99": 80}}]
        self.assertEqual(judge.ratios(e, s), {"m": {"p50": 3.0, "p95": 2.5, "p99": 2.0}})


class ConditionsTest(unittest.TestCase):
    def record(self, **server):
        eks = {"dataTokens": "aws-sts", "database": {"class": "db.t4g.small", "multiAz": False}, "replicas": 2,
               "jdbcPoolSize": "16", "pods": [{"resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                                             "limits": {"cpu": "2", "memory": "2Gi"}}}] * 2}
        sl = {"dataTokens": "aws-sts", "database": {"class": "db.t4g.small", "multiAz": False},
              "server": dict({"memoryMb": 2048, "architectures": ["x86_64"], "reservedConcurrency": 10,
                              "snapStart": "None"}, **server)}
        nodes = [{"pool": "catalog", "instanceType": "t3.large"}] * 2 + [{"pool": "clients", "instanceType": "t3.xlarge"}]
        return {"installs": {"eks": eks, "serverless": sl}, "nodes": nodes}

    def test_the_default_sizes(self):
        from perflib.scenarios import conditions

        self.assertTrue(all(v["ok"] for v in conditions("P5", self.record()).values()))
        # P4b needs SnapStart on, every other scenario off; a bigger function is not the terms' size.
        self.assertFalse(conditions("P4b", self.record())["serverless: SnapStart on"]["ok"])
        self.assertTrue(conditions("P4b", self.record(snapStart="PublishedVersions"))["serverless: SnapStart on"]["ok"])
        bigger = conditions("P1a", self.record(memoryMb=4096))
        self.assertFalse(bigger["serverless: the server function at 2048 MB, x86_64"]["ok"])


class EvidenceTest(unittest.TestCase):
    def test_kept_json_survives_the_sanitizer(self):
        from compatlib.sanitize import Sanitizer
        from perflib.context import dumps

        # A mean whose fraction is exactly twelve digits long, as the pilot's were: the sanitizer would
        # take them for an account ID and leave the JSON broken.
        value = {"mean": 910.123456789012, "series": [{"Average": 6.212632352449981}], None: 1, "n": 1226}
        kept = json.loads(Sanitizer().text(dumps(value)))
        self.assertEqual(kept["mean"], 910.123457)
        self.assertEqual(kept["n"], 1226)
        self.assertEqual(kept["null"], 1)


class CleanupTest(unittest.TestCase):
    class Target:
        form = "eks"
        databases = ["perf_a_r1", "perf_b_r1"]

        class install:
            bucket, prefix = "b", "w"

        def __init__(self, listed):
            self.listed, self.asked = listed, []

        def aws(self, *args):
            prefix = args[args.index("--prefix") + 1]
            self.asked.append(prefix)
            return {"Contents": [o for o in self.listed if o["Key"].startswith(prefix)]}

    def files_left(self, listed):
        class Run:
            args = type("Args", (), {"duration_scale": 1.0})()

        sc = scenarios.Scenario(Run())
        target = self.Target(listed)
        return sc.files_left(target), sc.extra["directoryMarkersLeft"], target.asked

    def test_nothing_left(self):
        self.assertEqual(self.files_left([]), ({}, {"eks": 0}, ["w/perf_a_r1.db/", "w/perf_b_r1.db/"]))

    def test_a_directory_marker_is_left_too(self):
        # The server removes a dropped database's S3A marker since #13: one left fails the clean-up.
        left, markers, _ = self.files_left([{"Key": "w/perf_a_r1.db/", "Size": 0},
                                            {"Key": "w/perf_b_r1.db/t/data-0.orc", "Size": 10}])
        self.assertEqual(left, {"perf_a_r1": 1, "perf_b_r1": 1})
        self.assertEqual(markers, {"eks": 1})


class ReportTest(unittest.TestCase):
    def test_summary_names_every_metric(self):
        body = {"scenario": "P1a", "run": "p1", "verdict": "PASS", "reasons": [], "reduced": True,
                "note": "reduced", "durationScale": 0.2, "started": "s", "finished": "f",
                "environment": {"harnessCommit": "abc", "region": "us-west-2", "installs": {"eks": {"env": "e"}}},
                "forms": {"eks": {"verdict": "PASS", "metrics": [
                    stats.judge_latency("lat", [1.0] * 1000, {"p95": 2, "p99": 3}),
                    stats.judge_at_least("rate", 12.0, 10), stats.observed("obs", {"a": 1})]}},
                "extra": {}, "cleanup": {"status": "PASS", "steps": [{"status": "PASS", "what": "x", "detail": "y"}]}}
        text = report.summary(body)
        for needle in ("| lat | 1,000 |", "| rate |", "obs (observed)", "Clean-up: PASS", "duration scale 0.2;"):
            self.assertIn(needle, text)
        json.dumps(body)


if __name__ == "__main__":
    unittest.main()
