# Development

## Build

```bash
mvn -B clean verify
```

This compiles with Error Prone, checks formatting with spotless, and runs the unit tests and the upstream Apache Paimon 2.0.0 contract tests (`ColvennaCatalogContractTest`) against a PostgreSQL in Testcontainers. It needs JDK 21 (`JAVA_HOME` pointing at a 21) and Docker. Judge the build by the `[INFO] BUILD SUCCESS` line, never by `mvn -q` and a grep.

After changing Java code, run `mvn spotless:apply` before committing (`mvn -P serverless-driver spotless:apply` for the test driver, `mvn -f tests/compat/flows/pom.xml spotless:apply` for the compatibility matrix's flows, `mvn -f tests/uat/flows/pom.xml spotless:apply` for the UAT harness's engine flows, `mvn -f tests/uat/paimon/pom.xml spotless:apply` for its Paimon tools, `mvn -f tests/performance/load/pom.xml spotless:apply` for the performance harness's load tool); the build fails on unformatted code.

## The serverless form's settings

The settings the Lambda form sets (the LLD's "配置项" and "无服务器形态") are tested without AWS: `NoDatabaseAtStartTest`, `LockTimeoutTest`, `ResponseLimitTest`, `LambdaListenerTest`, `ServerConfigTest`, and `CognitoDeveloperIdentityTest` with stubbed SDK clients. A test poses as a Lambda execution environment by putting `AWS_LAMBDA_RUNTIME_API` into the settings it gives `TestServers.withSettings`; the server reads nothing else to know its form.

## The admin API's description

[`docs/api/admin-v1.yaml`](api/admin-v1.yaml) describes `/colvenna/admin/v1` in OpenAPI 3.0, and `AdminOpenApiTest` holds it to the server:

- every route of `AdminRoutes` is documented once, under the audit action it records (`x-colvenna-action`), and nothing else is;
- each documented operation answers 401 without a token and 403 to a service identity, with an audit event of that action; every other method on a documented path is no route;
- each operation documents the request fields, required fields and query parameters its route reads, as found in `AdminRoutes.resolve`'s source;
- the request examples are of their schemas; run with them, every operation answers with a documented status and a body of the documented shape, and every write's audit event names a resource, a created principal its own name; the documented defaults hold; leaving out a required field answers 400 naming it.

Change a route, a field or a response together with the description, and with the `curl` examples of [the admin API's manual page](manual/admin-api.md).

## CI

`.github/workflows/ci.yml` runs eight jobs on every pull request and on `main`: `pre-commit` (the hooks in `.pre-commit-config.yaml`, then the tests of the old-name check, `python3 -m unittest discover -s scripts/rename -p 'test_*.py'`, and of the deployment-values check, `python3 -m unittest discover -s scripts/redaction -p 'test_*.py'`), `shellcheck` (`shellcheck -x` on every tracked `*.sh`), `deploy-test` (`python3 -m unittest discover -s tests/deploy`: the deployment scripts' logic, with stub commands for AWS and Kubernetes, `deploy/lib/vpc_check.py` and the maintenance function's loop among them; then `python3 -m unittest discover -s scripts/release -p 'test_*.py'`: the release manifest's schema and validation, the promotion of a GO candidate and the publish to GHCR end to end against stub OCI registries (see [Publishing to GHCR](#publishing-to-ghcr)), the third-party record and the links, see [Licenses](#licenses); then `python3 -m unittest discover -s scripts/security -p 'test_*.py'`: the security scans' listing of what Apache Paimon's jars bundle, and how ASH's false positives are suppressed: no entry of `.ash/.ash.yaml` names a line, the premises of its whole-file entries hold, and each detect-secrets mark gives its reason, see [Security scans](#security-scans)), `aws-test` (`scripts/aws-test.sh`: the CDK project's type check and tests, the chart's tests among them; every stack synthesized with placeholder inputs, the serverless form's network (new and existing), images and serverless stacks among them, so cdk-nag, CDK's template validation and the forbidden-resource guard gate it; and `helm lint`), `verify` (the build above, then `mvn -B -P serverless-driver clean verify`: the serverless live test's driver, `tests/serverless/driver`, compiled with the server's dependencies under the same Error Prone and spotless gates, into `target/serverless-driver/`), `compat-test` (the compatibility matrix offline: its declarations checked, its runner's tests, and its engine flows built; see [`tests/compat/README.md`](../tests/compat/README.md)) and `uat-test` (the UAT harness offline: `tests/uat/uat.py check` holds its cases to the cases document, then its tests, then its engine flows built, `mvn -B -f tests/uat/flows/pom.xml clean verify`, and its Paimon tools, `mvn -B -f tests/uat/paimon/pom.xml clean verify`; see [`tests/uat/README.md`](../tests/uat/README.md)) and `perf-test` (the performance harness offline: `tests/performance/perf.py check` holds its targets to [the performance terms](performance/terms.md), cell by cell, then its tests, then its load tool built, `mvn -B -f tests/performance/load/pom.xml clean verify`; see [`tests/performance/README.md`](../tests/performance/README.md)).

`scripts/install-tools.sh` installs the pinned kubectl, Helm and crane into `.tools/`; the deployment scripts and the chart's tests use them from there. Install the hooks locally with `pre-commit install`, which puts in the commit-msg hook too.

GitHub Actions is the CI of record: every job of `ci.yml` must have succeeded on a pull request's exact head SHA before it merges. `scripts/ci-local.sh` runs the same jobs on the current `HEAD`, as a supplement. It refuses to run (exit 2) on a dirty tree, when a tool the jobs need is not on `PATH`, or without a running Docker, and fails (exit 1) if a job fails or leaves changes behind. Only when Actions cannot run (its minutes used up, or an outage) is it the CI of record instead: run on the pull request's exact head SHA, its output posted on the pull request with the waiver, `WAIVED` and its reason. A candidate's release notes list every pull request since the previous candidate that merged under a waiver. Neither is a substitute for UAT or the performance acceptance.

## Test environments

The compatibility matrix, UAT and installer tests run only in this repository's own test environments: `tests/environment/up.sh <name> <region>` brings one up and `down.sh` takes it down (see [`tests/environment/README.md`](../tests/environment/README.md)). They are never shared with other repositories' environments.

The serverless form needs no cluster: `install.sh` with `COLVENNA_FORM=serverless` and `COLVENNA_NETWORK=create` makes its own VPC. Its live test runs against such an install: `tests/serverless/run.sh <env> <region>` deploys a driver function into the catalog's VPC (`deploy/aws/test-env/driver-stack.ts`; its role has no S3 permission) and runs the Paimon 2.0.0 client's create, commit and read cycle through the private API with per-table credentials, the unauthenticated and unauthorized refusals, and cold starts, then checks from the host that the API is unreachable from outside. Take it down with `tests/serverless/down.sh <env> <region>` before `uninstall.sh --delete-data`; `tests/serverless/verify-clean.sh <env> <region>` then proves nothing of the environment is left. While all that is left is what RDS is still deleting, the automated snapshots and backups of instances that no longer exist, it reads the account again every 30 seconds, the last time no later than 20 minutes after the first read, and says how long it waited ([UAT-DEP-09's harness](uat/cases.md#uat-dep-09-uninstall-deleting-the-data), #59).

## Releasing

A release candidate is named `v<major>.<minor>.<patch>-rc.<n>`, `v1.0.0-rc.1` say. It is two things in the release registry, the server's image and its Helm chart, pinned by digest in the candidate's manifest, and the commit they were built from. `release/<candidate>/` holds:

| File | What it is |
| --- | --- |
| `manifest.json` | The manifest ([`scripts/release/manifest.py`](../scripts/release/manifest.py) defines it; schema 1). It records the commit the candidate is built from, B (`source.built_from`). It records the image's repository, tag and digest (`linux/amd64`), and the chart's name, version, repository and OCI digest. It records the git objects of the chart directory (`chart.tree`), `install.sh`, `uninstall.sh`, `deploy/lib` and the CDK app (`deploy/aws`). It records the UAT cases document `docs/uat/cases.md` (its commit and blob, or `absent`) and the required cases it lists, and the performance terms `docs/performance/terms.md` (`performance`: its commit and blob, or `absent`), whose targets the candidate's performance acceptance is judged by. Last come the supported configurations: the deployment forms (`eks`, `serverless`, or both) and two or more standard Regions. It names no registry, no account and no identity |
| `notes.md` | The release notes, from [`release/notes-template.md`](../release/notes-template.md): at the tag, the candidate's identity only. The deliverables' state, the reports and the verdict are added on `main` later, linking immutable versions only |

A release, `v<major>.<minor>.<patch>` (`v1.0.0`), is one of its candidates judged GO and promoted, never rebuilt: the same image digest, and the candidate's chart with only its version and appVersion changed ([Promoting a GO candidate](#promoting-a-go-candidate)). `release/<release>/` holds the same two files and a third: the release's manifest ([`scripts/release/promotion.py`](../scripts/release/promotion.py)), which is the candidate's under the release's names with `promotion`, where it came from; the release notes, the candidate's as accepted; and `chart-comparison.txt`, the release's chart compared with the candidate's, file by file. The release's image and chart then go to the GitHub Container Registry, and only there ([Publishing to GHCR](#publishing-to-ghcr)).

`python3 scripts/release/manifest.py validate <manifest>` checks a manifest field by field, a candidate's or a release's (`--name <name>`: and that it is that candidate's or release's). With `--go`, it also says what the candidate still lacks to be a candidate for GO. A candidate built from a commit without `docs/uat/cases.md` or `docs/performance/terms.md` (`v1.0.0-rc.1` has neither) records them as absent, and the required case set as empty: it can be installed and tried, but it can never be GO. [The UAT cases](uat/cases.md) list the required cases in a table between `<!-- required-cases:start -->` and `<!-- required-cases:end -->` (their section 7), one case ID (`UAT-<AREA>-<nn>`) at the start of each row; `scripts/release/test_uat_cases.py` checks that the table parses and names only cases the document defines. The performance acceptance's scenarios and targets, which the requester confirmed on 2026-10-29, are in [the performance terms](performance/terms.md).

### The release registry

The CDK stack `ColvennaCatalogRelease` ([`deploy/aws/src/release-stack.ts`](../deploy/aws/src/release-stack.ts)) holds two ECR repositories in the maintainer's account:

- `colvenna-catalog-release/colvenna-catalog`: the server's image, tagged `<candidate>`;
- `colvenna-catalog-release/charts/colvenna-catalog`: the Helm chart as an OCI artifact, tagged with its version, the candidate without its `v` (`1.0.0-rc.1`). The chart directory's own `Chart.yaml` keeps version `0.1.0`; the release packages it with the candidate's version, so each candidate's chart has a tag of its own. The chart directory's git tree (`chart.tree`) says whether two candidates' charts are the same.

Tags never move and nothing is removed: the stack keeps both repositories when it is deleted, has no lifecycle rule, and no environment's install or uninstall touches it (the environment name `release` is reserved). Without pull accounts, there is no repository policy, and only the registry's own account reads it, by IAM. Other accounts are listed in `COLVENNA_RELEASE_PULL_ACCOUNTS` (12-digit IDs). Each repository then gets one statement letting those accounts' IAM principals (`arn:aws:iam::<ACCOUNT_ID>:root`) pull, with `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and `ecr:BatchCheckLayerAvailability`. They can never push, delete or list, and the policy is never open to `*`. The CDK tests check both rules for every repository. An installer in a pull account also needs, in its own account, those three actions on the release's repositories and `ecr:GetAuthorizationToken`. That last one can only be granted on `*`, and gives access to no repository by itself.

```bash
AWS_PROFILE=<profile> scripts/release/deploy-registry.sh                         # us-west-2, no other account
COLVENNA_RELEASE_PULL_ACCOUNTS=<ACCOUNT_ID>,<ACCOUNT_ID> scripts/release/deploy-registry.sh   # these may pull
```

Each deploy sets the list anew, from a clean, committed tree; `COLVENNA_RELEASE_REGION` moves the registry to another Region. The registry's address, `<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com`, is never written into the repository: the maintainer gives it to whoever installs.

### Making a candidate

1. On B, a clean commit of `main`, make the candidate:

   ```bash
   AWS_PROFILE=<profile> scripts/release/make-manifest.sh v1.0.0-rc.1
   ```

   It builds from B exactly (`git archive`). It builds the image for `linux/amd64` (`mvn package`, then `docker buildx`, labelled `org.opencontainers.image.revision=<B>`) and pushes it. For the chart, it copies B's chart directory and sets the copy's `Chart.yaml` to version `1.0.0-rc.1` and appVersion `v1.0.0-rc.1`, adding the annotation `colvenna-catalog/source-commit: <B>`. It packages the copy (`helm package`), checks that the package holds exactly the copy's files, and pushes it (`helm push … oci://<registry>/colvenna-catalog-release/charts`). Then it writes `release/v1.0.0-rc.1/manifest.json`, validated and checked against B. It verifies what it pushed as an install does (see [Verifying a candidate](#verifying-a-candidate)), recording the result in `release.txt` of its evidence, and writes `notes.md`. `RELEASE_REGIONS` (default `us-west-2,us-east-2`) gives the Regions the candidate is accepted in, and `RELEASE_FORMS` (default `eks`) the deployment forms: name `serverless` only for a candidate whose serverless form has passed. A run that stopped can run again: an image already under the tag must carry B's label, and a chart already under the version must hold the same files; otherwise the candidate's name is spent. Evidence, including who ran it, goes to `.state/release/<candidate>/`.

2. Commit the two files alone on top of B, merge that pull request, and tag the merged commit, C:

   ```bash
   git switch -c release-v1.0.0-rc.1 <B>
   git add release/v1.0.0-rc.1 && git commit -m "release: v1.0.0-rc.1"
   # once merged, C is the commit on main that adds only release/v1.0.0-rc.1/ to B
   git diff --name-only <B> <C>        # only release/v1.0.0-rc.1/manifest.json and notes.md
   git tag -a v1.0.0-rc.1 -m "v1.0.0-rc.1: built from <B>" <C>
   git push origin v1.0.0-rc.1
   ```

   This pull request is one of the two exceptions to [Delivering a change](#delivering-a-change) (the other is a release's, [Promoting a GO candidate](#promoting-a-go-candidate)): its branch is `release-<candidate>`, not an issue's, and it is never rebased. Between B and that merge, nothing else may be merged into `main`. C must differ from B under `release/v1.0.0-rc.1/` alone, or `install.sh --release` refuses the tag and the candidate must be made again from the new `main`.

### Installing a candidate

From a checkout of the tag, with `COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com` in the install's configuration:

```bash
git checkout v1.0.0-rc.1
AWS_PROFILE=<profile> ./install.sh --config deploy/config/<env>.env --release v1.0.0-rc.1
```

Before it changes anything, `install.sh --release` checks:

- the tree is clean;
- the tree differs from B under `release/<candidate>/` alone;
- the manifest is valid and pins the tree's own git objects;
- the release registry holds the image and the chart by digest. It reads them with `ecr:BatchGetImage`, as a pull account does;
- the image and the chart are B's (see [Verifying a candidate](#verifying-a-candidate));
- the candidate is accepted in the configuration's form (`COLVENNA_FORM` is in its manifest's `configurations.forms`).

A release installs the same way, from a checkout of its tag: `install.sh --release v1.0.0`. Its manifest is the release's ([Promoting a GO candidate](#promoting-a-go-candidate)), held to `promotion.py` as `promotion.py validate` holds it. The tree may differ from B under `release/<candidate>/` and `release/v1.0.0/` alone, and it must be where the manifest says the release came from (`manifest.py check-tree`, which for a release runs `promotion.py`'s; exit 3 when it is not):

- HEAD differs from the candidate's tag commit C (`promotion.tag_commit`) under `release/v1.0.0/` alone;
- `release/<candidate>/manifest.json` at C is the recorded blob (`promotion.manifest`), and the release's manifest is it under the release's names: every field but the release's names (`release`, `source.tag`, `image.tag`, the chart's version, appVersion and digest) and `generated` is the same;
- the acceptance record (`promotion.acceptance.commit`) is in the clone and has C; its `release/<candidate>/notes.md` is the recorded blob and gives the release verdict `GO` (`promotion.py verdict`), beside C's `release/<candidate>/manifest.json`. A clone without it is refused: `git fetch origin`.

The image and the chart are checked as a candidate's are: the chart, version `1.0.0` and appVersion `1.0.0`, must otherwise be B's chart directory as a release packages it, which is what `promote.sh` makes of the candidate's. `scripts/release/test_promote_publish.py` runs these checks on the tag and the chart `promote.sh` made; `tests/deploy/test_release.py` the refusals.

These run before anything about the cluster is checked. Any one failing refuses the install (exit 3). It then installs as from source, but builds nothing. It installs the CDK app's packages afresh with `npm ci` from the tag's lockfile: `deploy/aws/node_modules` is git-ignored, so the one an earlier checkout left could otherwise stand in for the release's. It copies the image by digest (`crane copy`) into the environment's own repository, tagged `<candidate>`; a tag holding another digest is never moved. It pulls the chart by digest (`helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@<digest>`) and installs that package with the copied image. The pods carry the candidate's name (`colvenna-catalog/release`) beside B (`colvenna-catalog/source-sha`); the stacks carry B as their `SourceSha` tag. The serverless form takes the same image: Lambda runs container images only from an ECR repository of its own account and Region, so the image is copied by digest into the environment's repository (the images stack's), and the functions run that digest. Its chart is verified like the image, and not installed.

Any registry holding the same digests can serve as `COLVENNA_RELEASE_REGISTRY`. Another project that runs the chart itself, Colvenna Lake for one, takes both by digest from the manifest:

```bash
crane copy <registry>/colvenna-catalog-release/colvenna-catalog@<image.digest> <its own repository>:<candidate>
helm pull oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@<chart.digest>
```

It then sets the chart's `image.repository` (its own copy) and `image.digest` (`image.digest`), as `install.sh` does.

### Verifying a candidate

The digests in the manifest say what to fetch; the image's label and the chart's annotation say which commit it was made from. From a clean checkout of the tag:

```bash
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com scripts/release/verify-artifacts.sh v1.0.0-rc.1
```

It makes the checks `install.sh --release` makes before it installs, with the same functions (`deploy/lib/release.sh`), and installs nothing. It takes a release's name too (`verify-artifacts.sh v1.0.0`), and checks where the release came from as `install.sh` does. Exit 3 names what is not the candidate's:

- the checkout is the tag's, as above;
- the image, read by `image.digest`, carries the label `org.opencontainers.image.revision` = `source.built_from` (B), and is for `linux/amd64`;
- the chart, pulled by `chart.digest`, has the manifest's version, and is annotated `colvenna-catalog/source-commit` = B;
- the chart holds exactly B's `deploy/helm/colvenna-catalog` (git tree `chart.tree`) as a release packages it. Only `Chart.yaml`'s version, appVersion and that annotation differ. So `chart.digest` is `chart.tree`'s.

By hand, with crane and Helm logged into the registry, the two labels are:

```bash
crane config <registry>/colvenna-catalog-release/colvenna-catalog@<image.digest> \
  | jq -r '.config.Labels["org.opencontainers.image.revision"]'                 # = source.built_from
helm show chart oci://<registry>/colvenna-catalog-release/charts/colvenna-catalog@<chart.digest> \
  | grep 'colvenna-catalog/source-commit'                                       # = source.built_from
```

### Promoting a GO candidate

A release is not built: once a candidate is judged GO, it is promoted (decided on 2026-10-31, #3). Its release notes on `main` record the verdict: the `Release verdict` row of their verdict table says `GO`, and its basis links the release check. From a clean checkout of the candidate's tag, as the release registry's maintainer:

```bash
git fetch origin --tags
git checkout v1.0.0-rc.<n>
AWS_PROFILE=<profile> scripts/release/promote.sh v1.0.0-rc.<n> v1.0.0
```

A candidate promotes to its own version only (`v1.0.0-rc.<n>` to `v1.0.0`). Before it reads anything from a registry, `promote.sh` checks (exit 2 when one fails):

- the tree is clean, and HEAD is the candidate's tag commit, C, which differs from B under `release/<candidate>/` alone; the candidate's manifest is valid, fit for GO (`manifest.py validate --go`), and pins C's git objects;
- the candidate's release notes at `RELEASE_RECORD` (default `origin/main`, which must have C) give the release verdict `GO`, plain, bold or as code, with a basis that links the release check. `NO-GO`, the template's `<GO / NO-GO>`, or a basis that is still the template's or links nothing is refused, and so is a record whose `release/<candidate>/manifest.json` is not C's;
- neither the tag `v1.0.0`, the branch `release-v1.0.0` nor `release/v1.0.0/` exists yet.

Then, before it writes anything into the release registry, it checks what that holds (exit 3 when it is not the candidate's):

- the image, by the candidate's digest, is labelled B and is for `linux/amd64`;
- the candidate's chart, pulled by its digest, is B's chart directory as the candidate's release packaged it, as `verify-artifacts.sh` checks.

It unpacks the candidate's chart package and changes only `Chart.yaml`'s version and appVersion, both to `1.0.0` (`promotion.py rechart`). The annotation `colvenna-catalog/source-commit` stays B. It packages that again and compares the two packages file by file (`promotion.py chart-diff`): every file must be in both, with the same bytes and mode, and every line of `Chart.yaml` but those two must be the same. Any other difference refuses the promotion.

Only then does it write into the release registry:

- the tag `v1.0.0` on the candidate's image. `crane tag` tags a remote image without downloading it ([crane's documentation](https://github.com/google/go-containerregistry/blob/v0.22.1/cmd/crane/doc/crane_tag.md)), so nothing is copied or built, and the digest is the candidate's. The repository's tags are immutable, which refuses a push under a tag it already holds ([Amazon ECR's documentation](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html)), and never a new tag;
- the chart as version `1.0.0` (`helm push`), a new digest. It pulls that back by its digest and compares it with the candidate's again. That comparison is `release/v1.0.0/chart-comparison.txt`.

Last, it writes `release/v1.0.0/manifest.json`, `notes.md` and `chart-comparison.txt`, commits the three files alone on C, on the branch `release-v1.0.0`, and tags that commit `v1.0.0` (annotated). It pushes nothing to git. Push the tag and the branch (`git push origin v1.0.0 release-v1.0.0`), then open the release's pull request from `release-v1.0.0`: it links the candidate's acceptance record and says how the release was promoted.

Like a candidate's, this pull request is an exception to [Delivering a change](#delivering-a-change): its branch is `release-<release>`, made at C, it is never rebased, and it merges with a merge commit, which brings `release/v1.0.0/` to `main`. Unlike a candidate's, the tag is on the branch's own commit, not on the merge, so other pull requests may merge before it.

The release's manifest is the candidate's, under the release's names, with `promotion`. `python3 scripts/release/promotion.py validate release/v1.0.0/manifest.json --release v1.0.0` checks it, and `install.sh --release v1.0.0` installs the release from a checkout of its tag ([Installing a candidate](#installing-a-candidate)). Every field it shares with the candidate's is checked as `manifest.py` checks a candidate's for GO.

| Field | In the release's manifest |
| --- | --- |
| `release` | `v1.0.0`, where a candidate's has `candidate` |
| `source` | The candidate's, `tag` saying that `v1.0.0` points at the candidate's tag commit with `release/v1.0.0/` added, nothing else |
| `image` | The candidate's: the same digest, the tag `v1.0.0` |
| `chart` | The candidate's name, path, tree and repository; version and appVersion `1.0.0` (a candidate's appVersion is its name, `v1.0.0-rc.<n>`), and the new package's digest |
| `promotion` | `candidate`; `tag_commit`, C; `manifest`, the candidate's manifest's path and blob at C; `acceptance`, the candidate's acceptance record: the commit whose `release/<candidate>/notes.md` records GO, that file's blob, and `verdict: GO`; `chart`, the candidate's chart's version and digest, what changed (`version`, `appVersion`) and how many files were compared |
| `installer`, `cdk`, `uat_cases`, `required_cases`, `performance`, `configurations` | The candidate's, unchanged |

The release notes are the candidate's at `RELEASE_RECORD`, as accepted. Their title and heading take the release's name, their date is the promotion's, and their status is `final`. Their first section is the release's identity: the candidate it was promoted from, a link to the acceptance record at its commit, the image, the chart and its comparison. Every other section is the candidate's, unchanged. The candidate's deliverables, verdict and limitations are the release's.

The release registry is the one `make-manifest.sh` uses: the caller's account's in `COLVENNA_RELEASE_REGION`, or `COLVENNA_RELEASE_REGISTRY`. A run that stopped can run again. A `v1.0.0` tag on the image must hold the candidate's digest, and a chart `1.0.0` already there must be the candidate's but for the two fields; each is then kept. Anything else is refused, because tags never move. The image keeps the label `org.opencontainers.image.version=<candidate>` it was built with. The evidence, including who ran it, goes to `.state/release/v1.0.0/`.

### Publishing to GHCR

Once both repositories' releases are GO (M3, design 9.4), the release's image and chart are published to the GitHub Container Registry (GHCR) by the same digests. They are never published to AWS ECR Public: the requester decided on 2026-10-30 that public artifacts go to GHCR only.

```bash
git fetch origin --tags
gh auth refresh -h github.com -s write:packages
COLVENNA_RELEASE_REGISTRY=<ACCOUNT_ID>.dkr.ecr.<region>.amazonaws.com AWS_PROFILE=<profile> \
  scripts/release/publish-ghcr.sh v1.0.0
```

`publish-ghcr.sh` reads `release/v1.0.0/manifest.json` from the tag `v1.0.0`, never the working tree, and validates it. Before it reads anything from a registry, it refuses (exit 2):

- a target that is not the release's GHCR repository: `ghcr.io/colvenna/colvenna-catalog` for the image and `ghcr.io/colvenna/charts/colvenna-catalog` for the chart. `PUBLISH_IMAGE` and `PUBLISH_CHART` may name only those, or a loopback registry's in a rehearsal;
- AWS ECR Public, in any form (`public.ecr.aws`, its API endpoints), as a target or as the release registry;
- a candidate's name: only a release is published.

The credentials it pushes with are `GHCR_USERNAME` and `GHCR_TOKEN`, a token with the `write:packages` scope ([GitHub's documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)). Without them it takes gh's: `gh auth token`, and the login of gh's user. gh's token must have `write:packages` among the scopes it was granted, which `gh api -i /user` reports in its `X-OAuth-Scopes` header ([GitHub's documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)), or it is refused.

It then works in three steps:

1. It checks the release registry's copies (exit 3 when they are not the release's). The image must be labelled `org.opencontainers.image.revision` = `source.built_from` and be for `linux/amd64`. The chart must be the manifest's version and appVersion, annotated `colvenna-catalog/source-commit` = `source.built_from`.
2. It copies each by its digest, tagged `1.0.0`: `crane copy` keeps the digest ([crane's documentation](https://github.com/google/go-containerregistry/blob/v0.22.1/cmd/crane/doc/crane_copy.md)). A tag already there that holds another digest is never moved (exit 3); one that holds the release's is kept.
3. With no credentials at all, it checks what anyone pulls. crane reads an empty configuration: without one, it falls back on `REGISTRY_AUTH_FILE` or Podman's login file ([its keychain](https://github.com/google/go-containerregistry/blob/v0.22.1/pkg/authn/keychain.go)). Each tag must resolve to the manifest's digest. The image must pull whole, with the same label and platform: `crane validate --remote` downloads and digests every layer unless told `--fast` ([crane's documentation](https://github.com/google/go-containerregistry/blob/v0.22.1/cmd/crane/doc/crane_validate.md)). The chart's package, fetched by crane and checked against its digest, must be the same version, appVersion and annotation, and Helm must pull the chart version `1.0.0` with the manifest's digest.

GHCR makes a package private when it is first published. A package linked to a repository (the image's `org.opencontainers.image.source` label links it) inherits that repository's access permissions, but not its visibility. So the first run ends with exit 4: both copies are there, but not readable without credentials. Before it copies, the script prints how to make each package public and how to check it. The steps are the organization's Packages tab, the package, Package settings, Danger Zone, Change visibility, Public; the organization's "Package creation" setting must allow public packages. A public package can never be made private again ([GitHub's documentation](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)). Each check must print `public` ([the REST API](https://docs.github.com/en/rest/packages/packages#get-a-package-for-an-organization); gh's token needs `read:packages`; the chart package's name has its `/` as `%2F`):

```bash
gh api /orgs/colvenna/packages/container/colvenna-catalog --jq .visibility
gh api /orgs/colvenna/packages/container/charts%2Fcolvenna-catalog --jq .visibility
```

Then run `publish-ghcr.sh` again: it keeps the copies, and exits 0 once anyone can pull both. Anyone then fetches them as:

```bash
crane digest ghcr.io/colvenna/colvenna-catalog:1.0.0                        # = image.digest
helm pull oci://ghcr.io/colvenna/charts/colvenna-catalog --version 1.0.0    # Digest: chart.digest
```

The evidence goes to `.state/release/publish-v1.0.0-<time>/`.

Both scripts take a loopback registry (`127.0.0.1:<port>`), read and written over plain HTTP, in place of the release registry or of GHCR, for a rehearsal. Rehearse in a clone of its own: `promote.sh` still commits and tags, and the commit's and the tag's messages then say they are a rehearsal's, never to be pushed. The offline tests do so: `scripts/release/test_promote_publish.py` runs both scripts end to end, in clones of their own. They run against stub OCI registries ([`scripts/release/registry_stub.py`](../scripts/release/registry_stub.py)), with the pinned crane and Helm, and with `aws` and `gh` that answer nothing. `scripts/release/test_promotion.py` tests `promotion.py`. The two scripts share their registry steps in [`scripts/release/registries.sh`](../scripts/release/registries.sh).

### Trial candidates

A trial of the tooling uses a name no real candidate will take, `v0.0.0-rc.<n>`. Its manifest is never committed to `main`, and it is removed from the registry afterwards, by tag (the chart's tag is its version):

```bash
python3 scripts/release/manifest.py names v0.0.0-rc.1 | while IFS=$'\t' read -r _ repository tag; do
  aws ecr batch-delete-image --region "${COLVENNA_RELEASE_REGION:-us-west-2}" --repository-name "$repository" \
    --image-ids "imageTag=$tag"
done
```

### What is not proven yet

- Cross-account pulls. There is one AWS account so far. That another account can pull and cannot push rests on the repository policy's text (read back after a deploy) and on the CDK tests. No second account has pulled a candidate, and no push from a pull account has been tried.
- `make-manifest.sh`'s rerun path has run against the registry once, on a trial candidate whose image and chart were both already pushed: it kept both, rebuilt nothing, and wrote the same manifest. A rerun after a run that stopped between the two pushes has not been tried.
- The serverless form's `install.sh --release`: the image copied by digest into the environment's repository for Lambda. It is covered by the offline tests (`tests/deploy`) and the CDK tests; no candidate has been installed in the serverless form yet. UAT-DEP-01 runs it live ([UAT cases](uat/cases.md), #21).
- The serverless form with `COLVENNA_NETWORK=existing`: the user's VPC, endpoints and database taken as given, as the preflight (`deploy/lib/vpc_check.py`) judges them. Only offline tests so far; the live installs used `COLVENNA_NETWORK=create`. UAT-DEP-02 runs it live ([UAT cases](uat/cases.md), #21).
- The EKS form's restore of the metadata database onto a new instance ([user manual](manual/user.md#62-restoring), section 6.2): the restored instance's preflight, the guards against moving the install off its database, and `uninstall.sh --delete-data`'s refusal while the restored instance is there are covered by the offline tests; UAT-DEP-10 runs it live ([UAT cases](uat/cases.md), #21). The database refusals that need a database set up wrong (publicly accessible, `rds.force_ssl=0`) and an Aurora cluster are offline only.
- `promote.sh` and `publish-ghcr.sh` have run only in their offline tests, against stub registries. Neither has run against the release registry or GHCR: no candidate has been judged GO yet, and GHCR waits for M3. How GHCR answers a read of a package that is not there yet, or not public yet, is taken from the registry API's error codes (`MANIFEST_UNKNOWN`, `NAME_UNKNOWN`, `UNAUTHORIZED`, `DENIED`, [the distribution specification's](https://github.com/opencontainers/distribution-spec/blob/v1.1.0/spec.md#error-codes)) or status 404, 401 or 403, and has not been seen. Any other failure to read a registry fails the run (exit 1); only an answer that something is not there is a mismatch (exit 3).
- `install.sh --release` installs candidates only: it refuses `v1.0.0`. A promoted chart is installed by its digest, as Colvenna Lake installs it, and the first such install (#3) has not been done.

## Licenses

[NOTICE](../NOTICE) carries the attributions the repository and the server's image owe. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) records every third-party component with its license, and the findings. Three of its tables are the license scan's:

```bash
python3 scripts/release/licenses.py scan            # compares the record with a fresh scan; exit 1 on a difference
python3 scripts/release/licenses.py scan --update   # rewrites the three tables and the pom stamp from the scan
```

The scan needs Maven, JDK 21 and Maven Central. It resolves the server's runtime dependencies, the jars `mvn package` copies into `target/lib/` and the Dockerfile into the image, with the licenses their POMs declare (license-maven-plugin 2.5.0). It reads what each of those jars says it bundles (its `META-INF/NOTICE`, the license files it keeps for bundled projects, and the `pom.properties` of other projects it keeps), and the CDK app's lockfile. The raw results go to `.state/licenses/<time>/`. After `--update`, review the diff and the record's findings, and add to NOTICE any new component whose own notice asks for attribution.

The server's image, which both forms run, keeps `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` in `/licenses/`, and in `/licenses/third-party/` the license texts of what it carries without a license file of its own. Those texts are in [`licenses/`](../licenses/), verbatim as their projects publish them, and the record's fourth table, "License texts in the image", says which component each is for. That table is kept by hand. The scan fails while a jar it finds without a license or notice file has no row there, so a new such jar needs its text in `licenses/` and its row, with the text's source. A built image is checked with:

```bash
python3 scripts/release/licenses.py image <image>    # exit 1 unless its /licenses/ is the record's and the tree's
```

`scripts/security/scan.sh` runs this on the image it builds.

The offline tests in `scripts/release/` (the `deploy-test` job) hold the record to the tree:

- the scan was made at the current `pom.xml` (the java table's marker holds its git blob), and every dependency `pom.xml` declares is in that table;
- the npm table is the lockfile;
- every image pinned by digest, every Dockerfile's `FROM`, every tool `scripts/install-tools.sh` downloads, the test environments' AWS Load Balancer Controller and every `pip install` pin is named;
- the Dockerfile copies into `/licenses/` exactly `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` and the texts the texts table names, and the server's stage takes them; each row of that table is a jar of the java table, at its version and license, or an image the Dockerfile copies from, with a text of every license it may be taken under; its jars are those the findings name.

So a change to `pom.xml`, the lockfile or a pin fails CI until the record follows it. The same tests check that no tracked file links to another repository of the series, which are private until each goes public, and that every relative link of the Markdown files resolves. Three places are not checked: the executed plans in `docs/superpowers/plans/`, kept with the names of their time (one of them holds a URL template, not a link, to Colvenna Lake's repository; see the [rename record](rename.md#例外)); `docs/rename.md`, which names this repository's former name; and the `v1.0.0-rc.1` manifest, released with the repository's address before it moved to the colvenna organization. A link to this repository at the address of the clone's origin is its own: until the move, the documents link its issues there. The owner of the address the `v1.0.0-rc.1` manifest records is one of the series' too, so that once the repository has moved, a link to its old address, or to another repository of that owner, is found.

## Security scans

`ASH_REPO=<ash-repo> scripts/security/scan.sh <out dir>` (`<ash-repo>`: a clone of ASH at its tag `v3.5.4`) runs every security scan on `HEAD` and keeps the raw results outside the repository: ASH over the tracked tree, Trivy over the tree and the chart, the server's image (Trivy and grype), what Apache Paimon's jars bundle, cdk-nag's report of every synthesis, and the license listings. [`docs/security/scans.md`](security/scans.md) says what each scan covers and cannot see, how a finding is handled (fixed; suppressed in `.ash/.ash.yaml` or `.trivyignore.yaml` only when it is a verified false positive, with its reason; otherwise left visible and handled in the record), and holds the current baseline. A candidate's scans run on B, and their record is `docs/security/<candidate>.md`, which the release notes link.

## Delivering a change

[CONTRIBUTING.md](../CONTRIBUTING.md) has these steps with their commands, and the rules every change follows.

1. Every change starts from an issue.
2. Work in a dedicated worktree, `.worktrees/issue-<n>-<slug>`, on the branch `codex/issue-<n>-<slug>`; never commit on `main`.
3. Open one pull request for the issue, with the validation it ran.
4. Review, and CI (GitHub Actions on the exact SHA; `scripts/ci-local.sh` there only under a recorded waiver, when Actions cannot run), must pass on a branch rebased onto the current `main`.
5. Merge that exact SHA, then remove the worktree and the branch.

The exceptions are the release pull requests. The first is a release candidate's ([Making a candidate](#making-a-candidate)). Its commit adds `release/<candidate>/` to B, the commit the candidate's image and chart were built from, and nothing else, because `install.sh --release` refuses a tag that differs from B anywhere else. So it is on a `release-<candidate>` branch made at B, it is not rebased onto a newer `main`, and nothing else merges before it. CI ran on B when it merged, and runs on this pull request's head, B with `release/<candidate>/`, as on any other. The second is a release's ([Promoting a GO candidate](#promoting-a-go-candidate)): its commit, which the release's tag points at, adds `release/<release>/` alone to the candidate's tag commit, on a `release-<release>` branch made there. It is not rebased either, and merges with a merge commit; since the tag is on the branch's commit, other pull requests may merge before it.

## Porting fixes from Colvenna Lake

This repository is the only place colvenna-catalog is developed. Until Colvenna Lake finishes its current release, Colvenna Lake changes its own copy of `colvenna-catalog/` only for problems that block that release, and every such fix is ported here. A ported commit carries the line `Ported-from: colvenna-lake@<full sha>`.

Colvenna Lake's operator, CLI and console call this server over HTTP, and Colvenna Lake will run this repository's release instead of its own copy. So the server keeps Colvenna Lake's contract exactly: the management API under `/colvenna/admin/v1`, `colvenna_` tokens, the `colvenna_catalog_*` metrics, the `COLVENNA_*` settings, and the migration chain (`V1`, `V2`, ...) with its `colvenna_schema_version` table.

## The old name

The product's former name and identifiers (see the [README](../README.md)) stay gone. The pre-commit hook `old-name` ([`scripts/rename/old_name.py`](../scripts/rename/old_name.py), Colvenna Lake's check) refuses them in any tracked file's text or path, including a hexadecimal literal that spells the name, except on the lines [`scripts/rename/exceptions.tsv`](../scripts/rename/exceptions.tsv) allows, each with its reason: only the naming history, the import's provenance, the plans executed before the rename, the rename record, the serverless spike's raw results, and the check's own files. `python3 scripts/rename/old_name.py --all` checks every tracked file. Ordinary words that start the same way (cover, recover, discover) are not hits.

## Placeholders only

Documents, issues, pull requests and commit messages use placeholders (`<ACCOUNT_ID>`, `example.com`, `<ARN>`) instead of account IDs, ARNs, real domains or internal addresses. Values for a real deployment live only in local, git-ignored configuration.

The pre-commit hooks `deployment-values` and `deployment-values-in-commit-message` ([`scripts/redaction/redaction.py`](../scripts/redaction/redaction.py), Colvenna Lake's check, #99) refuse them in every text file and in the commit message. What to look for comes from local files only, never from the repository, not even as a hash: the deployment's own settings of every `deploy/config/<env>.env` in every worktree of the clone (the account, bucket, database endpoint and admin secret, release registry, VPC, subnets and API endpoints; not the environment's name, which the tests use as fixtures), and `.state/redaction-values`, one value a line, for what no configuration holds, such as the maintainers' domains and the role and instance that ran an install. Whatever the values, it also refuses:

- a 12-digit number, an account ID's form, that is neither AWS's documentation example nor a synthetic one of the tests (`123456789012`, `111122223333`, `444455556666`, `210987654321`, `777788889999`) nor AWS's own, outside a UUID; an ARN or ECR registry of such an account;
- an internal IPv4 address or `ip-10-…` host name that is not a CIDR;
- a public IPv4 address outside the documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), with or without a prefix length.

A test line whose made-up account, ARN or internal address is the point (a check that refuses it, a sanitizer that redacts it) says `redaction: allow` and why, which never spares a local value. Only [`scripts/redaction/public-addresses.tsv`](../scripts/redaction/public-addresses.tsv) allows a public address, by file and address, each with its reason: versions and section numbers that read as addresses. `python3 scripts/redaction/redaction.py --all` checks every tracked file.
