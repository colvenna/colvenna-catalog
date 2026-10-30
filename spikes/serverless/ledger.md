# Resource ledger: Colvenna Catalog serverless spike (#12)

Every AWS resource the spike created, by type and logical name. Region `us-west-2` only. All of
them belong to the one CloudFormation stack `colvenna-catalog-spike`, carry the tag
`Project=colvenna-catalog-spike` (where the type supports tags) and are named with the prefix
`colvenna-catalog-spike` (the identity pool uses `colvenna_catalog_spike`: Cognito does not allow
`-` in pool names). No account IDs, ARNs or addresses are recorded here.

The stack's template and the spike's scripts (`deploy.sh`, `destroy.sh`, `verify-clean.sh` and the
rest), functions and images were deleted in #49, once the spike was over; this ledger and
`results/` are its record. They are in the history: `git show 1b88bc8:spikes/serverless/<file>`.

| Type | Logical ID | Name |
| --- | --- | --- |
| AWS::CloudFormation::Stack | — | `colvenna-catalog-spike` |
| AWS::S3::Bucket | SpikeBucket | generated, `colvenna-catalog-spike-spikebucket-…` (private, all public access blocked, TLS only) |
| AWS::S3::BucketPolicy | SpikeBucketPolicy | deny non-TLS |
| AWS::ECR::Repository | ImageRepository | `colvenna-catalog-spike` (emptied on delete) |
| AWS::EC2::VPC | Vpc | `colvenna-catalog-spike-vpc` (no internet gateway, no NAT gateway, no Elastic IP) |
| AWS::EC2::Subnet | SubnetA, SubnetB | `colvenna-catalog-spike-private-a`, `-private-b` |
| AWS::EC2::RouteTable (+2 associations) | RouteTable | `colvenna-catalog-spike-private` |
| AWS::EC2::SecurityGroup | FunctionSecurityGroup, EndpointSecurityGroup, DatabaseSecurityGroup | `colvenna-catalog-spike-functions`, `-endpoints`, `-database` |
| AWS::EC2::VPCEndpoint (gateway) | S3GatewayEndpoint | `colvenna-catalog-spike-s3` (policy: the spike bucket only) |
| AWS::EC2::VPCEndpoint (interface) | StsEndpoint, CognitoIdentityEndpoint, ExecuteApiEndpoint | `colvenna-catalog-spike-sts`, `-cognito-identity`, `-execute-api` (one AZ each) |
| AWS::RDS::DBSubnetGroup | DatabaseSubnets | `colvenna-catalog-spike-db` |
| AWS::RDS::DBInstance | Database | `colvenna-catalog-spike-db` (PostgreSQL 16, db.t4g.micro, no backups, deleted without snapshot) |
| AWS::Cognito::IdentityPool | IdentityPool | `colvenna_catalog_spike` (unauthenticated identities off, developer provider only, no roles attached) |
| AWS::IAM::Role | DataRole | `colvenna-catalog-spike-data` (trusts only the pool's authenticated identities; 12 h sessions) |
| AWS::IAM::Role | ChainControlRole | `colvenna-catalog-spike-chain-control` (role-chaining contrast case) |
| AWS::IAM::Role | ServerRole, HelperRole, ProbeRole | `colvenna-catalog-spike-server`, `-helper`, `-probe` |
| AWS::Logs::LogGroup | ServerLogs, SnapServerLogs, ZipServerLogs, AuthorizerLogs, DriverLogs, ProbeLogs, ProbeVpcLogs | `/aws/lambda/colvenna-catalog-spike-*` (1-day retention) |
| AWS::Lambda::Function | ServerFunction | `colvenna-catalog-spike-server` (container image, arm64, in VPC) |
| AWS::Lambda::Function (+Version, Alias) | SnapServerFunction, SnapServerVersion, SnapServerAlias | `colvenna-catalog-spike-server-snap`, alias `live` (container image with SnapStart; only while `EnableSnapStart=true`) |
| AWS::Lambda::LayerVersion | AdapterLayer | `colvenna-catalog-spike-adapter` (Lambda Web Adapter binary; layers take no tags) |
| AWS::Lambda::Function (+Version, Alias) | ZipServerFunction, ZipServerVersion, ZipServerAlias | `colvenna-catalog-spike-server-zip`, alias `live` (zip on java21 with SnapStart; only while `EnableZip=true`) |
| AWS::Lambda::Function | AuthorizerFunction, DriverFunction | `colvenna-catalog-spike-authorizer`, `-driver` (Python, in VPC) |
| AWS::Lambda::Function | ProbeFunction, ProbeVpcFunction | `colvenna-catalog-spike-cred-probe` (outside a VPC), `-cred-probe-vpc` |
| AWS::Lambda::Permission | ServerInvokePermission, SnapInvokePermission, ZipInvokePermission, AuthorizerInvokePermission | API Gateway may invoke, scoped to the spike API |
| AWS::ApiGateway::RestApi | Api | `colvenna-catalog-spike-api` (PRIVATE, resource policy: only through the spike execute-api endpoint) |
| AWS::ApiGateway::Authorizer | BearerAuthorizer | `colvenna-catalog-spike-bearer` (REQUEST, `Authorization` header) |
| AWS::ApiGateway::Resource, Method | ProxyResource/ProxyMethod, SnapResource/SnapProxyResource/SnapProxyMethod, ZipResource/ZipProxyResource/ZipProxyMethod | `/{proxy+}`, `/snap/{proxy+}`, `/zip/{proxy+}` (CUSTOM authorization) |
| AWS::ApiGateway::Deployment, Stage | InitialDeployment, Stage | stage `spike` (later deployments by `deploy.sh redeploy-api`) |

Objects inside those resources (bucket objects, image layers, database rows) went with them; an
operator upload of one test object (`warehouse/probe/db1/t2/data/existing.txt`) was removed when
the bucket was emptied.

Created outside CloudFormation by the services themselves, and removed with their owners:

- Lambda Hyperplane network interfaces in the spike subnets (removed by Lambda after the functions
  are deleted).
- Interface endpoint network interfaces (removed with the endpoints).

Never created: Lambda Function URLs, NAT gateways, internet gateways, Elastic IPs, public buckets,
identity pool role attachments, Secrets Manager secrets.

## Timeline

| When (UTC) | What |
| --- | --- |
| 2026-10-28 03:29 | Stack created, base phase (bucket, repository). |
| 2026-10-28 03:31 | Full phase rolled back: RDS refused `catalog` as a database name (reserved). The rollback deleted what it had created (four VPC endpoints, VPC, subnets, security groups, roles). |
| 2026-10-28 03:42 | Full phase created. Later updates of the same stack: helper code, S3A settings, SnapStart container function (03:58), zip function and adapter layer (04:04). |
| 2026-10-28 03:49–04:03 | Out-of-band changes to the server function only (environment nonce and memory size, to force cold starts); removed with the stack. |
| 2026-10-28 04:13 | `destroy.sh`: bucket emptied, stack deletion started. |
| 2026-10-28 04:33 | Stack deleted (the last VPC function's network interfaces took about 20 minutes). |
| 2026-10-28 04:36 | `verify-clean.sh`: nothing left; see `results/cleanup-verification.txt`. VPC count back to 22 of 30. |

## Cleanup proof

`verify-clean.sh` checks the tagging API and lists every type the spike used: stack, Lambda
functions and their URL configs, Lambda layers, REST APIs, identity pools, IAM roles, RDS
instances, snapshots, automated backups and subnet groups, VPCs, VPC endpoints, security groups
and their network interfaces, ECR repositories, S3 buckets and log groups. All came back empty.
Right after the deletion, the tagging index still listed the eight VPC endpoints of the two
creations; `DescribeVpcEndpoints` answers "does not exist" for each of them, so they are counted
as stale index entries, not as resources.
