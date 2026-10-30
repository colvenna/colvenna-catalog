import assert from 'node:assert/strict';
import test from 'node:test';
import { Stack } from 'aws-cdk-lib';
import { buildTestApp } from '../test-env/app';

const context = {
  name: 'ct1', account: '111122223333', region: 'us-west-2', sourceSha: 'a'.repeat(40),
  zones: 'us-west-2a,us-west-2b', operatorCidr: '198.51.100.7/32',
  adminPrincipalArn: 'arn:aws:iam::111122223333:role/operator',
};

test('builds ColvennaCatalogTest<Name> and nothing else', () => {
  const stacks = buildTestApp(context).node.children.filter((c): c is Stack => c instanceof Stack);
  assert.deepEqual(stacks.map((s) => s.stackName), ['ColvennaCatalogTestCt1']);
});

test('refuses a missing key, naming it', () => {
  for (const key of Object.keys(context)) {
    const without: Record<string, string> = { ...context };
    delete without[key];
    assert.throws(() => buildTestApp(without), new RegExp(`-c ${key}`), key);
  }
});
