import { Stack } from 'aws-cdk-lib';
import { Annotations, Match } from 'aws-cdk-lib/assertions';

/** Every cdk-nag finding on the stack that nothing acknowledges: its errors and its warnings. */
export function unacknowledged(stack: Stack): string[] {
  const annotations = Annotations.fromStack(stack);
  const nag = Match.stringLikeRegexp('AwsSolutions-');
  return [...annotations.findError('*', nag), ...annotations.findWarning('*', nag)]
    .map((m) => `${m.id}: ${String(m.entry.data)}`);
}
