#!/usr/bin/env bash
# Shared by tests/environment/up.sh, down.sh, verify.sh and network-policy.sh: names, pins and the state directory of a test
# environment. A test environment is <name> in <region>: the stack ColvennaCatalogTest<Name>, whose local
# state (kubeconfig, outputs, evidence) is in .state/test-<name>-<region>/, never committed.
# shellcheck disable=SC2034  # the pins are read by the scripts that source this file
set -euo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
export PATH="$REPO/.tools:$PATH"
AWS_DIR="$REPO/deploy/aws"

# The AWS Load Balancer Controller: the chart by checksum, the image by digest (as colvenna-lake pins them).
LBC_CHART_VERSION=3.6.0
LBC_CHART_SHA256=ee0403fcd713d6f1d28b1ff17d0d6d47e011cc4dd727a0aed0d95ae46897226a
LBC_IMAGE_DIGEST=sha256:337a9bf9a99bbac372c8da20afad7b059e0bc89034f1087e325455b28c7fd488
# A small image for the NetworkPolicy check: an HTTP server and a client.
BUSYBOX_IMAGE=public.ecr.aws/docker/library/busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
# The node groups (test-env/stack.ts): the catalogs' two nodes and the clients' tainted ones, told
# apart by POOL_LABEL. up.sh picks zones that offer both types.
CATALOG_NODE_TYPE=t3.large
CLIENT_NODE_TYPE=t3.xlarge
POOL_LABEL=colvenna-catalog/test-pool
CATALOG_POOL=catalog
CLIENT_POOL=clients
# The catalog pods, of every install in the cluster (the chart's labels).
CATALOG_PODS=app.kubernetes.io/name=colvenna-catalog
# Zones whose IDs EKS does not support for a cluster's control plane.
EKS_UNSUPPORTED_ZONE_IDS=(use1-az3 usw1-az2 cac1-az3)

die() {
  echo "$*" >&2
  exit 1
}

# check_args <name> <region>: the name as environments are named, a standard region.
check_args() {
  [[ "$1" =~ ^[a-z][a-z0-9]{0,7}$ && "$1" != release ]] \
    || die "name: a lowercase letter, then lowercase letters and digits, 8 at most (not release): $1"
  [[ "$2" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$2" != cn-* ]] || die "region: a standard AWS region: $2"
}

stack_name() {
  printf 'ColvennaCatalogTest%s%s\n' "$(printf '%s' "${1:0:1}" | tr '[:lower:]' '[:upper:]')" "${1:1}"
}

state_dir() {
  printf '%s/.state/test-%s-%s\n' "$REPO" "$1" "$2"
}

# stack_output <region> <stack> <key>
stack_output() {
  aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
    --query "Stacks[0].Outputs[?OutputKey=='$3'].OutputValue | [0]" --output text
}

# host_patch_tag: refuses a long run on this host while it carries the Patch Group tag, which
# reboots it at 00:00 UTC. Outside EC2 (no instance metadata) there is nothing to check.
host_patch_tag() {
  local token id region tag
  token="$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
  [[ -n "$token" ]] || return 0
  id="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)"
  region="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/placement/region)"
  tag="$(aws ec2 describe-tags --region "$region" --filters "Name=resource-id,Values=$id" 'Name=key,Values=Patch Group' \
    --query 'Tags[0].Value' --output text)"
  if [[ "$tag" != None ]]; then
    die "This host carries the tag Patch Group=$tag and reboots at 00:00 UTC; remove it before a long run"
  fi
}

# ensure_cdk: the CDK project's dependencies.
ensure_cdk() {
  [[ -d "$AWS_DIR/node_modules" ]] || (cd "$AWS_DIR" && npm ci --no-audit --no-fund >/dev/null)
}

# client_placement: a pod spec's nodeSelector and tolerations (JSON) that put it on the clients' nodes.
client_placement() {
  jq -cn --arg k "$POOL_LABEL" --arg v "$CLIENT_POOL" '{nodeSelector: {($k): $v},
    tolerations: [{key: $k, operator: "Equal", value: $v, effect: "NoSchedule"}]}'
}

# test_cdk <state dir> <cdk arguments...>: the CDK CLI on the test environment's app, its cloud
# assembly in the environment's state directory, so that environments in two regions can be brought
# up or down at once.
test_cdk() {
  local out="$1/cdk.out"
  shift
  (cd "$AWS_DIR" && npx cdk -a 'npx ts-node test-env/app.ts' --output "$out" --no-notices --no-version-reporting "$@")
}
