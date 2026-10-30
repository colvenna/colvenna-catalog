"""The serverless form's installer (deploy/lib/serverless.sh, and its configuration in config.sh): with
stub commands for AWS, and the steps that invoke Lambda functions replaced by recorders."""
import json
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


class Flow(unittest.TestCase):
    """install_serverless and uninstall_serverless, each step replaced by a recorder of its name."""

    STEPS = ("preflight_serverless confirm_install cdk_dependencies sl_stacks build_image copy_release_image sl_database "
             "sl_grants ensure_admin sl_smoke release_form_check release_registry_check release_auth release_verify_artifacts "
             "assert_account")

    def run_flow(self, script, env=None, running=""):
        recorders = "; ".join(f'{name}() {{ echo "{name} $*" >>"$WORK/steps"; }}' for name in self.STEPS.split())
        prelude = (f'{recorders}; sl_deploy_serverless() {{ echo "deploy $1 $2" >>"$WORK/steps"; API_URL=u ADMIN_TOKEN_SECRET_ARN=t; }};'
                   ' sl_maintenance() { echo "maintenance $1" >>"$WORK/steps"; };'
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
                                                         "build_image", "deploy", "sl_database", "maintenance", "sl_grants",
                                                         "ensure_admin", "sl_smoke"])
        self.assertIn("deploy repo@sha256:new repo@sha256:new", steps)
        self.assertIn("ensure_admin sl_", steps)

    def test_an_upgrade_migrates_before_the_server_takes_the_new_image(self):
        _, steps = self.run_flow("install_serverless", running="repo@sha256:old")
        order = [s for s in steps if s.startswith(("deploy", "maintenance", "sl_database", "sl_grants"))]
        self.assertEqual(order, ["deploy repo@sha256:old repo@sha256:new", "sl_database", "maintenance migrate", "sl_grants",
                                 "deploy repo@sha256:new repo@sha256:new"])

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
                                     "wait out-CatalogSecurityGroupId", "destroy ColvennaCatalogSl1Network",
                                     "logs /aws/lambda/ColvennaCatalogSl1Network"])

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
    """uninstall.sh's remove_storage, for both forms: the database, the buckets, the stack, and the log
    groups it never owned, its functions' among them by their own (truncated) names."""

    def test_the_data_the_stack_and_every_log_group_go(self):
        work = tempfile.mkdtemp()
        script = (f'source "{REPO}/uninstall.sh"; WORK="{work}";'
                  ' stack_status() { echo CREATE_COMPLETE; };'
                  ' stack_resources() { case "$2" in AWS::RDS::DBInstance) echo db-1;; AWS::S3::Bucket) printf "b-1\\nb-2\\n";;'
                  ' AWS::Lambda::Function) echo ColvennaCatalogSl13Storag-LogRetentionaae0aa3c5b4d-AbC;; esac; };'
                  ' for f in delete_database delete_bucket destroy_stack delete_log_groups; do'
                  ' eval "$f() { echo \"$f \$1\" >>\"$WORK/steps\"; }"; done;'
                  ' remove_storage ColvennaCatalogSl13Storage -c account=1')
        out = run_bash(script)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((Path(work) / "steps").read_text().splitlines(), [
            "delete_database db-1", "delete_bucket b-1", "delete_bucket b-2", "destroy_stack ColvennaCatalogSl13Storage",
            "delete_log_groups /aws/rds/instance/db-1/",
            # CloudFormation cut the stack's name short in the function's: found by the function, not the stack.
            "delete_log_groups /aws/lambda/ColvennaCatalogSl13Storag-LogRetentionaae0aa3c5b4d-AbC",
            "delete_log_groups /aws/lambda/ColvennaCatalogSl13Storage"])

    def test_a_storage_stack_already_gone(self):
        out = run_bash(f'source "{REPO}/uninstall.sh"; stack_status() {{ echo NONE; }}; destroy_stack() {{ exit 9; }};'
                       ' remove_storage ColvennaCatalogSl13Storage')
        self.assertEqual(out.returncode, 0, out.stderr)


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
