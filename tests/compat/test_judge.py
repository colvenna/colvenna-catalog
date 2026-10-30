"""From raw output and the access log to verdicts: nothing passes without its evidence."""

import json
import unittest

from compatlib import accesslog, judge

DB = "compat_20261028000000_ab12_flink120_p210"
PREFIX = "compat_20261028000000_ab12_"


def result(table, op, status="PASS", detail="ok"):
    return "COMPAT-RESULT " + json.dumps({"table": table, "op": op, "status": status, "detail": detail})


def flow(**overrides):
    out = []
    for table in ("pk", "pk-dv", "append"):
        for op in ("ddl", "write", "read", "credentials"):
            status = overrides.get(f"{table}/{op}", "PASS")
            if status is not None:
                out.append(result(table, op, status))
    return "\n".join(["some engine log line"] + out)


def log_line(path, status=200, action="GET_TABLE", method="GET", principal="writer", t="2026-10-28T08:00:01.000Z"):
    return (f"{t} INFO  [virtual-1] colvenna.access - id {method} {path} {status} 3ms "
            f"principal={principal} action={action}")


def access(*extra):
    lines = []
    for table, commits in (("t_pk", 3), ("t_pk_dv", 7), ("t_append", 2)):
        base = f"/v1/colvenna/databases/{DB}/tables/{table}"
        lines += [log_line(base + "/commit", action="COMMIT", method="POST")] * commits
        lines += [log_line(base + "/token", action="ISSUE_DATA_TOKEN")]
    lines += [log_line("/v1/config", action="GET_CONFIG"), *extra]
    return accesslog.parse("\n".join(lines))


class Writer(unittest.TestCase):
    def test_all_evidence_passes(self):
        v = judge.writer(flow(), "", access(), DB, PREFIX)
        for table in ("pk", "pk-dv", "append"):
            for op in ("ddl", "write", "read", "credentials", "endpoints"):
                self.assertEqual(v[table][op]["status"], "PASS", (table, op, v[table][op]))
        self.assertIn("3 commits through the catalog", v["pk"]["write"]["detail"])

    def test_a_missing_result_fails(self):
        v = judge.writer(flow(**{"pk/read": None}), "", access(), DB, PREFIX)
        self.assertEqual(v["pk"]["read"]["status"], "FAIL")
        self.assertIn("printed no result", v["pk"]["read"]["detail"])

    def test_a_flow_that_could_not_run(self):
        text = 'COMPAT-FATAL {"detail": "ValidationException: Unable to create catalog"}'
        v = judge.writer(text, "", access(), DB, PREFIX)
        self.assertEqual(v["append"]["ddl"]["status"], "FAIL")
        self.assertIn("Unable to create catalog", v["append"]["ddl"]["detail"])

    def test_writes_that_never_reached_the_catalog(self):
        requests = [r for r in access() if r.action != "COMMIT" or "t_append" not in r.path]
        v = judge.writer(flow(), "", requests, DB, PREFIX)
        self.assertEqual(v["append"]["write"]["status"], "FAIL")

    def test_credentials_need_the_catalogs_tokens(self):
        requests = [r for r in access() if r.action != "ISSUE_DATA_TOKEN" or "t_pk/" not in r.path + "/"]
        v = judge.writer(flow(), "", requests, DB, PREFIX)
        self.assertEqual(v["pk"]["credentials"]["status"], "FAIL")
        self.assertEqual(v["pk-dv"]["credentials"]["status"], "PASS")

    def test_credentials_need_the_write_and_the_read(self):
        v = judge.writer(flow(**{"pk/read": "FAIL"}), "", access(), DB, PREFIX)
        self.assertEqual(v["pk"]["credentials"]["status"], "FAIL")

    def test_an_endpoint_the_catalog_does_not_serve(self):
        extra = log_line(f"/v1/colvenna/databases/{DB}/tables/t_pk/schemas/0", status=404, action="-")
        v = judge.writer(flow(), "", access(extra), DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "FAIL")
        self.assertIn("schemas/0", v["pk"]["endpoints"]["detail"])
        self.assertEqual(v["append"]["endpoints"]["status"], "PASS")

    def test_a_501_unless_by_design(self):
        declined = log_line(f"/v1/colvenna/databases/{DB}/tables/t_append/partitions", status=501,
                            action="LIST_PARTITIONS")
        renamed = log_line(f"/v1/colvenna/databases/{DB}/tables/t_pk/branches/b/rename", status=501,
                           action="RENAME_BRANCH", method="POST")
        v = judge.writer(flow(), "", access(declined, renamed), DB, PREFIX)
        self.assertEqual(v["append"]["endpoints"]["status"], "PASS")
        self.assertIn("LIST_PARTITIONS", v["append"]["endpoints"]["detail"])
        self.assertEqual(v["pk"]["endpoints"]["status"], "FAIL")

    def test_a_shared_unserved_request_fails_every_table(self):
        v = judge.writer(flow(), "", access(log_line("/v1/colvenna/semantic-views", 404, "-")), DB, PREFIX)
        self.assertTrue(all(v[t]["endpoints"]["status"] == "FAIL" for t in ("pk", "pk-dv", "append")))

    def test_another_cells_requests_are_not_this_ones(self):
        other = log_line(f"/v1/colvenna/databases/{PREFIX}spark35_p200/tables/t_pk/schemas/0", 404, "-")
        v = judge.writer(flow(), "", access(other), DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "PASS")

    def test_another_runs_requests_are_not_this_ones(self):
        other = log_line("/v1/colvenna/databases/compat_20261028111111_cd34_flink22_p210/tables/t_pk/x", 404, "-")
        v = judge.writer(flow(), "", access(other), DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "PASS")

    def test_an_empty_log_is_no_pass(self):
        v = judge.writer(flow(), "", [], DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "FAIL")
        self.assertEqual(v["pk"]["write"]["status"], "FAIL")

    def test_without_the_log(self):
        v = judge.writer(flow(), "", None, DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "NOT_RUN")
        self.assertEqual(v["pk"]["credentials"]["status"], "PASS")
        self.assertIn("not counted", v["pk"]["credentials"]["detail"])

    def test_unauthenticated_requests_are_not_the_engines(self):
        anonymous = log_line("/v1/config", status=401, action="-", principal="-")
        v = judge.writer(flow(), "", access(anonymous), DB, PREFIX)
        self.assertEqual(v["pk"]["endpoints"]["status"], "PASS")


