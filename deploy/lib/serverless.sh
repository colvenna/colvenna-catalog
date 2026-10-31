# shellcheck shell=bash
# The serverless form (COLVENNA_FORM=serverless, #13): what install.sh and uninstall.sh do for it. The
# catalog runs on Lambda in the stacks of deploy/aws (network, storage, images, serverless); install.sh
# never reaches its database or its private API itself. It invokes two of the form's functions with
# IAM-authenticated Invokes instead (nothing else can invoke them):
#   the maintenance function (the server's own image, deploy/lambda/maintenance.sh): migrate and
#     bootstrap-admin, as the EKS form runs them in a catalog pod;
#   the ops function (deploy/aws/functions/ops.ts): the database's users, the authorizer's grants, and
#     requests to the private API through its VPC endpoint, as the EKS form's psql pod and port-forward.
# (The form's purge function, which deletes the files of dropped tables, runs on its own schedule; install.sh
# invokes it once, in its smoke check.)
# Passwords and tokens reach them in the Invoke's payload, written to files in WORK (mode 700) and never
# put on a command line or printed. Reads the configuration and REPO, STATE_DIR, EVIDENCE and WORK.

# The preflight's judgement of a VPC and a database (deploy/lib/vpc_check.py, beside this file).
SL_VPC_CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vpc_check.py"
# The change of the deployed serverless stack's template before an upgrade's migrate (beside this file).
SL_TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/serverless_template.py"
# CloudFormation takes a template of at most this many bytes in the request itself (UpdateStack's
# TemplateBody: https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_UpdateStack.html);
# a larger one goes by an S3 URL (TemplateURL), from the CDK bootstrap's staging bucket.
TEMPLATE_BODY_MAX=51200
# How long uninstall.sh waits for Lambda to release a function's network interfaces before the network
# stack can go (#12: about 20 minutes).
ENI_WAIT_MINUTES=45

# --- preflight ---------------------------------------------------------------------------------------

# sl_assert_api_logging: the stage writes access and error logs, which API Gateway does only once the
# account's API Gateway settings name a CloudWatch Logs role. install.sh never changes them.
sl_assert_api_logging() {
  local role
  role="$(aws apigateway get-account --region "$COLVENNA_REGION" --query cloudwatchRoleArn --output text 2>/dev/null)" || role=""
  [[ -n "$role" && "$role" != None ]] || refuse "API Gateway in $COLVENNA_REGION has no CloudWatch Logs role (its account" \
    "settings), which the private API's logs need: create a role API Gateway may assume with the AWS managed policy" \
    "AmazonAPIGatewayPushToCloudWatchLogs, then: aws apigateway update-account --region $COLVENNA_REGION" \
    "--patch-operations op=replace,path=/cloudwatchRoleArn,value=<the role's ARN>"
}

# sl_assert_concurrency: the account can reserve what the functions reserve (the server and the
# authorizer COLVENNA_LAMBDA_CONCURRENCY each, the maintenance and purge functions 1 each) and still keep
# the 100 unreserved that Lambda requires; reservations the functions already hold count as available.
sl_assert_concurrency() {
  local settings unreserved limit held=0 name n want
  settings="$(aws lambda get-account-settings --region "$COLVENNA_REGION" --output json)"
  unreserved="$(jq -r '.AccountLimit.UnreservedConcurrentExecutions' <<<"$settings")"
  limit="$(jq -r '.AccountLimit.ConcurrentExecutions' <<<"$settings")"
  for name in server authorizer maintenance purge; do
    n="$(aws lambda get-function-concurrency --region "$COLVENNA_REGION" --function-name "colvenna-catalog-$COLVENNA_ENV-$name" \
      --query ReservedConcurrentExecutions --output text 2>/dev/null)" || n=0
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    held=$((held + n))
  done
  want=$((2 * COLVENNA_LAMBDA_CONCURRENCY + 2))
  ((unreserved + held - want >= 100)) || refuse "COLVENNA_LAMBDA_CONCURRENCY=$COLVENNA_LAMBDA_CONCURRENCY reserves $want" \
    "concurrent executions; the account has $unreserved unreserved of $limit in $COLVENNA_REGION (and these functions" \
    "hold $held), and Lambda keeps 100 unreserved: lower it, or ask for a higher Lambda concurrency quota"
}

# sl_choose_zones: the zones of a new VPC (SL_ZONES): those of the network stack when it exists, which
# never move (its subnets would be replaced); otherwise the first two by name where the STS, Cognito
# identity and execute-api endpoint services and RDS PostgreSQL (db.t4g.small) are all offered.
sl_choose_zones() {
  SL_ZONES="$(cfn_output "$(stack_id Network)" SubnetZones)"
  if [[ -z "$SL_ZONES" ]]; then
    local services rds
    services="$(aws ec2 describe-vpc-endpoint-services --region "$COLVENNA_REGION" --service-names \
      "com.amazonaws.$COLVENNA_REGION.sts" "com.amazonaws.$COLVENNA_REGION.cognito-identity" "com.amazonaws.$COLVENNA_REGION.execute-api" \
      --query 'ServiceDetails[].{name: ServiceName, zones: AvailabilityZones}' --output json)"
    rds="$(aws rds describe-orderable-db-instance-options --region "$COLVENNA_REGION" --engine postgres \
      --db-instance-class db.t4g.small --query 'OrderableDBInstanceOptions[].AvailabilityZones[].Name' --output json)"
    SL_ZONES="$(jq -rn --argjson s "$services" --argjson r "$rds" '
      if ($s | length) < 3 then "" else
        reduce ($s[] | .zones) as $z ($r | unique; [.[] | select(. as $x | $z | index($x))]) | sort | .[:2] | join(",") end')"
  fi
  [[ "$SL_ZONES" == *,* ]] || refuse "Fewer than two zones of $COLVENNA_REGION offer the STS, Cognito identity and execute-api" \
    "endpoints and RDS PostgreSQL 16 (db.t4g.small)"
  export SL_ZONES
}

