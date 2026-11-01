---
title: "Colvenna Catalog performance acceptance: scenarios, method and targets"
date: 2026-11-01
status: confirmed, 需求方已于 2026-10-29 确认；P1b 的负载于 2026-11-01 由需求方确认修订，目标不变（#83；无服务器形态的 40 分钟窗口由发布协调方推出）；P5 的凭证未命中于 2026-11-01 由需求方确认改为只报告，其延迟由 P3 判定，目标不变（#91）
issue: 19
---

# Performance acceptance terms

> **需求方已于 2026-10-29 确认 (confirmed by the requester on 2026-10-29); P1b 的负载于 2026-11-01 由需求方确认修订 (P1b's load revised, its targets unchanged, confirmed by the requester on 2026-11-01, #83; the serverless form's 40-minute window is the release coordinator's, section 4, P1); P5 的凭证未命中只报告，由需求方 2026-11-01 确认 (P5's credential misses reported, not judged, their latency judged by P3, confirmed by the requester on 2026-11-01, #91; section 4, P5).** The implementer proposed these terms and the requester confirmed them unchanged (design 9.3: "targets are proposed by the implementer and confirmed by the requester before they are written into the release manifest"). Each candidate's release manifest pins this document's version (`performance`), so the targets of section 4 are fixed for that candidate. A change to them is a change for the next candidate, and needs the requester's confirmation again. The performance acceptance runs in `us-west-2` only; UAT still runs in both Regions.

This document fixes, before any run, what the performance acceptance of a release candidate measures, how, and what it must meet: the scenarios of the [1.0 design](../design/v1.0-design.md) section 9.3, each with its environment, method, metrics and targets, and the reasons for each target. Each candidate's results go to `docs/performance/<candidate>.md` (#20), with the raw data and the limits, and give the candidate's performance verdict. Performance is not part of UAT ([UAT cases](../uat/cases.md), section 2): it is never counted in UAT's numbers or verdict, and taking it out of UAT does not waive it.

## 1. Verdict

A candidate's performance verdict is one of:

- `PASS`: every judged metric of every scenario was measured with enough samples (section 3) and met its target; no error where the target says none;
- `NOT_JUDGED`: nothing failed, but a judged metric had too few samples, so it was only observed;
- `FAIL`: a judged metric missed its target, an error occurred where none is allowed, or a result is missing.

Only `PASS` meets design 9.4. Observed-only metrics (marked "observed") are reported and never judged. Scenario P1c is observed as a whole: its run's result is `OBSERVED` (or `FAIL` when it did not complete or its clean-up failed), and a candidate's verdict is that of the other scenarios.

## 2. Common environment

| Item | Value |
| --- | --- |
| Where | This repository's test environment only (`tests/environment/up.sh`), in `us-west-2` only (the requester's decision; UAT runs in both Regions), while no UAT case or other test runs on it: after step 8 of the UAT's order and before step 11 ([UAT cases](../uat/cases.md), section 3.2), or on installs of its own made the same way |
| Candidate | Installed as UAT installs it: `install.sh --release <candidate>` from a clean checkout of its tag, default settings, per-table credentials on (`COLVENNA_DATA_TOKENS=aws-sts`) |
| EKS form | The UAT's E1 install: 2 replicas (requests 0.5 CPU and 1 GiB, limits 2 CPU and 2 GiB; main pool `COLVENNA_JDBC_POOL_SIZE` 16) on the test environment's two `t3.large` nodes |
| Serverless form | The UAT's S1 install: the server function at 2048 MB, x86_64, reserved concurrency 10 (`COLVENNA_LAMBDA_CONCURRENCY`), SnapStart off. For scenario P4b only, S1 is run again with `COLVENNA_SNAPSTART=on`, and back to `off` afterwards |
| Metadata database | Each form its own RDS PostgreSQL 16, `db.t4g.small`, single-AZ, as `COLVENNA_DB=create` makes it. Its CPU and burst credit balance are recorded through every run |
| Load | Pods in the test environment's cluster, in the same Region, running the Paimon 2.0.0 Java client (`RESTCatalog`, `token.provider=bear`, `data-token.enabled=true`). They reach the EKS form through its Service and the serverless form through the test environment's `execute-api` endpoint. The load tool is written for #20; its source is committed and its build pinned in the results |

