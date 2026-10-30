import assert from 'node:assert/strict';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { nagChecks } from '../src/nag';
import { CatalogStorageStack, CatalogStorageStackProps } from '../src/storage-stack';
import { unacknowledged } from './nag-helpers';

const database = {
  vpcId: 'vpc-0123456789abcdef0',
  subnetIds: ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
  subnetZones: ['us-west-2a', 'us-west-2b'],
  clientSecurityGroupId: 'sg-0123456789abcdef0',
};
const base: CatalogStorageStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  environment: 'uat',
  sourceSha: 'a'.repeat(40),
  createBucket: true,
  database,
};

function build(overrides: Partial<CatalogStorageStackProps> = {}) {
  const stack = new CatalogStorageStack(new App(), 'ColvennaCatalogUatStorage', { ...base, ...overrides });
  return { stack, template: Template.fromStack(stack) };
}
const resources = (template: Template, type: string) => Object.values(template.findResources(type)) as any[];

test('keeps the data when the stack goes', () => {
  const { template } = build();
  const buckets = resources(template, 'AWS::S3::Bucket');
  assert.equal(buckets.length, 2, 'the warehouse and its access logs');
  for (const bucket of buckets) {
    assert.equal(bucket.DeletionPolicy, 'Retain');
    assert.equal(bucket.UpdateReplacePolicy, 'Retain');
  }
  const [instance] = resources(template, 'AWS::RDS::DBInstance');
  assert.equal(instance.DeletionPolicy, 'Snapshot');
  assert.equal(instance.UpdateReplacePolicy, 'Snapshot');
  assert.equal(instance.Properties.DeletionProtection, true);
});

test('the database admits only the cluster', () => {
  const { template } = build();
  const ingress = [
    ...resources(template, 'AWS::EC2::SecurityGroupIngress').map((r) => r.Properties),
    ...resources(template, 'AWS::EC2::SecurityGroup').flatMap((g) => g.Properties.SecurityGroupIngress ?? []),
  ];
  assert.deepEqual(ingress.map((rule) => ({
    from: rule.FromPort, to: rule.ToPort, source: rule.SourceSecurityGroupId, cidr: rule.CidrIp ?? rule.CidrIpv6,
  })), [{ from: 5432, to: 5432, source: 'sg-0123456789abcdef0', cidr: undefined }]);
});

test('the database requires TLS and stays private', () => {
  const { template } = build();
  template.hasResourceProperties('AWS::RDS::DBParameterGroup', { Parameters: { 'rds.force_ssl': '1' } });
  const [instance] = resources(template, 'AWS::RDS::DBInstance');
  assert.equal(instance.Properties.PubliclyAccessible, false);
  assert.equal(instance.Properties.StorageEncrypted, true);
  assert.equal(instance.Properties.Engine, 'postgres');
  assert.match(String(instance.Properties.EngineVersion), /^16/);
  assert.equal(instance.Properties.BackupRetentionPeriod, 7);
  // The serverless form signs in with IAM tokens; the EKS form's passwords still work.
  assert.equal(instance.Properties.EnableIAMDatabaseAuthentication, true);
});

test('the warehouse bucket logs access and refuses plain HTTP', () => {
  const { template } = build();
  const warehouse = resources(template, 'AWS::S3::Bucket').filter((b) => b.Properties.LoggingConfiguration);
  assert.equal(warehouse.length, 1);
  assert.ok(warehouse[0].Properties.LoggingConfiguration.DestinationBucketName);
  for (const policy of resources(template, 'AWS::S3::BucketPolicy')) {
    assert.ok(policy.Properties.PolicyDocument.Statement.some((s: any) => s.Effect === 'Deny'
      && s.Condition?.Bool?.['aws:SecureTransport'] === 'false'), 'refuses plain HTTP');
  }
});

test('bucket only and database only', () => {
  const bucketOnly = build({ database: undefined }).template;
  assert.equal(resources(bucketOnly, 'AWS::RDS::DBInstance').length, 0);
  bucketOnly.hasOutput('BucketName', {});
  assert.deepEqual(Object.keys(bucketOnly.findOutputs('*')).sort(), ['BucketName', 'SourceSha']);

  const databaseOnly = build({ createBucket: false }).template;
  assert.equal(resources(databaseOnly, 'AWS::S3::Bucket').length, 0);
  assert.deepEqual(Object.keys(databaseOnly.findOutputs('*')).sort(),
    ['DbAdminSecretArn', 'DbEndpoint', 'DbPort', 'DbResourceId', 'SourceSha']);
});

test('refuses to create nothing', () => {
  assert.throws(() => build({ createBucket: false, database: undefined }), /nothing to create/);
});

test('passes cdk-nag', () => {
  for (const overrides of [{}, { database: undefined }, { createBucket: false }]) {
    const { stack } = build(overrides);
    nagChecks(stack);
    assert.deepEqual(unacknowledged(stack), [], JSON.stringify(overrides));
  }
});

test("the database security group's description is one EC2 accepts", () => {
  const { template } = build();
  for (const group of resources(template, 'AWS::EC2::SecurityGroup')) {
    // EC2's GroupDescription pattern: no apostrophes, quotes or other characters outside this set.
    assert.match(group.Properties.GroupDescription, /^[a-zA-Z0-9. _\-:/()#,@[\]+=&;{}!$*]*$/);
  }
});

test('has no function URL and no IAM user', () => {
  const { template } = build();
  for (const type of ['AWS::Lambda::Url', 'AWS::IAM::User', 'AWS::IAM::AccessKey']) {
    assert.deepEqual(template.findResources(type), {}, type);
  }
});
