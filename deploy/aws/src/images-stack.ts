import { CfnOutput, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { account, commitSha, envName, standardRegion } from './context';

export interface CatalogImagesStackProps extends StackProps {
  environment: string;
  sourceSha: string;
}

/**
 * The serverless form's image repository, colvenna-catalog-<env>/colvenna-catalog: the catalog's image
 * as install.sh builds it, or a release's, copied there by digest (a Lambda function takes its image
 * from an ECR repository of its own account and Region). It is a stack of its own because the image has
 * to be in it before the serverless stack's functions can be made. Lambda may pull from it for this
 * environment's functions only; it goes, with its images, when the catalog is uninstalled.
 */
export class CatalogImagesStack extends Stack {
  constructor(scope: Construct, id: string, props: CatalogImagesStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    const accountId = account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Environment', env);
    Tags.of(this).add('SourceSha', sha);

    const repository = new ecr.Repository(this, 'Images', {
      repositoryName: `colvenna-catalog-${env}/colvenna-catalog`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      lifecycleRules: [{ maxImageCount: 10 }],
      removalPolicy: RemovalPolicy.DESTROY,
      emptyOnDelete: true,
    });
    repository.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'LambdaPullForThisEnvironmentsFunctions',
      principals: [new iam.ServicePrincipal('lambda.amazonaws.com')],
      actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
      conditions: {
        StringLike: { 'aws:sourceArn': `arn:${this.partition}:lambda:${region}:${accountId}:function:colvenna-catalog-${env}-*` },
      },
    }));

    new CfnOutput(this, 'RepositoryUri', { value: repository.repositoryUri });
    new CfnOutput(this, 'RepositoryName', { value: repository.repositoryName });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
