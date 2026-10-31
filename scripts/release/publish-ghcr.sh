#!/usr/bin/env bash
# Publishes release <release> (v<major>.<minor>.<patch>, made by promote.sh) to the GitHub Container
# Registry (GHCR), and nowhere else: never to AWS ECR Public (docs/development.md, "Publishing to
# GHCR"). From the release registry, by the digests of the tag's release/<release>/manifest.json:
#   - the image, image.digest, to ghcr.io/colvenna/colvenna-catalog:<version>;
#   - the chart, chart.digest, to ghcr.io/colvenna/charts/colvenna-catalog:<version>, an OCI artifact
#     as Helm pulls it;
# <version> being the release without its "v" (1.0.0). crane copies each by its digest, so GHCR holds
# the same manifests under the same digests; a tag already there must hold that digest (tags never
# move), and is then kept. It first checks the release registry's copies: the image is labelled
# org.opencontainers.image.revision=<built_from> and is for linux/amd64, and the chart is the
# manifest's version, annotated colvenna-catalog/source-commit: <built_from>. Then, with no credentials
# at all, it checks what anyone will pull: each tag resolves to the manifest's digest, and the image's
# label, its platform and the chart Helm pulls (its version, appVersion and annotation) are the same.
# GHCR makes a package private when it is first pushed: until each package is made public (once only:
# a public package never becomes private again), the anonymous check fails, with exit status 4, and the
# copies stay; make them public, then run again.
# It reads the manifest from the tag <release> (git show), never the working tree; it changes nothing
# in git. A loopback registry (127.0.0.1:<port>) stands in for the release registry or GHCR in a
# rehearsal, as in the offline tests.
#
# Usage: COLVENNA_RELEASE_REGISTRY=<registry> scripts/release/publish-ghcr.sh <release>
#   COLVENNA_RELEASE_REGISTRY  the release registry that holds <release> (an ECR registry, as for
#                              verify-artifacts.sh)
#   GHCR_USERNAME, GHCR_TOKEN  the credentials to push with (a token with the write:packages scope);
#                              without them, gh's: gh auth token, and the login of gh's user, whose
#                              token must have the scope (gh auth refresh -h github.com -s write:packages)
#   PUBLISH_IMAGE, PUBLISH_CHART  the repositories to publish to (default ghcr.io/colvenna/colvenna-catalog
#                              and ghcr.io/colvenna/charts/colvenna-catalog): ghcr.io/<owner>/<name>, or a
#                              loopback registry's; AWS ECR Public is refused
#   RELEASE_EVIDENCE           where the evidence goes (default .state/release/publish-<release>-<time>/,
#                              git-ignored)
# Exit status: 0 published, and verified as anyone pulls it; 1 a step failed; 2 the arguments or a
# setting is wrong (an AWS ECR Public target among them); 3 a tool is missing, or what a registry holds
# is not the release's; 4 copied, but not yet readable without credentials: make the packages public,
# then run again.
set -Eeuo pipefail
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/release.sh
source "$REPO/deploy/lib/release.sh"
tool="$REPO/scripts/release/promotion.py"
candidates="$REPO/scripts/release/manifest.py"

fail() {
  echo "publish-ghcr.sh: $1" >&2
  exit "${2:-1}"
}

