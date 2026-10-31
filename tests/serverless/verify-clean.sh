#!/usr/bin/env bash
# Proves that nothing of the serverless form's environment <env> is left in <region> after
# uninstall.sh --delete-data (and tests/serverless/down.sh): one line per check, names only (never an
# ARN or an account ID); exits 1 when anything remains, or a listing fails. While all that remains is
# what RDS is still deleting (below), it reads the account again, and says how long it waited. The
# caller sets AWS_PROFILE.
# Usage: [API_IDS=<id> ...] [RDS_DELETING_WAIT=<s>] [RDS_DELETING_SECONDS=<s>] tests/serverless/verify-clean.sh <env> <region>
set -uo pipefail
[[ $# -eq 2 ]] || { echo "usage: tests/serverless/verify-clean.sh <env> <region>" >&2; exit 2; }
[[ -n "${AWS_PROFILE:-}" ]] || { echo "Set AWS_PROFILE: this checks the account it names" >&2; exit 2; }
env="$1" region="$2"
[[ "$env" =~ ^[a-z][a-z0-9]{0,7}$ ]] || { echo "env: a lowercase letter, then lowercase letters and digits" >&2; exit 2; }
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region"
# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/stacks.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../deploy/lib/stacks.sh"
COLVENNA_ENV="$env" COLVENNA_REGION="$region"
stacks="$(stack_id "")" names="colvenna-catalog-$env"
mapfile -t stack_names < <(environment_stacks)

# What RDS is still deleting (#59): an instance deleted without retaining its automated backups loses
# them and their automated snapshots, but those were still listed after uninstall.sh's
# `aws rds wait db-instance-deleted` had returned, and gone some minutes later. While they, and the
# tagging index's entries for those snapshots, are all that is left, the account is read again every
# RDS_DELETING_WAIT seconds, for RDS_DELETING_SECONDS at most (no wait that would end past it). Anything
# else left is reported at once: a manual snapshot, or an automated one of an instance that still
# exists, is never waited for. An automated backup of an instance that no longer exists is waited for
# whatever its status, none of those RDS documents (active, retained, creating) being for the backups it
# is deleting: one it retains is reported when the wait ends.
RDS_DELETING_WAIT="${RDS_DELETING_WAIT:-30}"
RDS_DELETING_SECONDS="${RDS_DELETING_SECONDS:-1200}"
[[ "$RDS_DELETING_WAIT" =~ ^(0|[1-9][0-9]*)$ && "$RDS_DELETING_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] \
  || { echo "RDS_DELETING_WAIT and RDS_DELETING_SECONDS: whole seconds" >&2; exit 2; }

# listing <aws arguments>...: what aws answers, and UNREADABLE when it fails, which no check takes for
# nothing left: a listing that failed, while waiting say, must not pass for one that found nothing. A
# check over what an UNREADABLE listing lists (the functions' URLs, the groups' interfaces) is UNREADABLE
# too. Not listings: the reads of what the tagging index lists, and function_log_groups' (stacks.sh).
UNREADABLE="(unreadable)"
listing() {
  aws "$@" || echo "$UNREADABLE"
}

# check <what> <found> [<deleting>]: one line of the report, ok or LEFT; <deleting>, a line each, are
# those found that RDS is still deleting (above). Sets left when anything is found, other_left when
# anything else is; names what is waited for in waited_for.
check() {
  local what="$1" found deleting named
  found="$(tr '\t' '\n' <<<"$2" | sed '/^$/d;/^None$/d')"
  deleting="$(sed '/^$/d' <<<"${3:-}")"
  if [[ -z "$found" ]]; then
    report+=("ok    $what: none")
    return
  fi
  named="$(sed -E 's/[0-9]{12}/<ACCOUNT_ID>/g' <<<"$found" | tr '\n' ' ')"
  report+=("LEFT  $what: $named")
  left=1
  if grep -qvxF -f <(printf '%s\n' "$deleting") <<<"$found"; then
    other_left=1
  else
    waited_for+=("$what: ${named% }")
  fi
}

# arn_names <arn>...: "<service> <resource>" for each ARN, a line each, as the report names them.
arn_names() {
  sed -E 's#^arn:[^:]*:([^:]*):[^:]*:[^:]*:#\1 #' <<<"${1// /$'\n'}"
}

# instance_exists <instance>: whether the RDS instance is there; one that cannot be read counts as there.
instance_exists() {
  local err status=0
  err="$(mktemp)"
  if ! aws rds describe-db-instances --db-instance-identifier "$1" --query 'DBInstances[0].DBInstanceIdentifier' \
    --output text >/dev/null 2>"$err" && grep -q DBInstanceNotFound "$err"; then
    status=1
  fi
  rm -f "$err"
  return "$status"
}

# snapshot_state <snapshot>: gone (RDS has no such snapshot), deleting (an automated snapshot of an
# instance that no longer exists, above), or there (any other, or one that cannot be read).
snapshot_state() {
  local err out type instance
  err="$(mktemp)"
  if out="$(aws rds describe-db-snapshots --db-snapshot-identifier "$1" \
    --query 'DBSnapshots[0].[SnapshotType,DBInstanceIdentifier]' --output text 2>"$err")"; then
    read -r type instance <<<"$out"
    if [[ "$type" == automated ]] && ! instance_exists "$instance"; then echo deleting; else echo there; fi
  elif grep -q DBSnapshotNotFound "$err"; then
    echo gone
  else
    echo there
  fi
  rm -f "$err"
}

# API Gateway makes each API's execution log group itself: API_IDS names the environment's APIs (their
# IDs, recorded before the uninstall), whose groups must be gone too. The functions CloudFormation
# named after the stacks, their names cut short as it sees fit, are found as uninstall.sh finds them
# (function_log_groups), and not another environment's whose name starts with this one's.
read -ra apis <<<"${API_IDS:-}"
prefixes=("/aws/lambda/$names-" "/aws/apigateway/$names-" "/aws/rds/instance/colvennacatalog$env" "$stacks")
for api in "${apis[@]}"; do
  prefixes+=("API-Gateway-Execution-Logs_$api/")
done

# survey: every check, once, into report (left, other_left and waited_for as check sets them).
survey() {
  report=() left=0 other_left=0 waited_for=()
  local tagged arn existing="" deleting="" stale=0 functions groups found id
  # The tagging index lags behind deletions; an entry counts only while its resource still exists.
  # The install's resources carry Environment=<env>; the live-test driver's, Environment=test-<env>.
  tagged="$(listing resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=colvenna-catalog \
    "Key=Environment,Values=$env,test-$env" --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d')"
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
      arn:aws:rds:*:snapshot:*)
        case "$(snapshot_state "${arn#*:snapshot:}")" in
          gone) stale=$((stale + 1)) ;;
          deleting) existing+="$arn " deleting+="$arn " ;;
          *) existing+="$arn " ;;
        esac ;;
      *) existing+="$arn " ;;
    esac
  done
  check "tagged resources (Project=colvenna-catalog, Environment=$env or test-$env)" "$(arn_names "$existing")" "$(arn_names "$deleting")"
  ((stale == 0)) || report+=("note  the tagging index still lists $stale VPC endpoint(s), flow log(s) or RDS snapshot(s) that no longer exist")
  check "CloudFormation stacks" "$(listing cloudformation list-stacks \
    --query "StackSummaries[?starts_with(StackName, '$stacks') && StackStatus!='DELETE_COMPLETE'].StackName" --output text)"
  functions="$(listing lambda list-functions --query "Functions[?starts_with(FunctionName, '$names-')].FunctionName" --output text)"
  check "Lambda functions" "$functions"
  check "Lambda function URLs (never made)" "$(for f in $functions; do
    [[ "$f" != "$UNREADABLE" ]] || { echo "$UNREADABLE"; continue; }
    listing lambda list-function-url-configs --function-name "$f" --query 'FunctionUrlConfigs[].FunctionUrl' --output text; done)"
  check "REST APIs" "$(listing apigateway get-rest-apis --query "items[?starts_with(name, '$names-')].name" --output text)"
  # The purge function's schedule (EventBridge Scheduler schedules carry no tags) and its alarm (#69).
  check "EventBridge Scheduler schedules" "$(listing scheduler list-schedules --name-prefix "$names-" --query 'Schedules[].Name' --output text)"
  check "CloudWatch alarms" "$(listing cloudwatch describe-alarms --alarm-name-prefix "$names-" \
    --query 'MetricAlarms[].AlarmName' --output text)"
  check "HTTP APIs (never made)" "$(listing apigatewayv2 get-apis --query "Items[?starts_with(Name, '$names')].Name" --output text)"
  check "Cognito identity pools" "$(listing cognito-identity list-identity-pools --max-results 60 \
    --query "IdentityPools[?IdentityPoolName=='colvenna_catalog_$env'].IdentityPoolName" --output text)"
  check "IAM roles" "$(listing iam list-roles --query "Roles[?starts_with(RoleName, '$names-') || starts_with(RoleName, '$stacks')].RoleName" --output text)"
  check "secrets" "$(listing secretsmanager list-secrets --include-planned-deletion \
    --query "SecretList[?starts_with(Name, '$names/')].Name" --output text)"
  check "RDS instances" "$(listing rds describe-db-instances \
    --query "DBInstances[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBInstanceIdentifier" --output text)"
  # A snapshot gone since it was listed is on its way out too: the next read no longer lists it.
  found="$(listing rds describe-db-snapshots \
    --query "DBSnapshots[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBSnapshotIdentifier" --output text)"
  deleting=""
  for id in $found; do
    [[ "$id" != "$UNREADABLE" ]] || continue
    [[ "$(snapshot_state "$id")" == there ]] || deleting+="$id"$'\n'
  done
  check "RDS snapshots" "$found" "$deleting"
  found="$(listing rds describe-db-instance-automated-backups \
    --query "DBInstanceAutomatedBackups[?starts_with(DBInstanceIdentifier, 'colvennacatalog$env')].DBInstanceIdentifier" --output text)"
  deleting=""
  for id in $found; do
    [[ "$id" != "$UNREADABLE" ]] || continue
    instance_exists "$id" || deleting+="$id"$'\n'
  done
  check "RDS automated backups" "$found" "$deleting"
  check "VPCs" "$(listing ec2 describe-vpcs --filters "Name=tag:Environment,Values=$env" Name=tag:Project,Values=colvenna-catalog \
    --query 'Vpcs[].VpcId' --output text)"
  # shellcheck disable=SC2016  # a JMESPath literal, for aws
  check "VPC endpoints" "$(listing ec2 describe-vpc-endpoints --filters "Name=tag:Environment,Values=$env" \
    --query 'VpcEndpoints[?State!=`deleted`].VpcEndpointId' --output text)"
  groups="$(listing ec2 describe-security-groups --filters "Name=tag:Environment,Values=$env" Name=tag:Project,Values=colvenna-catalog \
    --query 'SecurityGroups[].GroupId' --output text)"
  check "security groups" "$groups"
  check "network interfaces of those groups" "$(for g in $groups; do
    [[ "$g" != "$UNREADABLE" ]] || { echo "$UNREADABLE"; continue; }
    listing ec2 describe-network-interfaces --filters "Name=group-id,Values=$g" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text; done)"
  # shellcheck disable=SC2016  # JMESPath literals, for aws
  check "NAT gateways, internet gateways, Elastic IPs tagged so (never made)" "$(
    listing ec2 describe-nat-gateways --filter "Name=tag:Environment,Values=$env" --query 'NatGateways[?State!=`deleted`].NatGatewayId' --output text
    listing ec2 describe-internet-gateways --filters "Name=tag:Environment,Values=$env" --query 'InternetGateways[].InternetGatewayId' --output text
    listing ec2 describe-addresses --filters "Name=tag:Environment,Values=$env" --query 'Addresses[].AllocationId' --output text)"
  check "ECR repositories" "$(listing ecr describe-repositories --query "repositories[?starts_with(repositoryName, '$names/')].repositoryName" --output text)"
  check "S3 buckets" "$(listing s3api list-buckets --query "Buckets[?starts_with(Name, 'colvennacatalog$env')].Name" --output text)"
  check "log groups" "$(for prefix in "${prefixes[@]}"; do
    listing logs describe-log-groups --log-group-name-prefix "$prefix" --query 'logGroups[].logGroupName' --output text; done
    function_log_groups "${stack_names[@]}")"
}

survey
began="$SECONDS" reads=0
while ((left && !other_left && SECONDS - began + RDS_DELETING_WAIT <= RDS_DELETING_SECONDS)); do
  if ((reads == 0)); then
    echo "wait  left: only what RDS is still deleting, the automated snapshots and backups of instances that no longer" \
      "exist ($(printf '%s; ' "${waited_for[@]}" | sed 's/; $//')); reading again every $RDS_DELETING_WAIT s," \
      "the last wait ending no later than $RDS_DELETING_SECONDS s after the first read"
  fi
  sleep "$RDS_DELETING_WAIT"
  survey
  reads=$((reads + 1))
done
printf '%s\n' "${report[@]}"
if ((reads > 0)); then
  waited="note  waited $((SECONDS - began)) s ($reads more read(s)) for what RDS is still deleting"
  if ((!left)); then
    echo "$waited: it is gone"
  elif ((!other_left)); then
    echo "$waited: it is still there"
  else
    echo "$waited, until something else was left"
  fi
fi
exit "$left"
