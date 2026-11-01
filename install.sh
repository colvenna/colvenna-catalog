#!/usr/bin/env bash
# Installs Colvenna Catalog in your AWS account, or brings an install up to date: into an EKS cluster
# (COLVENNA_FORM=eks), or on Lambda behind a private REST API (COLVENNA_FORM=serverless,
# deploy/lib/serverless.sh). Run it again with the same configuration at any time, and after a failure
# or an interruption, to carry on. Every step checks before it changes anything. Exit status: 0 installed; 1 a step failed (it says
# which); 2 the arguments or the configuration are wrong, or the install was not confirmed; 3 preflight
# refused (a tool missing or too old, the tree not clean for --from-source, not the tag of the candidate or
# the release for --release, the release registry lacking it, wrong account, cluster not ready,
# NetworkPolicy not enforced, ...).
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=deploy/lib/aws.sh
source "$REPO/deploy/lib/aws.sh"
# shellcheck source=deploy/lib/config.sh
source "$REPO/deploy/lib/config.sh"
# shellcheck source=deploy/lib/preflight.sh
source "$REPO/deploy/lib/preflight.sh"
# shellcheck source=deploy/lib/helm.sh
source "$REPO/deploy/lib/helm.sh"
# shellcheck source=deploy/lib/stacks.sh
source "$REPO/deploy/lib/stacks.sh"
# shellcheck source=deploy/lib/steps.sh
source "$REPO/deploy/lib/steps.sh"
# shellcheck source=deploy/lib/catalog.sh
source "$REPO/deploy/lib/catalog.sh"
# shellcheck source=deploy/lib/release.sh
source "$REPO/deploy/lib/release.sh"
# shellcheck source=deploy/lib/serverless.sh
source "$REPO/deploy/lib/serverless.sh"

usage() {
  cat <<'USAGE'
Usage: install.sh --config <file> (--from-source | --release <name>) [--yes]
                  [--allow-unenforced-network-policy]

  --config <file>     the install's configuration (deploy/config/example.env lists every key)
  --from-source       build the catalog's image from this commit (a clean, committed tree) and install it
  --release <name>    install release candidate <name> (v1.0.0-rc.1, say), or the release promoted from
                      a GO candidate (v1.0.0), from a clean checkout of its tag: its image, copied by
                      digest from the release registry COLVENNA_RELEASE_REGISTRY (a configuration key)
                      into the environment's own repository, and its Helm chart, pulled from there by
                      digest; nothing is built. Its manifest must list the configuration's form
                      (COLVENNA_FORM)
  --yes               do not ask before changing anything
  --allow-unenforced-network-policy
                      install even though nothing in the cluster enforces NetworkPolicy; only the
                      catalog's authentication then protects it (EKS form)
  --help              this text
USAGE
}

# parse_args: the arguments. RELEASE_CANDIDATE is the name --release gives: a candidate's or a release's.
parse_args() {
  CONFIG_FILE="" SOURCE="" RELEASE_CANDIDATE="" RELEASE_CHART="" ASSUME_YES="" ALLOW_UNENFORCED_NETWORK_POLICY=""
  while (($#)); do
    case "$1" in
      --config) CONFIG_FILE="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
      --from-source) [[ -z "$SOURCE" ]] || { echo "Give one of --from-source and --release" >&2; exit 2; }; SOURCE=source; shift ;;
      --release)
        [[ -z "$SOURCE" ]] || { echo "Give one of --from-source and --release" >&2; exit 2; }
        SOURCE=release RELEASE_CANDIDATE="${2:-}"
        shift 2 || { usage >&2; exit 2; }
        ;;
      --yes) ASSUME_YES=1; shift ;;
      --allow-unenforced-network-policy) ALLOW_UNENFORCED_NETWORK_POLICY=1; shift ;;
      --help) usage; exit 0 ;;
      *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
  [[ -n "$CONFIG_FILE" ]] || { echo "--config is required" >&2; usage >&2; exit 2; }
  [[ -n "$SOURCE" ]] || { echo "Give --from-source or --release <name>" >&2; exit 2; }
  if [[ "$SOURCE" == release ]]; then
    release_tool kind "$RELEASE_CANDIDATE" >/dev/null || exit 2
  fi
  export ALLOW_UNENFORCED_NETWORK_POLICY RELEASE_CANDIDATE
}

