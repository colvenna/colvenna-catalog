---
title: "Colvenna Catalog v1.0.0-rc.1 release notes"
date: 2026-10-28
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

# Colvenna Catalog v1.0.0-rc.1 release notes

## 1. Candidate

| Item | Value |
| --- | --- |
| Candidate | `v1.0.0-rc.1` (annotated tag) |
| Built from | commit `2afb93e4` (`2afb93e40a97734f4e3ed3f67c53100fddb01d14`). The tag `v1.0.0-rc.1` points at a commit that adds only `release/v1.0.0-rc.1/` to it |
| Manifest | [`release/v1.0.0-rc.1/manifest.json`](manifest.json): the image and the chart by digest, the git objects of the chart, `install.sh`, `uninstall.sh`, `deploy/lib` and the CDK app, the UAT cases document's version, the required cases and the supported configurations. It names no registry and no account: an install gives the release registry as `COLVENNA_RELEASE_REGISTRY` |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:4aff08682fc587db4f825f96d9734c82d1b03ba720e935882b15552c15f2efd6` (`linux/amd64`), labelled `org.opencontainers.image.revision=2afb93e40a97734f4e3ed3f67c53100fddb01d14` |
| Helm chart | `oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:1d45b659313afd7ce02106b884929fff3a3d5e16ca22b4fb2e445e60c91dbcf3`, version `1.0.0-rc.1`, annotated `colvenna-catalog/source-commit: 2afb93e40a97734f4e3ed3f67c53100fddb01d14`: the chart directory's tree `f52312b73f22732cc2c18956a29bcbd7b4ad4181`, with only `Chart.yaml`'s version, appVersion and that annotation set |
| UAT cases | `docs/uat/cases.md` is absent at the commit the candidate is built from (#19 writes it) |
| Generated | 2026-10-28 |

Install from the release, never building: from a checkout of the tag, with `COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` (the registry that holds this candidate) in the install's configuration (`deploy/config/<env>.env`):

```bash
git checkout v1.0.0-rc.1
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release v1.0.0-rc.1
```

A consumer that runs the chart itself fetches both by digest:

```bash
crane copy <registry>/colvenna-catalog-release/colvenna-catalog@sha256:4aff08682fc587db4f825f96d9734c82d1b03ba720e935882b15552c15f2efd6 <its own repository>:v1.0.0-rc.1
helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:1d45b659313afd7ce02106b884929fff3a3d5e16ca22b4fb2e445e60c91dbcf3
```

Verify that both are `2afb93e40a97734f4e3ed3f67c53100fddb01d14`'s before using them. From a checkout of the tag, `scripts/release/verify-artifacts.sh` checks the image's label, the chart's annotation, and the chart's files against that commit's `deploy/helm/colvenna-catalog` (`install.sh --release` makes the same checks):

```bash
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com scripts/release/verify-artifacts.sh v1.0.0-rc.1
```

By hand, both of these print `2afb93e40a97734f4e3ed3f67c53100fddb01d14`:

```bash
crane config <registry>/colvenna-catalog-release/colvenna-catalog@sha256:4aff08682fc587db4f825f96d9734c82d1b03ba720e935882b15552c15f2efd6 | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@sha256:1d45b659313afd7ce02106b884929fff3a3d5e16ca22b4fb2e445e60c91dbcf3 | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

## 2. Deliverables

| Deliverable | Pinned version | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](../../docs/manual/user.md) | <done / not done, with its issue> |
| Deployment guide | [`docs/manual/install.md`](../../docs/manual/install.md) | <done / not done, with its issue> |
| Install script | [`install.sh`](../../install.sh); the manifest's `installer` holds its blob | <done / not done, with its issue> |
| Cost estimate | [`docs/manual/cost.md`](../../docs/manual/cost.md) | <done / not done, with its issue> |
| UAT cases | [`docs/uat/cases.md`](../../docs/uat/cases.md); the manifest's `uat_cases` holds its version | <done / not done, with its issue> |
| UAT report | `docs/uat/reports/v1.0.0-rc.1/report.md` (at a commit of the early private repository that the record never filled in) | <done / not done, with its issue> |
| Performance document | `docs/performance/v1.0.0-rc.1.md` (at a commit of the early private repository that the record never filled in) | <done / not done, with its issue> |

## 3. Supported configurations

As the manifest's `configurations` says:

- Deployment forms: `eks`.
- Regions: the candidate is accepted in `us-west-2`, `us-east-2`. The installer takes any standard Region (partition `aws`); the others are not accepted for this candidate. China and GovCloud are not supported.

## 4. What the manifest still lacks for GO

- uat_cases: docs/uat/cases.md is absent at built_from: a candidate for GO needs the UAT cases document (#19 writes it)
- required_cases: empty: a candidate for GO needs its required case set, fixed before UAT (#19 writes it)

## 5. Verdict

| Item | Result | Basis |
| --- | --- | --- |
| UAT: <Region 1> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| UAT: <Region 2> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| Performance acceptance | <met / not met> | <the performance document's section> |
| Security scans (ASH, Trivy, cdk-nag, licenses) | <no open finding / open findings> | <the scans' links> |
| CI | <state, e.g. `WAIVED_BY_REQUESTER_NOT_PASS`: GitHub Actions does not start for billing reasons; every pull request ran every job locally on its exact SHA> | <each pull request's local CI record> |
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

> Note: the commit ids (such as `2afb93e4`) and tags (such as `v1.0.0-rc.1`) in this document are those of the project's early private repository; this repository has no pages for them. Links that pointed to a file at a tag or commit of that repository now point to the same file here.
