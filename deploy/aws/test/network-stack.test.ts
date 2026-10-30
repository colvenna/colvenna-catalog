import assert from 'node:assert/strict';
import test from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { nagChecks } from '../src/nag';
import { CatalogNetworkStack, CatalogNetworkStackProps } from '../src/network-stack';
import { unacknowledged } from './nag-helpers';

const created: CatalogNetworkStackProps = {
  env: { account: '111122223333', region: 'us-west-2' },
  environment: 'uat',
  sourceSha: 'a'.repeat(40),
  network: { mode: 'create', cidr: '10.80.0.0/16', zones: ['us-west-2a', 'us-west-2b'] },
  databasePort: 5432,
};
const existing: CatalogNetworkStackProps = {
  ...created,
  network: {
    mode: 'existing', vpcId: 'vpc-0123456789abcdef0', vpcCidr: '10.1.0.0/16',
    subnetIds: ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'], subnetZones: ['us-west-2a', 'us-west-2c'],
  },
};

function build(props: CatalogNetworkStackProps) {
  const stack = new CatalogNetworkStack(new App(), 'ColvennaCatalogUatNetwork', props);
  return { stack, template: Template.fromStack(stack) };
}
const resources = (template: Template, type: string) => Object.values(template.findResources(type)) as any[];
const flat = (value: unknown) => JSON.stringify(value);

test('a new VPC has no way to the internet', () => {
  const { template } = build(created);
  for (const type of ['AWS::EC2::InternetGateway', 'AWS::EC2::EgressOnlyInternetGateway', 'AWS::EC2::NatGateway',
    'AWS::EC2::EIP', 'AWS::EC2::VPCGatewayAttachment', 'AWS::EC2::TransitGatewayAttachment', 'AWS::EC2::VPCPeeringConnection']) {
    assert.deepEqual(template.findResources(type), {}, type);
  }
  const subnets = resources(template, 'AWS::EC2::Subnet');
  assert.deepEqual(subnets.map((s) => s.Properties.AvailabilityZone).sort(), ['us-west-2a', 'us-west-2b']);
  for (const subnet of subnets) {
    assert.equal(subnet.Properties.MapPublicIpOnLaunch, false);
    assert.match(subnet.Properties.CidrBlock, /\/18$/);
  }
  // No route leaves the VPC, but to the S3 gateway endpoint (added by the endpoint, not as a route).
  assert.deepEqual(template.findResources('AWS::EC2::Route'), {});
  template.resourceCountIs('AWS::EC2::FlowLog', 1);
});

