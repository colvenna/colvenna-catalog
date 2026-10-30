#!/bin/bash
# The StarRocks cell's container (the official all-in-one image): its FE and BE as the image starts
# them, then reader.py. The container ends when reader.py does.
set -euo pipefail
trust_name=starrocks
# shellcheck source-path=SCRIPTDIR source=truststore.sh
. /compat/truststore.sh
sr=/data/deploy/starrocks
# The image's 8 GB FE heap fits no test node.
sed -i 's/-Xmx8192m/-Xmx2048m/' "$sr/fe/conf/fe.conf"
export JAVA_TOOL_OPTIONS="$TRUST"
for jar in "$sr"/fe/lib/paimon*.jar; do echo "COMPAT-READER-INFO bundled ${jar##*/}"; done
(cd /data/deploy && ./entrypoint.sh) &
status=0
python3 /compat/reader.py starrocks || status=$?
# The engine's own account of its warnings, for the raw output.
for log in "$sr"/fe/log/fe.warn.log "$sr"/be/log/be.WARNING "$sr"/be/log/jni.log; do
  [[ -f "$log" ]] && { echo "COMPAT-ENGINE-LOG $log"; tail -n 200 "$log"; }
done
exit "$status"
