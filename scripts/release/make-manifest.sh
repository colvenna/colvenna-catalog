#!/usr/bin/env bash
# Makes release candidate <candidate> (v<major>.<minor>.<patch>-rc.<n>) of this commit, B, which must be
# HEAD of a clean, committed tree:
#   1. builds the server's image for linux/amd64 from B (mvn package, then docker buildx) and pushes it
#      into the release registry as colvenna-catalog-release/colvenna-catalog:<candidate>, labelled
#      with B (org.opencontainers.image.revision);
#   2. packages B's Helm chart as version <candidate without its "v"> (appVersion <candidate>), its
#      Chart.yaml annotated colvenna-catalog/source-commit: <B> (deploy/lib/release.sh's
#      release_chart_source, in a copy), checks the package holds exactly that copy's files, and pushes
#      it into the release registry as an OCI artifact,
#      colvenna-catalog-release/charts/colvenna-catalog:<version>;
#   3. writes release/<candidate>/manifest.json (manifest.py assemble: the two digests, the git objects
#      of the chart, install.sh, uninstall.sh, deploy/lib and the CDK app, the UAT cases document's
#      version or its absence, the required cases, the EKS form and the Regions), validated and
#      checked against B, then verifies what was pushed as install.sh --release does
#      (release_verify_artifacts: the image's label, and the chart pulled back by its digest, its
#      annotation and its files against B's), and writes release/<candidate>/notes.md, the release
#      notes' draft from release/notes-template.md.
# Both are built from B exactly (git archive), and nothing is written into the tree before both are
# pushed. The release registry (scripts/release/deploy-registry.sh) is in the caller's account and
# COLVENNA_RELEASE_REGION (default us-west-2); the manifest names its repositories and the digests,
# never the registry, the account or who made it (that goes to the evidence only). Commit the two files
# alone on top of B and tag that commit <candidate> (docs/development.md, "Releasing"): install.sh
# --release installs only from a tree that differs from B under release/<candidate>/ alone.
# A run that stopped can run again: an image already under the tag must be labelled with B, and a
# chart already under the version must hold the same files as B's; either is then kept. Otherwise the
# candidate's name is spent (tags never move): name another.
#
# Usage: [COLVENNA_RELEASE_REGION=<region>] [RELEASE_REGIONS=<region>,<region>...] \
#          scripts/release/make-manifest.sh <candidate>
#   RELEASE_REGIONS  the standard Regions the candidate is accepted in, two or more
#                    (configurations.regions; default us-west-2,us-east-2)
#   RELEASE_FORMS    the deployment forms it is accepted in, eks and serverless (configurations.forms;
#                    default eks): say serverless only for a candidate whose serverless form passed
#   RELEASE_EVIDENCE where the evidence goes (default .state/release/<candidate>/, git-ignored)
# Exit status: 0 made; 1 a step failed; 2 the arguments, a setting or the tree is wrong; 3 a tool is
# missing or too old, or what was pushed is not B's.
set -Eeuo pipefail
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/preflight.sh
source "$REPO/deploy/lib/preflight.sh"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/release.sh
source "$REPO/deploy/lib/release.sh"
tool="$REPO/scripts/release/manifest.py"

fail() {
  echo "make-manifest.sh: $1" >&2
  exit "${2:-1}"
}

