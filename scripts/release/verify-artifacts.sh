#!/usr/bin/env bash
# Verifies release candidate <candidate> from a clean checkout of its tag, as install.sh --release does
# before it installs, and installs nothing. It checks that:
#   - the checkout is the tag's: release/<candidate>/manifest.json is valid, the tree differs from the
#     commit the candidate was built from (source.built_from, B) under release/<candidate>/ alone, and
#     the manifest pins this checkout's git objects;
#   - the release registry COLVENNA_RELEASE_REGISTRY holds the image and the chart by digest;
#   - the image (read by its digest) is labelled org.opencontainers.image.revision=<B> and is for
#     linux/amd64;
#   - the chart (pulled by its digest) is the manifest's version, is annotated
#     colvenna-catalog/source-commit: <B>, and holds exactly B's deploy/helm/colvenna-catalog (chart.tree)
#     as a release packages it (Chart.yaml's version, appVersion and that annotation are all it adds).
# So the digests are B's. It reads with the pull accounts' rights only (ecr:BatchGetImage,
# ecr:GetDownloadUrlForLayer, and ecr:GetAuthorizationToken in the caller's own account).
#
# Usage: COLVENNA_RELEASE_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com \
#          scripts/release/verify-artifacts.sh <candidate>
#   RELEASE_EVIDENCE  where the evidence goes (default .state/release/verify-<candidate>-<time>/, git-ignored)
# Exit status: 0 verified; 1 a step failed; 2 the arguments or COLVENNA_RELEASE_REGISTRY are wrong;
# 3 something is not the candidate's (the checkout, the registry's content or the artifacts), or a tool
# is missing.
set -Eeuo pipefail
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/preflight.sh
source "$REPO/deploy/lib/preflight.sh"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/release.sh
source "$REPO/deploy/lib/release.sh"

if [[ $# -ne 1 ]]; then
  echo "usage: COLVENNA_RELEASE_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com $0 <candidate>" >&2
  exit 2
fi
candidate="$1"
release_tool names "$candidate" >/dev/null || exit 2
if [[ -z "${COLVENNA_RELEASE_REGISTRY:-}" ]]; then
  echo "COLVENNA_RELEASE_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com: the release registry that holds $candidate" >&2
  exit 2
fi
release_registry_parts "$COLVENNA_RELEASE_REGISTRY" >/dev/null || exit 2
for tool in aws jq git curl tar sha256sum; do
  command -v "$tool" >/dev/null || refuse "$tool is required"
done
"$REPO/scripts/install-tools.sh" >/dev/null
export PATH="$REPO/.tools:$PATH"
EVIDENCE="${RELEASE_EVIDENCE:-$REPO/.state/release/verify-$candidate-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$EVIDENCE"
WORK="$(mktemp -d)"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
export EVIDENCE WORK

release_checkout "$candidate"
release_registry_check
release_auth
release_verify_artifacts "$WORK/release"
cat "$EVIDENCE/release.txt"
echo "release $candidate: its image and chart are $SOURCE_SHA's; evidence in $EVIDENCE" | tee "$EVIDENCE/RESULT"
