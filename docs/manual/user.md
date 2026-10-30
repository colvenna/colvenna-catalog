# User manual

This manual is for the people who run Colvenna Catalog and connect engines to it: what 1.0 supports, its concepts, how each engine connects, how the catalog is administered, backed up, restored and upgraded, how to run it day to day and troubleshoot it, and its known limitations. Installing it is in the [deployment guide](install.md); the admin API, operation by operation with `curl`, is on [its own page](admin-api.md), with its reference in [`docs/api/admin-v1.yaml`](../api/admin-v1.yaml).

The examples assume an EKS install with the defaults of `install.sh`: namespace, release and Service `colvenna-catalog`, warehouse `colvenna`. Where this manual describes something that has not been run yet, it says so.

## 1. What 1.0 supports

- **The Apache Paimon REST catalog contract** of Paimon 2.0.0: every path of the REST catalog OpenAPI 1.0 that Paimon 2.0.0 publishes, except those the [LLD](../design/colvenna-catalog-lld.md#端点范围) lists (and section 9.4 below). Paimon's own contract tests (`RESTCatalogTest` and `CatalogTestBase` of the 2.0.0 test jars) run against the server and PostgreSQL in every build.
- **Engines**, through Paimon's `RESTCatalog` client, as the [compatibility matrix](../compat/matrix.md) measured them against the EKS form on 2026-10-28:

  | Engine | Paimon client | What passes |
  | --- | --- | --- |
  | Flink 1.20.5 | 2.0.0, 2.1.0 | Create and drop, write and commit, read, per-table credentials, on primary-key tables with and without deletion vectors and on append tables |
  | Flink 2.2.1 | 2.1.0 | As above |
  | Spark 3.5.9 | 2.0.0, 2.1.0 | As above |
  | Spark 4.1.3 | 2.1.0 | As above |
  | Doris 4.1.5 | 1.4.2, bundled | Read, with per-table credentials; not the rows of uncompacted level-0 files of a deletion-vector table (section 9.2) |
  | StarRocks 4.1.6 | 1.3.1, bundled | Read, with S3 credentials of its own; the same deletion-vector limitation (section 9.2) |

  No engine has run against the serverless form yet. The Paimon 2.0.0 Java client has: it created, wrote, committed and read tables through the private API with per-table credentials (`tests/serverless`).
- **Two deployment forms**, in a standard AWS Region: on Amazon EKS, two replicas behind a Service; or serverless, on AWS Lambda behind a private REST API.
- **Authentication** by bearer tokens the server issues; **authorization** by grants to principals; an **audit log** of writes, decisions and refusals; optional **per-table credentials**.

Each release candidate is accepted by its [UAT cases](../uat/cases.md), in both forms and two Regions, and its performance against [the performance terms](../performance/terms.md).

Not in 1.0: more than one warehouse in an instance; an Iceberg REST interface; role-based grants; OIDC or another external identity provider; clouds other than AWS; row filters and column masking; Paimon's REST Management API (an experimental API of Paimon 2.1.0); a command-line tool (the admin API is HTTP).

## 2. Concepts

- **Warehouse.** One catalog, named by `COLVENNA_WAREHOUSE_NAME` (`colvenna` by default). Clients pass it as `warehouse`; it is also the REST path prefix, `/v1/colvenna/...`. Its tables live under one S3 prefix, `s3://<bucket>/<COLVENNA_WAREHOUSE_PREFIX>/`, managed tables at `<db>.db/<table>` as in Paimon's own catalogs. Its metadata (databases, tables, snapshot pointers, principals, tokens, grants, audit) is in PostgreSQL. A table's own files (schemas, snapshots, manifests, data) stay where Paimon puts them, so a reader of the files sees the same table.
- **Principal.** A service identity: a Flink or Spark job, a Doris catalog, an operator's tooling. An **admin** principal may call the admin API; it holds no catalog privileges unless granted, like any other. `install.sh` makes the first admin, `admin`. A principal can be disabled, for good.
- **Token.** An opaque bearer token (`colvenna_` and 32 random bytes) of one principal, shown once when it is issued. The server keeps only its SHA-256. It expires (90 days by default, 400 at most) and can be revoked. Every request needs one, over TLS. Paimon clients send it with `token.provider=bear`, and never refresh it: a job keeps its token until it restarts.
- **Grant.** A privilege, `READ`, `WRITE` or `MANAGE` (each includes the ones before it), allowed or denied (`DENY` forbids that level and those above, and wins over every `ALLOW`) to one principal, on the whole catalog, a database, or one table, view or function. A table-level `READ` may list the columns it allows. Every grant names its purpose, who approved it, the approval's reference and when it expires; it counts only until then, and only in the environment it was made for (`COLVENNA_ENVIRONMENT`, which `install.sh` sets to `COLVENNA_ENV`). A principal without grants sees nothing but the database `default`.
- **Per-table credentials.** With `COLVENNA_DATA_TOKENS=aws-sts` (the installer's default) and a client that sets `data-token.enabled=true`, the catalog vends the client short-lived AWS credentials for one table: the data role's session, narrowed by a session policy to that table's directory, read-write for a principal with `WRITE` on the table, read-only for one with only `READ`. The engine then needs no S3 access of its own. They last `COLVENNA_DATA_TOKEN_DURATION` (3 hours by default), and the client takes new ones before they run out.
- **The default database.** Flink's and Spark's Paimon catalogs look up the database `default` when they start, so `install.sh` makes it and every authenticated principal sees it. Seeing it grants nothing in it.
- **Commits.** A commit goes to the server, which publishes the snapshot atomically: a row lock in PostgreSQL, and the snapshot file written with an S3 conditional write (`If-None-Match: *`), so that concurrent writers and replicas cannot overwrite each other's snapshots. A snapshot published in the table's files by anything else (another catalog, a job still writing through one) **fences** the table: its commits are refused with 409 until an admin reconciles it (section 7.5).

## 3. Reaching the catalog

- **EKS form, from a pod**: `https://colvenna-catalog.colvenna-catalog.svc:8181`, from a namespace labelled `colvenna-catalog/client=true` (`COLVENNA_CLIENT_NAMESPACE_LABEL`). Pods of other namespaces cannot connect at all, when the cluster enforces NetworkPolicy.
- **EKS form, from the VPC outside the cluster**: with `COLVENNA_INTERNAL_NLB=on`, `https://<the NLB's DNS name>:8181`, from the CIDRs `COLVENNA_NLB_ALLOWED_CIDRS` lists. `install.sh` prints the name.
- **EKS form, from an administrator's workstation**: `kubectl port-forward` to the Service, then `https://localhost:8181` (the [admin API page](admin-api.md#reach-the-api)).
- **Serverless form**: `https://<api-id>.execute-api.<region>.amazonaws.com/catalog`, which `install.sh` prints (the stack output `ApiUrl`), from a VPC with one of the `execute-api` endpoints the install lists. Where the endpoint has no private DNS, use the output `ApiEndpointUrl`, with `<api-id>-<vpce-id>` in the host name. Nothing outside those endpoints reaches it.

The admin API is under `<that address>/colvenna/admin/v1`, and the Paimon REST API under `<that address>/v1/<warehouse>`.

## 4. Connecting an engine

### 4.1 What every engine needs

1. **A principal, its grants and a token.** The [README's quick start](../../README.md#quick-start-on-amazon-eks) (step 3) and the [admin API page](admin-api.md) show how. A writer needs `WRITE` on what it writes (`MANAGE` to create and drop tables); a reader, `READ`. Keep the token out of command lines and logs: in a Kubernetes Secret, or a file only the job's user can read.
2. **The catalog's options**, the same for Flink and Spark:

   | Option | Value |
   | --- | --- |
   | `metastore` | `rest` |
   | `uri` | The address of section 3: in the EKS form without a path, in the serverless form with its `/catalog` |
   | `warehouse` | `colvenna` (`COLVENNA_WAREHOUSE_NAME`) |
   | `token.provider` | `bear` |
   | `token` | The principal's token |
   | `data-token.enabled` | `true` for per-table credentials. The catalog must vend them (`COLVENNA_DATA_TOKENS=aws-sts`), or every table access fails (the token endpoint answers 501); it never falls back to wider credentials. `false` when the engine reads and writes S3 with credentials of its own |

3. **A trust store** (EKS form). The server's certificate comes from the install's private CA, and Paimon checks it with the JVM's default trust store. That store must hold the JDK's public CAs **and** the CA: with the CA alone, the same JVM can no longer reach S3 and STS. Copy the CA out of the cluster (only its `ca.crt` key: the Secret also holds the CA's private key), and add it to a copy of the JDK's store:

   ```bash
   kubectl -n colvenna-catalog get secret colvenna-catalog-ca-key -o jsonpath='{.data.ca\.crt}' | base64 -d >ca.crt
   cp "$JAVA_HOME/lib/security/cacerts" colvenna-cacerts
   keytool -importcert -noprompt -alias colvenna-catalog -file ca.crt -keystore colvenna-cacerts -storepass changeit
   ```

   Then give every JVM that runs the Paimon client `-Djavax.net.ssl.trustStore=<path>/colvenna-cacerts -Djavax.net.ssl.trustStorePassword=changeit`. The serverless form's API presents a certificate the JDK already trusts, so it needs none of this.
4. **Paimon's jars** for the engine, as the matrix ran them (`tests/compat/cells.json` pins each by SHA-256): Flink, `paimon-flink-<flink version>-<paimon version>.jar` with `paimon-s3` and `paimon-hadoop-uber` of the same Paimon version; Spark, `paimon-spark-<spark version>_<scala version>-<paimon version>.jar` with `paimon-s3`. Doris and StarRocks bundle their own Paimon.

The matrix's own configuration of each engine is in [`tests/compat/`](../../tests/compat/README.md): the Flink and Spark flows (`flows/`) and the readers' `engine/reader.py`. The examples below follow them.

### 4.2 Flink

In Flink SQL (or `TableEnvironment.executeSql`):

```sql
CREATE CATALOG colvenna WITH (
  'type' = 'paimon',
  'metastore' = 'rest',
  'uri' = 'https://colvenna-catalog.colvenna-catalog.svc:8181',
  'warehouse' = 'colvenna',
  'token.provider' = 'bear',
  'token' = '<the token>',
  'data-token.enabled' = 'true'
);
USE CATALOG colvenna;
```

The matrix ran Flink 1.20.5 and 2.2.1 from their official images, in a local cluster in one JVM with the trust store's options, and its writes in batch mode (`EnvironmentSettings.inBatchMode()`, with `table.dml-sync=true`). On a Flink cluster, the JobManager and every TaskManager run the Paimon client and need the trust store (`env.java.opts.all`, in [Flink 1.20's configuration](https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/deployment/config/)). A bounded write in streaming mode at a parallelism above 1 can lose data: see section 9.1 first.

### 4.3 Spark

The same options under `spark.sql.catalog.<name>.`, with Paimon's catalog class and session extensions. In a properties file only the job's user can read (`spark-submit --properties-file <file>`, [Spark's submitting applications](https://spark.apache.org/docs/latest/submitting-applications.html)), so that the token is not on the command line; the trust store goes to the driver and the executors ([Spark's configuration](https://spark.apache.org/docs/latest/configuration.html)):

```text
spark.sql.extensions                         org.apache.paimon.spark.extensions.PaimonSparkSessionExtensions
spark.sql.catalog.colvenna                   org.apache.paimon.spark.SparkCatalog
spark.sql.catalog.colvenna.metastore         rest
spark.sql.catalog.colvenna.uri               https://colvenna-catalog.colvenna-catalog.svc:8181
spark.sql.catalog.colvenna.warehouse         colvenna
spark.sql.catalog.colvenna.token.provider    bear
spark.sql.catalog.colvenna.token             <the token>
spark.sql.catalog.colvenna.data-token.enabled true
spark.driver.extraJavaOptions                -Djavax.net.ssl.trustStore=<path>/colvenna-cacerts -Djavax.net.ssl.trustStorePassword=changeit
spark.executor.extraJavaOptions              -Djavax.net.ssl.trustStore=<path>/colvenna-cacerts -Djavax.net.ssl.trustStorePassword=changeit
```

The matrix ran Spark 3.5.9 and 4.1.3 from their official images in local mode (`--master local[2]`), where the driver is the only JVM.

### 4.4 Doris

Doris 4.1.5 reads tables written by Flink or Spark; in 1.0 it is not used to write, or to create or drop tables. As a Doris user allowed to create catalogs:

```sql
CREATE CATALOG colvenna PROPERTIES (
  'type' = 'paimon',
  'paimon.catalog.type' = 'rest',
  'uri' = 'https://colvenna-catalog.colvenna-catalog.svc:8181',
  'warehouse' = 'colvenna',
  'paimon.rest.token.provider' = 'bear',
  'paimon.rest.token' = '<the token>',
  'paimon.rest.data-token.enabled' = 'true'
);
```

Use the token of a principal with `READ` on what Doris reads. The trust store goes to both the FE and the BE (its JNI readers run the Paimon SDK), for example as `JAVA_TOOL_OPTIONS` in their environments, as the matrix did. Doris's own users and their privileges are Doris's: every query of the catalog reaches the server as the one principal of its token.

### 4.5 StarRocks

StarRocks 4.1.6 also only reads, and does not use the catalog's per-table credentials: give it read-only S3 credentials of its own. The matrix used the access key of an IAM user that may only `s3:GetObject` the objects under the warehouse prefix and `s3:ListBucket` that prefix:

```sql
CREATE EXTERNAL CATALOG colvenna PROPERTIES (
  "type" = "paimon",
  "paimon.catalog.type" = "rest",
  "paimon.catalog.warehouse" = "colvenna",
  "paimon.option.uri" = "https://colvenna-catalog.colvenna-catalog.svc:8181",
  "paimon.option.token.provider" = "bear",
  "paimon.option.token" = "<the token>",
  "aws.s3.use_instance_profile" = "false",
  "aws.s3.access_key" = "<access key ID>",
  "aws.s3.secret_key" = "<secret access key>",
  "aws.s3.region" = "<region>",
  "aws.s3.endpoint" = "<the Region's S3 endpoint>"
);
```

The matrix set these five `aws.s3.*` properties ([StarRocks's Paimon catalog](https://docs.starrocks.io/docs/data_source/catalog/paimon_catalog/) describes them); its results keep their names, not their values. The trust store goes to the FE and the BE, as for Doris. A role's temporary credentials instead (with `aws.s3.session_token`, and `paimon.option.fs.s3a.session.token` for its JNI reader) made the first read of a primary-key table fail in one run, and later reads pass (section 9.2). The release's UAT runs this cell with a role's short-lived credentials, never long-lived keys ([UAT-CMP-08](../uat/cases.md#uat-cmp-08-starrocks-416-read-only)); if you use an IAM user's key, keep its policy to reading the warehouse prefix, and rotate it.

### 4.6 Checking a connection

With an engine's token, `GET /v1/config?warehouse=colvenna` answers 200 (this is what the install's smoke check asks); without a token, 401. An engine connected without grants lists only `default`.

## 5. Administering the catalog

The admin API manages principals, tokens, grants, the audit log and the repair of tables; [the admin API page](admin-api.md) has each operation with `curl`. Its first steps copy the CA and the first admin's token into files only you can read; in the serverless form the token is in the Secrets Manager secret `colvenna-catalog-<env>/admin-token`. Give each operator an admin principal of their own (`"admin": true`) rather than sharing the installer's.

**The permission model**, in short (the [LLD](../design/colvenna-catalog-lld.md#授权) has every rule):

| Rule | What it means |
| --- | --- |
| Scopes | `CATALOG`, `DATABASE`, `TABLE`, `VIEW`, `FUNCTION`. A table's branches and system tables (`t$branch_b`, `t$snapshots`) count as the table |
| Levels | `READ`: read tables, views and functions, and take read-only credentials. `WRITE`: also commit, roll back, tags, branches, partitions, reset consumers, and take read-write credentials. `MANAGE`: also create, alter, rename and drop |
| Creating and renaming | Need `MANAGE` on the new name too; dropping a database needs `MANAGE` on each object in it |
| `DENY` | Forbids its level and those above, wins over every `ALLOW` |
| Columns | A table-level `READ` with `columns` limits reads to them, through Paimon's `/auth` call, which clients make only on tables with `query-auth.enabled=true`; it is not an isolation boundary (section 9.4) |
| Listings | Show only what the principal can see: a database it may read, or one holding an object it may read; reading anything else by name answers 403 |
| Admins | The admin API needs an admin principal; tables need grants, admins included |
| When changes count | Within 2 seconds (`COLVENNA_AUTH_CACHE_TTL`) on every replica; per-table credentials already issued last until they expire |

**Rotating tokens.** A job's token: issue a new one, give it to the job, restart the job, then revoke the old one ([Rotating a token](admin-api.md#rotating-a-token)). The first admin's token is renewed by `install.sh` when it has less than 30 days left (it lasts 90; section 6.1 of the [deployment guide](install.md#61-running-it-again)); the old one stays valid until it expires. To replace it at once, revoke the admin's tokens described `bootstrap` from another admin principal, then run `install.sh` again: the token it holds no longer authenticates, so it issues a new one into the same Secret (not run yet).

**Rotating the database secrets.** The passwords of the database's master user and of the catalog's database user are not rotated by Secrets Manager (an accepted risk, #49); [Rotating the database secrets](database-secrets.md) rotates them by hand.

**Withdrawing per-table credentials at once.** Revoking a token, disabling a principal or revoking a grant stops new credentials within 2 seconds, but those already issued work until they expire (3 hours by default). To end them all now, revoke the data role's active sessions in IAM (`colvenna-catalog-<env>-data`: "Revoke active sessions", a deny on `aws:TokenIssueTime`; [IAM's guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html)). This interrupts every engine that uses per-table credentials: the catalog's replicas keep vending their cached credentials, and Paimon clients do not refresh credentials on a 403. So restart the catalog's replicas, to clear their caches (`kubectl -n colvenna-catalog rollout restart deployment/colvenna-catalog`), then the engines' jobs (Doris: refresh the catalog). Otherwise table data may be unreadable for up to about the credentials' lifetime less one hour. This has not been run; the serverless form's execution environments cache credentials as replicas do, and how to clear them all at once has not been worked out.

## 6. Backing up and restoring the metadata database

### 6.1 What is backed up

The metadata (databases, tables and their snapshot pointers, principals, tokens, grants, audit) is in PostgreSQL; the tables' files are in S3.

- **A database the install made** keeps 7 days of automated backups, with point-in-time recovery, and is protected from deletion. `uninstall.sh` without `--delete-data` keeps it; `--delete-data` deletes it with its automated backups and no final snapshot.
- **A database you brought** is backed up as you set it up.
- **The bucket** is not versioned, and nothing in the install backs it up. A restore brings back the metadata only; the files are as they are now.

Take a manual snapshot before anything risky (an upgrade, a change by hand); it lasts until you delete it:

```bash
aws rds create-db-snapshot --region <region> --db-instance-identifier <instance> --db-snapshot-identifier <snapshot>
aws rds wait db-snapshot-available --region <region> --db-snapshot-identifier <snapshot>
```

The instance a `Storage` stack made is its `AWS::RDS::DBInstance`:

```bash
aws cloudformation describe-stack-resources --region <region> --stack-name ColvennaCatalog<Env>Storage \
  --query "StackResources[?ResourceType=='AWS::RDS::DBInstance'].PhysicalResourceId" --output text
```

### 6.2 Restoring

A restore makes a new instance from a snapshot, or from a point in time (RDS's guides: [from a snapshot](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_RestoreFromSnapshot.html), [to a point in time](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html)), and points the install at it. Nothing is restored over the running database, which stays as it was until you delete it.

1. Stop the jobs that write through the catalog, so that the tables are quiet for the checks of section 6.3.
2. Read the running instance's settings:

   ```bash
   aws rds describe-db-instances --region <region> --db-instance-identifier <instance> \
     --query 'DBInstances[0].{class: DBInstanceClass, subnets: DBSubnetGroup.DBSubnetGroupName, groups: VpcSecurityGroups[].VpcSecurityGroupId, parameters: DBParameterGroups[0].DBParameterGroupName}'
   ```

3. Make the new instance with them: from a snapshot (`aws rds describe-db-snapshots --region <region> --db-instance-identifier <instance>` lists them),

   ```bash
   aws rds restore-db-instance-from-db-snapshot --region <region> \
     --db-instance-identifier <new instance> --db-snapshot-identifier <snapshot> \
     --db-instance-class <class> --db-subnet-group-name <subnets> --vpc-security-group-ids <groups> \
     --db-parameter-group-name <parameters> --no-publicly-accessible --enable-iam-database-authentication \
     --backup-retention-period 7 --deletion-protection --enable-cloudwatch-logs-exports postgresql
   ```

   or from a point in time in the last 7 days,

   ```bash
   aws rds restore-db-instance-to-point-in-time --region <region> \
     --source-db-instance-identifier <instance> --target-db-instance-identifier <new instance> \
     --restore-time <YYYY-MM-DDTHH:MM:SSZ> \
     --db-instance-class <class> --db-subnet-group-name <subnets> --vpc-security-group-ids <groups> \
     --db-parameter-group-name <parameters> --no-publicly-accessible --enable-iam-database-authentication \
     --backup-retention-period 7 --deletion-protection --enable-cloudwatch-logs-exports postgresql
   ```

   then wait for it, and read its endpoint:

   ```bash
   aws rds wait db-instance-available --region <region> --db-instance-identifier <new instance>
   aws rds describe-db-instances --region <region> --db-instance-identifier <new instance> \
     --query 'DBInstances[0].Endpoint.Address' --output text
   aws logs put-retention-policy --region <region> --log-group-name /aws/rds/instance/<new instance>/postgresql \
     --retention-in-days 365
   ```

   The same subnet group and security groups put it where the old one was, admitted from the same clients; IAM database authentication is what the serverless form signs in with; its log, exported as the old one's, is kept a year as the stack keeps that one's (the log group exists once the instance has written to it).
4. Point the install at it, in its configuration, leaving every other key as it was (in the serverless form, `COLVENNA_NETWORK` too, `create` or `existing`): `COLVENNA_DB=existing`, `COLVENNA_DB_ENDPOINT=<the new endpoint>`, `COLVENNA_DB_PORT=5432`, and `COLVENNA_DB_ADMIN_SECRET_ARN`: for a database the install made, the `Storage` stack's output `DbAdminSecretArn` (the restored instance has the admin password the snapshot had). If the install made the bucket too, also `COLVENNA_BUCKET=<the stack's BucketName>`, so that `install.sh` leaves the `Storage` stack as it is ([section 6.4](install.md#64-installing-again-onto-kept-data) of the deployment guide reads both). Then run `install.sh` again, from the checkout that installed the catalog:
   - Its preflight checks the new instance: in the install's VPC (the EKS cluster's; in the serverless form, `COLVENNA_VPC_ID`, or with `COLVENNA_NETWORK=create` the VPC the install made, the `Network` stack's), admitting the catalog's port from the catalog (the nodes' security groups; the functions' security group, or CIDRs holding their subnets), not publicly accessible, taking TLS connections only (`rds.force_ssl=1`, which the `Storage` stack's parameter group sets), and, in the serverless form, with IAM database authentication on.
   - It brings the catalog's database users in line: in the EKS form, the catalog's user gets the password of the `Eks` stack's database user secret again; in the serverless form, the catalog's user and the authorizer's (`<COLVENNA_DB_USER>_authz`) have no password and are granted `rds_iam` again.
   - The catalog moves to the new instance: in the EKS form the pods roll to the new endpoint; in the serverless form the functions take it, and their `rds-db:connect` permission names the new instance.
   - It issues a new admin token if the one it holds is unknown to the restored database.
5. Check and repair the tables (section 6.3), then start the jobs again.

Preflight refuses (exit 3), before it changes anything:

- **`COLVENNA_DB=existing` without `COLVENNA_BUCKET`**, while the `Storage` stack holds a database: `install.sh` would deploy the stack again without it, deleting it, and the subnet group, security group and parameter group the restored instance uses. The message names the bucket to set.
- **An instance the catalog should not use**: outside the install's VPC; publicly accessible; not requiring TLS; in the serverless form, without IAM database authentication. The catalog itself always connects with `sslmode=verify-full`; nothing in the configuration lowers it.
- **In the serverless form with `COLVENNA_NETWORK=create`, `COLVENNA_DB=existing` before the install has made its network**: there is no VPC of the install's for the instance to be in. A first install makes its own database.
- **`COLVENNA_DB=create` while the install runs on another database than the `Storage` stack's**, as after a restore: it would move the install back to the stack's database. Keep the configuration pointed at the restored instance.

In the serverless form with a new network (`COLVENNA_NETWORK=create`), the procedure has run end to end, in us-west-2 on 2026-10-29 (#59): the restore from a manual snapshot took 11 minutes, and `install.sh`'s move to the new instance 7 (it also built and rolled out a new image). The catalog then listed the databases, tables, snapshots, principals and grants of the restore point, every table was consistent, and the database and principal made after the point were gone. In the EKS form it has run in offline tests only. The release's UAT runs it in both forms (UAT-DEP-10). When the restored database has served long enough, delete what you no longer need: the snapshot (`aws rds delete-db-snapshot`), and the old instance. An instance the `Storage` stack made is still the stack's: to delete it, turn off its deletion protection (`aws rds modify-db-instance --region <region> --db-instance-identifier <instance> --no-deletion-protection --apply-immediately`), then `aws rds delete-db-instance --region <region> --db-instance-identifier <instance> --final-db-snapshot-identifier <name>`. `uninstall.sh --delete-data` deletes the `Storage` stack's instance, skipping one already gone, but never the restored one, which the install did not make: it uses the stack's subnet group, security group and parameter group, which the stack cannot delete while the instance exists. So `--delete-data` refuses (exit 3, naming it) while the restored instance is there (an instance the install did not make, with the stack's subnet group or security group, or in the VPC a serverless install made), before it changes anything: delete the restored instance yourself first (turn off its deletion protection, then `aws rds delete-db-instance --region <region> --db-instance-identifier <new instance> --skip-final-snapshot --delete-automated-backups`, or keep a final snapshot of your own), and its log group (`/aws/rds/instance/<new instance>/postgresql`).

### 6.3 After the restore

The restored metadata is as at the restore point; the files are as now. So:

- **Tables committed to since the point** have snapshots in their files beyond the catalog's pointer. Reconcile them: the catalog re-reads the table's files and publishes those snapshots, the branches it lacks, and the schema.
- **Tables dropped since the point** are listed again, but their files are gone. The consistency check reports them missing: not consistent, with the problems `no schema file in <path>, where the catalog serves schema <id>` and, for each branch that had a snapshot, `<branch>: pointer snapshot <n> has no file`; reconciling refuses a table without a schema file (400). Unregister them: that removes them from the catalog and touches no file.
- **Tables created since the point** are missing from the catalog, though their files are in the warehouse. Register them again at their directory, with Paimon's `registerTable`, or with the REST API.
- **Tables dropped just before the point**, whose files were still to be deleted then, are back in the list of those files (`GET $CATALOG/purges`, [admin API](admin-api.md#dropped-tables-files)), and their files are deleted at the next purge, as if the restore had not happened. A table made since at the same directory is spared: the purge finds a schema file newer than the drop and removes only the record. Until then, registering that table again is refused (400, "still to be deleted"): run the purge (the [deployment guide](install.md#42-the-serverless-form) shows how; in the EKS form, wait at most 5 minutes), then register it.
- **Principals, tokens and grants** are as at the point: those made since are gone (reissue them), and those revoked or disabled since are back in force. Revoke and disable them again.
- **The audit log** since the point is only in the old database.

Check every table, with the admin's `curl` configuration of the [admin API page](admin-api.md#before-you-start) (`CATALOG` is the admin API's address there):

```bash
PAIMON=https://localhost:8181/v1/colvenna
# A large table's check comes in pages in the serverless form: the last page is the report.
check() {
  local page token
  page="$(curl -K admin.curlrc "$CATALOG/consistency/$1/$2")"
  while token="$(jq -r '.nextPageToken // empty' <<<"$page")"; [[ -n "$token" ]]; do
    page="$(curl -K admin.curlrc "$CATALOG/consistency/$1/$2?pageToken=$token")"
  done
  jq -c '{table, consistent, problems}' <<<"$page"
}
curl -K admin.curlrc "$PAIMON/databases?maxResults=1000" | jq -r '.databases[]' | while read -r db; do
  curl -K admin.curlrc "$PAIMON/databases/$db/tables?maxResults=1000" | jq -r '.tables[]' | while read -r table; do
    check "$db" "$table"
  done
done
```

The listings return 1000 names at most; follow `nextPageToken` (`&pageToken=<token>`) beyond that. In the serverless form a check answers after about 8 seconds of checking: a large table's report then comes in pages, which `check` follows ([admin API](admin-api.md#checking-and-repairing-a-table)). Then reconcile each table whose files are ahead of the catalog, and unregister each whose files are gone, as [Checking and repairing a table](admin-api.md#checking-and-repairing-a-table) shows (`POST $CATALOG/tables/<db>/<table>/reconcile` and `.../unregister`). Register each table created since the point again, through the Paimon REST API:

```bash
curl -K admin.curlrc -X POST "$PAIMON/databases/<db>/register" -H 'Content-Type: application/json' \
  -d '{"identifier": {"database": "<db>", "object": "<table>"}, "path": "s3://<bucket>/<prefix>/<db>.db/<table>"}'
```

The path is the table's directory: `<db>.db/<table>` under the warehouse, or `<db>.db/<table>-<8 characters>` when the name had been used before (`aws s3 ls s3://<bucket>/<prefix>/<db>.db/` lists them). A register takes only a directory in the warehouse that no registered table overlaps. Run the check again: every table should be consistent. The check, reconcile and register calls are tested against the server (`ConsistencyCheckTest`, `ReconcileTest`, `RegisterTableTest`), a restore simulated there by rewinding a table's pointer, or by deleting a table's files.

## 7. Day-to-day operation

### 7.1 Upgrading

Run `install.sh` again from the newer code ([deployment guide](install.md#63-upgrading), section 6.3). Clients keep their tokens, grants and tables. In the EKS form the replicas are replaced one at a time, so the Service keeps answering; in the serverless form the API moves to the new version at once. Before 1.0, versions promise no compatibility with each other: read the candidate's release notes first, and take a snapshot (section 6.1).

### 7.2 Health

- **EKS.** Two pods, Ready when their database answers and they are not stopping (`/readyz`; #64), alive while the server runs (`/healthz`); both listen on 127.0.0.1 inside the pod only:

  ```bash
  kubectl -n colvenna-catalog get pods -l app.kubernetes.io/name=colvenna-catalog
  kubectl -n colvenna-catalog exec deploy/colvenna-catalog -- curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8182/readyz
  ```

- **Serverless.** No health endpoint is reachable. API Gateway's and Lambda's own CloudWatch metrics show requests, errors and durations; the authorizer answers 503 when it cannot reach the database.

### 7.3 Logs

Each request is one line of the server's access log (`colvenna.access`): request id (the `X-Request-Id` response header, also the audit event's), method, path, status, duration, principal and action:

```text
<time> INFO  [<thread>] colvenna.access - <request id> GET /v1/colvenna/databases/sales/tables 200 10ms principal=etl-writer action=LIST_TABLES
```

An error's details are in the server log under the same request id; the client gets a generic message. Read it with `kubectl -n colvenna-catalog logs -l app.kubernetes.io/name=colvenna-catalog --prefix` (EKS), or in the log group `/aws/lambda/colvenna-catalog-<env>-server` (serverless; the API's own access log is `/aws/apigateway/colvenna-catalog-<env>-api`).

### 7.4 Metrics (EKS)

Port 9100 serves Prometheus metrics to pods of namespaces labelled `colvenna-catalog/monitoring=true` (`COLVENNA_METRICS_NAMESPACE_LABEL`): `colvenna_catalog_requests_total{action, status}`, `colvenna_catalog_request_seconds` (a histogram, by `action`) and `colvenna_catalog_commits_total{table, outcome}`. The serverless form serves no metrics port.

### 7.5 Audit, checks and repairs

- **The audit log** ([the admin API page](admin-api.md#the-audit-log)) answers who did what: every write, every decision on a catalog object, every refused request, every credential issued (with its access key ID, which CloudTrail's S3 data events also record). Watch its refusals (`outcome=DENIED`) and the catalog's own events: `FENCE_TABLE`, a table fenced, and `ROLL_FORWARD_COMMIT`, a commit the server completed after the request that made it was lost.
- **A fenced table** refuses commits with 409. A Paimon client sends the commit again `commit.max-retries` times (10 by default), each refused, then fails with `Commit failed after ... with <n> retries`, whose innermost cause is `AlreadyExistsException: Table <db>.<table> is fenced and accepts no commits: <why>`: no retry gets past the fence. Something else is committing to its files: find and stop that writer (often a job still configured with another catalog), then reconcile the table ([Checking and repairing a table](admin-api.md#checking-and-repairing-a-table)). Streaming readers find snapshots by their files, so they read the foreign snapshots in the meantime.
- **Expiry.** Tokens, grants and the admin's own token expire. List tokens with their expiry, and grants (the listing shows expired ones too), and renew what is due before jobs start failing with 401 or 403. Run `install.sh` again at least every 30 days, for the admin's token ([deployment guide](install.md#61-running-it-again), section 6.1).

## 8. Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| 401 | No token, or one that is unknown, expired or revoked, or of a disabled principal | Check the client's `token` and `token.provider=bear`; list the principal's tokens |
| 403 (`...NoPermissionException` in the engine) | No grant covers the operation, or a `DENY` does; or a non-admin called the admin API | The audit event of the request id says which privilege it needed and what it relied on |
| A database or table that exists is missing from a listing | The principal may not read it: listings show only what it may | Grant `READ` |
| Commits fail with 409 | The table is fenced | Section 7.5 |
| 503 | The client retries it (Paimon retries 503 up to five times), and a retried commit takes effect once: the server is at its request limit, the database is unavailable, a lock wait timed out, STS throttled, or the server's database session ended during the request (a restart, a failover, `pg_terminate_backend`). In the serverless form also: more requests at once than `COLVENNA_LAMBDA_CONCURRENCY` (the gateway answers it, without `X-Request-Id`, and nothing was done), or the server's function failed (section 9: a few writes are made again by the retry) | If it persists: the pods' readiness, the database, the server log; serverless: the server function's `Throttles` and errors, then a higher `COLVENNA_LAMBDA_CONCURRENCY` |
| 429 (serverless) | More requests a second than the API admits, `8 × COLVENNA_LAMBDA_CONCURRENCY`; nothing was done, and the client retries it | If jobs still fail after the client's retries: a higher `COLVENNA_LAMBDA_CONCURRENCY` |
| 500 saying the connection was lost while the request committed | The database session ended during a write's COMMIT (not a commit's: that is a 503), so whether the write took effect is unknown | Check the object (list the tables, read the schema) before repeating it |
| 501 from `/tables/<t>/token`, every table access failing | The client has `data-token.enabled=true`, and the catalog vends no credentials (`COLVENNA_DATA_TOKENS=disabled`) | Install with `aws-sts`, or set the client's option to `false` and give the engine S3 access |
| 400 from `/tables/<t>/token` | The table is not a managed table of the warehouse | Credentials are vended only for managed tables |
| S3 `AccessDenied` with per-table credentials | The credentials cover only the table's directory; a principal with only `READ` gets read-only ones | Check the grant, and that the client writes inside the table |
| `PKIX path building failed` | The JVM's trust store lacks the install's CA | Section 4.1, step 3, for every JVM |
| `certificate verify failed: CA cert does not include key usage extension` (Python 3.13 and later) | The CA's certificate is from an install made before #58, without `keyUsage` | Run `install.sh` again, then copy `ca.crt` again ([deployment guide](install.md#62-replacing-the-certificate-or-the-ca-by-hand-eks), section 6.2) |
| TLS errors to S3 or STS once the CA is trusted | The trust store holds only the CA | Start from a copy of the JDK's `cacerts` |
| `No subject alternative names matching` | The URI's host name is not one the certificate names | Use the Service's name, or the internal NLB's |
| A pod's connection times out (EKS) | Its namespace lacks the client label | Label the namespace (`COLVENNA_CLIENT_NAMESPACE_LABEL`) |
| `serve` refuses to start: `An S3 warehouse needs COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION` | The server's settings lack S3A's Region: they were not made by `install.sh` of this version (its chart or its serverless stack), which sets it | Install again with `install.sh`; elsewhere, set it to the bucket's Region ([deployment guide](install.md#24-for-the-eks-form), section 2.4) |
| Pods not Ready | The database does not answer: its security group, the instance, the password | The pods' log; `readyz` (section 7.2) |
| Pods refuse to start: the schema is newer | An older version against a database a newer one migrated | Install the newer version again ([deployment guide](install.md#63-upgrading)) |
| Serverless: 403 with API Gateway's own body | The request did not come through one of the install's `execute-api` endpoints | Use a VPC with a listed endpoint |
| Serverless: 413 | The request body is over about 6 MiB (Lambda's limit) | Nothing was done; commit fewer partitions at once (section 9.3) |
| Serverless: 504 | The request took longer than API Gateway's 29 seconds | A commit still takes effect exactly once; the client's retry finds it |
| Serverless: 503 from the authorizer | It cannot reach the database to check the token | The database, its security group, the authorizer's log |
| Doris or StarRocks miss the latest rows of a deletion-vector table | Their bundled Paimon does not read uncompacted level-0 files | Section 9.2 |
| A Flink job FINISHED, but rows are missing | Section 9.1 | Write bounded inputs in batch mode, or at parallelism 1 |

## 9. Known limitations

### 9.1 Apache Paimon: bounded streaming writes from Flink

Paimon 2.0.0's Flink connector (2.1.0 and Paimon's main branch have the same code) can silently drop data when Flink writes a **bounded** input in **streaming** mode, with checkpoints, at a **write parallelism above 1**, and the writer subtasks finish after different checkpoints. The committer then takes the end-of-input data of the subtask that finished later as already committed, and drops it. The job ends `FINISHED`, with no error and no log line; the dropped data files stay in the table's directory, unreferenced. It was reproduced deterministically on 2026-10-28. It is not the catalog's: the catalog commits everything it receives. Unbounded inputs, batch mode and a parallelism of 1 are not affected.

Until it is fixed upstream (Colvenna Lake's issue [#6](https://github.com/colvenna/colvenna-lake/issues/6) tracks the fix), write bounded inputs in batch mode (`execution.runtime-mode=batch`), or, in streaming mode, at a write parallelism of 1.

### 9.2 Engines

From the [compatibility matrix](../compat/matrix.md) (generated by `tests/compat/matrix.py page`; each limitation's reason is in `tests/compat/declared.tsv`, the raw results in `docs/compat/runs/`):

- **Doris 4.1.5** (bundles Paimon 1.4.2):
  - Read-only in 1.0: it is verified only reading tables Flink or Spark wrote, not writing, nor creating or dropping databases and tables.
  - Primary-key tables with deletion vectors: it does not read the rows still in uncompacted level-0 files, which takes Paimon 2.0 (`deletion-vectors.merge-on-read`). Fully compacted tables read correctly. Its native reader and `force_jni_scanner` give the same result.
  - Per-table credentials work (`paimon.rest.data-token.enabled=true`).
- **StarRocks 4.1.6** (bundles Paimon 1.3.1):
  - Read-only, as Doris.
  - Primary-key tables with deletion vectors: as Doris.
  - It does not use per-table credentials: it needs read-only S3 credentials of its own. An IAM user's access key works. With a role's temporary credentials instead (`aws.s3.session_token` with `paimon.option.fs.s3a.session.token`), the first read of a primary-key table through its JNI reader failed (`TemporaryAWSCredentialsProvider has no credentials`) and later reads passed; why was not established.
- **The serverless form** has not been run in the matrix yet.
- **Column grants** depend on the engine calling Paimon's `/auth` (tables with `query-auth.enabled=true`); which engines do is not verified.

### 9.3 The serverless form

- A request body of about 6 MiB at most (Lambda's limit, JSON escaping included): about 41,000 partitions in one commit, with a one-key partitioning. Beyond it, API Gateway answers 413 and nothing takes effect.
- A response of 4 MiB at most (`COLVENNA_MAX_RESPONSE_BYTES`): listings end their page before the item that would pass it, and the client reads on with the next page token; one item larger than that answers 500, naming it.
- API Gateway's 29-second timeout; the server's waits are set to end before it, but a request can still exceed it (504), section 8.
- The authorizer keeps each token's answer for 60 seconds; the server checks every request again, so a revoked token is still refused within 2 seconds, by the server.
- No metrics port; no engine run in the matrix yet; the `--release` path and `COLVENNA_NETWORK=existing` tested offline only.

### 9.4 The catalog

- **No row filters or column masking**, and no end users: the server knows principals only. An engine's users (Doris's, say) reach it as the principal of the engine's token; their privileges and their audit are the engine's.
- **Column-limited reads are not an isolation boundary.** An engine with a table's credentials can read every column of its files. Keep sensitive columns in a table of their own.
- **Without per-table credentials** (`COLVENNA_DATA_TOKENS=disabled`), a `READ` grant does not stop an engine with S3 write access of its own from writing the table's files. With them, the grants decide what an engine may do with the files only if its own role has no access to the tables' data.
- **The bucket is in the server's Region.** The data role's policy has no KMS permissions: SSE-S3 only, unless you grant the roles `kms:Decrypt` and `kms:GenerateDataKey` on your key yourself.
- **No client-chosen locations.** External tables (`path`), `data-file.external-paths` and global index paths outside the warehouse need roots the operator allows (`COLVENNA_EXTERNAL_LOCATION_ROOTS`), which the chart and `install.sh` do not set: they are refused.
- **Endpoints not served**: `rename` of a branch answers 501 (Paimon 2.0.0's client never sends it); the partition endpoints answer 501 except for tables with `metastore.partitioned-table=true` (the client then lists partitions from the files); the token endpoint answers 501 when the catalog vends no credentials.
- **Names** of databases, tables, views, functions, branches, tags and consumers cannot be `.` or `..` or hold `/ \ ? # % *` or control characters; database names cannot hold `.`.
- **A request body of 64 MiB at most** in the EKS form.
- **Requests beyond what the serverless form serves at once** (`COLVENNA_LAMBDA_CONCURRENCY`) answer 429 or 503, and nothing of them is done. A request whose server function failed answers 503 too, and it may have been done: the client's retry is then answered as a lost answer's is (a create gets 409), except for a few writes that a retry makes again: a branch fast-forward, `replaceTable`, a schema rollback, a consumer reset, and a token issued (list the principal's tokens and revoke the extra one). A rollback is not made again: the same rollback sent by the same caller within 5 minutes is taken for its retry (see rolling back, below). It takes a function failing after its write, which is rare; check such a write's effect when a job reports the failure ([LLD](../design/colvenna-catalog-lld.md#无服务器形态)). Paimon's client sends them again by itself, five times over about 31 seconds; a job that still fails with "Too Many Requests" or "Service Unavailable" needs a higher `COLVENNA_LAMBDA_CONCURRENCY` ([deployment guide](install.md#25-for-the-serverless-form)).
- **Dropping a table** deletes its managed directory only; client-chosen locations are never deleted by the server. The table is gone from the catalog when the drop answers; in the serverless form its files are deleted afterwards, within about 15 minutes, by the purge function ([admin API](admin-api.md#dropped-tables-files)), while in the EKS form the drop deletes them before it answers. A job still writing to a dropped table's directory makes its purge fail until it stops. Once the last table directory of a database is deleted, the database's directory under the warehouse goes too (in the serverless form, with that purge), unless something else is still in it, such as a table renamed into another database, which keeps its directory.
- **Rolling back, deleting a tag, deleting a branch** answer once the change is made: the snapshots after the target, the tag, the branch are gone. The data files and manifests that only they referenced are deleted afterwards, as a dropped table's are: a tag's within about 15 minutes by the purge function in the serverless form, before the request answers in the EKS form; a rollback's and a branch's an hour later in both forms, so that a job restoring from a checkpoint taken before can still commit them again ([admin API](admin-api.md#files-rollbacks-and-deletions-leave)); after that hour, such a restore fails on the missing files instead. Only what no snapshot, tag or changelog of any branch, nor any branch's current snapshot in the catalog, references then is deleted, so a branch made from a deleted tag keeps its files (Paimon's own tag deletion does not see other branches). Paimon's own rollback and branch deletion leave such files behind for good; the catalog deletes them too (the [LLD](../design/colvenna-catalog-lld.md#无服务器形态) cites Paimon's code for each). In the serverless form, a rollback that removes thousands of snapshots, or the deletion of a branch with thousands, may answer 503 after about 8 seconds of recording them, having changed nothing; sent again, it goes on (Paimon's client does so by itself, 5 times). The same rollback sent again by the same caller within 5 minutes is taken for its retry and not made twice: while the branch is where it left it, it answers as it did; once the branch has had commits since, it answers 409 rather than removing them. To roll back to the same snapshot again on purpose within that time, send the branch's latest snapshot as `fromSnapshot` (or, with `fromSnapshot` already, wait the 5 minutes).
- **Dropping a database** with many tables in the serverless form answers 503 after about 8 seconds of dropping them; the tables dropped stay dropped, and the request sent again goes on. Paimon's client sends it again by itself, up to 5 times; drop a database of thousands of tables again if it still answers 503.
- **A branch made without a tag** is not protected from the expiration of main's snapshots, whose data files it shares: make long-lived branches from a tag.
- **Schema files** are written as Paimon writes them, not conditionally. The consistency check compares them with PostgreSQL, which is what `GetTable` answers from.

## 10. The main flows and their UAT cases

The flows of this manual and the deployment guide, and the [UAT cases](../uat/cases.md) that check them in both forms and both Regions of a release candidate:

| Flow | Where | UAT case |
| --- | --- | --- |
| Install from nothing; into existing resources | [Deployment guide](install.md) 4 | UAT-DEP-01, UAT-DEP-02 |
| Run again; resume after an interruption; refused configurations | [Deployment guide](install.md) 4, 7 | UAT-DEP-03, 04, 05 |
| Upgrade; go back | [Deployment guide](install.md) 6.3; 6 | UAT-DEP-06, 07 |
| Uninstall keeping the data, install again; uninstall deleting it | [Deployment guide](install.md) 6.4, 8 | UAT-DEP-08, 09 |
| Restore the metadata database | 6 | UAT-DEP-10 |
| The internal NLB | 3; [deployment guide](install.md) 2.4 | UAT-DEP-11 |
| Connect each engine | 4 | UAT-CMP-01 to 08 |
| Tables through the catalog; per-table credentials, their scope and renewal | 2, 4 | UAT-CAT-01, 02, 03 |
| Grants and audit; the admin API; token rotation | 5, 7.5; [the admin API](admin-api.md) | UAT-CAT-04, 05, 06 |
| Health and metrics; size limits | 7.2, 7.4, 9.3 | UAT-CAT-07, 08 |
| Refusals at every entry; no function URL, no public resource | 3; [deployment guide](install.md) 5 | UAT-SEC-01 to 04 |
| Commits of concurrent writers under faults; a replica lost | 2 | UAT-FLT-01, 02 |
| A fenced table, reconciled | 7.5 | UAT-FLT-03 |

> Note: the issue and pull request numbers (such as #49) in this document are those of the project's early private repository; this repository has no pages for them. Linked issue numbers (such as [#9](https://github.com/colvenna/colvenna-catalog/issues/9)) are this repository's current ones.
