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

    def test_nothing_to_delete(self):
        stubs = Stubs({"aws": {"logs describe-log-groups": {"stdout": "\n"}}, "kubectl": {}})
        out = run_bash(f'source "{REPO}/uninstall.sh"; COLVENNA_REGION=us-west-2 delete_log_groups /aws/lambda/X',
                       env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(any("delete-log-group" in c for c in stubs.calls()))


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
