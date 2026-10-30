import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import test from 'node:test';
import { parseAllDocuments } from 'yaml';

const repo = resolve(__dirname, '../../..');
const chart = resolve(repo, 'deploy/helm/colvenna-catalog');
/** scripts/install-tools.sh's Helm unless HELM names another. */
const helm = process.env.HELM ?? resolve(repo, '.tools/helm');

/** What the server cannot start without; the tests add to it. */
const required: Record<string, string> = {
  'image.repository': 'repo',
  'image.digest': 'sha256:0',
  'warehouse.root': 's3://bucket/warehouse',
  region: 'us-west-2',
  'database.url': 'jdbc:postgresql://db/colvenna_catalog',
  'serviceAccount.roleArn': 'arn:aws:iam::111122223333:role/r',
};

/** `helm template t <chart>` with these values (--set) and JSON values (--set-json). */
function render(values: Record<string, string>, json: Record<string, unknown> = {}) {
  const args = ['template', 't', chart,
    ...Object.entries(values).flatMap(([k, v]) => ['--set', `${k}=${v}`]),
    ...Object.entries(json).flatMap(([k, v]) => ['--set-json', `${k}=${JSON.stringify(v)}`])];
  const out = spawnSync(helm, args, { encoding: 'utf8' });
  assert.equal(out.error, undefined, `helm at ${helm}: ${out.error?.message}`);
  return out;
}

/** The rendered manifests, which must render. */
function manifests(values: Record<string, string> = {}, json: Record<string, unknown> = {}) {
  const out = render({ ...required, ...values }, json);
  assert.equal(out.status, 0, out.stderr);
  return parseAllDocuments(out.stdout).map((d) => d.toJS()).filter((m) => m);
}

const one = (all: any[], kind: string, name?: string) => {
  const found = all.filter((m) => m.kind === kind && (name === undefined || m.metadata.name === name));
  assert.equal(found.length, 1, `one ${kind}${name ? ` ${name}` : ''}, found ${found.length}`);
  return found[0];
};
const spread = (values: Record<string, string>) =>
  one(manifests(values), 'Deployment').spec.template.spec.topologySpreadConstraints[0];

test('replicas wait for a second node rather than join one (minDomains)', () => {
  const constraint = spread({});
  assert.equal(constraint.whenUnsatisfiable, 'DoNotSchedule');
  assert.equal(constraint.minDomains, 2);
  assert.equal(constraint.topologyKey, 'kubernetes.io/hostname');
  assert.deepEqual(constraint.matchLabelKeys, ['pod-template-hash']);
  assert.equal(spread({ eligibleNodes: '3' }).minDomains, 2);
  assert.equal(spread({ eligibleNodes: '4' }).minDomains, 2);
  assert.equal(spread({ replicas: '3' }).minDomains, 2);
  assert.equal(spread({ replicas: '3', eligibleNodes: '4' }).minDomains, 3);
});

test('replicas spread without minDomains where waiting would never end', () => {
  assert.equal(spread({ eligibleNodes: '1' }).minDomains, undefined);
  assert.equal(spread({ replicas: '1' }).minDomains, undefined);
  const preferred = spread({ spreadAcrossNodes: 'false' });
  assert.equal(preferred.whenUnsatisfiable, 'ScheduleAnyway');
  assert.equal(preferred.minDomains, undefined);
});

test('labels say part-of colvenna-catalog', () => {
  const all = manifests();
  for (const kind of ['Deployment', 'Service', 'ConfigMap', 'NetworkPolicy']) {
    assert.equal(one(all, kind).metadata.labels['app.kubernetes.io/part-of'], 'colvenna-catalog', kind);
  }
});

test('admits client and monitoring namespaces by the new default labels', () => {
  const ingress = one(manifests(), 'NetworkPolicy').spec.ingress;
  assert.deepEqual(ingress.map((rule: any) => rule.from[0].namespaceSelector.matchLabels), [
    { 'colvenna-catalog/client': 'true' },
    { 'colvenna-catalog/monitoring': 'true' },
  ]);
});

