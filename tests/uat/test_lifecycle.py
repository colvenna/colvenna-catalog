"""The helpers of the upgrade, going-back and restore cases, offline: the loops' and the records' judges, and the installer's steps (uatlib/lifecycle.py, uatlib/installer.py)."""

import unittest

from uatlib import installer, lifecycle

T0 = 1_000_000.0


def at(seconds):
    return lifecycle.stamp(T0 + seconds)


def write(t, ok=True, snapshot=None):
    return {"kind": "write", "time": at(t), "ok": ok, "snapshot": snapshot}


class Loops(unittest.TestCase):
    def facts(self, writes, reads=None, tokens=None):
        return {"writer": writes, "reader": reads or [{"kind": "read", "time": at(t), "ok": True, "table": "x",
                                                         "snapshot": 1} for t in range(0, 121, 10)],
                "credentials": tokens or [{"kind": "token", "time": at(t), "ok": True, "table": "x"}
                                          for t in range(0, 121, 10)]}

    def test_uninterrupted(self):
        writes = [write(t, snapshot=4 + i) for i, t in enumerate(range(0, 121, 10))]
        out = lifecycle.judge_loops(self.facts(writes), T0, T0 + 120)
        self.assertTrue(all(ok for ok, _ in out.values()), out)

    def test_a_failure_a_gap_or_a_repeated_snapshot_fails(self):
        failed = [write(0, snapshot=1), write(10, ok=False), write(20, snapshot=2)]
        self.assertFalse(lifecycle.judge_loops(self.facts(failed), T0, T0 + 30)["writes-uninterrupted"][0])
        gap = [write(0, snapshot=1), write(90, snapshot=2), write(100, snapshot=3)]
        self.assertFalse(lifecycle.judge_loops(self.facts(gap), T0, T0 + 100)["writes-uninterrupted"][0])
        twice = [write(0, snapshot=1), write(10, snapshot=1), write(20, snapshot=2)]
        self.assertFalse(lifecycle.judge_loops(self.facts(twice), T0, T0 + 20)["writes-uninterrupted"][0])

    def test_a_commit_made_again_after_a_failure_fills_its_gap_but_the_failure_still_fails(self):
        writes = [write(0, snapshot=1), write(5, ok=False),
                  {"kind": "write-resolved", "time": at(6), "number": 2, "committedNow": 1}, write(10, snapshot=3)]
        ok, detail = lifecycle.judge_loops(self.facts(writes), T0, T0 + 10)["writes-uninterrupted"]
        self.assertFalse(ok)
        self.assertIn("1 of them committed again", detail)
        self.assertIn("contiguous but for those: True", detail)
        unexplained = [write(0, snapshot=1), write(5, ok=False), write(10, snapshot=3)]
        detail = lifecycle.judge_loops(self.facts(unexplained), T0, T0 + 10)["writes-uninterrupted"][1]
        self.assertIn("contiguous but for those: False", detail)

    def test_a_read_older_than_the_one_before_fails(self):
        reads = [{"kind": "read", "time": at(0), "ok": True, "table": "x", "snapshot": 5},
                 {"kind": "read", "time": at(10), "ok": True, "table": "x", "snapshot": 4}]
        writes = [write(0, snapshot=1), write(10, snapshot=2)]
        out = lifecycle.judge_loops(self.facts(writes, reads=reads), T0, T0 + 10)
        self.assertFalse(out["reads-uninterrupted"][0])


class Records(unittest.TestCase):
    def rec(self, schema, snapshots, tokens=None):
        return {"schema": schema, "snapshots": snapshots, "principals": {"p": {"name": "p"}},
                "tokens": tokens or {"p": [("t1", 1, None)]}, "grants": {"p": [(1,)]}}

    def test_migrations_forward(self):
        b = self.rec([("1", "V1"), ("2", "V2")], {})
        self.assertTrue(lifecycle.migration_forward(b, b, ["V1", "V2"])[0])
        self.assertTrue(lifecycle.migration_forward(b, self.rec([("1", "V1"), ("2", "V2"), ("3", "V3")], {}),
                                                    ["V1", "V2", "V3"])[0])
        self.assertFalse(lifecycle.migration_forward(b, self.rec([("1", "V1")], {}), ["V1", "V2"])[0])

    def test_data_unchanged_but_for_the_writer(self):
        s = {"events": {1: {"plannedRows": 20, "commitUser": "w"}}}
        b = self.rec([], s)
        grown = self.rec([], {"events": {1: {"plannedRows": 20, "commitUser": "w"}, 2: {"plannedRows": 30, "commitUser": "uat-loop-writer"}}})
        self.assertTrue(lifecycle.data_unchanged(b, grown, writer_table="events")[0])
        other = self.rec([], {"events": {1: {"plannedRows": 20, "commitUser": "w"}, 2: {"plannedRows": 30, "commitUser": "someone"}}})
        self.assertFalse(lifecycle.data_unchanged(b, other, writer_table="events")[0])
        changed = self.rec([], {"events": {1: {"plannedRows": 21, "commitUser": "w"}}})
        self.assertFalse(lifecycle.data_unchanged(b, changed, writer_table="events")[0])

    def test_the_writers_table_expired_by_paimon(self):
        """#56: a table the loops write to for over an hour loses its earliest snapshots to Paimon's own
        expiry; any other snapshot missing, or one of another table, is still a problem."""
        snap = {"plannedRows": 20, "commitUser": "w"}
        b = self.rec([], {"events": {"1": snap, "2": snap, "3": snap}, "orders": {"1": snap}})
        later = self.rec([], {"events": {"3": snap, "4": {"plannedRows": 30, "commitUser": "uat-loop-writer"}},
                              "orders": {"1": snap}})
        ok, detail = lifecycle.data_unchanged(b, later, writer_table="events")
        self.assertTrue(ok, detail)
        self.assertIn("expired by Paimon meanwhile (the earliest of the writer's table): 2 ['events 1', 'events 2']", detail)
        hole = self.rec([], {"events": {"1": snap, "3": snap}, "orders": {"1": snap}})
        self.assertFalse(lifecycle.data_unchanged(b, hole, writer_table="events")[0])  # not the earliest
        self.assertFalse(lifecycle.data_unchanged(b, self.rec([], {"events": later["snapshots"]["events"], "orders": {}}),
                                                  writer_table="events")[0])  # another table's
        self.assertFalse(lifecycle.data_unchanged(b, later, writer_table=None)[0])  # no writer: nothing expires
        self.assertEqual(lifecycle.expired({"1": snap}, {}), [])

    def test_identities_with_a_new_token(self):
        b = self.rec([], {})
        a = self.rec([], {}, tokens={"p": [("t1", 1, None), ("t2", 2, None)]})
        self.assertTrue(lifecycle.identities_unchanged(b, a, new_tokens=("t2",))[0])
        self.assertFalse(lifecycle.identities_unchanged(b, a)[0])


