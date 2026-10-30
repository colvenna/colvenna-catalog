#!/usr/bin/env bash
# Runs every job of .github/workflows/ci.yml on the current HEAD: a supplement to GitHub Actions
# (the CI of record), which takes its place only under a waiver recorded when Actions cannot run;
# the two list the same jobs, so change them together.
# Under a waiver, paste the output into the pull request: it names the exact SHA the jobs ran on.
# Exit codes: 0 every job passed; 1 a job failed or left changes in the tree; 2 refused to run
# (the working tree is not clean, a tool the jobs need is missing, or Docker, which the contract
# tests need, is not running).
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is not clean; commit first, so that the run is of HEAD" >&2
  exit 2
fi
for tool in pre-commit shellcheck mvn java docker node npm curl jq python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required but not on PATH" >&2
    exit 2
  fi
done
# The UAT harness's tests read the admin API's OpenAPI description (YAML), and the deploy-test job's
# check of ASH's settings reads .ash/.ash.yaml.
if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "python3 with PyYAML is required (the uat-test and deploy-test jobs)" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is required (Testcontainers)" >&2
  exit 2
fi

sha=$(git rev-parse HEAD)
logs="$root/.ci-local/$sha"
mkdir -p "$logs"
# The first line a command prints; never fails the run (no pipe for pipefail to trip on).
first_line() {
  local out
  out="$("$@" 2>&1)" || true
  printf '%s\n' "${out%%$'\n'*}"
}
{
  first_line pre-commit --version
  first_line shellcheck --version
  first_line mvn -B -v
  first_line java -version
  first_line node --version
  first_line docker version --format 'docker {{.Server.Version}}'
} >"$logs/environment.log"

declare -A result
run_job() { # <job> <command...>
  local job=$1
  shift
  if "$@" >"$logs/$job.log" 2>&1; then result[$job]=PASS; else result[$job]=FAIL; fi
}

# pre-commit, then the tests of its old-name and deployment-values hooks (scripts/rename,
# scripts/redaction), as the CI job runs them.
run_job pre-commit bash -c "pre-commit run --all-files --show-diff-on-failure \
  && python3 -m unittest discover -s scripts/rename -p 'test_*.py' \
  && python3 -m unittest discover -s scripts/redaction -p 'test_*.py'"
run_job shellcheck bash -c "git ls-files -z '*.sh' | xargs -0 -r shellcheck -x"
run_job aws-test scripts/aws-test.sh
run_job deploy-test bash -c "scripts/install-tools.sh >/dev/null && python3 -m unittest discover -s tests/deploy \
  && python3 -m unittest discover -s scripts/release -p 'test_*.py' \
  && python3 -m unittest discover -s scripts/security -p 'test_*.py'"
# The server, then the serverless live test's driver under the same gates (the serverless-driver profile).
run_job verify bash -c "mvn -B clean verify && mvn -B -P serverless-driver clean verify"
# Maven's exit status alone is not the verdict: each of the two builds must end with BUILD SUCCESS.
if [[ ${result[verify]} == PASS ]] && (($(grep -cx '\[INFO\] BUILD SUCCESS' "$logs/verify.log") != 2)); then
  result[verify]=FAIL
fi
# The compatibility matrix offline: its declarations, its runner's tests, and its flows' build.
run_job compat-test bash -c "python3 tests/compat/matrix.py check \
  && python3 -m unittest discover -s tests/compat -p 'test_*.py' \
  && mvn -B -f tests/compat/flows/pom.xml clean verify"
if [[ ${result[compat-test]} == PASS ]] && ! grep -qx '\[INFO\] BUILD SUCCESS' "$logs/compat-test.log"; then
  result[compat-test]=FAIL
fi

# The UAT harness offline: its cases held to the cases document, its tests, and its engine flows' and
# Paimon tools' builds.
run_job uat-test bash -c "python3 tests/uat/uat.py check && python3 -m unittest discover -s tests/uat -p 'test_*.py' \
  && mvn -B -f tests/uat/flows/pom.xml clean verify && mvn -B -f tests/uat/paimon/pom.xml clean verify"
# Two builds, each its BUILD SUCCESS line.
if [[ ${result[uat-test]} == PASS ]] && (($(grep -cx '\[INFO\] BUILD SUCCESS' "$logs/uat-test.log") < 2)); then
  result[uat-test]=FAIL
fi

# The performance harness offline: its targets held to the performance terms, its tests, and its load
# tool's build.
run_job perf-test bash -c "python3 tests/performance/perf.py check \
  && python3 -m unittest discover -s tests/performance -p 'test_*.py' \
  && mvn -B -f tests/performance/load/pom.xml clean verify"
if [[ ${result[perf-test]} == PASS ]] && ! grep -qx '\[INFO\] BUILD SUCCESS' "$logs/perf-test.log"; then
  result[perf-test]=FAIL
fi

status=0
tree=PASS
if [[ -n "$(git status --porcelain)" ]]; then
  echo "jobs left changes in the tree:"
  git status --short
  tree=FAIL
  status=1
fi

echo "sha: $sha"
sed 's/^/environment: /' "$logs/environment.log"
for job in pre-commit shellcheck aws-test deploy-test verify compat-test uat-test perf-test; do
  echo "$job: ${result[$job]}"
  [[ ${result[$job]} == PASS ]] || status=1
done
echo "clean tree after jobs: $tree"
echo "logs: $logs"
exit "$status"
