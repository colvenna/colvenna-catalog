import assert from 'node:assert/strict';
import test from 'node:test';
import { App, Aspects, CfnResource, Fn, Stack } from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { buildApp } from '../src/app';
import { ForbiddenResources, GuardOptions, forbidResources } from '../src/guard';

const FUNCTION = 'arn:aws:lambda:us-west-2:111122223333:function:f';
const SOURCE = 'arn:aws:execute-api:us-west-2:111122223333:api/stage/*/*';
const PRIVATE_POLICY = {
  Version: '2012-10-17',
  Statement: [
    { Effect: 'Allow', Principal: '*', Action: 'execute-api:Invoke', Resource: 'execute-api:/*',
      Condition: { StringEquals: { 'aws:SourceVpce': 'vpce-0123456789abcdef0' } } },
    { Effect: 'Deny', Principal: '*', Action: 'execute-api:Invoke', Resource: 'execute-api:/*',
      Condition: { StringNotEquals: { 'aws:SourceVpce': 'vpce-0123456789abcdef0' } } },
  ],
};

/** The guard's errors on a stack holding what `add` makes, by construct path. */
function refused(add: (stack: Stack) => void, options: GuardOptions = {}): string[] {
  const stack = new Stack(new App(), 'Probe');
  add(stack);
  forbidResources(stack, options);
  return Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('is not allowed')).map((e) => e.id).sort();
}

test('synthesis refuses a function URL, an IAM user and an access key', () => {
  assert.deepEqual(refused((stack) => {
    new lambda.CfnUrl(stack, 'Url', { targetFunctionArn: FUNCTION, authType: 'AWS_IAM' });
    const user = new iam.CfnUser(stack, 'User');
    new iam.CfnAccessKey(stack, 'Key', { userName: user.ref });
  }), ['/Probe/Key', '/Probe/Url', '/Probe/User']);
});

test('a function URL made by an L2 function, or by a raw resource, is refused', () => {
  assert.deepEqual(refused((stack) => {
    const fn = new lambda.Function(stack, 'Fn', {
      runtime: lambda.Runtime.NODEJS_24_X, handler: 'index.handler', code: lambda.Code.fromInline('exports.handler=()=>1'),
    });
    fn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    new CfnResource(stack, 'Raw', { type: 'AWS::Lambda::Url', properties: { TargetFunctionArn: FUNCTION, AuthType: 'AWS_IAM' } });
  }), ['/Probe/Fn/FunctionUrl/Resource', '/Probe/Fn/invoke-function', '/Probe/Fn/invoke-function-url', '/Probe/Raw']);
});

test('a function only API Gateway may invoke, for an API of this stack', () => {
  assert.deepEqual(refused((stack) => {
    const api = new apigw.CfnRestApi(stack, 'Api', { endpointConfiguration: { types: ['PRIVATE'] }, policy: PRIVATE_POLICY });
    const ours = Fn.join('', ['arn:aws:execute-api:us-west-2:111122223333:', api.ref, '/catalog/*/*']);
    const permission = (id: string, props: Partial<lambda.CfnPermissionProps>) =>
      new lambda.CfnPermission(stack, id, { functionName: FUNCTION, action: 'lambda:InvokeFunction', principal: 'apigateway.amazonaws.com', ...props });
    permission('Ok', { sourceArn: ours });
    permission('Anyone', { principal: '*', sourceArn: ours });
    permission('Account', { principal: '444455556666', sourceArn: ours });
    permission('AnySource', {});
    permission('UrlAuth', { principal: '*', functionUrlAuthType: 'NONE' });
    permission('UrlAction', { action: 'lambda:InvokeFunctionUrl', sourceArn: ours });
    // A service and a source the old check took: any bucket's events, and an API of another stack.
    permission('AnyBucket', { sourceArn: 'arn:aws:s3:::*' });
    permission('S3', { principal: 's3.amazonaws.com', sourceArn: 'arn:aws:s3:::*' });
    permission('OtherApi', { sourceArn: SOURCE });
  }), ['/Probe/Account', '/Probe/AnyBucket', '/Probe/AnySource', '/Probe/Anyone', '/Probe/OtherApi', '/Probe/S3',
    '/Probe/UrlAction', '/Probe/UrlAuth']);
});

