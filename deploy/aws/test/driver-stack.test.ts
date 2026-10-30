import assert from 'node:assert/strict';
import test from 'node:test';
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { nagChecks } from '../src/nag';
import { buildDriverApp } from '../test-env/driver-app';
import { DriverStack, DriverStackProps } from '../test-env/driver-stack';
import { unacknowledged } from './nag-helpers';

const props: DriverStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  environment: 'sl1',
  sourceSha: 'a'.repeat(40),
  vpcId: 'vpc-0123456789abcdef0',
  subnetIds: ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
  subnetZones: ['us-west-2a', 'us-west-2b'],
  repositoryName: 'colvenna-catalog-sl1/colvenna-catalog',
  imageDigest: `sha256:${'b'.repeat(64)}`,
};

function build(overrides: Partial<DriverStackProps> = {}) {
  const stack = new DriverStack(new App(), 'ColvennaCatalogTestSl1Driver', { ...props, ...overrides });
  return { stack, template: Template.fromStack(stack) };
}
const resources = (template: Template, type: string) => Object.values(template.findResources(type)) as any[];

test('one driver function, from the image by digest, in the VPC', () => {
  const { template } = build();
  const [fn] = resources(template, 'AWS::Lambda::Function');
  assert.equal(resources(template, 'AWS::Lambda::Function').length, 1);
  assert.equal(fn.Properties.FunctionName, 'colvenna-catalog-sl1-test-driver');
  assert.equal(fn.Properties.PackageType, 'Image');
  assert.deepEqual(fn.Properties.Architectures, ['x86_64']);
  assert.match(JSON.stringify(fn.Properties.Code.ImageUri), new RegExp(`colvenna-catalog-sl1/colvenna-catalog@sha256:${'b'.repeat(64)}`));
  assert.deepEqual(fn.Properties.VpcConfig.SubnetIds, props.subnetIds);
  assert.equal(fn.Properties.Timeout, 900);
});

test('no Function URL, no API and no resource-based policy: only IAM-signed Invoke reaches it', () => {
  const { template } = build();
  for (const type of ['AWS::Lambda::Url', 'AWS::Lambda::Permission', 'AWS::ApiGateway::RestApi', 'AWS::ApiGatewayV2::Api',
    'AWS::ElasticLoadBalancingV2::LoadBalancer', 'AWS::IAM::User', 'AWS::IAM::AccessKey']) {
    assert.deepEqual(template.findResources(type), {}, type);
  }
});

