import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { forbidResources } from '../src/guard';
import { nagChecks } from '../src/nag';
import { API_REQUEST_SECONDS, apiThrottle, CatalogServerlessStack, CatalogServerlessStackProps } from '../src/serverless-stack';
import { unacknowledged } from './nag-helpers';

const DIGEST = `sha256:${'1'.repeat(64)}`;
const RESOURCE_ID = 'db-ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const props: CatalogServerlessStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  environment: 'uat',
  sourceSha: 'a'.repeat(40),
  vpcId: 'vpc-0123456789abcdef0',
  subnetIds: ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
  subnetZones: ['us-west-2a', 'us-west-2b'],
  securityGroupId: 'sg-0123456789abcdef0',
  apiEndpointIds: ['vpce-0123456789abcdef0', 'vpce-0123456789abcdef1'],
  imageUri: `111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog@${DIGEST}`,
  bucketName: 'bucket',
  warehousePrefix: 'warehouse',
  warehouseName: 'colvenna',
  database: { endpoint: 'db.example.com', port: 5432, name: 'colvenna_catalog', user: 'colvenna_catalog', resourceId: RESOURCE_ID },
  dataTokens: { mode: 'aws-sts', duration: 'PT3H' },
  memoryMb: 2048,
  reservedConcurrency: 10,
  snapStart: false,
};

function build(overrides: Partial<CatalogServerlessStackProps> = {}) {
  const stack = new CatalogServerlessStack(new App(), 'ColvennaCatalogUatServerless', { ...props, ...overrides });
  return { stack, template: Template.fromStack(stack) };
}
// One synthesis for the tests that only read the default template.
const { template: defaults } = build();
const resources = (template: Template, type: string) => Object.entries(template.findResources(type)) as [string, any][];
const flat = (value: unknown) => JSON.stringify(value);
const fn = (template: Template, name: string) => {
  const found = resources(template, 'AWS::Lambda::Function').filter(([, f]) => f.Properties.FunctionName === name);
  assert.equal(found.length, 1, name);
  return found[0];
};
const environment = (template: Template, name: string) => fn(template, name)[1].Properties.Environment.Variables;
/** Every statement of the inline policies attached to the role (by its logical ID). */
const statementsOf = (template: Template, roleId: string) => resources(template, 'AWS::IAM::Policy')
  .filter(([, p]) => p.Properties.Roles.some((r: any) => r.Ref === roleId))
  .flatMap(([, p]) => p.Properties.PolicyDocument.Statement);
const roleOf = (template: Template, name: string) => fn(template, name)[1].Properties.Role['Fn::GetAtt'][0];

test('no function URL, no HTTP API, no role on the identity pool, no internet path', () => {
  for (const type of ['AWS::Lambda::Url', 'AWS::ApiGatewayV2::Api', 'AWS::ApiGatewayV2::Route', 'AWS::Cognito::IdentityPoolRoleAttachment',
    'AWS::ApiGateway::Account', 'AWS::EC2::NatGateway', 'AWS::EC2::EIP', 'AWS::EC2::InternetGateway', 'AWS::IAM::User']) {
    assert.deepEqual(defaults.findResources(type), {}, type);
  }
});

test('the REST API is private, and its resource policy admits only the given VPC endpoints', () => {
  const [[, api]] = resources(defaults, 'AWS::ApiGateway::RestApi');
  assert.deepEqual(api.Properties.EndpointConfiguration, {
    Types: ['PRIVATE'], VpcEndpointIds: ['vpce-0123456789abcdef0', 'vpce-0123456789abcdef1'],
  });
  const statements = api.Properties.Policy.Statement;
  assert.deepEqual(statements.map((s: any) => [s.Effect, s.Action, s.Resource, s.Condition]), [
    ['Allow', 'execute-api:Invoke', 'execute-api:/*',
      { StringEquals: { 'aws:SourceVpce': ['vpce-0123456789abcdef0', 'vpce-0123456789abcdef1'] } }],
    ['Deny', 'execute-api:Invoke', 'execute-api:/*',
      { StringNotEquals: { 'aws:SourceVpce': ['vpce-0123456789abcdef0', 'vpce-0123456789abcdef1'] } }],
  ]);
});

test('every method, the root\'s included, needs the bearer authorizer and goes to the server\'s alias', () => {
  const [[authorizerId, authorizer]] = resources(defaults, 'AWS::ApiGateway::Authorizer');
  assert.equal(authorizer.Properties.Type, 'REQUEST');
  assert.equal(authorizer.Properties.IdentitySource, 'method.request.header.Authorization');
  assert.equal(authorizer.Properties.AuthorizerResultTtlInSeconds, 60);
  const methods = resources(defaults, 'AWS::ApiGateway::Method');
  assert.equal(methods.length, 2, 'ANY on / and on /{proxy+}');
  for (const [id, method] of methods) {
    assert.equal(method.Properties.AuthorizationType, 'CUSTOM', id);
    assert.deepEqual(method.Properties.AuthorizerId, { Ref: authorizerId }, id);
    assert.equal(method.Properties.Integration.Type, 'AWS_PROXY', id);
    assert.equal(method.Properties.Integration.TimeoutInMillis, 29000, id);
    assert.match(flat(method.Properties.Integration.Uri), /"Ref":"ServerLive[0-9A-F]+"/, `${id}: the alias "live"`);
  }
});

