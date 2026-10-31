# UAT harness

Runs the cases of [the UAT cases](../../docs/uat/cases.md) on an install of Colvenna Catalog, from this repository's own [test environment](../environment/README.md), and judges each run by the cases document's section 3.3. It is ported from Colvenna Lake's catalog acceptance (`tests/acceptance/catalog/` at commit `50dae8755e758272a94d5b195e15e1a61713b569`) without Colvenna Lake's environment or fixtures: every case makes its own databases, tables, principals, tokens and grants, with synthetic data only, and removes them.

```bash
AWS_PROFILE=<profile> tests/uat/uat.py run UAT-CAT-04 --config deploy/config/<env>.env --test-env <name> --out <evidence>
tests/uat/uat.py judge <evidence>/UAT-CAT-04          # judge a run again from its verdicts
tests/uat/uat.py list                                 # every case, and whether this harness runs it
tests/uat/uat.py check                                # the harness against the cases document
```

The install's region is its configuration's (`COLVENNA_REGION`); every AWS call uses the profile named by `AWS_PROFILE`, which must be set: the harness never falls back to a default identity. The clients (the probe pods, psql, engines, tools and the test driver) run in the test environment's cluster, on its clients' nodes, never beside the catalogs: every pod the harness applies, and every Deployment's pod template, selects the nodes of the stack's output `ClientNodeSelector` and tolerates its `ClientNodeTaint` (`uatlib/kube.py`); a test environment without them stops the run. They run in namespaces of the run's own, `uat-<run>-<role>`, which the run deletes at the end. In the EKS form those that should reach the catalog carry the install's client label (`COLVENNA_CLIENT_NAMESPACE_LABEL`); in the serverless form they reach its private API through the test environment's `execute-api` endpoint, which the install lists in `COLVENNA_API_ENDPOINT_IDS`.

## What the host needs

Python 3.8 or later with PyYAML (UAT-CAT-05 reads the OpenAPI description), the AWS CLI v2, `kubectl` (on `PATH`, or `scripts/install-tools.sh`'s in `.tools/`, which the harness puts first), `helm` (UAT-DEP-07), `curl` and `jq`, Maven with JDK 21 (UAT-CAT-01, 02 and 03 build their flows on this host, and the fault and lifecycle cases the Paimon tools), Docker (the serverless form's test driver), and the operator's profile; the cluster's kubeconfig is made for each run from the test environment's stack. A client pod pulls `public.ecr.aws/docker/library/python` and `postgres` by digest.

## A run's evidence

`<out>/<case>/` (a rerun is `<case>-2`, `-3`, ...; an earlier run's evidence is never written over):

| File | What it is |
| --- | --- |
| `verdicts.tsv` | One line per check the case lists, written when it is decided: the time (UTC), the check, `PASS` or `FAIL`, and a detail. A precondition the case depends on is a line `precondition:<what>`; one that failed blocks the case |
| `result.json` | The result, `PASS`, `FAIL`, `BLOCKED` or `NOT_RUN`, and why; the checks the case lists for the form; the install, the test environment, the harness's commit and whether its tree was clean; the clean-up and each of its steps |
| `run.log` | What the run did, in order |
| the rest | The case's raw evidence: every request and its answer (JSON lines), what it read back (audit events, states) |

Everything written goes through the compatibility matrix's sanitizer, with every token and password the run holds or issues: no token, password, account ID, ARN, address or bucket is kept. A result is `PASS` only when the run exited 0, every check the case lists for the form is there and `PASS`, no check it does not list is there, and the clean-up left nothing; the clean-up (the case's databases dropped, its grants deleted, its principals disabled, its namespaces deleted, and what the case registered) runs whatever happened, also after a failure.

A run stopped with Ctrl-C, SIGTERM or SIGHUP still runs its clean-up and writes its result. If the harness itself is killed (SIGKILL), remove what the run made by hand; everything carries the run's id, `<run>` (in `result.json` and `run.log`):

