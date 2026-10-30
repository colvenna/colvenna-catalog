# shellcheck shell=bash
# install.sh --release <candidate>: a release candidate installed from a checkout of its tag, never
# built (docs/development.md, "Releasing"). Its manifest, release/<candidate>/manifest.json
# (scripts/release/manifest.py), pins the server's image and the Helm chart by digest; the release
# registry that holds them is the configuration's COLVENNA_RELEASE_REGISTRY
# (<account>.dkr.ecr.<region>.amazonaws.com), never the manifest's. Reads REPO and the configuration;
# release_auth needs WORK (install.sh's scratch directory), whose removal takes the credentials with it.
# install.sh --release and scripts/release/verify-artifacts.sh check a candidate with the same functions,
# and scripts/release/make-manifest.sh packages its chart with release_chart_source.

# The image's label and the packaged chart's annotation that name the commit a candidate was built from.
RELEASE_REVISION_LABEL=org.opencontainers.image.revision
RELEASE_SOURCE_ANNOTATION=colvenna-catalog/source-commit

# release_tool <manifest.py arguments...>
release_tool() {
  python3 "$REPO/scripts/release/manifest.py" "$@"
}

# release_value <field.field...>: one value of the candidate's manifest (RELEASE_MANIFEST).
release_value() {
  release_tool get "$RELEASE_MANIFEST" "$1"
}

# release_note <text>: a line of the install's evidence (EVIDENCE/release.txt), when there is one.
release_note() {
  [[ -z "${EVIDENCE:-}" ]] || printf '%s\n' "$*" >>"$EVIDENCE/release.txt"
}

# release_checkout <candidate>: this checkout installs the candidate, or the install is refused (status
# 3): its manifest is here and valid; the tree is clean and differs from the commit the candidate was
# built from (source.built_from) under release/<candidate>/ alone, as the candidate's tag does; and the
# git objects the manifest pins (the chart, install.sh, uninstall.sh, deploy/lib, the CDK app, the UAT
# cases) are this checkout's. Exports RELEASE_MANIFEST and SOURCE_SHA, the commit the candidate was
# built from (the stacks' SourceSha, the pods' source-sha).
release_checkout() {
  local candidate="$1" manifest="$REPO/release/$1/manifest.json" err built_from changed
  [[ -f "$manifest" ]] || refuse "No release/$candidate/manifest.json in this checkout: check out the candidate's tag" \
    "(git checkout $candidate)"
  [[ -z "$(git -C "$REPO" status --porcelain)" ]] \
    || refuse "--release installs from a clean checkout of the tag $candidate: commit or remove the local changes"
  err="$(mktemp)"
  if ! release_tool validate "$manifest" --candidate "$candidate" 2>"$err"; then
    refuse "release/$candidate/manifest.json is not a manifest this checkout can install: $(tr '\n' ' ' <"$err"; rm -f "$err")"
  fi
  RELEASE_MANIFEST="$manifest"
  built_from="$(release_value source.built_from)"
  if ! git -C "$REPO" cat-file -e "$built_from^{commit}" 2>/dev/null; then
    rm -f "$err"
    refuse "This clone lacks $built_from, the commit $candidate was built from: git fetch origin --tags"
  fi
  changed="$(git -C "$REPO" diff --name-only "$built_from" HEAD -- . ":(exclude)release/$candidate")"
  if [[ -n "$changed" ]]; then
    rm -f "$err"
    refuse "This checkout differs from $built_from, the commit $candidate was built from, outside" \
      "release/$candidate/: check out the tag $candidate. Differing: $(head -20 <<<"$changed" | tr '\n' ' ')"
  fi
  if ! release_tool check-tree "$manifest" "$REPO" HEAD 2>"$err"; then
    refuse "The manifest of $candidate does not describe this checkout: $(tr '\n' ' ' <"$err"; rm -f "$err")"
  fi
  rm -f "$err"
  release_note "checkout: $(git -C "$REPO" rev-parse HEAD), clean, differs from $built_from (built_from) under" \
    "release/$candidate/ alone; the manifest is valid and pins this checkout's git objects"
  SOURCE_SHA="$built_from"
  export RELEASE_MANIFEST SOURCE_SHA
}

