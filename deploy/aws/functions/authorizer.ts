/**
 * The REQUEST authorizer of the serverless form's private REST API (#13). It checks the bearer token
 * as the server does (TokenService.authenticate): the Authorization header must start with exactly
 * "Bearer ", the rest (trimmed as Java trims) must not be empty, and its SHA-256 must be a token of
 * api_tokens that is neither revoked nor expired, of a principal that is not disabled. Anything else
 * is refused with 401 before the server is invoked; the server then authenticates every request again
 * itself, so this is a gate in front of it, never the only check. It reads the database as a role that
 * may select only those columns, signing in with an RDS IAM token (no password). A database it cannot
 * reach fails the authorization, which the API answers with a retryable 503.
 */
import { createHash } from 'node:crypto';
import type { APIGatewayAuthorizerResult, APIGatewayRequestAuthorizerEvent } from 'aws-lambda';
import type { Client } from 'pg';
import { connect, iamConnection, setting } from './database';

const LOOKUP = 'SELECT p.name FROM api_tokens t JOIN principals p ON p.name = t.principal'
  + ' WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND t.expires_at > now() AND p.disabled_at IS NULL';

/** The principal whose valid token hashes so, if any. */
export type Lookup = (hash: Buffer) => Promise<string | undefined>;

/** The bearer token by the server's rule, or undefined when there is none. */
export function bearerToken(headers: Record<string, string | undefined> | null | undefined): string | undefined {
  const header = Object.entries(headers ?? {}).find(([name]) => name.toLowerCase() === 'authorization')?.[1];
  if (header === undefined || !header.startsWith('Bearer ')) {
    return undefined;
  }
  // String.trim() in Java removes the characters up to U+0020 at either end, and nothing else.
  const token = header.slice('Bearer '.length).replace(/^[\u0000- ]+|[\u0000- ]+$/g, '');
  return token === '' ? undefined : token;
}

/**
 * Every method and path of the request's stage: API Gateway caches the decision by the Authorization
 * header, so the next request with the same token, to any route, is decided by it too.
 */
export function stageResource(methodArn: string): string {
  // arn:aws:execute-api:<region>:<account>:<api>/<stage>/<METHOD>/<path...>
  const [api, stage] = methodArn.split('/');
  if (!api?.startsWith('arn:') || !stage) {
    throw new Error('Not a method ARN');
  }
  return `${api}/${stage}/*/*`;
}

/** The decision: an Allow for the stage, or the 'Unauthorized' error API Gateway answers 401 for. */
export async function decide(event: APIGatewayRequestAuthorizerEvent, lookup: Lookup): Promise<APIGatewayAuthorizerResult> {
  const token = bearerToken(event.headers);
  if (token === undefined) {
    throw new Error('Unauthorized');
  }
  const principal = await lookup(createHash('sha256').update(token, 'utf8').digest());
  if (principal === undefined) {
    throw new Error('Unauthorized');
  }
  return {
    principalId: principal,
    policyDocument: {
      Version: '2012-10-17',
      Statement: [{ Action: 'execute-api:Invoke', Effect: 'Allow', Resource: stageResource(event.methodArn) }],
    },
    context: { principal },
  };
}

let client: Client | undefined;

/** The lookup on one connection per execution environment, made again once if it broke. */
async function lookup(hash: Buffer): Promise<string | undefined> {
  for (let attempt = 1; ; attempt++) {
    try {
      if (client === undefined) {
        // A new connection, so a new token: one lasts 15 minutes, a connection longer.
        client = await connect(await iamConnection(setting('DB_USER'), setting('DB_NAME')));
        client.on('error', () => {
          client = undefined;
        });
      }
      const result = await client.query<{ name: string }>(LOOKUP, [hash]);
      return result.rows[0]?.name;
    } catch (e) {
      client?.end().catch(() => undefined);
      client = undefined;
      if (attempt >= 2) {
        // Not "Unauthorized": the API answers this with a retryable 503, not a 401.
        throw new Error(`token lookup failed: ${(e as Error).message}`);
      }
    }
  }
}

export const handler = (event: APIGatewayRequestAuthorizerEvent): Promise<APIGatewayAuthorizerResult> => decide(event, lookup);
