import assert from 'node:assert/strict';
import test from 'node:test';
import {
  account, accountList, bucketName, commitSha, databaseIdentifier, dataTokenDuration, dataTokenMode, envName, flag, hostName,
  identifier, imageUri, integer, k8sName, oidcIssuer, securityGroupId, standardRegion, subnetIds, subnetZones, vpcCidr,
  vpcEndpointIds, vpcId, warehouseName, warehousePrefix,
} from '../src/context';

const ISSUER = 'https://oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF';

test('envName: a lowercase letter, then letters and digits, eight at most; release is reserved', () => {
  assert.equal(envName('uat'), 'uat');
  assert.equal(envName('uat2'), 'uat2');
  for (const bad of ['', 'Uat', '2uat', 'uat-2', 'abcdefghi', 'release', undefined, 7]) {
    assert.throws(() => envName(bad), /^Error: -c env/);
  }
});

test('account: twelve digits', () => {
  assert.equal(account('111122223333'), '111122223333');
  for (const bad of ['11112222333', '1111222233334', 'abc', undefined]) {
    assert.throws(() => account(bad), /^Error: -c account/);
  }
});

test('standardRegion: the aws partition only', () => {
  assert.equal(standardRegion('us-west-2'), 'us-west-2');
  assert.equal(standardRegion('eu-central-1'), 'eu-central-1');
  for (const bad of ['cn-north-1', 'us-gov-west-1', 'us-west', 'us-west-0', 'US-WEST-2', undefined]) {
    assert.throws(() => standardRegion(bad), /^Error: -c region/);
  }
});

test('commitSha: forty lowercase hex digits', () => {
  assert.equal(commitSha('a'.repeat(40)), 'a'.repeat(40));
  for (const bad of ['a'.repeat(39), 'A'.repeat(40), 'g'.repeat(40), undefined]) {
    assert.throws(() => commitSha(bad), /^Error: -c sourceSha/);
  }
});

test('oidcIssuer: the EKS issuer of the same region', () => {
  assert.equal(oidcIssuer(ISSUER, 'us-west-2'), ISSUER);
  for (const bad of [
    ISSUER.replace('us-west-2', 'us-east-2'),
    ISSUER.replace('https://', 'http://'),
    ISSUER.toLowerCase(),
    `${ISSUER}/`,
    undefined,
  ]) {
    assert.throws(() => oidcIssuer(bad, 'us-west-2'), /^Error: -c oidcIssuer/);
  }
});

test('bucketName: S3 bucket naming', () => {
  for (const good of ['bucket', 'my.bucket-1', 'a1b']) {
    assert.equal(bucketName(good), good);
  }
  for (const bad of ['ab', 'Bucket', '-bucket', 'bucket-', 'a'.repeat(64), '192.168.1.1', 'my_bucket', undefined]) {  // redaction: allow - an address, which is no bucket name
    assert.throws(() => bucketName(bad), /^Error: -c bucket/);
  }
});

test('warehousePrefix: segments without leading or trailing slashes', () => {
  assert.equal(warehousePrefix('warehouse'), 'warehouse');
  assert.equal(warehousePrefix('lake/warehouse'), 'lake/warehouse');
  assert.equal(warehousePrefix('my_lake-1'), 'my_lake-1');
  for (const bad of ['warehouse/', '/warehouse', '', 'a//b', 'Ware', 'a'.repeat(65), 'a b', undefined]) {
    assert.throws(() => warehousePrefix(bad), /^Error: -c warehousePrefix/);
  }
});

test('k8sName: a DNS-1123 label', () => {
  assert.equal(k8sName('colvenna-catalog', 'namespace'), 'colvenna-catalog');
  for (const bad of ['', 'Colvenna', '-a', 'a-', 'a_b', 'a'.repeat(64), undefined]) {
    assert.throws(() => k8sName(bad, 'namespace'), /^Error: -c namespace/);
  }
});

test('databaseIdentifier: a PostgreSQL name without quoting', () => {
  assert.equal(databaseIdentifier('colvenna_catalog'), 'colvenna_catalog');
  assert.equal(databaseIdentifier('_a1'), '_a1');
  for (const bad of ['', '1a', 'Colvenna', 'colvenna-catalog', 'a'.repeat(64), undefined]) {
    assert.throws(() => databaseIdentifier(bad), /^Error: -c databaseUser/);
  }
});

test('vpcId, securityGroupId: their prefix and eight or seventeen hex digits', () => {
  assert.equal(vpcId('vpc-0123abcd'), 'vpc-0123abcd');
  assert.equal(vpcId('vpc-0123456789abcdef0'), 'vpc-0123456789abcdef0');
  assert.equal(securityGroupId('sg-0123456789abcdef0'), 'sg-0123456789abcdef0');
  for (const bad of ['vpc-0123', 'vpc-0123456789ABCDEF0', 'subnet-0123abcd', undefined]) {
    assert.throws(() => vpcId(bad), /^Error: -c vpcId/);
  }
  assert.throws(() => securityGroupId('sg-xyz'), /^Error: -c clientSecurityGroupId/);
});

test('subnetIds: two or more distinct subnets', () => {
  assert.deepEqual(subnetIds('subnet-0123abcd,subnet-4567abcd'), ['subnet-0123abcd', 'subnet-4567abcd']);
  for (const bad of ['subnet-0123abcd', 'subnet-0123abcd,subnet-0123abcd', 'subnet-0123abcd,vpc-0123abcd', '', undefined]) {
    assert.throws(() => subnetIds(bad), /^Error: -c subnetIds/);
  }
});

