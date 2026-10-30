#!/bin/sh
# A Spark cell's container: the flow (SparkFlow) through spark-submit in local mode inside the
# official image, with the cell's Paimon jars (/jars). Usage: spark.sh <flow jar> <mode> <argument>.
set -eu
trust_name=spark
# shellcheck source-path=SCRIPTDIR source=truststore.sh
. /compat/truststore.sh
flow="$1"
shift
jars="$(find /jars -name '*.jar' | sort | paste -sd, -)"
exec /opt/spark/bin/spark-submit --master 'local[2]' --driver-memory "${COMPAT_HEAP:-2g}" \
  --conf spark.ui.enabled=false --conf spark.local.dir=/tmp/spark \
  --conf "spark.driver.extraJavaOptions=$TRUST -Dlog4j.configurationFile=/compat/log4j2.properties" \
  --jars "$jars" --class com.colvenna.catalog.compat.spark.SparkFlow "/compat/$flow" "$@"
