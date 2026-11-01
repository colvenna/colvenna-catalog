# Performance harness

Runs the scenarios of the [performance terms](../../docs/performance/terms.md) (P1a, P1b, P1c, P2, P3, P4a, P4b, P5) on installs of Colvenna Catalog from this repository's own [test environment](../environment/README.md), and judges each run against the terms' confirmed targets. Performance is not part of UAT ([UAT cases](../../docs/uat/cases.md), section 2). A candidate's results go to `docs/performance/<candidate>.md`, written from [the template](../../docs/performance/template.md).

```bash
AWS_PROFILE=<profile> tests/performance/perf.py run P1a --config deploy/config/<env>.env --test-env <name> --out <evidence>
AWS_PROFILE=<profile> tests/performance/perf.py run P5 --config deploy/config/<eks-env>.env \
    --config deploy/config/<serverless-env>.env --test-env <name> --out <evidence>
AWS_PROFILE=<profile> tests/performance/perf.py run P4b --config deploy/config/<serverless-env>.env --test-env <name> \
    --out <evidence> --installer-checkout <candidate's checkout> --release <candidate>
tests/performance/perf.py check           # the harness's targets against terms.md, cell by cell
tests/performance/perf.py list            # the scenarios, their load and their targets
tests/performance/perf.py summary <run>   # a run's summary.md again, from its result.json
AWS_PROFILE=<profile> tests/performance/perf.py cleanup <run> --config deploy/config/<env>.env --test-env <name> --out <evidence>
                                          # what a killed run left, removed and read back
```