[[ $# -eq 1 ]] || fail "usage: [COLVENNA_RELEASE_REGION=<region>] [RELEASE_REGIONS=<r>,<r>] $0 <candidate>" 2
candidate="$1"
names="$(python3 "$tool" names "$candidate")" || exit 2
image_repository="$(awk -F'\t' '$1 == "image" {print $2}' <<<"$names")"
image_tag="$(awk -F'\t' '$1 == "image" {print $3}' <<<"$names")"
chart_repository="$(awk -F'\t' '$1 == "chart" {print $2}' <<<"$names")"
chart_version="$(awk -F'\t' '$1 == "chart" {print $3}' <<<"$names")"
regions="${RELEASE_REGIONS:-us-west-2,us-east-2}"
python3 "$tool" regions "$regions" >/dev/null || exit 2
forms="${RELEASE_FORMS:-eks}"
python3 "$tool" forms "$forms" >/dev/null || exit 2
release_region="${COLVENNA_RELEASE_REGION:-us-west-2}"
[[ "$release_region" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$release_region" != cn-* ]] \
  || fail "COLVENNA_RELEASE_REGION: a standard AWS region, e.g. us-west-2, not '$release_region'" 2
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || fail "Commit first: a candidate is built from a clean, committed tree" 2
sha="$(git -C "$REPO" rev-parse HEAD)"
if git -C "$REPO" cat-file -e "$sha:release/$candidate" 2>/dev/null; then
  fail "release/$candidate is already at $sha: a candidate is made once; name another" 2
fi
# still_this_commit: the tree is still B's, clean.
still_this_commit() {
  [[ -z "$(git -C "$REPO" status --porcelain)" && "$(git -C "$REPO" rev-parse HEAD)" == "$sha" ]] \
    || fail "The tree moved from $sha during the run: make the candidate again"
}

assert_tools install
"$REPO/scripts/install-tools.sh" >/dev/null
export PATH="$REPO/.tools:$PATH"
evidence="${RELEASE_EVIDENCE:-$REPO/.state/release/$candidate}"
mkdir -p "$evidence"
log() {
  echo "$*" | tee -a "$evidence/make-manifest.log"
}
account="$(aws sts get-caller-identity --query Account --output text)"
registry="$account.dkr.ecr.$release_region.amazonaws.com"
stack="$(python3 "$tool" stack)"
aws cloudformation describe-stacks --region "$release_region" --stack-name "$stack" >/dev/null 2>&1 \
  || fail "No release registry ($stack in $release_region): scripts/release/deploy-registry.sh"
for repository in "$image_repository" "$chart_repository"; do
  aws ecr describe-repositories --region "$release_region" --repository-names "$repository" >/dev/null 2>&1 \
    || fail "The release registry has no $repository: scripts/release/deploy-registry.sh at this commit"
done
log "$(date -u +%FT%TZ) candidate $candidate of $sha into $registry"
log "by $(aws sts get-caller-identity --query Arn --output text)"

work="$(mktemp -d)"
chmod 700 "$work"
# This run's own registry credentials, for docker, crane and Helm alike, removed with it.
export DOCKER_CONFIG="$work/docker" HELM_REGISTRY_CONFIG="$work/helm/registry.json" HELM_CACHE_HOME="$work/helm/cache"
trap 'rm -rf "$work"' EXIT
mkdir -p "$DOCKER_CONFIG" "$work/src" "$work/chart" "$work/held" "$work/check"
git -C "$REPO" archive "$sha" | tar -x -C "$work/src"

# The UAT cases document, if B has one (#19 writes it): its required cases, checked before anything is
# built.
cases=null
if git -C "$REPO" cat-file -e "$sha:docs/uat/cases.md" 2>/dev/null; then
  cp "$work/src/docs/uat/cases.md" "$work/cases.md"
  python3 "$tool" required-cases "$work/cases.md" >/dev/null || exit 2
  cases="$(jq -cn --arg c "$(git -C "$REPO" log -1 --format=%H "$sha" -- docs/uat/cases.md)" \
    --arg b "$(git -C "$REPO" rev-parse "$sha:docs/uat/cases.md")" '{commit: $c, blob: $b}')"
else
  log "docs/uat/cases.md is absent at $sha: the manifest records it so, and the candidate is not one for GO (#19)"
fi

# The performance terms, if B has them (#19): the scenarios, method and targets the candidate's
# performance acceptance is judged by.
performance=null
if git -C "$REPO" cat-file -e "$sha:docs/performance/terms.md" 2>/dev/null; then
  performance="$(jq -cn --arg c "$(git -C "$REPO" log -1 --format=%H "$sha" -- docs/performance/terms.md)" \
    --arg b "$(git -C "$REPO" rev-parse "$sha:docs/performance/terms.md")" '{commit: $c, blob: $b}')"
else
  log "docs/performance/terms.md is absent at $sha: the manifest records it so, and the candidate is not one for GO"
fi

aws ecr get-login-password --region "$release_region" \
  | docker login --username AWS --password-stdin "$registry" >"$evidence/login.log" 2>&1
aws ecr get-login-password --region "$release_region" \
  | helm registry login "$registry" --username AWS --password-stdin >>"$evidence/login.log" 2>&1
# held <repository> <tag>: the digest the release registry holds under that tag, or nothing.
held() {
  aws ecr describe-images --region "$release_region" --repository-name "$1" --image-ids "imageTag=$2" \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true
}

# 1. The image.
image_digest="$(held "$image_repository" "$image_tag")"
if [[ "$image_digest" == sha256:* ]]; then
  revision="$(crane config "$registry/$image_repository@$image_digest" \
    | jq -r --arg l "$RELEASE_REVISION_LABEL" '.config.Labels[$l] // ""')"
  [[ "$revision" == "$sha" ]] || fail "The release registry holds $image_repository:$image_tag ($image_digest), built from \
${revision:-an unknown commit}, not $sha: a candidate's name is spent once its image is pushed; name another"
  log "image: $image_repository:$image_tag is there already ($image_digest), built from $sha"
else
  (cd "$work/src" && mvn -B clean package -DskipTests) >"$evidence/mvn.log" 2>&1 \
    || { tail -30 "$evidence/mvn.log" >&2; fail "The Maven build failed ($evidence/mvn.log)"; }
  grep -qx '\[INFO\] BUILD SUCCESS' "$evidence/mvn.log" || fail "The Maven build did not succeed ($evidence/mvn.log)"
  still_this_commit
  docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
    --label "$RELEASE_REVISION_LABEL=$sha" --label "org.opencontainers.image.version=$candidate" \
    --label "org.opencontainers.image.source=https://github.com/colvenna/colvenna-catalog" \
    -t "$registry/$image_repository:$image_tag" --push "$work/src" >"$evidence/image.log" 2>&1 \
    || { tail -30 "$evidence/image.log" >&2; fail "The image build or push failed ($evidence/image.log)"; }
  image_digest="$(held "$image_repository" "$image_tag")"
  [[ "$image_digest" == sha256:* ]] || fail "No $image_repository:$image_tag in the release registry after the push"
  log "image: pushed $image_repository:$image_tag $image_digest"
fi
platform="$(crane config "$registry/$image_repository@$image_digest" | jq -r '"\(.os)/\(.architecture)"')"
[[ "$platform" == linux/amd64 ]] || fail "$image_repository@$image_digest is for $platform, not linux/amd64"

# 2. The chart, as an OCI artifact: B's chart directory as a release packages it (release_chart_source:
# Chart.yaml with the candidate's version and appVersion, and the annotation naming B), checked file by
# file against that directory on every run.
chart_source="$work/chart-source/colvenna-catalog"
release_chart_source "$work/src/deploy/helm/colvenna-catalog" "$chart_source" "$chart_version" "$candidate" "$sha"
{
  echo "Chart.yaml as packaged, against B's (only version, appVersion and the $RELEASE_SOURCE_ANNOTATION annotation differ):"
  diff "$work/src/deploy/helm/colvenna-catalog/Chart.yaml" "$chart_source/Chart.yaml" || true
} >"$evidence/chart-check.txt"
helm package "$chart_source" -d "$work/chart" >"$evidence/chart.log"
package="$work/chart/colvenna-catalog-$chart_version.tgz"
differences="$(release_chart_differences "$package" "$chart_source")"
[[ -z "$differences" ]] || fail "The packaged chart is not B's chart directory as a release packages it: $differences"
echo "the package holds exactly those files" >>"$evidence/chart-check.txt"
chart_digest="$(held "$chart_repository" "$chart_version")"
if [[ "$chart_digest" == sha256:* ]]; then
  # An earlier run's chart (packaging is not reproducible byte for byte): it must hold the same files.
  helm pull "oci://$registry/$chart_repository@$chart_digest" -d "$work/held" >>"$evidence/chart.log" 2>&1
  differences="$(release_chart_differences "$work/held/colvenna-catalog@sha256-${chart_digest#sha256:}.tgz" "$chart_source")"
  [[ -z "$differences" ]] \
    || fail "The release registry holds chart $chart_version ($chart_digest) with other files than $sha's: name another candidate"
  log "chart: $chart_repository:$chart_version is there already ($chart_digest), the same files as $sha's"
else
  helm push "$package" "oci://$registry/${chart_repository%/*}" >>"$evidence/chart.log" 2>&1 \
    || { tail -10 "$evidence/chart.log" >&2; fail "helm push failed ($evidence/chart.log)"; }
  chart_digest="$(held "$chart_repository" "$chart_version")"
  grep -qx "Digest: $chart_digest" "$evidence/chart.log" \
    || fail "The release registry holds $chart_repository:$chart_version as ${chart_digest:-nothing}, not what helm pushed"
  log "chart: pushed $chart_repository:$chart_version $chart_digest"
fi

# 3. The manifest and the notes' draft, from B.
still_this_commit
objects="$(python3 "$tool" paths | while read -r path; do
  jq -cn --arg p "$path" --arg o "$(git -C "$REPO" rev-parse "$sha:$path")" '{($p): $o}'
done | jq -cs add)"
jq -n --arg candidate "$candidate" --arg sha "$sha" --arg at "$(date -u +%FT%TZ)" --arg image "$image_digest" \
  --arg chart "$chart_digest" --argjson objects "$objects" --argjson cases "$cases" --arg regions "$regions" \
  --arg forms "$forms" --argjson performance "$performance" \
  '{candidate: $candidate, built_from: $sha, generated: $at, image_digest: $image, chart_digest: $chart,
    objects: $objects, uat_cases: $cases, performance: $performance, regions: ($regions | split(",")),
    forms: ($forms | split(","))}' >"$work/params.json"
python3 "$tool" assemble "$work" >"$work/manifest.json"
python3 "$tool" validate "$work/manifest.json" --name "$candidate"
python3 "$tool" check-tree "$work/manifest.json" "$REPO" "$sha"
# What install.sh --release and verify-artifacts.sh check, on what was pushed: the image's label and
# platform, and the chart pulled back by its digest (its annotation, and its files against B's).
export RELEASE_MANIFEST="$work/manifest.json" COLVENNA_RELEASE_REGISTRY="$registry" WORK="$work" EVIDENCE="$evidence"
release_auth
release_verify_artifacts "$work/verify"
log "verified: the image is labelled $RELEASE_REVISION_LABEL=$sha; the chart pulled by $chart_digest is annotated" \
  "$RELEASE_SOURCE_ANNOTATION=$sha and holds exactly B's chart directory as a release packages it ($evidence/release.txt)"
python3 "$tool" notes "$work/manifest.json" <"$work/src/release/notes-template.md" >"$work/notes.md"
cp "$work/manifest.json" "$work/notes.md" "$evidence/"
if ! gaps="$(python3 "$tool" validate "$work/manifest.json" --go 2>&1)"; then
  log "not yet a candidate for GO:"
  log "$(tail -n +2 <<<"$gaps")"
fi
# Into the tree only once both are whole: a run that stops before leaves it clean, to run again.
still_this_commit
mkdir -p "$REPO/release/$candidate"
cp "$work/manifest.json" "$work/notes.md" "$REPO/release/$candidate/"
log "wrote release/$candidate/manifest.json and notes.md: commit them alone on $sha, then tag that commit $candidate"
