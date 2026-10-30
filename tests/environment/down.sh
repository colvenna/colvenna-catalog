#!/usr/bin/env bash
# Removes the test environment <name> in <region>, and checks nothing of it is left: first every
# Service of type LoadBalancer (so the controller deletes its load balancers, which would otherwise
# keep the VPC from being deleted), then the controller, then the existing bucket's objects (what the
# UAT's existing installs left in it), then the stack, with its database (no final snapshot) and the
# database's admin secret. Only a stack named ColvennaCatalogTest<Name> and tagged
# Purpose=test-environment is ever deleted. Evidence goes to .state/test-<name>-<region>/evidence/down-<time>/.
# Usage: tests/environment/down.sh <name> <region>
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"
[[ $# -eq 2 ]] || die "usage: tests/environment/down.sh <name> <region>"
name="$1" region="$2"
check_args "$name" "$region"
stack="$(stack_name "$name")"
state="$(state_dir "$name" "$region")"
evidence="$state/evidence/down-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$evidence"
export AWS_REGION="$region"
"$REPO/scripts/install-tools.sh" >/dev/null
ensure_cdk

err="$(mktemp)"
# Only a stack that does not exist means there is nothing to do; any other failure to read it (expired
# credentials, the wrong profile) stops here rather than report success while the environment runs on.
if ! purpose="$(aws cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Tags[?Key=='Purpose'].Value | [0]" --output text 2>"$err")"; then
  if grep -q 'does not exist' "$err"; then
    # What a run interrupted once the stack was gone left: its functions' log groups (lib.sh).
    if [[ -e "$state/functions" ]]; then
      delete_recorded_log_groups "$state/functions"
      left="$(recorded_log_groups "$state/functions")"
      [[ -z "$left" ]] || die "Log groups of the stack's functions are left: $left"
      mv "$state/functions" "$evidence/functions.txt"
    fi
    echo "$stack does not exist in $region: nothing to remove" | tee "$evidence/RESULT"
    exit 0
  fi
  die "Cannot read $stack in $region: $(cat "$err")"
fi
[[ "$purpose" == test-environment ]] || die "$stack is tagged Purpose=$purpose, not test-environment: refusing"
cluster="$(stack_output "$region" "$stack" ClusterName)"
vpc="$(stack_output "$region" "$stack" VpcId)"
# A stack whose first deploy failed has no outputs; destroying it needs only its name, so the inputs the
# app checks get placeholders.
sha="$(stack_output "$region" "$stack" SourceSha)"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || sha="$(printf '0%.0s' {1..40})"
zones="$(stack_output "$region" "$stack" PrivateSubnetZones)"
[[ "$zones" == "$region"* ]] || zones="${region}a,${region}b"
account="$(aws sts get-caller-identity --query Account --output text)"
# The stack's database and its admin secret are named after the environment; its bucket is an output
# (None for a stack made before the existing resources were).
db="colvenna-catalog-test-$name"
db_secret="colvenna-catalog-test-$name/existing-db-admin"  # pragma: allowlist secret - the name of the test environment's generated database secret, not a value
bucket="$(stack_output "$region" "$stack" ExistingBucket)"

# --- what Kubernetes made in AWS ------------------------------------------------------------------
if [[ "$cluster" != None ]] && aws eks describe-cluster --name "$cluster" >/dev/null 2>&1; then
  export KUBECONFIG="$state/kubeconfig"
  aws eks update-kubeconfig --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null
  kubectl get services -A -o json \
    | jq -r '.items[] | select(.spec.type=="LoadBalancer") | "\(.metadata.namespace) \(.metadata.name)"' \
    | while read -r ns svc; do
      kubectl -n "$ns" delete service "$svc" --wait=true >>"$evidence/services.log"
    done
  # The controller deletes the load balancers it made; wait for them to go before removing it.
  for _ in $(seq 1 60); do
    left="$(aws resourcegroupstaggingapi get-resources --resource-type-filters elasticloadbalancing:loadbalancer \
      --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$cluster" --query 'length(ResourceTagMappingList)' --output text)"
    [[ "$left" -eq 0 ]] && break
    sleep 10
  done
  [[ "$left" -eq 0 ]] || die "$left load balancer(s) of $cluster are still there after 10 minutes"
  helm uninstall aws-load-balancer-controller --namespace kube-system --wait >>"$evidence/services.log" 2>&1 || true
fi

