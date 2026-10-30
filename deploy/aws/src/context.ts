/**
 * Every value the CDK app takes from `-c <key>=<value>` (install.sh passes them), checked before a
 * stack uses it. Each function returns the value it accepted and throws an Error naming the key
 * otherwise, so a missing or malformed input fails the synthesis instead of reaching AWS.
 */

function text(value: unknown, key: string, rule: string, pattern: RegExp): string {
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`-c ${key}=<value>: ${rule}, not ${JSON.stringify(value)}`);
  }
  return value;
}

/** The environment's name: its stacks and AWS resources are named after it. */
export function envName(value: unknown): string {
  const name = text(value, 'env', 'a lowercase letter, then lowercase letters and digits, 8 at most',
    /^[a-z][a-z0-9]{0,7}$/);
  if (name === 'release') {
    throw new Error('-c env: release is reserved for the release registry');
  }
  return name;
}

export function account(value: unknown): string {
  return text(value, 'account', 'the 12-digit AWS account', /^[0-9]{12}$/);
}

/**
 * A region of the standard partition (aws): three parts, the last a digit, not China's (cn-...).
 * GovCloud's names have four parts. Other partitions have their own ARNs and endpoints: unsupported.
 */
export function standardRegion(value: unknown): string {
  const region = text(value, 'region', 'a standard AWS region, e.g. us-west-2', /^[a-z]{2}-[a-z]+-[1-9]$/);
  if (region.startsWith('cn-')) {
    throw new Error(`-c region: ${region} is in the China partition, which is not supported`);
  }
  return region;
}

export function commitSha(value: unknown): string {
  return text(value, 'sourceSha', 'a full commit SHA (40 lowercase hex digits)', /^[0-9a-f]{40}$/);
}

/** The EKS cluster's OIDC issuer, which must be of the stack's region. */
export function oidcIssuer(value: unknown, region: string): string {
  const pattern = new RegExp(`^https://oidc\\.eks\\.${region.replace(/-/g, '\\-')}\\.amazonaws\\.com/id/[0-9A-F]{32}$`);
  return text(value, 'oidcIssuer', `the EKS cluster's OIDC issuer in ${region}`, pattern);
}

/** An S3 bucket name: 3–63 lowercase letters, digits, dots and hyphens, not shaped like an IP. */
export function bucketName(value: unknown): string {
  const name = text(value, 'bucket', 'an S3 bucket name', /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/);
  if (/^\d+\.\d+\.\d+\.\d+$/.test(name) || name.includes('..')) {
    throw new Error(`-c bucket: ${name} is not a valid S3 bucket name`);
  }
  return name;
}

/**
 * The warehouse's key prefix in the bucket: segments of [a-z0-9_-] joined by '/', with no leading or
 * trailing slash, so that `<prefix>/*` is the only object scope a policy is ever given.
 */
export function warehousePrefix(value: unknown): string {
  const prefix = text(value, 'warehousePrefix', 'segments of [a-z0-9_-] joined by "/", no leading or trailing "/"',
    /^[a-z0-9_-]+(\/[a-z0-9_-]+)*$/);
  if (prefix.length > 64) {
    throw new Error('-c warehousePrefix: 64 characters at most');
  }
  return prefix;
}

/** A Kubernetes name (DNS-1123 label), for the context key it came from. */
export function k8sName(value: unknown, key: string): string {
  return text(value, key, 'a DNS-1123 label (lowercase letters, digits and "-", 63 at most)',
    /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/);
}

/** A PostgreSQL identifier that needs no quoting. */
export function databaseIdentifier(value: unknown): string {
  return text(value, 'databaseUser', 'a PostgreSQL identifier ([a-z_][a-z0-9_]*, 63 at most)', /^[a-z_][a-z0-9_]{0,62}$/);
}

const resourceId = (prefix: string) => new RegExp(`^${prefix}-([0-9a-f]{8}|[0-9a-f]{17})$`);

export function vpcId(value: unknown): string {
  return text(value, 'vpcId', 'a VPC ID (vpc-...)', resourceId('vpc'));
}

export function securityGroupId(value: unknown): string {
  return text(value, 'clientSecurityGroupId', 'a security group ID (sg-...)', resourceId('sg'));
}

/** Two or more distinct subnet IDs, comma-separated. */
export function subnetIds(value: unknown): string[] {
  const ids = text(value, 'subnetIds', 'two or more subnet IDs, comma-separated', /^[^,]+(,[^,]+)+$/).split(',');
  if (ids.some((id) => !resourceId('subnet').test(id)) || new Set(ids).size !== ids.length) {
    throw new Error(`-c subnetIds: distinct subnet IDs (subnet-...), not ${JSON.stringify(value)}`);
  }
  return ids;
}

/** The availability zones of those subnets, in the same order: one per subnet, of the stack's region. */
export function subnetZones(value: unknown, region: string, count: number): string[] {
  const zone = `${region.replace(/-/g, '\\-')}[a-z]`;
  const zones = text(value, 'subnetZones', `${count} availability zones of ${region}, comma-separated`,
    new RegExp(`^${zone}(,${zone})*$`)).split(',');
  if (zones.length !== count) {
    throw new Error(`-c subnetZones: ${count} zones, one per subnet, not ${zones.length}`);
  }
  return zones;
}

