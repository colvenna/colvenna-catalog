#!/usr/bin/env bash
# Promotes release candidate <candidate>, judged GO, to the release <release>, its name without
# -rc.<n> (v1.0.0-rc.2 to v1.0.0), rebuilding nothing (docs/development.md, "Promoting a GO candidate"):
#   0. checks, before it reads anything from a registry: the tree is clean and HEAD is the candidate's
#      tag commit, C, which differs from the commit the candidate was built from, B, under
#      release/<candidate>/ alone; the candidate's manifest is valid, fit for GO and pins C's git
#      objects; the candidate's release notes at RELEASE_RECORD (a commit that has C, default
#      origin/main) record the release verdict GO, beside the same manifest as C's; and neither the tag
#      <release>, the branch release-<release> nor release/<release>/ exists;
#   1. checks what the release registry holds, before it writes anything there: the image by the
#      candidate's digest is labelled B and for linux/amd64; the candidate's chart, pulled by its digest,
#      is B's chart directory as a release packages it. That package is unpacked, only Chart.yaml's
#      version and appVersion are changed, both to <release without its "v"> (1.0.0), and it is
#      packaged again (promotion.py rechart, then helm package), then compared with the candidate's file
#      by file (promotion.py chart-diff);
#   2. writes into it: the tag <release> on the candidate's image (crane tag: the same manifest under a
#      second tag; nothing is copied or built), and the new chart as that version, which it pulls back
#      by its digest and compares again: that comparison is chart-comparison.txt;
#   3. writes release/<release>/manifest.json (promotion.py assemble: the candidate's manifest under the
#      release's names, the same image digest, the new chart digest, and where it was promoted from,
#      linking the candidate's acceptance record), notes.md (the candidate's release notes as accepted,
#      under the release's name) and chart-comparison.txt, commits the three alone on C on the branch
#      release-<release>, and tags that commit <release> (annotated). It pushes nothing to git.
# The release registry is COLVENNA_RELEASE_REGISTRY or, unset, the caller's account's in
# COLVENNA_RELEASE_REGION (default us-west-2), as for make-manifest.sh. A loopback registry
# (127.0.0.1:<port>) stands in for it in a rehearsal, as in the offline tests: the commit and the tag then
# say they are a rehearsal's, never to be pushed; rehearse in a clone of its own. A run that stopped can run
# again: the image's <release> tag must hold the candidate's digest, and a chart already under the
# version must be the candidate's but for version and appVersion; either is then kept.
#
# Usage: [COLVENNA_RELEASE_REGISTRY=<registry>] [RELEASE_RECORD=<commit>] \
#          scripts/release/promote.sh <candidate> <release>
#   RELEASE_RECORD    the commit whose release/<candidate>/notes.md records the verdict (default
#                     origin/main: git fetch origin first)
#   RELEASE_EVIDENCE  where the evidence goes (default .state/release/<release>/, git-ignored)
# Exit status: 0 promoted; 1 a step failed; 2 the arguments, a setting, the tree or the candidate is
# wrong (a candidate not judged GO among them); 3 a tool is missing, or what the release registry holds
# is not the candidate's.
set -Eeuo pipefail
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/release.sh
source "$REPO/deploy/lib/release.sh"
# shellcheck source-path=SCRIPTDIR source=registries.sh
source "$REPO/scripts/release/registries.sh"
promotion_py="$REPO/scripts/release/promotion.py"
manifest_py="$REPO/scripts/release/manifest.py"

fail() {
  echo "promote.sh: $1" >&2
  exit "${2:-1}"
}

