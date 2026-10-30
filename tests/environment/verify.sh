#!/usr/bin/env bash
# Checks that the test environment <name> in <region> holds what tests rely on, and writes one file
# per check to <evidence-dir>: the catalogs' two nodes Ready and untainted, and the clients' nodes
# Ready, at least their node group's minimum, each with the taint that keeps catalog pods off them;
# the catalogs' nodes' room recorded (docs/uat/cases.md 3.2 counts on it), with a warning for a pod
# there that requests CPU and is neither an install's nor a DaemonSet's; the VPC CNI's network policy agent
# enabled, and NetworkPolicy enforced in fact, for pods made after the policy too (a server admitting
# one labelled namespace: a client made before, unlabelled, is stopped; one made after, labelled, gets
# through once the agent has applied the policy to it, waited for); the API refusing anonymous
# requests to every path anonymous authentication still reaches (/healthz, /livez, /readyz) and to
# /version; and what the UAT cases need besides (docs/uat/cases.md 3.1): the execute-api endpoint,
# available with private DNS, an API's name resolving to it from a pod; the existing database,
# available, private, encrypted, with IAM database authentication; the existing bucket, private; and
# the client subnets, outside the cluster's, routed to S3 and nowhere out of the VPC. up.sh runs it
# last; network-policy.sh on runs it too; it also runs on its own against a running environment.
# Exits 1 on the first check that fails.
# Usage: tests/environment/verify.sh <name> <region> <evidence-dir>
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"
[[ $# -eq 3 ]] || die "usage: tests/environment/verify.sh <name> <region> <evidence-dir>"
name="$1" region="$2" evidence="$3"
check_args "$name" "$region"
mkdir -p "$evidence"
export AWS_REGION="$region"
stack="$(stack_name "$name")"
state="$(state_dir "$name" "$region")"
cluster="$(stack_output "$region" "$stack" ClusterName)"
export KUBECONFIG="$state/kubeconfig"
aws eks update-kubeconfig --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null

# --- nodes ----------------------------------------------------------------------------------------
# Each node group's Ready nodes, as many as it is meant to have: the catalogs' two (the performance
# terms' two t3.large), the clients' as many as their group's desired size (raised by hand, say).
nodes_json="$(kubectl get nodes -o json)"
kubectl get nodes -o wide -L "$POOL_LABEL" >"$evidence/nodes.txt"
unpooled="$(jq -r --arg k "$POOL_LABEL" '[.items[] | select(.metadata.labels[$k] == null) | .metadata.name] | join(" ")' <<<"$nodes_json")"
[[ -z "$unpooled" ]] || die "Nodes of neither node group (no $POOL_LABEL label), where catalog pods could land: $unpooled"
summary=()
# <pool> <its stack output> <its instance type> <its taint wanted: how many of its Ready nodes carry it>
for row in "$CATALOG_POOL CatalogNodegroup $CATALOG_NODE_TYPE none" "$CLIENT_POOL ClientNodegroup $CLIENT_NODE_TYPE all"; do
  read -r pool output type taint <<<"$row"
  group="$(stack_output "$region" "$stack" "$output")"
  [[ "$group" != None ]] || die "$stack has no output $output: bring it up again with tests/environment/up.sh $name $region"
  described="$(aws eks describe-nodegroup --cluster-name "$cluster" --nodegroup-name "$group" --output json)" \
    || die "Cannot describe the node group $group"
  read -r minimum desired types <<<"$(jq -r '.nodegroup
    | "\(.scalingConfig.minSize) \(.scalingConfig.desiredSize) \(.instanceTypes | join(","))"' <<<"$described")"
  read -r ready tainted <<<"$(jq -r --arg k "$POOL_LABEL" --arg p "$pool" --arg c "$CLIENT_POOL" '
    [.items[] | select(.metadata.labels[$k] == $p) | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))]
    | "\(length) \([.[] | select(any(.spec.taints[]?; .key == $k and .value == $c and .effect == "NoSchedule"))] | length)"' \
    <<<"$nodes_json")"
  echo "$pool: node group $group, $types, minimum $minimum, desired $desired; Ready $ready, with the clients' taint $tainted" \
    >>"$evidence/nodes.txt"
  [[ "$types" == "$type" ]] || die "The $pool node group is of $types, not $type"
  # The catalogs' two exactly (the performance terms'); the clients' at least their group's minimum, so
  # that raising the group by hand (or a node being replaced) is no failure while the new nodes join.
  if [[ "$taint" == none ]]; then
    [[ "$desired" -eq 2 && "$ready" -eq 2 ]] || die "The catalogs' node group must have two Ready nodes: $ready of $desired"
  else
    [[ "$ready" -ge "$minimum" && "$ready" -gt 0 ]] || die "The $pool node group has $ready Ready nodes, fewer than its minimum $minimum"
  fi
  if [[ "$taint" == none ]]; then want=0; else want="$ready"; fi
  [[ "$tainted" -eq "$want" ]] || die "$tainted of the $pool node group's $ready nodes carry the clients' taint, not $want"
  summary+=("$ready $pool")
