# Security scans

The [1.0 design](../design/v1.0-design.md) (section 9.4) makes a release depend on security scans (ASH, Trivy, cdk-nag, licenses) without an unhandled finding, and on ggshield's publication scan finding 0. This page says what is scanned and how, how a finding is handled, what the scanners cannot see, and the baseline #18 set. A candidate's own record follows it (section 7).

## 1. What is scanned

| Scan | Target | Settings | How a false positive is suppressed |
| --- | --- | --- | --- |
| ASH 3.5.4: bandit, checkov, cfn-nag, detect-secrets, grype, npm-audit, opengrep, semgrep, syft | The tracked tree, exported with `git archive` | [`.ash/.ash.yaml`](../../.ash/.ash.yaml): actionable at MEDIUM and above | For a finding of one line, a mark on that line with its reason: `pragma: allowlist secret - <reason>` for detect-secrets and checkov's secret checks, `# nosec <rule> - <reason>` for bandit; for a finding that holds for a whole file, an entry in `.ash/.ash.yaml`, by rule and file, with its reason (section 3) |
| Trivy: vulnerabilities, secrets, misconfigurations | The same tree: `pom.xml` (resolved to its runtime closure), the compatibility matrix's flows' poms, `deploy/aws/package-lock.json` (development dependencies included), the Dockerfiles, and the chart rendered with the values the server requires and the internal NLB on | [`trivy.yaml`](../../trivy.yaml) | An entry in [`.trivyignore.yaml`](../../.trivyignore.yaml) with its statement, pinned to the rule and the file. Each scan also runs with nothing suppressed |
| Trivy and grype | The server's image, built as a release builds it (`mvn package`, then `docker buildx` for `linux/amd64`) | none | none: every image finding is real |
| Trivy and grype | What Apache Paimon's jars in the image bundle, as their `META-INF/NOTICE` files list it ([`bundled_sbom.py`](../../scripts/security/bundled_sbom.py) writes it as a CycloneDX SBOM) | none | none |
| cdk-nag (AWS Solutions rules) | Every synthesis of the `aws-test` job: the six product stacks (the network stack in both modes), the test environment and the serverless test driver | [`deploy/aws/src/nag.ts`](../../deploy/aws/src/nag.ts): an unacknowledged finding fails the synthesis, so the deploy | `acknowledge(...)` beside the resource, with the reason (and `appliesTo` for IAM findings); each synthesis keeps its `NagReport.csv` |
| ggshield 1.56.1, GitGuardian's secret scanner: the publication scan, [`ggshield_scan.py`](../../scripts/security/ggshield_scan.py) | Everything the public can fetch: the history of every ref (pull requests' refs, and the heads that force pushes replaced, among them), every version of the GHCR image and chart with every layer opened (the image with `ggshield secret scan docker` as well), the release assets, the workflow artifacts and the GitHub text. Before that, each commit and push (the hooks, best effort) and each push and pull request (the `ggshield` check) | [`.gitguardian.yaml`](../../.gitguardian.yaml) | An entry of its `ignored_matches`, by ignore SHA, with a name that gives the file and the reason; no path and no detector is ignored |
| Licenses | `scripts/release/licenses.py scan`: the image's runtime jars, what they bundle, which carry no license file, and the CDK app's lockfile, against the record [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md); `licenses.py image`: the image's `/licenses/` against the same record; and the image's Ubuntu packages (Trivy) | [Licenses](../development.md#licenses) | not applicable: licenses are recorded as found |

## 2. Running the scans

```bash
git clone --depth 1 --branch v3.5.4 https://github.com/awslabs/automated-security-helper.git <ash-repo>
ASH_REPO=<ash-repo> scripts/security/scan.sh <out dir>
```

- ASH is the GitHub project at its release tag. A PyPI package of the same name is not ASH and provides no `ash` command.
- `scan.sh` scans `HEAD` of a clean tree and refuses anything else. It scans the tree from `git archive` (a working copy's `node_modules`, `cdk.out` and `target` would bury the scan) and builds from a detached worktree at `HEAD`, which it removes at the end with the image it built.
- It gives Docker an empty configuration, so registries are pulled from anonymously and buildx uses its default builder: ASH runs the image it builds from the local image store, which only the docker driver loads builds into.
- It needs Docker, Trivy, JDK 21 with Maven, Node 22 with npm, `jq` and `python3`. The first run builds ASH's image (about 4.5 GB); a run takes about 12 minutes.
- `<out dir>` must be new or empty, and outside the repository. It gets `summary.txt` (the counts of each scan), `versions.txt` (the tools, and the commit), `exit-codes.tsv`, `logs/`, and each scan's raw results (`ash/`, `trivy/`, `image/`, `bundled/`, `cdk-nag/`, `licenses/`), with the tree scanned in `src/`. Raw results are kept by the maintainer, outside the repository; records cite them by name, never copy them.
- Findings do not fail the run: exit 1 means a scan or the build failed, or that `THIRD_PARTY_NOTICES.md` is not the license scan's or the image's `/licenses/`; 2 that the run was refused.

The publication scan runs on its own, before a release and before anything is made public ([Secret scanning](../../CONTRIBUTING.md#secret-scanning) has its options and what it fetches):

```bash
scripts/install-tools.sh
python3 scripts/security/ggshield_scan.py --output <new private directory>
```

- It scans what the public can fetch, not `HEAD`, and sends it to GitGuardian's API under the maintainer's key: a full scan uses about 500 of the workspace's API calls (`ggshield quota`).
- It needs the pinned ggshield (`scripts/security/ggshield-requirements.lock`), gh, crane and, for the image, Docker.
- Its `summary.txt` gives the date, the scanned commit, the ggshield version, the quota, the packages' digests and each scan's count, and no value. It ends with `VERDICT: CLEAN` (exit 0) only when every scan is complete and finds 0. The record of a release, or of a repository made public, gives that summary's counts.
- Before a release is published to GHCR, it scans the release's image and chart, pulled from the release registry by their digests (`--path`); after, it scans them again from GHCR ([Publishing to GHCR](../development.md#publishing-to-ghcr)).

## 3. How a finding is handled

- Every finding at MEDIUM and above is handled. It is fixed, or suppressed, one by one; a component that cannot be upgraded here is handled as a whole, with every advisory against it. LOW findings are counted, by rule, in the record.
- **Fix it**, in code or by upgrading the component, whenever that is possible here.
- **Suppress it only if it is a verified false positive**: the risk the rule describes does not exist where it matched, checked against the code or the synthesized template.
  - That includes a rule about keeping, auditing or encrypting data, where it matched test-only infrastructure that holds synthetic data only and is part of neither deployment form, as the test environment's cdk-nag acknowledgments do.
  - The suppression goes into the scanner's own mechanism (section 1), with the specific reason, and names no line (#72): a suppression pinned to a line stopped matching whenever the code moved.
  - A false positive of one line is marked on that line, and the mark moves with it. For detect-secrets, which checkov's secret checks follow, it is `pragma: allowlist secret - <reason>` in the line's comment; in Java, whose formatter wraps long lines, `pragma: allowlist nextline secret - <reason>` on the line above. detect-secrets reports a value once a file, at its first unmarked line, so each line that holds the value needs its own mark. For bandit, it is `# nosec <rule> - <reason>`. A real secret written on another line is still reported.
  - Only a finding that holds for a whole file has an entry in `.ash/.ash.yaml`, by rule and file. Those are:
    - the hex-string rule in files that hold digests only (the release manifests, the matrix's run records and pins, and ggshield's ignore SHAs in `.gitguardian.yaml`), where every other secret rule still reports;
    - checkov's checks that judge a Dockerfile, or the `paths` of the admin API's description, as a whole;
    - a dependency's advisory that does not apply where the dependency is used (section 5.3).
  - [`test_ash_config.py`](../../scripts/security/test_ash_config.py) checks that no entry names a line and that each detect-secrets mark gives its reason. It also checks that the premises of those entries hold:
    - each `ADD` of the Dockerfile fetches an https URL by checksum;
    - each array under `paths` is a GET response;
    - the advisory's one copy is the dependency whose use was read (section 5.3).
  - Every suppression must be in use: ASH lists the entries it applied (`used_suppressions` in `ash/ash_aggregated_results.json`), and a mark is in use when its line, without the mark, is a finding.
- **Otherwise it stays visible** in the scanners' output, and the record lists it with what uses the component, how it can be reached, and the decision on it: accepted for the candidate, with the owner's sign-off, or blocking. Until the owner decides, it is pending, and no candidate with it can be GO.
- cdk-nag findings are acknowledged in the CDK code beside the resource, with the reason.
- A ggshield finding is never left visible: the publication scan must find 0.
  - A real credential is revoked or rotated first, then the history is reviewed. It is never ignored.
  - A verified false positive (a test value, a public example, a value that is not a credential) gets an entry in `.gitguardian.yaml`, by its ignore SHA, with a name that gives the file and the reason. Each entry is reviewed in its pull request, and its record here lists it (section 5.4 has the 30 of #111).
  - No path and no detector is ignored.
- Licenses are recorded as the listings find them, with no legal review (the maintainer's decision).

## 4. What the scanners cannot see

| Gap | How it is covered |
| --- | --- |
| ASH's grype reads only the dependencies `pom.xml` declares, not the closure Maven resolves | Trivy resolves `pom.xml`; the image scans read the jars themselves |
| ASH's cdk-nag scanner reports MISSING: it needs a synthesis context | cdk-nag runs in every synthesis (`aws-test`, `install.sh`); `scan.sh` keeps each synthesis's report |
| Apache Paimon's jars carry other projects' classes, relocated or in a plugin's own class path, mostly without `pom.properties`. The image scanners see those that keep one (in `paimon-format` and `paimon-api`) and miss the rest: `paimon-shade-jackson-2` (Jackson 2.14.2 with SnakeYAML 1.33, the server's JSON parser), `paimon-shade-guava-30` and `paimon-s3`'s plugin (Hadoop 3.4.2's S3A with AWS SDK 1.12.319 and 2.29.52, Jackson 2.12.7, Guava 27.0 and more) | `bundled_sbom.py` lists them from the jars' `NOTICE` files, and both scanners read that list (111 entries at the baseline). It covers only what those files name: the S3 plugin also holds Netty and Bouncy Castle classes, which its `NOTICE` does not list with a version and no scan identifies |
| The AWS Lambda Web Adapter is a compiled binary, copied from its image; neither scanner identifies what it is built from | Pinned by digest at 1.1.0, its latest release at the baseline |
| The compatibility matrix's flows (`tests/compat/flows`) declare Flink, Spark and Paimon as `provided`, which Trivy does not resolve | Nothing of theirs ships: the flows run on the engines' official images with the cell's pinned Paimon jars, wherever the matrix's user runs them |
| The Temurin JRE comes as files, not as a package; neither scanner matches it | The base image is pinned by digest and moved to Temurin's latest build of the same tag at each candidate |
| Trivy's secret scanner skips AWS's documented example keys; bandit rates `shell=True` with a literal command LOW | Positive controls at the baseline (section 5) |
| ggshield 1.56.1 leaves content out without notice. Its path scan drops 208 extensions unread, `.svg`, `.pdf` and `.jar` among them ([`_binary_extensions.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/utils/_binary_extensions.py)). It skips a document over 1 MiB, and one over 4 MiB without reading it ([`scannable.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/core/scan/scannable.py)). Its default exclusions (`vendor/`, `.venv/`, `*.mdx` and others) apply to every scan ([`secret_scan_common_options.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/cmd/secret/scan/secret_scan_common_options.py)), and `--ignore-default-excludes` is accepted and never read ([`scan/__init__.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/cmd/secret/scan/__init__.py)). A part of a scan that the API fails is reported, then left out, and the scan exits 0 ([`secret_scanner.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/verticals/secret/secret_scanner.py), `handle_scan_chunk_error`) | The publication scan prepares what ggshield would leave out: texts in parts, content over 1 MiB that is not UTF-8 decoded by ggshield's own decoder first (binary content counted), `.txt` names, neutral names. It fails on a reported error, on a skipped file that is not binary, and on a path of the history that the exclusions match ([Secret scanning](../../CONTRIBUTING.md#secret-scanning)) |
| `ggshield secret scan docker` reads only the layers that `COPY` or `ADD` made, skips system paths (`usr/`, `lib/`, `bin/` and others) and binary files, the image's jars among them, and skips a layer it found clean before ([`docker.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/verticals/secret/docker.py): `LAYER_TO_SCAN_PATTERN`, `FILEPATH_BANLIST`, the layer cache) | The publication scan opens every layer of every version in full and scans its files, jars opened; it runs the Docker scan with an empty layer cache, and fails if a layer is skipped as scanned before |
| The hooks are a best effort. The pre-push hook covers at most `max_commits_for_hook` commits of a push (ggshield's default is 50, [`user_config.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/core/config/user_config.py); 1000 here), and passes with a warning when it cannot work out the commit range ([`prepush.py`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/cmd/secret/scan/prepush.py)). pre-commit gives it only the first ref of a push that updates several ([`hook_impl.py`](https://github.com/pre-commit/pre-commit/blob/v4.6.0/pre_commit/commands/hook_impl.py), `_pre_push_ns`). Either hook can be skipped | The `ggshield` check scans each push and pull request again, and the publication scan the whole history before anything is made public |
| ggshield does not look for deployment values (account IDs, ARNs, endpoints, internal addresses) | The `deployment-values` hooks, and the maintainer's search of what the publication scan fetched ([Secret scanning](../../CONTRIBUTING.md#secret-scanning)) |
| Workflow run logs are public, and no scan reads them | GitHub redacts the secrets it passes to a job from its log ([GitHub's documentation](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions)); the only one is `GITGUARDIAN_API_KEY`, which only the `ggshield` check gets |
| The edit history of issues, pull requests and comments, which GitHub shows to anyone, is not fetched: the publication scan reads their current text | Not covered. A value written there and then edited away stays in the history until a maintainer deletes that revision ([GitHub's documentation](https://docs.github.com/en/communities/moderating-comments-and-conversations/tracking-changes-in-a-comment)) |

## 5. The baseline

Scanned on 2026-10-28: `main` at `cb38d08` when #18 began, and the head of #18's pull request after it (its SHA and `summary.txt` are in the pull request). In between, `main` took the compatibility matrix (#10), the license scan (#17) and the admin API's OpenAPI description (#15): ASH found 61 more findings in them (bandit 12, detect-secrets 48, checkov 1), handled below with the rest. Positive controls on a copy of the tree, with a fake AWS key in `values.yaml`, a fake secret key in a release manifest, `log4j-core` 2.14.1 in `pom.xml` and a Dockerfile `FROM ubuntu:latest` without a `USER`, were each reported: by detect-secrets and checkov, checkov, grype and Trivy, and checkov and Trivy respectively, through the suppressions in place. Trivy reported a fake key that is not AWS's example one.

| Scan | Before | After |
| --- | --- | --- |
| ASH | 80 actionable: bandit 3, cfn-nag 39, checkov 13, detect-secrets 25; then 61 more from #10, #15 and #17 | 0 actionable. 125 findings suppressed by 55 entries, all in use (detect-secrets 73, cfn-nag 39, checkov 13); 13 bandit findings marked `# nosec <rule>` at their lines, each with its reason. 92 LOW bandit findings: `subprocess` in tests and scripts (B603, B607, B404, 71), test fakes (B105, B106, 16), test asserts (B101, 4), an `xml.etree` import (B405, 1) |
| Trivy, the tree | `pom.xml`: 30 vulnerabilities (13 HIGH, 12 MEDIUM, 5 LOW); the lockfile: 0; the chart was not rendered, so not checked | `pom.xml`, the flows' poms and the lockfile: 0. Misconfigurations: 2 LOW (section 6.3); with nothing suppressed, 36: 1 CRITICAL, 4 HIGH, 7 MEDIUM and 24 LOW, of which the 34 suppressed are verified false positives |
| The image (Trivy) | Java: 24 HIGH, 20 MEDIUM, 5 LOW. Ubuntu: 31 MEDIUM, 22 LOW | Java: 11 HIGH, 8 MEDIUM, all inside Paimon's jars (section 6.1). Ubuntu: 27 MEDIUM, 4 LOW, none with a released fix (section 6.2). grype agrees on the Java findings (11 High, 8 Medium) and reports 87 Medium, 25 Low and 3 Negligible for the Ubuntu packages |
| Bundled in Paimon's jars | (not scanned) | Trivy: 30 HIGH, 28 MEDIUM, 2 LOW; grype: 48 High, 41 Medium, 3 Low (section 6.1) |
| cdk-nag | 9 syntheses, no unacknowledged finding | Unchanged: 65 acknowledged findings (the EKS stack 3, storage 5, serverless 11, the test environment 45, the test driver 1), their factual claims checked against the templates (section 6.4) |
| Licenses | (not recorded) | Section 6.5 |

What was fixed:

- The jars Paimon's poms pull into the image unshaded. Jackson, Guava and SnakeYAML Paimon does not use: it runs on its relocated copies (the serverless test driver uses Jackson). lz4-java and snappy-java it uses as they are. Jackson 2.14.2 to 2.21.8 (through `jackson-bom`, SnakeYAML 1.33 to 2.5 with it), Guava 30.1.1 to 33.7.2, lz4-java 1.10.4 to 1.11.4, snappy-java 1.1.10.8 to 1.1.10.12, and the server's logback 1.5.21 to 1.5.38. The build, its tests (the upstream contract tests among them) and the test driver's build pass; neither form has run with them on AWS yet, which the next candidate's UAT does.
- The Temurin base image, the same JDK (21.0.12.1+1) in its 2026-10-26 build, with Ubuntu's fixes for curl, zlib, libpng, libfreetype and libbrotli.
- `tests/deploy/test_release.py` ran `git archive | tar` through a shell (B602, HIGH); it runs both without one.
- `tests/serverless/outside.py` passed its arguments to `urlopen` (B310); it now refuses any URL but https first.
- `tests/serverless/driver/Dockerfile` names the user it runs as (checkov CKV_DOCKER_3, Trivy DS-0002), the server image's `1000:1000`, which it inherited already.
- The compatibility matrix made each pod's shared work directory world-writable in the host's `/tmp` (B103); it is now made inside the runner's 0700 scratch directory, which no other user of the host can enter.
- The license scan's SHA-1 is marked `usedforsecurity=False` (B324): it computes git's blob ids, which are SHA-1 by definition.

What was suppressed, as verified false positives: placeholders and fakes in tests (made-up resource ids, keys, tokens, passwords and Secrets Manager ARNs), the names of the chart's Secrets and of the matrix's mounted secret files, the release manifest's and the matrix records' digests and commit ids, the matrix's scratch directories' modes inside its 0700 directory, its operator-given `--server-log` command, its download of pinned-SHA-256 jars, its reader's SQL from its own fixture and its containers' own `/tmp`, the license scan's parsing of this repository's `pom.xml`, an unredacted `0.0.0.0`, the admin API description's response arrays (CKV_OPENAPI_21: the server writes them, it reads no such input), the tests' `urlopen` of their own stack's https URLs, the RDS CA bundle's checksum-pinned `ADD`, Docker `HEALTHCHECK` (neither EKS nor Lambda reads it), the test driver's `FROM ${BASE}` (always a digest), the chart's ConfigMap keys and namespace, and the serverless spike's stack (`spikes/serverless/template.yaml`), a throwaway of synthetic data deleted when the spike ended (`spikes/serverless/results/cleanup-verification.txt`), whose template was then kept as the spike's record: its rules about keeping, auditing and encrypting data, which it did not hold, and the rest as each entry said. #49 deleted the template, and these suppressions with it (section 5.1).

### 5.1 The owner's decisions, and the spike's files deleted (#49)

On 2026-10-29 the owner decided the four questions #18 left open (#49; sections 6.1, 6.2 and 6.4), and #49 deleted the serverless spike's deployment files: its CloudFormation template, scripts, functions and images. The spike's [report](../research/serverless-spike.md), its ledger and its `results/` stay as its record. With the files went the suppressions only they needed: 21 entries of `.ash/.ash.yaml` (the template's 19 and the spike image's 2), 16 entries of `.trivyignore.yaml` and the spike image's path in a 17th (DS-0026), and the pre-commit hook that checked the template's YAML; and the spike's rows of `THIRD_PARTY_NOTICES.md` (its Python packages and its copy of the Lambda Web Adapter, which the server's image still carries). The spike's stack was CloudFormation written by hand, not CDK, so no cdk-nag acknowledgment was about it; the two accepted SMG4 risks now name the rotation by hand in their reasons.

Rescanned on 2026-10-29. The first rescan found three more false positives, in this change's own [manual page](../manual/database-secrets.md): detect-secrets took a shell variable named `secret` (twice) and a sentence about a Kubernetes Secret for secrets. They were reworded, not suppressed, as was one more that the deployment guide (#14) brought in while this was open: a list of Secrets Manager's actions in [its permissions](../manual/install.md). It also found one cdk-nag acknowledgment not in use: storage's AwsSolutions-S1 on the access log bucket, which cdk-nag passes because that bucket receives the warehouse bucket's logs; it was removed. The heads of #49's pull request were scanned until it merged; their SHAs and `summary.txt` files, with the counts of every scan, are in the pull request. Until #52's UAT harness and test environment and #59 reached `main`, every scan ran clean: no finding at MEDIUM or above that is not suppressed or handled in section 6, and every suppression in use:

| Scan | After #49 |
| --- | --- |
| ASH | 0 actionable, and the 34 entries of `.ash/.ash.yaml` all in use; cfn-nag has nothing left to scan, and the spike's driver's `# nosec` went with it |
| Trivy, the tree | No vulnerability; of the misconfigurations, only section 6.3's LOW findings are visible, and the 4 entries of `.trivyignore.yaml` are all in use |
| The image, and what Paimon's jars bundle | As at the baseline (sections 6.1 and 6.2) |
| cdk-nag | No unacknowledged finding, and every acknowledgment in use |
| Licenses | `licenses.py scan` matches `THIRD_PARTY_NOTICES.md`, and the image's `/licenses/` matches it too (`licenses.py image`, #50) |

Those merges, which #49's last head was rebased onto, brought 22 actionable ASH findings into their tests and the test environment, not yet checked one by one: detect-secrets 21, and bandit 1 (B108, a path in `/tmp`). They also moved the lines that 8 line-pinned detect-secrets suppressions name, in `tests/deploy/` and `tests/compat/test_sanitize.py`, so those suppressions are no longer in use. None of this is the spike's. They stayed visible in the scans until #72 handled them (section 5.2).

### 5.2 Suppressions that name no line, and the findings since (#72)

Scanned on 2026-10-30 at `main` `04843cb`, before the repository moves to the colvenna organization (#99): 44 findings were actionable, detect-secrets 30 and bandit 14 at MEDIUM (B608 12, B108 2), and 10 of the 28 entries of `.ash/.ash.yaml` that named a line no longer matched. Each detect-secrets finding was checked by hand. None is a secret. They are made-up test values (resource ids, access key ids, catalog tokens, the passwords of stubbed answers, Secrets Manager ARNs in AWS's documentation account), the names of Kubernetes and Secrets Manager secrets (`secretName: compat-secret`, `colvenna-catalog-ca-key`), the JDK's default store password `changeit`, the SHA-256 of a pinned upstream file, and three sentences of the manuals that read a Secret's name as a value.

What was fixed:

- B108: the UAT harness's psql pods kept the database's password file in `/tmp`. It is now in a volume of its own, in memory (`/pgpass`).
- The three sentences (the cost estimate, the deployment guide, the test environments) were reworded, not marked.

What was marked on its line, with its reason (section 3): the other detect-secrets false positives, on 50 lines, each a finding on its own without its mark, and the UAT cases' 12 queries that bandit's B608 reads as SQL injection, which the harness builds from names it made and the install's settings. `.ash/.ash.yaml` went from 34 entries to 8, none naming a line; CKV_DOCKER_4 and CKV_OPENAPI_21 now name their file only, which is what checkov judges.

Rescanned with ASH 3.5.4 at this change's head: 0 actionable, the 8 entries all in use, 54 findings suppressed by them, and 284 LOW bandit findings (`subprocess` in tests and scripts, test fakes, test asserts, an `xml.etree` import), counted as section 3 says.

ASH is not a job of `scripts/ci-local.sh`. It runs in an image of its own, about 4.5 GB, built from a clone of its tag, and takes some 4 minutes on the tree. It stays in `scan.sh`, which each candidate's record runs (section 7). What let the findings pile up between candidates was a suppression that stopped matching without a sound; a mark moves with its line, and `test_ash_config.py` keeps line numbers out of `.ash/.ash.yaml`. A change that adds credential-shaped test data, or SQL built from strings, runs ASH (or `scan.sh`) before it merges, and its pull request gives the counts.

### 5.3 An advisory after `v1.0.0-rc.2` (#85)

`v1.0.0-rc.2`'s B was not at 0 on ASH: 10 detect-secrets findings, verified false positives, were marked after its tag (#26), so the release check counted them. Before the next candidate, `main` at `c861bde` was scanned again on 2026-11-01 (`scan.sh`, ASH 3.5.4 from its GitHub tag). detect-secrets, bandit, checkov, cfn-nag, semgrep and opengrep had nothing actionable. There were 5 actionable findings, all one advisory published on 2026-10-30: [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249) (CVE-2026-118764, MEDIUM), a denial of service by glob-pattern amplification in minimatch before 11.0.1.

- **Where it is.** grype's match in `deploy/aws/package-lock.json`, npm audit's on minimatch, and npm audit's entries for the three packages it names after it: `aws-cdk-lib`, `cdk-nag`, and `@aws-cdk/lambda-layer-kubectl-v36`. Trivy finds the same advisory in the lockfile. The lockfile's only minimatch is 10.2.6, bundled inside `aws-cdk-lib` 2.274.0.
- **Why it is not fixed.** No release of aws-cdk-lib carries a fixed minimatch: its latest, 2.275.0, declares `^10.2.6` too, and the fix is minimatch 11.0.1, a major version with no 10.x backport. aws-cdk-lib ships minimatch inside its own package ([`bundleDependencies`](https://docs.npmjs.com/cli/v10/configuring-npm/package-json#bundledependencies)), so the lockfile marks that copy `inBundle` and npm does not resolve it.
- **Why it is a false positive here.** The advisory's growth of glob parts happens in `firstPhasePreProcess` only when `optimizationLevel` is 2 or more. Minimatch's default is 1 ([`src/index.ts`](https://github.com/isaacs/minimatch/blob/v10.2.6/src/index.ts#L567-L573), v10.2.6). aws-cdk-lib's three callers in 2.274.0 never set it; they were read in its published package and at its tag: `Stack`'s match of the bundling-stacks context ([`stack.ts`](https://github.com/aws/aws-cdk/blob/v2.274.0/packages/aws-cdk-lib/core/lib/stack.ts#L1679)), `IgnoreStrategy.glob` for asset exclude patterns ([`fs/ignore.ts`](https://github.com/aws/aws-cdk/blob/v2.274.0/packages/aws-cdk-lib/core/lib/fs/ignore.ts#L139)), and the construct selectors ([`mixins/selectors.ts`](https://github.com/aws/aws-cdk/blob/v2.274.0/packages/aws-cdk-lib/core/lib/mixins/selectors.ts#L111)). Their patterns are this repository's own CDK code and the operator's context, at synthesis on the operator's host. Nothing of aws-cdk-lib is in the server's image or the chart.
- **What was suppressed.** `.ash/.ash.yaml` has the five entries, by rule and file, and `.trivyignore.yaml` has one, for the lockfile.
  - npm audit's three package entries only repeat the advisory, which is reported and judged on its own, at minimatch.
  - npm audit's `npm-audit-transitive-<package>` entries name the package, not the advisory. They hide the repeats of any advisory under those packages. That advisory is still reported and judged at the package that has it, under its own id (ASH 3.5.4's npm-audit scanner names a finding after its advisory whenever the package has one of its own).
  - `test_ash_config.py` holds the lockfile to that one bundled copy, and to aws-cdk-lib 2.274.0, whose callers were read. Another copy, or another aws-cdk-lib, fails it, and the premise must be checked again. Once the copy is at 11.0.1 or later, the advisory's two entries are unused, and they and the three package entries are removed together. The test keeps the five together.

Rescanned at this change's head: ASH has 0 actionable findings, and the 13 entries are all in use (`used_suppressions`). The counts are in the pull request.

### 5.4 A GitGuardian alert, and a scan with ggshield ([#111](https://github.com/colvenna/colvenna-catalog/issues/111))

The repository was made public on 2026-11-04 at 04:52 UTC. GitGuardian then reported a "Generic High Entropy Secret", and gave 04:57:39 UTC as the time it was pushed. No commit was pushed at that time: the alert came from GitGuardian's scan of the history.

- **What it found.** Doris BE heartbeat tokens of destroyed test clusters, in 12 versions of the compatibility matrix's raw run logs (`docs/compat/runs/<run>/raw/doris-4.1.5/be.log`). Each log has two `heartbeat_server.cpp` lines: the token the BE got from its FE, and the token it set. The initial commit `c19d86a` added them, and so did the matrix's runs recorded on 2026-10-31 (`ab594c0`) and 2026-11-01 (`867452a`).
- **What they are.** Each is the internal token that the FE of one test cluster gives its BE in the heartbeat. Each cluster ran in a test environment and was destroyed with it, so these tokens authenticate to nothing that exists. They are not AWS credentials, and they are not catalog tokens.
- **A scan with ggshield.** The same day, the whole history was scanned with ggshield 1.56.1, GitGuardian's own scanner (secrets engine 2.175.0), with nothing ignored. The release's image and chart were scanned too, and had no findings. The history had 30 findings, each checked by hand. None is a credential:

  | Findings | Detector | What they are |
  | --- | --- | --- |
  | 24 | Generic High Entropy Secret | The Doris BE heartbeat tokens above, two in each of the 12 logs |
  | 1 | Generic Password | `deploy/aws/functions/ops.ts`: a database name, passed where the detector expects a password |
  | 1 | Generic Password | `src/test/java/com/colvenna/catalog/testing/TestServers.java`: the password of the tests' throwaway Testcontainers PostgreSQL |
  | 2 | Username Password | `tests/deploy/test_preflight.py` and `tests/deploy/test_serverless.py`: a stubbed AWS CLI answer, with a made-up password |
  | 1 | Generic High Entropy Secret | `tests/uat/cases/cat06.py`: the name of an option a UAT case looks up, not a value |
  | 1 | Base64 Basic Authentication | `tests/uat/cases/sec01.py`: a deliberately wrong Basic credential, which the case sends to be refused |

- **The earlier scan.** Before publication, the whole history was scanned with gitleaks over every commit's patch of every ref, and with ASH's detect-secrets over every version of every file. There were no private keys, no real AWS keys (only the tests' made-up access key ids), no catalog tokens and no GitHub tokens. The owner accepted the heartbeat tokens as they are.
- **Decision (the owner, 2026-11-04): accepted.** The history is not rewritten. The owner is to mark the incident in GitGuardian as a test credential, revoked.
- **Remediation.**
  - The sanitizer: since [#108](https://github.com/colvenna/colvenna-catalog/pull/108) (`7b45a98`) it redacts a token written after a colon, as the BE writes this one. The logs in the tree hold `<REDACTED>` in its place.
  - [`.gitguardian.yaml`](../../.gitguardian.yaml), the settings of ggshield, for anyone who runs it on the repository or its history. Its `ignored_matches` lists each of the 30 findings by its ignore SHA, ggshield's SHA-256 of the matched string and its type ([`get_ignore_sha`](https://github.com/GitGuardian/ggshield/blob/v1.56.1/ggshield/core/filter.py), ggshield 1.56.1), with a name that says what the finding is. The file holds no value. It ignores no path and no detector, so any other value is reported, a new one included. With it, ggshield's scan of the history from the repository's root finds 0 incidents.
  - `.ash/.ash.yaml` suppresses detect-secrets' hex-string rule in `.gitguardian.yaml`, a file of digests only, as for the release manifests.
  - GitGuardian documents this file for ggshield only ([configuration](https://docs.gitguardian.com/ggshield-docs/configuration)). For the repositories it monitors, it documents exclusions set in its dashboard ([exclusion rules](https://docs.gitguardian.com/secrets-detection/customize-detection/exclusion-rules)).

Rescanned at this change's head: detect-secrets, through ASH, has 0 actionable findings, and the four hex-string entries of `.ash/.ash.yaml` are all in use, the new one for each of the file's 30 ignore SHAs. Trivy's secret scanner reports nothing. The counts, and those of ggshield, are in the pull request.

### 5.5 The publication scan, the hooks and the `ggshield` check ([#113](https://github.com/colvenna/colvenna-catalog/issues/113))

On 2026-11-04 at 07:51 UTC, before the repository was made public again, it was scanned with ggshield 1.56.1 (secrets engine 2.175.0) and the `.gitguardian.yaml` of `main` at `99c0d0b`, with 0 incidents in each scan:

- the history: a new `git clone --mirror` (1 branch, 6 tags, every `refs/pull/*`) and the 48 pull-request heads that force pushes had replaced, 313 commits;
- the image `ghcr.io/colvenna/colvenna-catalog:1.0.0` (`sha256:47d8834d…4a68`), with `ggshield secret scan docker`, again with an empty layer cache;
- the chart `ghcr.io/colvenna/charts/colvenna-catalog` 1.0.0 (`sha256:891ba036…1cbb`), unpacked, with `ggshield secret scan path -r`;
- the release `v1.0.0` has no assets.

Issue #113 makes that gate permanent:

- [`ggshield_scan.py`](../../scripts/security/ggshield_scan.py), the publication scan, scans everything the public can fetch ([Secret scanning](../../CONTRIBUTING.md#secret-scanning)). Unlike that first scan, it opens every layer of the image, its jars included, and the history scan fails if a path matches ggshield's default exclusions (section 4). [`test_ggshield_scan.py`](../../scripts/security/test_ggshield_scan.py), in the `deploy-test` job, tests it offline: archives and layers opened, parts and their lines, large content, the names, the registry's digests and tags, the releases' digests, and what makes a summary incomplete.
- The hooks `ggshield` and `ggshield-push` ([`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)) scan each commit and each push, as a best effort, with the ggshield that [`ggshield-requirements.lock`](../../scripts/security/ggshield-requirements.lock) pins and hash-locks; `.gitguardian.yaml` raises their limit to 1000 commits a push.
- The `ggshield` check ([`ggshield.yml`](../../.github/workflows/ggshield.yml)) scans each push and pull request, with the `GITGUARDIAN_API_KEY` Actions secret. The ruleset on `main` requires it with `ci.yml`'s eight jobs, from #113's merge on ([CI](../../CONTRIBUTING.md#ci)).

Before #113 merged, the publication scan ran on its pull request's head, with that head's `.gitguardian.yaml`. Its summary's counts are in the pull request and on #113.

## 6. Open findings and their handling

### 6.1 Inside Apache Paimon 2.0.0's jars

These copies are inside Paimon's own jars, so they cannot be upgraded here. Moving to Paimon 2.1.0 would not change the first rows: its build pins the same `paimon-shade` 0.8.0 (Jackson 2.14.2, Guava 30.1.1), snappy-java 1.1.10.8 and protobuf-java 3.19.6. What its other jars bundle was not checked. The advisories are those of the baseline's image scan and bundled-component scan (Trivy); grype reports the same components.

| Component | In | HIGH advisories | MEDIUM advisories | What the server does with it |
| --- | --- | --- | --- | --- |
| Jackson 2.14.2, SnakeYAML 1.33 | `paimon-shade-jackson-2` (`paimon-api`'s NOTICE names them too) | CVE-2025-52999, CVE-2026-68494, CVE-2026-89425, CVE-2026-54512, CVE-2026-54513, CVE-2026-68497, CVE-2026-91776, CVE-2026-91777; SnakeYAML CVE-2022-1471 | CVE-2026-19032, CVE-2026-54514, CVE-2026-54515, CVE-2026-59889, CVE-2026-77310, CVE-2026-83557 | Parses every request body and the JSON the server stores. Bodies are read only after authentication and authorization, at most 64 MiB, within the concurrency limit. Paimon's mapper has no default typing; its polymorphic types (schema, view and function changes, predicates) use names with closed subtype lists, with no default implementation and no `Comparable` base; none of its request types has `@JsonIdentityInfo`, XML date or duration, `Path`, `InetAddress`, `InetSocketAddress`, `@JsonView` or `@JsonUnwrapped` members; the server parses strings, not `DataInput` or the non-blocking parser. A body nested 100,000 levels deep is parsed without a stack overflow; a recursive type nested that deep (a predicate, say) can overflow the stack, which the server catches (`RestServer` catches every `Error`) and answers with 500. No YAML from a client is parsed |
| Guava 30.1.1 | `paimon-shade-guava-30`, `paimon-api` | CVE-2026-102554 | CVE-2023-2976 | Java deserialization of Guava's collections, and temporary files of `FileBackedOutputStream`; the server deserializes no Java objects from clients and does not use that class |
| Jackson 2.14.3 (two relocated copies), protobuf-java 3.19.6, commons-lang 2.6, commons-compress 1.22 | `paimon-format` | Jackson as in the first row, without SnakeYAML; protobuf CVE-2024-7254 | Jackson as in the first row; commons-lang CVE-2025-48924; commons-compress CVE-2023-42503, CVE-2024-25710, CVE-2024-26308 | Reads Avro manifests, in the admin API's consistency check only (an admin principal), from files that engines with write grants on the table wrote. The server reads no ORC (protobuf) or Parquet data, and no archives |
| httpclient5 5.4.2 and 5.5.1, httpcore5 5.3.3 and 5.3.6 (with h2) | `paimon-api`, `paimon-core` | CVE-2025-27820, CVE-2026-54399, CVE-2026-54428 | CVE-2026-64607 | Paimon's REST client. The server makes no REST calls: it serves them on the JDK's HTTP server |
| Jackson 2.12.7, Guava 27.0, commons-beanutils 1.9.4, commons-io 2.8.0, dnsjava 2.1.7, ion-java 1.0.2, snappy-java 1.1.10.8, commons-compress 1.21, commons-configuration2 2.1.1, woodstox 5.3.0 | `paimon-s3`'s plugin | Jackson CVE-2025-52999, CVE-2026-68494, CVE-2026-89425, CVE-2022-42003, CVE-2022-42004, CVE-2026-54512, CVE-2026-54513, CVE-2026-91776, CVE-2026-91777; Guava CVE-2026-102554; CVE-2025-48734; CVE-2024-47554; CVE-2024-25638; CVE-2024-21634; snappy-java CVE-2026-108106, CVE-2026-93451 | Jackson CVE-2025-49128, CVE-2026-19032, CVE-2026-50193, CVE-2026-54514, CVE-2026-54515, CVE-2026-59889, CVE-2026-77310, CVE-2026-83557; Guava CVE-2023-2976; snappy-java CVE-2026-116590; commons-compress CVE-2024-25710, CVE-2024-26308; commons-configuration2 CVE-2024-29131, CVE-2024-29133; woodstox CVE-2022-40152 | Hadoop's S3A, the server's FileIO: it talks to S3 alone, over TLS, with the server's role, and reads and writes objects under the warehouse prefix as bytes (their JSON is parsed by the first row's Jackson) |

**Decision (the owner, 2026-10-29, #49): accepted for the 1.0 candidates, and reported to Apache Paimon upstream.** The maintainer files the upstream report; #49 links it.

### 6.2 Ubuntu packages without a released fix

At the baseline, Trivy reports 27 MEDIUM and 4 LOW advisories in the base image's packages, every one "affected" with no fixed version in Ubuntu 24.04, and grype 87 Medium, 25 Low and 3 Negligible, every one "not-fixed" or "wont-fix":

- both: dash, glibc (`libc6`, `libc-bin`, `locales`), libexpat1, libgnutls30t64, libpcre2-8-0, libsasl2, tar and wget (MEDIUM); libsystemd0, libudev1, login and passwd (LOW);
- grype only: util-linux and its libraries (`bsdutils`, `libblkid1`, `libmount1`, `libsmartcols1`, `libuuid1`, `mount`), Kerberos (`libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0`), PAM (`libpam0g`, `libpam-modules`, `libpam-modules-bin`, `libpam-runtime`), libacl1, librtmp1 and perl-base (Medium); coreutils and GnuPG (`gnupg`, `gpg`, `gpgv` and their companions) (Low).

The server process is the JVM, which links glibc; it runs no shell, tar, wget, Perl or GnuPG, does its TLS in Java, not GnuTLS, and signs in with no Kerberos or PAM. The chart's probes run curl against the pod's own health port. **Decision (the owner, 2026-10-29, #49): accepted.** Each candidate moves the base image to Temurin's latest build of the tag (section 7, step 1), which takes Ubuntu's fixes as they are released.

### 6.3 LOW findings left visible

The chart's pod runs as UID and GID 1000 (Trivy KSV-0020, KSV-0021: 10000 or less): the image's own unprivileged user, the one Hadoop's login finds in `/etc/passwd`. The pod does not run as root and cannot escalate (`runAsNonRoot`, no privilege escalation, every capability dropped).

### 6.4 cdk-nag acknowledgments

The acknowledgments' factual claims were checked against the synthesized templates: the IAM resources and conditions, the database's settings, the API's private endpoint, the test cluster's endpoint and logs. All were made before #18. Two accept a risk, and say so; the owner accepted both (2026-10-29, #49), and [Rotating the database secrets](../manual/database-secrets.md) says how to rotate those secrets by hand. Three record a design choice the rule questions:

| Stack | Rule | Kind | What the acknowledgment says |
| --- | --- | --- | --- |
| storage | AwsSolutions-SMG4 | Accepted risk | The database's master secret is not rotated: `install.sh` uses it only to create the catalog's own user. Rotated by hand: on the instance, then in the secret |
| eks | AwsSolutions-SMG4 | Accepted risk | The catalog's database secret is not rotated by Secrets Manager: `install.sh` copies it into the Kubernetes Secret the catalog reads at start. Rotated by hand: a new value in the secret, `install.sh` run again to set it on the user, then the pods restarted |
| serverless | AwsSolutions-SMG4 | Design | The admin token is the catalog's own, which Secrets Manager cannot make; `install.sh` issues a new one when less than a month is left |
| storage | AwsSolutions-RDS3 | Design | The database is single-AZ by default: a deployment that needs Multi-AZ brings its own |
| storage | AwsSolutions-RDS11 | Design | The default port: the security group admits only the catalog's clients |

### 6.5 Licenses

[`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) is the record, and its findings section says what the license scan found; at the baseline `licenses.py scan` matches it (74 jars, 120 components they bundle, 119 npm packages). What #18 changed in it: the new versions of the unshaded jars, `jspecify` added, Logback 1.5.38 declaring `EPL-2.0 OR LGPL-2.1-only` (1.5.21's POM named the EPL 1.0 and the LGPL without a version), and 13 jars, not 16, without a license or notice file of their own. The record does not list the image's Ubuntu packages: Trivy lists each with the licenses Ubuntu distributes it under, the GPL and LGPL families among them (`licenses/image.json`).

## 7. A candidate's record

For each candidate, on B (the commit its image and chart are built from):

1. Before B, move the base image's digest to Temurin's latest build of its tag, so that the image takes the fixes released since.
2. Run `scripts/security/scan.sh` on a clean checkout of B.
3. Write `docs/security/<candidate>.md`: B and the scan's date; the tools and databases (`versions.txt`); the summary's counts against the previous record; every finding at MEDIUM and above that is not suppressed, with its handling (section 6's, where it is unchanged); and the owner's decisions.
4. A finding new since the previous record is fixed, suppressed as a verified false positive, or decided by the owner before the candidate can be GO. The release notes link the record from their verdict.

> Note: the issue and pull request numbers (such as #18) in this document are those of the project's early private repository; this repository has no pages for them. Linked issue and pull request numbers (such as [#111](https://github.com/colvenna/colvenna-catalog/issues/111)) are this repository's current ones.
