#!/usr/bin/env bash
# Takes down the serverless live test's driver (tests/serverless/run.sh) of <env> in <region>, and
# checks nothing of it is left: the stack ColvennaCatalogTest<Env>Driver (only if tagged
# Purpose=test-environment), the log group Lambda may write again after the stack removed it, and the
# driver images (test-driver-*) in the environment's repository. Run it before uninstall.sh: the
# driver's network interfaces are in the catalog's subnets. Evidence goes to
# .state/test-driver-<env>-<region>/evidence/down-<time>/.
# Usage: AWS_PROFILE=<profile> tests/serverless/down.sh <env> <region>
set -euo pipefail
[[ -n "${AWS_PROFILE:-}" ]] || { echo "Set AWS_PROFILE: this script never uses a default profile" >&2; exit 2; }
[[ $# -eq 2 ]] || { echo "usage: tests/serverless/down.sh <env> <region>" >&2; exit 2; }
env="$1" region="$2"
[[ "$env" =~ ^[a-z][a-z0-9]{0,7}$ ]] || { echo "env: a lowercase letter, then lowercase letters and digits, 8 at most" >&2; exit 2; }
[[ "$region" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$region" != cn-* ]] || { echo "region: a standard AWS region" >&2; exit 2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$here" rev-parse --show-toplevel)"
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region"
Env="$(tr '[:lower:]' '[:upper:]' <<<"${env:0:1}")${env:1}"
state="$REPO/.state/test-driver-$env-$region"
evidence="$state/evidence/down-$(date -u +%Y%m%dT%H%M%SZ)"
stack="ColvennaCatalogTest${Env}Driver"
name="colvenna-catalog-$env-test-driver"
repository_name="colvenna-catalog-$env/colvenna-catalog"
mkdir -p "$evidence"
account="$(aws sts get-caller-identity --query Account --output text)"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT
if purpose="$(aws cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Tags[?Key=='Purpose'].Value | [0]" --output text 2>"$err")"; then
  [[ "$purpose" == test-environment ]] || { echo "$stack is tagged Purpose=$purpose, not test-environment: refusing" >&2; exit 1; }
  [[ -d "$REPO/deploy/aws/node_modules" ]] || (cd "$REPO/deploy/aws" && npm ci --no-audit --no-fund >/dev/null)
  # destroy needs the app to synthesize, not to match what was deployed: placeholders for its inputs.
  (cd "$REPO/deploy/aws" && npx cdk -a 'npx ts-node test-env/driver-app.ts' destroy "$stack" --force \
    --no-notices --no-version-reporting --output "$state/cdk.out" \
    -c "env=$env" -c "account=$account" -c "region=$region" -c "sourceSha=$(printf '0%.0s' {1..40})" \
    -c vpcId=vpc-00000000 -c subnetIds=subnet-00000000,subnet-00000001 -c "subnetZones=${region}a,${region}b" \
    -c "repositoryName=$repository_name" -c "imageDigest=sha256:$(printf '0%.0s' {1..64})") \
    >"$evidence/destroy.log" 2>&1 || { tail -30 "$evidence/destroy.log" >&2; exit 1; }
elif ! grep -q 'does not exist' "$err"; then
  echo "Cannot read $stack: $(cat "$err")" >&2
  exit 1
fi

# A late log line makes Lambda create the group again after the stack deleted it.
if aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$name" --query 'logGroups[].logGroupName' \
  --output text | grep -qx "/aws/lambda/$name"; then
  aws logs delete-log-group --log-group-name "/aws/lambda/$name"
fi
# The driver images, by tag; the repository itself is the install's (uninstall.sh removes it).
if aws ecr describe-repositories --repository-names "$repository_name" >/dev/null 2>&1; then
  for tag in $(aws ecr describe-images --repository-name "$repository_name" \
    --query 'imageDetails[].imageTags[]' --output text | tr '\t' '\n' | grep '^test-driver-' || true); do
    aws ecr batch-delete-image --repository-name "$repository_name" --image-ids "imageTag=$tag" >/dev/null
  done
fi

# --- nothing left -----------------------------------------------------------------------------------
left=()
aws cloudformation describe-stacks --stack-name "$stack" >/dev/null 2>&1 && left+=("stack $stack")
aws lambda get-function --function-name "$name" >/dev/null 2>&1 && left+=("function $name")
aws iam get-role --role-name "$name" >/dev/null 2>&1 && left+=("role $name")
[[ -z "$(aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$name" --query 'logGroups[].logGroupName' \
  --output text)" ]] || left+=("log group /aws/lambda/$name")
[[ -z "$(aws ec2 describe-security-groups --filters "Name=tag:aws:cloudformation:stack-name,Values=$stack" \
  --query 'SecurityGroups[].GroupId' --output text)" ]] || left+=("security group of $stack")
if aws ecr describe-repositories --repository-names "$repository_name" >/dev/null 2>&1; then
  [[ -z "$(aws ecr describe-images --repository-name "$repository_name" --query 'imageDetails[].imageTags[]' \
    --output text | tr '\t' '\n' | grep '^test-driver-' || true)" ]] || left+=("test-driver-* images")
fi
if ((${#left[@]} > 0)); then
  printf 'left: %s\n' "${left[@]}" | tee "$evidence/RESULT" >&2
  exit 1
fi
echo "$stack in $region is down; no function, role, log group, security group or driver image of it is left" \
  | tee "$evidence/RESULT"
