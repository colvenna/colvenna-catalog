# shellcheck shell=bash
# Preflight: what install.sh checks before it changes anything, each check refusing with its reason
# and exit status 3. Reads the configuration's COLVENNA_* variables (load_config) and STATE_DIR; exports
# what later steps use: CLUSTER_ISSUER, CLUSTER_SECURITY_GROUP, CLUSTER_VPC, CLUSTER_SUBNETS and
# CLUSTER_SUBNET_ZONES (the cluster's private subnets and their zones, in the same order), and
# ELIGIBLE_NODES. KUBECONFIG is the install's own (STATE_DIR/kubeconfig), never the user's.

refuse() {
  echo "preflight: $*" >&2
  exit 3
}

# version_at_least <version> <minimum>: dotted numbers, compared field by field.
version_at_least() {
  local IFS=. have want i
  read -ra have <<<"$1"
  read -ra want <<<"$2"
  for ((i = 0; i < ${#want[@]}; i++)); do
    ((10#${have[i]:-0} == 10#${want[i]})) || return $((10#${have[i]:-0} < 10#${want[i]}))
  done
}

# require_version <name> <minimum> <regex> <command...>: the command's output (stdout and stderr) shows,
# in the regex's first group, a version at least the minimum; refused otherwise.
require_version() {
  local name="$1" minimum="$2" pattern="$3" out line
  shift 3
  out="$("$@" 2>&1)" || out=""
  if [[ "$out" =~ $pattern ]] && version_at_least "${BASH_REMATCH[1]}" "$minimum"; then
    return 0
  fi
  # The line the version should be on (the regex up to its group), else the first.
  line="$(grep -m1 -E -- "${pattern%%(*}" <<<"$out" || head -1 <<<"$out")"
  refuse "$name $minimum or later is required; '$*' says: ${line:-nothing}"
}

# assert_tools install|release|uninstall: the commands the entry point runs, at the versions whose
# features it uses (bash 4.4's mapfile and errtrace, curl's --fail-with-body, OpenSSL's x509 -ext,
# Buildx's --provenance, the JDK the pom's release 21 needs). A release (install.sh --release) builds
# nothing, so it needs no Docker, Maven or JDK. kubectl, Helm and crane are the pinned ones in .tools.
assert_tools() {
  local tool tools=(aws jq python3 node npm git curl tar sha256sum)
  [[ "$1" == uninstall ]] || tools+=(openssl)
  [[ "$1" != install ]] || tools+=(docker mvn)
  for tool in "${tools[@]}"; do
    command -v "$tool" >/dev/null || refuse "$tool is required"
  done
  ((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 404)) || refuse "bash 4.4 or later is required, not $BASH_VERSION"
  require_version "AWS CLI" 2 '^aws-cli/([0-9.]+)' aws --version
  require_version jq 1.6 '^jq-([0-9.]+)' jq --version
  require_version Python 3.8 '^Python ([0-9.]+)' python3 --version
  require_version Node.js 20 '^v([0-9.]+)' node --version
  [[ "$1" != uninstall ]] || return 0
  require_version curl 7.76 '^curl ([0-9.]+)' curl --version
  require_version OpenSSL 1.1.1 '^OpenSSL ([0-9.]+)' openssl version
  [[ "$1" == install ]] || return 0
  require_version Maven 3.8 'Apache Maven ([0-9.]+)' mvn -B -v
  require_version Java 21 'Java version: ([0-9.]+)' mvn -B -v
  require_version "Docker Buildx" 0.10 'v?([0-9]+\.[0-9]+(\.[0-9]+)?)' docker buildx version
}

# assert_account: the caller's credentials are of COLVENNA_ACCOUNT. First, so that a wrong account changes
# nothing and reads nothing else.
assert_account() {
  local caller
  caller="$(aws sts get-caller-identity --query Account --output text)" || refuse "Cannot read the caller's identity"
  [[ "$caller" == "$COLVENNA_ACCOUNT" ]] \
    || refuse "The credentials are of account $caller; the configuration installs into $COLVENNA_ACCOUNT"
}

# cluster_facts: the cluster exists and is ACTIVE; its OIDC issuer, security group, VPC, and private
# subnets (at least two zones, where the database may be made) with their zones.
cluster_facts() {
  local out err ids subnets
  err="$(mktemp)"
  if ! out="$(aws eks describe-cluster --name "$COLVENNA_EKS_CLUSTER" --region "$COLVENNA_REGION" --output json 2>"$err")"; then
    refuse "No EKS cluster $COLVENNA_EKS_CLUSTER in $COLVENNA_REGION that these credentials can read: $(tr '\n' ' ' <"$err")"
  fi
  [[ "$(jq -r .cluster.status <<<"$out")" == ACTIVE ]] \
    || refuse "The cluster $COLVENNA_EKS_CLUSTER is $(jq -r .cluster.status <<<"$out"), not ACTIVE"
  CLUSTER_ISSUER="$(jq -r .cluster.identity.oidc.issuer <<<"$out")"
  CLUSTER_SECURITY_GROUP="$(jq -r .cluster.resourcesVpcConfig.clusterSecurityGroupId <<<"$out")"
  CLUSTER_VPC="$(jq -r .cluster.resourcesVpcConfig.vpcId <<<"$out")"
  mapfile -t ids < <(jq -r '.cluster.resourcesVpcConfig.subnetIds[]' <<<"$out")
  subnets="$(aws ec2 describe-subnets --subnet-ids "${ids[@]}" --region "$COLVENNA_REGION" --output json)"
  # Every cluster subnet's CIDR (pods may take addresses from any of them).
  CLUSTER_SUBNET_CIDRS="$(jq -r '[.Subnets[].CidrBlock] | join(",")' <<<"$subnets")"
  # The cluster's order, private subnets only (no public address on launch).
  CLUSTER_SUBNETS="" CLUSTER_SUBNET_ZONES=""
  local id zone public
  for id in "${ids[@]}"; do
    zone="$(jq -r --arg id "$id" '.Subnets[] | select(.SubnetId == $id) | .AvailabilityZone' <<<"$subnets")"
    public="$(jq -r --arg id "$id" '.Subnets[] | select(.SubnetId == $id) | .MapPublicIpOnLaunch' <<<"$subnets")"
    [[ "$public" == false ]] || continue
    CLUSTER_SUBNETS="${CLUSTER_SUBNETS:+$CLUSTER_SUBNETS,}$id"
    CLUSTER_SUBNET_ZONES="${CLUSTER_SUBNET_ZONES:+$CLUSTER_SUBNET_ZONES,}$zone"
  done
  if [[ "$COLVENNA_DB" == create ]] && (($(tr , '\n' <<<"$CLUSTER_SUBNET_ZONES" | sort -u | grep -c .) < 2)); then
    refuse "The database needs private subnets of the cluster in at least two zones; found: ${CLUSTER_SUBNET_ZONES:-none}"
  fi
  export CLUSTER_ISSUER CLUSTER_SECURITY_GROUP CLUSTER_VPC CLUSTER_SUBNETS CLUSTER_SUBNET_ZONES CLUSTER_SUBNET_CIDRS
}

# assert_oidc_provider: IAM trusts the cluster's OIDC issuer, as IRSA needs.
assert_oidc_provider() {
  local path="${CLUSTER_ISSUER#https://}"
  aws iam list-open-id-connect-providers --output json \
    | jq -e --arg path "$path" '.OpenIDConnectProviderList[] | select(.Arn | endswith("oidc-provider/" + $path))' >/dev/null \
    || refuse "IAM has no OIDC provider for the cluster's issuer ($path); create it with:" \
      "eksctl utils associate-iam-oidc-provider --cluster $COLVENNA_EKS_CLUSTER --region $COLVENNA_REGION --approve"
}

# kube_access: the install's own kubeconfig for the cluster.
kube_access() {
  mkdir -p "$STATE_DIR"
  export KUBECONFIG="$STATE_DIR/kubeconfig"
  aws eks update-kubeconfig --name "$COLVENNA_EKS_CLUSTER" --region "$COLVENNA_REGION" --kubeconfig "$KUBECONFIG" >/dev/null
}

# network_policy_enforced: something in the cluster enforces NetworkPolicy: the VPC CNI's network
# policy agent (the aws-node container whose image is aws-network-policy-agent) with
# --enable-network-policy=true, or Calico, or Cilium. Otherwise refused, unless
# ALLOW_UNENFORCED_NETWORK_POLICY=1 (install.sh --allow-unenforced-network-policy), when only the
# catalog's authentication protects it.
network_policy_enforced() {
  local sets
  sets="$(kubectl get daemonset -A -o json)"
  if jq -e '.items[] | select(.metadata.name == "aws-node") | .spec.template.spec.containers[]
        | select(.image | test("/aws-network-policy-agent:")) | .args // [] | index("--enable-network-policy=true")' \
      <<<"$sets" >/dev/null \
    || jq -e '.items[] | select(.metadata.name == "calico-node" or .metadata.name == "cilium")' <<<"$sets" >/dev/null; then
    return 0
  fi
  if [[ "${ALLOW_UNENFORCED_NETWORK_POLICY:-}" == 1 ]]; then
    echo "preflight: NetworkPolicy is not enforced in this cluster; going on as asked, with only authentication" \
      "protecting the catalog" >&2
    return 0
  fi
  refuse "NetworkPolicy is not enforced in this cluster (no VPC CNI network policy agent, Calico or Cilium):" \
    "turn on the VPC CNI add-on's enableNetworkPolicy, or rerun with --allow-unenforced-network-policy"
}

# assert_lbc: the internal NLB needs the AWS Load Balancer Controller, available.
assert_lbc() {
  [[ "$COLVENNA_INTERNAL_NLB" == on ]] || return 0
  local out
  out="$(kubectl -n kube-system get deployment aws-load-balancer-controller -o json 2>/dev/null)" \
    || refuse "COLVENNA_INTERNAL_NLB=on needs the AWS Load Balancer Controller (kube-system/aws-load-balancer-controller)"
  (($(jq -r '.status.availableReplicas // 0' <<<"$out") > 0)) \
    || refuse "The AWS Load Balancer Controller has no available replica"
}

# assert_nlb_sources_hold_no_pods: the internal NLB's allowed CIDRs overlap no subnet pods take their
# addresses from (the cluster's subnets and every node's): the NetworkPolicy admits those CIDRs to the
# API port, so an overlap would admit every pod there, whatever its namespace's labels.
assert_nlb_sources_hold_no_pods() {
  [[ "$COLVENNA_INTERNAL_NLB" == on ]] || return 0
  local vpc_subnets nodes overlaps
  vpc_subnets="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$CLUSTER_VPC" --region "$COLVENNA_REGION" --output json)"
  nodes="$(kubectl get nodes -o json | jq -r '[.items[].status.addresses[]? | select(.type == "InternalIP") | .address] | join(",")')"
  overlaps="$(python3 -c '
import ipaddress, json, sys
allowed, cluster, nodes = (x.split(",") if x else [] for x in sys.argv[1:4])
subnets = [ipaddress.ip_network(s["CidrBlock"]) for s in json.load(sys.stdin)["Subnets"]]
pods = {ipaddress.ip_network(c) for c in cluster}
pods |= {s for s in subnets for n in nodes if ipaddress.ip_address(n) in s}
for a in allowed:
    for p in sorted(pods, key=str):
        if ipaddress.ip_network(a, strict=False).overlaps(p):
            print(f"{a} overlaps {p}")
' "$COLVENNA_NLB_ALLOWED_CIDRS" "$CLUSTER_SUBNET_CIDRS" "$nodes" <<<"$vpc_subnets")"
  [[ -z "$overlaps" ]] || refuse "COLVENNA_NLB_ALLOWED_CIDRS must hold no pod address (pods take theirs from the cluster's" \
    "and the nodes' subnets): $(tr '\n' ';' <<<"$overlaps" | sed 's/;$//')"
}

# ELIGIBLE_JQ: the nodes (of `kubectl get nodes -o json`) the catalog's pods may run on, as jq: Ready, and with
# no NoSchedule or NoExecute taint of a kind the cluster's workloads put there: the chart's pods tolerate no
# taint (#35: in v1.0.0-rc.2's UAT the test cluster's tainted clients' nodes were counted, and two more of them
# changed the chart's values on a rerun). Kubernetes' own condition taints (node.kubernetes.io/*: cordoned,
# under pressure, network unavailable; not out-of-service), a node a cloud provider still initializes, and the
# autoscalers' drain marks come and go: such a node still counts, so that a passing condition does not change
# the count, as the chart's spread expects of a cordoned node (values.yaml, spreadAcrossNodes).
ELIGIBLE_JQ='[.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))
  | select(all((.spec.taints // [])[];
      (.effect != "NoSchedule" and .effect != "NoExecute")
      or ((.key | startswith("node.kubernetes.io/")) and .key != "node.kubernetes.io/out-of-service")
      or .key == "node.cloudprovider.kubernetes.io/uninitialized"
      or .key == "ToBeDeletedByClusterAutoscaler" or .key == "DeletionCandidateOfClusterAutoscaler"
      or (.key | startswith("karpenter.sh/"))))]'

# eligible_nodes: how many nodes COLVENNA_NODE_SELECTOR allows (all if empty) are eligible (ELIGIBLE_JQ): the
# chart spreads its replicas over them. Exports ELIGIBLE_NODES.
eligible_nodes() {
  local args=()
  [[ -z "$COLVENNA_NODE_SELECTOR" ]] || args=(-l "$COLVENNA_NODE_SELECTOR")
  ELIGIBLE_NODES="$(kubectl get nodes "${args[@]}" -o json | jq "$ELIGIBLE_JQ | length")"
  ((ELIGIBLE_NODES > 0)) || refuse "No Ready node matches COLVENNA_NODE_SELECTOR=${COLVENNA_NODE_SELECTOR:-<any>} without a" \
    "workload's NoSchedule or NoExecute taint (the catalog's pods tolerate none; docs/manual/install.md, Nodes)"
  export ELIGIBLE_NODES
}

# assert_bucket: a bucket the install brings exists, these credentials reach it, and it is in COLVENNA_REGION.
assert_bucket() {
  [[ -n "$COLVENNA_BUCKET" ]] || return 0
  local out location
  out="$(aws s3api get-bucket-location --bucket "$COLVENNA_BUCKET" --output json 2>&1)" \
    || refuse "Cannot reach the bucket $COLVENNA_BUCKET: $out"
  location="$(jq -r '.LocationConstraint // "us-east-1"' <<<"$out")"
  [[ "$location" == "$COLVENNA_REGION" ]] || refuse "The bucket $COLVENNA_BUCKET is in $location, not $COLVENNA_REGION"
}

# assert_db_admin_secret: an existing database's admin secret can be read and holds a username and a
# password. The value goes from the AWS CLI straight into jq, which prints only whether they are there.
assert_db_admin_secret() {
  [[ "$COLVENNA_DB" == existing ]] || return 0
  aws secretsmanager describe-secret --secret-id "$COLVENNA_DB_ADMIN_SECRET_ARN" --region "$COLVENNA_REGION" >/dev/null 2>&1 \
    || refuse "Cannot read the database admin secret $COLVENNA_DB_ADMIN_SECRET_ARN"
  aws secretsmanager get-secret-value --secret-id "$COLVENNA_DB_ADMIN_SECRET_ARN" --region "$COLVENNA_REGION" \
    --query SecretString --output text 2>/dev/null \
    | jq -e '(.username | strings | length > 0) and (.password | strings | length > 0)' >/dev/null 2>&1 \
    || refuse "The database admin secret $COLVENNA_DB_ADMIN_SECRET_ARN must hold" \
      '{"username": ..., "password": ...} (both non-empty), and these credentials must read its value'
}

# db_describe: the existing database at COLVENNA_DB_ENDPOINT, an RDS instance's endpoint or an Aurora
# cluster's writer or reader endpoint, into DB_DESCRIPTION as {kind: instance|cluster, name, cluster (an
# instance's Aurora cluster, or ""), engine, port, status, reader, id (what rds-db:connect names it by),
# iam, public, parameters (its parameter group; a cluster's own), groups}; refused when it is none of
# these, or a reader endpoint. A variable, not its output: a refusal in a command substitution would be
# the entry point's step failure (1), not 3. The Region's databases reach jq on its standard input, as a
# busy account's are more than one argument may hold. Read-only.
db_describe() {
  local db instances clusters
  instances="$(aws rds describe-db-instances --region "$COLVENNA_REGION" --output json)"
  clusters="$(aws rds describe-db-clusters --region "$COLVENNA_REGION" --output json)"
  db="$(printf '%s\n%s\n' "$instances" "$clusters" | jq -s -c --arg e "$COLVENNA_DB_ENDPOINT" '
    .[0].DBInstances as $instances
    | ([$instances[] | select(.Endpoint.Address == $e) | {kind: "instance", name: .DBInstanceIdentifier,
         cluster: (.DBClusterIdentifier // ""), engine: .Engine, port: .Endpoint.Port, status: .DBInstanceStatus,
         reader: false, id: .DbiResourceId, iam: .IAMDatabaseAuthenticationEnabled, public: (.PubliclyAccessible // false),
         parameters: (.DBParameterGroups[0].DBParameterGroupName // ""), groups: [.VpcSecurityGroups[].VpcSecurityGroupId]}]
       + [.[1].DBClusters[] | select(.Endpoint == $e or .ReaderEndpoint == $e) | .DBClusterIdentifier as $c
         | {kind: "cluster", name: $c, cluster: $c, engine: .Engine, port: .Port, status: .Status,
            reader: (.ReaderEndpoint == $e), id: .DbClusterResourceId, iam: .IAMDatabaseAuthenticationEnabled,
            public: any($instances[]; .DBClusterIdentifier == $c and (.PubliclyAccessible // false)),
            parameters: (.DBClusterParameterGroup // ""), groups: [.VpcSecurityGroups[].VpcSecurityGroupId]}])[0] // empty')"
  [[ -n "$db" ]] || refuse "COLVENNA_DB_ENDPOINT $COLVENNA_DB_ENDPOINT is no RDS instance's endpoint, nor an Aurora cluster's" \
    "writer endpoint, in $COLVENNA_REGION"
  [[ "$(jq -r .reader <<<"$db")" == false ]] \
    || refuse "COLVENNA_DB_ENDPOINT $COLVENNA_DB_ENDPOINT is an Aurora cluster's reader endpoint; the catalog writes: give the writer endpoint"
  DB_DESCRIPTION="$db"
}

# assert_db_usable: the existing database (db_describe; DB_DESCRIPTION is left for the caller) is
# PostgreSQL, on COLVENNA_DB_PORT, and available or busy; not publicly accessible; and it takes TLS
# connections only: rds.force_ssl=1 in its parameter group, or its Aurora cluster's (an Aurora instance's
# endpoint included), as in the database the storage stack makes. The catalog itself always connects with
# sslmode=verify-full; there is no setting for less. Read-only.
assert_db_usable() {
  db_describe
  local db="$DB_DESCRIPTION" name cluster group value api=describe-db-parameters group_flag=--db-parameter-group-name
  [[ "$(jq -r .engine <<<"$db")" =~ ^(postgres|aurora-postgresql)$ ]] \
    || refuse "The database at $COLVENNA_DB_ENDPOINT is $(jq -r .engine <<<"$db"), not PostgreSQL"
  [[ "$(jq -r .port <<<"$db")" == "$COLVENNA_DB_PORT" ]] \
    || refuse "The database at $COLVENNA_DB_ENDPOINT listens on port $(jq -r .port <<<"$db"), not COLVENNA_DB_PORT=$COLVENNA_DB_PORT"
  [[ "$(jq -r .status <<<"$db")" =~ ^(available|backing-up|modifying|configuring-.*|upgrading|maintenance|storage-optimization)$ ]] \
    || refuse "The database at $COLVENNA_DB_ENDPOINT is $(jq -r .status <<<"$db"); start it, or wait, and run again"
  [[ "$(jq -r .public <<<"$db")" == false ]] || refuse "The database at $COLVENNA_DB_ENDPOINT is publicly accessible; the" \
    "catalog takes a database reached from its VPC only: turn it off (aws rds modify-db-instance --region $COLVENNA_REGION" \
    "--db-instance-identifier <instance> --no-publicly-accessible --apply-immediately, for each instance of an Aurora" \
    "cluster) and run again"
  name="$(jq -r .name <<<"$db")" cluster="$(jq -r .cluster <<<"$db")" group="$(jq -r .parameters <<<"$db")"
  if [[ -n "$cluster" ]]; then
    api=describe-db-cluster-parameters group_flag=--db-cluster-parameter-group-name
    # An Aurora instance's endpoint: its cluster's parameter group sets rds.force_ssl.
    [[ "$(jq -r .kind <<<"$db")" == cluster ]] || group="$(aws rds describe-db-clusters --region "$COLVENNA_REGION" \
      --db-cluster-identifier "$cluster" --query 'DBClusters[0].DBClusterParameterGroup' --output text)" \
      || refuse "Cannot read the Aurora cluster $cluster"
  fi
  # JSON, as the CLI joins its pages (a group has several hundred parameters): with --output text it
  # would apply a --query to each page, printing a line for each.
  value="$(aws rds "$api" --region "$COLVENNA_REGION" "$group_flag" "$group" --output json)" \
    || refuse "Cannot read the parameter group $group of the database at $COLVENNA_DB_ENDPOINT"
  value="$(jq -r '[.Parameters[] | select(.ParameterName == "rds.force_ssl") | .ParameterValue // empty][0] // "unset"' <<<"$value")"
  [[ "$value" == 1 ]] || refuse "The database at $COLVENNA_DB_ENDPOINT does not require TLS: rds.force_ssl is $value in its" \
    "parameter group $group. The catalog connects with sslmode=verify-full, and takes a database that refuses connections" \
    "without TLS, as the one it makes: set rds.force_ssl=1 in a parameter group of $name's own (a default group cannot be" \
    "changed) and run again"
}

# assert_db_reachable: an existing database is an RDS PostgreSQL instance, or an Aurora PostgreSQL
# cluster's writer endpoint, usable (assert_db_usable) on COLVENNA_DB_PORT in the cluster's VPC, and its
# security groups admit that port from the catalog's nodes (the eligible ones): by one of the nodes'
# security groups, or by CIDRs holding every node's subnet, since pods connect with the nodes' groups and
# addresses from their subnets. Read-only; the database step then proves the connection itself.
assert_db_reachable() {
  [[ "$COLVENNA_DB" == existing ]] || return 0
  local db args=() nodes ids groups node_groups subnets verdict
  assert_db_usable
  db="$DB_DESCRIPTION"

  [[ -z "$COLVENNA_NODE_SELECTOR" ]] || args=(-l "$COLVENNA_NODE_SELECTOR")
  nodes="$(kubectl get nodes "${args[@]}" -o json | jq -c "$ELIGIBLE_JQ"' | [.[]
    | {id: (.spec.providerID // "" | capture("(?<id>i-[0-9a-f]+)$").id),
       ip: [.status.addresses[]? | select(.type == "InternalIP") | .address][0]}]')"
  mapfile -t ids < <(jq -r '.[].id' <<<"$nodes")
  [[ -n "${ids[*]}" ]] || refuse "No EC2 instance among the eligible nodes, whose security groups the database must admit"
  node_groups="$(aws ec2 describe-instances --instance-ids "${ids[@]}" --region "$COLVENNA_REGION" --output json \
    | jq -c '[.Reservations[].Instances[].NetworkInterfaces[].Groups[].GroupId] | unique')"
  mapfile -t ids < <(jq -r '.groups[]' <<<"$db")
  groups="$(aws ec2 describe-security-groups --group-ids "${ids[@]}" --region "$COLVENNA_REGION" --output json)"
  subnets="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$CLUSTER_VPC" --region "$COLVENNA_REGION" --output json)"
  verdict="$(jq -n --argjson groups "$groups" --argjson nodes "$nodes" --argjson node_groups "$node_groups" \
    --argjson subnets "$subnets" '{$groups, $nodes, $node_groups, $subnets}' | python3 -c '
import ipaddress, json, sys
port, vpc = int(sys.argv[1]), sys.argv[2]
d = json.load(sys.stdin)
groups = d["groups"]["SecurityGroups"]
others = sorted({g["VpcId"] for g in groups if g["VpcId"] != vpc})
if others:
    sys.exit(print("vpc " + ",".join(others)))
subnets = [ipaddress.ip_network(s["CidrBlock"]) for s in d["subnets"]["Subnets"]]
needed = {s for s in subnets for n in d["nodes"] if n["ip"] and ipaddress.ip_address(n["ip"]) in s}
cidrs, admitted = [], False
for g in groups:
    for p in g.get("IpPermissions", []):
        if p["IpProtocol"] != "-1" and not (p["IpProtocol"] == "tcp" and p["FromPort"] <= port <= p["ToPort"]):
            continue
        admitted |= any(pair.get("GroupId") in d["node_groups"] for pair in p.get("UserIdGroupPairs", []))
        cidrs += [ipaddress.ip_network(r["CidrIp"]) for r in p.get("IpRanges", [])]
admitted |= bool(needed) and all(any(s.subnet_of(c) for c in cidrs) for s in needed)
print("open" if admitted else "closed")
' "$COLVENNA_DB_PORT" "$CLUSTER_VPC")"
  case "$verdict" in
    open) ;;
    vpc\ *) refuse "The database at $COLVENNA_DB_ENDPOINT is in ${verdict#vpc }, not the cluster's VPC $CLUSTER_VPC" ;;
    *) refuse "The database's security groups ($(jq -r '.groups | join(",")' <<<"$db")) admit TCP $COLVENNA_DB_PORT neither" \
      "from the nodes' security groups ($(jq -r 'join(",")' <<<"$node_groups")) nor from CIDRs holding their subnets" ;;
  esac
}

# assert_database_kept <running>: COLVENNA_DB agrees with the database of the Storage stack, when it has
# one (an earlier install made it), and with the database the install runs on, which the function
# <running> prints (empty before the first install). Refused, as each would change the database under the
# install (user manual, 6.2):
#   COLVENNA_DB=existing without COLVENNA_BUCKET: install.sh would deploy the Storage stack again without
#     its database, deleting it, and the subnet group, security group and parameter group a restored
#     instance uses;
#   COLVENNA_DB=create while the install runs on another database (a restored instance): install.sh would
#     move it back to the stack's.
# What cannot be read fails the step: it is no proof that there is no stack or no release. Read-only.
assert_database_kept() {
  local storage stack_db running
  storage="$(stack_id Storage)"
  stack_db="$(cfn_output "$storage" DbEndpoint)"
  [[ -n "$stack_db" ]] || return 0
  if [[ "$COLVENNA_DB" == existing && -z "$COLVENNA_BUCKET" ]]; then
    refuse "COLVENNA_DB=existing without COLVENNA_BUCKET: install.sh would deploy $storage again without the database it" \
      "holds, deleting it ($stack_db). Set COLVENNA_BUCKET=$(cfn_output "$storage" BucketName), the stack's bucket, so that" \
      "it leaves the stack as it is"
  fi
  if [[ "$COLVENNA_DB" == create ]]; then
    running="$("$1")"
    [[ -z "$running" || "$running" == "$stack_db" ]] || refuse "The install runs on the database at $running, not on" \
      "$storage's ($stack_db): it was pointed at another one, a restored instance say. Keep COLVENNA_DB=existing with" \
      "COLVENNA_DB_ENDPOINT=$running; COLVENNA_DB=create would move the install back to $stack_db"
  fi
}

# eks_database_endpoint: the database host the catalog's Helm release runs on (its database.url); empty
# when there is no release yet; fails when the release cannot be read.
eks_database_endpoint() {
  local err values
  err="$(mktemp)"
  if values="$(helm get values "$COLVENNA_RELEASE_NAME" --namespace "$COLVENNA_NAMESPACE" -o json 2>"$err")"; then
    jq -r '(.database.url // "") | capture("^jdbc:postgresql://(?<host>[^:/?]+)").host // empty' <<<"$values"
  elif ! grep -q "release: not found" "$err"; then
    echo "Cannot read the Helm release $COLVENNA_RELEASE_NAME: $(tr '\n' ' ' <"$err")" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
}

# assert_cdk_bootstrap: the region is CDK-bootstrapped, as deploying the stacks needs.
assert_cdk_bootstrap() {
  aws cloudformation describe-stacks --stack-name CDKToolkit --region "$COLVENNA_REGION" \
    --query 'Stacks[0].StackStatus' --output text >/dev/null 2>&1 \
    || refuse "$COLVENNA_REGION is not CDK-bootstrapped in $COLVENNA_ACCOUNT: npx cdk bootstrap aws://$COLVENNA_ACCOUNT/$COLVENNA_REGION"
}

# preflight: every check, in order; the account first.
preflight() {
  assert_account
  assert_cdk_bootstrap
  cluster_facts
  assert_oidc_provider
  kube_access
  network_policy_enforced
  assert_lbc
  assert_nlb_sources_hold_no_pods
  eligible_nodes
  assert_bucket
  assert_db_admin_secret
  assert_db_reachable
  assert_database_kept eks_database_endpoint
}
