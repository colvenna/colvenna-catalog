---
title: "Colvenna Catalog serverless spike: the five hard constraints (issue #12)"
date: 2026-10-28
status: final
sources:
  - Server at origin/main 42dbd5a (Java 21, Apache Paimon 2.0.0); raw results under spikes/serverless/results/; the spike's code, deleted in #49, at 1b88bc8
  - AWS account of the maintainer, us-west-2, 2026-10-28 03:29–04:33 UTC; every resource listed in spikes/serverless/ledger.md
  - https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html ; https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-custom.html (accessed 2026-10-28)
  - https://github.com/aws/aws-lambda-web-adapter (1.1.0, accessed 2026-10-28)
  - Paimon 2.0.0 sources (paimon-api, paimon-core source jars) for client behaviour
---

> The spike ran on the server at `42dbd5a`, before the rename (#31). Its settings, log lines and table names then began with the old prefix; this report writes them with their current names (`COLVENNA_*`, `colvenna_schema_version`), which #31 changed one for one. The raw results under `spikes/serverless/results/` are kept as recorded.

# Colvenna Catalog serverless spike: the five hard constraints

This spike checks the five hard constraints of the serverless form in section 6.2 of the
[1.0 design](../design/v1.0-design.md) (see also section 7.3 of the
[catalog landscape research](paimon-catalog-landscape.md)), and applies the rule of issue #12 to
decide the scope of 1.0. The server was run unchanged; everything the spike added was in
`spikes/serverless/`. Numbers come from the files in `spikes/serverless/results/`. Anything not
measured is marked NOT_RUN. Once the spike was over, #49 deleted its template, scripts, functions and
images, and kept this report, the [ledger](../../spikes/serverless/ledger.md) and `results/`; the
files this report names in `spikes/serverless/` are in the history at `1b88bc8` (see "How to rerun").

## Verdicts and the scope of 1.0

| # | Constraint | Verdict | Key numbers |
| --- | --- | --- | --- |
| 1 | Per-table credentials without role chaining | **Feasible** | Cognito developer identity, then `AssumeRoleWithWebIdentity` with a table session policy: 5,399 s and 43,199 s granted (1.5 h and 12 h asked); chained `AssumeRole` refused above 1 h |
| 2 | Request body limits | **Feasible**, with a documented limit | Commits are 0.7 KB at any file count, plus about 136 B per changed partition; the entry accepts up to about 41,000 partitions per commit and answers 413 above, with nothing applied |
| 3 | Timeouts | **Feasible**, only after a server change | A commit applied after the gateway's 504, then retried, took effect once. But lock wait (30 s) and S3 timeouts (30 s) are hard-coded above the 29 s gateway limit |
| 4 | Cold start | **Feasible**, only after a server change | Init 2.1–3.5 s at 2048 MB (n=15); SnapStart restore 0.42–0.75 s (container image), 0.66–1.16 s (zip). The init needs the database: with connections exhausted, inits hit the 10 s limit |
| 5 | Package size | **Feasible** | Container image 271 MB in ECR, about 495 MiB unpacked (limit 10 GB); a zip package is 191.5 MB unzipped (limit 250 MB) |

**Scope by the issue's rule.** Constraint 1 is feasible, so the serverless form does not have to
drop per-table credentials. None of constraints 2–5 was found infeasible, so the rule does not move
the serverless form to 1.1. That conclusion rests on server changes the spike did not make (listed
under "What the serverless form needs before it can ship"). If those changes cannot land and pass
in 1.0, constraints 3 and 4 are not met by the 1.0 server, and the rule then gives: EKS form only
in 1.0, serverless form in 1.1.

## What was built

One CloudFormation stack, `colvenna-catalog-spike` (`spikes/serverless/template.yaml`), in
us-west-2. Its resources are in [the ledger](../../spikes/serverless/ledger.md).

- **Network.** A new VPC with two private subnets, no internet gateway, no NAT gateway, no
  Elastic IP. VPC endpoints: S3 (gateway, limited to the spike bucket), STS, cognito-identity and
  execute-api (interface).
- **Entry.** API Gateway REST API, PRIVATE endpoint type, reachable only through the spike's
  execute-api endpoint. A resource policy allows `execute-api:Invoke` only when
  `aws:SourceVpce` is that endpoint and denies everything else. One `{proxy+}` route, `ANY`, with
  CUSTOM authorization by a REQUEST Lambda authorizer that reads the `Authorization` header; the
  SnapStart variants got `/snap/{proxy+}` and `/zip/{proxy+}` with the same authorizer.
  The integration is `AWS_PROXY` with a 29,000 ms timeout. The authorizer (`functions/authorizer.py`)
  looks the bearer token up as the server does (SHA-256 in `api_tokens`, with revocation, expiry
  and disabled principals checked). The server then authenticates the request again.
- **Server.** The repository's jars built with `mvn -B -DskipTests package`, in a Lambda container
  image (`image/Dockerfile`): the same `eclipse-temurin:21-jre-noble` base and classpath as the
  repository's Dockerfile, plus the AWS Lambda Web Adapter 1.1.0 extension. arm64, 2048 MB, in the
  VPC, reserved concurrency 10, pools `COLVENNA_JDBC_POOL_SIZE=2`, `COLVENNA_JDBC_AUX_POOL_SIZE=1`. The
  adapter forwards to port 8181 on loopback once `/healthz` on 8182 answers.
- **Database.** RDS PostgreSQL 16, db.t4g.micro, TLS with certificate verification
  (`sslmode=verify-full`), no backups, deleted without a snapshot.
- **Helpers.** An in-VPC driver function (`functions/driver.py`) that calls the private API and
  reads the database for evidence. Two credential probes (`functions/credprobe.py`), one outside
  any VPC and one inside.
- **Never created:** a Lambda Function URL in any form, a NAT gateway, a public bucket, a role
  attachment on the identity pool, any unauthenticated route.

## 1. Per-table credentials without role chaining: feasible

**Method.** `functions/credprobe.py` makes the calls the server would make, in Python with the
runtime's boto3, from a Lambda function. It calls `GetOpenIdTokenForDeveloperIdentity` on an
identity pool with only a developer provider. It then calls `AssumeRoleWithWebIdentity` on the data
role with the server's READ_WRITE session policy (the shape of `SessionPolicy.json`, 1,015 bytes)
for the prefix `warehouse/probe/db1/t1`. Last, it uses the credentials it got. The data role trusts
`cognito-identity.amazonaws.com` only, with `aud` set to the pool and `amr` set to `authenticated`.
Raw output: `results/per-table-credentials.json`.

| Check | Outside a VPC | Inside the VPC (endpoints, no NAT) |
| --- | --- | --- |
| `GetOpenIdTokenForDeveloperIdentity` | ok, 165 ms | ok, 210 ms |
| `AssumeRoleWithWebIdentity`, 5,400 s asked | granted 5,399 s | granted 5,399 s |
| … 43,200 s asked | granted 43,199 s | granted 43,199 s |
| … 43,201 s asked | refused (role maximum 12 h) | refused |
| S3 put / get / list inside the table prefix | allowed | allowed |
| S3 put to, and get of an existing object in, another table's prefix | AccessDenied ("no session policy allows") | AccessDenied |
| List the parent prefix; list `t1_v2/` (same name prefix) | AccessDenied | AccessDenied |
| Contrast: `AssumeRole` from the function's own role session, 5,400 s | refused: "exceeds the 1 hour session limit for roles assumed by role chaining" | same |
| … 3,600 s | granted 3,599 s | same |
| An AWS API without an endpoint in the VPC (SSM) | — | connect timeout after 8.1 s |

So the session is a web-identity session, not a chained one, and lasts 1.5–12 hours as
`COLVENNA_DATA_TOKEN_DURATION` needs. The pool's "classic flow" setting was off, and the flow worked
anyway.

**Nothing anonymous** (`results/outside-vpc-and-config.txt`, from the maintainer's host, outside
the spike VPC):

- Unsigned `GetId` on the pool: `NotAuthorizedException: Unauthenticated access is not supported
  for this identity pool.`
- Unsigned `GetOpenIdTokenForDeveloperIdentity`: `MissingAuthenticationTokenException`. The call
  needs SigV4 credentials with IAM permission on the pool.
- `GetCredentialsForIdentity` (the enhanced flow) with a valid developer token:
  `InvalidIdentityPoolConfigurationException`. The pool has no roles attached, so it hands out
  nothing by itself.
- A forged token sent to `AssumeRoleWithWebIdentity`: `InvalidIdentityToken`.
- Pool configuration: `AllowUnauthenticatedIdentities: false`, `AllowClassicFlow: false`, a
  developer provider only, no other providers.

**VPC.** A function outside a VPC reaches both services on their public endpoints. The server
function has to be in the VPC to reach its PostgreSQL, and without a NAT gateway it then needs
interface endpoints for `sts` and `cognito-identity`. Both endpoints exist in us-west-2, and the
in-VPC probe used them.

**Limits of this result.**

- The server's own Java issuer was not run in Lambda: NOT_RUN. `AwsStsTokenIssuer.create` reads the
  web identity token from `AWS_WEB_IDENTITY_TOKEN_FILE` (IRSA). The Lambda form needs a token source
  that calls `GetOpenIdTokenForDeveloperIdentity` through the AWS SDK.
- Whoever holds `cognito-identity:GetOpenIdTokenForDeveloperIdentity` on the pool can get a token
  and assume the data role, with a session policy of their own choosing. That permission has to be
  granted to the server's execution role alone, just as only the catalog's service account can use
  the IRSA token today. Pinning the data role's trust to the server's identity id
  (`cognito-identity.amazonaws.com:sub`) was not tried: NOT_RUN.
- Cognito request quotas for `GetOpenIdTokenForDeveloperIdentity` were not measured: NOT_RUN. The
  server caches each table credential until less than 70 minutes remain, which bounds the call rate.

## 2. Request body limits: feasible, with a documented limit

**What the Paimon client sends.** `spikes/serverless/request-sizes/` runs the real Paimon 2.0.0
`RESTCatalog` (paimon-core 2.0.0) against the server built from this repository, with PostgreSQL
16 in Docker and a local warehouse. It goes through a proxy that counts body bytes. "In event" is
the body's size once JSON-escaped into an API Gateway proxy event, which is what counts against
Lambda's 6 MB. Raw output: `results/request-sizes.json`.

| Operation | Request body | In event | Response body |
| --- | ---: | ---: | ---: |
| get config | 0 | — | 46 B |
| create database | 29 B | 37 B | 0 |
| create table, 2 / 100 / 1,000 / 10,000 columns | 218 B / 4.9 KB / 49 KB / 499 KB | +20 % | 0 |
| get table, same four | 0 | — | 468 B / 5.2 KB / 49 KB / 499 KB |
| alter table (add a column) | 106 B | 126 B | 0 |
| commit, 1 data file | 700 B | 764 B | 16 B |
| commit, 1,000 data files in one partition (1,000 buckets) | 718 B | 782 B | 16 B |
| commit, 10,000 data files in one partition | 723 B | 787 B | 16 B |
| commit, 1,000 partitions with one file each | 136,588 B | 152,640 B | 16 B |
| commit, 10,000 partitions | 1,360,590 B | 1,520,642 B | 16 B |
| commit, 40,000 partitions | 5,440,590 B | 6,080,642 B | 16 B |
| drop 10,000 partitions (a commit) | 1,390,647 B | 1,550,705 B | 16 B |
| list partitions, one page of 1,000 | 0 | — | 254,048 B |
| list table details, one page (16 tables, one with 10,000 columns) | 0 | — | 558,762 B |

**Commit bodies carry snapshot pointers, not file lists.** A commit is `tableId`,
`baseSnapshotUuid`, the new snapshot (manifest list names and sizes, counters) and one statistics
entry per changed partition (`FileStoreCommitImpl` builds them with
`PartitionEntry.merge(deltaFiles)`). The number of data files does not change the size (700 B for 1
file, 723 B for 10,000). The number of partitions touched does: about 136 B per partition for a
one-key spec such as `dt=2026-00001`, and more for longer values or more keys. The 10,000-partition
commit with `metastore.partitioned-table=true` took 4.9 s on the local server, against 0.3 s
without it.

**At the deployed entry** (`results/body-limits-private-api.json`, driver in the VPC, through the
private API):

| Body | Result | Took effect? |
| --- | --- | --- |
| create database, 6,100,000 B (padding, no escaping) | 200 | yes |
| … 6,400,000 B | **413** `{"message": "Request Too Long"}` | no: the database does not exist |
| … 11,000,000 B | **413** `HTTP content length exceeded 10485760 bytes.` | no |
| commit, 41,000 partitions (5,576,616 B; 6,232,670 B in event) | 200 | yes |
| commit, 42,000 partitions (5,712,616 B; 6,384,670 B in event) | **413** `Request Too Long` | no: pointer and commit intents unchanged |

So the binding limit is the Lambda event, about 6 MiB including JSON escaping and headers, not API
Gateway's 10 MiB. Requests over it are refused before the server sees them, so nothing is applied.
From the client code (not run against the gateway): Paimon's `DefaultErrorHandler` has no case for
413, so it throws `RESTException("Unable to process: Request Too Long")`. `FileStoreCommitImpl`
treats that as an uncertain commit and retries until `commit.max-retries`, and the job then fails.
The written data files stay behind as orphans, as after any failed commit.

**Verdict.** Ordinary catalog requests are in the bytes-to-kilobytes range, and commits do not
grow with the file count. A single commit touching more than about 41,000 partitions (fewer with
longer partition values) does exceed the limit: for example a full-table overwrite, compaction or
partition drop on a heavily partitioned table. That is rare but plausible. The entry then gives a
clear 413 with no partial effect, which is what section 6.2 asks for. The limit has to be written
in the manual, and the compatibility matrix has to record it.

**Responses are the weaker side** (`results/response-limit-private-api.json`). A list-table-details
page over 14 tables of 10,000 columns (about 7 MB) got 200 from the server. The Lambda Runtime API
refused it ("413 Payload Too Large"), and the client received **502**
`{"message": "Internal server error"}`. A page of 5 such tables (2.5 MB) worked. The request was
read-only, so nothing was applied, but the error says nothing useful. Which engines call
list-table-details was not checked: NOT_RUN.

## 3. Timeouts: feasible, only after a server change

**Where the waits are configured (origin/main 42dbd5a):**

| Wait | Where | Value | Settable without code? |
| --- | --- | --- | --- |
| PostgreSQL `lock_timeout`, main pool | `db/Db.java`, `Pool.MAIN("30s")`, passed as a JDBC startup option | 30 s | no |
| … autonomous, audit, auth pools | `Pool.AUTONOMOUS/AUDIT/AUTH("5s")` | 5 s | no |
| Hikari connection wait | `Db.create`: `setConnectionTimeout(10_000)` | 10 s | no |
| S3 conditional put of `snapshot-N` (AWS SDK) | `fs/SnapshotPublisher.S3ClientFactory.build`: `apiCallTimeout(30 s)`, `apiCallAttemptTimeout(10 s)` | 30 s | no |
| S3 FileIO (Paimon `S3FileIO`, shaded Hadoop S3A 3.4.2) | `core-default.xml` in `paimon-s3-2.0.0.jar` | connection 200 s, establish 30 s, 5 SDK attempts, 7 S3A retries | **yes**: `COLVENNA_FILEIO_FS_S3A_*` (the spike set 5 s / 10 s / 2 / 1) |
| Pending-intent abandonment | `CommitService.ABANDON_AFTER` | 5 min | no |
| Paimon client response timeout | `HttpClientUtils` (paimon-api) | 3 min | client-side |
| API Gateway REST integration | quota | 29 s | (raise by request) |

A lock timeout is reported as **HTTP 500 "Internal server error."** (`RestServer.map` sees only a
`PSQLException`). The Paimon client's HTTP layer retries only 429 and 503, for any method
(`ExponentialHttpRequestRetryStrategy`), so a 500 is not a clear, retryable answer.

**How a retried commit takes effect only once.** There is no idempotency key. Three mechanisms
together do the job:

1. **Compare-and-set with replay detection** (`CommitService.commit`, under the branch row lock
   `SELECT … FOR UPDATE`). If the client's `baseSnapshotUuid` is not the pointer, but its snapshot
   UUID *is* the pointer, the request is a replay and gets `success` ("replay of a committed
   request"). Any other stale base gets `success: false`.
2. **Commit intents and a conditional publish.** An intent is committed on its own before
   `snapshot-N` is written with `If-None-Match: *`. A snapshot that was published but not recorded
   is rolled forward by the next commit or snapshot load (`resolvePending`, `latest`).
3. **The client checks before it commits again.** After an exception, Paimon's
   `FileStoreCommitImpl.tryCommitOnce` looks through the snapshots since its last attempt for its
   own `commitUser` and `commitIdentifier`. If it finds one, it reports success without committing
   again.

`CommitProtocolTest` (11 cases, including `lostCommitResponseResolvesToOneCommit` and
`replayedCommitRequestIsIdempotent`) and `RetryConsistencyTest` (6) pass locally:
`results/commit-retry-tests.txt`.

**Through the deployed gateway** (`results/gateway-timeout-retry.json`). The driver held the
branch row lock while one commit went through the API, then sent the identical body twice more:

| Lock held | Client's first answer | Server (access log) | Applied by the first request? | Retry, same body | Snapshot |
| --- | --- | --- | --- | --- | --- |
| 20 s | 200 after 20.2 s | 200 | yes | 200 (replay) | +1 |
| 29.5 s | **504** after 29.0 s | **200 after 29.7 s**, after the gateway had answered | **yes** | 200 (replay) | +1, one `PUBLISHED` intent |
| 40 s | **504** after 29.0 s | 500 after 30.0 s (`canceling statement due to lock timeout`) | no | 200 | +1 |

The dangerous case happened as section 7.3 of the research predicted: the client saw a timeout
for a commit that did take effect. The retry still applied it only once.

**Verdict.** Retried commits are safe as the server stands. What section 6.2 asks for is not
possible yet: lock waits and S3 calls set below the gateway timeout, so that the server answers
with a clear error first. Two of the three limits are hard-coded at 30 s, and there is no deadline
per request. The change needed is small and confined to configuration and error mapping (see
below), so the verdict is feasible, but not with the current server.

## 4. Cold start: feasible, only after a server change

**Does the server need the database at init?** Yes. `serve` calls `ColvennaCatalogServer.create`. That
runs `Migrations.apply` (advisory lock, `CREATE TABLE IF NOT EXISTS`, a version read) and builds
four Hikari pools before `RestServer.start()`. The adapter's readiness check (`/healthz`) answers
only after all of that. Migrations therefore do run at init today. In the spike they found the
schema current, after the first start had applied V1 and V2. Section 6.2 requires `migrate` from the
installer instead.

**Measured** (`results/coldstart-*.json`). Every round forced new execution environments and sent
concurrent authenticated requests through the private API. "Init" is CloudWatch `Init Duration`,
"first request" is the `Duration` of the first invocation's REPORT line, and "client" is the time
the driver saw, including the authorizer:

| Form | n | Init Duration | First request | Client |
| --- | ---: | --- | --- | --- |
| Container image, 1024 MB | 5 | 2,995–4,066 ms (median 3,719) | 1,278–1,533 ms | 4.8–5.8 s |
| Container image, 2048 MB | 15 | 2,078–3,519 ms (median 3,078) | 336–721 ms | 2.6–4.9 s |
| Container image, 4096 MB | 5 | 1,602–1,973 ms (median 1,734) | 278–364 ms | 2.1–2.6 s |
| Container image + SnapStart, 2048 MB | 6 restores | Restore Duration 416–754 ms | 984–1,255 ms (n=4) | 1.6–2.1 s |
| Zip on `java21` + SnapStart, 2048 MB | 6 restores | Restore Duration 664–1,164 ms | 1,474–1,749 ms | 2.6–2.9 s |

**Where the 10 s limit was hit:**

- **First start of a new function.** The very first invoke after the function was created timed
  out at `Init Duration: 9999.06 ms` (status timeout). Of that, 9.5 s passed before the first
  migration line, presumably while the image was not yet cached. Lambda re-ran init inside the
  invoke (3.0 s), and the direct invoke returned in 15.9 s. Later cold starts of the same image did
  not repeat this. Only one such sample exists.
- **Database out of connections** (`results/coldstart-container-2048mb-run1-db-exhausted.json`,
  `results/db-connections.json`). On the third round of the first run, `max_connections` (79 on
  db.t4g.micro) was used up. 58 connections were idle, held by environments from earlier
  configurations. Each environment holds 5 (four pools: main 2, autonomous, audit and auth 1 each),
  and frozen or retired environments keep theirs open. Reserved concurrency (10) limits concurrent
  invocations, not the environments that still hold connections. The new inits waited on Hikari's
  10 s connection timeout during migrations. Three of five timed out at 10,000 ms, and the requests
  got 502 and 503 after 14–25 s. The clean runs above closed idle server connections before each
  round, as PostgreSQL's `idle_session_timeout` would.

**SnapStart.**

- **Container images:** the premise that SnapStart does not support them is out of date. AWS's
  SnapStart page (accessed 2026-10-28) lists Java 11+ "across ZIP and container image deployment
  format". Custom images such as this one need the label
  `com.amazonaws.lambda.feature.snapstart="Allow"` or an implementation of `/restore/next`. With
  the label, version 1 published with `OptimizationStatus: On`, and restores worked.
- **Zip:** the same jars fit a zip package. The function is 188,469,085 B and the adapter layer
  3,033,063 B, 191,502,148 B together, under the 262,144,000 B (250 MB) unzipped limit. Zipped it
  is 168.8 MB, so it must be uploaded through S3. On the managed `java21` runtime with
  `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`, SnapStart worked too.
- After a restore, the database connections in the snapshot are dead. Hikari detects them
  ("This connection has been closed") and opens new ones, which makes the first request about
  1–1.7 s instead of 0.3–0.7 s. Whether `SecureRandom` (used for API tokens) is unique after a
  restore in the Temurin image was not checked: NOT_RUN.
- Lambda also ran initializations that served no invocation: two at publish, and two more
  during the restore run, for each of the two SnapStart functions (2.7–3.4 s each).

**Verdict.** Measured inits are 2–4 s against a 10 s limit, and SnapStart restores are under
1.2 s, so the time fits. But init depends on the database being reachable and having free
connections, and the first start of a new image used up the whole 10 s once. The Lambda form has
to stop touching the database at init (no migrations, no eager pools), as section 6.2 already
plans. The cold start figures belong to the performance document (section 9.3) once that is done.

## 5. Package size: feasible

The server image as a Lambda container image (`results/package-sizes.json`) takes 271,267,206 B in
ECR, compressed, as a single-platform Docker v2 manifest. Unpacked it is about 495 MiB: JRE about
158 MiB, server jars 188,293,903 B, adapter 3,032,920 B. The limit is 10 GB. The three Paimon jars
are 126,499,676 B. A zip package also fits (constraint 4 above), with about 70 MB to spare.

The image was pushed with `docker buildx build --provenance=false --sbom=false` as a plain v2
manifest. The default `docker push` from this host produced an OCI image index with attestation
manifests, and whether Lambda accepts that was not tried: NOT_RUN.

## Authentication and exposure of the entry

`results/auth-private-api.json` was run from inside the VPC, through the private API;
`results/outside-vpc-and-config.txt` from outside.

| Request | Answer |
| --- | --- |
| No `Authorization` header (config, an unknown route, a POST commit) | 401 `Unauthorized`, from the gateway; the server is not invoked |
| Unknown bearer token, `Basic` scheme, empty bearer, revoked token | 401 `Unauthorized` (authorizer) |
| Valid token, principal without grants | 403 from the server ("has no permission") |
| Valid non-admin token on the admin API | 403 from the server |
| Valid admin token | 200 |
| The API hostname from outside the spike VPC | does not resolve (private DNS only) |
| Lambda Function URLs of the five functions that existed then | 0 configs each; the template has no `AWS::Lambda::Url` or `FunctionUrlConfig`, so the two SnapStart functions added later have none either |
| Anonymous list and get on the spike bucket | 403 |

## Other findings

- **S3A region in a VPC without egress.** With only `COLVENNA_FILEIO_S3_REGION` set (as the EKS chart
  sets it), the first table creation hung until the 60 s function timeout, and the gateway answered
  504 after 29 s. Setting `COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION` fixed it. The likely cause is that
  S3A did not get the region from `s3.region` and tried an endpoint the S3 gateway endpoint does not
  serve; the mechanism was not traced. The Lambda form must set the S3A region and S3A timeouts
  explicitly.
- **Hadoop needs a passwd entry.** Lambda ran the container as uid 993, gid 990, not the image's
  `USER`. The spike image adds a passwd entry for uid 993 at build time. Whether the server fails
  without it was not tried: NOT_RUN.
- **Plain HTTP on loopback.** The spike used `COLVENNA_ALLOW_PLAINTEXT=true`, which binds the API port
  on all interfaces. Section 6.2 asks for a Lambda-only setting that allows plain HTTP on loopback
  only. That needs a server change.
- **Background threads freeze between invocations.** Hikari's housekeeper logs "Thread starvation
  or clock leap detected" after every thaw. It is harmless noise, but idle-connection eviction does
  not run while an environment is frozen.
- **The database password** was passed in environment variables, for the spike only. The Lambda
  form should read it from Secrets Manager or use IAM database authentication. That would need
  another interface endpoint, or RDS IAM authentication, which needs none.

## What the serverless form needs before it can ship

Server changes, none of them made by this spike:

1. **No database at init:** a `serve` mode that skips migrations, with the installer running
   `migrate`. Pools must not connect during init.
2. **Configurable timeouts below the gateway's 29 s:** main-pool `lock_timeout`, the
   `SnapshotPublisher` S3 call and attempt timeouts, and a per-request deadline. A lock timeout
   should answer with a clear, retryable status (503 with a message) instead of 500.
3. **A response-size guard:** cap pages by bytes, or lower `COLVENNA_MAX_PAGE_SIZE` in the Lambda form,
   so that list responses stay under 6 MB instead of failing with 502.
4. **A Cognito token source for `AwsStsTokenIssuer`:** `GetOpenIdTokenForDeveloperIdentity` instead
   of `AWS_WEB_IDENTITY_TOKEN_FILE`.
5. **Connection budget:** keep pools at the minimum (four pools, five connections), and document
   that retired and frozen environments keep their connections. Set `idle_session_timeout` on the
   database or use RDS Proxy, and size reserved concurrency against `max_connections`.
6. **Loopback-only plain HTTP** as an explicit Lambda-form setting.

Manual and compatibility matrix:

- the ~6 MiB request limit (about 41,000 partitions per commit with a one-key spec);
- the 413 behaviour;
- the S3A region and timeout settings;
- the VPC endpoints the form needs: `sts`, `cognito-identity`, `execute-api`, and the S3 gateway
  endpoint.

## Cleanup

Every resource was in the one stack, which was deleted at 04:33 UTC. Deletion took 20 minutes,
most of it spent waiting for the network interfaces of the last VPC function. `verify-clean.sh`
then found nothing: no tagged resource, and none of any type the spike used
(`spikes/serverless/results/cleanup-verification.txt`). The tagging index still listed the eight
VPC endpoints of the two creations for a while after the deletion. `DescribeVpcEndpoints` reports
each of them as not existing. The account is back to 22 VPCs. The resources, their logical names
and the timeline are in the [ledger](../../spikes/serverless/ledger.md).

## How to rerun

The spike's files were deleted in #49. From `spikes/serverless/` of a checkout of `1b88bc8`
(`git worktree add <dir> 1b88bc8`), as the work profile, in us-west-2 only:

```text
request-sizes/run.sh                          # constraint 2, local; writes results/request-sizes.json
./deploy.sh base && ./deploy.sh image && ./deploy.sh functions && ./deploy.sh zip
./deploy.sh full [EnableSnapStart=true] [EnableZip=true]
aws lambda invoke ... colvenna-catalog-spike-server   # first start applies migrations (see section 4)
./deploy.sh driver '{"action":"auth"}'        # also crud, sizes, lock, bigresponse, sql, reap
./deploy.sh probe [vpc]                       # constraint 1
./coldstart.sh colvenna-catalog-spike-server "" 3 5
./destroy.sh                                  # empties the bucket, deletes the stack, runs verify-clean.sh
```
