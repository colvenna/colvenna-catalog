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

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) defines eight jobs: `pre-commit`, `shellcheck`, `aws-test`, `deploy-test`, `verify`, `compat-test`, `uat-test` and `perf-test`. GitHub Actions runs them on every pull request and on `main`, and it is the CI of record: a pull request merges only when every one of these checks has succeeded on its exact head SHA, unless Actions cannot run at all and the waiver below is recorded.

[`scripts/ci-local.sh`](scripts/ci-local.sh) runs the same eight jobs on `HEAD`, on your own host:

```bash
scripts/ci-local.sh
```

It is a supplement: run it before you push, or beside Actions when you want a second run; you may post its output, which names the SHA, on the pull request. Only when Actions cannot run (its minutes used up, or an outage) does it become the CI of record ([CI](docs/development.md#ci) has the rule): then it runs on the pull request's exact head SHA, its output is posted on the pull request, and the waiver, `WAIVED` with its reason, is recorded there, never as a GitHub pass. Run it with a clean tree; HEAD must be the same when it ends as when it began. It refuses to run (exit 2) on a dirty tree, when a tool the jobs need is missing, or without a running Docker, and fails (exit 1) if a job fails or leaves changes behind. It needs JDK 21 (`JAVA_HOME` pointing at a 21), Maven, Docker, Node.js with npm, Python 3, pre-commit, ShellCheck, `curl` and `jq`; `scripts/install-tools.sh`, which the jobs run, fetches the pinned kubectl, Helm and crane into `.tools/`. Local CI is no substitute for UAT or the performance acceptance.

While working, run the jobs for what you changed; these are the commands `scripts/ci-local.sh` runs:

```bash
# pre-commit: file checks, markdownlint, actionlint, the rename gate and the deployment-values check, then their own tests
pre-commit run --all-files --show-diff-on-failure
python3 -m unittest discover -s scripts/rename -p 'test_*.py'
python3 -m unittest discover -s scripts/redaction -p 'test_*.py'
# shellcheck: every tracked shell script
git ls-files -z '*.sh' | xargs -0 -r shellcheck -x
# aws-test: the CDK app's types and tests, every stack synthesized (cdk-nag and the guard), helm lint
scripts/aws-test.sh
# deploy-test: the deployment scripts, the release, license and link checks, the security scans' helpers
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

- **Placeholders only.** Do not include credentials, tokens, AWS account IDs, ARNs, real domains, internal addresses, or the role or instance that ran an install, in files, issues, pull requests or commit messages. Use placeholders (`<ACCOUNT_ID>`, `<ARN>`, `example.com`). An install's own values live only in its git-ignored `deploy/config/<env>.env` and `.state/`. The pre-commit hooks `deployment-values` and `deployment-values-in-commit-message` ([`scripts/redaction/redaction.py`](scripts/redaction/redaction.py)) refuse those values, read from the local files and `.state/redaction-values`, and, whatever the values, account IDs, ARNs and registries of real accounts, internal addresses and public IPv4 addresses ([Placeholders only](docs/development.md#placeholders-only)).
- **No anonymous entry, no Lambda function URL.** Every endpoint that can be reached requires authentication (IAM authentication counts), and every entry is tested with unauthenticated and over-privileged requests. Lambda function URLs are never used, whether through CDK, CloudFormation, the CLI or a third-party tool: the CDK app's guard refuses them when it synthesizes, and a review greps for `FunctionUrl` and `Lambda::Url`.
- **The rename gate.** The product's former name and identifiers (see the [README](README.md#naming)) stay gone. The pre-commit hook `old-name` ([`scripts/rename/old_name.py`](scripts/rename/old_name.py)) refuses them in any tracked file's text or path, except on the lines [`scripts/rename/exceptions.tsv`](scripts/rename/exceptions.tsv) allows, each with its reason: the naming history, the import's provenance, the plans executed before the rename, the rename record itself ([`docs/rename.md`](docs/rename.md)), the serverless spike's raw results, and the check's own files. Rename anything else rather than adding an exception. `python3 scripts/rename/old_name.py --all` checks every tracked file.
- **Documents follow the code.** Keep the README, the LLD, the admin API's [OpenAPI description](docs/api/admin-v1.yaml) and [manual page](docs/manual/admin-api.md) (a test holds the description to the server's routes), and the release and security records aligned with the code. Cite an official source (documentation, or source code at a fixed commit) for a new fact about an upstream system, or state it as a design decision. Keep what is implemented, what is proposed and what was validated apart.
- **Third-party licenses.** A new or re-pinned dependency, image or tool updates [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). A change to `pom.xml` or the CDK app's lockfile fails CI until the license scan (`python3 scripts/release/licenses.py scan --update`) has rewritten the record. A jar without a license file of its own needs its license text in [`licenses/`](licenses/) and a row in the record's table of license texts in the image ([Licenses](docs/development.md#licenses)).
- **Links.** No tracked file links to another repository of the series, which are private until each goes public, and every relative link of the Markdown files resolves; the tests in `scripts/release/` check both.
- **No compatibility commitment before 1.0.** A change may break the upgrade from an earlier version. The server keeps Colvenna Lake's HTTP contract, settings, metrics and migration chain exactly, so that Colvenna Lake can run its releases.

## Behaviour on AWS

The checks above need no AWS account. Behaviour on AWS needs one of this repository's own test environments (`tests/environment/up.sh`, never another repository's), which creates billable resources; see [Test environments](docs/development.md#test-environments).

For a confidential security concern, follow [SECURITY.md](SECURITY.md).
