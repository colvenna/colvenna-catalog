#!/usr/bin/env bash
# Proves that nothing of the serverless form's environment <env> is left in <region> after
# uninstall.sh --delete-data (and tests/serverless/down.sh): one line per check, names only (never an
# ARN or an account ID); exits 1 when anything remains. The caller sets AWS_PROFILE.
# Usage: [API_IDS=<id> ...] tests/serverless/verify-clean.sh <env> <region>
set -uo pipefail
[[ $# -eq 2 ]] || { echo "usage: tests/serverless/verify-clean.sh <env> <region>" >&2; exit 2; }
[[ -n "${AWS_PROFILE:-}" ]] || { echo "Set AWS_PROFILE: this checks the account it names" >&2; exit 2; }
env="$1" region="$2"
[[ "$env" =~ ^[a-z][a-z0-9]{0,7}$ ]] || { echo "env: a lowercase letter, then lowercase letters and digits" >&2; exit 2; }
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region"
# shellcheck source=../../deploy/lib/stacks.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../deploy/lib/stacks.sh"
COLVENNA_ENV="$env" COLVENNA_REGION="$region"
stacks="$(stack_id "")" names="colvenna-catalog-$env" left=0
mapfile -t stack_names < <(environment_stacks)

check() {
  local what="$1" found
  found="$(tr '\t' '\n' <<<"$2" | sed '/^$/d;/^None$/d')"
  if [[ -z "$found" ]]; then
    echo "ok    $what: none"
  else
    echo "LEFT  $what: $(sed -E 's/[0-9]{12}/<ACCOUNT_ID>/g' <<<"$found" | tr '\n' ' ')"
    left=1
  fi
}

# The tagging index lags behind deletions; an entry counts only while its resource still exists.
# The install's resources carry Environment=<env>; the live-test driver's, Environment=test-<env>.
tagged="$(aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=colvenna-catalog \
  "Key=Environment,Values=$env,test-$env" --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d')"
existing="" stale=0
for arn in $tagged; do
  case "$arn" in
    arn:aws:ec2:*:vpc-endpoint/*)
      # shellcheck disable=SC2016  # a JMESPath literal, for aws
      if [[ -n "$(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "${arn##*/}" --query 'VpcEndpoints[?State!=`deleted`].VpcEndpointId' \
        --output text 2>/dev/null)" ]]; then existing+="$arn "; else stale=$((stale + 1)); fi ;;
    arn:aws:ec2:*:vpc-flow-log/*)
      if [[ "$(aws ec2 describe-flow-logs --flow-log-ids "${arn##*/}" --query 'length(FlowLogs)' --output text 2>/dev/null)" != 0 ]]; then
        existing+="$arn "
      else
        stale=$((stale + 1))
      fi ;;
    *) existing+="$arn " ;;
  esac
done
check "tagged resources (Project=colvenna-catalog, Environment=$env or test-$env)" "$(sed -E 's#^arn:[^:]*:([^:]*):[^:]*:[^:]*:#\1 #' <<<"${existing// /$'\n'}")"
((stale == 0)) || echo "note  the tagging index still lists $stale VPC endpoint(s) or flow log(s) that no longer exist"
check "CloudFormation stacks" "$(aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName, '$stacks') && StackStatus!='DELETE_COMPLETE'].StackName" --output text)"
functions="$(aws lambda list-functions --query "Functions[?starts_with(FunctionName, '$names-')].FunctionName" --output text)"
check "Lambda functions" "$functions"
check "Lambda function URLs (never made)" "$(for f in $functions; do
  aws lambda list-function-url-configs --function-name "$f" --query 'FunctionUrlConfigs[].FunctionUrl' --output text; done)"
check "REST APIs" "$(aws apigateway get-rest-apis --query "items[?starts_with(name, '$names-')].name" --output text)"
# The purge function's schedule (EventBridge Scheduler schedules carry no tags) and its alarm (#69).
check "EventBridge Scheduler schedules" "$(aws scheduler list-schedules --name-prefix "$names-" --query 'Schedules[].Name' --output text)"
check "CloudWatch alarms" "$(aws cloudwatch describe-alarms --alarm-name-prefix "$names-" \
  --query 'MetricAlarms[].AlarmName' --output text)"
