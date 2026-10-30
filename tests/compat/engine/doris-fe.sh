#!/bin/bash
# The Doris cell's main container (the official FE image): the FE on loopback, sharing the pod's
# network with the BE container, then reader.py, which registers the BE and runs the checks. The
# container ends when reader.py does, after the tail of the engine's warnings.
set -euo pipefail
trust_name=doris-fe
# shellcheck source-path=SCRIPTDIR source=truststore.sh
. /compat/truststore.sh
fe=/opt/apache-doris/fe
# The image's 8 GB heap fits no test node; 1 GB is plenty for one catalog's metadata.
sed -i 's/-Xmx8192m -Xms8192m/-Xmx1024m -Xms1024m/' "$fe/conf/fe.conf"
echo 'priority_networks = 127.0.0.1/32' >>"$fe/conf/fe.conf"
# root has the run's random password from its first start (MySQL's double SHA-1 of it), never none.
python3 - "$COMPAT_ENGINE_PASSWORD_FILE" >>"$fe/conf/fe.conf" <<'PY'
import hashlib, sys
password = open(sys.argv[1]).read().strip().encode()
print("initial_root_password = *" + hashlib.sha1(hashlib.sha1(password).digest()).hexdigest().upper())
PY
export JAVA_TOOL_OPTIONS="$TRUST"
for jar in "$fe"/lib/paimon*.jar; do echo "COMPAT-READER-INFO bundled ${jar##*/}"; done
"$fe/bin/start_fe.sh" --daemon
status=0
python3 /compat/reader.py doris || status=$?
# The engine's own account of its warnings, for the raw output.
for log in "$fe"/log/fe.warn.log "$fe"/log/fe.out; do
  [[ -f "$log" ]] && { echo "COMPAT-ENGINE-LOG $log"; tail -n 200 "$log"; }
done
exit "$status"
