---
title: "Colvenna Catalog <candidate>: performance acceptance"
date: <YYYY-MM-DD>
status: template
issue: 20
---

# Colvenna Catalog `<candidate>`: performance acceptance

> **The template of `docs/performance/<candidate>.md`.** Copy it to that name and fill in every `<...>`; delete this note and every instruction in a quote. A section without its results, or a `<...>` left, is not a finished document (design 9.1). The terms (scenarios, method and targets) are [terms.md](terms.md) at the version the candidate's manifest pins (`performance`); this document reports against them and never changes them. Write the numbers as `result.json` and `summary.md` of each judged run give them (`tests/performance/perf.py summary <run>` writes the tables of section 5); keep the raw data private (section 6).

## 1. Verdict

`<PASS | NOT_JUDGED | FAIL>`, by terms.md section 1: `PASS` when every judged metric of every scenario was measured with enough samples and met its target; `NOT_JUDGED` when nothing failed but a judged metric had too few samples; `FAIL` when a judged metric missed its target, an error occurred where none is allowed, or a result is missing. Only `PASS` meets design 9.4, so the release check's performance item is `<met | not met>`.

- What decided it: `<the rows that failed or were not judged, each with its scenario, form, measured value, target and the run that measured it; or "every judged row passed">`.
- The judged runs: `<one per scenario, its id and verdict: P1a <run> PASS, ...>`; P1c's run, observed: `<run> OBSERVED`. Runs that were stopped or refused, and why: `<none | ...>`. A run is judged once; a later run is another run, and both are reported.
- What the verdict covers: the default sizes of terms.md section 2, the loads of section 4, in `us-west-2`. It is not a capacity limit (section 7).

## 2. The candidate tested

