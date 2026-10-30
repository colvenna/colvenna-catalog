"""The harness's side of #69, offline: a consistency check's pages followed to the whole report, and
the purge command's summary read from its log; and #13's: a dropped database's directory marker found."""

import unittest

from uatlib import purges
from uatlib.admin import Admin, AdminError


class FakeProbe:
    """Answers each plan's one step from a list, and keeps the URLs it was asked for."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.urls = []

    def run(self, plan, tokens=None, keep=None, timeout=None):
        self.urls.append(plan[0]["url"])
        return [self.answers.pop(0)]


def page(token, **report):
    return {"status": 200, "body": dict(report, nextPageToken=token)}


class Pages(unittest.TestCase):
    def test_the_pages_are_followed_to_the_last(self):
        probe = FakeProbe([page("a/b+c", consistent=False), page("d", consistent=False),
                           page(None, consistent=True, problems=[])])
        admin = Admin(probe, "https://catalog/colvenna/admin/v1", "colvenna_x", "UAT-X")
        result = admin.consistency_report("db", "t")
        self.assertEqual(3, result["pages"])
        self.assertTrue(result["body"]["consistent"])
        self.assertEqual("https://catalog/colvenna/admin/v1/consistency/db/t", probe.urls[0])
        self.assertTrue(probe.urls[1].endswith("/consistency/db/t?pageToken=a%2Fb%2Bc"))
        self.assertTrue(probe.urls[2].endswith("?pageToken=d"))
        probe.answers.append(page(None, consistent=True))
        self.assertTrue(admin.consistency("db", "t")["consistent"])

    def test_an_answer_other_than_200_ends_it(self):
        probe = FakeProbe([page("a"), {"status": 504, "body": {"message": "Endpoint request timed out"}}])
        admin = Admin(probe, "https://catalog/colvenna/admin/v1", "colvenna_x", "UAT-X")
        result = admin.consistency_report("db", "t")
        self.assertEqual(504, result["status"])
        self.assertEqual(2, result["pages"])
        probe.answers.append({"status": 504, "body": {}})
        with self.assertRaises(AdminError):
            admin.consistency("db", "t")


class Summary(unittest.TestCase):
    def test_the_last_json_line_is_the_summary(self):
        log = ("15:00:01 INFO TablePurges Purged the files of a dropped table at s3://b/w/db.db/t\n"
               '{"purged":1,"failed":0,"pending":0,"schemaVersion":3}\n')
        self.assertEqual({"purged": 1, "failed": 0, "pending": 0, "schemaVersion": 3}, purges.summary(log))
        self.assertIsNone(purges.summary("Unknown command purge\n"))
        self.assertIsNone(purges.summary(""))


class DirectoryMarker(unittest.TestCase):
    class Ctx:
        class install:
            bucket, prefix = "b", "w"

        def __init__(self, keys):
            self.keys, self.asked = keys, []

        def aws(self, *args):
            self.asked.append(args)
            prefix = args[args.index("--prefix") + 1]
            return {"Contents": [{"Key": k} for k in sorted(self.keys) if k.startswith(prefix)][:1]}

    def test_the_marker_is_found_and_nothing_else_counts(self):
        ctx = self.Ctx(["w/d.db/", "w/d.db/t/data-0.orc"])
        self.assertTrue(purges.directory_marker_left(ctx, "d"))
        self.assertEqual(("s3api", "list-objects-v2", "--bucket", "b", "--prefix", "w/d.db/", "--max-items", "1"),
                         ctx.asked[0])
        self.assertFalse(purges.directory_marker_left(self.Ctx(["w/d.db/t/data-0.orc", "w/d.dbx/"]), "d"))
        self.assertFalse(purges.directory_marker_left(self.Ctx([]), "d"))


if __name__ == "__main__":
    unittest.main()
