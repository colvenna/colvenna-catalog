"""UAT-DEP-06's judging, offline (the loops' and the records' judges are test_lifecycle.py's)."""

import unittest

from cases import dep06

MANIFEST = {"image": {"digest": "sha256:" + "a" * 64}, "source": {"built_from": "b" * 40}}


class Candidate(unittest.TestCase):
    def test_the_pods_run_the_candidates_image_with_its_name_and_commit(self):
        pods = [("p1", "repo@sha256:" + "a" * 64, "v1.0.0-rc.2", "b" * 40)] * 2
        self.assertTrue(dep06.judge_runs_candidate("eks", pods, "v1.0.0-rc.2", MANIFEST, "c" * 40)[0])
        wrong = pods[:1] + [("p2", "repo@sha256:" + "f" * 64, "v1.0.0-rc.2", "b" * 40)]
        self.assertFalse(dep06.judge_runs_candidate("eks", wrong, "v1.0.0-rc.2", MANIFEST, "c" * 40)[0])

    def test_a_source_install_is_no_candidate(self):
        ok, detail = dep06.judge_runs_candidate("eks", [("p1", "x", None, "c" * 40)], None, None, "c" * 40)
        self.assertFalse(ok)
        self.assertIn("from source", detail)

    def test_serverless_by_the_alias_image(self):
        self.assertTrue(dep06.judge_runs_candidate("serverless", [("7", "repo@sha256:" + "a" * 64)], "v1", MANIFEST, "")[0])
        self.assertFalse(dep06.judge_runs_candidate("serverless", [("7", "repo@sha256:" + "f" * 64)], "v1", MANIFEST, "")[0])

    def test_exit_token_and_audit(self):
        self.assertTrue(dep06.judge_install_exit(0)[0])
        self.assertFalse(dep06.judge_install_exit(1)[0])
        self.assertTrue(dep06.judge_new_token({"status": 200})[0])
        self.assertFalse(dep06.judge_new_token({"status": 401})[0])
        self.assertTrue(dep06.judge_audit(True, 41, 12)[0])
        self.assertFalse(dep06.judge_audit(False, 41, 12)[0])
        self.assertFalse(dep06.judge_audit(True, 41, 0)[0])


if __name__ == "__main__":
    unittest.main()
