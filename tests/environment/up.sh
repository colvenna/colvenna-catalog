#!/usr/bin/env bash
# Brings up the test environment <name> in <region> (deploy/aws/test-env): checks first, then the
# stack, then the AWS Load Balancer Controller, then verify.sh: what the environment must hold to be
# worth testing on (the catalogs' two nodes and the clients' nodes Ready, NetworkPolicy enforced in fact,
# anonymous requests refused by the API, and what the UAT cases need besides: the execute-api endpoint,
# the existing database and bucket, the client subnets). Rerunning it updates the stack and reinstalls
# the controller. Evidence goes to
# .state/test-<name>-<region>/evidence/up-<time>/.
# Usage: tests/environment/up.sh <name> <region>
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"
[[ $# -eq 2 ]] || die "usage: tests/environment/up.sh <name> <region>"
name="$1" region="$2"
check_args "$name" "$region"
stack="$(stack_name "$name")"
state="$(state_dir "$name" "$region")"
evidence="$state/evidence/up-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$evidence"
export AWS_REGION="$region"

# --- checks before anything is made -----------------------------------------------------------
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || die "Commit first: the stack records the commit it is of"
sha="$(git -C "$REPO" rev-parse HEAD)"
host_patch_tag
"$REPO/scripts/install-tools.sh" >/dev/null
for tool in aws node npm curl jq kubectl helm; do
  command -v "$tool" >/dev/null || die "$tool is required"
done
ensure_cdk
identity="$(aws sts get-caller-identity --output json)"
account="$(jq -r .Account <<<"$identity")"
caller="$(jq -r .Arn <<<"$identity")"
# The cluster's admin: the caller's role (its full ARN, path included), or the caller itself if a user.
if [[ "$caller" =~ ^arn:aws:sts::[0-9]{12}:assumed-role/([^/]+)/ ]]; then
  admin="$(aws iam get-role --role-name "${BASH_REMATCH[1]}" --query Role.Arn --output text)"
else
  admin="$caller"
fi
aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1 \
  || die "$region is not CDK-bootstrapped: npx cdk bootstrap aws://$account/$region"
existing="$(aws cloudformation describe-stacks --stack-name "$stack" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
if [[ -z "$existing" ]]; then
  # A new stack needs a VPC and an Elastic IP (its NAT gateway).
  vpcs="$(aws ec2 describe-vpcs --query 'length(Vpcs)' --output text)"
  vpc_quota="$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --query 'Quota.Value' --output text)"
  eips="$(aws ec2 describe-addresses --query 'length(Addresses)' --output text)"
  eip_quota="$(aws service-quotas get-service-quota --service-code ec2 --quota-code L-0263D0A3 --query 'Quota.Value' --output text)"
  (( vpcs < ${vpc_quota%.*} )) || die "No VPC left in $region: $vpcs of ${vpc_quota%.*}"
  (( eips < ${eip_quota%.*} )) || die "No Elastic IP left in $region: $eips of ${eip_quota%.*}"
fi
# Two zones that offer both node types and that EKS supports, the first two by name.
mapfile -t offered < <(aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=instance-type,Values=$CATALOG_NODE_TYPE,$CLIENT_NODE_TYPE" --output json \
  | jq -r '.InstanceTypeOfferings | group_by(.Location)[] | select(length == 2) | .[0].Location' | sort)
zones=()
for zone in "${offered[@]}"; do
  id="$(aws ec2 describe-availability-zones --zone-names "$zone" --query 'AvailabilityZones[0].ZoneId' --output text)"
  [[ " ${EKS_UNSUPPORTED_ZONE_IDS[*]} " == *" $id "* ]] || zones+=("$zone")
  [[ ${#zones[@]} -lt 2 ]] || break
done
[[ ${#zones[@]} -eq 2 ]] || die "Fewer than two zones of $region offer $CATALOG_NODE_TYPE and $CLIENT_NODE_TYPE for EKS"
operator="$(curl -fsS -m 10 https://checkip.amazonaws.com | tr -d '[:space:]')/32"
{
  echo "stack=$stack region=$region zones=${zones[0]},${zones[1]} sha=$sha"
  echo "account=$account admin=$admin operator=$operator"
} >"$evidence/params.txt"
echo "Bringing up $stack in $region (account $account), zones ${zones[*]}, API open to $operator only"

# --- the stack ----------------------------------------------------------------------------------
test_cdk "$state" deploy "$stack" --require-approval never --outputs-file "$state/outputs.json" \
  -c "name=$name" -c "account=$account" -c "region=$region" -c "sourceSha=$sha" \
  -c "zones=${zones[0]},${zones[1]}" -c "operatorCidr=$operator" -c "adminPrincipalArn=$admin" \
  >"$evidence/deploy.log" 2>&1 || { tail -40 "$evidence/deploy.log" >&2; die "cdk deploy failed"; }
cluster="$(stack_output "$region" "$stack" ClusterName)"
export KUBECONFIG="$state/kubeconfig"
aws eks update-kubeconfig --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null

# --- the AWS Load Balancer Controller -------------------------------------------------------------
# On the clients' nodes, as CoreDNS is: the catalogs' nodes keep their room for the installs.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
helm pull aws-load-balancer-controller --repo https://aws.github.io/eks-charts --version "$LBC_CHART_VERSION" \
  --destination "$work" >/dev/null
chart="$work/aws-load-balancer-controller-$LBC_CHART_VERSION.tgz"
printf '%s  %s\n' "$LBC_CHART_SHA256" "$chart" | sha256sum --check --quiet
client_placement >"$work/placement.json"
helm upgrade --install aws-load-balancer-controller "$chart" --namespace kube-system -f "$work/placement.json" \
  --set clusterName="$cluster" --set region="$region" --set vpcId="$(stack_output "$region" "$stack" VpcId)" \
  --set image.tag="v$LBC_CHART_VERSION@$LBC_IMAGE_DIGEST" \
  --set serviceAccount.create=false --set serviceAccount.name=aws-load-balancer-controller \
  --set createIngressClassResource=false --set enableShield=false --set enableWaf=false --set enableWafv2=false \
  --set resources.requests.cpu=50m --set resources.requests.memory=128Mi --set keepTLSSecret=true \
  --wait --timeout 10m >"$evidence/lbc.log"
kubectl -n kube-system rollout status deployment/aws-load-balancer-controller --timeout=5m >>"$evidence/lbc.log"

# --- what the environment must hold ---------------------------------------------------------------
"$(dirname "$0")/verify.sh" "$name" "$region" "$evidence"
