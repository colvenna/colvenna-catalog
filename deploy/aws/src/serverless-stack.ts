import * as path from 'node:path';
import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as nodejs from 'aws-cdk-lib/aws-lambda-nodejs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import {
  account, bucketName, commitSha, databaseResourceId, dataTokenDuration, dataTokenMode, envName, hostName, identifier, imageUri, integer,
  securityGroupId, standardRegion, subnetIds, subnetZones, vpcEndpointIds, vpcId, warehouseName, warehousePrefix,
} from './context';
import { acknowledge, objectsUnder } from './nag';
import { grantWarehouse } from './warehouse';

/** The private API's one stage: clients' catalog URI is https://<api>.execute-api.<region>.amazonaws.com/catalog. */
export const STAGE = 'catalog';
/** API Gateway's integration timeout: the server's waits add up to less (the LLD's "无服务器形态"). */
const GATEWAY_TIMEOUT = Duration.seconds(29);
/** The main pool's lock wait: with the connection wait and one S3 request, under the gateway's timeout. */
export const LOCK_WAIT_SECONDS = 3;
/** One S3 request through S3A at most: S3A 3.4's floor for its timeouts. */
export const S3A_REQUEST_SECONDS = 15;
/** Longer than the gateway's, so a commit the gateway gave up on still finishes, once (#12). */
const SERVER_TIMEOUT = Duration.seconds(60);
/** How often the purge function deletes the files of dropped tables (#69), and how long it may take. */
export const PURGE_INTERVAL = Duration.minutes(15);
/** How long a dropped table's files may wait, or the purge function stay silent, before its backlog alarm. */
export const PURGE_BACKLOG = Duration.hours(1);
/** The namespace of the purge function's metrics (Main.METRICS_NAMESPACE). */
export const PURGE_METRICS_NAMESPACE = 'ColvennaCatalog';
const PURGE_TIMEOUT = Duration.minutes(15);
/**
 * The mean duration of a request the stage's rate limit is sized for (#80): 112 ms measured under UAT-FLT-02's
 * twenty writers, rounded up (the LLD's "无服务器形态").
 */
export const API_REQUEST_SECONDS = 0.125;

/**
 * The stage's throttle for a server of `concurrency` reserved concurrency (#80): a token bucket of
 * `concurrency` tokens, refilled at `concurrency / API_REQUEST_SECONDS` a second. By Little's law the mean
 * concurrency is the request rate times the mean duration, so that rate is what `concurrency` execution
 * environments serve at the sized-for mean, and the bucket lets no more requests start at one instant than
 * there are environments. A ceiling, not a guarantee: slower requests still take more than `concurrency`,
 * and Lambda refuses those (answered 503). Past the ceiling the gateway answers 429 itself.
 */
export function apiThrottle(concurrency: number): { rateLimit: number; burstLimit: number } {
  return { rateLimit: Math.floor(concurrency / API_REQUEST_SECONDS), burstLimit: concurrency };
}
/** Where the image keeps the RDS CA bundle (Dockerfile). */
const RDS_CA = '/opt/colvenna-catalog/rds-global-bundle.pem';
/** The CA bundle of the Lambda Node.js runtime, RDS's CAs among them. */
const NODE_RDS_CA = '/var/runtime/ca-cert.pem';

export interface CatalogServerlessStackProps extends StackProps {
  environment: string;
  sourceSha: string;
  /** The catalog's network: the network stack's VPC, private subnets and security group. */
  vpcId: string;
  subnetIds: string[];
  subnetZones: string[];
  securityGroupId: string;
  /**
   * The execute-api VPC endpoints clients reach the API through, the one in the catalog's VPC first:
   * the API takes requests through these alone, and the ops function calls it through the first.
   */
  apiEndpointIds: string[];
  /** The server's image: the images stack's repository, by digest. */
  imageUri: string;
  /**
   * The maintenance function's image, the server's unless given. An upgrade gives the new image here
   * first, while the server keeps the old one, so that install.sh migrates the database before any new
   * server runs on it (the LLD's "连接池").
   */
  maintenanceImageUri?: string;
  bucketName: string;
  warehousePrefix: string;
  warehouseName: string;
  /** The metadata database; its resource ID names it in the functions' rds-db:connect permissions. */
  database: { endpoint: string; port: number; name: string; user: string; resourceId: string };
  dataTokens: { mode: 'aws-sts' | 'disabled'; duration: string };
  /** The server function's memory (MB). */
  memoryMb: number;
  /**
   * The connection budget: at most this many requests run at once, so at most five times as many
   * database connections are in use (each execution environment has pools of 2 + 1 + 1 + 1).
   */
  reservedConcurrency: number;
  /** Lambda SnapStart on the server's published versions; billed for container images (see the LLD). */
  snapStart: boolean;
}

