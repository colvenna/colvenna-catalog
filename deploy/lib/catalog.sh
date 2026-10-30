# shellcheck shell=bash
# The steps that install the catalog into the cluster, each one checking before it changes anything,
# so that install.sh can run again after a failure or an interruption. They read the configuration
# (COLVENNA_*), the stacks' outputs and what preflight found (see install.sh), and KUBECONFIG. Secrets go
# through files and Kubernetes Secrets only, never through arguments or logs.

# The images the install runs besides the catalog's, pinned by digest (as colvenna-lake pins them).
POSTGRES_IMAGE=postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
CURL_IMAGE=curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6
# The Secrets and ConfigMap the chart reads; their names are the chart's defaults (and colvenna-lake's).
DB_SECRET=colvenna-catalog-db
DB_CA_CONFIGMAP=colvenna-catalog-db-ca
TLS_SECRET=colvenna-catalog-tls
CA_SECRET=colvenna-catalog-ca-key
ADMIN_TOKEN_SECRET=colvenna-catalog-admin-token
# The pod that makes the catalog's database user, and the Secret that hands it the admin's password for
# as long as it runs.
DB_BOOTSTRAP=colvenna-catalog-db-bootstrap
# The admin's token and catalog grant last this long; each install renews whichever has less than a
# month left.
ADMIN_TOKEN_TTL=P90D
ADMIN_GRANT_DAYS=365
RENEW_WITHIN_DAYS=30
# How long the private CA's certificate and the server certificate last.
CA_DAYS=825
SERVER_DAYS=397

json_string() {
  jq -cn --arg v "$1" '$v'
}

# json_list <comma-separated>: a JSON array of the strings ([] for none).
json_list() {
  jq -cn --arg s "$1" '$s | if . == "" then [] else split(",") end'
}

# iso_in_days <n>: the UTC time n days from now, as YYYY-MM-DDTHH:MM:SSZ.
iso_in_days() {
  jq -rn --argjson n "$1" 'now + $n * 86400 | strftime("%Y-%m-%dT%H:%M:%SZ")'
}

# secret_file <secret> <key> <file>: one key of one of the namespace's Secrets, decoded, into the file.
secret_file() {
  kubectl -n "$COLVENNA_NAMESPACE" get secret "$1" -o "jsonpath={.data.${2//./\\.}}" | base64 -d >"$3"
}

# A jq definition: a timestamp (ISO-8601, fractions of seconds allowed, or seconds since the epoch) in
# seconds since the epoch.
JQ_EPOCH='def epoch: if type == "number" then . elif type == "string" then (sub("\\.[0-9]+"; "") | fromdateiso8601) else 0 end;'

# chart_value <key> <json>: one of chart_args' lines.
chart_value() {
  printf -- '--set-json=%s=%s\n' "$1" "$2"
}

