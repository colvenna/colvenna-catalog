"""UAT-CAT-05's judging, offline."""

import subprocess
import unittest

from cases import cat05
from uatlib.openapi import Description


def r(status, body=None):
    return {"status": status, "body": body}


class Judged(unittest.TestCase):
    def test_issue(self):
        now = 1_800_000_000
        issued = r(200, {"id": 7, "token": "colvenna_abc", "expiresAt": now + 90 * 86400})
        self.assertTrue(cat05.judge_issue(issued, r(200, {"tokens": [{"id": 7}]}), now)[0])
        self.assertFalse(cat05.judge_issue(issued, r(200, {"tokens": [{"id": 7, "x": "colvenna_abc"}]}), now)[0])
        self.assertFalse(cat05.judge_issue(r(200, {"token": "colvenna_abc", "expiresAt": now + 30 * 86400}), r(200, {}), now)[0])
        self.assertFalse(cat05.judge_issue(r(200, {"token": "other", "expiresAt": now + 90 * 86400}), r(200, {}), now)[0])

    def test_tokens_list_shows_no_token_or_hash(self):
        ok = r(200, {"tokens": [{"id": 1, "expiresAt": 1, "revokedAt": None}]})
        self.assertTrue(cat05.judge_tokens_list(ok, "colvenna_x")[0])
        self.assertFalse(cat05.judge_tokens_list(r(200, {"tokens": [{"id": 1, "expiresAt": 1, "revokedAt": None,
                                                                      "tokenHash": "ab"}]}), "colvenna_x")[0])
        self.assertFalse(cat05.judge_tokens_list(r(200, {"tokens": []}), "colvenna_x")[0])

    def test_revoke_and_disable(self):
        self.assertTrue(cat05.judge_revoke(r(200), r(200), r(401))[0])
        self.assertFalse(cat05.judge_revoke(r(200), r(200), r(200))[0])
        self.assertTrue(cat05.judge_disable(r(200), [401, 401])[0])
        self.assertFalse(cat05.judge_disable(r(200), [401, 200])[0])
        self.assertFalse(cat05.judge_disable(r(200), [])[0])

    def test_grant_cycle(self):
        self.assertTrue(cat05.judge_grant_cycle(r(200, {"id": 5}), r(200, {"grants": [{"id": 5}]}), r(200),
                                                r(200, {"grants": []}))[0])
        self.assertFalse(cat05.judge_grant_cycle(r(200, {"id": 5}), r(200, {"grants": [{"id": 5}]}), r(200),
                                                 r(200, {"grants": [{"id": 5}]}))[0])

    def test_each_approval_field_is_required(self):
        named = {k: r(400, {"message": f"Field {k} is required."}) for k in ("purpose", "approvedBy", "approval", "expiresAt")}
        self.assertTrue(cat05.judge_needs_approval(named)[0])
        self.assertFalse(cat05.judge_needs_approval(dict(named, approval=r(200, {"id": 9})))[0])
        self.assertFalse(cat05.judge_needs_approval(dict(named, purpose=r(400, {"message": "bad"})))[0])

    def test_audit_filters(self):
        e = [{"time": "2026-10-29T00:00:03Z", "principal": "admin"}, {"time": "2026-10-29T00:00:01Z", "principal": "admin"}]
        q = [("principal", "principal", "admin", r(200, {"events": e}))]
        window = r(200, {"events": e})
        since, until = "2026-10-29T00:00:00Z", "2026-10-29T00:01:00Z"
        over = r(200, {"events": [{}] * 1000})
        self.assertTrue(cat05.judge_audit_filters(q, window, since, until, over, 1500)[0])
        self.assertFalse(cat05.judge_audit_filters(q, window, since, until, over, 500)[0], "the limit was not judged")
        self.assertFalse(cat05.judge_audit_filters(q, window, since, until, r(200, {"events": [{}] * 1001}), 1500)[0])
        wrong = [("principal", "principal", "admin", r(200, {"events": e + [{"time": "2026-10-29T00:00:00Z", "principal": "x"}]}))]
        self.assertFalse(cat05.judge_audit_filters(wrong, window, since, until, over, 1500)[0])
        unordered = [("principal", "principal", "admin", r(200, {"events": list(reversed(e))}))]
        self.assertFalse(cat05.judge_audit_filters(unordered, window, since, until, over, 1500)[0])

    def test_consistency_reconcile_unregister(self):
        self.assertTrue(cat05.judge_consistency(r(200, {"consistent": True, "problems": []}))[0])
        self.assertFalse(cat05.judge_consistency(r(200, {"consistent": False, "problems": ["x"]}))[0])
        t = {"id": "a", "schemaId": 0, "path": "s3://b/w/d.db/t"}
        self.assertTrue(cat05.judge_reconcile(r(200), t, dict(t))[0])
        self.assertFalse(cat05.judge_reconcile(r(200), t, dict(t, schemaId=1))[0])
        self.assertTrue(cat05.judge_unregister(r(200), False, r(200), t, dict(t, id="b"))[0])
        self.assertFalse(cat05.judge_unregister(r(200), True, r(200), t, t)[0])
        self.assertFalse(cat05.judge_unregister(r(200), False, r(409), t, {})[0])

    def test_service_refusals(self):
        self.assertTrue(cat05.judge_service_refused([("GET /principals", 403)])[0])
        self.assertFalse(cat05.judge_service_refused([("GET /principals", 403), ("GET /audit", 200)])[0])
        self.assertFalse(cat05.judge_service_refused([])[0])

    def test_manual(self):
        script = "echo '### manual block 1'\nx\necho '### manual block 2'\ny\n"
        self.assertTrue(cat05.judge_manual(0, "### manual block 1\n### manual block 2\n401\n", script)[0])
        self.assertFalse(cat05.judge_manual(0, "### manual block 1\n401\n", script)[0])
        self.assertFalse(cat05.judge_manual(1, "### manual block 1\n### manual block 2\n401\n", script)[0])
        self.assertFalse(cat05.judge_manual(0, "### manual block 1\n### manual block 2\n200\n", script)[0])

    def test_openapi(self):
        spec = Description()
        ok, _, problems = cat05.judge_openapi(spec, [("GET", "/principals", 200, {"principals": []})])
        self.assertTrue(ok, problems)
        ok, _, problems = cat05.judge_openapi(spec, [("GET", "/principals", 200, {"principals": [], "extra": 1})])
        self.assertFalse(ok)
        ok, _, problems = cat05.judge_openapi(spec, [("GET", "/principals", 418, None)])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class Manual(unittest.TestCase):
    def test_the_serverless_examples_leave_out_what_only_the_eks_form_has(self):
        from uatlib import manual

        text = manual.script("u1", "serverless", {"<region>": "us-west-2", "<env>": "e", "<api-id>": "a"})
        self.assertNotIn("port-forward", text)
        self.assertNotIn('kill "$forward"', text)
        self.assertNotIn("--cacert", text)
        self.assertIn("https://a.execute-api.us-west-2.amazonaws.com/catalog/colvenna/admin/v1", text)
        self.assertIn("etl-writer-u1", text)
        self.assertNotIn("etl-writer\"", text)

    def test_the_serverless_stand_in_for_kubectl_ends_a_pipeline_normally(self):
        # #32: the manual's rotation pipes kubectl into kubectl apply -f -; the stand-in reads what it is given.
        from uatlib import manual

        text = manual.script("u1", "serverless", {"<region>": "us-west-2", "<env>": "e", "<api-id>": "a"})
        stand_in = next(line for line in text.splitlines() if line.startswith("kubectl()"))
        out = subprocess.run(["bash", "-c", "set -euo pipefail; " + stand_in + "\n"
                              "kubectl create secret generic s --dry-run=client -o yaml | kubectl apply -f -; "
                              "head -c 1000000 /dev/zero | kubectl apply --filename=- ; echo done"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("not run: kubectl apply -f -", out.stdout)
        self.assertTrue(out.stdout.rstrip().endswith("done"))

    def test_the_eks_examples_run_as_written(self):
        from uatlib import manual

        text = manual.script("u1", "eks", {})
        self.assertIn("port-forward svc/colvenna-catalog 8181:8181", text)
        self.assertIn('kill "$forward"', text)
        self.assertLess(text.index("port-forward"), text.index("### setup"))
