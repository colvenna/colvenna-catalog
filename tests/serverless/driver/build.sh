#!/usr/bin/env bash
# Builds the serverless live-test driver (#13) into <out-dir>: driver.jar, compiled for Java 21 by the
# root pom's serverless-driver profile with the server's own dependencies, Error Prone and spotless (the
# driver image takes those dependencies from the server image it is built on); and paimon-hadoop-uber,
# the Hadoop classes that Paimon's RESTCatalog needs and engines normally provide (the server's tests
# use the same jar), checked against its pinned SHA-256. It comes from the local Maven repository when
# a build has put it there, from Maven Central otherwise.
# Usage: tests/serverless/driver/build.sh <out-dir>
set -euo pipefail
[[ $# -eq 1 ]] || { echo "usage: $0 <out-dir>" >&2; exit 2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
out="$1"
HADOOP_UBER=paimon-hadoop-uber-2.0.0.jar
HADOOP_UBER_SHA256=fd88b93d8f8834535bde7d5ff7acc33a7765aed9e65d99f0830eff833ff3a918
mkdir -p "$out"
(cd "$repo" && mvn -B -q -P serverless-driver clean package -DskipTests) >"$out/build.log" 2>&1 \
  || { tail -30 "$out/build.log" >&2; exit 1; }
cp "$repo/target/serverless-driver/colvenna-catalog-test-driver.jar" "$out/driver.jar"
rm -f "$out/build.log"

local_copy="${HOME}/.m2/repository/org/apache/paimon/paimon-hadoop-uber/2.0.0/$HADOOP_UBER"
if [[ -f "$local_copy" ]]; then
  cp "$local_copy" "$out/$HADOOP_UBER"
else
  curl -fsS -o "$out/$HADOOP_UBER" "https://repo1.maven.org/maven2/org/apache/paimon/paimon-hadoop-uber/2.0.0/$HADOOP_UBER"
fi
printf '%s  %s\n' "$HADOOP_UBER_SHA256" "$out/$HADOOP_UBER" | sha256sum --check --quiet
echo "$out/driver.jar $out/$HADOOP_UBER"
