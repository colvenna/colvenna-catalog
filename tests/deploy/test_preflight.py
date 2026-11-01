import json
import tempfile
import unittest

from helpers import parameters, run_bash, stack_outputs
from stubs import Stubs

ISSUER = "https://oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF"
CONFIG = {
    "COLVENNA_ENV": "uat", "COLVENNA_ACCOUNT": "111122223333", "COLVENNA_REGION": "us-west-2", "COLVENNA_FORM": "eks",
    "COLVENNA_EKS_CLUSTER": "c1", "COLVENNA_NAMESPACE": "colvenna-catalog", "COLVENNA_RELEASE_NAME": "colvenna-catalog",
    "COLVENNA_BUCKET": "", "COLVENNA_DB": "create",
    "COLVENNA_INTERNAL_NLB": "off", "COLVENNA_NODE_SELECTOR": "",
}


def cluster(status="ACTIVE", subnets=("subnet-0000000a", "subnet-0000000b", "subnet-0000000c")):
    return {"cluster": {"status": status, "identity": {"oidc": {"issuer": ISSUER}},
                        "resourcesVpcConfig": {"clusterSecurityGroupId": "sg-00000001", "vpcId": "vpc-00000001",
                                               "subnetIds": list(subnets)}}}


SUBNETS = {"Subnets": [
    {"SubnetId": "subnet-0000000c", "AvailabilityZone": "us-west-2c", "MapPublicIpOnLaunch": True, "CidrBlock": "10.85.2.0/24"},
    {"SubnetId": "subnet-0000000b", "AvailabilityZone": "us-west-2b", "MapPublicIpOnLaunch": False, "CidrBlock": "10.85.16.0/21"},
    {"SubnetId": "subnet-0000000a", "AvailabilityZone": "us-west-2a", "MapPublicIpOnLaunch": False, "CidrBlock": "10.85.8.0/21"},
]}
# Every subnet of the VPC: the cluster's, and one where only clients run (no node, not the cluster's).
VPC_SUBNETS = {"Subnets": SUBNETS["Subnets"] + [
    {"SubnetId": "subnet-0000000d", "AvailabilityZone": "us-west-2a", "MapPublicIpOnLaunch": True, "CidrBlock": "10.85.0.0/24"},
    {"SubnetId": "subnet-0000000e", "AvailabilityZone": "us-west-2a", "MapPublicIpOnLaunch": False, "CidrBlock": "10.85.32.0/21"},
]}


def agent(enabled=True, image="602401143452.dkr.ecr.us-west-2.amazonaws.com/amazon/aws-network-policy-agent:v1.4.0"):
    containers = [{"name": "aws-node", "image": "x/amazon-k8s-cni:v1.22.4", "args": None}]
    if image:
        containers.append({"name": "aws-eks-nodeagent", "image": image,
                           "args": [f"--enable-network-policy={'true' if enabled else 'false'}"]})
    return {"items": [{"metadata": {"name": "aws-node", "namespace": "kube-system"},
                       "spec": {"template": {"spec": {"containers": containers}}}}]}


def ready_nodes(n, addresses=("10.85.9.10", "10.85.33.4")):  # redaction: allow - made-up node addresses in the test VPC's range
    return {"items": [{"spec": {"providerID": f"aws:///us-west-2a/i-000000000000000{i:x}"},
                       "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                  "addresses": [{"type": "InternalIP", "address": addresses[i % len(addresses)]}]}}
                      for i in range(n)]}


ENDPOINT = "db1.abcdefghijkl.us-west-2.rds.amazonaws.com"
AURORA_MEMBER = "au1-1.abcdefghijkl.us-west-2.rds.amazonaws.com"
DB_INSTANCES = {"DBInstances": [{"DBInstanceIdentifier": "db1", "Engine": "postgres", "DBInstanceStatus": "available",
                                 "Endpoint": {"Address": ENDPOINT, "Port": 5432}, "PubliclyAccessible": False,
                                 "DBParameterGroups": [{"DBParameterGroupName": "db1-parameters"}],
                                 "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-000000db", "Status": "active"}]},
                                {"DBInstanceIdentifier": "au1-1", "DBClusterIdentifier": "au1", "Engine": "aurora-postgresql",
                                 "DBInstanceStatus": "available", "Endpoint": {"Address": AURORA_MEMBER, "Port": 5432},
                                 "PubliclyAccessible": False, "DBParameterGroups": [{"DBParameterGroupName": "au1-instance"}],
                                 "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-000000db", "Status": "active"}]}]}
