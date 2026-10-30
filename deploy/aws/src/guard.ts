import { Annotations, Aspects, CfnResource, IAspect, Stack } from 'aws-cdk-lib';
import { IConstruct } from 'constructs';

/** Resource types no stack of this project may contain, and why. */
const FORBIDDEN: Record<string, string> = {
  'AWS::Lambda::Url': 'Lambda function URLs are banned; reach a function through an authenticated API Gateway',
  'AWS::IAM::User': 'no long-lived IAM users; identities are roles (IRSA, execution roles)',
  'AWS::IAM::AccessKey': 'no access keys; credentials are short-lived role sessions',
  'AWS::ApiGatewayV2::Api': 'HTTP and WebSocket APIs have no private endpoint; the catalog\'s only entry is a private REST API',
  'AWS::Cognito::IdentityPoolRoleAttachment': 'the identity pool hands out no credentials of its own: '
    + 'the server assumes the data role itself, with a per-table session policy',
};

/** What the product's stacks (src/app.ts) may not have either: a way between their networks and the internet. */
const INTERNET_PATHS: Record<string, string> = {
  'AWS::EC2::InternetGateway': 'the catalog\'s network has no internet gateway',
  'AWS::EC2::EgressOnlyInternetGateway': 'the catalog\'s network has no internet gateway',
  'AWS::EC2::NatGateway': 'the catalog\'s network needs no NAT gateway: VPC endpoints reach the AWS services it uses',
  'AWS::EC2::EIP': 'the catalog has no public address',
};

export interface GuardOptions {
  /** Also refuse internet gateways, NAT gateways and Elastic IPs (the product's stacks; not the test environment's). */
  noInternetPaths?: boolean;
}

/**
 * A property of the resource as the template will have it, overrides included, whichever construct
 * (L1, L2 or a raw CfnResource) made it: what CloudFormation is given is what the guard judges.
 */
function property(node: CfnResource, name: string): unknown {
  const rendered = Stack.of(node).resolve((node as unknown as { _toCloudFormation(): unknown })._toCloudFormation()) as {
    Resources: Record<string, { Properties?: Record<string, unknown> }>;
  };
  return Object.values(rendered.Resources)[0]?.Properties?.[name];
}

/** The logical IDs of the REST APIs in the resource's stack. */
function stackApis(node: CfnResource): string[] {
  const stack = Stack.of(node);
  return stack.node.findAll()
    .filter((c): c is CfnResource => c instanceof CfnResource && c.cfnResourceType === 'AWS::ApiGateway::RestApi')
    .map((api) => stack.resolve(api.logicalId) as string);
}

type Statement = { Effect?: unknown; Resource?: unknown; Condition?: Record<string, Record<string, unknown>> };

/** Why a private REST API's resource policy lets in more than its VPC endpoints, or undefined. */
function policyProblem(policy: unknown): string | undefined {
  const document = policy as { Statement?: Statement | Statement[] } | undefined;
  const statements = document?.Statement === undefined ? [] : [document.Statement].flat();
  if (statements.length === 0) {
    return 'a private REST API without a resource policy';
  }
  const has = (statement: Statement, operator: string) =>
    ['aws:SourceVpce', 'aws:SourceVpc'].some((key) => statement.Condition?.[operator]?.[key] !== undefined);
  // Every Allow is for requests through the given endpoints (or VPC), whatever else it says.
  if (statements.some((statement) => statement.Effect === 'Allow' && !has(statement, 'StringEquals'))) {
    return 'a private REST API whose resource policy allows requests that do not come through its VPC endpoints';
  }
  // And one Deny refuses everything else, on every method and path: not on some of them.
  const everything = (resource: unknown) => [resource].flat().some((r) => r === 'execute-api:/*' || r === '*');
  if (!statements.some((statement) => statement.Effect === 'Deny' && everything(statement.Resource)
      && has(statement, 'StringNotEquals'))) {
    return 'a private REST API without a resource policy that denies every request not through its VPC endpoints';
  }
  return undefined;
}

