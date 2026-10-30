import {
  Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags,
  aws_ec2 as ec2, aws_ecr as ecr, aws_iam as iam, aws_lambda as lambda, aws_logs as logs,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { account, commitSha, envName, standardRegion, subnetIds, subnetZones, vpcId } from '../src/context';
import { acknowledge } from '../src/nag';

export interface DriverStackProps extends StackProps {
  /** The serverless install's environment: the driver is colvenna-catalog-<environment>-test-driver. */
  environment: string;
  sourceSha: string;
  /** The catalog's VPC and private subnets (the network stack's outputs), with their zones. */
  vpcId: string;
  subnetIds: string[];
  subnetZones: string[];
  /** The environment's image repository (colvenna-catalog-<environment>/...) and the driver image's digest. */
  repositoryName: string;
  imageDigest: string;
  /**
   * The catalog's API port, when the driver is the client of an EKS install's internal NLB (UAT-DEP-11,
   * #52): egress to it besides HTTPS. Absent, HTTPS out only.
   */
  apiPort?: number;
  /**
   * For the UAT harness's sql action (#52): the catalog functions' security group, which the
   * database admits, and the database's resource ID and user that the driver may sign in as with
   * RDS IAM authentication. Without it the driver reaches no database.
   */
  databaseAccess?: { securityGroupId: string; dbResourceId: string; dbUser: string };
}

/**
 * The serverless form's live-test driver (#13; tests/serverless/run.sh deploys it): one Lambda
 * function inside the catalog's VPC, from the driver image (tests/serverless/driver), invoked by its
 * operator with IAM-signed Invoke calls only. It has no Function URL, no API in front and no
 * resource-based policy. Its role may write its logs and attach to the VPC, nothing else: no S3, so
 * that every object it reads or writes goes through the per-table credentials the catalog vends.
 * With databaseAccess (the UAT harness's sql action), it also joins the catalog functions' security
 * group and may sign in to the database as the catalog's user with RDS IAM authentication.
 */
export class DriverStack extends Stack {
  constructor(scope: Construct, id: string, props: DriverStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    const ids = subnetIds(props.subnetIds.join(','));
    const zones = subnetZones(props.subnetZones.join(','), region, ids.length);
    if (!new RegExp(`^colvenna-catalog-${env}/[a-z0-9._/-]+$`).test(props.repositoryName)) {
      throw new Error(`repositoryName: a repository of colvenna-catalog-${env}/, not ${JSON.stringify(props.repositoryName)}`);
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(props.imageDigest)) {
      throw new Error(`imageDigest: sha256:<64 hex digits>, not ${JSON.stringify(props.imageDigest)}`);
    }
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Purpose', 'test-environment');
    Tags.of(this).add('Environment', `test-${env}`);
    Tags.of(this).add('SourceSha', sha);

    const vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', { vpcId: vpcId(props.vpcId), availabilityZones: [...new Set(zones)] });
    const subnets = ids.map((subnetId, i) => {
      const subnet = ec2.Subnet.fromSubnetAttributes(this, `Subnet${i}`, { subnetId, availabilityZone: zones[i] });
      Annotations.of(subnet).acknowledgeWarning('@aws-cdk/aws-ec2:noSubnetRouteTableId',
        'Only the function attaches to the subnet; nothing reads its route table');
      return subnet;
    });
    const group = new ec2.SecurityGroup(this, 'DriverSecurityGroup', {
      vpc,
      description: 'colvenna-catalog live-test driver: HTTPS out only (the API, STS and S3 endpoints)',
      allowAllOutbound: false,
    });
    group.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS to the VPC endpoints and the S3 gateway endpoint');
    if (props.apiPort !== undefined) {
      if (!Number.isInteger(props.apiPort) || props.apiPort < 1 || props.apiPort > 65535 || props.apiPort === 443) {
        throw new Error(`apiPort: a TCP port other than 443, not ${JSON.stringify(props.apiPort)}`);
      }
      group.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(props.apiPort), 'The catalog API port, through its internal NLB');
    }

    const name = `colvenna-catalog-${env}-test-driver`;
    const role = new iam.Role(this, 'DriverRole', {
      roleName: name,
      description: 'colvenna-catalog live-test driver: its logs and its VPC network interfaces only',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole')],
    });
    acknowledge(role, {
      id: 'AwsSolutions-IAM4',
      reason: 'The AWS managed policy Lambda needs to write logs and attach to the VPC; test environment only',
      appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'],
    });
    const logGroup = new logs.LogGroup(this, 'DriverLogs', {
      logGroupName: `/aws/lambda/${name}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const repository = ecr.Repository.fromRepositoryAttributes(this, 'Images', {
      repositoryName: props.repositoryName,
      repositoryArn: `arn:${this.partition}:ecr:${region}:${this.account}:repository/${props.repositoryName}`,
    });
    const groups: ec2.ISecurityGroup[] = [group];
    const access = props.databaseAccess;
    if (access) {
      if (!/^sg-[0-9a-f]{8,17}$/.test(access.securityGroupId)) {
        throw new Error(`databaseAccess.securityGroupId: sg-<hex>, not ${JSON.stringify(access.securityGroupId)}`);
      }
      if (!/^db-[A-Z0-9]{10,40}$/.test(access.dbResourceId) || !/^[a-z_][a-z0-9_]{0,62}$/.test(access.dbUser)) {
        throw new Error(`databaseAccess: a db-<id> resource ID and a database user, not ${JSON.stringify(access)}`);
      }
      // The functions' group: the database admits its port from it, and it lets the port out.
      groups.push(ec2.SecurityGroup.fromSecurityGroupId(this, 'CatalogSecurityGroup', access.securityGroupId, {
        mutable: false,
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['rds-db:connect'],
        resources: [`arn:${this.partition}:rds-db:${region}:${this.account}:dbuser:${access.dbResourceId}/${access.dbUser}`],
      }));
    }
    const driver = new lambda.DockerImageFunction(this, 'Driver', {
      functionName: name,
      description: 'colvenna-catalog serverless live test: the Paimon 2.0.0 client inside the catalog VPC',
      code: lambda.DockerImageCode.fromEcr(repository, { tagOrDigest: props.imageDigest }),
      architecture: lambda.Architecture.X86_64,
      memorySize: 2048,
      timeout: Duration.minutes(15),
      role,
      logGroup,
      vpc,
      vpcSubnets: { subnets },
      securityGroups: groups,
      // The adapter in the server image is parked (nothing listens on its readiness port); quiet.
      environment: { RUST_LOG: 'warn' },
    });

    new CfnOutput(this, 'DriverFunctionName', { value: driver.functionName });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
