import assert from 'node:assert/strict';
import test from 'node:test';
import { App, Stack } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { acknowledge, nagChecks } from '../src/nag';
import { unacknowledged } from './nag-helpers';

test('reports an unacknowledged finding', () => {
  const stack = new Stack(new App(), 'Probe');
  new s3.Bucket(stack, 'Plain');
  nagChecks(stack);
  assert.ok(unacknowledged(stack).some((f) => f.includes('AwsSolutions-S10')), 'S10: the bucket allows plain HTTP');
});

test('an acknowledged finding is no longer reported', () => {
  const stack = new Stack(new App(), 'Probe');
  const bucket = new s3.Bucket(stack, 'Plain');
  acknowledge(bucket, { id: 'AwsSolutions-S10', reason: 'a test of acknowledging' });
  nagChecks(stack);
  assert.equal(unacknowledged(stack).some((f) => f.includes('AwsSolutions-S10')), false);
});
