# shellcheck shell=bash
# The CDK stacks of deploy/aws, as the entry points deploy, read and destroy them. Reads COLVENNA_ENV,
# COLVENNA_REGION, REPO, STATE_DIR (the CDK output and the stacks' outputs) and EVIDENCE (cdk.log).

# cdk_dependencies if-missing|exact: the CDK app's packages (deploy/aws/node_modules), from its
# lockfile: if-missing installs them when they are not there; exact (install.sh --release) always
# installs them afresh with npm ci, which installs exactly what package-lock.json pins. git ignores
# node_modules, so the one a checkout of another commit left would otherwise stand in for the release's.
cdk_dependencies() {
  [[ "$1" == exact || ! -d "$REPO/deploy/aws/node_modules" ]] || return 0
  (cd "$REPO/deploy/aws" && npm ci --no-audit --no-fund >/dev/null)
}

# stack_id <component>: ColvennaCatalog<Env><Component>, as deploy/aws/src/app.ts names the stacks.
stack_id() {
  printf 'ColvennaCatalog%s%s%s\n' "$(tr '[:lower:]' '[:upper:]' <<<"${COLVENNA_ENV:0:1}")" "${COLVENNA_ENV:1}" "$1"
}

# stack_status <stack>: the stack's status, or NONE when there is no such stack; fails on any other error.
stack_status() {
  local err out
  err="$(mktemp)"
  if out="$(aws cloudformation describe-stacks --region "$COLVENNA_REGION" --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>"$err")"; then
    printf '%s\n' "$out"
  elif grep -q "does not exist" "$err"; then
    echo NONE
  else
    echo "Cannot read the stack $1: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
}

# deploy_stack <stack> <cdk arguments...>: deploys the stack, leaving its outputs in STATE_DIR/<stack>.json.
deploy_stack() {
  local stack="$1"
  shift
  (cd "$REPO/deploy/aws" && npx cdk deploy "$stack" --require-approval never --no-notices --no-version-reporting \
    --output "$STATE_DIR/cdk.out" --outputs-file "$STATE_DIR/$stack.json" "$@") >>"$EVIDENCE/cdk.log" 2>&1 \
    || { tail -30 "$EVIDENCE/cdk.log" >&2; return 1; }
}

# cfn_output <stack> <key>: an output of a deployed stack, read from CloudFormation; empty when there
# is no such stack or output; fails when CloudFormation cannot be read, which is no proof of either.
cfn_output() {
  local err out
  err="$(mktemp)"
  if out="$(aws cloudformation describe-stacks --region "$COLVENNA_REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text 2>"$err")"; then
    [[ "$out" == None ]] || printf '%s\n' "$out"
  elif ! grep -q "does not exist" "$err"; then
    echo "Cannot read the stack $1: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
}

# stack_output <stack> <key>: one of the outputs deploy_stack left.
stack_output() {
  jq -er --arg s "$1" --arg k "$2" '.[$s][$k]' "$STATE_DIR/$1.json"
}

# destroy_stack <stack> <cdk arguments...>
destroy_stack() {
  (cd "$REPO/deploy/aws" && npx cdk destroy "$1" --force --no-notices --no-version-reporting \
    --output "$STATE_DIR/cdk.out" "${@:2}") >>"$EVIDENCE/cdk.log" 2>&1 || { tail -30 "$EVIDENCE/cdk.log" >&2; return 1; }
}

# stack_resources <stack> <type>: the physical IDs of the stack's resources of that type, one per line.
stack_resources() {
  local out
  out="$(aws cloudformation describe-stack-resources --region "$COLVENNA_REGION" --stack-name "$1" \
    --query "StackResources[?ResourceType=='$2'].PhysicalResourceId" --output text)"
  [[ -z "$out" || "$out" == None ]] || tr '\t' '\n' <<<"$out"
}