# chart_args: the chart's values, one "--set-json=<key>=<json>" argument per line (JSON, so that commas
# and dots in values are never taken for separators). Reads IMAGE_REPOSITORY, IMAGE_DIGEST,
# WAREHOUSE_ROOT, DB_ENDPOINT, DB_PORT, SERVER_ROLE_ARN, DATA_ROLE_ARN, ELIGIBLE_NODES, SOURCE_SHA,
# TLS_FINGERPRINT, NLB_HEALTH_CHECK_CIDRS and RELEASE_CANDIDATE (if any) besides the configuration.
chart_args() {
  chart_value image.repository "$(json_string "$IMAGE_REPOSITORY")"
  chart_value image.digest "$(json_string "$IMAGE_DIGEST")"
  chart_value region "$(json_string "$COLVENNA_REGION")"
  chart_value environment "$(json_string "$COLVENNA_ENV")"
  chart_value warehouse.name "$(json_string "$COLVENNA_WAREHOUSE_NAME")"
  chart_value warehouse.root "$(json_string "$WAREHOUSE_ROOT")"
  chart_value database.url "$(json_string \
    "jdbc:postgresql://$DB_ENDPOINT:$DB_PORT/$COLVENNA_DB_NAME?sslmode=verify-full&sslrootcert=/etc/colvenna-catalog/db-ca/ca.pem")"
  chart_value databaseSecret "$(json_string "$DB_SECRET")"
  chart_value databaseCaConfigMap "$(json_string "$DB_CA_CONFIGMAP")"
  chart_value tlsSecret "$(json_string "$TLS_SECRET")"
  chart_value serviceAccount.name "$(json_string "$COLVENNA_SERVICE_ACCOUNT")"
  chart_value serviceAccount.roleArn "$(json_string "$SERVER_ROLE_ARN")"
  chart_value dataTokens.mode "$(json_string "$COLVENNA_DATA_TOKENS")"
  if [[ "$COLVENNA_DATA_TOKENS" == aws-sts ]]; then
    chart_value dataTokens.roleArn "$(json_string "$DATA_ROLE_ARN")"
    chart_value dataTokens.duration "$(json_string "$COLVENNA_DATA_TOKEN_DURATION")"
  fi
  chart_value eligibleNodes "$ELIGIBLE_NODES"
  chart_value nodeSelector "$(jq -cn --arg s "$COLVENNA_NODE_SELECTOR" \
    '$s | if . == "" then {} else split(",") | map(split("=") | {(.[0]): (.[1:] | join("="))}) | add end')"
  chart_value clientNamespaceLabel "$(jq -cn --arg k "${COLVENNA_CLIENT_NAMESPACE_LABEL%%=*}" \
    --arg v "${COLVENNA_CLIENT_NAMESPACE_LABEL#*=}" '{key: $k, value: $v}')"
  chart_value metricsClientLabel "$(json_string "${COLVENNA_METRICS_NAMESPACE_LABEL%%=*}")"
  # A new server certificate changes the pods' template, so the rollout that serves it happens with the
  # upgrade, after an interrupted run too. A release's pods also name the candidate they run.
  chart_value podAnnotations "$(jq -cn --arg s "$SOURCE_SHA" --arg t "$TLS_FINGERPRINT" --arg r "${RELEASE_CANDIDATE:-}" \
    '{"colvenna-catalog/source-sha": $s, "colvenna-catalog/tls-sha256": $t}
      + (if $r == "" then {} else {"colvenna-catalog/release": $r} end)')"
  if [[ "$COLVENNA_INTERNAL_NLB" == on ]]; then
    chart_value internalLoadBalancer.enabled true
    chart_value internalLoadBalancer.allowedCidrs "$(json_list "$COLVENNA_NLB_ALLOWED_CIDRS")"
    chart_value internalLoadBalancer.healthCheckCidrs "$(json_list "${NLB_HEALTH_CHECK_CIDRS:-}")"
  fi
}

# run_pod <namespace> <name> <manifest>: runs a pod to completion and prints its log; fails if it fails.
run_pod() {
  local namespace="$1" name="$2" manifest="$3"
  kubectl -n "$namespace" delete pod "$name" --ignore-not-found --wait=true >/dev/null
  kubectl -n "$namespace" apply -f "$manifest" >/dev/null
  if ! kubectl -n "$namespace" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=300s >/dev/null; then
    kubectl -n "$namespace" logs "$name" >&2 || true
    kubectl -n "$namespace" delete pod "$name" --ignore-not-found >/dev/null
    echo "Pod $namespace/$name did not succeed" >&2
    return 1
  fi
  kubectl -n "$namespace" logs "$name"
  kubectl -n "$namespace" delete pod "$name" --ignore-not-found >/dev/null
}

# ensure_namespace: the catalog's namespace at Pod Security restricted. The installer owns it, not the
# chart, so that uninstalling the chart never deletes what else lives there.
ensure_namespace() {
  kubectl create namespace "$COLVENNA_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace "$COLVENNA_NAMESPACE" --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
}

# ensure_database <work-dir>: the RDS CA bundle (ConfigMap), the catalog's PostgreSQL user and database
# (created or brought in line by a psql pod, as the admin user), and the Secret the chart reads the
# user from. The user's password is the database user secret's (the EKS stack's), so a rerun keeps it.
# The admin's password is in the cluster only while the pod runs (DB_BOOTSTRAP_PENDING until then; see
# db_bootstrap_cleanup). Reads DB_ENDPOINT, DB_PORT, DB_ADMIN_SECRET_ARN and DB_USER_SECRET_ARN.
ensure_database() {
  local work="$1"
  curl -fsSL "https://truststore.pki.rds.amazonaws.com/$COLVENNA_REGION/$COLVENNA_REGION-bundle.pem" -o "$work/rds-ca.pem"
  grep -q "BEGIN CERTIFICATE" "$work/rds-ca.pem" || { echo "The RDS CA bundle for $COLVENNA_REGION is not a certificate bundle" >&2; return 1; }
  kubectl -n "$COLVENNA_NAMESPACE" create configmap "$DB_CA_CONFIGMAP" --from-file=ca.pem="$work/rds-ca.pem" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  aws secretsmanager get-secret-value --region "$COLVENNA_REGION" --secret-id "$DB_ADMIN_SECRET_ARN" --query SecretString \
    --output text >"$work/admin.json"
  aws secretsmanager get-secret-value --region "$COLVENNA_REGION" --secret-id "$DB_USER_SECRET_ARN" --query SecretString \
    --output text >"$work/user.json"
  jq -j .username "$work/admin.json" >"$work/PGUSER"
  jq -j .password "$work/admin.json" >"$work/PGPASSWORD"
  jq -j .password "$work/user.json" >"$work/CATALOG_PASSWORD"
  [[ "$(jq -r .username "$work/user.json")" == "$COLVENNA_DB_USER" ]] \
    || { echo "The database user secret names $(jq -r .username "$work/user.json"), not $COLVENNA_DB_USER" >&2; return 1; }
  printf '%s' "$COLVENNA_DB_USER" >"$work/username"
  kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$DB_SECRET" \
    --from-file=username="$work/username" --from-file=password="$work/CATALOG_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  DB_BOOTSTRAP_PENDING=1
  kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$DB_BOOTSTRAP" \
    --from-file=PGUSER="$work/PGUSER" --from-file=PGPASSWORD="$work/PGPASSWORD" \
    --from-file=CATALOG_PASSWORD="$work/CATALOG_PASSWORD" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f "$work/PGPASSWORD" "$work/CATALOG_PASSWORD" "$work/admin.json" "$work/user.json"
  cat >"$work/db-bootstrap.yaml" <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $DB_BOOTSTRAP
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 70
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: psql
      image: $POSTGRES_IMAGE
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: ["ALL"]}
      envFrom:
        - secretRef: {name: $DB_BOOTSTRAP}
      env:
        - {name: PGHOST, value: "$DB_ENDPOINT"}
        - {name: PGPORT, value: "$DB_PORT"}
        - {name: PGDATABASE, value: postgres}
        - {name: PGSSLMODE, value: verify-full}
        - {name: PGSSLROOTCERT, value: /ca/ca.pem}
        - {name: CATALOG_USER, value: "$COLVENNA_DB_USER"}
        - {name: CATALOG_DATABASE, value: "$COLVENNA_DB_NAME"}
      volumeMounts:
        - {name: ca, mountPath: /ca}
      command: ["sh", "-ec"]
      args:
        - |
          # The passwords reach psql through its environment, never its arguments.
          psql -v ON_ERROR_STOP=1 <<'SQL'
          \\getenv pw CATALOG_PASSWORD
          \\getenv u CATALOG_USER
          \\getenv d CATALOG_DATABASE
          SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'u', :'pw')
            WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'u') \\gexec
          SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'u', :'pw') \\gexec
          SELECT format('CREATE DATABASE %I OWNER %I', :'d', :'u')
            WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'d') \\gexec
          SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'d') \\gexec
          SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'d', :'u') \\gexec
          SQL
          echo database-ready
  volumes:
    - name: ca
      configMap: {name: $DB_CA_CONFIGMAP}