/** Accounts allowed to pull release images: none, or distinct 12-digit IDs. Never a wildcard. */
export function accountList(value: unknown): string[] {
  if (value === undefined || value === '') {
    return [];
  }
  const ids = text(value, 'pullAccounts', '12-digit account IDs, comma-separated', /^[0-9]{12}(,[0-9]{12})*$/).split(',');
  if (new Set(ids).size !== ids.length) {
    throw new Error('-c pullAccounts: an account is listed twice');
  }
  return ids;
}

export function flag(value: unknown, key: string): boolean {
  return text(value, key, 'true or false', /^(true|false)$/) === 'true';
}

/**
 * The serverless form's new VPC: an IPv4 CIDR from /16 to /24, which the network stack splits into
 * two subnets two bits longer.
 */
export function vpcCidr(value: unknown): string {
  const cidr = text(value, 'vpcCidr', 'an IPv4 CIDR from /16 to /24, e.g. 10.80.0.0/16',
    /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}\/(1[6-9]|2[0-4])$/);
  const [address, prefix] = cidr.split('/');
  const bits = address.split('.').reduce((n, part) => n * 256 + Number(part), 0);
  if (bits % 2 ** (32 - Number(prefix)) !== 0) {
    throw new Error(`-c vpcCidr: ${cidr} has host bits set; its network address is another`);
  }
  return cidr;
}

/** One or more distinct VPC endpoint IDs (vpce-...), comma-separated. */
export function vpcEndpointIds(value: unknown, key: string): string[] {
  const ids = text(value, key, 'one or more VPC endpoint IDs (vpce-...), comma-separated', /^[^,]+(,[^,]+)*$/).split(',');
  if (ids.some((id) => !resourceId('vpce').test(id)) || new Set(ids).size !== ids.length) {
    throw new Error(`-c ${key}: distinct VPC endpoint IDs (vpce-...), not ${JSON.stringify(value)}`);
  }
  return ids;
}

/** An image in an ECR repository of the stack's account and Region, by digest. */
export function imageUri(value: unknown, accountId: string, region: string): { repository: string; digest: string } {
  const host = `${accountId}\\.dkr\\.ecr\\.${region.replace(/-/g, '\\-')}\\.amazonaws\\.com`;
  const uri = text(value, 'imageUri', `<account>.dkr.ecr.${region}.amazonaws.com/<repository>@sha256:<digest> of ${accountId}`,
    new RegExp(`^${host}/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$`));
  const [path, digest] = uri.split('@');
  return { repository: path.slice(path.indexOf('/') + 1), digest };
}

/** A whole number from min to max, given as a context string. */
export function integer(value: unknown, key: string, min: number, max: number): number {
  const n = Number(text(value, key, `a whole number from ${min} to ${max}`, /^[1-9][0-9]*$/));
  if (n < min || n > max) {
    throw new Error(`-c ${key}: a whole number from ${min} to ${max}, not ${n}`);
  }
  return n;
}

/** A DNS host name, such as a database's endpoint. */
export function hostName(value: unknown, key: string): string {
  return text(value, key, 'a host name', /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/);
}

/** A PostgreSQL identifier that needs no quoting, for the context key it came from. */
export function identifier(value: unknown, key: string): string {
  return text(value, key, 'a PostgreSQL identifier ([a-z_][a-z0-9_]*, 63 at most)', /^[a-z_][a-z0-9_]{0,62}$/);
}

/** The warehouse name: the REST path prefix clients pass. */
export function warehouseName(value: unknown): string {
  return text(value, 'warehouseName', 'lowercase letters, digits, "_" and "-", 63 at most', /^[a-z0-9][a-z0-9_-]{0,62}$/);
}

/** Per-table credentials: aws-sts (the catalog vends them) or disabled. */
export function dataTokenMode(value: unknown): 'aws-sts' | 'disabled' {
  return text(value, 'dataTokens', 'aws-sts or disabled', /^(aws-sts|disabled)$/) as 'aws-sts' | 'disabled';
}

/** A table credential's lifetime: PT<n>H, PT<n>M or PT<n>H<n>M, from PT1H30M to PT12H. */
export function dataTokenDuration(value: unknown): string {
  const duration = text(value, 'dataTokenDuration', 'an ISO-8601 duration from PT1H30M to PT12H (PT<n>H<n>M)',
    /^PT(?=\d)(\d+H)?(\d+M)?$/);
  const [, hours, minutes] = /^PT(?:(\d+)H)?(?:(\d+)M)?$/.exec(duration)!;
  const total = Number(hours ?? 0) * 60 + Number(minutes ?? 0);
  if (total < 90 || total > 720) {
    throw new Error(`-c dataTokenDuration: from PT1H30M to PT12H, not ${duration}`);
  }
  return duration;
}

/** An RDS instance's (db-...) or Aurora cluster's (cluster-...) resource ID, as rds-db:connect names it. */
export function databaseResourceId(value: unknown): string {
  return text(value, 'databaseResourceId', "an RDS instance's or Aurora cluster's resource ID (db-... or cluster-...)",
    /^(db|cluster)-[A-Z0-9]{10,40}$/);
}