test('its role reaches no data: the managed VPC execution policy only', () => {
  const { template } = build();
  const [role] = resources(template, 'AWS::IAM::Role');
  assert.equal(resources(template, 'AWS::IAM::Role').length, 1);
  assert.match(JSON.stringify(role.Properties.ManagedPolicyArns), /service-role\/AWSLambdaVPCAccessExecutionRole/);
  assert.equal(role.Properties.Policies, undefined);
  // Any inline policy CDK adds (an image pull, say) must hold no S3, STS or Cognito action.
  for (const policy of resources(template, 'AWS::IAM::Policy')) {
    const text = JSON.stringify(policy.Properties.PolicyDocument);
    assert.doesNotMatch(text, /"(s3|sts|cognito-identity):/, text);
  }
});

test('HTTPS out only, nothing in', () => {
  const { template } = build();
  const [group] = resources(template, 'AWS::EC2::SecurityGroup');
  assert.equal(group.Properties.SecurityGroupIngress, undefined);
  assert.deepEqual(group.Properties.SecurityGroupEgress.map((r: any) => [r.IpProtocol, r.FromPort, r.ToPort, r.CidrIp]),
    [['tcp', 443, 443, '0.0.0.0/0']]);
});

test('with apiPort, also out to the catalog API port (an internal NLB), still nothing in', () => {
  const { template } = build({ apiPort: 8181 });
  const [group] = resources(template, 'AWS::EC2::SecurityGroup');
  assert.equal(group.Properties.SecurityGroupIngress, undefined);
  assert.deepEqual(group.Properties.SecurityGroupEgress.map((r: any) => [r.IpProtocol, r.FromPort, r.ToPort, r.CidrIp]),
    [['tcp', 443, 443, '0.0.0.0/0'], ['tcp', 8181, 8181, '0.0.0.0/0']]);
  // EC2 takes only these characters in a rule's description (an apostrophe failed a live deploy, #52).
  for (const rule of group.Properties.SecurityGroupEgress) {
    assert.match(rule.Description, /^[a-zA-Z0-9. _\-:/()#,@[\]+=&;{}!$*]{0,255}$/, rule.Description);
  }
  assert.throws(() => build({ apiPort: 443 }), /apiPort/);
  assert.throws(() => build({ apiPort: 70000 }), /apiPort/);
});

test('its log group goes with the stack', () => {
  const { template } = build();
  const [group] = resources(template, 'AWS::Logs::LogGroup');
  assert.equal(group.Properties.LogGroupName, '/aws/lambda/colvenna-catalog-sl1-test-driver');
  assert.equal(group.DeletionPolicy, 'Delete');
  assert.equal(group.Properties.RetentionInDays, 7);
});

test('passes cdk-nag', () => {
  const { stack } = build();
  nagChecks(stack);
  assert.deepEqual(unacknowledged(stack), []);
});

test('rejects malformed inputs', () => {
  assert.throws(() => build({ repositoryName: 'colvenna-catalog-other/colvenna-catalog' }), /repositoryName/);
  assert.throws(() => build({ imageDigest: 'latest' }), /imageDigest/);
  assert.throws(() => build({ subnetZones: ['us-west-2a'] }), /-c subnetZones/);
  assert.throws(() => build({ environment: 'Sl1' }), /-c env/);
});

test('the app builds the one driver stack and needs every input', () => {
  const context = {
    env: 'sl1', account: '111122223333', region: 'us-west-2', sourceSha: 'a'.repeat(40), vpcId: props.vpcId,
    subnetIds: props.subnetIds.join(','), subnetZones: props.subnetZones.join(','),
    repositoryName: props.repositoryName, imageDigest: props.imageDigest,
  };
  const stacks = buildDriverApp(context).node.children.filter((c): c is Stack => c instanceof Stack);
  assert.deepEqual(stacks.map((s) => s.stackName), ['ColvennaCatalogTestSl1Driver']);
  for (const key of Object.keys(context)) {
    const without: Record<string, string> = { ...context };
    delete without[key];
    assert.throws(() => buildDriverApp(without), new RegExp(`-c ${key}`), key);
  }
});

const access = { securityGroupId: 'sg-0123456789abcdef0', dbResourceId: 'db-ABCDEFGHIJKLMNOPQRSTUVWXYZ', dbUser: 'colvenna_catalog' };  // pragma: allowlist secret - a made-up RDS resource id

test('with database access: the functions\' group, and IAM sign-in as the catalog\'s user only', () => {
  const { template } = build({ databaseAccess: access });
  const [fn] = resources(template, 'AWS::Lambda::Function');
  assert.equal(fn.Properties.VpcConfig.SecurityGroupIds.length, 2);
  assert.match(JSON.stringify(fn.Properties.VpcConfig.SecurityGroupIds), /sg-0123456789abcdef0/);
  const statements = resources(template, 'AWS::IAM::Policy').flatMap((p) => p.Properties.PolicyDocument.Statement);
  const connect = statements.filter((s: any) => JSON.stringify(s.Action).includes('rds-db:connect'));
  assert.equal(connect.length, 1);
  assert.match(JSON.stringify(connect[0].Resource), /dbuser:db-ABCDEFGHIJKLMNOPQRSTUVWXYZ\/colvenna_catalog/);
  for (const s of statements) {
    assert.doesNotMatch(JSON.stringify(s.Action), /"(s3|sts|cognito-identity|secretsmanager):/);
  }
  // No group of its own is changed, and none admits anything in.
  assert.equal(resources(template, 'AWS::EC2::SecurityGroupIngress').length, 0);
});

test('with database access: passes cdk-nag; its inputs are checked; the app takes all three or none', () => {
  const { stack } = build({ databaseAccess: access });
  nagChecks(stack);
  assert.deepEqual(unacknowledged(stack), []);
  assert.throws(() => build({ databaseAccess: { ...access, securityGroupId: 'group' } }), /securityGroupId/);
  assert.throws(() => build({ databaseAccess: { ...access, dbUser: 'Bad User' } }), /databaseAccess/);
  const context = {
    env: 'sl1', account: '111122223333', region: 'us-west-2', sourceSha: 'a'.repeat(40), vpcId: props.vpcId,
    subnetIds: props.subnetIds.join(','), subnetZones: props.subnetZones.join(','),
    repositoryName: props.repositoryName, imageDigest: props.imageDigest, catalogSecurityGroupId: access.securityGroupId,
  };
  assert.throws(() => buildDriverApp(context), /all three, or none/);
  buildDriverApp({ ...context, dbResourceId: access.dbResourceId, dbUser: access.dbUser });
});