YAML
  local log
  log="$(run_pod "$COLVENNA_NAMESPACE" "$DB_BOOTSTRAP" "$work/db-bootstrap.yaml")"
  db_bootstrap_cleanup
  grep -qx database-ready <<<"$log" || { echo "The database bootstrap did not finish: $log" >&2; return 1; }
}

# db_bootstrap_cleanup: deletes the bootstrap pod and Secret, if ensure_database left them (it failed,
# or was interrupted, before its pod finished). For install.sh's exit too.
db_bootstrap_cleanup() {
  [[ "${DB_BOOTSTRAP_PENDING:-}" == 1 ]] || return 0
  kubectl -n "$COLVENNA_NAMESPACE" delete pod "$DB_BOOTSTRAP" --ignore-not-found --wait=false >/dev/null
  kubectl -n "$COLVENNA_NAMESPACE" delete secret "$DB_BOOTSTRAP" --ignore-not-found >/dev/null
  DB_BOOTSTRAP_PENDING=""
}

# service_names: the DNS names the catalog's Service answers to inside the cluster.
service_names() {
  local s="$COLVENNA_RELEASE_NAME" n="$COLVENNA_NAMESPACE"
  printf '%s\n' "$s" "$s.$n" "$s.$n.svc" "$s.$n.svc.cluster.local" localhost
}

# The extensions of the private CA and of the server certificate. The CA's are what RFC 5280 asks of a
# CA that signs certificates (sections 4.2.1.2, 4.2.1.3 and 4.2.1.9) and what clients that check X.509
# strictly require of one: Python 3.13 and later by default (VERIFY_X509_STRICT), OpenSSL's verify
# -x509_strict. The server certificate's are those of a TLS server's certificate, by design: keyUsage
# digitalSignature, extendedKeyUsage serverAuth, its names, and the authorityKeyIdentifier strict clients
# require of a certificate that is not self-signed. The key identifiers are their keys' hashes, so a CA
# issued again from its own key keeps its identifier; the server certificate names its CA by that
# identifier alone, not by the CA certificate's serial, so that a client's copy of the CA's earlier
# certificate still matches.
# tls_extensions ca | server <subjectAltName>
tls_extensions() {
  case "$1" in
    ca) printf '%s\n' "basicConstraints = critical,CA:TRUE" "keyUsage = critical,keyCertSign,cRLSign" \
      "subjectKeyIdentifier = hash" "authorityKeyIdentifier = keyid:always" ;;
    server) printf '%s\n' "basicConstraints = critical,CA:FALSE" "keyUsage = critical,digitalSignature" \
      "extendedKeyUsage = serverAuth" "subjectAltName = $2" "subjectKeyIdentifier = hash" \
      "authorityKeyIdentifier = keyid:always" ;;
    *) return 1 ;;
  esac
}

