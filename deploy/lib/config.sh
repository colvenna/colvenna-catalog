# shellcheck shell=bash
# The install configuration (deploy/config/<env>.env; deploy/config/example.env lists every key).
# load_config <file> reads it line by line as KEY=value, never sourcing it (so nothing in it runs or
# expands), refuses keys it does not know, applies the defaults, checks every value, and exports each
# key as a shell variable of the same name. It reports every error at once, as "config: <KEY>: why",
# and exits 2. The rules match deploy/aws/src/context.ts, which checks the same values again.

CONFIG_REQUIRED=(COLVENNA_ENV COLVENNA_ACCOUNT COLVENNA_REGION COLVENNA_FORM)
# Every optional key, with its default ("" for none).
declare -A CONFIG_DEFAULTS=(
  [COLVENNA_EKS_CLUSTER]=""
  [COLVENNA_NAMESPACE]=colvenna-catalog [COLVENNA_RELEASE_NAME]=colvenna-catalog [COLVENNA_SERVICE_ACCOUNT]=colvenna-catalog
  [COLVENNA_WAREHOUSE_NAME]=colvenna [COLVENNA_BUCKET]="" [COLVENNA_WAREHOUSE_PREFIX]=warehouse
  [COLVENNA_DB]=create [COLVENNA_DB_NAME]=colvenna_catalog [COLVENNA_DB_USER]=colvenna_catalog
  [COLVENNA_DB_ENDPOINT]="" [COLVENNA_DB_PORT]=5432 [COLVENNA_DB_ADMIN_SECRET_ARN]=""
  [COLVENNA_DATA_TOKENS]=aws-sts [COLVENNA_DATA_TOKEN_DURATION]=PT3H
  [COLVENNA_NODE_SELECTOR]="" [COLVENNA_INTERNAL_NLB]=off [COLVENNA_NLB_ALLOWED_CIDRS]=""
  [COLVENNA_CLIENT_NAMESPACE_LABEL]=colvenna-catalog/client=true [COLVENNA_METRICS_NAMESPACE_LABEL]=colvenna-catalog/monitoring=true
  [COLVENNA_RELEASE_REGISTRY]=""
  [COLVENNA_NETWORK]=create [COLVENNA_VPC_CIDR]=10.80.0.0/16 [COLVENNA_VPC_ID]="" [COLVENNA_SUBNET_IDS]=""
  [COLVENNA_API_ENDPOINT_IDS]="" [COLVENNA_LAMBDA_MEMORY]=2048 [COLVENNA_LAMBDA_CONCURRENCY]=10 [COLVENNA_SNAPSTART]=off
)
# The keys of one form only. The other form refuses them when they are set to anything but their
# default, so that a setting meant for one form is never silently ignored by the other.
CONFIG_EKS_ONLY=(COLVENNA_EKS_CLUSTER COLVENNA_NAMESPACE COLVENNA_RELEASE_NAME COLVENNA_SERVICE_ACCOUNT COLVENNA_NODE_SELECTOR
  COLVENNA_INTERNAL_NLB COLVENNA_NLB_ALLOWED_CIDRS COLVENNA_CLIENT_NAMESPACE_LABEL COLVENNA_METRICS_NAMESPACE_LABEL)
CONFIG_SERVERLESS_ONLY=(COLVENNA_NETWORK COLVENNA_VPC_CIDR COLVENNA_VPC_ID COLVENNA_SUBNET_IDS COLVENNA_API_ENDPOINT_IDS
  COLVENNA_LAMBDA_MEMORY COLVENNA_LAMBDA_CONCURRENCY COLVENNA_SNAPSTART)

