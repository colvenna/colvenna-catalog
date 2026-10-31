---
title: "Colvenna Catalog v1.0.0-rc.2 release check: the rename gate"
date: 2026-10-30
status: final
issue: 2
---

# `v1.0.0-rc.2` release check: the rename gate

The rename gate of #2 and #8 ("Release 前全面改名前置", 2026-10-25): the candidate's source and its built artifacts, bound to the candidate's SHA and digests, are scanned for the product's former name; the record gives the rules, the hits, the exceptions and the retests. The gate not done, or a required result `FAIL`, `UNKNOWN` or `NOT_RUN`, makes the candidate `NO-GO`. It is modelled on Colvenna Lake's `v1.0.0-rc.5` rename record, which scanned images by digest, entries inside the jars included.

This record does not write the former name: the hits and the exceptions are described, not quoted. The raw hits are in the private evidence (section 7).

## 1. Result

**The rename gate: PASS.**

| Item | Result | Basis |
| --- | --- | --- |
| Source | PASS: `scripts/rename/old_name.py --all` exits 0 on B (635 tracked files) and on the tag (637); the former name is only where the exception list allows it | Sections 3.1, 5 |
| Artifacts | PASS: the image and the chart, read from the release registry by the manifest's digests: 96,314 files, 3 hits, all third-party text; first-party hits 0 | Sections 3.2, 4 |
| Exceptions | 14, each with its path, the text it allows and its reason: the naming history, the import's provenance, the plans executed before the rename, the rename record, the serverless spike's raw results and the check's own files | Section 5 |
| Repository metadata | PASS: the name, description, homepage and topics hold no hit | Section 6 |
| Migration | Not needed: every install of this candidate was made from nothing under the new names | Section 6 |
| Retest | The candidate's UAT, performance and security scans all ran under the new names (#2) | Section 6 |

This gate judges the rename only. The candidate's release verdict is in [its release notes](notes.md).

## 2. The candidate