test('accountList: none, or distinct twelve-digit accounts; never a wildcard', () => {
  assert.deepEqual(accountList(undefined), []);
  assert.deepEqual(accountList(''), []);
  assert.deepEqual(accountList('111122223333,444455556666'), ['111122223333', '444455556666']);
  for (const bad of ['*', '1111222233334', '111122223333,111122223333', '111122223333,*']) {
    assert.throws(() => accountList(bad), /^Error: -c pullAccounts/);
  }
});

test('flag: true or false, spelt out', () => {
  assert.equal(flag('true', 'createBucket'), true);
  assert.equal(flag('false', 'createBucket'), false);
  for (const bad of ['yes', '1', 'TRUE', '', undefined]) {
    assert.throws(() => flag(bad, 'createBucket'), /^Error: -c createBucket/);
  }
});

test("subnetZones: one zone of the stack's region per subnet", () => {
  assert.deepEqual(subnetZones('us-west-2a,us-west-2b', 'us-west-2', 2), ['us-west-2a', 'us-west-2b']);
  for (const [bad, count] of [['us-west-2a', 2], ['us-east-2a,us-west-2b', 2], ['us-west-2,us-west-2b', 2], [undefined, 2]] as const) {
    assert.throws(() => subnetZones(bad, 'us-west-2', count), /^Error: -c subnetZones/);
  }
});

test('vpcCidr: an IPv4 network from /16 to /24, without host bits', () => {
  for (const good of ['10.80.0.0/16', '172.31.4.0/22', '192.168.7.0/24']) {
    assert.equal(vpcCidr(good), good);
  }
  for (const bad of ['10.80.0.0/15', '10.80.0.0/25', '10.80.1.0/16', '256.0.0.0/16', '10.80.0.0', 'fd00::/56', undefined]) {  // redaction: allow - made-up CIDRs, which the context must refuse
    assert.throws(() => vpcCidr(bad), /^Error: -c vpcCidr/);
  }
});

test('vpcEndpointIds: one or more distinct vpce- IDs', () => {
  assert.deepEqual(vpcEndpointIds('vpce-0123456789abcdef0', 'k'), ['vpce-0123456789abcdef0']);
  assert.deepEqual(vpcEndpointIds('vpce-0123abcd,vpce-4567abcd', 'k'), ['vpce-0123abcd', 'vpce-4567abcd']);
  for (const bad of ['', 'vpce-0123abcd,vpce-0123abcd', 'vpc-0123abcd', 'vpce-0123abcd,', '*', undefined]) {
    assert.throws(() => vpcEndpointIds(bad, 'apiEndpointIds'), /^Error: -c apiEndpointIds/);
  }
});

test("imageUri: a repository of the stack's own account and Region, by digest", () => {
  const digest = `sha256:${'a'.repeat(64)}`;
  const uri = `111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog@${digest}`;
  assert.deepEqual(imageUri(uri, '111122223333', 'us-west-2'), { repository: 'colvenna-catalog-uat/colvenna-catalog', digest });
  for (const bad of [uri.replace(`@${digest}`, ':v1'), uri.replace('111122223333', '444455556666'), uri.replace('us-west-2', 'us-east-2'),
    uri.replace('sha256:a', 'sha256:A'), `public.ecr.aws/x/y@${digest}`, undefined]) {
    assert.throws(() => imageUri(bad, '111122223333', 'us-west-2'), /^Error: -c imageUri/);
  }
});

test('integer, hostName, identifier, warehouseName, dataTokenMode, dataTokenDuration', () => {
  assert.equal(integer('2048', 'memory', 1024, 10240), 2048);
  for (const bad of ['0', '1023', '10241', '2048.5', '-1', '0x10', '', undefined]) {
    assert.throws(() => integer(bad, 'memory', 1024, 10240), /^Error: -c memory/);
  }
  assert.equal(hostName('db.abc123.us-west-2.rds.amazonaws.com', 'databaseEndpoint'), 'db.abc123.us-west-2.rds.amazonaws.com');
  for (const bad of ['db_1.example.com', '-db.example.com', 'db..example.com', '', undefined]) {
    assert.throws(() => hostName(bad, 'databaseEndpoint'), /^Error: -c databaseEndpoint/);
  }
  assert.equal(identifier('colvenna_catalog', 'databaseName'), 'colvenna_catalog');
  assert.throws(() => identifier('colvenna-catalog', 'databaseName'), /^Error: -c databaseName/);
  assert.equal(warehouseName('colvenna'), 'colvenna');
  assert.throws(() => warehouseName('Colvenna'), /^Error: -c warehouseName/);
  assert.equal(dataTokenMode('disabled'), 'disabled');
  assert.throws(() => dataTokenMode('irsa'), /^Error: -c dataTokens/);
  for (const good of ['PT1H30M', 'PT3H', 'PT90M', 'PT12H']) {
    assert.equal(dataTokenDuration(good), good);
  }
  for (const bad of ['PT1H', 'PT12H1M', 'PT', 'P1D', 'PT3h', undefined]) {
    assert.throws(() => dataTokenDuration(bad), /^Error: -c dataTokenDuration/);
  }
});