/**
 * The serverless form (#13; v1.0 design 6.2): the catalog's own image on Lambda, behind a private REST
 * API that nothing outside the given VPC endpoints can reach and that refuses every request without a
 * valid bearer token before the server sees it.
 * - The server function runs the image under the Lambda Web Adapter, plain HTTP on loopback only, with
 *   pools sized for one request per execution environment and reserved concurrency as the connection
 *   budget. It is invoked by the API alone, through the alias "live".
 * - A REQUEST authorizer checks the token as the server does; the server checks it again.
 * - Per-table credentials: the server gets an OpenID token for itself from a Cognito identity pool with
 *   a developer provider only (no unauthenticated identities, no roles), and assumes the data role with
 *   it and a per-table session policy: no role chaining, so sessions of up to 12 hours.
 * - The maintenance function (the same image: migrate, bootstrap-admin) and the ops function (database
 *   users, grants, requests through the API from inside the VPC) are what install.sh invokes.
 * - A drop answers once the table is gone from the catalog; the purge function (the same image again)
 *   deletes the files of dropped tables the database records, every 15 minutes on an EventBridge
 *   Scheduler schedule, through the schedule's own IAM role; an alarm watches its errors (#69).
 * Nothing here has a Function URL, a public endpoint or an unauthenticated route, and only the API's
 * functions have a resource policy (for API Gateway alone); the guard (src/guard.ts) refuses any of them
 * at synthesis.
 */
export class CatalogServerlessStack extends Stack {
  private readonly zones: string[];

  /** The given zones, never the context lookup Stack would make. */
  override get availabilityZones(): string[] {
    return this.zones;
  }

