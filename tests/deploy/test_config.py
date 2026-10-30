import tempfile
import unittest
from pathlib import Path

from helpers import REPO, run_bash, write

REQUIRED = {
    "COLVENNA_ENV": "uat",
    "COLVENNA_ACCOUNT": "111122223333",
    "COLVENNA_REGION": "us-west-2",
    "COLVENNA_FORM": "eks",
    "COLVENNA_EKS_CLUSTER": "colvenna-catalog-test-ct1",
}


class LoadConfig(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def load(self, values, extra=""):
        text = "".join(f"{k}={v}\n" for k, v in values.items()) + extra
        config = write(self.dir / "uat.env", text)
        return run_bash(f'load_config "{config}"; printenv | grep "^COLVENNA_" | sort')

    def loaded(self, values, extra=""):
        out = self.load(values, extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        return dict(line.split("=", 1) for line in out.stdout.splitlines())

    def refused(self, values, *needles, extra=""):
        out = self.load(values, extra)
        self.assertEqual(out.returncode, 2, f"loaded: {out.stdout}")
        for needle in needles:
            self.assertIn(needle, out.stderr)
        return out.stderr

    def test_the_example_with_the_required_values_loads_with_defaults(self):
        example = (REPO / "deploy/config/example.env").read_text()
        for key, value in REQUIRED.items():
            example = example.replace(f"{key}=", f"{key}={value}", 1) if f"\n{key}=\n" in f"\n{example}" else example
        config = write(self.dir / "example.env", example)
        out = run_bash(f'load_config "{config}"; printenv | grep "^COLVENNA_" | sort')
        self.assertEqual(out.returncode, 0, out.stderr)
        got = dict(line.split("=", 1) for line in out.stdout.splitlines())
        self.assertEqual(got["COLVENNA_NAMESPACE"], "colvenna-catalog")
        self.assertEqual(got["COLVENNA_DB"], "create")
        self.assertEqual(got["COLVENNA_DATA_TOKEN_DURATION"], "PT3H")
        self.assertEqual(got["COLVENNA_INTERNAL_NLB"], "off")

    def test_defaults(self):
        got = self.loaded(REQUIRED)
        self.assertEqual(got["COLVENNA_NAMESPACE"], "colvenna-catalog")
        self.assertEqual(got["COLVENNA_RELEASE_NAME"], "colvenna-catalog")
        self.assertEqual(got["COLVENNA_SERVICE_ACCOUNT"], "colvenna-catalog")
        self.assertEqual(got["COLVENNA_WAREHOUSE_NAME"], "colvenna")
        self.assertEqual(got["COLVENNA_WAREHOUSE_PREFIX"], "warehouse")
        self.assertEqual(got["COLVENNA_DB_NAME"], "colvenna_catalog")
        self.assertEqual(got["COLVENNA_DB_USER"], "colvenna_catalog")
        self.assertEqual(got["COLVENNA_DATA_TOKENS"], "aws-sts")
        self.assertEqual(got["COLVENNA_CLIENT_NAMESPACE_LABEL"], "colvenna-catalog/client=true")
        self.assertEqual(got["COLVENNA_METRICS_NAMESPACE_LABEL"], "colvenna-catalog/monitoring=true")
        self.assertEqual(got["COLVENNA_BUCKET"], "")

    def test_each_required_key_is_required(self):
        for key in REQUIRED:
            values = {k: v for k, v in REQUIRED.items() if k != key}
            self.refused(values, key)

    def test_a_misspelt_key_is_refused(self):
        self.refused(REQUIRED, "COLVENNA_INTERAL_NLB: unknown key", extra="COLVENNA_INTERAL_NLB=on\n")

    def test_a_key_set_twice_is_refused(self):
        self.refused(REQUIRED, "COLVENNA_ENV: set twice", extra="COLVENNA_ENV=dev\n")

    def test_values_are_not_expanded(self):
        marker = self.dir / "expanded"
        self.refused({**REQUIRED, "COLVENNA_NAMESPACE": f"$(touch {marker})"}, "COLVENNA_NAMESPACE")
        self.assertFalse(marker.exists())

    def test_comments_blank_lines_and_quotes(self):
        got = self.loaded(REQUIRED, extra="\n# a comment\n  # indented\nCOLVENNA_NAMESPACE=\"cat\"\n")
        self.assertEqual(got["COLVENNA_NAMESPACE"], "cat")

    def test_existing_database_needs_its_endpoint_and_admin_secret(self):
        self.refused({**REQUIRED, "COLVENNA_DB": "existing"}, "COLVENNA_DB_ENDPOINT", "COLVENNA_DB_ADMIN_SECRET_ARN")
        got = self.loaded({**REQUIRED, "COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": "db.example.com",
                           "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:db-admin-AbCdEf"})  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
        self.assertEqual(got["COLVENNA_DB_PORT"], "5432")

    def test_the_admin_secret_is_of_the_account_and_region(self):
        self.refused({**REQUIRED, "COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": "db.example.com",
                      "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:111122223333:secret:x"},  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
                     "COLVENNA_DB_ADMIN_SECRET_ARN")

    def test_the_internal_nlb_needs_its_cidrs(self):
        self.refused({**REQUIRED, "COLVENNA_INTERNAL_NLB": "on"}, "COLVENNA_NLB_ALLOWED_CIDRS")
        got = self.loaded({**REQUIRED, "COLVENNA_INTERNAL_NLB": "on", "COLVENNA_NLB_ALLOWED_CIDRS": "10.85.0.0/21,fd00::/64"})
        self.assertEqual(got["COLVENNA_NLB_ALLOWED_CIDRS"], "10.85.0.0/21,fd00::/64")

    def test_bad_values(self):
        cases = {
            "COLVENNA_REGION": "cn-north-1",
            "COLVENNA_ACCOUNT": "11112222333",
            "COLVENNA_ENV": "release",
            "COLVENNA_FORM": "serverless",
            "COLVENNA_WAREHOUSE_PREFIX": "warehouse/",
            "COLVENNA_DATA_TOKEN_DURATION": "PT1H",
            "COLVENNA_NAMESPACE": "Colvenna",
            "COLVENNA_DB_USER": "colvenna-catalog",
            "COLVENNA_BUCKET": "Bucket_1",
            "COLVENNA_DB": "rds",
            "COLVENNA_DATA_TOKENS": "on",
            "COLVENNA_INTERNAL_NLB": "yes",
            "COLVENNA_NODE_SELECTOR": "role",
            "COLVENNA_CLIENT_NAMESPACE_LABEL": "client",
            "COLVENNA_EKS_CLUSTER": "-bad",
        }
        for key, value in cases.items():
            with self.subTest(key=key, value=value):
                self.refused({**REQUIRED, key: value}, key)
        for duration in ["PT13H", "P1D", "PT90M30S"]:
            with self.subTest(duration=duration):
                self.refused({**REQUIRED, "COLVENNA_DATA_TOKEN_DURATION": duration}, "COLVENNA_DATA_TOKEN_DURATION")
        for cidr in ["0.0.0.0/0", "::/0", "10.0.0.0/33", "10.0.0.0", "x"]:  # redaction: allow - made-up CIDRs, which the check must refuse
            with self.subTest(cidr=cidr):
                self.refused({**REQUIRED, "COLVENNA_INTERNAL_NLB": "on", "COLVENNA_NLB_ALLOWED_CIDRS": cidr},
                             "COLVENNA_NLB_ALLOWED_CIDRS")

    def test_the_release_registry(self):
        self.assertEqual(self.loaded(REQUIRED)["COLVENNA_RELEASE_REGISTRY"], "")
        registry = "444455556666.dkr.ecr.us-east-2.amazonaws.com"
        self.assertEqual(self.loaded({**REQUIRED, "COLVENNA_RELEASE_REGISTRY": registry})["COLVENNA_RELEASE_REGISTRY"],
                         registry)
        for bad in ("ghcr.io", f"https://{registry}", f"{registry}/colvenna-catalog-release", "4444.dkr.ecr.us-east-2.amazonaws.com",
                    "444455556666.dkr.ecr.us-gov-west-1.amazonaws.com"):
            with self.subTest(bad):
                self.refused({**REQUIRED, "COLVENNA_RELEASE_REGISTRY": bad}, "COLVENNA_RELEASE_REGISTRY: the release registry")
        self.refused({**REQUIRED, "COLVENNA_RELEASE_REGISTRY": "444455556666.dkr.ecr.cn-north-1.amazonaws.com"},
                     "COLVENNA_RELEASE_REGISTRY: the China partition is not supported")

    def test_govcloud_and_china_are_refused_by_name(self):
        self.refused({**REQUIRED, "COLVENNA_REGION": "us-gov-west-1"}, "COLVENNA_REGION: GovCloud is not supported")
        self.refused({**REQUIRED, "COLVENNA_REGION": "cn-northwest-1"}, "COLVENNA_REGION: the China partition is not supported")

    def test_the_metrics_label_value_is_true(self):
        # The chart admits monitoring namespaces by the label key with the value "true".
        self.refused({**REQUIRED, "COLVENNA_METRICS_NAMESPACE_LABEL": "colvenna-catalog/monitoring=yes"}, "COLVENNA_METRICS_NAMESPACE_LABEL")

    def test_good_durations(self):
        for duration in ["PT1H30M", "PT90M", "PT12H", "PT3H"]:
            with self.subTest(duration=duration):
                self.loaded({**REQUIRED, "COLVENNA_DATA_TOKEN_DURATION": duration})

    def test_every_error_at_once(self):
        stderr = self.refused({**REQUIRED, "COLVENNA_REGION": "cn-north-1", "COLVENNA_NAMESPACE": "Colvenna"},
                              "COLVENNA_REGION", "COLVENNA_NAMESPACE")
        self.assertEqual(stderr.count("config: "), 2, stderr)

    def test_a_missing_file(self):
        out = run_bash(f'load_config "{self.dir}/none.env"')
        self.assertEqual(out.returncode, 2)
        self.assertIn("none.env", out.stderr)


if __name__ == "__main__":
    unittest.main()