# config_vpc_cidr <cidr>: an IPv4 network from /16 to /24 whose host bits are clear (the serverless
# form's new VPC; deploy/aws/src/context.ts checks the same).
config_vpc_cidr() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/(1[6-9]|2[0-4])$ ]] || return 1
  local octets=("${BASH_REMATCH[@]:1:4}") prefix="${BASH_REMATCH[5]}" octet bits=0
  for octet in "${octets[@]}"; do
    ((10#$octet <= 255)) || return 1
    bits=$((bits * 256 + 10#$octet))
  done
  ((bits % (1 << (32 - prefix)) == 0))
}

# config_ids <prefix> <comma-separated>: AWS resource IDs of that kind (vpc, subnet, vpce), distinct.
config_ids() {
  local id ids=()
  IFS=, read -ra ids <<<"$2"
  ((${#ids[@]} > 0)) || return 1
  for id in "${ids[@]}"; do
    [[ "$id" =~ ^$1-([0-9a-f]{8}|[0-9a-f]{17})$ ]] || return 1
  done
  (($(printf '%s\n' "${ids[@]}" | sort -u | wc -l) == ${#ids[@]}))
}

# config_minutes <ISO-8601 duration>: its length in minutes, for PT<n>H, PT<n>M and PT<n>H<n>M; empty otherwise.
config_minutes() {
  if [[ "$1" =~ ^PT(([0-9]+)H)?(([0-9]+)M)?$ && -n "${BASH_REMATCH[1]}${BASH_REMATCH[3]}" ]]; then
    echo $((10#${BASH_REMATCH[2]:-0} * 60 + 10#${BASH_REMATCH[4]:-0}))
  fi
}

# config_cidr <cidr>: an IPv4 or IPv6 CIDR whose prefix is longer than 0.
config_cidr() {
  [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([1-9]|[12][0-9]|3[0-2])$ ]] \
    || [[ "$1" =~ ^[0-9a-fA-F]*:[0-9a-fA-F:]*/([1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])$ ]]
}

# config_check <key> <pattern> <what it must be>: load_config's value of the key, if any, matches the
# extended regular expression; otherwise an error. Reads and adds to load_config's seen and errors.
config_check() {
  [[ -z "${seen[$1]:-}" || "${seen[$1]}" =~ $2 ]] || errors+=("$1: $3, not '${seen[$1]}'")
}

# config_label <key=value>: a Kubernetes label (an optional DNS prefix and a name; a value of up to 63).
config_label() {
  [[ "$1" =~ ^([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?=([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?$ ]]
}

load_config() {
  local file="$1" line key value n=0 errors=() minutes pair cidr
  if [[ ! -f "$file" ]]; then
    echo "config: $file: no such file" >&2
    exit 2
  fi
  declare -A seen=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n + 1))
    [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
    if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      errors+=("line $n: not KEY=value")
      continue
    fi
    key="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    if [[ " ${CONFIG_REQUIRED[*]} " != *" $key "* && -z "${CONFIG_DEFAULTS[$key]+set}" ]]; then
      errors+=("$key: unknown key")
      continue
    fi
    [[ -z "${seen[$key]+set}" ]] || errors+=("$key: set twice")
    seen[$key]="$value"
  done <"$file"

  for key in "${CONFIG_REQUIRED[@]}"; do
    [[ -n "${seen[$key]:-}" ]] || errors+=("$key: required")
  done
  # What the file sets, before the defaults fill in the rest.
  declare -A given=()
  for key in "${!seen[@]}"; do
    [[ -z "${seen[$key]}" ]] || given[$key]=1
  done
  for key in "${!CONFIG_DEFAULTS[@]}"; do
    # An empty value means the default.
    [[ -n "${seen[$key]:-}" ]] || seen[$key]="${CONFIG_DEFAULTS[$key]}"
  done

  config_check COLVENNA_ENV '^[a-z][a-z0-9]{0,7}$' 'a lowercase letter, then lowercase letters and digits, 8 at most'
  [[ "${seen[COLVENNA_ENV]:-}" != release ]] || errors+=("COLVENNA_ENV: release is reserved for the release registry")
  config_check COLVENNA_ACCOUNT '^[0-9]{12}$' 'the 12-digit AWS account'
  config_check COLVENNA_REGION '^[a-z]{2}-[a-z]+-[1-9]$' 'a standard AWS region, e.g. us-west-2'
  [[ "${seen[COLVENNA_REGION]:-}" != cn-* ]] || errors+=("COLVENNA_REGION: the China partition is not supported")
  [[ "${seen[COLVENNA_REGION]:-}" != us-gov-* ]] || errors+=("COLVENNA_REGION: GovCloud is not supported")
  config_check COLVENNA_FORM '^(eks|serverless)$' 'eks or serverless'
  local form="${seen[COLVENNA_FORM]:-}" other=()
  [[ "$form" == serverless ]] && other=("${CONFIG_EKS_ONLY[@]}")
  [[ "$form" == eks ]] && other=("${CONFIG_SERVERLESS_ONLY[@]}")
  for key in "${other[@]}"; do
    if [[ -n "${given[$key]:-}" && "${seen[$key]}" != "${CONFIG_DEFAULTS[$key]}" ]]; then
      errors+=("$key: not for COLVENNA_FORM=$form")
    fi
  done
  [[ "$form" != eks || -n "${seen[COLVENNA_EKS_CLUSTER]:-}" ]] || errors+=("COLVENNA_EKS_CLUSTER: required with COLVENNA_FORM=eks")
  config_check COLVENNA_EKS_CLUSTER '^[0-9A-Za-z][A-Za-z0-9_-]{0,99}$' 'an EKS cluster name'
  if [[ "$form" == serverless ]]; then
    config_check COLVENNA_NETWORK '^(create|existing)$' 'create or existing'
    config_check COLVENNA_LAMBDA_MEMORY '^[0-9]{4,5}$' 'the memory of the server function in MB, from 1024 to 10240'
    [[ ! "${seen[COLVENNA_LAMBDA_MEMORY]}" =~ ^[0-9]+$ ]] || ((10#${seen[COLVENNA_LAMBDA_MEMORY]} >= 1024 && 10#${seen[COLVENNA_LAMBDA_MEMORY]} <= 10240)) \
      || errors+=("COLVENNA_LAMBDA_MEMORY: from 1024 to 10240, not '${seen[COLVENNA_LAMBDA_MEMORY]}'")
    config_check COLVENNA_LAMBDA_CONCURRENCY '^([1-9][0-9]{0,2}|1000)$' 'a whole number from 1 to 1000'
    config_check COLVENNA_SNAPSTART '^(on|off)$' 'on or off'
    if [[ "${seen[COLVENNA_NETWORK]}" == create ]]; then
      config_vpc_cidr "${seen[COLVENNA_VPC_CIDR]}" \
        || errors+=("COLVENNA_VPC_CIDR: an IPv4 network from /16 to /24 without host bits, not '${seen[COLVENNA_VPC_CIDR]}'")
      for key in COLVENNA_VPC_ID COLVENNA_SUBNET_IDS; do
        [[ -z "${seen[$key]}" ]] || errors+=("$key: only with COLVENNA_NETWORK=existing")
      done
      # COLVENNA_DB=existing is a database in the VPC this install made (a restored instance):
      # preflight checks where it is (sl_assert_db_reachable).
    elif [[ "${seen[COLVENNA_NETWORK]}" == existing ]]; then
      [[ "${seen[COLVENNA_VPC_CIDR]}" == "${CONFIG_DEFAULTS[COLVENNA_VPC_CIDR]}" ]] \
        || errors+=("COLVENNA_VPC_CIDR: only with COLVENNA_NETWORK=create")
      config_ids vpc "${seen[COLVENNA_VPC_ID]}" || errors+=("COLVENNA_VPC_ID: a VPC ID (vpc-...), required with COLVENNA_NETWORK=existing")
      { config_ids subnet "${seen[COLVENNA_SUBNET_IDS]}" && [[ "${seen[COLVENNA_SUBNET_IDS]}" == *,* ]]; } \
        || errors+=("COLVENNA_SUBNET_IDS: two or more distinct subnet IDs, comma-separated, required with COLVENNA_NETWORK=existing")
      [[ -n "${seen[COLVENNA_API_ENDPOINT_IDS]}" ]] \
        || errors+=("COLVENNA_API_ENDPOINT_IDS: required with COLVENNA_NETWORK=existing (the execute-api endpoints clients use)")
    fi
    [[ -z "${seen[COLVENNA_API_ENDPOINT_IDS]}" ]] || config_ids vpce "${seen[COLVENNA_API_ENDPOINT_IDS]}" \
      || errors+=("COLVENNA_API_ENDPOINT_IDS: distinct VPC endpoint IDs (vpce-...), comma-separated")
    # The authorizer's database user is <COLVENNA_DB_USER>_authz.
    ((${#seen[COLVENNA_DB_USER]} <= 57)) || errors+=("COLVENNA_DB_USER: 57 characters at most with COLVENNA_FORM=serverless")
  fi
  for key in COLVENNA_NAMESPACE COLVENNA_RELEASE_NAME COLVENNA_SERVICE_ACCOUNT; do
    config_check "$key" '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$' 'a DNS-1123 label (lowercase letters, digits and "-", 63 at most)'
  done
  config_check COLVENNA_WAREHOUSE_NAME '^[a-z0-9][a-z0-9_-]{0,62}$' 'lowercase letters, digits, "_" and "-", 63 at most'
  config_check COLVENNA_BUCKET '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$' 'an S3 bucket name, or empty to create one'
  config_check COLVENNA_WAREHOUSE_PREFIX '^[a-z0-9_-]+(/[a-z0-9_-]+)*$' 'segments of [a-z0-9_-] joined by "/", no leading or trailing "/"'
  config_check COLVENNA_DB '^(create|existing)$' 'create or existing'
  config_check COLVENNA_DB_NAME '^[a-z_][a-z0-9_]{0,62}$' 'a PostgreSQL identifier'
  config_check COLVENNA_DB_USER '^[a-z_][a-z0-9_]{0,62}$' 'a PostgreSQL identifier'
  config_check COLVENNA_DB_PORT '^[1-9][0-9]{0,4}$' 'a port number'
  config_check COLVENNA_DATA_TOKENS '^(aws-sts|disabled)$' 'aws-sts or disabled'
  config_check COLVENNA_INTERNAL_NLB '^(on|off)$' 'on or off'
  config_check COLVENNA_RELEASE_REGISTRY '^[0-9]{12}\.dkr\.ecr\.[a-z]{2}-[a-z]+-[1-9]\.amazonaws\.com$' \
    'the release registry, <account>.dkr.ecr.<region>.amazonaws.com in a standard region, or empty'
  [[ "${seen[COLVENNA_RELEASE_REGISTRY]:-}" != *.dkr.ecr.cn-* ]] \
    || errors+=("COLVENNA_RELEASE_REGISTRY: the China partition is not supported")
  config_check COLVENNA_DB_ENDPOINT '^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$' 'a host name'
  if [[ "${seen[COLVENNA_DB]}" == existing ]]; then
    [[ -n "${seen[COLVENNA_DB_ENDPOINT]}" ]] || errors+=("COLVENNA_DB_ENDPOINT: required with COLVENNA_DB=existing")
    if [[ -z "${seen[COLVENNA_DB_ADMIN_SECRET_ARN]}" ]]; then
      errors+=("COLVENNA_DB_ADMIN_SECRET_ARN: required with COLVENNA_DB=existing")
    elif [[ ! "${seen[COLVENNA_DB_ADMIN_SECRET_ARN]}" =~ ^arn:aws:secretsmanager:${seen[COLVENNA_REGION]:-x}:${seen[COLVENNA_ACCOUNT]:-x}:secret:[A-Za-z0-9/_+=.@-]+$ ]]; then
      errors+=("COLVENNA_DB_ADMIN_SECRET_ARN: a Secrets Manager secret of COLVENNA_ACCOUNT in COLVENNA_REGION")
    fi
  fi
  minutes="$(config_minutes "${seen[COLVENNA_DATA_TOKEN_DURATION]}")"
  if [[ -z "$minutes" ]] || ((minutes < 90 || minutes > 720)); then
    errors+=("COLVENNA_DATA_TOKEN_DURATION: an ISO-8601 duration between PT1H30M and PT12H (PT<n>H<n>M), not '${seen[COLVENNA_DATA_TOKEN_DURATION]}'")
  fi
  if [[ -n "${seen[COLVENNA_NODE_SELECTOR]}" ]]; then
    IFS=, read -ra pairs <<<"${seen[COLVENNA_NODE_SELECTOR]}"
    for pair in "${pairs[@]}"; do
      config_label "$pair" || errors+=("COLVENNA_NODE_SELECTOR: k=v pairs separated by commas, not '$pair'")
    done
  fi
  for key in COLVENNA_CLIENT_NAMESPACE_LABEL COLVENNA_METRICS_NAMESPACE_LABEL; do
    config_label "${seen[$key]}" || errors+=("$key: a label key=value, not '${seen[$key]}'")
  done
  # The chart admits monitoring namespaces by the label's key with the value "true".
  [[ "${seen[COLVENNA_METRICS_NAMESPACE_LABEL]}" == *=true ]] \
    || errors+=("COLVENNA_METRICS_NAMESPACE_LABEL: its value must be true, not '${seen[COLVENNA_METRICS_NAMESPACE_LABEL]#*=}'")
  if [[ "${seen[COLVENNA_INTERNAL_NLB]}" == on ]]; then
    if [[ -z "${seen[COLVENNA_NLB_ALLOWED_CIDRS]}" ]]; then
      errors+=("COLVENNA_NLB_ALLOWED_CIDRS: required with COLVENNA_INTERNAL_NLB=on")
    else
      IFS=, read -ra cidrs <<<"${seen[COLVENNA_NLB_ALLOWED_CIDRS]}"
      for cidr in "${cidrs[@]}"; do
        config_cidr "$cidr" || errors+=("COLVENNA_NLB_ALLOWED_CIDRS: CIDRs with a prefix longer than 0, not '$cidr'")
      done
    fi
  fi

  if ((${#errors[@]} > 0)); then
    printf 'config: %s\n' "${errors[@]}" >&2
    exit 2
  fi
  for key in "${!seen[@]}"; do
    export "$key=${seen[$key]}"
  done
}