- the namespaces labelled `colvenna-uat/run=<run>`: `kubectl delete namespace -l colvenna-uat/run=<run>`;
- the catalog's objects of the run: its databases (`*_<run>`, dropped through the REST API with the admin's token, tables first), its principals (`*-<run>`, disabled) and their grants (deleted);
- UAT-SEC-03 (serverless): the VPC endpoint tagged `colvenna-uat/run=<run>`, and the IAM role `colvenna-uat-norights-<run>`;
- UAT-DEP-05: the Aurora cluster `uat-<run>-aurora`, its DB subnet group and its DB cluster parameter group (both named as the cluster), the bucket `uat-<run>-elsewhere-*` in the other Region, the git worktrees under the run's scratch directory (`git worktree prune` in the candidate's checkout); and, if the run was killed while enforcement was off, `tests/environment/network-policy.sh <test-env> <region> on`, which starts the catalog pods again itself;
- UAT-DEP-08, UAT-DEP-11 (and any case that deployed the test driver): `tests/serverless/down.sh <env> <region>`;
- UAT-DEP-11: the internal NLB, if left on: `install.sh` again with the install's own configuration;
- UAT-DEP-09 (E3, S3): the marker `<prefix>/uat-dep09-<run>.marker` in the bucket the install was given;
- UAT-DEP-04: the install's own `COLVENNA_DATA_TOKEN_DURATION`, if a round was cut short: `install.sh` again with its configuration.

## The engines

UAT-CAT-01, 02 and 03 run real engines ([`uatlib/engines.py`](uatlib/engines.py)) in a namespace of the run's own, labelled as the install's client, admitting ingress only from its own pods: the official Flink 1.20.5, Spark 3.5.9 and Doris 4.1.5 images and the Paimon 2.0.0 jars of the compatibility matrix's cells, as [`tests/compat/cells.json`](../compat/cells.json) pins them, fetched and checked by an init container. The flows are [`flows/`](flows/pom.xml) (`mvn -B -f tests/uat/flows/pom.xml package`, which a run does itself), the matrix's fixture writer and Doris reader. A Flink session cluster is a JobManager and two TaskManagers as Deployments and a Service, with no operator; its TaskManagers fetch their own per-table credentials. No pod has an AWS identity of its own.

## How a case is written

