"""The release scripts (scripts/release) refuse a bad argument or setting before they read anything from
AWS: deploy-registry.sh's pull accounts (account IDs, never "*") and Region, make-manifest.sh's
candidate name (a release is promoted, never built) and Regions, verify-artifacts.sh's candidate or release
name and registry. What they do against AWS is proved on the release registry itself."""
import os
import subprocess
import unittest

from helpers import REPO
from stubs import Stubs


class Scripts(unittest.TestCase):
    """The release scripts refuse a bad setting before they read anything from AWS."""

    def run_script(self, script, *args, **env):
        stubs = Stubs({"aws": {}, "npx": {}, "docker": {}, "helm": {}, "crane": {}},
                      commands=("aws", "npx", "docker", "helm", "crane"))
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("COLVENNA_", "RELEASE_"))}
        out = subprocess.run([str(REPO / "scripts/release" / script), *args], capture_output=True, text=True,
                             env={**clean, **stubs.env, "PATH": f"{stubs.bin}:{clean['PATH']}", **env})
        return out, stubs

    def test_pull_accounts_are_account_ids_never_a_wildcard(self):
        for accounts in ("*", "444455556666,*", "4444", "444455556666,444455556666", "arn:aws:iam::444455556666:root",
                         "444455556666 777788889999"):
            with self.subTest(accounts):
                out, stubs = self.run_script("deploy-registry.sh", COLVENNA_RELEASE_PULL_ACCOUNTS=accounts)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertIn("COLVENNA_RELEASE_PULL_ACCOUNTS", out.stderr)
                self.assertEqual(stubs.calls(), [])

    def test_the_registry_s_region(self):
        for region in ("cn-north-1", "us-gov-west-1", "west"):
            with self.subTest(region):
                out, stubs = self.run_script("deploy-registry.sh", COLVENNA_RELEASE_REGION=region)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(stubs.calls(), [])

    def test_make_manifest_needs_a_candidate_s_name_and_two_regions(self):
        cases = [((), {}), (("v1.0.0",), {}), (("v1.0.0-rc.1", "extra"), {}),
                 (("v1.0.0-rc.1",), {"RELEASE_REGIONS": "us-west-2"}),
                 (("v1.0.0-rc.1",), {"RELEASE_REGIONS": "us-west-2,cn-north-1"}),
                 (("v1.0.0-rc.1",), {"COLVENNA_RELEASE_REGION": "cn-north-1"})]
        for args, env in cases:
            with self.subTest(args=args, env=env):
                out, stubs = self.run_script("make-manifest.sh", *args, **env)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertEqual(stubs.calls(), [])

    def test_verify_artifacts_needs_a_candidate_s_or_a_release_s_name_and_a_registry(self):
        registry = "444455556666.dkr.ecr.us-east-2.amazonaws.com"
        cases = [((), {"COLVENNA_RELEASE_REGISTRY": registry}), (("1.0.0",), {"COLVENNA_RELEASE_REGISTRY": registry}),
                 (("v1.0",), {"COLVENNA_RELEASE_REGISTRY": registry}), (("v1.0.0-rc.1",), {}), (("v1.0.0",), {}),
                 (("v1.0.0-rc.1",), {"COLVENNA_RELEASE_REGISTRY": "ghcr.io"}),
                 (("v1.0.0",), {"COLVENNA_RELEASE_REGISTRY": "444455556666.dkr.ecr.cn-north-1.amazonaws.com"})]
        for args, env in cases:
            with self.subTest(args=args, env=env):
                out, stubs = self.run_script("verify-artifacts.sh", *args, **env)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertEqual(stubs.calls(), [])
        out, _ = self.run_script("verify-artifacts.sh", "v1.0.0")
        self.assertIn("the release registry that holds v1.0.0", out.stderr)


if __name__ == "__main__":
    unittest.main()
