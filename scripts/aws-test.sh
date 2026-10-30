#!/usr/bin/env bash
# The aws-test CI job (also run by scripts/ci-local.sh): the pinned tools, the CDK project's type
# check and tests (the chart's among them), each stack synthesized (cdk-nag gating it), and helm lint
# with the values the server requires.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
"$root/scripts/install-tools.sh"
cd "$root/deploy/aws"
npm ci --no-audit --no-fund
npm run check
HELM="$root/.tools/helm" npm test
# Placeholder inputs: synthesis needs no AWS credentials and makes no lookups. Each synthesis writes
# into its own directory, cdk.out/<name>/, which keeps its templates and cdk-nag's reports
# (AwsSolutions-<stack>-NagReport.csv: every rule checked, and every acknowledged finding with its
# reason) for scripts/security/scan.sh to collect.
rm -rf cdk.out
common=(-c account=111122223333 -c region=us-west-2 -c "sourceSha=$(git rev-parse HEAD)" -c env=ci)
synth() { # <name> <cdk arguments...>
  local name=$1
  shift
  npx cdk synth --quiet --no-notices --no-version-reporting -o "cdk.out/$name" "$@" >/dev/null
}
synth eks "${common[@]}" -c stack=eks -c bucket=bucket -c warehousePrefix=warehouse \
  -c oidcIssuer=https://oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF
synth storage "${common[@]}" -c stack=storage -c createBucket=true -c createDatabase=true \
  -c vpcId=vpc-0123456789abcdef0 -c subnetIds=subnet-0123456789abcdef0,subnet-0123456789abcdef1 \
  -c subnetZones=us-west-2a,us-west-2b -c clientSecurityGroupId=sg-0123456789abcdef0
synth release "${common[@]}" -c stack=release -c pullAccounts=444455556666
# The serverless form: its network (new, and existing), its image repository, and the form itself.
synth network-create "${common[@]}" -c stack=network -c networkMode=create -c vpcCidr=10.80.0.0/16 -c subnetZones=us-west-2a,us-west-2b \
  -c databasePort=5432
synth network-existing "${common[@]}" -c stack=network -c networkMode=existing -c vpcId=vpc-0123456789abcdef0 -c vpcCidr=10.1.0.0/16 \
  -c subnetIds=subnet-0123456789abcdef0,subnet-0123456789abcdef1 -c subnetZones=us-west-2a,us-west-2b -c databasePort=5432
synth images "${common[@]}" -c stack=images
synth serverless "${common[@]}" -c stack=serverless -c vpcId=vpc-0123456789abcdef0 \
  -c subnetIds=subnet-0123456789abcdef0,subnet-0123456789abcdef1 -c subnetZones=us-west-2a,us-west-2b \
  -c securityGroupId=sg-0123456789abcdef0 -c apiEndpointIds=vpce-0123456789abcdef0 \
  -c "imageUri=111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-ci/colvenna-catalog@sha256:$(printf '1%.0s' {1..64})" \
  -c bucket=bucket -c warehousePrefix=warehouse -c warehouseName=colvenna -c databaseEndpoint=db.example.com \
  -c databasePort=5432 -c databaseName=colvenna_catalog -c databaseUser=colvenna_catalog \
  -c databaseResourceId=db-ABCDEFGHIJKLMNOPQRSTUVWXYZ -c dataTokens=aws-sts \
  -c dataTokenDuration=PT3H -c memory=2048 -c concurrency=10 -c snapStart=true
# The test environment (tests/environment): its own app, never part of the product's.
synth test-env -a 'npx ts-node test-env/app.ts' \
  -c name=ci -c account=111122223333 -c region=us-west-2 -c "sourceSha=$(git rev-parse HEAD)" \
  -c zones=us-west-2a,us-west-2b -c operatorCidr=198.51.100.7/32 \
  -c adminPrincipalArn=arn:aws:iam::111122223333:role/operator
# The serverless form's live-test driver (tests/serverless), its own app too.
synth test-driver -a 'npx ts-node test-env/driver-app.ts' \
  -c env=ci -c account=111122223333 -c region=us-west-2 -c "sourceSha=$(git rev-parse HEAD)" \
  -c vpcId=vpc-0123456789abcdef0 -c subnetIds=subnet-0123456789abcdef0,subnet-0123456789abcdef1 \
  -c subnetZones=us-west-2a,us-west-2b -c repositoryName=colvenna-catalog-ci/colvenna-catalog \
  -c "imageDigest=sha256:$(printf '0%.0s' {1..64})"
echo "synthesized: eks, storage, release, network (new and existing), images, serverless, test environment, test driver"
required=(--set image.repository=r --set image.digest=sha256:0 --set warehouse.root=s3://bucket/warehouse
  --set region=us-west-2 --set database.url=jdbc:postgresql://db/colvenna_catalog
  --set serviceAccount.roleArn=arn:aws:iam::111122223333:role/r)
"$root/.tools/helm" lint "$root/deploy/helm/colvenna-catalog" "${required[@]}"
"$root/.tools/helm" lint "$root/deploy/helm/colvenna-catalog" "${required[@]}" \
  --set internalLoadBalancer.enabled=true --set-json 'internalLoadBalancer.allowedCidrs=["10.0.0.0/16"]'
