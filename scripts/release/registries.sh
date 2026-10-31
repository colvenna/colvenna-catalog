# shellcheck shell=bash
# The registry steps promote.sh and publish-ghcr.sh share. A registry is the release registry, an ECR
# one (promotion.py registry says ecr), or a loopback one (127.0.0.1:<port>, loopback) that stands in
# for a registry in a rehearsal, as in the offline tests, read and written over plain HTTP. crane and
# Helm keep their credentials where DOCKER_CONFIG and HELM_REGISTRY_CONFIG say, the caller's scratch
# directory. Reads REPO, and RELEASE_REVISION_LABEL (deploy/lib/release.sh).

# registry_use <kind> <registry> <log>: crane and Helm ready for the registry. An ECR one is logged into
# with the caller's AWS credentials (its Region from its name), the logins' output appended to the log;
# a loopback one needs no login, and Helm reaches it with --plain-http. Sets REGISTRY_PLAIN, the flags
# Helm takes for it.
# shellcheck disable=SC2034 # REGISTRY_PLAIN: the scripts that source this read it
registry_use() {
  local kind="$1" registry="$2" log="$3" region
  REGISTRY_PLAIN=()
  if [[ "$kind" == loopback ]]; then
    REGISTRY_PLAIN=(--plain-http)
    return 0
  fi
  region="$(python3 "$REPO/scripts/release/manifest.py" registry "$registry" | cut -f2)" || return 1
  aws ecr get-login-password --region "$region" | crane auth login "$registry" -u AWS --password-stdin >>"$log" 2>&1 \
    || return 1
  aws ecr get-login-password --region "$region" \
    | helm registry login "$registry" --username AWS --password-stdin >>"$log" 2>&1
}

# registry_absent <error file>: whether the error a registry read left in the file says that what was
# asked for is not there: the distribution API's MANIFEST_UNKNOWN, NAME_UNKNOWN or BLOB_UNKNOWN, status
# 404, or Helm's "not found".
registry_absent() {
  grep -qE 'MANIFEST_UNKNOWN|NAME_UNKNOWN|BLOB_UNKNOWN|status code 404|: not found' "$1"
}

# registry_read_failed <what> <error file>: after a read of what failed, leaving its error in the file:
# the caller's fail, with status 3 when the registry says it is not there, 1 otherwise (credentials, the
# network, the registry itself).
registry_read_failed() {
  local said
  said="$(grep . "$2" | tail -1)"
  if registry_absent "$2"; then
    fail "The registry has no $1: $said" 3
  fi
  fail "Cannot read $1: $said"
}

# registry_held <reference>: the digest the registry holds under the reference's tag, or nothing when it
# holds none; status 1, saying why on stderr, when it cannot tell. Only crane's output is the answer:
# what it says on stderr (a warning that it read with GET after a HEAD failed, say) is not.
registry_held() {
  local out err
  err="$(mktemp)"
  if out="$(crane digest "$1" 2>"$err")"; then
    rm -f "$err"
    echo "$out"
    return 0
  fi
  if registry_absent "$err"; then
    rm -f "$err"
    return 0
  fi
  echo "cannot read $1: $(grep . "$err" | tail -1)" >&2
  rm -f "$err"
  return 1
}

# registry_package <dir>: the one chart package a helm pull left in the directory; status 1 otherwise.
registry_package() {
  local packages
  packages="$(find "$1" -maxdepth 1 -name '*.tgz')"
  if [[ -z "$packages" || "$packages" == *$'\n'* ]]; then
    echo "helm pull left no single package in $1" >&2
    return 1
  fi
  echo "$packages"
}

# image_problems <configuration> <commit> <platform>: what is wrong with an image's configuration (as
# crane config prints it), one line each: its RELEASE_REVISION_LABEL is not the commit, or it is for
# another platform. Nothing when it is the commit's, for that platform.
image_problems() {
  local revision platform
  revision="$(jq -r --arg l "$RELEASE_REVISION_LABEL" '.config.Labels[$l] // ""' <<<"$1")"
  [[ "$revision" == "$2" ]] || echo "its label $RELEASE_REVISION_LABEL is ${revision:-missing}, not $2"
  platform="$(jq -r '"\(.os)/\(.architecture)"' <<<"$1")"
  [[ "$platform" == "$3" ]] || echo "it is for $platform, not $3"
}
