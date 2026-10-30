"""UAT-CAT-06's judging, offline."""

import time
import unittest

from cases import cat06

T0 = 1_800_000_000.0


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def r(t, status, server_id=None):
    return {"time": stamp(t), "status": status, "serverRequestId": server_id, "requestId": server_id or "sent"}


class Judged(unittest.TestCase):
    def test_rotation(self):
        reads_a = [r(T0 + i, 200) for i in range(3)]
        reads_b = [r(T0 + 10 + i, 200) for i in range(3)] + [r(T0 + 100, 401)]
        self.assertTrue(cat06.judge_rotation(reads_a, reads_b, disabled=T0 + 50)[0])
        self.assertFalse(cat06.judge_rotation(reads_a + [r(T0 + 4, 401)], reads_b, disabled=T0 + 50)[0])
        self.assertFalse(cat06.judge_rotation(reads_a, [], disabled=T0 + 50)[0])

    def test_revoked_eks_needs_both_replicas(self):
        refused = [r(T0 + 1 + i, 401, f"id{i}") for i in range(90)]
        both = {"pod-a": {"id1"}, "pod-b": {"id2"}}
        self.assertTrue(cat06.judge_revoked("eks", refused, T0, both)[0])
        self.assertFalse(cat06.judge_revoked("eks", refused, T0, {"pod-a": {"id1"}, "pod-b": set()})[0])
        self.assertFalse(cat06.judge_revoked("eks", refused + [r(T0 + 95, 200, "x")], T0, both)[0])
        # Before the grace period a 200 is allowed (the cache may still answer).
        self.assertTrue(cat06.judge_revoked("eks", [r(T0 + 2, 200, "e")] + refused, T0, both)[0])

    def test_revoked_serverless_server_then_gateway(self):
        refused = [r(T0 + 1 + i, 401, f"id{i}" if i < 40 else None) for i in range(90)]
        logged = {"server": {f"id{i}" for i in range(40)}}
        self.assertTrue(cat06.judge_revoked("serverless", refused, T0, logged)[0])
        leaked = refused[:80] + [r(T0 + 85, 401, "late")]
        self.assertFalse(cat06.judge_revoked("serverless", leaked, T0, {"server": logged["server"] | {"late"}})[0])
        self.assertFalse(cat06.judge_revoked("serverless", refused, T0, {"server": set()})[0], "server ids not in its log")

    def test_disabled_and_audit(self):
        self.assertTrue(cat06.judge_disabled({"status": 401}, {"status": 401})[0])
        self.assertFalse(cat06.judge_disabled({"status": 401}, {"status": 200})[0])
        self.assertTrue(cat06.judge_audited({"a", "b"}, {"a", "b", "c"})[0])
        self.assertFalse(cat06.judge_audited({"a", "b"}, {"a"})[0])
        self.assertFalse(cat06.judge_audited(set(), set())[0])

    def test_scope_is_recorded(self):
        creds = {"accessKeyId": "AK", "expiresAtMillis": (T0 + 3600) * 1000}
        self.assertTrue(cat06.judge_scope(creds, (False, "AccessDenied"), 10)[0])
        self.assertFalse(cat06.judge_scope({}, (False, "x"), 10)[0])

    def test_credentials_of_a_token_answer(self):
        c = cat06.credentials_of({"token": {"fs.s3a.access.key": "AK", "fs.s3a.secret.key": "SK",
                                            "fs.s3a.session.token": "ST"}, "expiresAtMillis": 1})
        self.assertEqual((c["accessKeyId"], c["secretAccessKey"], c["sessionToken"]), ("AK", "SK", "ST"))


if __name__ == "__main__":
    unittest.main()