# ca_conforms <ca.crt>: the CA certificate has the CA's extensions: a critical basicConstraints of a CA,
# keyUsage with keyCertSign, and a subjectKeyIdentifier. Installs before #58 made it without keyUsage.
ca_conforms() {
  local text
  text="$(openssl x509 -in "$1" -noout -text 2>/dev/null)" || return 1
  grep -A1 'X509v3 Basic Constraints: critical' <<<"$text" | grep -q 'CA:TRUE' \
    && grep -A1 'X509v3 Key Usage' <<<"$text" | grep -q 'Certificate Sign' \
    && grep -q 'X509v3 Subject Key Identifier' <<<"$text"
}

# server_conforms <server.crt> <ca.crt>: the server certificate has keyUsage with digitalSignature (its
# key is ECDSA: no keyEncipherment), extendedKeyUsage serverAuth and an authorityKeyIdentifier, and the
# CA's certificate verifies it strictly, for a TLS server.
server_conforms() {
  local text
  text="$(openssl x509 -in "$1" -noout -text 2>/dev/null)" || return 1
  grep -A1 'X509v3 Key Usage' <<<"$text" | grep -q 'Digital Signature' \
    && grep -A1 'X509v3 Extended Key Usage' <<<"$text" | grep -q 'TLS Web Server Authentication' \
    && grep -q 'X509v3 Authority Key Identifier' <<<"$text" \
    && openssl verify -x509_strict -purpose sslserver -CAfile "$2" "$1" >/dev/null 2>&1
}

# sign_ca <work-dir> <input> [-req]: <work-dir>/ca.crt, the CA's certificate self-signed with its key
# (<work-dir>/ca.key) and the CA's extensions, for CA_DAYS, with a new serial: from a request (-req), or
# from the CA's earlier certificate, whose name it keeps and whose extensions it drops.
sign_ca() {
  local work="$1" input="$2"
  shift 2
  openssl x509 "$@" -in "$input" -signkey "$work/ca.key" -clrext -extfile <(tls_extensions ca) \
    -set_serial "0x$(openssl rand -hex 16)" -days "$CA_DAYS" -out "$work/ca.signed" 2>/dev/null
  mv "$work/ca.signed" "$work/ca.crt"
  ca_conforms "$work/ca.crt" || { echo "openssl made the private CA without its extensions" >&2; return 1; }
}

