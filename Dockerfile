# colvenna-catalog runtime image, the same for both deployment forms. Build after `mvn package`.
# - EKS runs its ENTRYPOINT (`serve`).
# - Lambda (the serverless form, #13) runs it too, under the AWS Lambda Web Adapter: an extension in
#   /opt/extensions that Lambda starts beside it, and that nothing else ever starts. The maintenance
#   function runs /opt/colvenna-catalog/lambda-maintenance.sh instead (migrate, bootstrap-admin).
# The only steps that run a command run on the build platform, so an amd64 image builds on any host
# without emulation.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0@sha256:17cfd08eff1dfea3f6a9a1e9c65fdac80aa4919b6085e746615530f43f57d2f1 AS adapter

FROM eclipse-temurin:21-jre-noble@sha256:161310cd5bd41fd549d19cf9f90b614897cb4a1532555c613b43b7e0628fe5d6 AS runtime

# Hadoop's login (S3 FileIO) needs a passwd entry for the UID the process runs as: the image's USER
# below has one, and Lambda runs container images as its own sandbox user, UID 993 (seen in #12).
FROM --platform=$BUILDPLATFORM eclipse-temurin:21-jre-noble@sha256:161310cd5bd41fd549d19cf9f90b614897cb4a1532555c613b43b7e0628fe5d6 AS accounts
COPY --from=runtime /etc/passwd /accounts/passwd
RUN grep -q '^[^:]*:[^:]*:993:' /accounts/passwd \
  || echo 'sbx_user1051:x:993:990:Lambda sandbox user:/tmp:/usr/sbin/nologin' >>/accounts/passwd

# /licenses/: this repository's LICENSE, NOTICE and THIRD_PARTY_NOTICES.md, and in third-party/ the license
# texts of what the image carries without a license file of its own (THIRD_PARTY_NOTICES.md lists them).
# Made readable by every user here, whatever modes the build context has: COPY --chmod would give the
# directories it makes the same mode as the files.
FROM --platform=$BUILDPLATFORM eclipse-temurin:21-jre-noble@sha256:161310cd5bd41fd549d19cf9f90b614897cb4a1532555c613b43b7e0628fe5d6 AS licenses
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /licenses/
COPY licenses/ /licenses/third-party/
RUN chmod -R u=rwX,go=rX /licenses

FROM runtime
COPY --from=accounts /accounts/passwd /etc/passwd
COPY --from=adapter /lambda-adapter /opt/extensions/lambda-adapter
COPY target/lib/ /opt/colvenna-catalog/lib/
COPY target/colvenna-catalog-0.1.0.jar /opt/colvenna-catalog/colvenna-catalog.jar
# Into the directory the COPY above made: a --chmod applies to the directories it makes as well.
# The RDS CA bundle (every Region's), pinned by checksum: in Lambda there is no ConfigMap to mount the
# database's CA from, so JDBC verifies the certificate against this file (sslmode=verify-full).
ADD --chmod=644 --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /opt/colvenna-catalog/rds-global-bundle.pem
COPY --chmod=755 deploy/lambda/maintenance.sh /opt/colvenna-catalog/lambda-maintenance.sh
COPY --from=licenses /licenses/ /licenses/
# Container images may use Lambda SnapStart without runtime hooks when they carry this label; the
# serverless form turns SnapStart on only when asked (COLVENNA_SNAPSTART=on).
LABEL com.amazonaws.lambda.feature.snapstart="Allow"
# The base image's unprivileged ubuntu user.
USER 1000:1000
EXPOSE 8181 8182
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-Djava.io.tmpdir=/tmp", "-cp", "/opt/colvenna-catalog/colvenna-catalog.jar:/opt/colvenna-catalog/lib/*", "com.colvenna.catalog.Main"]
CMD ["serve"]
