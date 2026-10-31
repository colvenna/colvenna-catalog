---
title: "Colvenna Catalog v1.0.0-rc.2: release check"
date: 2026-10-31
status: final
issue: 2
---

# `v1.0.0-rc.2`: release check

The release check of design section 9.4 for the candidate `v1.0.0-rc.2` (tag object `61d8ecc5b604384e67f1305ca2e67ba2b077817f` at `3b3655da72424ad4f088f746bb16e5132a470c39`, built from B `9eefca8848cd2076f263797a70f3d4d0e8130c59`). Every link is to the tag or to a full commit SHA, but the one to the release notes beside this check, in the same commit, which cannot name itself: material merged after the tag links the commit that merged it, `b5b3e6b4ad0e720114ab6df5ae4470984d182c6e`.

## 1. Verdict

**NO-GO.** Decided on 2026-10-31 by the release coordinator: the performance acceptance failed, and the product defects [#54](https://github.com/colvenna/colvenna-catalog/issues/54), [#34](https://github.com/colvenna/colvenna-catalog/issues/34) and [#63](https://github.com/colvenna/colvenna-catalog/issues/63) stand. The UAT did not pass either (section 3). The blockers are in section 4.

## 2. The seven deliverables

| Deliverable | Immutable link | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/user.md) | Done |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/install.md) | Done |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/install.sh) (the manifest's `installer` blob) | Done; defects [#27](https://github.com/colvenna/colvenna-catalog/issues/27) (fixed on `main`), [#34](https://github.com/colvenna/colvenna-catalog/issues/34), [#54](https://github.com/colvenna/colvenna-catalog/issues/54), [#63](https://github.com/colvenna/colvenna-catalog/issues/63) |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/cost.md) | Done |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/uat/cases.md) (the manifest's `uat_cases`) | Done; how UAT-DEP-06/07 count a previous release's own defect was decided on 2026-10-31 ([#55](https://github.com/colvenna/colvenna-catalog/issues/55), on `main` for the next candidate), and StarRocks's `pk read` is a declared limitation on `main` ([#31](https://github.com/colvenna/colvenna-catalog/issues/31), [#80](https://github.com/colvenna/colvenna-catalog/pull/80)) |
| UAT report | [`docs/uat/reports/v1.0.0-rc.2/report.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/uat/reports/v1.0.0-rc.2/report.md) | Done: not passed |
| Performance document | [`docs/performance/v1.0.0-rc.2.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/performance/v1.0.0-rc.2.md) | Done: `FAIL` |

All seven are of this candidate: the first five at its tag, the last two built from its runs only. The release notes: [`release/v1.0.0-rc.2/notes.md`](notes.md), beside this check in the same commit.

## 3. The checks

| Check | Result | Basis |
| --- | --- | --- |
| Manifest | `python3 scripts/release/manifest.py validate --go` exit 0: nothing lacking for GO in the manifest itself | [`release/v1.0.0-rc.2/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/release/v1.0.0-rc.2/manifest.json) |
| UAT, `us-west-2` | 67 required runs: PASS 60, FAIL 7, BLOCKED 0, NOT_RUN 0 | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/uat/reports/v1.0.0-rc.2/report.md), sections 1 and 5 |
| UAT, `us-east-2` | 67 required runs: PASS 54, FAIL 10, BLOCKED 0, NOT_RUN 3 (S2's UAT-DEP-06 to 08, never run) | The same |
| Performance acceptance (`us-west-2`) | `FAIL`: P1a EKS 8.703 commits/s (≥ 10); P1b 67.9 % and 59.9 % of the offered commits done (≥ 99 %); P2 EKS `getTable` and snapshot p95 about 65 ms (≤ 50); P5 EKS reads and credential hits p95 57 to 67 ms (≤ 50) | [The performance document](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/performance/v1.0.0-rc.2.md), section 1 |
| Security scans on B | No unhandled finding (ASH, Trivy, grype, cdk-nag, licenses); but ASH on B itself is not at zero: its 10 actionable findings are verified false positives whose marks were merged after the tag ([#26](https://github.com/colvenna/colvenna-catalog/pull/26)) | [`docs/security/v1.0.0-rc.2.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/security/v1.0.0-rc.2.md) |
| Rename gate | `PASS`: source at B and at the tag, the image and chart by digest | [`release/v1.0.0-rc.2/rename.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/release/v1.0.0-rc.2/rename.md) |
| Database secrets | The manual's rotations ran as written on both forms, `install.sh` exit 0 after each | The UAT report, section 8 |
| CI | `PASS` on GitHub Actions for [#15](https://github.com/colvenna/colvenna-catalog/pull/15), [#17](https://github.com/colvenna/colvenna-catalog/pull/17), [#18](https://github.com/colvenna/colvenna-catalog/pull/18), [#19](https://github.com/colvenna/colvenna-catalog/pull/19), [#20](https://github.com/colvenna/colvenna-catalog/pull/20), [#21](https://github.com/colvenna/colvenna-catalog/pull/21), [#22](https://github.com/colvenna/colvenna-catalog/pull/22), [#23](https://github.com/colvenna/colvenna-catalog/pull/23), [#24](https://github.com/colvenna/colvenna-catalog/pull/24), [#25](https://github.com/colvenna/colvenna-catalog/pull/25), [#26](https://github.com/colvenna/colvenna-catalog/pull/26), [#28](https://github.com/colvenna/colvenna-catalog/pull/28), [#30](https://github.com/colvenna/colvenna-catalog/pull/30), [#33](https://github.com/colvenna/colvenna-catalog/pull/33), [#36](https://github.com/colvenna/colvenna-catalog/pull/36), [#40](https://github.com/colvenna/colvenna-catalog/pull/40), [#41](https://github.com/colvenna/colvenna-catalog/pull/41), [#42](https://github.com/colvenna/colvenna-catalog/pull/42), [#45](https://github.com/colvenna/colvenna-catalog/pull/45), [#47](https://github.com/colvenna/colvenna-catalog/pull/47), [#50](https://github.com/colvenna/colvenna-catalog/pull/50), [#53](https://github.com/colvenna/colvenna-catalog/pull/53), [#57](https://github.com/colvenna/colvenna-catalog/pull/57), [#60](https://github.com/colvenna/colvenna-catalog/pull/60), [#61](https://github.com/colvenna/colvenna-catalog/pull/61), [#68](https://github.com/colvenna/colvenna-catalog/pull/68), [#75](https://github.com/colvenna/colvenna-catalog/pull/75); `WAIVED` for [#69](https://github.com/colvenna/colvenna-catalog/pull/69), [#70](https://github.com/colvenna/colvenna-catalog/pull/70), [#71](https://github.com/colvenna/colvenna-catalog/pull/71), [#72](https://github.com/colvenna/colvenna-catalog/pull/72), [#73](https://github.com/colvenna/colvenna-catalog/pull/73), [#77](https://github.com/colvenna/colvenna-catalog/pull/77), [#78](https://github.com/colvenna/colvenna-catalog/pull/78), [#80](https://github.com/colvenna/colvenna-catalog/pull/80), [#81](https://github.com/colvenna/colvenna-catalog/pull/81), [#82](https://github.com/colvenna/colvenna-catalog/pull/82), this check's own included: Actions does not start jobs for the organization (billing), and by the requester's decision of 2026-10-31 a full `scripts/ci-local.sh` pass on each exact head SHA plus a recorded waiver | The release notes, section 5 |

## 4. Blockers

| Blocker | Issue | Who decides or fixes |
| --- | --- | --- |
| Performance `FAIL` (P1a EKS, P1b both forms, P2 EKS, P5 EKS) | [#1](https://github.com/colvenna/colvenna-catalog/issues/1); the EKS form's read latency [#37](https://github.com/colvenna/colvenna-catalog/issues/37) (fixed on `main`) | The requester (the targets; P1b's investigation); the next candidate |
| Serverless: an oversized request answered 503, not 413 (UAT-CAT-08 serverless, both Regions) | [#34](https://github.com/colvenna/colvenna-catalog/issues/34) | Fixed on `main` ([#71](https://github.com/colvenna/colvenna-catalog/pull/71)); the next candidate |
| Serverless: the way back and forward again fails at `functions` (UAT-DEP-07 S2 `us-west-2`) | [#54](https://github.com/colvenna/colvenna-catalog/issues/54) | Fixed on `main` ([#81](https://github.com/colvenna/colvenna-catalog/pull/81)); the next candidate |
| Serverless: going back publishes a new server version before `migrate` refuses (the candidate does the same) | [#63](https://github.com/colvenna/colvenna-catalog/issues/63) | Fixed on `main` ([#70](https://github.com/colvenna/colvenna-catalog/pull/70)); the next candidate |
| EKS: an install fails where the CLI's default Region is not the install's (UAT-DEP-01 EKS `us-east-2`) | [#27](https://github.com/colvenna/colvenna-catalog/issues/27) | Fixed on `main` ([#28](https://github.com/colvenna/colvenna-catalog/pull/28)); the next candidate's UAT |
| UAT-DEP-06 and 07 against the pinned previous releases' own behaviour | [#55](https://github.com/colvenna/colvenna-catalog/issues/55) | Decided 2026-10-31: the previous release's, once an extra run with this candidate as the previous release passes; not made: `FAIL` counts |
| UAT-CMP-08: StarRocks's `pk read` with short-lived credentials | [#31](https://github.com/colvenna/colvenna-catalog/issues/31) | Settled: a declared limitation from the next candidate ([#80](https://github.com/colvenna/colvenna-catalog/pull/80)), reported upstream |
| ASH's count on B itself not at zero (#2's requirement): 10 detect-secrets false positives, marked after the tag | [#26](https://github.com/colvenna/colvenna-catalog/pull/26) | The next candidate is built from a commit that carries the marks |
| UAT runs that failed by harness defects or incidents and were not retested after the NO-GO decision; S2 `us-east-2` not run | [#59](https://github.com/colvenna/colvenna-catalog/issues/59), [#64](https://github.com/colvenna/colvenna-catalog/issues/64), [#67](https://github.com/colvenna/colvenna-catalog/issues/67) ([#43](https://github.com/colvenna/colvenna-catalog/issues/43), [#62](https://github.com/colvenna/colvenna-catalog/issues/62) fixed since) | Harness fixes before the next candidate's UAT |

## 5. Environments

Every test environment and install of this candidate was torn down, each with a clean-up proof kept in the private evidence (`<evidence>/cleanup/<environment>/cleanup-proof.txt`: `p2w2`, `r2w2`, `r2e2`); the release registry `ColvennaCatalogRelease` is kept, its tags unmoved (the trial candidate `v0.0.0-rc.21` removed by tag, as [Trial candidates](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/development.md#trial-candidates) says).