test('a resource policy must allow nothing but its VPC endpoints, and deny everything else', () => {
  const statement = PRIVATE_POLICY.Statement;
  const api = (stack: Stack, id: string, statements: unknown[]) =>
    new apigw.CfnRestApi(stack, id, { endpointConfiguration: { types: ['PRIVATE'] }, policy: { Version: '2012-10-17', Statement: statements } });
  assert.deepEqual(refused((stack) => {
    api(stack, 'Ok', statement);
    // Anyone, with a Deny on one path only: the old guard took it (it had a Deny and a StringNotEquals).
    api(stack, 'AllowAll', [
      { Effect: 'Allow', Principal: '*', Action: 'execute-api:Invoke', Resource: 'execute-api:/*' },
      { ...statement[1], Resource: 'execute-api:/catalog/GET/admin' },
    ]);
    api(stack, 'ExtraAllow', [...statement, { Effect: 'Allow', Principal: '*', Action: 'execute-api:Invoke', Resource: 'execute-api:/*/GET/*' }]);
    api(stack, 'DenyOnePath', [statement[0], { ...statement[1], Resource: 'execute-api:/catalog/*' }]);
    api(stack, 'NoStatements', []);
  }), ['/Probe/AllowAll', '/Probe/DenyOnePath', '/Probe/ExtraAllow', '/Probe/NoStatements']);
});

test('a REST API defined by an OpenAPI document is refused: its methods would go unseen', () => {
  const open = {
    openapi: '3.0.1', info: { title: 'x', version: '1' },
    paths: { '/open': { get: { 'x-amazon-apigateway-integration': { type: 'mock' } } } },
  };
  assert.deepEqual(refused((stack) => {
    new apigw.CfnRestApi(stack, 'Body', { endpointConfiguration: { types: ['PRIVATE'] }, policy: PRIVATE_POLICY, body: open });
    new apigw.CfnRestApi(stack, 'S3Body', {
      endpointConfiguration: { types: ['PRIVATE'] }, policy: PRIVATE_POLICY, bodyS3Location: { bucket: 'b', key: 'openapi.json' },
    });
  }), ['/Probe/Body', '/Probe/S3Body']);
});

test('a REST API that is not private, or private without the policy keeping it to its endpoints, is refused', () => {
  assert.deepEqual(refused((stack) => {
    new apigw.CfnRestApi(stack, 'Ok', { endpointConfiguration: { types: ['PRIVATE'] }, policy: PRIVATE_POLICY });
    new apigw.CfnRestApi(stack, 'Edge', { policy: PRIVATE_POLICY });
    new apigw.CfnRestApi(stack, 'Regional', { endpointConfiguration: { types: ['REGIONAL'] }, policy: PRIVATE_POLICY });
    new apigw.CfnRestApi(stack, 'NoPolicy', { endpointConfiguration: { types: ['PRIVATE'] } });
    new apigw.CfnRestApi(stack, 'AllowOnly', {
      endpointConfiguration: { types: ['PRIVATE'] },
      policy: { ...PRIVATE_POLICY, Statement: [PRIVATE_POLICY.Statement[0]] },
    });
  }), ['/Probe/AllowOnly', '/Probe/Edge', '/Probe/NoPolicy', '/Probe/Regional']);
});

test('a method without authorization, and any HTTP or WebSocket API, are refused', () => {
  assert.deepEqual(refused((stack) => {
    const method = (id: string, authorizationType?: string) => new apigw.CfnMethod(stack, id, {
      restApiId: 'a', resourceId: 'r', httpMethod: 'GET', ...(authorizationType ? { authorizationType } : {}),
    });
    method('Custom', 'CUSTOM');
    method('Iam', 'AWS_IAM');
    method('None', 'NONE');
    method('Unsaid');
    new apigwv2.CfnApi(stack, 'Http', { name: 'h', protocolType: 'HTTP' });
  }), ['/Probe/Http', '/Probe/None', '/Probe/Unsaid']);
});

