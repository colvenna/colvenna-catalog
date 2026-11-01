#!/usr/bin/env bash
# The serverless form's live test (#13), against an install of COLVENNA_FORM=serverless named <env> in
# <region> (its stacks' outputs read from CloudFormation): builds the driver (tests/serverless/driver) on the
# environment's own server image, pushes it to the environment's repository as test-driver-<hash>,
# deploys it into the catalog's VPC as ColvennaCatalogTest<Env>Driver, and invokes each action with
# IAM-signed Lambda Invoke calls (default: cycle refusals cold). The admin's token goes from Secrets
# Manager into each invocation's payload through files only readable here; no output holds it. Then it
# checks from this host, outside the VPC, that the API cannot be reached. Evidence goes to
# .state/test-driver-<env>-<region>/evidence/<time>/: one JSON file per action, cold-starts.json
# (the server's and the authorizer's Init, Restore and first-invoke durations from their log groups),
# outside-vpc.txt and RESULT. Take the driver down with tests/serverless/down.sh before uninstalling.
# Usage: AWS_PROFILE=<profile> tests/serverless/run.sh <env> <region> [cycle|refusals|cold|purges-*...|deploy]
#   ROWS (default 500): rows the cycle writes; COLD_CONCURRENCY (default 5): requests sent at once.
#   purges-setup, purges-ops, purges-verify (#93): the driver's purges phases on the database
#   PURGES_DATABASE (setup makes it, for at most PURGES_SECONDS, default 780, a call; run it until its
#   result says done; PURGES_SIZE=<main>,<retained>,<branch> makes a smaller history to try the
#   driver); run the purge function between ops and verify.
#   deploy: only the driver, no action (the UAT harness invokes it: tests/uat/uatlib/driver.py).
#   DRIVER_DATABASE_ACCESS=on: the driver also joins the catalog functions' security group and may sign
#   in to the database (resource ID DRIVER_DB_RESOURCE_ID) as DRIVER_DB_USER with RDS IAM authentication
#   (the harness's sql action).
set -euo pipefail
[[ -n "${AWS_PROFILE:-}" ]] || { echo "Set AWS_PROFILE: this script never uses a default profile" >&2; exit 2; }
[[ $# -ge 2 ]] || { echo "usage: tests/serverless/run.sh <env> <region> [cycle|refusals|cold...|deploy]" >&2; exit 2; }
env="$1" region="$2"
shift 2
actions=("$@")
((${#actions[@]} > 0)) || actions=(cycle refusals cold)
[[ "$env" =~ ^[a-z][a-z0-9]{0,7}$ ]] || { echo "env: a lowercase letter, then lowercase letters and digits, 8 at most" >&2; exit 2; }
[[ "$region" =~ ^[a-z]{2}-[a-z]+-[1-9]$ && "$region" != cn-* ]] || { echo "region: a standard AWS region" >&2; exit 2; }
for action in "${actions[@]}"; do
  [[ "$action" =~ ^(cycle|refusals|cold|deploy|purges-setup|purges-ops|purges-verify)$ ]] \
    || { echo "Unknown action $action" >&2; exit 2; }
  if [[ "$action" == purges-* && ! "${PURGES_DATABASE:-}" =~ ^[a-z][a-z0-9_]{0,63}$ ]]; then
    echo "$action needs PURGES_DATABASE, a database name" >&2
    exit 2
  fi
done
if [[ " ${actions[*]} " == *" deploy "* && ${#actions[@]} -ne 1 ]]; then
  echo "deploy is an action of its own: the driver only" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$here" rev-parse --show-toplevel)"
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region"
Env="$(tr '[:lower:]' '[:upper:]' <<<"${env:0:1}")${env:1}"
state="$REPO/.state/test-driver-$env-$region"
evidence="$state/evidence/$(date -u +%Y%m%dT%H%M%SZ)"
stack="ColvennaCatalogTest${Env}Driver"
function_name="colvenna-catalog-$env-test-driver"
mkdir -p "$evidence"
work="$(mktemp -d)"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

# shellcheck source-path=SCRIPTDIR source=../../deploy/lib/aws.sh
source "$here/../../deploy/lib/aws.sh"
# shellcheck source=../../deploy/lib/stacks.sh
source "$here/../../deploy/lib/stacks.sh"
COLVENNA_REGION="$region"
# output <stack> <key>: one of the stack's outputs, from CloudFormation (cfn_output). install.sh keeps a
# copy in its checkout's .state/<env>/, but the install may have been made from another checkout than
# this one: a previous release's own (UAT-DEP-06's S2), or the candidate's while the harness runs from a
# fix's (#48).
output() {
  local value
  value="$(cfn_output "ColvennaCatalog$Env$1" "$2")" || return 1
  [[ -n "$value" ]] || { echo "ColvennaCatalog$Env$1 has no output $2" >&2; return 1; }
  printf '%s\n' "$value"
}

[[ -z "$(git -C "$REPO" status --porcelain)" ]] || { echo "Commit first: the driver stack records the commit it is of" >&2; exit 2; }
sha="$(git -C "$REPO" rev-parse HEAD)"
account="$(aws sts get-caller-identity --query Account --output text)"
api_url="$(output Serverless ApiUrl)"
warehouse="$(output Serverless WarehouseName)"
repository="$(output Images RepositoryUri)"
[[ "$repository" == "$account.dkr.ecr.$region.amazonaws.com/"* ]] \
  || { echo "The environment's repository is not of this account and region" >&2; exit 2; }
repository_name="${repository#*/}"

# --- the driver image: the server image the environment runs, plus the driver -----------------
# The image the server runs, by digest: tagged with the commit by a source install, with the candidate's
# name by a release install (#52), and the same digest either way.
server_image="$(output Serverless ServerImage)"
base_digest="${server_image##*@}"
[[ "${server_image%@*}" == "$repository" && "$base_digest" == sha256:* ]] \
  || { echo "The server does not run an image of $repository_name by digest: $server_image" >&2; exit 1; }
"$here/driver/build.sh" "$work/image" >/dev/null
cp "$here/driver/Dockerfile" "$work/image/"
tag="test-driver-$(cat "$work/image/driver.jar" "$work/image/Dockerfile" <(echo "$base_digest") | sha256sum | cut -c1-12)"
digest="$(aws ecr describe-images --repository-name "$repository_name" --image-ids "imageTag=$tag" \
  --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
if [[ "$digest" != sha256:* ]]; then
  export DOCKER_CONFIG="$work/docker"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${repository%%/*}" >/dev/null
  # A single-platform Docker v2 manifest, as Lambda takes it: no image index, no attestations.
  docker buildx build --platform linux/amd64 --provenance=false --sbom=false --build-arg "BASE=$repository@$base_digest" \
    --output "type=registry,name=$repository:$tag,oci-mediatypes=false" "$work/image" >"$evidence/image.log" 2>&1 \
    || { tail -20 "$evidence/image.log" >&2; exit 1; }
  unset DOCKER_CONFIG
  digest="$(aws ecr describe-images --repository-name "$repository_name" --image-ids "imageTag=$tag" \
    --query 'imageDetails[0].imageDigest' --output text)"
fi
echo "driver image: $repository_name:$tag ($digest) on $base_digest" | tee "$evidence/image.txt"

# --- the driver stack -----------------------------------------------------------------------------
database_access=()
if [[ "${DRIVER_DATABASE_ACCESS:-off}" == on ]]; then
  # The database's resource ID (rds describe-db-instances' DbiResourceId) and the catalog's user.
  [[ "${DRIVER_DB_RESOURCE_ID:-}" == db-* && -n "${DRIVER_DB_USER:-}" ]] \
    || { echo "DRIVER_DATABASE_ACCESS=on needs DRIVER_DB_RESOURCE_ID (db-...) and DRIVER_DB_USER" >&2; exit 2; }
  database_access=(-c "catalogSecurityGroupId=$(output Network CatalogSecurityGroupId)" \
    -c "dbResourceId=$DRIVER_DB_RESOURCE_ID" -c "dbUser=$DRIVER_DB_USER")
fi
[[ -d "$REPO/deploy/aws/node_modules" ]] || (cd "$REPO/deploy/aws" && npm ci --no-audit --no-fund >/dev/null)
(cd "$REPO/deploy/aws" && npx cdk -a 'npx ts-node test-env/driver-app.ts' deploy "$stack" --require-approval never \
  --no-notices --no-version-reporting --output "$state/cdk.out" --outputs-file "$state/outputs.json" \
  -c "env=$env" -c "account=$account" -c "region=$region" -c "sourceSha=$sha" \
  -c "vpcId=$(output Network VpcId)" -c "subnetIds=$(output Network SubnetIds)" -c "subnetZones=$(output Network SubnetZones)" \
  -c "repositoryName=$repository_name" -c "imageDigest=$digest" "${database_access[@]}") >"$evidence/deploy.log" 2>&1 \
  || { tail -30 "$evidence/deploy.log" >&2; exit 1; }
if [[ "${actions[*]}" == deploy ]]; then
  echo "driver $function_name deployed (database access: ${DRIVER_DATABASE_ACCESS:-off})" | tee "$evidence/RESULT"
  exit 0
fi

# --- the actions ----------------------------------------------------------------------------------
(umask 077 && aws secretsmanager get-secret-value --secret-id "$(output Serverless AdminTokenSecretArn)" \
  --query SecretString --output text >"$work/token")
start_ms=$(($(date +%s) * 1000))
status=0
for action in "${actions[@]}"; do
  # The payload is made from the token file, never from the command line or the environment.
  (umask 077 && python3 -I - "$work/token" "$action" "$api_url" "$warehouse" "${ROWS:-500}" "${COLD_CONCURRENCY:-5}" \
    "${PURGES_DATABASE:-}" "${PURGES_SECONDS:-780}" "${PURGES_SIZE:-}" >"$work/$action.event" <<'PY'
import json, sys
token_file, action, uri, warehouse, rows, concurrency, database, seconds, size = sys.argv[1:]
event = {"action": action, "uri": uri, "warehouse": warehouse, "token": open(token_file).read().strip()}
event.update({"cycle": {"rows": int(rows)}, "cold": {"concurrency": int(concurrency)}}.get(action, {}))
if action.startswith("purges-"):
    event.update({"action": "purges",
                  "purges": {"phase": action[len("purges-"):], "database": database, "seconds": int(seconds)}})
    if size:
        main, retained, branch = (int(n) for n in size.split(","))
        event["purges"].update({"main": main, "retained": retained, "branch": branch})
print(json.dumps(event))
PY
  )
  aws lambda invoke --function-name "$function_name" --cli-read-timeout 960 --cli-binary-format raw-in-base64-out \
    --payload "fileb://$work/$action.event" "$work/$action.out" >"$work/$action.meta"
  rm -f "$work/$action.event"
  if grep -qF -f "$work/token" "$work/$action.out"; then
    echo "$action: the result holds the token; not kept" >&2
    status=1
    continue
  fi
  jq . "$work/$action.out" >"$evidence/$action.json" 2>/dev/null || cp "$work/$action.out" "$evidence/$action.json"
  if jq -e '.FunctionError' "$work/$action.meta" >/dev/null; then
    echo "$action: the driver failed: $(head -c 400 "$evidence/$action.json")" >&2
    status=1
  fi
done
rm -f "$work/token"

# --- cold starts, from the functions' own REPORT lines ---------------------------------------------
if [[ " ${actions[*]} " == *" cold "* ]]; then
  sleep 20
  for fn in server authorizer; do
    aws logs filter-log-events --log-group-name "/aws/lambda/colvenna-catalog-$env-$fn" --start-time "$start_ms" \
      --filter-pattern '?REPORT ?INIT_REPORT ?RESTORE_REPORT' --output json >"$work/$fn-reports.json" \
      || echo '{"events": []}' >"$work/$fn-reports.json"
  done
  python3 -I "$here/cold_starts.py" "$work/server-reports.json" "$work/authorizer-reports.json" "$evidence/cold.json" \
    >"$evidence/cold-starts.json"
fi

# --- from outside the VPC: this host -----------------------------------------------------------------
python3 -I "$here/outside.py" "$api_url" "$(output Serverless ApiEndpointUrl)" "$warehouse" >"$evidence/outside-vpc.txt" \
  || status=1
python3 -I "$here/summary.py" "$evidence" | tee "$evidence/RESULT" || status=1
exit "$status"
