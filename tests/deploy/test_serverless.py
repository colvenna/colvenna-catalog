"""The serverless form's installer (deploy/lib/serverless.sh, and its configuration in config.sh): with
stub commands for AWS, and the steps that invoke Lambda functions replaced by recorders."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, parameters, run_bash, stack_outputs, write
from stubs import Stubs

sys.path.insert(0, str(REPO / "scripts" / "release"))
import manifest  # noqa: E402

SERVERLESS = {"COLVENNA_ENV": "sl1", "COLVENNA_ACCOUNT": "111122223333", "COLVENNA_REGION": "us-west-2",
              "COLVENNA_FORM": "serverless"}
EXISTING = {**SERVERLESS, "COLVENNA_NETWORK": "existing", "COLVENNA_VPC_ID": "vpc-0000000a",
            "COLVENNA_SUBNET_IDS": "subnet-0000000a,subnet-0000000b", "COLVENNA_API_ENDPOINT_IDS": "vpce-0000000f,vpce-0000000a"}
TOKEN = "colvenna_" + "Zz9_-" * 8


class Config(unittest.TestCase):
    def load(self, values, extra=""):
        config = write(Path(tempfile.mkdtemp()) / "c.env", "".join(f"{k}={v}\n" for k, v in values.items()) + extra)
        return run_bash(f'load_config "{config}"; printenv | grep "^COLVENNA_" | sort')

    def loaded(self, values):
        out = self.load(values)
        self.assertEqual(out.returncode, 0, out.stderr)
        return dict(line.split("=", 1) for line in out.stdout.splitlines())

    def refused(self, values, *needles):
        out = self.load(values)
        self.assertEqual(out.returncode, 2, out.stdout)
        for needle in needles:
            self.assertIn(needle, out.stderr)

    def test_the_serverless_defaults(self):
        got = self.loaded(SERVERLESS)
        self.assertEqual((got["COLVENNA_NETWORK"], got["COLVENNA_VPC_CIDR"], got["COLVENNA_LAMBDA_MEMORY"],
                          got["COLVENNA_LAMBDA_CONCURRENCY"], got["COLVENNA_SNAPSTART"], got["COLVENNA_DB"]),
                         ("create", "10.80.0.0/16", "2048", "10", "off", "create"))
        self.assertEqual(got["COLVENNA_EKS_CLUSTER"], "")

    def test_the_example_loads_for_either_form(self):
        example = (REPO / "deploy/config/example.env").read_text()
        for form, extra in (("eks", {"COLVENNA_EKS_CLUSTER": "c1"}), ("serverless", {})):
            text = example.replace("COLVENNA_FORM=eks", f"COLVENNA_FORM={form}")
            for key, value in {**SERVERLESS, "COLVENNA_FORM": form, **extra}.items():
                if f"\n{key}=\n" in f"\n{text}":
                    text = text.replace(f"\n{key}=\n", f"\n{key}={value}\n")
            config = write(Path(tempfile.mkdtemp()) / "e.env", text)
            out = run_bash(f'load_config "{config}"')
            self.assertEqual(out.returncode, 0, f"{form}: {out.stderr}")

    def test_each_form_refuses_the_other_s_settings(self):
        self.refused({**SERVERLESS, "COLVENNA_FORM": "eks"}, "COLVENNA_EKS_CLUSTER: required with COLVENNA_FORM=eks")
        self.refused({**SERVERLESS, "COLVENNA_EKS_CLUSTER": "c1"}, "COLVENNA_EKS_CLUSTER: not for COLVENNA_FORM=serverless")
        self.refused({**SERVERLESS, "COLVENNA_INTERNAL_NLB": "on", "COLVENNA_NLB_ALLOWED_CIDRS": "10.0.0.0/16"},
                     "COLVENNA_INTERNAL_NLB: not for COLVENNA_FORM=serverless")
        eks = {**SERVERLESS, "COLVENNA_FORM": "eks", "COLVENNA_EKS_CLUSTER": "c1"}
        self.refused({**eks, "COLVENNA_SNAPSTART": "on"}, "COLVENNA_SNAPSTART: not for COLVENNA_FORM=eks")
        self.refused({**eks, "COLVENNA_NETWORK": "existing"}, "COLVENNA_NETWORK: not for COLVENNA_FORM=eks")
        # Set to the defaults, as example.env sets them, the other form's keys pass.
        self.loaded({**eks, "COLVENNA_SNAPSTART": "off", "COLVENNA_NETWORK": "create"})
        self.loaded({**SERVERLESS, "COLVENNA_NAMESPACE": "colvenna-catalog", "COLVENNA_INTERNAL_NLB": "off"})
        self.refused({**SERVERLESS, "COLVENNA_FORM": "lambda"}, "COLVENNA_FORM: eks or serverless")

    def test_a_new_network(self):
        for cidr in ("10.80.0.0/15", "10.80.0.0/25", "10.80.1.0/16", "300.1.0.0/16", "10.80.0.0"):  # redaction: allow - made-up CIDRs, which the check must refuse
            self.refused({**SERVERLESS, "COLVENNA_VPC_CIDR": cidr}, "COLVENNA_VPC_CIDR")
        self.loaded({**SERVERLESS, "COLVENNA_VPC_CIDR": "172.20.0.0/20"})
        self.refused({**SERVERLESS, "COLVENNA_VPC_ID": "vpc-0000000a"}, "COLVENNA_VPC_ID: only with COLVENNA_NETWORK=existing")
        self.loaded({**SERVERLESS, "COLVENNA_API_ENDPOINT_IDS": "vpce-0123456789abcdef0"})

    def test_a_new_network_takes_an_existing_database_in_it(self):
        # A restored instance in the VPC the install made (preflight checks where it is).
        got = self.loaded({**SERVERLESS, "COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": "db.example.com",
                           "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:a",  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
                           "COLVENNA_BUCKET": "warehouse-bucket"})
        self.assertEqual((got["COLVENNA_NETWORK"], got["COLVENNA_DB"], got["COLVENNA_VPC_ID"]), ("create", "existing", ""))
        self.refused({**SERVERLESS, "COLVENNA_DB": "existing"}, "COLVENNA_DB_ENDPOINT: required with COLVENNA_DB=existing",
                     "COLVENNA_DB_ADMIN_SECRET_ARN: required with COLVENNA_DB=existing")

    def test_an_existing_network(self):
        self.loaded(EXISTING)
        for key in ("COLVENNA_VPC_ID", "COLVENNA_SUBNET_IDS", "COLVENNA_API_ENDPOINT_IDS"):
            self.refused({**EXISTING, key: ""}, f"{key}:")
        self.refused({**EXISTING, "COLVENNA_SUBNET_IDS": "subnet-0000000a"}, "COLVENNA_SUBNET_IDS: two or more")
        self.refused({**EXISTING, "COLVENNA_SUBNET_IDS": "subnet-0000000a,subnet-0000000a"}, "COLVENNA_SUBNET_IDS")
        self.refused({**EXISTING, "COLVENNA_API_ENDPOINT_IDS": "vpc-0000000a"}, "COLVENNA_API_ENDPOINT_IDS: distinct VPC endpoint IDs")
        self.refused({**EXISTING, "COLVENNA_VPC_CIDR": "10.9.0.0/16"}, "COLVENNA_VPC_CIDR: only with COLVENNA_NETWORK=create")
        self.loaded({**EXISTING, "COLVENNA_VPC_CIDR": "10.80.0.0/16"})

    def test_the_server_function_s_settings(self):
        for value in ("512", "10241", "two"):
            self.refused({**SERVERLESS, "COLVENNA_LAMBDA_MEMORY": value}, "COLVENNA_LAMBDA_MEMORY")
        for value in ("0", "1001", "-1"):
            self.refused({**SERVERLESS, "COLVENNA_LAMBDA_CONCURRENCY": value}, "COLVENNA_LAMBDA_CONCURRENCY")
        self.refused({**SERVERLESS, "COLVENNA_SNAPSTART": "yes"}, "COLVENNA_SNAPSTART: on or off")
        self.refused({**SERVERLESS, "COLVENNA_DB_USER": "u" * 58}, "COLVENNA_DB_USER: 57 characters at most")


def stubbed(script, aws=None, env=None):
    stubs = Stubs({"aws": aws or {}, "kubectl": {}})
    state = tempfile.mkdtemp()
    out = run_bash(script, env={**SERVERLESS, "COLVENNA_NETWORK": "create", "COLVENNA_LAMBDA_CONCURRENCY": "10",
                                "COLVENNA_DB": "create", "COLVENNA_DB_PORT": "5432", "STATE_DIR": state, **stubs.env, **(env or {})},
                   path_prepend=stubs.bin)
    return out, stubs


SERVICES = [{"name": f"com.amazonaws.us-west-2.{s}", "zones": zones} for s, zones in
            (("sts", ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"]),
             ("cognito-identity", ["us-west-2a", "us-west-2b", "us-west-2c"]),
             ("execute-api", ["us-west-2b", "us-west-2c", "us-west-2d"]))]
RDS_ZONES = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2c", "us-west-2d"]


class Preflight(unittest.TestCase):
    def test_api_gateway_must_be_able_to_log(self):
        out, _ = stubbed("sl_assert_api_logging", aws={"apigateway get-account": {"stdout": "None\n"}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("aws apigateway update-account", out.stderr)
        out, _ = stubbed("sl_assert_api_logging", aws={"apigateway get-account": {"stdout": "arn:aws:iam::111122223333:role/r\n"}})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_concurrency_the_functions_reserve_must_leave_100(self):
        def run(unreserved, held="None"):
            return stubbed("sl_assert_concurrency", aws={
                "lambda get-account-settings": {"stdout": {"AccountLimit": {"ConcurrentExecutions": 1000,
                                                                            "UnreservedConcurrentExecutions": unreserved}}},
                "lambda get-function-concurrency": {"stdout": f"{held}\n"},
            })[0]
        # The server and the authorizer 10 each, the maintenance and purge functions 1 each.
        self.assertEqual(run(122).returncode, 0, "2 x 10 + 2 reserved, 100 left")
        out = run(121)
        self.assertEqual(out.returncode, 3)
        self.assertIn("reserves 22", out.stderr)
        # A rerun: the four functions hold 10 + 10 + 1 + 1 already, which count as available.
        self.assertEqual(run(100, held="6").returncode, 0, "4 x 6 held")
        self.assertEqual(run(100, held="5").returncode, 3, "4 x 5 held")

    def test_a_new_vpc_s_zones_offer_every_endpoint_and_the_database(self):
        out, _ = stubbed('sl_choose_zones; echo "$SL_ZONES"', aws={
            "cloudformation describe-stacks": {"stderr": "Stack with id ColvennaCatalogSl1Network does not exist", "code": 254},
            "ec2 describe-vpc-endpoint-services": {"stdout": SERVICES},
            "rds describe-orderable-db-instance-options": {"stdout": RDS_ZONES},
        })
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "us-west-2b,us-west-2c")

    def test_a_network_stack_keeps_its_zones(self):
        out, stubs = stubbed('sl_choose_zones; echo "$SL_ZONES"', aws={
            "cloudformation describe-stacks": {"stdout": "us-west-2a,us-west-2d\n"},
        })
        self.assertEqual(out.stdout.strip(), "us-west-2a,us-west-2d")
        self.assertFalse(any("describe-vpc-endpoint-services" in c for c in stubs.calls()))

    def test_too_few_zones(self):
        services = [dict(s, zones=["us-west-2a"]) for s in SERVICES]
        out, _ = stubbed("sl_choose_zones", aws={
            "cloudformation describe-stacks": {"code": 254, "stderr": "does not exist"},
            "ec2 describe-vpc-endpoint-services": {"stdout": services},
            "rds describe-orderable-db-instance-options": {"stdout": RDS_ZONES},
        })
        self.assertEqual(out.returncode, 3)
        self.assertIn("Fewer than two zones", out.stderr)


def network(public=False, zones=("us-west-2a", "us-west-2b"), sts=True, cognito=True, s3=True, nat=False,
            api=(("vpce-0000000f", "vpc-0000000f"), ("vpce-0000000a", "vpc-0000000a")), api_service="execute-api"):
    """The aws responses describing an existing VPC for sl_existing_network."""
    endpoints = []
    if sts:
        endpoints.append({"VpcEndpointId": "vpce-00000001", "ServiceName": "com.amazonaws.us-west-2.sts",
                          "VpcEndpointType": "Interface", "PrivateDnsEnabled": True, "State": "available"})
    if cognito:
        endpoints.append({"VpcEndpointId": "vpce-00000002", "ServiceName": "com.amazonaws.us-west-2.cognito-identity",
                          "VpcEndpointType": "Interface", "PrivateDnsEnabled": True, "State": "available"})
    if s3:
        endpoints.append({"VpcEndpointId": "vpce-00000003", "ServiceName": "com.amazonaws.us-west-2.s3",
                          "VpcEndpointType": "Gateway", "RouteTableIds": ["rtb-0000000a"], "State": "available"})
    routes = [{"DestinationCidrBlock": "10.9.0.0/16", "GatewayId": "local", "State": "active"}]
    if nat:
        routes.append({"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0000000a", "State": "active"})
    return {
        "ec2 describe-vpcs": {"stdout": {"Vpcs": [{"VpcId": "vpc-0000000a", "CidrBlock": "10.9.0.0/16"}]}},
        "ec2 describe-subnets": {"stdout": {"Subnets": [
            {"SubnetId": "subnet-0000000b", "VpcId": "vpc-0000000a", "AvailabilityZone": zones[1], "MapPublicIpOnLaunch": public,
             "CidrBlock": "10.9.2.0/24"},
            {"SubnetId": "subnet-0000000a", "VpcId": "vpc-0000000a", "AvailabilityZone": zones[0], "MapPublicIpOnLaunch": False,
             "CidrBlock": "10.9.1.0/24"}]}},
        "ec2 describe-route-tables": {"stdout": {"RouteTables": [
            {"RouteTableId": "rtb-0000000a", "Associations": [{"Main": True}], "Routes": routes}]}},
        "ec2 describe-vpc-endpoints --region us-west-2 --filters": {"stdout": {"VpcEndpoints": endpoints}},
        "ec2 describe-vpc-endpoints --region us-west-2 --vpc-endpoint-ids": {"stdout": {"VpcEndpoints": [
            {"VpcEndpointId": i, "VpcId": v, "ServiceName": f"com.amazonaws.us-west-2.{api_service}", "VpcEndpointType": "Interface",
             "State": "available"} for i, v in api]}},
    }


class ExistingNetwork(unittest.TestCase):
    def run_check(self, **over):
        return stubbed('sl_existing_network; echo "$SL_VPC_CIDR $SL_SUBNET_ZONES $SL_API_ENDPOINTS"', aws=network(**over),
                       env={k: v for k, v in EXISTING.items()})[0]

    def test_a_vpc_with_its_endpoints(self):
        out = self.run_check()
        self.assertEqual(out.returncode, 0, out.stderr)
        # The zones in COLVENNA_SUBNET_IDS' order; the endpoint in the VPC first.
        self.assertEqual(out.stdout.strip(), "10.9.0.0/16 us-west-2a,us-west-2b vpce-0000000a,vpce-0000000f")

    def test_a_nat_gateway_stands_in_for_the_endpoints(self):
        self.assertEqual(self.run_check(sts=False, cognito=False, s3=False, nat=True).returncode, 0)

    def test_what_is_missing_is_named(self):
        for over, needle in (({"public": True}, "gives public addresses"),
                             ({"zones": ("us-west-2a", "us-west-2a")}, "two zones or more"),
                             ({"sts": False}, "sts: no interface endpoint"),
                             ({"cognito": False}, "cognito-identity: no interface endpoint"),
                             ({"s3": False}, "s3: no gateway endpoint"),
                             ({"api_service": "sts"}, "is not an execute-api interface endpoint"),
                             ({"api": (("vpce-0000000f", "vpc-0000000f"),)}, "none is in vpc-0000000a")):
            out = self.run_check(**over)
            self.assertEqual(out.returncode, 3, over)
            self.assertIn(needle, out.stderr, over)


DB_ENDPOINT = "db1.abcdefghijkl.us-west-2.rds.amazonaws.com"


def database(iam=True, rules=({"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [],
                               "IpRanges": [{"CidrIp": "10.9.0.0/16"}]},), vpc="vpc-0000000a", public=False, force_ssl="1"):
    """The aws responses describing an existing database for sl_assert_db_reachable."""
    return {
        "rds describe-db-instances": {"stdout": {"DBInstances": [{
            "DBInstanceIdentifier": "db1", "Engine": "postgres", "DBInstanceStatus": "available",
            "Endpoint": {"Address": DB_ENDPOINT, "Port": 5432}, "PubliclyAccessible": public,
            "DBParameterGroups": [{"DBParameterGroupName": "db1-parameters"}],
            "DbiResourceId": "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ", "IAMDatabaseAuthenticationEnabled": iam,  # pragma: allowlist secret - a stubbed RDS answer: a made-up resource id
            "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-000000db"}]}]}},
        "rds describe-db-clusters": {"stdout": {"DBClusters": []}},
        "rds describe-db-parameters --region us-west-2 --db-parameter-group-name db1-parameters": parameters(force_ssl),
        "ec2 describe-security-groups": {"stdout": {"SecurityGroups": [{"GroupId": "sg-000000db", "VpcId": vpc,
                                                                         "IpPermissions": list(rules)}]}},
        "ec2 describe-subnets": {"stdout": {"Subnets": [{"SubnetId": "subnet-0000000a", "CidrBlock": "10.9.1.0/24"},
                                                        {"SubnetId": "subnet-0000000b", "CidrBlock": "10.9.2.0/24"}]}},
        "cloudformation describe-stacks": {"stdout": "None\n"},
    }


class ExistingDatabase(unittest.TestCase):
    ENV = {**EXISTING, "COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": DB_ENDPOINT, "COLVENNA_DB_PORT": "5432"}

    def check(self, **over):
        return stubbed('sl_assert_db_reachable; echo "$SL_DB_RESOURCE_ID"', aws=database(**over), env=self.ENV)[0]

    def test_a_database_the_functions_can_sign_in_to(self):
        out = self.check()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ")  # pragma: allowlist secret - a made-up RDS resource id

    def test_iam_authentication_must_be_on(self):
        out = self.check(iam=False)
        self.assertEqual(out.returncode, 3)
        self.assertIn("does not have IAM database authentication enabled", out.stderr)

    def test_its_security_groups_must_admit_the_functions(self):
        out = self.check(rules=())
        self.assertEqual(out.returncode, 3)
        self.assertIn("do not admit TCP 5432", out.stderr)
        out = self.check(vpc="vpc-0000000f")
        self.assertEqual(out.returncode, 3)
        self.assertIn("is in vpc-0000000f", out.stderr)

    def test_it_must_take_tls_connections_only(self):
        for value, said in (("0", "0"), (None, "unset")):
            out = self.check(force_ssl=value)
            self.assertEqual(out.returncode, 3, value)
            self.assertIn(f"does not require TLS: rds.force_ssl is {said} in its parameter group db1-parameters", out.stderr)

    def test_it_must_not_be_publicly_accessible(self):
        out = self.check(public=True)
        self.assertEqual(out.returncode, 3)
        self.assertIn("is publicly accessible", out.stderr)
        self.assertIn("--no-publicly-accessible", out.stderr)

    def test_a_refusal_stays_a_refusal_under_install_sh_s_error_trap(self):
        # install.sh's ERR trap makes any failed command a step failure (1); a refusal is 3.
        for over in ({"public": True}, {"force_ssl": "0"}, {"iam": False}):
            out, _ = stubbed("set -E; trap 'on_error install.sh' ERR; sl_assert_db_reachable", aws=database(**over), env=self.ENV)
            self.assertEqual(out.returncode, 3, f"{over}: {out.stderr}")
            self.assertNotIn("failed at step", out.stderr)
        out, _ = stubbed("set -E; trap 'on_error install.sh' ERR; sl_assert_db_reachable", env=self.ENV,
                         aws={**database(), "rds describe-db-instances": {"stdout": {"DBInstances": []}},
                              "rds describe-db-clusters": {"stdout": {"DBClusters": []}}})
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("is no RDS instance's endpoint", out.stderr)


def made_network(vpc="vpc-0000000a", subnets="subnet-0000000a,subnet-0000000b", group="sg-0000ca70"):
    """The outputs of the Network stack an earlier install made (COLVENNA_NETWORK=create)."""
    return stack_outputs("ColvennaCatalogSl1Network", VpcId=vpc, SubnetIds=subnets, CatalogSecurityGroupId=group)


class ExistingDatabaseInANewNetwork(unittest.TestCase):
    """COLVENNA_NETWORK=create with COLVENNA_DB=existing: an instance restored into the VPC the install made."""

    ENV = {**SERVERLESS, "COLVENNA_NETWORK": "create", "COLVENNA_VPC_ID": "", "COLVENNA_SUBNET_IDS": "",
           "COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": DB_ENDPOINT, "COLVENNA_DB_PORT": "5432"}
    FROM_CATALOG = ({"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": "sg-0000ca70"}],
                     "IpRanges": []},)

    def check(self, network=None, **over):
        aws = {**database(**{"rules": self.FROM_CATALOG, **over}), **(made_network() if network is None else network)}
        return stubbed('sl_assert_db_reachable; echo "$SL_DB_RESOURCE_ID"', aws=aws, env=self.ENV)

    def test_a_database_in_the_install_s_own_vpc(self):
        out, stubs = self.check()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ")  # pragma: allowlist secret - a made-up RDS resource id
        # Admitted by CIDRs instead: those holding the subnets the install made.
        out, _ = self.check(rules=({"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [],
                                    "IpRanges": [{"CidrIp": "10.9.0.0/16"}]},))
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_before_the_install_made_its_network_there_is_nowhere_for_it_to_be(self):
        out, stubs = self.check(network={})
        self.assertEqual(out.returncode, 3)
        self.assertIn("COLVENNA_DB=existing with COLVENNA_NETWORK=create", out.stderr)
        self.assertIn("ColvennaCatalogSl1Network", out.stderr)
        self.assertIn("COLVENNA_DB=create", out.stderr)

    def test_a_database_outside_it_is_refused(self):
        out, _ = self.check(vpc="vpc-0000000f")
        self.assertEqual(out.returncode, 3)
        self.assertIn("is in vpc-0000000f, not the VPC this install made (vpc-0000000a)", out.stderr)

    def test_iam_authentication_must_be_on(self):
        out, _ = self.check(iam=False)
        self.assertEqual(out.returncode, 3)
        self.assertIn("does not have IAM database authentication enabled", out.stderr)

    def test_its_security_groups_must_admit_the_functions(self):
        out, _ = self.check(rules=())
        self.assertEqual(out.returncode, 3)
        self.assertIn("do not admit TCP 5432", out.stderr)
        self.assertIn("sg-0000ca70", out.stderr)


OLD_ENDPOINT = "colvennacatalogsl1storage-database.abcdefghijkl.us-west-2.rds.amazonaws.com"


def outputs(stack, **values):
    return stack_outputs(f"ColvennaCatalogSl1{stack}", **values)


class RepointingPreflight(unittest.TestCase):
    """preflight_serverless for an install with a new network pointed at a restored instance (user
    manual, 6.2): COLVENNA_NETWORK=create, COLVENNA_DB=existing, with the stacks an earlier install made."""

    ENV = {**ExistingDatabaseInANewNetwork.ENV, "COLVENNA_BUCKET": "colvennacatalogsl1-warehouse",
           "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:db-admin"}  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account

    def preflight(self, env=None, network=True, running=OLD_ENDPOINT):
        aws = {
            "sts get-caller-identity": {"stdout": "111122223333\n"},
            "cloudformation describe-stacks --stack-name CDKToolkit": {"stdout": "UPDATE_COMPLETE\n"},
            "apigateway get-account": {"stdout": "arn:aws:iam::111122223333:role/api-logs\n"},
            "lambda get-account-settings": {"stdout": {"AccountLimit": {"ConcurrentExecutions": 1000,
                                                                        "UnreservedConcurrentExecutions": 900}}},
            "lambda get-function-concurrency": {"stdout": "None\n"},
            # No Network stack yet: a VPC to spare, and zones to put it in.
            "ec2 describe-vpcs": {"stdout": "3\n"},
            "service-quotas get-service-quota": {"stdout": "5.0\n"},
            "ec2 describe-vpc-endpoint-services": {"stdout": SERVICES},
            "rds describe-orderable-db-instance-options": {"stdout": RDS_ZONES},
            "s3api get-bucket-location": {"stdout": {"LocationConstraint": "us-west-2"}},
            "secretsmanager describe-secret": {"stdout": "{}"},
            "secretsmanager get-secret-value": {"stdout": '{"username": "colvenna_admin", "password": "p4ss"}\n'},  # pragma: allowlist secret - a stubbed AWS CLI answer: a fake password
            **database(rules=ExistingDatabaseInANewNetwork.FROM_CATALOG),
            "cloudformation describe-stacks --region us-west-2 --stack-name ColvennaCatalogSl1Network --query Stacks[0].StackStatus":
                {"stdout": "NONE\n"} if not network else {"stdout": "CREATE_COMPLETE\n"},
            **outputs("Storage", DbEndpoint=OLD_ENDPOINT, BucketName="colvennacatalogsl1-warehouse"),
            **(outputs("Serverless", DatabaseEndpoint=running) if running else {}),
        }
        if network:
            aws.update(made_network())
            aws.update(outputs("Network", SubnetZones="us-west-2a,us-west-2b"))
        out, _ = stubbed("preflight_serverless", aws=aws, env={**self.ENV, **(env or {})})
        return out

    def test_a_restored_instance_in_the_install_s_network(self):
        out = self.preflight()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("p4ss", out.stdout + out.stderr)

    def test_before_the_network_exists_it_is_refused(self):
        out = self.preflight(network=False)
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("COLVENNA_DB=existing with COLVENNA_NETWORK=create", out.stderr)

    def test_without_the_bucket_the_storage_stack_s_database_would_go(self):
        out = self.preflight(env={"COLVENNA_BUCKET": ""})
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("COLVENNA_DB=existing without COLVENNA_BUCKET", out.stderr)
        self.assertIn("COLVENNA_BUCKET=colvennacatalogsl1-warehouse", out.stderr)  # pragma: allowlist secret - a made-up bucket name, in the message the test expects

    def test_create_never_moves_the_install_back_to_the_storage_stack_s_database(self):
        create = {"COLVENNA_DB": "create", "COLVENNA_DB_ENDPOINT": "", "COLVENNA_DB_ADMIN_SECRET_ARN": ""}
        out = self.preflight(env=create, running=DB_ENDPOINT)
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn(f"The install runs on the database at {DB_ENDPOINT}, not on ColvennaCatalogSl1Storage's ({OLD_ENDPOINT})",
                      out.stderr)
        # On the stack's own database, or a Serverless stack from before it named its database: as before.
        for running in (OLD_ENDPOINT, ""):
            with self.subTest(running=running):
                self.assertEqual(self.preflight(env=create, running=running).returncode, 0)


class Stacks(unittest.TestCase):
    """sl_stacks with a new network and an existing database (a restored instance): the Network stack is
    deployed as before, the Storage stack is left alone, and the functions get the restored instance."""

    def test_the_storage_stack_is_left_and_the_functions_get_the_restored_instance(self):
        script = ('deploy_stack() { echo "deploy $*" >>"$WORK/steps"; };'
                  ' stack_output() { case "$2" in VpcId) echo vpc-0000000a;; SubnetIds) echo subnet-0000000a,subnet-0000000b;;'
                  ' SubnetZones) echo us-west-2a,us-west-2b;; CatalogSecurityGroupId) echo sg-0000ca70;;'
                  ' ExecuteApiEndpointId) echo vpce-0000000e;; RepositoryUri) echo repo;; *) echo "UNEXPECTED $1 $2";; esac; };'
                  ' sl_stacks -c account=1; echo "$DB_ENDPOINT $DB_PORT $DB_ADMIN_SECRET_ARN $DB_RESOURCE_ID $BUCKET $WAREHOUSE_ROOT"')
        work = tempfile.mkdtemp()
        out, _ = stubbed(script, env={**ExistingDatabaseInANewNetwork.ENV, "WORK": work, "SL_ZONES": "us-west-2a,us-west-2b",
                                      "SL_DB_RESOURCE_ID": "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ", "COLVENNA_VPC_CIDR": "10.80.0.0/16",
                                      "COLVENNA_API_ENDPOINT_IDS": "", "COLVENNA_BUCKET": "colvennacatalogsl1-warehouse",
                                      "COLVENNA_WAREHOUSE_PREFIX": "warehouse",
                                      "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:db-admin"})  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("UNEXPECTED", out.stdout)
        deployed = [line.split()[1] for line in (Path(work) / "steps").read_text().splitlines()]
        self.assertEqual(deployed, ["ColvennaCatalogSl1Network", "ColvennaCatalogSl1Images"])
        self.assertIn("-c networkMode=create", (Path(work) / "steps").read_text())
        self.assertEqual(out.stdout.split(), [DB_ENDPOINT, "5432", "arn:aws:secretsmanager:us-west-2:111122223333:secret:db-admin",
                                              "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ", "colvennacatalogsl1-warehouse",  # pragma: allowlist secret - a made-up RDS resource id
                                              "s3://colvennacatalogsl1-warehouse/warehouse"])


class Steps(unittest.TestCase):
    """The steps that invoke the form's functions, with lambda_invoke replaced: it records each payload
    and answers from ANSWERS (by function), and the aws stub records every other call."""

    RECORDER = r'''
lambda_invoke() {
  cp "$2" "$WORK/payload-$(ls "$WORK" | grep -c '^payload-')-$1.json"
  printf '%s' "${ANSWERS[$1]}" >"$3"
}
'''

    def run_steps(self, script, answers, aws=None, env=None):
        work = tempfile.mkdtemp()
        evidence = tempfile.mkdtemp()
        declare = "declare -A ANSWERS=(" + " ".join(f"[{k}]={json.dumps(json.dumps(v))}" for k, v in answers.items()) + ")"
        out, stubs = stubbed(f"{declare}\n{self.RECORDER}\n{script}", aws=aws, env={
            "WORK": work, "EVIDENCE": evidence, "MAINTENANCE_FUNCTION": "maintenance", "PURGE_FUNCTION": "purge", "OPS_FUNCTION": "ops",
            "ADMIN_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:t", **(env or {})})  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
        payloads = {p.name: json.loads(p.read_text()) for p in sorted(Path(work).glob("payload-*"))}
        return out, stubs, payloads, Path(work), Path(evidence)

    def test_migrate(self):
        out, _, payloads, _, evidence = self.run_steps("sl_maintenance migrate", {"maintenance": {
            "command": "migrate", "exitCode": 0, "log": "U2NoZW1hIGF0IDI="}})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(list(payloads.values()), ["migrate"])
        self.assertEqual((evidence / "maintenance-migrate.log").read_text(), "Schema at 2")

    def test_a_failed_migrate_shows_its_output(self):
        out, *_ = self.run_steps("sl_maintenance migrate", {"maintenance": {"command": "migrate", "exitCode": 1, "log": "Ym9vbQ=="}})
        self.assertEqual(out.returncode, 1)
        self.assertIn("exited 1", out.stderr)
        self.assertIn("boom", out.stderr)

    def test_a_new_admin_token_goes_from_the_answer_into_the_secret_and_nowhere_else(self):
        out, stubs, _, work, _ = self.run_steps("sl_issue_admin_token", {"maintenance": {
            "command": "bootstrap-admin", "exitCode": 0, "log": "", "token": TOKEN}},
            aws={"secretsmanager put-secret-value": {"stdout": "{}"}})
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = stubs.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--secret-string file://", calls[0])
        self.assertNotIn(TOKEN, "\n".join(calls) + out.stdout + out.stderr)
        self.assertEqual([p.name for p in work.iterdir() if not p.name.startswith("payload-")], [], "no token file left")

    def test_the_placeholder_is_not_a_held_token(self):
        for value, held in (("Xy3kq9PlaceholderNoToken00000000", 1), (TOKEN, 0)):
            out, *_ = self.run_steps("sl_admin_token_held", {}, aws={"secretsmanager get-secret-value": {"stdout": value + "\n"}})
            self.assertEqual(out.returncode, held, value)

    def test_requests_carry_the_token_from_its_file(self):
        script = ('printf "%s\\n" "$TOKEN" >"$WORK/admin-token"; sl_admin_call POST /colvenna/admin/v1/grants \'{"a": 1}\';'
                  ' ops_request GET /v1/config')
        out, _, payloads, *_ = self.run_steps(script, {"ops": {"status": 200, "body": '{"ok": true}'}}, env={"TOKEN": TOKEN})
        self.assertEqual(out.returncode, 0, out.stderr)
        first, second = payloads.values()
        self.assertEqual(first, {"action": "request", "method": "POST", "path": "/colvenna/admin/v1/grants", "token": TOKEN,
                                 "body": {"a": 1}})
        self.assertEqual(second, {"action": "request", "method": "GET", "path": "/v1/config"}, "no token, no Authorization")
        self.assertIn('{"ok": true}', out.stdout)

    def test_an_admin_call_that_fails_says_so(self):
        out, *_ = self.run_steps('printf x >"$WORK/admin-token"; sl_admin_call GET /colvenna/admin/v1/principals',
                                 {"ops": {"status": 403, "body": "no"}})
        self.assertEqual(out.returncode, 1)
        self.assertIn("403 no", out.stderr)

    def test_the_database_step_takes_the_admin_s_password_alone_and_no_user_gets_one(self):
        aws = {"secretsmanager get-secret-value --region us-west-2 --secret-id admin":
               {"stdout": json.dumps({"username": "colvenna_admin", "password": "pa"}) + "\n"}}
        out, stubs, payloads, work, _ = self.run_steps("sl_database; sl_grants", {"ops": {"statements": 13}}, aws=aws,
                                                       env={"DB_ADMIN_SECRET_ARN": "admin"})  # pragma: allowlist secret - the made-up name of a stubbed secret
        self.assertEqual(out.returncode, 0, out.stderr)
        database, grants = payloads.values()
        # The users sign in with IAM tokens: no password of theirs exists to pass.
        self.assertEqual(database, {"action": "database", "admin": {"username": "colvenna_admin", "password": "pa"}})  # pragma: allowlist secret - a stubbed secret's fake password, which the test checks is passed on unchanged
        self.assertEqual(grants, {"action": "grants"})
        self.assertEqual([c.split()[1] for c in stubs.calls()], ["secretsmanager"], "the admin's secret, read once")
        self.assertNotIn('"pa"', "\n".join(stubs.calls()))
        self.assertEqual(sorted(p.name for p in work.iterdir() if not p.name.startswith("payload-")), [], "no secret left")

    def smoke(self, statuses, urls="0", types='["PRIVATE"]', purge_fails=False):
        answers = iter(statuses)
        script = ('ops_request() { case "$#" in 2) echo \'{"status": ' + str(statuses[0]) + '}\';; *) [[ "$3" == *forged* ]]'
                  ' && echo \'{"status": ' + str(statuses[1]) + '}\' || echo \'{"status": ' + str(statuses[2]) + '}\';; esac; };'
                  ' sl_smoke "$EVIDENCE"')
        if purge_fails:
            script = ('lambda_invoke() { echo "$1 failed (PurgeFailed): purge exited 1" >&2; return 1; }; ' + script)
        next(answers)
        policy = json.dumps(json.dumps({"Statement": [{"Effect": "Deny", "Condition": {"StringNotEquals": {"aws:SourceVpce": "v"}}}]},
                                       separators=(",", ":")))[1:-1]
        return self.run_steps(script, {"purge": {"command": "purge", "exitCode": 0, "log": ""}}, aws={
            "secretsmanager get-secret-value": {"stdout": TOKEN + "\n"},
            "lambda list-function-url-configs": {"stdout": urls + "\n"},
            "apigateway get-rest-api": {"stdout": json.dumps({"endpointConfiguration": {"types": json.loads(types)}, "policy": policy})},
        }, env={"SERVER_FUNCTION": "s", "AUTHORIZER_FUNCTION": "a", "API_ID": "abc", "COLVENNA_WAREHOUSE_NAME": "colvenna"})

    def test_the_smoke_check(self):
        out, stubs, payloads, _, evidence = self.smoke([401, 401, 200])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(payloads, {"payload-0-purge.json": "purge"})
        # Every function of the form, the purge function's among them, is checked for function URLs.
        checked = [c.split("--function-name ")[1].split()[0] for c in stubs.calls() if "list-function-url-configs" in c]
        self.assertEqual(sorted(checked), ["a", "maintenance", "ops", "purge", "s"])
        self.assertIn("anonymous=401 forged=401 admin=200", (evidence / "smoke.txt").read_text())
        # The purge function runs once, its payload the command (#69).
        self.assertIn("purge function: ran", (evidence / "smoke.txt").read_text())
        out, *_ = self.smoke([401, 401, 200], purge_fails=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("the purge function's run failed", out.stderr)
        self.assertIn("PurgeFailed", out.stderr)
        for statuses, needle in (([200, 401, 200], "unauthenticated requests got 200"), ([401, 200, 200], "got 401 and 200"),
                                 ([401, 401, 403], "the admin's request got 403")):
            out, *_ = self.smoke(statuses)
            self.assertEqual(out.returncode, 1, statuses)
            self.assertIn(needle, out.stderr)
        out, *_ = self.smoke([401, 401, 200], urls="1")
        self.assertIn("has 1 function URL(s)", out.stderr)
        out, *_ = self.smoke([401, 401, 200], types='["REGIONAL"]')
        self.assertIn("the API is not private", out.stderr)


def lambda_function(name, image, sha="a" * 40, database="db.example.com"):
    return {"Type": "AWS::Lambda::Function", "DependsOn": [f"{name}RoleDefaultPolicy", f"{name}Role"], "Properties": {
        "FunctionName": f"colvenna-catalog-sl1-{name}", "Description": f"colvenna-catalog {sha}", "PackageType": "Image",
        "Code": {"ImageUri": {"Fn::Join": ["", ["111122223333.dkr.ecr.us-west-2.", {"Ref": "AWS::URLSuffix"}, f"/repo@{image}"]]}},
        "Environment": {"Variables": {"COLVENNA_JDBC_URL": f"jdbc:postgresql://{database}:5432/colvenna_catalog"}},
        "LoggingConfig": {"LogGroup": {"Ref": f"{name}Logs"}}, "Role": {"Fn::GetAtt": [f"{name}Role", "Arn"]}}}


def role_policy(name, resource_id="db-OLD"):
    return {"Type": "AWS::IAM::Policy", "Properties": {"PolicyName": f"{name}RoleDefaultPolicy", "Roles": [{"Ref": f"{name}Role"}],
                                                        "PolicyDocument": {"Statement": [{"Action": "rds-db:connect", "Effect": "Allow",
                                                                                          "Resource": f"dbuser:{resource_id}/u"}]}}}


def serverless_template(image, sha="a" * 40, database="db.example.com", resource_id="db-OLD"):
    """A serverless stack's template, as far as sl_maintenance_first reads it."""
    resources = {}
    for name in ("server", "maintenance", "purge"):
        resources[name] = lambda_function(name, image, sha, database)
        resources[f"{name}Role"] = {"Type": "AWS::IAM::Role", "Properties": {"RoleName": f"colvenna-catalog-sl1-{name}"}}
        resources[f"{name}RoleDefaultPolicy"] = role_policy(name, resource_id)
        resources[f"{name}Logs"] = {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": f"/aws/lambda/colvenna-catalog-sl1-{name}"}}
    resources["serverCurrentVersion" + sha[:8]] = {"Type": "AWS::Lambda::Version", "Properties": {"FunctionName": {"Ref": "server"}}}
    resources["serverLive"] = {"Type": "AWS::Lambda::Alias", "Properties": {
        "Name": "live", "FunctionName": {"Ref": "server"}, "FunctionVersion": {"Fn::GetAtt": ["serverCurrentVersion" + sha[:8], "Version"]}}}
    return {"Parameters": {"BootstrapVersion": {"Type": "AWS::SSM::Parameter::Value<String>", "Default": "/cdk-bootstrap/hnb659fds/version"}},
            "Resources": resources,
            "Outputs": {"MaintenanceImage": {"Value": f"repo@{image}"}, "ServerImage": {"Value": f"repo@{image}"}}}


DEPLOYED = serverless_template("sha256:old")
# This version's, onto a restored instance (UAT-DEP-07's newer-schema branch): another image, commit and database.
SYNTHESIZED = serverless_template("sha256:new", sha="b" * 40, database="restored.example.com", resource_id="db-NEW")
SERVERLESS_STACK = "ColvennaCatalogSl1Serverless"


class ServerlessTemplate(unittest.TestCase):
    """deploy/lib/serverless_template.py: the deployed template with the maintenance function as this version makes it, and nothing
    else changed (#63)."""

    def change(self, deployed, synthesized=SYNTHESIZED, function="colvenna-catalog-sl1-maintenance", as_string=False):
        work = Path(tempfile.mkdtemp())
        write(work / "deployed.json", json.dumps(json.dumps(deployed) if as_string else deployed))
        write(work / "synthesized.json", json.dumps(synthesized))
        return subprocess.run([sys.executable, str(REPO / "deploy/lib/serverless_template.py"), "maintenance-first",
                               str(work / "deployed.json"), str(work / "synthesized.json"), function], capture_output=True, text=True)

    def test_the_maintenance_function_its_policy_and_its_output_are_this_version_s_and_nothing_else_changes(self):
        for as_string in (False, True):
            out = self.change(DEPLOYED, as_string=as_string)
            self.assertEqual(out.returncode, 0, out.stderr)
            changed = json.loads(out.stdout)
            expected = json.loads(json.dumps(DEPLOYED))
            expected["Resources"]["maintenance"]["Properties"] = SYNTHESIZED["Resources"]["maintenance"]["Properties"]
            expected["Resources"]["maintenanceRoleDefaultPolicy"]["Properties"]["PolicyDocument"] = \
                SYNTHESIZED["Resources"]["maintenanceRoleDefaultPolicy"]["Properties"]["PolicyDocument"]
            expected["Outputs"]["MaintenanceImage"]["Value"] = "repo@sha256:new"
            self.assertEqual(changed, expected)
            # The server function, its version and the alias, the purge function: as deployed. The new database too.
            for rid in ("server", "serverCurrentVersionaaaaaaaa", "serverLive", "purge", "purgeRoleDefaultPolicy", "serverRoleDefaultPolicy"):
                self.assertEqual(changed["Resources"][rid], DEPLOYED["Resources"][rid], rid)
            self.assertNotIn("serverCurrentVersionbbbbbbbb", changed["Resources"])
            self.assertIn("restored.example.com", json.dumps(changed["Resources"]["maintenance"]))
            self.assertNotIn("\n", out.stdout.strip(), "compact")

    def test_a_template_without_that_one_function_is_refused(self):
        out = self.change(DEPLOYED, function="colvenna-catalog-sl2-maintenance")
        self.assertEqual(out.returncode, 1)
        self.assertIn("the deployed template has 0 functions named colvenna-catalog-sl2-maintenance", out.stderr)
        twice = json.loads(json.dumps(DEPLOYED))
        twice["Resources"]["maintenance2"] = twice["Resources"]["maintenance"]
        self.assertIn("2 functions named", self.change(twice).stderr)

    def test_what_the_deployed_stack_lacks_is_refused(self):
        newer = json.loads(json.dumps(SYNTHESIZED))
        newer["Resources"]["maintenance"]["Properties"]["LoggingConfig"] = {"LogGroup": {"Ref": "MaintenanceLogsRenamed"}}
        out = self.change(DEPLOYED, synthesized=newer)
        self.assertEqual(out.returncode, 1)
        self.assertIn("refers to ['MaintenanceLogsRenamed'], which the deployed stack does not have", out.stderr)
        other_role = json.loads(json.dumps(SYNTHESIZED))
        other_role["Resources"]["maintenance"]["Properties"]["Role"] = {"Fn::GetAtt": ["otherRole", "Arn"]}
        self.assertIn("role is otherRole in the synthesized template", self.change(DEPLOYED, synthesized=other_role).stderr)
        # Fn::GetAtt's other form names the same role; a role that is no GetAtt is refused, never a traceback.
        dotted = json.loads(json.dumps(SYNTHESIZED))
        dotted["Resources"]["maintenance"]["Properties"]["Role"] = {"Fn::GetAtt": "maintenanceRole.Arn"}
        self.assertEqual(self.change(DEPLOYED, synthesized=dotted).returncode, 0)
        given = json.loads(json.dumps(SYNTHESIZED))
        given["Resources"]["maintenance"]["Properties"]["Role"] = "arn:aws:iam::111122223333:role/given"
        out = self.change(DEPLOYED, synthesized=given)
        self.assertEqual(out.returncode, 1)
        self.assertIn("does not take the ARN of a role of its own", out.stderr)
        self.assertNotIn("Traceback", out.stderr)


class MaintenanceFirst(unittest.TestCase):
    """sl_maintenance_first: CloudFormation updates the deployed template so changed; no CDK, no other change."""

    def run_step(self, update=None, wait=None, statuses=("UPDATE_COMPLETE",), deployed=None, rm=None):
        outputs = stack_outputs(SERVERLESS_STACK, MaintenanceFunctionName="colvenna-catalog-sl1-maintenance")
        describe = f"cloudformation describe-stacks --region us-west-2 --stack-name {SERVERLESS_STACK}"
        aws = {
            **outputs,
            f"{describe} --query Stacks[0].StackStatus": [{"stdout": f"{s}\n"} for s in statuses],
            f"{describe} --query Stacks[0].Parameters[].ParameterKey": {"stdout": "BootstrapVersion\n"},
            "cloudformation get-template": {"stdout": deployed if deployed is not None else DEPLOYED},
            "cloudformation update-stack": update or {"stdout": "{}"},
            "cloudformation wait stack-update-complete": wait or {"stdout": ""},
            **stack_outputs("CDKToolkit", BucketName="cdk-staging"),
            "s3 cp": {"stdout": ""},
            "s3 rm": rm or {"stdout": ""},
        }
        work, state = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        (state / "cdk.out").mkdir()
        write(state / "cdk.out" / f"{SERVERLESS_STACK}.template.json", json.dumps(SYNTHESIZED))
        out, stubs = stubbed('stack_settled() { echo UPDATE_COMPLETE; }; sl_maintenance_first;'
                             ' echo "functions $MAINTENANCE_FUNCTION"', aws=aws, env={"WORK": work, "STATE_DIR": str(state)})
        return out, stubs, Path(work)

    def test_the_deployed_template_with_the_maintenance_function_as_this_version_makes_it(self):
        out, stubs, work = self.run_step()
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = stubs.calls()
        update = next(c for c in calls if " update-stack " in c)
        self.assertIn(f"--stack-name {SERVERLESS_STACK} --template-body file://{work}/serverless-template.json", update)
        self.assertIn("--parameters ParameterKey=BootstrapVersion,UsePreviousValue=true", update)
        self.assertIn("--capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND", update)
        # The stack's own role and tags: none given, so CloudFormation keeps them.
        self.assertNotIn("--role-arn", update)
        self.assertNotIn("--tags", update)
        sent = json.loads((work / "serverless-template.json").read_text())
        self.assertEqual(sent["Resources"]["maintenance"]["Properties"], SYNTHESIZED["Resources"]["maintenance"]["Properties"])
        self.assertEqual({k: v for k, v in sent["Resources"].items() if not k.startswith("maintenance")},
                         {k: v for k, v in DEPLOYED["Resources"].items() if not k.startswith("maintenance")})
        self.assertTrue(any(" wait stack-update-complete " in c for c in calls))
        self.assertIn("functions colvenna-catalog-sl1-maintenance", out.stdout)
        # Never the CDK, which would deploy this version's whole stack.
        self.assertEqual([c for c in calls if c.startswith("npx")], [])

    def test_nothing_to_update_is_no_failure_and_a_failed_update_is(self):
        out, *_ = self.run_step(update={"code": 254, "stderr": "An error occurred (ValidationError): No updates are to be performed.\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already as this version makes it", out.stdout)
        out, *_ = self.run_step(update={"code": 254, "stderr": "An error occurred (AccessDenied): not allowed\n"})
        self.assertEqual(out.returncode, 1)
        self.assertIn("not allowed", out.stderr)
        out, *_ = self.run_step(wait={"code": 255, "stderr": "Waiter StackUpdateComplete failed\n"},
                                statuses=("UPDATE_ROLLBACK_COMPLETE",))
        self.assertEqual(out.returncode, 1)
        self.assertIn("did not complete: UPDATE_ROLLBACK_COMPLETE", out.stderr)

    def test_a_template_too_large_for_the_request_goes_by_the_staging_bucket(self):
        # The stack's own template is (51,365 bytes measured, with CDK's metadata): as CDK sends its own.
        big = json.loads(json.dumps(DEPLOYED))
        big["Resources"]["server"]["Properties"]["Description"] = "x" * 52000
        out, stubs, _ = self.run_step(deployed=big)
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = stubs.calls()
        put = next(c for c in calls if " s3 cp " in c)
        self.assertRegex(put, r"s3://cdk-staging/colvenna-catalog/ColvennaCatalogSl1Serverless/maintenance-first-[0-9a-f]{64}\.json$")
        key = put.split("s3://cdk-staging/")[1]
        update = next(c for c in calls if " update-stack " in c)
        self.assertIn(f"--template-url https://s3.us-west-2.amazonaws.com/cdk-staging/{key}", update)
        self.assertNotIn("--template-body", update)
        # Gone from the bucket once the update is over, whatever it came to.
        self.assertTrue(any(f" s3 rm --region us-west-2 --only-show-errors s3://cdk-staging/{key}" in c for c in calls))
        self.assertLess(calls.index(update), calls.index(next(c for c in calls if " s3 rm " in c)))
        out, stubs, _ = self.run_step(deployed=big, update={"code": 254, "stderr": "An error occurred (ValidationError): bad\n"})
        self.assertEqual(out.returncode, 1)
        self.assertTrue(any(" s3 rm " in c for c in stubs.calls()))
        self.assertIn("ValidationError): bad", out.stderr)
        # A copy that cannot be deleted changes nothing of the answer.
        out, *_ = self.run_step(deployed=big, rm={"code": 1, "stderr": "AccessDenied\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("could not be deleted", out.stderr)

    def test_it_waits_for_an_operation_under_way(self):
        out, _ = stubbed(f'sleep() {{ :; }}; stack_settled {SERVERLESS_STACK}', aws={
            f"cloudformation describe-stacks --region us-west-2 --stack-name {SERVERLESS_STACK} --query Stacks[0].StackStatus":
                [{"stdout": "UPDATE_IN_PROGRESS\n"}, {"stdout": "UPDATE_ROLLBACK_IN_PROGRESS\n"}, {"stdout": "UPDATE_ROLLBACK_COMPLETE\n"}]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "UPDATE_ROLLBACK_COMPLETE")


class OrphanLogGroups(unittest.TestCase):
    """sl_delete_orphan_log_groups (#54): before the serverless stack is deployed, a log group it makes for a function of this
    install that is gone, which no stack owns (Lambda made it anew after the stack had deleted it), is deleted."""

    TEMPLATE = {"Resources": {
        "ServerLogs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/aws/lambda/colvenna-catalog-sl1-server"}},
        "PurgeLogs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/aws/lambda/colvenna-catalog-sl1-purge"}},
        "OpsLogs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/aws/lambda/colvenna-catalog-sl1-ops"}},
        "ApiAccessLogs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/aws/apigateway/colvenna-catalog-sl1-api"}},
        "Server": {"Type": "AWS::Lambda::Function", "Properties": {"FunctionName": "colvenna-catalog-sl1-server"}},
    }}

    def run_step(self, owned, existing, functions, stack_status="UPDATE_COMPLETE"):
        work = tempfile.mkdtemp()
        template = write(Path(work) / "t.json", json.dumps(self.TEMPLATE))
        aws = {
            f"cloudformation describe-stacks --region us-west-2 --stack-name {SERVERLESS_STACK} --query Stacks[0].StackStatus":
                {"stdout": f"{stack_status}\n"} if stack_status != "NONE" else {"code": 254, "stderr": "Stack with id x does not exist\n"},
            "cloudformation describe-stack-resources": {"stdout": "\t".join(owned) + "\n" if owned else "\n"},
            "logs delete-log-group": {"stdout": ""},
        }
        for group in [g["Properties"]["LogGroupName"] for g in self.TEMPLATE["Resources"].values() if g["Type"] == "AWS::Logs::LogGroup"]:
            aws[f"logs describe-log-groups --region us-west-2 --log-group-name-prefix {group} "] = {"stdout": "1\n" if group in existing else "0\n"}
        for function in ("colvenna-catalog-sl1-server", "colvenna-catalog-sl1-purge", "colvenna-catalog-sl1-ops"):
            aws[f"lambda get-function --region us-west-2 --function-name {function}"] = (
                {"stdout": "{}"} if function in functions else
                {"code": 254, "stderr": "An error occurred (ResourceNotFoundException) when calling the GetFunction operation\n"})
        out, stubs = stubbed(f'sl_delete_orphan_log_groups {SERVERLESS_STACK} "{template}"', aws=aws, env={"WORK": work})
        deleted = [c.split("--log-group-name ")[1] for c in stubs.calls() if " delete-log-group " in c]
        return out, stubs, deleted

    def test_a_gone_function_s_group_that_no_stack_owns_is_deleted(self):
        # Going back to a version without the purge function, then upgrading again (UAT-DEP-07's forward-again).
        out, _, deleted = self.run_step(owned=["/aws/lambda/colvenna-catalog-sl1-server", "/aws/lambda/colvenna-catalog-sl1-ops"],
                                        existing={"/aws/lambda/colvenna-catalog-sl1-server", "/aws/lambda/colvenna-catalog-sl1-ops",
                                                  "/aws/lambda/colvenna-catalog-sl1-purge"},
                                        functions={"colvenna-catalog-sl1-server", "colvenna-catalog-sl1-ops"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(deleted, ["/aws/lambda/colvenna-catalog-sl1-purge"])
        self.assertIn("deleted /aws/lambda/colvenna-catalog-sl1-purge", out.stdout)

    def test_what_the_stack_owns_what_is_not_there_and_another_function_s_group_are_left(self):
        out, stubs, deleted = self.run_step(owned=["/aws/lambda/colvenna-catalog-sl1-server"],
                                            existing={"/aws/lambda/colvenna-catalog-sl1-server", "/aws/lambda/colvenna-catalog-sl1-ops",
                                                      "/aws/apigateway/colvenna-catalog-sl1-api"},
                                            functions={"colvenna-catalog-sl1-server", "colvenna-catalog-sl1-ops"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(deleted, [])
        # A function still there keeps its group, for CloudFormation to refuse; not a function's group: never looked at.
        self.assertIn("/aws/lambda/colvenna-catalog-sl1-ops is the function colvenna-catalog-sl1-ops's", out.stderr)
        self.assertEqual([c for c in stubs.calls() if "apigateway" in c], [])

    def test_a_first_install_owns_nothing_and_finds_nothing(self):
        out, stubs, deleted = self.run_step(owned=[], existing=set(), functions=set(), stack_status="NONE")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(deleted, [])
        self.assertEqual([c for c in stubs.calls() if "describe-stack-resources" in c], [])
        # One an earlier install of this name left, its function gone with it: deleted too.
        out, _, deleted = self.run_step(owned=[], existing={"/aws/lambda/colvenna-catalog-sl1-server"}, functions=set(),
                                        stack_status="NONE")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(deleted, ["/aws/lambda/colvenna-catalog-sl1-server"])

    def test_a_listing_that_fails_stops_the_run(self):
        work = tempfile.mkdtemp()
        template = write(Path(work) / "t.json", json.dumps(self.TEMPLATE))
        out, stubs = stubbed(f'sl_delete_orphan_log_groups {SERVERLESS_STACK} "{template}"', env={"WORK": work}, aws={
            f"cloudformation describe-stacks --region us-west-2 --stack-name {SERVERLESS_STACK} --query Stacks[0].StackStatus":
                {"stdout": "UPDATE_COMPLETE\n"},
            "cloudformation describe-stack-resources": {"stdout": "\n"},
            "logs describe-log-groups": {"code": 254, "stderr": "An error occurred (ThrottlingException)\n"},
        })
        self.assertEqual(out.returncode, 1)
        self.assertIn("ThrottlingException", out.stderr)
        self.assertEqual([c for c in stubs.calls() if " delete-log-group " in c], [])

    def test_a_function_that_cannot_be_read_is_no_reason_to_delete(self):
        work = tempfile.mkdtemp()
        template = write(Path(work) / "t.json", json.dumps(self.TEMPLATE))
        out, stubs = stubbed(f'sl_delete_orphan_log_groups {SERVERLESS_STACK} "{template}"', env={"WORK": work}, aws={
            f"cloudformation describe-stacks --region us-west-2 --stack-name {SERVERLESS_STACK} --query Stacks[0].StackStatus":
                {"stdout": "UPDATE_COMPLETE\n"},
            "cloudformation describe-stack-resources": {"stdout": "\n"},
            "logs describe-log-groups": {"stdout": "1\n"},
            "lambda get-function": {"code": 254, "stderr": "An error occurred (AccessDeniedException)\n"},
        })
        self.assertEqual(out.returncode, 1)
        self.assertIn("AccessDeniedException", out.stderr)
        self.assertEqual([c for c in stubs.calls() if " delete-log-group " in c], [])

    def test_the_orphans_go_before_the_synthesized_stack_is_deployed(self):
        script = ('sl_delete_orphan_log_groups() { echo "orphans $1 $2" >>"$WORK/steps"; };'
                  ' deploy_assembly() { echo "deploy $1" >>"$WORK/steps"; }; stack_output() { echo "$2"; };'
                  ' sl_deploy_serverless')
        work = tempfile.mkdtemp()
        out, _ = stubbed(script, env={"WORK": work})
        self.assertEqual(out.returncode, 0, out.stderr)
        steps = (Path(work) / "steps").read_text().splitlines()
        self.assertEqual([s.split()[0] for s in steps], ["orphans", "deploy"])
        self.assertTrue(steps[0].endswith(f"/cdk.out/{SERVERLESS_STACK}.template.json"), steps[0])


class RedeployStage(unittest.TestCase):
    """sl_redeploy_stage (#34): once the stack is deployed, the stage serves a new deployment of the API as it is, since a
    gateway response the stack dropped is deleted only after CloudFormation's own new deployment, which keeps it."""

    def run_step(self, marked):
        aws = {
            "apigateway create-deployment --region us-west-2 --rest-api-id abc --stage-name catalog": {"stdout": "new1\n"},
            "apigateway get-deployments --region us-west-2 --rest-api-id abc": {"stdout": f"{marked}\n"},
            "apigateway delete-deployment": {"stdout": "{}"},
        }
        out, stubs = stubbed("sl_redeploy_stage", aws=aws, env={"API_ID": "abc", "SOURCE_SHA": "b" * 40})
        return out, stubs.calls()

    def test_the_stage_takes_a_new_deployment_and_only_install_sh_s_others_go(self):
        out, calls = self.run_step(marked="mine0\tmine1")
        self.assertEqual(out.returncode, 0, out.stderr)
        create = next(c for c in calls if " create-deployment " in c)
        self.assertIn("--description install.sh (" + "b" * 40 + "): the API as its stack left it", create)
        listing = next(c for c in calls if " get-deployments " in c)
        # Only those marked as install.sh's, never the new one: CloudFormation's and any other stay.
        self.assertIn("items[?starts_with(description || '', 'install.sh (') && id != 'new1'].id", listing)
        self.assertEqual([c.split("--deployment-id ")[1] for c in calls if " delete-deployment " in c], ["mine0", "mine1"])
        # The new one first, so that the stage never points at a deployment that is gone.
        self.assertLess(calls.index(create), calls.index(next(c for c in calls if " delete-deployment " in c)))
        self.assertIn("serves the API as its stack left it (deployment new1)", out.stdout)

    def test_none_marked(self):
        out, calls = self.run_step(marked="None")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([c for c in calls if " delete-deployment " in c], [])

    def test_the_stack_s_deploy_ends_with_it(self):
        script = ('sl_delete_orphan_log_groups() { :; }; deploy_assembly() { echo "deploy $1" >>"$WORK/steps"; };'
                  ' stack_output() { echo "$2"; }; sl_redeploy_stage() { echo "redeploy $API_ID" >>"$WORK/steps"; }; sl_deploy_serverless')
        work = tempfile.mkdtemp()
        out, _ = stubbed(script, env={"WORK": work})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((Path(work) / "steps").read_text().split(), ["deploy", "ColvennaCatalogSl1Serverless", "redeploy", "ApiId"])


class Flow(unittest.TestCase):
    """install_serverless and uninstall_serverless, each step replaced by a recorder of its name."""

    STEPS = ("preflight_serverless confirm_install cdk_dependencies sl_stacks build_image copy_release_image sl_database sl_delete_orphan_log_groups "
             "sl_grants ensure_admin sl_smoke release_form_check release_registry_check release_auth release_verify_artifacts "
             "assert_account")

    def run_flow(self, script, env=None, running=""):
        recorders = "; ".join(f'{name}() {{ echo "{name} $*" >>"$WORK/steps"; }}' for name in self.STEPS.split())
        prelude = (f'{recorders}; sl_synth_serverless() {{ echo "synth $1" >>"$WORK/steps"; }};'
                   ' sl_deploy_serverless() { echo "deploy" >>"$WORK/steps"; API_URL=u ADMIN_TOKEN_SECRET_ARN=t; };'
                   ' sl_maintenance_first() { echo "maintenance_first" >>"$WORK/steps"; };'
                   ' sl_maintenance() { echo "maintenance $1" >>"$WORK/steps"; [[ -z "${MIGRATE_REFUSES:-}" ]]; };'
                   f' cfn_output() {{ echo "{running}"; }};'
                   ' build_image() { echo "build_image $1" >>"$WORK/steps"; IMAGE_REPOSITORY=repo IMAGE_DIGEST=sha256:new; };'
                   ' copy_release_image() { echo "copy_release_image $1" >>"$WORK/steps"; IMAGE_REPOSITORY=repo IMAGE_DIGEST=sha256:rel; };'
                   ' REPOSITORY_URI=repo BUCKET=b DB_ENDPOINT=db DB_PORT=5432 API_ENDPOINTS=vpce-0000000a COLVENNA_DB_NAME=d;')
        work = tempfile.mkdtemp()
        out, _ = stubbed(prelude + script, env={"WORK": work, "EVIDENCE": tempfile.mkdtemp(), "SOURCE_SHA": "b" * 40,
                                                "RELEASE_CANDIDATE": "", **(env or {})})
        steps = [line.strip() for line in (Path(work) / "steps").read_text().splitlines()] if (Path(work) / "steps").exists() else []
        return out, steps

    def test_a_first_install(self):
        out, steps = self.run_flow("install_serverless")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([s.split()[0] for s in steps], ["preflight_serverless", "confirm_install", "cdk_dependencies", "sl_stacks",
                                                         "build_image", "synth", "sl_delete_orphan_log_groups", "deploy",
                                                         "sl_database", "maintenance", "sl_grants", "ensure_admin", "sl_smoke"])
        self.assertIn("synth repo@sha256:new", steps)
        self.assertIn("ensure_admin sl_", steps)

    def test_an_upgrade_migrates_before_anything_that_serves_changes(self):
        # #63: the maintenance function alone takes the new version; the stack, the server's new version and the
        # alias live among it, only once migrate has succeeded; then the new ops function's database step and grants.
        out, steps = self.run_flow("install_serverless", running="repo@sha256:old")
        self.assertEqual(out.returncode, 0, out.stderr)
        order = [s for s in steps if s.startswith(("synth", "deploy", "maintenance", "sl_database", "sl_grants", "sl_delete"))]
        self.assertEqual([s.split()[0] for s in order], ["synth", "sl_delete_orphan_log_groups", "maintenance_first", "maintenance",
                                                         "deploy", "sl_database", "sl_grants"])
        # The steps announced in their usual order (the UAT harness's every-step-done), the server's after migrate.
        announced = [line[3:] for line in out.stdout.splitlines() if line.startswith("== ")]
        self.assertEqual(announced, ["preflight", "stacks", "image", "functions", "database", "migrate", "server", "admin", "smoke"])
        self.assertIn("== server", out.stdout)

    def test_a_migrate_that_refuses_leaves_the_stack_as_it_was(self):
        # Going back onto a newer schema (UAT-DEP-07's newer-schema-refused): the run ends at migrate, and the
        # serverless stack was never deployed, so the server function, its versions and the alias are as they were;
        # nor did the database step run.
        out, steps = self.run_flow("set -E; trap 'on_error install.sh' ERR; install_serverless", running="repo@sha256:newer",
                                   env={"MIGRATE_REFUSES": "1"})
        self.assertEqual(out.returncode, 1)
        self.assertIn('failed at step "migrate"', out.stderr)
        self.assertEqual([s for s in steps if s.startswith(("deploy", "maintenance", "sl_database"))], ["maintenance_first", "maintenance migrate"])
        # A first install, or a run again of the same image, has nothing serving to keep: the stack goes first.
        out, steps = self.run_flow("install_serverless", running="repo@sha256:new", env={"MIGRATE_REFUSES": "1"})
        self.assertEqual(out.returncode, 1)
        self.assertEqual([s for s in steps if s.startswith(("deploy", "maintenance"))], ["deploy", "maintenance migrate"])

    def test_a_release_checks_the_candidate_s_form_and_artifacts_first(self):
        _, steps = self.run_flow("install_serverless", env={"RELEASE_CANDIDATE": "v1.0.0-rc.1"})
        self.assertEqual([s.split()[0] for s in steps][:6], ["assert_account", "release_form_check", "release_registry_check",
                                                              "release_auth", "release_verify_artifacts", "preflight_serverless"])
        self.assertIn("release_form_check serverless", steps)
        self.assertIn("copy_release_image repo", steps)
        self.assertNotIn("build_image repo", steps)

    def uninstall(self, delete_data="", database="db-1", stacks="Serverless Images Storage Network"):
        statuses = " ".join(f'[ColvennaCatalogSl1{s}]=CREATE_COMPLETE' for s in stacks.split())
        script = (f'declare -A STATUS=({statuses}); stack_status() {{ echo "${{STATUS[$1]:-NONE}}"; }};'
                  ' destroy_stack() { echo "destroy $1" >>"$WORK/steps"; unset "STATUS[$1]"; };'
                  f' stack_resources() {{ echo "{database}"; }};'
                  ' remove_storage() { echo "remove_storage $1" >>"$WORK/steps"; unset "STATUS[$1]"; };'
                  ' cfn_output() { echo "out-$2"; }; sl_wait_interfaces() { echo "wait $1" >>"$WORK/steps"; };'
                  ' delete_log_groups() { echo "logs $1" >>"$WORK/steps"; };'
                  f' DELETE_DATA={delete_data} uninstall_serverless -c account=1')
        return self.run_flow(script)

    def test_uninstalling_keeps_the_data_and_the_network_it_lives_in(self):
        out, steps = self.uninstall()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(steps, ["destroy ColvennaCatalogSl1Serverless", "logs /aws/lambda/colvenna-catalog-sl1-",
                                 "logs API-Gateway-Execution-Logs_out-ApiId/", "destroy ColvennaCatalogSl1Images"])
        self.assertIn("ColvennaCatalogSl1Network: kept", out.stdout)

    def test_deleting_the_data_takes_the_network_too_once_lambda_lets_it_go(self):
        _, steps = self.uninstall(delete_data="1")
        self.assertEqual(steps[3:], ["destroy ColvennaCatalogSl1Images", "remove_storage ColvennaCatalogSl1Storage",
                                     "wait out-CatalogSecurityGroupId", "destroy ColvennaCatalogSl1Network"])

    def test_without_a_database_of_its_own_the_network_goes(self):
        _, steps = self.uninstall(database="")
        self.assertIn("destroy ColvennaCatalogSl1Network", steps)
        _, steps = self.uninstall(stacks="Serverless Images Network")
        self.assertIn("destroy ColvennaCatalogSl1Network", steps)

    def test_a_rerun_after_everything_is_gone(self):
        out, steps = self.uninstall(stacks="")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(steps, [])


class RemoveStorage(unittest.TestCase):
    """uninstall.sh's remove_storage, for both forms: the buckets' access logs stopped, the database, the
    buckets (the one that took the logs last), the stack, and the database's log groups, which it never
    owned (its function's are delete_function_log_groups', test_entrypoints.GeneratedNames)."""

    def test_the_data_the_stack_and_the_database_s_log_groups_go(self):
        work = tempfile.mkdtemp()
        script = (f'source "{REPO}/uninstall.sh"; WORK="{work}";'
                  ' stack_status() { echo CREATE_COMPLETE; };'
                  ' stack_resources() { case "$2" in AWS::RDS::DBInstance) echo db-1;; AWS::S3::Bucket) printf "b-1\\nb-2\\n";; esac; };'
                  ' cfn_output() { echo b-1; };'
                  ' for f in stop_access_logs delete_database delete_bucket destroy_stack delete_log_groups; do'
                  ' eval "$f() { echo \"$f \\$1\" >>\"$WORK/steps\"; }"; done;'
                  ' remove_storage ColvennaCatalogSl13Storage -c account=1')
        out = run_bash(script)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((Path(work) / "steps").read_text().splitlines(), [
            "stop_access_logs b-1", "delete_database db-1", "delete_bucket b-1", "delete_bucket b-2",
            "destroy_stack ColvennaCatalogSl13Storage", "delete_log_groups /aws/rds/instance/db-1/"])

    def storage(self, failing=""):
        """remove_storage with its steps recorded: the access log bucket listed first, as CloudFormation
        lists the stack's resources (AccessLogs...), and the warehouse bucket (its output BucketName)."""
        work = tempfile.mkdtemp()
        script = (f'source "{REPO}/uninstall.sh"; set -E; trap "on_error uninstall.sh" ERR; WORK="{work}";'
                  ' stack_status() { echo CREATE_COMPLETE; };'
                  ' stack_resources() { case "$2" in AWS::RDS::DBInstance) echo db-1;; AWS::S3::Bucket) printf "logs-1\\nwh-1\\n";; esac; };'
                  ' cfn_output() { [[ "$2" == BucketName ]] && echo wh-1; };'
                  ' stop_access_logs() { echo "stop_access_logs $*" >>"$WORK/steps"; };'
                  ' for f in delete_database destroy_stack delete_log_groups; do'
                  ' eval "$f() { echo \"$f \\$1\" >>\"$WORK/steps\"; }"; done;'
                  f' delete_bucket() {{ echo "delete_bucket $1" >>"$WORK/steps"; [[ "$1" != "{failing}" ]]; }};'
                  ' remove_storage ColvennaCatalogSl13Storage -c account=1')
        out = run_bash(script)
        return out, (Path(work) / "steps").read_text().splitlines()

    def test_access_logs_stop_first_and_their_bucket_goes_last(self):
        out, steps = self.storage()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(steps[:5], ["stop_access_logs logs-1 wh-1", "delete_database db-1", "delete_bucket wh-1",
                                     "delete_bucket logs-1", "destroy_stack ColvennaCatalogSl13Storage"])

    def test_a_bucket_that_fails_keeps_neither_the_other_nor_the_stack_from_a_run_again(self):
        out, steps = self.storage(failing="wh-1")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(steps, ["stop_access_logs logs-1 wh-1", "delete_database db-1", "delete_bucket wh-1",
                                 "delete_bucket logs-1"])
        self.assertIn("Not deleted: the bucket(s) wh-1, as said above; ColvennaCatalogSl13Storage stays", out.stderr)
        self.assertIn('uninstall.sh: failed at step "ColvennaCatalogSl13Storage and its data" (status 1)', out.stderr)

    def test_a_storage_stack_already_gone(self):
        out = run_bash(f'source "{REPO}/uninstall.sh"; stack_status() {{ echo NONE; }}; destroy_stack() {{ exit 9; }};'
                       ' remove_storage ColvennaCatalogSl13Storage')
        self.assertEqual(out.returncode, 0, out.stderr)


class VerifyClean(unittest.TestCase):
    """tests/serverless/verify-clean.sh finds what is left of an environment; the log groups of the
    functions CloudFormation named after its stacks as uninstall.sh does (stacks.sh, function_log_groups)."""

    def verify(self, groups):
        functions = "logs describe-log-groups --region us-west-2 --log-group-name-prefix /aws/lambda/ColvennaCatalogUx1e"
        stubs = Stubs({"aws": {"": {"stdout": ""}, functions: {"stdout": "\t".join(groups) + "\n"}}})
        out = run_bash(f'"{REPO}/tests/serverless/verify-clean.sh" ux1e us-west-2', env={**stubs.env, "AWS_PROFILE": "stub"},
                       path_prepend=stubs.bin)
        return out, [line for line in out.stdout.splitlines() if "log groups" in line]

    def test_a_cut_function_name_s_group_is_left_another_environment_s_is_not_this_one_s(self):
        mine = "/aws/lambda/ColvennaCatalogUx1eStorag-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl"  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
        theirs = "/aws/lambda/ColvennaCatalogUx1exStorag-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl"  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
        out, lines = self.verify([mine, theirs])
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(lines, [f"LEFT  log groups: {mine} "])
        out, lines = self.verify([theirs])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(lines, ["ok    log groups: none"])


class VerifyCleanWaitsForRds(unittest.TestCase):
    """verify-clean.sh waits while all that is left is what RDS is still deleting (#59): the automated
    snapshots and backups of instances that no longer exist, and the tagging index's entries for those
    snapshots; anything else is reported at once, and a read that fails is never taken for nothing left."""

    INSTANCE = "colvennacatalogux1estorage-database"
    SNAPSHOT = f"rds:{INSTANCE}-2026-10-30-22-29"
    TAGGED = f"arn:aws:rds:us-west-2:111122223333:snapshot:{SNAPSHOT}"
    TAGGED_LINE = "tagged resources (Project=colvenna-catalog, Environment=ux1e or test-ux1e)"
    # The stub's keys: the reads of the account, and the describes of one snapshot or instance.
    TAGGING = "resourcegroupstaggingapi get-resources"
    SNAPSHOTS_LISTED = "rds describe-db-snapshots --query"
    SNAPSHOT_DESCRIBED = "rds describe-db-snapshots --db-snapshot-identifier"
    INSTANCE_DESCRIBED = "rds describe-db-instances --db-instance-identifier"
    INSTANCES_LISTED = "rds describe-db-instances --query"
    BACKUPS_LISTED = "rds describe-db-instance-automated-backups"
    AUTOMATED = {"stdout": f"automated\t{INSTANCE}\n"}
    NO_SNAPSHOT = {"stderr": "An error occurred (DBSnapshotNotFound) when calling the DescribeDBSnapshots operation\n", "code": 254}
    NO_INSTANCE = {"stderr": "An error occurred (DBInstanceNotFound) when calling the DescribeDBInstances operation\n", "code": 254}
    DENIED = {"stderr": "An error occurred (AccessDenied) when calling the operation\n", "code": 254}
    THROTTLED = {"stderr": "An error occurred (Throttling) when calling the operation: Rate exceeded\n", "code": 254}

    def clean(self, responses, wait="0", seconds="60"):
        stubs = Stubs({"aws": {"": {"stdout": ""}, **responses}})
        out = run_bash(f'"{REPO}/tests/serverless/verify-clean.sh" ux1e us-west-2', path_prepend=stubs.bin,
                       env={**stubs.env, "AWS_PROFILE": "stub", "RDS_DELETING_WAIT": wait, "RDS_DELETING_SECONDS": seconds})
        self.calls = stubs.calls()  # every aws call of the run, for the tests that look at them
        reads = len([c for c in self.calls if c.startswith(f"aws {self.SNAPSHOTS_LISTED}")])
        # Nothing on stderr but the errors the stub was told to answer with, no shell error; a refusal
        # (exit 2) says why there.
        if out.returncode != 2:
            self.assertEqual([line for line in out.stderr.splitlines() if not line.startswith("An error occurred")], [])
        return out, out.stdout.splitlines(), reads

    def deleting(self, **overrides):
        """The deleted instance's automated snapshot, listed and tagged, until the overrides say otherwise."""
        return {self.TAGGING: {"stdout": self.TAGGED + "\n"}, self.SNAPSHOTS_LISTED: {"stdout": self.SNAPSHOT + "\n"},
                self.SNAPSHOT_DESCRIBED: self.AUTOMATED, self.INSTANCE_DESCRIBED: self.NO_INSTANCE, **overrides}

    def assert_at_once(self, out, lines, reads):
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(reads, 1)
        self.assertFalse([line for line in lines if line.startswith("wait") or "waited" in line], lines)

    def test_a_deleted_instance_s_automated_snapshot_is_waited_for_until_it_is_gone(self):
        out, lines, reads = self.clean(self.deleting(**{
            self.TAGGING: [{"stdout": self.TAGGED + "\n"}] * 2 + [{"stdout": ""}],
            self.SNAPSHOTS_LISTED: [{"stdout": self.SNAPSHOT + "\n"}] * 2 + [{"stdout": ""}]}))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(reads, 3)
        self.assertFalse([line for line in lines if line.startswith("LEFT")], lines)
        self.assertIn("ok    RDS snapshots: none", lines)
        self.assertIn(f"ok    {self.TAGGED_LINE}: none", lines)
        # What it waits for is named, on a line the judge does not take for one of the report's.
        self.assertTrue(lines[0].startswith("wait  left: only what RDS is still deleting"), lines[0])
        self.assertIn(f"({self.TAGGED_LINE}: rds snapshot:{self.SNAPSHOT}; RDS snapshots: {self.SNAPSHOT}); reading again "
                      "every 0 s, the last wait ending no later than 60 s after the first read", lines[0])
        self.assertRegex(lines[-1], r"^note  waited [0-9]+ s \(2 more read\(s\)\) for what RDS is still deleting: it is gone$")

    def test_the_tagging_index_s_entry_of_a_snapshot_gone_does_not_count(self):
        # The index lags: it still lists the snapshot once RDS has deleted it. Each read describes the
        # tagged snapshot, then the listed one: the third time, the snapshot is gone.
        out, lines, reads = self.clean(self.deleting(**{
            self.SNAPSHOTS_LISTED: [{"stdout": self.SNAPSHOT + "\n"}, {"stdout": ""}],
            self.SNAPSHOT_DESCRIBED: [self.AUTOMATED, self.AUTOMATED, self.NO_SNAPSHOT]}))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(reads, 2)
        self.assertIn(f"ok    {self.TAGGED_LINE}: none", lines)
        self.assertIn("note  the tagging index still lists 1 VPC endpoint(s), flow log(s) or RDS snapshot(s) that no "
                      "longer exist", lines)
        self.assertRegex(lines[-1], r"^note  waited [0-9]+ s \(1 more read\(s\)\) for what RDS is still deleting: it is gone$")

    def test_a_tagged_snapshot_of_a_deleted_instance_not_named_as_the_install_s_is_waited_for(self):
        # The instance UAT-DEP-10 restored, say: its snapshots are not listed by the install's names.
        restored = "colvenna-ux1e-restored-abc123"
        snapshot = f"rds:{restored}-2026-10-30-22-29"
        out, lines, reads = self.clean({
            self.TAGGING: [{"stdout": f"arn:aws:rds:us-west-2:111122223333:snapshot:{snapshot}\n"}, {"stdout": ""}],
            self.SNAPSHOT_DESCRIBED: {"stdout": f"automated\t{restored}\n"}, self.INSTANCE_DESCRIBED: self.NO_INSTANCE})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(reads, 2)
        self.assertIn(f"rds snapshot:{snapshot}", lines[0])

    def test_what_rds_has_not_deleted_in_time_is_left(self):
        out, lines, reads = self.clean(self.deleting(), wait="1", seconds="2")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertGreaterEqual(reads, 2)
        self.assertIn(f"LEFT  RDS snapshots: {self.SNAPSHOT} ", lines)
        self.assertIn(f"LEFT  {self.TAGGED_LINE}: rds snapshot:{self.SNAPSHOT} ", lines)
        self.assertRegex(lines[-1], r"^note  waited [0-9]+ s \([0-9]+ more read\(s\)\) for what RDS is still deleting: "
                                    r"it is still there$")

    def test_a_read_that_fails_while_waiting_is_left_not_clean(self):
        out, lines, reads = self.clean(self.deleting(**{
            self.TAGGING: [{"stdout": self.TAGGED + "\n"}, self.THROTTLED],
            self.SNAPSHOTS_LISTED: [{"stdout": self.SNAPSHOT + "\n"}, self.THROTTLED]}))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(reads, 2)
        self.assertIn(f"LEFT  {self.TAGGED_LINE}: (unreadable) ", lines)
        self.assertIn("LEFT  RDS snapshots: (unreadable) ", lines)
        self.assertRegex(lines[-1], r"for what RDS is still deleting, until something else was left$")
        # Any other listing too, at the first read; and what is checked of what it lists.
        out, lines, reads = self.clean({"s3api list-buckets": self.THROTTLED, "lambda list-functions": self.THROTTLED,
                                        "ec2 describe-security-groups": self.THROTTLED})
        self.assert_at_once(out, lines, reads)
        for line in ("S3 buckets", "Lambda functions", "Lambda function URLs (never made)", "security groups",
                     "network interfaces of those groups"):
            self.assertIn(f"LEFT  {line}: (unreadable) ", lines)
        self.assertFalse([c for c in self.calls if "(unreadable)" in c], self.calls)

    def test_something_else_left_while_waiting_ends_the_wait(self):
        out, lines, reads = self.clean(self.deleting(**{
            "s3api list-buckets": [{"stdout": ""}, {"stdout": "colvennacatalogux1e-warehouse\n"}]}))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(reads, 2)
        self.assertIn("LEFT  S3 buckets: colvennacatalogux1e-warehouse ", lines)
        self.assertRegex(lines[-1], r"^note  waited [0-9]+ s \(1 more read\(s\)\) for what RDS is still deleting, until "
                                    r"something else was left$")

    def test_a_deleted_instance_s_automated_backup_is_waited_for_until_it_is_gone(self):
        out, lines, reads = self.clean({self.BACKUPS_LISTED: [{"stdout": self.INSTANCE + "\n"}, {"stdout": ""}],
                                        self.INSTANCE_DESCRIBED: self.NO_INSTANCE})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok    RDS automated backups: none", lines)
        self.assertIn(f"(RDS automated backups: {self.INSTANCE})", lines[0])
        self.assertRegex(lines[-1], r"^note  waited [0-9]+ s \(1 more read\(s\)\) .*: it is gone$")

    def test_a_manual_snapshot_is_never_waited_for(self):
        out, lines, reads = self.clean(self.deleting(**{self.TAGGING: {"stdout": ""},
                                                        self.SNAPSHOT_DESCRIBED: {"stdout": f"manual\t{self.INSTANCE}\n"}}))
        self.assert_at_once(out, lines, reads)
        self.assertIn(f"LEFT  RDS snapshots: {self.SNAPSHOT} ", lines)

    def test_an_automated_snapshot_of_an_instance_still_there_is_never_waited_for(self):
        there = {"stdout": self.INSTANCE + "\n"}
        out, lines, reads = self.clean(self.deleting(**{self.INSTANCE_DESCRIBED: there, self.INSTANCES_LISTED: there}))
        self.assert_at_once(out, lines, reads)
        self.assertIn(f"LEFT  RDS snapshots: {self.SNAPSHOT} ", lines)
        # Tagged only, of an instance not named as the install's (the one UAT-DEP-10 restored, say).
        restored = "colvenna-ux1e-restored-abc123"
        snapshot = f"rds:{restored}-2026-10-30-22-29"
        out, lines, reads = self.clean({self.TAGGING: {"stdout": f"arn:aws:rds:us-west-2:111122223333:snapshot:{snapshot}\n"},
                                        self.SNAPSHOT_DESCRIBED: {"stdout": f"automated\t{restored}\n"},
                                        self.INSTANCE_DESCRIBED: {"stdout": restored + "\n"}})
        self.assert_at_once(out, lines, reads)
        self.assertIn(f"LEFT  {self.TAGGED_LINE}: rds snapshot:{snapshot} ", lines)

    def test_a_snapshot_or_an_instance_that_cannot_be_read_is_never_waited_for(self):
        for overrides in ({self.SNAPSHOT_DESCRIBED: self.DENIED}, {self.INSTANCE_DESCRIBED: self.DENIED}):
            out, lines, reads = self.clean(self.deleting(**overrides))
            self.assert_at_once(out, lines, reads)
            self.assertIn(f"LEFT  {self.TAGGED_LINE}: rds snapshot:{self.SNAPSHOT} ", lines)

    def test_anything_else_left_is_reported_at_once(self):
        out, lines, reads = self.clean(self.deleting(**{"s3api list-buckets": {"stdout": "colvennacatalogux1e-warehouse\n"}}))
        self.assert_at_once(out, lines, reads)
        self.assertIn("LEFT  S3 buckets: colvennacatalogux1e-warehouse ", lines)
        self.assertIn(f"LEFT  RDS snapshots: {self.SNAPSHOT} ", lines)

    def test_the_bounds_are_whole_seconds(self):
        for wait, seconds in (("soon", "60"), ("08", "60"), ("30", "-1")):
            out, _, reads = self.clean({}, wait=wait, seconds=seconds)
            self.assertEqual(out.returncode, 2, (wait, seconds))
            self.assertEqual(reads, 0)
            self.assertIn("RDS_DELETING_WAIT and RDS_DELETING_SECONDS: whole seconds", out.stderr)


class ReleaseForm(unittest.TestCase):
    def check(self, forms, form):
        directory = Path(tempfile.mkdtemp())
        objects = {path: "a" * 40 for path in manifest.PINNED_PATHS}
        built = manifest.build({"candidate": "v1.0.0-rc.1", "built_from": "b" * 40, "generated": "2026-10-28T06:00:00Z",
                                "image_digest": "sha256:" + "1" * 64, "chart_digest": "sha256:" + "2" * 64, "objects": objects,
                                "uat_cases": None, "required_cases": [], "regions": ["us-west-2", "us-east-2"], "forms": forms})
        path = write(directory / "manifest.json", json.dumps(built))
        return run_bash(f'REPO="{REPO}"; RELEASE_MANIFEST="{path}" release_form_check {form}', env={"EVIDENCE": str(directory)})

    def test_a_candidate_installs_only_in_the_forms_it_lists(self):
        self.assertEqual(self.check(["eks", "serverless"], "serverless").returncode, 0)
        self.assertEqual(self.check(["eks"], "eks").returncode, 0)
        out = self.check(["eks"], "serverless")
        self.assertEqual(out.returncode, 3)
        self.assertIn("accepted in the form(s) eks, not serverless", out.stderr)
        self.assertEqual(self.check(["serverless"], "eks").returncode, 3)


if __name__ == "__main__":
    unittest.main()