test("keeps colvenna-lake's labels when set", () => {
  const ingress = one(manifests({
    'clientNamespaceLabel.key': 'colvenna-lake.io/catalog-client',
    metricsClientLabel: 'colvenna-lake.io/monitoring',
  }), 'NetworkPolicy').spec.ingress;
  assert.deepEqual(ingress.map((rule: any) => rule.from[0].namespaceSelector.matchLabels), [
    { 'colvenna-lake.io/catalog-client': 'true' },
    { 'colvenna-lake.io/monitoring': 'true' },
  ]);
});

test('renders no load balancer by default', () => {
  const all = manifests();
  assert.equal(all.filter((m) => m.kind === 'Service' && m.spec.type === 'LoadBalancer').length, 0);
  assert.equal(JSON.stringify(one(all, 'NetworkPolicy')).includes('ipBlock'), false);
});

test('an internal NLB on the API port only', () => {
  const all = manifests({ 'internalLoadBalancer.enabled': 'true' },
    { 'internalLoadBalancer.allowedCidrs': ['10.0.0.0/16'] });
  const nlb = one(all, 'Service', 't-nlb');
  assert.equal(nlb.spec.type, 'LoadBalancer');
  assert.deepEqual(nlb.metadata.annotations, {
    'service.beta.kubernetes.io/aws-load-balancer-type': 'external',
    'service.beta.kubernetes.io/aws-load-balancer-nlb-target-type': 'ip',
    'service.beta.kubernetes.io/aws-load-balancer-scheme': 'internal',
    // ip targets see the NLB's own address unless the client's is preserved; the NetworkPolicy's
    // ipBlock is about the client.
    'service.beta.kubernetes.io/aws-load-balancer-target-group-attributes': 'preserve_client_ip.enabled=true',
  });
  // The NLB's security group admits the same sources.
  assert.deepEqual(nlb.spec.loadBalancerSourceRanges, ['10.0.0.0/16']);
  assert.deepEqual(nlb.spec.ports, [{ name: 'https', port: 8181, targetPort: 'https', protocol: 'TCP' }]);
  assert.deepEqual(nlb.spec.selector, one(all, 'Service', 't').spec.selector);
  const ingress = one(all, 'NetworkPolicy').spec.ingress;
  const fromCidrs = ingress.filter((rule: any) => rule.from.some((peer: any) => peer.ipBlock));
  assert.deepEqual(fromCidrs, [{ from: [{ ipBlock: { cidr: '10.0.0.0/16' } }], ports: [{ port: 8181, protocol: 'TCP' }] }]);
});

test("admits the NLB's health checks from its own addresses only", () => {
  const all = manifests({ 'internalLoadBalancer.enabled': 'true' }, {
    'internalLoadBalancer.allowedCidrs': ['10.0.0.0/16'],
    'internalLoadBalancer.healthCheckCidrs': ['10.0.1.5/32', '10.0.2.6/32'],
  });
  const ingress = one(all, 'NetworkPolicy').spec.ingress;
  const fromCidrs = ingress.filter((rule: any) => rule.from.some((peer: any) => peer.ipBlock));
  assert.deepEqual(fromCidrs.map((rule: any) => rule.from.map((peer: any) => peer.ipBlock.cidr)),
    [['10.0.0.0/16', '10.0.1.5/32', '10.0.2.6/32']]);
  assert.deepEqual(fromCidrs[0].ports, [{ port: 8181, protocol: 'TCP' }]);
  for (const wide of [['10.0.1.0/24'], ['fd00::/64']]) {
    const out = render({ ...required, 'internalLoadBalancer.enabled': 'true' }, {
      'internalLoadBalancer.allowedCidrs': ['10.0.0.0/16'],
      'internalLoadBalancer.healthCheckCidrs': wide,
    });
    assert.notEqual(out.status, 0, `healthCheckCidrs ${JSON.stringify(wide)} rendered`);
    assert.match(out.stderr, /internalLoadBalancer\.healthCheckCidrs/);
  }
});

