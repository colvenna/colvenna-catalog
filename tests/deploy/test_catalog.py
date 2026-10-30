import subprocess
import unittest

from helpers import REPO, run_bash
from stubs import Stubs

HELM = REPO / ".tools/helm"
CHART = REPO / "deploy/helm/colvenna-catalog"
BASE = {
    "COLVENNA_ENV": "uat", "COLVENNA_REGION": "us-west-2", "COLVENNA_WAREHOUSE_NAME": "colvenna", "COLVENNA_DB_NAME": "colvenna_catalog",
    "COLVENNA_SERVICE_ACCOUNT": "colvenna-catalog", "COLVENNA_DATA_TOKENS": "aws-sts", "COLVENNA_DATA_TOKEN_DURATION": "PT3H",
    "COLVENNA_NODE_SELECTOR": "", "COLVENNA_INTERNAL_NLB": "off", "COLVENNA_NLB_ALLOWED_CIDRS": "",
    "COLVENNA_CLIENT_NAMESPACE_LABEL": "colvenna-catalog/client=true", "COLVENNA_METRICS_NAMESPACE_LABEL": "colvenna-catalog/monitoring=true",
    "IMAGE_REPOSITORY": "111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog",
    "IMAGE_DIGEST": "sha256:" + "a" * 64, "WAREHOUSE_ROOT": "s3://bucket/warehouse",
    "DB_ENDPOINT": "db.example.com", "DB_PORT": "5432", "ELIGIBLE_NODES": "2", "SOURCE_SHA": "b" * 40,
    "SERVER_ROLE_ARN": "arn:aws:iam::111122223333:role/colvenna-catalog-uat-server",
    "DATA_ROLE_ARN": "arn:aws:iam::111122223333:role/colvenna-catalog-uat-data", "NLB_HEALTH_CHECK_CIDRS": "",
    "TLS_FINGERPRINT": "c" * 64,
}


def args(**over):
    out = run_bash("chart_args", env={**BASE, **over})
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def render(**over):
    """The chart as helm template renders it with chart_args' arguments."""
    out = subprocess.run([str(HELM), "template", "colvenna-catalog", str(CHART), *args(**over)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


class ChartArgs(unittest.TestCase):
    def test_every_argument_is_set_json(self):
        for arg in args():
            self.assertRegex(arg, r"^--set-json=[A-Za-z][A-Za-z0-9.]*=")

    def test_the_default_render(self):
        manifests = render()
        self.assertIn('COLVENNA_WAREHOUSE_ROOT: "s3://bucket/warehouse"', manifests)
        self.assertIn('COLVENNA_DATA_TOKEN_ROLE_ARN: "arn:aws:iam::111122223333:role/colvenna-catalog-uat-data"', manifests)
        self.assertIn('COLVENNA_DATA_TOKEN_DURATION: "PT3H"', manifests)
        self.assertIn('COLVENNA_ENVIRONMENT: "uat"', manifests)
        self.assertIn('eks.amazonaws.com/role-arn: "arn:aws:iam::111122223333:role/colvenna-catalog-uat-server"', manifests)
        self.assertIn("@sha256:" + "a" * 64, manifests)
        self.assertIn('colvenna-catalog/source-sha: ' + "b" * 40, manifests)
        # The server certificate's fingerprint: a new certificate rolls the pods, whenever it was made.
        self.assertIn('colvenna-catalog/tls-sha256: ' + "c" * 64, manifests)
        self.assertIn("name: colvenna-catalog-db-ca", manifests)
        self.assertNotIn("type: LoadBalancer", manifests)
        self.assertIn('colvenna-catalog/client: "true"', manifests)
        self.assertIn('colvenna-catalog/monitoring: "true"', manifests)

    def test_a_release_s_pods_name_the_candidate(self):
        self.assertNotIn("colvenna-catalog/release", render())
        manifests = render(RELEASE_CANDIDATE="v1.0.0-rc.1")
        self.assertIn("colvenna-catalog/release: v1.0.0-rc.1", manifests)
        self.assertIn("colvenna-catalog/source-sha: " + "b" * 40, manifests)

    def test_the_database_url(self):
        self.assertIn('COLVENNA_JDBC_URL: "jdbc:postgresql://db.example.com:5432/colvenna_catalog?sslmode=verify-full'
                      '&sslrootcert=/etc/colvenna-catalog/db-ca/ca.pem"', render())

    def test_no_data_role_without_per_table_credentials(self):
        manifests = render(COLVENNA_DATA_TOKENS="disabled")
        self.assertIn('COLVENNA_DATA_TOKEN_MODE: "disabled"', manifests)
        self.assertNotIn("COLVENNA_DATA_TOKEN_ROLE_ARN", manifests)
        self.assertFalse(any("dataTokens.roleArn" in a for a in args(COLVENNA_DATA_TOKENS="disabled")))

    def test_the_internal_nlb_in_two_steps(self):
        first = render(COLVENNA_INTERNAL_NLB="on", COLVENNA_NLB_ALLOWED_CIDRS="10.85.0.0/21,10.85.8.0/21")
        self.assertIn("type: LoadBalancer", first)
        self.assertIn("cidr: 10.85.0.0/21", first)
        self.assertIn("cidr: 10.85.8.0/21", first)
        self.assertNotIn("/32", first)
        second = render(COLVENNA_INTERNAL_NLB="on", COLVENNA_NLB_ALLOWED_CIDRS="10.85.0.0/21",
                        NLB_HEALTH_CHECK_CIDRS="10.85.0.10/32,10.85.8.11/32")
        self.assertIn("cidr: 10.85.0.10/32", second)
        self.assertIn("cidr: 10.85.8.11/32", second)

    def test_a_node_selector_of_several_pairs(self):
        manifests = render(COLVENNA_NODE_SELECTOR="kubernetes.io/os=linux,role=catalog", ELIGIBLE_NODES="3")
        self.assertIn("kubernetes.io/os: linux", manifests)
        self.assertIn("role: catalog", manifests)
        self.assertIn("minDomains: 2", manifests)

    def test_colvenna_lake_names(self):
        manifests = render(COLVENNA_CLIENT_NAMESPACE_LABEL="colvenna-lake.io/catalog-client=true",
                           COLVENNA_METRICS_NAMESPACE_LABEL="colvenna-lake.io/monitoring=true")
        self.assertIn('colvenna-lake.io/catalog-client: "true"', manifests)
        self.assertIn('colvenna-lake.io/monitoring: "true"', manifests)



class Probe(unittest.TestCase):
    def test_a_probe_with_no_answer_is_run_again(self):
        # run_pod's log of a short-lived pod can come back empty once; the probe runs it again.
        # probe calls run_pod in a command substitution (a subshell): the count lives in a file.
        out = run_bash('c="$(mktemp)"; echo 0 >"$c"; '
                       'run_pod() { local n; n=$(($(cat "$c") + 1)); echo "$n" >"$c"; '
                       '[[ $n -ge 2 ]] && echo "anonymous=401 admin=200"; true; }; '
                       'probe ns /dev/null; echo "runs=$(cat "$c")"')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), ["anonymous=401 admin=200", "runs=2"])

    def test_a_probe_that_never_answers_fails(self):
        out = run_bash('run_pod() { echo; }; probe ns /dev/null')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no answer", out.stderr)


