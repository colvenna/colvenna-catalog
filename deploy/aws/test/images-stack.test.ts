import assert from 'node:assert/strict';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { CatalogImagesStack } from '../src/images-stack';
import { nagChecks } from '../src/nag';
import { unacknowledged } from './nag-helpers';

function build() {
  const stack = new CatalogImagesStack(new App(), 'ColvennaCatalogUatImages', {
    env: { account: '111122223333', region: 'us-west-2' }, environment: 'uat', sourceSha: 'a'.repeat(40),
  });
  return { stack, template: Template.fromStack(stack) };
}

test("the environment's repository: immutable tags, scanned, removed with its images", () => {
  const { template } = build();
  const [repository] = Object.values(template.findResources('AWS::ECR::Repository')) as any[];
  assert.equal(repository.Properties.RepositoryName, 'colvenna-catalog-uat/colvenna-catalog');
  assert.equal(repository.Properties.ImageTagMutability, 'IMMUTABLE');
  assert.equal(repository.Properties.ImageScanningConfiguration.ScanOnPush, true);
  assert.equal(repository.Properties.EmptyOnDelete, true);
  assert.equal(repository.DeletionPolicy, 'Delete');
});

test("only Lambda may pull, for this environment's functions, and nothing may push", () => {
  const { template } = build();
  const [repository] = Object.values(template.findResources('AWS::ECR::Repository')) as any[];
  const statements = repository.Properties.RepositoryPolicyText.Statement;
  assert.equal(statements.length, 1);
  assert.deepEqual(statements[0].Principal, { Service: 'lambda.amazonaws.com' });
  assert.deepEqual([statements[0].Action].flat().sort(), ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer']);
  assert.match(JSON.stringify(statements[0].Condition),
    /"StringLike":\{"aws:sourceArn":\{"Fn::Join":\["",\["arn:",\{"Ref":"AWS::Partition"\},":lambda:us-west-2:111122223333:function:colvenna-catalog-uat-\*"\]\]\}\}/);
});

test('passes cdk-nag', () => {
  const { stack } = build();
  nagChecks(stack);
  assert.deepEqual(unacknowledged(stack), []);
});