# confirm_install: shows what is about to change and asks, unless --yes; refuses without a terminal.
confirm_install() {
  local where database
  database="$([[ "$COLVENNA_DB" == create ]] && echo "<a new RDS PostgreSQL 16>" || echo "$COLVENNA_DB_ENDPOINT:$COLVENNA_DB_PORT") ($COLVENNA_DB_NAME)"
  if [[ "$COLVENNA_FORM" == serverless ]]; then
    where="$([[ "$COLVENNA_NETWORK" == create ]] && echo "a new VPC $COLVENNA_VPC_CIDR in ${SL_ZONES:-two zones}, no internet path" \
      || echo "$COLVENNA_VPC_ID, subnets $COLVENNA_SUBNET_IDS")"
    cat <<SUMMARY
Colvenna Catalog install, serverless
  account   $COLVENNA_ACCOUNT
  region    $COLVENNA_REGION
  network   $where
  entry     a private REST API${COLVENNA_API_ENDPOINT_IDS:+ through $COLVENNA_API_ENDPOINT_IDS}$([[ "$COLVENNA_NETWORK" == create ]] && echo " (and the new VPC's endpoint)")
  server    Lambda, ${COLVENNA_LAMBDA_MEMORY} MB, at most $COLVENNA_LAMBDA_CONCURRENCY at once, SnapStart $COLVENNA_SNAPSTART
  version   $(install_version)
  bucket    ${COLVENNA_BUCKET:-<a new one>} (prefix $COLVENNA_WAREHOUSE_PREFIX)
  database  $database
SUMMARY
  else
    cat <<SUMMARY
Colvenna Catalog install
  account   $COLVENNA_ACCOUNT
  region    $COLVENNA_REGION
  cluster   $COLVENNA_EKS_CLUSTER (namespace $COLVENNA_NAMESPACE, release $COLVENNA_RELEASE_NAME)
  version   $(install_version)
  bucket    ${COLVENNA_BUCKET:-<a new one>} (prefix $COLVENNA_WAREHOUSE_PREFIX)
  database  $database
  internal NLB $COLVENNA_INTERNAL_NLB${COLVENNA_NLB_ALLOWED_CIDRS:+ for $COLVENNA_NLB_ALLOWED_CIDRS}
SUMMARY
  fi
  [[ "${ASSUME_YES:-}" != 1 ]] || return 0
  if [[ ! -t 0 ]]; then
    echo "Not confirmed: no terminal to ask on; rerun with --yes" >&2
    exit 2
  fi
  local answer
  read -r -p "Install? Type the environment name ($COLVENNA_ENV) to go on: " answer
  [[ "$answer" == "$COLVENNA_ENV" ]] || { echo "Not confirmed" >&2; exit 2; }
}

# install_version: what is installed, as the summary and the result say it.
install_version() {
  if [[ -n "${RELEASE_CANDIDATE:-}" ]]; then
    echo "release $RELEASE_CANDIDATE (built from ${SOURCE_SHA:-<its commit>}; image and chart by digest from ${COLVENNA_RELEASE_REGISTRY:-<the release registry>})"
  else
    echo "${SOURCE_SHA:-<this commit>} (from source)"
  fi
}

