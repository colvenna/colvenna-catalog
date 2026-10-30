/**
 * How the serverless form's Node.js functions (the authorizer and the ops function) reach the metadata
 * database: the host, port and database name from the function's environment, over TLS with the
 * server's certificate verified and its host name checked, as the server's sslmode=verify-full. The
 * RDS certificate authorities are the ones the Lambda Node.js runtime carries, which
 * NODE_EXTRA_CA_CERTS=/var/runtime/ca-cert.pem adds to Node's own. They sign in with RDS IAM
 * authentication: a token presigned with the function's own credentials for each new connection, so
 * no password is in their environment (the ops function's admin step alone takes one, in its event).
 */
import { Signer } from '@aws-sdk/rds-signer';
import { Client, ClientConfig } from 'pg';

/** One of the function's settings; a function without it is misconfigured. */
export function setting(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === '') {
    throw new Error(`${name} is not set`);
  }
  return value;
}

/** The connection settings for this user and database. */
export function connection(user: string, password: string, database: string): ClientConfig {
  return {
    host: setting('DB_HOST'),
    port: Number(setting('DB_PORT')),
    database,
    user,
    password,
    ssl: { rejectUnauthorized: true },
    connectionTimeoutMillis: 5000,
    query_timeout: 8000,
    statement_timeout: 8000,
    application_name: setting('AWS_LAMBDA_FUNCTION_NAME'),
  };
}

/** A connected client; the caller ends it. */
export async function connect(settings: ClientConfig): Promise<Client> {
  const client = new Client(settings);
  await client.connect();
  return client;
}

/** The connection settings for this user, signing in with a fresh RDS IAM authentication token. */
export async function iamConnection(user: string, database: string,
  sign: (user: string) => Promise<string> = iamToken): Promise<ClientConfig> {
  return connection(user, await sign(user), database);
}

/** A token for the user at DB_HOST:DB_PORT, signed locally with the function's credentials. */
export async function iamToken(user: string): Promise<string> {
  return new Signer({
    hostname: setting('DB_HOST'),
    port: Number(setting('DB_PORT')),
    username: user,
    region: setting('AWS_REGION'),
  }).getAuthToken();
}