[[ $# -eq 2 ]] || fail "usage: [COLVENNA_RELEASE_REGISTRY=<registry>] [RELEASE_RECORD=<commit>] $0 <candidate> <release>" 2
candidate="$1" release="$2"
names="$(python3 "$promotion_py" names "$candidate" "$release")" || exit 2
image_repository="$(awk -F'\t' '$1 == "image" {print $2}' <<<"$names")"
image_tag="$(awk -F'\t' '$1 == "image" {print $4}' <<<"$names")"
chart_repository="$(awk -F'\t' '$1 == "chart" {print $2}' <<<"$names")"
candidate_version="$(awk -F'\t' '$1 == "chart" {print $3}' <<<"$names")"
version="$(awk -F'\t' '$1 == "chart" {print $4}' <<<"$names")"
if [[ -n "${COLVENNA_RELEASE_REGISTRY:-}" ]]; then
  registry_kind="$(python3 "$promotion_py" registry "$COLVENNA_RELEASE_REGISTRY")" || exit 2
else
  registry_kind=ecr
  release_region="${COLVENNA_RELEASE_REGION:-us-west-2}"
  [[ "$release_region" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$release_region" != cn-* ]] \
    || fail "COLVENNA_RELEASE_REGION: a standard AWS region, e.g. us-west-2, not '$release_region'" 2
fi

# 0. The tree, the candidate and its verdict.
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || fail "Commit first: a release is promoted from a clean checkout of its candidate's tag" 2
head="$(git -C "$REPO" rev-parse HEAD)"
tag_commit="$(git -C "$REPO" rev-parse -q --verify "refs/tags/$candidate^{commit}")" \
  || fail "No tag $candidate in this clone: git fetch origin --tags" 2
[[ "$head" == "$tag_commit" ]] || fail "HEAD is $head, not $candidate's tag commit $tag_commit: git checkout $candidate" 2
! git -C "$REPO" rev-parse -q --verify "refs/tags/$release" >/dev/null \
  || fail "The tag $release exists already: a release is promoted once" 2
! git -C "$REPO" rev-parse -q --verify "refs/heads/release-$release" >/dev/null \
  || fail "The branch release-$release exists already: remove it, or finish the promotion it holds" 2
! git -C "$REPO" cat-file -e "HEAD:release/$release" 2>/dev/null || fail "release/$release is already at $head" 2
manifest="$REPO/release/$candidate/manifest.json"
[[ -f "$manifest" ]] || fail "No release/$candidate/manifest.json at $candidate's tag" 2
python3 "$manifest_py" validate "$manifest" --candidate "$candidate" --go \
  || fail "$candidate's manifest is not that of a candidate for GO" 2
built_from="$(python3 "$manifest_py" get "$manifest" source.built_from)"
git -C "$REPO" cat-file -e "$built_from^{commit}" 2>/dev/null \
  || fail "This clone lacks $built_from, the commit $candidate was built from: git fetch origin" 2
changed="$(git -C "$REPO" diff --name-only "$built_from" HEAD -- . ":(exclude)release/$candidate")"
[[ -z "$changed" ]] || fail "$candidate's tag commit differs from $built_from outside release/$candidate/: $(tr '\n' ' ' <<<"$changed")" 2
python3 "$manifest_py" check-tree "$manifest" "$REPO" HEAD || fail "$candidate's manifest does not describe its tag commit" 2
record_ref="${RELEASE_RECORD:-origin/main}"
record="$(git -C "$REPO" rev-parse -q --verify "$record_ref^{commit}")" \
  || fail "RELEASE_RECORD: no commit $record_ref in this clone (git fetch origin)" 2
git -C "$REPO" merge-base --is-ancestor "$head" "$record" \
  || fail "RELEASE_RECORD $record_ref ($record) does not have $candidate's tag commit $head" 2
manifest_blob="$(git -C "$REPO" rev-parse "HEAD:release/$candidate/manifest.json")"
[[ "$(git -C "$REPO" rev-parse -q --verify "$record:release/$candidate/manifest.json" || true)" == "$manifest_blob" ]] \
  || fail "release/$candidate/manifest.json at $record_ref is not the one $candidate's tag holds" 2
notes_blob="$(git -C "$REPO" rev-parse -q --verify "$record:release/$candidate/notes.md")" \
  || fail "No release/$candidate/notes.md at $record_ref" 2

work="$(mktemp -d)"
chmod 700 "$work"
# This run's own registry credentials, for crane and Helm alike, removed with it.
export DOCKER_CONFIG="$work/docker" HELM_REGISTRY_CONFIG="$work/helm/registry.json" HELM_CACHE_HOME="$work/helm/cache"
trap 'rm -rf "$work"' EXIT
mkdir -p "$DOCKER_CONFIG" "$work/candidate" "$work/source" "$work/release" "$work/held" "$work/pulled"
git -C "$REPO" show "$record:release/$candidate/notes.md" >"$work/candidate-notes.md"
python3 "$promotion_py" verdict "$work/candidate-notes.md" >/dev/null \
  || fail "$candidate is not judged GO in release/$candidate/notes.md at $record_ref ($record)" 2

needed=(jq tar)
[[ "$registry_kind" != ecr ]] || needed+=(aws)
for command in "${needed[@]}"; do
  command -v "$command" >/dev/null || fail "$command is required" 3
done
(cd "$REPO" && scripts/install-tools.sh >/dev/null)
export PATH="$REPO/.tools:$PATH"
evidence="${RELEASE_EVIDENCE:-$REPO/.state/release/$release}"
mkdir -p "$evidence"
log() {
  echo "$*" | tee -a "$evidence/promote.log"
}
if [[ -n "${COLVENNA_RELEASE_REGISTRY:-}" ]]; then
  registry="$COLVENNA_RELEASE_REGISTRY"
else
  registry="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$release_region.amazonaws.com"
fi
[[ "$registry_kind" != ecr ]] || log "by $(aws sts get-caller-identity --query Arn --output text)"
registry_use "$registry_kind" "$registry" "$evidence/login.log" || fail "Cannot log in to $registry ($evidence/login.log)"
rehearsal=""
[[ "$registry_kind" != loopback ]] \
  || rehearsal="a rehearsal against the loopback registry $registry, not a release: never push it"
log "$(date -u +%FT%TZ) $candidate ($tag_commit, built from $built_from) to $release in $registry;" \
  "verdict GO in release/$candidate/notes.md at $record${rehearsal:+; $rehearsal}"

# still_this_commit: the tree is still C's, clean.
still_this_commit() {
  [[ -z "$(git -C "$REPO" status --porcelain)" && "$(git -C "$REPO" rev-parse HEAD)" == "$head" ]] \
    || fail "The tree moved from $head during the run: promote again"
}

# 1. Checks of what the release registry holds, before anything is written into it. The image: the
# candidate's digest, B's, and what the release's tag holds, if anything.
image="$registry/$image_repository"
image_digest="$(python3 "$manifest_py" get "$manifest" image.digest)"
config="$(crane config "$image@$image_digest" 2>&1)" \
  || fail "The release registry has no $image_repository@$image_digest, $candidate's image: $(tail -1 <<<"$config")" 3
faults="$(image_problems "$config" "$built_from" "$(python3 "$manifest_py" get "$manifest" image.platform)")"
[[ -z "$faults" ]] || fail "$image_repository@$image_digest, $candidate's image: $(tr '\n' ' ' <<<"$faults")" 3
image_held="$(registry_held "$image:$image_tag")" || fail "Cannot tell what $image_repository:$image_tag holds"
[[ -z "$image_held" || "$image_held" == "$image_digest" ]] \
  || fail "$image_repository:$image_tag is already $image_held, not $candidate's $image_digest: tags never move" 3

# The chart: the candidate's package, B's chart directory as the candidate's release packages it, with
# only its version and appVersion changed, and compared file by file.
chart="$registry/$chart_repository"
candidate_chart="$(python3 "$manifest_py" get "$manifest" chart.digest)"
chart_name="$(python3 "$manifest_py" get "$manifest" chart.name)"
chart_path="$(python3 "$manifest_py" get "$manifest" chart.path)"
helm pull "oci://$chart@$candidate_chart" -d "$work/candidate" "${REGISTRY_PLAIN[@]}" >>"$evidence/chart.log" 2>&1 \
  || { tail -5 "$evidence/chart.log" >&2; fail "Cannot pull $candidate's chart, $chart_repository@$candidate_chart" 3; }
candidate_package="$(registry_package "$work/candidate")" || fail "Cannot pull $candidate's chart"
git -C "$REPO" archive "$built_from" "$chart_path" | tar -x -C "$work/source"
release_chart_source "$work/source/$chart_path" "$work/expected/$chart_name" "$candidate_version" "$candidate" "$built_from"
differences="$(release_chart_differences "$candidate_package" "$work/expected/$chart_name")" \
  || fail "Cannot compare $candidate's chart with $built_from's chart directory"
[[ -z "$differences" ]] || fail "$chart_repository@$candidate_chart is not $built_from's chart directory as $candidate's release packages it: $(head -10 <<<"$differences" | tr '\n' ' ')" 3
directory="$(python3 "$promotion_py" rechart "$candidate_package" "$work/release" "$version" "$version")"
helm package "$directory" -d "$work/packaged" >>"$evidence/chart.log" 2>&1 \
  || { tail -5 "$evidence/chart.log" >&2; fail "helm package failed ($evidence/chart.log)"; }
package="$work/packaged/$chart_name-$version.tgz"
python3 "$promotion_py" chart-diff "$candidate_package" "$package" "$version" "$version" >"$evidence/chart-packaged.txt" \
  || { cat "$evidence/chart-packaged.txt" >&2; fail "The packaged chart is not $candidate's but for version and appVersion ($evidence/chart-packaged.txt)"; }
chart_files="$(tar -tzf "$package" | grep -vc '/$')"
chart_digest="$(registry_held "$chart:$version")" || fail "Cannot tell what $chart_repository:$version holds"
if [[ -n "$chart_digest" ]]; then
  # An earlier run's chart (packaging is not reproducible byte for byte): it must be the same files.
  helm pull "oci://$chart@$chart_digest" -d "$work/held" "${REGISTRY_PLAIN[@]}" >>"$evidence/chart.log" 2>&1 \
    || fail "Cannot pull $chart_repository@$chart_digest"
  held_package="$(registry_package "$work/held")" || fail "Cannot pull $chart_repository@$chart_digest"
  python3 "$promotion_py" chart-diff "$candidate_package" "$held_package" "$version" "$version" >>"$evidence/chart.log" 2>&1 \
    || fail "The release registry holds chart $version ($chart_digest), which is not $candidate's but for version and appVersion: tags never move" 3
fi

# 2. Into the release registry: the image's second tag, and the chart.
if [[ -z "$image_held" ]]; then
  crane tag "$image@$image_digest" "$image_tag" >>"$evidence/image.log" 2>&1 \
    || { tail -5 "$evidence/image.log" >&2; fail "crane tag failed ($evidence/image.log)"; }
  log "image: tagged $image_repository@$image_digest $image_tag"
else
  log "image: $image_repository:$image_tag is $image_digest already"
fi
image_held="$(registry_held "$image:$image_tag")" || fail "Cannot tell what $image_repository:$image_tag holds"
[[ "$image_held" == "$image_digest" ]] || fail "$image_repository:$image_tag is ${image_held:-nothing}, not $image_digest, after the tag" 3
if [[ -n "$chart_digest" ]]; then
  log "chart: $chart_repository:$version is there already ($chart_digest), $candidate's but for version and appVersion"
else
  helm push "$package" "oci://${chart%/*}" "${REGISTRY_PLAIN[@]}" >"$work/push.log" 2>&1 \
    || { cat "$work/push.log" >>"$evidence/chart.log"; tail -5 "$work/push.log" >&2; fail "helm push failed ($evidence/chart.log)"; }
  cat "$work/push.log" >>"$evidence/chart.log"
  chart_digest="$(registry_held "$chart:$version")" || fail "Cannot tell what $chart_repository:$version holds"
  grep -qx "Digest: $chart_digest" "$work/push.log" \
    || fail "The release registry holds $chart_repository:$version as ${chart_digest:-nothing}, not what helm pushed"
  log "chart: pushed $chart_repository:$version $chart_digest"
fi
# What the registry holds, pulled back by its digest, compared with the candidate's: the comparison kept.
helm pull "oci://$chart@$chart_digest" -d "$work/pulled" "${REGISTRY_PLAIN[@]}" >>"$evidence/chart.log" 2>&1 \
  || fail "Cannot pull $chart_repository@$chart_digest back"
pulled_package="$(registry_package "$work/pulled")" || fail "Cannot pull $chart_repository@$chart_digest back"
{
  echo "$chart_repository@$chart_digest (version $version), pulled back from the release registry, against"
  echo "$candidate's chart $chart_repository@$candidate_chart (version $candidate_version), file by file:"
  python3 "$promotion_py" chart-diff "$candidate_package" "$pulled_package" "$version" "$version"
} >"$work/chart-comparison.txt" \
  || { cp "$work/chart-comparison.txt" "$evidence/"; fail "$chart_repository@$chart_digest, pulled back, is not $candidate's chart but for version and appVersion ($evidence/chart-comparison.txt)" 3; }
log "chart: $chart_repository@$chart_digest, pulled back, is $candidate's chart with only version and appVersion" \
  "changed, to $version ($chart_files files)"

# 3. The manifest, the notes and the comparison, then the commit and the tag.
still_this_commit
cp "$manifest" "$work/candidate.json"
jq -n --arg release "$release" --arg at "$(date -u +%FT%TZ)" --arg tag_commit "$tag_commit" --arg blob "$manifest_blob" \
  --arg record "$record" --arg notes "$notes_blob" --arg chart "$chart_digest" --argjson files "$chart_files" \
  '{release: $release, generated: $at, tag_commit: $tag_commit, manifest_blob: $blob,
    acceptance: {commit: $record, blob: $notes}, chart_digest: $chart, chart_files: $files}' >"$work/params.json"
python3 "$promotion_py" assemble "$work" >"$work/manifest.json"
python3 "$promotion_py" validate "$work/manifest.json" --release "$release"
python3 "$promotion_py" check-tree "$work/manifest.json" "$REPO" HEAD
python3 "$promotion_py" notes "$work/manifest.json" <"$work/candidate-notes.md" >"$work/notes.md"
written=(manifest.json notes.md chart-comparison.txt)
(cd "$work" && cp "${written[@]}" "$evidence/")
still_this_commit
mkdir -p "$REPO/release/$release"
(cd "$work" && cp "${written[@]}" "$REPO/release/$release/")
git -C "$REPO" switch -q -c "release-$release"
git -C "$REPO" add "release/$release"
git -C "$REPO" commit -q -m "release: $release, promoted from $candidate${rehearsal:+ ($rehearsal)}" \
  -m "$candidate (built from $built_from) judged GO in release/$candidate/notes.md at $record; the same image, the chart re-versioned only."
commit="$(git -C "$REPO" rev-parse HEAD)"
[[ "$(git -C "$REPO" diff --name-only "$head" "$commit" | sort | tr '\n' ' ')" \
  == "release/$release/chart-comparison.txt release/$release/manifest.json release/$release/notes.md " ]] \
  || fail "The commit $commit holds more than release/$release/: remove it (git switch --detach $head; git branch -D release-$release)"
git -C "$REPO" tag -a "$release" -m "$release: $candidate promoted, built from $built_from${rehearsal:+ ($rehearsal)}" "$commit"
log "committed release/$release/ on $candidate's tag commit as $commit (branch release-$release), tagged $release"
if [[ -n "$rehearsal" ]]; then
  log "$rehearsal; remove it: git tag -d $release; git switch --detach $head; git branch -D release-$release"
else
  log "next: git push origin $release release-$release, then the pull request release-$release (merged with a merge commit, never rebased)"
fi