done
# On the catalogs' nodes, only the installs' pods and the DaemonSets' request CPU: what the order of
# the UAT counts on to leave each node room for a rolling update's extra pod (docs/uat/cases.md 3.2).
pods_json="$(kubectl get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json)"
room="$(jq -nr --arg k "$POOL_LABEL" --arg p "$CATALOG_POOL" --arg ck "${CATALOG_PODS%%=*}" --arg cv "${CATALOG_PODS#*=}" \
  --slurpfile nodes <(printf '%s' "$nodes_json") --slurpfile pods <(printf '%s' "$pods_json") '
  def millicores: if . == null then 0 elif endswith("m") then (.[:-1] | tonumber) else (tonumber * 1000) end;
  def cpu: [(.spec.initContainers // [] | map(.resources.requests.cpu | millicores) | max // 0),
            (.spec.containers | map(.resources.requests.cpu | millicores) | add)] | max;
  $nodes[0].items[] | select(.metadata.labels[$k] == $p) | .metadata.name as $n
  | {node: $n, allocatable: (.status.allocatable.cpu | millicores),
     pods: [$pods[0].items[] | select(.spec.nodeName == $n)
            | {name: "\(.metadata.namespace)/\(.metadata.name)", cpu: cpu,
               kind: (if any(.metadata.ownerReferences[]?; .kind == "DaemonSet") then "daemonset"
                      elif .metadata.labels[$ck] == $cv then "catalog"
                      else "other" end)}]}
  | . + {daemonsets: ([.pods[] | select(.kind == "daemonset") | .cpu] | add // 0),
         catalogs: ([.pods[] | select(.kind == "catalog") | .cpu] | add // 0),
         others: [.pods[] | select(.kind == "other" and .cpu > 0) | "\(.name) \(.cpu)m"]}
  | "\(.node): allocatable \(.allocatable)m; DaemonSets \(.daemonsets)m; catalog pods \(.catalogs)m;"
    + " others \(.others | join(", ") | if . == "" then "none" else . end);"
    + " room \(.allocatable - .daemonsets - .catalogs)m"')"
printf '%s\n' "$room" >"$evidence/catalog-nodes.txt"
# Recorded, and said, but no failure: what verify.sh proves is the environment, not what runs on it.
others="$(grep -v 'others none;' <<<"$room" || true)"
[[ -z "$others" ]] || echo "Warning: pods that are neither an install's nor a DaemonSet's request CPU on the catalogs'" \
  "nodes, and take the room a rolling update needs (docs/uat/cases.md 3.2): $others" | tee "$evidence/catalog-nodes-warning.txt" >&2

# --- NetworkPolicy ---------------------------------------------------------------------------------
# The agent is the aws-node container whose image is aws-network-policy-agent (named aws-eks-nodeagent).
agent="$(kubectl -n kube-system get daemonset aws-node -o json \
  | jq -r '.spec.template.spec.containers[] | select(.image | test("/aws-network-policy-agent:")) | .args[]' \
  | grep -c '^--enable-network-policy=true$' || true)"
echo "network policy agent with --enable-network-policy=true: $agent" >"$evidence/netpol-agent.txt"
[[ "$agent" -eq 1 ]] || die "NetworkPolicy enforcement is OFF: the VPC CNI's network policy agent is not enabled;" \
  "turn it on: tests/environment/network-policy.sh $name $region on"

# In fact, as the installs' policies work: a server on a catalogs' node, whose policy admits only the
# namespaces with a label; a client on a clients' node in a namespace without it, made before the
# policy, reaches the server, then cannot; a client in a labelled namespace, made after the policy,
# gets through once the agent has applied the policy to it (after network-policy.sh on, a minute or so).
ns=colvenna-catalog-netpol-check
client_ns=colvenna-catalog-netpol-check-client
admitted=colvenna-catalog-netpol-check/client
placement="$(client_placement)"
for n in "$ns" "$client_ns"; do
  kubectl delete namespace "$n" --ignore-not-found --wait=true >/dev/null
  kubectl create namespace "$n" >/dev/null
  kubectl label namespace "$n" pod-security.kubernetes.io/enforce=restricted >/dev/null
done
kubectl label namespace "$client_ns" "$admitted=true" >/dev/null
# client <namespace> <name>: a client pod on the clients' nodes.
client() {
  jq -n --arg ns "$1" --arg name "$2" --arg image "$BUSYBOX_IMAGE" --argjson placement "$placement" '{
    apiVersion: "v1", kind: "Pod", metadata: {name: $name, namespace: $ns},
    spec: ({securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: "RuntimeDefault"}},
            containers: [{name: "client", image: $image, command: ["sleep", "3600"],
              securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}}]}
           + $placement)}' | kubectl apply -f - >/dev/null
}
cat <<YAML | kubectl -n "$ns" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: server, labels: {app: server}}
spec:
  nodeSelector: {$POOL_LABEL: catalog}
  securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: httpd
      image: $BUSYBOX_IMAGE
      command: ["sh", "-c", "mkdir -p /tmp/www && echo ok > /tmp/www/index.html && exec httpd -f -p 8080 -h /tmp/www"]
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
      volumeMounts: [{name: tmp, mountPath: /tmp}]
  volumes: [{name: tmp, emptyDir: {}}]