# ensure_tls <work-dir> [extra DNS names...]: the private CA (made once, kept in the CA Secret) and a
# server certificate for the Service's names and the extra ones (the internal NLB's), each with the
# extensions of tls_extensions. A CA certificate without them (an install before #58) is issued again from
# the same key and name: what either certificate signs verifies with the other, so clients that trust the
# earlier one keep trusting the server, and clients that check X.509 strictly need the new one. The server
# certificate is made again when it lacks a name or an extension, the CA does not verify it, its key store
# holds another certificate of the CA's, or it has less than a month left. Exports TLS_FINGERPRINT, the
# SHA-256 of the certificate the TLS Secret then holds (chart_args puts it on the pods).
ensure_tls() {
  local work="$1" names=() name sans have="" listed
  shift
  listed="$(service_names)"
  mapfile -t names <<<"$listed"
  names+=("$@")
  if kubectl -n "$COLVENNA_NAMESPACE" get secret "$CA_SECRET" >/dev/null 2>&1; then
    secret_file "$CA_SECRET" ca.key "$work/ca.key"
    secret_file "$CA_SECRET" ca.crt "$work/ca.crt"
    if ! ca_conforms "$work/ca.crt"; then
      sign_ca "$work" "$work/ca.crt"
      kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$CA_SECRET" \
        --from-file=ca.key="$work/ca.key" --from-file=ca.crt="$work/ca.crt" --dry-run=client -o yaml \
        | kubectl replace -f - >/dev/null
      echo "  the private CA's certificate lacked keyUsage, which clients that check X.509 strictly refuse:" \
        "issued again from the same key and name. Clients keep trusting the server with the earlier copy;" \
        "give clients that check strictly (Python 3.13 and later) the new ca.crt"
    fi
  else
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=colvenna-catalog private CA" \
      -keyout "$work/ca.key" -out "$work/ca.csr" 2>/dev/null
    sign_ca "$work" "$work/ca.csr" -req
    kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$CA_SECRET" \
      --from-file=ca.key="$work/ca.key" --from-file=ca.crt="$work/ca.crt" >/dev/null
  fi
  # The certificate there now: kept if the CA signed it with every extension, it names every name and
  # lasts another 30 days, and its key store's chain is the CA's certificate of now.
  if kubectl -n "$COLVENNA_NAMESPACE" get secret "$TLS_SECRET" >/dev/null 2>&1; then
    secret_file "$TLS_SECRET" keystore.p12 "$work/current.p12"
    secret_file "$TLS_SECRET" password "$work/current-password"
    if openssl pkcs12 -in "$work/current.p12" -passin "file:$work/current-password" -nokeys -clcerts 2>/dev/null \
      | openssl x509 -out "$work/current.crt" 2>/dev/null \
      && openssl pkcs12 -in "$work/current.p12" -passin "file:$work/current-password" -nokeys -cacerts 2>/dev/null \
      | openssl x509 -out "$work/current-ca.crt" 2>/dev/null \
      && [[ "$(certificate_sha256 "$work/current-ca.crt")" == "$(certificate_sha256 "$work/ca.crt")" ]] \
      && server_conforms "$work/current.crt" "$work/ca.crt" \
      && openssl x509 -in "$work/current.crt" -noout -checkend $((RENEW_WITHIN_DAYS * 86400)) >/dev/null; then
      have="$(openssl x509 -in "$work/current.crt" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/^ *DNS://p')"
      local missing=0
      for name in "${names[@]}"; do
        grep -qx "$name" <<<"$have" || missing=1
      done
      if ((!missing)); then
        rm -f "$work/ca.key" "$work/current-password"
        TLS_FINGERPRINT="$(certificate_sha256 "$work/current.crt")"
        export TLS_FINGERPRINT
        return 0
      fi
    fi
  fi
  sans="$(printf 'DNS:%s,' "${names[@]}")"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -subj "/CN=$COLVENNA_RELEASE_NAME.$COLVENNA_NAMESPACE.svc" -keyout "$work/server.key" -out "$work/server.csr" 2>/dev/null
  openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -days "$SERVER_DAYS" \
    -out "$work/server.crt" -extfile <(tls_extensions server "${sans%,}") 2>/dev/null
  server_conforms "$work/server.crt" "$work/ca.crt" \
    || { echo "openssl made the server certificate without its extensions, or the CA does not verify it" >&2; return 1; }
  # No trailing newline: the Secret's value is the key store's password verbatim.
  openssl rand -hex 24 | tr -d '\n' >"$work/keystore-password"
  openssl pkcs12 -export -in "$work/server.crt" -inkey "$work/server.key" -certfile "$work/ca.crt" \
    -name colvenna-catalog -out "$work/keystore.p12" -passout "file:$work/keystore-password"
  kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$TLS_SECRET" \
    --from-file=keystore.p12="$work/keystore.p12" --from-file=password="$work/keystore-password" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f "$work/server.key" "$work/keystore-password" "$work/ca.key" "$work/current-password"
  TLS_FINGERPRINT="$(certificate_sha256 "$work/server.crt")"
  export TLS_FINGERPRINT
}

# certificate_sha256 <pem>: the SHA-256 of the certificate's DER encoding, in hex.
certificate_sha256() {
  local der
  der="$(mktemp)"
  openssl x509 -in "$1" -outform der -out "$der"
  sha256sum "$der" | cut -d' ' -f1
  rm -f "$der"
}

# nlb_hostname: the DNS name of the internal NLB of <release>-nlb; empty while there is none.
nlb_hostname() {
  kubectl -n "$COLVENNA_NAMESPACE" get service "$COLVENNA_RELEASE_NAME-nlb" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true
}

# nlb_arn <hostname>: the ARN of the load balancer with that DNS name, or None.
nlb_arn() {
  aws elbv2 describe-load-balancers --region "$COLVENNA_REGION" \
    --query "LoadBalancers[?DNSName=='$1'].LoadBalancerArn | [0]" --output text
}

