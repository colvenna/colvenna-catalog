import { Aspects, Stack } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagPackSuppression, NagSuppressions } from 'cdk-nag';
import { IConstruct } from 'constructs';

/**
 * cdk-nag's AWS Solutions rules on everything under the scope: app.ts applies them to the stack it
 * synthesizes, the tests to every stack they build. A finding not acknowledged, with its reason,
 * beside the resource it is about fails the synthesis, so the deploy too.
 */
export function nagChecks(scope: IConstruct): void {
  Aspects.of(scope).add(new AwsSolutionsChecks());
}

/** Acknowledges findings on a construct and everything under it, each with its reason. */
export function acknowledge(construct: IConstruct, ...findings: NagPackSuppression[]): void {
  NagSuppressions.addResourceSuppressions(construct, findings, true);
}

/** A literal string as it reads inside a regular expression. */
const literal = (text: string) => text.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

/** The IAM5 finding of a role reaching the objects under one prefix of the bucket, and no more. */
export function objectsUnder(bucketName: string, prefix: string): { regex: string } {
  return { regex: `/^Resource::arn:<AWS::Partition>:s3:::${literal(bucketName)}\\/${literal(prefix)}\\/\\*$/g` };
}

/**
 * What CDK makes itself to set log groups' retention (RDS's cloudwatchLogsRetention): one Lambda
 * function per stack, with the AWS managed basic execution role and logs:PutRetentionPolicy on any
 * log group. It has no HTTP endpoint of any kind.
 */
export function acknowledgeLogRetention(stack: Stack): void {
  const singleton = stack.node.children.find((c) => c.node.id.startsWith('LogRetention'));
  if (!singleton) {
    return;
  }
  acknowledge(singleton,
    {
      id: 'AwsSolutions-IAM4',
      reason: 'CDK\'s log retention function: the AWS managed Lambda basic execution role',
      appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
    },
    {
      id: 'AwsSolutions-IAM5',
      reason: 'CDK\'s log retention function sets the retention of the log groups RDS (and EKS) create, named only at run time',
      appliesTo: ['Resource::*'],
    });
}

/** Choices the design makes for the metadata database the storage stack creates. */
export const RDS_DESIGN: NagPackSuppression[] = [
  {
    id: 'AwsSolutions-RDS3',
    reason: 'Single-AZ by default: the catalog is stateless and the database is restored from its backups '
      + '(the user manual\'s backup and restore); a deployment that needs Multi-AZ brings its own database',
  },
  {
    id: 'AwsSolutions-RDS11',
    reason: 'The default port: the security group admits only the catalog\'s clients, so another port would hide nothing',
  },
];

/** Accepted: the database's master secret has no automatic rotation (SMG4). */
export const MASTER_SECRET: NagPackSuppression = {
  id: 'AwsSolutions-SMG4',
  reason: 'Accepted risk (#49): install.sh uses the master user only to create the catalog\'s own database user; '
    + 'the catalog never reads it. Rotated by hand: docs/manual/database-secrets.md',
};

/** Accepted: the catalog's database user secret has no automatic rotation (SMG4). */
export const SECRET_ROTATION: NagPackSuppression = {
  id: 'AwsSolutions-SMG4',
  reason: 'Accepted risk (#49): install.sh copies it into the Kubernetes Secret the catalog reads at start. '
    + 'Rotated by hand, not by Secrets Manager: a new value in the secret, install.sh run again, the pods restarted '
    + '(docs/manual/database-secrets.md)',
};

/** The AWS managed policies EKS requires of the cluster, node group and CDK's kubectl handler roles. */
export const EKS_MANAGED_POLICIES: NagPackSuppression = {
  id: 'AwsSolutions-IAM4',
  reason: 'The AWS managed policies EKS requires of the cluster, node group and kubectl handler roles',
  appliesTo: [
    'Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSClusterPolicy',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSWorkerNodePolicy',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKS_CNI_Policy',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
    'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
    { regex: '/AmazonElasticContainerRegistryPublicReadOnly/' },
  ],
};

/** What CDK's EKS providers (cluster, kubectl and OIDC handlers, their waiter) need acknowledged. */
export const EKS_PROVIDERS: NagPackSuppression[] = [
  EKS_MANAGED_POLICIES,
  {
    id: 'AwsSolutions-IAM5',
    reason: 'CDK\'s EKS providers invoke their own functions, any version of them',
    appliesTo: [{ regex: '/^Resource::<[A-Za-z0-9]+\\.Arn>:\\*$/g' }],
  },
  {
    id: 'AwsSolutions-L1',
    reason: 'CDK\'s kubectl handler: its runtime goes with the CDK release and the kubectl layer',
  },
  { id: 'AwsSolutions-SF1', reason: 'CDK\'s cluster provider waiter only polls the cluster\'s creation' },
  { id: 'AwsSolutions-SF2', reason: 'CDK\'s cluster provider waiter only polls the cluster\'s creation' },
];
