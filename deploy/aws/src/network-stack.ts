import { CfnOutput, Fn, RemovalPolicy, Stack, StackProps, Tags, Validations } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { account, commitSha, envName, integer, standardRegion, subnetIds, subnetZones, vpcCidr, vpcId } from './context';

/** A new VPC for the catalog: its range, and the two zones of the stack's region its subnets go in. */
export interface CreatedNetwork {
  mode: 'create';
  cidr: string;
  zones: string[];
}

/** The user's VPC, whose endpoints are taken as given (install.sh's preflight checks them). */
export interface ExistingNetwork {
  mode: 'existing';
  vpcId: string;
  /** The VPC's IPv4 range, where the catalog's functions reach the database. */
  vpcCidr: string;
  /** The private subnets, in two or more zones, where the functions (and a database it makes) go. */
  subnetIds: string[];
  subnetZones: string[];
}

export interface CatalogNetworkStackProps extends StackProps {
  environment: string;
  sourceSha: string;
  network: CreatedNetwork | ExistingNetwork;
  /** The port of the metadata database, which the catalog's functions connect to. */
  databasePort: number;
}

/**
 * The serverless form's network. With a new VPC: two private subnets and nothing that reaches the
 * internet (no internet gateway, no NAT gateway, no Elastic IP), and the endpoints the catalog needs
 * there: S3 (a gateway endpoint), and STS, Cognito identity (the per-table credentials) and execute-api
 * (the private API's only way in), as interface endpoints with private DNS. In either case, the
 * catalog's security group, which its functions run with and which a database the storage stack makes
 * admits. A database the storage stack made lives in these subnets and admits that group, so this
 * stack stays as long as the database does.
 */
export class CatalogNetworkStack extends Stack {
  private readonly zones: string[];

  /** The given zones, never the context lookup Stack would make. */
  override get availabilityZones(): string[] {
    return this.zones;
  }

  constructor(scope: Construct, id: string, props: CatalogNetworkStackProps) {
    super(scope, id, props);
    const region = standardRegion(props.env?.region);
    account(props.env?.account);
    const env = envName(props.environment);
    const sha = commitSha(props.sourceSha);
    const port = integer(String(props.databasePort), 'databasePort', 1, 65535);
    const named = (name: string) => `colvenna-catalog-${env}${name}`;
    Tags.of(this).add('Project', 'colvenna-catalog');
    Tags.of(this).add('Environment', env);
    Tags.of(this).add('SourceSha', sha);

    let vpc: ec2.IVpc;
    let range: string;
    let subnets: { ids: string; zones: string[] };
    if (props.network.mode === 'create') {
      range = vpcCidr(props.network.cidr);
      const zones = subnetZones(props.network.zones.join(','), region, 2);
      if (zones[0] === zones[1]) {
        throw new Error(`-c subnetZones: two different zones, not ${zones.join(',')}`);
      }
      this.zones = zones;
      const created = new ec2.Vpc(this, 'Vpc', {
        vpcName: named(''),
        ipAddresses: ec2.IpAddresses.cidr(range),
        availabilityZones: zones,
        natGateways: 0,
        subnetConfiguration: [{
          name: 'catalog', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: Number(range.split('/')[1]) + 2,
        }],
      });
      for (const subnet of created.isolatedSubnets) {
        Validations.of(subnet).acknowledge({
          id: 'CloudFormation-Validate::W3010',
          reason: 'The zones are chosen by install.sh (where every endpoint the catalog needs is offered) and given',
        });
      }
      created.addFlowLog('FlowLog', {
        destination: ec2.FlowLogDestination.toCloudWatchLogs(new logs.LogGroup(this, 'FlowLogs', {
          retention: logs.RetentionDays.THREE_MONTHS,
          removalPolicy: RemovalPolicy.DESTROY,
        })),
        trafficType: ec2.FlowLogTrafficType.ALL,
      });

      // S3: the warehouse, and only this account's buckets, without a way out of the VPC.
      const s3 = created.addGatewayEndpoint('S3Endpoint', {
        service: ec2.GatewayVpcEndpointAwsService.S3,
        subnets: [{ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }],
      });
      s3.addToPolicy(new iam.PolicyStatement({
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:*'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
      }));
      const endpoints = new ec2.SecurityGroup(this, 'EndpointSecurityGroup', {
        vpc: created,
        securityGroupName: named('-endpoints'),
        description: 'colvenna-catalog VPC endpoints: HTTPS from the VPC',
        allowAllOutbound: false,
      });
      endpoints.addIngressRule(ec2.Peer.ipv4(range), ec2.Port.tcp(443), 'HTTPS from the VPC');
      const services: [string, ec2.IInterfaceVpcEndpointService][] = [
        // AssumeRoleWithWebIdentity, for the per-table credentials.
        ['Sts', ec2.InterfaceVpcEndpointAwsService.STS],
        // GetOpenIdTokenForDeveloperIdentity, the web identity the server assumes the data role with.
        ['CognitoIdentity', new ec2.InterfaceVpcEndpointService(`com.amazonaws.${region}.cognito-identity`, 443)],
        // The private REST API's way in.
        ['ExecuteApi', ec2.InterfaceVpcEndpointAwsService.APIGATEWAY],
      ];
      const made: Record<string, ec2.InterfaceVpcEndpoint> = {};
      for (const [name, service] of services) {
        made[name] = created.addInterfaceEndpoint(`${name}Endpoint`, {
          service,
          privateDnsEnabled: true,
          open: false,
          securityGroups: [endpoints],
          subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
        });
      }
      new CfnOutput(this, 'ExecuteApiEndpointId', { value: made.ExecuteApi.vpcEndpointId });
      vpc = created;
      subnets = {
        ids: Fn.join(',', created.isolatedSubnets.map((s) => s.subnetId)),
        zones: created.isolatedSubnets.map((s) => s.availabilityZone),
      };
    } else {
      range = vpcCidr(props.network.vpcCidr);
      const ids = subnetIds(props.network.subnetIds.join(','));
      const zones = subnetZones(props.network.subnetZones.join(','), region, ids.length);
      if (new Set(zones).size < 2) {
        throw new Error(`-c subnetZones: the subnets must span two zones or more, not ${zones.join(',')}`);
      }
      this.zones = [...new Set(zones)];
      vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', { vpcId: vpcId(props.network.vpcId), availabilityZones: this.zones });
      subnets = { ids: ids.join(','), zones };
    }

    // The catalog's functions: no ingress at all (Lambda needs none); HTTPS out (the endpoints, and S3
    // through its gateway endpoint, whose addresses are S3's), and the database's port within the VPC.
    const catalog = new ec2.SecurityGroup(this, 'CatalogSecurityGroup', {
      vpc,
      securityGroupName: named('-functions'),
      description: 'colvenna-catalog functions: HTTPS out, and PostgreSQL within the VPC',
      allowAllOutbound: false,
    });
    catalog.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS: the VPC endpoints and S3');
    catalog.addEgressRule(ec2.Peer.ipv4(range), ec2.Port.tcp(port), 'PostgreSQL: the metadata database');

    new CfnOutput(this, 'VpcId', { value: vpc.vpcId });
    new CfnOutput(this, 'VpcCidr', { value: range });
    new CfnOutput(this, 'SubnetIds', { value: subnets.ids });
    new CfnOutput(this, 'SubnetZones', { value: subnets.zones.join(',') });
    new CfnOutput(this, 'CatalogSecurityGroupId', { value: catalog.securityGroupId });
    new CfnOutput(this, 'SourceSha', { value: sha });
  }
}
