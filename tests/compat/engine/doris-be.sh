#!/bin/bash
# The Doris cell's BE container (the official BE image), on loopback beside the FE. Its JNI readers
# (the Paimon SDK) trust the catalog's CA through JAVA_TOOL_OPTIONS.
set -euo pipefail
trust_name=doris-be
# shellcheck source-path=SCRIPTDIR source=truststore.sh
. /compat/truststore.sh
be=/opt/apache-doris/be
echo 'priority_networks = 127.0.0.1/32' >>"$be/conf/be.conf"
# The JNI readers' heap: 1 GB of the container's memory rather than 2.
sed -i 's/-Xmx2048m/-Xmx1024m/' "$be/conf/be.conf"
export JAVA_TOOL_OPTIONS="$TRUST"
# vm.max_map_count and swap are the node's, which a test pod does not change.
export SKIP_CHECK_ULIMIT=true
exec "$be/bin/start_be.sh" --console