The default sizes are what the acceptance is of; the results are not capacity limits. Bigger nodes, more replicas, a bigger database or more Lambda concurrency are not measured.

## 3. Method

- **Client-observed latency** of a REST request comes from the Paimon client's own log line, `[rest] requestId:<id> method:<m> url:<u> duration:<ms>ms` (`LoggingInterceptor` in `paimon-api` 2.0.0: from sending the request to receiving the response's headers, per attempt). The load tool logs it at INFO.
- **Server-side latency** comes from the server's access log, `colvenna.access - <id> <method> <path> <status> <ms>ms principal=<p> action=<a>` (EKS: the pods' logs; serverless: the server function's log group), joined to the client's line by `X-Request-Id`.
- **Operation latency** is timed by the load tool around one call of the Paimon API (a whole commit, a `getTable`), the client's own retries included.
- **Open and closed loops.** Reads and credential requests are sent open-loop at a fixed rate, and their latency is measured from the time each was due to be sent, so that a slow catalog cannot slow the load down and hide it. Commits are closed-loop: a writer commits when its previous commit returned, or at its schedule if later.
- **Percentiles** are nearest-rank over every sample of the window, warm-up excluded. A judged percentile needs at least 1,000 samples (scenario P4: at least 40); with fewer, it is observed only.
- **Errors**: an operation that fails to its caller, after the client's own retries, is an error. A commit refused on a stale base (`success: false`, which the client retries) is a conflict, counted, not an error.
- **Classification by the audit**: a credential request is a cache miss or hit as its `ISSUE_DATA_TOKEN` audit event says, joined by request id.
- **Raw data** for every run: the load tool's logs, the client's REST lines, the server's access log, the audit export, Lambda's `REPORT` lines, the database's CloudWatch metrics, and the environment record. They are kept in the private evidence directory; the document names where.

## 4. Scenarios and targets

### P1. Concurrent writers: commit throughput and latency

| | P1a: writers on separate tables | P1b: writers on one table | P1c: one table's capacity (observed) |
| --- | --- | --- | --- |
| Load | 8 writers, each its own unpartitioned append table; each commit one data file of 100 rows; commit back to back | 4 writers on one append table, each commit one data file of 100 rows. EKS: each writer commits every 4 s (1 commit/s offered); serverless: every 8 s (0.5 commits/s offered). Paimon's default `commit.max-retries` (10) | P1b's load before 2026-11-01: 4 writers on one append table, each committing every 2 s (2 commits/s offered, about one table's capacity with Paimon 2.0.0's client, B7); Paimon's defaults |
| Duration | 10 minutes after 1 minute of warm-up, each form | EKS: 20 minutes after 1 minute of warm-up; serverless: 40 minutes after 1 minute of warm-up. Each window offers 1,200 commits | 10 minutes after 1 minute of warm-up, each form |
| Metrics | Successful commits per second (all writers); client-observed latency of the `POST .../commit` request; server-side commit latency (observed); a whole commit's latency (observed); errors | Commits done of those offered; a whole commit's latency, conflict retries included; conflicts (observed); errors | All observed: commits done per second and of those offered, the failed ones, a whole commit's latency, conflicts |

