import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import type { APIGatewayRequestAuthorizerEvent } from 'aws-lambda';
import { bearerToken, decide, stageResource } from '../functions/authorizer';
import { iamConnection } from '../functions/database';
import { IDLE_SESSION_TIMEOUT, databaseStatements, grantStatements, handler as ops, requestUrl } from '../functions/ops';

const METHOD_ARN = 'arn:aws:execute-api:us-west-2:111122223333:abcdef1234/catalog/GET/v1/colvenna/databases';
const TOKEN = 'colvenna_0123456789abcdefABCDEF-_xyz';  // pragma: allowlist secret - a made-up catalog token, which no server issued
const event = (headers: Record<string, string> | null): APIGatewayRequestAuthorizerEvent =>
  ({ type: 'REQUEST', methodArn: METHOD_ARN, headers } as unknown as APIGatewayRequestAuthorizerEvent);

test('bearerToken: the server\'s rule, "Bearer " exactly, then a token trimmed as Java trims', () => {
  assert.equal(bearerToken({ Authorization: `Bearer ${TOKEN}` }), TOKEN);
  assert.equal(bearerToken({ authorization: `Bearer ${TOKEN}` }), TOKEN, 'header names are not case-sensitive');
  assert.equal(bearerToken({ Authorization: `Bearer   ${TOKEN}\t ` }), TOKEN);
  // Java's trim() keeps what is above U+0020, so a no-break space stays part of the token, which then matches none.
  assert.equal(bearerToken({ Authorization: `Bearer ${TOKEN} ` }), `${TOKEN} `);
  for (const header of ['', 'Bearer', 'Bearer ', 'Bearer    ', `bearer ${TOKEN}`, `BEARER ${TOKEN}`, `Basic ${TOKEN}`, `Bearer\t${TOKEN}`, TOKEN]) {
    assert.equal(bearerToken({ Authorization: header }), undefined, JSON.stringify(header));
  }
  assert.equal(bearerToken({}), undefined);
  assert.equal(bearerToken(null), undefined);
});

test('stageResource: every method and path of the request\'s stage', () => {
  assert.equal(stageResource(METHOD_ARN), 'arn:aws:execute-api:us-west-2:111122223333:abcdef1234/catalog/*/*');
  assert.throws(() => stageResource('catalog/GET/x'), /method ARN/);
});

test('decide: an Allow for the token\'s principal; anything else is the Unauthorized that API Gateway answers 401', async () => {
  const hashes: Buffer[] = [];
  const lookup = async (hash: Buffer) => {
    hashes.push(hash);
    return hash.equals(createHash('sha256').update(TOKEN, 'utf8').digest()) ? 'etl' : undefined;
  };
  const allowed = await decide(event({ Authorization: `Bearer ${TOKEN}` }), lookup);
  assert.equal(allowed.principalId, 'etl');
  assert.deepEqual(allowed.policyDocument.Statement, [{
    Action: 'execute-api:Invoke', Effect: 'Allow', Resource: 'arn:aws:execute-api:us-west-2:111122223333:abcdef1234/catalog/*/*',
  }]);
  // The SHA-256 of the token's UTF-8 bytes, as TokenService.hash.
  assert.equal(hashes[0].toString('hex'), createHash('sha256').update(Buffer.from(TOKEN, 'utf8')).digest('hex'));
  await assert.rejects(decide(event({ Authorization: `Bearer ${TOKEN}x` }), lookup), /^Error: Unauthorized$/);
  const before = hashes.length;
  for (const headers of [null, {}, { Authorization: 'Bearer ' }, { Authorization: `Basic ${TOKEN}` }] as (Record<string, string> | null)[]) {
    await assert.rejects(decide(event(headers), lookup), /^Error: Unauthorized$/);
  }
  assert.equal(hashes.length, before, 'no lookup without a bearer token');
  await assert.rejects(decide(event({ Authorization: `Bearer ${TOKEN}` }), async () => { throw new Error('connection refused'); }),
    (e: Error) => e.message !== 'Unauthorized', 'a database failure is not a 401');
});

test('the database step: users without passwords, signing in with IAM tokens only', () => {
  const statements = databaseStatements('colvenna_catalog', 'colvenna_catalog', 'colvenna_catalog_authz');
  for (const [format, args] of statements) {
    assert.match(format, /^SELECT format\('[^']*'(, \$\d::text)+\)( WHERE NOT EXISTS \(SELECT 1 FROM pg_(roles|database) WHERE (rolname|datname) = \$1\))?$/);
    // Names are identifiers (%I) and values literals (%L): nothing is spliced into the SQL.
    assert.ok(!args.some((a) => format.includes(a)), format);
  }
  const formats = statements.map(([f]) => f).join('\n');
  assert.match(formats, /CREATE ROLE %I LOGIN'/);
  assert.doesNotMatch(formats, /PASSWORD %L/, 'no password is ever set');
  assert.equal(statements.filter(([f]) => /PASSWORD NULL/.test(f)).length, 2, 'an old password is removed from both users');
  assert.deepEqual(statements.filter(([f]) => /GRANT rds_iam TO %I/.test(f)).map(([, a]) => a[0]),
    ['colvenna_catalog', 'colvenna_catalog_authz']);
  assert.match(formats, /CREATE DATABASE %I OWNER %I/);
  assert.match(formats, /REVOKE ALL ON DATABASE %I FROM PUBLIC/);
  assert.deepEqual(statements.filter(([f]) => /idle_session_timeout/.test(f)).map(([, a]) => a[1]),
    [IDLE_SESSION_TIMEOUT, IDLE_SESSION_TIMEOUT]);
});

test('an IAM connection signs in with a token made for its user, over verified TLS', async () => {
  process.env.DB_HOST = 'db.example.com';
  process.env.DB_PORT = '5432';
  process.env.AWS_LAMBDA_FUNCTION_NAME = 'colvenna-catalog-uat-authorizer';
  const signed: string[] = [];
  const settings = await iamConnection('colvenna_catalog_authz', 'colvenna_catalog', async (user) => {
    signed.push(user);
    return `token-for-${user}`;
  });
  assert.deepEqual(signed, ['colvenna_catalog_authz']);
  assert.equal(settings.user, 'colvenna_catalog_authz');
  assert.equal(settings.password, 'token-for-colvenna_catalog_authz');
  assert.equal(settings.database, 'colvenna_catalog');
  assert.deepEqual(settings.ssl, { rejectUnauthorized: true });
});

test('the grants step: the authorizer\'s user reads the columns the token check reads, no more', () => {
  const formats = grantStatements('colvenna_catalog_authz').map(([f]) => f);
  assert.deepEqual(formats.map((f) => /GRANT (.*) TO %I/.exec(f)![1]), [
    'USAGE ON SCHEMA public',
    'SELECT (token_hash, principal, expires_at, revoked_at) ON api_tokens',
    'SELECT (name, disabled_at) ON principals',
  ]);
});

test('the request step: a path below the stage, never another host', () => {
  const base = 'https://abcdef1234-vpce-0123456789abcdef0.execute-api.us-west-2.amazonaws.com/catalog';
  assert.equal(requestUrl(base, '/v1/config?warehouse=colvenna').href, `${base}/v1/config?warehouse=colvenna`);
  for (const path of ['v1/config', '//evil.example.com/x', '/../x', '/v1/../../x', '/a b', 'https://example.com/']) {
    assert.throws(() => requestUrl(base, path), /request: path/, path);
  }
});

test('the ops function refuses an unknown action', async () => {
  await assert.rejects(ops({ action: 'drop' } as never), /action: database, grants or request/);
});