class Checkouts(unittest.TestCase):
    def test_steps_done(self):
        out = "== tools\n== preflight\n== smoke\nColvenna Catalog x is installed in c/n; evidence in e\n"
        self.assertTrue(installer.steps_done(out)[0])
        self.assertFalse(installer.steps_done(out + 'install.sh: failed at step "smoke" (status 1)\n')[0])
        self.assertFalse(installer.steps_done("== tools\n")[0])


class Requests(unittest.TestCase):
    def test_half_a_memory_quantity(self):
        from uatlib.tools import half

        self.assertEqual(half("1536Mi"), "768Mi")
        self.assertEqual(half("2Gi"), "1024Mi")


class Snapshots(unittest.TestCase):
    def test_rows_compared_where_both_records_read_them(self):
        read = {"exists": True, "commitKind": "APPEND", "commitUser": "w", "plannedRows": 10, "rows": 10, "checksum": "a"}
        planned = dict(read, rows=None, checksum=None)
        self.assertTrue(lifecycle.same_snapshot(read, planned))
        self.assertTrue(lifecycle.same_snapshot(read, read))
        self.assertFalse(lifecycle.same_snapshot(read, dict(read, checksum="b")))
        self.assertFalse(lifecycle.same_snapshot(read, dict(planned, plannedRows=11)))
        self.assertFalse(lifecycle.same_snapshot(read, None))

class Kept(unittest.TestCase):
    def test_a_kept_record_loads_as_recorded(self):
        import json
        import tempfile
        rec = {"time": 1.0, "schema": [("1", "V1__catalog.sql")], "snapshots": {"t": {1: {"plannedRows": 10}}},
               "tokens": {"p": [("id1", 5, None)]}, "grants": {"p": [(7, "DATABASE", "d", None, "READ", "ALLOW", None)]}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
        back = lifecycle.load_record(f.name)
        self.assertEqual(back["snapshots"], rec["snapshots"])
        self.assertEqual(back["tokens"], rec["tokens"])
        self.assertEqual(back["grants"], rec["grants"])
        self.assertEqual(back["schema"], rec["schema"])

    def test_the_configuration_pointed_at_a_restored_instance(self):
        import tempfile
        from pathlib import Path

        class Install:
            def output(self, part, key):
                return {"DbAdminSecretArn": "<ARN>", "BucketName": "made-bucket"}[key]

        class Ctx:
            install = Install()
            kept = {}

            def keep(self, name, text):
                self.kept[name] = text

        d = Path(tempfile.mkdtemp())
        original = d / "e1.env"
        original.write_text("COLVENNA_ENV=e1\nCOLVENNA_DB=create\nCOLVENNA_NEW_KEY=x\n")
        ctx = Ctx()
        path = lifecycle.restored_config(ctx, "new.example.com", original, d / "e1-restored.env")
        text = path.read_text()
        self.assertIn("COLVENNA_DB=existing", text)
        self.assertIn("COLVENNA_DB_ENDPOINT=new.example.com", text)
        self.assertIn("COLVENNA_BUCKET=made-bucket", text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        without = lifecycle.restored_config(ctx, "new.example.com", original, d / "w.env", bucket=False).read_text()
        self.assertNotIn("COLVENNA_BUCKET", without)
        older = lifecycle.restored_config(ctx, "new.example.com", original, d / "o.env",
                                          known={"COLVENNA_ENV", "COLVENNA_DB", "COLVENNA_DB_ENDPOINT"}).read_text()
        self.assertNotIn("COLVENNA_NEW_KEY", older)
        self.assertIn("COLVENNA_DB_ENDPOINT=new.example.com", older)


if __name__ == "__main__":
    unittest.main()
