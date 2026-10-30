#!/usr/bin/env bash
# Uninstalls Colvenna Catalog, keeping its data unless asked otherwise. The serverless form
# (COLVENNA_FORM=serverless, deploy/lib/serverless.sh): the serverless and images stacks, then the
# network stack unless a kept database lives in it. The EKS form: the internal
# NLB (waiting until its load balancer is gone), the Helm release, the catalog's Secrets but its CA,
# and the EKS stack (its roles, database user secret and image repository). The storage stack (the
# bucket and the database, if install.sh made them), the CA and the namespace stay, so a later install
# with COLVENNA_BUCKET and COLVENNA_DB=existing finds every table again. --delete-data also deletes the
# database (no final snapshot), empties and deletes the buckets, and removes the storage stack and the
# CA, after you type the environment's name, and then prints how to remove the catalog's part of what
# the install was given (an existing database's and bucket's), which it never deletes. Run it again after a failure or an interruption: what is
# already gone is skipped. Exit status: 0 done; 1 a step failed (it says which); 2 the arguments or the
# configuration are wrong, or not confirmed; 3 a tool is missing or too old, the credentials are of
# another account, or --delete-data while an instance the install did not make (a restored one) uses its
# database's subnet group or security group, or the VPC it made.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=deploy/lib/config.sh
source "$REPO/deploy/lib/config.sh"
# shellcheck source=deploy/lib/preflight.sh
source "$REPO/deploy/lib/preflight.sh"
# shellcheck source=deploy/lib/catalog.sh
source "$REPO/deploy/lib/catalog.sh"
# shellcheck source=deploy/lib/stacks.sh
source "$REPO/deploy/lib/stacks.sh"
# shellcheck source=deploy/lib/steps.sh
source "$REPO/deploy/lib/steps.sh"
# shellcheck source=deploy/lib/serverless.sh
source "$REPO/deploy/lib/serverless.sh"

usage() {
  cat <<'USAGE'
Usage: uninstall.sh --config <file> [--yes] [--delete-data]

  --config <file>  the install's configuration
  --yes            do not ask before removing the catalog (the data is kept)
  --delete-data    also delete the database and the buckets install.sh made; asks for the
                   environment's name even with --yes
  --help           this text
USAGE
}

parse_args() {
  CONFIG_FILE="" ASSUME_YES="" DELETE_DATA=""
  while (($#)); do
    case "$1" in
      --config) CONFIG_FILE="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
      --yes) ASSUME_YES=1; shift ;;
      --delete-data) DELETE_DATA=1; shift ;;
      --help) usage; exit 0 ;;
      *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
  [[ -n "$CONFIG_FILE" ]] || { echo "--config is required" >&2; usage >&2; exit 2; }
}

# uninstall_data_clause: what becomes of the data, for the confirmation's first line.
uninstall_data_clause() {
  [[ "${DELETE_DATA:-}" == 1 ]] && echo "; --delete-data: the data goes too" || echo "; the data is kept"
}

confirm_uninstall() {
  if [[ "$COLVENNA_FORM" == serverless ]]; then
    echo "Uninstalling Colvenna Catalog (serverless, environment $COLVENNA_ENV) from $COLVENNA_REGION ($COLVENNA_ACCOUNT)$(uninstall_data_clause)"
  else
    echo "Uninstalling Colvenna Catalog from $COLVENNA_EKS_CLUSTER/$COLVENNA_NAMESPACE in $COLVENNA_REGION ($COLVENNA_ACCOUNT)$(uninstall_data_clause)"
  fi
  [[ "${ASSUME_YES:-}" != 1 ]] || return 0
  [[ -t 0 ]] || { echo "Not confirmed: no terminal to ask on; rerun with --yes" >&2; exit 2; }
  local answer
  read -r -p "Type the environment name ($COLVENNA_ENV) to go on: " answer
  [[ "$answer" == "$COLVENNA_ENV" ]] || { echo "Not confirmed" >&2; exit 2; }
}