# sl_assert_vpc_quota: a new VPC needs one to spare (only before the network stack exists).
sl_assert_vpc_quota() {
  [[ "$(stack_status "$(stack_id Network)")" == NONE ]] || return 0
  local vpcs quota
  vpcs="$(aws ec2 describe-vpcs --region "$COLVENNA_REGION" --query 'length(Vpcs)' --output text)"
  quota="$(aws service-quotas get-service-quota --region "$COLVENNA_REGION" --service-code vpc --quota-code L-F678F1CE \
    --query Quota.Value --output text)"
  ((vpcs < ${quota%.*})) || refuse "No VPC left in $COLVENNA_REGION for COLVENNA_NETWORK=create: $vpcs of ${quota%.*}"
}

# sl_existing_network: with COLVENNA_NETWORK=existing, what the catalog needs of the user's VPC, or
# refused naming all that is missing: the VPC; the subnets in it, private, in two zones or more; STS and
# Cognito identity (interface endpoints with private DNS) and S3 (a gateway endpoint on every subnet's
# route table) reachable from them, or a NAT gateway as every subnet's default route; and the execute-api
# endpoints, one of them in the VPC. Exports SL_VPC_CIDR, SL_SUBNET_ZONES (in COLVENNA_SUBNET_IDS'
# order) and SL_API_ENDPOINTS (the one in the VPC first). Read-only.
sl_existing_network() {
  local vpc subnets tables endpoints api verdict ids=()
  IFS=, read -ra ids <<<"$COLVENNA_SUBNET_IDS"
  vpc="$(aws ec2 describe-vpcs --region "$COLVENNA_REGION" --vpc-ids "$COLVENNA_VPC_ID" --output json 2>&1)" \
    || refuse "No VPC $COLVENNA_VPC_ID in $COLVENNA_REGION that these credentials can read: $(tail -1 <<<"$vpc")"
  subnets="$(aws ec2 describe-subnets --region "$COLVENNA_REGION" --subnet-ids "${ids[@]}" --output json 2>&1)" \
    || refuse "COLVENNA_SUBNET_IDS: cannot read them all: $(tail -1 <<<"$subnets")"
  tables="$(aws ec2 describe-route-tables --region "$COLVENNA_REGION" --filters "Name=vpc-id,Values=$COLVENNA_VPC_ID" --output json)"
  endpoints="$(aws ec2 describe-vpc-endpoints --region "$COLVENNA_REGION" --filters "Name=vpc-id,Values=$COLVENNA_VPC_ID" --output json)"
  IFS=, read -ra ids <<<"$COLVENNA_API_ENDPOINT_IDS"
  api="$(aws ec2 describe-vpc-endpoints --region "$COLVENNA_REGION" --vpc-endpoint-ids "${ids[@]}" --output json 2>&1)" \
    || refuse "COLVENNA_API_ENDPOINT_IDS: cannot read them all: $(tail -1 <<<"$api")"
  verdict="$(jq -n --argjson vpc "$vpc" --argjson subnets "$subnets" --argjson tables "$tables" --argjson endpoints "$endpoints" \
    --argjson api "$api" '{$vpc, $subnets, $tables, $endpoints, $api}' \
    | python3 "$SL_VPC_CHECK" network "$COLVENNA_REGION" "$COLVENNA_VPC_ID" "$COLVENNA_SUBNET_IDS")"
  if [[ "$(jq '.problems | length' <<<"$verdict")" != 0 ]]; then
    refuse "The VPC $COLVENNA_VPC_ID lacks what the serverless form needs: $(jq -r '.problems | join("; ")' <<<"$verdict")"
  fi
  SL_VPC_CIDR="$(jq -r .cidr <<<"$verdict")"
  SL_SUBNET_ZONES="$(jq -r .zones <<<"$verdict")"
  SL_API_ENDPOINTS="$(jq -r .endpoints <<<"$verdict")"
  export SL_VPC_CIDR SL_SUBNET_ZONES SL_API_ENDPOINTS
}

# sl_assert_db_reachable: an existing database is RDS or Aurora PostgreSQL in the catalog's VPC,
# available on COLVENNA_DB_PORT, with IAM database authentication enabled, and its security groups admit
# that port from the catalog's functions: from CIDRs holding every one of the catalog's subnets, or from
# their security group, once the network stack has made it. The catalog's VPC and subnets are
# COLVENNA_VPC_ID and COLVENNA_SUBNET_IDS with COLVENNA_NETWORK=existing; with create, those the Network
# stack made, so the database is one restored into them (user manual, 6.2), and refused before that
# stack exists. Exports SL_DB_RESOURCE_ID (what rds-db:connect names it by). Read-only.
sl_assert_db_reachable() {
  [[ "$COLVENNA_DB" == existing ]] || return 0
  local db ids=() groups subnets catalog verdict network vpc="$COLVENNA_VPC_ID" subnet_ids="$COLVENNA_SUBNET_IDS"
  local where="COLVENNA_VPC_ID $COLVENNA_VPC_ID"
  network="$(stack_id Network)"
  if [[ "$COLVENNA_NETWORK" == create ]]; then
    vpc="$(cfn_output "$network" VpcId)" subnet_ids="$(cfn_output "$network" SubnetIds)"
    [[ -n "$vpc" && -n "$subnet_ids" ]] || refuse "COLVENNA_DB=existing with COLVENNA_NETWORK=create is a database in the VPC" \
      "this install made ($network), and there is none yet: install with COLVENNA_DB=create first; for a database in a VPC" \
      "of your own, use COLVENNA_NETWORK=existing"
    where="the VPC this install made ($vpc)"
  fi
  assert_db_usable
  db="$DB_DESCRIPTION"
  # The functions sign in with RDS IAM tokens: the database must take them.
  [[ "$(jq -r '.iam // false' <<<"$db")" == true ]] || refuse "The database at $COLVENNA_DB_ENDPOINT does not have IAM database" \
    "authentication enabled, which the serverless form signs in with (no password): enable it (aws rds modify-db-instance" \
    "--region $COLVENNA_REGION --enable-iam-database-authentication, or modify-db-cluster for Aurora) and run again"
  SL_DB_RESOURCE_ID="$(jq -r '.id // ""' <<<"$db")"
  export SL_DB_RESOURCE_ID
  mapfile -t ids < <(jq -r '.groups[]' <<<"$db")
  groups="$(aws ec2 describe-security-groups --region "$COLVENNA_REGION" --group-ids "${ids[@]}" --output json)"
  IFS=, read -ra ids <<<"$subnet_ids"
  subnets="$(aws ec2 describe-subnets --region "$COLVENNA_REGION" --subnet-ids "${ids[@]}" --output json)"
  catalog="$(cfn_output "$network" CatalogSecurityGroupId)"
  verdict="$(jq -n --argjson groups "$groups" --argjson subnets "$subnets" '{$groups, $subnets}' \
    | python3 "$SL_VPC_CHECK" database "$COLVENNA_DB_PORT" "$vpc" "$catalog")"
  case "$verdict" in
    open) ;;
    vpc\ *) refuse "The database at $COLVENNA_DB_ENDPOINT is in ${verdict#vpc }, not $where" ;;
    *) refuse "The database's security groups ($(jq -r '.groups | join(",")' <<<"$db")) do not admit TCP $COLVENNA_DB_PORT" \
      "from the catalog's subnets ($(jq -r '[.Subnets[].CidrBlock] | join(",")' <<<"$subnets"))${catalog:+ or its security group $catalog}:" \
      "add an ingress rule for one of them" ;;
  esac
}

