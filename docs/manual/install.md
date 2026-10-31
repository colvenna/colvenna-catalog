# Deployment guide

This guide installs Colvenna Catalog into your AWS account with `install.sh`, keeps it up to date, and removes it with `uninstall.sh`. It covers both deployment forms (on Amazon EKS, and serverless on AWS Lambda) and both ways of providing what the catalog needs (new resources the install makes, or resources you already have). Using the installed catalog, connecting engines and running it day to day are in the [user manual](user.md).

Every `install.sh` and `uninstall.sh` command here follows the scripts' own usage and options. Where a path has so far run only in offline tests (the deployment scripts' stub tests in `tests/deploy` and the CDK tests), this guide says so; the release's UAT runs those paths live.

## 1. What gets installed

The catalog runs from one image in either form. `install.sh` deploys AWS CDK stacks from [`deploy/aws`](../../deploy/aws), named `ColvennaCatalog<Env><Component>`, where `<Env>` is the environment's name (`COLVENNA_ENV`) with its first letter in upper case: `uw2` gives `ColvennaCatalogUw2Eks`.

| Form | Stacks | Besides the stacks |
| --- | --- | --- |
| `eks` | `Storage`, only when the install makes the bucket or the database. `Eks`: the server's IAM role and the data role (both for the catalog's service account, through IRSA), the catalog's database user secret, and the environment's ECR repository | In the cluster: the namespace (Pod Security `restricted`), the Helm release (a Deployment of two replicas, a Service, a NetworkPolicy, a PodDisruptionBudget of `minAvailable: 1`, the ServiceAccount, a ConfigMap; with `COLVENNA_INTERNAL_NLB=on`, a Service of type LoadBalancer), and the Secrets of section 5.1 |
| `serverless` | `Network`, `Storage` (only when the install makes the bucket or the database), `Images` (the environment's ECR repository), `Serverless` (five functions, the private REST API, a Cognito identity pool for the per-table credentials, the data role, the admin token's secret, and the purge function's schedule and alarm) | Nothing else: the [LLD](../design/colvenna-catalog-lld.md#部署) describes each stack |

What the install makes and what it takes as given are chosen by three keys:

| Resource | New, made by the install | Yours, taken as given |
| --- | --- | --- |
| The S3 bucket | `COLVENNA_BUCKET` empty | `COLVENNA_BUCKET=<bucket>` |
| The metadata database | `COLVENNA_DB=create`: RDS PostgreSQL 16 | `COLVENNA_DB=existing`: an RDS PostgreSQL instance, or an Aurora PostgreSQL cluster |
| The network (serverless form) | `COLVENNA_NETWORK=create`: a new VPC with no internet path | `COLVENNA_NETWORK=existing`: your VPC, subnets and endpoints |
| The EKS cluster (EKS form) | Never made | Always yours: `COLVENNA_EKS_CLUSTER` |

What each choice costs, idle and in use, is in [the cost estimate](cost.md).

What has run live, in this repository's own test environments, and what only offline:

| Path | Run live | Offline only |
| --- | --- | --- |
| EKS form from source, with a new bucket, a new database and the internal NLB; run again; interrupted and run again; refused configurations; uninstalled keeping the data and installed again onto it (`COLVENNA_DB=existing`); uninstalled with `--delete-data` | us-west-2 | |
| EKS form from a release candidate (`--release`), run again, uninstalled | us-west-2, us-east-2 (`v1.0.0-rc.2`'s UAT) | |
| Serverless form from source, `COLVENNA_NETWORK=create`, `COLVENNA_DB=create`; run again and upgraded; uninstalled keeping the data, then with `--delete-data` | us-west-2 | |
| Serverless form with `COLVENNA_NETWORK=create`, pointed at a database restored from a snapshot (`COLVENNA_DB=existing`, [user manual](user.md#62-restoring) 6.2); the refusals of `COLVENNA_DB=existing` without `COLVENNA_BUCKET`, of `COLVENNA_DB=create` after the move, and of `--delete-data` while the restored instance is there | us-west-2 | |
| Serverless form from a release candidate (`--release`) | us-west-2, us-east-2 (`v1.0.0-rc.2`'s UAT) | |
| Either form from a release promoted from a GO candidate (`--release v1.0.0`) | | yes |
| EKS form pointed at a database restored from a snapshot | us-west-2, us-east-2 (`v1.0.0-rc.2`'s UAT-DEP-10) | |
| Serverless form with `COLVENNA_NETWORK=existing` and `COLVENNA_DB=existing` | us-west-2, us-east-2 (`v1.0.0-rc.2`'s UAT-DEP-02) | |
| Serverless form with `COLVENNA_NETWORK=existing` and `COLVENNA_DB=create` | | yes |
| An existing RDS instance an earlier install did not make (`COLVENNA_DB=existing`), both forms | us-west-2, us-east-2 (`v1.0.0-rc.2`'s UAT-DEP-02) | |
| An Aurora PostgreSQL database | | yes (preflight; UAT-DEP-05 runs its refusals live) |
| us-east-2 | `v1.0.0-rc.2`'s UAT: every case but S2's (UAT-DEP-06 to 09, not run) | |
| Any other standard Region | | yes (configuration and preflight) |

## 2. Before you start

### 2.1 The machine you install from

- **Linux** on x86_64 or arm64, with **bash 4.4** or later. `scripts/install-tools.sh` fetches the kubectl, Helm and crane versions it pins, each checked against its SHA-256, into `.tools/`; it supports only those two architectures of Linux.
- **A git clone** of this repository, not an archive: the scripts read the commit they install, and `--from-source` refuses a tree with changes.
- **Tools**, which preflight checks before it changes anything (exit 3 if one is missing or too old):

  | Tool | Version | For |
  | --- | --- | --- |
  | AWS CLI | 2 | every command |
  | jq | 1.6 | every command |
  | Python | 3.8 | every command |
  | Node.js with npm | 20 | the CDK app (`npm ci` in `deploy/aws`) |
  | git, tar, sha256sum | | every command |
  | curl | 7.76 | `install.sh` |
  | OpenSSL | 1.1.1 | `install.sh`: the private CA and the server certificate |
  | Docker with Buildx | Buildx 0.10 | `install.sh --from-source`: the image |
  | Maven, with JDK 21 | Maven 3.8, Java 21 | `install.sh --from-source`: the build |

  The Helm step also uses `flock` (util-linux), which preflight does not check.
- **Network access** from the machine: the AWS APIs of the Region; the npm registry (the CDK app's packages); `dl.k8s.io`, `get.helm.sh` and `github.com` (the pinned tools); `truststore.pki.rds.amazonaws.com` (the Region's RDS certificate bundle, EKS form); for `--from-source`, Maven Central and the registries of the Dockerfile's base images; for the EKS form, the cluster's API endpoint.
- **Local state.** `.state/<env>/` (git ignores it) keeps the install's kubeconfig, the stacks' outputs and the evidence of every run. Install an environment from one checkout at a time: the lock that serializes Helm changes is the checkout's own.

### 2.2 Account and Region

- `COLVENNA_ACCOUNT` is the account to install into. The credentials must be of that account, or preflight refuses before it reads anything else.
- `COLVENNA_REGION` is a standard AWS Region. The configuration refuses the China Regions (`cn-*`) and GovCloud (`us-gov-*`).
- The Region must be bootstrapped for the AWS CDK in the account. Preflight checks for the `CDKToolkit` stack and, without it, refuses with the command to run (from `deploy/aws`, after `npm ci`): `npx cdk bootstrap aws://<ACCOUNT_ID>/<region>`.
- A bucket you bring must be in the same Region; preflight checks. The per-table credentials name the server's Region, and buckets in other Regions are not supported.
- The serverless form needs more of the Region, all checked by preflight: API Gateway's account-level CloudWatch Logs role (section 2.5), enough unreserved Lambda concurrency, and with `COLVENNA_NETWORK=create` one VPC to spare and two zones that offer every endpoint it makes.

Section 9 lists what differs between Regions.

### 2.3 Identity and permissions

Run every command with an explicit profile (`AWS_PROFILE=<profile> ./install.sh ...`). So far every install has run as an identity with administrator access to the account and, in the EKS form, as a cluster administrator. A least-privilege policy for installing has not been written or tested. What the scripts need, from their code:

- **AWS.** The CDK deploys and destroys the stacks through the Region's CDK bootstrap roles, which the identity must be able to assume. Directly, the scripts call: STS `GetCallerIdentity`; CloudFormation `DescribeStacks` and `DescribeStackResources`, and for the serverless form's upgrade `GetTemplate` and `UpdateStack` of the `Serverless` stack, which CloudFormation then runs with the stack's own role, the CDK bootstrap's execution role, with no `iam:PassRole` of the identity's ([UpdateStack](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_UpdateStack.html)), and, the changed template being over the 51,200 bytes a request takes, `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on it in the CDK bootstrap's staging bucket (and the use of that bucket's KMS key, if the bootstrap was given one of its own); EKS `DescribeCluster`; IAM `ListOpenIDConnectProviders`; EC2 `Describe*` (subnets, security groups, instances, VPCs, route tables, VPC endpoints and endpoint services, network interfaces); RDS `DescribeDBInstances`, `DescribeDBClusters`, `DescribeDBParameters`, `DescribeDBClusterParameters`, `DescribeOrderableDBInstanceOptions`; S3 `GetBucketLocation`; Secrets Manager `DescribeSecret` and `GetSecretValue`, and for the serverless form `PutSecretValue`; ECR `DescribeImages`, `GetAuthorizationToken`, and pushing to the environment's repository; Elastic Load Balancing `Describe*` (internal NLB); for the serverless form, Lambda `GetAccountSettings`, `GetFunctionConcurrency`, `ListFunctionUrlConfigs` and `InvokeFunction` on the install's functions, API Gateway `GET` on the account and the REST API and its deployments, and creating and deleting the REST API's deployments, and Service Quotas `GetServiceQuota`. `uninstall.sh` also calls CloudWatch Logs `DescribeLogGroups` and `DeleteLogGroup`, and with `--delete-data`, RDS `ModifyDBInstance` and `DeleteDBInstance`, and S3 `GetBucketLogging`, `PutBucketLogging`, `GetBucketPolicy`, `PutBucketPolicy`, `DeleteBucketPolicy`, object deletion and `DeleteBucket`.
- **Who can invoke the serverless form's maintenance and ops functions** can issue an admin token and send requests through the private API with a token. Keep `lambda:InvokeFunction` on them to the account's administrators, as you would `pods/exec` in the catalog's namespace in the EKS form. Who can invoke the purge function can only make it delete, sooner, the files of tables already dropped: it takes nothing from its payload but the command.
- **Kubernetes** (EKS form). The identity needs access to the cluster (an EKS access entry, or the `aws-auth` ConfigMap) with rights to: list nodes and DaemonSets in every namespace; create and label namespaces; manage Secrets, ConfigMaps, Pods (with `exec`, `logs` and `port-forward`) and Services in the catalog's namespace and in the two namespaces the smoke check creates and deletes (`<namespace>-smoke-client` and `<namespace>-smoke-outsider`); and everything the chart installs.
- **The database admin secret** (`COLVENNA_DB_ADMIN_SECRET_ARN`, with `COLVENNA_DB=existing`) must be readable, and hold `{"username": ..., "password": ...}` of a user that may create roles and databases.
- **A release candidate or a release** (`--release`): the maintainer of the release registry lists your account among its pull accounts, and your identity needs `ecr:GetAuthorizationToken`, and `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and `ecr:BatchCheckLayerAvailability` on the release's repositories.

### 2.4 For the EKS form

- **The cluster** is `ACTIVE` in the Region, and the credentials can describe it.
- **IRSA.** IAM has an OIDC provider for the cluster's issuer. Without one, preflight refuses and prints how to make it: `eksctl utils associate-iam-oidc-provider --cluster <cluster> --region <region> --approve`. EKS Pod Identity is not used.
- **NetworkPolicy is enforced** by the VPC CNI's network policy agent (the `aws-node` DaemonSet's agent started with `--enable-network-policy=true`: the VPC CNI add-on's `enableNetworkPolicy`), by Calico or by Cilium. New EKS clusters do not enforce it by default. Otherwise preflight refuses, unless you give `--allow-unenforced-network-policy`: then any pod in the cluster can reach the API port, only the bearer token protects it, and the smoke check no longer tests that a namespace without the client label is refused. `install.sh` never changes the cluster's CNI.
- **Nodes.** Ready nodes that `COLVENNA_NODE_SELECTOR` allows (all, when empty), without a workload's `NoSchedule` or `NoExecute` taint: the catalog's pods tolerate none, and the replicas spread over these nodes only. A cordoned node, or one under a passing condition, still counts: Kubernetes' own `node.kubernetes.io/` taints but `out-of-service` ([taint-based evictions](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/#taint-based-evictions), [well-known taints](https://kubernetes.io/docs/reference/labels-annotations-taints/)), `node.cloudprovider.kubernetes.io/uninitialized` (a node its cloud provider still initializes), and the drain marks of the [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md) (`ToBeDeletedByClusterAutoscaler`, `DeletionCandidateOfClusterAutoscaler`) and [Karpenter](https://karpenter.sh/docs/concepts/disruption/) (`karpenter.sh/`). The replicas' spread honours the other taints too ([`nodeTaintsPolicy: Honor`](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/#spread-constraint-definition)). With two or more, the two replicas run on different nodes; with one, both run on it. Each replica requests 0.5 CPU and 1 GiB of memory, and is limited to 2 CPUs and 2 GiB.
- **The nodes reach the internet.** The install runs two short-lived pods from Docker Hub images pinned by digest (`postgres:16-alpine`, which creates the catalog's database user, and `curlimages/curl`, the smoke check). Every live install has used nodes behind a NAT gateway.
- **The server's S3 Region.** The chart gives the server's S3 client (Hadoop S3A, inside Paimon's S3 FileIO) the install's Region, `COLVENNA_REGION`, as `COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION` (S3A's `fs.s3a.endpoint.region`), as the serverless form does. The server's S3A client then sends to the Region's own S3 endpoint only (an offline test, `S3aRegionTest`, checks the host it connects to), which an S3 gateway endpoint in the Region serves ([gateway endpoints are available only in their own Region](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html)). Without it, S3A sends to us-east-2 first and follows the redirect to the bucket's Region, which hangs where the nodes have no internet egress; so `serve` refuses an S3 warehouse without it, in either form. Besides S3, the server reaches STS (its IRSA credentials and the per-table credentials; without internet egress, through an `sts` interface endpoint with private DNS) and the database. This ran live on an EKS install in `us-west-2` on 2026-10-30, with a NetworkPolicy that let the catalog's pods reach only the VPC (the database, DNS, and an `sts` interface endpoint added for the test) and the Region's S3 addresses (the gateway endpoint's prefix list), as in a VPC without internet egress: before the fix, creating a table got no answer in 200 seconds; after it, creating a database and a table, reading the table and dropping both all answered, the slowest in 3.3 seconds.
- **With `COLVENNA_DB=create`**, the database goes into the cluster's private subnets (those that give no public address), which must span two zones or more. Its security group admits PostgreSQL (5432) from the **cluster security group** only. The catalog's pods connect with their node's security groups, so the nodes must carry the cluster security group. EKS managed node groups attach it by default; self-managed nodes may not, and then the database step fails.
- **With `COLVENNA_DB=existing`**, preflight requires `COLVENNA_DB_ENDPOINT` to be an RDS PostgreSQL instance's endpoint or an Aurora PostgreSQL cluster's writer endpoint (a reader endpoint is refused), in the cluster's VPC, listening on `COLVENNA_DB_PORT`, and available, or in one of the busy states preflight accepts (backing up, modifying, configuring, upgrading, maintenance, storage optimization); any other state is refused. Its security groups must admit that port from one of the eligible nodes' security groups, or from CIDRs holding every eligible node's subnet. A database in a peered VPC, or admitted by a prefix-list rule, is refused by preflight even if it is reachable. It must not be publicly accessible, and must take TLS connections only: `rds.force_ssl=1` in its parameter group (an Aurora cluster's: in its cluster parameter group), as in the database the install makes. Its default is on for RDS for PostgreSQL 15 and later ([RDS: requiring an SSL connection](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html)), and for Aurora PostgreSQL 17 and later, but off for Aurora PostgreSQL 16 and earlier ([Aurora: requiring an SSL/TLS connection](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Security.html)), where the cluster needs a cluster parameter group of its own that sets it. The server connects with TLS (`sslmode=verify-full`) against the Region's RDS certificate bundle, and nothing in the configuration lowers it. The catalog is tested on PostgreSQL 16.
- **With `COLVENNA_INTERNAL_NLB=on`**, the AWS Load Balancer Controller runs in `kube-system` with an available replica, and `COLVENNA_NLB_ALLOWED_CIDRS` overlaps no address a pod can have: not the cluster's subnets, nor any node's. The NetworkPolicy admits those CIDRs to the API port, so an overlap would admit every pod there whatever its namespace's labels; preflight refuses it. List the subnets of the clients outside the cluster (EMR, say).
- **A bucket you bring** is encrypted with SSE-S3, as the buckets the install makes are: the server and data roles have no KMS permissions. With a customer managed KMS key, grant both roles `kms:Decrypt` and `kms:GenerateDataKey` on it yourself (not tested).

### 2.5 For the serverless form

- **`COLVENNA_NETWORK=create`** makes a VPC (`COLVENNA_VPC_CIDR`, /16 to /24) with two private subnets in two zones, no internet gateway, NAT gateway or Elastic IP, an S3 gateway endpoint that admits only the account's buckets, and `sts`, `cognito-identity` and `execute-api` interface endpoints with private DNS. Clients reach the API through that `execute-api` endpoint, or through `execute-api` endpoints of their own VPCs that you list in `COLVENNA_API_ENDPOINT_IDS`. A first install with `create` makes its database too (`COLVENNA_DB=create`); later, the install can be pointed at a database restored into that VPC (`COLVENNA_DB=existing`, [user manual](user.md#62-restoring), section 6.2).
- **`COLVENNA_NETWORK=existing`** takes `COLVENNA_VPC_ID` and two or more of its private subnets (`COLVENNA_SUBNET_IDS`, in two zones or more) as given, and never changes them. From those subnets, the functions must reach STS and Cognito identity (interface endpoints with private DNS) and S3 (a gateway endpoint on every subnet's route table), or else a NAT gateway as every subnet's default route. `COLVENNA_API_ENDPOINT_IDS` lists the available `execute-api` interface endpoints clients use, one of them in `COLVENNA_VPC_ID`, and their security groups must admit HTTPS from the catalog's subnets. Preflight checks the VPC, the subnets, their routes and endpoints, and each `execute-api` endpoint's service, state and VPC, and names everything missing; it does not check the endpoints' security groups. This mode ran live in `v1.0.0-rc.2`'s UAT, with an existing database, in both Regions (UAT-DEP-02, S3).
- **With `COLVENNA_DB=existing`**, preflight requires `COLVENNA_DB_ENDPOINT` to be an RDS PostgreSQL instance's endpoint or an Aurora PostgreSQL cluster's writer endpoint, in the catalog's VPC, listening on `COLVENNA_DB_PORT`, available or in one of the busy states preflight accepts (backing up, modifying, configuring, upgrading, maintenance, storage optimization), and with IAM database authentication on: the functions sign in with RDS IAM tokens, with no password. Like the EKS form's, it must not be publicly accessible and must take TLS connections only (`rds.force_ssl=1`). The catalog's VPC and subnets are `COLVENNA_VPC_ID` and `COLVENNA_SUBNET_IDS` with `COLVENNA_NETWORK=existing`. With `create`, they are the VPC and subnets the install made (the `Network` stack's): the database is then one restored into them, and preflight refuses `COLVENNA_DB=existing` before that stack exists. Its security groups must admit the port from CIDRs holding every one of the catalog's subnets, or from the catalog's security group (the `Network` stack's output `CatalogSecurityGroupId`, which exists only after the first install). `COLVENNA_DB_USER` is 57 characters at most: the authorizer's own user is `<COLVENNA_DB_USER>_authz`.
- **API Gateway's CloudWatch Logs role.** The private API writes access and error logs, which API Gateway does only when the account's API Gateway settings in the Region name a CloudWatch Logs role. `install.sh` never changes account settings; without the role, preflight refuses with the commands to set it: a role API Gateway may assume, with the AWS managed policy `AmazonAPIGatewayPushToCloudWatchLogs`, then `aws apigateway update-account --region <region> --patch-operations op=replace,path=/cloudwatchRoleArn,value=<the role's ARN>`.
- **Lambda concurrency.** The server and the authorizer each reserve `COLVENNA_LAMBDA_CONCURRENCY`, and the maintenance and purge functions 1 each: `2 × COLVENNA_LAMBDA_CONCURRENCY + 2`. Lambda keeps 100 of the account's concurrency unreserved; preflight refuses when what is left would not cover the reservation. The API admits about `8 × COLVENNA_LAMBDA_CONCURRENCY` requests a second (what that concurrency serves at a mean of 125 ms a request), in bursts of at most `COLVENNA_LAMBDA_CONCURRENCY`, and answers 429 past that; more requests running at once than the reserved concurrency (slower ones take more of it) answer 503; Paimon's client sends both again by itself, five times over about 31 seconds. Raise `COLVENNA_LAMBDA_CONCURRENCY` for more ([LLD](../design/colvenna-catalog-lld.md#无服务器形态)).

## 3. The configuration

Copy [`deploy/config/example.env`](../../deploy/config/example.env) to `deploy/config/<env>.env` (git ignores it) and fill it in. Each line is `KEY=value`, taken literally: nothing is expanded or run. `install.sh` refuses a key it does not know, a key set twice, and, for each form, the other form's keys set to anything but their defaults, so that a misspelt or misplaced key never passes for a default. It reports every error at once and exits 2. An empty value means the default.

| Key | Default | Meaning |
| --- | --- | --- |
| `COLVENNA_ENV` | (required) | The environment's name: a lowercase letter, then lowercase letters and digits, 8 at most. `release` and `test` are reserved (for the release registry and the repository's test environments) |
| `COLVENNA_ACCOUNT` | (required) | The 12-digit account |
| `COLVENNA_REGION` | (required) | A standard Region |
| `COLVENNA_FORM` | (required) | `eks` or `serverless` |
| `COLVENNA_EKS_CLUSTER` | | EKS: the cluster; required |
| `COLVENNA_NAMESPACE`, `COLVENNA_RELEASE_NAME`, `COLVENNA_SERVICE_ACCOUNT` | `colvenna-catalog` | EKS: the namespace, the Helm release (and the Service) and the service account |
| `COLVENNA_WAREHOUSE_NAME` | `colvenna` | The warehouse name clients pass, and the REST path prefix (`/v1/<name>/...`) |
| `COLVENNA_BUCKET` | (new bucket) | An existing bucket in the Region |
| `COLVENNA_WAREHOUSE_PREFIX` | `warehouse` | The catalog's key prefix in the bucket |
| `COLVENNA_DB` | `create` | `create` or `existing` |
| `COLVENNA_DB_NAME`, `COLVENNA_DB_USER` | `colvenna_catalog` | The catalog's PostgreSQL database and user, which the install creates or brings in line |
| `COLVENNA_DB_ENDPOINT`, `COLVENNA_DB_PORT`, `COLVENNA_DB_ADMIN_SECRET_ARN` | port `5432` | With `existing`: the endpoint, its port, and the admin secret (of the account and Region) |
| `COLVENNA_DATA_TOKENS` | `aws-sts` | `aws-sts`: the catalog vends per-table credentials; `disabled`: engines use their own S3 access |
| `COLVENNA_DATA_TOKEN_DURATION` | `PT3H` | Their lifetime, `PT1H30M` to `PT12H` |
| `COLVENNA_NODE_SELECTOR` | (any node) | EKS: `k=v` pairs, comma-separated |
| `COLVENNA_INTERNAL_NLB`, `COLVENNA_NLB_ALLOWED_CIDRS` | `off` | EKS: the internal NLB, and the client CIDRs it admits (required with `on`) |
| `COLVENNA_CLIENT_NAMESPACE_LABEL` | `colvenna-catalog/client=true` | EKS: the label of namespaces whose pods may call the API |
| `COLVENNA_METRICS_NAMESPACE_LABEL` | `colvenna-catalog/monitoring=true` | EKS: the label of namespaces whose pods may scrape the metrics port; its value must be `true` |
| `COLVENNA_RELEASE_REGISTRY` | | `--release`: the release registry, `<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` |
| `COLVENNA_NETWORK` | `create` | Serverless: `create` or `existing` |
| `COLVENNA_VPC_CIDR` | `10.80.0.0/16` | Serverless, `create`: the new VPC's range |
| `COLVENNA_VPC_ID`, `COLVENNA_SUBNET_IDS` | | Serverless, `existing`: your VPC and its private subnets |
| `COLVENNA_API_ENDPOINT_IDS` | | Serverless: the `execute-api` endpoints clients use (required with `existing`) |
| `COLVENNA_LAMBDA_MEMORY` | `2048` | Serverless: the server function's memory in MB, 1024 to 10240 |
| `COLVENNA_LAMBDA_CONCURRENCY` | `10` | Serverless: the most requests served at once, 1 to 1000; each execution environment holds up to 5 database connections. The API admits about 8 times as many requests a second, in bursts of as many (429 past that); more running at once answer 503 |
| `COLVENNA_SNAPSTART` | `off` | Serverless: `on` turns on Lambda SnapStart on the server's published versions, which for a container image is billed while the version is active, idle or not |

The names (`COLVENNA_NAMESPACE`, `COLVENNA_RELEASE_NAME`, `COLVENNA_SERVICE_ACCOUNT`, `COLVENNA_WAREHOUSE_NAME`, `COLVENNA_DB_NAME`, `COLVENNA_DB_USER`) default to those of Colvenna Lake's catalog, so that Colvenna Lake can switch to this one in place. Two installs in one cluster need names of their own, and client labels of their own, so that a client namespace reaches only the install it is for.

## 4. Installing

`install.sh` shows what it is about to change and asks you to type the environment's name; `--yes` skips the question, and without a terminal to ask on, the install is refused unless `--yes` is given. Run it again at any time with the same configuration: every step checks before it changes anything, so a rerun after a failure or an interruption carries on, and a rerun of a complete install changes nothing but what is due for renewal (section 6.1).

```text
Usage: install.sh --config <file> (--from-source | --release <name>) [--yes]
                  [--allow-unenforced-network-policy]
```

### 4.1 The EKS form

```bash
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --from-source
```

It prints each step as it starts (`== <step>`):

1. **tools**: the tools of section 2.1; the pinned kubectl, Helm and crane into `.tools/`; a clean tree (with `--release`, the checkout of the candidate's or the release's tag, section 4.3).
2. **preflight**: the account, the CDK bootstrap, the cluster and its private subnets, the OIDC provider, NetworkPolicy, the Load Balancer Controller and the NLB's CIDRs (with the NLB), the eligible nodes, the bucket, the database and its admin secret. Then the summary and the question.
3. **stacks**: `Storage` (if it makes the bucket or the database), then `Eks`.
4. **image**: built with Maven and Docker Buildx for `linux/amd64` and pushed, tagged with the commit, unless the tag is there already (tags never move).
5. **namespace**: made if missing, and labelled Pod Security `restricted`.
6. **database**: the Region's RDS certificate bundle in a ConfigMap; the catalog's user and database made or brought in line by a `psql` pod signed in as the admin user, whose password is in the cluster only while that pod runs.
7. **tls**: the private CA, made once (its certificate issued again from the same key if it lacks the extensions of section 5.1); a server certificate for the Service's names (and `localhost`, for port-forwards), made again only when it lacks a name or an extension, the CA does not verify it or the CA's certificate changed, or it has less than 30 days left.
8. **chart**: `helm upgrade --install` of the chart, waiting until every pod runs the new template.
9. **internal NLB** (with `COLVENNA_INTERNAL_NLB=on`): waits for the NLB, admits its health checks, adds its DNS name to the certificate, upgrades the chart again, and waits until its targets are healthy.
10. **admin**: the first admin's token (issued by the server, `bootstrap-admin`, straight into the Secret `colvenna-catalog-admin-token`, never printed), its `MANAGE` grant on the whole catalog, and Paimon's `default` database.
11. **smoke**: from a pod in a namespace with the client label, an anonymous request is refused (401) and the admin's succeeds (200); from a namespace without the label, the API cannot be reached.

It ends with `Colvenna Catalog (<commit>) is installed in <cluster>/<namespace>; evidence in <dir>`. The [README's quick start](../../README.md#quick-start-on-amazon-eks) and the [user manual](user.md#4-connecting-an-engine) go on from there, and [the admin API page](admin-api.md) has every administrative operation.

### 4.2 The serverless form

With `COLVENNA_FORM=serverless`, the same command installs it:

1. **tools**, as above.
2. **preflight**: the account, the CDK bootstrap, API Gateway's CloudWatch Logs role, Lambda concurrency; with `create`, a VPC to spare and the two zones; with `existing`, the VPC, its subnets and endpoints, and the database; the bucket and the admin secret.
3. **stacks**: `Network`, `Storage` (if needed) and `Images`.
4. **image**: built and pushed to the `Images` stack's repository (Lambda runs images only from a repository in its own account and Region).
5. **functions**: the `Serverless` stack, synthesized, then deployed; then the API's stage takes a new deployment of the API as the stack left it, since CloudFormation deletes a gateway response the stack dropped only after its own new deployment, which keeps it (#34). When the install's server runs another image (an upgrade, or going back to an older version), nothing that serves changes before the database is migrated (#63): the stack as deployed is updated with only its maintenance function as the new version makes it (its image, its settings and the database it migrates, and its role's sign-in to that database), and the server function, its version and the alias `live`, the authorizer and the ops and purge functions stay as they are. Before anything changes, and again before the synthesized stack is deployed, a log group it makes for one of the install's functions that is gone, which the `Serverless` stack does not own, is deleted (#54): Lambda makes a function's log group again when a run of the function writes to it after its stack has let it go, as going back to a version without the purge function does to the purge function's while a run of it goes on, and CloudFormation would refuse to make it. A group that is still another function's is left, and the run then stops naming it; one written again after it was deleted makes the deploy fail, and a run again deletes it.
6. **database**: the catalog's user and database, and the authorizer's read-only user, made or brought in line by the ops function as the admin user; neither user has a password (`rds_iam`). When the server ran another image, this comes after **server** (below): until then the ops function is the serving version's, on the database that version serves, and the install's database has the catalog's users already. So a run that changes both the version and the database, onto one that has no catalog user yet, fails at **migrate**: change one, then the other.
7. **migrate**: the maintenance function runs `migrate`. When the server ran another image, **server** follows: the whole `Serverless` stack takes the new version, and the API moves to the server's new version at once; a `migrate` that refuses ends the run with the server as it was, and nothing written. Then the authorizer's user is granted what the token check reads.
8. **admin**: as in the EKS form; the token goes into the Secrets Manager secret `colvenna-catalog-<env>/admin-token`.
9. **smoke**: from inside the VPC, through the private API: a request without a token and one with a forged token are refused (401), the admin's succeeds (200); no function has a function URL; the API is private and its policy denies every request not through its VPC endpoints; the purge function runs once.

The `Serverless` stack also holds the **purge function**, which `install.sh` runs once in its smoke check (its first report). Dropping a table answers as soon as the table is gone from the catalog, and the table's files are deleted afterwards: an EventBridge Scheduler schedule, `colvenna-catalog-<env>-purge`, runs the function every 15 minutes, and each run deletes the files of the tables dropped since, as the database records them, and then those that rollbacks and tag and branch deletions left unreferenced (#93). A purge that fails stays recorded and is tried again, and the CloudWatch alarm `colvenna-catalog-<env>-purge-errors` goes into `ALARM`; the alarm `colvenna-catalog-<env>-purge-backlog` does when a dropped table's files are still there after an hour, or a rollback's or deletion's an hour after they were due (a rollback's and a branch's wait an hour before they are) (each run's summary, in the function's log, is the metric it watches), and shows `INSUFFICIENT_DATA` when no run has reported for an hour. The alarms have no action: add one (an SNS topic, say) to be told, an insufficient-data one too. `GET /colvenna/admin/v1/purges` lists what is still to be deleted, with each purge's last error ([admin API](admin-api.md#dropped-tables-files)). To run it at once, invoke it as an administrator: `aws lambda invoke --function-name colvenna-catalog-<env>-purge --cli-binary-format raw-in-base64-out --payload '"purge"' <answer file>`. The [LLD](../design/colvenna-catalog-lld.md#无服务器形态) gives the timings.

`install.sh` never connects to the database or the private API itself: it invokes the maintenance and ops functions with IAM-authenticated Lambda Invoke calls, with passwords and tokens in payload files only you can read. It ends with the API's URL, `https://<api-id>.execute-api.<region>.amazonaws.com/catalog`, the endpoints clients reach it through, and the admin token's secret. The live install with a new network and database took about 20 minutes, most of it the RDS instance.

### 4.3 From a release candidate or a release

A release candidate (`v1.0.0-rc.<n>`) is installed from a clean checkout of its tag, with `COLVENNA_RELEASE_REGISTRY` in the configuration (the maintainer gives you the registry's address):

```bash
git fetch origin --tags
git checkout <candidate>        # the candidate's tag, v1.0.0-rc.<n>
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release <candidate>
```

Before it changes anything, it checks that the tree is clean and is the candidate's (it differs from the commit the candidate was built from under `release/<candidate>/` alone), that the manifest is valid and pins this checkout's files, that the registry holds the image and the chart by the manifest's digests and that they were built from that commit, and that the candidate is accepted in the configuration's form. Any of these failing refuses the install (exit 3). It then installs as from source, but builds nothing: the CDK app's packages come afresh from the tag's lockfile (`npm ci`), the image is copied by digest into the environment's own repository, and the chart is pulled by digest. `scripts/release/verify-artifacts.sh <candidate>`, with `COLVENNA_RELEASE_REGISTRY` set, makes the same checks and installs nothing. [Releasing](../development.md#releasing) describes candidates. The serverless form's `--release` path ran live in `v1.0.0-rc.2`'s UAT, the first candidate to name that form.

A release (`v1.0.0`) is a candidate judged GO and promoted from it, never rebuilt ([Promoting a GO candidate](../development.md#promoting-a-go-candidate)). It installs the same way, from a clean checkout of its own tag, with the same registry:

```bash
git fetch origin --tags
git checkout v1.0.0
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release v1.0.0
```

Its manifest, `release/v1.0.0/manifest.json`, is the candidate's under the release's names, and records under `promotion` which candidate it was promoted from. The tree may differ from the commit the candidate was built from under `release/<candidate>/` and `release/v1.0.0/` alone. Before it changes anything, the install also checks where the release came from (exit 3 when it is not so):

- the checkout is the candidate's tag commit (`promotion.tag_commit`) with `release/v1.0.0/` added, nothing else;
- the candidate's manifest there is the one recorded (`promotion.manifest`), and the release's manifest is that one under the release's names: the same image digest, commit, pinned files, UAT cases, performance terms, forms and Regions;
- the commit whose release notes recorded the candidate's verdict when it was promoted (`promotion.acceptance`; `promote.sh` reads them on `origin/main` unless told otherwise) is in the clone and has the candidate's tag commit, and its `release/<candidate>/notes.md` is the recorded file and gives the release verdict `GO`, beside the same manifest. A clone that lacks that commit, a shallow or single-branch one say, is refused: fetch it (`git fetch origin`).

The image is the candidate's, by the same digest, tagged `v1.0.0` in the environment's repository. The chart, pulled by its own digest, must be version `1.0.0` (appVersion `1.0.0`) and otherwise the candidate's: the chart directory of the commit the candidate was built from, annotated with that commit. The pods carry `colvenna-catalog/release: v1.0.0`. `scripts/release/verify-artifacts.sh v1.0.0` makes the same checks. A release's install has run only in offline tests: no release has been promoted yet.

### 4.4 Example configurations

In two Regions, with the keys left out at their defaults. Account IDs, ARNs, IDs and endpoints are placeholders.

An EKS install making its own bucket and database, in us-west-2:

```bash
COLVENNA_ENV=uw2
COLVENNA_ACCOUNT=<ACCOUNT_ID>
COLVENNA_REGION=us-west-2
COLVENNA_FORM=eks
COLVENNA_EKS_CLUSTER=<cluster>
```

An EKS install onto your own bucket and database, with an internal NLB, in us-east-2:

```bash
COLVENNA_ENV=ue2
COLVENNA_ACCOUNT=<ACCOUNT_ID>
COLVENNA_REGION=us-east-2
COLVENNA_FORM=eks
COLVENNA_EKS_CLUSTER=<cluster>
COLVENNA_BUCKET=<bucket>
COLVENNA_DB=existing
COLVENNA_DB_ENDPOINT=<instance>.<id>.us-east-2.rds.amazonaws.com
COLVENNA_DB_ADMIN_SECRET_ARN=arn:aws:secretsmanager:us-east-2:<ACCOUNT_ID>:secret:<name>
COLVENNA_INTERNAL_NLB=on
COLVENNA_NLB_ALLOWED_CIDRS=<client subnet CIDR>
```

A serverless install in a new network, in us-west-2, also reachable from a VPC of your clients through its own `execute-api` endpoint:

```bash
COLVENNA_ENV=sw2
COLVENNA_ACCOUNT=<ACCOUNT_ID>
COLVENNA_REGION=us-west-2
COLVENNA_FORM=serverless
COLVENNA_API_ENDPOINT_IDS=vpce-<id>
```

A serverless install in your VPC, onto your database, in us-east-2 (as `v1.0.0-rc.2`'s UAT-DEP-02 ran it):

```bash
COLVENNA_ENV=se2
COLVENNA_ACCOUNT=<ACCOUNT_ID>
COLVENNA_REGION=us-east-2
COLVENNA_FORM=serverless
COLVENNA_NETWORK=existing
COLVENNA_VPC_ID=vpc-<id>
COLVENNA_SUBNET_IDS=subnet-<id>,subnet-<id>
COLVENNA_API_ENDPOINT_IDS=vpce-<id>
COLVENNA_DB=existing
COLVENNA_DB_ENDPOINT=<instance>.<id>.us-east-2.rds.amazonaws.com
COLVENNA_DB_ADMIN_SECRET_ARN=arn:aws:secretsmanager:us-east-2:<ACCOUNT_ID>:secret:<name>
```

## 5. Network and security

Neither form has a public or anonymous entry, and the serverless form never has a Lambda function URL: the CDK app refuses, when it synthesizes, any function URL, any public load balancer, any API that is not private, any API method without an authorizer, and an internet gateway, NAT gateway or Elastic IP in a product stack. cdk-nag's AWS Solutions rules gate every synthesis.

### 5.1 The EKS form

| Port | What | Who reaches it |
| --- | --- | --- |
| 8181 | The Paimon REST API and the admin API, HTTPS, every request with a bearer token | Pods of namespaces with the client label (`COLVENNA_CLIENT_NAMESPACE_LABEL`), through the Service `<release>.<namespace>.svc`; with the internal NLB, clients in `COLVENNA_NLB_ALLOWED_CIDRS` (the NLB passes TCP through, and keeps the clients' addresses) |
| 9100 | `/metrics` (Prometheus), no catalog data | Pods of namespaces with the monitoring label (`COLVENNA_METRICS_NAMESPACE_LABEL`) |
| 8182 | `/healthz` and `/readyz` | Nobody outside the pod: it listens on 127.0.0.1, and the probes run inside the container |

- **TLS.** The server terminates TLS itself, with a certificate from the install's private CA (ECDSA P-256; the CA lasts 825 days, the server certificate 397). The certificate names the Service's DNS names, `localhost`, and the internal NLB's name. Clients trust the JDK's public CAs plus this CA (the [user manual](user.md#41-what-every-engine-needs)). Clients that check X.509 strictly, Python 3.13 and later among them ([`ssl.create_default_context`](https://docs.python.org/3/library/ssl.html#ssl.create_default_context) sets `VERIFY_X509_STRICT` since 3.13), accept both certificates. The CA has what [RFC 5280](https://www.rfc-editor.org/rfc/rfc5280#section-4.2.1.3) asks of a CA that signs certificates: a critical `basicConstraints` of a CA, `keyUsage` `keyCertSign` and `cRLSign` (critical) and a `subjectKeyIdentifier`. The server certificate has, by design, `keyUsage` `digitalSignature` (critical; its key is ECDSA, so no `keyEncipherment`), `extendedKeyUsage` `serverAuth`, the names above, and an `authorityKeyIdentifier` naming the CA by its key, which strict clients require of a certificate that is not self-signed.
- **Identities.** The pods run as the service account `COLVENNA_SERVICE_ACCOUNT`, whose IRSA role (`colvenna-catalog-<env>-server`) may only list the warehouse prefix and read and write the objects under it. The data role (`colvenna-catalog-<env>-data`) trusts the same service account alone, has the same ceiling, and is assumed with a session policy for one table each time the catalog vends credentials.
- **Secrets**, in the catalog's namespace: `colvenna-catalog-ca-key` (the CA's key and certificate; copy only `ca.crt` out of it), `colvenna-catalog-tls` (the server's key store), `colvenna-catalog-db` (the catalog's database user and password; the password is also in the Secrets Manager secret `colvenna-catalog-<env>/db-user`), `colvenna-catalog-admin-token` (the first admin's token), and the ConfigMap `colvenna-catalog-db-ca` (the RDS certificate bundle). A new database's admin password is in the Secrets Manager secret `colvenna-catalog-<env>/db-admin`. Who can read Secrets or exec into pods in the namespace can act as the catalog: give that only to its administrators.
- **The database** the install makes is RDS PostgreSQL 16 (`db.t4g.small`, 20 GiB gp3, encrypted, single-AZ, not publicly accessible, TLS required by its parameter group, 7 days of automated backups, deletion protection, its log exported to CloudWatch Logs for a year). Its security group admits 5432 from the cluster security group only (section 2.4).
- **The bucket** the install makes uses SSE-S3, blocks public access, refuses requests without TLS, and logs its server access to a second bucket, kept for a year.

### 5.2 The serverless form

- **The entry** is a private API Gateway REST API, reachable only through the `execute-api` VPC endpoints the install lists: its resource policy allows `execute-api:Invoke` only from them and denies everything else. API Gateway terminates TLS with its own certificate. Every method goes through a Lambda authorizer that checks the bearer token as the server does, and keeps each answer for 60 seconds; the server checks the token again. Only API Gateway may invoke the server and the authorizer.
- **The functions** run in the network's private subnets, with a security group that admits nothing in, and lets out only HTTPS, and the database's port within the VPC. They sign in to PostgreSQL with RDS IAM tokens: no function holds a password or another secret in its configuration. The one secret of the stack is the admin's token, which no function reads. A database the install makes is as in the EKS form (section 5.1), in these subnets, and admits only the functions' security group.
- **Per-table credentials** come from the data role through a Cognito identity pool with only a developer provider (no unauthenticated identities, no classic flow, no role attached), which only the server's role may use.
- **The purge function** has its own role: the warehouse prefix, as the server's, and the database as the catalog's user. Its schedule invokes it with a role of the schedule's own, which only EventBridge Scheduler may assume, for this account, and which may invoke this function alone; the function has no resource policy.
- **Logs**: the API's access log (`/aws/apigateway/colvenna-catalog-<env>-api`) and each function's (`/aws/lambda/colvenna-catalog-<env>-<server|authorizer|maintenance|purge|ops>`), kept for a year, and the API's error log, which API Gateway writes to `API-Gateway-Execution-Logs_<api-id>/catalog`.

## 6. Running it again, upgrading and rebuilding

### 6.1 Running it again

A rerun of a complete install, with the same configuration and checkout, changes nothing but what is due:

| What | Lifetime | Renewed by a run of `install.sh` when |
| --- | --- | --- |
| The first admin's token | 90 days | it is missing, does not authenticate, or has less than 30 days left. The old token stays valid until it expires |
| The first admin's `MANAGE` grant on the catalog | 365 days | it is missing, or has less than 30 days left |
| The server certificate (EKS) | 397 days | it lacks a name the install needs or an extension of section 5.1, the install's CA does not verify it (a certificate put in its Secret by hand is replaced), the CA's certificate changed, or it has less than 30 days left; the pods then roll to it |
| The private CA (EKS) | 825 days | it is made only when its Secret is missing (section 6.2). Its certificate is issued again, from the same key, when it lacks the extensions of section 5.1 |

So run `install.sh` again at least every 30 days: a run renews the admin's token only in its last 30 days, so with longer gaps the token can expire between runs, and the admin then has no working token until the next run issues a new one. Engines' tokens are yours to rotate (the [user manual](user.md#5-administering-the-catalog)).

### 6.2 Replacing the certificate or the CA by hand (EKS)

To replace the server certificate now, for a key that may have leaked, delete its Secret and run `install.sh` again: it makes a new certificate, and the pods roll to it. Clients keep trusting the same CA.

```bash
kubectl -n colvenna-catalog delete secret colvenna-catalog-tls
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --from-source   # or --release <name>
```

The CA is made only when the Secret `colvenna-catalog-ca-key` is missing. Deleting it as well, before the rerun, gives a new CA and a new certificate; every client must then trust the new CA before it can connect again. This has not been run.

**A CA made before #58.** Installs before #58 made the CA's certificate without `keyUsage` (and the server certificate without it too), so a client that checks X.509 strictly refuses the server: in Python 3.13 and later, `certificate verify failed: CA cert does not include key usage extension`. Java 21 and `curl` accepted that CA when this was run (below). Run `install.sh` again, with the same configuration, from a checkout that has the fix. It then:

- issues the CA's certificate again from the CA's own key, with the same name and the extensions of section 5.1, for another 825 days, and replaces `ca.crt` in `colvenna-catalog-ca-key`, the CA's Secret; the run says so (`the private CA's certificate lacked keyUsage ... issued again from the same key and name`);
- makes a new server certificate with its extensions, signed by that key, and the pods roll to it.

Clients that trust the earlier copy of `ca.crt` keep connecting: the new server certificate verifies with it, as with the new copy, because both copies have the same name and key. Only clients that check strictly need the new copy: copy `ca.crt` out of the Secret again for them (the [user manual](user.md#41-what-every-engine-needs), step 3). The earlier copy still expires on its own date, so replace it in the other clients' trust stores before then. This ran live on an EKS install in `us-west-2` made before the fix (#58): after the rerun, Python 3.13's default context refused the earlier copy and accepted the new one, and Java 21 and `curl` accepted both.

### 6.3 Upgrading

First take a manual snapshot of the metadata database ([user manual](user.md#61-what-is-backed-up), section 6.1): it is the way back if the newer version moves the schema forward. Then run `install.sh` with the same configuration from the newer code: a checkout of a newer commit with `--from-source`, or a checkout of a newer candidate's or release's tag with `--release <name>`. Keys the newer version added take their defaults.

- **EKS.** The chart rolls the Deployment one pod at a time, never below two ready replicas (`maxUnavailable: 0`). A replaced pod stops so as not to refuse its clients (#64). Kubernetes runs the pod's `preStop` hook before it sends the server SIGTERM, and meanwhile takes the pod out of the Service's endpoints ([pod termination flow](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow)); the hook waits 15 seconds (`shutdown.preStopSeconds`), so that every node's Service rules drop the pod while it still serves. On SIGTERM the server fails its readiness, closes each connection after its answer (the client's next request goes to another replica), takes new connections for 5 more seconds (`COLVENNA_SHUTDOWN_DRAIN`), and then stops taking them and gives the requests in flight up to 20 seconds (`COLVENNA_SHUTDOWN_TIMEOUT`); the pod's grace period is the three plus 10 seconds. The pods being replaced stop as their own version does: an upgrade from a version before #64 still stops its old pods the old way, at once, and a client may then see one request fail. The wait is reckoned for the Service's rules; the internal NLB's deregistration of a target was not measured against it (`shutdown.preStopSeconds` is the chart's value to raise). This ran live on an EKS install in `us-west-2` from one commit with it to a later one (#64): with UAT-DEP-06's loops running, the rolling upgrade, a `helm rollback` and the upgrade again failed none of 719 commits, 2,894 reads and 583 credential requests, and a pod deleted by hand was out of the endpoints at once, got SIGTERM 16 seconds later and stopped taking connections 5 seconds after that. Each new pod migrates the database schema as it starts, under a PostgreSQL advisory lock, so replicas migrate one at a time; old replicas keep serving the migrated schema until they are replaced.
- **Serverless.** The maintenance function alone takes the new version first and runs `migrate`: `install.sh` updates the deployed `Serverless` stack with only that function (and its role's policy) changed, not with the newer version's whole stack, which would publish a new version of the server function and move the alias `live` (#63). Only once `migrate` has succeeded does the whole stack take the newer version, and the API moves to the server's new version at once (the alias `live`). Upgrading a live serverless install has run live the way before #63, which deployed the whole stack first: from source, and in `v1.0.0-rc.2`'s UAT (UAT-DEP-06, `us-west-2`) from a commit of the early private repository to that candidate; this way has run offline only so far.
- **Compatibility.** Before 1.0, versions do not promise compatibility with each other, and an upgrade may need steps of its own: the release notes of each candidate say. The upgrade from the previous release is a UAT case of every candidate.
- **Going back** to the previous version depends on whether the newer one added migrations: whether its list of released migrations (`Migrations.VERSIONS` in `src/main/java/com/colvenna/catalog/db/Migrations.java`) is longer than the previous version's. The release's UAT runs whichever applies (UAT-DEP-07); `v1.0.0-rc.2`'s ran the newer-schema way, the refusal and the restore, in both forms (the EKS form passed in `us-west-2`; the serverless form met [#54](https://github.com/colvenna/colvenna-catalog/issues/54) and [#63](https://github.com/colvenna/colvenna-catalog/issues/63)).
  - **The same schema.** In the EKS form, roll the Helm release back to the previous version's last revision, with kubectl and Helm access to the cluster (the install's own are `.state/<env>/kubeconfig` and `.tools/helm`):

    ```bash
    helm history colvenna-catalog --namespace colvenna-catalog
    helm rollback colvenna-catalog <revision> --namespace colvenna-catalog --wait --timeout 10m
    ```

    A release candidate's chart is named with its version in the `CHART` column (`colvenna-catalog-1.0.0-rc.1`), and a release's with its own (`colvenna-catalog-1.0.0`); from source every chart is `colvenna-catalog-0.1.0`, and `helm get values colvenna-catalog --namespace colvenna-catalog --revision <revision>` shows the commit in the pod annotation `colvenna-catalog/source-sha`. In the serverless form, run the previous version's `install.sh` from a clean checkout of it, with the configuration as it takes it (keys it does not know left out): it moves the API back to the previous image, and its `migrate` finds nothing to do.
  - **The schema moved forward.** A server refuses to start on, or to serve, a schema newer than its own, and writes nothing: in the EKS form the older pods never become ready, and the newer ones go on serving; in the serverless form the older `migrate` refuses, before the server function changes: the API goes on with the newer version, and only the maintenance function is left on the older image, until the newer version's `install.sh` runs again. A version before #63 (`v1.0.0-rc.2` and earlier) deploys its whole stack first, so its refusal comes after it has published a version of the server function, on the newer image with its own settings, and moved the alias to it. The way back is the snapshot taken before the upgrade: restore it as the [user manual](user.md#62-restoring) (section 6.2) says, onto a new instance, with the writing jobs stopped, and install the previous version onto that. Expect an interruption while the install moves to the restored instance; what was written since the snapshot is then reconciled as that section says.

### 6.4 Installing again onto kept data

`uninstall.sh` without `--delete-data` keeps the bucket, the database and, in the EKS form, the CA (section 8). To install again onto them, read the bucket's name and the database's endpoint and admin secret from the `Storage` stack:

```bash
aws cloudformation describe-stacks --region <region> --stack-name ColvennaCatalog<Env>Storage \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName` || OutputKey==`DbEndpoint` || OutputKey==`DbAdminSecretArn`]' --output table
```

and set them in the configuration, with `COLVENNA_BUCKET=<BucketName>`, `COLVENNA_DB=existing`, `COLVENNA_DB_ENDPOINT=<DbEndpoint>` and `COLVENNA_DB_ADMIN_SECRET_ARN=<DbAdminSecretArn>`. `install.sh` then leaves the `Storage` stack as it is, and finds every table, principal and grant again; it issues a new admin token if the kept one is gone. In the EKS form this has run live. A serverless install that made its own network (`COLVENNA_NETWORK=create`) keeps that key: its kept `Network` stack holds the database, and the rerun finds it unchanged (`v1.0.0-rc.2`'s UAT-DEP-08, S1, both Regions). Without `COLVENNA_BUCKET`, preflight refuses `COLVENNA_DB=existing` while the `Storage` stack holds a database: deploying the stack again without it would delete it. Once an install runs on another database than the stack's, a restored one, preflight refuses `COLVENNA_DB=create` too, which would move it back.

## 7. When something fails

| Exit status | Meaning |
| --- | --- |
| 0 | Done |
| 1 | A step failed; the message names it: `install.sh: failed at step "<step>" (status <n>); fix the cause and run it again` |
| 2 | The arguments or the configuration are wrong (each error as `config: <KEY>: why`), or the install was not confirmed |
| 3 | Preflight refused, with the reason, before anything changed: a tool missing or too old, a tree not clean, not the tag of the candidate or the release, the release registry lacking it, the wrong account, the cluster, NetworkPolicy, the database, the network |

- **Fix the cause and run the same command again.** Every step checks what is there first, so a run that failed, or was interrupted (a lost session, Ctrl-C), carries on. A run killed while Helm was changing the release leaves it `pending-*`; the next run of the same checkout rolls it back to the last deployed revision (or uninstalls a first install that never deployed) and goes on. A pending revision this checkout did not start is left alone, and the run says how to recover it by hand (`helm rollback` or `helm uninstall`).
- **Evidence.** Each run writes `.state/<env>/evidence/install-<time>/`: `RESULT`, the smoke check's `smoke.txt`, and the logs of the CDK (`cdk.log`), Helm (`helm.log`), the build (`mvn.log`) and the image (`image.log`); the serverless form adds the maintenance function's output. `.state/<env>/install.json` records what is installed. On a failure, the output ends with the last lines of the failing step's log, where it has one.
- **The catalog's own log.** EKS: `kubectl -n colvenna-catalog logs -l app.kubernetes.io/name=colvenna-catalog --prefix`. Serverless: the log groups of section 5.2. The [user manual](user.md#8-troubleshooting) lists what the API's answers mean.

## 8. Uninstalling

```text
Usage: uninstall.sh --config <file> [--yes] [--delete-data]
```

```bash
AWS_PROFILE=<profile> ./uninstall.sh --config deploy/config/<env>.env
```

It asks for the environment's name, unless `--yes`. Run it again after a failure or an interruption: what is gone is skipped. By default it keeps the data:

| Form | Removed | Kept |
| --- | --- | --- |
| `eks` | The internal NLB (waiting until its load balancer is gone), the Helm release, the catalog's Secrets and ConfigMap but the CA, the smoke check's namespaces, and the `Eks` stack: its roles, the database user secret and the image repository | The `Storage` stack with the bucket and the database, the CA Secret, and the namespace |
| `serverless` | The `Serverless` and `Images` stacks, and the log groups their functions and the API write; then the `Network` stack, unless a kept database lives in it (it waits up to 45 minutes for Lambda to release the functions' network interfaces) | The `Storage` stack with the bucket and the database, and the `Network` stack that holds that database |

A later install finds the data again (section 6.4).

Once a stack is gone, so are the log groups of the functions CloudFormation named after it (CDK's, such as the `Storage` stack's log retention function), whose names it may have cut short (`/aws/lambda/ColvennaCatalog<Env>Storag-...`): a name counts as the stack's when what comes before its first hyphen is the stack's name, or the first 23 characters of it or more, which no other environment's stack names share. Another environment's groups are never touched, even one whose name starts with this one's. Those of a stack that stays stay with it.

`--delete-data` also deletes the database the install made (without a final snapshot, and with its automated backups), empties and deletes the buckets it made, and removes the `Storage` stack, the database's and its functions' log groups and, in the EKS form, the CA. It asks for the environment's name even with `--yes`, from the terminal or from its standard input:

```bash
printf '%s\n' <env> | AWS_PROFILE=<profile> ./uninstall.sh --config deploy/config/<env>.env --yes --delete-data
```

The warehouse bucket the install made writes S3 server access logs to the access log bucket it made. S3 delivers those logs late, most within a few hours of the requests, and a change to a bucket's logging takes effect over time ([S3's guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html)), so the access log bucket can take new logs as soon as it is emptied. Before anything is deleted, `--delete-data` therefore turns the warehouse bucket's logging off and takes from the access log bucket's policy its grant to S3's logging service, the only grant the service writes under (the bucket takes no ACLs: [log delivery permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-server-access-logging.html#grant-log-delivery-permissions-general)); the rest of the policy, which refuses requests without TLS, stays. It then deletes the database, the warehouse bucket, and the access log bucket last. A bucket that is not empty again when it is deleted is emptied again every 30 seconds, for 20 attempts and 10 minutes at most; past that, the run fails saying so. A bucket that cannot be deleted does not keep the others from being deleted, and the `Storage` stack then stays, so that a run again finds them; each step is made only if it is still to make, so a run again carries on.

Both forms have run this live (us-west-2, 2026-10-30): `--delete-data` removed an EKS install and a serverless install, each in one run, while their access log buckets already held server access logs. Neither run had to empty a bucket again, so the retry has run only in the offline tests. The log groups of both `Storage` stacks' log retention functions, whose names CloudFormation had cut to `ColvennaCatalog<Env>Storag-...`, went with them; the first uninstall left the other install's group alone. `tests/serverless/verify-clean.sh` then found nothing of either install left.

If you restored the database onto a new instance ([user manual](user.md#62-restoring), section 6.2), delete that instance yourself before `--delete-data`, and its log group (`/aws/rds/instance/<instance>/postgresql`): the install did not make it, and it uses the `Storage` stack's subnet group, security group and parameter group, which the stack cannot delete while the instance exists. Until you do, `--delete-data` refuses (exit 3), naming the instance, before it changes anything: it refuses any instance the install did not make that uses the stack's subnet group or security group or, in the serverless form with `COLVENNA_NETWORK=create`, lives in the VPC the install made.

It never deletes what you brought. With `COLVENNA_BUCKET` and `COLVENNA_DB=existing`, the catalog's database and user stay in your PostgreSQL, and its objects under `COLVENNA_WAREHOUSE_PREFIX` stay in your bucket. Remove them yourself, once the uninstall has finished, if you no longer need them; `--delete-data` ends by printing these commands with the install's own names. As the database's admin user, connected to another database (`postgres`):

```sql
GRANT "colvenna_catalog" TO CURRENT_USER;
DROP DATABASE "colvenna_catalog";
DROP ROLE "colvenna_catalog";
DROP ROLE "colvenna_catalog_authz";  -- the serverless form only
```

with your own `COLVENNA_DB_USER` (the role) and `COLVENNA_DB_NAME` (the database) if you changed them. The `GRANT` comes first because the catalog's user owns its database (`install.sh` makes it so), and only the owner of a database, or a role that has the owner's privileges as a member of its role, may drop it ([PostgreSQL: privileges](https://www.postgresql.org/docs/16/ddl-priv.html)). The admin user of RDS and Aurora is no superuser ([RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.MasterAccounts.html), [Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/UsingWithRDS.MasterAccounts.html)): without the `GRANT`, `DROP DATABASE` fails with `must be owner of database`. In PostgreSQL 16 the admin holds the role it made with the admin option but without its privileges; with that option it may grant itself the role, which then gives it them ([PostgreSQL: role attributes](https://www.postgresql.org/docs/16/role-attributes.html)), and `DROP ROLE` removes the membership with the role. The EKS form's statements ran on RDS PostgreSQL 16 in `us-west-2` (#73); the serverless form's `_authz` user has been dropped the same way after a UAT run there (#52). Then remove the objects: `aws s3 rm s3://<bucket>/<prefix>/ --recursive`. In the EKS form the namespace stays in every case; delete it when nothing else lives there.

The live uninstall of the serverless form took about 22 minutes keeping the data, most of it waiting for Lambda to release the network interfaces, and about 10 more with `--delete-data`.

## 9. Regions

The configuration takes any standard Region (`<area>-<name>-<n>`), and refuses the China Regions and GovCloud. Nothing in the configuration but `COLVENNA_REGION` (and the Region in an ARN or endpoint) changes between Regions. What each Region needs of its own:

- the CDK bootstrap, in each Region and account you install into;
- the serverless form: API Gateway's CloudWatch Logs role, which is a setting of each Region; the Lambda concurrency and, with a new network, a VPC to spare there; and two zones offering the `sts`, `cognito-identity` and `execute-api` endpoint services and RDS PostgreSQL on `db.t4g.small`, which `install.sh` picks (the first two by name) and keeps for the life of the `Network` stack;
- the EKS form: the Region's RDS certificate bundle, which `install.sh` downloads.

The release registry may be in another Region than the install: `crane` copies the image by digest from it. So far the registry and the installs have been in the same Region.

Every live install so far ran in us-west-2. A release candidate names the Regions it is accepted in (by default us-west-2 and us-east-2), and its UAT runs every required case in each of them.

> Note: the issue and pull request numbers (such as #93) in this document are those of the project's early private repository; this repository has no pages for them. Linked issue numbers (such as [#14](https://github.com/colvenna/colvenna-catalog/issues/14)) are this repository's current ones.