def read(table, check, expected, output, rc=0, error=""):
    return "COMPAT-READ " + json.dumps({"table": table, "check": check, "expected": expected, "output": output,
                                        "rc": rc, "error": error})


READER_DB = "compat_20261028000000_ab12_fixture"
PROBE = {t: ("PASS", "refused") for t in ("pk", "pk-dv", "append")}


def reader_log():
    lines = [log_line(f"/v1/colvenna/databases/{READER_DB}/tables/{t}/token", action="ISSUE_DATA_TOKEN",
                      principal="reader") for t in ("t_pk", "t_pk_dv", "t_append")]
    return accesslog.parse("\n".join(lines))


class Reader(unittest.TestCase):
    def text(self, dv_error=None):
        lines = ['COMPAT-READ {"table": "-", "check": "create-catalog", "rc": 0, "output": "", "error": ""}',
                 read("pk", "rows", 105, "105"), read("append", "rows", 120, "120")]
        lines.append(read("pk-dv", "rows", 105, "", 1, dv_error) if dv_error else read("pk-dv", "rows", 105, "105"))
        return "\n".join(lines + ["COMPAT-READ-DONE {}"])

    def test_reads_pass_on_their_values(self):
        v = judge.reader("doris", self.text(), "", reader_log(), READER_DB, "compat_20261028000000_ab12_", PROBE)
        self.assertEqual(v["pk"]["read"]["status"], "PASS")
        self.assertEqual(v["pk"]["credentials"]["status"], "PASS")
        self.assertEqual(v["pk"]["ddl"]["status"], "NOT_RUN")

    def test_which_reader_took_the_splits(self):
        text = self.text() + '\nCOMPAT-READ-PLAN {"table": "pk", "rc": 0, "splits": ["paimonNativeReadSplits=0/2"]}'
        v = judge.reader("doris", text, "", reader_log(), READER_DB, "compat_", PROBE)
        self.assertIn("native reader took 0 of 2 splits, the Paimon SDK (JNI) 2", v["pk"]["read"]["detail"])

    def test_the_sdk_alone_is_evidence_not_the_verdict(self):
        text = self.text() + ('\nCOMPAT-READ-JNI {"table": "pk-dv", "check": "rows", "expected": 105, "rc": 1, '
                              '"output": "", "error": "ERROR 1105: cannot read deletion vectors"}')
        v = judge.reader("doris", text, "", reader_log(), READER_DB, "compat_", PROBE)
        self.assertEqual(v["pk-dv"]["read"]["status"], "PASS")
        self.assertIn("force_jni_scanner (the Paimon SDK alone): rows: ERROR 1105", v["pk-dv"]["read"]["detail"])

    def test_a_failed_read_says_why(self):
        v = judge.reader("doris", self.text("ERROR 1105 (HY000): deletion vector\nmore"), "", reader_log(), READER_DB,
                         "compat_", PROBE)
        self.assertEqual(v["pk-dv"]["read"]["status"], "FAIL")
        self.assertIn("deletion vector", v["pk-dv"]["read"]["detail"])
        self.assertEqual(v["pk-dv"]["credentials"]["status"], "FAIL")
        self.assertIn("no query of the table answered", v["pk-dv"]["credentials"]["detail"])

    def test_wrong_values_still_show_how_the_files_were_reached(self):
        text = self.text().replace(read("pk-dv", "rows", 105, "105"), read("pk-dv", "rows", 115, "105"))
        v = judge.reader("doris", text, "", reader_log(), READER_DB, "compat_", PROBE)
        self.assertEqual(v["pk-dv"]["read"]["status"], "FAIL")
        self.assertEqual(v["pk-dv"]["credentials"]["status"], "PASS")
        self.assertIn("its queries answered", v["pk-dv"]["credentials"]["detail"])

    def test_no_catalog_fails_every_read(self):
        text = 'COMPAT-READ {"table": "-", "check": "create-catalog", "rc": 1, "output": "", "error": "ERROR 1105: x"}'
        v = judge.reader("starrocks", text, "", reader_log(), READER_DB, "compat_", PROBE)
        self.assertTrue(all(v[t]["read"]["status"] == "FAIL" for t in ("pk", "pk-dv", "append")))
        self.assertIn("could not be created", v["pk"]["read"]["detail"])

    def test_starrocks_credentials_are_its_own(self):
        v = judge.reader("starrocks", self.text(), "", reader_log(), READER_DB, "compat_", PROBE)
        self.assertEqual(v["pk"]["credentials"]["status"], "NOT_RUN")

    def test_doris_credentials_need_the_probe(self):
        probe = dict(PROBE, pk=("NOT_RUN", "the flow ran without the catalog's per-table credentials"))
        v = judge.reader("doris", self.text(), "", reader_log(), READER_DB, "compat_", probe)
        self.assertEqual(v["pk"]["credentials"]["status"], "FAIL")

    def test_a_reader_that_never_finished(self):
        text = self.text().replace("COMPAT-READ-DONE {}", "")
        v = judge.reader("doris", text, "the main container ran past its deadline", reader_log(), READER_DB,
                         "compat_", PROBE)
        self.assertEqual(v["pk"]["read"]["status"], "FAIL")
        self.assertIn("did not finish (the main container ran past its deadline)", v["pk"]["read"]["detail"])

    def test_a_planned_check_that_never_ran(self):
        v = judge.reader("doris", self.text(), "", reader_log(), READER_DB, "compat_", PROBE,
                         planned={"pk": ["rows", "sum"], "pk-dv": ["rows"], "append": ["rows"]})
        self.assertEqual(v["pk"]["read"]["status"], "FAIL")
        self.assertIn("sum: never ran", v["pk"]["read"]["detail"])
        self.assertEqual(v["append"]["read"]["status"], "PASS")

    def test_endpoints_need_the_tables_own_requests(self):
        log = accesslog.parse("\n".join(log_line(f"/v1/colvenna/databases/{READER_DB}/tables/{t}/token",
                                                  action="ISSUE_DATA_TOKEN", principal="reader")
                                         for t in ("t_pk", "t_append")))
        v = judge.reader("doris", self.text(), "", log, READER_DB, "compat_", PROBE)
        self.assertEqual(v["pk-dv"]["endpoints"]["status"], "FAIL")
        self.assertIn("log is incomplete", v["pk-dv"]["endpoints"]["detail"])
        self.assertEqual(v["pk"]["endpoints"]["status"], "PASS")

    def test_a_reader_that_printed_nothing(self):
        v = judge.reader("doris", "", "the pod's containers did not start", None, READER_DB, "compat_", PROBE)
        self.assertIn("did not start", v["append"]["read"]["detail"])
        self.assertEqual(v["append"]["endpoints"]["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
