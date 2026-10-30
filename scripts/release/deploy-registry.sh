#!/usr/bin/env bash
# Deploys ColvennaCatalogRelease, the release registry (deploy/aws/src/release-stack.ts): the ECR
# repositories colvenna-catalog-release/colvenna-catalog (the server's image) and
# colvenna-catalog-release/charts/colvenna-catalog (its Helm chart, as an OCI artifact), which release
# candidates are pushed into (make-manifest.sh) and installed from (install.sh --release). They are
# in the caller's account and COLVENNA_RELEASE_REGION (default us-west-2), and belong to no
# environment: no install or uninstall touches them, and the stack keeps them when it is deleted.
# Deploying it again changes nothing a candidate holds; it only sets who may pull.
#
# Usage: [COLVENNA_RELEASE_REGION=<region>] [COLVENNA_RELEASE_PULL_ACCOUNTS=<account>,<account>] \
#          scripts/release/deploy-registry.sh
#   COLVENNA_RELEASE_PULL_ACCOUNTS  other accounts whose installs may pull the release's image and chart:
#                                   each repository's policy lets their IAM principals pull
#                                   (ecr:BatchGetImage, ecr:GetDownloadUrlForLayer,
#                                   ecr:BatchCheckLayerAvailability), never push, never anyone ("*");
#                                   none by default. Every deploy sets the list anew: an account left
#                                   out loses its access.
# From a clean, committed tree (the stack records the commit). Exit status: 0 deployed; 1 the deploy
# failed; 2 the tree or a setting is wrong. The stack's outputs and the policies it set go to
# .state/release/ (git-ignored).
set -Eeuo pipefail
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
tool="$REPO/scripts/release/manifest.py"

die() {
  echo "$*" >&2
  exit 2
}

[[ $# -eq 0 ]] || die "usage: [COLVENNA_RELEASE_REGION=<region>] [COLVENNA_RELEASE_PULL_ACCOUNTS=<id>,<id>] $0"
region="${COLVENNA_RELEASE_REGION:-us-west-2}"
[[ "$region" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$region" != cn-* ]] \
  || die "COLVENNA_RELEASE_REGION: a standard AWS region, e.g. us-west-2, not '$region'"
pullers="${COLVENNA_RELEASE_PULL_ACCOUNTS:-}"
if [[ -n "$pullers" ]]; then
  # The stack checks them again; here, before anything is read from AWS.
  [[ "$pullers" =~ ^[0-9]{12}(,[0-9]{12})*$ ]] \
    || die "COLVENNA_RELEASE_PULL_ACCOUNTS: 12-digit account IDs, comma-separated (never '*'), not '$pullers'"
  [[ "$(tr , '\n' <<<"$pullers" | sort | uniq -d)" == "" ]] || die "COLVENNA_RELEASE_PULL_ACCOUNTS: an account is listed twice"
fi
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || die "Commit first: the stack records the commit it is of"
sha="$(git -C "$REPO" rev-parse HEAD)"
stack="$(python3 "$tool" stack)"
account="$(aws sts get-caller-identity --query Account --output text)"
state="$REPO/.state/release"
mkdir -p "$state"
echo "$stack in $region (account $account) at $sha; pull accounts: ${pullers:-none}"

[[ -d "$REPO/deploy/aws/node_modules" ]] || (cd "$REPO/deploy/aws" && npm ci --no-audit --no-fund >/dev/null)
args=(-c stack=release -c "account=$account" -c "region=$region" -c "sourceSha=$sha")
[[ -z "$pullers" ]] || args+=(-c "pullAccounts=$pullers")
(cd "$REPO/deploy/aws" && npx cdk deploy "$stack" --require-approval never --no-notices --no-version-reporting \
  --output "$state/cdk.out" --outputs-file "$state/outputs.json" "${args[@]}") >"$state/deploy.log" 2>&1 \
  || { tail -30 "$state/deploy.log" >&2; exit 1; }

# What the repositories allow now, for the record.
for repository in $(python3 "$tool" repositories); do
  policy="$(aws ecr get-repository-policy --region "$region" --repository-name "$repository" \
    --query policyText --output text 2>/dev/null || echo none)"
  printf '%s: %s\n' "$repository" "$policy"
done | tee "$state/policies.txt"
echo "$stack deployed; outputs in $state/outputs.json"
