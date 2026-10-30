import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { forbidResources } from '../src/guard';
import { nagChecks } from '../src/nag';
import {
  CATALOG_NODE_POOL, CLIENT_NODE_POOL, POOL_LABEL, TestEnvironmentStack, TestEnvironmentStackProps,
} from '../test-env/stack';
import { unacknowledged } from './nag-helpers';

const props: TestEnvironmentStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  name: 'ct1',
  sourceSha: 'a'.repeat(40),
  availabilityZones: ['us-west-2a', 'us-west-2b'],
  operatorCidr: '198.51.100.7/32',
  adminPrincipalArn: 'arn:aws:iam::111122223333:role/operator',
};
const build = (overrides: Partial<TestEnvironmentStackProps> = {}) => {
  const stack = new TestEnvironmentStack(new App(), 'ColvennaCatalogTestCt1', { ...props, ...overrides });
  return { stack, template: Template.fromStack(stack) };
};
const of = (template: Template, type: string) => Object.values(template.findResources(type)) as any[];

// One synthesis for the read-only checks: the cluster makes it slow.
const { stack: built, template } = build();

test("exposes the API to the operator's /32 only", () => {
  const [cluster] = of(template, 'Custom::AWSCDK-EKS-Cluster');
  assert.deepEqual(cluster.Properties.Config.resourcesVpcConfig.publicAccessCidrs, ['198.51.100.7/32']);
  for (const operatorCidr of ['10.0.0.1/32', '192.168.1.1/32', '198.51.100.0/24', '0.0.0.0/0', '::1/128', 'x']) {
    assert.throws(() => new TestEnvironmentStack(new App(), 'T', { ...props, operatorCidr }), /operatorCidr/, operatorCidr);
  }
});

test('admits only authenticated users to public info', () => {
  const [patch] = of(template, 'Custom::AWSCDK-EKS-KubernetesPatch');
  assert.equal(patch.Properties.ResourceName, 'clusterrolebinding/system:public-info-viewer');
  for (const key of ['ApplyPatchJson', 'RestorePatchJson']) {
    assert.deepEqual(JSON.parse(patch.Properties[key]).subjects,
      [{ apiGroup: 'rbac.authorization.k8s.io', kind: 'Group', name: 'system:authenticated' }], key);
  }
});

test('enforces network policy', () => {
  const cni = of(template, 'AWS::EKS::Addon').find((a) => a.Properties.AddonName === 'vpc-cni');
  assert.equal(JSON.parse(cni.Properties.ConfigurationValues).enableNetworkPolicy, 'true');
});

test('gives the admin principal cluster admin only', () => {
  const [entry] = of(template, 'AWS::EKS::AccessEntry');
  assert.equal(entry.Properties.PrincipalArn, 'arn:aws:iam::111122223333:role/operator');
  assert.deepEqual(entry.Properties.AccessPolicies, [{
    AccessScope: { Type: 'cluster' },
    PolicyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy',
  }]);
});

test("the load balancer controller's service account carries the pinned upstream policy", () => {
  const manifests = of(template, 'Custom::AWSCDK-EKS-KubernetesResource')
    .map((r) => JSON.stringify(r.Properties.Manifest));
  assert.ok(manifests.some((m) => m.includes('\\"kind\\":\\"ServiceAccount\\"')
    && m.includes('aws-load-balancer-controller') && m.includes('kube-system')), 'the service account');
  const pinned = JSON.parse(readFileSync(resolve(__dirname, '../test-env/lbc-iam-policy.json'), 'utf8'));
  const policies = of(template, 'AWS::IAM::Policy').filter((p) => p.Properties.PolicyName.startsWith('LbcPolicy'));
  assert.equal(policies.length, 1);
  assert.equal(policies[0].Properties.PolicyDocument.Statement.length, pinned.Statement.length);
});

test('lbc policy is the pinned upstream file', () => {
  const file = readFileSync(resolve(__dirname, '../test-env/lbc-iam-policy.json'));
  assert.equal(createHash('sha256').update(file).digest('hex'),
    '16f232c9d9f79366fe949c4550ad517a202380058a9e48d45a4e215044a20a6a');  // pragma: allowlist secret - the SHA-256 of the pinned upstream file, which the test checks
});