DB_CLUSTERS = {"DBClusters": [{"DBClusterIdentifier": "au1", "Engine": "aurora-postgresql", "Status": "available",
                               "Endpoint": "au1.cluster-abcdefghijkl.us-west-2.rds.amazonaws.com",
                               "ReaderEndpoint": "au1.cluster-ro-abcdefghijkl.us-west-2.rds.amazonaws.com", "Port": 5432,
                               "DBClusterParameterGroup": "au1-parameters",
                               "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-000000db", "Status": "active"}]}]}
FORCE_SSL = {"rds describe-db-parameters --region us-west-2 --db-parameter-group-name db1-parameters": parameters("1"),
             "rds describe-db-cluster-parameters --region us-west-2 --db-cluster-parameter-group-name au1-parameters": parameters("1"),
             "rds describe-db-clusters --region us-west-2 --db-cluster-identifier au1": {"stdout": "au1-parameters\n"}}
NODE_INSTANCES = {"Reservations": [{"Instances": [
    {"InstanceId": "i-0000000000000000", "NetworkInterfaces": [{"Groups": [{"GroupId": "sg-00000001"}, {"GroupId": "sg-0000node"}]}]},
    {"InstanceId": "i-0000000000000001", "NetworkInterfaces": [{"Groups": [{"GroupId": "sg-00000001"}]}]}]}]}


def db_groups(*permissions, vpc="vpc-00000001"):
    return {"SecurityGroups": [{"GroupId": "sg-000000db", "VpcId": vpc, "IpPermissions": list(permissions)}]}


def from_group(group, port=5432, protocol="tcp"):
    return {"IpProtocol": protocol, "FromPort": port, "ToPort": port, "UserIdGroupPairs": [{"GroupId": group}], "IpRanges": []}


def from_cidrs(*cidrs, port=5432):
    return {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "UserIdGroupPairs": [],
            "IpRanges": [{"CidrIp": c} for c in cidrs]}


OLD_ENDPOINT = "colvennacatalogstorage-database.abcdefghijkl.us-west-2.rds.amazonaws.com"


def storage_stack(endpoint=OLD_ENDPOINT, bucket="colvennacatalog-warehouse"):
    """The outputs of the Storage stack an earlier install made, with its bucket and database."""
    return stack_outputs("ColvennaCatalogUatStorage", DbEndpoint=endpoint, BucketName=bucket)


# No stack of the environment's but those a test gives (CDKToolkit is answered on its own).
NO_STACK = {"cloudformation describe-stacks --region us-west-2 --stack-name": {
    "stderr": "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id X does not exist\n",
    "code": 254}}


def responses(**over):
    aws = {
        "sts get-caller-identity": {"stdout": "111122223333\n"},
        "eks describe-cluster": {"stdout": cluster()},
        "ec2 describe-subnets": {"stdout": SUBNETS},
        "ec2 describe-subnets --filters": {"stdout": VPC_SUBNETS},
        "iam list-open-id-connect-providers": {"stdout": {"OpenIDConnectProviderList": [
            {"Arn": f"arn:aws:iam::111122223333:oidc-provider/{ISSUER[len('https://'):]}"}]}},
        "eks update-kubeconfig": {"stdout": ""},
        "s3api get-bucket-location": {"stdout": {"LocationConstraint": "us-west-2"}},
        "secretsmanager describe-secret": {"stdout": {"Name": "db-admin"}},
        "secretsmanager get-secret-value": {"stdout": '{"username": "postgres", "password": "p4ss"}\n'},  # pragma: allowlist secret - a stubbed AWS CLI answer: a fake password
        "rds describe-db-instances": {"stdout": DB_INSTANCES},
        "rds describe-db-clusters": {"stdout": DB_CLUSTERS},
        "ec2 describe-instances": {"stdout": NODE_INSTANCES},
        "ec2 describe-security-groups": {"stdout": db_groups(from_group("sg-00000001"))},
        "cloudformation describe-stacks --stack-name CDKToolkit": {"stdout": "CREATE_COMPLETE\n"},
        **NO_STACK,
        **FORCE_SSL,
    }
    kubectl = {
        "get daemonset -A -o json": {"stdout": agent()},
        "get nodes": {"stdout": ready_nodes(2)},
        "-n kube-system get deployment aws-load-balancer-controller -o json": {"stdout": {"status": {"availableReplicas": 1}}},
    }
    aws.update(over.get("aws", {}))
    kubectl.update(over.get("kubectl", {}))
    return {"aws": aws, "kubectl": kubectl, "helm": over.get("helm", {})}


