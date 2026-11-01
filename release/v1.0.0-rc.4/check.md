---
title: "Colvenna Catalog v1.0.0-rc.4: release check"
date: 2026-11-01
status: final
issue: 2
---

# `v1.0.0-rc.4`: release check

The release check of design section 9.4 for the candidate `v1.0.0-rc.4` (tag object `b4e391275ab4df7263f70ca4aec7da4603bd0215` at `5a11f1518cea38f61338f9d8891c29a4fc480533`, built from B `77984fb24823acc53e4c1e2d00747d494961552d`). Every link is to the tag or to a full commit SHA, but the one to the release notes beside this check: the notes are committed after it, in the same pull request, and link it by its commit, which is what `promote.sh` copies into the release's notes. Material merged after the tag links the commit that merged it, `bb644ca2ce808b04f1f89032ff1834625faafd87`.

## 1. Verdict

**GO.** Every item of design 9.4 is met (section 3), and nothing blocks the release (section 4). Judged on 2026-11-01 by the criteria of [#2](https://github.com/colvenna/colvenna-catalog/issues/2) and design 9.4: every required case `PASS` in both Regions, the performance acceptance `PASS`, the security record at zero on B, the rename gate passed, the database secrets rotated on both forms.

## 2. The seven deliverables

| Deliverable | Immutable link | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/user.md) | Done |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/install.md) | Done |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/install.sh) (the manifest's `installer` blob) | Done; one product defect found, [#94](https://github.com/colvenna/colvenna-catalog/issues/94): a throttled API Gateway call fails a serverless install, and running the install again recovers. No required case met it; fixed on `main` after the tag by [#97](https://github.com/colvenna/colvenna-catalog/pull/97), for the next version |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/cost.md) | Done |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/uat/cases.md) (the manifest's `uat_cases`) | Done |
| UAT report | [`docs/uat/reports/v1.0.0-rc.4/report.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/uat/reports/v1.0.0-rc.4/report.md) | Done: passed |
| Performance document | [`docs/performance/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/performance/v1.0.0-rc.4.md) | Done: `PASS` |

All seven are of this candidate: the first five at its tag, the last two built from its runs only. The release notes: [`release/v1.0.0-rc.4/notes.md`](notes.md), beside this check.

## 3. The checks

| Check | Result | Basis |
| --- | --- | --- |
| Manifest | `python3 scripts/release/manifest.py validate --go` exit 0: nothing lacking for GO in the manifest itself | [`release/v1.0.0-rc.4/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/release/v1.0.0-rc.4/manifest.json) |
| UAT, `us-west-2` | 67 required runs: PASS 67, FAIL 0, BLOCKED 0, NOT_RUN 0 | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/uat/reports/v1.0.0-rc.4/report.md), section 1 |
| UAT, `us-east-2` | 67 required runs: PASS 67, FAIL 0, BLOCKED 0, NOT_RUN 0 | The same |
| UAT's first runs that failed | Four, by three defects of the harness, each fixed on `main` and retested on this candidate, `PASS`: UAT-DEP-05 EKS in both Regions ([#95](https://github.com/colvenna/colvenna-catalog/issues/95)), UAT-DEP-02 EKS `us-west-2` ([#98](https://github.com/colvenna/colvenna-catalog/issues/98)), UAT-DEP-09 EKS `us-east-2` ([#100](https://github.com/colvenna/colvenna-catalog/issues/100)); no product defect failed a case | The UAT report, sections 1 and 6 |
| Compatibility matrix | Every cell passed every entry declared supported, on both forms in both Regions | The UAT report, section 7 |
| Performance acceptance (`us-west-2`) | `PASS`: P1a, P1b, P2, P3 and P5 in both forms, P4a and P4b; P1c observed; every run official, not reduced, its clean-up `PASS` | [The performance document](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/performance/v1.0.0-rc.4.md), section 1 |
| Security scans on B | No unhandled finding (ASH, Trivy, grype, cdk-nag, licenses); ASH 0 actionable on B itself; no new risk accepted | [`docs/security/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/security/v1.0.0-rc.4.md) |
| Rename gate | `PASS`: source at B and at the tag, the image and chart by digest | [`release/v1.0.0-rc.4/rename.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/release/v1.0.0-rc.4/rename.md) |
| Database secrets | The manual's rotations ran as written on both forms, `install.sh` exit 0 after each | The UAT report, section 8 |
| CI | `PASS` on GitHub Actions for every pull request merged since `v1.0.0-rc.2`'s release notes ([#82](https://github.com/colvenna/colvenna-catalog/pull/82)), the release notes' own included; `WAIVED` for [#79](https://github.com/colvenna/colvenna-catalog/pull/79), merged just before #82 (Actions could not start jobs then; `scripts/ci-local.sh` passed on its exact SHA) | The release notes, section 5 |

## 4. Blockers, known limitations and follow-up issues

- Blockers: none.
- Known limitations: the one this candidate found, [#94](https://github.com/colvenna/colvenna-catalog/issues/94) (closed: fixed after the tag, so the candidate and a release promoted from it keep it), is in the release notes, section 7; the user manual's, section 9, stand.
- Follow-up issues: [#103](https://github.com/colvenna/colvenna-catalog/issues/103) (the performance harness's redaction) and [#105](https://github.com/colvenna/colvenna-catalog/issues/105) (the release's notes keep the candidate's relative links), neither blocking; [#3](https://github.com/colvenna/colvenna-catalog/issues/3), the release `v1.0.0` and its publication; [#8](https://github.com/colvenna/colvenna-catalog/issues/8), the rename, whose open items this candidate's rename gate and UAT meet. The release notes, section 8, has each with its impact.

## 5. Environments

The acceptance's test environments were torn down, each with a clean-up proof kept in the private evidence (`<evidence>/cleanup/<environment>/cleanup-proof.txt`): `r3e2` and `p3w2`. `r3w2` was kept for the release's smoke install (`install.sh --release v1.0.0`, [Promoting a GO candidate](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/development.md#promoting-a-go-candidate)) and is torn down after it, its proof reported on [#2](https://github.com/colvenna/colvenna-catalog/issues/2). The release registry `ColvennaCatalogRelease` is kept, its tags unmoved; the trial candidates `v0.0.0-rc.31` and `v0.0.0-rc.32` were removed from it by tag, as [Trial candidates](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/development.md#trial-candidates) says (the private ledger records both).
