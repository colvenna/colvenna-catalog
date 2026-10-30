import assert from 'node:assert/strict';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { CatalogEksStack, CatalogEksStackProps } from '../src/eks-stack';
import { nagChecks } from '../src/nag';
import { unacknowledged } from './nag-helpers';

const ISSUER_PATH = 'oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF';
const props: CatalogEksStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  environment: 'uat',
  sourceSha: 'a'.repeat(40),
  oidcIssuer: `https://${ISSUER_PATH}`,
  namespace: 'colvenna-catalog',
  serviceAccount: 'colvenna-catalog',
  databaseUser: 'colvenna_catalog',
  bucketName: 'bucket',
  warehousePrefix: 'warehouse',
};

function build(overrides: Partial<CatalogEksStackProps> = {}) {
  const stack = new CatalogEksStack(new App(), 'ColvennaCatalogUatEks', { ...props, ...overrides });
  return { stack, template: Template.fromStack(stack) };
}

const roles = (template: Template) => Object.values(template.findResources('AWS::IAM::Role')) as any[];
const roleNamed = (template: Template, name: string) => {
  const found = roles(template).filter((r) => r.Properties.RoleName === name);
  assert.equal(found.length, 1, `one role ${name}`);
  return found[0];
};
/** Every statement of the policies attached to the role (by its logical ID). */
function statementsOf(template: Template, roleName: string): any[] {
  const [logicalId] = Object.entries(template.findResources('AWS::IAM::Role'))
    .find(([, r]: any) => r.Properties.RoleName === roleName)!;
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === logicalId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}
const flat = (value: unknown) => JSON.stringify(value);

test("trusts only the catalog's service account", () => {
  const { template } = build();
  for (const name of ['colvenna-catalog-uat-server', 'colvenna-catalog-uat-data']) {
    const statements = roleNamed(template, name).Properties.AssumeRolePolicyDocument.Statement;
    assert.equal(statements.length, 1, name);
    assert.equal(statements[0].Action, 'sts:AssumeRoleWithWebIdentity');
    assert.deepEqual(statements[0].Condition, {
      StringEquals: {
        [`${ISSUER_PATH}:aud`]: 'sts.amazonaws.com',
        [`${ISSUER_PATH}:sub`]: 'system:serviceaccount:colvenna-catalog:colvenna-catalog',
      },
    });
    assert.match(flat(statements[0].Principal), new RegExp(`oidc-provider/${ISSUER_PATH}`));
  }
});

test('reaches the warehouse prefix and nothing else', () => {
  const { template } = build();
  for (const name of ['colvenna-catalog-uat-server', 'colvenna-catalog-uat-data']) {
    const statements = statementsOf(template, name);
    const objects = statements.filter((s) => [s.Action].flat().includes('s3:GetObject'));
    assert.equal(objects.length, 1, name);
    assert.deepEqual([objects[0].Action].flat().sort(), [
      's3:AbortMultipartUpload', 's3:DeleteObject', 's3:GetObject', 's3:ListMultipartUploadParts', 's3:PutObject',
    ]);
    assert.match(flat(objects[0].Resource), /:s3:::bucket\/warehouse\/\*"/);
    for (const s of statements) {
      assert.doesNotMatch(flat(s.Resource), /:s3:::bucket\/\*"/, `${name}: no whole-bucket objects`);
      assert.notEqual(s.Resource, '*', `${name}: no wildcard resource`);
    }
    const list = statements.find((s) => [s.Action].flat().includes('s3:ListBucket'));
    // Only under the prefix: a bare 'warehouse' would also list keys of a sibling such as 'warehouse2/'.
    assert.deepEqual(list.Condition, { StringLike: { 's3:prefix': ['warehouse/*'] } });
  }
});

test('the data role allows twelve-hour sessions', () => {
  const { template } = build();
  assert.equal(roleNamed(template, 'colvenna-catalog-uat-data').Properties.MaxSessionDuration, 43200);
  assert.equal(roleNamed(template, 'colvenna-catalog-uat-server').Properties.MaxSessionDuration, undefined);
});

test('names its resources after the environment', () => {
  const { template } = build();
  roleNamed(template, 'colvenna-catalog-uat-server');
  roleNamed(template, 'colvenna-catalog-uat-data');
  template.hasResourceProperties('AWS::SecretsManager::Secret', {
    Name: 'colvenna-catalog-uat/db-user',
    GenerateSecretString: {
      SecretStringTemplate: JSON.stringify({ username: 'colvenna_catalog' }),
      GenerateStringKey: 'password',
      PasswordLength: 40,
      ExcludePunctuation: true,
    },
  });
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: 'colvenna-catalog-uat/colvenna-catalog',
    ImageTagMutability: 'IMMUTABLE',
    ImageScanningConfiguration: { ScanOnPush: true },
  });
  for (const output of ['ServerRoleArn', 'DataRoleArn', 'DbUserSecretArn', 'RepositoryUri', 'SourceSha']) {
    template.hasOutput(output, {});
  }
});

test('has no function URL and no IAM user', () => {
  const { template } = build();
  for (const type of ['AWS::Lambda::Url', 'AWS::IAM::User', 'AWS::IAM::AccessKey']) {
    assert.deepEqual(template.findResources(type), {}, type);
  }
});

test('passes cdk-nag', () => {
  const { stack } = build();
  nagChecks(stack);
  assert.deepEqual(unacknowledged(stack), []);
});

test('rejects malformed inputs', () => {
  assert.throws(() => build({ warehousePrefix: 'warehouse/' }), /-c warehousePrefix/);
  assert.throws(() => build({ oidcIssuer: `https://${ISSUER_PATH.replace('us-west-2', 'us-east-2')}` }), /-c oidcIssuer/);
  assert.throws(() => build({ namespace: 'Colvenna' }), /-c namespace/);
  assert.throws(() => build({ databaseUser: 'colvenna-catalog' }), /-c databaseUser/);
  assert.throws(() => build({ environment: 'release' }), /-c env/);
});