class Dates(unittest.TestCase):
    def test_a_date_some_days_from_now(self):
        out = run_bash('iso_in_days 365; date -u +%Y')
        self.assertEqual(out.returncode, 0, out.stderr)
        later, year = out.stdout.splitlines()
        self.assertRegex(later, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(int(later[:4]), int(year) + 1)


class Cleanup(unittest.TestCase):
    """What a failed or interrupted install leaves behind is removed on its way out."""

    def test_the_bootstrap_secret_is_deleted_only_while_it_is_there(self):
        stubs = Stubs({"kubectl": {"-n colvenna-catalog delete secret colvenna-catalog-db-bootstrap": {"stdout": ""},
                                   "-n colvenna-catalog delete pod colvenna-catalog-db-bootstrap": {"stdout": ""}}})
        env = {**stubs.env, "COLVENNA_NAMESPACE": "colvenna-catalog"}
        out = run_bash("db_bootstrap_cleanup", env=env, path_prepend=stubs.bin)
        self.assertEqual((out.returncode, stubs.calls()), (0, []), out.stderr)
        out = run_bash("DB_BOOTSTRAP_PENDING=1; db_bootstrap_cleanup; echo \"pending=$DB_BOOTSTRAP_PENDING\"",
                       env=env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pending=", out.stdout)
        self.assertEqual(stubs.calls(), ["kubectl -n colvenna-catalog delete pod colvenna-catalog-db-bootstrap --ignore-not-found --wait=false",
                                         "kubectl -n colvenna-catalog delete secret colvenna-catalog-db-bootstrap --ignore-not-found"])

    def test_the_smoke_namespaces_go_with_the_token_in_them(self):
        stubs = Stubs({"kubectl": {"delete namespace": {"stdout": ""}}})
        env = {**stubs.env, "COLVENNA_NAMESPACE": "colvenna-catalog"}
        out = run_bash("smoke_cleanup", env=env, path_prepend=stubs.bin)
        self.assertEqual((out.returncode, stubs.calls()), (0, []), out.stderr)
        out = run_bash('SMOKE_DIR="$(mktemp -d)"; d="$SMOKE_DIR"; smoke_cleanup; [[ ! -e "$d" ]]', env=env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stubs.calls(), ["kubectl delete namespace colvenna-catalog-smoke-client colvenna-catalog-smoke-outsider "
                                         "--ignore-not-found --wait=false"])


class NlbTargets(unittest.TestCase):
    def targets(self, groups):
        stubs = Stubs({"aws": {"elbv2 describe-target-groups": groups,
                               "elbv2 describe-target-health": {"stdout": "0\n"}}})
        out = run_bash("nlb_targets_healthy", env={**stubs.env, "COLVENNA_REGION": "us-west-2",
                                                   "NLB_ARN": "arn:aws:elasticloadbalancing:x"}, path_prepend=stubs.bin)
        return out, [c for c in stubs.calls() if "describe-target-health" in c]

    def test_every_target_group_healthy(self):
        out, checked = self.targets({"stdout": "arn:tg-1\tarn:tg-2\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(checked), 2)

    def test_an_unreadable_target_list_is_a_failure_not_health(self):
        for label, groups in [("error", {"stderr": "AccessDenied", "code": 254}), ("none", {"stdout": "\n"})]:
            with self.subTest(label):
                out, checked = self.targets(groups)
                self.assertNotEqual(out.returncode, 0)
                self.assertEqual(checked, [])


if __name__ == "__main__":
    unittest.main()