# sl_database_endpoint: the database host the functions run on (the Serverless stack's output); empty
# before the first install, and for a stack deployed before it had the output.
sl_database_endpoint() {
  cfn_output "$(stack_id Serverless)" DatabaseEndpoint
}

# preflight_serverless: every check of the serverless form, in order; the account first.
preflight_serverless() {
  assert_account
  assert_cdk_bootstrap
  sl_assert_api_logging
  sl_assert_concurrency
  if [[ "$COLVENNA_NETWORK" == create ]]; then
    sl_assert_vpc_quota
    sl_choose_zones
  else
    sl_existing_network
  fi
  sl_assert_db_reachable
  assert_bucket
  assert_db_admin_secret
  assert_database_kept sl_database_endpoint
}

# --- invoking the form's functions -------------------------------------------------------------------

# lambda_invoke <function> <payload file> <answer file>: an IAM-authenticated Invoke of the function,
# waiting for it to finish; fails when the function did, printing its error (never the payload).
lambda_invoke() {
  local failed
  failed="$(aws lambda invoke --region "$COLVENNA_REGION" --function-name "$1" --cli-read-timeout 900 \
    --payload "fileb://$2" "$3" --query FunctionError --output text)" || return 1
  if [[ -n "$failed" && "$failed" != None ]]; then
    echo "$1 failed ($failed): $(jq -r '.errorMessage // .' "$3" 2>/dev/null | head -c 2000)" >&2
    return 1
  fi
}

# sl_payload <file> <jq filter> <jq arguments...>: a payload file only this user can read.
sl_payload() {
  local file="$1"
  shift
  (umask 077 && jq -n "$@" >"$file")
}

# sl_maintenance <command> [token file]: migrate or bootstrap-admin, by the maintenance function; for
# bootstrap-admin, the new token goes into the token file, never anywhere else.
sl_maintenance() {
  local payload="$WORK/maintenance-payload.json" answer="$WORK/maintenance-answer.json" code
  # shellcheck disable=SC2016  # a jq filter, for jq
  sl_payload "$payload" --arg c "$1" '$c'
  lambda_invoke "$MAINTENANCE_FUNCTION" "$payload" "$answer" || { rm -f "$payload" "$answer"; return 1; }
  rm -f "$payload"
  code="$(jq -r '.exitCode // "none"' "$answer")"
  jq -r '.log // ""' "$answer" | base64 -d >"$EVIDENCE/maintenance-$1.log" 2>/dev/null || true
  if [[ "$code" != 0 ]]; then
    echo "The maintenance function's $1 exited $code; its output ends:" >&2
    tail -20 "$EVIDENCE/maintenance-$1.log" >&2
    rm -f "$answer"
    return 1
  fi
  if [[ -n "${2:-}" ]]; then
    (umask 077 && jq -j '.token // ""' "$answer" >"$2")
    [[ -s "$2" ]] || { rm -f "$answer"; echo "bootstrap-admin answered without a token" >&2; return 1; }
  fi
  rm -f "$answer"
}

# sl_secret_file <secret ARN> <file>: the secret's string, into a file only this user can read.
sl_secret_file() {
  (umask 077 && aws secretsmanager get-secret-value --region "$COLVENNA_REGION" --secret-id "$1" \
    --query SecretString --output text >"$2")
}

# ops_request <method> <path> [token file] [json body]: one request to the private API, from the ops
# function in the catalog's VPC through its execute-api endpoint; prints {"status": n, "body": "..."}.
ops_request() {
  local payload="$WORK/ops-request.json" answer="$WORK/ops-answer.json" args=(--arg m "$1" --arg p "$2" --arg b "${4:-}")
  if [[ -n "${3:-}" ]]; then
    args+=(--rawfile t "$3")
  else
    args+=(--arg t "")
  fi
  # shellcheck disable=SC2016  # a jq filter, for jq
  sl_payload "$payload" "${args[@]}" '{action: "request", method: $m, path: $p}
    + (if $t == "" then {} else {token: ($t | rtrimstr("\n"))} end) + (if $b == "" then {} else {body: ($b | fromjson)} end)'
  lambda_invoke "$OPS_FUNCTION" "$payload" "$answer" || { rm -f "$payload" "$answer"; return 1; }
  rm -f "$payload"
  cat "$answer"
  rm -f "$answer"
}

# --- the admin, as ensure_admin (catalog.sh) uses it with via=sl_ -----------------------------------

