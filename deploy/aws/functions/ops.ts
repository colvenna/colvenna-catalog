/**
 * The serverless form's ops function (#13): what install.sh needs done from inside the catalog's VPC,
 * where the database and the private API are, and it is not. It is reached only by an
 * IAM-authenticated Invoke (no API, no Function URL, no trigger), and it keeps no secret: whatever a
 * step needs (the admin's password, a bearer token) comes in that step's event and is never logged or
 * returned.
 *   {"action": "database", ...}  as the database's admin user: the catalog's user and database, and
 *                                the authorizer's read-only user, made or brought in line (as the EKS
 *                                form's psql pod does). Neither has a password: both sign in with RDS
 *                                IAM tokens (rds_iam) only. Both users' idle sessions end after 5 minutes
 *   {"action": "grants"}         as the catalog's user (an IAM token), after migrate: the authorizer's
 *                                user may read the token and principal columns it checks, no more
 *   {"action": "request", ...}   one HTTPS request to the private API through its VPC endpoint
 */
import { request as https } from 'node:https';
import { ClientConfig } from 'pg';
import { connect, connection, iamConnection, setting } from './database';

export interface DatabaseEvent {
  action: 'database';
  admin: { username: string; password: string };
}

export interface GrantsEvent {
  action: 'grants';
}

export interface RequestEvent {
  action: 'request';
  method: string;
  /** The path below the API's stage, such as /v1/config?warehouse=colvenna. */
  path: string;
  token?: string;
  body?: unknown;
}

export type OpsEvent = DatabaseEvent | GrantsEvent | RequestEvent;

/** Frozen and retired execution environments keep their connections; the database ends them so. */
export const IDLE_SESSION_TIMEOUT = '300s';

/**
 * The statements of the database step, each a format() the database runs and whose text it then
 * runs: names are quoted as identifiers and passwords as literals by PostgreSQL itself. [$1, $2] are
 * the arguments of the format().
 */
export function databaseStatements(database: string, user: string, authorizer: string): [string, string[]][] {
  const role = (name: string): [string, string[]][] => [
    ['SELECT format(\'CREATE ROLE %I LOGIN\', $1::text) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)', [name]],
    // No password: one left from an earlier install could still sign in, which IAM tokens make needless.
    ['SELECT format(\'ALTER ROLE %I WITH LOGIN PASSWORD NULL\', $1::text)', [name]],
    ['SELECT format(\'GRANT rds_iam TO %I\', $1::text)', [name]],
    ['SELECT format(\'ALTER ROLE %I SET idle_session_timeout = %L\', $1::text, $2::text)', [name, IDLE_SESSION_TIMEOUT]],
  ];
  return [
    ...role(user),
    ...role(authorizer),
    ['SELECT format(\'CREATE DATABASE %I OWNER %I\', $1::text, $2::text)'
      + ' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)', [database, user]],
    ['SELECT format(\'REVOKE ALL ON DATABASE %I FROM PUBLIC\', $1::text)', [database]],
    ['SELECT format(\'GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I\', $1::text, $2::text)', [database, user]],
    ['SELECT format(\'GRANT CONNECT ON DATABASE %I TO %I\', $1::text, $2::text)', [database, authorizer]],
  ];
}

/** The grants step: the authorizer's user reads what TokenService.authenticate reads, no more. */
export function grantStatements(authorizer: string): [string, string[]][] {
  return [
    ['SELECT format(\'GRANT USAGE ON SCHEMA public TO %I\', $1::text)', [authorizer]],
    ['SELECT format(\'GRANT SELECT (token_hash, principal, expires_at, revoked_at) ON api_tokens TO %I\', $1::text)',
      [authorizer]],
    ['SELECT format(\'GRANT SELECT (name, disabled_at) ON principals TO %I\', $1::text)', [authorizer]],
  ];
}

/** Runs each format() and then the statement it made, on one connection. */
async function run(settings: ClientConfig, statements: [string, string[]][]): Promise<number> {
  const client = await connect(settings);
  let ran = 0;
  try {
    for (const [format, args] of statements) {
      const made = await client.query<{ format: string }>(format, args);
      for (const row of made.rows) {
        await client.query(row.format);
        ran++;
      }
    }
  } finally {
    await client.end();
  }
  return ran;
}

const METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'DELETE']);
const MAX_BODY = 1024 * 1024;

/** The request step's URL, if its path is one: absolute, without "..", whitespace or a scheme. */
export function requestUrl(base: string, path: string): URL {
  if (!/^\/[^\s]*$/.test(path) || path.split(/[/?]/).includes('..') || path.startsWith('//')) {
    throw new Error('request: path must be an absolute path below the API\'s stage');
  }
  return new URL(base.replace(/\/$/, '') + path);
}

async function send(event: RequestEvent): Promise<{ status: number; requestId: string | null; body: string }> {
  const method = String(event.method).toUpperCase();
  if (!METHODS.has(method)) {
    throw new Error(`request: method ${method} is not one of ${[...METHODS].join(', ')}`);
  }
  const url = requestUrl(setting('API_URL'), event.path);
  const body = event.body === undefined ? undefined : Buffer.from(JSON.stringify(event.body));
  const headers: Record<string, string> = {};
  if (event.token !== undefined) {
    headers.Authorization = `Bearer ${event.token}`;
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    headers['Content-Length'] = String(body.length);
  }
  return new Promise((resolve, reject) => {
    const req = https(url, { method, headers, timeout: 30000 }, (res) => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size <= MAX_BODY) {
          chunks.push(chunk);
        }
      });
      res.on('end', () => resolve({
        status: res.statusCode ?? 0,
        requestId: (res.headers['x-request-id'] as string | undefined) ?? (res.headers['x-amzn-requestid'] as string | undefined) ?? null,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
      res.on('error', reject);
    });
    req.on('timeout', () => req.destroy(new Error('request: no answer in 30 seconds')));
    req.on('error', reject);
    req.end(body);
  });
}

export async function handler(event: OpsEvent): Promise<unknown> {
  switch (event?.action) {
    case 'database':
      return {
        statements: await run(connection(event.admin.username, event.admin.password, 'postgres'),
          databaseStatements(setting('DB_NAME'), setting('DB_USER'), setting('DB_AUTHORIZER_USER'))),
      };
    case 'grants':
      return {
        statements: await run(await iamConnection(setting('DB_USER'), setting('DB_NAME')),
          grantStatements(setting('DB_AUTHORIZER_USER'))),
      };
    case 'request':
      return send(event);
    default:
      throw new Error('action: database, grants or request');
  }
}
