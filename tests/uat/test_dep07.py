"""UAT-DEP-07's judging, offline."""

import argparse
import tempfile
import unittest
from pathlib import Path

from cases import dep07

MANIFEST = {"image": {"digest": "sha256:" + "a" * 64}}


def checkout(versions):
    d = Path(tempfile.mkdtemp())
    (d / "install.sh").write_text("")
    java = d / "src" / "main" / "java" / "com" / "colvenna" / "catalog" / "db"
    java.mkdir(parents=True)
    (java / "Migrations.java").write_text(
        "static final List<String> VERSIONS = List.of(" + ", ".join(f'"{v}"' for v in versions) + ");")
    return str(d)


class Branch(unittest.TestCase):
    def test_from_the_two_checkouts_migrations(self):
        same = argparse.Namespace(candidate=checkout(["V1", "V2"]), previous=checkout(["V1", "V2"]))
        newer = argparse.Namespace(candidate=checkout(["V1", "V2", "V3"]), previous=same.previous)
        self.assertEqual(dep07.branch(same), "same-schema")
        self.assertEqual(dep07.branch(newer), "newer-schema")
        self.assertEqual(dep07.checks("eks", same), dep07.SAME + ["forward-again"])
        self.assertEqual(dep07.checks("eks", newer), dep07.NEWER + ["forward-again"])
        self.assertEqual(dep07.checks("eks", argparse.Namespace(config=None)), dep07.SAME + dep07.NEWER + ["forward-again"])


class Judges(unittest.TestCase):
    def test_the_previous_release_serves(self):
        pods = [("p1", "r@sha256:" + "a" * 64, "v1.0.0-rc.1", "x")]
        self.assertTrue(dep07.judge_previous_serves("eks", pods, MANIFEST, "h")[0])
        self.assertFalse(dep07.judge_previous_serves("eks", [("p1", "r@sha256:" + "f" * 64, None, "x")], MANIFEST, "h")[0])
        self.assertTrue(dep07.judge_previous_serves("eks", [("p1", "i", None, "h")], None, "h")[0])
        self.assertTrue(dep07.judge_previous_serves("serverless", [("3", "r@sha256:z", ["h", "latest"])], None, "h")[0])
        self.assertFalse(dep07.judge_previous_serves("serverless", [("3", "r@sha256:z", ["other"])], None, "h")[0])

    def test_rolled_back_and_uninterrupted(self):
        self.assertTrue(dep07.judge_rolled_back(0, "helm rollback", (True, "ok"))[0])
        self.assertFalse(dep07.judge_rolled_back(1, "helm rollback", (True, "ok"))[0])
        self.assertTrue(dep07.judge_uninterrupted({"a": (True, ""), "b": (True, "")})[0])
        self.assertFalse(dep07.judge_uninterrupted({"a": (True, ""), "b": (False, "")})[0])
        self.assertFalse(dep07.judge_data_kept((True, ""), (False, ""))[0])

    def test_the_data_kept(self):
        self.assertTrue(dep07.judge_data_kept((True, ""), (True, ""))[0])

    def test_the_newer_schema_refused_naming_the_versions(self):
        log = "WARN Catalog schema is at version 3, this server needs 2; refusing to serve an older server on a newer schema"
        self.assertTrue(dep07.names_versions(log, (3, 2)))
        self.assertTrue(dep07.names_versions("Catalog schema version 3 is newer than this server (2); refusing", (3, 2)))
        self.assertFalse(dep07.names_versions("version 3\nand 2 elsewhere", (3, 2)))
        self.assertTrue(dep07.judge_refused("eks", 1, 0, log, (3, 2), None, None)[0])
        self.assertFalse(dep07.judge_refused("eks", 1, 1, log, (3, 2), None, None)[0])
        self.assertFalse(dep07.judge_refused("eks", 1, 0, "refused", (3, 2), None, None)[0])
        self.assertTrue(dep07.judge_refused("serverless", 1, None, log, (3, 2), "7", "7")[0])
        self.assertFalse(dep07.judge_refused("serverless", 1, None, log, (3, 2), "7", "8")[0])
        self.assertFalse(dep07.judge_refused("serverless", 0, None, log, (3, 2), "7", "7")[0])

    def test_nothing_written_and_restored_as_at_the_snapshot(self):
        self.assertTrue(dep07.judge_nothing_written(([1], [2]), ([1], [2]))[0])
        self.assertFalse(dep07.judge_nothing_written(([1], [2]), ([1], [3]))[0])
        point = {"listing": ["orders", "events"], "snapshots": {"orders": {1: {"plannedRows": 20}},
                                                                 "events": {1: {"plannedRows": 20}}}}
        self.assertTrue(dep07.judge_as_at_snapshot(point, point, ["orders", "events"])[0])
        later = {"listing": ["orders", "events"], "snapshots": {"orders": {1: {"plannedRows": 20}},
                                                                 "events": {1: {"plannedRows": 20}, 2: {"plannedRows": 30}}}}
        self.assertFalse(dep07.judge_as_at_snapshot(later, point, ["orders", "events"])[0])
        self.assertFalse(dep07.judge_as_at_snapshot(point, None, ["orders"])[0])
        ok = (True, "")
        self.assertTrue(dep07.judge_restored(0, ok, ok, ok, [True, True], 900)[0])
        self.assertFalse(dep07.judge_restored(0, ok, ok, ok, [True, False], 900)[0])
        self.assertFalse(dep07.judge_restored(0, ok, (False, ""), ok, [True, True], 900)[0])
        self.assertFalse(dep07.judge_restored(0, ok, ok, (False, ""), [True, True], 900)[0])

    def test_forward_again(self):
        ok = (True, "")
        good = (0, ok, ok, ["1", "2"], ["V1", "V2"], ok, ok, ok, ok, ok)
        self.assertTrue(dep07.judge_forward_again(*good)[0])
        self.assertFalse(dep07.judge_forward_again(0, ok, (False, ""), ["1", "2"], ["V1", "V2"], ok, ok, ok, ok, ok)[0])
        self.assertFalse(dep07.judge_forward_again(0, ok, ok, ["1"], ["V1", "V2"], ok, ok, ok, ok, ok)[0])
        for i in range(5, 10):
            failing = list(good)
            failing[i] = (False, "")
            self.assertFalse(dep07.judge_forward_again(*failing)[0])


if __name__ == "__main__":
    unittest.main()