test('a new VPC has the endpoints the catalog needs, and no others', () => {
  const { template } = build(created);
  const endpoints = resources(template, 'AWS::EC2::VPCEndpoint');
  const services = endpoints.map((e) => flat(e.Properties.ServiceName)).sort();
  assert.equal(endpoints.length, 4);
  assert.match(services.join(' '), /\.cognito-identity"/);
  assert.match(services.join(' '), /\.execute-api"/);
  assert.match(services.join(' '), /\.sts"/);
  assert.match(services.join(' '), /\.s3"/);
  for (const endpoint of endpoints) {
    if (endpoint.Properties.VpcEndpointType === 'Interface') {
      assert.equal(endpoint.Properties.PrivateDnsEnabled, true);
      assert.equal(endpoint.Properties.SubnetIds.length, 2, 'in both zones');
    } else {
      assert.equal(endpoint.Properties.VpcEndpointType, 'Gateway');
      // This account's buckets only.
      assert.deepEqual(endpoint.Properties.PolicyDocument.Statement[0].Condition,
        { StringEquals: { 'aws:ResourceAccount': '111122223333' } });
    }
  }
  template.hasOutput('ExecuteApiEndpointId', {});
});

test('security groups: the endpoints admit HTTPS from the VPC; the functions admit nothing', () => {
  const { template } = build(created);
  const groups = resources(template, 'AWS::EC2::SecurityGroup');
  const endpoints = groups.find((g) => g.Properties.GroupName === 'colvenna-catalog-uat-endpoints');
  assert.deepEqual(endpoints.Properties.SecurityGroupIngress.map((r: any) => [r.IpProtocol, r.FromPort, r.ToPort, r.CidrIp]),
    [['tcp', 443, 443, '10.80.0.0/16']]);
  const catalog = groups.find((g) => g.Properties.GroupName === 'colvenna-catalog-uat-functions');
  assert.equal(catalog.Properties.SecurityGroupIngress, undefined);
  assert.deepEqual(catalog.Properties.SecurityGroupEgress.map((r: any) => [r.IpProtocol, r.FromPort, r.CidrIp]).sort(),
    [['tcp', 443, '0.0.0.0/0'], ['tcp', 5432, '10.80.0.0/16']]);
  assert.deepEqual(template.findResources('AWS::EC2::SecurityGroupIngress'), {});
});

test('an existing VPC: only the functions\' security group is made; its endpoints are taken as given', () => {
  const { template } = build(existing);
  for (const type of ['AWS::EC2::VPC', 'AWS::EC2::Subnet', 'AWS::EC2::VPCEndpoint', 'AWS::EC2::FlowLog']) {
    assert.deepEqual(template.findResources(type), {}, type);
  }
  const [catalog] = resources(template, 'AWS::EC2::SecurityGroup');
  assert.equal(catalog.Properties.VpcId, 'vpc-0123456789abcdef0');
  assert.deepEqual(catalog.Properties.SecurityGroupEgress.map((r: any) => [r.FromPort, r.CidrIp]).sort(),
    [[443, '0.0.0.0/0'], [5432, '10.1.0.0/16']]);
  const outputs = template.findOutputs('*');
  assert.equal(outputs.SubnetIds.Value, 'subnet-0123456789abcdef0,subnet-0123456789abcdef1');
  assert.equal(outputs.SubnetZones.Value, 'us-west-2a,us-west-2c');
  assert.equal(outputs.ExecuteApiEndpointId, undefined);
});

test('outputs what the later stacks take', () => {
  for (const props of [created, existing]) {
    const outputs = Object.keys(build(props).template.findOutputs('*'));
    for (const key of ['VpcId', 'VpcCidr', 'SubnetIds', 'SubnetZones', 'CatalogSecurityGroupId', 'SourceSha']) {
      assert.ok(outputs.includes(key), `${props.network.mode}: ${key}`);
    }
  }
});

test('passes cdk-nag', () => {
  for (const props of [created, existing]) {
    const { stack } = build(props);
    nagChecks(stack);
    assert.deepEqual(unacknowledged(stack), [], props.network.mode);
  }
});

test('rejects malformed inputs', () => {
  assert.throws(() => build({ ...created, network: { mode: 'create', cidr: '10.80.0.0/12', zones: ['us-west-2a', 'us-west-2b'] } }),
    /-c vpcCidr/);
  assert.throws(() => build({ ...created, network: { mode: 'create', cidr: '10.80.0.1/16', zones: ['us-west-2a', 'us-west-2b'] } }),
    /host bits/);
  assert.throws(() => build({ ...created, network: { mode: 'create', cidr: '10.80.0.0/16', zones: ['us-west-2a', 'us-west-2a'] } }),
    /two different zones/);
  assert.throws(() => build({ ...created, network: { mode: 'create', cidr: '10.80.0.0/16', zones: ['us-east-2a', 'us-east-2b'] } }),
    /-c subnetZones/);
  assert.throws(() => build({
    ...existing,
    network: { ...existing.network, subnetZones: ['us-west-2a', 'us-west-2a'] } as CatalogNetworkStackProps['network'],
  }), /two zones or more/);
  assert.throws(() => build({ ...created, databasePort: 0 }), /-c databasePort/);
  assert.throws(() => build({ ...created, environment: 'release' }), /-c env/);
});