# sl_admin_token_held: the admin token secret holds a token, not the placeholder the stack made it with.
sl_admin_token_held() {
  sl_secret_file "$ADMIN_TOKEN_SECRET_ARN" "$WORK/admin-token" || return 1
  local held
  held="$(grep -cE '^colvenna_[A-Za-z0-9_-]+$' "$WORK/admin-token" || true)"
  rm -f "$WORK/admin-token"
  [[ "$held" == 1 ]]
}

# sl_issue_admin_token: a new admin token from the server itself (bootstrap-admin, in the maintenance
# function), straight into the admin token secret.
sl_issue_admin_token() {
  sl_maintenance bootstrap-admin "$WORK/new-admin-token"
  aws secretsmanager put-secret-value --region "$COLVENNA_REGION" --secret-id "$ADMIN_TOKEN_SECRET_ARN" \
    --secret-string "file://$WORK/new-admin-token" >/dev/null
  rm -f "$WORK/new-admin-token"
}

sl_admin_open() {
  sl_secret_file "$ADMIN_TOKEN_SECRET_ARN" "$WORK/admin-token"
}

sl_admin_close() {
  rm -f "$WORK/admin-token"
}

# sl_admin_status <method> <path>: the HTTP status only.
sl_admin_status() {
  ops_request "$1" "$2" "$WORK/admin-token" | jq -r .status
}

# sl_admin_call <method> <path> [json-body]: prints the response body; fails on HTTP >= 400.
sl_admin_call() {
  local answer
  answer="$(ops_request "$1" "$2" "$WORK/admin-token" "${3:-}")" || return 1
  if (($(jq -r .status <<<"$answer") >= 400)); then
    echo "$1 $2: $(jq -r '"\(.status) \(.body)"' <<<"$answer" | head -c 500)" >&2
    return 1
  fi
  jq -r .body <<<"$answer"
}

# --- the steps -----------------------------------------------------------------------------------------

# sl_stacks: the network stack, the storage stack (when the install makes the bucket or the database)
# and the images stack. Exports what the later steps use: the network (VPC_ID, SUBNET_IDS,
# SUBNET_ZONES, CATALOG_SECURITY_GROUP, API_ENDPOINTS), the warehouse (BUCKET, WAREHOUSE_ROOT), the
# database (DB_ENDPOINT, DB_PORT, DB_ADMIN_SECRET_ARN) and the image repository (REPOSITORY_URI).
sl_stacks() {
  local common=("$@") network storage images extra=()
  network="$(stack_id Network)"
  if [[ "$COLVENNA_NETWORK" == create ]]; then
    deploy_stack "$network" "${common[@]}" -c stack=network -c networkMode=create -c "vpcCidr=$COLVENNA_VPC_CIDR" \
      -c "subnetZones=$SL_ZONES" -c "databasePort=$COLVENNA_DB_PORT"
  else
    deploy_stack "$network" "${common[@]}" -c stack=network -c networkMode=existing -c "vpcId=$COLVENNA_VPC_ID" \
      -c "vpcCidr=$SL_VPC_CIDR" -c "subnetIds=$COLVENNA_SUBNET_IDS" -c "subnetZones=$SL_SUBNET_ZONES" \
      -c "databasePort=$COLVENNA_DB_PORT"
  fi
  VPC_ID="$(stack_output "$network" VpcId)"
  SUBNET_IDS="$(stack_output "$network" SubnetIds)"
  SUBNET_ZONES="$(stack_output "$network" SubnetZones)"
  CATALOG_SECURITY_GROUP="$(stack_output "$network" CatalogSecurityGroupId)"
  if [[ "$COLVENNA_NETWORK" == create ]]; then
    API_ENDPOINTS="$(stack_output "$network" ExecuteApiEndpointId)"
    [[ -z "$COLVENNA_API_ENDPOINT_IDS" ]] || IFS=, read -ra extra <<<"$COLVENNA_API_ENDPOINT_IDS"
    local id
    for id in "${extra[@]}"; do
      [[ ",$API_ENDPOINTS," == *",$id,"* ]] || API_ENDPOINTS+=",$id"
    done
  else
    API_ENDPOINTS="$SL_API_ENDPOINTS"
  fi
  BUCKET="$COLVENNA_BUCKET"
  storage="$(stack_id Storage)"
  if [[ -z "$COLVENNA_BUCKET" || "$COLVENNA_DB" == create ]]; then
    deploy_stack "$storage" "${common[@]}" -c stack=storage \
      -c "createBucket=$([[ -z "$COLVENNA_BUCKET" ]] && echo true || echo false)" \
      -c "createDatabase=$([[ "$COLVENNA_DB" == create ]] && echo true || echo false)" \
      -c "vpcId=$VPC_ID" -c "subnetIds=$SUBNET_IDS" -c "subnetZones=$SUBNET_ZONES" \
      -c "clientSecurityGroupId=$CATALOG_SECURITY_GROUP"
  fi
  [[ -n "$BUCKET" ]] || BUCKET="$(stack_output "$storage" BucketName)"
  if [[ "$COLVENNA_DB" == create ]]; then
    DB_ENDPOINT="$(stack_output "$storage" DbEndpoint)"
    DB_PORT="$(stack_output "$storage" DbPort)"
    DB_ADMIN_SECRET_ARN="$(stack_output "$storage" DbAdminSecretArn)"
    DB_RESOURCE_ID="$(stack_output "$storage" DbResourceId)"
  else
    # sl_assert_db_reachable read the existing database's resource ID.
    DB_ENDPOINT="$COLVENNA_DB_ENDPOINT" DB_PORT="$COLVENNA_DB_PORT" DB_ADMIN_SECRET_ARN="$COLVENNA_DB_ADMIN_SECRET_ARN"
    DB_RESOURCE_ID="$SL_DB_RESOURCE_ID"
  fi
  WAREHOUSE_ROOT="s3://$BUCKET/$COLVENNA_WAREHOUSE_PREFIX"
  images="$(stack_id Images)"
  deploy_stack "$images" "${common[@]}" -c stack=images
  REPOSITORY_URI="$(stack_output "$images" RepositoryUri)"
  export VPC_ID SUBNET_IDS SUBNET_ZONES CATALOG_SECURITY_GROUP API_ENDPOINTS BUCKET WAREHOUSE_ROOT DB_ENDPOINT DB_PORT \
    DB_ADMIN_SECRET_ARN DB_RESOURCE_ID REPOSITORY_URI
}