| Item | Value |
| --- | --- |
| Candidate | `<candidate>`, its tag at `<C: full SHA>`, built from `<B: full SHA>` (the manifest's `source.built_from`; the tag differs from it under `release/<candidate>/` only) |
| Image | `colvenna-catalog@<digest>` (`linux/amd64`), the manifest's `image.digest`; each install's record (`environment.json`, `installs`) shows it running: `<yes, or what runs instead>` |
| Chart, `install.sh`, CDK app | As the manifest pins them (`chart.tree`, `install.sh`, `deploy/aws`) |
| Terms | [terms.md](terms.md) at `<commit>` (blob `<blob>`), the manifest's `performance`; `perf.py check` agreed with it at the harness's commit: `<yes>` |
| Harness | `tests/performance/` at `<commit>` (clean tree), the load tool's jar SHA-256 `<sha256>`; the load ran on `<image>` with the Paimon 2.0.0 jars of the cell `flink-1.20-paimon-2.0.0` |
| Client | Paimon 2.0.0 `RESTCatalog` and `RESTApi`, `token.provider=bear`, `data-token.enabled=true`; its timeouts and retries unchanged: response, connection-request and connect timeouts 3 minutes; `ExponentialHttpRequestRetryStrategy(5)` (429 and 503 retried after 1, 2, 4, 8 and 16 s, about 31 s in all; never a timeout or a 504); `commit.max-retries` 10 |

## 3. Environment

| Item | Value |
| --- | --- |
| Account and Region | `<ACCOUNT_ID>`, `us-west-2` |
| Test environment | `<name>`, made by `tests/environment/up.sh` at `<commit>`: EKS `<version>` (`<platform version>`), kubelet `<version>`; the catalogs' nodes `<2 x t3.large, zones>`, the clients' nodes `<2 x t3.xlarge, zones>` |
| When | `<from> to <to>` (UTC): after UAT step 8 and before step 11 ([UAT cases](../uat/cases.md), section 3.2), with no UAT case or other test on the installs; or on installs of its own: `<which>` |
| EKS form | `<env>`: `install.sh --release <candidate>`, defaults, per-table credentials on; 2 replicas (requests 0.5 CPU and 1 GiB, limits 2 CPU and 2 GiB), `COLVENNA_JDBC_POOL_SIZE` 16, on the catalogs' nodes `<nodes>` |
| Serverless form | `<env>`: the server function at 2048 MB, x86_64, reserved concurrency 10, SnapStart off (P4b: on for that run, then off again: `<checked>`) |
| Metadata databases | Each form its own: RDS PostgreSQL `<version>`, `db.t4g.small`, single-AZ. CPU and burst credit balance through every run: section 5, each scenario's "metadata database" row |
| Load | Pods on the clients' nodes (`<nodes>`), one per job (`environment.json`, `loadPods`) |
| Patch window | `<the Patch Group tag removed from the host and the nodes before the window, restored afterwards; or "no run crossed 00:00 UTC">` |

## 4. Scenarios, method and targets

As [terms.md](terms.md) section 4 fixes them; the harness ([`tests/performance/README.md`](../../tests/performance/README.md)) runs each as one command, `perf.py run <scenario>`. In brief:

- **Client-observed latency** of a request is the Paimon client's own log line, per attempt; **operation latency** is the load tool's timing around one Paimon call, the client's retries included. Open-loop requests are timed from when each was due; a request is sent on time when it started within 100 ms of that.
- **Joins**: by the server's `X-Request-Id`, which the client's line carries; an operation's attempts are its thread's lines between its start and end. A credential request is a miss or a hit as its `ISSUE_DATA_TOKEN` audit event says; a commit refused on a stale base is a conflict, not an error.
- **Percentiles**: nearest rank over every sample of the window, warm-up excluded; 1,000 samples for a judged percentile (P4: 40).
- **Open-loop percentiles** are of the requests answered; those that failed are errors, counted apart. The client's request line ends at the answer's headers, so for P2 and P3 it is reported beside the judged latency, which includes reading the answer.
- **Choices the terms leave to the method**: P1b's whole commit is the `commit` call (conflict retries included); P3b's 20 tables are asked once each, then a minute of warm-up at the rate, before its window; P5's warm-up is 2 minutes; P5 reports apart the requests a serverless environment that started in the window served first.
- **P5's credential misses**: about 360 a form (10 % of 2 a second for 30 minutes), fewer than the 1,000 a judged percentile needs; by the requester's decision of 2026-11-01 (#91; terms.md, P5) their latency is reported, not judged, and P3 judges it. A miss that failed is still one of P5's errors.

> Note here any difference from the terms' durations or loads, and why: a judged run has none (`result.json`, `reduced: false`, `official: true`).

## 5. Results

> One table per scenario, both forms, as `summary.md` gives them (latencies in milliseconds). Keep every judged row, and the observed rows the terms name. Under each table, state what the numbers show without changing a verdict: the slowest steps, cold starts in a window, the database's credit balance.

### 5.1 P1a: writers on separate tables

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | Commits per second | | | | | | ≥ 10 | |
| EKS | Commit request, client-observed | | | | | | p95 ≤ 500, p99 ≤ 1,000 | |
| EKS | Errors | | | | | | 0 | |
| EKS | Server-side commit latency | | | | | | observed | |
| EKS | Whole commit | | | | | | observed | |
| Serverless | Commits per second | | | | | | ≥ 5 | |
| Serverless | Commit request, client-observed | | | | | | p95 ≤ 1,000, p99 ≤ 2,000 | |
| Serverless | Errors | | | | | | 0 | |
| Serverless | Server-side commit latency | | | | | | observed | |
| Serverless | Whole commit | | | | | | observed | |

### 5.2 P1b and P1c: writers on one table

P1b: 4 writers every 4 s on the EKS form (1 commit/s, 20 minutes), every 8 s on the serverless form (0.5/s, 40 minutes), 1,200 commits offered each.

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | Commits done of those offered | | | | | | ≥ 99 %, none failed | |
| EKS | Whole commit, conflict retries included | | | | | | p95 ≤ 3,000, p99 ≤ 6,000 | |
| EKS | Conflicts | | | | | | observed | |
| Serverless | Commits done of those offered | | | | | | ≥ 99 %, none failed | |
| Serverless | Whole commit, conflict retries included | | | | | | p95 ≤ 5,000, p99 ≤ 10,000 | |
| Serverless | Conflicts | | | | | | observed | |

P1c, one table's capacity (observed, never judged; its run is `OBSERVED`): 4 writers every 2 s (2 commits/s offered), 10 minutes.

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | Commits done per second; done of those offered, failed | | | | | | observed | |
| EKS | Whole commit, conflict retries included; conflicts per commit done | | | | | | observed | |
| Serverless | Commits done per second; done of those offered, failed | | | | | | observed | |
| Serverless | Whole commit, conflict retries included; conflicts per commit done | | | | | | observed | |

### 5.3 P2: metadata reads

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | `getTable` at 200/s | | | | | | p95 ≤ 50, p99 ≤ 100 | |
| EKS | Latest snapshot at 200/s | | | | | | p95 ≤ 50, p99 ≤ 100 | |
| EKS | `listTables` at 200/s | | | | | | p95 ≤ 100, p99 ≤ 250 | |
| EKS | Sustainable rate | | | | | | ≥ 200/s | |
| Serverless | `getTable` at 50/s | | | | | | p95 ≤ 150, p99 ≤ 300 | |
| Serverless | Latest snapshot at 50/s | | | | | | p95 ≤ 150, p99 ≤ 300 | |
| Serverless | `listTables` at 50/s | | | | | | p95 ≤ 250, p99 ≤ 500 | |
| Serverless | Sustainable rate | | | | | | ≥ 50/s at concurrency 10 | |

Every step (observed): per operation p50 / p95 / p99, errors by status, the share sent on time, and whether the step was within every target.

| Form | Step (/s) | `getTable` p95 / p99 | Snapshot p95 / p99 | `listTables` p95 / p99 | Errors | On time | Within |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | 50, 100, 200, 400, 800 | | | | | | |
| Serverless | 25, 50, 100, 150 | | | | | | |

### 5.4 P3: per-table credentials

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | Misses (P3a) | | | | | | p95 ≤ 500, p99 ≤ 1,000 | |
| EKS | Hits (P3b) | | | | | | p95 ≤ 50, p99 ≤ 100 | |
| EKS | Errors | | | | | | 0 | |
| Serverless | Misses (P3a) | | | | | | p95 ≤ 1,000, p99 ≤ 2,000 | |
| Serverless | Hits (P3b) | | | | | | p95 ≤ 150, p99 ≤ 300 | |
| Serverless | Errors | | | | | | 0 | |

Requests by the class the audit gave them, and P3b's requests the server missed (observed): `<...>`.

### 5.5 P4a and P4b: cold starts and SnapStart restores (serverless)

| Run | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P4a | Errors: init timeouts, 5xx, 504 | | | | | | 0 in every sample | |
| P4a | Server `Init Duration` | | | | | | p95 ≤ 4,000, max ≤ 6,000 | |
| P4a | First request, client-observed | | | | | | p95 ≤ 7,000, max ≤ 10,000 | |
| P4a | Server's first invocation, authorizer's init and first invocation | | | | | | observed | |
| P4a | Requests after 30 minutes without traffic (6) | | | | | | observed: cold or not, times | |
| P4b | Errors: init timeouts, 5xx, 504 | | | | | | 0 in every sample | |
| P4b | Server `Restore Duration` | | | | | | p95 ≤ 1,500, max ≤ 2,000 | |
| P4b | First request, client-observed | | | | | | p95 ≤ 5,000, max ≤ 8,000 | |

The client's timeouts (section 2) against these: the client waits 3 minutes; the limits that bind are Lambda's 10 s for an init and the gateway's 29 s. Cold requests that also waited on a lock or on S3: `<0: GET /v1/config and getTable take neither>`.

### 5.6 P5: both forms under the same load

| Form | Metric | Samples | p50 | p95 | p99 | Max | Target | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EKS | Errors | | | | | | 0 | |
| EKS | Commit request | | | | | | P1a's | |
| EKS | `getTable`, snapshot, `listTables` | | | | | | P2's | |
| EKS | Credential hits | | | | | | P3's | |
| EKS | Credential misses | | | | | | observed (P3 judges misses) | |
| Serverless | Errors | | | | | | 0 | |
| Serverless | Commit request | | | | | | P1a's | |
| Serverless | `getTable`, snapshot, `listTables` | | | | | | P2's | |
| Serverless | Credential hits | | | | | | P3's | |
| Serverless | Credential misses | | | | | | observed (P3 judges misses) | |

The difference between the forms (reported, not judged): serverless / EKS of each p50, p95 and p99 (`result.json`, `extra.ratios`); the serverless form's cold starts in the window and their requests, apart; each database's peak connections by user and its CPU.

## 6. Raw data

Kept in the private, access-controlled evidence directory `<evidence>/<candidate>/us-west-2/perf/` (outside the repository; it holds no token or password, and its records name no account, address or ARN): one directory per run, `<scenario>[-<n>]/`, as the [harness's README](../../tests/performance/README.md#a-runs-evidence) lists, with `result.json`, `summary.md`, `environment.json`, `run.log` and each form's raw data (the load tool's records and the client's request log, the server's access log, the audit export, Lambda's `REPORT` lines and the API's access log, the database's CloudWatch metrics).

| Scenario | Run | Directory | Verdict |
| --- | --- | --- | --- |
| P1a | `<run>` | `<scenario>/` | |
| P1b | | | |
| P1c (observed) | | | `OBSERVED` |
| P2 | | | |
| P3 | | | |
| P4a | | | |
| P4b | | | |
| P5 | | | |

Each run's clean-up was read back (`result.json`, `cleanup`): `<every step PASS | ...>`.

## 7. Limitations

As terms.md section 7, and of this measurement:

- One Region (`us-west-2`), the default sizes, synthetic load from inside the test environment's VPC; clients in other VPCs or Regions, bigger nodes, more replicas, a bigger database or more Lambda concurrency are not measured.
- The metadata database is a burstable `db.t4g.small`: each scenario's lowest credit balance is in section 5; a run that spent its credits would measure the credits, not the catalog.
- STS, Cognito, API Gateway and Lambda account quotas are not measured.
- Lambda's idle reaping is not the test's to control: the idle requests of P4a are observed only. The judged cold starts are forced by new versions, as a deploy makes them.
- Engines' own work is not measured: a commit's latency here is the catalog's and the client's.
- The client's request line times an attempt from sending it to receiving the answer's headers; an operation's time includes reading the answer, the client's retries and, for open-loop requests, any wait after the time it was due. Where the two differ much (section 5), say by how much and where the time goes.
- `<anything of this run: a scenario rerun and why, a metric observed only, a step the load tool could not send on time>`.

## 8. Trademarks

> Apache, Apache Paimon, Paimon, Apache Flink and Flink are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. No endorsement by The Apache Software Foundation is implied by the use of these marks.

Colvenna Catalog is not an Apache Software Foundation project. AWS, Amazon EKS, AWS Lambda, Amazon API Gateway and Amazon RDS are trademarks of Amazon.com, Inc. or its affiliates.
