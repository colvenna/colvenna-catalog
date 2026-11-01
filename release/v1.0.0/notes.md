---
title: "Colvenna Catalog v1.0.0 release notes"
date: 2026-11-01
status: final
---

<!--
scripts/release/promote.sh wrote these notes when it promoted v1.0.0-rc.4 to v1.0.0: section 1 from
release/v1.0.0/manifest.json; every other section is v1.0.0-rc.4's release notes as accepted
(release/v1.0.0-rc.4/notes.md at 7afb7ea0d79ef3a9472107a99a01f820e07a92f0), unchanged.
-->

# Colvenna Catalog v1.0.0 release notes

## 1. Release

| Item | Value |
| --- | --- |
| Release | `v1.0.0` (annotated tag) |
| Promoted from | `v1.0.0-rc.4`, judged GO: its release notes as accepted, [`release/v1.0.0-rc.4/notes.md`](https://github.com/colvenna/colvenna-catalog/blob/7afb7ea0d79ef3a9472107a99a01f820e07a92f0/release/v1.0.0-rc.4/notes.md) at commit `7afb7ea0d79ef3a9472107a99a01f820e07a92f0`. Nothing was rebuilt. The sections after this one are those notes, unchanged |
| Built from | commit [`77984fb2`](https://github.com/colvenna/colvenna-catalog/commit/77984fb24823acc53e4c1e2d00747d494961552d) (`77984fb24823acc53e4c1e2d00747d494961552d`). The tag `v1.0.0` points at a commit that adds only `release/v1.0.0/` to `v1.0.0-rc.4`'s tag commit, `5a11f1518cea38f61338f9d8891c29a4fc480533` |
| Manifest | [`release/v1.0.0/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0/release/v1.0.0/manifest.json): `v1.0.0-rc.4`'s manifest under the release's names, and `promotion`, where it came from |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68` (`linux/amd64`): `v1.0.0-rc.4`'s image, tagged `v1.0.0` as well, labelled `org.opencontainers.image.revision=77984fb24823acc53e4c1e2d00747d494961552d` (its `org.opencontainers.image.version` label stays `v1.0.0-rc.4`, as it was built) |
| Helm chart | `oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:891ba0366a34b8523ee2eb54dfea1b6cd248e8509e700f983b0d5464dbdb1cbb`, version `1.0.0`, appVersion `1.0.0`: `v1.0.0-rc.4`'s chart (`sha256:06b5328572d2bc80069a0f7ebd164c5e01f331317d20cf6a376d05e4e53e4449`, version `1.0.0-rc.4`) with only `Chart.yaml`'s version and appVersion changed, its 10 files compared one by one ([`chart-comparison.txt`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0/release/v1.0.0/chart-comparison.txt)); annotated `colvenna-catalog/source-commit: 77984fb24823acc53e4c1e2d00747d494961552d` |
| Public copies | By the same digests, `ghcr.io/colvenna/colvenna-catalog:1.0.0` and `oci://ghcr.io/colvenna/charts/colvenna-catalog` version `1.0.0`, once `scripts/release/publish-ghcr.sh v1.0.0` has published them |
| Generated | 2026-11-01 |

A consumer that runs the chart itself fetches both by digest from the release registry:

```bash
crane copy <registry>/colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68 <its own repository>:v1.0.0
helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:891ba0366a34b8523ee2eb54dfea1b6cd248e8509e700f983b0d5464dbdb1cbb
```

By hand, both of these print `77984fb24823acc53e4c1e2d00747d494961552d`:

```bash
crane config <registry>/colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68 | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:891ba0366a34b8523ee2eb54dfea1b6cd248e8509e700f983b0d5464dbdb1cbb | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

## 2. Deliverables

| Deliverable | Pinned version | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/user.md) | Done |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/install.md) | Done |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/install.sh); the manifest's `installer` holds its blob | Done; one product defect found, [#94](https://github.com/colvenna/colvenna-catalog/issues/94): a throttled API Gateway call fails a serverless install, and running the install again recovers (section 7). No required case met it; fixed on `main` after the tag by [#97](https://github.com/colvenna/colvenna-catalog/pull/97), for the next version |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/cost.md) | Done |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/uat/cases.md); the manifest's `uat_cases` holds its version | Done |
| UAT report | [`docs/uat/reports/v1.0.0-rc.4/report.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/uat/reports/v1.0.0-rc.4/report.md) | Done: UAT passed (section 5) |
| Performance document | [`docs/performance/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/performance/v1.0.0-rc.4.md) | Done: `PASS` (section 5) |

## 3. Supported configurations

As the manifest's `configurations` says:

- Deployment forms: `eks`, `serverless`.
- Regions: the candidate is accepted in `us-west-2`, `us-east-2`. The installer takes any standard Region (partition `aws`); the others are not accepted for this candidate. China and GovCloud are not supported.

## 4. What the manifest still lacks for GO

- None: the manifest has what a candidate for GO needs.

## 5. Verdict

| Item | Result | Basis |
| --- | --- | --- |
| UAT: `us-west-2` | PASS 67, FAIL 0, BLOCKED 0, NOT_RUN 0 (67 required runs: 34 cases on the EKS form, 33 on the serverless form) | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/uat/reports/v1.0.0-rc.4/report.md), section 1 |
| UAT: `us-east-2` | PASS 67, FAIL 0, BLOCKED 0, NOT_RUN 0 (67 required runs) | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/uat/reports/v1.0.0-rc.4/report.md), section 1 |
| Performance acceptance | Met: `PASS` (P1a, P1b, P2, P3 and P5 in both forms, P4a and P4b; P1c observed) | [The performance document](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/performance/v1.0.0-rc.4.md), section 1 |
| Security scans (ASH, Trivy, grype, cdk-nag, licenses) | No unhandled finding: `scripts/security/scan.sh` on `77984fb2`, the commit the candidate is built from, where ASH has 0 actionable findings | [`docs/security/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/bb644ca2ce808b04f1f89032ff1834625faafd87/docs/security/v1.0.0-rc.4.md) |
| CI | `PASS`: GitHub Actions, every job of `ci.yml`, on each pull request's exact head SHA, for [#86](https://github.com/colvenna/colvenna-catalog/pull/86), [#87](https://github.com/colvenna/colvenna-catalog/pull/87), [#88](https://github.com/colvenna/colvenna-catalog/pull/88), [#89](https://github.com/colvenna/colvenna-catalog/pull/89), [#90](https://github.com/colvenna/colvenna-catalog/pull/90), [#92](https://github.com/colvenna/colvenna-catalog/pull/92), [#93](https://github.com/colvenna/colvenna-catalog/pull/93), [#96](https://github.com/colvenna/colvenna-catalog/pull/96), [#97](https://github.com/colvenna/colvenna-catalog/pull/97), [#99](https://github.com/colvenna/colvenna-catalog/pull/99), [#101](https://github.com/colvenna/colvenna-catalog/pull/101), [#102](https://github.com/colvenna/colvenna-catalog/pull/102) and [#104](https://github.com/colvenna/colvenna-catalog/pull/104), these notes' (every pull request merged since `v1.0.0-rc.2`'s release notes, [#82](https://github.com/colvenna/colvenna-catalog/pull/82): the commit the candidate is built from, its tag commit, and the fixes and records merged since; #86 to #92 also carry a waiver comment, written while Actions' state was in doubt, and their Actions runs passed on the same SHAs before each merge); `WAIVED` for [#79](https://github.com/colvenna/colvenna-catalog/pull/79), merged before #82 and not in `v1.0.0-rc.2`'s list: Actions did not start jobs for the organization then (billing), and by the requester's decision of 2026-10-31 `scripts/ci-local.sh` ran every job of `ci.yml` on its exact head SHA, all passing, with the waiver recorded | Each pull request's checks and #79's waiver comment |
| Release verdict | **GO** | [The release check](https://github.com/colvenna/colvenna-catalog/blob/417edc6dfe3f98f5058b07a4cbc3ae1ef4bb8f4b/release/v1.0.0-rc.4/check.md), committed just before these notes, in the same pull request |

## 6. Changes

Since `v1.0.0-rc.2`. The name `v1.0.0-rc.3` was spent before it was tagged: its image and chart, built from `5601257` before P5's terms changed, are in the release registry, neither removed nor retagged, and it was never tagged in git, installed or accepted ([#2](https://github.com/colvenna/colvenna-catalog/issues/2)).

- The EKS form: its database step reads the secrets in the install's Region ([#28](https://github.com/colvenna/colvenna-catalog/pull/28), [#27](https://github.com/colvenna/colvenna-catalog/issues/27)); the eligible nodes are those the catalog's pods may run on ([#36](https://github.com/colvenna/colvenna-catalog/pull/36), [#35](https://github.com/colvenna/colvenna-catalog/issues/35)); the HTTP server's connections have Nagle's algorithm off, which brings P2's and P5's read latencies within their targets ([#41](https://github.com/colvenna/colvenna-catalog/pull/41), [#37](https://github.com/colvenna/colvenna-catalog/issues/37)), and its test checks that on every answer's connection instead of timing ([#75](https://github.com/colvenna/colvenna-catalog/pull/75), [#66](https://github.com/colvenna/colvenna-catalog/issues/66)).
- The serverless form: a request over Lambda's payload limit is answered 413 again ([#71](https://github.com/colvenna/colvenna-catalog/pull/71), [#34](https://github.com/colvenna/colvenna-catalog/issues/34)); an upgrade migrates before anything that serves changes ([#70](https://github.com/colvenna/colvenna-catalog/pull/70), [#63](https://github.com/colvenna/colvenna-catalog/issues/63)); a gone function's log group that no stack owns is removed before the stack makes it again ([#81](https://github.com/colvenna/colvenna-catalog/pull/81), [#54](https://github.com/colvenna/colvenna-catalog/issues/54)).
- The catalog's config gives clients `fs.s3a.create.performance=true` as a default ([#79](https://github.com/colvenna/colvenna-catalog/pull/79), [#1](https://github.com/colvenna/colvenna-catalog/issues/1)).
- Releases: a GO candidate is promoted to `v1.0.0` without rebuilding, and a release is published to GHCR only ([#61](https://github.com/colvenna/colvenna-catalog/pull/61), [#58](https://github.com/colvenna/colvenna-catalog/issues/58)); `install.sh --release` takes the promoted release ([#68](https://github.com/colvenna/colvenna-catalog/pull/68), [#65](https://github.com/colvenna/colvenna-catalog/issues/65)).
- The acceptance's terms, decided by the requester: how UAT-DEP-06 and 07 count a previous release's own defect ([#78](https://github.com/colvenna/colvenna-catalog/pull/78), [#55](https://github.com/colvenna/colvenna-catalog/issues/55)); StarRocks's `pk read` with short-lived credentials a declared limitation ([#80](https://github.com/colvenna/colvenna-catalog/pull/80), [#31](https://github.com/colvenna/colvenna-catalog/issues/31)); P1b within one table's commit capacity, its targets unchanged, and the earlier load observed as P1c ([#87](https://github.com/colvenna/colvenna-catalog/pull/87), [#83](https://github.com/colvenna/colvenna-catalog/issues/83)); P5's credential misses reported, their latency judged by P3 ([#92](https://github.com/colvenna/colvenna-catalog/pull/92), [#91](https://github.com/colvenna/colvenna-catalog/issues/91)).
- Security: ASH at 0 actionable on the commit the candidate is built from: the detect-secrets false positives marked ([#26](https://github.com/colvenna/colvenna-catalog/pull/26), [#2](https://github.com/colvenna/colvenna-catalog/issues/2)), and minimatch's advisory in the copy `aws-cdk-lib` bundles suppressed as a false positive ([#88](https://github.com/colvenna/colvenna-catalog/pull/88), [#85](https://github.com/colvenna/colvenna-catalog/issues/85)).
- The tests and the UAT harness: fixed before the candidate in [#30](https://github.com/colvenna/colvenna-catalog/pull/30) ([#29](https://github.com/colvenna/colvenna-catalog/issues/29)), [#33](https://github.com/colvenna/colvenna-catalog/pull/33) ([#32](https://github.com/colvenna/colvenna-catalog/issues/32)), [#40](https://github.com/colvenna/colvenna-catalog/pull/40) ([#39](https://github.com/colvenna/colvenna-catalog/issues/39)), [#42](https://github.com/colvenna/colvenna-catalog/pull/42) ([#38](https://github.com/colvenna/colvenna-catalog/issues/38)), [#45](https://github.com/colvenna/colvenna-catalog/pull/45) ([#44](https://github.com/colvenna/colvenna-catalog/issues/44)), [#47](https://github.com/colvenna/colvenna-catalog/pull/47) ([#46](https://github.com/colvenna/colvenna-catalog/issues/46)), [#50](https://github.com/colvenna/colvenna-catalog/pull/50) ([#48](https://github.com/colvenna/colvenna-catalog/issues/48)), [#53](https://github.com/colvenna/colvenna-catalog/pull/53) ([#52](https://github.com/colvenna/colvenna-catalog/issues/52)), [#57](https://github.com/colvenna/colvenna-catalog/pull/57) ([#56](https://github.com/colvenna/colvenna-catalog/issues/56)), [#60](https://github.com/colvenna/colvenna-catalog/pull/60) ([#59](https://github.com/colvenna/colvenna-catalog/issues/59)), [#72](https://github.com/colvenna/colvenna-catalog/pull/72) ([#43](https://github.com/colvenna/colvenna-catalog/issues/43)), [#73](https://github.com/colvenna/colvenna-catalog/pull/73) ([#62](https://github.com/colvenna/colvenna-catalog/issues/62)), [#77](https://github.com/colvenna/colvenna-catalog/pull/77) ([#76](https://github.com/colvenna/colvenna-catalog/issues/76)), [#86](https://github.com/colvenna/colvenna-catalog/pull/86) ([#84](https://github.com/colvenna/colvenna-catalog/issues/84)), [#89](https://github.com/colvenna/colvenna-catalog/pull/89) ([#64](https://github.com/colvenna/colvenna-catalog/issues/64)), [#90](https://github.com/colvenna/colvenna-catalog/pull/90) ([#59](https://github.com/colvenna/colvenna-catalog/issues/59)); after the tag, for the retests (the UAT report, section 6), in [#96](https://github.com/colvenna/colvenna-catalog/pull/96) ([#95](https://github.com/colvenna/colvenna-catalog/issues/95)), [#99](https://github.com/colvenna/colvenna-catalog/pull/99) ([#98](https://github.com/colvenna/colvenna-catalog/issues/98)), [#101](https://github.com/colvenna/colvenna-catalog/pull/101) ([#100](https://github.com/colvenna/colvenna-catalog/issues/100)).
- After the tag, for the next version: [#97](https://github.com/colvenna/colvenna-catalog/pull/97) ([#94](https://github.com/colvenna/colvenna-catalog/issues/94)).

## 7. Known limitations

Found by this candidate, beyond the user manual's (its section 9, which this candidate's acceptance leaves as it is):

- Serverless: a throttled API Gateway call fails a serverless install, and running the install again recovers ([#94](https://github.com/colvenna/colvenna-catalog/issues/94)). It met no required case, and one performance install, which the second run completed. Fixed on `main` after the tag ([#97](https://github.com/colvenna/colvenna-catalog/pull/97)), so this candidate, and a release promoted from it, keep it.

## 8. Open issues

The issues that bear on this candidate, the closed ones marked:

| Issue | Impact | Blocks the release |
| --- | --- | --- |
| [#94](https://github.com/colvenna/colvenna-catalog/issues/94) (closed by [#97](https://github.com/colvenna/colvenna-catalog/pull/97)) | The known limitation of section 7; fixed after the tag | No: no required case met it |
| [#95](https://github.com/colvenna/colvenna-catalog/issues/95), [#98](https://github.com/colvenna/colvenna-catalog/issues/98), [#100](https://github.com/colvenna/colvenna-catalog/issues/100) (closed) | Three defects of the UAT harness, which failed four first runs (UAT-DEP-05 EKS in both Regions, UAT-DEP-02 EKS `us-west-2`, UAT-DEP-09 EKS `us-east-2`); fixed after the tag ([#96](https://github.com/colvenna/colvenna-catalog/pull/96), [#99](https://github.com/colvenna/colvenna-catalog/pull/99), [#101](https://github.com/colvenna/colvenna-catalog/pull/101)), and the retests on this candidate passed | No |
| [#103](https://github.com/colvenna/colvenna-catalog/issues/103) | The performance harness's `summary.md` redacts a float's 12 decimals as an account ID (one line of one run); the judging reads `result.json`, and the performance document renders that line from it | No |
| [#105](https://github.com/colvenna/colvenna-catalog/issues/105) | `promote.sh` copies the notes' sections unchanged into `release/<release>/`, where a relative link no longer resolves; these notes link the release check by its commit instead | No |
| [#31](https://github.com/colvenna/colvenna-catalog/issues/31) | StarRocks's `pk read` with short-lived credentials: a declared limitation of the compatibility matrix and the user manual (section 9.2), tracked upstream | No |
| [#8](https://github.com/colvenna/colvenna-catalog/issues/8) | The rename: what it left open, the scan of a candidate's image and chart and a clean install under the new names, are this candidate's rename gate (`PASS`) and UAT (`PASS`) | No |
| [#3](https://github.com/colvenna/colvenna-catalog/issues/3) | The release `v1.0.0`: promoted from this candidate into the private release registry once these notes are merged; the GHCR publish follows once both of the series' repositories are GO (#3, 2026-10-31), and the repository turns public then (the requester's decision of 2026-10-30) | No |
| [#1](https://github.com/colvenna/colvenna-catalog/issues/1), [#2](https://github.com/colvenna/colvenna-catalog/issues/2) | The performance acceptance and the candidate's acceptance: both passed for this candidate | No |

## 9. Upgrades

Before 1.0, versions do not promise upgrade compatibility with each other, and no cross-version rollback is rehearsed. The upgrade, rebuild and recovery paths this candidate claims are in the deployment guide and verified in UAT.

## 10. Trademarks

Apache, Apache Paimon, and Paimon are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. Colvenna Catalog is not affiliated with or endorsed by The Apache Software Foundation.