check "HTTP APIs (never made)" "$(aws apigatewayv2 get-apis --query "Items[?starts_with(Name, '$names')].Name" --output text)"
check "Cognito identity pools" "$(aws cognito-identity list-identity-pools --max-results 60 \
  --query "IdentityPools[?IdentityPoolName=='colvenna_catalog_$env'].IdentityPoolName" --output text)"
check "IAM roles" "$(aws iam list-roles --query "Roles[?starts_with(RoleName, '$names-') || starts_with(RoleName, '$stacks')].RoleName" --output text)"
check "secrets" "$(aws secretsmanager list-secrets --include-planned-deletion \
  --query "SecretList[?starts_with(Name, '$names/')].Name" --output text)"
check "RDS instances" "$(aws rds describe-db-instances \
  --query "DBInstances[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBInstanceIdentifier" --output text)"
check "RDS snapshots" "$(aws rds describe-db-snapshots \
  --query "DBSnapshots[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBSnapshotIdentifier" --output text)"
check "RDS automated backups" "$(aws rds describe-db-instance-automated-backups \
  --query "DBInstanceAutomatedBackups[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBInstanceIdentifier" --output text)"
check "VPCs" "$(aws ec2 describe-vpcs --filters "Name=tag:Environment,Values=$env" Name=tag:Project,Values=colvenna-catalog \
  --query 'Vpcs[].VpcId' --output text)"
# shellcheck disable=SC2016  # a JMESPath literal, for aws
check "VPC endpoints" "$(aws ec2 describe-vpc-endpoints --filters "Name=tag:Environment,Values=$env" \
  --query 'VpcEndpoints[?State!=`deleted`].VpcEndpointId' --output text)"
groups="$(aws ec2 describe-security-groups --filters "Name=tag:Environment,Values=$env" Name=tag:Project,Values=colvenna-catalog \
  --query 'SecurityGroups[].GroupId' --output text)"
check "security groups" "$groups"
check "network interfaces of those groups" "$(for g in $groups; do aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=$g" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text; done)"
# shellcheck disable=SC2016  # JMESPath literals, for aws
check "NAT gateways, internet gateways, Elastic IPs tagged so (never made)" "$(
  aws ec2 describe-nat-gateways --filter "Name=tag:Environment,Values=$env" --query 'NatGateways[?State!=`deleted`].NatGatewayId' --output text
  aws ec2 describe-internet-gateways --filters "Name=tag:Environment,Values=$env" --query 'InternetGateways[].InternetGatewayId' --output text
  aws ec2 describe-addresses --filters "Name=tag:Environment,Values=$env" --query 'Addresses[].AllocationId' --output text)"
check "ECR repositories" "$(aws ecr describe-repositories --query "repositories[?starts_with(repositoryName, '$names/')].repositoryName" --output text)"
check "S3 buckets" "$(aws s3api list-buckets --query "Buckets[?starts_with(Name, 'colvennacatalog$env')].Name" --output text)"
# API Gateway makes each API's execution log group itself: API_IDS names the environment's APIs (their
# IDs, recorded before the uninstall), whose groups must be gone too. The functions CloudFormation
# named after the stacks, their names cut short as it sees fit, are found as uninstall.sh finds them
# (function_log_groups), and not another environment's whose name starts with this one's.
read -ra apis <<<"${API_IDS:-}"
prefixes=("/aws/lambda/$names-" "/aws/apigateway/$names-" "/aws/rds/instance/colvennacatalog$env" "$stacks")
for api in "${apis[@]}"; do
  prefixes+=("API-Gateway-Execution-Logs_$api/")
done
check "log groups" "$(for prefix in "${prefixes[@]}"; do
  aws logs describe-log-groups --log-group-name-prefix "$prefix" --query 'logGroups[].logGroupName' --output text; done
  function_log_groups "${stack_names[@]}")"
exit "$left"