| Item | Value |
| --- | --- |
| Candidate | `v1.0.0-rc.2`, an annotated tag pointing at `3b3655da72424ad4f088f746bb16e5132a470c39` (the merge of #25) |
| B | `9eefca8848cd2076f263797a70f3d4d0e8130c59` (the merge of #24): the commit the image and the chart were built from; the tag's commit differs from it under `release/v1.0.0-rc.2/` only |
| Manifest | [`release/v1.0.0-rc.2/manifest.json`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/release/v1.0.0-rc.2/manifest.json) |
| When | Source: on B at 13:50 UTC on 2026-10-30, and on the tag at 14:03. Artifacts: 14:03 to 14:12, after the tag was pushed, read from the release registry by digest |

## 3. The rules

### 3.1 Source: `old_name.py`

The rules are in [`scripts/rename/old_name.py`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/scripts/rename/old_name.py)'s description:

- Case does not matter. The former name as a word or the start of one, in a file's text or its path, is a hit; so is a segment of an identifier that a capital starts (camel case).
- Not a hit: inside a word (recover, discover), and the English words that start like it, their letters in the same case (cover, covenant, coverage, covet, covey). Only ASCII letters are letters.
- A hexadecimal literal (`0x` and 8 to 16 digits) whose bytes, either way round, spell it is a hit: Colvenna Lake's `v1.0.0-rc.4` found its catalog's lock key written that way.
- Exceptions only as [`scripts/rename/exceptions.tsv`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/scripts/rename/exceptions.tsv) lists them (section 5).
- The pre-commit hook `old-name` runs it on every commit, and CI's `pre-commit` job on every file.

Result: exit 0 on B (`old_name.py --all`, 635 files) and on the tag's checkout (637 files: B and the two files of `release/v1.0.0-rc.2/`).

### 3.2 Artifacts: the image and the chart

The scanner is Colvenna Lake's `v1.0.0-rc.5` one, unchanged (`scan_image.py`); its driver (`run.sh`) reads this candidate's manifest:

- Scope: the candidate's two artifacts, the server's image (which both forms run: the EKS form's pods, the serverless form's functions, each copied by digest) and the Helm chart. The CDK app, `install.sh`, `uninstall.sh` and `deploy/lib` are in the tag's source and section 3.1 scans them.
- Reading: `crane export --platform linux/amd64` of the image by its digest streams its flattened file system; the chart is pulled by its digest (`helm pull oci://…@<digest>`) and read as the tar it is.
- What it looks at: every file's path and contents; every entry of a `.jar`, `.zip`, `.war`, `.whl` or `.egg` (under 1 GiB), its name and its contents, two levels of nesting down.
- What matches: the former name's four letters, case not mattering, not after a letter, and not before `r` or `n`. It is coarser than section 3.1 (it does not spare covet and covey), so it finds more, each classified (section 4). A constant compiled into a binary is found by its bytes: a long constant of a Java class is stored as its bytes.
- Output: one line per hit (artifact, path, offset, 40 bytes of context) and one `#SUMMARY` line per artifact, in `rename-scan/hits.tsv`.

## 4. The artifacts and their hits

| Artifact | Repository and digest | Files | Hits | First-party |
| --- | --- | --- | --- | --- |
| Image | `colvenna-catalog-release/colvenna-catalog@sha256:5b2cb844590539cf6c4bc2942ce9f3a81e84a42528da26ea7b2e4d19984cc778` (`linux/amd64`) | 96,304 | 3 | 0 |
| Chart | `colvenna-catalog-release/charts/colvenna-catalog@sha256:238ccc4d79d3a7ad6ba1fed98884a31a02ccea3a3be5758e003b889e6fc4029e` (version `1.0.0-rc.2`; its package's SHA-256 `8a64f7a984613d9a0a130a8a36ac5f32a78ddd847f6ea17dc87326a822e0e1a5`) | 10 | 0 | 0 |
| Total | | 96,314 | 3 | 0 |

The three hits, each third-party text that the project did not write and does not name:

| What | Where | Hits | Why it is third-party |
| --- | --- | --- | --- |
| Two field names of JTS's `CoverageSimplifier` (the four letters before `Edge` and `Edges`) | `opt/colvenna-catalog/lib/paimon-format-2.0.0.jar`, in the JTS that Apache Paimon 2.0.0 shades | 2 | JTS's source, brought in by Paimon |
| The English name of Christmas Island's capital, a time zone name | `opt/java/openjdk/lib/modules`, the JDK's localized time zone names | 1 | The JDK's CLDR data |

The image's first-party files, `opt/colvenna-catalog/colvenna-catalog.jar` (the server), `lambda-maintenance.sh` and the server's resources, have no hit; neither has the chart. Colvenna Lake's `v1.0.0-rc.5` scan found the same three hits in its own copy of the catalog's image. Third-party text is not rewritten, and is not an exception: the exception list is about this repository's source.

## 5. The exceptions

[`scripts/rename/exceptions.tsv`](https://github.com/colvenna/colvenna-catalog/blob/v1.0.0-rc.2/scripts/rename/exceptions.tsv) at the tag, 14 entries, each with its path, the text a line must hold (or any line) and its reason:

| Kind | Entries | Paths | Scope | Reason |
| --- | --- | --- | --- | --- |
| Naming history | 3 | `README.md`, `assets/brand/README.md`, `docs/design/v1.0-design.md` | One line each | The former name and icon, and the names the import kept until the rename (#26, #31 of the early private repository) |
| The import's provenance | 2 | `docs/design/colvenna-catalog-lld.md`, `docs/design/v1.0-design.md` | One line each | How the import commit names its source, and the commit the LLD was imported from |
| Plans executed before the rename | 4 | the four plans of 2026-10-24 in `docs/superpowers/plans/` | The whole file | Kept as written, with the names and paths of their time |
| The rename record | 1 | `docs/rename.md` | The whole file | Every old name beside its new one |
| The serverless spike's raw results | 1 | `spikes/serverless/results/*` | The whole directory | Recorded on the server before the rename |
| The check itself | 3 | `scripts/rename/old_name.py`, `test_old_name.py`, `exceptions.tsv` | The whole file | Its definitions, its tests and this list |

This candidate's records (this file, the release notes, the UAT report, the performance document and the security record) need no exception: they are written in the new names, describe the hits without quoting them, and `old_name.py` passes them.

## 6. Repository metadata, migration and retests

**Repository metadata.** `gh repo view colvenna/colvenna-catalog` (2026-10-30): the name `colvenna-catalog`, the description "REST catalog server for Apache Paimon. Open source (Apache-2.0), backed by PostgreSQL; runs on Amazon EKS or serverless on AWS Lambda.", no homepage and no topics: no hit. The issues' titles carry the new names (#8).

**Migration: not needed.** No install under the former name exists (`docs/rename.md`, "已有环境与升级"), and before 1.0 no upgrade compatibility is kept. Every install of this candidate's UAT and performance acceptance was made from nothing, in the new names, from the candidate's tag (`install.sh --release v1.0.0-rc.2`); the upgrade cases start from releases made after the rename (`v1.0.0-rc.1`, and the merge of #59 of the early private repository), so both ends of each upgrade carry the new names.

**Retests.** The candidate's whole acceptance ran under the new names: the UAT in both Regions and both forms (install from nothing, into existing resources, run again, resume, refusals, upgrade and going back, uninstall keeping and deleting the data, the restore of the metadata database), the performance acceptance and the security scans. Their results are the candidate's [UAT report](../../docs/uat/reports/v1.0.0-rc.2/report.md), [performance document](../../docs/performance/v1.0.0-rc.2.md) and [security record](../../docs/security/v1.0.0-rc.2.md). None of their failures is the rename's.

## 7. Evidence

Private, on the execution host, in `<evidence>/rc2/rename-scan/`:

- `source-B.txt` and `source-tag.txt`: `old_name.py --all` on B and on the tag, with the commit and the number of files;
- `manifest.json` (read from the tag), `artifacts.tsv` (the image's and the chart's repository and digest), `chart-sha256.txt`;
- `hits.tsv` (one line per hit, one `#SUMMARY` line per artifact), `scan.log`, `run.stdout`, `image.err`, `chart.err`;
- `scan_image.py` (Colvenna Lake's, unchanged), `run.sh` and `chart-only.sh` (the chart's part run again after its first pull had no directory to write to);
- `repo-metadata.txt`.

`hits.tsv` quotes the context of each hit, the former name with it; this record only describes them.

> Note: issue numbers with a link are this repository's; numbers named "of the early private repository" have no pages here.
