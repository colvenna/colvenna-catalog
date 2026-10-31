---
title: "Colvenna Catalog v1.0.0-rc.2 release notes"
date: 2026-10-31
status: final
---

<!--
The release notes' template. scripts/release/make-manifest.sh fills in the candidate's identity from
its manifest (the candidate, the commit it is built from, the date, the image and chart digests, the
configurations) and writes release/<candidate>/notes.md, which the candidate's tag commit carries
beside the manifest. At the tag, the notes record only who the candidate is. What is in angle
brackets (the deliverables' state, the reports' links, the verdict) is filled in by the release owner
once the material is there, and committed to main's release/<candidate>/notes.md; the release check
links that commit. Rules:

- Every link points at an immutable version: the tag, or a full commit SHA. Never a branch or main.
- Material merged after the tag (the UAT report, the performance document) links the commit that
  merged it.
- Material that is not there yet says "not done" and links the issue that tracks it; nothing is left
  blank. While anything is not done, a required case is not PASS, or the performance acceptance
  misses its fixed targets, these notes can only say NO-GO.
- No password, key or other secret; raw evidence is named by where it is kept, never copied here.
-->

# Colvenna Catalog v1.0.0-rc.2 release notes

## 1. Candidate

| Item | Value |
| --- | --- |
| Candidate | `v1.0.0-rc.2` (annotated tag) |
| Built from | commit [`9eefca88`](https://github.com/colvenna/colvenna-catalog/commit/9eefca8848cd2076f263797a70f3d4d0e8130c59) (`9eefca8848cd2076f263797a70f3d4d0e8130c59`). The tag `v1.0.0-rc.2` points at a commit that adds only `release/v1.0.0-rc.2/` to it |
| Manifest | [`release/v1.0.0-rc.2/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/release/v1.0.0-rc.2/manifest.json): the image and the chart by digest, the git objects of the chart, `install.sh`, `uninstall.sh`, `deploy/lib` and the CDK app, the UAT cases document's version, the required cases, the performance terms' version and the supported configurations. It names no registry and no account: an install gives the release registry as `COLVENNA_RELEASE_REGISTRY` |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:5b2cb844590539cf6c4bc2942ce9f3a81e84a42528da26ea7b2e4d19984cc778` (`linux/amd64`), labelled `org.opencontainers.image.revision=9eefca8848cd2076f263797a70f3d4d0e8130c59` |
| Helm chart | `oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:238ccc4d79d3a7ad6ba1fed98884a31a02ccea3a3be5758e003b889e6fc4029e`, version `1.0.0-rc.2`, annotated `colvenna-catalog/source-commit: 9eefca8848cd2076f263797a70f3d4d0e8130c59`: the chart directory's tree `719ffc05189e2b4ae4f3fa5256e5f79a6e35e6f1`, with only `Chart.yaml`'s version, appVersion and that annotation set |
| UAT cases | `docs/uat/cases.md` at commit `38c482f1daba1cc61d31c42c8fe0a04aa449c7a1` (blob `b6a95852595311f3e524602502b964fec3ed56be`), with 34 required cases |
| Performance terms | `docs/performance/terms.md` at commit `c19d86a4d9e4557e41c4c96db2dd59ec0706a826` (blob `e1759fe7a2abf2825aafe912e0c8ede5cb63c291`): the scenarios, method and targets the performance acceptance is judged by |
| Generated | 2026-10-30 |

Install from the release, never building: from a checkout of the tag, with `COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` (the registry that holds this candidate) in the install's configuration (`deploy/config/<env>.env`):

```bash
git checkout v1.0.0-rc.2
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release v1.0.0-rc.2
```

A consumer that runs the chart itself fetches both by digest:

```bash
crane copy <registry>/colvenna-catalog-release/colvenna-catalog@sha256:5b2cb844590539cf6c4bc2942ce9f3a81e84a42528da26ea7b2e4d19984cc778 <its own repository>:v1.0.0-rc.2
helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:238ccc4d79d3a7ad6ba1fed98884a31a02ccea3a3be5758e003b889e6fc4029e
```

Verify that both are `9eefca8848cd2076f263797a70f3d4d0e8130c59`'s before using them. From a checkout of the tag, `scripts/release/verify-artifacts.sh` checks the image's label, the chart's annotation, and the chart's files against that commit's `deploy/helm/colvenna-catalog` (`install.sh --release` makes the same checks):

```bash
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com scripts/release/verify-artifacts.sh v1.0.0-rc.2
```

By hand, both of these print `9eefca8848cd2076f263797a70f3d4d0e8130c59`:

```bash
crane config <registry>/colvenna-catalog-release/colvenna-catalog@sha256:5b2cb844590539cf6c4bc2942ce9f3a81e84a42528da26ea7b2e4d19984cc778 | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:238ccc4d79d3a7ad6ba1fed98884a31a02ccea3a3be5758e003b889e6fc4029e | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

## 2. Deliverables

| Deliverable | Pinned version | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/user.md) | Done |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/install.md) | Done |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/install.sh); the manifest's `installer` holds its blob | Done; it failed one required case, [#27](https://github.com/colvenna/colvenna-catalog/issues/27) (fixed on `main` for the next candidate), and its serverless form has the defects [#34](https://github.com/colvenna/colvenna-catalog/issues/34), [#54](https://github.com/colvenna/colvenna-catalog/issues/54), [#63](https://github.com/colvenna/colvenna-catalog/issues/63) |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/manual/cost.md) | Done |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/docs/uat/cases.md); the manifest's `uat_cases` holds its version | Done; how UAT-DEP-06/07 count a previous release's own defect was decided on 2026-10-31 ([#55](https://github.com/colvenna/colvenna-catalog/issues/55), on `main` for the next candidate); StarRocks's `pk read` [#31](https://github.com/colvenna/colvenna-catalog/issues/31) is declared a limitation on `main` since [#80](https://github.com/colvenna/colvenna-catalog/pull/80) |
| UAT report | [`docs/uat/reports/v1.0.0-rc.2/report.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/uat/reports/v1.0.0-rc.2/report.md) | Done: UAT not passed (section 5) |
| Performance document | [`docs/performance/v1.0.0-rc.2.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/performance/v1.0.0-rc.2.md) | Done: `FAIL` (section 5) |

## 3. Supported configurations

As the manifest's `configurations` says:

- Deployment forms: `eks`, `serverless`.
- Regions: the candidate is accepted in `us-west-2`, `us-east-2`. The installer takes any standard Region (partition `aws`); the others are not accepted for this candidate. China and GovCloud are not supported.

## 4. What the manifest still lacks for GO

- None: the manifest has what a candidate for GO needs.

## 5. Verdict

| Item | Result | Basis |
| --- | --- | --- |
| UAT: `us-west-2` | PASS 60, FAIL 7, BLOCKED 0, NOT_RUN 0 (67 required runs: 34 cases on the EKS form, 33 on the serverless form) | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/uat/reports/v1.0.0-rc.2/report.md), sections 1 and 5 |
| UAT: `us-east-2` | PASS 54, FAIL 10, BLOCKED 0, NOT_RUN 3 (67 required runs) | [The UAT report](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/uat/reports/v1.0.0-rc.2/report.md), sections 1 and 5 |
| Performance acceptance | Not met: `FAIL` (P1a and P2 on the EKS form, P1b in both forms, P5 on the EKS form) | [The performance document](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/performance/v1.0.0-rc.2.md), section 1 |
| Security scans (ASH, Trivy, grype, cdk-nag, licenses) | No unhandled finding: `scripts/security/scan.sh` on `9eefca88`; but ASH's count on B itself is not the zero [#2](https://github.com/colvenna/colvenna-catalog/issues/2) requires: its 10 actionable findings are verified false positives whose marks were merged after the tag ([#26](https://github.com/colvenna/colvenna-catalog/pull/26)) | [`docs/security/v1.0.0-rc.2.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/docs/security/v1.0.0-rc.2.md) |
| CI | `PASS`: GitHub Actions, every job of `ci.yml`, on each pull request's exact head SHA, for [#15](https://github.com/colvenna/colvenna-catalog/pull/15), [#17](https://github.com/colvenna/colvenna-catalog/pull/17), [#18](https://github.com/colvenna/colvenna-catalog/pull/18), [#19](https://github.com/colvenna/colvenna-catalog/pull/19), [#20](https://github.com/colvenna/colvenna-catalog/pull/20), [#21](https://github.com/colvenna/colvenna-catalog/pull/21), [#22](https://github.com/colvenna/colvenna-catalog/pull/22), [#23](https://github.com/colvenna/colvenna-catalog/pull/23), [#24](https://github.com/colvenna/colvenna-catalog/pull/24), [#25](https://github.com/colvenna/colvenna-catalog/pull/25), [#26](https://github.com/colvenna/colvenna-catalog/pull/26), [#28](https://github.com/colvenna/colvenna-catalog/pull/28), [#30](https://github.com/colvenna/colvenna-catalog/pull/30), [#33](https://github.com/colvenna/colvenna-catalog/pull/33), [#36](https://github.com/colvenna/colvenna-catalog/pull/36), [#40](https://github.com/colvenna/colvenna-catalog/pull/40), [#41](https://github.com/colvenna/colvenna-catalog/pull/41), [#42](https://github.com/colvenna/colvenna-catalog/pull/42), [#45](https://github.com/colvenna/colvenna-catalog/pull/45), [#47](https://github.com/colvenna/colvenna-catalog/pull/47), [#50](https://github.com/colvenna/colvenna-catalog/pull/50), [#53](https://github.com/colvenna/colvenna-catalog/pull/53), [#57](https://github.com/colvenna/colvenna-catalog/pull/57), [#60](https://github.com/colvenna/colvenna-catalog/pull/60), [#61](https://github.com/colvenna/colvenna-catalog/pull/61), [#68](https://github.com/colvenna/colvenna-catalog/pull/68), [#75](https://github.com/colvenna/colvenna-catalog/pull/75) (the candidate's B and the fixes merged since); `WAIVED` for [#69](https://github.com/colvenna/colvenna-catalog/pull/69), [#70](https://github.com/colvenna/colvenna-catalog/pull/70), [#71](https://github.com/colvenna/colvenna-catalog/pull/71), [#72](https://github.com/colvenna/colvenna-catalog/pull/72), [#73](https://github.com/colvenna/colvenna-catalog/pull/73), [#77](https://github.com/colvenna/colvenna-catalog/pull/77), [#78](https://github.com/colvenna/colvenna-catalog/pull/78), [#80](https://github.com/colvenna/colvenna-catalog/pull/80), [#81](https://github.com/colvenna/colvenna-catalog/pull/81), [#82](https://github.com/colvenna/colvenna-catalog/pull/82) (the last being these notes'): Actions does not start jobs for the organization (billing), and by the requester's decision of 2026-10-31 `scripts/ci-local.sh` ran every job of `ci.yml` on each exact head SHA, all passing, with the waiver recorded in each | Each pull request's checks and waiver comment |
| Release verdict | **NO-GO** | [The release check](check.md), beside these notes in the same commit (the one link that cannot name a commit: a commit cannot name itself) |

## 6. Changes

Since `v1.0.0-rc.1` (a candidate of the project's early private repository):

- The serverless form is named: the manifest's forms are `eks` and `serverless`, and the UAT ran every required case of both.
- The product's name is Colvenna Catalog in its code, configuration and artifacts ([#8](https://github.com/colvenna/colvenna-catalog/issues/8)); the rename gate passed ([`release/v1.0.0-rc.2/rename.md`](https://github.com/colvenna/colvenna-catalog/blob/b5b3e6b4ad0e720114ab6df5ae4470984d182c6e/release/v1.0.0-rc.2/rename.md)).
- The manifest pins the UAT cases and the performance terms, so this is the first candidate measured against both.
- The base image is Temurin's 21-jre-noble, build 21.0.13+9 ([#24](https://github.com/colvenna/colvenna-catalog/pull/24)); the CI of record is GitHub Actions, and public artifacts are to go to GHCR only ([#23](https://github.com/colvenna/colvenna-catalog/pull/23)).
- The fixes this candidate's acceptance led to were merged after its tag, for the next candidate: [#28](https://github.com/colvenna/colvenna-catalog/pull/28) ([#27](https://github.com/colvenna/colvenna-catalog/issues/27)), [#36](https://github.com/colvenna/colvenna-catalog/pull/36) ([#35](https://github.com/colvenna/colvenna-catalog/issues/35)), [#41](https://github.com/colvenna/colvenna-catalog/pull/41) ([#37](https://github.com/colvenna/colvenna-catalog/issues/37)), and the harness's (the UAT report, section 6).

## 7. Known limitations

- An EKS install fails where the AWS CLI's default Region is not the install's ([#27](https://github.com/colvenna/colvenna-catalog/issues/27); fixed on `main`).
- The EKS form answers metadata reads about 40 to 50 ms late on kept-alive connections (Nagle's algorithm, [#37](https://github.com/colvenna/colvenna-catalog/issues/37); fixed on `main`): P2's and P5's read latencies miss their targets.
- A commit's cost at the client bounds the commit rate: eight writers commit about 8.7 times a second on the EKS form, and four writers on one table fall behind 2 commits a second (the performance document, section 1; [#1](https://github.com/colvenna/colvenna-catalog/issues/1)).
- Serverless: a request over Lambda's payload limit is answered 503 (retry), not 413 ([#34](https://github.com/colvenna/colvenna-catalog/issues/34); fixed on `main`).
- Serverless: going back to a version without the purge function and forward again fails at `functions` until the orphan log group is deleted ([#54](https://github.com/colvenna/colvenna-catalog/issues/54)); going back over a newer schema publishes a new server version before the refusal ([#63](https://github.com/colvenna/colvenna-catalog/issues/63)); both fixed on `main`.
- ASH's count on B is not zero: 10 detect-secrets false positives in tests, marked after the tag ([#26](https://github.com/colvenna/colvenna-catalog/pull/26)); the next candidate is built from a commit that carries the marks.
- An upgrade from `v1.0.0-rc.1`, whose pods stop at once, can fail a request in flight ([#55](https://github.com/colvenna/colvenna-catalog/issues/55)).
- StarRocks 4.1.6's first read of a primary-key table fails with short-lived credentials: its bundled Hadoop 3.3.4's credentials race (HADOOP-18233; [#31](https://github.com/colvenna/colvenna-catalog/issues/31), a declared limitation from the next candidate, [#80](https://github.com/colvenna/colvenna-catalog/pull/80)).

## 8. Open issues

| Issue | Impact | Blocks the release |
| --- | --- | --- |
| [#1](https://github.com/colvenna/colvenna-catalog/issues/1) | The performance acceptance failed (P1a, P1b, P2, P5); the targets and P1b's investigation are the requester's | Yes |
| [#34](https://github.com/colvenna/colvenna-catalog/issues/34), [#54](https://github.com/colvenna/colvenna-catalog/issues/54), [#63](https://github.com/colvenna/colvenna-catalog/issues/63) | Product defects of the serverless form (section 7), each closed: fixed on `main` since ([#71](https://github.com/colvenna/colvenna-catalog/pull/71), [#81](https://github.com/colvenna/colvenna-catalog/pull/81), [#70](https://github.com/colvenna/colvenna-catalog/pull/70)), for the next candidate | Yes, for this candidate |
| [#55](https://github.com/colvenna/colvenna-catalog/issues/55) (closed) | The counting was decided on 2026-10-31 (cases.md UAT-DEP-06 and 07): the runs that met the previous releases' own defects belong to them once an extra run with this candidate as the previous release passes; it was not made, so they count `FAIL` here | Yes, for this candidate |
| [#31](https://github.com/colvenna/colvenna-catalog/issues/31) | StarRocks's `pk read` with short-lived credentials: Hadoop 3.3.4's credentials race (HADOOP-18233) in the paimon-s3 StarRocks bundles; declared a limitation on `main` ([#80](https://github.com/colvenna/colvenna-catalog/pull/80)) and tracked upstream; UAT-CMP-08 `FAIL` here, by this candidate's declarations | Yes, for this candidate |
| [#59](https://github.com/colvenna/colvenna-catalog/issues/59), [#64](https://github.com/colvenna/colvenna-catalog/issues/64), [#67](https://github.com/colvenna/colvenna-catalog/issues/67) | Defects of the UAT harness ([#43](https://github.com/colvenna/colvenna-catalog/issues/43) and [#62](https://github.com/colvenna/colvenna-catalog/issues/62) fixed since, [#72](https://github.com/colvenna/colvenna-catalog/pull/72), [#73](https://github.com/colvenna/colvenna-catalog/pull/73)); runs that met them after the NO-GO decision were not retested | A clean UAT for the next candidate needs them fixed |
| [#74](https://github.com/colvenna/colvenna-catalog/issues/74) | The execution's procedure errors and host faults, linked from the report | No |
| [#65](https://github.com/colvenna/colvenna-catalog/issues/65) (closed by [#68](https://github.com/colvenna/colvenna-catalog/pull/68)) | `install.sh --release` accepts the final name (`v1.0.0`) | No |

## 9. Upgrades

Before 1.0, versions do not promise upgrade compatibility with each other, and no cross-version rollback is rehearsed. The upgrade, rebuild and recovery paths this candidate claims are in the deployment guide and verified in UAT.

## 10. Trademarks

Apache, Apache Paimon, and Paimon are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. Colvenna Catalog is not affiliated with or endorsed by The Apache Software Foundation.
