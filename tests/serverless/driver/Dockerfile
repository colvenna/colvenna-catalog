# check=skip=InvalidDefaultArgInFrom
# The serverless live-test driver (#13): the catalog's own server image, so the same Paimon 2.0.0
# client jars, plus the driver and paimon-hadoop-uber (the Hadoop classes RESTCatalog needs, which engines
# provide and the server does not ship). It is its own Lambda runtime (DriverMain); the Lambda Web Adapter the
# server image carries in /opt/extensions registers for no events and never takes an invocation here,
# because nothing listens on its readiness port. Built by tests/serverless/run.sh with BASE = the
# environment's server image by digest, and only COPY steps, so it builds on any host.
ARG BASE
FROM ${BASE}
# BASE has no default: a build without it fails at FROM, never builds on another image. The mode
# applies to the directory COPY makes too, which Lambda's own user must be able to enter.
COPY --chmod=0755 driver.jar paimon-hadoop-uber-2.0.0.jar /opt/colvenna-catalog-test/
# BASE's own unprivileged user, said again here (Lambda runs the image as its sandbox user anyway).
USER 1000:1000
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-Djava.io.tmpdir=/tmp", "-cp", "/opt/colvenna-catalog-test/driver.jar:/opt/colvenna-catalog/lib/*:/opt/colvenna-catalog-test/paimon-hadoop-uber-2.0.0.jar", "com.colvenna.catalog.testdriver.DriverMain"]
CMD []