test('outputs what the scripts read', () => {
  for (const output of ['ClusterName', 'OidcIssuer', 'ClusterSecurityGroupId', 'VpcId', 'PrivateSubnetIds',
    'PrivateSubnetZones', 'NodeSubnetCidrs', 'LbcRoleArn', 'SourceSha', 'ApiEndpointId', 'ExistingDbEndpoint',
    'ExistingDbPort', 'ExistingDbInstanceId', 'ExistingDbAdminSecretArn', 'ExistingDbSecurityGroupId',
    'ExistingBucket', 'ClientSubnetIds', 'ClientSubnetCidrs', 'ClientSubnetZones', 'ClientSecurityGroupId',
    'CatalogNodegroup', 'ClientNodegroup', 'ClientNodeSelector', 'ClientNodeTaint']) {
    template.hasOutput(output, {});
  }
  for (const [output, group] of [['CatalogNodegroup', 'Nodes'], ['ClientNodegroup', 'ClientNodes']]) {
    const [id] = Object.keys(template.findResources('AWS::EKS::Nodegroup')).filter((k) => k.startsWith(`ClusterNodegroup${group}`));
    template.hasOutput(output, { Value: { 'Fn::GetAtt': [id, 'NodegroupName'] } });
  }
  template.hasOutput('ClientNodeSelector', { Value: 'colvenna-catalog/test-pool=clients' });
  template.hasOutput('ClientNodeTaint', { Value: 'colvenna-catalog/test-pool=clients:NoSchedule' });
});

test("two node groups: the catalogs' two t3.large, untainted; the clients', tainted", () => {
  const groups = of(template, 'AWS::EKS::Nodegroup');
  const pool = (name: string) => groups.find((g) => g.Properties.Labels[POOL_LABEL] === name);
  assert.equal(groups.length, 2);
  const catalog = pool(CATALOG_NODE_POOL).Properties;
  assert.deepEqual(catalog.InstanceTypes, ['t3.large']);
  assert.deepEqual(catalog.ScalingConfig, { MinSize: 2, DesiredSize: 2, MaxSize: 2 });
  assert.equal(catalog.Taints, undefined);
  const clients = pool(CLIENT_NODE_POOL).Properties;
  assert.deepEqual(clients.InstanceTypes, ['t3.xlarge']);
  assert.deepEqual(clients.ScalingConfig, { MinSize: 2, DesiredSize: 2, MaxSize: 4 });
  assert.deepEqual(clients.Taints, [{ Key: POOL_LABEL, Value: CLIENT_NODE_POOL, Effect: 'NO_SCHEDULE' }]);
});

test("CoreDNS runs on the clients' nodes", () => {
  const dns = of(template, 'AWS::EKS::Addon').find((a) => a.Properties.AddonName === 'coredns');
  const values = JSON.parse(dns.Properties.ConfigurationValues);
  assert.deepEqual(values.nodeSelector, { [POOL_LABEL]: CLIENT_NODE_POOL });
  assert.deepEqual(values.tolerations[0], { key: POOL_LABEL, operator: 'Equal', value: CLIENT_NODE_POOL, effect: 'NoSchedule' });
  // After both node groups: it has nowhere to run before the clients' nodes.
  for (const id of Object.keys(template.findResources('AWS::EKS::Nodegroup'))) {
    assert.ok(dns.DependsOn.includes(id), id);
  }
});

/** The logical IDs of the subnets of a subnet group (Vpc<group>Subnet<n>Subnet...). */
const subnetsOf = (group: string) => Object.entries(template.findResources('AWS::EC2::Subnet'))
  .filter(([id]) => id.startsWith(`Vpc${group}Subnet`)).map(([id]) => id).sort();
const refs = (list: any[]) => list.map((r) => r.Ref).sort();

test('the cluster and its nodes run in the nodes subnets only', () => {
  const nodes = subnetsOf('nodes');
  assert.equal(nodes.length, 2);
  const [cluster] = of(template, 'Custom::AWSCDK-EKS-Cluster');
  assert.deepEqual(refs(cluster.Properties.Config.resourcesVpcConfig.subnetIds), nodes);
  for (const group of of(template, 'AWS::EKS::Nodegroup')) {
    assert.deepEqual(refs(group.Properties.Subnets), nodes);
  }
  // The subnets made before the additions keep their CIDRs, so an environment updates in place.
  const cidrs = Object.fromEntries(Object.entries(template.findResources('AWS::EC2::Subnet'))
    .map(([id, r]: any) => [id.replace(/Subnet[0-9A-F]{8}$/, ''), r.Properties.CidrBlock]));
  assert.deepEqual(cidrs, {
    VpcpublicSubnet1: '10.85.0.0/24', VpcpublicSubnet2: '10.85.1.0/24',
    VpcnodesSubnet1: '10.85.8.0/21', VpcnodesSubnet2: '10.85.16.0/21',
    VpcclientsSubnet1: '10.85.24.0/24', VpcclientsSubnet2: '10.85.25.0/24',
  });
});