---
apiVersion: v1
kind: Service
metadata: {name: server}
spec: {selector: {app: server}, ports: [{port: 8080, targetPort: 8080}]}
YAML
client "$ns" client
kubectl -n "$ns" wait --for=condition=Ready pod/server pod/client --timeout=180s >/dev/null
# fetch <namespace> <pod>: "reached" or "refused", decided inside the client pod; anything else
# (kubectl exec itself failing) is neither, and stops the check rather than count as refused.
fetch() {
  local out
  out="$(kubectl -n "$1" exec "$2" -- sh -c \
    "if wget -T 3 -qO- http://server.$ns:8080/ >/dev/null 2>&1; then echo reached; else echo refused; fi")" \
    || die "kubectl exec into the client pod $1/$2 failed"
  [[ "$out" == reached || "$out" == refused ]] || die "Unexpected answer from the client pod $1/$2: $out"
  printf '%s\n' "$out"
}
# until_answer <want> <seconds> <namespace> <pod>: fetches until the answer is <want> or the time is up;
# prints the last answer and the seconds it took.
until_answer() {
  local answer start=$SECONDS
  while :; do
    answer="$(fetch "$3" "$4")"
    if [[ "$answer" == "$1" ]] || ((SECONDS - start >= $2)); then
      break
    fi
    sleep 3
  done
  printf '%s %s\n' "$answer" "$((SECONDS - start))"
}
read -r before _ < <(until_answer reached 60 "$ns" client)
[[ "$before" == reached ]] || die "The client could not reach the server before any policy"
cat <<YAML | kubectl -n "$ns" apply -f - >/dev/null
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: admit-labelled}
spec:
  podSelector: {}
  policyTypes: ["Ingress"]
  ingress: [{from: [{namespaceSelector: {matchLabels: {$admitted: "true"}}}], ports: [{port: 8080}]}]