One module per case in [`cases/`](cases/__init__.py), with `ID`, `FORMS`, `checks(form, args)` (the concrete names of the checks it writes, templates such as `audit-<call>` filled in) and `run(ctx)`. `uat.py check` and `test_doc.py` hold every case to the cases document: it writes exactly the checks the document lists for each form (each concrete name is the document's check of that name, or fills in exactly one of its templates, and every template is filled in), and the document's Harness line names `tests/uat/uat.py run <case>`. A case collects, then judges: each check's verdict comes from a function that takes what was collected and nothing else (`judge_*`), which the case's `test_<case>.py` tests offline, with answers that should pass and answers that should not. A preparation the case makes for itself (a database, a principal, a VPC endpoint, a reader pod) is made inside `ctx.preparing(...)`, so that its failure blocks the case instead of failing it. The library ([`uatlib/`](uatlib/)) gives a case:

- `ctx.probe`: the probe pod ([`probe/probe.py`](probe/probe.py), Python's standard library only), which makes HTTP requests, TCP connections, plain-HTTP tries and DNS lookups from inside the cluster. A plan, and the tokens it uses, go to the pod on stdin, never as an argument; `Background` runs one on its own (a reader that keeps reading);
- `ctx.admin` and `ctx.rest`: the admin API and the REST API with the admin's token, and the tokens the case issues, held in memory;
- `Sql(ctx)`: SQL on the metadata database, in the EKS form by psql in a pod of the run (as the catalog's user or the database's admin, its password in a file, never the environment);
- `catalog.py`: the catalog's pods, its access log of a time window in either form (the catalog names each request by an id of its own, whatever the client sent: match by the answer's `serverRequestId`), its database instance;
- `deploy.py`: `install.sh` and `uninstall.sh` run as their user runs them (a terminal to type the confirmation on, or none; a signal to the whole process group once a condition holds, the script started with SIGHUP, SIGINT, SIGQUIT and SIGTERM at their default whatever the harness inherited, #76), their output kept line by line with its time; the install, the account and the cluster recorded, and compared;
- `testdriver.py`: the serverless live test's driver (`tests/serverless/driver`, the Paimon 2.0.0 client), its image built on the install's own image in the install's repository, run in a pod (`--local`) or deployed as a function into given subnets;
- `Sql(ctx)` in the serverless form: the database admits only the catalog's functions, so the statements go through the test driver's `sql` action (`uatlib/driver.py`), inside the catalog's VPC. The harness deploys the driver once per install (`tests/serverless/run.sh <env> <region> deploy` with `DRIVER_DATABASE_ACCESS=on`): it then also joins the functions' security group, and its role may sign in to the database as the catalog's user with RDS IAM authentication, so no password is used. Remove it with `tests/serverless/down.sh` before the install is uninstalled;
- `Tools(ctx)`: the Paimon tools of [`paimon/`](paimon/pom.xml) (Paimon 2.0.0's Java API: concurrent writers, maintenance, the upgrade's loops, state dumps and an outside writer), built with this host's Maven and run in pods on the compatibility matrix's Flink 1.20.5 image with the cell `flink-1.20-paimon-2.0.0`'s jars, fetched and checked against their SHA-256: short tools in a tool pod through `kubectl exec`, the token on stdin; long ones as job pods, their token in a Secret of their own. Each tool's facts are lines `UAT <json>`;
- `ctx.check`, `ctx.preparing`, `ctx.keep` and `ctx.on_cleanup`.

The deployment cases run `install.sh` and `uninstall.sh` of the candidate's checkout: this one, or the one `--installer-checkout` names (a candidate's clean checkout, when the harness runs from another). `--release <candidate>` says which candidate the install is of; without it a case that installs runs `--from-source`. UAT-DEP-01, 02, 08 and 11 change the install (UAT-DEP-08 writes the reinstall's keys into its configuration, keeping the old one in its evidence), and UAT-DEP-09 removes it: run them in the cases document's order (section 3.2). Run one `install.sh` or `uninstall.sh` at a time in a checkout: a release install reinstalls the CDK app's packages (`npm ci`), so two at once in one checkout break each other; cases on installs that run at once each need a checkout of the candidate of their own.

## The cases it runs

| Case | Forms | What runs |
| --- | --- | --- |
| UAT-CAT-01 | EKS, serverless | `FlinkCatalogFlow` on a standalone Flink 1.20.5 session cluster (JobManager and two TaskManagers, no operator), submitted at parallelism 2, its bounded streaming write at parallelism 1; `SparkCatalogFlow` in Spark 3.5.9 (`local[4]`); the dropped table's files looked for; the access log for routes the catalog lacks |
| UAT-CAT-02 | EKS, serverless | Three tables seeded through the catalog (`Seed`); three principals' credentials for one, each used alone on S3 from this host; the pods' own identity (`Probe`); the session in STS and CloudTrail; a client-chosen location and a registration outside the warehouse, refused; the audit; the clients searched for S3 keys |
| UAT-CAT-03 | EKS, serverless | With 90-minute credentials: Flink's 100-minute bounded streaming write (parallelism 1, no restart), Spark's and Doris's (the matrix's reader) second rounds after 95 minutes, the renewals in the audit; then `install.sh` with the default duration (`--reinstall`). About 1 hour 50 minutes |
| UAT-CAT-04 | EKS, serverless | Six principals' requests from a client pod; each request's audit event by its id; a deleted grant and an expiring one |
| UAT-CAT-05 | EKS, serverless | Every admin operation and its invalid variants, held to the OpenAPI description; every operation refused to a service principal; the admin API manual's examples as written (EKS: on this host, through the manual's port-forward, which needs local port 8181 free; serverless: `curl` relayed to the client pod) |
| UAT-CAT-06 | EKS, serverless | A reader through the manual's rotation order; a revoked token every second for 90 s; an expired one; a disabled principal's |
| UAT-CAT-07 | EKS, serverless | The Deployment, PDB and HPA, `/readyz` inside each pod and `/metrics` from a monitoring namespace (EKS); the functions' configuration and log (serverless); the schema version |
| UAT-CAT-08 | EKS, serverless | A commit body over 64 MiB (EKS); commits of 41,000 and 42,000 partitions, table-details pages of 16 wide tables and one table too large for a page (serverless); each refusal's table state before and after |
| UAT-SEC-01 | EKS, serverless | Five routes with eight kinds of missing or bad credential, each 401; plain HTTP to the TLS port (EKS); the gateway refusing before the server (serverless) |
| UAT-SEC-02 | EKS, serverless | A principal without a grant and one with `READ`, at the form's entry |
| UAT-SEC-03 | EKS, serverless | The API, health and metrics ports from namespaces with and without the labels, and inside the pods; no public load balancer (EKS); `tests/serverless/outside.py`, an unlisted `execute-api` endpoint made for the run, Lambda Invoke by an IAM role of the run's own with no permission, the database's security group (serverless) |
| UAT-SEC-04 | EKS, serverless | Function URLs in the Region and in every stack's template, the REST API's endpoint type, policy and methods, internet entries, buckets (and unsigned requests), repositories, secrets, the database, the identity pool (serverless) |
| UAT-DEP-01 | EKS, serverless | `install.sh --release <candidate>` from the candidate's checkout (`--installer-checkout`), its confirmation typed on a terminal; its output, evidence, the stacks, the running image and chart; the test driver's cycle in a client pod (EKS) or `tests/serverless/run.sh cycle` (serverless) |
| UAT-DEP-02 | EKS, serverless | As UAT-DEP-01 with `--yes`, into the test environment's database, bucket (and VPC, serverless); those recorded before and after, with their databases, roles and prefixes; the minutes to the first write and read (EKS) |
| UAT-DEP-03 | EKS, serverless | The install recorded (stacks and physical ids, Helm release and pods or functions and alias, images, the admin token's version, principals, grants, tables), `install.sh --yes` again, recorded again |
| UAT-DEP-04 | EKS, serverless | A new `COLVENNA_DATA_TOKEN_DURATION`, `install.sh` killed by `SIGHUP` and by `SIGKILL` to its process group while its change is under way (Helm `pending-upgrade`; the stack's update), then run again |
| UAT-DEP-05 | EKS, serverless | Every refusal, each with a configuration of the case's own environment wrong in one key; the fixtures it makes and removes (an Aurora cluster without an instance, a bucket in another Region, checkouts not the candidate's); NetworkPolicy switched off and on (EKS) |
| UAT-DEP-08 | EKS, serverless | The driver's tables and a principal, the account recorded, `uninstall.sh --yes` twice, the install again onto the kept data (its keys written into the configuration), the tables read again |
| UAT-DEP-09 | EKS, serverless | E1 and S1 on the instance UAT-DEP-10 restored: `uninstall.sh --yes --delete-data` refused, then the instance and its log group deleted; `uninstall.sh --yes --delete-data` without the name, then with it; `tests/serverless/verify-clean.sh` (which, while all that is left is what RDS is still deleting, the automated snapshots and backups of the instances deleted, reads the account again, the last time no later than 20 minutes after the first read, #59); the existing database's and bucket's leftovers checked (a marker of the run's own under the prefix, #44), then dropped (E3, S3) |
| UAT-DEP-11 | EKS | The install run again with the internal NLB for the client subnets; the test driver there (its CA, port 8181): the cycle, the certificate, UAT-SEC-01's requests; a pod and this host refused; the NLB off again |
| UAT-FLT-01 | EKS, serverless | Six writers in three pods (two through the response-dropping proxy, `probe/proxy.py`), readers and maintenance; the form's faults, timed in `timeline.txt`; the writers' checks, the commits' audit, the intents and the consistency report from the database |
| UAT-FLT-02 | EKS, serverless | Writers while a replica's node is drained and the serving replica killed (EKS), or the server's sessions ended, the environments replaced and twice the reserved concurrency writing (serverless) |
| UAT-FLT-03 | EKS, serverless | An outside writer's snapshot (a Paimon filesystem table on the same path, with the credentials of an IAM role the case makes on the table's prefix and deletes), the fenced commit, the reads, reconcile, a commit again |
| UAT-DEP-06 | EKS, serverless | The previous release in use and recorded, its database snapshotted (kept for UAT-DEP-07), the loops running; `install.sh --yes` from `--candidate`'s checkout; 5 minutes more; a new token; the record again |
| UAT-DEP-07 | EKS, serverless | The way back that the two checkouts' migrations call for (`helm rollback`, or the previous release's `install.sh`; or its refusal and the snapshot restore), the loops running; then the candidate again |
| UAT-DEP-10 | EKS, serverless | The user manual's restore: a snapshot as the restore point, changes after it, a new instance from it, `install.sh` again from `--install`'s checkout onto it (refused first without `COLVENNA_BUCKET`, and after it with `COLVENNA_DB=create`), the checks and repairs of its section 6.3. `--install` must hold #59's guards |
