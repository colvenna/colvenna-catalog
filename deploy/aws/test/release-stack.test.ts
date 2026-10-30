import assert from 'node:assert/strict';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { nagChecks } from '../src/nag';
import { CatalogReleaseStack, RELEASE_REPOSITORIES } from '../src/release-stack';
import { unacknowledged } from './nag-helpers';

const build = (pullAccounts: string[]) => {
  const stack = new CatalogReleaseStack(new App(), 'ColvennaCatalogRelease', {
    env: { account: '111122223333', region: 'us-west-2' },
    sourceSha: 'a'.repeat(40),
    pullAccounts,
  });
  return { stack, template: Template.fromStack(stack) };
};
/** The stack's repositories by name: the image's and the chart's, nothing else. */
const repositories = (template: Template) => {
  const found = Object.values(template.findResources('AWS::ECR::Repository')) as any[];
  const byName: Record<string, any> = Object.fromEntries(found.map((r) => [r.Properties.RepositoryName, r]));
  assert.deepEqual(Object.keys(byName).sort(), Object.values(RELEASE_REPOSITORIES).sort());
  return byName;
};
/** Everything an ECR policy could let a principal do to a repository's content but read it. */
const WRITES = /^ecr:(\*|Put|Initiate|Upload|Complete|BatchDelete|Delete|Set|Tag|Untag|Replicate|Start|Create)/;

test('the image and the chart repositories', () => {
  const byName = repositories(build([]).template);
  assert.equal(RELEASE_REPOSITORIES.image, 'colvenna-catalog-release/colvenna-catalog');
  assert.equal(RELEASE_REPOSITORIES.chart, 'colvenna-catalog-release/charts/colvenna-catalog');
  assert.deepEqual(byName[RELEASE_REPOSITORIES.image].Properties.ImageScanningConfiguration, { ScanOnPush: true });
  assert.deepEqual(byName[RELEASE_REPOSITORIES.chart].Properties.ImageScanningConfiguration, { ScanOnPush: false });
});

test('no policy without pull accounts', () => {
  for (const repo of Object.values(repositories(build([]).template))) {
    assert.equal(repo.Properties.RepositoryPolicyText, undefined);
  }
});

test('pull accounts may pull the image and the chart, and do nothing else', () => {
  for (const [name, repo] of Object.entries(repositories(build(['444455556666', '777788889999']).template))) {
    const statements = repo.Properties.RepositoryPolicyText.Statement;
    assert.equal(statements.length, 1, name);
    assert.equal(statements[0].Sid, 'ReleasePull');
    assert.equal(statements[0].Effect, 'Allow');
    assert.equal(statements[0].Condition, undefined);
    assert.deepEqual([statements[0].Action].flat().sort(),
      ['ecr:BatchCheckLayerAvailability', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'], name);
    const principals = [statements[0].Principal.AWS].flat().map((p: unknown) => JSON.stringify(p));
    assert.equal(principals.length, 2, name);
    assert.match(principals[0], /:iam::444455556666:root/);
    assert.match(principals[1], /:iam::777788889999:root/);
  }
});

test('the policy can never grant a push, or anything to anyone', () => {
  // Whatever the pull accounts, every statement allows reads only, to those accounts only.
  for (const accounts of [['444455556666'], ['444455556666', '777788889999', '123456789012']]) {
    for (const [name, repo] of Object.entries(repositories(build(accounts).template))) {
      for (const statement of repo.Properties.RepositoryPolicyText.Statement) {
        const actions: string[] = [statement.Action].flat();
        assert.deepEqual(actions.filter((a) => WRITES.test(a) || a.includes('*')), [], name);
        assert.notEqual(statement.Principal, '*', name);
        assert.equal(JSON.stringify(statement.Principal).includes('"*"'), false, name);
        assert.equal(statement.NotPrincipal, undefined, name);
        assert.equal(statement.NotAction, undefined, name);
      }
    }
  }
});

test('rejects a wildcard or a malformed account', () => {
  for (const accounts of [['*'], ['1'], ['444455556666', '444455556666'], ['444455556666', '*'], ['arn:aws:iam::444455556666:root']]) {
    assert.throws(() => build(accounts), /-c pullAccounts/, JSON.stringify(accounts));
  }
});

test('keeps every image and chart', () => {
  for (const repo of Object.values(repositories(build([]).template))) {
    assert.equal(repo.DeletionPolicy, 'Retain');
    assert.equal(repo.Properties.LifecyclePolicy, undefined);
    assert.equal(repo.Properties.ImageTagMutability, 'IMMUTABLE');
  }
});

test('outputs both repositories', () => {
  const outputs = build([]).template.toJSON().Outputs;
  assert.deepEqual(Object.keys(outputs).sort(), ['ChartRepositoryUri', 'ImageRepositoryUri']);
});

test('passes cdk-nag', () => {
  for (const accounts of [[], ['444455556666']]) {
    const { stack } = build(accounts);
    nagChecks(stack);
    assert.deepEqual(unacknowledged(stack), []);
  }
});