test('only API Gateway may invoke, each permission for this API only', () => {
  const [[apiId]] = resources(defaults, 'AWS::ApiGateway::RestApi');
  const permissions = resources(defaults, 'AWS::Lambda::Permission');
  assert.equal(permissions.length, 3, 'the authorizer, and the alias for / and /{proxy+}');
  for (const [id, permission] of permissions) {
    assert.equal(permission.Properties.Principal, 'apigateway.amazonaws.com', id);
    assert.equal(permission.Properties.Action, 'lambda:InvokeFunction', id);
    assert.equal(permission.Properties.FunctionUrlAuthType, undefined, id);
    const source = flat(permission.Properties.SourceArn);
    assert.ok(source.includes(':execute-api:us-west-2:111122223333:') && source.includes(`{"Ref":"${apiId}"}`), `${id}: ${source}`);
  }
  // The maintenance, purge and ops functions are invoked by IAM-authenticated callers alone (the purge
  // function's schedule through its role): no permission names them.
  for (const name of ['colvenna-catalog-uat-maintenance', 'colvenna-catalog-uat-purge', 'colvenna-catalog-uat-ops']) {
    const [logicalId] = fn(defaults, name);
    assert.ok(!flat(permissions).includes(`"${logicalId}"`), name);
  }
});

test('a function URL added to any function fails the synthesis', () => {
  for (const id of ['Server', 'Maintenance', 'Purge', 'Authorizer', 'Ops']) {
    const { stack } = build();
    (stack.node.findChild(id) as lambda.Function).addFunctionUrl({ authType: lambda.FunctionUrlAuthType.AWS_IAM });
    forbidResources(stack, { noInternetPaths: true });
    const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('AWS::Lambda::Url is not allowed'));
    assert.equal(errors.length, 1, id);
  }
});

test('an unauthenticated method or a public endpoint added to the API fails the synthesis', () => {
  const { stack } = build();
  const api = stack.node.findChild('Api') as import('aws-cdk-lib/aws-apigateway').RestApi;
  api.root.addResource('open').addMethod('GET', undefined, {
    authorizationType: (require('aws-cdk-lib/aws-apigateway') as typeof import('aws-cdk-lib/aws-apigateway')).AuthorizationType.NONE,
  });
  forbidResources(stack);
  const errors = Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('a method without authorization'));
  assert.equal(errors.length, 1);
});