test('an execute-api endpoint with private DNS in the nodes subnets, HTTPS from the VPC only', () => {
  const [endpoint] = of(template, 'AWS::EC2::VPCEndpoint')
    .filter((e) => JSON.stringify(e.Properties.ServiceName).includes('execute-api'));
  assert.equal(endpoint.Properties.VpcEndpointType, 'Interface');
  assert.equal(endpoint.Properties.PrivateDnsEnabled, true);
  assert.deepEqual(refs(endpoint.Properties.SubnetIds), subnetsOf('nodes'));
  const groups = template.findResources('AWS::EC2::SecurityGroup');
  const [group] = endpoint.Properties.SecurityGroupIds.map((g: any) => groups[g['Fn::GetAtt'][0]]);
  assert.deepEqual(group.Properties.SecurityGroupIngress,
    [{ CidrIp: '10.85.0.0/16', Description: 'HTTPS from the VPC', FromPort: 443, IpProtocol: 'tcp', ToPort: 443 }]);
});

test('an existing PostgreSQL 16 with IAM authentication, TLS only, deleted with the stack', () => {
  const [id, db]: any = Object.entries(template.findResources('AWS::RDS::DBInstance'))[0];
  assert.equal(db.Properties.Engine, 'postgres');
  assert.match(db.Properties.EngineVersion, /^16(\.|$)/);
  assert.equal(db.Properties.DBInstanceClass, 'db.t4g.small');
  assert.equal(db.Properties.DBInstanceIdentifier, 'colvenna-catalog-test-ct1');
  assert.equal(db.Properties.EnableIAMDatabaseAuthentication, true);
  assert.equal(db.Properties.StorageEncrypted, true);
  assert.equal(db.Properties.PubliclyAccessible, false);
  assert.equal(db.Properties.MultiAZ, false);
  assert.equal(db.Properties.DeletionProtection, false);
  assert.equal(db.Properties.DeleteAutomatedBackups, true);
  assert.equal(db.DeletionPolicy, 'Delete', `${id}: no final snapshot`);
  const [parameters] = of(template, 'AWS::RDS::DBParameterGroup');
  assert.equal(parameters.Properties.Parameters['rds.force_ssl'], '1');
  const [subnets] = of(template, 'AWS::RDS::DBSubnetGroup');
  assert.deepEqual(refs(subnets.Properties.SubnetIds), subnetsOf('nodes'));
  const groups = template.findResources('AWS::EC2::SecurityGroup');
  const group = groups[db.Properties.VPCSecurityGroups[0]['Fn::GetAtt'][0]];
  assert.deepEqual(group.Properties.SecurityGroupIngress, [{
    CidrIp: '10.85.0.0/16', Description: 'PostgreSQL from the VPC', FromPort: 5432, IpProtocol: 'tcp', ToPort: 5432,
  }]);
  const [secret] = of(template, 'AWS::SecretsManager::Secret');
  assert.equal(secret.Properties.Name, 'colvenna-catalog-test-ct1/existing-db-admin');
  assert.equal(JSON.parse(secret.Properties.GenerateSecretString.SecretStringTemplate).username, 'colvenna_test_admin');
});

test('an existing bucket, private and TLS only, deleted with the stack', () => {
  const [bucket] = of(template, 'AWS::S3::Bucket');
  assert.deepEqual(bucket.Properties.PublicAccessBlockConfiguration, {
    BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true,
  });
  assert.ok(bucket.Properties.BucketEncryption, 'encrypted');
  assert.equal(bucket.DeletionPolicy, 'Delete');
  const [policy] = of(template, 'AWS::S3::BucketPolicy');
  assert.ok(policy.Properties.PolicyDocument.Statement.some((st: any) => st.Effect === 'Deny'
    && st.Condition?.Bool?.['aws:SecureTransport'] === 'false'), 'denies requests without TLS');
});