  constructor(scope: Construct, id: string, props: CatalogServerlessStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    const accountId = account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    const ids = subnetIds(props.subnetIds.join(','));
    const zones = subnetZones(props.subnetZones.join(','), region, ids.length);
    this.zones = [...new Set(zones)];
    const endpointIds = vpcEndpointIds(props.apiEndpointIds.join(','), 'apiEndpointIds');
    const image = imageUri(props.imageUri, accountId, region);
    const maintenanceImage = imageUri(props.maintenanceImageUri ?? props.imageUri, accountId, region);
    if (maintenanceImage.repository !== image.repository) {
      throw new Error('-c maintenanceImageUri: in the server image\'s repository');
    }
    const bucket = s3.Bucket.fromBucketName(this, 'Warehouse', bucketName(props.bucketName));
    const prefix = warehousePrefix(props.warehousePrefix);
    const warehouse = warehouseName(props.warehouseName);
    const db = {
      endpoint: hostName(props.database.endpoint, 'databaseEndpoint'),
      port: integer(String(props.database.port), 'databasePort', 1, 65535),
      name: identifier(props.database.name, 'databaseName'),
      user: identifier(props.database.user, 'databaseUser'),
      resourceId: databaseResourceId(props.database.resourceId),
    };
    const authorizerUser = identifier(`${db.user}_authz`, 'databaseUser');
    const tokens = { mode: dataTokenMode(props.dataTokens.mode), duration: dataTokenDuration(props.dataTokens.duration) };
    const memory = integer(String(props.memoryMb), 'memory', 1024, 10240);
    const concurrency = integer(String(props.reservedConcurrency), 'concurrency', 1, 1000);
    const named = (name: string) => `colvenna-catalog-${env}${name}`;
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Environment', env);
    Tags.of(this).add('SourceSha', sha);

    // --- network: the network stack's, as given --------------------------------------------------
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', { vpcId: vpcId(props.vpcId), availabilityZones: this.zones });
    const subnets = ids.map((subnetId, i) => {
      const subnet = ec2.Subnet.fromSubnetAttributes(this, `Subnet${i}`, { subnetId, availabilityZone: zones[i] });
      Annotations.of(subnet).acknowledgeWarning('@aws-cdk/aws-ec2:noSubnetRouteTableId',
        'Only the functions\' network configuration uses the subnet; nothing reads its route table');
      return subnet;
    });
    const securityGroup = ec2.SecurityGroup.fromSecurityGroupId(this, 'CatalogSecurityGroup',
      securityGroupId(props.securityGroupId), { mutable: false });
    const inVpc = { vpc, vpcSubnets: { subnets }, securityGroups: [securityGroup] };

    // --- secrets: the admin's token only; the functions sign in to the database with IAM tokens ----------
    const adminToken = new secretsmanager.Secret(this, 'AdminTokenSecret', {
      secretName: `${named('')}/admin-token`,
      description: 'The colvenna-catalog admin\'s bearer token; install.sh writes it (until then, a placeholder that is no token)',
      generateSecretString: { excludePunctuation: true, passwordLength: 32 },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    acknowledge(adminToken, {
      id: 'AwsSolutions-SMG4',
      reason: 'The token is the catalog\'s own (Secrets Manager cannot make one): install.sh issues a new one, '
        + 'with bootstrap-admin, when this one has less than a month left',
    });

    // --- per-table credentials -------------------------------------------------------------------------
    const developerProvider = `colvenna-catalog-${env}.catalog`;
    const pool = new cognito.CfnIdentityPool(this, 'IdentityPool', {
      identityPoolName: `colvenna_catalog_${env}`,
      allowUnauthenticatedIdentities: false,
      allowClassicFlow: false,
      developerProviderName: developerProvider,
    });
    const poolArn = `arn:${this.partition}:cognito-identity:${region}:${accountId}:identitypool/${pool.ref}`;
    const data = new iam.Role(this, 'DataRole', {
      roleName: named('-data'),
      description: 'Assumed by colvenna-catalog with a per-table session policy to vend table credentials',
      assumedBy: new iam.FederatedPrincipal('cognito-identity.amazonaws.com', {
        StringEquals: { 'cognito-identity.amazonaws.com:aud': pool.ref },
        'ForAnyValue:StringLike': { 'cognito-identity.amazonaws.com:amr': 'authenticated' },
      }, 'sts:AssumeRoleWithWebIdentity'),
      maxSessionDuration: Duration.hours(12),
    });
    grantWarehouse(data, bucket, prefix);
    acknowledge(data, {
      id: 'AwsSolutions-IAM5',
      reason: 'The ceiling of the per-table credentials, each narrowed by its session policy: '
        + 'the objects under the warehouse prefix only',
      appliesTo: [objectsUnder(bucket.bucketName, prefix)],
    });

    // --- the functions' roles: each its own, the server's alone with S3 and the identity pool ------------
    const functionRole = (roleId: string, name: string, description: string) => {
      const role = new iam.Role(this, roleId, {
        roleName: named(name),
        description,
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole')],
      });
      acknowledge(role, {
        id: 'AwsSolutions-IAM4',
        reason: 'The AWS managed policy a function in a VPC needs: its network interfaces and its logs',
        appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'],
      });
      return role;
    };
    const serverRole = functionRole('ServerRole', '-server', 'colvenna-catalog server function: table metadata, and the identity pool');
    grantWarehouse(serverRole, bucket, prefix);
    serverRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cognito-identity:GetOpenIdTokenForDeveloperIdentity'],
      resources: [poolArn],
    }));
    acknowledge(serverRole, {
      id: 'AwsSolutions-IAM5',
      reason: 'Table metadata: the objects under the warehouse prefix only',
      appliesTo: [objectsUnder(bucket.bucketName, prefix)],
    });
    const maintenanceRole = functionRole('MaintenanceRole', '-maintenance', 'colvenna-catalog maintenance function: the database only');
    const purgeRole = functionRole('PurgeRole', '-purge', 'colvenna-catalog purge function: the database, and the warehouse\'s dropped tables');
    grantWarehouse(purgeRole, bucket, prefix);
    acknowledge(purgeRole, {
      id: 'AwsSolutions-IAM5',
      reason: 'The files of dropped tables: the objects under the warehouse prefix only',
      appliesTo: [objectsUnder(bucket.bucketName, prefix)],
    });
    const authorizerRole = functionRole('AuthorizerRole', '-authorizer', 'colvenna-catalog authorizer: the database only');
    const opsRole = functionRole('OpsRole', '-ops', 'colvenna-catalog ops function: the database and the private API');
    // RDS IAM authentication: each role may sign in as its one database user, on this database alone.
    const signInAs = (role: iam.Role, user: string) => role.addToPolicy(new iam.PolicyStatement({
      actions: ['rds-db:connect'],
      resources: [`arn:${this.partition}:rds-db:${region}:${accountId}:dbuser:${db.resourceId}/${user}`],
    }));
    signInAs(serverRole, db.user);
    signInAs(maintenanceRole, db.user);
    signInAs(purgeRole, db.user);
    // The ops function grants the authorizer's columns as their owner, after migrate.
    signInAs(opsRole, db.user);
    signInAs(authorizerRole, authorizerUser);

