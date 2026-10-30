"""Nothing the runner keeps holds a deployment value."""

import unittest

from compatlib.sanitize import Sanitizer


class Sanitized(unittest.TestCase):
    def setUp(self):
        self.s = Sanitizer(secrets=["colvenna_secret-token-value-123"],
                           values={"https://colvenna-catalog.colvenna-catalog.svc:8181": "<CATALOG_URI>"})

    def test_identifiers(self):
        text = ("User: arn:aws:sts::123456789012:assumed-role/data/colvenna-x is not authorized; "
                "account 123456789012; key ASIAABCDEFGHIJKLMNOP; s3://my-warehouse-bucket/warehouse/db.db/t; "  # pragma: allowlist secret - a made-up access key id, which the sanitizer must redact
                "my-warehouse-bucket.s3.us-west-2.amazonaws.com; ABCDEF.gr7.us-west-2.eks.amazonaws.com; "
                "from 10.85.3.4:8181 at ip-10-85-3-4.us-west-2.compute.internal")  # redaction: allow - a made-up node of the test VPC, which the sanitizer must redact
        out = self.s.text(text)
        for leaked in ("123456789012", "ASIAABCDEFGHIJKLMNOP", "my-warehouse-bucket", "ABCDEF.gr7", "10.85.3.4",  # pragma: allowlist secret - the made-up values above, which the sanitizer must redact (redaction: allow)
                       "ip-10-85"):
            self.assertNotIn(leaked, out)
        self.assertIn("<ARN>", out)
        self.assertIn("s3://<BUCKET>/warehouse/db.db/t", out)

    def test_secrets_and_the_uri(self):
        out = self.s.text("token colvenna_secret-token-value-123 at https://colvenna-catalog.colvenna-catalog.svc:8181/v1")
        self.assertEqual(out, "token <REDACTED> at <CATALOG_URI>/v1")

    def test_any_catalog_token(self):
        self.assertEqual(self.s.text("Bearer colvenna_AbCdEfGhIjKlMnOpQrStUv"), "Bearer <TOKEN>")

    def test_the_servers_metric_names_are_no_tokens(self):
        text = "colvenna_catalog_requests_total{action=\"COMMIT\"} 3 colvenna_catalog_request_seconds_bucket"
        self.assertEqual(self.s.text(text), text)
        self.assertEqual(self.s.text("Bearer colvenna_catalog_Ab-Cd0123456789xyz"), "Bearer <TOKEN>")
        self.assertEqual(self.s.text("Bearer colvenna_catalog_ab-Cd0123456789xyz"), "Bearer <TOKEN>")

    def test_an_account_id_as_api_gateway_masks_it(self):
        out = self.s.text("on resource: arn:aws:execute-api:us-west-2:********4269:abc/catalog/GET/v1 and ********4269")
        self.assertNotIn("4269", out)

    def test_versions_and_loopback_stay(self):
        text = "jdk-17.0.20.1+1 on 127.0.0.1, Paimon 2.1.0, Flink 1.20.5, 2026-10-28T08:00:00.123Z took 1234ms"
        self.assertEqual(self.s.text(text), text)

    def test_credentials_as_keys_and_values(self):
        out = self.s.text('fs.s3a.secret.key=abcdEFGH1234/xyz+9 and "s3.session-token": "IQoJEXAMPLEonly"')
        self.assertEqual(out, 'fs.s3a.secret.key=<REDACTED> and "s3.session-token": "<REDACTED>"')

    def test_credentials_as_an_engine_logs_them(self):
        fake = "IQoJ" + "EXAMPLEsessionTOKEN0123456789" * 3  # pragma: allowlist secret - an STS session token's shape, made up
        line = f"s3_util.cpp:460] create one s3 client with (ak=xxxxxxxEXAMPLExxxxxxx, token={fake}, region=us-west-2)"
        out = self.s.text(line)
        self.assertNotIn("IQoJ", out)
        self.assertNotIn("EXAMPLE", out)
        self.assertIn("(ak=<REDACTED>, token=<REDACTED>, region=us-west-2)", out)
        self.assertEqual(self.s.text(f"a bare {fake} token"), "a bare <REDACTED> token")

    def test_digests_and_commits_stay(self):
        text = "commit d988467c547af9f3c091d96cfc671499022900df image sha256:000fd431958bc81a24abe1e8e5f0f0fd"
        self.assertEqual(self.s.text(text), text)

    def test_uuids_stay_whole(self):
        # A request id whose last group is all digits is a UUID, not an account ID (#52).
        line = ("2026-10-28T12:33:21.903Z INFO  [virtual-12647] colvenna.access - "
                "142d0e5f-b35a-4867-842a-123456789012 GET /v1/colvenna/databases/compat 404 3ms "
                "principal=compat-writer action=GET_DATABASE")
        self.assertEqual(self.s.text(line), line)
        for uuid in ("142D0E5F-B35A-4867-842A-123456789012", "00000000-0000-0000-0000-000000000000",
                     "12345678-1234-1234-1234-123456789012"):
            self.assertEqual(self.s.text(f"id {uuid} done"), f"id {uuid} done")

    def test_standalone_twelve_digits_go_beside_a_uuid(self):
        out = self.s.text("request 142d0e5f-b35a-4867-842a-123456789012 by account 210987654321")
        self.assertEqual(out, "request 142d0e5f-b35a-4867-842a-123456789012 by account <ACCOUNT_ID>")
        self.assertEqual(self.s.text("142d0e5f-b35a-4867-842a-123456789012,210987654321"),
                         "142d0e5f-b35a-4867-842a-123456789012,<ACCOUNT_ID>")

    def test_twelve_digits_in_a_name_still_go(self):
        # Hyphens around an account ID in a name do not make it a UUID's last group.
        self.assertEqual(self.s.text("cdk-hnb659fds-assets-123456789012-us-west-2"),
                         "cdk-hnb659fds-assets-<ACCOUNT_ID>-us-west-2")
        self.assertEqual(self.s.text("colvenna-catalog-123456789012-a1b2c3d4e5f6"),
                         "colvenna-catalog-<ACCOUNT_ID>-a1b2c3d4e5f6")
        self.assertEqual(self.s.text("account=123456789012;"), "account=<ACCOUNT_ID>;")

    def test_not_a_uuid_when_a_group_is_too_long(self):
        # Nine hexadecimal digits before the first hyphen: no UUID, so its digits are judged alone.
        self.assertEqual(self.s.text("a142d0e5f-b35a-4867-842a-123456789012"),
                         "a142d0e5f-b35a-4867-842a-<ACCOUNT_ID>")

    def test_named_values(self):
        s = Sanitizer(values={"my-cluster-cm10": "<CLUSTER>"})
        self.assertEqual(s.text("cluster my-cluster-cm10 ready"), "cluster <CLUSTER> ready")

    def test_json_values(self):
        self.assertEqual(self.s.data({"a": ["10.0.0.1", 3]}), {"a": ["<IP>", 3]})  # redaction: allow - a made-up address, which the sanitizer must redact


if __name__ == "__main__":
    unittest.main()
