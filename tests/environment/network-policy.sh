#!/usr/bin/env bash
# Switches NetworkPolicy enforcement in the test environment <name> in <region> off, and on again, for
# UAT-DEP-05 (install.sh refuses a cluster that does not enforce it): the VPC CNI managed add-on's
# enableNetworkPolicy, as the stack sets it (true). Each switch updates the add-on, waits for the update
# and for aws-node to roll out on every node, and reads the network policy agent's own argument back.
# Then it starts every install's catalog pods again (a rolling restart of each Deployment labelled
# app.kubernetes.io/name=colvenna-catalog, one after the other), because the agent's switch does not
# reach pods already running, as #70 observed (and this script's live check after it): off leaves
# them filtering with the eBPF programs the agent attached before, which no longer learn of new
# pods, so that a client made since is dropped.
#   off     the agent runs with --enable-network-policy=false, and the catalog pods run without the
#           programs: nothing enforces NetworkPolicy on them, or on any pod made from now on. Another pod
#           that a NetworkPolicy selected before the switch still filters as it did, with the programs
#           of that moment, until it is started again: restart it too (kubectl rollout restart) if a
#           test needs it unfiltered.
#   on      the stack's setting again; the catalog pods start again, so the agent attaches their
#           programs afresh; then tests/environment/verify.sh proves enforcement in fact, and waits
#           until it applies to pods made after the switch (a minute or so after it)
#   status  what the agent runs with, and the add-on's configuration; changes nothing
# Each restart is a rolling update: it needs room on the catalogs' nodes for one more catalog pod, as
# docs/uat/cases.md (3.2) has it when at most two installs are in the cluster.
# Only a stack named ColvennaCatalogTest<Name> and tagged Purpose=test-environment is ever switched.
# Evidence goes to .state/test-<name>-<region>/evidence/network-policy-<switch>-<time>/.
# Usage: tests/environment/network-policy.sh <name> <region> off|on|status
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"
[[ $# -eq 3 && "$3" =~ ^(off|on|status)$ ]] || die "usage: tests/environment/network-policy.sh <name> <region> off|on|status"
name="$1" region="$2" switch="$3"
check_args "$name" "$region"
stack="$(stack_name "$name")"
state="$(state_dir "$name" "$region")"
evidence="$state/evidence/network-policy-$switch-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$evidence"
export AWS_REGION="$region"
"$REPO/scripts/install-tools.sh" >/dev/null

purpose="$(aws cloudformation describe-stacks --stack-name "$stack" \
  --query "Stacks[0].Tags[?Key=='Purpose'].Value | [0]" --output text)" || die "Cannot read $stack in $region"
[[ "$purpose" == test-environment ]] || die "$stack is tagged Purpose=$purpose, not test-environment: refusing"
cluster="$(stack_output "$region" "$stack" ClusterName)"
export KUBECONFIG="$state/kubeconfig"
aws eks update-kubeconfig --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null

# agent_setting: the value of the network policy agent's --enable-network-policy (the aws-node container
# whose image is aws-network-policy-agent), as install.sh's preflight reads it.
agent_setting() {
  kubectl -n kube-system get daemonset aws-node -o json \
    | jq -r '.spec.template.spec.containers[] | select(.image | test("/aws-network-policy-agent:")) | .args // [] | .[]' \
    | sed -n 's/^--enable-network-policy=//p'
}

report() {
  local setting configuration
  setting="$(agent_setting)"
  configuration="$(aws eks describe-addon --cluster-name "$cluster" --addon-name vpc-cni \
    --query 'addon.configurationValues' --output text)"
  {
    echo "network policy agent: --enable-network-policy=${setting:-<none>}"
    echo "vpc-cni add-on configuration: $configuration"
    if [[ "$setting" == true ]]; then
      echo "NetworkPolicy enforcement: ON"
    else
      echo "NetworkPolicy enforcement: OFF; turn it on again: tests/environment/network-policy.sh $name $region on"
    fi
  } | tee "$evidence/status.txt"
}

if [[ "$switch" == status ]]; then
  report
  exit 0
fi

want=true
[[ "$switch" == on ]] || want=false
version="$(aws eks describe-addon --cluster-name "$cluster" --addon-name vpc-cni --query addon.addonVersion --output text)"
if [[ "$(agent_setting)" != "$want" ]]; then
  echo "Switching NetworkPolicy enforcement $switch in $cluster (vpc-cni $version)"
  update="$(aws eks update-addon --cluster-name "$cluster" --addon-name vpc-cni --addon-version "$version" \
    --resolve-conflicts OVERWRITE --configuration-values "{\"enableNetworkPolicy\":\"$want\"}" \
    --query update.id --output text)"
  echo "update $update: enableNetworkPolicy=$want" >"$evidence/update.txt"
  status=InProgress
  for _ in $(seq 1 90); do
    status="$(aws eks describe-update --name "$cluster" --addon-name vpc-cni --update-id "$update" \
      --query update.status --output text)"
    [[ "$status" == InProgress ]] || break
    sleep 10
  done
  echo "update status: $status" >>"$evidence/update.txt"
  [[ "$status" == Successful ]] || die "The add-on update $update is $status, not Successful"
fi
kubectl -n kube-system rollout status daemonset/aws-node --timeout=10m >"$evidence/rollout.txt"
setting="$(agent_setting)"
report
[[ "$setting" == "$want" ]] || die "The network policy agent runs with --enable-network-policy=$setting, not $want"
# Every install's catalog pods started again, one Deployment after the other.
kubectl get deployments -A -l "$CATALOG_PODS" -o json \
  | jq -r '.items[] | "\(.metadata.namespace) \(.metadata.name)"' >"$evidence/catalog-deployments.txt"
mapfile -t deployments <"$evidence/catalog-deployments.txt"
for entry in "${deployments[@]}"; do
  read -r ns deployment <<<"$entry"
  kubectl -n "$ns" rollout restart "deployment/$deployment" >/dev/null
  kubectl -n "$ns" rollout status "deployment/$deployment" --timeout=10m >>"$evidence/catalog-restart.txt" \
    || die "The catalog Deployment $ns/$deployment did not roll out again within 10 minutes"
  echo "Started the catalog pods of $ns/$deployment again"
done
[[ -s "$evidence/catalog-deployments.txt" ]] || echo "No catalog Deployment in $cluster to start again"
if [[ "$switch" == on ]]; then
  # In fact, not only by the agent's argument: a policy stops a client it does not admit again, and
  # lets one it admits, made after the switch, through.
  "$(dirname "$0")/verify.sh" "$name" "$region" "$evidence/verify"
else
  echo "Pods that a NetworkPolicy selected before the switch, other than the catalogs', still filter as they" \
    "did until they start again"
fi
