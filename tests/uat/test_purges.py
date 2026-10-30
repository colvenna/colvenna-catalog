"""The harness's side of #69, offline: a consistency check's pages followed to the whole report, and
the purge command's summary read from its log."""

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


if __name__ == "__main__":
    unittest.main()