test('client subnets outside the cluster, routed to S3 and nowhere out of the VPC', () => {
  const clients = subnetsOf('clients');
  assert.equal(clients.length, 2);
  const resources = template.toJSON().Resources;
  const tables = clients.map((id) => id.replace(/Subnet[0-9A-F]{8}$/, 'RouteTable'))
    .map((prefix) => Object.keys(resources).find((k) => k.startsWith(prefix) && resources[k].Type === 'AWS::EC2::RouteTable'));
  for (const table of tables) {
    assert.ok(table);
    const routes = of(template, 'AWS::EC2::Route').filter((r) => r.Properties.RouteTableId.Ref === table);
    assert.deepEqual(routes, [], `${table}: no route out of the VPC`);
  }
  const [s3] = of(template, 'AWS::EC2::VPCEndpoint').filter((e) => e.Properties.VpcEndpointType === 'Gateway');
  for (const table of tables) {
    assert.ok(refs(s3.Properties.RouteTableIds).includes(table as string), `${table} reaches S3 through the gateway endpoint`);
  }
  const groups = Object.entries(template.findResources('AWS::EC2::SecurityGroup'))
    .filter(([, g]: any) => String(g.Properties.GroupDescription).includes('clients outside the cluster'));
  assert.equal(groups.length, 1);
  const [, group]: any = groups[0];
  assert.equal(group.Properties.SecurityGroupIngress, undefined, 'admits nothing');
  assert.deepEqual(group.Properties.SecurityGroupEgress.map((e: any) => [e.CidrIp, e.FromPort, e.ToPort]).sort(),
    [['0.0.0.0/0', 443, 443], ['10.85.0.0/16', 0, 65535]]);
});

test('passes cdk-nag and the guard', () => {
  const { stack } = build();
  nagChecks(stack);
  forbidResources(stack);
  assert.deepEqual(unacknowledged(stack), []);
  assert.ok(built);
});

test('rejects zones of another region or the same zone twice', () => {
  assert.throws(() => build({ availabilityZones: ['us-east-2a', 'us-west-2b'] }), /zones/);
  assert.throws(() => build({ availabilityZones: ['us-west-2a', 'us-west-2a'] }), /zones/);
  assert.throws(() => build({ adminPrincipalArn: 'arn:aws:iam::444455556666:role/x' }), /adminPrincipalArn/);
  assert.throws(() => build({ name: 'Bad' }), /-c env|name/);
});

test('the cluster is named after the environment', () => {
  const [cluster] = of(template, 'Custom::AWSCDK-EKS-Cluster');
  assert.equal(cluster.Properties.Config.name, 'colvenna-catalog-test-ct1');
});

test('the control-plane log group goes only after the cluster', () => {
  // EKS writes its log group while the cluster shuts down: removed earlier, it would be made again.
  const retention = Object.entries(template.findResources('Custom::LogRetention'))
    .filter(([, r]: any) => JSON.stringify(r.Properties.LogGroupName).includes('/aws/eks/'));
  assert.equal(retention.length, 1);
  assert.deepEqual(retention[0][1].Properties.LogGroupName, '/aws/eks/colvenna-catalog-test-ct1/cluster');
  const [cluster] = of(template, 'Custom::AWSCDK-EKS-Cluster');
  assert.ok((cluster.DependsOn ?? []).includes(retention[0][0]), `the cluster depends on ${retention[0][0]}`);
});

test("the load balancer controller's role trusts its service account only", () => {
  // addServiceAccount writes the trust conditions through a CfnJson (their keys hold the issuer).
  const conditions = of(template, 'Custom::AWSCDKCfnJson').map((r) => JSON.stringify(r.Properties.Value));
  assert.ok(conditions.some((c) => c.includes('system:serviceaccount:kube-system:aws-load-balancer-controller')
    && c.includes('sts.amazonaws.com')), 'sub and aud of the controller service account');
  const trusts = of(template, 'AWS::IAM::Role')
    .map((r) => r.Properties.AssumeRolePolicyDocument.Statement)
    .filter((s) => s.some((st: any) => st.Action === 'sts:AssumeRoleWithWebIdentity'));
  assert.equal(trusts.length, 1, 'one role assumed with a web identity: the controller\'s');
  assert.equal(trusts[0].length, 1);
  assert.ok(trusts[0][0].Condition.StringEquals, 'conditioned on sub and aud');
});

test("the log retention function's log-group scope is acknowledged here, not for every stack", () => {
  const nagSource = readFileSync(resolve(__dirname, '../src/nag.ts'), 'utf8');
  assert.doesNotMatch(nagSource, /log-group:\.\+/, 'src/nag.ts keeps the product stacks\' acknowledgement narrow');
});