# --- the existing bucket's objects: CloudFormation deletes only an empty bucket --------------------
if [[ "$bucket" != None ]] && aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
  aws s3 rm "s3://$bucket" --recursive --only-show-errors >"$evidence/bucket.log" 2>&1 \
    || { tail -5 "$evidence/bucket.log" >&2; die "Could not empty the bucket $bucket"; }
fi

# --- the stack ----------------------------------------------------------------------------------
# Its functions, and its nested stacks', by name (lib.sh): their log groups outlive them.
{ cat "$state/functions" 2>/dev/null || true; stack_functions "$stack"; } | sort -u >"$state/functions.new"
mv "$state/functions.new" "$state/functions"
# destroy needs the app to synthesize, not to match what was deployed: placeholders where it checks inputs.
test_cdk "$state" destroy "$stack" --force \
  -c "name=$name" -c "account=$account" -c "region=$region" -c "sourceSha=$sha" -c "zones=$zones" \
  -c "operatorCidr=198.51.100.7/32" -c "adminPrincipalArn=arn:aws:iam::$account:role/destroy" \
  >"$evidence/destroy.log" 2>&1 || { tail -40 "$evidence/destroy.log" >&2; die "cdk destroy failed"; }

# --- nothing left ---------------------------------------------------------------------------------
if aws cloudformation describe-stacks --stack-name "$stack" >/dev/null 2>&1; then
  die "$stack still exists"
fi
left="$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$cluster" \
  --query 'length(ResourceTagMappingList)' --output text)"
[[ "$left" -eq 0 ]] || die "$left resource(s) the controller made for $cluster are left"
if [[ "$vpc" == vpc-* ]] && aws ec2 describe-vpcs --vpc-ids "$vpc" >/dev/null 2>&1; then
  die "The VPC $vpc is left (and whatever network interfaces keep it)"
fi
# The stack's functions' log groups, which it never owned (lib.sh).
delete_recorded_log_groups "$state/functions"
groups="$(aws logs describe-log-groups --log-group-name-prefix "/aws/eks/$cluster/" --query 'length(logGroups)' --output text)"
functions="$(recorded_log_groups "$state/functions" | sed '/^$/d' | wc -l)"
# The database went without a final snapshot and without its automated backups; its admin secret is
# deleted without a recovery window, if the stack's deletion only scheduled it.
if aws rds describe-db-instances --db-instance-identifier "$db" >/dev/null 2>&1; then
  die "The database $db is left"
fi
snapshots="$(aws rds describe-db-snapshots --db-instance-identifier "$db" --query 'length(DBSnapshots)' --output text)"
backups="$(aws rds describe-db-instance-automated-backups --filters "Name=db-instance-id,Values=$db" \
  --query 'length(DBInstanceAutomatedBackups)' --output text)"
pending="$(aws secretsmanager list-secrets --include-planned-deletion --filters "Key=name,Values=$db_secret" \
  --query 'SecretList[].ARN' --output text)"
for arn in $pending; do
  aws secretsmanager delete-secret --secret-id "$arn" --force-delete-without-recovery >/dev/null
done
secrets="$(aws secretsmanager list-secrets --include-planned-deletion --filters "Key=name,Values=$db_secret" \
  --query 'length(SecretList)' --output text)"
buckets=0
if [[ "$bucket" != None ]] && aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
  buckets=1
fi
echo "stack: deleted; VPC: deleted; load balancer resources left: $left; cluster log groups left: $groups;" \
  "provider function log groups left: $functions; database: deleted; its snapshots left: $snapshots;" \
  "its automated backups left: $backups; admin secrets left (planned deletion included): $secrets;" \
  "existing buckets left: $buckets" >"$evidence/left.txt"
[[ "$groups" -eq 0 ]] || die "The cluster's log group is left"
[[ "$functions" -eq 0 ]] || die "Log groups of the stack's functions are left"
[[ "$snapshots" -eq 0 && "$backups" -eq 0 ]] || die "Snapshots or automated backups of $db are left"
[[ "$secrets" -eq 0 ]] || die "The database's admin secret $db_secret is left"
[[ "$buckets" -eq 0 ]] || die "The existing bucket $bucket is left"
# The functions' names are of this stack, which is gone: kept with the evidence, not for the next one.
mv "$state/functions" "$evidence/functions.txt"
echo "$stack in $region is down; nothing of it is left" | tee "$evidence/RESULT"
