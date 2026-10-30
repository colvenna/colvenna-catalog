# shellcheck shell=sh disable=SC2034  # TRUST is the sourcing script's
# Sourced by every engine container's script, which sets trust_name first: a trust store of the
# JDK's public CAs plus the catalog's CA (/ca/ca.crt, when the run has one) in
# /work/<trust_name>-cacerts, and TRUST, the JVM options that use it (empty without a CA). The CA is
# added to the public ones, not used alone: the same JVM reaches S3 and STS too.
store="/work/${trust_name:-engine}-cacerts"
TRUST=""
if [ -f /ca/ca.crt ]; then
  cp "$JAVA_HOME/lib/security/cacerts" "$store"
  chmod 600 "$store"
  keytool -importcert -noprompt -alias colvenna-catalog -file /ca/ca.crt -keystore "$store" \
    -storepass changeit >/dev/null
  TRUST="-Djavax.net.ssl.trustStore=$store -Djavax.net.ssl.trustStorePassword=changeit"
fi