# sl_synth_serverless <image> <cdk arguments...>: this version's serverless stack synthesized, its
# functions on the image (repository@digest), into STATE_DIR/cdk.out: what an upgrade takes the
# maintenance function from before migrate (sl_maintenance_first), and what sl_deploy_serverless deploys.
sl_synth_serverless() {
  local image="$1"
  shift
  synth_stack "$(stack_id Serverless)" "$@" -c stack=serverless -c "vpcId=$VPC_ID" -c "subnetIds=$SUBNET_IDS" \
    -c "subnetZones=$SUBNET_ZONES" -c "securityGroupId=$CATALOG_SECURITY_GROUP" -c "apiEndpointIds=$API_ENDPOINTS" \
    -c "imageUri=$image" -c "bucket=$BUCKET" -c "warehousePrefix=$COLVENNA_WAREHOUSE_PREFIX" \
    -c "warehouseName=$COLVENNA_WAREHOUSE_NAME" -c "databaseEndpoint=$DB_ENDPOINT" -c "databasePort=$DB_PORT" \
    -c "databaseName=$COLVENNA_DB_NAME" -c "databaseUser=$COLVENNA_DB_USER" -c "databaseResourceId=$DB_RESOURCE_ID" \
    -c "dataTokens=$COLVENNA_DATA_TOKENS" \
    -c "dataTokenDuration=$COLVENNA_DATA_TOKEN_DURATION" -c "memory=$COLVENNA_LAMBDA_MEMORY" \
    -c "concurrency=$COLVENNA_LAMBDA_CONCURRENCY" -c "snapStart=$([[ "$COLVENNA_SNAPSTART" == on ]] && echo true || echo false)"
}

# sl_delete_orphan_log_groups <stack> <template>: before the template is deployed, the log groups it makes
# for this install's functions that are there already and that the stack does not own (#54): a function's
# log group is written again after the stack that made it let it go, Lambda making it anew, as when going
# back to a version without the function deletes it while a run of it goes on (the purge function's runs
# take up to 15 minutes). CloudFormation would then refuse to make it ("already exists"), so such a group
# is deleted, if it is one of this install's functions' (/aws/lambda/colvenna-catalog-<env>-...) and that
# function is gone; a group still another function's is left, for CloudFormation to refuse. Prints what it
# deleted; a group written again after that makes the deploy fail as before, and a run again deletes it.
sl_delete_orphan_log_groups() {
  local stack="$1" template="$2" owned="" groups group found function err="$WORK/orphan-log-groups.err"
  [[ "$(stack_status "$stack")" == NONE ]] || owned="$(stack_resources "$stack" AWS::Logs::LogGroup)"
  groups="$(jq -r '.Resources[] | select(.Type == "AWS::Logs::LogGroup") | .Properties.LogGroupName | strings' "$template")"
  for group in $groups; do
    [[ "$group" == "/aws/lambda/colvenna-catalog-$COLVENNA_ENV-"* ]] || continue
    grep -qxF -- "$group" <<<"$owned" && continue
    found="$(aws logs describe-log-groups --region "$COLVENNA_REGION" --log-group-name-prefix "$group" \
      --query "logGroups[?logGroupName=='$group'] | length(@)" --output text 2>"$err")" || { cat "$err" >&2; return 1; }
    [[ "$found" == 1 ]] || continue
    function="${group#/aws/lambda/}"
    if aws lambda get-function --region "$COLVENNA_REGION" --function-name "$function" >/dev/null 2>"$err"; then
      echo "  $group is the function $function's, which $stack does not own: left" >&2
      continue
    fi
    grep -q ResourceNotFoundException "$err" || { cat "$err" >&2; return 1; }
    aws logs delete-log-group --region "$COLVENNA_REGION" --log-group-name "$group"
    echo "  deleted $group: the log group of $function, which is gone, written after its stack let it go"
  done
  rm -f "$err"
}

# sl_deploy_serverless: the serverless stack sl_synth_serverless synthesized, deployed, the log groups
# it makes of functions that are gone deleted first (sl_delete_orphan_log_groups, which the functions
# step runs once already before it changes anything); exports the names and secrets the later steps use.
sl_deploy_serverless() {
  local stack
  stack="$(stack_id Serverless)"
  sl_delete_orphan_log_groups "$stack" "$STATE_DIR/cdk.out/$stack.template.json"
  deploy_assembly "$stack"
  API_URL="$(stack_output "$stack" ApiUrl)"
  SERVER_FUNCTION="$(stack_output "$stack" ServerFunctionName)"
  MAINTENANCE_FUNCTION="$(stack_output "$stack" MaintenanceFunctionName)"
  PURGE_FUNCTION="$(stack_output "$stack" PurgeFunctionName)"
  AUTHORIZER_FUNCTION="$(stack_output "$stack" AuthorizerFunctionName)"
  OPS_FUNCTION="$(stack_output "$stack" OpsFunctionName)"
  ADMIN_TOKEN_SECRET_ARN="$(stack_output "$stack" AdminTokenSecretArn)"
  API_ID="$(stack_output "$stack" ApiId)"
  export API_URL SERVER_FUNCTION MAINTENANCE_FUNCTION PURGE_FUNCTION AUTHORIZER_FUNCTION OPS_FUNCTION ADMIN_TOKEN_SECRET_ARN API_ID
}