[[ $# -eq 1 ]] || fail "usage: COLVENNA_RELEASE_REGISTRY=<registry> $0 <release>" 2
release="$1"
published="$(python3 "$tool" published "$release")" || exit 2
version="$(awk -F'\t' '$1 == "chart" {print $2}' <<<"$published")"
image_target="${PUBLISH_IMAGE:-ghcr.io/colvenna/colvenna-catalog}"
chart_target="${PUBLISH_CHART:-ghcr.io/colvenna/charts/colvenna-catalog}"
image_kind="$(python3 "$tool" target "$image_target")" || exit 2
chart_kind="$(python3 "$tool" target "$chart_target")" || exit 2
[[ -n "${COLVENNA_RELEASE_REGISTRY:-}" ]] \
  || fail "COLVENNA_RELEASE_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com: the release registry that holds $release" 2
source_kind="$(python3 "$tool" registry "$COLVENNA_RELEASE_REGISTRY")" || exit 2
if [[ -n "${GHCR_TOKEN:-}" && -z "${GHCR_USERNAME:-}" ]]; then
  fail "GHCR_USERNAME: the user GHCR_TOKEN is of" 2
fi
git -C "$REPO" rev-parse -q --verify "refs/tags/$release" >/dev/null || fail "No tag $release in this clone: git fetch origin --tags" 2

work="$(mktemp -d)"
chmod 700 "$work"
export DOCKER_CONFIG="$work/docker" HELM_REGISTRY_CONFIG="$work/helm/registry.json" HELM_CACHE_HOME="$work/helm/cache"
trap 'rm -rf "$work"' EXIT
mkdir -p "$DOCKER_CONFIG" "$work/anonymous" "$work/source-chart" "$work/public-chart"
git -C "$REPO" show "$release:release/$release/manifest.json" >"$work/manifest.json" 2>/dev/null \
  || fail "The tag $release has no release/$release/manifest.json" 2
python3 "$tool" validate "$work/manifest.json" --release "$release" || fail "The tag $release's manifest is not a release's" 2
get() {
  python3 "$candidates" get "$work/manifest.json" "$1"
}
built_from="$(get source.built_from)"
image_digest="$(get image.digest)"
chart_digest="$(get chart.digest)"
platform="$(get image.platform)"

for command in jq tar sha256sum; do
  command -v "$command" >/dev/null || fail "$command is required" 3
done
# The credentials to push with, before anything is read from a registry.
username="${GHCR_USERNAME:-}" token="${GHCR_TOKEN:-}"
if [[ -z "$token" && ( "$image_kind" == ghcr || "$chart_kind" == ghcr ) ]]; then
  command -v gh >/dev/null || fail "gh is required, or GHCR_USERNAME and GHCR_TOKEN" 3
  scopes="$(gh api -i /user 2>/dev/null | tr -d '\r' | sed -n 's/^[Xx]-[Oo][Aa]uth-[Ss]copes: //p')" \
    || fail "gh is not logged in to github.com: gh auth login, or give GHCR_USERNAME and GHCR_TOKEN" 2
  tr -d ' ' <<<"$scopes" | tr ',' '\n' | grep -qx 'write:packages' \
    || fail "gh's token has the scopes '${scopes:-none}', not write:packages: gh auth refresh -h github.com -s write:packages" 2
  username="$(gh api /user --jq .login)"
  token="$(gh auth token)"
fi
(cd "$REPO" && scripts/install-tools.sh >/dev/null)
export PATH="$REPO/.tools:$PATH"
evidence="${RELEASE_EVIDENCE:-$REPO/.state/release/publish-$release-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$evidence"
log() {
  echo "$*" | tee -a "$evidence/publish.log"
}
registry="$COLVENNA_RELEASE_REGISTRY"
source_plain=()
if [[ "$source_kind" == ecr ]]; then
  aws ecr get-login-password --region "$(python3 "$candidates" registry "$registry" | cut -f2)" \
    | crane auth login "$registry" -u AWS --password-stdin >"$evidence/login.log" 2>&1
  aws ecr get-login-password --region "$(python3 "$candidates" registry "$registry" | cut -f2)" \
    | helm registry login "$registry" --username AWS --password-stdin >>"$evidence/login.log" 2>&1
else
  source_plain=(--plain-http)
fi
if [[ -n "$token" ]]; then
  for host in $(printf '%s\n' "${image_target%%/*}" "${chart_target%%/*}" | sort -u); do
    crane auth login "$host" -u "$username" --password-stdin <<<"$token" >>"$evidence/login.log" 2>&1 \
      || fail "crane cannot log in to $host as $username ($evidence/login.log)"
  done
fi
log "$(date -u +%FT%TZ) $release (built from $built_from) from $registry to $image_target and $chart_target, as $version"

# held <reference>: the digest the registry holds under that tag, or nothing when it holds none.
held() {
  local out
  if out="$(crane digest "$1" 2>&1)"; then
    echo "$out"
  elif ! grep -qE 'MANIFEST_UNKNOWN|NAME_UNKNOWN|status code 404' <<<"$out"; then
    fail "Cannot read $1: $(tail -1 <<<"$out")"
  fi
}
# only_package <dir>: the one package a helm pull left there.
only_package() {
  local packages
  packages="$(find "$1" -maxdepth 1 -name '*.tgz')"
  [[ -n "$packages" && "$packages" != *$'\n'* ]] || fail "helm pull left no single package in $1"
  echo "$packages"
}
# image_problems <configuration>: what is wrong with the image's configuration, one per line.
image_problems() {
  local revision found
  revision="$(jq -r --arg l "$RELEASE_REVISION_LABEL" '.config.Labels[$l] // ""' <<<"$1")"
  [[ "$revision" == "$built_from" ]] || echo "its label $RELEASE_REVISION_LABEL is ${revision:-missing}, not $built_from"
  found="$(jq -r '"\(.os)/\(.architecture)"' <<<"$1")"
  [[ "$found" == "$platform" ]] || echo "it is for $found, not $platform"
}
# chart_problems <package>: what is wrong with the chart package, one per line.
chart_problems() {
  local shown annotation
  shown="$(helm show chart "$1")"
  [[ "$(sed -n 's/^version: //p' <<<"$shown")" == "$(get chart.version)" ]] || echo "it is not version $(get chart.version)"
  [[ "$(sed -n 's/^appVersion: //p' <<<"$shown" | tr -d '"')" == "$(get chart.appVersion)" ]] \
    || echo "its appVersion is not $(get chart.appVersion)"
  annotation="$(sed -n "s|^  $RELEASE_SOURCE_ANNOTATION: ||p" <<<"$shown" | tr -d '"')"
  [[ "$annotation" == "$built_from" ]] || echo "its annotation $RELEASE_SOURCE_ANNOTATION is ${annotation:-missing}, not $built_from"
}

# 1. The release registry's copies are the release's.
source_image="$registry/$(get image.repository)@$image_digest"
source_chart="$registry/$(get chart.repository)@$chart_digest"
config="$(crane config "$source_image" 2>&1)" || fail "The release registry has no $source_image: $(tail -1 <<<"$config")" 3
found="$(image_problems "$config")"
[[ -z "$found" ]] || fail "$source_image: $(tr '\n' ' ' <<<"$found")" 3
helm pull "oci://$source_chart" -d "$work/source-chart" "${source_plain[@]}" >>"$evidence/chart.log" 2>&1 \
  || { tail -5 "$evidence/chart.log" >&2; fail "Cannot pull oci://$source_chart" 3; }
source_package="$(only_package "$work/source-chart")"
found="$(chart_problems "$source_package")"
[[ -z "$found" ]] || fail "oci://$source_chart: $(tr '\n' ' ' <<<"$found")" 3
log "release registry: $source_image is labelled $RELEASE_REVISION_LABEL=$built_from, for $platform;" \
  "oci://$source_chart is version $version, annotated $RELEASE_SOURCE_ANNOTATION=$built_from"

# 2. The copies, by digest.
# check_visibility <repository>: the command that prints a GHCR package's visibility (the package's
# name is the repository's path after its owner, its / as %2F).
check_visibility() {
  local path="${1#*/}"
  local owner="${path%%/*}" name="${path#*/}"
  echo "  gh api /orgs/$owner/packages/container/${name//\//%2F} --jq .visibility"
}
{
  echo "Before anyone can pull them: GHCR makes a package private when it is first pushed. Once each of the"
  echo "two is there, make it public in its settings (the organization's Packages tab, the package, Package"
  echo "settings, Danger Zone, Change visibility, Public); a public package can never be made private again."
  echo "Check each with these (gh's token needs read:packages), which must print public:"
  check_visibility "$image_target"
  check_visibility "$chart_target"
  echo "Until both are public, the check without credentials below fails (exit 4); the copies stay, and"
  echo "running this again keeps them."
} | tee -a "$evidence/publish.log"
publish() {
  local what="$1" from="$2" to="$3" digest="$4" existing
  existing="$(held "$to:$version")"
  if [[ -z "$existing" ]]; then
    crane copy "$from" "$to:$version" >>"$evidence/copy.log" 2>&1 \
      || { tail -5 "$evidence/copy.log" >&2; fail "crane copy $from $to:$version failed ($evidence/copy.log)"; }
    log "$what: copied $from to $to:$version"
  elif [[ "$existing" == "$digest" ]]; then
    log "$what: $to:$version is $digest already"
  else
    fail "$to:$version is already $existing, not $release's $digest: tags never move" 3
  fi
  [[ "$(held "$to:$version")" == "$digest" ]] || fail "$to:$version is not $digest after the copy" 3
}
publish image "$source_image" "$image_target" "$image_digest"
publish chart "$source_chart" "$chart_target" "$chart_digest"

# 3. What anyone pulls: no credentials at all.
private=()
anonymous() {
  DOCKER_CONFIG="$work/anonymous" HELM_REGISTRY_CONFIG="$work/anonymous/helm.json" "$@"
}
# hidden <output>: whether the registry would not show it without credentials (refused, or answered
# that it is not there, which it is to the credentials that copied it).
hidden() {
  grep -qE 'UNAUTHORIZED|DENIED|MANIFEST_UNKNOWN|NAME_UNKNOWN|status code 40[134]' <<<"$1"
}
if ! out="$(anonymous crane digest "$image_target:$version" 2>&1)"; then
  hidden "$out" || fail "Cannot read $image_target:$version without credentials: $(tail -1 <<<"$out")"
  private+=("$image_target")
else
  [[ "$out" == "$image_digest" ]] || fail "$image_target:$version is $out to anyone, not $image_digest" 3
  config="$(anonymous crane config "$image_target@$image_digest" 2>&1)" \
    || fail "Cannot read $image_target@$image_digest's configuration without credentials: $(tail -1 <<<"$config")"
  found="$(image_problems "$config")"
  [[ -z "$found" ]] || fail "$image_target@$image_digest: $(tr '\n' ' ' <<<"$found")" 3
  log "anyone: $image_target:$version is $image_digest, labelled $RELEASE_REVISION_LABEL=$built_from, for $platform"
fi
chart_plain=()
[[ "$chart_kind" != loopback ]] || chart_plain=(--plain-http)
if ! out="$(anonymous crane digest "$chart_target:$version" 2>&1)"; then
  hidden "$out" || fail "Cannot read $chart_target:$version without credentials: $(tail -1 <<<"$out")"
  private+=("$chart_target")
else
  [[ "$out" == "$chart_digest" ]] || fail "$chart_target:$version is $out to anyone, not $chart_digest" 3
  anonymous helm pull "oci://$chart_target" --version "$version" -d "$work/public-chart" "${chart_plain[@]}" \
    >"$work/pull.log" 2>&1 || { cat "$work/pull.log" >>"$evidence/chart.log"; fail "helm cannot pull oci://$chart_target version $version without credentials: $(tail -1 "$work/pull.log")"; }
  cat "$work/pull.log" >>"$evidence/chart.log"
  grep -qx "Digest: $chart_digest" "$work/pull.log" || fail "helm pulled oci://$chart_target version $version, not $chart_digest" 3
  public_package="$(only_package "$work/public-chart")"
  found="$(chart_problems "$public_package")"
  [[ -z "$found" ]] || fail "oci://$chart_target version $version: $(tr '\n' ' ' <<<"$found")" 3
  log "anyone: helm pulls oci://$chart_target version $version as $chart_digest, annotated $RELEASE_SOURCE_ANNOTATION=$built_from"
fi
if ((${#private[@]} > 0)); then
  log "not readable without credentials yet: ${private[*]}: make each package public (above), then run again"
  exit 4
fi
log "$release published: $image_target:$version@$image_digest and oci://$chart_target version $version@$chart_digest; evidence in $evidence"
