"""deploy/lib/vpc_check.py, the serverless preflight's judgement of a VPC and a database."""
import importlib.util
import json
import subprocess
import sys
import unittest

from helpers import REPO

SCRIPT = REPO / "deploy/lib/vpc_check.py"
spec = importlib.util.spec_from_file_location("vpc_check", SCRIPT)
vpc_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpc_check)

REGION, VPC = "us-west-2", "vpc-0000000a"


def endpoint(service, kind="Interface", **extra):
    return {"ServiceName": f"com.amazonaws.{REGION}.{service}", "VpcEndpointType": kind, "State": "available", **extra}


def described(endpoints=None, routes=(), api=None, zones=("us-west-2a", "us-west-2b"), public=False):
    if endpoints is None:
        endpoints = [endpoint("sts", PrivateDnsEnabled=True), endpoint("cognito-identity", PrivateDnsEnabled=True),
                     endpoint("s3", "Gateway", RouteTableIds=["rtb-1"])]
    return {
        "vpc": {"Vpcs": [{"VpcId": VPC, "CidrBlock": "10.9.0.0/16"}]},
        "subnets": {"Subnets": [
            {"SubnetId": "subnet-b", "VpcId": VPC, "AvailabilityZone": zones[1], "MapPublicIpOnLaunch": False},
            {"SubnetId": "subnet-a", "VpcId": VPC, "AvailabilityZone": zones[0], "MapPublicIpOnLaunch": public}]},
        "tables": {"RouteTables": [{"RouteTableId": "rtb-1", "Associations": [{"Main": True}], "Routes": list(routes)}]},
        "endpoints": {"VpcEndpoints": endpoints},
        "api": {"VpcEndpoints": api if api is not None else [
            endpoint("execute-api", VpcEndpointId="vpce-other", VpcId="vpc-0000000f"),
            endpoint("execute-api", VpcEndpointId="vpce-mine", VpcId=VPC)]},
    }


class Network(unittest.TestCase):
    def judge(self, **kwargs):
        return vpc_check.network(REGION, VPC, ["subnet-a", "subnet-b"], described(**kwargs))

    def test_a_vpc_with_its_endpoints(self):
        verdict = self.judge()
        self.assertEqual(verdict["problems"], [])
        self.assertEqual(verdict["cidr"], "10.9.0.0/16")
        self.assertEqual(verdict["zones"], "us-west-2a,us-west-2b", "in the subnets' order")
        self.assertEqual(verdict["endpoints"], "vpce-mine,vpce-other", "the VPC's own endpoint first")

    def test_a_nat_gateway_stands_in_for_the_endpoints(self):
        nat = [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]
        self.assertEqual(self.judge(endpoints=[], routes=nat)["problems"], [])
        blackholed = [dict(nat[0], State="blackhole")]
        self.assertEqual(len(self.judge(endpoints=[], routes=blackholed)["problems"]), 3)

    def test_each_thing_missing_is_named(self):
        cases = (
            ({"public": True}, "subnet-a gives public addresses"),
            ({"zones": ("us-west-2a", "us-west-2a")}, "two zones or more"),
            ({"endpoints": [endpoint("sts", PrivateDnsEnabled=False), endpoint("cognito-identity", PrivateDnsEnabled=True),
                            endpoint("s3", "Gateway", RouteTableIds=["rtb-1"])]}, "sts: no interface endpoint with private DNS"),
            ({"endpoints": [endpoint("sts", PrivateDnsEnabled=True), endpoint("cognito-identity", PrivateDnsEnabled=True),
                            endpoint("s3", "Gateway", RouteTableIds=["rtb-9"])]}, "s3: no gateway endpoint"),
            ({"api": [endpoint("sts", VpcEndpointId="vpce-x", VpcId=VPC)]}, "vpce-x is not an execute-api interface endpoint"),
            ({"api": [endpoint("execute-api", VpcEndpointId="vpce-x", VpcId=VPC, State="pending")]}, "vpce-x is pending"),
            ({"api": [endpoint("execute-api", VpcEndpointId="vpce-x", VpcId="vpc-0000000f")]}, "none is in vpc-0000000a"),
        )
        for kwargs, needle in cases:
            problems = self.judge(**kwargs)["problems"]
            self.assertTrue(any(needle in p for p in problems), f"{kwargs}: {problems}")


class Database(unittest.TestCase):
    SUBNETS = {"Subnets": [{"CidrBlock": "10.9.1.0/24"}, {"CidrBlock": "10.9.2.0/24"}]}

    def judge(self, permissions, catalog="", vpc=VPC):
        groups = {"SecurityGroups": [{"GroupId": "sg-db", "VpcId": vpc, "IpPermissions": permissions}]}
        return vpc_check.database(5432, VPC, catalog, {"groups": groups, "subnets": self.SUBNETS})

    def test_cidrs_must_hold_every_subnet(self):
        rule = lambda *cidrs: {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [],  # noqa: E731
                               "IpRanges": [{"CidrIp": c} for c in cidrs]}
        self.assertEqual(self.judge([rule("10.9.0.0/16")]), "open")
        self.assertEqual(self.judge([rule("10.9.1.0/24", "10.9.2.0/24")]), "open")
        self.assertEqual(self.judge([rule("10.9.1.0/24")]), "closed")
        self.assertEqual(self.judge([dict(rule("10.9.0.0/16"), FromPort=5433, ToPort=5433)]), "closed")
        self.assertEqual(self.judge([{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "10.9.0.0/16"}]}]), "open")

    def test_or_the_functions_security_group(self):
        rule = {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": "sg-fn"}], "IpRanges": []}
        self.assertEqual(self.judge([rule], catalog="sg-fn"), "open")
        self.assertEqual(self.judge([rule], catalog=""), "closed", "before the network stack made the group")

    def test_a_database_of_another_vpc(self):
        self.assertEqual(self.judge([], vpc="vpc-0000000f"), "vpc vpc-0000000f")


class CommandLine(unittest.TestCase):
    def test_reads_stdin_and_refuses_unknown_arguments(self):
        out = subprocess.run([sys.executable, "-I", str(SCRIPT), "network", REGION, VPC, "subnet-a,subnet-b"],
                             input=json.dumps(described()), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["endpoints"], "vpce-mine,vpce-other")
        out = subprocess.run([sys.executable, "-I", str(SCRIPT), "other"], input="{}", capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("vpc_check.py network <region>", out.stderr)


if __name__ == "__main__":
    unittest.main()
