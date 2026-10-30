import assert from 'node:assert/strict';
import test from 'node:test';
import { Stack } from 'aws-cdk-lib';
import { buildApp } from '../src/app';

const common = { account: '111122223333', region: 'us-west-2', sourceSha: 'a'.repeat(40) };
const contexts: Record<string, Record<string, string>> = {
  eks: {
    ...common, stack: 'eks', env: 'uat', bucket: 'bucket', warehousePrefix: 'warehouse',
    oidcIssuer: 'https://oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF',
  },
  storage: {
    ...common, stack: 'storage', env: 'uat', createBucket: 'true', createDatabase: 'true',
    vpcId: 'vpc-0123456789abcdef0', subnetIds: 'subnet-0123456789abcdef0,subnet-0123456789abcdef1',
    subnetZones: 'us-west-2a,us-west-2b', clientSecurityGroupId: 'sg-0123456789abcdef0',
  },
  release: { ...common, stack: 'release', pullAccounts: '444455556666' },
  network: {
    ...common, stack: 'network', env: 'uat', networkMode: 'create', vpcCidr: '10.80.0.0/16', subnetZones: 'us-west-2a,us-west-2b',
    databasePort: '5432',
  },
  existingNetwork: {
    ...common, stack: 'network', env: 'uat', networkMode: 'existing', vpcId: 'vpc-0123456789abcdef0', vpcCidr: '10.1.0.0/16',
    subnetIds: 'subnet-0123456789abcdef0,subnet-0123456789abcdef1', subnetZones: 'us-west-2a,us-west-2b', databasePort: '5432',
  },
  images: { ...common, stack: 'images', env: 'uat' },
  serverless: {
    ...common, stack: 'serverless', env: 'uat', vpcId: 'vpc-0123456789abcdef0',
    subnetIds: 'subnet-0123456789abcdef0,subnet-0123456789abcdef1', subnetZones: 'us-west-2a,us-west-2b',
    securityGroupId: 'sg-0123456789abcdef0', apiEndpointIds: 'vpce-0123456789abcdef0',
    imageUri: `111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog@sha256:${'1'.repeat(64)}`,
    bucket: 'bucket', warehousePrefix: 'warehouse', warehouseName: 'colvenna', databaseEndpoint: 'db.example.com',
    databasePort: '5432', databaseName: 'colvenna_catalog', databaseUser: 'colvenna_catalog', databaseResourceId: 'db-ABCDEFGHIJKLMNOPQRSTUVWXYZ', dataTokens: 'aws-sts',  // pragma: allowlist secret - a made-up RDS resource id
    dataTokenDuration: 'PT3H', memory: '2048', concurrency: '10', snapStart: 'false',
  },
};
const without = (context: Record<string, string>, key: string) => {
  const copy = { ...context };
  delete copy[key];
  return copy;
};
const stacks = (context: Record<string, string>) =>
  buildApp(context).node.children.filter((c): c is Stack => c instanceof Stack).map((s) => s.stackName);

test('app builds exactly the stack it is asked for', () => {
  assert.deepEqual(stacks(contexts.eks), ['ColvennaCatalogUatEks']);
  assert.deepEqual(stacks(contexts.storage), ['ColvennaCatalogUatStorage']);
  assert.deepEqual(stacks(contexts.release), ['ColvennaCatalogRelease']);
  assert.deepEqual(stacks(without(contexts.release, 'pullAccounts')), ['ColvennaCatalogRelease']);
  assert.deepEqual(stacks({ ...contexts.storage, createDatabase: 'false', vpcId: '', subnetIds: '' }),
    ['ColvennaCatalogUatStorage']);
  assert.deepEqual(stacks(contexts.network), ['ColvennaCatalogUatNetwork']);
  assert.deepEqual(stacks(contexts.existingNetwork), ['ColvennaCatalogUatNetwork']);
  assert.deepEqual(stacks(contexts.images), ['ColvennaCatalogUatImages']);
  assert.deepEqual(stacks(contexts.serverless), ['ColvennaCatalogUatServerless']);
});

test('app refuses a stack without its context', () => {
  const cases: [string, string][] = [
    ['eks', 'oidcIssuer'], ['eks', 'bucket'], ['eks', 'warehousePrefix'], ['eks', 'env'],
    ['storage', 'createBucket'], ['storage', 'createDatabase'], ['storage', 'vpcId'], ['storage', 'subnetIds'],
    ['storage', 'subnetZones'], ['storage', 'clientSecurityGroupId'],
    ['eks', 'account'], ['storage', 'region'], ['release', 'sourceSha'],
    ['network', 'networkMode'], ['network', 'vpcCidr'], ['network', 'subnetZones'], ['network', 'databasePort'],
    ['existingNetwork', 'vpcId'], ['existingNetwork', 'subnetIds'], ['existingNetwork', 'vpcCidr'], ['images', 'env'],
    ...['vpcId', 'subnetIds', 'subnetZones', 'securityGroupId', 'apiEndpointIds', 'imageUri', 'bucket', 'warehousePrefix',
      'warehouseName', 'databaseEndpoint', 'databasePort', 'databaseName', 'databaseUser', 'databaseResourceId', 'dataTokens', 'dataTokenDuration',
      'memory', 'concurrency', 'snapStart'].map((key): [string, string] => ['serverless', key]),
  ];
  for (const [stack, key] of cases) {
    assert.throws(() => buildApp(without(contexts[stack], key)), new RegExp(`-c ${key}`), `${stack} without ${key}`);
  }
});

test('the defaults of the Kubernetes and database names are those colvenna-lake uses', () => {
  const app = buildApp(contexts.eks);
  const template = JSON.stringify(app.synth().getStackByName('ColvennaCatalogUatEks').template);
  assert.match(template, /system:serviceaccount:colvenna-catalog:colvenna-catalog/);
  assert.match(template, /\\"username\\":\\"colvenna_catalog\\"/);
});

test('unknown stack', () => {
  assert.throws(() => buildApp({ ...common, stack: 'foo' }), /-c stack/);
  assert.throws(() => buildApp(common), /-c stack/);
  assert.throws(() => buildApp({ ...contexts.network, networkMode: 'peered' }), /-c networkMode/);
});
