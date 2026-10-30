"""UAT-CAT-02's judging, offline."""

import unittest
from types import SimpleNamespace

from cases import cat02

ROLE = "colvenna-catalog-ek1-data"


class Collected(unittest.TestCase):
    def test_s3_answers(self):
        def p(code, err=""):
            return SimpleNamespace(returncode=code, stderr=err)
        self.assertEqual(cat02.classify(p(0)), "allow")
        self.assertEqual(cat02.classify(p(254, "An error occurred (AccessDenied) when calling")), "deny")
        self.assertEqual(cat02.classify(p(254, "An error occurred (403) when calling the HeadObject operation: Forbidden")),
                         "deny")
        self.assertEqual(cat02.classify(p(254, "An error occurred (404) when calling the HeadObject operation: Not Found")),
                         "missing")
        self.assertTrue(cat02.classify(p(255, "Could not connect to the endpoint URL")).startswith("error"))

    def test_credentials_and_durations(self):
        body = {"token": {"s3.access-key-id": "ASIAXXXX", "s3.secret-access-key": "s", "s3.session-token": "t"}}
        self.assertEqual(cat02.credentials(body), {"accessKeyId": "ASIAXXXX", "secretAccessKey": "s", "sessionToken": "t"})
        self.assertEqual(cat02.minutes("PT3H"), 180)
        self.assertEqual(cat02.minutes("PT1H30M"), 90)
        self.assertIsNone(cat02.minutes("P1D"))