# release_form_check <form>: the candidate is accepted in this deployment form (its manifest's
# configurations.forms), or the install is refused (status 3). A candidate made before the serverless
# form (#13), or without RELEASE_FORMS naming it, is the EKS form's alone.
release_form_check() {
  local forms
  forms="$(release_value configurations.forms)"
  jq -e --arg f "$1" 'index($f) != null' <<<"$forms" >/dev/null \
    || refuse "$(release_value candidate) is accepted in the form(s) $(jq -r 'join(", ")' <<<"$forms"), not $1"
  release_note "form: $1 is among the candidate's forms ($(jq -r 'join(", ")' <<<"$forms"))"
}

# release_registry_parts <registry>: its account and Region, tab-separated (manifest.py registry).
release_registry_parts() {
  release_tool registry "$1"
}

# The media types of the manifests an install reads: images (OCI and Docker) and the chart (OCI).
RELEASE_MEDIA_TYPES=(application/vnd.oci.image.manifest.v1+json application/vnd.docker.distribution.manifest.v2+json
  application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json)

# release_registry_check: COLVENNA_RELEASE_REGISTRY holds the manifest's image and chart, each by its
# digest. Reads with ecr:BatchGetImage alone, which is what the release registry lets another account
# do; refuses (status 3) naming each one missing or unreadable.
release_registry_check() {
  local account region what repository digest out err missing=()
  IFS=$'\t' read -r account region < <(release_registry_parts "$COLVENNA_RELEASE_REGISTRY")
  err="$(mktemp)"
  for what in image chart; do
    repository="$(release_value "$what.repository")"
    digest="$(release_value "$what.digest")"
    # Only stdout is the answer; a warning the AWS CLI prints goes with the error, if any.
    if ! out="$(aws ecr batch-get-image --region "$region" --registry-id "$account" --repository-name "$repository" \
      --image-ids "imageDigest=$digest" --accepted-media-types "${RELEASE_MEDIA_TYPES[@]}" \
      --query '[images[0].imageId.imageDigest, failures[0].failureCode]' --output text 2>"$err")"; then
      missing+=("$what: cannot read $repository in $COLVENNA_RELEASE_REGISTRY: $(grep . "$err" | tail -1)")
      continue
    fi
    read -r out _ <<<"$out"
    if [[ "$out" == "$digest" ]]; then
      release_note "registry: $repository@$digest is there (ecr:BatchGetImage)"
    else
      missing+=("$what: $COLVENNA_RELEASE_REGISTRY holds no $repository@$digest")
    fi
  done
  rm -f "$err"
  ((${#missing[@]} == 0)) || refuse "The release registry lacks what $(release_value candidate) pins:" \
    "$(printf '%s; ' "${missing[@]}")ask its maintainer to add this account to its pull accounts, or give the registry that holds it"
}

# release_crane_login <registry>: crane logged into the ECR registry, with credentials in RELEASE_AUTH.
release_crane_login() {
  local region
  region="$(release_registry_parts "$1" | cut -f2)"
  aws ecr get-login-password --region "$region" \
    | DOCKER_CONFIG="$RELEASE_AUTH" crane auth login "$1" -u AWS --password-stdin >/dev/null 2>&1
}

# release_auth: crane and Helm logged into the release registry, with credentials kept in WORK only.
# Sets RELEASE_AUTH.
release_auth() {
  local region
  RELEASE_AUTH="$WORK/registry-auth"
  mkdir -p "$RELEASE_AUTH"
  release_crane_login "$COLVENNA_RELEASE_REGISTRY"
  region="$(release_registry_parts "$COLVENNA_RELEASE_REGISTRY" | cut -f2)"
  aws ecr get-login-password --region "$region" \
    | helm registry login "$COLVENNA_RELEASE_REGISTRY" --registry-config "$RELEASE_AUTH/helm.json" \
      --username AWS --password-stdin >/dev/null 2>&1
}

# release_chart_source <chart directory> <out> <version> <appVersion> <commit>: the chart as a release
# packages it, in <out> (named as the chart): a copy of the directory whose Chart.yaml has the
# candidate's version and appVersion and names the commit in the RELEASE_SOURCE_ANNOTATION annotation.
# Nothing else differs. make-manifest.sh packages this copy; a verification makes it again from
# built_from and compares the release's chart with it.
release_chart_source() {
  local from="$1" out="$2" version="$3" app="$4" commit="$5"
  if grep -q '^annotations:' "$from/Chart.yaml"; then
    echo "$from/Chart.yaml has annotations of its own: release_chart_source cannot add $RELEASE_SOURCE_ANNOTATION" >&2
    return 1
  fi
  rm -rf "$out"
  mkdir -p "$(dirname "$out")"
  cp -R "$from" "$out"
  sed -i -e "s/^version: .*/version: $version/" -e "s/^appVersion: .*/appVersion: \"$app\"/" "$out/Chart.yaml"
  printf 'annotations:\n  %s: "%s"\n' "$RELEASE_SOURCE_ANNOTATION" "$commit" >>"$out/Chart.yaml"
}

# release_chart_differences <package> <directory>: what differs between the packaged chart and the chart
# directory (release_chart_source's), one line each; nothing when the package holds exactly its files.
# Helm rewrites Chart.yaml as it packages it, so that one is compared as Helm reads it (helm show chart).
# Fails, rather than find nothing, when it cannot read either.
release_chart_differences() {
  local package="$1" directory="$2" files want have
  want="$(helm show chart "$directory")" || return 1
  have="$(helm show chart "$package")" || return 1
  files="$(mktemp -d)"
  # -m: the files' times are not compared, and a package made a moment ago is not "in the future".
  tar -xzmf "$package" -C "$files" || { rm -rf "$files"; return 1; }
  diff -r --exclude=Chart.yaml "$directory" "$files/$(basename "$directory")" || true
  diff <(printf '%s\n' "$want") <(printf '%s\n' "$have") | sed 's/^/Chart.yaml: /' || true
  rm -rf "$files"
}

# copy_release_image <repository URI>: the candidate's image, copied by its digest from the release
# registry into the environment's own repository, tagged <candidate> there; a tag that repository
# already holds must be that digest (tags never move). After release_auth. Sets IMAGE_REPOSITORY and
# IMAGE_DIGEST.
copy_release_image() {
  local repository="$1" digest tag held source
  digest="$(release_value image.digest)"
  tag="$(release_value image.tag)"
  source="$COLVENNA_RELEASE_REGISTRY/$(release_value image.repository)@$digest"
  release_crane_login "${repository%%/*}"
  held="$(aws ecr describe-images --region "$COLVENNA_REGION" --repository-name "${repository#*/}" \
    --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
  if [[ "$held" == sha256:* && "$held" != "$digest" ]]; then
    echo "$repository:$tag is already $held, not the release's $digest" >&2
    return 1
  fi
  if [[ "$held" != "$digest" ]]; then
    DOCKER_CONFIG="$RELEASE_AUTH" crane copy "$source" "$repository:$tag" >>"$EVIDENCE/image.log" 2>&1 \
      || { tail -10 "$EVIDENCE/image.log" >&2; return 1; }
  fi
  held="$(DOCKER_CONFIG="$RELEASE_AUTH" crane digest "$repository:$tag")"
  [[ "$held" == "$digest" ]] || { echo "$repository:$tag is $held after the copy, not $digest" >&2; return 1; }
  echo "image: $source as $repository:$tag" | tee -a "$EVIDENCE/image.log"
  IMAGE_REPOSITORY="$repository" IMAGE_DIGEST="$digest"
  export IMAGE_REPOSITORY IMAGE_DIGEST
}

# fetch_release_chart <dir>: the candidate's chart, pulled by its digest from the release registry into
# the directory, made empty first (after release_auth). Sets RELEASE_CHART, the package helm upgrades the
# release with; release_verify_artifacts checks it.
fetch_release_chart() {
  local dir="$1" reference packages
  reference="oci://$COLVENNA_RELEASE_REGISTRY/$(release_value chart.repository)@$(release_value chart.digest)"
  rm -rf "$dir"
  mkdir -p "$dir"
  helm pull "$reference" --registry-config "$RELEASE_AUTH/helm.json" -d "$dir" >>"$EVIDENCE/helm.log" 2>&1 \
    || { tail -5 "$EVIDENCE/helm.log" >&2; return 1; }
  # The one package the pull left (Helm names it <chart>@sha256-<hex>.tgz).
  packages="$(find "$dir" -maxdepth 1 -name '*.tgz')"
  [[ -n "$packages" && "$packages" != *$'\n'* ]] || { echo "helm pull $reference left no single package in $dir" >&2; return 1; }
  RELEASE_CHART="$packages"
  echo "chart: $reference" | tee -a "$EVIDENCE/helm.log"
  export RELEASE_CHART
}

# release_verify_artifacts <dir>: the release registry's image and chart are those of the commit the
# candidate was built from, B (source.built_from), or the install is refused (status 3), naming each
# mismatch. After release_auth; <dir> is scratch space. The image (read by its digest) carries the label
# RELEASE_REVISION_LABEL=B and is for the manifest's platform. The chart, pulled by its digest
# (fetch_release_chart), is the manifest's name and version, names B in its RELEASE_SOURCE_ANNOTATION
# annotation, and holds exactly B's chart directory (chart.path, whose tree is chart.tree) as a release
# packages it: so chart.digest is chart.tree's.
release_verify_artifacts() {
  local dir="$1" built_from image config revision platform name version path differences annotation problems=()
  built_from="$(release_value source.built_from)"
  image="$COLVENNA_RELEASE_REGISTRY/$(release_value image.repository)@$(release_value image.digest)"
  if ! config="$(DOCKER_CONFIG="$RELEASE_AUTH" crane config "$image" 2>&1)"; then
    problems+=("image: cannot read its configuration: $(tail -1 <<<"$config")")
  else
    revision="$(jq -r --arg l "$RELEASE_REVISION_LABEL" '.config.Labels[$l] // ""' <<<"$config")"
    [[ "$revision" == "$built_from" ]] \
      || problems+=("image: its label $RELEASE_REVISION_LABEL is ${revision:-missing}, not $built_from (built_from)")
    platform="$(jq -r '"\(.os)/\(.architecture)"' <<<"$config")"
    [[ "$platform" == "$(release_value image.platform)" ]] \
      || problems+=("image: it is for $platform, not $(release_value image.platform)")
  fi
  fetch_release_chart "$dir/pulled"
  name="$(release_value chart.name)"
  version="$(release_value chart.version)"
  path="$(release_value chart.path)"
  [[ "$(helm show chart "$RELEASE_CHART" | sed -n 's/^name: //p')" == "$name" ]] || problems+=("chart: not $name")
  [[ "$(helm show chart "$RELEASE_CHART" | sed -n 's/^version: //p')" == "$version" ]] \
    || problems+=("chart: not version $version")
  annotation="$(helm show chart "$RELEASE_CHART" | sed -n "s|^  $RELEASE_SOURCE_ANNOTATION: ||p" | tr -d '"')"
  [[ "$annotation" == "$built_from" ]] \
    || problems+=("chart: its annotation $RELEASE_SOURCE_ANNOTATION is ${annotation:-missing}, not $built_from (built_from)")
  mkdir -p "$dir/source"
  git -C "$REPO" archive "$built_from" "$path" | tar -x -C "$dir/source"
  release_chart_source "$dir/source/$path" "$dir/expected/$name" "$version" "$(release_value chart.appVersion)" "$built_from"
  differences="$(release_chart_differences "$RELEASE_CHART" "$dir/expected/$name")"
  [[ -z "$differences" ]] || problems+=("chart: its files are not $built_from's $path as a release packages it:" \
    "$(head -10 <<<"$differences" | tr '\n' ' ')")
  ((${#problems[@]} == 0)) || refuse "$(release_value candidate) is not what the registry holds: $(printf '%s; ' "${problems[@]}")"
  release_note "image: $image is labelled $RELEASE_REVISION_LABEL=$built_from, for $platform"
  release_note "chart: $RELEASE_CHART is $name $version, annotated $RELEASE_SOURCE_ANNOTATION=$built_from, and holds" \
    "exactly $built_from's $path ($(release_value chart.tree)) as a release packages it"
}
