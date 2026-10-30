"""UAT-DEP-10's judging, offline."""

import unittest

from cases import dep10


def record(snapshots, tables=None):
    return {"snapshots": snapshots, "tables": tables or {}}


class Restore(unittest.TestCase):
    def test_listing_reads_and_history_as_at_the_point(self):
        self.assertTrue(dep10.judge_lists(0, ["t2", "gone", "t1"])[0])
        self.assertFalse(dep10.judge_lists(0, ["t1", "t2", "t3", "gone"])[0])
        self.assertFalse(dep10.judge_lists(1, ["gone", "t1", "t2"])[0])
        point = record({"t1": {1: {"plannedRows": 10}}, "t2": {1: {"plannedRows": 10}}},
                       {"t1": {"rows": 10, "checksum": "a"}, "t2": {"rows": 10, "checksum": "b"}})
        self.assertTrue(dep10.judge_reads(point, point)[0])
        self.assertTrue(dep10.judge_history(point, point)[0])
        other = record({"t1": {1: {"plannedRows": 11}}, "t2": {1: {"plannedRows": 10}}},
                       {"t1": {"rows": 11, "checksum": "c"}, "t2": {"rows": 10, "checksum": "b"}})
        self.assertFalse(dep10.judge_reads(other, point)[0])
        self.assertFalse(dep10.judge_history(other, point)[0])

    def test_repointed_needs_the_run_the_new_endpoint_and_the_users_signed_in(self):
        self.assertTrue(dep10.judge_repointed(0, "new.example.com", "new.example.com", (True, "signed in"))[0])
        self.assertFalse(dep10.judge_repointed(1, "new.example.com", "new.example.com", (True, ""))[0])
        self.assertFalse(dep10.judge_repointed(0, "old.example.com", "new.example.com", (True, ""))[0])
        self.assertFalse(dep10.judge_repointed(0, "new.example.com", "new.example.com", (False, ""))[0])

    def test_the_users_signed_in(self):
        self.assertTrue(dep10.judge_signed_in("eks", "colvenna_catalog", ["colvenna_catalog"])[0])
        self.assertFalse(dep10.judge_signed_in("eks", "colvenna_catalog", [])[0])
        both = ["colvenna_catalog_authz", "colvenna_catalog"]
        self.assertTrue(dep10.judge_signed_in("serverless", "colvenna_catalog", both)[0])
        self.assertFalse(dep10.judge_signed_in("serverless", "colvenna_catalog", ["colvenna_catalog"])[0])

    def test_each_guarded_run_refused_naming_the_stacks_database_and_changing_nothing(self):
        state = {"stacks": {"Storage": ("UPDATE_COMPLETE", "t0")}, "revision": 7}
        refused = ("without COLVENNA_BUCKET", 3, "deleting it (old.example.com). Set COLVENNA_BUCKET=b", state, state)
        create = ("COLVENNA_DB=create", 3, "move the install back to old.example.com", state, state)
        self.assertTrue(dep10.judge_guarded([refused, create], "old.example.com")[0])
        self.assertFalse(dep10.judge_guarded([], "old.example.com")[0])
        self.assertFalse(dep10.judge_guarded([refused[:1] + (0,) + refused[2:], create], "old.example.com")[0])
        self.assertFalse(dep10.judge_guarded([refused, create[:2] + ("refused",) + create[3:]], "old.example.com")[0])
        moved = dict(state, revision=8)
        self.assertFalse(dep10.judge_guarded([refused, create[:4] + (moved,)], "old.example.com")[0])

    def test_an_error_answer_is_no_finding(self):
        found = dep10.judge_check({"status": 500, "error": "No schema file in s3://b/w/d.db/gone"})
        self.assertTrue(found.startswith("no report: 500"), found)
        self.assertFalse(dep10.judge_found({"status": 500, "error": "No schema file"}, "missing", "gone")[0])

    def test_what_the_consistency_check_finds(self):
        self.assertEqual(dep10.judge_check({"consistent": True}), "consistent")
        self.assertEqual(dep10.judge_check({"consistent": False, "problems": [
            "main: snapshot file 11 exists beyond the published pointer 10"]}), "behind")
        self.assertEqual(dep10.judge_check({"consistent": False, "problems": ["main: pointer snapshot 3 has no file"]}),
                         "missing")
        # A table whose files are gone (#75): no schema file, and the pointer's snapshot without one.
        gone = ["no schema file in s3://b/w/d.db/gone, where the catalog serves schema 0",
                "main: pointer snapshot 3 has no file"]
        self.assertEqual(dep10.judge_check({"consistent": False, "problems": gone}), "missing")
        self.assertEqual(dep10.judge_check({"consistent": False, "problems": gone[:1]}), "missing")
        self.assertTrue(dep10.judge_found({"consistent": True}, "consistent", "t2")[0])
        self.assertFalse(dep10.judge_found({"consistent": True}, "behind", "t1")[0])

    def test_in_line_history_writes_identities(self):
        self.assertTrue(dep10.judge_in_line({t: {"consistent": True} for t in ("t1", "t2", "t3")})[0])
        self.assertFalse(dep10.judge_in_line({t: {"consistent": True} for t in ("t1", "t2")})[0])
        before = record({"t1": {1: {"plannedRows": 1}, 2: {"plannedRows": 2}}, "t3": {1: {"plannedRows": 1}}})
        self.assertTrue(dep10.judge_history_before(before, before)[0])
        self.assertFalse(dep10.judge_history_before(record({"t1": {1: {"plannedRows": 1}}, "t3": {1: {"plannedRows": 1}}}), before)[0])
        self.assertTrue(dep10.judge_takes_writes(0, 0)[0])
        self.assertFalse(dep10.judge_takes_writes(0, 1)[0])
        self.assertTrue(dep10.judge_identities({"admin"}, [1, 2], "late", 9)[0])
        self.assertFalse(dep10.judge_identities({"admin", "late"}, [1, 2], "late", 9)[0])
        self.assertFalse(dep10.judge_identities({"admin"}, [1, 9], "late", 9)[0])


if __name__ == "__main__":
    unittest.main()