**Targets (confirmed 2026-10-29; P1c's observed row added 2026-11-01):**

| Metric | EKS | Serverless |
| --- | --- | --- |
| P1a commits per second | ≥ 10 | ≥ 5 |
| P1a commit request p95 / p99 | ≤ 500 ms / ≤ 1,000 ms | ≤ 1,000 ms / ≤ 2,000 ms |
| P1a errors | 0 | 0 |
| P1b commits done of those offered | ≥ 99 %, none failed | ≥ 99 %, none failed |
| P1b whole commit p95 / p99 | ≤ 3 s / ≤ 6 s | ≤ 5 s / ≤ 10 s |
| P1c one table's capacity (2 commits/s offered) | observed | observed |

**Why.**

- The server-side commit on the EKS form, one engine at a time on this test environment (the matrix runs of #10, section 6, B1), was p50 126 ms, p95 226 ms, max 548 ms (n = 144). Twice that p95 leaves room for eight writers sharing two replicas and a `db.t4g.small`.
- The serverless form's one warm commit through the private API took 279 ms at the client (B2), and every request also crosses API Gateway and the cached authorizer. Hence twice the EKS form's latency targets and half its throughput.
- A Paimon commit makes 3 to 6 REST requests (credentials, snapshot loads, the commit; B1), and the client's own S3 writes come on top. Eight writers back to back should give some 15 to 25 commits per second on the EKS form and about 10 on the serverless form, so the targets should hold without saturating the catalog.
- What a user needs: a Flink job commits once per checkpoint (Colvenna Lake's default is 30 s, B4). 10 commits per second serve about 300 such jobs at once; 5 per second, about 150.
- P1b: on one table, writers race on the table's snapshot, and the loser commits again on the new base. Colvenna Lake's run with 6 writers 3 s apart on one table (B5) landed every commit: 720 commits, 720 stale-base refusals retried, no outcome unknown. Its latency was not recorded, so P1b's latency targets have the weakest basis here: they allow a commit one or two retries at P1a's latency.
- **P1b's load, revised (需求方 2026-11-01 确认: the offered load drops, the targets stay as confirmed on 2026-10-29).** The first measurement (`v1.0.0-rc.2`, 2 commits/s offered) and #1's investigation (B7) found that the Paimon client's commit protocol caps one table at about 2 commits/s. The bottleneck is the client, not the catalog: each time the catalog refuses a commit on a stale base, the client reloads the latest snapshot, reads its manifest lists, writes a new base manifest list, a delta manifest and a delta manifest list to S3, and commits again ([`FileStoreCommitImpl.tryCommit`, `tryCommitOnce`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/operation/FileStoreCommitImpl.java)), about 0.35 to 0.42 s an attempt on the EKS form, while the catalog decides a commit in about 20 ms and publishes one in about 85 ms. 2 commits/s offered is at or above that ceiling, so the writers fall behind without bound and a writer that keeps losing exhausts `commit.max-retries`: every failed commit of those runs did, after 11 refusals, with no server error. So P1b now offers about half of one table's capacity, where it measures the catalog and not the ceiling: 1 commit/s on the EKS form, and on the serverless form half of it, as P1a's serverless targets are half of EKS's (its attempts are slower through the API, B7). Each window offers 1,200 commits, so that the whole commit's p95 and p99 have the 1,000 samples section 3 requires to be judged. The EKS form's 20 minutes are the requester's; the serverless form's 40 minutes are not the requester's own figure: the release coordinator derived them on 2026-11-01 so that 0.5 commits/s offers 1,200 (20 minutes would offer 600, and its percentiles would never be judged). They change if the requester decides otherwise.
- P1c keeps the earlier 2 commits/s as a measurement of one table's capacity: reported, never judged, and not part of the candidate's verdict (section 1). What a user can tune when many writers commit to one table is in the [user manual](../manual/user.md#95-many-writers-on-one-table).

### P2. Metadata reads: latency and sustainable request rate

| Item | Value |
| --- | --- |
| Data | 20 databases of 50 tables (1,000 tables), each with one snapshot |
| Load | Open-loop mix: `getTable` 60 %, load the latest snapshot 30 %, `listTables` (a page of 100) 10 %, random tables, one reader principal with `READ` on the catalog |
| Steps | EKS: 50, 100, 200, 400 and 800 requests per second; serverless: 25, 50, 100 and 150. 5 minutes each, after 2 minutes of warm-up at the first step |
| Metrics | Per operation and step: client-observed p50, p95, p99; errors (5xx, timeouts, 503 from the in-flight limit). The sustainable rate is the highest step at which every operation is within its latency targets, errors are under 0.1 %, and at least 99 % of the offered requests were sent on time |

**Targets (confirmed 2026-10-29):**

| Metric | EKS | Serverless |
| --- | --- | --- |
| `getTable`, load snapshot p95 / p99, at the target rate | ≤ 50 ms / ≤ 100 ms | ≤ 150 ms / ≤ 300 ms |
| `listTables` p95 / p99, at the target rate | ≤ 100 ms / ≤ 250 ms | ≤ 250 ms / ≤ 500 ms |
| Sustainable rate | ≥ 200 requests per second | ≥ 50 requests per second at concurrency 10 |

**Why.**

- One engine at a time on the EKS form, server-side (B1): `GET_TABLE` p95 14 ms (max 19, n = 120), `LOAD_SNAPSHOT` p95 16 ms (n = 735), `LIST_TABLES` p95 9 ms (n = 90). Inside the cluster the TLS round trip adds a few milliseconds; the targets allow about three times that for 200 requests per second over two replicas.
- Serverless, warm, at the client through the private API (B2): load snapshot 76 ms, `listTables` 54 ms, single samples.
- With reserved concurrency 10 and about 80 ms per request, the form tops out near 10 / 0.08 s = 125 requests per second. 50 keeps a margin of two and a half; raising `COLVENNA_LAMBDA_CONCURRENCY` raises the ceiling and is not measured.
- What a user needs: engines read metadata when they plan a query or a commit, not per row. An external-table query in Doris, or a Flink checkpoint's commit, makes a few requests. 200 per second is ample for the default size; the higher steps show where its limit lies (observed).

### P3. Per-table credentials: issuance latency

| Item | Value |
| --- | --- |
| P3a, cache misses | 3 requests per second for 6 minutes, each for a (principal, table, access level) not issued before: 540 tables, two principals (one with `WRITE`, one with `READ`) |
| P3b, cache hits | 20 requests per second for 5 minutes over 20 tables already issued |
| Metrics | Client-observed latency of `GET .../tables/{table}/token`, misses and hits apart; errors (a 503 from STS or Cognito throttling is one) |

**Targets (confirmed 2026-10-29):**

| Metric | EKS | Serverless |
| --- | --- | --- |
| Miss p95 / p99 | ≤ 500 ms / ≤ 1,000 ms | ≤ 1,000 ms / ≤ 2,000 ms |
| Hit p95 / p99 | ≤ 50 ms / ≤ 100 ms | ≤ 150 ms / ≤ 300 ms |
| Errors | 0 | 0 |

**Why.**

- Server-side issuance on the EKS form (B1), misses and hits together: p50 5 ms, p95 53 ms, max 121 ms (n = 201). The slow ones are the misses, one `AssumeRoleWithWebIdentity` call each.
- The serverless form makes a Cognito `GetOpenIdTokenForDeveloperIdentity` call first: 165 ms from outside a VPC, 210 ms inside one (B2). Its targets are twice the EKS form's.
- A hit is a lookup in the replica's cache, as cheap as a metadata read, so it gets P2's targets.
- The server keeps a credential until less than 70 minutes of it remain, so a busy client misses about once per table and principal in each `COLVENNA_DATA_TOKEN_DURATION` minus 70 minutes. Three misses per second is well beyond that, yet stays clear of the STS and Cognito request quotas (not measured).

### P4. Serverless cold start and SnapStart restore

| | P4a: on-demand init (the default) | P4b: SnapStart (`COLVENNA_SNAPSTART=on`) |
| --- | --- | --- |
| Load | Before each round, force new execution environments: publish a new version of the server function with an environment variable of the test's own changed and move the alias `live` to it, as a deploy does; then 5 authenticated requests at once (`GET /v1/config`, then `getTable`) through the private API. 8 rounds: at least 40 cold samples. Besides, 6 requests each after at least 30 minutes without traffic (observed: whether it was cold, and its times) | The same procedure, with S1 run again with `COLVENNA_SNAPSTART=on`: at least 40 restores |
| Metrics | The server function's `Init Duration` (Lambda's `REPORT` line), its first invocation's `Duration`, the authorizer's init and first invocation (observed), the client-observed latency of the first request, its status, and init timeouts | `Restore Duration` instead of `Init Duration`; the rest the same |

**Targets (confirmed 2026-10-29):**

| Metric | P4a on-demand | P4b SnapStart |
| --- | --- | --- |
| Errors: init timeouts, 5xx, 504 | 0 in every sample | 0 in every sample |
| Server `Init Duration` (P4a) or `Restore Duration` (P4b) p95 / max | ≤ 4 s / ≤ 6 s | ≤ 1.5 s / ≤ 2 s |
| First request, client-observed, server and authorizer cold, p95 / max | ≤ 7 s / ≤ 10 s | ≤ 5 s / ≤ 8 s |

**Why.**

- Measured by #13's live test (B3), x86_64 at 2048 MB, as installed: `Init Duration` 2,100 to 2,353 ms (n = 4, after the switch to RDS IAM authentication; 2,124 to 2,269 ms before, n = 5); the first request 1,669 to 2,494 ms, because it opens the database connections; the authorizer's init 120 to 173 ms and its first request 513 to 627 ms. Together, about 4.5 to 5.7 s at the client.
- SnapStart (B3): `Restore Duration` 510 to 656 ms (n = 6); the first request 1,888 to 2,156 ms.
- The spike (B2), at 2048 MB on arm64, with migrations still at init: init 2,078 to 3,519 ms (n = 15), 2.6 to 4.9 s at the client. Once, the first start of a new image hit Lambda's 10 s init limit; P4a's 6 s maximum is meant to catch that.
- The p95 targets leave about a quarter above the measured highs. The maxima stay well inside the limits below.

**The client's timeouts (design 9.3: a cold start must fit within them).** Read in the Paimon client's sources (B6):

- **Paimon 2.0.0 and 2.1.0.** `HttpClientUtils.createBuilder` sets a response timeout of 3 minutes and a connection-request (pool) timeout of 3 minutes. The connect timeout is HttpClient 5.5.1's default, also 3 minutes (read from the shaded `ConnectionConfig` class of 2.0.0's and 2.1.0's jars).
- **Retries.** `ExponentialHttpRequestRetryStrategy(5)` retries up to 5 times. It retries 429 and 503 answers whatever the method, after 1, 2, 4, 8 and 16 s (plus up to 10 % jitter, or `Retry-After`): about 31 s in all. It retries I/O errors only for idempotent methods, and never a timeout, a refused connection, an unknown host or a TLS error. A 504 is not retried.
- **Paimon 1.4.2 and 1.3.1**, which Doris 4.1.5 and StarRocks 4.1.6 bundle, set the same 3 minutes, the same retry strategy and the same 5 retries.

So the client waits 3 minutes, and a 2 to 6 s cold start is far inside that. The limits that bind are shorter, and on AWS's side:

- Lambda's 10 s for an init.
- API Gateway's 29 s integration timeout. The gateway then answers 504, and the client does not retry it.
- The server's own waits. In the Lambda form these are 10 s to get a connection, a 3 s lock wait and 15 s for an S3 request through S3A: 28 s, checked at start to stay under 29 s (LLD "无服务器形态"). They are sized for a warm environment, and a cold start uses up part of the same 29 s. A 10 s cold-start maximum keeps a cold request inside the gateway's limit for every request that does not wait on a lock or a slow S3 call. The results must report how many cold requests also waited on a lock or on S3.

### P5. EKS and serverless under the same load

| Item | Value |
| --- | --- |
| Load | The same, at once, on both forms, in the same window: 4 writers on tables of their own committing every 2 s (2 commits per second); P2's read mix at 50 requests per second; credential requests at 2 per second (mostly hits, with 10 % misses); 30 minutes after warm-up |
| Metrics | Per operation and form: p50, p95, p99, and the ratio of serverless to EKS (observed); errors; the serverless form's cold starts in the window (observed, their requests reported apart); the database's peak connections by user and its CPU (observed) |

**Targets (confirmed 2026-10-29; the credential misses' row 2026-11-01):**

| Metric | Target |
| --- | --- |
| Errors | 0 in both forms |
| Each form's latencies at this load | Within that form's targets of P1a (commit request), P2 (reads) and P3 (credential hits) |
| The credential misses' latency | Reported, not judged: fewer than 1,000 at this load; P3 judges it |
| The difference between the forms | Reported, not judged |

**Why.** The two forms differ by design. Every serverless request crosses API Gateway, the authorizer and Lambda; every EKS request is a TLS call inside the cluster. So a target on the difference would only restate that. What is judged is that both carry the same moderate load with no error and within their own targets. The comparison serves the user manual and the cost estimate (#16), which cite it.

**The credential misses, revised (需求方 2026-11-01 确认: reported, not judged; the targets unchanged; #91).** At this load a form makes about 360 credential misses (10 % of 2 requests a second for 30 minutes; `v1.0.0-rc.2` measured 384 on the EKS form and 546 on the serverless form, whose execution environments each keep a cache of their own), fewer than the 1,000 samples a judged percentile needs (section 3). Judged, that row could only be `NOT_JUDGED`, and by section 1 no candidate's verdict could then be `PASS`. So the misses' latency is reported here and judged by P3, whose P3a makes 1,080 misses in each form. Every other part of P5 gives 1,000 samples or more at this load (about 3,600 commits, 90,000 reads and 3,240 credential hits a form) and is judged by its form's targets as before, and "0 in both forms" counts every request, the misses among them.

## 5. What the manifest records

Each candidate's release manifest pins this document as `performance`: its path, the commit that last changed it up to the commit the candidate is built from, and its blob (`scripts/release/manifest.py`, written by `make-manifest.sh`). A manifest whose `performance` is absent is valid, but not one for GO. The targets are the ones of section 4 at that version. The document carries no version number of its own: that commit and blob are its version, so a candidate built after the revisions of 2026-11-01 pins the revised P1b (#83) and P5 (#91), and `v1.0.0-rc.2`'s manifest still pins the terms it was judged by.

## 6. The evidence the targets rest on

| | What | Where | Conditions |
| --- | --- | --- | --- |
| B1 | Server-side latencies of the EKS form, one engine at a time | The compatibility matrix's runs of #10 (branch `codex/issue-10-matrix` at `de4cdd0`, `docs/compat/runs/20261028T120119Z-c93a`, `20261028T122104Z-9305`, `20261028T122711Z-4166`, `raw/*/catalog-access.log`), joined by request id: `COMMIT` n = 144, p50 126 / p95 226 / max 548 ms; `LOAD_SNAPSHOT` (200) n = 735, 7 / 16 / 72; `ISSUE_DATA_TOKEN` n = 201, 5 / 53 / 121; `GET_TABLE` (200) n = 120, 4 / 14 / 19; `LIST_TABLES` n = 90, 5 / 9 / 14; `CREATE_TABLE` n = 36, 298 / 356 / 382 | 2026-10-28, `us-west-2`, this repository's test environment (EKS 1.36), the server from source at `d988467`, 2 replicas, `db.t4g.small`; no concurrent load |
| B2 | The serverless spike | [Its report](../research/serverless-spike.md), sections 1 and 4; `spikes/serverless/results/crud-private-api.json` (warm: load snapshot 76 ms, commit 279 ms, `listTables` 54 ms at the client), `coldstart-*.json`, `per-table-credentials.json` | 2026-10-28, `us-west-2`, arm64 at 2048 MB, `db.t4g.micro`, migrations at init; single samples for the warm requests |
| B3 | #13's live test | The [LLD](../design/colvenna-catalog-lld.md), "无服务器形态", "实测" | 2026-10-28, `us-west-2`, x86_64 at 2048 MB, the form as installed |
| B4 | Colvenna Lake's measured paths through this catalog | Colvenna Lake's v1.0.0-rc.5 performance acceptance (its `docs/performance/v1.0.0-rc.5.md` at commit `50dae8755e758272a94d5b195e15e1a61713b569`), section 5.1: MySQL → ODS p95 32.1 s and Kafka → ODS p95 30 s with 30 s checkpoints, every commit through the catalog; Paimon → Doris external table p95 6.93 s | 2026-10-28, its default size, the catalog inside Colvenna Lake, 2 replicas, `db.t4g.small`. The paths include the engines' own work, so they bound the catalog's part only loosely; they show its commits add nothing visible at a checkpoint's scale |
| B5 | Colvenna Lake's commits of 6 writers on one table | Its M2 commit recovery report (`docs/history/m2/commit-recovery.md` at the same commit), "结果": 720 commits all applied once, 720 stale-base refusals retried, 12 first attempts failed during injected faults and resolved | 2026-10-13, EKS, 2 replicas, with faults injected; outcomes recorded, latencies not |
| B6 | The Paimon client's timeouts and retries | `paimon-api` 2.0.0 sources (the local Maven repository, SHA-1 `ddba49212de45157bb13fe0e1b3ac65b6287278f`), and 2.1.0, 1.4.2 and 1.3.1 sources from Maven Central (SHA-1 `6395b58229e6e11368311ca5efdb4aa5cf9c0b32`, `bc9d87bbcd69a186ebf310863ba1a41825449bb7`, `496d96663401508876a05c217e9fe36148283a41`, each matching Central's `.sha1`): `org/apache/paimon/rest/HttpClientUtils.java`, `ExponentialHttpRequestRetryStrategy.java` | Read 2026-10-28 |
| B7 | One table's commit capacity with Paimon 2.0.0's client: #1's investigation of `v1.0.0-rc.2`'s P1b | #1 (its record is private: account, buckets and addresses). EKS, one table, 4 writers: every 2 s (2 commits/s offered) 88.6 % done, 39 failed, whole commit p50 / p95 / p99 1.49 / 2.30 / 3.50 s (10 minutes); with #79's client default 96.6 % done, 33 failed; every 3 s (1.33/s) 99.8 % done, 1 failed (5 minutes), with #79 100 %, none; every 4 s (1 commit/s) 600 of 600 done, none failed, 0.66 / 1.39 / 2.30 s (10 minutes); every 8 s (0.5/s) all done, 0.70 / 0.82 / 1.47 s (5 minutes); back to back 1.77 commits/s, with #79 2.12 (5 minutes, 20 and 22 failed). An attempt, from loading the snapshot to the catalog's decision, 420 ms on average when refused (346 ms with #79), of which the catalog about 17 ms; a success holds the table's lock about 86 ms. Every failed commit made 11 commit requests, each refused on a stale base, then `Commit failed after ... with 10 retries`. 4 writers back to back: whole commit p99 3.63 s (default); every 2 s with #79: p99 4.48 s. A winner's snapshot expiry after its commit, inside its commit call (`snapshot.expire.execution-mode=sync`, the default): 151 ms on average. Modelled, not measured: `commit.max-retries` 30 at 2 commits/s offered fails none but does 77 %, p99 about 40 s. Serverless, `v1.0.0-rc.2`'s P1b: 719 commits done in 600 s, about 1.2 a second (`docs/performance/v1.0.0-rc.2.md`); its attempts 577 ms on average, against the EKS form's 525 ms on the same build (#1) | 2026-10-31, `us-west-2`, this repository's test environment (EKS 1.36), the EKS form from source at `f9c4dee` (with #37), defaults; Paimon 2.0.0's client as the harness runs it; serverless from `v1.0.0-rc.2`'s acceptance (`docs/performance/v1.0.0-rc.2.md`) |

## 7. Limits of the acceptance

- One Region (`us-west-2`), the default sizes, synthetic load from inside the same VPC; clients in other VPCs or Regions are not measured.
- The database is a burstable `db.t4g.small`: a run long enough to spend its CPU credits would measure the credits, not the catalog. The runs are short, and the credit balance is recorded.
- STS and Cognito request quotas, and API Gateway and Lambda account quotas, are not measured.
- Lambda's idle reaping is not under the test's control, so idle cold starts (P4a) are observed only. The judged samples force new environments; afterwards the alias `live` is moved back to the installed version and the test's versions are deleted.
- Engines' own work is not measured: a commit's latency here is the catalog's and the client's, not a Flink checkpoint's.
