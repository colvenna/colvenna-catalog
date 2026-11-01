---
title: "Colvenna Catalog v1.0.0-rc.4: UAT report"
date: 2026-11-01
status: final
issue: 2
---

# Colvenna Catalog `v1.0.0-rc.4`: UAT report

The complete UAT report of the release candidate `v1.0.0-rc.4` ([1.0 design](../../../design/v1.0-design.md), section 9.2; [UAT cases](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/uat/cases.md) at the version the candidate pins). Raw evidence is private, on the execution host, in `<evidence>`, a private, access-controlled directory; this report names where each piece is and copies none of it. Nothing here holds an account ID, an ARN, an address, a token or a password.

`v1.0.0-rc.3` was never a candidate: its image and chart were pushed into the release registry from `5601257` on 2026-11-01, before the requester's decision on P5's credential misses ([#91](https://github.com/colvenna/colvenna-catalog/issues/91)) changed the performance terms, which a candidate's B must carry. Its name is spent and its artifacts stay in the registry untouched; no tag, install or acceptance was made of it ([#2](https://github.com/colvenna/colvenna-catalog/issues/2)). The private evidence directory of this acceptance keeps the name first planned, `rc3`.

## 1. Result

**UAT: passed.** All 134 required runs (34 cases; two Regions; both forms but UAT-DEP-11, EKS only) count `PASS`: 0 `FAIL`, 0 `BLOCKED`, 0 `NOT_RUN`. Three test defects failed first runs; each was fixed on `main` (harness only) and its retest on this candidate passed (section 6). No product defect failed a case. The UAT item of the release check (design 9.4) is met.

| Region | Form | Required | PASS | FAIL | BLOCKED | NOT_RUN |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `us-west-2` | EKS | 34 | 34 | 0 | 0 | 0 |
| `us-west-2` | serverless | 33 | 33 | 0 | 0 | 0 |
| `us-east-2` | EKS | 34 | 34 | 0 | 0 | 0 |
| `us-east-2` | serverless | 33 | 33 | 0 | 0 | 0 |
| `us-west-2` | both | 67 | 67 | 0 | 0 | 0 |
| `us-east-2` | both | 67 | 67 | 0 | 0 | 0 |
| both | both | 134 | 134 | 0 | 0 | 0 |

No required case counts other than `PASS`. The first runs that failed, each a test defect retested on the same candidate after its fix (section 6):

- UAT-DEP-05, EKS, both Regions: `release-not-the-tag` and `nothing-made` ([#95](https://github.com/colvenna/colvenna-catalog/issues/95)); `UAT-DEP-05-2` `PASS` in both.
- UAT-DEP-02, EKS, `us-west-2`: `existing-untouched` ([#98](https://github.com/colvenna/colvenna-catalog/issues/98)); `UAT-DEP-02-2` `PASS`, with E3's `UAT-DEP-09-2`.
- UAT-DEP-09, EKS, `us-east-2` (E1, `UAT-DEP-09-2`): `others-untouched` ([#100](https://github.com/colvenna/colvenna-catalog/issues/100)); `UAT-DEP-09-4` `PASS` (the EKS form's runs of a case share one numbering: `UAT-DEP-09-3` is E2's).

Performance is not part of UAT (cases.md section 2): it is [the performance document](../../../performance/v1.0.0-rc.4.md).

## 2. The candidate

| Item | Value |
| --- | --- |
| Candidate | `v1.0.0-rc.4`, an annotated tag (object `b4e391275ab4df7263f70ca4aec7da4603bd0215`) at `5a11f1518cea38f61338f9d8891c29a4fc480533` (C, the merge of [#93](https://github.com/colvenna/colvenna-catalog/pull/93)) |
| Built from | `77984fb24823acc53e4c1e2d00747d494961552d` (B, the merge of [#92](https://github.com/colvenna/colvenna-catalog/pull/92)); `git diff --name-only B C`: `release/v1.0.0-rc.4/manifest.json` and `notes.md` only |
| Manifest | [`release/v1.0.0-rc.4/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/release/v1.0.0-rc.4/manifest.json): forms `eks`, `serverless`; Regions `us-west-2`, `us-east-2`; 34 required cases |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68` (`linux/amd64`, labelled with B); every install of the candidate ran this digest, copied by digest into its own repository (UAT-DEP-01, 02, 06 `runs-the-candidate`) |
| Chart | `colvenna-catalog-release/charts/colvenna-catalog@sha256:06b5328572d2bc80069a0f7ebd164c5e01f331317d20cf6a376d05e4e53e4449`, version `1.0.0-rc.4`, tree `f8d49690a9e0ae60e655d4f2a9167f19bef9ebc9` |
| `install.sh`, `uninstall.sh`, `deploy/lib`, CDK app | Blobs and trees as the manifest pins them (`97988627`, `d261a4c2`, `1cc61b38`, `7ae331a5`); every install and uninstall of the candidate ran from a clean checkout of the tag |
| UAT cases | `docs/uat/cases.md` at `9dd5d05c1b729f30c445d83ec17dc4e168c7eaf2` (blob `00ccb1e17a671a3ebd093b54eea36a03d21c226b`), the manifest's `uat_cases`; `tests/compat/declared.tsv` and `cells.json` at the tag |
| Harness | `tests/uat/` and `tests/compat/` at the tag (clean checkouts, one per install), except the runs after the three test defects, which ran the harness at the merge of their fix from a checkout of their own: UAT-DEP-05 (E1's retests and S1's first runs, `fd92b57`), E3 `us-west-2`'s UAT-DEP-02-2 and 09-2 (`bfe991d`), E1 `us-east-2`'s UAT-DEP-09-4 (`59d1a39`) (section 6; each run's `harnessCommit` in its `result.json`); `install.sh` and `uninstall.sh` always from the install's own checkout of the tag (`--installer-checkout`) |
| Executed | 2026-11-01, 02:41 UTC (the first test environment) to 18:09 UTC (the last run's end; the test environments were then torn down, `r3w2` after the release's smoke install) |

## 3. Environments

| Item | Value |
| --- | --- |
| Account | `<ACCOUNT_ID>`, the maintainer's; the release registry `ColvennaCatalogRelease` in `us-west-2` (the installs read it by IAM) |
| Regions | `us-west-2` and `us-east-2`, at once. `us-east-2`'s VPC quota was raised from 6 to 8 for the window (approved by Service Quotas at once), so that its test environment and S1's network could exist together |
| Test environments | `r3w2` (`us-west-2`) and `r3e2` (`us-east-2`), each `tests/environment/up.sh` at B: EKS 1.36 (`eks.17`), the catalogs' two `t3.large` nodes, the clients' `t3.xlarge` nodes (raised by hand to 4 right after `up.sh`, before any install, and kept so to the end, as the test environment's README allows), NetworkPolicy enforced, the AWS Load Balancer Controller, the `execute-api` endpoint, the existing PostgreSQL 16 (`db.t4g.small`) and bucket, the client subnets; `verify.sh` passed at the end of each `up.sh` (02:41 to 03:02 UTC) |
| Installs | Section 3.1 below |
| Metadata databases | RDS PostgreSQL 16, `db.t4g.small`, single-AZ, encrypted, TLS only, IAM database authentication on (E1, S1, E2, S2 made by `COLVENNA_DB=create`; E3 and S3 in the test environment's own) |
| Execution host | The maintainer's host, its `Patch Group` tag kept off; the test nodes' tags removed by a watcher every 10 minutes (`<evidence>/patch-tag-watch.log`) and put back on the nodes still running at the end (`<evidence>/patch-tag-restore.txt`); Python 3.12, AWS CLI 2, JDK 21, Maven 3.8, Docker 29.1, and kubectl, Helm and crane as `scripts/install-tools.sh` pins them |
| The AWS CLI's Region | Every command named `AWS_PROFILE` explicitly, and every run set `AWS_REGION` to its install's Region |

### 3.1 The installs

| Role (cases.md 3.1) | `us-west-2` | `us-east-2` | Made by | Configuration |
| --- | --- | --- | --- | --- |
| E1: EKS, new resources | `ekw2` | `eke2` | UAT-DEP-01 | `COLVENNA_DB=create`, a new bucket, the defaults (namespace `colvenna-catalog`); from UAT-DEP-10 on, the restored instance (`<env>-restored.env`) |
| S1: serverless, new network | `slw2` | `sle2` | UAT-DEP-01 | `COLVENNA_NETWORK=create`, `COLVENNA_DB=create`, the test environment's `execute-api` endpoint listed; from UAT-DEP-10 on, the restored instance |
| E3: EKS, existing resources | `exw2` | `exe2` | UAT-DEP-02 | `COLVENNA_DB=existing` (the test environment's PostgreSQL, database and user `ex<r>_catalog`), its bucket (prefix `ex<r>`), a namespace, release, service account and client label of its own |
| S3: serverless, existing network | `sxw2` | `sxe2` | UAT-DEP-02 | `COLVENNA_NETWORK=existing` (the test environment's VPC, its two private subnets, its `execute-api` endpoint), `COLVENNA_DB=existing` (`sx<r>_catalog`), the bucket (prefix `sx<r>`) |
| E2: EKS, upgraded | `euw2` | `eue2` | `v1.0.0-rc.2`'s own `install.sh --release v1.0.0-rc.2` from a clean checkout of that tag, then UAT-DEP-06 | `COLVENNA_DB=create`, names of its own |
| S2: serverless, upgraded | `suw2` | `sue2` | The same, from `v1.0.0-rc.2` | `COLVENNA_NETWORK=existing` in the test environment's VPC (its private subnets and `execute-api` endpoint), `COLVENNA_DB=create`, a new bucket |

The previous release is `v1.0.0-rc.2`, the newest earlier candidate whose manifest names both forms (cases.md UAT-DEP-06). S2 is in the test environment's VPC, not in a network of its own: `us-east-2` had one VPC to spare when the window was planned, and `us-west-2`'s gateway endpoints had been full the day before. The cases give S2 no network mode of their own (S1's is `create`, S3's `existing`), and with the same schema in both releases UAT-DEP-07's way back does not move it.

## 4. Execution

The order of cases.md section 3.2, in both Regions at once, with these differences:

- **UAT-DEP-05** (step 3) met a test defect on E1 ([#95](https://github.com/colvenna/colvenna-catalog/issues/95)); S1's run was held until the fix, and both ran after step 4, E1's as the retest (`UAT-DEP-05-2`), with E1 and S1 idle.
- **Step 4** ran as three streams per Region: the compatibility matrix (the EKS form's run, then the serverless form's: one `tests/compat/matrix.py run` each with all eight cells, the cases UAT-CMP-01 to 08 being its cells), and E1's and S1's catalog and security cases, one at a time each.
- **E3 `us-west-2`**'s retest of UAT-DEP-02 and 09 after a test defect ([#98](https://github.com/colvenna/colvenna-catalog/issues/98)) ran after its step 9 and before its E2 was made (step 10 held meanwhile), so that the cluster never held more than two EKS installs.
- **One serverless `install.sh` at a time in a Region.** The candidate's `install.sh` fails when API Gateway throttles its stage redeployment ([#94](https://github.com/colvenna/colvenna-catalog/issues/94), section 6), which two installs at once in one Region did to a performance install. From then on every case that runs `install.sh` on a serverless install, and the performance acceptance's own, took the Region's lock, so that this acceptance made no such contention itself; S1's and S2's UAT-DEP-08, which may run at once, ran one after the other. In `us-west-2` the performance acceptance's P4b held that lock through its idle samples, in which it runs no `install.sh`; S1's and S2's UAT-DEP-08 and 09 there then ran outside it, one `install.sh` at a time, beside E1's and E2's UAT-DEP-09.
- **UAT-DEP-07** took the same-schema branch in every run: `v1.0.0-rc.2` and the candidate have the same migrations (`V1` to `V4`). Its steps say "which branch runs depends on whether the candidate added migrations to the previous release's", and its expected result is "for the branch that ran, its checks; then `forward-again`": the newer-schema branch's checks (`newer-schema-refused`, `-nothing-written`, `-restored`) are not this candidate's to run, and each run lists the same-schema checks and `forward-again` alone. That branch, with [#63](https://github.com/colvenna/colvenna-catalog/issues/63)'s fix, ran live before the candidate, on trial builds (2026-10-31), not on it.
- **No extra run of UAT-DEP-06 or 07** ([#55](https://github.com/colvenna/colvenna-catalog/issues/55)): no check of theirs failed, on the previous release's account or any other.

What happened in the execution that is not a result (each in the private ledger, `<evidence>/ledger.md`; none of them a run of a case):

- 04:35 to 04:47 UTC: UAT-DEP-05's `release-not-the-tag` installed instead of refusing (#95): the operator stopped both installs in their stacks step (SIGTERM to their process groups) and removed what they had made with `uninstall.sh --delete-data` from clean checkouts of the tag, the refusal's configuration given Kubernetes names of its own so that it could not reach E1's (`verify-clean.sh`: nothing left).
- 10:28 UTC: a chain that was to start step 9 in `us-east-2` stopped at its own gate, which had read `deploy/config/example.env`; the gate was fixed and the chain started again at 10:32.
- 14:51 UTC: S1's and S2's UAT-DEP-08 in `us-west-2` were waiting for the Region's install lock, held by P4b; the waits were stopped before either case started (two `NO-RESULT` lines in the private run log, not runs), and both ran outside the lock afterwards.

## 5. Results by case

### `us-west-2`, EKS form

| Case | Result | Runs (install: run, UTC on 2026-11-01, or on the 2nd marked 02, result) | Evidence | Defect, retest |
| --- | --- | --- | --- | --- |
| UAT-CMP-01 | **PASS** | UAT-CMP-01 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-01/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-02 | **PASS** | UAT-CMP-02 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-02/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-03 | **PASS** | UAT-CMP-03 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-03/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-04 | **PASS** | UAT-CMP-04 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-04/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-05 | **PASS** | UAT-CMP-05 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-05/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-06 | **PASS** | UAT-CMP-06 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-06/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-07 | **PASS** | UAT-CMP-07 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-07/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CMP-08 | **PASS** | UAT-CMP-08 04:53-05:13 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-08/ (matrix run 20261101T045324Z-9e70)` | - |
| UAT-CAT-01 | **PASS** | UAT-CAT-01 04:52-05:02 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-01/` | - |
| UAT-CAT-02 | **PASS** | UAT-CAT-02 05:02-05:11 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-02/` | - |
| UAT-CAT-03 | **PASS** | UAT-CAT-03 08:39-10:32 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-03/` | - |
| UAT-CAT-04 | **PASS** | UAT-CAT-04 05:11-05:17 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-04/` | - |
| UAT-CAT-05 | **PASS** | UAT-CAT-05 05:23-05:28 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-05/` | - |
| UAT-CAT-06 | **PASS** | UAT-CAT-06 05:28-05:33 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-06/` | - |
| UAT-CAT-07 | **PASS** | UAT-CAT-07 05:33-05:37 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-07/` | - |
| UAT-CAT-08 | **PASS** | UAT-CAT-08 05:37-05:41 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-CAT-08/` | - |
| UAT-SEC-01 | **PASS** | UAT-SEC-01 05:41-05:44 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-SEC-01/` | - |
| UAT-SEC-02 | **PASS** | UAT-SEC-02 05:44-05:46 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-SEC-02/` | - |
| UAT-SEC-03 | **PASS** | UAT-SEC-03 05:46-05:50 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-SEC-03/` | - |
| UAT-SEC-04 | **PASS** | UAT-SEC-04 05:50-05:54 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-SEC-04/` | - |
| UAT-DEP-01 | **PASS** | E1: UAT-DEP-01 03:49-04:09 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-01/` | - |
| UAT-DEP-02 | **PASS** | E3: UAT-DEP-02 10:32-10:42 FAIL; E3: UAT-DEP-02-2 12:22-12:31 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-02/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-02-2/` | UAT-DEP-02: harness: [#98](https://github.com/colvenna/colvenna-catalog/issues/98): `existing-untouched` compared the existing instance's status, which RDS's backup in the instance's backup window had turned to `backing-up` (RDS's events: backing up 10:36 to 10:40 UTC); every other check PASS. E3 was uninstalled by its UAT-DEP-09, then made again for the retest |
| UAT-DEP-03 | **PASS** | E1: UAT-DEP-03 06:16-06:23 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-03/` | - |
| UAT-DEP-04 | **PASS** | E1: UAT-DEP-04 06:23-06:37 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-04/` | - |
| UAT-DEP-05 | **PASS** | E1: UAT-DEP-05 04:21-04:51 FAIL; E1: UAT-DEP-05-2 06:01-06:16 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-05/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-05-2/` | UAT-DEP-05: harness: [#95](https://github.com/colvenna/colvenna-catalog/issues/95): `release-not-the-tag`'s checkout of `main` was `origin/main`, the tag's own commit right after the tag, so `install.sh --release` began a real install of the refusal's name (its Storage stack made; stopped by the operator in the stacks step, E1 untouched; removed with `uninstall.sh --delete-data`, `verify-clean.sh` clean) and `nothing-made` found it; every other check PASS |
| UAT-DEP-06 | **PASS** | E2: UAT-DEP-06 12:52-13:17 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-06/` | - |
| UAT-DEP-07 | **PASS** | E2: UAT-DEP-07 13:17-13:57 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-07/` | - |
| UAT-DEP-08 | **PASS** | E2: UAT-DEP-08 14:28-14:40 PASS; E1: UAT-DEP-08-2 14:28-14:40 PASS; E1 PASS, E2 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-08/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-08-2/` | - |
| UAT-DEP-09 | **PASS** | E3: UAT-DEP-09 11:35-11:40 PASS; E3: UAT-DEP-09-2 12:31-12:37 PASS; E1: UAT-DEP-09-3 14:51-14:58 PASS; E2: UAT-DEP-09-4 14:51-15:04 PASS; E1 PASS, E2 PASS, E3 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-09/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-09-2/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-09-3/`; `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-09-4/` | - |
| UAT-DEP-10 | **PASS** | UAT-DEP-10 07:19-07:54 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-10/` | - |
| UAT-DEP-11 | **PASS** | UAT-DEP-11 10:51-11:35 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-DEP-11/` | - |
| UAT-FLT-01 | **PASS** | UAT-FLT-01 06:37-06:53 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-FLT-01/` | - |
| UAT-FLT-02 | **PASS** | UAT-FLT-02 06:53-07:06 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-FLT-02/` | - |
| UAT-FLT-03 | **PASS** | UAT-FLT-03 07:06-07:10 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/eks/UAT-FLT-03/` | - |

### `us-west-2`, serverless form

| Case | Result | Runs (install: run, UTC on 2026-11-01, or on the 2nd marked 02, result) | Evidence | Defect, retest |
| --- | --- | --- | --- | --- |
| UAT-CMP-01 | **PASS** | UAT-CMP-01 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-01/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-02 | **PASS** | UAT-CMP-02 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-02/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-03 | **PASS** | UAT-CMP-03 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-03/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-04 | **PASS** | UAT-CMP-04 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-04/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-05 | **PASS** | UAT-CMP-05 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-05/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-06 | **PASS** | UAT-CMP-06 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-06/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-07 | **PASS** | UAT-CMP-07 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-07/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CMP-08 | **PASS** | UAT-CMP-08 05:15-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CMP/cmp-c2611010514/UAT-CMP-08/ (matrix run 20261101T051511Z-6acf)` | - |
| UAT-CAT-01 | **PASS** | UAT-CAT-01 04:52-05:03 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-01/` | - |
| UAT-CAT-02 | **PASS** | UAT-CAT-02 05:03-05:12 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-02/` | - |
| UAT-CAT-03 | **PASS** | UAT-CAT-03 08:36-10:28 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-03/` | - |
| UAT-CAT-04 | **PASS** | UAT-CAT-04 05:12-05:18 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-04/` | - |
| UAT-CAT-05 | **PASS** | UAT-CAT-05 05:19-05:26 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-05/` | - |
| UAT-CAT-06 | **PASS** | UAT-CAT-06 05:26-05:32 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-06/` | - |
| UAT-CAT-07 | **PASS** | UAT-CAT-07 05:32-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-07/` | - |
| UAT-CAT-08 | **PASS** | UAT-CAT-08 05:40-05:45 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-CAT-08/` | - |
| UAT-SEC-01 | **PASS** | UAT-SEC-01 05:45-05:49 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-SEC-01/` | - |
| UAT-SEC-02 | **PASS** | UAT-SEC-02 05:49-05:51 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-SEC-02/` | - |
| UAT-SEC-03 | **PASS** | UAT-SEC-03 05:51-05:57 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-SEC-03/` | - |
| UAT-SEC-04 | **PASS** | UAT-SEC-04 05:57-06:00 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-SEC-04/` | - |
| UAT-DEP-01 | **PASS** | S1: UAT-DEP-01 03:49-04:20 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-01/` | - |
| UAT-DEP-02 | **PASS** | S3: UAT-DEP-02 10:32-10:51 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-02/` | - |
| UAT-DEP-03 | **PASS** | S1: UAT-DEP-03 06:16-06:23 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-03/` | - |
| UAT-DEP-04 | **PASS** | S1: UAT-DEP-04 06:23-06:36 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-04/` | - |
| UAT-DEP-05 | **PASS** | S1: UAT-DEP-05 06:01-06:06 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-05/` | - |
| UAT-DEP-06 | **PASS** | S2: UAT-DEP-06 12:57-13:31 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-06/` | - |
| UAT-DEP-07 | **PASS** | S2: UAT-DEP-07 13:32-14:27 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-07/` | - |
| UAT-DEP-08 | **PASS** | S1: UAT-DEP-08 14:52-16:17 PASS; S2: UAT-DEP-08-2 16:17-17:41 PASS; S1 PASS, S2 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-08/`; `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-08-2/` | - |
| UAT-DEP-09 | **PASS** | S3: UAT-DEP-09 11:35-12:21 PASS; S1: UAT-DEP-09-2 16:17-16:47 PASS; S2: UAT-DEP-09-3 17:41-18:09 PASS; S1 PASS, S2 PASS, S3 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-09/`; `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-09-2/`; `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-09-3/` | - |
| UAT-DEP-10 | **PASS** | UAT-DEP-10 07:54-08:32 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-DEP-10/` | - |
| UAT-FLT-01 | **PASS** | UAT-FLT-01 06:36-06:52 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-FLT-01/` | - |
| UAT-FLT-02 | **PASS** | UAT-FLT-02 06:52-07:14 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-FLT-02/` | - |
| UAT-FLT-03 | **PASS** | UAT-FLT-03 07:14-07:19 PASS | `<evidence>/v1.0.0-rc.4/us-west-2/serverless/UAT-FLT-03/` | - |

### `us-east-2`, EKS form

| Case | Result | Runs (install: run, UTC on 2026-11-01, or on the 2nd marked 02, result) | Evidence | Defect, retest |
| --- | --- | --- | --- | --- |
| UAT-CMP-01 | **PASS** | UAT-CMP-01 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-01/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-02 | **PASS** | UAT-CMP-02 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-02/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-03 | **PASS** | UAT-CMP-03 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-03/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-04 | **PASS** | UAT-CMP-04 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-04/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-05 | **PASS** | UAT-CMP-05 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-05/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-06 | **PASS** | UAT-CMP-06 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-06/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-07 | **PASS** | UAT-CMP-07 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-07/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CMP-08 | **PASS** | UAT-CMP-08 04:53-05:14 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CMP/cmp-c2611010452/UAT-CMP-08/ (matrix run 20261101T045319Z-c4aa)` | - |
| UAT-CAT-01 | **PASS** | UAT-CAT-01 04:52-05:02 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-01/` | - |
| UAT-CAT-02 | **PASS** | UAT-CAT-02 05:02-05:10 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-02/` | - |
| UAT-CAT-03 | **PASS** | UAT-CAT-03 08:36-10:28 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-03/` | - |
| UAT-CAT-04 | **PASS** | UAT-CAT-04 05:10-05:16 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-04/` | - |
| UAT-CAT-05 | **PASS** | UAT-CAT-05 05:16-05:23 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-05/` | - |
| UAT-CAT-06 | **PASS** | UAT-CAT-06 05:23-05:28 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-06/` | - |
| UAT-CAT-07 | **PASS** | UAT-CAT-07 05:28-05:32 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-07/` | - |
| UAT-CAT-08 | **PASS** | UAT-CAT-08 05:32-05:36 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-CAT-08/` | - |
| UAT-SEC-01 | **PASS** | UAT-SEC-01 05:36-05:39 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-SEC-01/` | - |
| UAT-SEC-02 | **PASS** | UAT-SEC-02 05:39-05:41 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-SEC-02/` | - |
| UAT-SEC-03 | **PASS** | UAT-SEC-03 05:41-05:45 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-SEC-03/` | - |
| UAT-SEC-04 | **PASS** | UAT-SEC-04 05:45-05:47 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-SEC-04/` | - |
| UAT-DEP-01 | **PASS** | E1: UAT-DEP-01 03:49-04:09 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-01/` | - |
| UAT-DEP-02 | **PASS** | E3: UAT-DEP-02 10:32-10:42 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-02/` | - |
| UAT-DEP-03 | **PASS** | E1: UAT-DEP-03 06:10-06:15 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-03/` | - |
| UAT-DEP-04 | **PASS** | E1: UAT-DEP-04 06:15-06:29 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-04/` | - |
| UAT-DEP-05 | **PASS** | E1: UAT-DEP-05 04:21-04:50 FAIL; E1: UAT-DEP-05-2 05:57-06:10 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-05/`; `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-05-2/` | UAT-DEP-05: harness: [#95](https://github.com/colvenna/colvenna-catalog/issues/95): `release-not-the-tag`'s checkout of `main` was `origin/main`, the tag's own commit right after the tag, so `install.sh --release` began a real install of the refusal's name (its Storage stack made; stopped by the operator in the stacks step, E1 untouched; removed with `uninstall.sh --delete-data`, `verify-clean.sh` clean) and `nothing-made` found it; every other check PASS |
| UAT-DEP-06 | **PASS** | E2: UAT-DEP-06 12:33-12:55 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-06/` | - |
| UAT-DEP-07 | **PASS** | E2: UAT-DEP-07 12:55-13:32 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-07/` | - |
| UAT-DEP-08 | **PASS** | E2: UAT-DEP-08 14:04-14:16 PASS; E1: UAT-DEP-08-2 14:04-14:16 PASS; E1 PASS, E2 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-08/`; `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-08-2/` | - |
| UAT-DEP-09 | **PASS** | E3: UAT-DEP-09 11:32-11:37 PASS; E1: UAT-DEP-09-2 16:53-16:59 FAIL; E2: UAT-DEP-09-3 16:53-17:00 PASS; E1: UAT-DEP-09-4 17:37-17:45 PASS; E1 PASS, E2 PASS, E3 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-09/`; `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-09-2/`; `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-09-3/`; `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-09-4/` | UAT-DEP-09-2: harness: [#100](https://github.com/colvenna/colvenna-catalog/issues/100): `others-untouched` took S1 `us-east-2`'s functions, roles, secret and log groups, which S1's own UAT-DEP-09 (started the same second, step 12) was deleting, for this case's doing: the after inventory had read S1's stacks before that uninstall began and its resources while CloudFormation deleted them; every other check PASS (`restored-instance-refused` among them). E1 was made again from the tag for the retest |
| UAT-DEP-10 | **PASS** | UAT-DEP-10 07:08-07:42 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-10/` | - |
| UAT-DEP-11 | **PASS** | UAT-DEP-11 10:51-11:32 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-DEP-11/` | - |
| UAT-FLT-01 | **PASS** | UAT-FLT-01 06:29-06:45 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-FLT-01/` | - |
| UAT-FLT-02 | **PASS** | UAT-FLT-02 06:45-06:58 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-FLT-02/` | - |
| UAT-FLT-03 | **PASS** | UAT-FLT-03 06:58-07:02 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/eks/UAT-FLT-03/` | - |

### `us-east-2`, serverless form

| Case | Result | Runs (install: run, UTC on 2026-11-01, or on the 2nd marked 02, result) | Evidence | Defect, retest |
| --- | --- | --- | --- | --- |
| UAT-CMP-01 | **PASS** | UAT-CMP-01 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-01/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-02 | **PASS** | UAT-CMP-02 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-02/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-03 | **PASS** | UAT-CMP-03 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-03/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-04 | **PASS** | UAT-CMP-04 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-04/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-05 | **PASS** | UAT-CMP-05 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-05/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-06 | **PASS** | UAT-CMP-06 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-06/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-07 | **PASS** | UAT-CMP-07 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-07/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CMP-08 | **PASS** | UAT-CMP-08 05:16-05:43 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CMP/cmp-c2611010515/UAT-CMP-08/ (matrix run 20261101T051609Z-44be)` | - |
| UAT-CAT-01 | **PASS** | UAT-CAT-01 04:52-05:04 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-01/` | - |
| UAT-CAT-02 | **PASS** | UAT-CAT-02 05:04-05:12 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-02/` | - |
| UAT-CAT-03 | **PASS** | UAT-CAT-03 08:20-10:12 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-03/` | - |
| UAT-CAT-04 | **PASS** | UAT-CAT-04 05:12-05:18 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-04/` | - |
| UAT-CAT-05 | **PASS** | UAT-CAT-05 05:18-05:26 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-05/` | - |
| UAT-CAT-06 | **PASS** | UAT-CAT-06 05:26-05:31 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-06/` | - |
| UAT-CAT-07 | **PASS** | UAT-CAT-07 05:31-05:40 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-07/` | - |
| UAT-CAT-08 | **PASS** | UAT-CAT-08 05:40-05:44 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-CAT-08/` | - |
| UAT-SEC-01 | **PASS** | UAT-SEC-01 05:44-05:48 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-SEC-01/` | - |
| UAT-SEC-02 | **PASS** | UAT-SEC-02 05:48-05:50 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-SEC-02/` | - |
| UAT-SEC-03 | **PASS** | UAT-SEC-03 05:50-05:54 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-SEC-03/` | - |
| UAT-SEC-04 | **PASS** | UAT-SEC-04 05:54-05:57 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-SEC-04/` | - |
| UAT-DEP-01 | **PASS** | S1: UAT-DEP-01 03:49-04:19 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-01/` | - |
| UAT-DEP-02 | **PASS** | S3: UAT-DEP-02 10:32-10:51 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-02/` | - |
| UAT-DEP-03 | **PASS** | S1: UAT-DEP-03 06:10-06:15 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-03/` | - |
| UAT-DEP-04 | **PASS** | S1: UAT-DEP-04 06:15-06:28 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-04/` | - |
| UAT-DEP-05 | **PASS** | S1: UAT-DEP-05 05:57-06:01 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-05/` | - |
| UAT-DEP-06 | **PASS** | S2: UAT-DEP-06 12:36-13:06 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-06/` | - |
| UAT-DEP-07 | **PASS** | S2: UAT-DEP-07 13:06-14:04 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-07/` | - |
| UAT-DEP-08 | **PASS** | S1: UAT-DEP-08 14:04-15:30 PASS; S2: UAT-DEP-08-2 15:30-16:53 PASS; S1 PASS, S2 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-08/`; `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-08-2/` | - |
| UAT-DEP-09 | **PASS** | S3: UAT-DEP-09 11:32-12:18 PASS; S2: UAT-DEP-09-3 16:53-17:21 PASS; S1: UAT-DEP-09-2 16:53-17:24 PASS; S1 PASS, S2 PASS, S3 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-09/`; `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-09-3/`; `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-09-2/` | - |
| UAT-DEP-10 | **PASS** | UAT-DEP-10 07:42-08:17 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-DEP-10/` | - |
| UAT-FLT-01 | **PASS** | UAT-FLT-01 06:28-06:44 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-FLT-01/` | - |
| UAT-FLT-02 | **PASS** | UAT-FLT-02 06:44-07:04 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-FLT-02/` | - |
| UAT-FLT-03 | **PASS** | UAT-FLT-03 07:04-07:08 PASS | `<evidence>/v1.0.0-rc.4/us-east-2/serverless/UAT-FLT-03/` | - |

## 6. Defects and retests

| Issue | Kind | Cases (runs) | Impact | Fix | Retest |
| --- | --- | --- | --- | --- | --- |
| [#94](https://github.com/colvenna/colvenna-catalog/issues/94) | Product (serverless): `install.sh`'s stage redeployment ([#34](https://github.com/colvenna/colvenna-catalog/issues/34)'s) fails when API Gateway throttles `CreateDeployment` | No UAT case; the performance acceptance's serverless install `pfsl` (04:10 UTC), beside S1's install in the same Region | An install fails at `functions` when another deployment in the account and Region throttles it; a run again recovers (`pfsl` did, 04:21) | [#97](https://github.com/colvenna/colvenna-catalog/pull/97), merged after the tag: the next candidate | The next candidate. Here every serverless `install.sh` of the acceptance ran one at a time per Region (section 4), and none met it again |
| [#95](https://github.com/colvenna/colvenna-catalog/issues/95) | Test: UAT-DEP-05's checkout of `main` was the tag's own commit right after the tag, and the refusals took E1's default Kubernetes names | UAT-DEP-05 EKS, both Regions (`release-not-the-tag`, `nothing-made`) | None of the product's: `install.sh --release` installs from the tag, as it should; the refusal's stopped install was removed, E1 untouched | [#96](https://github.com/colvenna/colvenna-catalog/pull/96): a commit `install.sh --release` refuses (the tag's first parent while `main` is the tag), and names of the case's own | `UAT-DEP-05-2` (E1), harness at `fd92b57`: `PASS` in both Regions; S1's first runs, the same harness: `PASS` |
| [#98](https://github.com/colvenna/colvenna-catalog/issues/98) | Test: UAT-DEP-02's `existing-untouched` compared the existing instance's status, which RDS's backup in its backup window turns to `backing-up` | UAT-DEP-02 E3 `us-west-2` | None: the install changed nothing of the instance | [#99](https://github.com/colvenna/colvenna-catalog/pull/99): being backed up reported, not counted; any other change of status, the settings and `PendingModifiedValues` compared (UAT-DEP-09's `existing-kept` too) | E3 `us-west-2` uninstalled by its UAT-DEP-09 (`PASS`), made again: `UAT-DEP-02-2` and `UAT-DEP-09-2`, harness at `bfe991d`: `PASS` |
| [#100](https://github.com/colvenna/colvenna-catalog/issues/100) | Test: the account inventory read the stacks first and the resources a minute later, so an install whose uninstall began in between had its resources going while its stacks looked as they were | UAT-DEP-09 E1 `us-east-2` (`others-untouched`; S1's own UAT-DEP-09 ran at the same time, step 12) | None: S1's resources were S1's own run's to delete | [#101](https://github.com/colvenna/colvenna-catalog/pull/101): the stacks read again at each record's end, what happened to them judged from the first record's first reading to the last record's second | E1 `us-east-2` made again from the tag: `UAT-DEP-09-4`, harness at `59d1a39`: `PASS` |

The execution's own events (section 4) are the operator's, not the harness's or the product's, and no case's result rests on them.

## 7. The compatibility matrix

UAT-CMP-01 to 08 are the cells of four `tests/compat/matrix.py` runs, one per Region and form, each all eight cells, with the runner, `cells.json` and `declared.tsv` at the tag (`run-is-the-candidate`), the engines on the test environments' clients' nodes from their official images by digest, no AWS identity of their own: `20261101T045324Z-9e70` (`us-west-2`, EKS), `20261101T051511Z-6acf` (`us-west-2`, serverless), `20261101T045319Z-c4aa` (`us-east-2`, EKS), `20261101T051609Z-44be` (`us-east-2`, serverless). Their results are committed beside the page they make, [`docs/compat/matrix.md`](../../../compat/matrix.md), regenerated from them (`matrix.py page`).

In every run every cell passed every entry declared supported: Flink 1.20.5 (Paimon 2.0.0 and 2.1.0), Flink 2.2.1, Spark 3.5.9 (2.0.0 and 2.1.0), Spark 4.1.3, Doris 4.1.5 and StarRocks 4.1.6. Limitations were recorded as declared, StarRocks's primary-key read among them ([#31](https://github.com/colvenna/colvenna-catalog/issues/31)). The one that passed, for the next declaration: Doris's `pk-dv` credentials on the EKS form (declared a limitation there, supported on the serverless form), in both EKS runs, as in `v1.0.0-rc.2`'s.

## 8. The database secrets, rotated

`docs/manual/database-secrets.md`'s procedures ran once per form, as written, on the performance acceptance's installs once its runs were done (`us-west-2`, 17:59 to 18:10 UTC; `<evidence>/logs/rotate-*.log`): both passed. EKS form (`pfek`): the catalog's database user's password put in its secret (a new version), `install.sh --release v1.0.0-rc.4` exit 0 with its smoke check (401 unauthenticated and 200 for the admin from the client namespace, no connection from another), the pods restarted and ready; then the master user's password set on the instance (applied: `available`, nothing pending), then in its secret, and `install.sh` exit 0, its `database` step signing in with it. Serverless form (`pfsl`): the master user's password the same way, and `install.sh` exit 0 with its smoke check (401 unauthenticated and forged, 200 for the admin, through the private API; no function URL; the API private).

## 9. What was checked besides the cases

- **UAT-DEP-09 `restored-instance-refused`**: E1 and S1 ran UAT-DEP-09 on the instance UAT-DEP-10 restored, after UAT-DEP-10, in both Regions: exit 3, naming the restored instance, before the name is asked for, nothing changed; then the instance and its log group deleted as the deployment guide says.
- **No historical evidence is cited**: every result above is of this candidate's runs.
- **CI**: the pull requests of this candidate and of its fixes: [#92](https://github.com/colvenna/colvenna-catalog/pull/92) (B) merged on a full local CI run with a recorded waiver, GitHub Actions having passed its exact head too; [#93](https://github.com/colvenna/colvenna-catalog/pull/93) (C), [#96](https://github.com/colvenna/colvenna-catalog/pull/96), [#99](https://github.com/colvenna/colvenna-catalog/pull/99) and [#101](https://github.com/colvenna/colvenna-catalog/pull/101) on GitHub Actions on their exact head SHA, the local CI as a supplement.

## 10. Trademarks

Apache, Apache Paimon, Paimon, Apache Flink, Flink, Apache Spark, Spark, Apache Doris and Doris are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. StarRocks is a trademark of the Linux Foundation. Colvenna Catalog is not affiliated with or endorsed by The Apache Software Foundation. AWS, Amazon EKS, AWS Lambda, Amazon API Gateway and Amazon RDS are trademarks of Amazon.com, Inc. or its affiliates.

> Note: issue and pull request numbers with a link are this repository's; numbers named "of the early private repository" have no pages here.
