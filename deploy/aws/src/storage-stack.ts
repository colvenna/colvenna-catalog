import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { account, commitSha, envName, securityGroupId, standardRegion, subnetIds, subnetZones, vpcId } from './context';
import { MASTER_SECRET, RDS_DESIGN, acknowledge, acknowledgeLogRetention } from './nag';

export interface CatalogStorageStackProps extends StackProps {
  environment: string;
  sourceSha: string;
  /** Create the warehouse bucket (and its access log bucket); false when the install brings one. */
  createBucket: boolean;
  /** Create the metadata database in these subnets, reachable from this security group only. */
  database?: { vpcId: string; subnetIds: string[]; subnetZones: string[]; clientSecurityGroupId: string };
}

/**
 * What an install may ask the stack to create instead of bringing it: the warehouse bucket and the
 * metadata database (RDS PostgreSQL 16). Both outlive the stack: the buckets are retained, the
 * database is protected from deletion and leaves a final snapshot, so tearing the stack down never
 * takes the catalog's data with it.
 */
export class CatalogStorageStack extends Stack {
  constructor(scope: Construct, id: string, props: CatalogStorageStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    if (!props.createBucket && props.database === undefined) {
      throw new Error('The storage stack has nothing to create: neither the bucket nor the database');
    }
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Environment', env);
    Tags.of(this).add('SourceSha', sha);

    if (props.createBucket) {
      // Who read or wrote which table files, for a year.
      const accessLogs = new s3.Bucket(this, 'AccessLogs', {
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        enforceSSL: true,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        lifecycleRules: [{ expiration: Duration.days(365) }],
        removalPolicy: RemovalPolicy.RETAIN,
      });
      // Not logged itself, which would loop; cdk-nag's S1 passes a bucket that receives another's access logs.
      const warehouse = new s3.Bucket(this, 'Warehouse', {
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        enforceSSL: true,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        lifecycleRules: [{ abortIncompleteMultipartUploadAfter: Duration.days(7) }],
        removalPolicy: RemovalPolicy.RETAIN,
      });
      // Its server access logs go to the access log bucket, delivered under a bucket policy for the
      // logging service: that bucket takes no ACLs (bucket-owner-enforced ownership).
      (warehouse.node.defaultChild as s3.CfnBucket).loggingConfiguration = {
        destinationBucketName: accessLogs.bucketName,
        logFilePrefix: 'warehouse/',
      };
      accessLogs.addToResourcePolicy(new iam.PolicyStatement({
        principals: [new iam.ServicePrincipal('logging.s3.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [accessLogs.arnForObjects('warehouse/*')],
        conditions: {
          ArnLike: { 'aws:SourceArn': warehouse.bucketArn },
          StringEquals: { 'aws:SourceAccount': this.account },
        },
      }));
      new CfnOutput(this, 'BucketName', { value: warehouse.bucketName });
    }

    if (props.database !== undefined) {
      const ids = subnetIds(props.database.subnetIds.join(','));
      const zones = subnetZones(props.database.subnetZones.join(','), region, ids.length);
      const subnets = ids.map((subnetId, i) => {
        const subnet = ec2.Subnet.fromSubnetAttributes(this, `Subnet${i}`, { subnetId, availabilityZone: zones[i] });
        Annotations.of(subnet).acknowledgeWarning('@aws-cdk/aws-ec2:noSubnetRouteTableId',
          'Only the database subnet group uses the subnet; nothing reads its route table');
        return subnet;
      });
      const vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', {
        vpcId: vpcId(props.database.vpcId),
        availabilityZones: [...new Set(zones)],
      });
      const securityGroup = new ec2.SecurityGroup(this, 'DatabaseSecurityGroup', {
        vpc,
        description: 'colvenna-catalog metadata database: PostgreSQL from the cluster only',
        allowAllOutbound: false,
      });
      securityGroup.addIngressRule(
        ec2.Peer.securityGroupId(securityGroupId(props.database.clientSecurityGroupId)),
        ec2.Port.tcp(5432),
        'PostgreSQL from the cluster',
      );
      const engine = rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_16 });
      const instance = new rds.DatabaseInstance(this, 'Database', {
        engine,
        instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.SMALL),
        vpc,
        vpcSubnets: { subnets },
        securityGroups: [securityGroup],
        credentials: rds.Credentials.fromGeneratedSecret('colvenna_admin', { secretName: `colvenna-catalog-${env}/db-admin` }),
        parameterGroup: new rds.ParameterGroup(this, 'Parameters', { engine, parameters: { 'rds.force_ssl': '1' } }),
        allocatedStorage: 20,
        storageType: rds.StorageType.GP3,
        storageEncrypted: true,
        publiclyAccessible: false,
        multiAz: false,
        backupRetention: Duration.days(7),
        // The serverless form signs in with RDS IAM tokens; password sign-in (the EKS form's) still works.
        iamAuthentication: true,
        deletionProtection: true,
        removalPolicy: RemovalPolicy.SNAPSHOT,
        cloudwatchLogsExports: ['postgresql'],
        cloudwatchLogsRetention: logs.RetentionDays.ONE_YEAR,
      });
      acknowledge(instance, ...RDS_DESIGN, MASTER_SECRET);
      acknowledgeLogRetention(this);
      new CfnOutput(this, 'DbEndpoint', { value: instance.dbInstanceEndpointAddress });
      new CfnOutput(this, 'DbPort', { value: instance.dbInstanceEndpointPort });
      new CfnOutput(this, 'DbAdminSecretArn', { value: instance.secret!.secretArn });
      // What an IAM policy names the instance by: arn:aws:rds-db:<region>:<account>:dbuser:<id>/<user>.
      new CfnOutput(this, 'DbResourceId', { value: instance.instanceResourceId! });
    }
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
