import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import {
  account, bucketName, commitSha, databaseIdentifier, envName, k8sName, oidcIssuer, standardRegion, warehousePrefix,
} from './context';
import { SECRET_ROTATION, acknowledge, objectsUnder } from './nag';
import { grantWarehouse } from './warehouse';

export interface CatalogEksStackProps extends StackProps {
  /** The environment's name: the stack's resources are colvenna-catalog-<environment>-... */
  environment: string;
  sourceSha: string;
  /** The EKS cluster's OIDC issuer; its IAM OIDC provider must exist already. */
  oidcIssuer: string;
  /** Where the catalog runs, and as which service account. */
  namespace: string;
  serviceAccount: string;
  /** The catalog's own PostgreSQL user, whose password the stack generates. */
  databaseUser: string;
  /** The warehouse: an existing bucket, and the key prefix the catalog owns in it. */
  bucketName: string;
  warehousePrefix: string;
}

/**
 * What the EKS form needs in AWS besides the cluster, the bucket and the database:
 * - the server role, the catalog pods' IRSA identity: table metadata under the warehouse prefix only;
 * - the data role, which the catalog assumes with a per-table session policy to vend table
 *   credentials. Its sessions may last 12 hours: the server assumes it straight from its web identity
 *   token, not by role chaining (capped at one hour), as Paimon clients refresh credentials with less
 *   than an hour left;
 * - the catalog's database user secret, and the environment's image repository.
 * Both roles trust the catalog's service account alone, through the cluster's OIDC provider.
 */
export class CatalogEksStack extends Stack {
  constructor(scope: Construct, id: string, props: CatalogEksStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    const accountId = account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    const issuer = oidcIssuer(props.oidcIssuer, region);
    const namespace = k8sName(props.namespace, 'namespace');
    const serviceAccount = k8sName(props.serviceAccount, 'serviceAccount');
    const databaseUser = databaseIdentifier(props.databaseUser);
    const bucket = s3.Bucket.fromBucketName(this, 'Warehouse', bucketName(props.bucketName));
    const prefix = warehousePrefix(props.warehousePrefix);
    const named = (name: string) => `colvenna-catalog-${env}${name}`;

    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Environment', env);
    Tags.of(this).add('SourceSha', sha);

    const issuerPath = issuer.slice('https://'.length);
    const provider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(this, 'EksOidcProvider',
      `arn:${this.partition}:iam::${accountId}:oidc-provider/${issuerPath}`);
    const catalogPods = () => new iam.OpenIdConnectPrincipal(provider, {
      StringEquals: {
        [`${issuerPath}:aud`]: 'sts.amazonaws.com',
        [`${issuerPath}:sub`]: `system:serviceaccount:${namespace}:${serviceAccount}`,
      },
    });

    const server = new iam.Role(this, 'ServerRole', {
      roleName: named('-server'),
      description: 'colvenna-catalog pods: table metadata under the warehouse prefix',
      assumedBy: catalogPods(),
    });
    grantWarehouse(server, bucket, prefix);
    acknowledge(server, {
      id: 'AwsSolutions-IAM5',
      reason: 'Table metadata: the objects under the warehouse prefix only',
      appliesTo: [objectsUnder(bucket.bucketName, prefix)],
    });

    const data = new iam.Role(this, 'DataRole', {
      roleName: named('-data'),
      description: 'Assumed by colvenna-catalog with a per-table session policy to vend table credentials',
      assumedBy: catalogPods(),
      maxSessionDuration: Duration.hours(12),
    });
    grantWarehouse(data, bucket, prefix);
    acknowledge(data, {
      id: 'AwsSolutions-IAM5',
      reason: 'The ceiling of the per-table credentials, each narrowed by its session policy: '
        + 'the objects under the warehouse prefix only',
      appliesTo: [objectsUnder(bucket.bucketName, prefix)],
    });

    const databaseSecret = new secretsmanager.Secret(this, 'DbUserSecret', {
      secretName: `${named('')}/db-user`,
      description: `PostgreSQL user ${databaseUser} of the colvenna-catalog metadata database`,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: databaseUser }),
        generateStringKey: 'password',
        excludePunctuation: true,
        passwordLength: 40,
      },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    acknowledge(databaseSecret, SECRET_ROTATION);

    const repository = new ecr.Repository(this, 'Images', {
      repositoryName: `${named('')}/colvenna-catalog`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      lifecycleRules: [{ maxImageCount: 10 }],
      removalPolicy: RemovalPolicy.DESTROY,
      emptyOnDelete: true,
    });

    new CfnOutput(this, 'ServerRoleArn', { value: server.roleArn });
    new CfnOutput(this, 'DataRoleArn', { value: data.roleArn });
    new CfnOutput(this, 'DbUserSecretArn', { value: databaseSecret.secretArn });
    new CfnOutput(this, 'RepositoryUri', { value: repository.repositoryUri });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