# sl_maintenance_first: an upgrade's first change, while the server runs another image (#63): the
# serverless stack as it is deployed, with the maintenance function as this version makes it (its image,
# its settings and the database it migrates, which install onto a restored instance changes too, and its
# role's policy, which names that database) and nothing else changed, so that this version's migrate runs
# before anything that serves changes, and a migrate that refuses (an older version going back onto a
# newer schema) leaves the server function, its published versions and the alias live, the authorizer and
# the ops and purge functions as they were. Not a CDK deploy of this version: it would also change the
# server function's description and settings, and with them publish a new version and move the alias.
# CloudFormation updates the stack with its deployed template so changed (serverless_template.py, from
# what sl_synth_serverless made) and the stack's own parameters; given no role and no tags, UpdateStack
# keeps the stack's role (the CDK bootstrap's execution role) and tags (its API reference, read
# 2026-10-31: https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_UpdateStack.html). It
# rolls back if that fails; a template with nothing to change (a run again after a refused or failed
# migrate) is no failure. A template over TEMPLATE_BODY_MAX (the stack's is, with the metadata CDK puts
# in it: 51,365 bytes measured) goes by its URL in the CDK bootstrap's staging bucket, as CDK sends its
# own, and is deleted from there once the update is over (the bucket's versioning keeps a noncurrent
# copy as its lifecycle rule says). Exports the name the migrate step uses, from the stack's outputs.
sl_maintenance_first() {
  local stack maintenance keys key parameters=() deployed="$WORK/serverless-deployed.json" template="$WORK/serverless-template.json"
  local err="$WORK/update-stack.err" source=() bucket="" object="" updated=0
  stack="$(stack_id Serverless)"
  stack_settled "$stack" >/dev/null
  maintenance="$(cfn_output "$stack" MaintenanceFunctionName)"
  [[ -n "$maintenance" ]] || { echo "$stack has no output MaintenanceFunctionName: not a serverless stack this knows" >&2; return 1; }
  MAINTENANCE_FUNCTION="$maintenance"
  export MAINTENANCE_FUNCTION
  aws cloudformation get-template --region "$COLVENNA_REGION" --stack-name "$stack" --template-stage Original \
    --query TemplateBody --output json >"$deployed"
  python3 "$SL_TEMPLATE" maintenance-first "$deployed" "$STATE_DIR/cdk.out/$stack.template.json" "$maintenance" >"$template"
  # The stack's parameters (CDK's BootstrapVersion) as they are.
  keys="$(aws cloudformation describe-stacks --region "$COLVENNA_REGION" --stack-name "$stack" \
    --query 'Stacks[0].Parameters[].ParameterKey' --output text)"
  for key in $keys; do
    [[ "$key" == None ]] || parameters+=("ParameterKey=$key,UsePreviousValue=true")
  done
  ((${#parameters[@]} == 0)) || parameters=(--parameters "${parameters[@]}")
  if (($(wc -c <"$template") > TEMPLATE_BODY_MAX)); then
    bucket="$(cfn_output CDKToolkit BucketName)"
    [[ -n "$bucket" ]] || { echo "The CDK bootstrap stack CDKToolkit names no staging bucket (output BucketName)" >&2; return 1; }
    object="colvenna-catalog/$stack/maintenance-first-$(sha256sum "$template" | cut -c1-64).json"
    aws s3 cp --region "$COLVENNA_REGION" --only-show-errors "$template" "s3://$bucket/$object"
    # Path-style, as CDK gives its own templates' URLs.
    source=(--template-url "https://s3.$COLVENNA_REGION.amazonaws.com/$bucket/$object")
  else
    source=(--template-body "file://$template")
  fi
  if aws cloudformation update-stack --region "$COLVENNA_REGION" --stack-name "$stack" "${source[@]}" \
    "${parameters[@]}" --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND >/dev/null 2>"$err"; then
    aws cloudformation wait stack-update-complete --region "$COLVENNA_REGION" --stack-name "$stack" && updated=1
  elif grep -q "No updates are to be performed" "$err"; then
    updated=2
  fi
  # Best effort: what the update came to is the step's answer, not this.
  if [[ -n "$object" ]] && ! aws s3 rm --region "$COLVENNA_REGION" --only-show-errors "s3://$bucket/$object" 2>/dev/null; then
    echo "  (s3://$bucket/$object could not be deleted; it is a copy of the template, and may go)" >&2
  fi
  case "$updated" in
    1) echo "  $maintenance as this version makes it; nothing else of $stack changed" ;;
    2) echo "  $maintenance already as this version makes it" ;;
    *)
      [[ -s "$err" ]] && cat "$err" >&2
      echo "$stack: the maintenance function's update did not complete: $(stack_status "$stack")" >&2
      return 1
      ;;
  esac
}

# sl_database: the catalog's database user and database, and the authorizer's read-only user, made or
# brought in line by the ops function as the database's admin user. Neither user has a password: the
# functions sign in with RDS IAM tokens (rds_iam). The admin's password is the one secret, and goes in
# this step's payload alone.
sl_database() {
  sl_secret_file "$DB_ADMIN_SECRET_ARN" "$WORK/admin.json"
  # shellcheck disable=SC2016  # a jq filter, for jq
  sl_payload "$WORK/database.json" --slurpfile a "$WORK/admin.json" \
    '{action: "database", admin: {username: $a[0].username, password: $a[0].password}}'
  rm -f "$WORK/admin.json"
  local ok=0
  lambda_invoke "$OPS_FUNCTION" "$WORK/database.json" "$WORK/database-answer.json" && ok=1
  rm -f "$WORK/database.json"
  ((ok)) || return 1
  echo "  database: $(jq -r .statements "$WORK/database-answer.json") statements run" | tee -a "$EVIDENCE/database.txt"
  rm -f "$WORK/database-answer.json"
}

# sl_grants: after migrate, the authorizer's user may read what the token check reads, as the catalog's
# user (the tables' owner, signing in with an IAM token) grants it.
sl_grants() {
  sl_payload "$WORK/grants.json" '{action: "grants"}'
  local ok=0
  lambda_invoke "$OPS_FUNCTION" "$WORK/grants.json" "$WORK/grants-answer.json" && ok=1
  rm -f "$WORK/grants.json"
  ((ok)) || return 1
  rm -f "$WORK/grants-answer.json"
}