class Preflight(unittest.TestCase):
    def run_preflight(self, script, config=None, **over):
        stubs = Stubs(responses(**over), commands=("aws", "kubectl", "helm"))
        env = {**CONFIG, **(config or {}), **stubs.env, "STATE_DIR": tempfile.mkdtemp()}
        return run_bash(script, env=env, path_prepend=stubs.bin), stubs

    def test_account_mismatch_refuses_before_anything_else(self):
        out, stubs = self.run_preflight("preflight", aws={"sts get-caller-identity": {"stdout": "444455556666\n"}})
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("444455556666", out.stderr)
        self.assertIn("111122223333", out.stderr)
        self.assertEqual(stubs.calls(), ["aws sts get-caller-identity --query Account --output text"])

    def test_the_whole_preflight_passes(self):
        out, stubs = self.run_preflight('preflight; echo "issuer=$CLUSTER_ISSUER subnets=$CLUSTER_SUBNETS zones=$CLUSTER_SUBNET_ZONES nodes=$ELIGIBLE_NODES"')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"issuer={ISSUER}", out.stdout)
        # Private subnets only, in the cluster's order, each with its zone.
        self.assertIn("subnets=subnet-0000000a,subnet-0000000b zones=us-west-2a,us-west-2b", out.stdout)
        self.assertIn("nodes=2", out.stdout)

    def test_a_cluster_not_active_is_refused(self):
        out, _ = self.run_preflight("assert_account; cluster_facts", aws={"eks describe-cluster": {"stdout": cluster("CREATING")}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("CREATING", out.stderr)

    def test_a_missing_cluster_is_refused(self):
        out, _ = self.run_preflight("cluster_facts", aws={"eks describe-cluster": {
            "stderr": "An error occurred (ResourceNotFoundException) when calling the DescribeCluster operation: No cluster found for name: c1.", "code": 254}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("c1", out.stderr)

    def test_fewer_than_two_private_zones_is_refused(self):
        subnets = {"Subnets": [{"SubnetId": "subnet-0000000a", "AvailabilityZone": "us-west-2a", "MapPublicIpOnLaunch": False},
                               {"SubnetId": "subnet-0000000c", "AvailabilityZone": "us-west-2c", "MapPublicIpOnLaunch": True}]}
        out, _ = self.run_preflight("cluster_facts", config={"COLVENNA_DB": "create"},
                                    aws={"eks describe-cluster": {"stdout": cluster(subnets=("subnet-0000000a", "subnet-0000000c"))},
                                         "ec2 describe-subnets": {"stdout": subnets}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("two", out.stderr)

    def test_a_missing_oidc_provider_is_refused_with_the_fix(self):
        out, _ = self.run_preflight("cluster_facts; assert_oidc_provider",
                                    aws={"iam list-open-id-connect-providers": {"stdout": {"OpenIDConnectProviderList": []}}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("associate-iam-oidc-provider", out.stderr)

    def test_network_policy_detection(self):
        def daemonsets(*names):
            return {"items": [{"metadata": {"name": n, "namespace": "kube-system"},
                               "spec": {"template": {"spec": {"containers": [{"name": n, "image": f"x/{n}:1", "args": []}]}}}}
                              for n in names]}
        cases = [
            ("agent on", agent(True), None, 0),
            ("agent off", agent(False), None, 3),
            ("no agent", agent(image=None), None, 3),
            ("calico", daemonsets("calico-node"), None, 0),
            ("cilium", daemonsets("cilium"), None, 0),
            ("agent off, allowed", agent(False), "1", 0),
        ]
        for label, sets, allow, code in cases:
            with self.subTest(label):
                env = {"ALLOW_UNENFORCED_NETWORK_POLICY": allow} if allow else {}
                out, _ = self.run_preflight("network_policy_enforced", config=env,
                                            kubectl={"get daemonset -A -o json": {"stdout": sets}})
                self.assertEqual(out.returncode, code, out.stderr)
                if label == "agent off, allowed":
                    self.assertIn("not enforced", out.stderr)

    def test_the_internal_nlb_needs_the_load_balancer_controller(self):
        out, _ = self.run_preflight("assert_lbc", config={"COLVENNA_INTERNAL_NLB": "on"}, kubectl={
            "-n kube-system get deployment aws-load-balancer-controller -o json": {"stderr": "Error from server (NotFound)", "code": 1}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("AWS Load Balancer Controller", out.stderr)
        out, _ = self.run_preflight("assert_lbc", config={"COLVENNA_INTERNAL_NLB": "off"}, kubectl={
            "-n kube-system get deployment aws-load-balancer-controller -o json": {"stderr": "NotFound", "code": 1}})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_nlb_sources_must_hold_no_pod_address(self):
        # Pods take their addresses from the subnets of the cluster and of its nodes: an allowed CIDR
        # overlapping one would admit those pods past the namespace label.
        nlb = {"COLVENNA_INTERNAL_NLB": "on"}
        for cidrs, refused in [("10.85.8.0/21", True),      # a cluster subnet
                               ("10.85.8.0/24", True),      # inside one
                               ("10.85.0.0/16", True),      # around them all
                               ("10.85.32.0/21", True),  # redaction: allow - not the cluster's, but a node's (10.85.33.4)
                               ("10.85.0.0/24", False),     # a client subnet
                               ("192.168.0.0/24", False)]:  # outside the VPC
            with self.subTest(cidrs=cidrs):
                out, _ = self.run_preflight("cluster_facts; assert_nlb_sources_hold_no_pods",
                                            config={**nlb, "COLVENNA_NLB_ALLOWED_CIDRS": cidrs})
                self.assertEqual(out.returncode, 3 if refused else 0, out.stderr)
                if refused:
                    self.assertIn(cidrs, out.stderr)
        out, _ = self.run_preflight("cluster_facts; assert_nlb_sources_hold_no_pods", config={"COLVENNA_INTERNAL_NLB": "off"})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_no_eligible_node_is_refused(self):
        out, _ = self.run_preflight("eligible_nodes", config={"COLVENNA_NODE_SELECTOR": "role=catalog"},
                                    kubectl={"get nodes": {"stdout": ready_nodes(0)}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("role=catalog", out.stderr)

    def test_only_the_nodes_the_catalog_may_run_on_are_eligible(self):
        # #35: a node tainted NoSchedule or NoExecute takes no catalog pod (the chart tolerates none), so it is not
        # counted; a cordoned node is (the chart's spread waits for it); a node not Ready is not.
        taint = lambda key, effect: [{"key": key, "value": "x", "effect": effect}]  # noqa: E731
        # (taints, counted): a workload's taints rule a node out; Kubernetes' own condition taints, a cloud
        # provider's while it initializes and the autoscalers' drain marks come and go, and do not.
        cases = [(None, True), (taint("colvenna-catalog/test-pool", "NoSchedule"), False),
                 (taint("dedicated", "NoExecute"), False), (taint("soft", "PreferNoSchedule"), True),
                 (taint("node.kubernetes.io/unschedulable", "NoSchedule"), True),
                 (taint("node.kubernetes.io/disk-pressure", "NoSchedule"), True),
                 (taint("node.kubernetes.io/out-of-service", "NoExecute"), False),
                 (taint("node.cloudprovider.kubernetes.io/uninitialized", "NoSchedule"), True),
                 (taint("ToBeDeletedByClusterAutoscaler", "NoSchedule"), True), (taint("karpenter.sh/disrupted", "NoSchedule"), True),
                 (taint("node.kubernetes.io/unschedulable", "NoSchedule") + taint("dedicated", "NoSchedule"), False)]
        for taints, counted in cases:
            with self.subTest(taints=taints):
                nodes = ready_nodes(2)  # one plain, one under test: a refusal never hides the verdict
                if taints:
                    nodes["items"][1]["spec"]["taints"] = taints
                out, _ = self.run_preflight('eligible_nodes; echo "nodes=$ELIGIBLE_NODES"',
                                            kubectl={"get nodes": {"stdout": nodes}})
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(f"nodes={2 if counted else 1}", out.stdout)
        not_ready = ready_nodes(2)
        not_ready["items"][1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        out, _ = self.run_preflight('eligible_nodes; echo "nodes=$ELIGIBLE_NODES"', kubectl={"get nodes": {"stdout": not_ready}})
        self.assertIn("nodes=1", out.stdout)
        only_tainted = ready_nodes(2)
        for node in only_tainted["items"]:
            node["spec"]["taints"] = taint("colvenna-catalog/test-pool", "NoSchedule")
        out, _ = self.run_preflight("eligible_nodes", kubectl={"get nodes": {"stdout": only_tainted}})
        self.assertEqual(out.returncode, 3)

    def test_a_bucket_of_another_region_is_refused(self):
        out, _ = self.run_preflight("assert_bucket", config={"COLVENNA_BUCKET": "bucket"},
                                    aws={"s3api get-bucket-location": {"stdout": {"LocationConstraint": "eu-west-1"}}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("eu-west-1", out.stderr)
        # us-east-1's buckets have no location constraint.
        out, _ = self.run_preflight("assert_bucket", config={"COLVENNA_BUCKET": "bucket", "COLVENNA_REGION": "us-east-1"},
                                    aws={"s3api get-bucket-location": {"stdout": {"LocationConstraint": None}}})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_region_without_cdk_bootstrap_is_refused(self):
        out, _ = self.run_preflight("assert_cdk_bootstrap", aws={"cloudformation describe-stacks --stack-name CDKToolkit": {
            "stderr": "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id CDKToolkit does not exist",
            "code": 254}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("cdk bootstrap aws://111122223333/us-west-2", out.stderr)

    def test_an_unreadable_admin_secret_is_refused(self):
        out, _ = self.run_preflight("assert_db_admin_secret", config={
            "COLVENNA_DB": "existing", "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:x"},  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
            aws={"secretsmanager describe-secret": {"stderr": "AccessDeniedException", "code": 254}})
        self.assertEqual(out.returncode, 3)
        self.assertIn("secret", out.stderr)

    def test_the_admin_secret_must_hold_a_username_and_a_password(self):
        existing = {"COLVENNA_DB": "existing", "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:x"}  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
        for value, code in [('{"username": "postgres", "password": "p4ss"}', 0), ('{"username": "postgres"}', 3),  # pragma: allowlist secret - a stubbed AWS CLI answer: a fake password
                            ('{"user": "postgres", "password": "p4ss"}', 3), ('{"username": "", "password": "p4ss"}', 3),  # pragma: allowlist secret - a fake password of a stubbed answer
                            ("p4ss", 3)]:
            with self.subTest(value=value):
                out, _ = self.run_preflight("assert_db_admin_secret", config=existing,
                                            aws={"secretsmanager get-secret-value": {"stdout": value + "\n"}})
                self.assertEqual(out.returncode, code, out.stderr)
                self.assertNotIn("p4ss", out.stderr + out.stdout)
                if code:
                    self.assertIn("username", out.stderr)

    def reachable(self, endpoint=ENDPOINT, config=None, **over):
        existing = {"COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": endpoint, "COLVENNA_DB_PORT": "5432", **(config or {})}
        return self.run_preflight("cluster_facts; assert_db_reachable", config=existing, **over)

    def test_an_existing_database_the_nodes_reach(self):
        cases = [
            ("the nodes' security group", {}),
            ("an Aurora cluster's writer endpoint", {"endpoint": DB_CLUSTERS["DBClusters"][0]["Endpoint"]}),
            ("all traffic from a node's other group", {"aws": {"ec2 describe-security-groups": {
                "stdout": db_groups(from_group("sg-0000node", port=-1, protocol="-1"))}}}),
            ("CIDRs holding every node's subnet", {"aws": {"ec2 describe-security-groups": {
                "stdout": db_groups(from_cidrs("10.85.8.0/21"), from_cidrs("10.85.32.0/20"))}}}),
        ]
        for label, over in cases:
            with self.subTest(label):
                out, _ = self.reachable(**over)
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_database_need_admit_only_the_eligible_nodes(self):
        # #35: the same nodes as the replicas' spread; a tainted node's instance is not looked up.
        nodes = ready_nodes(3)
        nodes["items"][2]["spec"]["taints"] = [{"key": "colvenna-catalog/test-pool", "value": "clients", "effect": "NoSchedule"}]
        out, stubs = self.reachable(kubectl={"get nodes": {"stdout": nodes}})
        self.assertEqual(out.returncode, 0, out.stderr)
        looked_up = [c for c in stubs.calls() if c.startswith("aws ec2 describe-instances")]
        self.assertTrue(looked_up)
        self.assertNotIn(nodes["items"][2]["spec"]["providerID"].rsplit("/", 1)[1], " ".join(looked_up))
        self.assertIn(nodes["items"][0]["spec"]["providerID"].rsplit("/", 1)[1], " ".join(looked_up))

    def test_an_existing_database_the_nodes_cannot_use_is_refused(self):
        cases = [
            ("unknown endpoint", {"endpoint": "db.example.com"}, "db.example.com"),
            ("a reader endpoint", {"endpoint": DB_CLUSTERS["DBClusters"][0]["ReaderEndpoint"]}, "writer endpoint"),
            ("another port", {"config": {"COLVENNA_DB_PORT": "5433"}}, "5433"),
            ("not PostgreSQL", {"aws": {"rds describe-db-instances": {"stdout": {"DBInstances": [
                {**DB_INSTANCES["DBInstances"][0], "Engine": "mysql"}]}}}}, "mysql"),
            ("stopped", {"aws": {"rds describe-db-instances": {"stdout": {"DBInstances": [
                {**DB_INSTANCES["DBInstances"][0], "DBInstanceStatus": "stopped"}]}}}}, "stopped"),
            ("another VPC", {"aws": {"ec2 describe-security-groups": {
                "stdout": db_groups(from_group("sg-00000001"), vpc="vpc-00000002")}}}, "vpc-00000002"),
            ("another port open", {"aws": {"ec2 describe-security-groups": {
                "stdout": db_groups(from_group("sg-00000001", port=3306))}}}, "sg-000000db"),
            ("a CIDR missing a node's subnet", {"aws": {"ec2 describe-security-groups": {
                "stdout": db_groups(from_cidrs("10.85.8.0/21"))}}}, "sg-000000db"),
        ]
        for label, over, needle in cases:
            with self.subTest(label):
                out, _ = self.reachable(**over)
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn(needle, out.stderr)

    def test_an_existing_database_must_take_tls_connections_only(self):
        cluster = DB_CLUSTERS["DBClusters"][0]["Endpoint"]
        cases = [
            ("an instance", {}, "rds describe-db-parameters", "db1-parameters"),
            ("an Aurora cluster", {"endpoint": cluster}, "rds describe-db-cluster-parameters", "au1-parameters"),
            ("an Aurora instance's endpoint: its cluster's group", {"endpoint": AURORA_MEMBER}, "rds describe-db-cluster-parameters",
             "au1-parameters"),
        ]
        for label, over, call, group in cases:
            with self.subTest(label):
                out, _ = self.reachable(**over)
                self.assertEqual(out.returncode, 0, out.stderr)
                key = next(k for k in FORCE_SSL if k.startswith(call) and k.endswith(group))
                for value, said in (("0", "0"), (None, "unset")):
                    out, _ = self.reachable(**over, aws={key: parameters(value)})
                    self.assertEqual(out.returncode, 3, out.stderr)
                    self.assertIn(f"does not require TLS: rds.force_ssl is {said} in its parameter group {group}", out.stderr)

    def test_a_busy_region_s_databases_are_read_whatever_their_number(self):
        # More than a command line can carry: 3000 other instances, about 600 KB of JSON.
        many = {"DBInstances": DB_INSTANCES["DBInstances"] + [
            {**DB_INSTANCES["DBInstances"][0], "DBInstanceIdentifier": f"other-{i}", "Endpoint": {"Address": f"other-{i}.x", "Port": 5432},
             "TagList": [{"Key": "padding", "Value": "p" * 100}]} for i in range(3000)]}
        for endpoint in (ENDPOINT, DB_CLUSTERS["DBClusters"][0]["Endpoint"]):
            with self.subTest(endpoint):
                out, _ = self.reachable(endpoint=endpoint, aws={"rds describe-db-instances": {"stdout": many}})
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_an_existing_database_must_not_be_publicly_accessible(self):
        public = {"DBInstances": [{**i, "PubliclyAccessible": True} for i in DB_INSTANCES["DBInstances"]]}
        for endpoint in (ENDPOINT, DB_CLUSTERS["DBClusters"][0]["Endpoint"]):
            with self.subTest(endpoint):
                out, _ = self.reachable(endpoint=endpoint, aws={"rds describe-db-instances": {"stdout": public}})
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn("is publicly accessible", out.stderr)

    def test_an_existing_database_leaves_the_storage_stack_s_database_in_it(self):
        # Without COLVENNA_BUCKET, install.sh deploys the Storage stack again, without its database.
        existing = {"COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": ENDPOINT, "COLVENNA_DB_PORT": "5432",
                    "COLVENNA_DB_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:x"}  # pragma: allowlist secret - a placeholder Secrets Manager ARN in AWS's documentation account
        out, _ = self.run_preflight("preflight", config=existing, aws=storage_stack())
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("COLVENNA_DB=existing without COLVENNA_BUCKET", out.stderr)
        self.assertIn(f"deleting it ({OLD_ENDPOINT})", out.stderr)
        self.assertIn("COLVENNA_BUCKET=colvennacatalog-warehouse", out.stderr)  # pragma: allowlist secret - a made-up bucket name, in the message the test expects
        out, _ = self.run_preflight("preflight", config={**existing, "COLVENNA_BUCKET": "colvennacatalog-warehouse"},
                                    aws=storage_stack())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_create_never_moves_an_install_back_from_the_database_it_runs_on(self):
        def values(host):
            return {"get values colvenna-catalog --namespace colvenna-catalog -o json": {"stdout": {"database": {
                "url": f"jdbc:postgresql://{host}:5432/colvenna_catalog?sslmode=verify-full&sslrootcert=/etc/x/ca.pem"}}}}
        out, _ = self.run_preflight("preflight", aws=storage_stack(), helm=values(ENDPOINT))
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn(f"The install runs on the database at {ENDPOINT}, not on ColvennaCatalogUatStorage's ({OLD_ENDPOINT})",
                      out.stderr)
        self.assertIn(f"COLVENNA_DB=existing with COLVENNA_DB_ENDPOINT={ENDPOINT}", out.stderr)
        no_release = {"get values": {"stderr": "Error: release: not found\n", "code": 1}}
        for label, helm in (("on the stack's database", values(OLD_ENDPOINT)), ("no release yet", no_release)):
            with self.subTest(label):
                out, _ = self.run_preflight("preflight", aws=storage_stack(), helm=helm)
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_guard_fails_closed_when_it_cannot_read_what_it_compares(self):
        # A release or a stack that cannot be read is no proof that there is none.
        out, _ = self.run_preflight("preflight", aws=storage_stack(),
                                    helm={"get values": {"stderr": "Error: Kubernetes cluster unreachable\n", "code": 1}})
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("Kubernetes cluster unreachable", out.stderr)
        # Throttled for longer than aws_retry waits (#94; none here: THROTTLE_SECONDS=0).
        out, _ = self.run_preflight("THROTTLE_SECONDS=0; preflight", aws={"cloudformation describe-stacks --region us-west-2 --stack-name": {
            "stderr": "An error occurred (Throttling) when calling the DescribeStacks operation: Rate exceeded\n", "code": 254}})
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("Rate exceeded", out.stderr)
        self.assertIn("aws cloudformation describe-stacks: throttled (Throttling) 1 time(s)", out.stderr)

    def test_a_database_the_install_makes_is_not_looked_up(self):
        out, stubs = self.run_preflight("assert_db_reachable", config={"COLVENNA_DB": "create"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stubs.calls(), [])


TOOLS = {
    "aws": {"--version": {"stdout": "aws-cli/2.34.30 Python/3.14.4 Linux/7.0.0 exe/x86_64\n"}},
    "jq": {"--version": {"stdout": "jq-1.7.1\n"}},
    "curl": {"--version": {"stdout": "curl 8.5.0 (x86_64-pc-linux-gnu) libcurl/8.5.0\n"}},
    "openssl": {"version": {"stdout": "OpenSSL 3.0.13 30 Jan 2024\n"}},
    "node": {"--version": {"stdout": "v22.11.0\n"}},
    "mvn": {"-B -v": {"stdout": "Apache Maven 3.9.9 (8e8579a9)\nJava version: 21.0.5, vendor: Amazon.com Inc.\n"}},
    "docker": {"buildx version": {"stdout": "github.com/docker/buildx v0.19.3 48d6a39\n"}},
}


class Tools(unittest.TestCase):
    def check(self, purpose="install", **over):
        stubs = Stubs({**TOOLS, **over}, commands=tuple(TOOLS))
        return run_bash(f"assert_tools {purpose}", env=stubs.env, path_prepend=stubs.bin)

    def test_the_tools_at_their_versions_pass(self):
        out = self.check()
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_old_or_other_tools_are_refused(self):
        cases = [
            ("aws", {"--version": {"stdout": "aws-cli/1.33.0 Python/3.9.0\n"}}, "AWS CLI 2"),
            ("jq", {"--version": {"stdout": "jq-1.5\n"}}, "jq 1.6"),
            ("curl", {"--version": {"stdout": "curl 7.68.0 (x86_64-pc-linux-gnu)\n"}}, "curl 7.76"),
            ("openssl", {"version": {"stdout": "LibreSSL 3.3.6\n"}}, "OpenSSL 1.1.1"),
            ("node", {"--version": {"stdout": "v18.20.4\n"}}, "Node.js 20"),
            ("mvn", {"-B -v": {"stdout": "Apache Maven 3.9.9\nJava version: 17.0.12, vendor: x\n"}}, "Java 21"),
            ("docker", {"buildx version": {"stderr": "docker: 'buildx' is not a docker command.\n", "code": 1}}, "Docker Buildx"),
        ]
        for name, answers, needle in cases:
            with self.subTest(name):
                out = self.check(**{name: answers})
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertIn(needle, out.stderr)

    def test_uninstalling_needs_no_build_or_tls_tools(self):
        out = self.check("uninstall", mvn={}, docker={}, openssl={}, curl={})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_release_builds_nothing_but_needs_the_tls_tools(self):
        out = self.check("release", mvn={}, docker={})
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.check("release", openssl={"version": {"stdout": "LibreSSL 3.3.6\n"}})
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("OpenSSL 1.1.1", out.stderr)

    def test_versions_compare_field_by_field(self):
        for have, want, ok in [("1.10", "1.6", True), ("1.6", "1.6", True), ("1.5.9", "1.6", False),
                               ("2", "1.1.1", True), ("1.1.0", "1.1.1", False), ("7.76.0", "7.76", True)]:
            with self.subTest(have=have, want=want):
                self.assertEqual(run_bash(f"version_at_least {have} {want}").returncode == 0, ok)


if __name__ == "__main__":
    unittest.main()
