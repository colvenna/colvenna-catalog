---
title: "Colvenna Catalog v1.0.0-rc.4 release notes"
date: 2026-11-01
status: draft
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

# Colvenna Catalog v1.0.0-rc.4 release notes

## 1. Candidate

| Item | Value |
| --- | --- |
| Candidate | `v1.0.0-rc.4` (annotated tag) |
| Built from | commit [`77984fb2`](https://github.com/colvenna/colvenna-catalog/commit/77984fb24823acc53e4c1e2d00747d494961552d) (`77984fb24823acc53e4c1e2d00747d494961552d`). The tag `v1.0.0-rc.4` points at a commit that adds only `release/v1.0.0-rc.4/` to it |
| Manifest | [`release/v1.0.0-rc.4/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/release/v1.0.0-rc.4/manifest.json): the image and the chart by digest, the git objects of the chart, `install.sh`, `uninstall.sh`, `deploy/lib` and the CDK app, the UAT cases document's version, the required cases, the performance terms' version and the supported configurations. It names no registry and no account: an install gives the release registry as `COLVENNA_RELEASE_REGISTRY` |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68` (`linux/amd64`), labelled `org.opencontainers.image.revision=77984fb24823acc53e4c1e2d00747d494961552d` |
| Helm chart | `oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:06b5328572d2bc80069a0f7ebd164c5e01f331317d20cf6a376d05e4e53e4449`, version `1.0.0-rc.4`, annotated `colvenna-catalog/source-commit: 77984fb24823acc53e4c1e2d00747d494961552d`: the chart directory's tree `f8d49690a9e0ae60e655d4f2a9167f19bef9ebc9`, with only `Chart.yaml`'s version, appVersion and that annotation set |
| UAT cases | `docs/uat/cases.md` at commit `9dd5d05c1b729f30c445d83ec17dc4e168c7eaf2` (blob `00ccb1e17a671a3ebd093b54eea36a03d21c226b`), with 34 required cases |
| Performance terms | `docs/performance/terms.md` at commit `606ced4ea93c50a8a3da710b329fbe5993c8b6a0` (blob `2ae4339058d459a8abe35bc7da62d0f9e8d334c0`): the scenarios, method and targets the performance acceptance is judged by |
| Generated | 2026-11-01 |

Install from the release, never building: from a checkout of the tag, with `COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` (the registry that holds this candidate) in the install's configuration (`deploy/config/<env>.env`):

```bash
git checkout v1.0.0-rc.4
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release v1.0.0-rc.4
```

A consumer that runs the chart itself fetches both by digest:

```bash
crane copy <registry>/colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68 <its own repository>:v1.0.0-rc.4
helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:06b5328572d2bc80069a0f7ebd164c5e01f331317d20cf6a376d05e4e53e4449
```

Verify that both are `77984fb24823acc53e4c1e2d00747d494961552d`'s before using them. From a checkout of the tag, `scripts/release/verify-artifacts.sh` checks the image's label, the chart's annotation, and the chart's files against that commit's `deploy/helm/colvenna-catalog` (`install.sh --release` makes the same checks):

```bash
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com scripts/release/verify-artifacts.sh v1.0.0-rc.4
```

By hand, both of these print `77984fb24823acc53e4c1e2d00747d494961552d`:

```bash
crane config <registry>/colvenna-catalog-release/colvenna-catalog@sha256:47d8834d463ca4b75d82f2c4f1dbb4466fb04cd8fb07c682e1163b3cffcc4a68 | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:06b5328572d2bc80069a0f7ebd164c5e01f331317d20cf6a376d05e4e53e4449 | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

## 2. Deliverables

| Deliverable | Pinned version | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/user.md) | <done / not done, with its issue> |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/manual/install.md) | <done / not done, with its issue> |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/install.sh); the manifest's `installer` holds its blob | <done / not done, with its issue> |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/manual/cost.md) | <done / not done, with its issue> |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.4/docs/uat/cases.md); the manifest's `uat_cases` holds its version | <done / not done, with its issue> |
| UAT report | [`docs/uat/reports/v1.0.0-rc.4/report.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/uat/reports/v1.0.0-rc.4/report.md) | <done / not done, with its issue> |
| Performance document | [`docs/performance/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/performance/v1.0.0-rc.4.md) | <done / not done, with its issue> |

## 3. Supported configurations

As the manifest's `configurations` says:

- Deployment forms: `eks`, `serverless`.
- Regions: the candidate is accepted in `us-west-2`, `us-east-2`. The installer takes any standard Region (partition `aws`); the others are not accepted for this candidate. China and GovCloud are not supported.

## 4. What the manifest still lacks for GO

- None: the manifest has what a candidate for GO needs.

## 5. Verdict

| Item | Result | Basis |
| --- | --- | --- |
| UAT: <Region 1> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| UAT: <Region 2> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| Performance acceptance | <met / not met> | <the performance document's section> |
| Security scans (ASH, Trivy, grype, cdk-nag, licenses) | <no unhandled finding / unhandled findings>: `scripts/security/scan.sh` on `77984fb2` | [`docs/security/v1.0.0-rc.4.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/security/v1.0.0-rc.4.md) |
| CI | <`PASS`: GitHub Actions, every job of `ci.yml`, on each pull request's exact head SHA; or name each pull request merged `WAIVED` (Actions could not run; `scripts/ci-local.sh` on its exact SHA instead), with its reason> | <each pull request's checks, and any waiver's record> |
| Release verdict | <GO / NO-GO> | <the release check report's link> |

## 6. Changes

<The changes since the previous release; for the first, "the first candidate". New capabilities and limits, each in the user manual, the UAT cases, the cost and the performance scope.>

## 7. Known limitations

<Limitations this candidate found, beyond the user manual's; each with its issue.>

## 8. Open issues

<The open issues that affect this candidate: number, impact, whether they block the release.>

## 9. Upgrades

Before 1.0, versions do not promise upgrade compatibility with each other, and no cross-version rollback is rehearsed. The upgrade, rebuild and recovery paths this candidate claims are in the deployment guide and verified in UAT.

## 10. Trademarks

Apache, Apache Paimon, and Paimon are either registered trademarks or trademarks of The Apache Software Foundation in the United States and/or other countries. Colvenna Catalog is not affiliated with or endorsed by The Apache Software Foundation.
