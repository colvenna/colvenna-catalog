# Cost

What Colvenna Catalog costs in your AWS account, in each form: what is billed for every hour it is installed, what grows with requests, what is still billed when nothing calls it, and what is not the catalog's at all (your metadata database and your network), with the unit price, its source and the formula of each item, so that you can recompute it for your own sizes and Region. The settings named here are those of the [deployment guide](install.md), section 3. Section 9 checks the estimate against this repository's test installs: their bill, and the usage their metrics recorded.

The software itself is free (Apache License 2.0) and has no service fee. Everything below is what AWS charges for the resources an install makes or uses.

## 1. Basis

- **Prices.** On-Demand unit prices from the AWS Price List API (`aws pricing get-products`), fetched on 2026-10-29. Each table names the `usagetype` it uses; section 10 shows how to fetch them. Tiered prices are the first tier's.
- **Region.** `us-west-2`. Every unit price used here is the same in `us-east-2`, the other Region release candidates are accepted in (fetched the same day). Section 10 recomputes for another Region.
- **Currency and time.** US dollars; a month is 730 hours (2,628,000 seconds). Sizes are in GiB, multiplied by AWS's per-GB price as AWS bills them.
- **Not counted.** Tax, support plans, Savings Plans and reservations, credits, enterprise discounts, and free tiers. Where a free tier applies (Lambda, API Gateway, KMS, CloudWatch), the item says so; the figures are without it.
- **What an install makes.** Read from the code at `main` `619eb01` (2026-10-29), and the serverless form's purge function, its schedule, alarms and metric since (#69): the CDK stacks in [`deploy/aws/src/`](../../deploy/aws/src), the chart's [`values.yaml`](../../deploy/helm/colvenna-catalog/values.yaml), and the defaults of [`deploy/config/example.env`](../../deploy/config/example.env) (`COLVENNA_DB=create`, `COLVENNA_NETWORK=create`, `COLVENNA_DATA_TOKENS=aws-sts`, `COLVENNA_LAMBDA_MEMORY=2048`, `COLVENNA_LAMBDA_CONCURRENCY=10`, `COLVENNA_SNAPSTART=off`, `COLVENNA_INTERNAL_NLB=off`).
- **Labels.** Each figure is one of:
  - **list price**: a unit price and a quantity the code fixes;
  - **measured**: read from a test install's CloudWatch metrics, the evidence of its runs, or the bill (section 9);
  - **derived**: computed from a measured quantity and a list price;
  - **not measured**: a unit price and a formula only.

  Sections 2, 5.3 and 7 summarise the figures of the sections they name, and section 9's tables are the bill and the metrics as measured; other tables without a label column hold list prices only.
