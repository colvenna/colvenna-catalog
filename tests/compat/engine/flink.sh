#!/bin/sh
# A Flink cell's container: the flow (FlinkFlow) in a local Flink inside the official image, with
# the cell's Paimon jars (/jars) and the flow jar (/compat). Usage: flink.sh <mode> <argument>.
set -eu
trust_name=flink
# shellcheck source-path=SCRIPTDIR source=truststore.sh
. /compat/truststore.sh
# The container's CPU share, not the node's: the local cluster sizes its parallelism from it.
# shellcheck disable=SC2086  # TRUST is a list of options
exec java "-Xmx${COMPAT_HEAP:-2g}" -XX:ActiveProcessorCount=2 $TRUST -Dlog4j.configurationFile=/compat/log4j2.properties \
  -cp "/compat/compat-flink.jar:/jars/*:/opt/flink/lib/*" com.colvenna.catalog.compat.flink.FlinkFlow "$@"