Every AWS call uses the profile `AWS_PROFILE` names; the harness never falls back to a default identity. The install's region is its configuration's. A run refuses to start while `perf.py check` finds the harness's targets differ from the terms' (`perflib/terms.py` holds every row of section 4's tables, and the check compares them cell by cell).

## What runs where

- **The load** is the load tool, [`load/`](load/pom.xml): Java on the Paimon 2.0.0 API, `RESTCatalog` and `RESTApi` with `token.provider=bear` and `data-token.enabled=true`, the client's own HTTP client, timeouts and retries unchanged (3 minutes; `ExponentialHttpRequestRetryStrategy(5)` on 429 and 503, about 31 s; recorded in each run's `environment.json` as `client`). It runs in pods on the test environment's clients' nodes (the stack's outputs `ClientNodeSelector` and `ClientNodeTaint`), on the compatibility matrix's Flink 1.20.5 image with the cell `flink-1.20-paimon-2.0.0`'s jars, fetched and checked by SHA-256, as the UAT harness's Paimon tools run. Its jar is built on this host (`mvn -B -f tests/performance/load/pom.xml package`, which a run does itself) and its SHA-256 recorded. The pods hold catalog tokens only, in Secrets of the run (files, never arguments or the environment), and no AWS identity of their own: table files are written with the per-table credentials the catalog vends. They reach an EKS install through its Service, in a namespace with its client label, and a serverless install through the test environment's `execute-api` endpoint.
- **The fixtures** are the run's own and synthetic: principals `pf<role>-<run>` (an owner with `MANAGE` on the catalog, which makes and drops the databases; writers and readers with the grants the scenario names), databases `<label>_<run>` with unpartitioned append tables `t000`, `t001`, ... (one commit of 10 rows each where a scenario reads snapshots).
- **The library** is the UAT harness's ([`tests/uat/uatlib`](../uat/README.md)): the install and the test environment, kubectl with the clients' placement, the admin API through a probe pod, SQL on the metadata database (psql in a pod, or the serverless test driver's `sql` action).

## The scenarios

| Scenario | Forms | What the run does |
| --- | --- | --- |
| P1a | EKS, serverless | 8 writers, each its own table, commit 100 rows at a time back to back for 1 + 10 minutes (`writers`) |
| P1b | EKS, serverless | 4 writers on one table, each commit due every 4 s on the EKS form and every 8 s on the serverless form (closed loop: made when the previous returned, or when due if later); 1 + 20 minutes on the EKS form, 1 + 40 minutes on the serverless form: 1,200 commits offered in each window (#83) |
| P1c | EKS, serverless | One table's capacity, observed and never judged (its result is `OBSERVED`): P1b's earlier load, each of the 4 writers' commits due every 2 s, 1 + 10 minutes |
| P2 | EKS, serverless | 20 databases of 50 tables with a snapshot each; the read mix open-loop (60 % `getTable`, 30 % the latest snapshot, 10 % `listTables` of a page of 100), 2 minutes of warm-up at the first step, then 5 minutes at each step (EKS 50 to 800 a second, serverless 25 to 150) (`reads`) |
| P3 | EKS, serverless | P3a: 3 credential requests a second for 6 minutes, each a (principal, table) pair never issued (540 tables, a `WRITE` and a `READ` principal); then P3b: 20 a second for 5 minutes over 20 tables issued before (each pair asked once, then a minute of warm-up at the rate) (`tokens`) |
| P4a | serverless | 8 rounds of forced new execution environments (a new version of the server with a variable of the test's own, the alias `live` moved to it, the authorizer's configuration changed alike), each 5 clients at once with tokens never used before: `GET /v1/config`, then `getTable` (`cold`); then 6 requests each after 30 minutes without traffic; the alias, configurations and versions put back |
| P4b | serverless | `install.sh` with `COLVENNA_SNAPSTART=on` (the install's own configuration otherwise; `--installer-checkout` and `--release` or `--from-source` say which), P4a's rounds, then `install.sh` with it `off` again, checked |
| P5 | both at once | On each form, in the same window: 4 writers on tables of their own every 2 s, P2's mix at 50 a second, credential requests at 2 a second (10 % pairs never issued), 2 minutes of warm-up, then 30 minutes (`mixed`); the database's sessions by user every 30 s |

`--duration-scale <f>` scales every warm-up, window and step, and `--rounds`, `--idle-samples` and `--idle-minutes` change P4's: for a trial of the harness only. Such a run is marked `reduced` in its result and is never a candidate's acceptance. Each run also checks the default sizes terms.md section 2 measures, from what it recorded (`environment.json`, `conditions`): per-table credentials on, the metadata database a single-AZ `db.t4g.small`; EKS 2 replicas with their requests and limits, `COLVENNA_JDBC_POOL_SIZE` 16, the catalogs' two `t3.large` nodes; serverless 2048 MB on x86_64, reserved concurrency 10, SnapStart off (on for P4b). A run is `official` only when it is not reduced, its tree was clean and every condition held. That no UAT case or other test runs on the installs meanwhile is the operator's to keep (terms.md section 2).

## How a run is judged

As terms.md section 3 says, with these choices of the method:

- **Client-observed latency of a request**: the Paimon client's own log line, one per attempt, `[rest] requestId:<id> method:<m> url:<u> duration:<ms>ms`, which the load tool's log4j2 configuration writes to `rest.log` with the time and the thread. It runs from sending the request to receiving the answer's headers (`LoggingInterceptor`, a response interceptor), so the time to read the answer's body is not in it. P1a's commit request and P4's first request are judged on it; P2's and P3's are reported beside their judged latencies (observed).
- **Operation latency**: the load tool's own timing around one call of the Paimon API, the client's retries included and the answer read whole. Open-loop requests (P2, P3, P5's reads and credentials) are timed from the time each was due, so a slow catalog cannot slow the load down; their percentiles are of the requests answered, and those that failed are errors, counted apart. A request counts as sent on time when it started within 100 ms of that time (P2's sustainable rate needs 99 % on time; the terms leave the bound to the method). P1b's whole commit is the `commit` call, conflict retries included; writing the data file before it is reported apart.
- **Joins**: the server answers every request with its `X-Request-Id`, which the client's line carries. An operation ran on one thread from its start to its end, so the client's lines of that thread in that span are its attempts; through their ids come the server's access log entries (status, server-side latency) and the audit events: a credential request is a miss or a hit as its `ISSUE_DATA_TOKEN` event's `cached` says, and a commit refused on a stale base is a `COMMIT` event with outcome `FAILED` answered 200 (`success: false`: a conflict, counted, not an error).
- **Percentiles and verdicts**: nearest rank over every sample of the window, warm-up excluded; a judged percentile needs 1,000 samples (P4: 40), else it is `NOT_JUDGED` (observed only). A scenario is `PASS` when every judged metric passed, `NOT_JUDGED` when none failed but one had too few samples, `FAIL` otherwise; a run whose clean-up failed, or that did not complete, is `FAIL`. P1c judges nothing: its run is `OBSERVED` (exit 0) when it completed and cleaned up.
- **P4's environments**: from the functions' `REPORT`, `INIT_REPORT` and `RESTORE_REPORT` lines, per log stream (an execution environment), each assigned to the round whose requests came just before its first invocation; errors are init timeouts or failures, the gateway's 5xx answers in the rounds (its access log), and clients that failed.
- **P5's cold requests**: in the serverless form, the first request each execution environment that started in the window served is reported apart from the judged latencies, with its action: a `COMMIT` is one that may also have waited on the commit lock and on S3 (terms.md P4). P4's own cold requests, `GET /v1/config` and `getTable`, take neither.
- **P5's credential misses**: 10 % of 2 a second for 30 minutes is about 360 misses, fewer than the 1,000 a judged percentile needs (section 3). Judged, that row could only be `NOT_JUDGED`, and the candidate's verdict never `PASS`. The requester decided on 2026-11-01 (#91; terms.md, P5) that P5 reports the misses' latency and does not judge it: P3 judges it, on P3a's 1,080 misses. Every other part of P5 gives 1,000 samples or more at this load and is judged by its form's targets; a miss that failed is still one of P5's errors.
- **The catalog's own metrics**: in the EKS form, every pod's `/metrics` before and after the run, from a probe in a namespace with the install's monitoring label (the counters' difference is reported); in the serverless form the server function's and the API's CloudWatch metrics.

### What the terms leave open

- **Choices the method makes where the terms are silent**: "sent on time" is within 100 ms of the due time; P3b's 20 tables are asked once each, then a minute of warm-up at the rate, before its window ("tables already issued"); P5's warm-up is 2 minutes (P2's).

## A run's evidence

`<out>/<scenario>/` (a rerun is `<scenario>-2`, `-3`, ...; an earlier run's evidence is never written over), all of it through the compatibility matrix's sanitizer with the run's tokens: no token, password, account ID, ARN, address or bucket is kept. The JSON it writes has its floats rounded to six decimals first: the sanitizer takes twelve digits standing alone for an account ID, and a float's fraction can be exactly twelve digits long.

| File | What it is |
| --- | --- |
| `result.json` | The verdict and why; per form each metric (samples, p50, p95, p99, max, mean), its target and verdict; the load's parameters; the clean-up, each step read back |
| `summary.md` | The same as the performance document's tables |
| `environment.json` | The run, the harness's commit and whether its tree was clean, the duration scale; the cluster, every node (node group, instance type, zone, kubelet), where each load pod ran; each install (its source commit or candidate, image, replicas or function settings, the database's class and engine); the load tool's image, jars and jar SHA-256; the client's timeouts and retries |
| `run.log` | What the run did, in order |
| `<form>/<job>/` | The load tool's output: its records (`commits.jsonl`, `reads.jsonl`, `tokens.jsonl`, `r<n>-cold.jsonl`: one line per operation), the client's request log (`rest.log`), its facts (`tool.log`) |
| `<form>/server-access.log` | The server's access log over the run: the catalog pods' logs, followed every 20 s while the load runs (the kubelet rotates them at 10 MiB), or the server function's log group |
| `<form>/audit.jsonl` | The audit events of the run's principals, read back in pieces of 1,000 |
| `<form>/database-metrics.json` | The metadata database's CPU, burst credit balance and usage, connections, IO latency and memory through the run, one-minute points |
| `<form>/function-metrics.json`, `gateway-access.jsonl`, `*-reports.json` | Serverless: the server function's and the API's CloudWatch metrics, the API's access log, the functions' REPORT lines |
| `<form>/connections.json` | P5: the database's sessions by user, every 30 s |
| `<form>/functions-before.json`, `round-<n>.log`, `idle-<n>.log`, `install-snapstart.log` | P4: what it changed and put back, each round, `install.sh`'s runs |

## Clean-up

Whatever happens, the run's clean-up runs and is read back: P4's functions and alias put back (and SnapStart off again, P4b), the serverless test driver removed if P5 deployed it, the run's databases dropped with their tables and listed as gone, no object left under their prefixes (the serverless form's purge function run first, as UAT-CAT-01 does; the zero-byte `<database>.db/` object the pilot found left after each drop, S3A's marker of an empty directory, counts too, as the server removes it since [#13](https://github.com/colvenna/colvenna-catalog/issues/13), and how many there were is reported apart, `directoryMarkersLeft`), the grants deleted and the principals disabled (both listed again), the namespaces deleted. A run stopped with Ctrl-C, SIGTERM or SIGHUP still runs it. If the harness itself is killed (SIGKILL), `perf.py cleanup <run>` with the run's configurations removes what carries the run's id `<run>` (in `run.log`), with a principal of its own that it disables afterwards, and reads it back; or by hand:

- `kubectl delete namespace -l colvenna-perf/run=<run>`;
- the databases `*_<run>`, dropped with the owner's token or another principal with `MANAGE` (the REST API, or Paimon's `dropDatabase` with `cascade`); the principals `pf*-<run>` disabled and their grants deleted with the admin API;
- P4: the alias `live` of `colvenna-catalog-<env>-server` back on the version the stack's output `ServerVersion` names, `PERF_ROUND` removed from the server's and the authorizer's environment, the versions the run published deleted; P4b: `install.sh` again with the install's own configuration (`COLVENNA_SNAPSTART=off`);
- P5, serverless: `tests/serverless/down.sh <env> <region>` if the run deployed the test driver.

## What the host needs

What the UAT harness needs ([its README](../uat/README.md)): Python 3.8 or later, the AWS CLI v2, `kubectl`, Maven with JDK 21, and for P5 on a serverless install Docker (the test driver). P4b runs `install.sh` from the candidate's checkout, with that script's tools.