# sl_smoke <evidence-dir>: from inside the VPC, through the private API: a request without a token, and
# one with a token that is none, are refused (401) before the server; the admin's succeeds (200). No
# function of the form has a function URL; the API is private and its policy denies everything not
# through its VPC endpoints. Writes smoke.txt.
# sl_purge_now: one run of the purge function, as its schedule runs it (#69): proof that it runs, and its
# first summary, the metric its backlog alarm reads. A run of the schedule's under way holds the function's
# one execution: asked again. Prints "ran" or "failed".
sl_purge_now() {
  local payload="$WORK/purge-payload.json" answer="$WORK/purge-answer.json" errors="$WORK/purge-errors" try
  sl_payload "$payload" '"purge"'
  for try in 1 2 3 4; do
    if lambda_invoke "$PURGE_FUNCTION" "$payload" "$answer" 2>"$errors"; then
      jq -r '.log // ""' "$answer" 2>/dev/null | base64 -d >"$EVIDENCE/purge.log" 2>/dev/null || true
      rm -f "$payload" "$answer" "$errors"
      echo ran
      return 0
    fi
    grep -q TooManyRequestsException "$errors" || break
    ((try == 4)) || sleep 20
  done
  cat "$errors" >&2
  rm -f "$payload" "$answer" "$errors"
  echo failed
}

