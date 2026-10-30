import {
  CfnOutput, Duration, Fn, RemovalPolicy, Stack, StackProps, Tags, Validations,
  aws_ec2 as ec2, aws_eks as eks, aws_iam as iam, aws_logs as logs, aws_rds as rds, aws_s3 as s3,
} from 'aws-cdk-lib';
import { KubectlV36Layer } from '@aws-cdk/lambda-layer-kubectl-v36';
import { Construct } from 'constructs';
import { isIP } from 'node:net';
import { account, commitSha, envName, standardRegion } from '../src/context';
import { EKS_MANAGED_POLICIES, EKS_PROVIDERS, RDS_DESIGN, acknowledge, acknowledgeLogRetention } from '../src/nag';
// The AWS Load Balancer Controller's own policy for v3.6.0 (docs/install/iam_policy.json upstream),
// kept verbatim and pinned by its SHA-256 in the tests.
import lbcPolicy from './lbc-iam-policy.json';

export interface TestEnvironmentStackProps extends StackProps {
  /** The test environment's name: ColvennaCatalogTest<Name>, colvenna-catalog-test-<name>-... */
  name: string;
  sourceSha: string;
  /** Two zones of the stack's region, given rather than looked up. */
  availabilityZones: string[];
  /** The one public IPv4 address (/32) the EKS API endpoint admits: the operator's. */
  operatorCidr: string;
  /** The IAM role or user (of this account) that administers the cluster. */
  adminPrincipalArn: string;
}

/** The subnet groups: the cluster and everything of it runs in `nodes`; `clients` is outside them (UAT-DEP-11). */
export const NODES = 'nodes';
export const CLIENTS = 'clients';
/** The master user of the database the UAT's "existing" installs (E3, S3) take as given. */
export const EXISTING_DB_ADMIN = 'colvenna_test_admin';
/** The VPC's CIDR, written out: security group rules name it as a literal, which cdk-nag can judge. */
export const VPC_CIDR = '10.85.0.0/16';
/**
 * The node groups, told apart by this label (docs/uat/cases.md 3.1 and 3.2): the catalogs' two nodes,
 * which hold only the EKS installs' pods and the per-node DaemonSets, and the clients' nodes, tainted so
 * that no catalog pod lands there, for everything else: engines, probes, test drivers, load, and the
 * cluster's own controllers (CoreDNS, the AWS Load Balancer Controller).
 */
export const POOL_LABEL = 'colvenna-catalog/test-pool';
export const CATALOG_NODE_POOL = 'catalog';
export const CLIENT_NODE_POOL = 'clients';
/** The catalogs' nodes, as the performance terms name them (docs/performance/terms.md, section 2). */
export const CATALOG_NODE_TYPE = 't3.large';
export const CATALOG_NODES = 2;
export const CLIENT_NODE_TYPE = 't3.xlarge';
export const CLIENT_NODES = 2;
/** What a client's pod tolerates (and selects) to run on the clients' nodes. */
export const CLIENT_TOLERATION = { key: POOL_LABEL, operator: 'Equal', value: CLIENT_NODE_POOL, effect: 'NoSchedule' };

/** Private, loopback, link-local, shared, multicast and reserved IPv4 ranges: not an operator's public address. */
const NOT_PUBLIC = [/^10\./, /^127\./, /^0\./, /^169\.254\./, /^192\.168\./, /^172\.(1[6-9]|2\d|3[01])\./,
  /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./, /^(22[4-9]|2[3-5]\d)\./];

/**
 * The repository's own test environment (docs/design/v1.0-design.md 9.2): a VPC, an EKS 1.36 cluster
 * whose public endpoint admits only the operator's /32 and needs IAM authentication for everything
 * (anonymous requests may not even read the public info), the VPC CNI with network policy enforced,
 * two managed node groups (the catalogs', the clients'), and the AWS Load Balancer Controller's
 * identity. Besides, what the UAT
 * cases need of it (docs/uat/cases.md 3.1): an execute-api endpoint with private DNS, through which
 * its pods reach the serverless installs' private APIs; an RDS PostgreSQL 16 with IAM database
 * authentication and a bucket, the existing resources of the UAT's E3 and S3 installs; and client
 * subnets outside the cluster's, for UAT-DEP-11's client of the internal NLB. It keeps no data: the
 * stack deletes everything with it. tests/environment/up.sh and down.sh deploy and remove it.
 */
export class TestEnvironmentStack extends Stack {
  private readonly zones: string[];

  /** The given zones, never the context lookup Stack would make. */
  override get availabilityZones(): string[] {
    return this.zones;
  }

