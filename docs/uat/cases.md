---
title: "Colvenna Catalog UAT cases"
date: 2026-10-28
status: draft
issue: 19
---

# UAT cases

These are the user acceptance test (UAT) cases of Colvenna Catalog, one of the seven release deliverables of the [1.0 design](../design/v1.0-design.md) (section 9.1). Each case has a stable ID, the requirement it answers, the configurations it applies to, its preconditions, input, steps, expected result and clean-up. Section 7 fixes the cases every release candidate must run before its UAT starts; the candidate's manifest pins this document's version and that list (`uat_cases`, `required_cases`; see [Releasing](../development.md#releasing)). Each candidate's results go to `docs/uat/reports/<candidate>/report.md` (#21).

The starting point is Colvenna Lake's UAT-CAT-01 to 12 (its `docs/uat/cases.md` at commit `50dae8755e758272a94d5b195e15e1a61713b569`, sections 5.1 to 5.3), which ran this server inside Colvenna Lake. Section 4 maps each of them to the cases here. Colvenna Lake removed its own catalog upgrade test (`run-upgrade.sh`) when it began to run this repository's release (colvenna-lake #295); its place is UAT-DEP-06.

Performance is not UAT. Its scenarios, method and targets are in [the performance terms](../performance/terms.md), and each candidate's results in `docs/performance/<candidate>.md` (#20).

## 1. Scope

UAT checks function only:

- **Functional.**
  - Every cell of the compatibility matrix (design section 7): UAT-CMP-01 to 08, one case per cell.
  - The catalog's own behaviour, through the engines and the HTTP API: table operations, per-table credentials (their scope and their renewal), authorization and audit, the admin API, tokens, health and metrics, and request size limits (UAT-CAT).
- **Security.** At every entry: refusal of the unauthenticated, of the authenticated but not permitted, and of any way around the intended entry. No Lambda function URL and no public or anonymous resource. Health endpoints not exposed (UAT-SEC, and the checks marked "(negative)" in other cases; section 6 lists them by entry).
- **Deployment and operations.** In both forms: install from nothing, into existing resources, run again, resume after an interruption, refuse wrong configurations, upgrade online, go back, uninstall keeping the data, uninstall deleting it, restore the metadata database from a snapshot, and the EKS form's internal NLB (UAT-DEP).
- **Fault recovery.** Atomic commits by concurrent writers under faults; consistent commit history after a replica or execution environment is lost; a table written outside the catalog fenced and released (UAT-FLT).

Every required case runs in the two standard Regions `us-west-2` and `us-east-2`, in each deployment form a candidate names (`configurations.forms` of its manifest). A case that applies to one form only says why. Data is synthetic only: each case makes its own databases, tables and identities, and removes them or says what it leaves.

A time bound in a case checks a function: that something happens within the time the design gives it (a revoked token refused within the authentication cache's lifetime, an install readable and writable within the 15 minutes the press release states). It proves nothing about performance, and no latency percentile is judged here.

## 2. Not in UAT

| What | Why |
| --- | --- |
| Throughput, request rates, latency percentiles, capacity, load, cold-start times, cost efficiency | The performance acceptance, judged separately against [its terms](../performance/terms.md) (design 9.2, "boundary"). Not counted in UAT's numbers or verdict |
| Unit tests, the upstream contract tests (`ColvennaCatalogContractTest`), the deployment scripts' offline tests (`tests/deploy`, including `test_helm_upgrade.py`), the CDK tests (cdk-nag, the forbidden-resource guard) | CI runs them on every commit (`scripts/ci-local.sh`); the release check cites the run on the candidate's commit |
| Colvenna Lake taking over its existing install in place | Verified in Colvenna Lake's switch issue (design section 3; #23), not in this repository's UAT |
| A rollback that keeps serving across a schema change | Before 1.0 versions do not promise compatibility with each other (design section 3). The paths 1.0 claims are UAT-DEP-06 and 07: the online upgrade; going back by a rollback when the schema is the same; and, when it moved forward, the previous version's refusal and the restore from the pre-upgrade snapshot |
| An Iceberg REST interface, several warehouses in one instance, role-based grants, clouds other than AWS | Not in 1.0 (design section 2 and 10) |
| Running in Colvenna Lake's environments (`uat`, `uat2`) or any other repository's | UAT runs only in this repository's own test environments (design 9.2, #7) |

## 3. Environments and execution

### 3.1 Environments

Each Region has its own set, made for the UAT window and taken down after it. Nothing is shared with another repository. The account is written `<ACCOUNT_ID>` here and in the report; real values live only in the execution host's git-ignored configuration (`deploy/config/<env>.env`).

| Role | Suggested name | Made by | Cases |
| --- | --- | --- | --- |
| Test environment | `uat<r>` | `tests/environment/up.sh uat<r> <region>`: a VPC with private nodes behind one NAT gateway, EKS 1.36 with NetworkPolicy enforced, the catalogs' two `t3.large` nodes and the clients' two `t3.xlarge` nodes, the AWS Load Balancer Controller, and the additions below | Hosts the EKS installs on the catalogs' nodes, and every client on the clients' nodes: engines, probes, test drivers and load run as pods in it |
| E1: EKS, new resources | `ek<r>` | UAT-DEP-01 (`COLVENNA_DB=create`, a new bucket) | UAT-CMP (EKS), UAT-CAT, UAT-SEC, UAT-DEP-03, 04, 08, 09, 10, UAT-FLT |
| E2: EKS, upgraded | `eu<r>` | The previous release, then UAT-DEP-06 | UAT-DEP-06, 07, then 08 and 09 |
| E3: EKS, existing resources | `ex<r>` | UAT-DEP-02 (`COLVENNA_DB=existing`, an existing bucket) | UAT-DEP-02, 11, then 09 |
| S1: serverless, new network | `sl<r>` | UAT-DEP-01 (`COLVENNA_NETWORK=create`, `COLVENNA_DB=create`) | UAT-CMP (serverless), UAT-CAT, UAT-SEC, UAT-DEP-03, 04, 08, 09, 10, UAT-FLT |
| S2: serverless, upgraded | `su<r>` | The previous release (see UAT-DEP-06), then UAT-DEP-06 | UAT-DEP-06, 07, then 08 and 09 |
| S3: serverless, existing network | `sx<r>` | UAT-DEP-02 (`COLVENNA_NETWORK=existing` in the test environment's VPC, `COLVENNA_DB=existing`) | UAT-DEP-02, then 09 |

`<r>` is `w2` for `us-west-2` and `e2` for `us-east-2`. E1, E2 and E3 share the test environment's cluster with names of their own (`COLVENNA_NAMESPACE`, `COLVENNA_RELEASE_NAME`, `COLVENNA_SERVICE_ACCOUNT`; E1 keeps the defaults) and client labels of their own (`COLVENNA_CLIENT_NAMESPACE_LABEL=colvenna-catalog/client-<env>=true`), so that a client namespace reaches only the install it is for. The serverless installs list the test environment's `execute-api` endpoint in `COLVENNA_API_ENDPOINT_IDS`, so that the pods reach their private APIs.

**What the test environment provides besides the cluster** (#52). `up.sh` makes the first three, its stack's outputs name them, and its last step (`tests/environment/verify.sh`) checks them; the fourth is a script:

- an `execute-api` interface endpoint with private DNS in its VPC (output `ApiEndpointId`), for the clients of the serverless installs, and as the endpoint of S3's existing network;
- an RDS PostgreSQL 16 instance in its VPC with IAM database authentication on and TLS only, its port open to the VPC, with an admin secret in Secrets Manager, and a bucket (outputs `ExistingDbEndpoint`, `ExistingDbPort`, `ExistingDbInstanceId`, `ExistingDbAdminSecretArn`, `ExistingDbSecurityGroupId`, `ExistingBucket`): the existing resources of E3 and S3, each with its own database, user and warehouse prefix (`COLVENNA_DB_NAME`, `COLVENNA_DB_USER`, `COLVENNA_WAREHOUSE_PREFIX`). `down.sh` deletes them with the environment, the database without a final snapshot;
- client subnets outside the cluster's subnets, routed to S3 and nowhere out of the VPC, with a security group for their clients (outputs `ClientSubnetIds`, `ClientSubnetCidrs`, `ClientSubnetZones`, `ClientSecurityGroupId`; the nodes' are `NodeSubnetCidrs`), for the internal NLB (UAT-DEP-11). Its client is a function, the test driver (`tests/serverless/driver`), which UAT-DEP-11's harness deploys into them and removes;
- for UAT-DEP-05, `tests/environment/network-policy.sh uat<r> <region> off` switches NetworkPolicy enforcement off (the VPC CNI add-on's `enableNetworkPolicy`), and `on` switches it back and runs `tests/environment/verify.sh`, which proves it is enforced again; `status` says which.

**The execution host.** It has the tools of `install.sh --help`'s preflight, Docker, Maven with JDK 21, Python 3, `curl`, `jq`, and a clean checkout of the candidate's tag. Every command runs with an explicit `AWS_PROFILE=<profile>`; nothing relies on a default profile. `tests/environment/up.sh` refuses to run while the host carries the `Patch Group` tag (a reboot at 00:00 UTC); for a run that crosses 00:00 UTC, remove the tag of the host and of the test nodes first, check again during the run, and restore the nodes' tags afterwards.

**What is installed.** The candidate, from a clean checkout of its tag: `install.sh --config deploy/config/<env>.env --release <candidate>`, with `COLVENNA_RELEASE_REGISTRY` in the configuration. Nothing is built. E2 and S2 start from the previous release (UAT-DEP-06).

**Nodes** (#65). The cluster has two node groups. The catalogs' two `t3.large` nodes, which the [performance terms](../performance/terms.md) name, hold only the EKS installs' pods and the per-node DaemonSets. The clients' nodes, two `t3.xlarge` tainted `colvenna-catalog/test-pool=clients:NoSchedule` so that no catalog pod lands there, hold everything else: every client, and the cluster's CoreDNS and AWS Load Balancer Controller. A client's pod selects them and tolerates the taint (the stack's outputs `ClientNodeSelector` and `ClientNodeTaint`): `tests/uat/uat.py` gives every pod it makes that placement, and `tests/compat/matrix.py` takes it as `--node-selector` and `--toleration`. Section 3.2 counts the catalogs' nodes' room.

**Clients.** Engines and probes are pods in the test environment's cluster, on its clients' nodes, in namespaces labelled for the install they test, as the users' are. They hold a catalog token and nothing else: no S3 keys, no IRSA role (their pod identity falls back to the node's, a clients' node's role, which the probes show is refused the tables' data). They reach an EKS install through its Service and a serverless install through the test environment's `execute-api` endpoint. They trust the JDK's `cacerts` plus the install's CA (EKS form; the serverless form presents an AWS certificate).

**Evidence.** Raw evidence goes to a private, access-controlled directory outside the repository, `<evidence>/<candidate>/<region>/<form>/<case>[-<n>]/`, and the report names where it is kept. Nothing in the repository holds a token, a password, an account ID, an ARN or an address.

### 3.2 Order

In each Region, in this order (cases in one step may run at once unless the case says it needs the environment to itself):

1. `tests/environment/up.sh uat<r> <region>`, then the additions of section 3.1.
2. UAT-DEP-01: E1 and S1.
3. UAT-DEP-05: refusals; nothing is made. It comes after E1 and S1 are made (#71): `flag-overrides-refusal` and `nothing-made` need E1, which it has to itself while NetworkPolicy enforcement is off, and `serverless-no-api-endpoint-in-the-vpc` gives S1's `execute-api` endpoint, the endpoint of another VPC than the test environment's. It also needs an eks-only candidate when the candidate names serverless.
4. UAT-CMP-01 to 08 on E1 and S1; UAT-CAT-01, 02, 04 to 08; UAT-SEC-01 to 04. The two forms' UAT-CMP cases run one after the other: all of step 4 at once would ask the clients' nodes for more memory than they have (below).
5. UAT-DEP-03, then UAT-DEP-04, on E1 and S1.
6. UAT-FLT-01, 02, 03 on E1 and S1, one at a time: each needs its install to itself.
7. UAT-DEP-10 on E1 and S1, one at a time.
8. UAT-CAT-03 (about 1 hour 45 minutes) on E1 and S1.
9. UAT-DEP-02: E3 and S3; UAT-DEP-11 on E3; then UAT-DEP-09 on E3 and S3, so that E3 is gone before E2 is made.
10. The previous release on E2 and S2, then UAT-DEP-06, then UAT-DEP-07.
11. UAT-DEP-08 on E1, S1, E2 and S2.
12. UAT-DEP-09 on E1, S1, E2 and S2.
13. `tests/environment/down.sh uat<r> <region>`.

**Room on the catalogs' nodes** (#65). A case that changes a running EKS install rolls its Deployment: the chart's rolling update starts one more catalog pod before it stops one (`maxSurge: 1`, `maxUnavailable: 0`), and that pod needs room on a catalogs' node. UAT-DEP-04, 06 and 07 roll an install, and so do the runs of `install.sh` that change a setting in UAT-CAT-03, UAT-DEP-10 and UAT-DEP-11, and `tests/environment/network-policy.sh` in UAT-DEP-05. Per catalogs' node, by CPU requests, as measured on a test environment of this layout (2026-10-29):

| Per catalogs' node | CPU |
| --- | --- |
| Allocatable (`t3.large`: 2 vCPU less what EKS reserves) | 1,930m |
| DaemonSets: `aws-node` (50m: the VPC CNI and its network policy agent), `kube-proxy` (100m), the account's GuardDuty runtime monitoring agent (200m) | 350m |
| Left for catalog pods, of 500m each | 1,580m: three pods |

The chart keeps one of an install's two pods on each node. With E1 and one other EKS install in the cluster, each node holds two catalog pods (1,000m), and a rolling update's extra pod makes three on one node (1,500m, within 1,580m). With three installs each node holds three (1,500m), and the extra pod would need 2,000m: it stays `Pending` (`Insufficient cpu`), as #65 found. So at most two EKS installs are in the cluster at a time, E1 and E3 or E1 and E2: step 9 uninstalls E3 before step 10 makes E2. CoreDNS (100m a replica) and the AWS Load Balancer Controller (50m) run on the clients' nodes: on the catalogs' nodes they would leave 1,430m, too little for even two installs to roll. Memory is not the limit: 6.9 GiB allocatable a node, 1 GiB a catalog pod. `tests/environment/verify.sh` records each catalogs' node's room (`catalog-nodes.txt`), and warns of a pod there that requests CPU and is neither an install's nor a DaemonSet's.

**Room on the clients' nodes.** Two `t3.xlarge`, 3,920m and 14.4 GiB allocatable each, of which the DaemonSets and the two controllers take 500m and 0.45 GiB: about 6.8 CPU and 28 GiB for clients. The heaviest step, UAT-CAT-03 on E1 and S1 at once, asks for about 2 CPU and 15 GiB. Step 4 run all at once in both forms would ask for more memory than there is (the matrix's Doris and StarRocks cells take 5 GiB each, UAT-CAT-01's Flink session cluster and Spark 8 GiB): run the two forms' UAT-CMP cases one after the other. A pod that finds no room waits, and its case fails on the wait; raise the clients' node group to four nodes for a window that needs more (`tests/environment/README.md`).

**Harness.** Each case says what runs it. What exists today: `install.sh` and `uninstall.sh` (their evidence is under `.state/<env>/evidence/`), `tests/environment/`, `tests/serverless/run.sh` (`cycle`, `refusals`, `cold`) with `verify-clean.sh`, and `tests/compat/matrix.py` (#10). The rest is `tests/uat/uat.py` ([its README](../../tests/uat/README.md)), written in #52 before #21: it runs every case of sections 5.2 to 5.5 (the matrix's, section 5.1, are `tests/compat/matrix.py`'s), ported from Colvenna Lake's acceptance flows (`tests/acceptance/catalog/` at the commit named above: `FlinkCatalogFlow`, `SparkCatalogFlow`, `CommitStress`, `MaintenanceLoop`, the renewal flows, the access and authorization checks) without Colvenna Lake's environment and fixtures. Every check a case lists is one line its harness writes to `verdicts.tsv` (time, check, `PASS` or `FAIL`, detail), and `uat.py` judges the run from them as section 3.3 says (`result.json`).

**Flink and colvenna-lake #297.** Paimon 2.0.0 and 2.1.0 lose data when Flink writes a bounded input in streaming mode with checkpoints at a parallelism above 1: the sink's last data of a subtask that finishes later is silently dropped (colvenna-lake #297, upstream, not the catalog). Every case here that writes a bounded input with Flink either runs in batch mode or writes at parallelism 1 in streaming mode, and says which. The cases that test catalog behaviour under load use the Paimon Java API, not Flink.

### 3.3 Results and verdicts

For each required case, each Region and each form it applies to, the report gives one of:

- `PASS`: the case's run exited 0 and every check it lists is there and `PASS` (a check marked "only on failure" may be absent);
- `FAIL`: a check is `FAIL`, or a check that should be there is not;
- `BLOCKED`: a preparation the case depends on (its preconditions) failed, so the case could not run or its result cannot be trusted; the report names the preparation;
- `NOT_RUN`: it did not run.

A required case is never dropped from the count, nor changed to "not applicable". A rerun after a fix is another run (`<case>-2`), on a new candidate when the fix changed the product; the report keeps the first run, its failure and the retest, and links each failure and block to its issue. A check marked "(negative)" expects a refusal: an explicit 401 or 403, an S3 `AccessDenied`, a refused or impossible connection, or the installer's refusal with its exit status; a timeout or an empty answer is never taken for a refusal unless the check says so.

## 4. Coverage

| Requirement | Cases |
| --- | --- |
| Compatibility matrix: every cell, every table type and operation (design 7) | UAT-CMP-01 to 08 |
| Table operations through the catalog: DDL, writes, reads, tags, branches, time travel, procedures, views, functions, rename, catalog-managed partitions, consumers | UAT-CAT-01 |
| Per-table credentials: scope (design 6.1, 6.2 constraint 1) | UAT-CAT-02 |
| Per-table credentials: renewal | UAT-CAT-03 |
| Authorization and audit | UAT-CAT-04 |
| Admin API (identities, tokens, grants, audit queries, consistency, reconcile, unregister) | UAT-CAT-05 |
| Token rotation, revocation, expiry and disabled principals | UAT-CAT-06 |
| Health, schema version and metrics | UAT-CAT-07 |
| Request and response size limits (design 6.2 constraint 2) | UAT-CAT-08 |
| Unauthenticated requests refused at every entry | UAT-SEC-01; UAT-DEP-11 (the internal NLB) |
| Authenticated but not permitted, refused at every entry | UAT-SEC-02; UAT-CAT-04 |
| No way around the intended entry; health endpoints not exposed | UAT-SEC-03 |
| No Lambda function URL; no public or anonymous resource | UAT-SEC-04 |
| Install from nothing, the CDK app's "new" mode (design 6.3, 6.4) | UAT-DEP-01 |
| Install into existing resources, the "existing" mode; 15 minutes to readable and writable (press release) | UAT-DEP-02 |
| Run again; resume after an interruption | UAT-DEP-03, 04 |
| Refuse wrong configurations, unsupported Regions, and what is not the candidate | UAT-DEP-05 |
| Upgrade online from the previous release (#19's comment) | UAT-DEP-06 |
| Go back to the previous release ("rollback", design 9.2): a rollback without interruption when the schema is the same, a refusal and the snapshot restore when it moved forward | UAT-DEP-07 |
| Uninstall keeping the data, and finding it again | UAT-DEP-08 |
| Uninstall deleting the data; nothing left | UAT-DEP-09 |
| Metadata database restored from a snapshot | UAT-DEP-10 |
| The internal NLB for clients outside the cluster (design 6.1) | UAT-DEP-11 |
| Atomic commits of concurrent writers under faults | UAT-FLT-01 |
| A replica or execution environment lost: commit history consistent | UAT-FLT-02 |
| A table written outside the catalog: fenced, then released | UAT-FLT-03 |

Colvenna Lake's catalog cases, and where each went:

| Colvenna Lake | Here | Changes |
| --- | --- | --- |
| UAT-CAT-01 Flink through the catalog | UAT-CAT-01; UAT-CMP-01 to 03 | The matrix covers every Flink cell; UAT-CAT-01 keeps the long flow, at Paimon 2.0.0 |
| UAT-CAT-02 Spark through the catalog | UAT-CAT-01; UAT-CMP-04 to 06 | As above |
| UAT-CAT-03 Doris, read only | UAT-CMP-07; UAT-CAT-03 | The official Doris 4.1.5 with its bundled Paimon 1.4.2, not Colvenna Lake's own build with a Paimon 2.0.0 reader: primary-key tables with deletion vectors are a declared limitation |
| UAT-CAT-04 Flink session cluster | UAT-CAT-01 | The TaskManagers fetch their own credentials; the streaming step writes at parallelism 1 (#297; Colvenna Lake lost 1,494 rows there at parallelism 2) |
| UAT-CAT-05 per-table credential scope | UAT-CAT-02 | Both forms: IRSA web identity (EKS), Cognito developer identity (serverless) |
| UAT-CAT-06 authorization and audit | UAT-CAT-04 | Both forms |
| UAT-CAT-07 tokens and network access | UAT-SEC-01, UAT-SEC-03, UAT-CAT-06 | Split by entry; the serverless form's gateway and authorizer added |
| UAT-CAT-08 health | UAT-CAT-07 | No Colvenna Lake operators; the serverless form's functions added |
| UAT-CAT-09 no endpoint the catalog lacks | Each UAT-CMP case's `endpoints` entries | Judged per cell, on the cell's own requests |
| UAT-CAT-10 atomic commits under faults | UAT-FLT-01 | Serverless faults and lost responses added |
| UAT-CAT-11 instance fault and metadata recovery | UAT-FLT-02 (part A); UAT-DEP-10 (part B) | The restore is from an RDS snapshot, as the user manual describes, not `pg_dump` |
| UAT-CAT-12 credential renewal | UAT-CAT-03 | Both forms |
| `run-upgrade.sh` (removed by colvenna-lake #295) | UAT-DEP-06, 07 | From a released candidate, online, both forms |

## 5. Cases

What every case assumes: the Region's test environment is up with the additions of section 3.1, the install the case names has passed UAT-DEP-01 or 02 (or, for E2 and S2, the start of UAT-DEP-06), and the host is set up as section 3.1 says. Below, `<run>` is the run's id (letters and digits, unique per run), `<env>` the install's name, `<evidence>` the case's evidence directory.

### 5.1 Compatibility matrix

#### What UAT-CMP-01 to 08 share

A cell is an engine version and the Paimon client it runs (`tests/compat/cells.json`), against a catalog in one deployment form; each case below is one cell, and its result in each form is that cell's result in that form (design section 7). The runner is `tests/compat/matrix.py`, described in `tests/compat/README.md` (#10).

**Requirement**: design section 7 and 9.2 ("functional: every cell of the compatibility matrix"); #10, #11.

**Configuration**: both forms (EKS on E1, serverless on S1); `us-west-2` and `us-east-2`.

**Preconditions**:

- `tests/compat/declared.tsv` at the candidate's commit declares the cell's fifteen entries (three table types: `pk`, `pk-dv`, `append`; five operations: `ddl`, `write`, `read`, `credentials`, `endpoints`) for the form, each `supported` or `limitation` with its reason. These declarations are part of the required set (section 7). A form without them cannot run its cells: `NOT_RUN`. Both forms are declared, each limitation an engine's own: the serverless form's rows (#52) before any serverless run.
- A writer token whose principal holds `MANAGE` on the catalog and a reader token with `READ`, issued for the run through the admin API, with their grants' purpose, approver, approval and expiry.
- The install's CA (EKS form).
- For the StarRocks cell, read-only S3 credentials of its own: short-lived credentials of a role that may only read the warehouse prefix, never long-lived keys.
- The catalog's access log for the run's window: `kubectl:<namespace>:app.kubernetes.io/name=colvenna-catalog` (EKS), or `command:` reading the server function's CloudWatch log group between `$COMPAT_SINCE` and `$COMPAT_UNTIL` (serverless).

**Input**: the matrix's synthetic dataset (`Dataset`: ids 1 to 120), in databases named `compat_<time>_<random>_*`.

**Steps**:

1. `tests/compat/matrix.py run --form <eks|serverless> --backend kubernetes --cells <cell> --uri <the catalog URI> --token-file <writer> --read-token-file <reader> [--ca-file <ca>] --s3-region <region> --server-log <as above> [--starrocks-s3-file <file>] --node-selector <ClientNodeSelector> --toleration <ClientNodeTaint> --out <evidence>`. The engines run in a namespace of the runner's own, with the install's client label, on the test environment's clients' nodes (its stack's outputs `ClientNodeSelector` and `ClientNodeTaint`; section 3.1), from their official images by digest, with no AWS identity of their own.
2. For a reader cell (Doris, StarRocks), the run first writes the three tables with its fixture writer (`fixtureWriter` in `cells.json`: Flink 1.20 with Paimon 2.1.0), and the reader only reads them.
3. The Flink flows run Flink's Table API in batch mode (`FlinkFlow`), so the bounded-input loss of colvenna-lake #297 does not arise.
4. After both Regions and both forms, `tests/compat/matrix.py page <evidence of each run>... --out docs/compat/matrix.md` makes the published matrix page from these runs, of the same candidate (design section 7).

**Expected**: the run exits 0, and in `<evidence>/<run id>/`:

| Check | Expected |
| --- | --- |
| `declared-supported-pass` | `cells/<cell>.json`: every entry declared `supported` for the form is `PASS`, with its evidence (the engine's requests to the catalog by endpoint and status, commits, credentials issued) |
| `limitations-recorded` | Every entry declared `limitation` carries its reason and what the run saw. Its outcome does not change the verdict; one that passed is named in the report, so that the next candidate's declaration can be revised |
| `credentials-only-the-catalogs` | (negative) For each `credentials` entry declared `supported`: the probe beside the engine finds the pod's own identity refused the table's files, and the access log shows the catalog issued the engine the table's credentials |
| `no-missing-endpoint` | (negative) For each `endpoints` entry: no request of the engine went to a route the catalog lacks, and no 501 but the partition listing it declines by design |
| `run-is-the-candidate` | `run.json` names the candidate's commit, and the SHA-256 of `cells.json` and `declared.tsv` are those at the candidate's commit |
| `nothing-left` | `summary.json`: the clean-up passed; no database of the run, no namespace of the runner left |

**Cleanup**: the run drops every database with its prefix and deletes its namespace, also when stopped. If the runner itself is killed, remove what is left as the matrix README says, and record it.

**Harness**: `tests/compat/matrix.py` (#10).

#### UAT-CMP-01 Flink 1.20 with Paimon 2.0.0

Cell `flink-1.20-paimon-2.0.0`: Flink 1.20.5, the official connector `paimon-flink-1.20-2.0.0`, `metastore=rest`. EKS and serverless declarations: all 15 entries supported.

#### UAT-CMP-02 Flink 1.20 with Paimon 2.1.0

Cell `flink-1.20-paimon-2.1.0`: Flink 1.20.5, `paimon-flink-1.20-2.1.0`. EKS and serverless declarations: all 15 entries supported. This cell is also the fixture writer of UAT-CMP-07 and 08.

#### UAT-CMP-03 Flink 2.2 with Paimon 2.1.0

Cell `flink-2.2-paimon-2.1.0`: Flink 2.2.1, `paimon-flink-2.2-2.1.0`. EKS and serverless declarations: all 15 entries supported.

#### UAT-CMP-04 Spark 3.5 with Paimon 2.0.0

Cell `spark-3.5-paimon-2.0.0`: Spark 3.5.9, `paimon-spark-3.5_2.12-2.0.0`. EKS and serverless declarations: all 15 entries supported.

#### UAT-CMP-05 Spark 3.5 with Paimon 2.1.0

Cell `spark-3.5-paimon-2.1.0`: Spark 3.5.9, `paimon-spark-3.5_2.12-2.1.0`. EKS and serverless declarations: all 15 entries supported.

#### UAT-CMP-06 Spark 4.1 with Paimon 2.1.0

Cell `spark-4.1-paimon-2.1.0`: Spark 4.1.3, `paimon-spark-4.1_2.13-2.1.0`. EKS and serverless declarations: all 15 entries supported.

#### UAT-CMP-07 Doris 4.1.5, read only

Cell `doris-4.1.5`: the official Doris 4.1.5 images with their bundled Paimon 1.4.2, `paimon.catalog.type=rest` with `paimon.rest.*` passed through. EKS declarations: 7 supported (read, credentials and endpoints of `pk` and `append`; endpoints of `pk-dv`) and 8 limitations: Doris is read-only in 1.0, and its Paimon 1.4.2 cannot read primary-key tables with deletion vectors (design section 7). Serverless declarations: the same, but `pk-dv` credentials supported (8 supported, 7 limitations), as the EKS runs measured them.

#### UAT-CMP-08 StarRocks 4.1.6, read only

Cell `starrocks-4.1.6`: the official StarRocks 4.1.6 image with its bundled Paimon 1.3.1, `paimon.catalog.type=rest` with `paimon.option.*` passed through. EKS and serverless declarations: 5 supported (read and endpoints of `pk` and `append`; endpoints of `pk-dv`) and 10 limitations: read-only in 1.0; no per-table credentials (it reads with read-only S3 credentials of its own, as the cell states); `pk-dv` reads declared as Doris's until a run shows otherwise (design section 7).

### 5.2 Catalog

#### UAT-CAT-01 Table operations through the catalog

**Requirement**: the LLD's contract, endpoint range, branches, tags and consumers ([LLD](../design/colvenna-catalog-lld.md)); Colvenna Lake's UAT-CAT-01, 02 and 04.

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: tokens for principals `uat-flink-<run>` and `uat-spark-<run>`, each with `MANAGE` on the case's database (granted after the database exists: first `MANAGE` on the name `flow_<run>`); the engines' images as the matrix pins them (Flink 1.20.5, Spark 3.5.9) with the Paimon 2.0.0 jars, the server's own Paimon version.

**Input**: database `flow_<run>`: primary-key table `orders` (100 rows inserted in batch, then upserts, an update and deletes), append table `events` with three partitions managed by the catalog (`metastore.partitioned-table=true`), Spark's own table `spark_sales` (4 rows).

**Steps**:

1. A Flink session cluster, one JobManager and two TaskManagers on the official image, standalone on Kubernetes. The flow (Colvenna Lake's `FlinkCatalogFlow`) is submitted at parallelism 2 for its batch steps. The TaskManagers fetch per-table credentials themselves (`data-token.enabled=true`; no S3 keys anywhere).
2. Its streaming step writes a bounded `datagen` input of 3,000 rows in streaming mode with checkpoints every 3 s, at parallelism 1: at a higher parallelism Paimon 2.0.0 may drop a subtask's last data (#297). The step checks that checkpointed writes through the catalog make several commits and lose no row; one commit holding several subtasks' data is not what it tests.
3. A Spark job (`local[4]`, Colvenna Lake's `SparkCatalogFlow`) reads what Flink wrote, makes its own table and writes into Flink's.
4. Afterwards, the access log of the window is checked for requests to routes the catalog lacks.

**Expected**: both flows exit 0, every step `OK`:

| Check | Expected |
| --- | --- |
| `create-database`, `create-primary-key-table`, `create-append-table` | Made through the catalog |
| `batch-insert` | 100 rows |
| `upsert-update-delete` | The rows after 2 upserts, 1 update and 5 deletes |
| `alter-add-column` | A column added through `alterTable`, readable |
| `streaming-append-commits` | 3,000 rows committed, by more than one commit, at parallelism 1 |
| `catalog-partitions` | The catalog lists the three partitions |
| `consumer-id-stream-read` | A streaming read with a consumer id resumes from its recorded progress |
| `tag-create-and-read`, `time-travel-by-snapshot` | The tag and snapshot 1 read as they were |
| `branch-write-and-read` | A branch made from the tag; its writes do not reach `main` |
| `compact-procedure`, `expire-snapshots` | A compaction commit of its own; 2 snapshots kept |
| `rollback-on-scratch-table` | A scratch table rolled back to snapshot 1 |
| `view`, `catalog-function` | A view; a function made, called, listed and dropped |
| `rename-and-drop` | Rename changes only metadata; drop removes the managed table's files (serverless: once the purge function has run, which the case invokes then, #69) |
| `spark-read-flink-tables`, `spark-time-travel`, `spark-read-branch`, `spark-system-tables` | Spark reads Flink's tables, by snapshot id and tag, the branch, and `$snapshots`, `$schemas`, `$tags`, `$files` |
| `spark-create-insert`, `spark-update-delete-merge`, `spark-alter`, `spark-procedures` | Spark's own table, row-level changes, MERGE, schema change, `create_tag` and compaction |
| `spark-writes-flink-table` | Spark writes into Flink's `orders`: one commit authority for both engines |
| `no-missing-endpoint` | (negative) No request to a route the catalog lacks; no 501 but the partition listing of tables whose partitions are in files |

**Cleanup**: the database is dropped through the catalog at the end, and the session cluster, the jobs and the principals' grants removed; the principals are disabled.

**Harness**: `tests/uat/uat.py run UAT-CAT-01` (#52): Colvenna Lake's `FlinkCatalogFlow` and `SparkCatalogFlow`, ported into `tests/uat/flows/`, on a standalone Flink session cluster and a Spark pod.

#### UAT-CAT-02 Per-table credential scope

**Requirement**: design 6.1 and 6.2 (constraint 1); LLD "按表数据凭证"; Colvenna Lake's UAT-CAT-05.

**Configuration**: both forms (E1, S1): the EKS form issues with the pod's IRSA web identity, the serverless form with a Cognito developer identity; both Regions.

**Preconditions**: database `cred_<run>` with tables `orders` and `orders_v2` (a name sharing `orders`' prefix) and database `cred_<run>_other` with table `secrets`, each with data; principals `cred-writer-<run>` (`WRITE` on `orders`), `cred-reader-<run>` (`READ` on `orders`), `cred-outsider-<run>` (no grant); the execution host may call S3 with the credentials it is given.

**Input**: the three principals' requests for `orders`' credentials; S3 calls made with those credentials only.

**Steps**:

1. Each principal requests `GET .../databases/cred_<run>/tables/orders/token`.
2. With each credential alone, call S3 on: the table's root key and keys under it; `orders_v2`; the bare prefix `.../orders` (which would list `orders_v2`); the database's directory; the other database; the whole bucket; a key outside the warehouse prefix.
3. Pods in a client namespace, with no IRSA role, try the same objects with their own identity.
4. Make a table whose `path` is a location outside the warehouse, and register a path outside it.
5. Read the issuing audit events.

**Expected**:

| Check | Expected |
| --- | --- |
| `writer-gets-read-write`, `reader-gets-read-only` | 200; credentials of the data role's session, valid for `COLVENNA_DATA_TOKEN_DURATION` (default 3 hours, within 1.5 to 12) |
| `outsider-gets-none` | (negative) 403 |
| `own-table-read-write-list-delete` | Writer: allowed under the table's root; a missing key is 404, not 403 |
| `reader-reads`, `reader-writes-consumer-progress` | Reader: reads allowed; it may write and remove its own `consumer/` progress |
| `reader-cannot-write-or-delete-data` | (negative) `AccessDenied` |
| `sibling-table`, `bare-prefix-list`, `database-directory-list`, `other-database`, `whole-bucket-list`, `outside-the-warehouse` | (negative) `AccessDenied` for every call |
| `pod-identity-refused` | (negative) The pods' own identity (the node's role) is refused reading and listing the tables' data |
| `issued-without-role-chaining` | The session is the data role's, named `colvenna-<principal>`; EKS: issued by `AssumeRoleWithWebIdentity` with the service account's token; serverless: with a Cognito developer identity's OpenID token, the identity's login being the session name |
| `client-location-refused` | (negative) Step 4's table: 400, "this catalog allows no client-chosen locations", and no table made. An install allows no client-chosen location (`COLVENNA_EXTERNAL_LOCATION_ROOTS` is not a setting of `install.sh` or the chart; [user manual](../manual/user.md), "No client-chosen locations"), so no external table exists whose credentials could be asked for (#63) |
| `register-outside-refused` | (negative) Step 4's registration: 400, only tables inside the warehouse can be registered, and no table made |
| `issuance-audited` | One `ISSUE_DATA_TOKEN` event per issuance (cache hits too), with session name, access key id, cache hit, level, prefix and expiry, and no secret |
| `no-s3-keys-in-clients` | (negative) No S3 key in any client's configuration, ConfigMap, Secret or pod |

**Cleanup**: the two databases are dropped, the three principals disabled and their grants deleted.

**Harness**: `tests/uat/uat.py run UAT-CAT-02` (#52), from Colvenna Lake's `verify-data-tokens.sh`; the tables are seeded through the catalog by `tests/uat/flows`' `Seed`, and the pods' own identity probed by its `Probe`. The refusal of an external table's credentials (400, "Data tokens cover managed tables only") is not checked live, since an install can make no external table: the server's unit tests cover it (`AwsStsTokenIssuerTest.externalAndNonS3TablesGetNoToken`), with the refusal of every client-chosen location by a server with no external roots, the install's (`NoExternalLocationsTest`), and of a registration outside the warehouse (`PathSafetyTest.registeringAPathThatOverlapsAnotherTableIsRejected`). CI runs them on every commit (section 2).

#### UAT-CAT-03 Per-table credential renewal

**Requirement**: design 6.2 (constraint 1: the client refreshes when less than an hour is left); LLD "按表数据凭证"; Colvenna Lake's UAT-CAT-12.

**Configuration**: both forms (E1, S1); both Regions. It needs the install to itself for about 1 hour 45 minutes.

**Preconditions**: the install run again with `COLVENNA_DATA_TOKEN_DURATION=PT1H30M`, the shortest the catalog allows (a run of `install.sh` that only changes that key); a primary-key table without deletion vectors and an append table, written once by the matrix's fixture writer in a database of its own, `renew_<run>_fixture` (Doris reads them); database `renew_<run>` with two empty append tables of its own, Flink's and Spark's, which only they write, so that Doris's second round reads what its first did (#52's harness made both explicit, and the implementer confirmed them on 2026-10-29); principals for Flink (`WRITE` on its table), Spark (`WRITE` on its table) and Doris (`READ` on the fixture's database).

**Input**: a Flink streaming write of 100 minutes; a Spark session and a Doris catalog that each read and write (Doris: read) once, wait 95 minutes, then do it again.

**Steps**:

1. The script checks that the install issues 90-minute credentials.
2. At once:
   - Flink: a session cluster (one JobManager, two TaskManagers) runs a streaming `INSERT` from a rate-limited, bounded `datagen` lasting 100 minutes into its append table, with checkpoints every 30 s and no restart allowed, at parallelism 1 (#297: the input is bounded). The TaskManagers fetch their own credentials.
   - Spark: one session reads and writes its table, sleeps 95 minutes, and reads and writes again, also through a table object it obtained before the wait.
   - Doris 4.1.5: the matrix's reader checks on both tables, then the same checks 95 minutes later, in the same catalog.
3. Read the `ISSUE_DATA_TOKEN` events of the window that were not cache hits.
4. Run `install.sh` again with the default duration.

**Expected**:

| Check | Expected |
| --- | --- |
| `flink-writes-across-expiry` | The Flink job runs the 100 minutes without a failure or restart, and the table holds exactly the rows the input made |
| `spark-after-the-wait` | Spark's reads and writes after the 95 minutes succeed |
| `doris-after-the-wait` | Doris's second checks give the first's answers |
| `renewed-before-expiry` | For a table Flink writes, a new (not cached) issuance to Flink's principal at least 15 minutes after an earlier one and before that one expired |
| `duration-restored` | After step 4 the install issues 3-hour credentials again |

**Cleanup**: the database is dropped, the session cluster removed, the principals disabled. If step 4 failed, run `install.sh` with the default duration by hand before any other case.

**Harness**: `tests/uat/uat.py run UAT-CAT-03 --reinstall '<install.sh source arguments>'` (#52), from Colvenna Lake's `run-renewal.sh` and its renewal flows; `--reinstall` (`--from-source`, or `--release <candidate>`) is step 4.

#### UAT-CAT-04 Authorization and audit

**Requirement**: LLD "授权" and "认证与审计"; design section 3 (security baseline); Colvenna Lake's UAT-CAT-06.

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: database `authz_<run>` with table `orders` (columns `id`, `amount`, `note`) and another table `ledger`.

**Input**: six principals, each with a 2-hour token: `owner` (`MANAGE` on the database), `writer` (`WRITE` on `orders`), `reader` (`READ` on `orders`), `columns` (`READ` on `orders`, columns `id` only), `denied` (`READ` on the database, `DENY READ` on `orders`), `outsider` (none); every request with its own `X-Request-Id`; commit and alter bodies `{}` (a 400 means the request passed authorization).

**Steps**:

1. From a client pod, each principal makes the requests below.
2. Find each request's audit event by its request id through `GET /colvenna/admin/v1/audit`.
3. Delete `reader`'s grant, wait the authentication cache's lifetime (`COLVENNA_AUTH_CACHE_TTL`, 2 s) plus 3 s, and ask again; make a grant that expires in 2 minutes and ask before and after.

**Expected**:

| Check | Expected |
| --- | --- |
| `list-databases/owner`, `list-databases/reader` | 200, listing `authz_<run>` |
| `list-databases/outsider` | (negative) 200 without `authz_<run>`; the `default` database is visible to every principal |
| `get-table/owner`, `get-table/writer`, `get-table/reader` | 200 |
| `get-table/denied`, `get-table/outsider` | (negative) 403: `DENY` wins over every `ALLOW` |
| `get-table-by-id/outsider` | (negative) 404, naming no table |
| `list-tables/reader` | 200, `orders` only; pages never name a table the caller cannot see |
| `list-tables/outsider` | (negative) 403 |
| `data-token/writer`, `data-token/reader` | 200 (read-write; read-only) |
| `data-token/denied`, `data-token/outsider` | (negative) 403 |
| `column-auth/allowed`, `column-auth/denied` | `/auth` for `id`: 200; for `id` and `amount`: (negative) 403 naming `amount` |
| `commit/owner`, `commit/writer`, `alter-table/owner` | 400: authorized |
| `commit/reader`, `commit/outsider`, `alter-table/writer`, `create-table/writer`, `drop-table/writer` | (negative) 403 |
| `create-drop-table/owner` | 200 for a scratch table |
| `grant-deleted-takes-effect` | (negative) After step 3's deletion, `reader` gets 403, and no new credentials |
| `grant-expiry-takes-effect` | 200 before the expiry, (negative) 403 after |
| `audit-<call>` | For each call above, the database listings included: the event with the request id, principal, action, resource, outcome (`SUCCEEDED`, `DENIED` or `FAILED`), status and time; denials carry the policy version and the level needed; the refused lookup by id is `DENIED` with its 404, under the table's id, naming no table |
| `audit-no-failure-as-success` | No event with a status of 400 or more recorded as `SUCCEEDED` |
| `audit-grants` | Each grant's creation and deletion recorded with all its fields |

**Cleanup**: the database is dropped, the six principals disabled, their grants deleted.

**Harness**: `tests/uat/uat.py run UAT-CAT-04` (#52), from Colvenna Lake's `verify-authorization.sh`.

#### UAT-CAT-05 Admin API

**Requirement**: design section 2 (the admin API is REST, with an OpenAPI description and `curl` examples; #15); design 9.1 (the user manual's examples can be reproduced).

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: the admin's token; the user manual's `curl` examples and the OpenAPI description at the candidate's commit.

**Input**: principal `adm-<run>`; its tokens and grants; a database and table of the case's own.

**Steps**: every operation of the admin API, once, as the manual's examples make them (in the serverless form from a pod, through the private API), then the invalid variants below.

**Expected**:

| Check | Expected |
| --- | --- |
| `principals-create-list` | `POST`, then `GET /colvenna/admin/v1/principals` lists it |
| `token-issue` | `POST .../principals/{name}/tokens`: 200 with a `colvenna_` token, shown once; without `ttl` it expires in 90 days |
| `token-ttl-too-long` | (negative) `ttl` over 400 days: 400 |
| `tokens-list` | `GET .../principals/{name}/tokens` lists ids, expiry and revocation, never a token or its hash |
| `token-revoke` | `DELETE /colvenna/admin/v1/tokens/{id}`: the token is refused afterwards (UAT-CAT-06 checks the timing) |
| `principal-disable` | `POST .../principals/{name}/disable`: every token of it refused |
| `grant-create-list-delete` | `POST`, `GET ?principal=` and `DELETE /colvenna/admin/v1/grants...` |
| `grant-needs-approval` | (negative) A grant without `purpose`, `approvedBy`, `approval` or `expiresAt`: 400, one by one |
| `audit-filters` | `GET /colvenna/admin/v1/audit` by `principal`, `action`, `resourceType`, `resource`, `outcome`, `since` and `until`: only matching events, newest first; `limit` over 1,000 gives at most 1,000 |
| `consistency` | `GET /colvenna/admin/v1/consistency/{db}/{table}` of a table at rest: consistent, no problem |
| `reconcile`, `unregister` | Exercised in UAT-FLT-03 and UAT-DEP-10; here each answers for a consistent table without change |
| `manual-examples` | Every `curl` example of the user manual runs as written, with placeholders filled in, and gives what the manual says |
| `openapi-matches` | Each response's status and body match the OpenAPI description's |
| `service-identity-refused` | (negative) Each operation with a service principal's token: 403 |

**Cleanup**: the database is dropped, the principal disabled, its grants deleted.

**Harness**: `tests/uat/uat.py run UAT-CAT-05` (#52; the manual's examples as #15 ran them, on this host in the EKS form, through the client pod in the serverless form).

#### UAT-CAT-06 Token rotation and revocation

**Requirement**: LLD "认证与审计" (revocation within `COLVENNA_AUTH_CACHE_TTL`; rotation order) and "无服务器形态" (the authorizer caches its decision for 60 s, the server checks again); Colvenna Lake's UAT-CAT-07.

**Configuration**: both forms (E1, S1): in the serverless form the gateway's cached decision is part of what is checked; both Regions.

**Preconditions**: principal `rot-<run>` with `READ` on a table of the case's own; a reader pod reading that table every 5 s with token A.

**Input**: tokens A and B (one day each), C (`ttl` one minute) of `rot-<run>`.

**Steps**:

1. Issue B, switch the reader to B (restart it), revoke A: the manual's rotation order.
2. From 1 s after the revocation, send requests with A every second for 90 s (EKS: enough of them that both replicas answer).
3. Wait until C expires, plus 10 s; use it.
4. Disable `rot-<run>`; use B for requests and for `/token`.

**Expected**:

| Check | Expected |
| --- | --- |
| `rotation-uninterrupted` | The reader's reads all succeed through step 1 |
| `revoked-refused-by-the-server` | (negative) Every request with A from 5 s after the revocation is 401. Serverless: until the authorizer's cached decision expires the 401 comes from the server (its access log has the request id); after 60 s from the gateway (the server is not invoked) |
| `expired-refused` | (negative) C: 401 |
| `disabled-refused` | (negative) B after the disable: 401 for the catalog API and for `/token` within 5 s |
| `issued-credentials-scope` | The S3 credentials B obtained before the disable still read until their expiry, as the manual states; recorded, not a defect |
| `refusals-audited` | The refusals appear in the audit (unauthenticated refusals are recorded up to 20 per second) |

**Cleanup**: the reader pod and the table are removed; the principal stays disabled.

**Harness**: `tests/uat/uat.py run UAT-CAT-06` (#52), from Colvenna Lake's `verify-catalog.sh`.

#### UAT-CAT-07 Health, schema version and metrics

**Requirement**: design 6.1 (two replicas, kept apart, PDB `minAvailable: 1`, no HPA) and 6.2 (the serverless form's functions); LLD "部署形态", "配置项"; Colvenna Lake's UAT-CAT-08.

**Configuration**: both forms (E1, S1), each with checks of its own: the forms run differently; both Regions.

**Preconditions**: none beyond the install.

**Input**: the running install; the number of migrations in the candidate (`src/main/resources/db/migration`).

**Steps**: read the install's state with `kubectl` or the Lambda API, the database's schema version with a read-only query, and the metrics port from a pod with the install's monitoring label.

**Expected**:

| Check | Expected |
| --- | --- |
| `eks-replicas` | (EKS) 2 ready replicas, on two nodes; a PodDisruptionBudget with `minAvailable: 1`; no HorizontalPodAutoscaler |
| `eks-readyz` | (EKS) `/readyz` on 127.0.0.1:8182 inside each pod: 200 |
| `eks-metrics` | (EKS) `/metrics` on port 9100 from a pod in a namespace with the monitoring label: `colvenna_catalog_requests_total`, `colvenna_catalog_request_seconds`, `colvenna_catalog_commits_total`, and nothing of the tables' data |
| `serverless-functions` | (serverless) The server function at `COLVENNA_LAMBDA_MEMORY` and `COLVENNA_LAMBDA_CONCURRENCY`, timeout 60 s, invoked by the API through its alias `live`, SnapStart as `COLVENNA_SNAPSTART` says, no provisioned concurrency; the authorizer, maintenance, purge and ops functions present |
| `serverless-no-secret-in-functions` | (serverless, negative) No function's configuration holds a variable named like a password or secret, a secret reference, or credentials |
| `serverless-logs` | (serverless) The server function's log group has the access log lines of the case's requests |
| `schema-version` | The database's schema version equals the candidate's number of migrations |

**Cleanup**: read only.

**Harness**: `tests/uat/uat.py run UAT-CAT-07` (#52).

#### UAT-CAT-08 Request and response size limits

**Requirement**: design 6.2 (constraint 2: a request over the limit gets a clear error and takes no partial effect); LLD "资源保护" and "无服务器形态"; the serverless spike, section 2.

**Configuration**: both forms (E1, S1), with each form's limits: 64 MiB for the EKS form's server, about 6 MiB for the serverless form's Lambda event and 4 MiB for its responses (`COLVENNA_MAX_RESPONSE_BYTES`); both Regions.

**Preconditions**: a database of the case's own with a partitioned table (one key) and 16 tables of many columns.

**Input**: generated request bodies: a commit of 41,000 partitions and one of 42,000 (serverless), a body over 64 MiB (EKS); list-table-details pages over 4 MiB in all, and one table whose details alone exceed 4 MiB (serverless).

**Steps**: send each; read the table's pointer, commit intents and snapshots before and after.

**Expected**:

| Check | Expected |
| --- | --- |
| `serverless-commit-under-the-limit` | (serverless) The 41,000-partition commit: 200, one snapshot more |
| `serverless-commit-over-the-limit` | (serverless, negative) The 42,000-partition commit: 413; pointer, intents and snapshots unchanged |
| `eks-body-over-the-limit` | (EKS, negative) A body over 64 MiB: 400 naming the limit; nothing changed |
| `serverless-pages-under-4-mib` | (serverless) The Paimon client lists every table's details, in pages that each stay under 4 MiB |
| `serverless-item-over-the-limit` | (serverless, negative) The one table too large for a page: 500 naming the table and the limit, recorded as a failed audit event; no empty page |

**Cleanup**: the database is dropped.

**Harness**: `tests/uat/uat.py run UAT-CAT-08` (#52), from the spike's driver (`spikes/serverless/functions/driver.py`) and `request-sizes/`, which #49 deleted with the spike's other files (they are in the history at `1b88bc8`); the driver's commit template is now `tests/uat/cases/data/commit-template.json`.

### 5.3 Security

#### UAT-SEC-01 Unauthenticated requests are refused at every entry

**Requirement**: design section 3 (every API needs authentication, no anonymous route; every entry tested with unauthenticated requests); LLD "认证与审计".

**Configuration**: both forms: the EKS form's Service (E1), the serverless form's private API (S1); the internal NLB in UAT-DEP-11; both Regions.

**Preconditions**: a principal with an expired token (`ttl` one minute, used 70 s later), a revoked one and a disabled principal's.

**Input**: for each entry, requests to `/v1/config`, a table's `GET`, a commit `POST`, the admin API's `GET /colvenna/admin/v1/principals` and an unknown route, with: no `Authorization`; a forged token; the expired, revoked and disabled tokens; `Basic`; an empty `Bearer`; a lowercase `bearer`.

**Steps**: send each from a client pod; EKS also plain HTTP to the TLS port.

**Expected**:

| Check | Expected |
| --- | --- |
| `<entry>-<route>-<credential>` | (negative) 401, every one, the unknown route too: no answer tells whether a route exists |
| `eks-plain-http` | (EKS, negative) Plain HTTP to port 8181 gets no HTTP response |
| `serverless-refused-before-the-server` | (serverless, negative) The gateway answers 401 and the server function is not invoked (no access log line for these requests) |
| `admin-control` | With the admin's token, the same `GET` requests answer 200: the refusals are not an outage |

**Cleanup**: the principals are disabled.

**Harness**: `tests/uat/uat.py run UAT-SEC-01` (#52; both forms, from Colvenna Lake's `verify-catalog.sh`); `tests/serverless/run.sh <env> <region> refusals` makes the serverless form's refusals too, from inside its own VPC.

#### UAT-SEC-02 Authenticated requests without permission are refused at every entry

**Requirement**: design section 3 (every entry tested with requests beyond the caller's rights); LLD "授权".

**Configuration**: both forms (E1, S1); both Regions. UAT-CAT-04 checks the full authorization model; this case makes the same few refusals at each entry.

**Preconditions**: principals `none-<run>` (no grant) and `read-<run>` (`READ` on one table).

**Input**: requests by each to the admin API, the table, its credentials and its commit.

**Steps**: send each from a client pod at each entry.

**Expected**:

| Check | Expected |
| --- | --- |
| `<entry>-admin-api-by-a-service-principal` | (negative) 403 |
| `<entry>-table-without-a-grant`, `<entry>-credentials-without-a-grant` | (negative) 403 |
| `<entry>-commit-with-read-only` | (negative) 403 |
| `<entry>-read-with-read` | 200: the refusals are the grants' |

**Cleanup**: the principals are disabled, their grants deleted.

**Harness**: `tests/uat/uat.py run UAT-SEC-02` (#52).

#### UAT-SEC-03 No way around the intended entry; health endpoints not exposed

**Requirement**: design 6.1 (a Service reached only from labelled namespaces; health only inside the pod; no public entry) and 6.2 (a private REST API reached only through the listed `execute-api` endpoints; invoked only by API Gateway); design section 3 ("绕过预期入口" refused); LLD "部署形态".

**Configuration**: both forms, each with its own entries (E1, S1); both Regions.

**Preconditions**: a namespace without the install's client label; a namespace with the monitoring label; for the serverless form, an `execute-api` endpoint in the test environment's VPC that is not listed in `COLVENNA_API_ENDPOINT_IDS` (made by the case for its run), and an IAM identity without rights on the install's functions.

**Input**: connection attempts and requests with a valid admin token, so that only the path is wrong.

**Steps**: try each path below.

**Expected**:

| Check | Expected |
| --- | --- |
| `eks-unlabelled-namespace` | (EKS, negative) No connection to the API port from a namespace without the client label (NetworkPolicy) |
| `eks-health-port` | (EKS, negative) Port 8182 from another pod, and on the pod's own address from inside it: no connection. On 127.0.0.1 inside the pod: 200 |
| `eks-metrics-port` | (EKS, negative) Port 9100 from a namespace without the monitoring label: no connection; with it: 200 |
| `eks-no-public-load-balancer` | (EKS, negative) No Service of type `LoadBalancer` but the internal NLB when it is on, and that one `internal` |
| `serverless-outside-the-vpc` | (serverless, negative) From the execution host, outside every VPC: the API's name does not resolve; the endpoint-specific name resolves to private addresses only and cannot be reached (`tests/serverless/outside.py`) |
| `serverless-unlisted-endpoint` | (serverless, negative) Through the unlisted endpoint: 403 from the resource policy |
| `serverless-direct-invoke` | (serverless, negative) `lambda:InvokeFunction` on the server and authorizer functions by the identity without rights: `AccessDenied`; the functions' resource policies admit only API Gateway, for this API |
| `serverless-database-only-from-the-functions` | (serverless, negative) The database's security group admits its port from the functions' security group only; a pod in the test environment cannot connect |

**Cleanup**: the case's namespaces and its unlisted endpoint are deleted.

**Harness**: `tests/uat/uat.py run UAT-SEC-03` (#52; EKS from Colvenna Lake's `verify-catalog.sh`; serverless with `tests/serverless/outside.py` for the outside check).

#### UAT-SEC-04 No Lambda function URL, no public or anonymous resource

**Requirement**: design section 2 (anti-goals: no anonymously reachable API or resource; no Lambda function URL, ever) and section 3; the CDK guard (design 6.3) checked on what was really deployed.

**Configuration**: both forms (E1, S1, and the release registry they install from); both Regions.

**Preconditions**: none beyond the installs.

**Input**: every stack of the installs and of the test environment, the account's functions in the Region, the buckets, repositories, secrets, database instances and identity pools of the installs.

**Steps**: list them with the AWS CLI; make unsigned (anonymous) requests where a public path would be.

**Expected**:

| Check | Expected |
| --- | --- |
| `no-function-url` | (negative) `lambda list-function-url-configs` gives none for any function in the account and Region, ours or not; no stack template has `AWS::Lambda::Url` |
| `rest-apis-private` | Every REST API of the installs is `PRIVATE`, with a resource policy that allows only from `aws:SourceVpce` and denies everything else; every method has `CUSTOM` authorization; no HTTP or WebSocket API |
| `no-internet-entry` | (negative) No internet-facing load balancer; no internet gateway, NAT gateway or Elastic IP in a product stack (the test environment's own NAT gateway is the test environment's) |
| `buckets-private` | All four Block Public Access settings on; the policy denies requests without TLS; unsigned list and get: 403 |
| `repositories-private` | No repository policy names `*` as principal; the release registry's names only its pull accounts, for pulls only |
| `secrets-private` | No secret has a resource policy open to `*` |
| `database-private` | Not publicly accessible; the serverless form's with IAM database authentication on |
| `identity-pool-closed` | (serverless) Unauthenticated identities and the classic flow off, the developer provider only, no roles; unsigned `GetId`: `NotAuthorizedException`; unsigned `GetOpenIdTokenForDeveloperIdentity`: `MissingAuthenticationTokenException` |

**Cleanup**: read only.

**Harness**: `tests/uat/uat.py run UAT-SEC-04` (#52).

### 5.4 Deployment and operations

#### UAT-DEP-01 Install from nothing

**Requirement**: design 6.3 (the CDK app's "new" mode) and 6.4 (`install.sh`); design 9.1 (the install script: from nothing, with evidence, no unrecorded manual step); the release manifest's install (`install.sh --release`).

**Configuration**: both forms: EKS into the test environment's cluster with `COLVENNA_DB=create` and a new bucket (E1); serverless with `COLVENNA_NETWORK=create` and `COLVENNA_DB=create` (S1), whose `--release` path has run live for the first time here; both Regions.

**Preconditions**: nothing of the install's name in the account (`install.sh` checks it); the candidate's tag checked out clean; the release registry holds the candidate, and the account is one of its pull accounts.

**Input**: the install's configuration; the candidate.

**Steps**:

1. `AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release <candidate>`, answering the confirmation with the environment's name (not `--yes`, so that the summary is seen before anything changes).
2. A Paimon 2.0.0 client in a client pod makes a database and a table, commits and reads back, with per-table credentials (serverless: `tests/serverless/run.sh <env> <region> cycle`).

**Expected**:

| Check | Expected |
| --- | --- |
| `install-exit` | 0 |
| `summary-before-change` | The summary (account, Region, cluster or network, version `release <candidate> (built from <B>...)`, bucket, database) is printed and confirmed before the first stack or Kubernetes object changes |
| `release-checks-first` | The release checks (tree, manifest, registry digests, labels, form) pass before any check of the cluster or network |
| `every-step-done` | Every step announced, none failed; the smoke check passed |
| `installed-from-nothing` | Every stack of the install was created after the run began |
| `runs-the-candidate` | The running image is the manifest's digest, copied by digest into the install's own repository and tagged `<candidate>`; EKS: the chart is the manifest's, pulled by digest, and the pods carry `colvenna-catalog/release=<candidate>` and `colvenna-catalog/source-sha=<B>`; stacks are tagged `SourceSha=<B>` |
| `nothing-built` | No Maven or Docker build ran |
| `admin-token-not-printed` | (negative) No token appears in the output or the evidence; the admin's token is in its Kubernetes Secret (EKS) or Secrets Manager (`colvenna-catalog-<env>/admin-token`, serverless), and works |
| `readable-writable` | Step 2 passes |

The time of each step is recorded, not judged.

**Cleanup**: the install stays for the cases after it; UAT-DEP-09 removes it.

**Harness**: `tests/uat/uat.py run UAT-DEP-01` (#52): it runs `install.sh --release <candidate>`, answering the confirmation on a terminal, and judges its output, its evidence, the account and the cluster; the client's cycle is the test driver's (`tests/serverless/driver`) in a client pod (EKS), or `tests/serverless/run.sh <env> <region> cycle` (serverless).

#### UAT-DEP-02 Install into existing resources

**Requirement**: design 6.3 (the "existing" mode) and section 2, goal 3 (in an account with an EKS cluster and PostgreSQL, one command and 15 minutes to readable and writable: a target the requester accepted, judged here, as the requester decided on 2026-10-29; the press release states it); [What is not proven yet](../development.md#what-is-not-proven-yet) (`COLVENNA_NETWORK=existing` had run offline only).

**Configuration**: both forms: EKS into the test environment's cluster with `COLVENNA_DB=existing` (the test environment's PostgreSQL) and an existing bucket (E3); serverless with `COLVENNA_NETWORK=existing` (the test environment's VPC, two of its private subnets, its endpoints, its `execute-api` endpoint in `COLVENNA_API_ENDPOINT_IDS`), `COLVENNA_DB=existing` (IAM database authentication on) and the existing bucket (S3); both Regions.

**Preconditions**: the existing resources of section 3.1; a record of them before the install.

**Input**: the install's configuration; the candidate.

**Steps**: as UAT-DEP-01, with `--yes`; then record the existing resources again.

**Expected**:

| Check | Expected |
| --- | --- |
| `install-exit`, `every-step-done`, `runs-the-candidate`, `nothing-built`, `admin-token-not-printed`, `readable-writable` | As UAT-DEP-01 |
| `existing-untouched` | The VPC, subnets, route tables, endpoints, the database instance (its parameters, security groups and settings) and the bucket's configuration and policy are as recorded before; the install made only its own database, users, prefix and stacks |
| `eks-ready-within-15-minutes` | (EKS) From the command's start to the client's first successful write and read: at most 15 minutes. Serverless: recorded only (the claim is the EKS form's) |

**Cleanup**: the install stays for UAT-DEP-11 (E3); UAT-DEP-09 removes it, keeping the existing resources.

**Harness**: `tests/uat/uat.py run UAT-DEP-02` (#52): as UAT-DEP-01's, with the existing resources recorded before and after, and what is in them (the instance's databases and roles, read by psql as its admin; the bucket's top-level prefixes); the stacks the run deployed are those its CDK log names.

#### UAT-DEP-03 Run again

**Requirement**: design 6.4 (repeated runs are idempotent).

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: the install at the candidate; no other case running on it.

**Input**: the same configuration and candidate.

**Steps**: record the install (stacks and each resource's physical id, the Helm release and its values, pods and their UIDs, the functions' versions and alias, the repository's images, the admin token secret's version, the grants and principals); run `install.sh --yes` again; record again.

**Expected**:

| Check | Expected |
| --- | --- |
| `install-exit`, `every-step-done` | 0; every step done |
| `nothing-made-or-replaced` | The same stacks and physical ids; no resource made or replaced |
| `no-restart` | EKS: the same pods (UIDs); serverless: the alias `live` on the same version |
| `no-image-copied-or-built` | The install's repository holds the same images |
| `admin-token-kept` | The admin token is not issued again |
| `data-and-grants-kept` | Tables, grants and principals as recorded |

**Cleanup**: none.

**Harness**: `tests/uat/uat.py run UAT-DEP-03` (#52).

#### UAT-DEP-04 Resume after an interruption

**Requirement**: design 6.4 (an interrupted run can be run again to recover); `install.sh`'s header.

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: the install at the candidate; no other case running on it.

**Input**: a change that makes the step take time: a new `COLVENNA_DATA_TOKEN_DURATION` (rolls the EKS pods; updates the serverless functions); the signal `SIGHUP` (the host's session lost), and in a second run `SIGKILL` (the host lost).

**Steps**:

1. Run `install.sh --yes` with the change, in a session of its own.
2. Interrupt it: EKS while the Helm release is `pending-upgrade` (step `chart`); serverless while the serverless stack's update is in progress (step `functions`). If the step ends before the interruption, the check `interrupted` fails and the case runs again; it never passes that way.
3. Run `install.sh --yes` again with the same configuration.
4. Repeat steps 1 to 3 with `SIGKILL`, changing the duration back.

**Expected**:

| Check | Expected |
| --- | --- |
| `interrupted` | The interrupted run exits non-zero in the step named; the state it left is recorded (Helm status, stack status) |
| `resumed` | The next run exits 0 and every step is done; a Helm release this workspace left pending is recovered, a stack update in progress is waited for |
| `converged` | The install has the changed setting; no duplicate resources; the smoke check passes |

**Cleanup**: the install is left at the default duration.

**Harness**: `tests/uat/uat.py run UAT-DEP-04` (#52): each run of `install.sh` in a process group of its own, the signal sent to the whole group once the step's change is under way (the Helm release's status, the stack's).

#### UAT-DEP-05 Refused configurations

**Requirement**: design 6.4 (input validated; unsupported Regions, China and GovCloud, insufficient capability and mismatched resources refused); design 6.1 (NetworkPolicy not enforced: refused unless `--allow-unenforced-network-policy`); `install.sh --release`'s checks.

**Configuration**: both forms, each with its own refusals; both Regions. Every refusal runs with a name of the case's own, which has nothing in the account, except where it says otherwise.

**Preconditions**: the test environment; E1 (EKS: `flag-overrides-refusal` runs on it, and `nothing-made` checks it), and nothing else running on it; an eks-only candidate when the candidate names serverless (`release-form-not-accepted`); an `execute-api` endpoint of another VPC than the test environment's (serverless: `serverless-no-api-endpoint-in-the-vpc`), which S1's is. Section 3.2 runs the case after UAT-DEP-01 for these (#71).

**Input**: configurations wrong in one key each; checkouts that are not the candidate's.

**Steps**: run each; record its exit status and message; afterwards, list the account.

**Expected**: each refusal exits with the status `install.sh` documents (2 for arguments and configuration, 3 for preflight) and its message, before changing anything:

| Check | Expected |
| --- | --- |
| `args-*` | (negative) No `--config`; an unknown option; both `--from-source` and `--release`: 2 |
| `config-unknown-key`, `config-other-forms-key`, `config-env-name` | (negative) An unknown `COLVENNA_` key; a key of the other form set; a malformed name: 2 |
| `region-missing`, `region-china`, `region-china-northwest`, `region-govcloud`, `region-malformed` | (negative) No Region, `cn-north-1`, `cn-northwest-1`, `us-gov-west-1`, `uswest2`: 2 |
| `account-not-the-callers` | (negative) `COLVENNA_ACCOUNT` not the credentials' account: 3 |
| `not-confirmed` | (negative) No `--yes` and no terminal: 2 |
| `eks-no-cluster`, `eks-no-matching-node`, `eks-nlb-cidr-holds-pods`, `eks-bucket-other-region`, `eks-db-not-rds`, `eks-db-reader-endpoint`, `eks-db-admin-secret-unreadable` | (EKS, negative) 3 each |
| `eks-network-policy-not-enforced` | (EKS, negative) With enforcement switched off in the test cluster: 3, saying how to turn it on; afterwards enforcement is on again (`tests/environment/verify.sh`) |
| `flag-overrides-refusal` | (EKS) On E1 while enforcement is off, `--allow-unenforced-network-policy` passes preflight, and the run changes nothing (as UAT-DEP-03) |
| `serverless-one-zone`, `serverless-no-api-endpoint-in-the-vpc`, `serverless-vpc-lacks-endpoints`, `serverless-db-without-iam-auth`, `serverless-concurrency-too-high` | (serverless, negative) 3 each |
| `release-no-registry` | (negative) `--release` without `COLVENNA_RELEASE_REGISTRY`: 2 |
| `release-not-the-tag`, `release-dirty-tree`, `release-differs-from-b` | (negative) A checkout of `main`; uncommitted changes; a commit on top of the tag: 3 |
| `release-registry-lacks-it` | (negative) A registry that does not hold the candidate's digests (the same account's registry address in another Region, which has no release repositories): 3 |
| `release-form-not-accepted` | (negative) A serverless install of a candidate whose manifest does not name `serverless`: 3. The candidate itself when it names only `eks`; otherwise a trial candidate (`v0.0.0-rc.<n>`, made from the candidate's commit with `RELEASE_FORMS=eks` and removed afterwards, as [Trial candidates](../development.md#trial-candidates) says) |
| `uninstall-not-confirmed`, `uninstall-other-account` | (negative) `uninstall.sh` without `--yes` and no terminal: 2; another account: 3 |
| `nothing-made` | No stack, role, repository, function, secret, log group or bucket of the case's names in the Region; E1 and the test environment unchanged |

Some refusals need a change to the account (no CloudWatch Logs role for API Gateway, no CDK bootstrap, VPC or Elastic IP quota full), or a database set up wrong (publicly accessible, or not requiring TLS: `rds.force_ssl=0`): they are covered by the offline tests in `tests/deploy` and not repeated live. Those that need an earlier install's stacks (`COLVENNA_DB=existing` without `COLVENNA_BUCKET` while the `Storage` stack holds a database, `COLVENNA_DB=create` on an install moved to a restored instance, `uninstall.sh --delete-data` while that instance is there) are UAT-DEP-10's `repoint-guarded` and UAT-DEP-09's `restored-instance-refused`.

**Cleanup**: NetworkPolicy enforcement back on and verified; the case's checkouts removed.

**Harness**: `tests/uat/uat.py run UAT-DEP-05` (#52), from Colvenna Lake's `run-refusals.sh` and `run-release-refusals.sh`: what a refusal needs that the test environment lacks (an Aurora PostgreSQL cluster without an instance or IAM database authentication, a bucket in another Region, the checkouts that are not the candidate's) it makes and removes; an eks-only candidate, when the candidate names serverless, is given with `--eks-only-release`. E1 (EKS), the eks-only candidate and an `execute-api` endpoint of another VPC (serverless) are preconditions: without them the case is `BLOCKED`. The endpoint is S1's own, which `COLVENNA_NETWORK=create` made in its VPC (its `Network` stack's output `ExecuteApiEndpointId`; the serverless run is on S1); without it the case is `BLOCKED`, never given another endpoint of the Region, which could be another repository's environment's. The test environment holds no second VPC for it: S1 exists by then, its endpoint is exactly the misconfiguration the check needs (an available `execute-api` endpoint in another VPC), and a second VPC per environment would take quota the Regions have little of. `tests/environment/network-policy.sh` starts every install's catalog pods again after each switch, since the agent's eBPF programs on running pods outlive it (#70), and `on` waits until enforcement applies to pods made after the switch; the case checks that E1's pods were started again before the flagged run.

#### UAT-DEP-06 Online upgrade from the previous release

**Requirement**: #19's requirement (from the previous release to the candidate, reads, writes and tokens uninterrupted, the migration chain only moving forward, data and grants unchanged, in both forms); design section 3 ("the upgrade paths 1.0 claims are verified"); LLD "连接池" (an upgrade migrates before the server takes the new image).

**Configuration**: both forms (E2, S2); both Regions.

**Preconditions**: the install at the previous release, made with that release's own tag and `install.sh --release`, and in use: the databases, tables, principals, tokens and grants of step 1.

- The previous release is the newest earlier candidate whose manifest names the form: for the first candidate after `v1.0.0-rc.1`, that is `v1.0.0-rc.1` for the EKS form.
- `v1.0.0-rc.1` has no serverless form. On the first candidate that names it, S2 starts from `install.sh --from-source` at `cb38d08792909899b6e013e3c22e381a17367858` (the merge of #13), and the report says so. Every later candidate upgrades from the candidate before it.

**Input**: database `upg_<run>` with a primary-key table and an append table; three principals (writer, reader, and one only used after the upgrade) with tokens issued before the upgrade; their grants.

**Steps**:

1. Record: the database's `colvenna_schema_version` rows, every table's snapshots and their contents (row count, checksum), the principals, tokens (ids, expiry, revocation) and grants (ids and contents), and the audit's latest event id.
2. Take a manual snapshot of the metadata database, as the deployment guide says to before an upgrade. UAT-DEP-07 restores it if the schema moved forward.
3. Start, from client pods, and keep running through the upgrade:
   - a writer committing to the append table every 2 s (Paimon Java API, not Flink);
   - a reader loading each table and reading its latest snapshot every second;
   - a credential loop requesting `/token` for each table every 5 s and reading one data file with what it gets;
   - all with the tokens issued before the upgrade.
4. Upgrade as the deployment guide says: from a clean checkout of the candidate's tag, `install.sh --yes --release <candidate>` with the install's configuration (keys the candidate added left at their defaults).
5. After it exits, keep the loops running for 5 minutes, then stop them; issue a new token to the third principal and use it.
6. Record as in step 1.

**Expected**:

| Check | Expected |
| --- | --- |
| `install-exit`, `every-step-done`, `runs-the-candidate` | As UAT-DEP-01, with the previous version replaced: EKS by a rolling update; serverless with the maintenance function on the new image and `migrate` before the server function, and the alias `live` moved once |
| `writes-uninterrupted` | Every commit the writer made in the window succeeded (after the Paimon client's own retries); no gap of more than 60 s between two successful commits; each commit exactly once (snapshot ids contiguous). The pods replaced are the previous release's, and stop as it does: one before #64 stops at once, which can fail a request (deployment guide, section 6.3) |
| `reads-uninterrupted` | Every read succeeded and saw a snapshot no older than the one before; no gap of more than 60 s |
| `tokens-uninterrupted` | The tokens issued before the upgrade worked throughout and after; every `/token` request succeeded and its credentials read |
| `migration-chain-forward` | The schema version rows before are all still there, unchanged; any added have higher versions; the version equals the candidate's number of migrations |
| `data-unchanged` | Every table's snapshots from before have the same contents; the only new snapshots are the writer's |
| `grants-and-identities-unchanged` | Principals, tokens and grants as recorded, plus the new token |
| `audit-continuous` | The audit events from before are still there; events of the window recorded |
| `new-token-works` | The token issued after the upgrade works |

**Cleanup**: the install stays at the candidate for UAT-DEP-07.

**Harness**: `tests/uat/uat.py run UAT-DEP-06 ... --candidate <the candidate's checkout>` (#52): the loops are `tests/uat/paimon`'s; the pre-upgrade snapshot is kept, tagged, for UAT-DEP-07.

#### UAT-DEP-07 Going back to the previous release

**Requirement**: design 9.2 ("upgrade and rollback"), within design section 3 (no compatibility promised across versions before 1.0; the upgrade and recovery paths 1.0 claims are verified). The requester fixed the terms on 2026-10-29:

- when the schema has not changed between the two, going back is a rollback (`helm rollback` in the EKS form), and reads and writes go on uninterrupted;
- when the candidate's schema has moved forward, the previous version must refuse to start and write nothing; the way back is the restore from a snapshot (UAT-DEP-10's procedure), and an interruption is allowed.

**Configuration**: both forms (E2, S2, after UAT-DEP-06); both Regions.

**Preconditions**: the install at the candidate, upgraded from the previous release by UAT-DEP-06, with the snapshot of the metadata database taken before that upgrade (UAT-DEP-06's step 2).

**Input**: the previous release (as UAT-DEP-06 defines it); the loops of UAT-DEP-06's step 3; the pre-upgrade snapshot.

**Steps**: which branch runs depends on whether the candidate added migrations to the previous release's (the report says which).

1. Record as UAT-DEP-06's step 1; start the loops.
2. The same schema:
   - EKS: `helm rollback` of the release to the previous release's revision, as the deployment guide says.
   - Serverless: the previous release's `install.sh` from a clean checkout of it (its tag; for S2 without one, its commit), with the install's configuration as that release takes it (keys it does not know left out). It moves the alias `live` back to the previous image, and its `migrate` changes nothing.
3. The schema moved forward:
   - Go back as in step 2, and see the previous version refuse.
   - Then restore the pre-upgrade snapshot as the user manual says (UAT-DEP-10's procedure), and install the previous release on it, its configuration pointed at the restored instance (`COLVENNA_DB=existing`; S2 keeps `COLVENNA_NETWORK=create`).
   - In the serverless form, that needs a previous version whose `install.sh` points an install with a new network at a restored instance: the merge of #59 and later. One before it refuses `COLVENNA_DB=existing` with `COLVENNA_NETWORK=create`, so when this branch applies, S2 starts UAT-DEP-06 from the merge of #59 or a later candidate, not from an earlier commit, and the report says so.
4. Stop the loops 5 minutes later and record. Then upgrade to the candidate again, as UAT-DEP-06's step 4.

**Expected**: for the branch that ran, its checks; then `forward-again`:

| Check | Expected |
| --- | --- |
| `same-schema-rolled-back` | (Same schema) The previous version serves: EKS: every pod runs the previous release's image after the rollback; serverless: the alias `live` on a version of the previous image |
| `same-schema-uninterrupted` | (Same schema) UAT-DEP-06's `writes-uninterrupted`, `reads-uninterrupted` and `tokens-uninterrupted` hold through the rollback |
| `same-schema-data-kept` | (Same schema) Data, grants and identities as recorded, plus the loops' commits |
| `newer-schema-refused` | (Schema moved forward, negative) The previous version refuses to start against the newer schema, naming the versions: EKS: its pods never become ready; serverless: its `migrate` refuses, before the server function changes |
| `newer-schema-nothing-written` | (Schema moved forward) The schema version rows, every table's pointer and the commit intents are unchanged by the refused version |
| `newer-schema-restored` | (Schema moved forward) After the restore, the previous release serves; tables, grants and identities are as at the snapshot, consistent once reconciled (as UAT-DEP-10); the interruption's length is recorded, not judged |
| `forward-again` | The upgrade back to the candidate succeeds, and UAT-DEP-06's checks hold for it |

**Cleanup**: the install is left at the candidate; the pre-upgrade snapshot is deleted.

**Harness**: `tests/uat/uat.py run UAT-DEP-07 ... --candidate <checkout> --previous <checkout>` (#52); which branch runs follows from the two checkouts' migrations. With the schema moved forward it leaves the install on the restored instance, its configuration so changed written beside the original as `<env>-restored.env`, as UAT-DEP-10 does; `newer-schema-restored` compares with UAT-DEP-06's record from just before its snapshot, found beside this run's evidence.

#### UAT-DEP-08 Uninstall keeping the data, and install again

**Requirement**: design 6.4 (uninstall removes only this environment's resources and keeps the database and the S3 data by default); `uninstall.sh`'s header (a later install with `COLVENNA_BUCKET` and `COLVENNA_DB=existing` finds every table again).

**Configuration**: both forms (E1, S1, E2, S2); both Regions.

**Preconditions**: the case's own tables with known contents, a principal with a token and grants; no other case running on the install.

**Input**: the install; its configuration.

**Steps**:

1. Record the account in the Region: every stack (status, last update), bucket, database instance, secret, role, function, repository, log group and VPC endpoint, of every install and of the release registry.
2. `uninstall.sh --config deploy/config/<env>.env --yes`; then once more.
3. Record again.
4. Install the candidate again with the kept bucket (`COLVENNA_BUCKET`) and database (`COLVENNA_DB=existing`), as the deployment guide says.

**Expected**:

| Check | Expected |
| --- | --- |
| `uninstall-exit`, `uninstall-again-exit` | 0 both times; the second removes nothing more |
| `removed` | EKS: the internal NLB, the Helm release, the catalog's Secrets but its CA, the EKS stack; serverless: the serverless and images stacks, and the network stack unless the kept database is in it |
| `kept` | The storage stack, its bucket and database; EKS: the CA and the namespace |
| `others-untouched` | Every other install's resources, the test environment and the release registry as recorded |
| `reinstall-finds-everything` | After step 4: every table reads as before, its snapshots too; principals, grants and the token from before work |

**Cleanup**: the install is in place again, for UAT-DEP-09.

**Harness**: `tests/uat/uat.py run UAT-DEP-08` (#52): it writes the reinstall's keys into the install's configuration, keeping the old one in its evidence.

#### UAT-DEP-09 Uninstall deleting the data

**Requirement**: design 6.4 and 9.1 (uninstall and what it keeps); `uninstall.sh --delete-data`.

**Configuration**: both forms, every install of the Region (E1, E2, E3, S1, S2, S3): the last case of each; both Regions.

**Preconditions**: the install's evidence is saved; nothing else runs on it. E1 and S1 run on the instance UAT-DEP-10 restored, which the install did not make.

**Input**: the install; its configuration.

**Steps**:

1. E1 and S1: `uninstall.sh --yes --delete-data` while the restored instance is there; then delete it and its log group, as the deployment guide says (section 8).
2. `uninstall.sh --yes --delete-data` without typing the name; then typing it.
3. Check the account (serverless: `tests/serverless/verify-clean.sh <env> <region>`, with the driver taken down first by `tests/serverless/down.sh`).

**Expected**:

| Check | Expected |
| --- | --- |
| `restored-instance-refused` | (E1 and S1, negative) Step 1: 3, naming the restored instance, before the name is asked for; nothing changed |
| `delete-data-needs-the-name` | (negative) Without the typed name: 2, nothing deleted, even with `--yes` |
| `uninstall-exit` | 0 with the name |
| `nothing-of-it-left` | No stack, function, role, repository, bucket, database instance (and no final snapshot), secret, log group, identity pool, VPC endpoint or network interface of the install |
| `existing-kept` | E3 and S3: the existing database instance and bucket are untouched, with the catalog's database, users and prefix still in them: `--delete-data` deletes only what `install.sh` made, as `uninstall.sh` says. The case then drops them, as the deployment guide tells a user to (section 8: the catalog's role granted to the admin first, #73), with the guide's statements, which must be what `uninstall.sh` printed |
| `others-untouched` | As UAT-DEP-08 |

**Cleanup**: this is the clean-up.

**Harness**: `tests/uat/uat.py run UAT-DEP-09` (#52): an install whose configuration is the one the user manual's restore leaves (`COLVENNA_DB=existing` with the bucket the install made) runs on the restored instance, step 1; any other passes `restored-instance-refused` as not its step. Then `uninstall.sh`, then `tests/serverless/verify-clean.sh` for either form (it names the install's resources by its names and tags, whatever the form), the EKS form's Helm release and Secrets too.

#### UAT-DEP-10 Restore the metadata database from a snapshot

**Requirement**: design 9.2 ("the metadata database restored from a snapshot"); the user manual's backup and restore (#14); LLD "提交协议" (consistency check, reconcile); Colvenna Lake's UAT-CAT-11 part B and UAT-DEP-09.

**Configuration**: both forms (E1, S1); both Regions. S1 keeps `COLVENNA_NETWORK=create`: the instance is restored into the VPC it made. The restored instance has IAM database authentication on, as the manual's command makes it, which the serverless form signs in with. It needs the install to itself.

**Preconditions**: no other writer on the install; the manual's restore procedure at the candidate's commit.

**Input**: database `rst_<run>`: tables `t1` (10 commits), `t2` (5) and `gone` (3), written before the restore point; after it, one more commit to `t1`, a new table `t3` (5 commits), and `gone` dropped; a principal and a grant made after the restore point.

**Steps**:

1. Write the tables; record the listing, each table's state (row count, checksum, latest snapshot, schema, tags and branches) and each snapshot's contents.
2. Take a manual RDS snapshot: the restore point.
3. Make the changes after the point.
4. Restore as the manual says: a new instance from the snapshot, with the old one's subnet group, security groups and parameter group; then `install.sh` with `COLVENNA_DB=existing`, the new endpoint, the `Storage` stack's admin secret and bucket, and the rest of the configuration as it was. Before that run, the same configuration without `COLVENNA_BUCKET`; after it, the configuration with `COLVENNA_DB=create`.
5. `GET /colvenna/admin/v1/consistency/...` for each table; `reconcile` `t1`; register `t3` again; `unregister` `gone`.

**Expected**:

| Check | Expected |
| --- | --- |
| `repointed` | Step 4's `install.sh` exits 0, and the catalog runs on the new instance: EKS, the release's `database.url`; serverless, the `Serverless` stack's output `DatabaseEndpoint`. The catalog's users sign in to it: EKS, with the password of the `Eks` stack's secret; serverless, with IAM tokens, the catalog's and the authorizer's users granted `rds_iam` |
| `repoint-guarded` | (negative) Without `COLVENNA_BUCKET` (before step 4's run), and with `COLVENNA_DB=create` (after it): 3 each, naming the `Storage` stack's database, nothing changed |
| `lists-as-at-point` | After step 4: `gone`, `t1`, `t2`, no `t3` |
| `reads-as-at-point`, `history-as-at-point` | `t1` and `t2` read, snapshot by snapshot, as at the point |
| `check-t1-behind`, `check-t2-consistent`, `check-gone-missing` | The consistency check finds `t1`'s files ahead of its pointer, `t2` consistent, `gone`'s files missing |
| `all-in-line` | After step 5 every table of the database is consistent |
| `history-as-before-restore` | `t1` and `t3` read as before the restore, `t1`'s later commit included |
| `takes-writes` | `t1` and `t3` take new commits |
| `identities-as-at-point` | The principal and grant made after the point are gone, as the manual says |

The time the catalog was unavailable is recorded, not judged.

**Cleanup**: the restore point's snapshot and the old instance are deleted as the manual says, once the checks pass; the database `rst_<run>` is dropped. The install goes on on the restored instance, which UAT-DEP-09 deletes.

**Harness**: `tests/uat/uat.py run UAT-DEP-10 ... --install <the install's checkout>` (#52), from Colvenna Lake's `run-rds-restore.sh`, by the user manual's sections 6.1 to 6.3. It leaves the install on the restored instance, its configuration so changed written beside the original as `<env>-restored.env` (a copy in the evidence), which the install's later runs and its uninstall use.

#### UAT-DEP-11 The internal NLB

**Requirement**: design 6.1 (an internal NLB for clients in the VPC outside the cluster, admitting only the allowed CIDRs; no public entry); `deploy/config/example.env` (`COLVENNA_INTERNAL_NLB`). It is a function of the design, so the case is required (the requester, 2026-10-29).

**Configuration**: EKS form only (E3): the serverless form has no load balancer, its VPC entry being the private API; both Regions.

**Preconditions**: E3 run again with `COLVENNA_INTERNAL_NLB=on` and `COLVENNA_NLB_ALLOWED_CIDRS` set to the client subnet of section 3.1; the client there, with the install's CA.

**Input**: a Paimon 2.0.0 client's cycle (make a table, commit, read with per-table credentials) and the requests of UAT-SEC-01.

**Steps**:

1. Run the cycle and the refusals from the client, through the NLB's name.
2. Try the NLB from a pod in the cluster and from an address outside the allowed CIDRs.
3. Run `install.sh` again with `COLVENNA_INTERNAL_NLB=off`.

**Expected**:

| Check | Expected |
| --- | --- |
| `nlb-internal` | The load balancer's scheme is `internal`; no public address |
| `nlb-cycle` | The cycle passes; the certificate the client verified names the NLB |
| `nlb-unauthenticated-refused` | (negative) UAT-SEC-01's requests: 401 each |
| `nlb-only-allowed-cidrs` | (negative) From the cluster's pods and from outside the allowed CIDRs: no connection |
| `nlb-removed` | After step 3, the load balancer and its target groups are gone |

**Cleanup**: the NLB is off.

**Harness**: `tests/uat/uat.py run UAT-DEP-11` (#52): the client is the test driver (`tests/serverless/driver`), deployed into the test environment's client subnets with the install's CA and egress to the API port, and removed by `tests/serverless/down.sh`.

### 5.5 Fault recovery

#### UAT-FLT-01 Concurrent writers under faults

**Requirement**: design 9.2 ("concurrent commits of several writers and atomic commits under faults") and 6.2 (constraint 3: a retried commit takes effect once); LLD "提交协议"; Colvenna Lake's UAT-CAT-10.

**Configuration**: both forms (E1, S1), each with its own faults; both Regions. It needs the install to itself (about 30 minutes).

**Preconditions**: a database owner's admin access to the install's PostgreSQL for the case (to end sessions, close and open the database, hold a row lock); the install's normal state checked first (UAT-CAT-07).

**Input**: database `flt_<run>`, append table `events`; 6 writers (Colvenna Lake's `CommitStress`, on the Paimon Java API, each its own `commitUser`), each 120 commits of 500 rows, 3 s apart; readers checking that they only ever see whole commits; a maintenance loop compacting every 20 s with fast retries (200 retries, 50 ms to 0.5 s); a proxy in two writers' pods that drops the response to every tenth commit after the server answered, and sends the same commit again at once, as a client whose response was lost does (the proxy sends it again itself: Paimon's HTTP client does not resend a POST, and the client's own retry after a lost response first finds its commit published by the catalog and sends nothing more, so without the proxy no retried request would reach the catalog; #52's harness made this explicit, and the implementer confirmed it on 2026-10-29).

**Steps**:

1. Start the writers, readers and maintenance; wait for 5 snapshots and a consistent table.
2. Inject, each while the writers are committing, with at least 30 s between:
   - EKS: (1) force-delete one replica, 30 s later the other original one; (2) end the catalog's PostgreSQL sessions every 3 s for 30 s; (3) close the catalog's database to connections for 40 s (`ALLOW_CONNECTIONS false`, open sessions ended), then open it; (4) force-delete both replicas at once; (5) hold the branch row lock of `events` for 35 s, longer than the 30 s lock timeout.
   - Serverless: (1) replace every execution environment while commits are in flight: publish a new version of the server function with an environment variable of the case's own changed (`UAT_ROUND`) and move the alias `live` to it, as a deploy does; (2) and (3) as for EKS; (5) hold the row lock for 5 s, longer than the 3 s lock timeout.
   - Both: the dropping proxy runs throughout.
3. After the writers end, read the table's consistency report, the audit and the commit intents.

**Expected**: the run exits 0, and:

| Check | Expected |
| --- | --- |
| `faults-hit-live-writers` | Every fault began while the writers were committing; fault (2) ended sessions (count above 0); during fault (3) the catalog had no database (EKS: ready replicas 0; serverless: requests answered 503) |
| `every-commit-once`, `every-outcome-known`, `readers-see-whole-commits`, `snapshot-ids-contiguous` | The writers' and readers' own checks: every acknowledged commit exactly once, every outcome known, never a partial commit, no gap in snapshot ids |
| `failed-at-first-then-resolved` | At least one commit failed at first and was resolved by its writer's retry: a fault hit a commit in flight |
| `lock-timeout-retryable` | During fault (5) commits were answered 503 (retryable) and applied once after the client retried |
| `lost-responses-applied-once` | Each commit whose response the proxy dropped was applied exactly once; its retry was answered as a replay |
| `stale-base-refused-and-retried` | Commits on a stale base were refused (`success: false`, audited `FAILED`) and their writers committed again |
| `intents-published-once` | Published commit intents number the snapshots 1 to N, each once; none left `PENDING` |
| `maintenance-after-the-faults` | Every maintenance round from 60 s after the last fault succeeded |
| `consistent` | The table's consistency report: consistent, no problem |
| `no-failure-as-success` | No audit event with a status of 400 or more recorded as `SUCCEEDED` |

**Cleanup**: the database is opened again and the lock released also when the case fails; `flt_<run>` is dropped, each table's drop timed, and its tables' files purged and looked for (serverless: the purge function run then, #69), none left, nor the database's directory marker `<db>.db/` ([#13](https://github.com/colvenna/colvenna-catalog/issues/13)); serverless: the alias `live` moved back to the installed version and the case's versions deleted (CloudFormation would not do it: the template has not changed).

**Harness**: `tests/uat/uat.py run UAT-FLT-01` (#52), from Colvenna Lake's `run-commit-faults.sh`: the writers and the maintenance loop are `tests/uat/paimon` (`CommitStress`, `MaintenanceLoop`), the proxy `tests/uat/probe/proxy.py`; in the serverless form the database's faults run through the test driver's `sql` action inside the catalog's VPC.

#### UAT-FLT-02 A replica or execution environment lost

**Requirement**: design 9.2 ("after a replica or execution environment fails, the commit history stays consistent") and 6.1 (two replicas kept apart, PDB `minAvailable: 1`); Colvenna Lake's UAT-CAT-11 part A.

**Configuration**: both forms (E1, S1), each losing what it runs on; both Regions. It needs the install to itself.

**Preconditions**: EKS: the two replicas on different nodes; serverless: the reserved concurrency as installed.

**Input**: database `lost_<run>`, append table `t1`; 6 writers as in UAT-FLT-01 (EKS); serverless: twice as many writers as `COLVENNA_LAMBDA_CONCURRENCY` allows at once.

**Steps**:

1. EKS: while the writers commit, drain the node of one replica (only the catalog's pod evicted); then make the node schedulable again and force-delete the replica that kept serving.
2. Serverless: while the writers commit, end every PostgreSQL session of the server's user (as retired environments' are) and replace every execution environment (as in UAT-FLT-01's fault 1), then let the writers run over the reserved concurrency for 5 minutes.
3. Check the writers' results and the table.

**Expected**:

| Check | Expected |
| --- | --- |
| `eks-never-unavailable` | (EKS) At least one ready replica at every moment of the drain (the PDB held); the two replicas never on one node |
| `eks-evicted-replica-placed` | (EKS) The evicted replica waits for a node or runs on another, never beside the other replica |
| `serverless-over-concurrency-no-effect` | (serverless) Requests over what the install serves at once are refused before the server runs, with no effect: by Lambda's throttle (which the gateway answers 503) or by the gateway's own (429) (#80), with no error of the server function among them (a function's failure is a 503 too). Every answer other than 200 a watching client gets is 429 or 503, which the Paimon client sends again: never 500, nor anything else |
| `every-commit-once`, `every-outcome-known`, `snapshot-ids-contiguous` | As UAT-FLT-01 |
| `consistent` | The consistency report of `t1`: consistent |

**Cleanup**: nodes schedulable, the install as it was (two replicas; serverless: the alias `live` back on the installed version, the case's versions deleted); `lost_<run>` dropped, each table's drop timed (it answers once the table is gone from the catalog, whatever its files, #69), and its tables' files purged and looked for (serverless: the purge function run then), none left, nor the database's directory marker `<db>.db/` ([#13](https://github.com/colvenna/colvenna-catalog/issues/13)).

**Harness**: `tests/uat/uat.py run UAT-FLT-02` (#52), from Colvenna Lake's `run-recovery.sh`.

#### UAT-FLT-03 A table written outside the catalog is fenced, and reconcile releases it

**Requirement**: LLD "提交协议" (a snapshot not of the catalog fences the table; reconcile releases it) and "管理面修复".

**Configuration**: both forms (E1, S1); both Regions.

**Preconditions**: a table of the case's own with commits through the catalog; an identity of the case's own allowed to write the table's prefix directly (the role a writer bypassing the catalog would have).

**Input**: one snapshot committed by a Paimon filesystem catalog on the same table path, with that identity.

**Steps**:

1. Commit the outside snapshot.
2. Commit through the catalog; read through the catalog.
3. Stop the outside writer; `POST /colvenna/admin/v1/tables/{db}/{table}/reconcile`; commit through the catalog again.

**Expected**:

| Check | Expected |
| --- | --- |
| `fenced` | (negative) Step 2's commit: 409 on every try, its first request and one per retry (`commit.max-retries`, the LLD's "提交协议"), none accepted, and the client fails after its retries; audit `FENCE_TABLE` |
| `catalog-reads-hold` | `loadSnapshot` and batch reads stay at the catalog's last snapshot |
| `reconcile-releases` | Reconcile publishes the outside snapshot and lifts the fence; it reports each branch's snapshot before and after and the state `FENCED` |
| `commits-again` | Step 3's commit succeeds; the consistency report is consistent |

**Cleanup**: the table and the identity are deleted.

**Harness**: `tests/uat/uat.py run UAT-FLT-03` (#52): the outside writer is `tests/uat/paimon`'s `Fence`, a Paimon filesystem table on the table's path, with the credentials of an IAM role the case makes for itself on the table's prefix and deletes. Its commit through the catalog allows 2 retries, and the case counts the writer's commit requests among the table's audit events (`resource=<db>.<table>`, #90): 3, each 409 (#66).

## 6. Negative security checks by entry

| Entry | Unauthenticated | Authenticated, not permitted | Around the entry |
| --- | --- | --- | --- |
| EKS: the Service | UAT-SEC-01; UAT-CAT-06 | UAT-SEC-02; UAT-CAT-04; UAT-CAT-05 `service-identity-refused` | UAT-SEC-03 `eks-unlabelled-namespace`, `eks-no-public-load-balancer` |
| EKS: the internal NLB | UAT-DEP-11 `nlb-unauthenticated-refused` | UAT-SEC-02 (the same server) | UAT-DEP-11 `nlb-only-allowed-cidrs`, `nlb-internal` |
| EKS: health and metrics ports | — (no catalog data) | — | UAT-SEC-03 `eks-health-port`, `eks-metrics-port` |
| Serverless: the private API | UAT-SEC-01 `serverless-refused-before-the-server`; UAT-CAT-06 | UAT-SEC-02; UAT-CAT-04; UAT-CAT-05 | UAT-SEC-03 `serverless-outside-the-vpc`, `serverless-unlisted-endpoint` |
| Serverless: the functions | — | UAT-SEC-03 `serverless-direct-invoke` | UAT-SEC-04 `no-function-url` |
| The metadata database | — | — | UAT-SEC-03 `serverless-database-only-from-the-functions`; UAT-SEC-04 `database-private` |
| The warehouse bucket | UAT-SEC-04 `buckets-private` | UAT-CAT-02; UAT-CMP `credentials-only-the-catalogs` | UAT-CAT-02 `pod-identity-refused`, `no-s3-keys-in-clients` |
| The identity pool | UAT-SEC-04 `identity-pool-closed` | — | — |
| The installer | — | UAT-DEP-05 `account-not-the-callers`, the release refusals | UAT-DEP-01 `admin-token-not-printed` |

## 7. The required case set

This set holds for every candidate from the first after `v1.0.0-rc.1`, until this document changes. A candidate's manifest pins it at the commit the candidate is built from: `uat_cases` (this document's commit and blob) and `required_cases` (the IDs below, read by `scripts/release/manifest.py required-cases`). A change to it after a candidate is made is a change for the next candidate.

- Each case runs in `us-west-2` and in `us-east-2`, in each form the row names that the candidate's manifest lists in `configurations.forms`. A candidate that names only `eks` runs the EKS rows; one that names `serverless` runs its rows too.
- The declarations in `tests/compat/declared.tsv` at the candidate's commit are part of the set: each entry declared `supported` for a form must pass in that form; an entry declared `limitation` does not count against the case. A form without declarations cannot run its UAT-CMP cases, which are then `NOT_RUN`; both forms are declared (#52).
- The report gives each case, Region and form one result (section 3.3). Every one must be `PASS` for a `GO` (design 9.4).

<!-- required-cases:start -->

| Case | Name | Forms | Regions |
| --- | --- | --- | --- |
| UAT-CMP-01 | Flink 1.20 with Paimon 2.0.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-02 | Flink 1.20 with Paimon 2.1.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-03 | Flink 2.2 with Paimon 2.1.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-04 | Spark 3.5 with Paimon 2.0.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-05 | Spark 3.5 with Paimon 2.1.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-06 | Spark 4.1 with Paimon 2.1.0 | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-07 | Doris 4.1.5, read only | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CMP-08 | StarRocks 4.1.6, read only | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-01 | Table operations through the catalog | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-02 | Per-table credential scope | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-03 | Per-table credential renewal | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-04 | Authorization and audit | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-05 | Admin API | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-06 | Token rotation and revocation | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-07 | Health, schema version and metrics | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-CAT-08 | Request and response size limits | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-SEC-01 | Unauthenticated requests are refused at every entry | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-SEC-02 | Authenticated requests without permission are refused at every entry | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-SEC-03 | No way around the intended entry; health endpoints not exposed | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-SEC-04 | No Lambda function URL, no public or anonymous resource | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-01 | Install from nothing | EKS, serverless (`COLVENNA_NETWORK=create`) | `us-west-2`, `us-east-2` |
| UAT-DEP-02 | Install into existing resources | EKS, serverless (`COLVENNA_NETWORK=existing`) | `us-west-2`, `us-east-2` |
| UAT-DEP-03 | Run again | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-04 | Resume after an interruption | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-05 | Refused configurations | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-06 | Online upgrade from the previous release | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-07 | Going back to the previous release | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-08 | Uninstall keeping the data, and install again | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-09 | Uninstall deleting the data | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-10 | Restore the metadata database from a snapshot | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-DEP-11 | The internal NLB | EKS only: the serverless form has no load balancer | `us-west-2`, `us-east-2` |
| UAT-FLT-01 | Concurrent writers under faults | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-FLT-02 | A replica or execution environment lost | EKS, serverless | `us-west-2`, `us-east-2` |
| UAT-FLT-03 | A table written outside the catalog is fenced, and reconcile releases it | EKS, serverless | `us-west-2`, `us-east-2` |

<!-- required-cases:end -->

34 cases: for a candidate in both forms, 67 runs in each Region, 134 in all.

Not in the required set:

| What | Why |
| --- | --- |
| Section 2's items | See section 2 |
| UAT-DEP-04 with other steps or signals than its two | May run in addition; not counted |
| The performance scenarios | [Performance terms](../performance/terms.md); judged on their own, never counted in UAT |

> Note: the issue and pull request numbers (such as #52) in this document are those of the project's early private repository; this repository has no pages for them. Linked issue numbers (such as [#13](https://github.com/colvenna/colvenna-catalog/issues/13)) are this repository's current ones.