sl_smoke() {
  local into="$1" path="/v1/config?warehouse=$COLVENNA_WAREHOUSE_NAME" anonymous forged admin fn urls api purge problems=()
  anonymous="$(ops_request GET "$path" | jq -r .status)"
  (umask 077 && printf 'colvenna_%s' "$(openssl rand -hex 24)" >"$WORK/forged-token")
  forged="$(ops_request GET "$path" "$WORK/forged-token" | jq -r .status)"
  rm -f "$WORK/forged-token"
  sl_admin_open
  admin="$(sl_admin_status GET "$path")"
  sl_admin_close
  for fn in "$SERVER_FUNCTION" "$MAINTENANCE_FUNCTION" "$PURGE_FUNCTION" "$AUTHORIZER_FUNCTION" "$OPS_FUNCTION"; do
    urls="$(aws lambda list-function-url-configs --region "$COLVENNA_REGION" --function-name "$fn" \
      --query 'length(FunctionUrlConfigs)' --output text)"
    [[ "$urls" == 0 ]] || problems+=("$fn has $urls function URL(s)")
  done
  api="$(aws apigateway get-rest-api --region "$COLVENNA_REGION" --rest-api-id "$API_ID" --output json)"
  [[ "$(jq -c .endpointConfiguration.types <<<"$api")" == '["PRIVATE"]' ]] || problems+=("the API is not private")
  jq -r '.policy // ""' <<<"$api" | sed 's/\\"/"/g' | grep -q '"StringNotEquals":{"aws:SourceVpce"' \
    || problems+=("the API's policy does not deny requests from elsewhere")
  purge="$(sl_purge_now)"
  [[ "$purge" == ran ]] || problems+=("the purge function's run failed")
  printf 'through the private API from the VPC: anonymous=%s forged=%s admin=%s\nfunction URLs: %s; API: %s; purge function: %s\n' \
    "$anonymous" "$forged" "$admin" "$([[ " ${problems[*]} " == *"function URL"* ]] && echo found || echo none)" \
    "$(jq -c .endpointConfiguration.types <<<"$api")" "$purge" | tee "$into/smoke.txt"
  [[ "$anonymous" == 401 && "$forged" == 401 ]] || problems+=("unauthenticated requests got $anonymous and $forged, not 401")
  [[ "$admin" == 200 ]] || problems+=("the admin's request got $admin, not 200")
  ((${#problems[@]} == 0)) || { echo "Smoke check failed: $(printf '%s; ' "${problems[@]}")" >&2; return 1; }
}

# install_serverless: install.sh's steps for the serverless form, after the tools and, for --release,
# the candidate's checks. An upgrade, or a way back to an older version, migrates first: only the
# maintenance function takes this version (sl_maintenance_first), then migrate runs, and only once it has
# succeeded does the whole stack take this version, the server's new version and the alias live among it
# (#63), and this version's ops function brings the database's users in line. A migrate that refuses ends
# the run with the server as it was, and nothing written.
install_serverless() {
  step "preflight"
  if [[ -n "$RELEASE_CANDIDATE" ]]; then
    assert_account
    release_form_check serverless
    release_registry_check
    release_auth
    release_verify_artifacts "$WORK/release"
  fi
  preflight_serverless
  confirm_install

  step "stacks"
  cdk_dependencies "$([[ -n "$RELEASE_CANDIDATE" ]] && echo exact || echo if-missing)"
  local common=(-c "account=$COLVENNA_ACCOUNT" -c "region=$COLVENNA_REGION" -c "sourceSha=$SOURCE_SHA" -c "env=$COLVENNA_ENV")
  sl_stacks "${common[@]}"

  step "image"
  if [[ -n "$RELEASE_CANDIDATE" ]]; then
    copy_release_image "$REPOSITORY_URI"
  else
    build_image "$REPOSITORY_URI"
  fi
  local image="$IMAGE_REPOSITORY@$IMAGE_DIGEST" running
  running="$(cfn_output "$(stack_id Serverless)" ServerImage)"

  step "functions"
  sl_synth_serverless "$image" "${common[@]}"
  # Before anything changes: a group of a function still there stops the run here, not after migrate.
  sl_delete_orphan_log_groups "$(stack_id Serverless)" "$STATE_DIR/cdk.out/$(stack_id Serverless).template.json"
  local upgrade=""
  if [[ -n "$running" && "$running" != "$image" ]]; then
    upgrade=1
    echo "  the server keeps $running until the database is migrated: the maintenance function alone takes $image"
    sl_maintenance_first
  else
    sl_deploy_serverless
  fi
  step "database"
  if [[ -n "$upgrade" ]]; then
    # The ops function deployed is the serving version's, on the database it serves; the install's database
    # (another, once moved onto a restored instance) has the catalog's users already.
    echo "  after migrate, by this version's ops function (step server)"
  else
    sl_database
  fi
  step "migrate"
  sl_maintenance migrate
  if [[ -n "$upgrade" ]]; then
    step "server"
    sl_deploy_serverless
    sl_database
  fi
  # The grants of this version's ops function, once it is deployed.
  sl_grants
  step "admin"
  ensure_admin sl_
  step "smoke"
  sl_smoke "$EVIDENCE"

  jq -n --arg sha "$SOURCE_SHA" --arg release "$RELEASE_CANDIDATE" --arg image "$image" --arg bucket "$BUCKET" \
    --arg db "$DB_ENDPOINT:$DB_PORT/$COLVENNA_DB_NAME" --arg api "$API_URL" --arg endpoints "$API_ENDPOINTS" \
    '{form: "serverless", sourceSha: $sha, release: (if $release == "" then null else $release end), image: $image,
      bucket: $bucket, database: $db, apiUrl: $api, apiEndpoints: ($endpoints | split(","))}' >"$STATE_DIR/install.json"
  local installed="($SOURCE_SHA)"
  [[ -z "$RELEASE_CANDIDATE" ]] || installed="release $RELEASE_CANDIDATE (built from $SOURCE_SHA)"
  echo "Colvenna Catalog $installed is installed, serverless, in $COLVENNA_REGION: $API_URL (private; through" \
    "$API_ENDPOINTS); the admin's token is in $ADMIN_TOKEN_SECRET_ARN; evidence in $EVIDENCE" | tee "$EVIDENCE/RESULT"
}

# --- uninstalling ----------------------------------------------------------------------------------------

# sl_wait_interfaces <security group>: until no network interface uses the group (Lambda releases a
# function's some minutes after the function is gone), ENI_WAIT_MINUTES at most.
sl_wait_interfaces() {
  local left
  for _ in $(seq 1 $((ENI_WAIT_MINUTES * 2))); do
    left="$(aws ec2 describe-network-interfaces --region "$COLVENNA_REGION" --filters "Name=group-id,Values=$1" \
      --query 'length(NetworkInterfaces)' --output text)"
    [[ "$left" != 0 ]] || return 0
    sleep 30
  done
  echo "$left network interface(s) still use $1 after $ENI_WAIT_MINUTES minutes; run uninstall.sh again later" >&2
  return 1
}

# uninstall_serverless <placeholders...>: uninstall.sh's steps for the serverless form: the serverless
# stack (functions, API, identity pool, roles, secrets) and the images stack, with the log groups they
# never owned; then the data as asked (remove_storage, uninstall.sh); then the network stack, unless a
# kept database still lives in it (its security group admits the catalog's). The log groups of the
# functions CloudFormation named are uninstall.sh's delete_function_log_groups, once the stacks are gone.
uninstall_serverless() {
  local placeholders=("$@") serverless images storage network api group zero
  zero="$(printf '0%.0s' {1..64})"
  serverless="$(stack_id Serverless)" images="$(stack_id Images)" storage="$(stack_id Storage)" network="$(stack_id Network)"
  if [[ "$(stack_status "$serverless")" != NONE ]]; then
    step "$serverless"
    api="$(cfn_output "$serverless" ApiId)"
    destroy_stack "$serverless" "${placeholders[@]}" -c stack=serverless -c vpcId=vpc-00000000 \
      -c subnetIds=subnet-00000000,subnet-00000001 -c "subnetZones=${COLVENNA_REGION}a,${COLVENNA_REGION}b" \
      -c securityGroupId=sg-00000000 -c apiEndpointIds=vpce-00000000 \
      -c "imageUri=$COLVENNA_ACCOUNT.dkr.ecr.$COLVENNA_REGION.amazonaws.com/colvenna-catalog-$COLVENNA_ENV/colvenna-catalog@sha256:$zero" \
      -c bucket=placeholder-bucket -c warehousePrefix=warehouse -c warehouseName=colvenna -c databaseEndpoint=db.example.com \
      -c databasePort=5432 -c databaseName=colvenna_catalog -c databaseUser=colvenna_catalog \
      -c databaseResourceId=db-PLACEHOLDER0000000000 -c dataTokens=aws-sts \
      -c dataTokenDuration=PT3H -c memory=2048 -c concurrency=1 -c snapStart=false
    # What the stack never owned: the log groups its functions may write again as they stop, and the
    # API's execution logs, which API Gateway makes itself.
    delete_log_groups "/aws/lambda/colvenna-catalog-$COLVENNA_ENV-"
    [[ -z "$api" ]] || delete_log_groups "API-Gateway-Execution-Logs_$api/"
  fi
  if [[ "$(stack_status "$images")" != NONE ]]; then
    step "$images"
    destroy_stack "$images" "${placeholders[@]}" -c stack=images
  fi
  if [[ "${DELETE_DATA:-}" == 1 ]]; then
    remove_storage "$storage" "${placeholders[@]}"
  fi
  if [[ "$(stack_status "$network")" != NONE ]]; then
    if [[ "$(stack_status "$storage")" != NONE && -n "$(stack_resources "$storage" AWS::RDS::DBInstance)" ]]; then
      echo "$network: kept, with the database that lives in it ($storage)"
      return 0
    fi
    step "$network"
    group="$(cfn_output "$network" CatalogSecurityGroupId)"
    [[ -z "$group" ]] || sl_wait_interfaces "$group"
    destroy_stack "$network" "${placeholders[@]}" -c stack=network -c networkMode=create -c vpcCidr=10.80.0.0/16 \
      -c "subnetZones=${COLVENNA_REGION}a,${COLVENNA_REGION}b" -c databasePort=5432
  fi
}
