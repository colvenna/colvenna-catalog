---
title: "Colvenna Catalog {{candidate}} release notes"
date: {{date}}
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

# Colvenna Catalog {{candidate}} release notes

## 1. Candidate

| Item | Value |
| --- | --- |
| Candidate | `{{candidate}}` (annotated tag) |
| Built from | commit [`{{built_from_short}}`](https://github.com/colvenna/colvenna-catalog/commit/{{built_from}}) (`{{built_from}}`). The tag `{{candidate}}` points at a commit that adds only `release/{{candidate}}/` to it |
| Manifest | [`release/{{candidate}}/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/{{candidate}}/release/{{candidate}}/manifest.json): the image and the chart by digest, the git objects of the chart, `install.sh`, `uninstall.sh`, `deploy/lib` and the CDK app, the UAT cases document's version, the required cases, the performance terms' version and the supported configurations. It names no registry and no account: an install gives the release registry as `COLVENNA_RELEASE_REGISTRY` |
| Image | `{{image_repository}}@{{image_digest}}` (`linux/amd64`), labelled `org.opencontainers.image.revision={{built_from}}` |
| Helm chart | `oci://<registry>/{{chart_repository}}@{{chart_digest}}`, version `{{chart_version}}`, annotated `colvenna-catalog/source-commit: {{built_from}}`: the chart directory's tree `{{chart_tree}}`, with only `Chart.yaml`'s version, appVersion and that annotation set |
| UAT cases | {{uat_cases}} |
| Performance terms | {{performance}} |
| Generated | {{date}} |

Install from the release, never building: from a checkout of the tag, with `COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` (the registry that holds this candidate) in the install's configuration (`deploy/config/<env>.env`):

```bash
git checkout {{candidate}}
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release {{candidate}}
```

A consumer that runs the chart itself fetches both by digest:

```bash
crane copy <registry>/{{image_repository}}@{{image_digest}} <its own repository>:{{candidate}}
helm pull oci://<registry>/{{chart_repository}}@{{chart_digest}}
```

Verify that both are `{{built_from}}`'s before using them. From a checkout of the tag, `scripts/release/verify-artifacts.sh` checks the image's label, the chart's annotation, and the chart's files against that commit's `deploy/helm/colvenna-catalog` (`install.sh --release` makes the same checks):

```bash
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com scripts/release/verify-artifacts.sh {{candidate}}
```

By hand, both of these print `{{built_from}}`:

```bash
crane config <registry>/{{image_repository}}@{{image_digest}} | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/{{chart_repository}}@{{chart_digest}} | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

## 2. Deliverables

| Deliverable | Pinned version | State |
| --- | --- | --- |
| User manual | [`docs/manual/user.md`](https://github.com/colvenna/colvenna-catalog/blob/{{candidate}}/docs/manual/user.md) | <done / not done, with its issue> |
| Deployment guide | [`docs/manual/install.md`](https://github.com/colvenna/colvenna-catalog/blob/{{candidate}}/docs/manual/install.md) | <done / not done, with its issue> |
| Install script | [`install.sh`](https://github.com/colvenna/colvenna-catalog/blob/{{candidate}}/install.sh); the manifest's `installer` holds its blob | <done / not done, with its issue> |
| Cost estimate | [`docs/manual/cost.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/manual/cost.md) | <done / not done, with its issue> |
| UAT cases | [`docs/uat/cases.md`](https://github.com/colvenna/colvenna-catalog/blob/{{candidate}}/docs/uat/cases.md); the manifest's `uat_cases` holds its version | <done / not done, with its issue> |
| UAT report | [`docs/uat/reports/{{candidate}}/report.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/uat/reports/{{candidate}}/report.md) | <done / not done, with its issue> |
| Performance document | [`docs/performance/{{candidate}}.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/performance/{{candidate}}.md) | <done / not done, with its issue> |

## 3. Supported configurations

As the manifest's `configurations` says:

- Deployment forms: {{forms}}.
- Regions: the candidate is accepted in {{regions}}. The installer takes any standard Region (partition `aws`); the others are not accepted for this candidate. China and GovCloud are not supported.

## 4. What the manifest still lacks for GO

{{go_gaps}}

## 5. Verdict

| Item | Result | Basis |
| --- | --- | --- |
| UAT: <Region 1> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| UAT: <Region 2> | PASS <#>, FAIL <#>, BLOCKED <#>, NOT_RUN <#> (<#> required cases) | <the UAT report's section> |
| Performance acceptance | <met / not met> | <the performance document's section> |
| Security scans (ASH, Trivy, grype, cdk-nag, licenses) | <no unhandled finding / unhandled findings>: `scripts/security/scan.sh` on `{{built_from_short}}` | [`docs/security/{{candidate}}.md`](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/security/{{candidate}}.md) |
| Publication scan (ggshield) | <0 incidents, `VERDICT: CLEAN` / findings>: `scripts/security/ggshield_scan.py`, with the image and the chart above pulled from the release registry by their digests (`--path`) | <the scan's record ([what it gives](https://github.com/colvenna/colvenna-catalog/blob/<commit>/docs/security/scans.md#2-running-the-scans)), in `docs/security/{{candidate}}.md`> |
| CI | <`PASS`: GitHub Actions, every job of `ci.yml` and `ggshield`, on each pull request's exact head SHA; or name each pull request merged `WAIVED` (Actions could not run; `scripts/ci-local.sh` and a ggshield scan of its commits on its exact SHA instead), with its reason> | <each pull request's checks, and any waiver's record> |
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