# build_image: the catalog's image from this commit, in the environment's repository, tagged with the
# commit; built and pushed only if that tag is not there yet (tags never move). Sets IMAGE_DIGEST.
build_image() {
  local repository="$1" registry="${1%%/*}" digest
  digest="$(aws ecr describe-images --region "$COLVENNA_REGION" --repository-name "${repository#*/}" \
    --image-ids "imageTag=$SOURCE_SHA" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
  if [[ "$digest" != sha256:* ]]; then
    (cd "$REPO" && mvn -B clean package -DskipTests >"$EVIDENCE/mvn.log" 2>&1) || { tail -30 "$EVIDENCE/mvn.log" >&2; return 1; }
    grep -qx '\[INFO\] BUILD SUCCESS' "$EVIDENCE/mvn.log" || { echo "The Maven build did not succeed" >&2; return 1; }
    aws ecr get-login-password --region "$COLVENNA_REGION" \
      | docker login --username AWS --password-stdin "$registry" >"$EVIDENCE/image.log" 2>&1 \
      || { tail -30 "$EVIDENCE/image.log" >&2; return 1; }
    docker buildx build --platform linux/amd64 --provenance=false -t "$repository:$SOURCE_SHA" --push "$REPO" \
      >>"$EVIDENCE/image.log" 2>&1 || { tail -30 "$EVIDENCE/image.log" >&2; return 1; }
    digest="$(aws ecr describe-images --region "$COLVENNA_REGION" --repository-name "${repository#*/}" \
      --image-ids "imageTag=$SOURCE_SHA" --query 'imageDetails[0].imageDigest' --output text)"
  fi
  [[ "$digest" == sha256:* ]] || { echo "No image $repository:$SOURCE_SHA after the push" >&2; return 1; }
  IMAGE_REPOSITORY="$repository" IMAGE_DIGEST="$digest"
  export IMAGE_REPOSITORY IMAGE_DIGEST
}

# upgrade_chart: the chart (this checkout's, or RELEASE_CHART, the release's package) with chart_args' values,
# every pod on the new template before it returns (a new certificate fingerprint rolls them).
upgrade_chart() {
  local listed args
  listed="$(chart_args)"
  mapfile -t args <<<"$listed"
  helm_upgrade "$COLVENNA_RELEASE_NAME" "${RELEASE_CHART:-$REPO/deploy/helm/colvenna-catalog}" --namespace "$COLVENNA_NAMESPACE" \
    --wait --timeout 10m "${args[@]}" >>"$EVIDENCE/helm.log"
  kubectl -n "$COLVENNA_NAMESPACE" rollout status "deployment/$COLVENNA_RELEASE_NAME" --timeout=10m >/dev/null
}

# install_cleanup: on the way out, whatever happened: the port-forward, the bootstrap Secret and the
# smoke namespaces a failed step may have left, and the scratch directory.
install_cleanup() {
  admin_close
  db_bootstrap_cleanup || true
  smoke_cleanup || true
  [[ -z "${WORK:-}" ]] || rm -rf "$WORK"
}

main() {
  aws_cli_retries
  parse_args "$@"
  load_config "$CONFIG_FILE"
  STATE_DIR="$REPO/.state/$COLVENNA_ENV"
  EVIDENCE="$STATE_DIR/evidence/install-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$EVIDENCE"
  export STATE_DIR EVIDENCE
  trap 'on_error install.sh' ERR
  trap install_cleanup EXIT

  if [[ -n "$RELEASE_CANDIDATE" && -z "$COLVENNA_RELEASE_REGISTRY" ]]; then
    echo "config: COLVENNA_RELEASE_REGISTRY: required with --release: the release registry that holds $RELEASE_CANDIDATE" >&2
    exit 2
  fi

  step "tools"
  assert_tools "$([[ -n "$RELEASE_CANDIDATE" ]] && echo release || echo install)"
  "$REPO/scripts/install-tools.sh" >/dev/null
  export PATH="$REPO/.tools:$PATH"
  if [[ -n "$RELEASE_CANDIDATE" ]]; then
    release_checkout "$RELEASE_CANDIDATE"
  else
    [[ -z "$(git -C "$REPO" status --porcelain)" ]] || refuse "--from-source needs a clean, committed tree"
    SOURCE_SHA="$(git -C "$REPO" rev-parse HEAD)"
    export SOURCE_SHA
  fi

  WORK="$(mktemp -d)"
  chmod 700 "$WORK"
  if [[ "$COLVENNA_FORM" == serverless ]]; then
    install_serverless
    return 0
  fi
  step "preflight"
  if [[ -n "$RELEASE_CANDIDATE" ]]; then
    # The account first, then what is installed: the release's image and chart are in the release
    # registry, and are the commit's it was built from. Then where (preflight).
    assert_account
    release_form_check eks
    release_registry_check
    release_auth
    release_verify_artifacts "$WORK/release"
  fi
  preflight
  confirm_install

  step "stacks"
  cdk_dependencies "$([[ -n "$RELEASE_CANDIDATE" ]] && echo exact || echo if-missing)"
  local common=(-c "account=$COLVENNA_ACCOUNT" -c "region=$COLVENNA_REGION" -c "sourceSha=$SOURCE_SHA" -c "env=$COLVENNA_ENV")
  local storage eks bucket="$COLVENNA_BUCKET"
  storage="$(stack_id Storage)"
  eks="$(stack_id Eks)"
  if [[ -z "$COLVENNA_BUCKET" || "$COLVENNA_DB" == create ]]; then
    deploy_stack "$storage" "${common[@]}" -c stack=storage \
      -c "createBucket=$([[ -z "$COLVENNA_BUCKET" ]] && echo true || echo false)" \
      -c "createDatabase=$([[ "$COLVENNA_DB" == create ]] && echo true || echo false)" \
      -c "vpcId=$CLUSTER_VPC" -c "subnetIds=$CLUSTER_SUBNETS" -c "subnetZones=$CLUSTER_SUBNET_ZONES" \
      -c "clientSecurityGroupId=$CLUSTER_SECURITY_GROUP"
  fi
  [[ -n "$bucket" ]] || bucket="$(stack_output "$storage" BucketName)"
  if [[ "$COLVENNA_DB" == create ]]; then
    DB_ENDPOINT="$(stack_output "$storage" DbEndpoint)"
    DB_PORT="$(stack_output "$storage" DbPort)"
    DB_ADMIN_SECRET_ARN="$(stack_output "$storage" DbAdminSecretArn)"
  else
    DB_ENDPOINT="$COLVENNA_DB_ENDPOINT" DB_PORT="$COLVENNA_DB_PORT" DB_ADMIN_SECRET_ARN="$COLVENNA_DB_ADMIN_SECRET_ARN"
  fi
  WAREHOUSE_ROOT="s3://$bucket/$COLVENNA_WAREHOUSE_PREFIX"
  deploy_stack "$eks" "${common[@]}" -c stack=eks -c "oidcIssuer=$CLUSTER_ISSUER" -c "bucket=$bucket" \
    -c "warehousePrefix=$COLVENNA_WAREHOUSE_PREFIX" -c "namespace=$COLVENNA_NAMESPACE" \
    -c "serviceAccount=$COLVENNA_SERVICE_ACCOUNT" -c "databaseUser=$COLVENNA_DB_USER"
  SERVER_ROLE_ARN="$(stack_output "$eks" ServerRoleArn)"
  DATA_ROLE_ARN="$(stack_output "$eks" DataRoleArn)"
  DB_USER_SECRET_ARN="$(stack_output "$eks" DbUserSecretArn)"
  export DB_ENDPOINT DB_PORT DB_ADMIN_SECRET_ARN WAREHOUSE_ROOT SERVER_ROLE_ARN DATA_ROLE_ARN DB_USER_SECRET_ARN

  step "image"
  local repository
  repository="$(stack_output "$eks" RepositoryUri)"
  if [[ -n "$RELEASE_CANDIDATE" ]]; then
    # The chart preflight pulled and verified is RELEASE_CHART.
    copy_release_image "$repository"
  else
    build_image "$repository"
  fi

  step "namespace"
  ensure_namespace
  step "database"
  ensure_database "$WORK"
  step "tls"
  local extra=()
  NLB_HOSTNAME="$(nlb_hostname)"
  [[ "$COLVENNA_INTERNAL_NLB" != on || -z "$NLB_HOSTNAME" ]] || extra=("$NLB_HOSTNAME")
  ensure_tls "$WORK" "${extra[@]}"
  step "chart"
  # Keep the health checks the NLB had, so a rerun does not cut them off before they are read again.
  local values
  values="$(helm get values "$COLVENNA_RELEASE_NAME" --namespace "$COLVENNA_NAMESPACE" -o json 2>/dev/null || echo '{}')"
  NLB_HEALTH_CHECK_CIDRS="$(jq -r '(.internalLoadBalancer.healthCheckCidrs // []) | join(",")' <<<"$values")"
  export NLB_HEALTH_CHECK_CIDRS
  upgrade_chart
  if [[ "$COLVENNA_INTERNAL_NLB" == on ]]; then
    step "internal NLB"
    local checked="$NLB_HEALTH_CHECK_CIDRS"
    nlb_addresses
    if [[ "$NLB_HEALTH_CHECK_CIDRS" != "$checked" ]]; then
      echo "  $NLB_HOSTNAME: its targets fail their health checks until the NetworkPolicy admits its addresses" \
        "($NLB_HEALTH_CHECK_CIDRS), and its certificate names it only after this pass; it is not ready before then"
    else
      echo "  $NLB_HOSTNAME: health checks from $NLB_HEALTH_CHECK_CIDRS, as before"
    fi
    ensure_tls "$WORK" "$NLB_HOSTNAME"
    upgrade_chart
    nlb_targets_healthy
  fi
  step "admin"
  ensure_admin
  step "smoke"
  smoke "$EVIDENCE"

  jq -n --arg sha "$SOURCE_SHA" --arg release "$RELEASE_CANDIDATE" --arg image "$IMAGE_REPOSITORY@$IMAGE_DIGEST" \
    --arg chart "$([[ -z "$RELEASE_CANDIDATE" ]] || release_value chart.digest)" --arg bucket "$bucket" \
    --arg db "$DB_ENDPOINT:$DB_PORT/$COLVENNA_DB_NAME" --arg nlb "${NLB_HOSTNAME:-}" \
    '{sourceSha: $sha, release: (if $release == "" then null else $release end), image: $image,
      chart: (if $chart == "" then "source" else $chart end), bucket: $bucket, database: $db, internalNlb: $nlb}' \
    >"$STATE_DIR/install.json"
  local installed="($SOURCE_SHA)"
  [[ -z "$RELEASE_CANDIDATE" ]] || installed="release $RELEASE_CANDIDATE (built from $SOURCE_SHA)"
  echo "Colvenna Catalog $installed is installed in $COLVENNA_EKS_CLUSTER/$COLVENNA_NAMESPACE; evidence in $EVIDENCE" \
    | tee "$EVIDENCE/RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