test('rejects an open or empty NLB allow list', () => {
  for (const cidrs of [[], ['0.0.0.0/0'], ['::/0'], ['10.0.0.0/16', '0.0.0.0/0'], ['10.0.0.1/0'], ['::0/0'],
    ['0::/0'], [''], ['not-a-cidr'], ['10.0.0.0'], ['10.0.0.0/33']]) {  // redaction: allow - made-up addresses, which the chart must refuse
    const out = render({ ...required, 'internalLoadBalancer.enabled': 'true' },
      { 'internalLoadBalancer.allowedCidrs': cidrs });
    assert.notEqual(out.status, 0, `allowedCidrs ${JSON.stringify(cidrs)} rendered`);
    assert.match(out.stderr, /internalLoadBalancer\.allowedCidrs/);
  }
});

test('requires what the server cannot start without', () => {
  for (const key of Object.keys(required)) {
    const values = { ...required };
    delete values[key];
    const out = render(values);
    assert.notEqual(out.status, 0, `rendered without ${key}`);
    assert.match(out.stderr, new RegExp(key.replace('.', '\\.')));
  }
});

test("the server's S3A client gets the install's Region, as in the serverless form (#9)", () => {
  for (const region of ['us-west-2', 'eu-central-1']) {
    const data = one(manifests({ region }), 'ConfigMap').data;
    // fs.s3a.endpoint.region: S3A does not read s3.region, and would send to us-east-2 without it.
    assert.equal(data.COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION, region);
    assert.equal(data.COLVENNA_FILEIO_S3_REGION, region);
    assert.equal(data.AWS_REGION, region);
  }
});

test('a replica that stops waits out the Service rules, then drains, within its grace period (#64)', () => {
  const all = manifests();
  const pod = one(all, 'Deployment').spec.template.spec;
  const container = pod.containers[0];
  // Kubernetes takes a terminating pod out of the endpoints while it runs preStop, then sends SIGTERM.
  assert.deepEqual(container.lifecycle.preStop, { exec: { command: ['sleep', '15'] } });
  const env = one(all, 'ConfigMap').data;
  assert.equal(env.COLVENNA_SHUTDOWN_DRAIN, 'PT5S');
  assert.equal(env.COLVENNA_SHUTDOWN_TIMEOUT, 'PT20S');
  // The wait, the drain and the requests in flight, with 10 s to spare, before the kubelet kills it.
  assert.equal(pod.terminationGracePeriodSeconds, 15 + 5 + 20 + 10);
  const longer = manifests({ 'shutdown.preStopSeconds': '30', 'shutdown.drainSeconds': '2', 'shutdown.timeoutSeconds': '60' });
  const longerPod = one(longer, 'Deployment').spec.template.spec;
  assert.deepEqual(longerPod.containers[0].lifecycle.preStop.exec.command, ['sleep', '30']);
  assert.equal(longerPod.terminationGracePeriodSeconds, 30 + 2 + 60 + 10);
  assert.equal(one(longer, 'ConfigMap').data.COLVENNA_SHUTDOWN_TIMEOUT, 'PT60S');
  // A value that is not a whole number of seconds fails the render rather than become 0.
  for (const [key, value] of [['preStopSeconds', '15s'], ['drainSeconds', '-1'], ['timeoutSeconds', 'PT20S']]) {
    const out = render({ ...required, [`shutdown.${key}`]: value });
    assert.notEqual(out.status, 0, `shutdown.${key}=${value} rendered`);
    assert.match(out.stderr, new RegExp(`shutdown\\.${key}: "${value}" is not a whole number of seconds`));
  }
});