YAML
read -r after after_s < <(until_answer refused 180 "$ns" client)
client "$client_ns" client
kubectl -n "$client_ns" wait --for=condition=Ready pod/client --timeout=180s >/dev/null
read -r new new_s < <(until_answer reached 180 "$client_ns" client)
{
  echo "before the policy: $before"
  echo "after it, the client made before, unlabelled: $after (${after_s}s)"
  echo "a client made after it, labelled: $new (${new_s}s)"
} >"$evidence/netpol-check.txt"
# The execute-api endpoint's private DNS, from the same client: any API's name resolves to the
# endpoint's private addresses inside the VPC (outside it, to public ones).
api_name="colvenna-dns-check.execute-api.$region.amazonaws.com"
answer="$(kubectl -n "$ns" exec client -- nslookup "$api_name" 2>&1 || true)"
mapfile -t addresses < <(awk '/^Name:/ {named = 1; next} named && /^Address/ {print $NF}' <<<"$answer")
kubectl delete namespace "$ns" "$client_ns" --wait=false >/dev/null
[[ "$after" == refused ]] || die "A NetworkPolicy did not stop the client it does not admit: NetworkPolicy is not enforced"
[[ "$new" == reached ]] || die "A client the NetworkPolicy admits, made after it, was not let through in ${new_s}s"
echo "$api_name from a pod: ${addresses[*]:-nothing}" >"$evidence/api-endpoint-dns.txt"
[[ ${#addresses[@]} -gt 0 ]] || die "$api_name does not resolve from a pod: $answer"
for address in "${addresses[@]}"; do
  [[ "$address" == 10.85.* ]] || die "$api_name resolves to $address from a pod, not to the endpoint inside the VPC"
done

# --- anonymous requests ----------------------------------------------------------------------------
# EKS authenticates anonymous requests only on the health paths; there the public-info binding decides,
# and with only system:authenticated in it they get 403. /version is refused by authentication (401).
endpoint="$(aws eks describe-cluster --name "$cluster" --query cluster.endpoint --output text)"
: >"$evidence/anonymous.txt"
for path in /healthz /livez /readyz /version; do
  code="$(curl -sk -o /dev/null -w '%{http_code}' -m 10 "$endpoint$path")"
  echo "anonymous GET $path: $code" >>"$evidence/anonymous.txt"
  [[ "$code" == 401 || "$code" == 403 ]] || die "An anonymous request to $path got $code"
done

# --- what the UAT cases need besides (docs/uat/cases.md 3.1) -------------------------------------------
endpoint_id="$(stack_output "$region" "$stack" ApiEndpointId)"
read -r state dns service < <(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$endpoint_id" \
  --query 'VpcEndpoints[0].[State,PrivateDnsEnabled,ServiceName]' --output text)
echo "execute-api endpoint: $state, private DNS $dns, $service" >"$evidence/api-endpoint.txt"
[[ "${state,,}" == available && "$dns" == True && "$service" == "com.amazonaws.$region.execute-api" ]] \
  || die "The execute-api endpoint is $state with private DNS $dns ($service)"

db="$(stack_output "$region" "$stack" ExistingDbInstanceId)"
read -r status iam public encrypted version < <(aws rds describe-db-instances --db-instance-identifier "$db" \
  --query 'DBInstances[0].[DBInstanceStatus,IAMDatabaseAuthenticationEnabled,PubliclyAccessible,StorageEncrypted,EngineVersion]' \
  --output text)
echo "existing database: $status, IAM authentication $iam, public $public, encrypted $encrypted, PostgreSQL $version" \
  >"$evidence/existing-db.txt"
[[ "$status" =~ ^(available|backing-up)$ && "$iam" == True && "$public" == False && "$encrypted" == True \
  && "$version" == 16.* ]] || die "The existing database is not as UAT's E3 and S3 need it: $(cat "$evidence/existing-db.txt")"

bucket="$(stack_output "$region" "$stack" ExistingBucket)"
blocked="$(aws s3api get-public-access-block --bucket "$bucket" --query 'PublicAccessBlockConfiguration' --output json \
  | jq '[.BlockPublicAcls, .IgnorePublicAcls, .BlockPublicPolicy, .RestrictPublicBuckets] | all')"
echo "existing bucket: every public access block on: $blocked" >"$evidence/existing-bucket.txt"
[[ "$blocked" == true ]] || die "The existing bucket does not block public access"

IFS=, read -r -a clients <<<"$(stack_output "$region" "$stack" ClientSubnetIds)"
cluster_subnets="$(aws eks describe-cluster --name "$cluster" --query 'cluster.resourcesVpcConfig.subnetIds' --output text)"
tables="$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$(IFS=,; echo "${clients[*]}")" --output json)"
: >"$evidence/client-subnets.txt"
for subnet in "${clients[@]}"; do
  [[ " $cluster_subnets " != *" $subnet "* ]] || die "The client subnet $subnet is one of the cluster's"
  routes="$(jq -c --arg s "$subnet" '[.RouteTables[] | select(any(.Associations[]; .SubnetId == $s)) | .Routes[]
    | {to: (.DestinationCidrBlock // .DestinationIpv6CidrBlock // .DestinationPrefixListId),
       via: (.GatewayId // .NatGatewayId // .TransitGatewayId // .VpcPeeringConnectionId
             // .EgressOnlyInternetGatewayId // .NetworkInterfaceId // .InstanceId // "-")}]' <<<"$tables")"
  echo "$subnet: $routes" >>"$evidence/client-subnets.txt"
  jq -e 'any(.[]; (.to | startswith("pl-")) and (.via | startswith("vpce-")))' <<<"$routes" >/dev/null \
    || die "The client subnet $subnet has no route to S3 through the gateway endpoint"
  # No way out of the VPC: no default route, nothing through an internet, NAT, transit or egress-only
  # gateway, a peering, an instance or an interface. (Routes an account's GuardDuty runtime monitoring
  # adds, to link-local addresses through its own endpoint, stay inside the VPC.)
  jq -e 'all(.[]; (.to != "0.0.0.0/0" and .to != "::/0")
      and (.via | test("^(igw|nat|tgw|pcx|eigw|eni|i)-") | not))' <<<"$routes" >/dev/null \
    || die "The client subnet $subnet routes out of the VPC: $routes"
done

echo "$stack in $region: ${summary[0]} and ${summary[1]} nodes Ready, NetworkPolicy enforced (a new client let" \
  "through after ${new_s}s), anonymous requests refused on every path;" \
  "the execute-api endpoint, the existing database and bucket, and the client subnets as the UAT needs them" \
  | tee "$evidence/RESULT"
