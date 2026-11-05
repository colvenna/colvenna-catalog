# Contributing

This repository contains Colvenna Catalog:

- the server (Java 21, Maven): `src/` and `pom.xml`;
- its deployment: `install.sh`, `uninstall.sh` and `deploy/` (the AWS CDK app in TypeScript, the Helm chart, the install's shell libraries, and the Lambda maintenance loop);
- the tests beyond the build, in `tests/`: the deployment scripts' offline tests, the compatibility matrix, the test environments, the serverless form's live test, the UAT harness and the performance harness;
- the release, rename and security scripts, in `scripts/`;
- the release records, in `release/`;
- the documentation, in `docs/`; the LLD and the design are written in Chinese;
- the brand, in `assets/brand/`.

The [README](README.md) is the overview, and [docs/development.md](docs/development.md) has the details of every step below.

## Delivering a change

1. Every change starts from an issue. Name in it the part of the [LLD](docs/design/colvenna-catalog-lld.md) or the [1.0 design](docs/design/v1.0-design.md) it concerns, or say why none does.
2. Work in a worktree of the issue's own, `.worktrees/issue-<n>-<slug>`, on the branch `codex/issue-<n>-<slug>`, made from the current `origin/main`. Git ignores `.worktrees/`. Never commit on `main`, and leave the main checkout unedited.

   ```bash
   git fetch origin
   git worktree add -b codex/issue-<n>-<slug> .worktrees/issue-<n>-<slug> origin/main
   ```

3. A commit message names the issue: `<kind>: #<n> <what changed>`, the kind saying what the change is (`feat`, `fix`, `docs`, `test`, `ci`, `build`, `deploy`, `release`, `security` and so on), with an area if it helps (`docs(admin): #15 ...`). A commit ported from Colvenna Lake carries the line `Ported-from: colvenna-lake@<full sha>` ([Porting fixes from Colvenna Lake](docs/development.md#porting-fixes-from-colvenna-lake)).
4. Open one pull request for the issue. It links the issue (`Closes #<n>` when it resolves it), and says what changed and how it was validated.
5. Review and CI (GitHub Actions, every job; `scripts/ci-local.sh` only under a recorded waiver, when Actions cannot run) must pass on the pull request's exact head SHA, on a branch rebased onto the current `main`. Whenever `main` moves before the merge, rebase, run CI again, and review what the rebase changed.
6. Merge that exact SHA with a merge commit, and nothing else:

   ```bash
   gh pr merge <n> --merge --match-head-commit <sha>
   ```

   Then remove the worktree and the branch.

The exceptions are the release pull requests. A release candidate's branch is `release-<candidate>`, made at the commit the candidate was built from. It is never rebased, and nothing else merges before it ([Making a candidate](docs/development.md#making-a-candidate)). A release's branch is `release-<release>`, made at its GO candidate's tag commit by `scripts/release/promote.sh`, which tags its commit. It is never rebased, and merges with a merge commit ([Promoting a GO candidate](docs/development.md#promoting-a-go-candidate)).

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) defines eight jobs: `pre-commit`, `shellcheck`, `aws-test`, `deploy-test`, `verify`, `compat-test`, `uat-test` and `perf-test`. GitHub Actions runs them on every pull request and on `main`; [`.github/workflows/ggshield.yml`](.github/workflows/ggshield.yml) runs a ninth, `ggshield`, on every push and pull request ([Secret scanning](#secret-scanning)). Actions is the CI of record: a pull request merges only when every one of these checks has succeeded on its exact head SHA, unless Actions cannot run at all and the waiver below is recorded.

A ruleset on `main` enforces this. It requires a pull request, and the nine checks (`pre-commit`, `shellcheck`, `aws-test`, `deploy-test`, `verify`, `compat-test`, `uat-test`, `perf-test` and `ggshield`, each from GitHub Actions) passing on a branch up to date with `main`. It also blocks force pushes to `main` and its deletion. No one can bypass it. So under a waiver, when the checks cannot pass, a pull request merges only after the repository's owner has changed the ruleset, and the waiver records the change.

[`scripts/ci-local.sh`](scripts/ci-local.sh) runs the same eight jobs on `HEAD`, on your own host:

```bash
scripts/ci-local.sh
```

It is a supplement: run it before you push, or beside Actions when you want a second run; you may post its output, which names the SHA, on the pull request. Only when Actions cannot run (its minutes used up, or an outage) does it become the CI of record ([CI](docs/development.md#ci) has the rule): then it runs on the pull request's exact head SHA, its output is posted on the pull request, and the waiver, `WAIVED` with its reason, is recorded there, never as a GitHub pass. Run it with a clean tree; HEAD must be the same when it ends as when it began. It refuses to run (exit 2) on a dirty tree, when a tool the jobs need is missing, or without a running Docker, and fails (exit 1) if a job fails or leaves changes behind. It needs JDK 21 (`JAVA_HOME` pointing at a 21), Maven, Docker, Node.js with npm, Python 3, pre-commit, ShellCheck, `curl` and `jq`; `scripts/install-tools.sh`, which the jobs run, fetches the pinned kubectl, Helm and crane into `.tools/`. It does not run the `ggshield` check, which needs a GitGuardian API key; under a waiver, [CI](docs/development.md#ci) says what replaces it. Local CI is no substitute for UAT or the performance acceptance.

While working, run the jobs for what you changed; these are the commands `scripts/ci-local.sh` runs:

```bash
# pre-commit: file checks, markdownlint, actionlint, the rename gate and the deployment-values check, then their own tests
# (SKIP=ggshield: the ggshield hook scans staged changes, and the ggshield workflow the commits)
SKIP=ggshield pre-commit run --all-files --show-diff-on-failure
python3 -m unittest discover -s scripts/rename -p 'test_*.py'
python3 -m unittest discover -s scripts/redaction -p 'test_*.py'
# shellcheck: every tracked shell script
git ls-files -z '*.sh' | xargs -0 -r shellcheck -x
# aws-test: the CDK app's types and tests, every stack synthesized (cdk-nag and the guard), helm lint
scripts/aws-test.sh
# deploy-test: the deployment scripts, the release, license and link checks, the security scans' helpers
# (the publication scan's and the ggshield hooks' among them)
scripts/install-tools.sh
python3 -m unittest discover -s tests/deploy
python3 -m unittest discover -s scripts/release -p 'test_*.py'
python3 -m unittest discover -s scripts/security -p 'test_*.py'
# verify: the server (Error Prone, Spotless, unit and Paimon contract tests; Docker), then the serverless test driver
mvn -B clean verify
mvn -B -P serverless-driver clean verify
# compat-test: the compatibility matrix offline
python3 tests/compat/matrix.py check
python3 -m unittest discover -s tests/compat -p 'test_*.py'
mvn -B -f tests/compat/flows/pom.xml clean verify
# uat-test: the UAT harness offline
python3 tests/uat/uat.py check
python3 -m unittest discover -s tests/uat -p 'test_*.py'
mvn -B -f tests/uat/flows/pom.xml clean verify
mvn -B -f tests/uat/paimon/pom.xml clean verify
# perf-test: the performance harness offline (its targets held to docs/performance/terms.md)
python3 tests/performance/perf.py check
python3 -m unittest discover -s tests/performance -p 'test_*.py'
mvn -B -f tests/performance/load/pom.xml clean verify
```

Judge a Maven build by its `[INFO] BUILD SUCCESS` line, never by `mvn -q` and a grep. After changing Java code, run `mvn spotless:apply` (with `-P serverless-driver` for the serverless test driver, `-f tests/compat/flows/pom.xml` for the matrix's flows, `-f tests/uat/paimon/pom.xml` for the UAT harness's Paimon tools, and `-f tests/performance/load/pom.xml` for the performance harness's load tool); the build fails on unformatted code.

## Rules for every change

- **No secrets.** ggshield checks commits before they are pushed (the hooks, a best effort) and after (the `ggshield` check), and everything public before a release ([Secret scanning](#secret-scanning)).
- **Placeholders only.** Do not include credentials, tokens, AWS account IDs, ARNs, real domains, internal addresses, or the role or instance that ran an install, in files, issues, pull requests or commit messages. Use placeholders (`<ACCOUNT_ID>`, `<ARN>`, `example.com`). An install's own values live only in its git-ignored `deploy/config/<env>.env` and `.state/`. The pre-commit hooks `deployment-values` and `deployment-values-in-commit-message` ([`scripts/redaction/redaction.py`](scripts/redaction/redaction.py)) refuse those values, read from the local files and `.state/redaction-values`, and, whatever the values, account IDs, ARNs and registries of real accounts, internal addresses and public IPv4 addresses ([Placeholders only](docs/development.md#placeholders-only)).
- **No anonymous entry, no Lambda function URL.** Every endpoint that can be reached requires authentication (IAM authentication counts), and every entry is tested with unauthenticated and over-privileged requests. Lambda function URLs are never used, whether through CDK, CloudFormation, the CLI or a third-party tool: the CDK app's guard refuses them when it synthesizes, and a review greps for `FunctionUrl` and `Lambda::Url`.
- **The rename gate.** The product's former name and identifiers (see the [README](README.md#naming)) stay gone. The pre-commit hook `old-name` ([`scripts/rename/old_name.py`](scripts/rename/old_name.py)) refuses them in any tracked file's text or path, except on the lines [`scripts/rename/exceptions.tsv`](scripts/rename/exceptions.tsv) allows, each with its reason: the naming history, the import's provenance, the plans executed before the rename, the rename record itself ([`docs/rename.md`](docs/rename.md)), the serverless spike's raw results, and the check's own files. Rename anything else rather than adding an exception. `python3 scripts/rename/old_name.py --all` checks every tracked file.
- **Documents follow the code.** Keep the README, the LLD, the admin API's [OpenAPI description](docs/api/admin-v1.yaml) and [manual page](docs/manual/admin-api.md) (a test holds the description to the server's routes), and the release and security records aligned with the code. Cite an official source (documentation, or source code at a fixed commit) for a new fact about an upstream system, or state it as a design decision. Keep what is implemented, what is proposed and what was validated apart.
- **Third-party licenses.** A new or re-pinned dependency, image or tool updates [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). A change to `pom.xml` or the CDK app's lockfile fails CI until the license scan (`python3 scripts/release/licenses.py scan --update`) has rewritten the record. A jar without a license file of its own needs its license text in [`licenses/`](licenses/) and a row in the record's table of license texts in the image ([Licenses](docs/development.md#licenses)).
- **Links.** No tracked file links to another repository of the series, which are private until each goes public, and every relative link of the Markdown files resolves; the tests in `scripts/release/` check both.
- **No compatibility commitment before 1.0.** A change may break the upgrade from an earlier version. The server keeps Colvenna Lake's HTTP contract, settings, metrics and migration chain exactly, so that Colvenna Lake can run its releases.

## Secret scanning

Nothing is made public before [ggshield](https://docs.gitguardian.com/ggshield-docs/getting-started), GitGuardian's command-line scanner, finds 0 incidents in it with this repository's [`.gitguardian.yaml`](.gitguardian.yaml) (#113). That holds for the history, the GHCR image and chart, the release assets and the GitHub text. The repository is public, so a push publishes its commits. The hooks below are a best-effort check before that, and the ggshield workflow scans each push again afterwards. The publication scan is the gate before a release, and before anything else is made public. ggshield is installed from a hash-locked file, for CPython 3.12 on Linux (arm64 or x86_64):

```bash
python3.12 -m venv ~/.venvs/ggshield
~/.venvs/ggshield/bin/python -m pip install --require-hashes --only-binary=:all: \
  -r scripts/security/ggshield-requirements.lock
export PATH="$HOME/.venvs/ggshield/bin:$PATH" GITGUARDIAN_API_KEY=...  # or: ggshield auth login
```

- **Before a release, and before anything is made public**, a maintainer runs the publication scan, then records its summary ([the security scans](docs/security/scans.md), section 2). Check the remaining API quota first with `ggshield quota`: a full scan uses about 500 calls.

  ```bash
  scripts/install-tools.sh  # crane, which fetches the packages
  python3 scripts/security/ggshield_scan.py --output <new private directory> [--path <file or directory>]
  ```

  It scans everything the public can fetch:
  - an anonymous mirror clone (every branch, tag and `refs/pull/*`), plus each pull-request head that a force push replaced and GitHub still serves; and the wiki when it is enabled. An enabled wiki that cannot be cloned makes the scan fail, and so do enabled discussions, which it does not fetch;
  - every version of the repository's GHCR packages (those linked to it, and those named after it), the image `ghcr.io/colvenna/colvenna-catalog` and the chart `ghcr.io/colvenna/charts/colvenna-catalog`. Each manifest and blob is fetched by its digest and checked against it, and each tag must resolve to its version's digest. Each image is also scanned with `ggshield secret scan docker`, with an empty layer cache;
  - the release assets and workflow artifacts as published, the packages' layers, and any `--path`, with every archive opened (zip, jar, wheel, tar, gzip);
  - the GitHub text: issues, pull requests, comments, reviews, release notes and commit comments.

  It uses the `.gitguardian.yaml` of `main`; `--branch` selects another branch. It refuses any ggshield version other than the one in the lock file, because its checks read that version's messages. It also refuses an output directory that exists or is inside a git work tree.

  ggshield 1.56.1 would leave some content out without notice, so the scan prepares it first, or fails:
  - ggshield exits 0 even when part of a scan failed. An `errors` entry in its report, or "Scanning failed" in its output, makes the scan fail.
  - A text over ggshield's 1 MiB limit is scanned in overlapping parts. ggshield skips content over 4 MiB without reading it, so for content over 1 MiB that is not UTF-8 the scan asks ggshield's decoder first: what it finds text in is scanned in parts, and binary content is counted. Any other file that ggshield skips, except a binary one, makes the scan fail.
  - ggshield's path scan drops files by extension unread (for example `.svg`, `.pdf` and `.jar`), so such a file is scanned under a `.txt` name, and ggshield's decoder decides whether it is text.
  - ggshield's default exclusions (for example `vendor/`, `node_modules/`, `.venv/`, `*.mdx` and `*.sops`) apply to every scan, and its `--ignore-default-excludes` has no effect. Unpacked files that they match are scanned under a neutral name. Any path in the history that they match makes the scan fail, and so does an output directory under such a path.
  - `ggshield secret scan docker` reads only the layers that `COPY` or `ADD` made. It skips system paths (`usr/`, `lib/`, `bin/` and others) and binary files, the image's jars among them, and it skips a layer it found clean before. So the scan opens every layer in full as well, and a layer skipped as scanned before makes it fail.
  - An archive nested more than 6 deep makes the scan fail.

  The sources of these facts, in ggshield's code at `v1.56.1`, are in [the security scans](docs/security/scans.md), section 4.

  It exits 0 only when every scan is complete and finds 0 incidents. The summary counts the binary files that ggshield cannot decode, and those over 1 MiB. The reports in `--output` contain the matched values, so keep that directory private. The summary has no values. It records the date, the commit whose `.gitguardian.yaml` it used, the refs and commits of the history, the ggshield version, the quota, the packages' digests (and whether each release's image and chart, by the digests of its `release/<release>/manifest.json`, are among them), and each finding's location and ignore SHA.

  A release's image and chart are scanned before `scripts/release/publish-ghcr.sh` makes them public, from the release registry by the digests of `release/<release>/manifest.json`, and scanned again from GHCR afterwards, when the summary must also say that each release's image and chart were scanned ([Publishing to GHCR](docs/development.md#publishing-to-ghcr)):

  ```bash
  crane pull <release registry>/colvenna-catalog@<image digest> <directory>/image.tar
  crane pull <release registry>/charts/colvenna-catalog@<chart digest> <directory>/chart.tar
  python3 scripts/security/ggshield_scan.py --output <new private directory> --path <directory>
  ```

- **Each commit and each push (best effort):** `pre-commit install` enables the [ggshield hooks](.pre-commit-config.yaml) with the others, the pre-push hook among them, so the hooks that run at pre-push (the deployment-values and old-name checks too) check the files a push changes. The ggshield hooks use the `ggshield` on `PATH` and refuse any version other than the one in the lock file.
  - The `ggshield` hook scans the staged changes.
  - The `ggshield-push` hook scans the commits about to be published. It covers at most `max_commits_for_hook` commits of a push (1000, set in `.gitguardian.yaml`), and ggshield lets a push through with only a warning when it cannot work out the commit range. When one push updates several refs (`git push --all`, or several branches or tags), pre-commit hands ggshield only the first, so push them one at a time.
  - Without an API key, they stop with ggshield's error.
  - `SKIP=ggshield,ggshield-push`, or `--no-verify` on `git commit` or `git push`, skips them. The ggshield workflow and the publication scan still apply; they cannot be skipped.
- **Each push and pull request:** the [ggshield workflow](.github/workflows/ggshield.yml) runs `ggshield secret scan ci` on the new commits, with the `GITGUARDIAN_API_KEY` Actions secret, which the repository's owner sets. A manual run scans the whole history. Its check, `ggshield`, is one of those the ruleset on `main` requires ([CI](#ci)). Pull requests from forks get no secrets, so for them the job only notes this, and a maintainer scans such a change before merging it.
- **A finding:**
  - A real credential is revoked or rotated first, then the history is reviewed. It is never ignored.
  - Any other finding (a test value, a public example, a false positive) is added to `.gitguardian.yaml` by its ignore SHA, with a name that gives the file and the reason. The addition is reviewed in its pull request, and the record in [the security scans](docs/security/scans.md) gives it.
  - No path or detector is ignored.
  - A tracked file's name should avoid credential words such as "secret", "token" or "password" where a file pairs it with a hash, as a manifest of paths and their SHA-256 does: ggshield 1.56.1 has reported a path with "secret" in it, paired with its SHA-256, as a secret. Rename the file rather than ignore the pair, which would need a new entry each time the file changed.
- **Deployment values:** ggshield does not look for account IDs, ARNs, endpoints or internal addresses. The `deployment-values` hooks ([Placeholders only](docs/development.md#placeholders-only)) check the tracked files and the commit messages; for what the publication scan fetched, a maintainer searches its output too:
  - Each account that a deployment of yours used must appear 0 times anywhere.
  - In the first-party content, list the accounts named in ARNs, the AWS endpoint hosts and the private IPv4 addresses, and review each one. The first-party content is the histories, the release assets and the GitHub text.
  - The packages' layers carry third-party software (Ubuntu, the JRE, the jars), which names documentation and AWS-owned accounts and endpoints, so only your account is searched there. What the image adds of this repository's own is built from the tracked tree, which the hooks check.

  Expected hits are documentation examples (such as `123456789012` and `example.com`), accounts and endpoints that AWS owns, and example addresses:

  ```bash
  ACCOUNT=<your deployment account ID>; DIR=<the scan's output directory>
  histories() { for git in "$DIR"/mirror.git "$DIR"/wiki.git; do if [ -d "$git" ]; then git -C "$git" log --all -p; fi; done; }
  first_party() { histories; grep -rhI '' "$DIR/unpacked/release-assets" "$DIR/unpacked/GitHub-text"; }
  { first_party; grep -rhI '' "$DIR/unpacked/packages" "$DIR/unpacked/workflow-artifacts"; } | grep -c "$ACCOUNT"  # must be 0
  first_party | grep -oE 'arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}:' | grep -oE '[0-9]{12}' | sort | uniq -c
  first_party | grep -oE '[a-z0-9.-]+\.(amazonaws\.com|cloudfront\.net|awsapps\.com)' | sort | uniq -c
  first_party | grep -oE '\b(10\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]+\.[0-9]+\b' | sort | uniq -c
  ```

## Behaviour on AWS

The checks above need no AWS account. Behaviour on AWS needs one of this repository's own test environments (`tests/environment/up.sh`, never another repository's), which creates billable resources; see [Test environments](docs/development.md#test-environments).

For a confidential security concern, follow [SECURITY.md](SECURITY.md).
