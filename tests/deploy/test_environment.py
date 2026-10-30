"""tests/environment/lib.sh's log groups of the test environment's functions, which down.sh deletes by the
functions' own names: a cut of one test environment's stack name may be another's whole."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, write
from stubs import Stubs

STACK = "ColvennaCatalogTestAbcdefg"
NESTED = "arn:aws:cloudformation:us-west-2:111122223333:stack/ColvennaCatalogTestAbcdefg-awscdkawseksKubectlProviderNe-X/1"
# CloudFormation cut the stack's 26 characters to 25: the whole of the test environment abcdef's stack name.
OWN = "ColvennaCatalogTestAbcdef-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl"
NESTED_OWN = "ColvennaCatalogTestAbcdefg-awscdkaw-Handler886CB40B-MnOpQrStUvWx"  # 64, the most


def lib(script, stubs):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLVENNA_")}
    environment.update({**stubs.env, "AWS_REGION": "us-west-2", "PATH": f"{stubs.bin}:{os.environ['PATH']}"})
    return subprocess.run(["bash", "-c", f'source "{REPO}/tests/environment/lib.sh"; {script}'], cwd=REPO, env=environment,
                          capture_output=True, text=True)


class FunctionLogGroups(unittest.TestCase):
    def test_the_stack_s_functions_and_its_nested_stacks(self):
        listing = "cloudformation list-stack-resources --stack-name"
        stubs = Stubs({"aws": {
            f"{listing} {STACK} ": {"stdout": f"AWS::Lambda::Function\t{OWN}\nAWS::CloudFormation::Stack\t{NESTED}\n"},
            f"{listing} {NESTED} ": {"stdout": f"AWS::Lambda::Function\t{NESTED_OWN}\n"},
        }})
        out = lib(f"stack_functions {STACK}", stubs)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [OWN, NESTED_OWN])

    def test_a_stack_that_cannot_be_read_is_a_failure(self):
        stubs = Stubs({"aws": {"cloudformation list-stack-resources": {"stderr": "Throttling", "code": 254}}})
        self.assertNotEqual(lib(f"stack_functions {STACK}", stubs).returncode, 0)

    def test_only_the_named_functions_groups_are_deleted(self):
        names = write(Path(tempfile.mkdtemp()) / "functions", f"{OWN}\n{NESTED_OWN}\n")
        describe = "logs describe-log-groups --log-group-name-prefix"
        stubs = Stubs({"aws": {
            f"{describe} /aws/lambda/{OWN} ": {"stdout": f"/aws/lambda/{OWN}\n"},
            f"{describe} /aws/lambda/{NESTED_OWN} ": {"stdout": ""},  # gone already
            "logs delete-log-group": {"stdout": ""},
        }})
        out = lib(f'recorded_log_groups "{names}"', stubs)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [f"/aws/lambda/{OWN}"])
        out = lib(f'delete_recorded_log_groups "{names}"', stubs)
        self.assertEqual(out.returncode, 0, out.stderr)
        deleted = [c for c in stubs.calls() if c.startswith("aws logs delete-log-group")]
        self.assertEqual(deleted, [f"aws logs delete-log-group --log-group-name /aws/lambda/{OWN}"])
        # The groups are looked up by the exact names alone: the test environment abcdef's, whose
        # functions' names begin as these do, are never listed.
        self.assertTrue(all("--log-group-name-prefix /aws/lambda/ColvennaCatalogTestAbcdef " not in c for c in stubs.calls()))

    def test_a_group_that_cannot_be_looked_up_is_a_failure(self):
        names = write(Path(tempfile.mkdtemp()) / "functions", f"{OWN}\n{NESTED_OWN}\n")
        stubs = Stubs({"aws": {f"logs describe-log-groups --log-group-name-prefix /aws/lambda/{OWN} ": {"stdout": ""},
                               "logs describe-log-groups": {"stderr": "ThrottlingException", "code": 254}}})
        self.assertNotEqual(lib(f'recorded_log_groups "{names}"', stubs).returncode, 0)
        out = lib(f'delete_recorded_log_groups "{names}"; echo deleted', stubs)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("deleted", out.stdout)

    def test_no_names_kept_nothing_to_delete(self):
        stubs = Stubs({"aws": {}})
        out = lib(f'delete_recorded_log_groups "{tempfile.mkdtemp()}/functions"', stubs)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stubs.calls(), [])


if __name__ == "__main__":
    unittest.main()