/** Why the resource is a public or anonymous entry, or undefined when it is not one. */
function publicEntry(node: CfnResource): string | undefined {
  const type = node.cfnResourceType;
  if (type === 'AWS::Lambda::Permission') {
    const principal = property(node, 'Principal');
    if (property(node, 'FunctionUrlAuthType') !== undefined || /InvokeFunctionUrl/.test(String(property(node, 'Action')))) {
      return 'a permission for a function URL';
    }
    // The catalog's functions are invoked by its own private API alone (and by IAM-authenticated
    // callers, who need no permission): API Gateway, for an API of this stack.
    if (principal !== 'apigateway.amazonaws.com') {
      return `invocation by ${JSON.stringify(principal)}: only API Gateway may be granted it, for an API of this stack`;
    }
    const source = JSON.stringify(property(node, 'SourceArn') ?? null);
    if (!source.includes(':execute-api:') || !stackApis(node).some((api) => source.includes(JSON.stringify({ Ref: api })))) {
      return `invocation by API Gateway from ${source}: a permission names an API of this stack as its SourceArn`;
    }
  }
  if (type === 'AWS::ApiGateway::RestApi') {
    if (property(node, 'Body') !== undefined || property(node, 'BodyS3Location') !== undefined) {
      return 'a REST API defined by an OpenAPI document, whose methods the guard cannot see: define its methods as resources';
    }
    const endpoint = property(node, 'EndpointConfiguration') as { Types?: unknown } | undefined;
    if (JSON.stringify(endpoint?.Types) !== '["PRIVATE"]') {
      return `a REST API whose endpoint is ${JSON.stringify(endpoint?.Types ?? ['EDGE'])}, not PRIVATE`;
    }
    const problem = policyProblem(property(node, 'Policy'));
    if (problem !== undefined) {
      return problem;
    }
  }
  if (type === 'AWS::ApiGateway::Method') {
    const authorization = property(node, 'AuthorizationType');
    if (authorization === undefined || authorization === 'NONE') {
      return 'a method without authorization';
    }
  }
  if (type === 'AWS::Cognito::IdentityPool') {
    if (property(node, 'AllowUnauthenticatedIdentities') !== false) {
      return 'an identity pool that does not explicitly refuse unauthenticated identities';
    }
    if (property(node, 'AllowClassicFlow') === true) {
      return 'an identity pool with the classic (basic) flow';
    }
    for (const providers of ['CognitoIdentityProviders', 'SupportedLoginProviders', 'OpenIdConnectProviderARNs', 'SamlProviderARNs']) {
      if (property(node, providers) !== undefined) {
        return `an identity pool with ${providers}: its identities come from the catalog's developer provider alone`;
      }
    }
  }
  if (type === 'AWS::ElasticLoadBalancingV2::LoadBalancer' && property(node, 'Scheme') !== 'internal') {
    return 'a load balancer that is not internal';
  }
  return undefined;
}

/**
 * Fails the synthesis (an error annotation) on any resource of a forbidden type, and on any public or
 * anonymous entry: a function URL or its permission, a function anything but this stack's own REST API
 * may invoke, an HTTP API, a REST API that is not private, is defined by an OpenAPI document, or has a
 * resource policy that allows a request not through its VPC endpoints or does not deny all others, a
 * method without authorization, an identity pool that allows unauthenticated identities or other
 * providers, or a load balancer that faces the internet.
 */
export class ForbiddenResources implements IAspect {
  constructor(private readonly options: GuardOptions = {}) {}

  visit(node: IConstruct): void {
    if (!(node instanceof CfnResource)) {
      return;
    }
    const type = node.cfnResourceType;
    if (type in FORBIDDEN) {
      Annotations.of(node).addError(`${type} is not allowed: ${FORBIDDEN[type]}`);
    } else if (this.options.noInternetPaths && type in INTERNET_PATHS) {
      Annotations.of(node).addError(`${type} is not allowed: ${INTERNET_PATHS[type]}`);
    } else {
      const why = publicEntry(node);
      if (why !== undefined) {
        Annotations.of(node).addError(`${type} is not allowed: ${why}; the catalog has no public or anonymous entry`);
      }
    }
  }
}

/** The guard on everything under the scope; app.ts applies it to every stack it builds. */
export function forbidResources(scope: IConstruct, options: GuardOptions = {}): void {
  Aspects.of(scope).add(new ForbiddenResources(options));
}