    const logGroup = (logId: string, name: string) => new logs.LogGroup(this, logId, {
      logGroupName: `/aws/lambda/${named(name)}`,
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // --- the server ------------------------------------------------------------------------------------
    const repository = ecr.Repository.fromRepositoryAttributes(this, 'Images', {
      repositoryName: image.repository,
      repositoryArn: `arn:${this.partition}:ecr:${region}:${accountId}:repository/${image.repository}`,
    });
    // The server's file access (Paimon's S3 FileIO through S3A), the purge function's as well.
    const fileIo = {
      COLVENNA_FILEIO_S3_REGION: region,
      // The SDK's default chain: the execution role's credentials are in environment variables in an
      // initialized execution environment, but come from the container credentials endpoint in one
      // restored from a SnapStart snapshot (found live; the spike's SnapStart test never reached S3).
      COLVENNA_FILEIO_S3_AWS_CREDENTIALS_PROVIDER: 'software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider',
      // Without internet egress S3A must not guess its region.
      COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION: region,
      // S3A 3.4 raises connection, socket and request timeouts below 15 s to 15 s, and applies the
      // request timeout to each attempt and to the whole call, the SDK's retries included. So one S3
      // request through S3A takes 15 s at most, provided S3A itself retries nothing: neither errors
      // (retry.limit) nor throttling (retry.throttle.limit, 20 by default).
      COLVENNA_FILEIO_FS_S3A_CONNECTION_ESTABLISH_TIMEOUT: `${S3A_REQUEST_SECONDS}s`,
      COLVENNA_FILEIO_FS_S3A_CONNECTION_TIMEOUT: `${S3A_REQUEST_SECONDS}s`,
      COLVENNA_FILEIO_FS_S3A_CONNECTION_REQUEST_TIMEOUT: `${S3A_REQUEST_SECONDS}s`,
      COLVENNA_FILEIO_FS_S3A_ATTEMPTS_MAXIMUM: '1',
      COLVENNA_FILEIO_FS_S3A_RETRY_LIMIT: '0',
      COLVENNA_FILEIO_FS_S3A_RETRY_THROTTLE_LIMIT: '0',
    };
    const jdbc = {
      COLVENNA_JDBC_URL: `jdbc:postgresql://${db.endpoint}:${db.port}/${db.name}?sslmode=verify-full&sslrootcert=${RDS_CA}`,
      COLVENNA_JDBC_USER: db.user,
      // No password anywhere: a token for each new connection, signed with the function's own role.
      COLVENNA_JDBC_AUTH: 'iam',
      COLVENNA_WAREHOUSE_NAME: warehouse,
      COLVENNA_WAREHOUSE_ROOT: `s3://${bucket.bucketName}/${prefix}`,
      COLVENNA_ENVIRONMENT: env,
    };
    const server = new lambda.DockerImageFunction(this, 'Server', {
      functionName: named('-server'),
      description: `colvenna-catalog ${sha}`,
      code: lambda.DockerImageCode.fromEcr(repository, { tagOrDigest: image.digest }),
      // The image is linux/amd64, a release's as well.
      architecture: lambda.Architecture.X86_64,
      memorySize: memory,
      timeout: SERVER_TIMEOUT,
      reservedConcurrentExecutions: concurrency,
      role: serverRole,
      logGroup: logGroup('ServerLogs', '-server'),
      ...inVpc,
      environment: {
        ...jdbc,
        // TLS ends at API Gateway; the adapter reaches the API port over loopback, the only address it binds.
        COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT: 'true',
        COLVENNA_PORT: '8181',
        COLVENNA_HEALTH_PORT: '8182',
        // The installer runs migrate; Lambda's init (10 seconds) must not wait on the database.
        COLVENNA_MIGRATE_ON_START: 'false',
        // One request at a time per execution environment.
        COLVENNA_JDBC_POOL_SIZE: '2',
        COLVENNA_JDBC_AUX_POOL_SIZE: '1',
        // What a request may wait in sequence stays under the gateway's 29 s: 10 s for a connection (the
        // server's, fixed) + 3 s of lock wait + one S3 request, 15 s at most through S3A (below) or 8 s
        // for the snapshot's conditional put: 28 s.
        COLVENNA_JDBC_LOCK_TIMEOUT: `PT${LOCK_WAIT_SECONDS}S`,
        COLVENNA_S3_CALL_TIMEOUT: 'PT8S',
        COLVENNA_S3_CALL_ATTEMPT_TIMEOUT: 'PT3S',
        // Lambda carries 6 MB of response at most; 4 MiB leaves room for base64.
        COLVENNA_MAX_RESPONSE_BYTES: '4194304',
        // Nothing waits on files beyond the gateway's 29 s (the LLD's "无服务器形态"): a drop only removes
        // the table from the catalog and records its files, which the purge function deletes; a
        // consistency check answers in pages of at most 10 s of checking, each with the next's token,
        // and a database drop answers 503 after 10 s of dropping its tables, to be asked again.
        COLVENNA_DROP_PURGE: 'deferred',
        COLVENNA_REQUEST_WORK_TIME: 'PT8S',
        ...fileIo,
        COLVENNA_DATA_TOKEN_MODE: tokens.mode,
        ...(tokens.mode === 'aws-sts' ? {
          COLVENNA_DATA_TOKEN_ROLE_ARN: data.roleArn,
          COLVENNA_DATA_TOKEN_DURATION: tokens.duration,
          COLVENNA_DATA_TOKEN_WEB_IDENTITY: 'cognito',
          COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID: pool.ref,
          COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER: developerProvider,
        } : {}),
        // The adapter forwards to the API port once /healthz (which needs no database) answers.
        AWS_LWA_PORT: '8181',
        AWS_LWA_READINESS_CHECK_PORT: '8182',
        AWS_LWA_READINESS_CHECK_PATH: '/healthz',
      },
      currentVersionOptions: { removalPolicy: RemovalPolicy.DESTROY },
    });
    if (props.snapStart) {
      (server.node.defaultChild as lambda.CfnFunction).snapStart = { applyOn: 'PublishedVersions' };
    }
    // API Gateway invokes this alias, so each deploy moves every new request to the new version at once.
    const live = new lambda.Alias(this, 'ServerLive', { aliasName: 'live', version: server.currentVersion });

    // --- the maintenance function: the same image, its own Runtime API loop (migrate, bootstrap-admin) ----
    const maintenance = new lambda.DockerImageFunction(this, 'Maintenance', {
      functionName: named('-maintenance'),
      description: `colvenna-catalog ${sha}: migrate and bootstrap-admin, for install.sh`,
      code: lambda.DockerImageCode.fromEcr(repository, {
        tagOrDigest: maintenanceImage.digest,
        entrypoint: ['/opt/colvenna-catalog/lambda-maintenance.sh'],
      }),
      architecture: lambda.Architecture.X86_64,
      memorySize: 2048,
      timeout: Duration.minutes(5),
      // One at a time: install.sh runs it, step by step.
      reservedConcurrentExecutions: 1,
      role: maintenanceRole,
      logGroup: logGroup('MaintenanceLogs', '-maintenance'),
      ...inVpc,
      environment: {
        ...jdbc,
        // The adapter (in the image's /opt/extensions) must never take this function's invocations:
        // it would only once its readiness check passed, and nothing listens on port 1.
        AWS_LWA_PORT: '1',
        AWS_LWA_READINESS_CHECK_PROTOCOL: 'tcp',
      },
    });

    // --- the purge function: the same image and loop, purge alone, on a schedule (#69) ----------------------
    // A drop records the table's files in the database (table_purges) in the transaction that removes the
    // table; each run deletes the recorded ones, idempotently, until a minute before its deadline, and
    // keeps a failed one recorded for the next run. It takes the maintenance function's image, so on an
    // upgrade the new one before the server (it does nothing while the schema is behind).
    const purge = new lambda.DockerImageFunction(this, 'Purge', {
      functionName: named('-purge'),
      description: `colvenna-catalog ${sha}: deletes the files of dropped tables, on its schedule`,
      code: lambda.DockerImageCode.fromEcr(repository, {
        tagOrDigest: maintenanceImage.digest,
        entrypoint: ['/opt/colvenna-catalog/lambda-maintenance.sh'],
      }),
      architecture: lambda.Architecture.X86_64,
      memorySize: 2048,
      timeout: PURGE_TIMEOUT,
      // One run at a time; a run due while another goes on waits in Lambda's queue for asynchronous events.
      reservedConcurrentExecutions: 1,
      // A run that fails (a purge left recorded, or the run itself) is tried again twice, then at the next run.
      retryAttempts: 2,
      role: purgeRole,
      logGroup: logGroup('PurgeLogs', '-purge'),
      ...inVpc,
      environment: {
        ...jdbc,
        ...fileIo,
        COLVENNA_MAINTENANCE_COMMANDS: 'purge',
        // As the maintenance function's: the adapter never takes an invocation.
        AWS_LWA_PORT: '1',
        AWS_LWA_READINESS_CHECK_PROTOCOL: 'tcp',
      },
    });
    // The schedule invokes the function asynchronously with its own role, which may invoke this function
    // alone and which only EventBridge Scheduler, for this account's default schedule group, may assume: no
    // resource policy on the function.
    const purgeScheduleRole = new iam.Role(this, 'PurgeScheduleRole', {
      roleName: named('-purge-schedule'),
      description: 'colvenna-catalog purge schedule: invokes the purge function',
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': accountId,
            'aws:SourceArn': `arn:${this.partition}:scheduler:${region}:${accountId}:schedule-group/default`,
          },
        },
      }),
    });
    const purgeSchedule = new scheduler.Schedule(this, 'PurgeSchedule', {
      scheduleName: named('-purge'),
      description: 'colvenna-catalog: deletes the files of dropped tables',
      schedule: scheduler.ScheduleExpression.rate(PURGE_INTERVAL),
      timeWindow: scheduler.TimeWindow.off(),
      target: new schedulerTargets.LambdaInvoke(purge, {
        role: purgeScheduleRole,
        // The payload the loop matches: the command as a JSON string.
        input: scheduler.ScheduleTargetInput.fromText('"purge"'),
        // A run not delivered by the next one's time is superseded by it.
        maxEventAge: PURGE_INTERVAL,
      }),
    });
    acknowledge(purgeScheduleRole, {
      id: 'AwsSolutions-IAM5',
      reason: 'lambda:InvokeFunction on the purge function and its versions (CDK\'s grant): that one function only',
      appliesTo: [{ regex: `/^Resource::<${this.getLogicalId(purge.node.defaultChild as lambda.CfnFunction)}\\.Arn>:\\*$/g` }],
    });
    // A purge that failed, or a run that did, is an error of the function (deploy/lambda/maintenance.sh).
    const purgeAlarm = new cloudwatch.Alarm(this, 'PurgeErrors', {
      alarmName: named('-purge-errors'),
      alarmDescription: 'colvenna-catalog: the purge function failed to delete the files of a dropped table, '
        + 'which stays recorded and is tried again; GET /colvenna/admin/v1/purges lists them with their last error',
      metric: purge.metricErrors({ period: PURGE_INTERVAL, statistic: cloudwatch.Stats.SUM }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    // What the errors do not show (#69): a dropped table's files still there after an hour. Each run's
    // summary is a line of CloudWatch's embedded metric format, which becomes a metric without any
    // permission or network (Main.summary): the first report over an hour sets it off. A run that
    // reports nothing for an hour (the schedule off, the function failing before its summary, a schema
    // behind its image) leaves the alarm without data, INSUFFICIENT_DATA rather than a false ALARM on
    // a new install before its first report; install.sh runs the function once at its end.
    const purgeBacklog = new cloudwatch.Alarm(this, 'PurgeBacklog', {
      alarmName: named('-purge-backlog'),
      alarmDescription: 'colvenna-catalog: a dropped table\'s files are still to be deleted after an hour (INSUFFICIENT_DATA: the '
        + 'purge function has not reported for an hour); GET /colvenna/admin/v1/purges lists what is left',
      metric: new cloudwatch.Metric({
        namespace: PURGE_METRICS_NAMESPACE,
        metricName: 'TablePurgeOldestSeconds',
        dimensionsMap: { Environment: env },
        statistic: cloudwatch.Stats.MAXIMUM,
        period: PURGE_INTERVAL,
      }),
      threshold: PURGE_BACKLOG.toSeconds(),
      evaluationPeriods: PURGE_BACKLOG.toMinutes() / PURGE_INTERVAL.toMinutes(),
      datapointsToAlarm: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.MISSING,
    });

    // --- the authorizer and the ops function (Node.js, bundled here by esbuild) ----------------------------
    const bundled = (functionId: string, name: string, entry: string, role: iam.Role, environment: Record<string, string>,
      extra: Partial<nodejs.NodejsFunctionProps>) => new nodejs.NodejsFunction(this, functionId, {
      functionName: named(name),
      entry: path.join(__dirname, '..', 'functions', entry),
      handler: 'handler',
      runtime: lambda.Runtime.NODEJS_24_X,
      architecture: lambda.Architecture.ARM_64,
      memorySize: 256,
      role,
      logGroup: logGroup(`${functionId}Logs`, name),
      ...inVpc,
      environment: {
        DB_HOST: db.endpoint,
        DB_PORT: String(db.port),
        DB_NAME: db.name,
        NODE_EXTRA_CA_CERTS: NODE_RDS_CA,
        ...environment,
      },
      depsLockFilePath: path.join(__dirname, '..', 'package-lock.json'),
      bundling: {
        format: nodejs.OutputFormat.CJS,
        target: 'node24',
        // pg's optional native binding is never used. The RDS signer is bundled: the runtime's SDK lacks it.
        externalModules: ['pg-native'],
        forceDockerBundling: false,
        sourceMap: false,
        minify: false,
      },
      ...extra,
    });
    const authorizerFunction = bundled('Authorizer', '-authorizer', 'authorizer.ts', authorizerRole, {
      DB_USER: authorizerUser,
    }, { timeout: Duration.seconds(10), reservedConcurrentExecutions: concurrency });

    // --- the private REST API -----------------------------------------------------------------------------
    const endpoints = endpointIds.map((vpcEndpointId, i) => ec2.InterfaceVpcEndpoint.fromInterfaceVpcEndpointAttributes(
      this, `ApiEndpoint${i}`, { vpcEndpointId, port: 443 }));
    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      logGroupName: `/aws/apigateway/${named('-api')}`,
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const authorizer = new apigw.RequestAuthorizer(this, 'BearerAuthorizer', {
      authorizerName: named('-bearer'),
      handler: authorizerFunction,
      identitySources: [apigw.IdentitySource.header('Authorization')],
      resultsCacheTtl: Duration.seconds(60),
    });
    const throttle = apiThrottle(concurrency);
    const api = new apigw.RestApi(this, 'Api', {
      restApiName: named('-api'),
      defaultIntegration: new apigw.LambdaIntegration(live, { proxy: true, timeout: GATEWAY_TIMEOUT, allowTestInvoke: false }),
      defaultMethodOptions: { authorizationType: apigw.AuthorizationType.CUSTOM, authorizer },
      description: 'colvenna-catalog: the Paimon REST catalog API, private, behind a bearer token authorizer',
      endpointConfiguration: { types: [apigw.EndpointType.PRIVATE], vpcEndpoints: endpoints },
      policy: new iam.PolicyDocument({
        statements: [
          new iam.PolicyStatement({
            sid: 'ThroughTheCatalogsVpcEndpoints',
            principals: [new iam.AnyPrincipal()],
            actions: ['execute-api:Invoke'],
            resources: ['execute-api:/*'],
            conditions: { StringEquals: { 'aws:SourceVpce': endpointIds } },
          }),
          new iam.PolicyStatement({
            sid: 'NothingElse',
            effect: iam.Effect.DENY,
            principals: [new iam.AnyPrincipal()],
            actions: ['execute-api:Invoke'],
            resources: ['execute-api:/*'],
            conditions: { StringNotEquals: { 'aws:SourceVpce': endpointIds } },
          }),
        ],
      }),
      // The account's API Gateway settings are not this stack's to change.
      cloudWatchRole: false,
      deployOptions: {
        stageName: STAGE,
        accessLogDestination: new apigw.LogGroupLogDestination(accessLogs),
        accessLogFormat: apigw.AccessLogFormat.custom(JSON.stringify({
          requestId: '$context.requestId',
          time: '$context.requestTime',
          sourceVpce: '$context.identity.vpceId',
          ip: '$context.identity.sourceIp',
          method: '$context.httpMethod',
          path: '$context.path',
          status: '$context.status',
          principal: '$context.authorizer.principalId',
          authorizerError: '$context.authorizer.error',
          responseLength: '$context.responseLength',
          latencyMs: '$context.responseLatency',
          integrationStatus: '$context.integration.status',
          integrationError: '$context.integrationErrorMessage',
          // Which gateway response answered, if one did (a throttled invocation's, say), and why (#80).
          errorType: '$context.error.responseType',
          error: '$context.error.message',
        })),
        loggingLevel: apigw.MethodLoggingLevel.ERROR,
        dataTraceEnabled: false,
        metricsEnabled: true,
        // A ceiling at what the reserved concurrency serves at the sized-for mean: past it, 429 from the
        // gateway itself (#80). Slower requests still take more than the concurrency: those get 503 (below).
        throttlingRateLimit: throttle.rateLimit,
        throttlingBurstLimit: throttle.burstLimit,
      },
    });
    // Every method and path, the root's included (addProxy puts its method on the root too), goes to
    // the server through the authorizer: these are the API's defaults, which every method takes.
    api.root.addProxy({ anyMethod: true });
    // A database the authorizer cannot reach is a reason to retry, not a server error.
    for (const [responseId, type] of [
      ['AuthorizerFailure', apigw.ResponseType.AUTHORIZER_FAILURE],
      ['AuthorizerConfigurationError', apigw.ResponseType.AUTHORIZER_CONFIGURATION_ERROR],
    ] as const) {
      api.addGatewayResponse(responseId, {
        type,
        statusCode: '503',
        templates: { 'application/json': '{"message": "The catalog cannot check the bearer token now; retry."}' },
      });
    }
    // Requests over the reserved concurrency (#80). Lambda refuses to invoke the server (429), which API
    // Gateway answers as an integration failure, 500 by default: Paimon's client never retries it. A
    // function that ran and failed is the same gateway response, 502 by default (both measured: the
    // access log's errorType), and a gateway response's status is fixed by its type. So both answer 503,
    // which the client sends again. A trade-off, the LLD's "无服务器形态": a throttled request never ran,
    // and most writes apply once however often they are sent; a few (a rollback without its base, a
    // fast-forward, replaceTable, a schema rollback, a consumer reset, a token issued) would apply again
    // if the function failed after their commit, which takes running out of memory or Lambda failing. A
    // gateway timeout (504) keeps its status: the request may still be running.
    api.addGatewayResponse('IntegrationFailure', {
      type: apigw.ResponseType.INTEGRATION_FAILURE,
      statusCode: '503',
      templates: { 'application/json': '{"message": "The catalog could not take the request now; retry."}' },
    });
    // The stage's own throttle (above): 429, its default, with a message of the catalog's.
    api.addGatewayResponse('Throttled', {
      type: apigw.ResponseType.THROTTLED,
      statusCode: '429',
      templates: { 'application/json': '{"message": "The catalog takes no more requests at once; retry."}' },
    });
    acknowledge(api,
      { id: 'AwsSolutions-APIG2', reason: 'Every request goes to the server, which validates it against the Paimon REST contract' },
      {
        id: 'AwsSolutions-APIG3',
        reason: 'Private: only the given VPC endpoints reach it, and each request is authenticated twice '
          + '(the authorizer, then the server); a web ACL would add an idle cost and nothing a bearer token does not',
      },
      {
        id: 'AwsSolutions-COG4',
        reason: 'Paimon clients send an opaque bearer token (the catalog\'s own, design 6.2), not a Cognito user pool token: '
          + 'a REQUEST Lambda authorizer checks it',
      });
    const apiUrl = `https://${api.restApiId}.execute-api.${region}.${this.urlSuffix}/${STAGE}`;
    const apiEndpointUrl = `https://${api.restApiId}-${endpointIds[0]}.execute-api.${region}.${this.urlSuffix}/${STAGE}`;

    const ops = bundled('Ops', '-ops', 'ops.ts', opsRole, {
      DB_USER: db.user,
      DB_AUTHORIZER_USER: authorizerUser,
      // The endpoint's own name: it resolves to the endpoint whether or not it has private DNS.
      API_URL: apiEndpointUrl,
    }, { timeout: Duration.minutes(1) });

    new CfnOutput(this, 'ApiId', { value: api.restApiId });
    new CfnOutput(this, 'ApiUrl', { value: apiUrl });
    new CfnOutput(this, 'ApiEndpointUrl', { value: apiEndpointUrl });
    new CfnOutput(this, 'ServerFunctionName', { value: server.functionName });
    new CfnOutput(this, 'ServerVersion', { value: server.currentVersion.version });
    new CfnOutput(this, 'ServerImage', { value: props.imageUri });
    new CfnOutput(this, 'MaintenanceImage', { value: props.maintenanceImageUri ?? props.imageUri });
    new CfnOutput(this, 'MaintenanceFunctionName', { value: maintenance.functionName });
    new CfnOutput(this, 'PurgeFunctionName', { value: purge.functionName });
    new CfnOutput(this, 'PurgeScheduleName', { value: purgeSchedule.scheduleName });
    new CfnOutput(this, 'PurgeAlarmName', { value: purgeAlarm.alarmName });
    new CfnOutput(this, 'PurgeBacklogAlarmName', { value: purgeBacklog.alarmName });
    new CfnOutput(this, 'AuthorizerFunctionName', { value: authorizerFunction.functionName });
    new CfnOutput(this, 'OpsFunctionName', { value: ops.functionName });
    new CfnOutput(this, 'AdminTokenSecretArn', { value: adminToken.secretArn });
    new CfnOutput(this, 'DataRoleArn', { value: data.roleArn });
    new CfnOutput(this, 'IdentityPoolId', { value: pool.ref });
    new CfnOutput(this, 'WarehouseName', { value: warehouse });
    new CfnOutput(this, 'WarehouseRoot', { value: `s3://${bucket.bucketName}/${prefix}` });
    // The database the functions run on: install.sh refuses COLVENNA_DB=create while it is not the storage
    // stack's (a restored instance), which would move the install back.
    new CfnOutput(this, 'DatabaseEndpoint', { value: db.endpoint });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