# confirm_delete_data: the environment's name, typed (or piped), whatever --yes says.
confirm_delete_data() {
  local answer=""
  echo "--delete-data deletes the catalog's database (no final snapshot) and its buckets in $COLVENNA_REGION ($COLVENNA_ACCOUNT)."
  read -r -p "Type the environment name ($COLVENNA_ENV) to delete its data: " answer || true
  [[ "$answer" == "$COLVENNA_ENV" ]] || { echo "Not confirmed: nothing was deleted" >&2; exit 2; }
}

# cluster_status: the EKS cluster's status, or NONE when there is no such cluster; fails on any other error.
cluster_status() {
  local err out
  err="$(mktemp)"
  if out="$(aws eks describe-cluster --name "$COLVENNA_EKS_CLUSTER" --region "$COLVENNA_REGION" \
    --query cluster.status --output text 2>"$err")"; then
    printf '%s\n' "$out"
  elif grep -q ResourceNotFoundException "$err"; then
    echo NONE
  else
    echo "Cannot read the cluster $COLVENNA_EKS_CLUSTER: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
}

# delete_database <instance>: deletes the RDS instance without a final snapshot and waits until it is
# gone; one already deleting is waited for, one already gone skipped.
delete_database() {
  local err status
  err="$(mktemp)"
  if ! status="$(aws rds describe-db-instances --region "$COLVENNA_REGION" --db-instance-identifier "$1" \
    --query 'DBInstances[0].DBInstanceStatus' --output text 2>"$err")"; then
    if grep -q DBInstanceNotFound "$err"; then
      rm -f "$err"
      return 0
    fi
    echo "Cannot read the database $1: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  if [[ "$status" != deleting ]]; then
    aws rds modify-db-instance --region "$COLVENNA_REGION" --db-instance-identifier "$1" \
      --no-deletion-protection --apply-immediately >/dev/null
    aws rds delete-db-instance --region "$COLVENNA_REGION" --db-instance-identifier "$1" \
      --skip-final-snapshot --delete-automated-backups >/dev/null
  fi
  aws rds wait db-instance-deleted --region "$COLVENNA_REGION" --db-instance-identifier "$1"
}

# delete_bucket's bound: a bucket found not empty again when it is deleted, objects having arrived since
# it was emptied (the access log bucket, which S3 may go on delivering logs to: stop_access_logs), is
# emptied again BUCKET_DELETE_WAIT seconds later, BUCKET_DELETE_ATTEMPTS times in all at most: about 10
# minutes.
BUCKET_DELETE_ATTEMPTS=20
BUCKET_DELETE_WAIT=30

# delete_bucket <bucket>: empties and deletes the bucket, emptying it again while objects arrive (above);
# one already gone is skipped. Every failure returns 1, saying why, so that a caller may go on with
# other buckets.
delete_bucket() {
  local err attempt
  err="$(mktemp)"
  if ! aws s3api head-bucket --bucket "$1" --region "$COLVENNA_REGION" >/dev/null 2>"$err"; then
    if grep -qE '\(404\)|Not Found' "$err"; then
      rm -f "$err"
      return 0
    fi
    echo "Cannot read the bucket $1: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  for ((attempt = 1; ; attempt++)); do
    if ! aws s3 rm "s3://$1" --recursive --region "$COLVENNA_REGION" --only-show-errors 2>"$err"; then
      echo "Cannot empty the bucket $1: $(tr '\n' ' ' <"$err")" >&2
      rm -f "$err"
      return 1
    fi
    if aws s3api delete-bucket --bucket "$1" --region "$COLVENNA_REGION" 2>"$err"; then
      rm -f "$err"
      return 0
    fi
    if ! grep -q BucketNotEmpty "$err"; then
      echo "Cannot delete the bucket $1: $(tr '\n' ' ' <"$err")" >&2
      rm -f "$err"
      return 1
    fi
    if ((attempt >= BUCKET_DELETE_ATTEMPTS)); then
      echo "The bucket $1 is still not empty after $attempt attempts over $(((attempt - 1) * BUCKET_DELETE_WAIT)) seconds:" \
        "objects go on arriving after it is emptied (S3 delivers access logs late, most within a few hours)" >&2
      rm -f "$err"
      return 1
    fi
    echo "The bucket $1 is not empty again, objects having arrived since it was emptied; emptying it again in" \
      "$BUCKET_DELETE_WAIT seconds (attempt $((attempt + 1)) of $BUCKET_DELETE_ATTEMPTS)"
    sleep "$BUCKET_DELETE_WAIT"
  done
}

