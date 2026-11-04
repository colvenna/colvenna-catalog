<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/logo-dark.png">
  <img alt="Colvenna Catalog" src="assets/brand/logo-light.png" width="660">
</picture>

# Colvenna Catalog

An open, Apache-2.0 REST catalog server for [Apache Paimon](https://paimon.apache.org/), backed by PostgreSQL, for teams running Paimon on AWS. It implements the REST catalog contract of Apache Paimon 2.0.0, so engines connect with Paimon's own `RESTCatalog` client, and it runs in your own AWS account: the metadata stays in your PostgreSQL and the tables in your S3 bucket.

**Status: 1.0.0 released** ([release notes](release/v1.0.0/notes.md)), the release candidate `v1.0.0-rc.4` promoted without a rebuild. It installs into your AWS account in two forms, on Amazon EKS or serverless on AWS Lambda, from one image. The image and the Helm chart are on the GitHub Container Registry, by the digests of [`release/v1.0.0/manifest.json`](release/v1.0.0/manifest.json), and anyone can pull them:

```bash
docker pull ghcr.io/colvenna/colvenna-catalog:1.0.0
helm pull oci://ghcr.io/colvenna/charts/colvenna-catalog --version 1.0.0
```

## What it does

- **The Paimon REST contract.** Every path of the REST catalog OpenAPI 1.0 that Paimon 2.0.0 publishes is implemented, except the few the [LLD](docs/design/colvenna-catalog-lld.md) lists with their reasons. Paimon's own contract tests (`RESTCatalogTest` and `CatalogTestBase` from the 2.0.0 test jars) run against the real server and PostgreSQL in every build: on 2026-10-28, 107 cases, 96 passed and 11 skipped, each skip with its reason.
- **Atomic commits on S3.** Without a catalog that commits for it, Paimon publishes a snapshot by renaming a temporary file, which is not atomic on S3. Colvenna Catalog commits on the server instead: it takes a row lock in PostgreSQL and writes the snapshot file with an S3 conditional write (`If-None-Match: *`), so concurrent writers and replicas cannot overwrite each other's snapshots.
- **No anonymous access.** Every API request needs a bearer token the server issued, over TLS. Tokens are opaque (`colvenna_` and 32 random bytes); the server keeps only their SHA-256, and they expire and can be revoked. The health endpoints listen only on the loopback interface.
- **Grants and audit.** Service identities (principals) get `READ`, `WRITE` or `MANAGE` on the whole catalog, a database or one object, with explicit `DENY` and column-limited reads. Every grant names its purpose, approver, approval and expiry. Writes, credential issues and denials are audited, and the audit can be queried.
- **Per-table credentials.** Optionally (`COLVENNA_DATA_TOKENS=aws-sts`, the installer's default), the server vends short-lived AWS STS credentials whose session policy covers one table's prefix, so engines need no S3 access of their own.

## Install

`install.sh` installs into your AWS account, in either form, from a clean checkout (`--from-source`), or the tag of a release candidate or of a release promoted from one (`--release <candidate>` or `--release v1.0.0`; a release's install has run live on the EKS form, and on the serverless form in offline tests); `uninstall.sh` removes it and keeps the database and the bucket unless given `--delete-data`. Copy [`deploy/config/example.env`](deploy/config/example.env), which lists every key, to `deploy/config/<env>.env` (git ignores it) and set `COLVENNA_FORM`:

- `eks`: two catalog pods in your EKS cluster, behind a Service that only labelled namespaces reach (and, if asked, an internal NLB).
- `serverless`: the same image on AWS Lambda, behind a private REST API that only your VPC's `execute-api` endpoints reach, with a bearer-token authorizer in front of the server's own check, and no compute running while idle. `COLVENNA_NETWORK=create` makes a VPC with no internet path and the endpoints it needs; `existing` uses yours. The functions sign in to PostgreSQL with RDS IAM tokens, so no password is stored in them. The [LLD](docs/design/colvenna-catalog-lld.md#部署) has the details. The serverless form's `--release` path and `COLVENNA_NETWORK=existing` (with an existing database) ran live in `v1.0.0-rc.2`'s UAT, in both Regions (UAT-DEP-01 and 02 of the [UAT cases](docs/uat/cases.md); [the report](docs/uat/reports/v1.0.0-rc.2/report.md)).

Neither form has a public or anonymous entry, and the serverless form never has a Lambda function URL: the CDK app refuses both when it synthesizes.

### Quick start on Amazon EKS

You need, in a standard AWS Region:

- an active EKS cluster that enforces NetworkPolicy, with an IAM OIDC provider for its issuer (for IRSA);
- the Region bootstrapped for the AWS CDK in the cluster's account, and credentials of that account;
- on the machine you install from: AWS CLI v2, `jq`, Python 3.8+, Node.js 20+ with `npm`, OpenSSL, `curl`, `git`, `tar`, `sha256sum` and bash 4.4+, and, to build the image from source, Docker with Buildx, Maven 3.8+ and JDK 21.

The installer fetches its own pinned `kubectl`, Helm and `crane`. Its preflight checks all of this, and refuses with the reason, before it changes anything.

1. Copy the configuration and fill it in. [`deploy/config/example.env`](deploy/config/example.env) lists and explains every key: the environment's name, account, Region and cluster, an existing S3 bucket or a new one, an existing PostgreSQL or a new RDS PostgreSQL 16, per-table credentials, and an optional internal Network Load Balancer.

   ```bash
   cp deploy/config/example.env deploy/config/<env>.env   # git ignores it
   ```

2. Install. It shows the account, Region, cluster and version, and asks before it changes anything; run it again at any time to carry on or bring the install up to date.

   ```bash
   AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --from-source
   ```

   It deploys the AWS CDK stacks (the server's IAM roles, an ECR repository, and the bucket and database if it makes them), builds and pushes the image, makes a private CA and the server's certificate, installs the Helm chart with two replicas, issues the first admin token straight into the Kubernetes Secret `colvenna-catalog-admin-token` (it is never printed), creates Paimon's `default` database, and checks that an anonymous request is refused and the admin's succeeds. `./uninstall.sh --config deploy/config/<env>.env` removes it again and keeps the bucket and the database.

3. Give an engine its own identity. With the admin token and the CA from the cluster (default names shown), in a directory of their own, out of the checkout:

   ```bash
   umask 077
   mkdir -p ~/.colvenna-admin && cd ~/.colvenna-admin
   kubectl -n colvenna-catalog get secret colvenna-catalog-admin-token -o jsonpath='{.data.token}' | base64 -d > admin.token
   kubectl -n colvenna-catalog get secret colvenna-catalog-ca-key -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
   printf 'header = "Authorization: Bearer %s"\n' "$(cat admin.token)" > admin.curlrc
   kubectl -n colvenna-catalog port-forward svc/colvenna-catalog 8181:8181 >/dev/null &
   for _ in $(seq 30); do curl -s -o /dev/null --cacert ca.crt https://localhost:8181/ && break; sleep 1; done
   api() { curl -sS --fail-with-body --cacert ca.crt -K admin.curlrc -H 'Content-Type: application/json' "$@"; }

   api -X POST https://localhost:8181/v1/colvenna/databases -d '{"name": "sales", "options": {}}'
   api -X POST https://localhost:8181/colvenna/admin/v1/principals -d '{"name": "flink-sales"}'
   api -X POST https://localhost:8181/colvenna/admin/v1/grants -d '{"principal": "flink-sales",
     "scope": "DATABASE", "database": "sales", "privilege": "MANAGE", "purpose": "sales pipeline",
     "approvedBy": "<approver>", "approval": "<ticket>", "expiresAt": "2027-06-30T00:00:00Z"}'
   api -X POST https://localhost:8181/colvenna/admin/v1/principals/flink-sales/tokens -d '{"ttl": "P30D"}'
   ```

   The last call returns the engine's token, once. Grants are the only access there is: the installer grants the admin `MANAGE` on the catalog, and a principal without grants sees nothing. `curl` reads the admin's token from `admin.curlrc` (`printf` is built into the shell), so it is never on a command line. [The admin API's manual page](docs/manual/admin-api.md) has the other operations: tokens and their rotation, grants, the audit log, table repairs.

4. Point the engine's Paimon catalog at the server, from a namespace labelled `colvenna-catalog/client=true` (`COLVENNA_CLIENT_NAMESPACE_LABEL`), with a JVM trust store holding the JDK's `cacerts` and `ca.crt`. In Flink SQL:

   ```sql
   CREATE CATALOG colvenna WITH (
     'type' = 'paimon',
     'metastore' = 'rest',
     'uri' = 'https://colvenna-catalog.colvenna-catalog.svc:8181',
     'warehouse' = 'colvenna',
     'token.provider' = 'bear',
     'token' = '<the engine token>',
     'data-token.enabled' = 'true'
   );
   ```

   Spark takes the same options as `spark.sql.catalog.<name>.*` with `org.apache.paimon.spark.SparkCatalog`.

### The serverless form

With `COLVENNA_FORM=serverless`, the same command installs the four stacks the [LLD](docs/design/colvenna-catalog-lld.md#部署) describes: the network (`COLVENNA_NETWORK=create`, or `existing` with `COLVENNA_VPC_ID`, `COLVENNA_SUBNET_IDS` and `COLVENNA_API_ENDPOINT_IDS`), the storage (when it makes the bucket or the database), the image repository, and the functions with their private REST API. Besides the tools above it needs, in the Region, API Gateway's account-level CloudWatch Logs role and enough unreserved Lambda concurrency for `COLVENNA_LAMBDA_CONCURRENCY`; preflight checks both, and with `existing`, the VPC's endpoints and database.

The install ends by printing the API's URL, `https://<api>.execute-api.<region>.amazonaws.com/catalog`; the first admin token is in the Secrets Manager secret `colvenna-catalog-<env>/admin-token`. Clients reach the API only through the `execute-api` VPC endpoints the install lists, so from inside such a VPC: the admin API is under `<url>/colvenna/admin/v1/`, as in step 3 above, and an engine takes the URL as its `uri`, with the options of step 4. API Gateway terminates TLS with its own certificate, so no CA of this install is needed in the trust store.

The [deployment guide](docs/manual/install.md) covers both forms in full: prerequisites, identity and permissions, every configuration key, installing from source or from a release candidate, network and security, running again, upgrading, failures and uninstalling. The [user manual](docs/manual/user.md) covers connecting each engine, administering, backing up and restoring the metadata database, day-to-day operation, troubleshooting and the known limitations. Releases and the test environments are described in [docs/development.md](docs/development.md).

## Compatibility

The [compatibility matrix](docs/compat/matrix.md) is generated from runs of [`tests/compat/`](tests/compat/README.md), which anyone can repeat against their own catalog. A cell is an engine version with the Paimon client it runs, on a primary-key table without and with deletion vectors and on an append table. Before a run, every operation of every cell is declared either supported, which must pass, or a known limitation of the engine, with its reason. Three runs against the EKS form on 2026-10-28 found:

| Engine | Paimon client | Result |
| --- | --- | --- |
| Flink 1.20.5 | 2.0.0 and 2.1.0 | Every operation passes on all three tables: create and drop, write and commit, read, per-table credentials, and no request to an endpoint the catalog does not serve |
| Flink 2.2.1 | 2.1.0 | As above |
| Spark 3.5.9 | 2.0.0 and 2.1.0 | As above |
| Spark 4.1.3 | 2.1.0 | As above |
| Doris 4.1.5 | 1.4.2, bundled | Read-only in 1.0. Reads and per-table credentials pass, but on a deletion-vector table it does not read the rows still in uncompacted level-0 files, a limitation of Paimon 1.4.2 |
| StarRocks 4.1.6 | 1.3.1, bundled | Read-only in 1.0, with read-only S3 credentials of its own instead of per-table ones. Reads pass, with the same deletion-vector limitation. With short-lived credentials, as `v1.0.0-rc.2`'s UAT gave it, its first read of a primary-key table after its BE starts failed, a defect of the Hadoop it bundles, now declared a limitation ([user manual](docs/manual/user.md#92-engines), section 9.2) |

The serverless form ran the same cells on `v1.0.0-rc.2`, with the same outcomes ([the matrix](docs/compat/matrix.md)).

## Documentation

- [Deployment guide](docs/manual/install.md) and [user manual](docs/manual/user.md).
- [colvenna-catalog LLD](docs/design/colvenna-catalog-lld.md): how the server implements the Paimon REST contract, commits, authentication, grants, audit and per-table credentials, and its settings (in Chinese).
- [1.0 design](docs/design/v1.0-design.md): scope, the EKS and serverless forms, the compatibility matrix and the release acceptance criteria (in Chinese).
- [Press release & FAQ](docs/product/press-release.md): what 1.0 is meant to deliver (in Chinese).
- [Compatibility matrix](docs/compat/matrix.md), and how to run it: [`tests/compat/`](tests/compat/README.md).
- [The admin API](docs/manual/admin-api.md): principals, tokens, grants, the audit log and table repairs, with `curl`; its reference is the OpenAPI description [`docs/api/admin-v1.yaml`](docs/api/admin-v1.yaml).
- [Rotating the database secrets](docs/manual/database-secrets.md): the metadata database's master user and the catalog's database user, by hand.
- [Cost](docs/manual/cost.md): what each form costs fixed, idle and per request, what your database and network cost, with unit prices, formulas and the test installs' bill.
- [Catalog landscape research](docs/research/paimon-catalog-landscape.md) (as of 2026-10-23) and the [serverless spike](docs/research/serverless-spike.md) (in Chinese).
- [UAT cases](docs/uat/cases.md): the release's acceptance cases and the required case set each candidate runs, in both forms and two Regions; [the performance terms](docs/performance/terms.md): the performance scenarios and their targets.
- [Development](docs/development.md): build, CI, delivering a change, and [releasing](docs/development.md#releasing).
- [Contributing](CONTRIBUTING.md): how a change is delivered, and the rules every change follows; [Security](SECURITY.md): how to report a vulnerability, and the security model in brief.
- [Brand assets](assets/brand/README.md).
- [NOTICE](NOTICE) and [third-party notices](THIRD_PARTY_NOTICES.md).

## Building

```bash
mvn -B clean verify
```

This needs JDK 21 and Docker (the tests run PostgreSQL in Testcontainers); judge it by its `BUILD SUCCESS` line. [docs/development.md](docs/development.md) lists every CI job and how to run them locally.

## Naming

The product was called Cove Catalog, and its technical identifiers began with `cove`, until the rename; they are all `colvenna` now. The server came from Colvenna Lake, a lakehouse product of the same series, whose catalog it is; it keeps Colvenna Lake's HTTP contract (`/colvenna/admin/v1`, `colvenna_` tokens), settings (`COLVENNA_*`) and database migrations, so that Colvenna Lake can run this server's releases.

## License

[Apache License 2.0](LICENSE). Third-party components keep their own licenses: see [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Trademarks

Apache, Apache Paimon, and Paimon are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. Colvenna Catalog is not affiliated with or endorsed by The Apache Software Foundation.