class Judged(unittest.TestCase):
    def test_issued(self):
        arn = f"arn:aws:sts::123456789012:assumed-role/{ROLE}/colvenna-cred-writer-u1"
        self.assertTrue(cat02.judge_issued({"status": 200}, 179.6, 180, arn, ROLE, "cred-writer-u1")[0])
        self.assertFalse(cat02.judge_issued({"status": 200}, 60, 180, arn, ROLE, "cred-writer-u1")[0])
        self.assertFalse(cat02.judge_issued({"status": 200}, 180, 180, arn.replace(ROLE, "other"), ROLE, "cred-writer-u1")[0])
        self.assertFalse(cat02.judge_issued({"status": 403}, None, 180, "", ROLE, "cred-writer-u1")[0])

    def test_own_table(self):
        good = {"list": "allow", "read": "allow", "missing": "missing", "write": "allow", "delete": "allow"}
        self.assertTrue(cat02.judge_own_table(good)[0])
        # A missing key answered 403 would hide whether the object exists: not what the case expects.
        self.assertFalse(cat02.judge_own_table(dict(good, missing="deny"))[0])
        self.assertFalse(cat02.judge_own_table(dict(good, write="deny"))[0])

    def test_all_and_negatives(self):
        self.assertTrue(cat02.judge_all({"read": "allow", "list": "allow"}, "allow")[0])
        self.assertFalse(cat02.judge_all({"read": "allow", "list": "deny"}, "allow")[0])
        self.assertTrue(cat02.judge_negative({"writer": ["deny", "deny"], "reader": ["deny"]})[0])
        self.assertFalse(cat02.judge_negative({"writer": ["deny", "allow"], "reader": ["deny"]})[0])
        self.assertFalse(cat02.judge_negative({"writer": ["error: timeout"]})[0])

    def test_probe(self):
        lines = [f"PROBE d.t{i} {a} denied AccessDenied" for i in range(3) for a in ("read", "list")]
        self.assertTrue(cat02.judge_probe(lines, 6)[0])
        self.assertFalse(cat02.judge_probe(lines[:5], 6)[0])
        self.assertFalse(cat02.judge_probe(lines[:5] + ["PROBE d.t2 list allowed"], 6)[0])

    def test_web_identity(self):
        eks = {"userIdentity": {"type": "WebIdentityUser",
                                "identityProvider": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/X"},
               "requestParameters": {"roleSessionName": "colvenna-cred-writer-u1"}}
        self.assertTrue(cat02.judge_web_identity(eks, "eks", "cred-writer-u1")[0])
        self.assertFalse(cat02.judge_web_identity(eks, "serverless", "cred-writer-u1")[0])
        cognito = {"userIdentity": {"identityProvider": "cognito-identity.amazonaws.com"},
                   "requestParameters": {"roleSessionName": "colvenna-cred-writer-u1"}}
        self.assertTrue(cat02.judge_web_identity(cognito, "serverless", "cred-writer-u1")[0])
        self.assertFalse(cat02.judge_web_identity(None, "eks", "cred-writer-u1")[0])

    def test_outsider_refused(self):
        self.assertTrue(cat02.judge_refused({"status": 403, "body": {"message": "Forbidden"}})[0])
        self.assertFalse(cat02.judge_refused({"status": 200, "body": {}})[0])

    def test_no_external_table(self):
        gone = {"status": 404, "body": {"message": "Table not found"}}
        refused = {"status": 400, "body": {"message": "Option path names s3://b/uat-external-u1/t, which is not under an "
                                                      "allowed external location root; this catalog allows no client-chosen "
                                                      "locations."}}
        self.assertTrue(cat02.judge_client_location(refused, gone)[0])
        # Made after all, refused for another reason, or refused but there anyway.
        self.assertFalse(cat02.judge_client_location({"status": 200, "body": {}}, {"status": 200})[0])
        self.assertFalse(cat02.judge_client_location({"status": 400, "body": {"message": "Invalid schema"}}, gone)[0])
        self.assertFalse(cat02.judge_client_location(refused, {"status": 200})[0])
        outside = {"status": 400, "body": {"message": "Only tables inside the warehouse s3://b/w can be registered; "
                                                      "create an external table for s3://b/uat-external-u1/t."}}
        self.assertTrue(cat02.judge_register_outside(outside, gone)[0])
        self.assertFalse(cat02.judge_register_outside({"status": 200, "body": {}}, {"status": 200})[0])
        self.assertFalse(cat02.judge_register_outside({"status": 403, "body": {"message": "Forbidden"}}, gone)[0])
        self.assertFalse(cat02.judge_register_outside({"error": "timed out"}, gone)[0])

    def test_issuances(self):
        detail = {"roleSessionName": "colvenna-x", "accessKeyId": "ASIAX", "cached": False, "access": "READ_WRITE",
                  "prefix": "s3://b/w/d.db/t/", "expiresAt": "2026-10-29T08:00:00Z"}
        events = {"r1": {"detail": detail}, "r2": {"detail": dict(detail, access="READ")},
                  "r3": {"detail": detail}, "r4": {"detail": dict(detail, cached=True)}}
        asked = {"writer": {"serverRequestId": "r1"}, "reader": {"serverRequestId": "r2"},
                 "writer-again-1": {"serverRequestId": "r3"}, "writer-again-2": {"serverRequestId": "r4"}}
        self.assertTrue(cat02.judge_issuances(events, asked, ["secret-key"])[0])
        self.assertFalse(cat02.judge_issuances(dict(events, r4={"detail": detail}), asked, [])[0])
        self.assertFalse(cat02.judge_issuances({k: events[k] for k in ("r1", "r3", "r4")}, asked, [])[0])
        leaky = dict(events, r1={"detail": dict(detail, note="secret-key-value")})
        self.assertFalse(cat02.judge_issuances(leaky, asked, ["secret-key-value"])[0])

    def test_keys(self):
        self.assertTrue(cat02.judge_keys([], ["ns"])[0])
        self.assertFalse(cat02.judge_keys(["ns/secrets/x"], ["ns"])[0])
        self.assertTrue(cat02.KEY_PATTERN.search('"fs.s3a.access.key": "x"'))
        self.assertTrue(cat02.KEY_PATTERN.search("AKIAABCDEFGHIJKLMNOP"))  # pragma: allowlist secret - a made-up access key id, which the pattern must find
        self.assertFalse(cat02.KEY_PATTERN.search('"UAT_TOKEN_FILE": "/secret/x"'))


if __name__ == "__main__":
    unittest.main()