# nlb_addresses: waits for the internal NLB of <release>-nlb and its network interfaces, then sets
# NLB_HOSTNAME, NLB_ARN and NLB_HEALTH_CHECK_CIDRS (each interface's address as a /32, comma-separated).
nlb_addresses() {
  local arn name id ips=() listed
  NLB_HOSTNAME=""
  for _ in $(seq 1 60); do
    NLB_HOSTNAME="$(nlb_hostname)"
    [[ -z "$NLB_HOSTNAME" ]] || break
    sleep 10
  done
  [[ -n "$NLB_HOSTNAME" ]] || { echo "The internal NLB of $COLVENNA_RELEASE_NAME-nlb did not come up in 10 minutes" >&2; return 1; }
  arn="$(nlb_arn "$NLB_HOSTNAME")"
  [[ "$arn" == arn:* ]] || { echo "No load balancer has the DNS name $NLB_HOSTNAME" >&2; return 1; }
  # An NLB's network interfaces are described "ELB net/<name>/<id>", one per zone it serves.
  name="${arn#*:loadbalancer/net/}"
  id="${name#*/}" name="${name%%/*}"
  local zones
  zones="$(aws elbv2 describe-load-balancers --region "$COLVENNA_REGION" --load-balancer-arns "$arn" \
    --query 'length(LoadBalancers[0].AvailabilityZones)' --output text)"
  for _ in $(seq 1 30); do
    listed="$(aws ec2 describe-network-interfaces --region "$COLVENNA_REGION" \
      --filters "Name=description,Values=ELB net/$name/$id" --query 'NetworkInterfaces[].PrivateIpAddress' --output text)"
    ips=()
    [[ -z "$listed" ]] || read -ra ips <<<"$listed"
    [[ ${#ips[@]} -lt "$zones" ]] || break
    sleep 10
  done
  [[ ${#ips[@]} -ge "$zones" && ${#ips[@]} -gt 0 ]] || { echo "The NLB's network interfaces did not appear" >&2; return 1; }
  NLB_HEALTH_CHECK_CIDRS="$(printf '%s/32,' "${ips[@]}")"
  NLB_HEALTH_CHECK_CIDRS="${NLB_HEALTH_CHECK_CIDRS%,}"
  NLB_ARN="$arn"
  export NLB_HOSTNAME NLB_ARN NLB_HEALTH_CHECK_CIDRS
}

# nlb_targets_healthy: every target of the internal NLB (NLB_ARN) is healthy (10 minutes at most).
nlb_targets_healthy() {
  local listed groups=() group unhealthy count
  listed="$(aws elbv2 describe-target-groups --region "$COLVENNA_REGION" --load-balancer-arn "$NLB_ARN" \
    --query 'TargetGroups[].TargetGroupArn' --output text)"
  [[ -z "$listed" || "$listed" == None ]] || read -ra groups <<<"$listed"
  ((${#groups[@]} > 0)) || { echo "The internal NLB has no target group" >&2; return 1; }
  for _ in $(seq 1 60); do
    unhealthy=0
    for group in "${groups[@]}"; do
      count="$(aws elbv2 describe-target-health --region "$COLVENNA_REGION" --target-group-arn "$group" \
        --query "length(TargetHealthDescriptions[?TargetHealth.State!='healthy'])" --output text)"
      unhealthy=$((unhealthy + count))
    done
    ((unhealthy > 0)) || return 0
    sleep 10
  done
  echo "The internal NLB's targets are not all healthy after 10 minutes" >&2
  return 1
}

# --- the admin API, over a port-forward to the Service (IAM-authenticated, RBAC-checked) -----------
# admin_open: a port-forward and a directory (ADMIN_DIR) with the CA and the admin's token; admin_close
# removes both, and is safe to call at any time (install.sh's exit calls it).
admin_open() {
  ADMIN_DIR="$(mktemp -d)"
  chmod 700 "$ADMIN_DIR"
  ADMIN_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  secret_file "$CA_SECRET" ca.crt "$ADMIN_DIR/ca.crt"
  secret_file "$ADMIN_TOKEN_SECRET" token "$ADMIN_DIR/token"
  printf 'header = "Authorization: Bearer %s"\n' "$(cat "$ADMIN_DIR/token")" >"$ADMIN_DIR/curlrc"
  kubectl -n "$COLVENNA_NAMESPACE" port-forward "svc/$COLVENNA_RELEASE_NAME" "$ADMIN_PORT:8181" >"$ADMIN_DIR/port-forward.log" 2>&1 &
  ADMIN_FORWARD_PID=$!
  for _ in $(seq 1 30); do
    kill -0 "$ADMIN_FORWARD_PID" 2>/dev/null || { echo "port-forward exited: $(cat "$ADMIN_DIR/port-forward.log")" >&2; return 1; }
    curl -s -o /dev/null --cacert "$ADMIN_DIR/ca.crt" "https://localhost:$ADMIN_PORT/" && return 0
    sleep 1
  done
  echo "port-forward to $COLVENNA_RELEASE_NAME did not come up" >&2
  return 1
}

admin_close() {
  if [[ -n "${ADMIN_FORWARD_PID:-}" ]]; then
    kill "$ADMIN_FORWARD_PID" 2>/dev/null || true
    wait "$ADMIN_FORWARD_PID" 2>/dev/null || true
  fi
  [[ -z "${ADMIN_DIR:-}" ]] || rm -rf "$ADMIN_DIR"
  ADMIN_FORWARD_PID="" ADMIN_DIR=""
}

# admin_call <method> <path> [json-body]: prints the response body; fails on HTTP >= 400.
admin_call() {
  local args=(-sS --fail-with-body --cacert "$ADMIN_DIR/ca.crt" -K "$ADMIN_DIR/curlrc" -X "$1")
  [[ -z "${3:-}" ]] || args+=(-H 'Content-Type: application/json' --data "$3")
  curl "${args[@]}" "https://localhost:$ADMIN_PORT$2"
}

# admin_status <method> <path>: the HTTP status only.
admin_status() {
  curl -s -o /dev/null -w '%{http_code}' --cacert "$ADMIN_DIR/ca.crt" -K "$ADMIN_DIR/curlrc" -X "$1" "https://localhost:$ADMIN_PORT$2"
}

# issue_admin_token: a new admin token from the server itself (bootstrap-admin, in a catalog pod),
# straight into the admin token Secret; the token is written and removed inside the pod, never printed.
issue_admin_token() {
  local pod token
  pod="$(kubectl -n "$COLVENNA_NAMESPACE" get pods -l app.kubernetes.io/name=colvenna-catalog,app.kubernetes.io/instance="$COLVENNA_RELEASE_NAME" \
    --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
  [[ -n "$pod" ]] || { echo "No running catalog pod to issue the admin token in" >&2; return 1; }
  kubectl -n "$COLVENNA_NAMESPACE" exec "$pod" -- sh -c 'rm -f /tmp/admin-token' >/dev/null
  kubectl -n "$COLVENNA_NAMESPACE" exec "$pod" -- java -cp '/opt/colvenna-catalog/colvenna-catalog.jar:/opt/colvenna-catalog/lib/*' \
    com.colvenna.catalog.Main bootstrap-admin admin "$ADMIN_TOKEN_TTL" /tmp/admin-token >/dev/null
  token="$(mktemp)"
  # shellcheck disable=SC2016  # expanded by the pod's shell
  kubectl -n "$COLVENNA_NAMESPACE" exec "$pod" -- sh -c 'cat /tmp/admin-token; s=$?; rm -f /tmp/admin-token; exit $s' >"$token"
  [[ -s "$token" ]] || { rm -f "$token"; echo "bootstrap-admin wrote no token" >&2; return 1; }
  kubectl -n "$COLVENNA_NAMESPACE" create secret generic "$ADMIN_TOKEN_SECRET" --from-file=token="$token" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f "$token"
}

# admin_token_held: the admin's token is where the EKS form keeps it, the admin token Secret.
admin_token_held() {
  kubectl -n "$COLVENNA_NAMESPACE" get secret "$ADMIN_TOKEN_SECRET" >/dev/null 2>&1
}

# ensure_admin [via]: the admin's token (issued if missing, invalid, or with less than a month left), its
# MANAGE grant on the catalog (made again with a year to run when less than a month is left), and
# Paimon's default database, which Flink and Spark catalogs look up when they start. The EKS form uses
# the functions above (admin_token_held, issue_admin_token, admin_open, admin_call, admin_status and
# admin_close); the serverless form passes via=sl_, the prefix of its own (deploy/lib/serverless.sh).
ensure_admin() {
  local via="${1:-}" renew=0 tokens grants grant left id expires
  if ! "${via}admin_token_held"; then
    "${via}issue_admin_token"
  fi
  "${via}admin_open"
  if [[ "$("${via}admin_status" GET /colvenna/admin/v1/principals/admin/tokens)" != 200 ]]; then
    renew=1
  else
    tokens="$("${via}admin_call" GET /colvenna/admin/v1/principals/admin/tokens)"
    left="$(jq --argjson within "$RENEW_WITHIN_DAYS" "$JQ_EPOCH"'[.tokens[]? | select(.revokedAt == null)
      | ((.expiresAt | epoch) - now) / 86400 | select(. > $within)] | length' <<<"$tokens")"
    ((left > 0)) || renew=1
  fi
  if ((renew)); then
    "${via}admin_close"
    "${via}issue_admin_token"
    "${via}admin_open"
  fi
  grants="$("${via}admin_call" GET '/colvenna/admin/v1/grants?principal=admin')"
  grant="$(jq -c '[.grants[] | select(.scope == "CATALOG" and .privilege == "MANAGE" and .effect == "ALLOW")][0] // {}' <<<"$grants")"
  id="$(jq -r '.id // empty' <<<"$grant")"
  expires="$(jq -r '.expiresAt // empty' <<<"$grant")"
  if [[ -n "$id" ]] && (($(jq -n --arg e "$expires" "$JQ_EPOCH"'($e | epoch) - now | floor') / 86400 < RENEW_WITHIN_DAYS)); then
    "${via}admin_call" DELETE "/colvenna/admin/v1/grants/$id" >/dev/null
    id=""
  fi
  if [[ -z "$id" ]]; then
    expires="$(iso_in_days "$ADMIN_GRANT_DAYS")"
    "${via}admin_call" POST /colvenna/admin/v1/grants "$(jq -cn --arg exp "$expires" '{
      principal: "admin", scope: "CATALOG", privilege: "MANAGE", effect: "ALLOW",
      purpose: "catalog administration by install.sh", approvedBy: "install.sh",
      approval: "the installation of this catalog", expiresAt: $exp}')" >/dev/null
  fi
  if [[ "$("${via}admin_status" GET "/v1/$COLVENNA_WAREHOUSE_NAME/databases/default")" == 404 ]]; then
    "${via}admin_call" POST "/v1/$COLVENNA_WAREHOUSE_NAME/databases" '{"name":"default","options":{}}' >/dev/null
  fi
  "${via}admin_close"
}

# probe <namespace> <manifest>: runs the smoke probe pod and prints its answer ("anonymous=... admin=...").
# The log of a pod that has just finished can come back empty, so a probe without an answer runs again,
# three times at most.
probe() {
  local out=""
  for _ in 1 2 3; do
    out="$(run_pod "$1" smoke "$2")"
    if [[ "$out" == anonymous=* ]]; then
      printf '%s\n' "$out"
      return 0
    fi
    sleep 2
  done
  echo "The smoke probe in $1 gave no answer three times (last: '$out')" >&2
  return 1
}

# smoke_namespaces: the two namespaces the smoke check runs its probes in.
smoke_namespaces() {
  printf '%s\n' "$COLVENNA_NAMESPACE-smoke-client" "$COLVENNA_NAMESPACE-smoke-outsider"
}

# smoke_cleanup: deletes the smoke check's namespaces (one holds the admin's token) and its directory, if
# smoke made them (SMOKE_DIR). For install.sh's exit too.
smoke_cleanup() {
  [[ -n "${SMOKE_DIR:-}" ]] || return 0
  local listed namespaces
  listed="$(smoke_namespaces)"
  mapfile -t namespaces <<<"$listed"
  kubectl delete namespace "${namespaces[@]}" --ignore-not-found --wait=false >/dev/null
  rm -rf "$SMOKE_DIR"
  SMOKE_DIR=""
}

# smoke <evidence-dir>: from a pod in a namespace labelled as a catalog client, an anonymous request is
# refused (401) and the admin's succeeds (200); from a namespace without the label, the API is not
# reachable at all when NetworkPolicy is enforced. Writes smoke.txt.
smoke() {
  local evidence="$1" client outsider work url ns listed
  listed="$(smoke_namespaces)"
  { read -r client; read -r outsider; } <<<"$listed"
  # A namespace an earlier run is still deleting is waited for, not reused.
  kubectl delete namespace "$client" "$outsider" --ignore-not-found --wait=true --timeout=5m >/dev/null
  SMOKE_DIR="$(mktemp -d)"
  work="$SMOKE_DIR"
  chmod 700 "$work"
  url="https://$COLVENNA_RELEASE_NAME.$COLVENNA_NAMESPACE.svc:8181/v1/config?warehouse=$COLVENNA_WAREHOUSE_NAME"
  secret_file "$CA_SECRET" ca.crt "$work/ca.crt"
  secret_file "$ADMIN_TOKEN_SECRET" token "$work/admin"
  printf none >"$work/none"
  for ns in "$client" "$outsider"; do
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl label namespace "$ns" --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
    kubectl -n "$ns" create configmap smoke-ca --from-file=ca.crt="$work/ca.crt" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  done
  kubectl label namespace "$client" --overwrite "$COLVENNA_CLIENT_NAMESPACE_LABEL" >/dev/null
  # The admin's token only where the request should succeed; the outsider gets a placeholder.
  kubectl -n "$client" create secret generic smoke-token --from-file=admin="$work/admin" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$outsider" create secret generic smoke-token --from-file=admin="$work/none" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f "$work/admin"
  # The probe: anonymous and admin requests; the HTTP status of each (000: no connection).
  # shellcheck disable=SC2016  # expanded by the pod's shell
  local script='code() { curl -s -o /dev/null -w "%{http_code}" --max-time 10 --cacert /ca/ca.crt "$@" || true; }
printf "Authorization: Bearer %s" "$(cat /token/admin)" >/tmp/h
echo "anonymous=$(code "$URL") admin=$(code -H @/tmp/h "$URL")"'
  cat >"$work/probe.yaml" <<YAML
apiVersion: v1
kind: Pod
metadata: {name: smoke}
spec:
  restartPolicy: Never
  securityContext: {runAsNonRoot: true, runAsUser: 100, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: curl
      image: $CURL_IMAGE
      env: [{name: URL, value: $(json_string "$url")}]
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
      volumeMounts: [{name: ca, mountPath: /ca}, {name: token, mountPath: /token}]
      command: ["sh", "-c", $(json_string "$script")]
  volumes:
    - {name: ca, configMap: {name: smoke-ca}}
    - {name: token, secret: {secretName: smoke-token}}
YAML
  local inside outside
  inside="$(probe "$client" "$work/probe.yaml")"
  outside="$(probe "$outsider" "$work/probe.yaml")"
  smoke_cleanup
  printf 'client namespace: %s\nother namespace: %s\n' "$inside" "$outside" | tee "$evidence/smoke.txt"
  grep -q 'anonymous=401 admin=200' <<<"$inside" || { echo "Smoke check failed: $inside" >&2; return 1; }
  if [[ "${ALLOW_UNENFORCED_NETWORK_POLICY:-}" != 1 ]]; then
    grep -q 'anonymous=000 admin=000' <<<"$outside" \
      || { echo "A namespace without the client label reached the API: $outside" >&2; return 1; }
  fi
}
