import { App, Stack } from 'aws-cdk-lib';
import { envName, flag, integer } from './context';
import { CatalogEksStack } from './eks-stack';
import { forbidResources } from './guard';
import { CatalogImagesStack } from './images-stack';
import { nagChecks } from './nag';
import { CatalogNetworkStack } from './network-stack';
import { CatalogReleaseStack } from './release-stack';
import { CatalogServerlessStack } from './serverless-stack';
import { CatalogStorageStack } from './storage-stack';

/** Every context key the app reads; install.sh passes them as -c <key>=<value>. */
export const CONTEXT_KEYS = [
  'stack', 'account', 'region', 'sourceSha',
  'env', 'oidcIssuer', 'bucket', 'warehousePrefix', 'namespace', 'serviceAccount', 'databaseUser',
  'createBucket', 'createDatabase', 'vpcId', 'subnetIds', 'subnetZones', 'clientSecurityGroupId',
  'pullAccounts',
  'networkMode', 'vpcCidr', 'securityGroupId', 'apiEndpointIds', 'imageUri', 'warehouseName', 'databaseEndpoint',
  'databasePort', 'databaseName', 'databaseResourceId', 'dataTokens', 'dataTokenDuration', 'memory', 'concurrency', 'snapStart',
] as const;
type Context = Partial<Record<(typeof CONTEXT_KEYS)[number], string>>;
const STACKS = ['eks', 'storage', 'release', 'network', 'images', 'serverless'] as const;

/** ColvennaCatalog<Env><Component>: uat, Eks -> ColvennaCatalogUatEks. */
const stackName = (env: string, component: string) => `ColvennaCatalog${env[0].toUpperCase()}${env.slice(1)}${component}`;

/**
 * Builds the one stack `stack` names into the app, from the context only, with cdk-nag and the
 * forbidden-resource guard (src/guard.ts) gating it. A key the stack needs and the context lacks fails
 * here, named, before any default could stand in for it. The EKS form has the storage and eks stacks;
 * the serverless form has network, storage (if it makes the bucket or the database), images and
 * serverless; release is the release registry.
 */
export function buildApp(context: Context, app: App = new App()): App {
  const stack = context.stack;
  const need = (key: keyof Context): string => {
    const value = context[key];
    if (value === undefined || value === '') {
      throw new Error(`-c ${key} is required for stack ${stack}`);
    }
    return value;
  };
  if (!STACKS.includes(stack as (typeof STACKS)[number])) {
    throw new Error(`-c stack=<${STACKS.join('|')}>, not ${JSON.stringify(stack)}`);
  }
  const env = { account: need('account'), region: need('region') };
  const sourceSha = need('sourceSha');
  let built: Stack;
  if (stack === 'eks') {
    const environment = envName(need('env'));
    built = new CatalogEksStack(app, stackName(environment, 'Eks'), {
      env,
      environment,
      sourceSha,
      oidcIssuer: need('oidcIssuer'),
      bucketName: need('bucket'),
      warehousePrefix: need('warehousePrefix'),
      // The names colvenna-lake's catalog has, so that colvenna-lake can switch to this one in place.
      namespace: context.namespace || 'colvenna-catalog',
      serviceAccount: context.serviceAccount || 'colvenna-catalog',
      databaseUser: context.databaseUser || 'colvenna_catalog',
    });
  } else if (stack === 'storage') {
    const environment = envName(need('env'));
    const createDatabase = flag(need('createDatabase'), 'createDatabase');
    built = new CatalogStorageStack(app, stackName(environment, 'Storage'), {
      env,
      environment,
      sourceSha,
      createBucket: flag(need('createBucket'), 'createBucket'),
      database: createDatabase
        ? {
          vpcId: need('vpcId'),
          subnetIds: need('subnetIds').split(','),
          subnetZones: need('subnetZones').split(','),
          clientSecurityGroupId: need('clientSecurityGroupId'),
        }
        : undefined,
    });
  } else if (stack === 'network') {
    const environment = envName(need('env'));
    const mode = need('networkMode');
    if (mode !== 'create' && mode !== 'existing') {
      throw new Error(`-c networkMode=<create|existing>, not ${JSON.stringify(mode)}`);
    }
    built = new CatalogNetworkStack(app, stackName(environment, 'Network'), {
      env,
      environment,
      sourceSha,
      databasePort: integer(need('databasePort'), 'databasePort', 1, 65535),
      network: mode === 'create'
        ? { mode, cidr: need('vpcCidr'), zones: need('subnetZones').split(',') }
        : {
          mode,
          vpcId: need('vpcId'),
          vpcCidr: need('vpcCidr'),
          subnetIds: need('subnetIds').split(','),
          subnetZones: need('subnetZones').split(','),
        },
    });
  } else if (stack === 'images') {
    const environment = envName(need('env'));
    built = new CatalogImagesStack(app, stackName(environment, 'Images'), { env, environment, sourceSha });
  } else if (stack === 'serverless') {
    const environment = envName(need('env'));
    built = new CatalogServerlessStack(app, stackName(environment, 'Serverless'), {
      env,
      environment,
      sourceSha,
      vpcId: need('vpcId'),
      subnetIds: need('subnetIds').split(','),
      subnetZones: need('subnetZones').split(','),
      securityGroupId: need('securityGroupId'),
      apiEndpointIds: need('apiEndpointIds').split(','),
      imageUri: need('imageUri'),
      bucketName: need('bucket'),
      warehousePrefix: need('warehousePrefix'),
      warehouseName: need('warehouseName'),
      database: {
        endpoint: need('databaseEndpoint'),
        port: integer(need('databasePort'), 'databasePort', 1, 65535),
        name: need('databaseName'),
        user: need('databaseUser'),
        resourceId: need('databaseResourceId'),
      },
      dataTokens: { mode: need('dataTokens') as 'aws-sts' | 'disabled', duration: need('dataTokenDuration') },
      memoryMb: integer(need('memory'), 'memory', 1024, 10240),
      reservedConcurrency: integer(need('concurrency'), 'concurrency', 1, 1000),
      snapStart: flag(need('snapStart'), 'snapStart'),
    });
  } else {
    built = new CatalogReleaseStack(app, 'ColvennaCatalogRelease', {
      env,
      sourceSha,
      pullAccounts: context.pullAccounts ? context.pullAccounts.split(',') : [],
    });
  }
  nagChecks(built);
  forbidResources(built, { noInternetPaths: true });
  return app;
}

if (require.main === module) {
  const app = new App();
  const context: Context = {};
  for (const key of CONTEXT_KEYS) {
    const value = app.node.tryGetContext(key);
    if (value !== undefined) {
      context[key] = String(value);
    }
  }
  buildApp(context, app).synth();
}
