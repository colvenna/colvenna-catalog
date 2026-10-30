import { App } from 'aws-cdk-lib';
import { forbidResources } from '../src/guard';
import { nagChecks } from '../src/nag';
import { TestEnvironmentStack } from './stack';

/** The context keys tests/environment/up.sh passes, every one required. */
export const TEST_CONTEXT_KEYS = ['name', 'account', 'region', 'sourceSha', 'zones', 'operatorCidr', 'adminPrincipalArn'] as const;
type TestContext = Partial<Record<(typeof TEST_CONTEXT_KEYS)[number], string>>;

/**
 * The test environment's app (npx cdk -a 'npx ts-node test-env/app.ts'), apart from the product's
 * src/app.ts so no product synthesis ever includes it. Builds ColvennaCatalogTest<Name>, with cdk-nag and
 * the forbidden-resource guard.
 */
export function buildTestApp(context: TestContext, app: App = new App()): App {
  const need = (key: (typeof TEST_CONTEXT_KEYS)[number]): string => {
    const value = context[key];
    if (value === undefined || value === '') {
      throw new Error(`-c ${key} is required for the test environment`);
    }
    return value;
  };
  const name = need('name');
  const stack = new TestEnvironmentStack(app, `ColvennaCatalogTest${name[0].toUpperCase()}${name.slice(1)}`, {
    env: { account: need('account'), region: need('region') },
    name,
    sourceSha: need('sourceSha'),
    availabilityZones: need('zones').split(','),
    operatorCidr: need('operatorCidr'),
    adminPrincipalArn: need('adminPrincipalArn'),
  });
  nagChecks(stack);
  forbidResources(stack);
  return app;
}

if (require.main === module) {
  const app = new App();
  const context: TestContext = {};
  for (const key of TEST_CONTEXT_KEYS) {
    const value = app.node.tryGetContext(key);
    if (value !== undefined) {
      context[key] = String(value);
    }
  }
  buildTestApp(context, app).synth();
}