- **Upstream facts** other than unit prices come from AWS's pages, read on 2026-10-29: [Lambda pricing](https://aws.amazon.com/lambda/pricing/) (SnapStart's cache, its 3-hour minimum and its exemption for Java managed runtimes; the free tier) and the [INIT phase billing announcement](https://aws.amazon.com/blogs/compute/aws-lambda-standardizes-billing-for-init-phase/) (initialization billed for every function since 2025-08-01, for container images before); [API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/) (no data transfer out for private APIs; the free tier) and its [metrics](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-metrics-and-dimensions.html) (detailed metrics are charged); [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) (metrics prorated by the hour and charged only in hours with data); [Cognito pricing](https://aws.amazon.com/cognito/pricing/) (identity pools at no charge); [KMS pricing](https://aws.amazon.com/kms/pricing/) (AWS managed keys: no monthly fee, requests charged, 20,000 free a month); [ECR pricing](https://aws.amazon.com/ecr/pricing/) (transfer within the Region free); the [RDS FAQ](https://aws.amazon.com/rds/faqs/) (free backup storage) and [RDS for PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/) (T4g in Unlimited mode).

## 2. Summary

Per month, in `us-west-2`:

| | EKS form | Serverless form |
| --- | --- | --- |
| Fixed: the catalog's own resources | $0.43: one secret and one image. The two pods run on your nodes, which you pay for anyway; their requests (1 vCPU, 2 GiB in all) are worth about $30.37 at `t3.large` prices (section 4.1) | $1.26: one secret and one image ($0.43), the purge function's runs every 15 minutes ($0.33), its two alarms and one metric ($0.50) |
| Idle: no request all month | The same as fixed: the two replicas keep running | $1.26: of compute only the purge function's scheduled runs, no request charge |
| 1 million requests a month | $0.43 and the pods' share of your nodes, as fixed: requests add no charge of their own (section 4.2) | $11.69 to $25.23 (section 5.3) |
| 10 million requests a month | The same | $65.10 to $200.43 (section 5.3) |
| Optional | Internal NLB: +$16.43, idle or not (section 4.3) | SnapStart: +$7.91, idle or not (section 5.4) |
| Not included: the metadata database | Yours (`COLVENNA_DB=existing`), or the RDS PostgreSQL that `COLVENNA_DB=create`, the default, makes: $26.06 (section 6.1) | The same |
| Not included: the network | Your cluster and VPC (section 6.3) | Yours (`COLVENNA_NETWORK=existing`), or the VPC that `create`, the default, makes: $43.91, nearly all for its three interface endpoints (section 6.2) |
| Idle, with what the defaults make | $26.49 (the catalog's $0.43 and the database), and the pods' share of your nodes | $71.23 (the catalog's $1.26, the database and the network) |

- The fixed cost of the catalog's own resources is a few cents above a Secrets Manager secret. What decides the bill is the form: the EKS form takes a share of your nodes whether or not anyone calls it; the serverless form costs next to nothing to run while idle (the purge function's scheduled runs, #69) and is billed per request.
- The defaults make a database and, in the serverless form, a network; both are billed by the hour, idle or not. With your own (`existing`), only the catalog's own resources are added to your bill.
- The serverless range is wide because request durations vary: the test runs measured a median of 47 ms and a mean of 453 ms (section 5.2). Your own mean, from the server function's `Duration` metric, gives your figure (section 5.3).
- [Section 9](#9-estimate-against-the-bill) says what was checked against the bill and the metrics, and what was not.

## 3. What both forms have

### 3.1 The image repository (ECR)

Each install has its own repository, `colvenna-catalog-<env>/colvenna-catalog`: the image built from source, or a release candidate's, copied there by digest. Tags are immutable, a lifecycle rule keeps the newest 10 images, and the repository goes with its images when the catalog is uninstalled (`deploy/aws/src/eks-stack.ts`, `images-stack.ts`).

| Item | Quantity | Unit price | Per month | Label |
| --- | --- | --- | --- | --- |
| Image storage (`TimedStorage-ByteHrs`) | One image, 0.2533 GiB (`v1.0.0-rc.1`: 271,990,763 bytes, `imageSizeInBytes`) | $0.10/GB-month | $0.03 | derived |
| At the lifecycle limit | 10 images of that size, after nine upgrades | $0.10/GB-month | $0.25 | derived |

- Images that share layers may be billed for less than the sum of their sizes; that was not checked.
- Transfer within the Region should be free (ECR's pricing page: transfer between ECR and other services in the same Region is free), but every EKS test install's bill showed one image's size of transfer out of the Region from its repository, at $0.02/GB: about $0.005 an install, more for days of reruns; the serverless install's showed none. The cause was not found (section 9.2). Scanning on push (basic scanning) has no price in ECR's price list.
- The serverless form's live test pushes its driver images to the same repository and removes them when it is taken down.

### 3.2 Secrets Manager

| Form | Secrets | Unit price | Per month |
| --- | --- | --- | --- |
| EKS | The catalog's database user (`colvenna-catalog-<env>/db-user`, EKS stack) | $0.40/secret-month (`AWSSecretsManager-Secrets`) | $0.40 |
| Serverless | The first admin's token (`colvenna-catalog-<env>/admin-token`, serverless stack); the functions sign in to PostgreSQL with RDS IAM tokens and hold no password | $0.40/secret-month | $0.40 |
| Either, with `COLVENNA_DB=create` | The database's admin user (`colvenna-catalog-<env>/db-admin`), counted with the database (section 6.1) | $0.40/secret-month | $0.40 |

API calls cost $0.05 per 10,000 (`AWSSecretsManagerAPIRequest`). Only `install.sh` reads or writes the secrets, a few times per run, and you when [rotating them by hand](database-secrets.md); the server never calls Secrets Manager (the EKS pods read the database password from a Kubernetes Secret). The TLS certificate, its private CA and the EKS form's admin token are Kubernetes Secrets, made with `openssl`: no AWS Private CA and no ACM certificate is used.

### 3.3 KMS

No customer managed key is made: $0 a month (`KMS-Keys`, $1 per key, does not apply). Secrets Manager, RDS storage encryption and Lambda's environment variables use the AWS managed keys (`aws/secretsmanager`, `aws/rds`, `aws/lambda`), which have no monthly fee. Requests to them are billed at $0.03 per 10,000 (`KMS-Requests`) beyond the account's free 20,000 a month. The catalog's requests come from the secret reads and writes of `install.sh` and from starting the database and the functions; they were not counted, and the bill does not attribute them to an install (section 11). The warehouse bucket uses SSE-S3, keys that S3 manages itself, not KMS ([S3's guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html)).

### 3.4 S3: the warehouse

The warehouse is a prefix in an S3 bucket: yours (`COLVENNA_BUCKET`), or one the storage stack makes (with its access log bucket). Its data is your tables'.

| Item | Unit price | What it grows with | Label |
| --- | --- | --- | --- |
| Storage, S3 Standard (`TimedStorage-ByteHrs`) | $0.023/GB-month, first 50 TB | Your tables: data files, manifests, snapshots and schemas. The bucket has no versioning; its one lifecycle rule aborts multipart uploads after 7 days | not measured |
| PUT, COPY, POST, LIST (`Requests-Tier1`) | $0.005 per 1,000 | Engines write data and manifest files with their own or per-table credentials. The server itself writes a schema file per table created or altered, and one snapshot file per commit (a conditional `PutObject`), and reads and checks other files through Paimon's file I/O | not measured |
| GET and the rest (`Requests-Tier2`) | $0.0004 per 1,000 | Engines' reads; the server's reads at commit | not measured |
| Server access logs, when the stack makes the bucket | $0.023/GB-month for the log objects, kept 365 days; no charge for delivery ([S3's guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html)) | One log record per request to the warehouse bucket | not measured |

- The server's own S3 cost is `C × (P × 0.000005 + G × 0.0000004)` a month for `C` commits, with `P` PUT-class and `G` GET-class requests per commit: `P` is at least 1 (the snapshot), and neither was counted (section 11).
- Both forms reach S3 inside the Region: the EKS form as your cluster's network allows, the serverless form through the S3 gateway endpoint, which is free.
- Section 9 gives the request counts the bill showed for the test installs' buckets. They include the tests' own requests and cannot be split from the server's.

## 4. The EKS form

### 4.1 Fixed

| Item | Quantity | Unit price | Per month | Label |
| --- | --- | --- | --- | --- |
| Database user secret | 1 | $0.40/secret-month | $0.40 | list price |
| Image repository | 1 image (up to 10) | $0.10/GB-month | $0.03 ($0.25) | derived |
| IAM roles (server, data), the chart's Service, NetworkPolicy, PDB | 2 roles; Kubernetes objects | No charge | $0 | list price |
| **The catalog's own** | | | **$0.43** | |
| The two pods, on your nodes | 2 replicas, each requesting 500m CPU and 1 GiB (limits 2 CPU, 2 GiB; [`values.yaml`](../../deploy/helm/colvenna-catalog/values.yaml)) | Your nodes' price | About $30.37 at `t3.large` prices | list price, a reference |

**The pods.** They are not a separate bill: they take room on the nodes of your cluster. The reference figure is their CPU requests as a share of the test environment's nodes: a `t3.large` ($0.0832/hour, `BoxUsage:t3.large`; 2 vCPU, 8 GiB) costs $60.74 a month, and two replicas request 1 vCPU, half its CPU (and a quarter of its memory): $60.74 / 2 = $30.37. On your own nodes, use their price per vCPU. The chart keeps two replicas and no autoscaling ([1.0 design](../design/v1.0-design.md) section 6.1), and spreads them across two nodes. Whether they need nodes of their own depends on how full your nodes are.

### 4.2 With requests

Requests add no charge of their own: the pods are the same two whatever they serve. What grows:

| Item | Unit price | What it grows with | Label |
| --- | --- | --- | --- |
| S3 requests and storage | Section 3.4 | Commits and table changes | not measured |
| Data between Availability Zones (`DataTransfer-Regional-Bytes`) | $0.01/GB each way | Engines calling a pod in another zone; pods calling a single-AZ database in another zone | not measured |
| STS (`AssumeRoleWithWebIdentity`, per-table credentials) | No charge | Credential requests | list price |
| Your nodes' CPU | Your nodes' price | Under load a pod may burst to its limit of 2 CPU | not measured |

The catalog's logs go to the pods' standard output: whatever your cluster does with them (Container Insights, Fluent Bit) is your cluster's cost.

### 4.3 Optional: the internal NLB

With `COLVENNA_INTERNAL_NLB=on`, the AWS Load Balancer Controller (yours) makes an internal Network Load Balancer on the API port, for clients in the VPC outside the cluster.

| Item | Unit price | Per month | Label |
| --- | --- | --- | --- |
| NLB hours (`LoadBalancerUsage`, Network) | $0.0225/hour | $16.43, idle or not | list price |
| NLB capacity units (`LCUUsage`, Network) | $0.006 per NLCU-hour | With traffic; for TCP an NLCU is 800 new connections a second, 100,000 active connections or 1 GB processed an hour, billed on whichever is used most ([ELB pricing](https://aws.amazon.com/elasticloadbalancing/pricing/)) | not measured |

It is internal and uses no public IPv4 address. Cross-zone load balancing is off by default for NLBs ([NLB guide](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html#cross-zone-load-balancing)); a client in another zone than its target pays the transfer between zones.

### 4.4 When idle

Everything above is still billed: the two replicas keep running (the chart has no scale to zero), the secret and the image stay, and the NLB, if on, is billed by the hour. Idle, the EKS form costs what it costs fixed: $0.43 of its own, the share of your nodes, and $16.43 with the NLB.

## 5. The serverless form

### 5.1 Fixed

| Item | Quantity | Unit price | Per month | Label |
| --- | --- | --- | --- | --- |
| Admin token secret | 1 | $0.40/secret-month | $0.40 | list price |
| Image repository | 1 image (up to 10) | $0.10/GB-month | $0.03 ($0.25) | derived |
| Lambda functions: server (2048 MB, x86_64), authorizer and ops (256 MB, arm64), maintenance and purge (2048 MB, x86_64) | 5, reserved concurrency 10 for the server and the authorizer, 1 each for maintenance and purge | No charge without invocations; reserved concurrency is free | $0 | list price |
| The purge function's schedule (#69): every 15 minutes, 2,880 runs a month | 1 schedule | A run with nothing to delete measured 2.7 to 3.2 s at 2048 MB (2026-10-29): about 19,000 GB-seconds a month; a run that deletes the files of a rollback, a tag and a branch takes longer, 79.6 s for 3,846 files (#93, 2026-10-30), about $0.003 at $0.0000166667, and 2,880 requests at $0.20 per million; EventBridge Scheduler: 14 million invocations a month free ([pricing](https://aws.amazon.com/eventbridge/pricing/)) | $0.33 | derived |
| Private REST API, stage `catalog`, Lambda authorizer | 1 | No charge without requests; no API cache | $0 | list price |
| Cognito identity pool (developer identities only), the data role, five function roles and the purge schedule's role | 1 pool, 7 roles | Identity pools: "provided at no charge" (Cognito pricing page); IAM and STS: no charge | $0 | list price |
| Log groups: five functions and the API's access log, kept one year | 6 | $0.03/GB-month stored (`TimedStorage-ByteHrs`) | Grows with use: about 1.3 GiB ingested per million requests (section 5.2) | derived |
| CloudWatch alarms and metric: the purge function's errors and backlog alarms, and the backlog metric of its runs' summaries (#69) | 2 standard-resolution alarms, 1 custom metric | $0.10 per alarm metric a month, $0.30 per custom metric a month ([CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)); the metric has data every hour | $0.50 | derived |
| **The catalog's own** | | | **$1.26** | |

The functions run in your VPC's private subnets; their network interfaces cost nothing. What the VPC needs (the endpoints the functions and the clients use) is network, section 6.2.

### 5.2 Per request

Every request to the catalog crosses API Gateway, the authorizer (unless its answer is cached) and the server function. Per request:

| Item | Unit price | Per request | Per million requests | Label |
| --- | --- | --- | --- | --- |
| API Gateway REST API (`ApiGatewayRequest`) | $3.50 per million, first 333 million a month | 1 request, if the authorizer lets it through (section 9.3) | $3.50 | list price |
| Server function requests (`Request`) | $0.20 per million | 1 invocation | $0.20 | list price |
| Server function duration (`Lambda-GB-Second`, x86_64) | $0.0000166667/GB-second, first 6 billion GB-seconds | duration × 2 GB | $0.033333 per ms of mean duration: $1.57 at 47 ms, $15.10 at 453 ms | derived |
| Log ingestion (`DataProcessing-Bytes`) | $0.50/GB | 1,433 bytes per request past the authorizer: the server's log 827, the access log 445, the authorizer's 161 (the refused requests' entries included) | $0.667 | derived |
| Detailed API metrics (`CW:MetricMonitorUsage`) | $0.30 per metric-month, prorated by the hour, billed only in hours with data | 5 metrics per HTTP method used (the stage has `metricsEnabled`): GET, POST and DELETE, 15 in all | At most $4.50 a month, if every hour has requests: not per request | derived |
| Authorizer (`Request-ARM`, `Lambda-GB-Second-ARM`) | $0.20 per million; $0.0000133334/GB-second | Only when its 60-second cache misses: at most one call per bearer token per minute, 157 ms mean at 256 MB, $0.00000072 a call | At most $0.032 a month per token used every minute | derived |
| Cold starts (initialization, billed for container images) | $0.0000166667/GB-second | 2.1 to 2.4 s of initialization at 2 GB per new execution environment, as AWS bills container images; the test installs' bill did not reconcile with the recorded cold starts hour by hour (section 9.3) | $0.073 per 1,000 cold starts | derived |
| Execution logs (ERROR level, `API-Gateway-Execution-Logs_<api>/catalog`) | $0.50/GB | Only errors of API Gateway itself: none was written in the test runs | $0 | measured |

- **The measured durations.** From the server function's `Duration` metric over every invocation of the serverless test installs on 2026-10-28 (272 invocations: the live test's create, commit and read cycle, its refusals and its cold-start bursts, and `install.sh`'s smoke checks): median (p50) 47 ms, p90 1,134 ms, p99 4,764 ms, mean 453 ms. The mean includes each new environment's first invocation (1.7 to 2.5 s) and the commits; a steady load of warm reads is nearer the median. The duration excludes initialization, which is billed on top for container images (2.1 to 2.4 s per cold start measured; billed for every function since 2025-08-01, and for container images before).
- **Memory.** The server function used at most 239 MB of its 2048 MB. `COLVENNA_LAMBDA_MEMORY` (1024 to 10240) sets it; Lambda allocates CPU in proportion to memory ([Lambda's guide](https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html)), so a smaller setting halves the price per millisecond but may lengthen each request. Only 2048 MB was measured.
- **Refused requests.** A request the authorizer refuses reaches API Gateway and the authorizer (when not cached), never the server. API Gateway's pricing page does not say whether it is billed; on the test installs' bill it was not: the billed requests were exactly those that reached the server (section 9.3).
- **The network's share.** Each request also passes an `execute-api` interface endpoint, $0.01 per GB processed (`VpcEndpoint-Bytes`), and the server function reaches the database and S3: those are network and database, section 6. A private API has no data transfer out charge (API Gateway pricing page).

### 5.3 Example usage levels

The formula, per month:

```text
serverless = 1.26                                       # fixed (section 5.1)
           + N × (3.50 + 0.20 + 0.033333 × D + 0.667)  # N million requests past the authorizer, D ms mean
           + 4.50                                       # detailed metrics, when every hour has requests
           + authorizer + cold starts                   # cents (section 5.2)
```

| Requests a month | At the measured median, D = 47 ms | At the measured mean, D = 453 ms |
| --- | --- | --- |
| 1 million | $11.69 | $25.23 |
| 10 million | $65.10 | $200.43 |
| 0 (idle) | $1.26 | $1.26 |

- Derived from the measured durations and log sizes of section 5.2 and the list prices; the test runs served 306 requests, not millions, so the steady-state mean of a real workload is not measured.
- Lambda's free tier (1 million requests and 400,000 GB-seconds a month, shared by every function of the account) and API Gateway's (1 million REST calls a month, for new customers in their first year) are not deducted.
- Section 9.3 checks the per-request items against the test installs' metrics and bill.

### 5.4 Optional: SnapStart

With `COLVENNA_SNAPSTART=on`, the server's published version is restored from a snapshot (0.51 to 0.66 s in the test runs) instead of initialized (2.1 to 2.4 s). For a container image, SnapStart is billed (it is free only for Java managed runtimes):

| Item | Unit price | Per month | Label |
| --- | --- | --- | --- |
| Snapshot cache (`Lambda-SnapStart-Cached-GB-S`) | $0.0000015046/GB-second of the function's memory, while the version is active, at least 3 hours | 2 GB × 2,628,000 s = $7.91, idle or not | list price |
| Restores (`Lambda-SnapStart-Restored-GB`) | $0.0001397998 per GB restored | $0.00028 per restore at 2 GB | list price |

Each deploy that changes the server function publishes a new version and deletes the old one, so one version is cached at a time, but a version replaced within minutes is still billed its 3 hours: 21,600 GB-seconds, $0.0325 at 2 GB. The test installs published four versions with SnapStart on (two per install run), alive for about 30 minutes in all, and their bill follows four such 3-hour windows (section 9.3). Each upgrade with SnapStart on thus adds $0.03 to $0.07 to the month's $7.91. It breaks the form's "no charge while idle", which is why it is off by default.

### 5.5 When idle

With no request, API Gateway, the authorizer and the detailed metrics bill nothing, and of Lambda only the purge function's scheduled runs (#69): the serverless form costs its $1.26 of fixed resources (the purge function's runs, alarms and metric among them), plus the storage of the logs it kept (a year at most), plus $7.91 with SnapStart. The test installs' bill had no API Gateway request and no server invocation in the hours without a request (section 9.3). The database and the network are billed idle or not (section 6).

## 6. Not included: your database and network

The metadata database and the network are your infrastructure ([1.0 design](../design/v1.0-design.md), goal 2), and are not in the catalog's figures above. `install.sh` can make a minimal set of both; these are their prices, so that nothing is left out.

### 6.1 The metadata database

`COLVENNA_DB=existing` uses your PostgreSQL (RDS or Aurora) as it is; the catalog adds a database and its users to it. `COLVENNA_DB=create` makes one (`deploy/aws/src/storage-stack.ts`):

| Item | Quantity | Unit price | Per month | Label |
| --- | --- | --- | --- | --- |
| RDS for PostgreSQL 16, `db.t4g.small`, Single-AZ (`InstanceUsage:db.t4g.small`) | 1 | $0.032/hour | $23.36 | list price |
| Storage, gp3 (`RDS:GP3-Storage`) | 20 GiB, encrypted, no storage autoscaling | $0.115/GB-month | $2.30 | list price |
| Automated backups, kept 7 days (`RDS:ChargedBackupUsage`) | Free up to the account's provisioned database storage in the Region (RDS FAQ) | $0.095/GB-month beyond that | $0 while within it | list price |
| CPU credits (`CPUCredits:db.t4g`) | T4g instances run in Unlimited mode: billed when the average CPU over 24 hours exceeds the baseline | $0.075/vCPU-hour | $0 at low load | not measured |
| PostgreSQL logs to CloudWatch Logs, kept a year | Measured: 5 to 25 KB per test install | $0.50/GB ingested, $0.03/GB-month stored | under $0.01 | derived |
| Its admin secret (`db-admin`) | 1 | $0.40/secret-month | $0.40 | list price |
| **The database `install.sh` makes** | | | **$26.06** | |

It is billed by the hour whatever the catalog does, in both forms. For your own database, the catalog adds its load and its connections: in the serverless form, at most 5 per server execution environment (50 at the default `COLVENNA_LAMBDA_CONCURRENCY` of 10) and the authorizer's; in the EKS form, pools of 16 and 4 per replica (`database.poolSize`, `database.auxPoolSize`).

### 6.2 The serverless form's network

`COLVENNA_NETWORK=existing` uses your VPC as it is: the functions need S3, STS and Cognito identity from their subnets (endpoints or a NAT gateway), and clients need an `execute-api` endpoint. `COLVENNA_NETWORK=create` makes a VPC with no internet path (`deploy/aws/src/network-stack.ts`):

| Item | Quantity | Unit price | Per month | Label |
| --- | --- | --- | --- | --- |
| Interface endpoints `sts`, `cognito-identity`, `execute-api` (`VpcEndpoint-Hours`) | 3 endpoints × 2 Availability Zones | $0.01 per endpoint-AZ-hour | $43.80, idle or not | list price |
| Data through them (`VpcEndpoint-Bytes`) | Requests and responses through `execute-api`; STS and Cognito calls | $0.01/GB | With traffic | not measured |
| S3 gateway endpoint, VPC, subnets, security groups | 1; 2 subnets | No charge | $0 | list price |
| VPC flow logs (all traffic) to CloudWatch Logs, kept 3 months (`VendedLog-Bytes`) | Measured: about 0.3 MB an hour during the test runs | $0.50/GB ingested, $0.03/GB-month stored | about $0.11 at that rate | derived |
| **The network `install.sh` makes** | | | **$43.91** | |

`COLVENNA_DATA_TOKENS=disabled` does not remove the `cognito-identity` endpoint. Clients in other VPCs reach the API through `execute-api` endpoints of their own (`COLVENNA_API_ENDPOINT_IDS`): $14.60 a month for one in two zones, plus $0.01/GB, in their account.

### 6.3 The EKS form's cluster and network

The EKS form installs into your cluster and uses its VPC; nothing of either is made. For reference, the cluster of the test environment this repository installs into ([`tests/environment/`](../../tests/environment/README.md)) costs $237.37 a month before data: the EKS control plane ($0.10/hour, `AmazonEKS-Hours:perCluster`, $73.00), the two `t3.large` nodes the installs run on ($121.47) with 40 GiB gp3 each ($0.08/GB-month, $6.40), one NAT gateway ($0.045/hour, $32.85) and its public IPv4 address ($0.005/hour, $3.65); plus its control-plane logs and flow logs. For its UAT cases the test environment also holds two `t3.xlarge` nodes of its clients' own ($0.1664/hour each, `BoxUsage:t3.xlarge`, with 40 GiB gp3 each: $249.34 a month), an `execute-api` endpoint and a database and bucket of its own, about $0.40 an hour more (its README), which a cluster for the EKS form does not need. The catalog's share of such a cluster is section 4.1's.

### 6.4 Engines and account-wide services

- The engines (Flink, Spark, Doris, StarRocks) and their reads and writes of table files are yours, as are the data they move.
- Account-wide services bill per resource where they are on: GuardDuty (S3 data events, RDS login activity, Lambda network activity, EKS protection), CloudTrail data events, AWS Config, Amazon Inspector's enhanced scanning of ECR. They are not counted here.

## 7. After uninstalling

`uninstall.sh` keeps the data unless given `--delete-data`:

| What stays | EKS form | Serverless form | Per month |
| --- | --- | --- | --- |
| The bucket(s) the storage stack made | Kept | Kept | Your tables' size at $0.023/GB-month, plus the access logs |
| The database `COLVENNA_DB=create` made, with its admin secret and logs | Kept, running | Kept, running | $26.06 (section 6.1) |
| The network `COLVENNA_NETWORK=create` made | — | Kept while a kept database lives in it | $43.91 (section 6.2) |
| The catalog's secret, image repository, roles, functions, API, log groups | Removed | Removed | $0 |

So a serverless install made with `create` for both, uninstalled with its data kept, still costs about $69.97 a month, besides its tables' storage, until `uninstall.sh --delete-data`. `--delete-data` deletes the database without a final snapshot and empties and deletes the buckets; nothing of the environment is left billing. Deleting the stacks yourself, without `uninstall.sh`, leaves the buckets (retained) billing. The database has deletion protection, so deleting the storage stack fails and the database keeps running; once protection is off, CloudFormation leaves a final snapshot, billed as backup storage ($0.095/GB-month, at most $1.90 for 20 GiB) until you delete it.

The release registry, `colvenna-catalog-release/*`, is in the maintainer's account only: each candidate keeps its image (0.2533 GiB, $0.03 a month) and its chart (6 KB) for good. Installing a candidate copies the image into the environment's repository (section 3.1); nothing else of the registry is billed to an installer. A release is installed from a copy of its public packages in a registry of your own ([deployment guide](install.md#43-from-a-release-candidate-or-a-release), section 4.3): the image (0.2533 GiB, $0.03 a month) and the chart, billed until you delete the two repositories, which `uninstall.sh` leaves. GitHub does not bill pulls of public packages ("GitHub Packages usage is free for public packages", [GitHub's billing documentation](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-packages/about-billing-for-github-packages)); the copy into ECR is transfer in, and the copy from it into the environment's repository is billed as section 8 says, between Regions when your copy is in another.

## 8. One-off costs

| When | What | Cost | Label |
| --- | --- | --- | --- |
| Install, upgrade | CloudFormation for these stacks; the CDK's log retention function (storage stack) | No charge for CloudFormation; seconds of Lambda | list price |
| Install, upgrade (serverless) | The maintenance function (migrate, bootstrap-admin) and the ops function, invoked by `install.sh` | Measured over the five serverless installs and reruns of 2026-10-28: 7 maintenance invocations (2.6 to 15 s each at 2 GB, 77.6 GB-seconds) and 49 ops invocations (256 MB, 8.6 GB-seconds): $0.0014 in all | derived |
| Install from source | The image pushed into ECR from the installing host | Free into ECR ("no charge for data transfer in", ECR's pricing page). Between Regions, transfer is billed "at Internet Data Transfer rates on both sides" (the same page), so a host in another AWS Region pays its own Region's transfer out; the host is not under an install's tag, so the bill did not show it | not measured |
| Install a release candidate, or a release from your copy | The image copied by digest (`crane copy`) through the installing host | 0.2533 GiB each way: at most $0.03 | derived |
| Restore the metadata database ([user manual](user.md), section 6.2) | A new instance, of the running one's class, from a snapshot or a point in time | Billed as in section 6.1 from its creation; the old instance stays billed until you delete it, and its manual snapshots beyond the free backup storage at $0.095/GB-month | list price |

## 9. Estimate against the bill

### 9.1 How

- **The bill.** AWS Cost Explorer (`aws ce get-cost-and-usage`): unblended cost and usage quantity per usage type, daily and hourly, filtered by the cost allocation tag `Environment`. Every stack of an install tags its resources `Environment=<env>`, and the test environment's stack `Environment=test-<env>`, so the catalog's resources and the test cluster's are billed apart. Read on 2026-10-29; Cost Explorer still marked both days as estimated, so the month's final bill may differ slightly.
- **The usage.** The installs' CloudWatch metrics (`AWS/Lambda`, `AWS/ApiGateway`, `AWS/Logs`, at one-hour periods) and the evidence of their runs.
- The raw outputs name the account and the resources, so they are kept outside the repository; this section has the numbers only.
- **Which installs.** The test installs this repository made for its issues, each from source or from a release candidate, with `COLVENNA_DB=create` and a new bucket (the serverless one also with `COLVENNA_NETWORK=create`), tested, and removed with `uninstall.sh --delete-data` within hours: in the EKS form, #8's on 2026-10-24 (with the internal NLB on), and #9's (from a release candidate), #10's (the compatibility matrix) and #15's (three installs and uninstalls) on 2026-10-28; in the serverless form, #13's on 2026-10-28. Their stacks and chart had the same billable resources and sizes as at `619eb01`, only names and IAM policies having changed since, with one exception: #13's first install also made two secrets for the functions' database passwords, which the code has not made since it moved them to RDS IAM tokens.

### 9.2 The EKS form: four installs' bills

Each install's whole bill under its tag, by usage type (us-west-2):

| Usage type | #8 | #9 | #10 | #15 | Unit price of sections 3 to 6 |
| --- | --- | --- | --- | --- | --- |
| `InstanceUsage:db.t4g.small` (hours) | 2.801 | 0.308 | 3.696 | 0.787 | $0.032/hour |
| `RDS:GP3-Storage` (GB-months) | 0.0753 | 0.0083 | 0.0994 | 0.0211 | $0.115/GB-month |
| `AWSSecretsManager-Secrets` (secret-months; `db-user` and `db-admin`) | 0.0074 | 0.0009 | 0.0101 | 0.0021 | $0.40/secret-month |
| `AWSSecretsManagerAPIRequest` | 273 | 51 | 49 | 147 | $0.05 per 10,000 |
| ECR `TimedStorage-ByteHrs` (GB-months) | 0.00072 | — | 0.00090 | 0.00006 | $0.10/GB-month |
| ECR transfer out of the Region, `USW2-USW1-AWS-Out-Bytes` (GB) | 2.02 | 0.25 | 0.25 | 0.77 | $0.02/GB |
| S3 `Requests-Tier1` / `Requests-Tier2` | 85 / 711 | 13 / 102 | 14,692 / 63,119 | 90 / 336 | $0.005 / $0.0004 per 1,000 |
| Lambda: the storage stack's log retention function (requests; GB-seconds) | 6; 1.12 | 2; 0.40 | 2; 0.38 | 6; 1.15 | $0.20 per million; the duration within the account's free tier, $0 |
| S3 transfer to `us-east-1`, where the tests' host was (GB) | 0.00002 | 0.000001 | 0.0128 | 0.000003 | $0.02/GB |
| **Total** | **$0.1438** | **$0.0166** | **$0.2381** | **$0.0452** | |

- **Every line is its quantity at the list price** of the sections above, On-Demand, except the log retention function's duration, which the account's free tier covered.
- **The database was most of it**: 54% to 68% of each install's bill, as section 6.1 has it. The catalog's own secret and image were a fraction of a cent for those hours, as section 4.1 has it (#8: $0.0015 for its secret, $0.00007 for its image).
- **S3**: the compatibility matrix (#10: eight engine cells writing and reading tables) made 77,811 requests, $0.0987, 41% of its bill; the other installs' smoke checks made a few hundred. The bill cannot split the server's requests from the engines'.
- **Secrets Manager API calls**: 49 to 273 per install day, $0.0014 at most: the stacks' creation of the two secrets and `install.sh`'s reads, more for more reruns.
- **ECR transfer out of the Region.** ECR's pricing page has transfer within the Region free, yet every EKS install showed one image's size (0.25 GB) of transfer out of the Region from the environment's repository, $0.005 each time: once per install for #9, #10 and #15, eight times on #8's day of reruns. The usage type names `us-west-1`, where neither the cluster (in `us-west-2`) nor the installing host was; the serverless install showed none. The cause was not found.
- **Not on the bill under the install's tag**:
  - the pods' share of the cluster. The test cluster's control plane and nodes appear under no `Environment` tag (only its NAT gateway and its public address, data between zones, flow logs and control-plane logs, and the CDK's functions and state machine did), so the bill cannot split the cluster; section 4.1's figure for the pods stays derived;
  - the internal NLB, which the AWS Load Balancer Controller makes with its own tags, not the stacks'; section 4.3's figure stays list price.

### 9.3 The serverless form: the bill and the metrics

The installs of #13 (2026-10-28, from 09:13 to 11:17 and from 12:29 to 13:33 UTC: installed, rerun three times, twice with SnapStart on, live-tested, uninstalled with its data kept, then removed; installed again, live-tested, removed). Its whole bill under its tag, as Cost Explorer had it on 2026-10-29 at 08:51 UTC, against the CloudWatch metrics and the runs' evidence:

| Usage type | On the bill | Measured | What it shows |
| --- | --- | --- | --- |
| `ApiGatewayRequest` | 272 requests, $0.00095 | 306 requests (`Count`), 272 of them reached the server | Requests the authorizer refused were not billed: every hour, the billed requests equal the server's invocations (91, 114, 67) |
| `Request` (x86_64) | 283 | 272 server, 7 maintenance, 4 log retention invocations | Matches |
| `Lambda-GB-Second` (x86_64) | 361.1 GB-seconds: $0 within the account's free tier, $0.0060 at list price | 323.9 GB-seconds of duration at 2 GB (server 246.3, maintenance 77.6), and the log retention function's 0.5 | 36.6 GB-seconds more, against 39.6 of initialization the runs recorded (9 cold starts). Not reconciled: in the 12:00 hour four recorded cold starts (17.6 GB-seconds) did not appear, and in the 10:00 hour 17.8 GB-seconds appeared against 4.5 recorded; SnapStart's snapshots and the maintenance function's start are not recorded at all |
| `Request-ARM`, `Lambda-GB-Second-ARM` | 115; 12.0 GB-seconds | 66 authorizer and 49 ops invocations; 11.2 GB-seconds of duration | Matches, with the functions' initialization |
| `Lambda-SnapStart-Cached-GB-S` | 57,600 GB-seconds, $0.087 | SnapStart on for about 30 minutes, over four published versions (two per install run) | By the hour (09:00 to 13:00): 2,035, 27,501, none, 26,764 and 1,299: four 3-hour windows at 2 GB, one per version, published from 09:47 to 10:06. The four windows make 86,400 GB-seconds ($0.130); the 28,800 of the 11:00 hour were not posted yet |
| `Lambda-SnapStart-Restored-GB` | 16 GB, $0.0022 | 6 restores recorded by the live tests | 8 restores of 2 GB billed: 2 more than recorded |
| `DataProcessing-Bytes` (log ingestion) | 0.000113 GB, $0.00006 | 121,020 bytes into the API's access log, 291,905 into the functions' | Only the access log's ingestion is under the tag |
| `CW:MetricMonitorUsage` | Not under the tag | 15 detailed metrics with data in 3 hours: $0.018 | Billed to the account, not the install |
| `AWSSecretsManager-Secrets` and `AWSSecretsManagerAPIRequest` | 0.0104 secret-months, 331 calls; $0.0058 | `admin-token` and `db-admin`; in the first install also the two password secrets the code no longer makes (section 9.1) | Matches the secrets there were |
| ECR `TimedStorage-ByteHrs` | 0.00088 GB-months | The image, and the live test's driver images | Matches |
| S3 `Requests-Tier1` / `Requests-Tier2` | 362 / 942, $0.0022 | The server's and the live test driver's | Cannot be split |
| `VpcEndpoint-Hours`, `VpcEndpoint-Bytes` (network) | 18 endpoint-AZ-hours, 0.0012 GB; $0.18 | 6 endpoint-AZs, in place in 5 hours | 6 in each of 3 hours; the 11:00 and 13:00 hours, when the endpoints still stood, were not posted yet (12 endpoint-AZ-hours, $0.12) |
| `VendedLog-Bytes` (network and database) | 0.00125 GB, $0.0006 | 1,045,415 bytes of flow logs, 49,867 of the database's log | The bill counted somewhat more than the log groups' ingestion metric |
| `InstanceUsage:db.t4g.small`, `RDS:GP3-Storage` (database) | 2.66 hours, 0.0715 GB-months; $0.093 | | Section 6.1 |
| **Total** | **$0.372** ($0.378 without the free tier) | | With the lines not posted yet, $0.542 at list price |

- **The bill was not complete when read.** Cost Explorer still marked the day as estimated, and two kinds of line were missing for hours the resources stood in: the endpoints' 11:00 and 13:00 hours, and SnapStart's 11:00 hour. The figures below add them as the hours around them and the 3-hour minimum imply (derived).
- **What it cost, and why.** Of the $0.542 at list price: the network $0.301 (56%), the database $0.095 (18%), and the catalog's own resources $0.146 (27%), of which SnapStart $0.132 and the two obsolete secrets about $0.002. The 272 requests themselves (API Gateway, the functions' requests and duration, the access log) were $0.0073, and $0.0014 of that was `install.sh`'s maintenance and ops functions.
- **Per request.** The server's GB-seconds on the bill, after the maintenance and log retention functions', were 283, 1.04 per billed request: 520 ms at 2 GB, the 453 ms of measured duration and 67 ms of initialization. Section 5.3's formula with D = 520 gives $21.70 per million. The bill's own items came to $21.46: they include the authorizer ($0.22) but, of the logs, only the access log's ingestion ($0.21), the functions' not being under the tag.
- **Idle.** In the hours the API and the functions were in place with no request (11:00 to 11:07, 13:00 to 13:33), the bill had no API Gateway request and no server invocation, as the metrics had none: only the log retention function's one invocation as the stacks went, the database, the secrets and SnapStart's remaining minimum. Those two hours were among the ones not complete when read.
- **SnapStart** cost eighteen times the requests in these runs: its cache is billed 3 hours per published version however short the version's life, and an install run published two (section 5.4).

## 10. Another Region

The quantities do not change with the Region; only the unit prices do. Fetch each from the Price List API, whose endpoint is in `us-east-1`, with the Region's `regionCode` and, where the tables name one, its `usagetype` with the Region's prefix (`USW2-` for `us-west-2`, `USE2-` for `us-east-2`, `EUC1-` for `eu-central-1`; KMS's are `<region>-KMS-Requests`). Take the dimension whose `beginRange` is 0:

```bash
aws pricing get-products --region us-east-1 --service-code AWSLambda \
  --filters Type=TERM_MATCH,Field=regionCode,Value=eu-central-1 \
            Type=TERM_MATCH,Field=usagetype,Value=EUC1-Lambda-GB-Second \
  --query 'PriceList[]' --output text \
  | jq -r '.terms.OnDemand[].priceDimensions[] | "\(.beginRange) \(.unit) \(.pricePerUnit.USD)"'
```

| Item | `service-code` | `usagetype` (after the prefix), or filters |
| --- | --- | --- |
| Lambda | `AWSLambda` | `Request`, `Lambda-GB-Second`, `Request-ARM`, `Lambda-GB-Second-ARM`, `Lambda-SnapStart-Cached-GB-S`, `Lambda-SnapStart-Restored-GB` |
| API Gateway | `AmazonApiGateway` | `ApiGatewayRequest` |
| CloudWatch | `AmazonCloudWatch` | `DataProcessing-Bytes`, `TimedStorage-ByteHrs`, `VendedLog-Bytes`, `CW:MetricMonitorUsage` |
| Secrets Manager | `AWSSecretsManager` | `AWSSecretsManager-Secrets` and `AWSSecretsManagerAPIRequest` |
| ECR | `AmazonECR` | `TimedStorage-ByteHrs` |
| S3 | `AmazonS3` | `TimedStorage-ByteHrs`, `Requests-Tier1`, `Requests-Tier2` |
| KMS | `awskms` | `<region>-KMS-Requests`, `<region>-KMS-Keys` (no prefix) |
| NLB | `AWSELB` | `LoadBalancerUsage`, `LCUUsage`, with `productFamily=Load Balancer-Network` |
| VPC endpoints, public IPv4 | `AmazonVPC` | `VpcEndpoint-Hours`, `VpcEndpoint-Bytes`, `PublicIPv4:InUseAddress` |
| RDS | `AmazonRDS` | `instanceType=db.t4g.small`, `databaseEngine=PostgreSQL`, `deploymentOption=Single-AZ`; `RDS:GP3-Storage`, `RDS:ChargedBackupUsage`, `CPUCredits:db.t4g` with `databaseEngine=PostgreSQL` |
| EKS, EC2 | `AmazonEKS`, `AmazonEC2` | `AmazonEKS-Hours:perCluster`; `BoxUsage:t3.large` and `BoxUsage:t3.xlarge` with `operatingSystem=Linux`, `tenancy=Shared`, `preInstalledSw=NA`, `capacitystatus=Used`; `NatGateway-Hours`, `EBS:VolumeUsage.gp3` |

Fetched on 2026-10-29, every price this document uses is the same in `us-west-2` and `us-east-2`. For comparison, in `eu-central-1` these differ: API Gateway $3.70 per million, log ingestion and vended logs $0.63/GB and storage $0.0324/GB-month, S3 $0.0245/GB-month and $0.0054 / $0.00043 per 1,000 requests, NLB $0.027/hour, interface endpoints $0.012 per AZ-hour, `db.t4g.small` $0.037/hour, its gp3 $0.137/GB-month and backups $0.103/GB-month, `t3.large` $0.096/hour and `t3.xlarge` $0.192/hour, NAT gateway $0.052/hour and $0.052/GB, EBS gp3 $0.0952/GB-month; Lambda, Secrets Manager, ECR, KMS, detailed metrics, the EKS control plane and public IPv4 are the same. There, the serverless form's fixed $1.26 is unchanged, 10 million requests at the measured mean cost $204.17, and the network and database `install.sh` makes cost $52.70 and $30.15.

## 11. What is not measured

- **A real workload's request mix and mean duration** in the serverless form: the test runs served 306 requests. Section 5.3 gives the range between the measured median and mean.
- **The server's own S3 requests per commit** (`P` and `G` of section 3.4), apart from the engines': no bucket of a test install had request metrics, and the bill counts requests per bucket.
- **Data between Availability Zones** and through the interface endpoints.
- **KMS requests** to the AWS managed keys: the bill does not attribute them to an install.
- **The pods' actual CPU** in the EKS form; section 4.1 prices their requests.
- **A whole idle hour on the bill**: the test installs' idle windows were 7 and 33 minutes long (section 9.3).
- **What the bill left unexplained or incomplete**: the EKS installs' ECR transfer out of the Region (section 9.2); the serverless install's initialization, hour by hour; and the lines Cost Explorer had not posted yet for two of its hours (section 9.3).
- **The bill of the account-wide items** an install adds to: the detailed API metrics and the functions' log ingestion are not under the install's tag; sections 5.2 and 9.3 price them from the metrics.
- **Whether ECR bills layers shared between images once.**
- **The compressed size of stored logs**, which log storage is billed by; the figures use the ingested size, an upper bound.
