import json
import os
import random
import re
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, run_bash, stack_outputs, write
from stubs import Stubs

CONFIG = "COLVENNA_ENV=uat\nCOLVENNA_ACCOUNT=111122223333\nCOLVENNA_REGION=us-west-2\nCOLVENNA_FORM=eks\nCOLVENNA_EKS_CLUSTER=c1\n"  # pragma: allowlist secret - a placeholder install configuration: a test environment's name, AWS's documentation account


def install(args, stdin=None, env=None):
    return run_bash(f'"{REPO}/install.sh" {args}', stdin=stdin, env=env)


def uninstall(args, stdin=None, env=None):
    return run_bash(f'"{REPO}/uninstall.sh" {args}', stdin=stdin, env=env)


class Entrypoints(unittest.TestCase):
    def setUp(self):
        self.config = write(Path(tempfile.mkdtemp()) / "uat.env", CONFIG)

    def test_help(self):
        for run in (install, uninstall):
            out = run("--help")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("Usage:", out.stdout)

    def test_install_needs_a_config_and_one_source(self):
        self.assertEqual(install("--from-source").returncode, 2)
        self.assertEqual(install(f"--config {self.config}").returncode, 2)
        self.assertEqual(install(f"--config {self.config} --from-source --release v1.0.0-rc.1").returncode, 2)
        self.assertEqual(install(f"--config {self.config} --from-source --bogus").returncode, 2)

    def test_a_release_install_needs_its_registry(self):
        # The release's own checks are tests/deploy/test_release.py's.
        out = install(f"--config {self.config} --release v1.0.0-rc.1")
        self.assertEqual(out.returncode, 2)
        self.assertIn("COLVENNA_RELEASE_REGISTRY: required with --release", out.stderr)

    def test_a_bad_config_stops_before_anything_else(self):
        bad = write(Path(tempfile.mkdtemp()) / "bad.env", CONFIG + "COLVENNA_INTERAL_NLB=on\n")
        stubs = Stubs({"aws": {}, "kubectl": {}})
        out = run_bash(f'"{REPO}/install.sh" --config {bad} --from-source', env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 2)
        self.assertIn("COLVENNA_INTERAL_NLB: unknown key", out.stderr)
        self.assertEqual(stubs.calls(), [])

    def test_no_confirmation_without_a_terminal(self):
        out = run_bash(f'source "{REPO}/install.sh"; load_config "{self.config}"; confirm_install', stdin="")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--yes", out.stderr)

    def test_yes_confirms(self):
        out = run_bash(f'source "{REPO}/install.sh"; load_config "{self.config}"; ASSUME_YES=1 confirm_install', stdin="")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_deleting_data_needs_the_environment_name_typed(self):
        stubs = Stubs({"aws": {}, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; load_config "{self.config}"; confirm_delete_data',
                       stdin="wrong\n", env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 2)
        self.assertEqual(stubs.calls(), [])
        out = run_bash(f'source "{REPO}/uninstall.sh"; load_config "{self.config}"; confirm_delete_data', stdin="uat\n")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_uninstall_needs_a_config(self):
        self.assertEqual(uninstall("").returncode, 2)
        self.assertEqual(uninstall(f"--config {self.config} --bogus").returncode, 2)



class DeleteLogGroups(unittest.TestCase):
    def test_deletes_every_group_under_the_prefix(self):
        stubs = Stubs({"aws": {
            "logs describe-log-groups": {
                "stdout": "/aws/rds/instance/db-1/postgresql\t/aws/rds/instance/db-1/upgrade\n"},
            "logs delete-log-group": {"stdout": ""},
        }, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 delete_log_groups /aws/rds/instance/db-1/',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        listed = [c for c in stubs.calls() if c.startswith("aws logs describe-log-groups")]
        self.assertIn("--log-group-name-prefix /aws/rds/instance/db-1/", listed[0])
        deleted = [c for c in stubs.calls() if c.startswith("aws logs delete-log-group")]
        self.assertEqual(len(deleted), 2)
        self.assertIn("--log-group-name /aws/rds/instance/db-1/postgresql", deleted[0])
        self.assertIn("--log-group-name /aws/rds/instance/db-1/upgrade", deleted[1])

    def test_every_page_of_the_listing(self):
        # The CLI writes each page of a listing in text on a line of its own.
        stubs = Stubs({"aws": {"logs describe-log-groups": {"stdout": "/aws/lambda/a-1\t/aws/lambda/a-2\n/aws/lambda/a-3\n"},
                               "logs delete-log-group": {"stdout": ""}}, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 delete_log_groups /aws/lambda/a-',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len([c for c in stubs.calls() if c.startswith("aws logs delete-log-group")]), 3)

    def test_nothing_to_delete(self):
        stubs = Stubs({"aws": {"logs describe-log-groups": {"stdout": "\n"}}, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 delete_log_groups /aws/lambda/X',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(any("delete-log-group" in c for c in stubs.calls()))


COMPONENTS = tuple(run_bash('echo "${STACK_COMPONENTS[@]}"').stdout.split())


def capital(name):
    return name[0].upper() + name[1:]


def stacks_of(env):
    """An environment's stack names, as deploy/aws/src/app.ts names them."""
    return [f"ColvennaCatalog{capital(env)}{c}" for c in COMPONENTS]


class GeneratedNames(unittest.TestCase):
    """CloudFormation names a stack's functions <stack>-<logical id>-<suffix>, the stack's name cut short
    when it is long; uninstall.sh deletes their log groups by stacks.sh's named_after_stacks and no other."""

    def test_no_cut_as_long_as_the_bound_is_another_environment_s(self):
        cut_min = int(run_bash("echo $STACK_CUT_MIN").stdout)
        # Environment names (a lowercase letter, then lowercase letters and digits, 8 at most) built to
        # share as much as they can with each other's stack names: one name running on into another's
        # components, test, release, driver; and a sample of any.
        seeds = ["a", "x1", "ux1", "sl13", "t", "te", "tes", "test", "r", "release", "d", "driver", "1", "9z",
                 *(c.lower() for c in COMPONENTS), *(c.lower()[:4] for c in COMPONENTS)]
        names = {(a + b)[:k] for a in seeds for b in ["", *seeds] for k in range(1, 9)}
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        rng = random.Random(11)
        names |= {rng.choice(alphabet[:26]) + "".join(rng.choice(alphabet) for _ in range(rng.randrange(8)))
                  for _ in range(3000)}
        names = {n for n in names if re.fullmatch(r"[a-z][a-z0-9]{0,7}", n)}
        owned = []  # (stack name, its owner)
        for n in names:
            if n not in ("release", "test"):
                owned += [(s, ("env", n)) for s in stacks_of(n)]
                owned.append((f"ColvennaCatalogTest{capital(n)}Driver", ("env", n)))  # tests/serverless's driver
            if n != "release":
                owned.append((f"ColvennaCatalogTest{capital(n)}", ("test environment", n)))
        owned.append(("ColvennaCatalogRelease", ("release registry",)))
        owned.sort()
        longest = 0
        for i, (stack, owner) in enumerate(owned):
            if owner[0] != "env" or stack.endswith("Driver"):
                continue
            # The other owners' names nearest in order share the most with it.
            for step in (-1, 1):
                j = i + step
                while 0 <= j < len(owned) and owned[j][1] == owner:
                    j += step
                if 0 <= j < len(owned):
                    other = owned[j][0]
                    shared = len(os.path.commonprefix([stack, other]))
                    longest = max(longest, shared)
                    self.assertLess(shared, cut_min, (stack, owned[j]))
                    self.assertFalse(other.startswith(stack), (stack, owned[j]))
        self.assertEqual(longest, cut_min - 1)  # abcdefg and abcdefgh: the bound is the least that holds
        # The environment test is reserved: its stacks would be the test environments' (eks, storage, ...).
        self.assertEqual(stacks_of("test")[1], "ColvennaCatalogTestStorage")

    def generated(self, name, env="ux1e"):
        stacks = " ".join(stacks_of(env))
        return run_bash(f"named_after_stacks {name} {stacks}").returncode == 0

    def test_a_name_generated_for_the_stack_or_a_cut_of_it_at_least_the_bound_long(self):
        for name in ("ColvennaCatalogUx1eStorage-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl",  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
                     "ColvennaCatalogUx1eStorag-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl",  # cut to 25, as seen; pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
                     "ColvennaCatalogUx1eStor-X-Y",  # 23
                     "ColvennaCatalogUx1eEks-Handler886CB40B-AbC", "ColvennaCatalogUx1eServerl-X-Y"):
            with self.subTest(name):
                self.assertTrue(self.generated(name))
        for name in ("ColvennaCatalogUx1eSto-X-Y",  # a cut of 22: another environment's could be so
                     "ColvennaCatalogUx1eStorage",  # no generated name
                     "ColvennaCatalogUx1exStorag-X-Y", "ColvennaCatalogUx1Storage-X-Y", "ColvennaCatalogUx1eStorageX-X-Y",
                     "ColvennaCatalogTestUx1e-X-Y", "ColvennaCatalogTestUx1eDriver-X-Y", "colvenna-catalog-ux1e-server"):
            with self.subTest(name):
                self.assertFalse(self.generated(name))
        # An environment of 8 characters: its stack names' cuts begin at the end of its name.
        self.assertTrue(self.generated("ColvennaCatalogAbcdefgh-X-Y", env="abcdefgh"))
        self.assertFalse(self.generated("ColvennaCatalogAbcdefgh-X-Y", env="abcdefg"))

    GROUPS = ["/aws/lambda/ColvennaCatalogUx1eStorag-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl",  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
              "/aws/lambda/ColvennaCatalogUx1eEks-Handler886CB40B-AbCdEfGhIjKl",  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
              "/aws/lambda/ColvennaCatalogUx1exStorag-LogRetentionaae0aa3c5b4d-MnOpQrStUvWx",  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
              "/aws/lambda/ColvennaCatalogUx1eSto-X-Y"]

    def test_the_log_groups_of_the_stacks_functions(self):
        stubs = Stubs({"aws": {"logs describe-log-groups": {"stdout": "\t".join(self.GROUPS[:3]) + "\n" + self.GROUPS[3] + "\n"}}})
        stacks = " ".join(stacks_of("ux1e"))
        out = run_bash(f"function_log_groups {stacks}", env={**stubs.env, "COLVENNA_REGION": "us-west-2"}, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), self.GROUPS[:2])
        # Listed under the start the stacks share, which the other environment's share too.
        self.assertIn("--log-group-name-prefix /aws/lambda/ColvennaCatalogUx1e --query", stubs.calls()[0])
        out = run_bash("function_log_groups ColvennaCatalogUx1eStorage", env={**stubs.env, "COLVENNA_REGION": "us-west-2"},
                       path_prepend=stubs.bin)
        self.assertEqual(out.stdout.splitlines(), self.GROUPS[:1])
        self.assertIn("--log-group-name-prefix /aws/lambda/ColvennaCatalogUx1eStor --query", stubs.calls()[-1])

    def test_uninstall_deletes_those_of_the_stacks_that_are_gone(self):
        gone = {"stderr": "An error occurred (ValidationError): Stack with id X does not exist\n", "code": 254}
        query = "cloudformation describe-stacks --region us-west-2 --stack-name"
        aws = {f"{query} {s}": gone for s in stacks_of("ux1e")}
        aws[f"{query} ColvennaCatalogUx1eEks"] = {"stdout": "CREATE_COMPLETE\n"}  # kept: its functions' groups stay
        aws["logs describe-log-groups"] = {"stdout": "\t".join(self.GROUPS) + "\n"}
        aws["logs delete-log-group"] = {"stdout": ""}
        stubs = Stubs({"aws": aws, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_ENV=ux1e COLVENNA_REGION=us-west-2 delete_function_log_groups',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        deleted = [c.split("--log-group-name ")[1] for c in stubs.calls() if c.startswith("aws logs delete-log-group")]
        self.assertEqual(deleted, self.GROUPS[:1])
        # An environment of 8 characters, its Storage stack kept: a cut that could be of Storage or of
        # Serverless (ColvennaCatalogAbcdefghS) stays, one of Serverless alone goes.
        aws = {f"{query} {s}": gone for s in stacks_of("abcdefgh")}
        aws[f"{query} ColvennaCatalogAbcdefghStorage"] = {"stdout": "CREATE_COMPLETE\n"}
        either, theirs = "/aws/lambda/ColvennaCatalogAbcdefghS-X-Y", "/aws/lambda/ColvennaCatalogAbcdefghServ-X-Y"
        aws["logs describe-log-groups"] = {"stdout": f"{either}\t{theirs}\n"}
        aws["logs delete-log-group"] = {"stdout": ""}
        stubs = Stubs({"aws": aws, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_ENV=abcdefgh COLVENNA_REGION=us-west-2 delete_function_log_groups',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        deleted = [c.split("--log-group-name ")[1] for c in stubs.calls() if c.startswith("aws logs delete-log-group")]
        self.assertEqual(deleted, [theirs])
        # Nothing gone: nothing listed.
        aws = {f"{query} {s}": gone for s in stacks_of("ux1e")}
        aws["logs describe-log-groups"] = {"stdout": ""}
        aws.update({f"{query} {s}": {"stdout": "CREATE_COMPLETE\n"} for s in stacks_of("ux1e")})
        stubs = Stubs({"aws": aws, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_ENV=ux1e COLVENNA_REGION=us-west-2 delete_function_log_groups',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(any(c.startswith("aws logs") for c in stubs.calls()))


class Steps(unittest.TestCase):
    def test_a_failing_command_in_a_step_exits_1_naming_the_step(self):
        out = run_bash("set -E; trap 'on_error install.sh' ERR; step one; f() { aws_missing_command 2>/dev/null; }; f; echo after")
        self.assertEqual(out.returncode, 1)
        self.assertIn('install.sh: failed at step "one"', out.stderr)
        self.assertNotIn("after", out.stdout)

    def test_a_failure_in_a_command_substitution_is_reported_once(self):
        out = run_bash("set -E; trap 'on_error install.sh' ERR; step two; f() { local x; x=\"$(false)\"; }; f")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stderr.count("failed at step"), 1, out.stderr)

    def test_a_refusal_keeps_its_status(self):
        out = run_bash("set -E; trap 'on_error install.sh' ERR; step three; refuse no")
        self.assertEqual(out.returncode, 3)
        self.assertNotIn("failed at step", out.stderr)

    def test_the_chart_is_not_upgraded_with_arguments_that_failed(self):
        env = {"COLVENNA_RELEASE_NAME": "colvenna-catalog", "COLVENNA_NAMESPACE": "colvenna-catalog", "EVIDENCE": tempfile.mkdtemp()}
        script = (f'source "{REPO}/install.sh"; chart_args() {{ echo --set-json=a=1; return $FAIL; }}; '
                  'helm_upgrade() { echo "UPGRADED $*" >&2; }; kubectl() { :; }; upgrade_chart')
        out = run_bash(script, env={**env, "FAIL": "0"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("UPGRADED colvenna-catalog", out.stderr)
        self.assertIn("--set-json=a=1", out.stderr)
        out = run_bash(script, env={**env, "FAIL": "1"})
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("UPGRADED", out.stderr)


class Stacks(unittest.TestCase):
    def status(self, answer):
        stubs = Stubs({"aws": {"cloudformation describe-stacks": answer}})
        return run_bash("stack_status ColvennaCatalogUatEks", env={**stubs.env, "COLVENNA_REGION": "us-west-2"}, path_prepend=stubs.bin)

    def test_a_stack_is_there_or_not(self):
        out = self.status({"stdout": "CREATE_COMPLETE\n"})
        self.assertEqual((out.returncode, out.stdout), (0, "CREATE_COMPLETE\n"), out.stderr)
        out = self.status({"stderr": "An error occurred (ValidationError) when calling the DescribeStacks operation: "
                                     "Stack with id ColvennaCatalogUatEks does not exist\n", "code": 254})
        self.assertEqual((out.returncode, out.stdout), (0, "NONE\n"), out.stderr)

    def test_a_stack_that_cannot_be_read_is_a_failure(self):
        out = self.status({"stderr": "An error occurred (AccessDenied) when calling the DescribeStacks operation\n", "code": 254})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("AccessDenied", out.stderr)

    def test_an_output_is_there_or_not_and_unreadable_is_a_failure(self):
        def output(answer):
            stubs = Stubs({"aws": {"cloudformation describe-stacks": answer}})
            return run_bash("cfn_output ColvennaCatalogUatStorage DbEndpoint", env={**stubs.env, "COLVENNA_REGION": "us-west-2"},
                            path_prepend=stubs.bin)
        self.assertEqual(output({"stdout": "db.example.com\n"}).stdout, "db.example.com\n")
        for answer in ({"stdout": "None\n"}, {"stderr": "Stack with id ColvennaCatalogUatStorage does not exist\n", "code": 254}):
            out = output(answer)
            self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)
        out = output({"stderr": "An error occurred (AccessDenied) when calling the DescribeStacks operation\n", "code": 254})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Cannot read the stack ColvennaCatalogUatStorage: An error occurred (AccessDenied)", out.stderr)

    def test_resources_that_cannot_be_listed_are_a_failure(self):
        stubs = Stubs({"aws": {"cloudformation describe-stack-resources": {"stderr": "Throttling", "code": 254}}})
        out = run_bash("stack_resources S AWS::S3::Bucket", env={**stubs.env, "COLVENNA_REGION": "us-west-2"}, path_prepend=stubs.bin)
        self.assertNotEqual(out.returncode, 0)


class ClusterStatus(unittest.TestCase):
    def status(self, answer):
        stubs = Stubs({"aws": {"eks describe-cluster": answer}, "kubectl": {}})
        return run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 COLVENNA_EKS_CLUSTER=c1 cluster_status',
                        env=stubs.env, path_prepend=stubs.bin)

    def test_a_cluster_is_there_or_not(self):
        out = self.status({"stdout": "ACTIVE\n"})
        self.assertEqual((out.returncode, out.stdout), (0, "ACTIVE\n"), out.stderr)
        out = self.status({"stderr": "An error occurred (ResourceNotFoundException) when calling the DescribeCluster "
                                     "operation: No cluster found for name: c1.\n", "code": 254})
        self.assertEqual((out.returncode, out.stdout), (0, "NONE\n"), out.stderr)

    def test_a_cluster_that_cannot_be_read_is_a_failure_not_absence(self):
        out = self.status({"stderr": "An error occurred (AccessDeniedException) when calling the DescribeCluster operation\n",
                           "code": 254})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("AccessDeniedException", out.stderr)


class DeleteData(unittest.TestCase):
    """--delete-data run again after an interruption carries on past what is already gone."""

    def run_uninstall(self, script, aws):
        stubs = Stubs({"aws": aws, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2; {script}', env=stubs.env, path_prepend=stubs.bin)
        return out, [c for c in stubs.calls() if not c.startswith("aws rds describe-db-instances")
                     and not c.startswith("aws s3api head-bucket")]

    def test_a_database(self):
        gone = {"stderr": "An error occurred (DBInstanceNotFound) when calling the DescribeDBInstances operation: DBInstance db-1 not found.\n",
                "code": 254}
        cases = [
            ("available", {"stdout": "available\n"}, ["modify-db-instance", "delete-db-instance", "wait db-instance-deleted"]),
            ("deleting", {"stdout": "deleting\n"}, ["wait db-instance-deleted"]),
            ("gone", gone, []),
        ]
        for label, described, expected in cases:
            with self.subTest(label):
                out, calls = self.run_uninstall("delete_database db-1", {
                    "rds describe-db-instances": described, "rds modify-db-instance": {"stdout": ""},
                    "rds delete-db-instance": {"stdout": ""}, "rds wait db-instance-deleted": {"stdout": ""}})
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual([c.split(" ", 2)[2].split(" --")[0] for c in calls], expected)
        out, _ = self.run_uninstall("delete_database db-1", {"rds describe-db-instances": {"stderr": "AccessDenied", "code": 254}})
        self.assertNotEqual(out.returncode, 0)

    def test_a_bucket(self):
        cases = [
            ("there", {"stdout": '{"BucketArn": "arn:aws:s3:::b-1", "BucketRegion": "us-west-2"}\n'}, ["s3 rm", "s3api delete-bucket"]),
            ("gone", {"stderr": "An error occurred (404) when calling the HeadBucket operation: Not Found\n", "code": 254}, []),
        ]
        for label, head, expected in cases:
            with self.subTest(label):
                out, calls = self.run_uninstall("delete_bucket b-1", {
                    "s3api head-bucket": head, "s3 rm": {"stdout": ""}, "s3api delete-bucket": {"stdout": ""}})
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn("BucketArn", out.stdout)
                self.assertEqual([" ".join(c.split(" ")[1:3]) for c in calls], expected)
        out, _ = self.run_uninstall("delete_bucket b-1", {"s3api head-bucket": {
            "stderr": "An error occurred (403) when calling the HeadBucket operation: Forbidden\n", "code": 254}})
        self.assertNotEqual(out.returncode, 0)

    HEAD_FOUND = {"stdout": '{"BucketRegion": "us-west-2"}\n'}
    NOT_EMPTY = {"stderr": "An error occurred (BucketNotEmpty) when calling the DeleteBucket operation: The bucket you tried "
                           "to delete is not empty\n", "code": 254}

    def test_a_bucket_that_takes_objects_again_once_emptied_is_emptied_again(self):
        # The access log bucket, which S3 goes on delivering logs to (#14): not empty at the first delete.
        out, calls = self.run_uninstall("BUCKET_DELETE_WAIT=0; delete_bucket b-1", {
            "s3api head-bucket": self.HEAD_FOUND, "s3 rm": {"stdout": ""},
            "s3api delete-bucket": [self.NOT_EMPTY, {"stdout": ""}]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([" ".join(c.split(" ")[1:3]) for c in calls], ["s3 rm", "s3api delete-bucket"] * 2)
        self.assertIn("The bucket b-1 is not empty again", out.stdout)
        self.assertIn("(attempt 2 of 20 at most)", out.stdout)

    def test_a_bucket_that_never_stays_empty_fails_once_the_attempts_are_spent(self):
        out, calls = self.run_uninstall("BUCKET_DELETE_WAIT=0 BUCKET_DELETE_ATTEMPTS=3; delete_bucket b-1", {
            "s3api head-bucket": self.HEAD_FOUND, "s3 rm": {"stdout": ""}, "s3api delete-bucket": self.NOT_EMPTY})
        self.assertEqual(out.returncode, 1)
        self.assertEqual([" ".join(c.split(" ")[1:3]) for c in calls], ["s3 rm", "s3api delete-bucket"] * 3)
        self.assertRegex(out.stderr, r"The bucket b-1 is still not empty after 3 attempt\(s\) in [0-9]+ seconds")
        self.assertIn("S3 delivers access logs late", out.stderr)
        # The time is bounded too: no wait that would end past BUCKET_DELETE_SECONDS.
        out, calls = self.run_uninstall("BUCKET_DELETE_WAIT=5 BUCKET_DELETE_ATTEMPTS=100 BUCKET_DELETE_SECONDS=4; delete_bucket b-1", {
            "s3api head-bucket": self.HEAD_FOUND, "s3 rm": {"stdout": ""}, "s3api delete-bucket": self.NOT_EMPTY})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(len([c for c in calls if "delete-bucket" in c]), 1)
        self.assertRegex(out.stderr, r"still not empty after 1 attempt\(s\) in [0-9]+ seconds")

    def test_a_bucket_that_cannot_be_deleted_is_not_tried_again(self):
        out, calls = self.run_uninstall("BUCKET_DELETE_WAIT=0; delete_bucket b-1", {
            "s3api head-bucket": self.HEAD_FOUND, "s3 rm": {"stdout": ""},
            "s3api delete-bucket": {"stderr": "An error occurred (AccessDenied) when calling the DeleteBucket operation\n",
                                    "code": 254}})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(len([c for c in calls if "delete-bucket" in c]), 1)
        self.assertIn("Cannot delete the bucket b-1: An error occurred (AccessDenied)", out.stderr)
        out, calls = self.run_uninstall("delete_bucket b-1", {
            "s3api head-bucket": self.HEAD_FOUND, "s3 rm": {"stderr": "delete failed: AccessDenied\n", "code": 1}})
        self.assertEqual(out.returncode, 1)
        self.assertIn("Cannot empty the bucket b-1: delete failed: AccessDenied", out.stderr)
        self.assertFalse(any("delete-bucket" in c for c in calls))

    LOGS_POLICY = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Principal": {"AWS": "*"}, "Action": "s3:*", "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Effect": "Allow", "Principal": {"Service": "logging.s3.amazonaws.com"}, "Action": "s3:PutObject"}]}

    def stop(self, buckets, aws):
        out, calls = self.run_uninstall(f"stop_access_logs {buckets}", {
            "s3api put-bucket-logging": {"stdout": ""}, "s3api put-bucket-policy": {"stdout": ""},
            "s3api delete-bucket-policy": {"stdout": ""}, **aws})
        return out, [c for c in calls if " get-bucket-" not in c]

    def test_access_logs_stop_before_the_buckets_are_emptied(self):
        logging, policy = "s3api get-bucket-logging --bucket", "s3api get-bucket-policy --bucket"
        no_policy = {"stderr": "An error occurred (NoSuchBucketPolicy) when calling the GetBucketPolicy operation\n", "code": 254}
        out, changes = self.stop("logs-1 wh-1 gone-1", {
            f"{logging} logs-1 ": {"stdout": "None\n"}, f"{logging} wh-1 ": {"stdout": "logs-1\n"},
            f"{logging} gone-1 ": {"stderr": "An error occurred (NoSuchBucket) when calling the GetBucketLogging operation\n",
                                   "code": 254},
            f"{policy} logs-1 ": {"stdout": json.dumps(self.LOGS_POLICY) + "\n"}, f"{policy} wh-1 ": no_policy})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        kept = json.dumps({"Version": "2012-10-17", "Statement": self.LOGS_POLICY["Statement"][:1]}, separators=(",", ":"))
        # The warehouse stops logging; the access log bucket's policy loses the logging service's grant
        # and keeps its TLS-only rule.
        self.assertEqual(changes, [
            f"aws s3api put-bucket-policy --bucket logs-1 --region us-west-2 --policy {kept}",
            "aws s3api put-bucket-logging --bucket wh-1 --region us-west-2 --bucket-logging-status {}"])
        # A run again: nothing left to change.
        out, changes = self.stop("logs-1 wh-1", {
            f"{logging} ": {"stdout": "None\n"}, f"{policy} logs-1 ": {"stdout": kept + "\n"}, f"{policy} wh-1 ": no_policy})
        self.assertEqual((out.returncode, changes), (0, []), out.stderr)
        # A policy of that grant alone goes; a bucket that cannot be read is said, and the others go on.
        only = {"Version": "2012-10-17", "Statement": self.LOGS_POLICY["Statement"][1:]}
        out, changes = self.stop("wh-1 logs-1", {
            f"{logging} wh-1 ": {"stderr": "AccessDenied", "code": 254}, f"{logging} logs-1 ": {"stdout": "None\n"},
            f"{policy} logs-1 ": {"stdout": json.dumps(only) + "\n"}})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Cannot read the logging of the bucket wh-1: AccessDenied", out.stderr)
        self.assertEqual(changes, ["aws s3api delete-bucket-policy --bucket logs-1 --region us-west-2"])


class GivenLeftovers(unittest.TestCase):
    """After --delete-data, uninstall.sh prints how to remove the catalog's part of what it was given, which
    it never deletes: on RDS the admin takes the catalog's role before it drops the database the role owns
    (#73), and the prefix of a bucket that is still there."""

    def leftovers(self, settings, given="1", head=None):
        head = head or {"stdout": "{}"}
        stubs = Stubs({"aws": {"s3api head-bucket": head}, "kubectl": {}})
        assignments = " ".join(f"{k}={v}" for k, v in settings.items())
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 COLVENNA_WAREHOUSE_PREFIX=ex1 '
                       f'COLVENNA_DB=existing COLVENNA_BUCKET= COLVENNA_FORM=eks {assignments}; print_given_leftovers "{given}"',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    GIVEN = {"COLVENNA_DB_ENDPOINT": "db.example.com", "COLVENNA_DB_NAME": "ex1_catalog", "COLVENNA_DB_USER": "ex1_user"}

    def test_an_existing_database_and_bucket(self):
        out = self.leftovers({**self.GIVEN, "COLVENNA_BUCKET": "my-bucket"})
        self.assertIn("as the admin user of db.example.com", out)
        statements = [line.strip() for line in out.splitlines() if line.startswith("    ")]
        self.assertEqual(statements, ['GRANT "ex1_user" TO CURRENT_USER;', 'DROP DATABASE "ex1_catalog";', 'DROP ROLE "ex1_user";'])
        self.assertIn("  aws s3 rm s3://my-bucket/ex1/ --recursive", out.splitlines())

    def test_the_serverless_form_drops_the_authorizer_s_user_too(self):
        out = self.leftovers({**self.GIVEN, "COLVENNA_FORM": "serverless"})
        self.assertEqual([line.strip() for line in out.splitlines() if line.startswith("    ")],
                         ['GRANT "ex1_user" TO CURRENT_USER;', 'DROP DATABASE "ex1_catalog";', 'DROP ROLE "ex1_user";',
                          'DROP ROLE "ex1_user_authz";'])
        self.assertNotIn("aws s3 rm", out)

    def test_what_the_install_made_or_restored_says_nothing(self):
        # The database a restore left in the configuration is a copy of the install's own, not given.
        self.assertEqual(self.leftovers(self.GIVEN, given=""), "")
        # A bucket --delete-data removed (the install's own, named after a restore) is not there to clean.
        gone = {"stderr": "An error occurred (404) when calling the HeadBucket operation: Not Found\n", "code": 254}
        self.assertEqual(self.leftovers({"COLVENNA_BUCKET": "gone-bucket"}, given="", head=gone), "")
        # A bucket that cannot be read is named all the same.
        denied = {"stderr": "An error occurred (403) when calling the HeadBucket operation: Forbidden\n", "code": 254}
        self.assertIn("aws s3 rm s3://mine/ex1/", self.leftovers({"COLVENNA_BUCKET": "mine"}, given="", head=denied))

    def test_only_a_database_no_stack_of_the_install_holds_was_given(self):
        query = "cloudformation describe-stacks --region us-west-2 --stack-name ColvennaCatalogEx1Storage --query"
        for label, answer, given in [
            ("no Storage stack", {"stderr": "An error occurred (ValidationError): Stack with id x does not exist\n", "code": 254}, 0),
            ("a Storage stack of the bucket alone", {"stdout": "None\n"}, 0),
            ("the install's own database", {"stdout": "colvennacatalogex1storage-db.abc.us-west-2.rds.amazonaws.com\n"}, 1),
        ]:
            with self.subTest(label):
                stubs = Stubs({"aws": {query: answer}})
                out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_ENV=ex1 COLVENNA_REGION=us-west-2 COLVENNA_DB=existing; '
                               'given_database', env=stubs.env, path_prepend=stubs.bin)
                self.assertEqual(out.returncode, given, out.stderr)


class DeleteDataWithARestoredInstance(unittest.TestCase):
    """uninstall.sh --delete-data refuses, before it changes anything, while an RDS instance the install did
    not make (a restored one, user manual 6.2) uses the Storage stack's subnet group: it never deletes
    such an instance, and the stack could not be deleted under it."""

    CONFIG = ("COLVENNA_ENV=sl1\nCOLVENNA_ACCOUNT=111122223333\nCOLVENNA_REGION=us-west-2\nCOLVENNA_FORM=serverless\n"  # pragma: allowlist secret - a placeholder install configuration: a test environment's name, AWS's documentation account
              "COLVENNA_BUCKET=colvennacatalogsl1-warehouse\nCOLVENNA_DB=existing\n"  # pragma: allowlist secret - a placeholder install configuration: a test environment's name, AWS's documentation account
              "COLVENNA_DB_ENDPOINT=restored.abcdefghijkl.us-west-2.rds.amazonaws.com\n"
              "COLVENNA_DB_ADMIN_SECRET_ARN=arn:aws:secretsmanager:us-west-2:111122223333:secret:db-admin\n")

    def uninstall(self, instances):
        """instances: (name, subnet group, its VPC, security group) of each RDS instance in the Region."""
        config = write(Path(tempfile.mkdtemp()) / "sl1.env", self.CONFIG)
        resources = "cloudformation describe-stack-resources --region us-west-2 --stack-name ColvennaCatalogSl1Storage --query"
        stubs = Stubs({"aws": {
            **stack_outputs("ColvennaCatalogSl1Network", VpcId="vpc-0000000a"),
            f"{resources} StackResources[?ResourceType=='AWS::EC2::SecurityGroup']": {"stdout": "sg-000000db\n"},
            "--version": {"stdout": "aws-cli/2.34.30 Python/3.14.4 Linux/7.0.0 exe/x86_64\n"},
            "sts get-caller-identity": {"stdout": "111122223333\n"},
            "cloudformation describe-stacks --region us-west-2 --stack-name ColvennaCatalogSl1Storage": {"stdout": "CREATE_COMPLETE\n"},
            f"{resources} StackResources[?ResourceType=='AWS::RDS::DBSubnetGroup']": {"stdout": "ColvennaCatalogSl1Storage-DbSubnets-AbC\n"},
            f"{resources} StackResources[?ResourceType=='AWS::RDS::DBInstance']": {"stdout": "colvennacatalogsl1storage-database-xyz\n"},
            "rds describe-db-instances": {"stdout": {"DBInstances": [
                {"DBInstanceIdentifier": name, "DBSubnetGroup": {"DBSubnetGroupName": group, "VpcId": vpc},
                 "VpcSecurityGroups": [{"VpcSecurityGroupId": sg}]} for name, group, vpc, sg in instances]}},
        }, "kubectl": {}})
        out = run_bash(f'"{REPO}/uninstall.sh" --config {config} --yes --delete-data', env=stubs.env, path_prepend=stubs.bin, stdin="")
        return out, stubs

    STACKS = ("colvennacatalogsl1storage-database-xyz", "colvennacatalogsl1storage-dbsubnets-abc", "vpc-0000000a", "sg-000000db")
    ELSEWHERE = ("elsewhere", "other-subnets", "vpc-0000000f", "sg-0000000f")

    def test_a_restored_instance_in_the_stack_s_subnet_group_stops_it(self):
        out, stubs = self.uninstall([self.STACKS, ("colvennacatalogsl1-restored", "colvennacatalogsl1storage-dbsubnets-abc",
                                                   "vpc-0000000a", "sg-000000db"), self.ELSEWHERE])
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("colvennacatalogsl1-restored", out.stderr)
        self.assertNotIn("elsewhere", out.stderr)
        self.assertIn("delete it first", out.stderr)
        self.assertFalse([c for c in stubs.calls() if " delete" in c or "destroy" in c or " modify" in c], stubs.calls())

    def test_one_with_the_stack_s_security_group_or_in_the_vpc_it_made_stops_it_too(self):
        for label, instance in (("the stack's security group", ("r1", "own-subnets", "vpc-0000000b", "sg-000000db")),
                                ("the VPC the install made", ("r2", "own-subnets", "vpc-0000000a", "sg-0000000e"))):
            with self.subTest(label):
                out, _ = self.uninstall([self.STACKS, instance, self.ELSEWHERE])
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn(f"--delete-data: {instance[0]},", out.stderr)

    def test_without_one_it_goes_on_to_ask_for_the_name(self):
        out, _ = self.uninstall([self.STACKS, self.ELSEWHERE])
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("Not confirmed: nothing was deleted", out.stderr)


if __name__ == "__main__":
    unittest.main()