# stop_access_logs <bucket>...: for --delete-data, before any of the buckets is emptied: each bucket that
# logs its requests to another of them stops, and that one loses its policy, under which S3's logging
# service writes the logs (its bucket-owner-enforced ownership leaves the service no ACL to write by).
# S3 delivers the logs it recorded late (most within a few hours), and a change to a bucket's logging
# takes time to take effect: without this, the access log bucket took logs again as soon as it was
# emptied (#14). Prints the buckets that took the logs, one per line, which go last.
stop_access_logs() {
  local bucket target err
  err="$(mktemp)"
  for bucket in "$@"; do
    if ! target="$(aws s3api get-bucket-logging --bucket "$bucket" --region "$COLVENNA_REGION" \
      --query LoggingEnabled.TargetBucket --output text 2>"$err")"; then
      if grep -q NoSuchBucket "$err"; then
        continue
      fi
      echo "Cannot read the logging of the bucket $bucket: $(tr '\n' ' ' <"$err")" >&2
      rm -f "$err"
      return 1
    fi
    [[ -n "$target" && "$target" != None ]] || continue
    aws s3api put-bucket-logging --bucket "$bucket" --region "$COLVENNA_REGION" --bucket-logging-status '{}'
    # Only a bucket of the stack's own loses its policy.
    if printf '%s\n' "$@" | grep -qxF "$target"; then
      aws s3api delete-bucket-policy --bucket "$target" --region "$COLVENNA_REGION"
      printf '%s\n' "$target"
    fi
  done
  rm -f "$err"
}

# delete_log_groups <prefix>: deletes every log group whose name starts so. For the groups no stack owns:
# those RDS exports to and those the serverless form's functions and API write as they run.
delete_log_groups() {
  local listed group
  listed="$(aws logs describe-log-groups --region "$COLVENNA_REGION" --log-group-name-prefix "$1" \
    --query 'logGroups[].logGroupName' --output text)"
  # Each page of the listing on a line of its own.
  for group in $listed; do
    [[ "$group" == None ]] || aws logs delete-log-group --region "$COLVENNA_REGION" --log-group-name "$group"
  done
}