test('an identity pool with unauthenticated identities, the classic flow, other providers or a role is refused', () => {
  assert.deepEqual(refused((stack) => {
    const pool = (id: string, props: Partial<cognito.CfnIdentityPoolProps>) => new cognito.CfnIdentityPool(stack, id, {
      allowUnauthenticatedIdentities: false, developerProviderName: 'p', ...props,
    });
    pool('Ok', {});
    pool('Unauthenticated', { allowUnauthenticatedIdentities: true });
    new CfnResource(stack, 'Unsaid', { type: 'AWS::Cognito::IdentityPool', properties: { DeveloperProviderName: 'p' } });
    pool('Classic', { allowClassicFlow: true });
    pool('Login', { supportedLoginProviders: { 'accounts.google.com': 'x' } });
    pool('Oidc', { openIdConnectProviderArns: ['arn:aws:iam::111122223333:oidc-provider/x'] });
    new cognito.CfnIdentityPoolRoleAttachment(stack, 'Roles', { identityPoolId: 'p', roles: { authenticated: 'arn:aws:iam::1:role/r' } });
  }), ['/Probe/Classic', '/Probe/Login', '/Probe/Oidc', '/Probe/Roles', '/Probe/Unauthenticated', '/Probe/Unsaid']);
});

test('a load balancer facing the internet is refused', () => {
  assert.deepEqual(refused((stack) => {
    new elbv2.CfnLoadBalancer(stack, 'Internal', { scheme: 'internal', type: 'network' });
    new elbv2.CfnLoadBalancer(stack, 'Public', { scheme: 'internet-facing', type: 'network' });
    new elbv2.CfnLoadBalancer(stack, 'Unsaid', { type: 'application' });
  }), ['/Probe/Public', '/Probe/Unsaid']);
});

test("the product's stacks also refuse internet gateways, NAT gateways and Elastic IPs; the test environment may have them", () => {
  const internet = (stack: Stack) => {
    new ec2.CfnInternetGateway(stack, 'Igw');
    new ec2.CfnEIP(stack, 'Eip');
    new ec2.CfnNatGateway(stack, 'Nat', { subnetId: 'subnet-0123abcd' });
  };
  assert.deepEqual(refused(internet, { noInternetPaths: true }), ['/Probe/Eip', '/Probe/Igw', '/Probe/Nat']);
  assert.deepEqual(refused(internet), []);
});

test('every stack the app builds carries the guard, and none holds a forbidden resource', () => {
  const common = { account: '111122223333', region: 'us-west-2', sourceSha: 'a'.repeat(40) };
  const network = { vpcId: 'vpc-0123456789abcdef0', subnetIds: 'subnet-0123456789abcdef0,subnet-0123456789abcdef1', subnetZones: 'us-west-2a,us-west-2b' };
  for (const context of [
    { ...common, stack: 'release' },
    { ...common, stack: 'storage', env: 'uat', createBucket: 'true', createDatabase: 'false' },
    { ...common, stack: 'network', env: 'uat', networkMode: 'create', vpcCidr: '10.80.0.0/16', subnetZones: 'us-west-2a,us-west-2b', databasePort: '5432' },
    { ...common, stack: 'images', env: 'uat' },
    {
      ...common, ...network, stack: 'serverless', env: 'uat', securityGroupId: 'sg-0123456789abcdef0', apiEndpointIds: 'vpce-0123456789abcdef0',
      imageUri: `111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog@sha256:${'1'.repeat(64)}`,
      bucket: 'bucket', warehousePrefix: 'warehouse', warehouseName: 'colvenna', databaseEndpoint: 'db.example.com', databasePort: '5432',
      databaseName: 'colvenna_catalog', databaseUser: 'colvenna_catalog', databaseResourceId: 'db-ABCDEFGHIJKLMNOPQRSTUVWXYZ', dataTokens: 'aws-sts', dataTokenDuration: 'PT3H',  // pragma: allowlist secret - a made-up RDS resource id
      memory: '2048', concurrency: '10', snapStart: 'false',
    },
  ]) {
    const stacks = buildApp(context).node.children.filter((c): c is Stack => c instanceof Stack);
    assert.equal(stacks.length, 1);
    assert.ok(Aspects.of(stacks[0]).all.some((a) => a instanceof ForbiddenResources), context.stack);
    const template = Template.fromStack(stacks[0]);
    for (const type of ['AWS::Lambda::Url', 'AWS::IAM::User', 'AWS::IAM::AccessKey', 'AWS::ApiGatewayV2::Api', 'AWS::EC2::NatGateway']) {
      assert.deepEqual(template.findResources(type), {}, `${context.stack}: ${type}`);
    }
    assert.deepEqual(Annotations.fromStack(stacks[0]).findError('*', Match.stringLikeRegexp('is not allowed')), [], context.stack);
  }
});
