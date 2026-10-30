import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';

/**
 * The warehouse prefix, and nothing else of the bucket, for one of the catalog's roles: the server's
 * (table metadata) and the data role (the ceiling of every per-table credential, each narrowed by its
 * session policy). Both forms grant exactly this.
 */
export function grantWarehouse(role: iam.Role, bucket: s3.IBucket, prefix: string): void {
  role.addToPolicy(new iam.PolicyStatement({
    actions: ['s3:GetBucketLocation'],
    resources: [bucket.bucketArn],
  }));
  role.addToPolicy(new iam.PolicyStatement({
    actions: ['s3:ListBucket', 's3:ListBucketMultipartUploads'],
    resources: [bucket.bucketArn],
    // Under the prefix only: a bare prefix would also list a sibling's keys (warehouse2/...).
    conditions: { StringLike: { 's3:prefix': [`${prefix}/*`] } },
  }));
  role.addToPolicy(new iam.PolicyStatement({
    actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
    resources: [bucket.arnForObjects(`${prefix}/*`)],
  }));
}
