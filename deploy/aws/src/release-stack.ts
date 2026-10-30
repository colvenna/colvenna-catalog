import { CfnOutput, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { account, accountList, commitSha, standardRegion } from './context';

/** The release registry's repositories are all under this prefix (scripts/release/manifest.py's PREFIX). */
export const RELEASE_PREFIX = 'colvenna-catalog-release';
/**
 * The release's repositories: the server's image, tagged with the candidate (v1.0.0-rc.1), and its
 * Helm chart as an OCI artifact, tagged with the chart's version (1.0.0-rc.1). `helm push <chart>
 * oci://<registry>/colvenna-catalog-release/charts` puts the chart colvenna-catalog in the second.
 */
export const RELEASE_REPOSITORIES = {
  image: `${RELEASE_PREFIX}/colvenna-catalog`,
  chart: `${RELEASE_PREFIX}/charts/colvenna-catalog`,
} as const;
/** What an install of another account does with a release: read its image and chart by digest. */
export const PULL_ACTIONS = ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'];

export interface CatalogReleaseStackProps extends StackProps {
  sourceSha: string;
  /**
   * Accounts whose installs may pull the release's image and chart: each repository's policy lets
   * their IAM principals pull, nothing more, and never anyone. None: no policy, only this account by IAM.
   */
  pullAccounts: string[];
}

/**
 * The release registry, in the maintainer's account: every release candidate's image and chart are
 * pushed here (scripts/release/make-manifest.sh) and installed from here (install.sh --release copies
 * the image by digest into the environment's own repository and installs the chart by digest). Tags
 * never move and nothing is removed, with the stack or otherwise; the environment name release is
 * reserved, so no environment's resources can be named like it.
 */
export class CatalogReleaseStack extends Stack {
  constructor(scope: Construct, id: string, props: CatalogReleaseStackProps) {
    super(scope, id, props);
    const accountId = account(props.env?.account);
    const region = standardRegion(props.env?.region);
    const sha = commitSha(props.sourceSha);
    const pullers = accountList(props.pullAccounts.join(','));
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Purpose', 'release');
    Tags.of(this).add('SourceSha', sha);

    const repository = (id: string, output: string, name: string, description: string, scanOnPush: boolean) => {
      const made = new ecr.Repository(this, id, {
        repositoryName: name,
        // Basic scanning reads container images; a chart is not one.
        imageScanOnPush: scanOnPush,
        imageTagMutability: ecr.TagMutability.IMMUTABLE,
        removalPolicy: RemovalPolicy.RETAIN,
      });
      if (pullers.length > 0) {
        made.addToResourcePolicy(new iam.PolicyStatement({
          sid: 'ReleasePull',
          principals: pullers.map((p) => new iam.AccountPrincipal(p)),
          actions: PULL_ACTIONS,
        }));
      }
      new CfnOutput(this, output, {
        description,
        value: `${accountId}.dkr.ecr.${region}.amazonaws.com/${name}`,
      });
    };
    // The image repository keeps the logical ID it was first given (Images).
    repository('Images', 'ImageRepositoryUri', RELEASE_REPOSITORIES.image, 'The server\'s image, tagged with each candidate',
      true);
    repository('Chart', 'ChartRepositoryUri', RELEASE_REPOSITORIES.chart,
      'The Helm chart (OCI), tagged with each candidate\'s chart version', false);
  }
}