  constructor(scope: Construct, id: string, props: TestEnvironmentStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    const accountId = account(props.env?.account);
    const name = envName(props.name);
    const sha = commitSha(props.sourceSha);
    const zones = props.availabilityZones;
    if (zones.length !== 2 || zones[0] === zones[1] || zones.some((z) => !new RegExp(`^${region}[a-z]$`).test(z))) {
      throw new Error(`availabilityZones: two distinct zones of ${region}, not ${JSON.stringify(zones)}`);
    }
    this.zones = zones;
    const [address, prefix] = props.operatorCidr.split('/');
    if (prefix !== '32' || isIP(address) !== 4 || NOT_PUBLIC.some((range) => range.test(address))) {
      throw new Error(`operatorCidr: one public IPv4 address as a /32, not ${props.operatorCidr}`);
    }
    if (!new RegExp(`^arn:aws:iam::${accountId}:(role|user)/[A-Za-z0-9+=,.@_/-]+$`).test(props.adminPrincipalArn)) {
      throw new Error(`adminPrincipalArn: a role or user of account ${accountId}, not ${props.adminPrincipalArn}`);
    }
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Purpose', 'test-environment');
    Tags.of(this).add('Environment', `test-${name}`);
    Tags.of(this).add('SourceSha', sha);

    const vpc = new ec2.Vpc(this, 'Vpc', {
      ipAddresses: ec2.IpAddresses.cidr(VPC_CIDR),
      availabilityZones: zones,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: NODES, subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 21 },
        // Outside the cluster's subnets, and with no route out of the VPC but to S3: where UAT-DEP-11's
        // client (the test driver, deployed by its harness) reaches the internal NLB from.
        { name: CLIENTS, subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });
    // Everything of the cluster selects the nodes subnets by name, never by type.
    const nodeSubnets: ec2.SubnetSelection = { subnetGroupName: NODES };
    const clientSubnets = vpc.selectSubnets({ subnetGroupName: CLIENTS }).subnets;
    // The zones are given (up.sh picks two that offer the node type), so the subnets name them.
    for (const subnet of [...vpc.publicSubnets, ...vpc.privateSubnets, ...vpc.isolatedSubnets]) {
      Validations.of(subnet).acknowledge({
        id: 'CloudFormation-Validate::W3010',
        reason: 'The zones are chosen by tests/environment/up.sh and given, never looked up',
      });
    }
    // On every subnet's route table, the clients' included.
    vpc.addGatewayEndpoint('S3Endpoint', { service: ec2.GatewayVpcEndpointAwsService.S3 });
    // The pods reach the serverless installs' private APIs through this endpoint (each install lists it
    // in COLVENNA_API_ENDPOINT_IDS); it is also the endpoint of S3's existing network. Private DNS: the
    // APIs' own names resolve to it inside the VPC. Its security group admits HTTPS from the VPC.
    const apiEndpoint = vpc.addInterfaceEndpoint('ExecuteApiEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.APIGATEWAY,
      privateDnsEnabled: true,
      subnets: nodeSubnets,
      open: false,
    });
    apiEndpoint.connections.allowFrom(ec2.Peer.ipv4(VPC_CIDR), ec2.Port.tcp(443), 'HTTPS from the VPC');
    vpc.addFlowLog('FlowLog', {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(new logs.LogGroup(this, 'FlowLogs', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      })),
      trafficType: ec2.FlowLogTrafficType.ALL,
    });

    const clusterName = `colvenna-catalog-test-${name}`;
    // EKS makes the control-plane log group and sets no retention: a month here. The cluster depends
    // on it, so that deleting the stack removes the group only after the cluster, which writes to it
    // while it shuts down (removed earlier, the group is made again and left behind).
    const clusterLogs = new logs.LogRetention(this, 'ClusterLogRetention', {
      logGroupName: `/aws/eks/${clusterName}/cluster`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const cluster = new eks.Cluster(this, 'Cluster', {
      clusterName,
      vpc,
      vpcSubnets: [nodeSubnets],
      version: eks.KubernetesVersion.of('1.36'),
      kubectlLayer: new KubectlV36Layer(this, 'KubectlLayer'),
      defaultCapacity: 0,
      authenticationMode: eks.AuthenticationMode.API_AND_CONFIG_MAP,
      endpointAccess: eks.EndpointAccess.PUBLIC_AND_PRIVATE.onlyFrom(props.operatorCidr),
      clusterLogging: [eks.ClusterLoggingTypes.API, eks.ClusterLoggingTypes.AUDIT, eks.ClusterLoggingTypes.AUTHENTICATOR],
    });
    const adminAccess = new eks.CfnAccessEntry(this, 'AdminAccess', {
      clusterName: cluster.clusterName,
      principalArn: props.adminPrincipalArn,
      type: 'STANDARD',
      accessPolicies: [{
        accessScope: { type: 'cluster' },
        policyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy',
      }],
    });
    adminAccess.node.addDependency(cluster);
    (cluster.node.findChild('Resource') as Construct).node.addDependency(clusterLogs);
    // Anonymous requests may read the API's public info (version, health) by default; only
    // authenticated ones may here. Kubernetes owns the binding, so the patch is kept on deletion too.
    const authenticatedPublicInfo = {
      metadata: {
        annotations: {
          'rbac.authorization.kubernetes.io/autoupdate': 'false',
          'colvenna-catalog/source-sha': sha,
        },
      },
      subjects: [{ apiGroup: 'rbac.authorization.k8s.io', kind: 'Group', name: 'system:authenticated' }],
    };
    new eks.KubernetesPatch(this, 'AuthenticatedPublicInfo', {
      cluster,
      resourceName: 'clusterrolebinding/system:public-info-viewer',
      patchType: eks.PatchType.MERGE,
      applyPatch: authenticatedPublicInfo,
      restorePatch: authenticatedPublicInfo,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // The catalogs' two nodes: an install's two replicas run on different nodes. Nothing but the EKS
    // installs' pods and the per-node DaemonSets is placed on them, so that the order of the UAT
    // (docs/uat/cases.md 3.2) can count their room for a rolling update's extra pod.
    const nodes = cluster.addNodegroupCapacity('Nodes', {
      instanceTypes: [new ec2.InstanceType(CATALOG_NODE_TYPE)],
      amiType: eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
      diskSize: 40,
      minSize: CATALOG_NODES,
      desiredSize: CATALOG_NODES,
      maxSize: CATALOG_NODES,
      subnets: nodeSubnets,
      labels: { 'colvenna-catalog/test': 'true', [POOL_LABEL]: CATALOG_NODE_POOL },
    });
    // The clients' nodes: the taint keeps every catalog pod off them (the chart sets no toleration);
    // a client selects them and tolerates the taint. Their number may be raised by hand to maxSize
    // for a heavy window (verify.sh counts the group's desired size).
    const clientNodes = cluster.addNodegroupCapacity('ClientNodes', {
      instanceTypes: [new ec2.InstanceType(CLIENT_NODE_TYPE)],
      amiType: eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
      diskSize: 40,
      minSize: CLIENT_NODES,
      desiredSize: CLIENT_NODES,
      maxSize: 2 * CLIENT_NODES,
      subnets: nodeSubnets,
      labels: { 'colvenna-catalog/test': 'true', [POOL_LABEL]: CLIENT_NODE_POOL },
      taints: [{ key: POOL_LABEL, value: CLIENT_NODE_POOL, effect: eks.TaintEffect.NO_SCHEDULE }],
    });
    // The VPC CNI as a managed add-on with its network policy agent on, as the chart's NetworkPolicy
    // needs; kube-proxy and CoreDNS as managed add-ons of the cluster's version. Versions as colvenna-lake's.
    new eks.CfnAddon(this, 'VpcCniAddon', {
      addonName: 'vpc-cni',
      addonVersion: 'v1.22.4-eksbuild.3',
      clusterName: cluster.clusterName,
      resolveConflicts: 'OVERWRITE',
      configurationValues: JSON.stringify({ enableNetworkPolicy: 'true' }),
    });
    new eks.CfnAddon(this, 'KubeProxyAddon', {
      addonName: 'kube-proxy', addonVersion: 'v1.36.0-eksbuild.48', clusterName: cluster.clusterName, resolveConflicts: 'OVERWRITE',
    }).node.addDependency(nodes);
    // CoreDNS on the clients' nodes, its default tolerations kept beside theirs.
    new eks.CfnAddon(this, 'CoreDnsAddon', {
      addonName: 'coredns',
      addonVersion: 'v1.14.7-eksbuild.11',
      clusterName: cluster.clusterName,
      resolveConflicts: 'OVERWRITE',
      configurationValues: JSON.stringify({
        nodeSelector: { [POOL_LABEL]: CLIENT_NODE_POOL },
        tolerations: [CLIENT_TOLERATION, { key: 'CriticalAddonsOnly', operator: 'Exists' }],
      }),
    }).node.addDependency(nodes, clientNodes);

    // The AWS Load Balancer Controller's identity; up.sh installs the controller with Helm.
    const lbc = cluster.addServiceAccount('Lbc', { name: 'aws-load-balancer-controller', namespace: 'kube-system' });
    const lbcPermissions = new iam.Policy(this, 'LbcPolicy', { document: iam.PolicyDocument.fromJson(lbcPolicy) });
    lbc.role.attachInlinePolicy(lbcPermissions);
    acknowledge(lbcPermissions, {
      id: 'AwsSolutions-IAM5',
      reason: 'The controller\'s policy as its project publishes it for v3.6.0, verbatim; test environment only',
    });

    // --- what the UAT's existing installs (E3, S3) take as given ---------------------------------------
    // A PostgreSQL 16 as a user would bring one: in the nodes subnets, its port open to the VPC (so to
    // the nodes, the E3 install's check, and to every subnet a serverless install may run in, S3's),
    // TLS only, and IAM database authentication on, which the serverless form signs in with. Each
    // install makes its own database and user in it; nothing of it is kept.
    const dbSecurityGroup = new ec2.SecurityGroup(this, 'ExistingDbSecurityGroup', {
      vpc,
      description: 'colvenna-catalog test environment: the existing PostgreSQL, from the VPC only',
      allowAllOutbound: false,
    });
    dbSecurityGroup.addIngressRule(ec2.Peer.ipv4(VPC_CIDR), ec2.Port.tcp(5432), 'PostgreSQL from the VPC');
    const engine = rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_16 });
    const db = new rds.DatabaseInstance(this, 'ExistingDb', {
      engine,
      instanceIdentifier: `colvenna-catalog-test-${name}`,
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.SMALL),
      vpc,
      vpcSubnets: nodeSubnets,
      securityGroups: [dbSecurityGroup],
      credentials: rds.Credentials.fromGeneratedSecret(EXISTING_DB_ADMIN, {
        secretName: `colvenna-catalog-test-${name}/existing-db-admin`,  // pragma: allowlist secret - the name the test environment gives its database's generated secret, not a value
      }),
      parameterGroup: new rds.ParameterGroup(this, 'ExistingDbParameters', { engine, parameters: { 'rds.force_ssl': '1' } }),
      allocatedStorage: 20,
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
      publiclyAccessible: false,
      multiAz: false,
      backupRetention: Duration.days(1),
      deleteAutomatedBackups: true,
      iamAuthentication: true,
      deletionProtection: false,
      // No final snapshot: the test environment keeps nothing.
      removalPolicy: RemovalPolicy.DESTROY,
    });
    acknowledge(db, ...RDS_DESIGN,
      { id: 'AwsSolutions-RDS10', reason: 'A test environment\'s database, deleted with it: it holds only synthetic data' },
      {
        id: 'AwsSolutions-SMG4',
        reason: 'A test environment\'s master secret, deleted with it within the test window; installs read it once',
      });
    // The bucket of the existing installs, as a user would bring one; down.sh empties it before the stack goes.
    const bucket = new s3.Bucket(this, 'ExistingDataBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    acknowledge(bucket, {
      id: 'AwsSolutions-S1',
      reason: 'A test environment\'s bucket of synthetic data, emptied and deleted with it; '
        + 'the catalog\'s own audit records which table credentials it issued',
    });

    // UAT-DEP-11's clients: in the clients subnets, they may reach the VPC (the internal NLB) and HTTPS
    // beyond it, which their subnets route to S3 only.
    const clientSecurityGroup = new ec2.SecurityGroup(this, 'ClientSecurityGroup', {
      vpc,
      description: 'colvenna-catalog test environment: clients outside the cluster (UAT-DEP-11)',
      allowAllOutbound: false,
    });
    clientSecurityGroup.addEgressRule(ec2.Peer.ipv4(VPC_CIDR), ec2.Port.allTcp(), 'The VPC: the internal NLB');
    clientSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS: the subnets route only to S3');

    acknowledge(cluster,
      {
        id: 'AwsSolutions-EKS1',
        reason: 'The public endpoint admits only the operator\'s /32, and every request needs IAM authentication',
      },
      {
        id: 'AwsSolutions-EKS2',
        reason: 'The api, audit and authenticator logs record who did what; '
          + 'controllerManager and scheduler logs are operational detail',
        appliesTo: ['LogExport::controllerManager', 'LogExport::scheduler'],
      },
      EKS_MANAGED_POLICIES);
    acknowledge(cluster.node.findChild('Resource'), {
      id: 'AwsSolutions-IAM5',
      reason: 'CDK\'s cluster creation role, as CDK\'s EKS construct writes it: EKS, IAM and EC2 actions on any '
        + 'resource, and EKS actions on this cluster\'s and its Fargate profiles\' sub-resources',
      appliesTo: ['Resource::*',
        `Resource::arn:<AWS::Partition>:eks:${region}:${accountId}:cluster/${clusterName}/*`,
        `Resource::arn:<AWS::Partition>:eks:${region}:${accountId}:fargateprofile/${clusterName}/*`],
    });
    for (const provider of ['@aws-cdk--aws-eks.ClusterResourceProvider', '@aws-cdk--aws-eks.KubectlProvider']) {
      const made = this.node.tryFindChild(provider);
      if (made) {
        acknowledge(made, ...EKS_PROVIDERS);
      }
    }
    acknowledgeLogRetention(this);
    // Here only: the cluster's log group is deleted with the stack (RemovalPolicy.DESTROY), which gives
    // the retention function the deletion of that one group and its streams.
    const retentionFunction = this.node.children.find((c) => c.node.id.startsWith('LogRetention'));
    if (retentionFunction) {
      acknowledge(retentionFunction, {
        id: 'AwsSolutions-IAM5',
        reason: 'CDK\'s log retention function deletes the cluster\'s log group with the stack: that group and its streams',
        appliesTo: [`Resource::arn:<AWS::Partition>:logs:${region}:${accountId}:log-group:/aws/eks/${clusterName}/cluster:*`],
      });
    }

    new CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new CfnOutput(this, 'OidcIssuer', { value: cluster.clusterOpenIdConnectIssuerUrl });
    new CfnOutput(this, 'ClusterSecurityGroupId', { value: cluster.clusterSecurityGroupId });
    new CfnOutput(this, 'VpcId', { value: vpc.vpcId });
    const nodeSubnetList = vpc.selectSubnets(nodeSubnets).subnets;
    new CfnOutput(this, 'PrivateSubnetIds', { value: Fn.join(',', nodeSubnetList.map((s) => s.subnetId)) });
    new CfnOutput(this, 'PrivateSubnetZones', { value: nodeSubnetList.map((s) => s.availabilityZone).join(',') });
    new CfnOutput(this, 'NodeSubnetCidrs', { value: Fn.join(',', nodeSubnetList.map((s) => s.ipv4CidrBlock)) });
    new CfnOutput(this, 'ApiEndpointId', { value: apiEndpoint.vpcEndpointId });
    new CfnOutput(this, 'ExistingDbEndpoint', { value: db.dbInstanceEndpointAddress });
    new CfnOutput(this, 'ExistingDbPort', { value: db.dbInstanceEndpointPort });
    new CfnOutput(this, 'ExistingDbInstanceId', { value: db.instanceIdentifier });
    new CfnOutput(this, 'ExistingDbAdminSecretArn', { value: db.secret!.secretArn });
    new CfnOutput(this, 'ExistingDbSecurityGroupId', { value: dbSecurityGroup.securityGroupId });
    new CfnOutput(this, 'ExistingBucket', { value: bucket.bucketName });
    new CfnOutput(this, 'ClientSubnetIds', { value: Fn.join(',', clientSubnets.map((s) => s.subnetId)) });
    new CfnOutput(this, 'ClientSubnetCidrs', { value: Fn.join(',', clientSubnets.map((s) => s.ipv4CidrBlock)) });
    new CfnOutput(this, 'ClientSubnetZones', { value: clientSubnets.map((s) => s.availabilityZone).join(',') });
    new CfnOutput(this, 'ClientSecurityGroupId', { value: clientSecurityGroup.securityGroupId });
    // The node groups' own names (the resources' Ref is <cluster>/<name>).
    for (const [id, group] of [['CatalogNodegroup', nodes], ['ClientNodegroup', clientNodes]] as const) {
      new CfnOutput(this, id, { value: (group.node.defaultChild as eks.CfnNodegroup).attrNodegroupName });
    }
    // What a client's pod gives to run there: this nodeSelector (key=value), and a toleration of this taint.
    new CfnOutput(this, 'ClientNodeSelector', { value: `${POOL_LABEL}=${CLIENT_NODE_POOL}` });
    new CfnOutput(this, 'ClientNodeTaint', { value: `${POOL_LABEL}=${CLIENT_NODE_POOL}:NoSchedule` });
    new CfnOutput(this, 'LbcRoleArn', { value: lbc.role.roleArn });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
