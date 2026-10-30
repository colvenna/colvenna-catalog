#!/usr/bin/env bash
# The security scans of HEAD (#18), their raw results kept for review:
#   - ASH: its SAST, secret, IaC and dependency scanners over the tracked tree (.ash/.ash.yaml);
#   - Trivy over the tree, the CDK app's lockfile and the rendered chart (trivy.yaml, .trivyignore.yaml),
#     once as configured and once with nothing suppressed;
#   - the server's image as a release builds it (mvn package, docker buildx for linux/amd64), with
#     Trivy and grype;
#   - what Apache Paimon's jars bundle, which image scanners cannot see (bundled_sbom.py), with both;
#   - cdk-nag's report of every synthesis scripts/aws-test.sh makes;
#   - the licenses: scripts/release/licenses.py's scan against THIRD_PARTY_NOTICES.md (the image's jars,
#     what they bundle, which carry no license file, the CDK app's lockfile), the image's /licenses/
#     against the same record, and the image's packages (Trivy).
# docs/security/scans.md says what each covers, what it cannot see, and how its findings are handled.
#
# usage: ASH_REPO=<clone of awslabs/automated-security-helper at v3.5.4> scripts/security/scan.sh <out dir>
#
# <out dir> is new (or empty) and outside the repository; summary.txt in it counts what each scan found.
# The tree scanned is HEAD's, from git archive; the build is a detached worktree at HEAD, removed at
# the end with the image built from it. Findings do not fail the run. Exit codes: 0 every scan ran;
# 1 a scan or the build failed (logs/<step>.log), THIRD_PARTY_NOTICES.md among them when it differs from
# the license scan or the image's /licenses/; 2 refused: the tree is dirty, a tool is missing,
# ASH_REPO is not v3.5.4, Docker's builder is not the docker driver, or <out dir> is in use.
# shellcheck disable=SC2317  # functions run through step and trap, which shellcheck does not follow
set -euo pipefail

ASH_VERSION=v3.5.4
# The image ASH builds and runs, whose grype the image scans use too (ASH reads the same variable).
ASH_IMAGE="${ASH_IMAGE_NAME:-automated-security-helper:non-root}"
SEVERITIES=UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL

refuse() {
  echo "scan.sh: $*" >&2
  exit 2
}
[[ $# -eq 1 ]] || refuse "usage: ASH_REPO=<ash clone> scripts/security/scan.sh <out dir>"
root="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
cd "$root"
[[ -z "$(git status --porcelain)" ]] || refuse "the working tree is not clean; commit first, so that the scan is of HEAD"
for tool in git docker trivy mvn java node npm jq python3 tar; do
  command -v "$tool" >/dev/null 2>&1 || refuse "$tool is required but not on PATH"
done
docker info >/dev/null 2>&1 || refuse "Docker is not running"
# The real ASH is the GitHub project at a release tag (the PyPI package of that name is not it).
[[ -n "${ASH_REPO:-}" && -x "$ASH_REPO/ash" ]] || refuse "ASH_REPO must be a clone of awslabs/automated-security-helper"
[[ "$(git -C "$ASH_REPO" remote get-url origin)" =~ github\.com[:/]awslabs/automated-security-helper(\.git)?$ ]] \
  || refuse "ASH_REPO's origin is not github.com/awslabs/automated-security-helper"
[[ "$(git -C "$ASH_REPO" describe --tags --exact-match HEAD 2>/dev/null)" == "$ASH_VERSION" ]] \
  || refuse "ASH_REPO is not at $ASH_VERSION"
[[ -z "$(git -C "$ASH_REPO" status --porcelain)" ]] || refuse "ASH_REPO has local changes"

# Physical paths, so that a symbolic link into the repository is refused too; nothing is made before.
parent="$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)" || refuse "$(dirname "$1") does not exist"
out="$parent/$(basename "$1")"
[[ "$out/" != "$root/"* ]] || refuse "$out is inside the repository; write the results elsewhere"
[[ ! -e "$out" || -z "$(ls -A "$out")" ]] || refuse "$out is not empty"
mkdir -p "$out"

sha="$(git rev-parse HEAD)"
image="colvenna-catalog-scan:$sha"
work="$(mktemp -d)"
build="$work/build"
cleanup() {
  docker image rm -f "$image" >/dev/null 2>&1 || true
  git -C "$root" worktree remove --force "$build" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
# Registries are pulled from anonymously: a stale login in ~/.docker would turn pulls into 403s. It also
# leaves buildx on its default builder: ASH builds its image and runs it from the local image store,
# which only the docker driver loads builds into.
export DOCKER_CONFIG="$work/docker"
mkdir -p "$DOCKER_CONFIG"
[[ "$(docker buildx inspect 2>/dev/null | awk '$1 == "Driver:" { print $2; exit }')" == docker ]] \
  || refuse "buildx's default builder is not the docker driver (is BUILDX_BUILDER set, or buildx missing?)"
mkdir -p "$out"/{logs,ash,trivy,image,bundled,cdk-nag,licenses}

status=0
step() { # <name> <command...>: logs to logs/<name>.log; a failure is recorded and fails the run
  local name=$1 rc=0
  shift
  "$@" >"$out/logs/$name.log" 2>&1 || rc=$?
  printf '%s\t%s\n' "$name" "$rc" >>"$out/exit-codes.tsv"
  if ((rc != 0)); then
    echo "$name: exit $rc (logs/$name.log)" >&2
    status=1
  fi
  return 0
}
{
  echo "commit: $sha"
  echo "scanned: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "ash: $ASH_VERSION ($(git -C "$ASH_REPO" rev-parse HEAD))"
  trivy --version 2>&1 | sed 's/^/trivy: /'
  echo "docker: $(docker version --format '{{.Server.Version}}')"
  echo "java: $(java -version 2>&1 | head -1)"
} >"$out/versions.txt"

# --- 1. the tracked tree, and the build -------------------------------------------------------------
mkdir "$out/src"
git archive "$sha" | tar -x -C "$out/src"
git worktree add --detach "$build" "$sha" >/dev/null 2>&1

# --- 2. ASH (exit 2 means actionable findings: counted below, not a failure of the run) -------------
ash_rc=0
"$ASH_REPO/ash" --source-dir "$out/src" --output-dir "$out/ash" --format json >"$out/logs/ash.log" 2>&1 || ash_rc=$?
printf 'ash\t%s\n' "$ash_rc" >>"$out/exit-codes.tsv"
if ((ash_rc != 0 && ash_rc != 2)); then
  echo "ash: exit $ash_rc (logs/ash.log)" >&2
  status=1
fi

# --- 3. Trivy over the tree, from its root so that trivy.yaml applies -------------------------------
trivy_fs() { # <output> <extra arguments...>
  local output=$1
  shift
  (cd "$out/src" && trivy fs --scanners vuln,secret,misconfig --include-dev-deps --severity "$SEVERITIES" \
    --format json --output "$output" "$@" .)
}
step trivy-fs trivy_fs "$out/trivy/fs.json"
step trivy-fs-unsuppressed trivy_fs "$out/trivy/fs-unsuppressed.json" --ignorefile /dev/null

# --- 4. the image, built as scripts/release/make-manifest.sh builds a candidate's ------------------
build_image() {
  (cd "$build" && mvn -B clean package -DskipTests) \
    && docker buildx build --builder default --platform linux/amd64 --provenance=false --sbom=false \
      --label "org.opencontainers.image.revision=$sha" -t "$image" --load "$build"
}
step build build_image
# Everything below scans the image or its jars.
if ! grep -qx '\[INFO\] BUILD SUCCESS' "$out/logs/build.log" || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "build: no BUILD SUCCESS, or no image (logs/build.log); the image's scans cannot run" >&2
  exit 1
fi
ls "$build/target/lib" >"$out/image/lib.txt"
docker image inspect "$image" --format '{{.Id}} {{.Os}}/{{.Architecture}}' >"$out/image/id.txt"
grype_of() { # <grype target inside the container> <output name>: the grype that ASH's image carries
  docker run --rm -v "$work:/work:ro" -v "$out:/out" --entrypoint sh "$ASH_IMAGE" \
    -c "grype '$1' -o json >'/out/$2' && grype version && grype db status"
}
step trivy-image trivy image --scanners vuln,secret --severity "$SEVERITIES" --format json \
  --output "$out/image/trivy.json" "$image"
step image-save docker save "$image" -o "$work/image.tar"
chmod 644 "$work/image.tar" 2>/dev/null || true
step grype-image grype_of docker-archive:/work/image.tar image/grype.json
rm -f "$work/image.tar"

# --- 5. what Paimon's jars bundle -------------------------------------------------------------------
step bundled python3 -I "$build/scripts/security/bundled_sbom.py" "$build/target/lib" \
  "$out/bundled/sbom.json" "$out/bundled/components.tsv"
cp "$out/bundled/sbom.json" "$work/bundled.json" 2>/dev/null || true
step trivy-bundled trivy sbom --scanners vuln --format json --output "$out/bundled/trivy.json" "$work/bundled.json"
step grype-bundled grype_of sbom:/work/bundled.json bundled/grype.json

# --- 6. cdk-nag: every synthesis of the aws-test CI job, with its reports ---------------------------
step aws-test bash -c "cd '$build' && scripts/aws-test.sh"
# Every synthesis must have left cdk-nag's report: one without would be a synthesis cdk-nag did not see.
shopt -s nullglob
syntheses=("$build"/deploy/aws/cdk.out/*/)
((${#syntheses[@]} > 0)) || { echo "cdk-nag: no synthesis output" >&2; status=1; }
for synthesis in "${syntheses[@]}"; do
  name="$(basename "$synthesis")"
  reports=("$synthesis"*NagReport.csv)
  if ((${#reports[@]} == 0)); then
    echo "cdk-nag: $name has no NagReport.csv" >&2
    status=1
  fi
  files=("$synthesis"*.template.json "${reports[@]}")
  mkdir -p "$out/cdk-nag/$name"
  ((${#files[@]} == 0)) || cp "${files[@]}" "$out/cdk-nag/$name/"
done
shopt -u nullglob

# --- 7. licenses: the record's own scan (exit 1 if THIRD_PARTY_NOTICES.md differs from it), the image's
#     /licenses/ against the record, and the image's packages, which that record does not list ----------
step licenses python3 -I "$build/scripts/release/licenses.py" scan --out "$out/licenses/scan"
step licenses-files python3 -I "$build/scripts/release/licenses.py" image "$image"
step licenses-image trivy image --scanners license --format json --output "$out/licenses/image.json" "$image"

# --- summary -----------------------------------------------------------------------------------------
summarize() {
  echo "commit $sha"
  echo
  echo "== ASH (exit $ash_rc; actionable = at or above MEDIUM and not suppressed)"
  grep -E '^\| (Scanner|[a-z-]+) \|' "$out/ash/reports/ash.summary.md" || echo "no ASH summary"
  for f in fs fs-unsuppressed; do
    echo
    echo "== Trivy, the tree ($f): vulnerabilities, misconfigurations and secrets by severity"
    jq -r '[.Results[]? | ((.Vulnerabilities // [])[] | "vulnerability " + .Severity),
      ((.Misconfigurations // [])[] | select(.Status == "FAIL") | "misconfiguration " + .Severity),
      ((.Secrets // [])[] | "secret " + .Severity)] | group_by(.) | map("\(.[0]): \(length)") | .[]' \
      "$out/trivy/$f.json"
  done
  echo
  echo "== the image: Trivy by class and severity; grype by type and severity"
  jq -r '[.Results[]? | .Class as $c | (.Vulnerabilities // [])[] | "\($c) \(.Severity)"]
    | group_by(.) | map("trivy \(.[0]): \(length)") | .[]' "$out/image/trivy.json"
  jq -r '[.matches[] | "\(.artifact.type) \(.vulnerability.severity)"] | group_by(.)
    | map("grype \(.[0]): \(length)") | .[]' "$out/image/grype.json"
  echo
  echo "== bundled in Paimon's jars ($(($(wc -l <"$out/bundled/components.tsv") - 1)) NOTICE entries)"
  jq -r '[.Results[]? | (.Vulnerabilities // [])[] | .Severity] | group_by(.)
    | map("trivy \(.[0]): \(length)") | .[]' "$out/bundled/trivy.json"
  jq -r '[.matches[] | .vulnerability.severity] | group_by(.) | map("grype \(.[0]): \(length)") | .[]' \
    "$out/bundled/grype.json"
  echo
  echo "== cdk-nag, by synthesis: rule rows by compliance (Non-Compliant fails the synthesis)"
  for dir in "$out"/cdk-nag/*/; do
    printf '%s: ' "$(basename "$dir")"
    tail -q -n +2 "$dir"*NagReport.csv 2>/dev/null | awk -F'","' '{ n[$3]++ } END { for (k in n) printf "%s %d; ", k, n[k] }'
    echo
  done
  echo
  echo "== licenses: the record's scan (logs/licenses.log), the image's /licenses/, then the image's packages by license"
  tail -n 3 "$out/logs/licenses.log"
  tail -n 5 "$out/logs/licenses-files.log"
  jq -r '[.Results[]? | (.Licenses // [])[] | .Name] | group_by(.) | map("image \(length) \(.[0])") | .[]' \
    "$out/licenses/image.json"
  echo
  echo "== exit codes"
  cat "$out/exit-codes.tsv"
}
summarize >"$out/summary.txt" 2>&1 || status=1
cat "$out/summary.txt"
exit "$status"
