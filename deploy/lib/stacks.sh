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

# The components of an environment's stacks, both forms'.
STACK_COMPONENTS=(Eks Storage Network Images Serverless)

# environment_stacks: the names of the environment's stacks, both forms', one per line.
environment_stacks() {
  local component
  for component in "${STACK_COMPONENTS[@]}"; do
    stack_id "$component"
  done
}

# CloudFormation names what it generates for a stack's resources (a Lambda function, say)
# <stack>-<logical id>-<suffix>, and cuts a long stack name short in it: ColvennaCatalogSl13Storag-...,
# 25 of the stack's 26 characters, as seen. So a generated name whose first part, up to its first
# hyphen, is a cut of a stack's name at least STACK_CUT_MIN characters long is that stack's too. No
# other environment's stack shares that many first characters with an environment's: its stacks'
# names, ColvennaCatalog<Env><Component>, are the same for 15 characters; then where one environment's
# name (1 to 8 characters, the first a capital here) runs on past the other's, at most 7 characters on,
# a lowercase letter or a digit meets a component's capital: 22 characters at most. The test
# environments' and their drivers' stacks (ColvennaCatalogTest<Name>, ColvennaCatalogTest<Env>Driver)
# part from them by the 20th character, the environment test being reserved (config.sh), and the
# release registry's (ColvennaCatalogRelease, 22 characters) by the 23rd. tests/deploy/test_entrypoints.py
# checks this. A cut shorter could be another environment's, and is never taken.
STACK_CUT_MIN=23

# named_after_stacks <name> <stack>...: whether <name> is one CloudFormation generated for one of the
# stacks: its first part, up to its first hyphen, is the stack's name, or a cut of it at least
# STACK_CUT_MIN characters long. Names as they are: lowercased, two environments' stack names share
# more (a and aserverl, 24 characters).
named_after_stacks() {
  local head="${1%%-*}" stack
  [[ "$1" == "$head-"* ]] || return 1
  for stack in "${@:2}"; do
    [[ "$head" != "$stack" ]] || return 0
    ((${#head} < STACK_CUT_MIN)) || [[ "$stack" != "$head"* ]] || return 0
  done
  return 1
}

# function_log_groups <stack>...: the log groups of the stacks' Lambda functions (/aws/lambda/<function>),
# which Lambda makes as they run and no stack owns. A function CloudFormation named is found by
# named_after_stacks, among the groups under the start the stacks' names and their cuts share (those of
# an environment whose name starts with this one's are there too, and are not taken); one named
# explicitly (colvenna-catalog-<env>-...) is not under it. One per line.
function_log_groups() {
  local prefix="${1:0:STACK_CUT_MIN}" stack listed group
  for stack in "${@:2}"; do
    while [[ "$stack" != "$prefix"* ]]; do
      prefix="${prefix%?}"
    done
  done
  listed="$(aws logs describe-log-groups --region "$COLVENNA_REGION" --log-group-name-prefix "/aws/lambda/$prefix" \
    --query 'logGroups[].logGroupName' --output text)"
  for group in $listed; do
    [[ "$group" == None ]] || ! named_after_stacks "${group#/aws/lambda/}" "$@" || printf '%s\n' "$group"
  done
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

# synth_stack <stack> <cdk arguments...>: the stack synthesized into STATE_DIR/cdk.out, for a look at its
# template (STATE_DIR/cdk.out/<stack>.template.json) before deploy_assembly deploys it.
synth_stack() {
  local stack="$1"
  shift
  (cd "$REPO/deploy/aws" && npx cdk synth "$stack" --quiet --no-notices --no-version-reporting \
    --output "$STATE_DIR/cdk.out" "$@") >>"$EVIDENCE/cdk.log" 2>&1 || { tail -30 "$EVIDENCE/cdk.log" >&2; return 1; }
}

# deploy_assembly <stack>: deploys the stack synth_stack synthesized, as it is (no synthesis again),
# leaving its outputs in STATE_DIR/<stack>.json as deploy_stack does.
deploy_assembly() {
  (cd "$REPO/deploy/aws" && npx cdk deploy "$1" --app "$STATE_DIR/cdk.out" --require-approval never --no-notices \
    --no-version-reporting --outputs-file "$STATE_DIR/$1.json") >>"$EVIDENCE/cdk.log" 2>&1 \
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
