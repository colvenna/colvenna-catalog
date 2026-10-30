import { App } from 'aws-cdk-lib';
import { forbidResources } from '../src/guard';
import { nagChecks } from '../src/nag';
import { DriverStack, DriverStackProps } from './driver-stack';

/** The context keys tests/serverless/run.sh passes, every one required. */
export const DRIVER_CONTEXT_KEYS = [
  'env', 'account', 'region', 'sourceSha', 'vpcId', 'subnetIds', 'subnetZones', 'repositoryName', 'imageDigest',
] as const;
/** Optional: the catalog's API port the driver may reach besides HTTPS (an internal NLB's, UAT-DEP-11). */
export const DRIVER_OPTIONAL_KEYS = ['apiPort'] as const;
/** The optional keys of the UAT harness's database access (DRIVER_DATABASE_ACCESS=on): all three, or none. */
export const DATABASE_ACCESS_KEYS = ['catalogSecurityGroupId', 'dbResourceId', 'dbUser'] as const;
type DriverContext = Partial<
  Record<
    | (typeof DRIVER_CONTEXT_KEYS)[number]
    | (typeof DRIVER_OPTIONAL_KEYS)[number]
    | (typeof DATABASE_ACCESS_KEYS)[number],
    string
  >
>;

function databaseAccess(context: DriverContext): DriverStackProps['databaseAccess'] {
  const given = DATABASE_ACCESS_KEYS.filter((k) => context[k] !== undefined && context[k] !== '');
  if (given.length === 0) {
    return undefined;
  }
  if (given.length !== DATABASE_ACCESS_KEYS.length) {
    throw new Error(`-c ${DATABASE_ACCESS_KEYS.join(', -c ')}: all three, or none`);
  }
  return { securityGroupId: context.catalogSecurityGroupId!, dbResourceId: context.dbResourceId!, dbUser: context.dbUser! };
}

/**
 * The serverless live-test driver's app (npx cdk -a 'npx ts-node test-env/driver-app.ts'), apart from
 * the product's src/app.ts. Builds ColvennaCatalogTest<Env>Driver, with cdk-nag and the
 * forbidden-resource guard.
 */
export function buildDriverApp(context: DriverContext, app: App = new App()): App {
  const need = (key: (typeof DRIVER_CONTEXT_KEYS)[number]): string => {
    const value = context[key];
    if (value === undefined || value === '') {
      throw new Error(`-c ${key} is required for the live-test driver`);
    }
    return value;
  };
  const env = need('env');
  const stack = new DriverStack(app, `ColvennaCatalogTest${env[0].toUpperCase()}${env.slice(1)}Driver`, {
    env: { account: need('account'), region: need('region') },
    environment: env,
    sourceSha: need('sourceSha'),
    vpcId: need('vpcId'),
    subnetIds: need('subnetIds').split(','),
    subnetZones: need('subnetZones').split(','),
    repositoryName: need('repositoryName'),
    imageDigest: need('imageDigest'),
    ...(context.apiPort ? { apiPort: Number(context.apiPort) } : {}),
    databaseAccess: databaseAccess(context),
  });
  nagChecks(stack);
  forbidResources(stack);
  return app;
}

if (require.main === module) {
  const app = new App();
  const context: DriverContext = {};
  for (const key of [...DRIVER_CONTEXT_KEYS, ...DRIVER_OPTIONAL_KEYS, ...DATABASE_ACCESS_KEYS]) {
    const value = app.node.tryGetContext(key);
    if (value !== undefined) {
      context[key] = String(value);
    }
  }
  buildDriverApp(context, app).synth();
}