# assert_no_foreign_databases: for --delete-data, before anything changes: no RDS instance but the
# Storage stack's own uses the stack's database subnet group or security group, or, in the serverless
# form with COLVENNA_NETWORK=create, lives in the VPC the install made. One that does (a restored
# instance, user manual 6.2) is not the install's to delete, and the stacks' subnet group, security group
# and VPC cannot be deleted under it: refused, naming it.
assert_no_foreign_databases() {
  local storage network security_groups="" subnets="" mine="" vpc="" foreign
  storage="$(stack_id Storage)" network="$(stack_id Network)"
  if [[ "$(stack_status "$storage")" != NONE ]]; then
    subnets="$(stack_resources "$storage" AWS::RDS::DBSubnetGroup)"
    security_groups="$(stack_resources "$storage" AWS::EC2::SecurityGroup)"
    mine="$(stack_resources "$storage" AWS::RDS::DBInstance)"
  fi
  [[ "$COLVENNA_FORM" != serverless || "$COLVENNA_NETWORK" != create ]] || vpc="$(cfn_output "$network" VpcId)"
  [[ -n "$subnets$security_groups$vpc" ]] || return 0
  # RDS keeps subnet group names in lower case.
  foreign="$(aws rds describe-db-instances --region "$COLVENNA_REGION" --output json | jq -r --arg subnets "$subnets" \
    --arg groups "$security_groups" --arg vpc "$vpc" --arg mine "$mine" '
    ($subnets | ascii_downcase | split("\n")) as $s | ($groups | split("\n")) as $g | ($mine | split("\n")) as $m
    | .DBInstances[] | select(.DBInstanceIdentifier as $i | $m | index($i) | not)
    | select(((.DBSubnetGroup.DBSubnetGroupName // "" | ascii_downcase) as $n | $n != "" and ($s | index($n)))
        or any(.VpcSecurityGroups[]?; .VpcSecurityGroupId as $id | $g | index($id))
        or ($vpc != "" and .DBSubnetGroup.VpcId == $vpc))
    | .DBInstanceIdentifier' | paste -sd, -)"
  [[ -z "$foreign" ]] || refuse "--delete-data: $foreign, which the install did not make (a restored instance, say), uses the" \
    "database subnet group or security group of $storage, or the VPC the install made, which cannot be deleted under it;" \
    "uninstall.sh never deletes what it did not make: delete it first, with its log group" \
    "(/aws/rds/instance/<instance>/postgresql), then run again; nothing was changed"
}

# remove_storage <storage stack> <placeholders...>: --delete-data: the database (no final snapshot) and
# the buckets the storage stack made, the stack, and the database's exported logs, which it never owned
# (its function's, CDK's log retention one, go with delete_function_log_groups). The buckets stop their
# access logs first, so that S3 has the database's deletion to settle in, and the access log bucket goes
# last. A bucket that cannot be deleted does not keep the others from it; the stack then stays, so that a
# run again finds them.
remove_storage() {
  local storage="$1" instances buckets instance bucket targets ordered=() failed=()
  shift
  [[ "$(stack_status "$storage")" != NONE ]] || return 0
  step "$storage and its data"
  instances="$(stack_resources "$storage" AWS::RDS::DBInstance)"
  buckets="$(stack_resources "$storage" AWS::S3::Bucket)"
  # shellcheck disable=SC2086  # the stack's buckets, one word each
  targets="$(stop_access_logs $buckets)"
  for instance in $instances; do
    delete_database "$instance"
  done
  for bucket in $buckets; do
    if ! grep -qxF "$bucket" <<<"$targets"; then
      ordered+=("$bucket")
    fi
  done
  for bucket in $buckets; do
    if grep -qxF "$bucket" <<<"$targets"; then
      ordered+=("$bucket")
    fi
  done
  for bucket in "${ordered[@]}"; do
    delete_bucket "$bucket" || failed+=("$bucket")
  done
  if ((${#failed[@]})); then
    echo "Not deleted: the bucket(s) ${failed[*]}, as said above; $storage stays, so that a run again finds them" >&2
    return 1
  fi
  destroy_stack "$storage" "$@" -c stack=storage -c createBucket=true -c createDatabase=false
  for instance in $instances; do
    delete_log_groups "/aws/rds/instance/$instance/"
  done
}

# delete_function_log_groups: the log groups of the functions of the environment's stacks that are
# gone, which Lambda makes as they run and no stack owns: CDK's (the storage stack's log retention
# function), which CloudFormation named after their stack, cutting a long name short
# (ColvennaCatalogSl13Storag-...: function_log_groups). Those of a stack that stays stay with it, a
# cut that could be either stack's too (ColvennaCatalogAbcdefghS-, of Storage or Serverless). Found by
# their names, not the stacks' resources, they are found by a run again after an interruption too. The
# serverless form's functions, which the install names, are uninstall_serverless's.
delete_function_log_groups() {
  local stack gone=() kept=() groups group
  for stack in $(environment_stacks); do
    if [[ "$(stack_status "$stack")" == NONE ]]; then
      gone+=("$stack")
    else
      kept+=("$stack")
    fi
  done
  ((${#gone[@]})) || return 0
  groups="$(function_log_groups "${gone[@]}")"
  for group in $groups; do
    if ((${#kept[@]} == 0)) || ! named_after_stacks "${group#/aws/lambda/}" "${kept[@]}"; then
      aws logs delete-log-group --region "$COLVENNA_REGION" --log-group-name "$group"
    fi
  done
}

# given_database: whether the configuration's database is one the install was given (COLVENNA_DB=existing,
# and no Storage stack of the install's own holds a database), not its own or a restored copy of its own
# (user manual 6.2). Read before --delete-data removes the stack.
given_database() {
  [[ "$COLVENNA_DB" == existing && -z "$(cfn_output "$(stack_id Storage)" DbEndpoint)" ]]
}

# print_given_leftovers <given database: 1 or empty>: after --delete-data, the catalog's part of what the
# install was given, which it never deletes, and how to remove it (the deployment guide's section 8): its
# database and users in the given PostgreSQL, and its prefix in a given bucket, unless the bucket is gone
# (one the install made). The catalog's user owns its database, and the admin user of RDS and Aurora is no
# superuser: the admin first grants itself the role, which it may as the role's creator (#73).
print_given_leftovers() {
  local lines=() err
  if [[ -n "$1" ]]; then
    lines+=("  as the admin user of $COLVENNA_DB_ENDPOINT, connected to the database postgres:"
      "    GRANT \"$COLVENNA_DB_USER\" TO CURRENT_USER;" "    DROP DATABASE \"$COLVENNA_DB_NAME\";"
      "    DROP ROLE \"$COLVENNA_DB_USER\";")
    [[ "$COLVENNA_FORM" != serverless ]] || lines+=("    DROP ROLE \"${COLVENNA_DB_USER}_authz\";")
  fi
  if [[ -n "$COLVENNA_BUCKET" ]]; then
    err="$(mktemp)"
    if aws s3api head-bucket --bucket "$COLVENNA_BUCKET" --region "$COLVENNA_REGION" >/dev/null 2>"$err" \
      || ! grep -qE '\(404\)|Not Found' "$err"; then
      lines+=("  aws s3 rm s3://$COLVENNA_BUCKET/$COLVENNA_WAREHOUSE_PREFIX/ --recursive")
    fi
    rm -f "$err"
  fi
  ((${#lines[@]})) || return 0
  echo "What the install was given stays. Remove the catalog's part of it if you no longer need it (deployment guide, section 8):"
  printf '%s\n' "${lines[@]}"
}

main() {
  parse_args "$@"
  load_config "$CONFIG_FILE"
  STATE_DIR="$REPO/.state/$COLVENNA_ENV"
  EVIDENCE="$STATE_DIR/evidence/uninstall-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$EVIDENCE"
  export STATE_DIR EVIDENCE
  trap 'on_error uninstall.sh' ERR

  step "tools"
  assert_tools uninstall
  "$REPO/scripts/install-tools.sh" >/dev/null
  export PATH="$REPO/.tools:$PATH"
  assert_account
  [[ "$DELETE_DATA" != 1 ]] || assert_no_foreign_databases
  confirm_uninstall
  [[ "$DELETE_DATA" != 1 ]] || confirm_delete_data
  local given=""
  [[ "$DELETE_DATA" != 1 ]] || ! given_database || given=1
  cdk_dependencies if-missing
  local placeholders=(-c "account=$COLVENNA_ACCOUNT" -c "region=$COLVENNA_REGION" -c "sourceSha=$(printf '0%.0s' {1..40})"
    -c "env=$COLVENNA_ENV")
  if [[ "$COLVENNA_FORM" == serverless ]]; then
    uninstall_serverless "${placeholders[@]}"
    step "function log groups"
    delete_function_log_groups
    local stack status
    for stack in "$(stack_id Serverless)" "$(stack_id Images)" "$(stack_id Storage)" "$(stack_id Network)"; do
      status="$(stack_status "$stack")"
      echo "$stack: $([[ "$status" == NONE ]] && echo removed || echo "kept ($status)")"
    done | tee "$EVIDENCE/RESULT"
    [[ "$DELETE_DATA" != 1 ]] || print_given_leftovers "$given"
    return 0
  fi

  step "cluster"
  local cluster
  cluster="$(cluster_status)"
  if [[ "$cluster" != NONE ]]; then
    kube_access
    local hostname arn listed namespaces
    hostname="$(nlb_hostname)"
    if kubectl -n "$COLVENNA_NAMESPACE" get service "$COLVENNA_RELEASE_NAME-nlb" >/dev/null 2>&1; then
      step "internal NLB"
      kubectl -n "$COLVENNA_NAMESPACE" delete service "$COLVENNA_RELEASE_NAME-nlb" --wait=true >/dev/null
    fi
    if [[ -n "$hostname" ]]; then
      for _ in $(seq 1 60); do
        arn="$(nlb_arn "$hostname")"
        [[ "$arn" == arn:* ]] || break
        sleep 10
      done
      [[ "$arn" != arn:* ]] || { echo "The load balancer $hostname is still there after 10 minutes" >&2; return 1; }
    fi
    step "release"
    if helm status "$COLVENNA_RELEASE_NAME" --namespace "$COLVENNA_NAMESPACE" >/dev/null 2>&1; then
      helm uninstall "$COLVENNA_RELEASE_NAME" --namespace "$COLVENNA_NAMESPACE" --wait --timeout 10m >>"$EVIDENCE/helm.log"
    fi
    kubectl -n "$COLVENNA_NAMESPACE" delete secret "$DB_SECRET" "$TLS_SECRET" "$ADMIN_TOKEN_SECRET" "$DB_BOOTSTRAP" \
      --ignore-not-found >/dev/null
    kubectl -n "$COLVENNA_NAMESPACE" delete configmap "$DB_CA_CONFIGMAP" --ignore-not-found >/dev/null
    [[ "$DELETE_DATA" != 1 ]] || kubectl -n "$COLVENNA_NAMESPACE" delete secret "$CA_SECRET" --ignore-not-found >/dev/null
    # What an interrupted install's smoke check may have left (one holds the admin's token).
    listed="$(smoke_namespaces)"
    mapfile -t namespaces <<<"$listed"
    kubectl delete namespace "${namespaces[@]}" --ignore-not-found --wait=false >/dev/null
  else
    echo "The cluster $COLVENNA_EKS_CLUSTER is not there: nothing to remove from it"
  fi

  local eks storage eks_status storage_status
  eks="$(stack_id Eks)"
  storage="$(stack_id Storage)"
  eks_status="$(stack_status "$eks")"
  if [[ "$eks_status" != NONE ]]; then
    step "$eks"
    destroy_stack "$eks" "${placeholders[@]}" -c stack=eks -c bucket=placeholder-bucket -c warehousePrefix=warehouse \
      -c "oidcIssuer=https://oidc.eks.$COLVENNA_REGION.amazonaws.com/id/$(printf '0%.0s' {1..32})"
  fi
  [[ "$DELETE_DATA" != 1 ]] || remove_storage "$storage" "${placeholders[@]}"
  step "function log groups"
  delete_function_log_groups

  eks_status="$(stack_status "$eks")"
  storage_status="$(stack_status "$storage")"
  {
    echo "release: removed; $eks: $([[ "$eks_status" == NONE ]] && echo removed || echo "$eks_status")"
    echo "$storage: $([[ "$storage_status" == NONE ]] && echo removed || echo "kept ($storage_status)")"
  } | tee "$EVIDENCE/RESULT"
  [[ "$DELETE_DATA" != 1 ]] || print_given_leftovers "$given"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