test('the server function: the image by digest, its settings for Lambda, a timeout longer than the gateway\'s', () => {
  const [, server] = fn(defaults, 'colvenna-catalog-uat-server');
  assert.equal(server.Properties.PackageType, 'Image');
  assert.match(flat(server.Properties.Code.ImageUri), new RegExp(`/colvenna-catalog-uat/colvenna-catalog@${DIGEST}"`));
  assert.deepEqual(server.Properties.Architectures, ['x86_64']);
  assert.equal(server.Properties.Timeout, 60);
  assert.equal(server.Properties.MemorySize, 2048);
  assert.equal(server.Properties.ReservedConcurrentExecutions, 10);
  assert.equal(server.Properties.ImageConfig, undefined, 'the image\'s own entry point: serve');
  assert.ok(server.Properties.VpcConfig.SubnetIds.length === 2);
  const env = environment(defaults, 'colvenna-catalog-uat-server');
  assert.equal(env.COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT, 'true');
  assert.equal(env.COLVENNA_ALLOW_PLAINTEXT, undefined);
  assert.equal(env.COLVENNA_TLS_KEYSTORE, undefined);
  assert.equal(env.COLVENNA_MIGRATE_ON_START, 'false');
  assert.equal(env.COLVENNA_JDBC_POOL_SIZE, '2');
  assert.equal(env.COLVENNA_JDBC_AUX_POOL_SIZE, '1');
  assert.equal(env.COLVENNA_MAX_RESPONSE_BYTES, '4194304');
  assert.equal(env.COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION, 'us-west-2');
  // Not the environment variables alone: a SnapStart restore has no AWS_ACCESS_KEY_ID.
  assert.equal(env.COLVENNA_FILEIO_S3_AWS_CREDENTIALS_PROVIDER, 'software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider');
  assert.equal(env.COLVENNA_WAREHOUSE_ROOT, 's3://bucket/warehouse');
  assert.equal(env.COLVENNA_ENVIRONMENT, 'uat');
  // The waits a request may take in sequence add up to less than the gateway's 29 seconds: 10 s for a
  // connection (fixed in the server), the lock wait, and one S3 request: the snapshot's conditional put,
  // or one through S3A, whose timeouts S3A 3.4 raises to 15 s at least, each attempt and the whole call
  // bounded by the request timeout as long as S3A retries nothing itself.
  const seconds = (value: string) => Number((/^PT(\d+)S$/.exec(value) ?? /^(\d+)s$/.exec(value))![1]);
  const lock = seconds(env.COLVENNA_JDBC_LOCK_TIMEOUT);
  const s3a = Math.max(15, seconds(env.COLVENNA_FILEIO_FS_S3A_CONNECTION_REQUEST_TIMEOUT));
  assert.equal(env.COLVENNA_FILEIO_FS_S3A_RETRY_LIMIT, '0');
  assert.equal(env.COLVENNA_FILEIO_FS_S3A_RETRY_THROTTLE_LIMIT, '0');
  for (const key of ['COLVENNA_FILEIO_FS_S3A_CONNECTION_ESTABLISH_TIMEOUT', 'COLVENNA_FILEIO_FS_S3A_CONNECTION_TIMEOUT']) {
    assert.ok(seconds(env[key]) <= s3a, `${key} within the request timeout`);
  }
  assert.ok(10 + lock + s3a < 29, `10 + ${lock} + ${s3a}`);
  assert.ok(10 + lock + seconds(env.COLVENNA_S3_CALL_TIMEOUT) < 29);
  assert.ok(seconds(env.COLVENNA_S3_CALL_ATTEMPT_TIMEOUT) <= seconds(env.COLVENNA_S3_CALL_TIMEOUT));
  assert.match(env.COLVENNA_JDBC_URL, /^jdbc:postgresql:\/\/db\.example\.com:5432\/colvenna_catalog\?sslmode=verify-full&sslrootcert=\/opt\//);
  assert.deepEqual([env.AWS_LWA_PORT, env.AWS_LWA_READINESS_CHECK_PORT, env.AWS_LWA_READINESS_CHECK_PATH], ['8181', '8182', '/healthz']);
  // Nothing waits on files beyond the gateway's timeout (#69): a drop leaves the files to the purge
  // function; a consistency check stops taking steps after its work time, and a database drop
  // dropping tables. The drop's worst case: the connection, the work time, its last table (the table's
  // lock, then all its branch pointers' in one) and the database's own lock.
  assert.equal(env.COLVENNA_DROP_PURGE, 'deferred');
  const work = seconds(env.COLVENNA_REQUEST_WORK_TIME);
  assert.ok(10 + work + 3 * lock < 29, `10 + ${work} + 3 x ${lock}`);
});

test('no function has a password or any secret in its environment: they sign in with RDS IAM tokens', () => {
  for (const [, f] of resources(defaults, 'AWS::Lambda::Function')) {
    const env = f.Properties.Environment?.Variables ?? {};
    const name = f.Properties.FunctionName;
    assert.ok(!Object.keys(env).some((k) => /PASSWORD|SECRET|TOKEN(?!_)/.test(k) && !/DATA_TOKEN/.test(k)), `${name}: ${Object.keys(env)}`);
    assert.doesNotMatch(flat(env), /resolve:secretsmanager|resolve:ssm/, `${name}: no dynamic reference`);
  }
  for (const name of ['colvenna-catalog-uat-server', 'colvenna-catalog-uat-maintenance', 'colvenna-catalog-uat-purge']) {
    assert.equal(environment(defaults, name).COLVENNA_JDBC_AUTH, 'iam', name);
    assert.equal(environment(defaults, name).COLVENNA_JDBC_PASSWORD, undefined, name);
  }
  // The admin's token is the one secret the stack holds; no function reads it.
  assert.deepEqual(resources(defaults, 'AWS::SecretsManager::Secret').map(([, r]) => r.Properties.Name), ['colvenna-catalog-uat/admin-token']);
});

test('each role may sign in as its own database user, on this database alone', () => {
  const connect = (name: string) => statementsOf(defaults, roleOf(defaults, name))
    .filter((st: any) => flat(st.Action).includes('rds-db')).map((st: any) => [st.Action, flat(st.Resource)]);
  const arn = (user: string) => `"arn:",{"Ref":"AWS::Partition"},":rds-db:us-west-2:111122223333:dbuser:${RESOURCE_ID}/${user}"`;
  for (const [name, user] of [['colvenna-catalog-uat-server', 'colvenna_catalog'], ['colvenna-catalog-uat-maintenance', 'colvenna_catalog'],
    ['colvenna-catalog-uat-purge', 'colvenna_catalog'], ['colvenna-catalog-uat-ops', 'colvenna_catalog'],
    ['colvenna-catalog-uat-authorizer', 'colvenna_catalog_authz']]) {
    const found = connect(name);
    assert.equal(found.length, 1, name);
    assert.equal(found[0][0], 'rds-db:connect');
    assert.ok(found[0][1].includes(arn(user)), `${name}: ${found[0][1]}`);
  }
});

test('per-table credentials: a developer-only identity pool, a data role only its authenticated identities may assume', () => {
  const [[poolId, pool]] = resources(defaults, 'AWS::Cognito::IdentityPool');
  const { IdentityPoolTags: _tags, ...settings } = pool.Properties;
  assert.deepEqual(settings, {
    IdentityPoolName: 'colvenna_catalog_uat',
    AllowUnauthenticatedIdentities: false,
    AllowClassicFlow: false,
    DeveloperProviderName: 'colvenna-catalog-uat.catalog',
  });
  const [[dataId, data]] = resources(defaults, 'AWS::IAM::Role').filter(([, r]) => r.Properties.RoleName === 'colvenna-catalog-uat-data');
  assert.equal(data.Properties.MaxSessionDuration, 43200);
  assert.deepEqual(data.Properties.AssumeRolePolicyDocument.Statement, [{
    Action: 'sts:AssumeRoleWithWebIdentity',
    Effect: 'Allow',
    Principal: { Federated: 'cognito-identity.amazonaws.com' },
    Condition: {
      StringEquals: { 'cognito-identity.amazonaws.com:aud': { Ref: poolId } },
      'ForAnyValue:StringLike': { 'cognito-identity.amazonaws.com:amr': 'authenticated' },
    },
  }]);
  for (const s of statementsOf(defaults, dataId)) {
    assert.doesNotMatch(flat(s.Resource), /:s3:::bucket\/\*"/, 'no whole-bucket objects');
  }
  const env = environment(defaults, 'colvenna-catalog-uat-server');
  assert.equal(env.COLVENNA_DATA_TOKEN_MODE, 'aws-sts');
  assert.equal(env.COLVENNA_DATA_TOKEN_WEB_IDENTITY, 'cognito');
  assert.deepEqual(env.COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID, { Ref: poolId });
  assert.equal(env.COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER, 'colvenna-catalog-uat.catalog');
  assert.deepEqual(env.COLVENNA_DATA_TOKEN_ROLE_ARN, { 'Fn::GetAtt': [dataId, 'Arn'] });
});

test('only the server\'s role may get tokens from the identity pool; only it and the purge function\'s touch the warehouse', () => {
  const [[poolId]] = resources(defaults, 'AWS::Cognito::IdentityPool');
  const serverRole = roleOf(defaults, 'colvenna-catalog-uat-server');
  const grants = resources(defaults, 'AWS::IAM::Policy').flatMap(([, p]) => p.Properties.PolicyDocument.Statement
    .filter((s: any) => flat(s.Action).includes('cognito-identity')).map((s: any) => ({ roles: p.Properties.Roles, s })));
  assert.equal(grants.length, 1);
  assert.deepEqual(grants[0].roles, [{ Ref: serverRole }]);
  assert.equal(grants[0].s.Action, 'cognito-identity:GetOpenIdTokenForDeveloperIdentity');
  assert.ok(flat(grants[0].s.Resource).includes(`:identitypool/",{"Ref":"${poolId}"}`), flat(grants[0].s.Resource));
  for (const name of ['colvenna-catalog-uat-maintenance', 'colvenna-catalog-uat-authorizer', 'colvenna-catalog-uat-ops']) {
    assert.deepEqual(statementsOf(defaults, roleOf(defaults, name)).map((st: any) => st.Action), ['rds-db:connect'],
      `${name}: the managed VPC policy, and signing in as its database user`);
  }
  const touching = resources(defaults, 'AWS::IAM::Policy')
    .filter(([, p]) => flat(p.Properties.PolicyDocument.Statement).includes('"s3:'))
    .flatMap(([, p]) => p.Properties.Roles.map((r: any) => r.Ref)).sort();
  const [[dataId]] = resources(defaults, 'AWS::IAM::Role').filter(([, r]) => r.Properties.RoleName === 'colvenna-catalog-uat-data');
  assert.deepEqual(touching, [dataId, roleOf(defaults, 'colvenna-catalog-uat-purge'), roleOf(defaults, 'colvenna-catalog-uat-server')].sort(),
    'the data role (the ceiling of the per-table credentials), the server and the purge function');
  for (const roleId of [roleOf(defaults, 'colvenna-catalog-uat-server'), roleOf(defaults, 'colvenna-catalog-uat-purge')]) {
    const objects = statementsOf(defaults, roleId).filter((st: any) => flat(st.Action).includes('s3:DeleteObject'));
    assert.equal(objects.length, 1, roleId);
    assert.match(flat(objects[0].Resource), /:s3:::bucket\/warehouse\/\*"/, `${roleId}: the warehouse prefix only`);
  }
});

test('the maintenance function: the same image, its own runtime loop, the adapter kept off', () => {
  const [, maintenance] = fn(defaults, 'colvenna-catalog-uat-maintenance');
  assert.deepEqual(maintenance.Properties.ImageConfig, { EntryPoint: ['/opt/colvenna-catalog/lambda-maintenance.sh'] });
  assert.match(flat(maintenance.Properties.Code.ImageUri), new RegExp(`@${DIGEST}"`));
  assert.equal(maintenance.Properties.ReservedConcurrentExecutions, 1);
  const env = environment(defaults, 'colvenna-catalog-uat-maintenance');
  assert.equal(env.AWS_LWA_PORT, '1');
  assert.equal(env.COLVENNA_DATA_TOKEN_MODE, undefined);
});

test('the purge function: the maintenance image, its own loop for purge alone, the server\'s file settings', () => {
  const [, purge] = fn(defaults, 'colvenna-catalog-uat-purge');
  assert.equal(purge.Properties.PackageType, 'Image');
  assert.deepEqual(purge.Properties.ImageConfig, { EntryPoint: ['/opt/colvenna-catalog/lambda-maintenance.sh'] });
  assert.match(flat(purge.Properties.Code.ImageUri), new RegExp(`/colvenna-catalog-uat/colvenna-catalog@${DIGEST}"`));
  assert.deepEqual(purge.Properties.Architectures, ['x86_64']);
  assert.equal(purge.Properties.MemorySize, 2048);
  assert.equal(purge.Properties.Timeout, 900);
  assert.equal(purge.Properties.ReservedConcurrentExecutions, 1);
  assert.ok(purge.Properties.VpcConfig.SubnetIds.length === 2);
  const [[, invokeConfig]] = resources(defaults, 'AWS::Lambda::EventInvokeConfig');
  assert.deepEqual(invokeConfig.Properties.FunctionName, { Ref: fn(defaults, 'colvenna-catalog-uat-purge')[0] });
  assert.equal(invokeConfig.Properties.MaximumRetryAttempts, 2);
  const env = environment(defaults, 'colvenna-catalog-uat-purge');
  assert.equal(env.COLVENNA_MAINTENANCE_COMMANDS, 'purge');
  assert.deepEqual([env.AWS_LWA_PORT, env.AWS_LWA_READINESS_CHECK_PROTOCOL], ['1', 'tcp']);
  assert.equal(env.COLVENNA_DATA_TOKEN_MODE, undefined);
  // The same file access as the server's.
  const server = environment(defaults, 'colvenna-catalog-uat-server');
  const fileIo = (vars: Record<string, unknown>) => Object.fromEntries(Object.entries(vars).filter(([k]) => k.startsWith('COLVENNA_FILEIO_')));
  assert.ok(Object.keys(fileIo(env)).length >= 8);
  assert.deepEqual(fileIo(env), fileIo(server));
  for (const key of ['COLVENNA_JDBC_URL', 'COLVENNA_JDBC_USER', 'COLVENNA_WAREHOUSE_NAME', 'COLVENNA_WAREHOUSE_ROOT', 'COLVENNA_ENVIRONMENT']) {
    assert.deepEqual(env[key], server[key], key);
  }
  assert.equal(environment(defaults, 'colvenna-catalog-uat-maintenance').COLVENNA_MAINTENANCE_COMMANDS, undefined,
    'the maintenance function runs its default commands, migrate and bootstrap-admin');
});

test('the purge schedule: every 15 minutes, the payload "purge", through a role only the scheduler may assume', () => {
  const [[, schedule]] = resources(defaults, 'AWS::Scheduler::Schedule');
  const [purgeId] = fn(defaults, 'colvenna-catalog-uat-purge');
  assert.equal(schedule.Properties.Name, 'colvenna-catalog-uat-purge');
  assert.equal(schedule.Properties.ScheduleExpression, 'rate(15 minutes)');
  assert.equal(schedule.Properties.State, 'ENABLED');
  assert.deepEqual(schedule.Properties.FlexibleTimeWindow, { Mode: 'OFF' });
  assert.deepEqual(schedule.Properties.Target.Arn, { 'Fn::GetAtt': [purgeId, 'Arn'] });
  // The loop matches the command as a JSON string, quotes and all.
  assert.equal(schedule.Properties.Target.Input, '"purge"');
  assert.equal(schedule.Properties.Target.RetryPolicy.MaximumEventAgeInSeconds, 900);
  const roleId = schedule.Properties.Target.RoleArn['Fn::GetAtt'][0];
  const [[, role]] = resources(defaults, 'AWS::IAM::Role').filter(([id]) => id === roleId);
  assert.equal(role.Properties.RoleName, 'colvenna-catalog-uat-purge-schedule');
  assert.deepEqual(role.Properties.ManagedPolicyArns, undefined);
  const [trust] = role.Properties.AssumeRolePolicyDocument.Statement;
  assert.equal(role.Properties.AssumeRolePolicyDocument.Statement.length, 1);
  assert.deepEqual([trust.Effect, trust.Action, trust.Principal], ['Allow', 'sts:AssumeRole', { Service: 'scheduler.amazonaws.com' }]);
  assert.equal(trust.Condition.StringEquals['aws:SourceAccount'], '111122223333');
  assert.match(flat(trust.Condition.StringEquals['aws:SourceArn']), /:scheduler:us-west-2:111122223333:schedule-group\/default"/);
  const statements = statementsOf(defaults, roleId);
  assert.deepEqual(statements.map((st: any) => st.Action), ['lambda:InvokeFunction'], 'invoking the purge function, nothing else');
  assert.deepEqual(statements[0].Resource, [{ 'Fn::GetAtt': [purgeId, 'Arn'] }, { 'Fn::Join': ['', [{ 'Fn::GetAtt': [purgeId, 'Arn'] }, ':*']] }]);
});

test('an alarm on the purge function\'s errors, which a failed purge is', () => {
  const alarms = resources(defaults, 'AWS::CloudWatch::Alarm');
  assert.equal(alarms.length, 2);
  const [, alarm] = alarms.find(([, a]: any) => a.Properties.AlarmName === 'colvenna-catalog-uat-purge-errors')!;
  assert.equal(alarm.Properties.AlarmName, 'colvenna-catalog-uat-purge-errors');
  assert.deepEqual([alarm.Properties.Namespace, alarm.Properties.MetricName, alarm.Properties.Statistic, alarm.Properties.Period],
    ['AWS/Lambda', 'Errors', 'Sum', 900]);
  assert.deepEqual(alarm.Properties.Dimensions, [{ Name: 'FunctionName', Value: { Ref: fn(defaults, 'colvenna-catalog-uat-purge')[0] } }]);
  assert.deepEqual([alarm.Properties.ComparisonOperator, alarm.Properties.Threshold, alarm.Properties.EvaluationPeriods],
    ['GreaterThanOrEqualToThreshold', 1, 1]);
  assert.equal(alarm.Properties.TreatMissingData, 'notBreaching');
  assert.equal(alarm.Properties.AlarmActions, undefined, 'the operator adds the action');
});

test('an alarm on the purge backlog: files left for an hour; no run reporting for an hour is insufficient data', () => {
  const [, alarm] = resources(defaults, 'AWS::CloudWatch::Alarm')
    .find(([, a]: any) => a.Properties.AlarmName === 'colvenna-catalog-uat-purge-backlog')!;
  // The purge command's summary (Main.summary): CloudWatch's embedded metric format.
  assert.deepEqual([alarm.Properties.Namespace, alarm.Properties.MetricName, alarm.Properties.Statistic, alarm.Properties.Period],
    ['ColvennaCatalog', 'TablePurgeOldestSeconds', 'Maximum', 900]);
  assert.deepEqual(alarm.Properties.Dimensions, [{ Name: 'Environment', Value: 'uat' }]);
  // The first report over an hour sets it off, within the next run.
  assert.deepEqual([alarm.Properties.ComparisonOperator, alarm.Properties.Threshold, alarm.Properties.EvaluationPeriods,
    alarm.Properties.DatapointsToAlarm], ['GreaterThanThreshold', 3600, 4, 1]);
  // No report for the hour is INSUFFICIENT_DATA, not a false ALARM on a new install before its first.
  assert.equal(alarm.Properties.TreatMissingData, 'missing');
  assert.equal(alarm.Properties.AlarmActions, undefined, 'the operator adds the action');
  // The server's environment, which the summary's dimension is.
  const [, server] = fn(defaults, 'colvenna-catalog-uat-server');
  assert.equal(server.Properties.Environment.Variables.COLVENNA_ENVIRONMENT, 'uat');
});

test('the Node.js functions verify the database\'s certificate against the runtime\'s RDS CAs', () => {
  for (const name of ['colvenna-catalog-uat-authorizer', 'colvenna-catalog-uat-ops']) {
    const [, f] = fn(defaults, name);
    assert.equal(f.Properties.Runtime, 'nodejs24.x');
    assert.equal(environment(defaults, name).NODE_EXTRA_CA_CERTS, '/var/runtime/ca-cert.pem');
  }
  assert.equal(environment(defaults, 'colvenna-catalog-uat-authorizer').DB_USER, 'colvenna_catalog_authz');
  assert.equal(fn(defaults, 'colvenna-catalog-uat-authorizer')[1].Properties.ReservedConcurrentExecutions, 10);
  assert.match(flat(environment(defaults, 'colvenna-catalog-uat-ops').API_URL), /-vpce-0123456789abcdef0\.execute-api\.us-west-2\./);
});

test('SnapStart only when asked, on the published version the alias points at', () => {
  assert.equal(fn(defaults, 'colvenna-catalog-uat-server')[1].Properties.SnapStart, undefined);
  const { template } = build({ snapStart: true });
  assert.deepEqual(fn(template, 'colvenna-catalog-uat-server')[1].Properties.SnapStart, { ApplyOn: 'PublishedVersions' });
  const [[versionId, version]] = resources(template, 'AWS::Lambda::Version');
  // An old version goes when a new one replaces it: an active SnapStart version is billed.
  assert.equal(version.DeletionPolicy, 'Delete');
  const [[, alias]] = resources(template, 'AWS::Lambda::Alias');
  assert.equal(alias.Properties.Name, 'live');
  assert.deepEqual(alias.Properties.FunctionVersion, { 'Fn::GetAtt': [versionId, 'Version'] });
});

/** The deployed template as sl_maintenance_first changes it (deploy/lib/serverless_template.py) from the synthesized one. */
function maintenanceFirst(deployed: object, synthesized: object): any {
  const dir = mkdtempSync(path.join(tmpdir(), 'maintenance-first-'));
  writeFileSync(path.join(dir, 'deployed.json'), JSON.stringify(deployed));
  writeFileSync(path.join(dir, 'synthesized.json'), JSON.stringify(synthesized));
  const run = spawnSync('python3', [path.join(__dirname, '..', '..', 'lib', 'serverless_template.py'), 'maintenance-first',
    path.join(dir, 'deployed.json'), path.join(dir, 'synthesized.json'), 'colvenna-catalog-uat-maintenance'], { encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  return JSON.parse(run.stdout);
}

/** The paths at which two JSON values differ. */
function differences(a: any, b: any, at = ''): string[] {
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null || Array.isArray(a) !== Array.isArray(b)) {
    return flat(a) === flat(b) ? [] : [at];
  }
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])];
  return keys.flatMap((k) => differences(a[k], b[k], `${at}/${k}`));
}

test('an upgrade migrates before anything that serves changes: the maintenance function alone takes the new version (#63)', () => {
  // Why install.sh does not deploy the new version's stack before migrate: another commit alone (the server's
  // description) makes another version of the server, which the alias live would move to.
  const versionId = (template: Template) => resources(template, 'AWS::Lambda::Version').map(([id]) => id);
  assert.notDeepEqual(versionId(build({ sourceSha: 'b'.repeat(40) }).template), versionId(defaults));
  // What it does instead: the deployed template, with the maintenance function as the new version makes it. Here the
  // new version also goes onto a restored instance (UAT-DEP-07's newer-schema branch): another image, commit and database.
  const next = props.imageUri.replace(DIGEST, `sha256:${'2'.repeat(64)}`);
  const restored = { ...props.database, endpoint: 'restored.abcdefghijkl.us-west-2.rds.amazonaws.com', resourceId: 'db-RESTOREDRESTOREDRESTORED00' };
  const synthesized = build({ sourceSha: 'b'.repeat(40), imageUri: next, database: restored }).template.toJSON();
  const deployed = defaults.toJSON();
  const changed = maintenanceFirst(deployed, synthesized);
  const [maintenanceId, maintenance] = fn(defaults, 'colvenna-catalog-uat-maintenance');
  const policyId = resources(defaults, 'AWS::IAM::Policy')
    .filter(([, p]) => p.Properties.Roles.some((r: any) => r.Ref === maintenance.Properties.Role['Fn::GetAtt'][0])).map(([id]) => id);
  assert.equal(policyId.length, 1);
  const where = differences(deployed, changed);
  assert.ok(where.length > 0);
  for (const at of where) {
    assert.ok(at.startsWith(`/Resources/${maintenanceId}/Properties/`) || at.startsWith(`/Resources/${policyId[0]}/Properties/PolicyDocument/`)
      || at === '/Outputs/MaintenanceImage/Value', at);
  }
  // It migrates the database the new configuration names, signing in to it.
  assert.equal(flat(changed.Resources[maintenanceId].Properties), flat(fn(Template.fromJSON(synthesized), 'colvenna-catalog-uat-maintenance')[1].Properties));
  assert.match(flat(changed.Resources[policyId[0]].Properties.PolicyDocument), /db-RESTOREDRESTOREDRESTORED00/);
  assert.equal(changed.Outputs.MaintenanceImage.Value, next);
  // So the server function, its one version and the alias live, the authorizer and the ops and purge functions are as
  // deployed: CloudFormation publishes no version and moves no alias.
  for (const name of ['server', 'authorizer', 'ops', 'purge']) {
    const [id] = fn(defaults, `colvenna-catalog-uat-${name}`);
    assert.deepEqual(changed.Resources[id], deployed.Resources[id], name);
  }
  for (const type of ['AWS::Lambda::Version', 'AWS::Lambda::Alias']) {
    const ids = resources(defaults, type).map(([id]) => id);
    assert.deepEqual(Object.keys(changed.Resources).filter((id) => changed.Resources[id].Type === type), ids, type);
    for (const id of ids) {
      assert.deepEqual(changed.Resources[id], deployed.Resources[id], id);
    }
  }
});

test('what the gateway answers itself is retryable: a database the authorizer cannot reach, a request over the concurrency', () => {
  const responses = resources(defaults, 'AWS::ApiGateway::GatewayResponse').map(([, r]) => [r.Properties.ResponseType, r.Properties.StatusCode]);
  assert.deepEqual(responses.sort(), [
    ['AUTHORIZER_CONFIGURATION_ERROR', '503'],
    ['AUTHORIZER_FAILURE', '503'],
    // A throttled invocation of the server, and a function that failed (#80): never 500 or 502, which
    // Paimon's client does not retry.
    ['INTEGRATION_FAILURE', '503'],
    // The stage's own throttle.
    ['THROTTLED', '429'],
  ]);
  // Not the catch-all, which would change every other 5XX; not the gateway's timeout, whose request may
  // still be running.
  for (const [, r] of resources(defaults, 'AWS::ApiGateway::GatewayResponse')) {
    assert.ok(!['DEFAULT_5XX', 'INTEGRATION_TIMEOUT'].includes(r.Properties.ResponseType), r.Properties.ResponseType);
  }
});

test('the stage throttles at 8 x the reserved concurrency a second, bursts of the concurrency (#80)', () => {
  // Little's law at the sized-for mean of 125 ms: 10 environments serve 80 requests a second.
  assert.equal(API_REQUEST_SECONDS, 0.125);
  assert.deepEqual(apiThrottle(10), { rateLimit: 80, burstLimit: 10 });
  assert.deepEqual(apiThrottle(1), { rateLimit: 8, burstLimit: 1 });
  assert.deepEqual(apiThrottle(250), { rateLimit: 2000, burstLimit: 250 });
  for (const [template, concurrency] of [[defaults, 10], [build({ reservedConcurrency: 25 }).template, 25]] as const) {
    const [[, stage]] = resources(template, 'AWS::ApiGateway::Stage');
    const settings = stage.Properties.MethodSettings;
    assert.equal(settings.length, 1, 'one setting for every method and path');
    assert.deepEqual([settings[0].HttpMethod, settings[0].ResourcePath], ['*', '/*']);
    assert.deepEqual([settings[0].ThrottlingRateLimit, settings[0].ThrottlingBurstLimit],
      concurrency === 10 ? [80, 10] : [200, 25]);
    assert.equal(settings[0].LoggingLevel, 'ERROR');
  }
});

test('the stage is "catalog", with access logs; the account\'s API Gateway settings are left alone', () => {
  const [[, stage]] = resources(defaults, 'AWS::ApiGateway::Stage');
  assert.equal(stage.Properties.StageName, 'catalog');
  assert.ok(stage.Properties.AccessLogSetting.DestinationArn);
  assert.match(stage.Properties.AccessLogSetting.Format, /\$context\.identity\.vpceId/);
  // Which gateway response answered: a throttled invocation's is told from a server's own 503 (#80).
  assert.match(stage.Properties.AccessLogSetting.Format, /\$context\.error\.responseType/);
  assert.deepEqual(defaults.findResources('AWS::ApiGateway::Account'), {});
});

test('without per-table credentials the server gets none of their settings', () => {
  const { template } = build({ dataTokens: { mode: 'disabled', duration: 'PT3H' } });
  const env = environment(template, 'colvenna-catalog-uat-server');
  assert.equal(env.COLVENNA_DATA_TOKEN_MODE, 'disabled');
  assert.equal(env.COLVENNA_DATA_TOKEN_ROLE_ARN, undefined);
  assert.equal(env.COLVENNA_DATA_TOKEN_WEB_IDENTITY, undefined);
});

test('outputs what install.sh reads', () => {
  const outputs = Object.keys(defaults.findOutputs('*'));
  for (const key of ['ApiId', 'ApiUrl', 'ApiEndpointUrl', 'ServerFunctionName', 'ServerVersion', 'ServerImage', 'MaintenanceImage', 'MaintenanceFunctionName',
    'PurgeFunctionName', 'PurgeScheduleName', 'PurgeAlarmName', 'PurgeBacklogAlarmName', 'AuthorizerFunctionName', 'OpsFunctionName', 'AdminTokenSecretArn',
    'DataRoleArn', 'IdentityPoolId', 'WarehouseName', 'WarehouseRoot', 'DatabaseEndpoint', 'SourceSha']) {
    assert.ok(outputs.includes(key), key);
  }
});

test('names the database the functions run on, which install.sh holds COLVENNA_DB to', () => {
  assert.equal(defaults.findOutputs('*').DatabaseEndpoint.Value, 'db.example.com');
  const { template } = build({ database: { ...props.database, endpoint: 'restored.abcdefghijkl.us-west-2.rds.amazonaws.com' } });
  assert.equal(template.findOutputs('*').DatabaseEndpoint.Value, 'restored.abcdefghijkl.us-west-2.rds.amazonaws.com');
});

test('passes cdk-nag', () => {
  for (const overrides of [{}, { snapStart: true }, { dataTokens: { mode: 'disabled' as const, duration: 'PT3H' } }]) {
    const { stack } = build(overrides);
    nagChecks(stack);
    assert.deepEqual(unacknowledged(stack), [], JSON.stringify(overrides));
  }
});

test('rejects malformed inputs', () => {
  assert.throws(() => build({ imageUri: props.imageUri.replace(`@${DIGEST}`, ':latest') }), /-c imageUri/);
  assert.throws(() => build({ imageUri: props.imageUri.replace('111122223333', '444455556666') }), /-c imageUri/);
  assert.throws(() => build({ imageUri: props.imageUri.replace('us-west-2', 'us-east-2') }), /-c imageUri/);
  assert.throws(() => build({ apiEndpointIds: [] }), /-c apiEndpointIds/);
  assert.throws(() => build({ apiEndpointIds: ['vpce-0123456789abcdef0', 'vpce-0123456789abcdef0'] }), /-c apiEndpointIds/);
  assert.throws(() => build({ securityGroupId: 'sg-1' }), /-c clientSecurityGroupId/);
  assert.throws(() => build({ subnetZones: ['us-west-2a'] }), /-c subnetZones/);
  assert.throws(() => build({ memoryMb: 512 }), /-c memory/);
  assert.throws(() => build({ reservedConcurrency: 0 }), /-c concurrency/);
  assert.throws(() => build({ dataTokens: { mode: 'aws-sts', duration: 'PT1H' } }), /-c dataTokenDuration/);
  assert.throws(() => build({ database: { ...props.database, user: 'u'.repeat(60) } }), /-c databaseUser/);
  assert.throws(() => build({ database: { ...props.database, endpoint: 'db_1.example.com' } }), /-c databaseEndpoint/);
  assert.throws(() => build({ warehouseName: 'Colvenna' }), /-c warehouseName/);
  assert.throws(() => build({ database: { ...props.database, resourceId: 'db-abc' } }), /-c databaseResourceId/);
});
