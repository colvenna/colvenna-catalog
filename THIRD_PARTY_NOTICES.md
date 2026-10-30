# Third-party notices

Colvenna Catalog's own source code, deployment code, documentation and brand assets are under the [Apache License 2.0](LICENSE). Third-party components keep their own licenses. This file records them: the third-party material in this repository, what the server's image ships, the images and tools the install, the test environments and the compatibility matrix run, and the packages the deployment installs. Redistribution must keep the license and notice files each component ships with (for jars, their `META-INF/LICENSE` and `NOTICE`; for container images, those their packages install). This record does not replace them.

This is the maintainers' engineering record of declared licenses, not legal advice, and no legal review was made. The tables below marked as the scan's are the output of the license scan of 2026-10-28, `python3 scripts/release/licenses.py scan` ([Development](docs/development.md#licenses)): the licenses the Maven POMs of the server's runtime dependencies declare, what the jars say they bundle (their own `META-INF/NOTICE` files, the license files they keep for bundled projects, and the Maven `pom.properties` of other projects they keep), and `deploy/aws/package-lock.json`. The images' and tools' licenses were read from their upstream projects and the pinned images' metadata on the same day. What could not be established is marked UNKNOWN; what was not examined, NOT_RUN.

## Findings

- **No strong copyleft in what this project builds.** No GPL, AGPL or SSPL license is declared by any jar of the server's image or by anything those jars say they bundle, nor by any package of the CDK app (one, `case`, is `MIT OR GPL-3.0-or-later`, and MIT can be chosen).
- **Weak copyleft in the server's image:** Logback (`logback-classic`, `logback-core`), `EPL-2.0 OR LGPL-2.1-only`; and, bundled inside `paimon-s3`, `javax.xml.bind:jaxb-api` under the CDDL 1.1 and `jakarta.activation:jakarta.activation-api` under the Eclipse Distribution License 1.0. The image's base carries OpenJDK (GPL-2.0 with the Classpath Exception) and Ubuntu's packages.
- **GPL-2.0 in images that are run, not built:** the install's `postgres` and `curl` images are on Alpine Linux, whose BusyBox is GPL-2.0, and the test environments run BusyBox itself. This project pulls them by digest from their publishers and does not redistribute them.
- **Jars without license files.** 13 of the image's 74 jars carry no license or notice file under `META-INF/`: `HikariCP`, `error_prone_annotations`, `eventstream`, `j2objc-annotations`, `jspecify`, `jsr305`, `logback-classic`, `logback-core`, `lz4-java`, `reactive-streams`, `snakeyaml`, `snappy-java` and `zstd-jni`. The image holds the jars as Maven Central serves them, and their license texts in `/licenses/third-party/` ([License texts in the image](#license-texts-in-the-image)), beside this repository's `LICENSE`, `NOTICE` and this file in `/licenses/`. Three of them, `lz4-java`, `snappy-java` and `zstd-jni`, also carry native libraries built from other projects: LZ4 1.10.0 (BSD-2-Clause), Snappy 1.1.10 (BSD-3-Clause) with Bitshuffle 0.3.4 (MIT), and Zstandard 1.5.5 (BSD-3-Clause, or GPL-2.0; the BSD license is the one taken), by each jar's build at its tag; their license texts are in `/licenses/third-party/` too.
- **Bundled in the PostgreSQL driver:** OnGres SCRAM and stringprep (`com.ongres.scram:scram-client`, `scram-common` 3.2, `com.ongres.stringprep:saslprep`, `stringprep` 2.2), BSD-2-Clause by the license files the driver keeps for them.
- **What the Paimon jars bundle without naming its license.** `paimon-s3`'s NOTICE lists the components of its shaded Hadoop (`paimon-hadoop-shaded`: Hadoop 3.4.2, Guava 27, Jackson 2.12, Woodstox, Apache Commons and Kerby, among others) under "This project includes software developed at The Apache Software Foundation" with no license per component, and `paimon-api`'s lists the Guava 30 and Caffeine it carries with none (the jars `paimon-shade-guava-30` and `paimon-shade-caffeine-2` name the Apache Software License 2.0 for the same versions). They are recorded as "not stated" below. `paimon-api`, `paimon-common` and `paimon-format` also keep the Maven `pom.properties` of five projects their NOTICE files do not list (among them FlatBuffers, aircompressor and commons-io), recorded as "not listed in its NOTICE". The licenses of both kinds were not looked up one by one (NOT_RUN).

## Third-party material in this repository

### AWS Load Balancer Controller IAM policy (test environments only)

[`deploy/aws/test-env/lbc-iam-policy.json`](deploy/aws/test-env/lbc-iam-policy.json) is the IAM policy the AWS Load Balancer Controller project publishes for v3.6.0 (`docs/install/iam_policy.json` in [kubernetes-sigs/aws-load-balancer-controller](https://github.com/kubernetes-sigs/aws-load-balancer-controller/tree/v3.6.0)), kept verbatim (the CDK tests pin its SHA-256), under the Apache License 2.0. Only the stack of this repository's test environments uses it.

### Brand assets

The Colvenna series mark and the Colvenna Catalog wordmark in [`assets/brand/`](assets/brand/README.md) are the series' own, drawn by [`generate.py`](assets/brand/generate.py) (Apache-2.0). The SVGs embed no fonts, scripts or images; they name Arial, Helvetica and the system's sans-serif font, which the viewer's machine supplies. The PNGs are rendered from them with `rsvg-convert`; which installed fonts rendered the committed PNGs is not recorded (UNKNOWN). Rendered glyphs carry no font software.

### Quotations in research reports

The reports in [`docs/research/`](docs/research/) quote the documentation and source code of Apache Paimon and other open-source projects, and AWS documentation, as design reference. That material remains the copyright of its owners.

## The server's image

[`Dockerfile`](Dockerfile) builds the image `install.sh` and the release candidates run. Its base is `eclipse-temurin:21-jre-noble`, pinned by digest: Ubuntu 24.04 with the Eclipse Temurin JRE 21.0.13+9, which is OpenJDK under GPL-2.0 with the Classpath Exception (the image's Dockerfiles, from [adoptium/containers](https://github.com/adoptium/containers), are Apache-2.0). Ubuntu's packages in it keep their own licenses and are not listed here (NOT_RUN). Onto the base, the image copies:

- the AWS Lambda Web Adapter 1.1.0 binary, from `public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0` pinned by digest, into `/opt/extensions/`, where only Lambda (the serverless form) starts it. It is Apache-2.0 ([awslabs/aws-lambda-web-adapter](https://github.com/awslabs/aws-lambda-web-adapter/tree/v1.1.0)), and its NOTICE is in [NOTICE](NOTICE);
- the Amazon RDS CA certificate bundle `global-bundle.pem`, from `truststore.pki.rds.amazonaws.com` pinned by checksum, which JDBC verifies the database's certificate against in Lambda. It is AWS's public CA certificates, published for clients to verify RDS; no license is stated with it (UNKNOWN);
- the server's own jar and `deploy/lambda/maintenance.sh`, and `target/lib/`: the jars below, unmodified, which `mvn package` copies from pom.xml's compile and runtime dependencies;
- `/licenses/`: this repository's `LICENSE` and `NOTICE`, this file, and in `third-party/` the license texts of [`licenses/`](licenses/) for what the image carries without a license file of its own ([License texts in the image](#license-texts-in-the-image)).

Test dependencies (JUnit, AssertJ, Testcontainers, MockWebServer, Mockito, Paimon's test jars and `paimon-hadoop-uber`) and build plugins (Error Prone, Spotless) are not in it.

### Runtime dependencies

The scan's: each jar, with the license its POM declares (SPDX identifiers; "A OR B" for a choice of either).

<!-- licenses:java:start pom=878f7eaa043210e5dd4199abbeb62c98645dbd69 -->

| Artifact | Version | Declared license |
| --- | --- | --- |
| `at.yawk.lz4:lz4-java` | 1.11.4 | Apache-2.0 |
| `ch.qos.logback:logback-classic` | 1.5.38 | EPL-2.0 OR LGPL-2.1-only |
| `ch.qos.logback:logback-core` | 1.5.38 | EPL-2.0 OR LGPL-2.1-only |
| `com.fasterxml.jackson.core:jackson-annotations` | 2.21 | Apache-2.0 |
| `com.fasterxml.jackson.core:jackson-core` | 2.21.8 | Apache-2.0 |
| `com.fasterxml.jackson.core:jackson-databind` | 2.21.8 | Apache-2.0 |
| `com.fasterxml.jackson.dataformat:jackson-dataformat-csv` | 2.21.8 | Apache-2.0 |
| `com.fasterxml.jackson.dataformat:jackson-dataformat-yaml` | 2.21.8 | Apache-2.0 |
| `com.fasterxml.jackson.datatype:jackson-datatype-jdk8` | 2.21.8 | Apache-2.0 |
| `com.fasterxml.jackson.datatype:jackson-datatype-jsr310` | 2.21.8 | Apache-2.0 |
| `com.github.ben-manes.caffeine:caffeine` | 2.9.3 | Apache-2.0 |
| `com.github.luben:zstd-jni` | 1.5.5-11 | BSD-2-Clause |
| `com.google.code.findbugs:jsr305` | 1.3.9 | Apache-2.0 |
| `com.google.errorprone:error_prone_annotations` | 2.50.0 | Apache-2.0 |
| `com.google.guava:failureaccess` | 1.0.3 | Apache-2.0 |
| `com.google.guava:guava` | 33.7.2-jre | Apache-2.0 |
| `com.google.j2objc:j2objc-annotations` | 3.1 | Apache-2.0 |
| `com.zaxxer:HikariCP` | 7.1.0 | Apache-2.0 |
| `commons-io:commons-io` | 2.20.0 | Apache-2.0 |
| `org.apache.commons:commons-lang3` | 3.18.0 | Apache-2.0 |
| `org.apache.commons:commons-math3` | 3.6.1 | Apache-2.0 |
| `org.apache.httpcomponents.client5:httpclient5` | 5.6.4 | Apache-2.0 |
| `org.apache.httpcomponents.core5:httpcore5` | 5.4.3 | Apache-2.0 |
| `org.apache.httpcomponents.core5:httpcore5-h2` | 5.4.3 | Apache-2.0 |
| `org.apache.paimon:paimon-api` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-codegen-loader` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-common` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-core` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-format` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-s3` | 2.0.0 | Apache-2.0 |
| `org.apache.paimon:paimon-shade-caffeine-2` | 2.9.3-0.8.0 | Apache-2.0 |
| `org.apache.paimon:paimon-shade-guava-30` | 30.1.1-jre-0.8.0 | Apache-2.0 |
| `org.apache.paimon:paimon-shade-jackson-2` | 2.14.2-0.8.0 | Apache-2.0 |
| `org.checkerframework:checker-qual` | 3.55.1 | MIT |
| `org.jspecify:jspecify` | 1.0.1 | Apache-2.0 |
| `org.postgresql:postgresql` | 42.7.13 | BSD-2-Clause |
| `org.reactivestreams:reactive-streams` | 1.0.4 | MIT-0 |
| `org.slf4j:slf4j-api` | 2.0.17 | MIT |
| `org.xerial.snappy:snappy-java` | 1.1.10.12 | Apache-2.0 |
| `org.yaml:snakeyaml` | 2.5 | Apache-2.0 |
| `software.amazon.awssdk:annotations` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:apache5-client` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:arns` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:auth` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:aws-core` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:aws-json-protocol` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:aws-query-protocol` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:aws-xml-protocol` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:checksums` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:checksums-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:cognitoidentity` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:crt-core` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:endpoints-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:http-auth` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:http-auth-aws` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:http-auth-aws-eventstream` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:http-auth-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:http-client-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:identity-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:json-utils` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:metrics-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:profiles` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:protocol-core` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:rds` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:regions` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:retries` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:retries-spi` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:s3` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:sdk-core` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:sts` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:third-party-jackson-core` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:utils` | 2.55.15 | Apache-2.0 |
| `software.amazon.awssdk:utils-lite` | 2.55.15 | Apache-2.0 |
| `software.amazon.eventstream:eventstream` | 1.0.1 | Apache-2.0 |

<!-- licenses:java:end -->

### What the shaded jars bundle

The scan's: some jars are shaded and contain other projects' classes. The Paimon jars list them in their `META-INF/NOTICE`, with the licenses' texts for the non-Apache ones under `META-INF/licenses/`; `paimon-codegen-loader`'s list is the NOTICE of the `paimon-codegen` jar it carries inside. The PostgreSQL driver keeps each bundled project's license file under `META-INF/licenses/<groupId>/<artifactId>-<version>/`; its license is named here by that file's wording. A project a jar keeps only the Maven `pom.properties` of, and names in neither, is "not listed in its NOTICE".

<!-- licenses:bundled:start -->

| Jar | License, as the jar names it | Bundled |
| --- | --- | --- |
| `paimon-api` | Apache Software License 2.0 | `com.fasterxml.jackson.core:jackson-annotations:2.14.2`, `com.fasterxml.jackson.core:jackson-core:2.14.2`, `com.fasterxml.jackson.core:jackson-databind:2.14.2`, `com.fasterxml.jackson.dataformat:jackson-dataformat-csv:2.14.2`, `com.fasterxml.jackson.dataformat:jackson-dataformat-yaml:2.14.2`, `com.fasterxml.jackson.datatype:jackson-datatype-jdk8:2.14.2`, `com.fasterxml.jackson.datatype:jackson-datatype-jsr310:2.14.2`, `org.apache.httpcomponents.client5:httpclient5:5.4.2`, `org.apache.httpcomponents.core5:httpcore5:5.3.3`, `org.yaml:snakeyaml:1.33` |
| `paimon-api` | not listed in its NOTICE (a pom.properties only) | `org.apache.httpcomponents.core5:httpcore5-h2:5.3.6` |
| `paimon-api` | not stated | `com.github.ben-manes.caffeine:caffeine:2.9.3`, `com.google.guava:failureaccess:1.0.1`, `com.google.guava:guava:30.1.1-jre` |
| `paimon-codegen-loader` | Apache Software License 2.0 | `org.scala-lang:scala-compiler:2.12.15`, `org.scala-lang:scala-library:2.12.15`, `org.scala-lang:scala-reflect:2.12.15` |
| `paimon-common` | Apache Software License 2.0 | `org.apache.datasketches:datasketches-java:4.2.0`, `org.apache.datasketches:datasketches-memory:2.2.0`, `org.roaringbitmap:RoaringBitmap:1.2.1` |
| `paimon-common` | BSD 3-clause license | `com.github.davidmoten:guava-mini:0.1.3`, `com.github.davidmoten:hilbert-curve:0.2.2`, `it.unimi.dsi:fastutil:8.5.12`, `net.openhft:zero-allocation-hashing:0.16`, `org.antlr:antlr4-runtime:4.9.3`, `org.codehaus.janino:commons-compiler:3.0.11`, `org.codehaus.janino:janino:3.0.11` |
| `paimon-common` | not listed in its NOTICE (a pom.properties only) | `io.airlift:aircompressor:2.0.3` |
| `paimon-core` | Apache Software License 2.0 | `org.apache.httpcomponents.client5:httpclient5:5.5.1`, `org.apache.httpcomponents.core5:httpcore5:5.3.6` |
| `paimon-format` | Apache Software License 2.0 | `com.fasterxml.jackson.core:jackson-annotations:2.14.2`, `com.fasterxml.jackson.core:jackson-core:2.14.2`, `com.fasterxml.jackson.core:jackson-databind:2.14.2`, `commons-lang:commons-lang:2.6`, `commons-pool:commons-pool:1.6`, `io.airlift:aircompressor:2.0.3`, `org.apache.avro:avro:1.11.4`, `org.apache.commons:commons-compress:1.22`, `org.apache.commons:commons-lang3:3.18.0`, `org.apache.hive:hive-storage-api:2.8.1`, `org.apache.orc:orc-core:1.9.2`, `org.apache.orc:orc-shims:1.9.2`, `org.apache.parquet:parquet-column:1.16.0`, `org.apache.parquet:parquet-common:1.16.0`, `org.apache.parquet:parquet-encoding:1.16.0`, `org.apache.parquet:parquet-format-structures:1.16.0`, `org.apache.parquet:parquet-hadoop:1.16.0`, `org.apache.parquet:parquet-jackson:1.16.0` |
| `paimon-format` | BSD 3-clause license | `org.locationtech.jts:jts-core:1.20.0` |
| `paimon-format` | BSD license | `com.github.luben:zstd-jni:1.5.5-11`, `com.google.protobuf:protobuf-java:3.19.6`, `org.threeten:threeten-extra:1.7.1` |
| `paimon-format` | not listed in its NOTICE (a pom.properties only) | `com.google.flatbuffers:flatbuffers-java:23.5.26`, `commons-io:commons-io:2.16.1`, `net.openhft:zero-allocation-hashing:0.27ea0` |
| `paimon-s3` | Apache Software License 2.0 | `com.amazonaws:aws-java-sdk-core:1.12.319`, `com.amazonaws:aws-java-sdk-dynamodb:1.12.319`, `com.amazonaws:aws-java-sdk-kms:1.12.319`, `com.amazonaws:aws-java-sdk-s3:1.12.319`, `com.amazonaws:aws-java-sdk-sts:1.12.319`, `com.amazonaws:jmespath-java:1.12.319`, `com.fasterxml.jackson.dataformat:jackson-dataformat-cbor:2.12.6`, `commons-codec:commons-codec:1.15`, `joda-time:joda-time:2.8.1`, `org.apache.hadoop:hadoop-aws:3.4.2`, `org.apache.httpcomponents:httpclient:4.5.13`, `org.apache.httpcomponents:httpcore:4.4.13`, `org.wildfly.openssl:wildfly-openssl:1.0.7.Final`, `software.amazon.awssdk:bundle:2.29.52`, `software.amazon.ion:ion-java:1.0.2` |
| `paimon-s3` | BSD License | `org.codehaus.woodstox:stax2-api:4.2.1` |
| `paimon-s3` | BSD-2 License | `dnsjava:dnsjava:2.1.7` |
| `paimon-s3` | CDDL 1.1 license | `javax.xml.bind:jaxb-api:2.3.1` |
| `paimon-s3` | Eclipse Distribution License - v 1.0 | `jakarta.activation:jakarta.activation-api:1.2.1` |
| `paimon-s3` | Go License | `com.google.re2j:re2j:1.1` |
| `paimon-s3` | MIT | `org.checkerframework:checker-qual:2.5.2`, `org.codehaus.mojo:animal-sniffer-annotations:1.17`, `org.slf4j:slf4j-api:1.7.32` |
| `paimon-s3` | not stated | `com.fasterxml.jackson.core:jackson-annotations:2.12.7`, `com.fasterxml.jackson.core:jackson-core:2.12.7`, `com.fasterxml.jackson.core:jackson-databind:2.12.7`, `com.fasterxml.woodstox:woodstox-core:5.3.0`, `com.google.code.findbugs:jsr305:1.3.9`, `com.google.guava:failureaccess:1.0`, `com.google.guava:guava:27.0-jre`, `com.google.guava:listenablefuture:9999.0-empty-to-avoid-conflict-with-guava`, `com.google.j2objc:j2objc-annotations:1.1`, `commons-beanutils:commons-beanutils:1.9.4`, `commons-collections:commons-collections:3.2.2`, `commons-io:commons-io:2.8.0`, `commons-logging:commons-logging:1.1.3`, `org.apache.commons:commons-compress:1.21`, `org.apache.commons:commons-configuration2:2.1.1`, `org.apache.commons:commons-lang3:3.18.0`, `org.apache.commons:commons-text:1.4`, `org.apache.hadoop.thirdparty:hadoop-shaded-guava:1.4.0`, `org.apache.hadoop.thirdparty:hadoop-shaded-protobuf_3_25:1.4.0`, `org.apache.hadoop:hadoop-annotations:3.4.2`, `org.apache.hadoop:hadoop-auth:3.4.2`, `org.apache.hadoop:hadoop-common:3.4.2`, `org.apache.kerby:kerb-core:1.0.1`, `org.apache.kerby:kerby-asn1:1.0.1`, `org.apache.kerby:kerby-pkix:1.0.1`, `org.apache.kerby:kerby-util:1.0.1`, `org.xerial.snappy:snappy-java:1.1.10.8` |
| `paimon-shade-caffeine-2` | Apache Software License 2.0 | `com.github.ben-manes.caffeine:caffeine:2.9.3` |
| `paimon-shade-guava-30` | Apache Software License 2.0 | `com.google.guava:failureaccess:1.0.1`, `com.google.guava:guava:30.1.1-jre` |
| `paimon-shade-jackson-2` | Apache Software License 2.0 | `com.fasterxml.jackson.core:jackson-annotations:2.14.2`, `com.fasterxml.jackson.core:jackson-core:2.14.2`, `com.fasterxml.jackson.core:jackson-databind:2.14.2`, `com.fasterxml.jackson.dataformat:jackson-dataformat-csv:2.14.2`, `com.fasterxml.jackson.dataformat:jackson-dataformat-yaml:2.14.2`, `com.fasterxml.jackson.datatype:jackson-datatype-jdk8:2.14.2`, `com.fasterxml.jackson.datatype:jackson-datatype-jsr310:2.14.2`, `org.yaml:snakeyaml:1.33` |
| `postgresql` | BSD-2-Clause, by its license file | `com.ongres.scram:scram-client:3.2`, `com.ongres.scram:scram-common:3.2`, `com.ongres.stringprep:saslprep:2.2`, `com.ongres.stringprep:stringprep:2.2` |

<!-- licenses:bundled:end -->

### License texts in the image

What the image carries without a license file of its own: the jars that keep none under `META-INF/` (the license scan lists them) and the Lambda Web Adapter's binary. The Dockerfile copies the texts named here from [`licenses/`](licenses/) into `/licenses/third-party/`. A component that may be taken under either of two licenses has both texts; a jar whose native library is built from another project has that project's license text as well. The other jars keep their license and notice files inside them, unmodified.

This table is kept by hand, and checked: the license scan fails while a jar it finds without a license or notice file has no row here, or a jar with a row has its own; the offline tests check that each row is a jar of the java table above, at its version and license, or an image the Dockerfile copies from, that each row has a text of every license it may be taken under, and that the Dockerfile copies exactly these texts and `LICENSE`, `NOTICE` and this file into `/licenses/`; `licenses.py image <image>` checks a built image's `/licenses/` against the same ([Development](docs/development.md#licenses)).

<!-- licenses:texts:start -->

| Component | Version | License | Texts in `/licenses/third-party/` |
| --- | --- | --- | --- |
| `at.yawk.lz4:lz4-java` | 1.11.4 | Apache-2.0 | `Apache-2.0.txt`, `lz4.txt` |
| `ch.qos.logback:logback-classic` | 1.5.38 | EPL-2.0 OR LGPL-2.1-only | `logback.txt`, `EPL-2.0.txt`, `LGPL-2.1-only.txt` |
| `ch.qos.logback:logback-core` | 1.5.38 | EPL-2.0 OR LGPL-2.1-only | `logback.txt`, `EPL-2.0.txt`, `LGPL-2.1-only.txt` |
| `com.github.luben:zstd-jni` | 1.5.5-11 | BSD-2-Clause | `zstd-jni.txt`, `zstd.txt` |
| `com.google.code.findbugs:jsr305` | 1.3.9 | Apache-2.0 | `Apache-2.0.txt` |
| `com.google.errorprone:error_prone_annotations` | 2.50.0 | Apache-2.0 | `Apache-2.0.txt` |
| `com.google.j2objc:j2objc-annotations` | 3.1 | Apache-2.0 | `Apache-2.0.txt` |
| `com.zaxxer:HikariCP` | 7.1.0 | Apache-2.0 | `Apache-2.0.txt` |
| `org.jspecify:jspecify` | 1.0.1 | Apache-2.0 | `Apache-2.0.txt` |
| `org.reactivestreams:reactive-streams` | 1.0.4 | MIT-0 | `reactive-streams.txt` |
| `org.xerial.snappy:snappy-java` | 1.1.10.12 | Apache-2.0 | `Apache-2.0.txt`, `snappy.txt`, `bitshuffle.txt` |
| `org.yaml:snakeyaml` | 2.5 | Apache-2.0 | `Apache-2.0.txt` |
| `public.ecr.aws/awsguru/aws-lambda-adapter` | 1.1.0 | Apache-2.0 | `Apache-2.0.txt` |
| `software.amazon.eventstream:eventstream` | 1.0.1 | Apache-2.0 | `Apache-2.0.txt` |

<!-- licenses:texts:end -->

The texts are kept verbatim, as published (the pre-commit hooks leave `licenses/` as it is):

- `Apache-2.0.txt`: the Apache License 2.0, `https://www.apache.org/licenses/LICENSE-2.0.txt`, the same text as this repository's `LICENSE`;
- `EPL-2.0.txt`: the Eclipse Public License 2.0, `https://www.eclipse.org/org/documents/epl-2.0/EPL-2.0.txt`;
- `LGPL-2.1-only.txt`: the GNU Lesser General Public License 2.1, `https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt`;
- `logback.txt`: Logback's `LICENSE.txt` at its tag `v_1.5.38` ([qos-ch/logback](https://github.com/qos-ch/logback/blob/v_1.5.38/LICENSE.txt)): its copyright, and the choice of the EPL 2.0 or the LGPL 2.1;
- `reactive-streams.txt`: the `LICENSE` of Reactive Streams at `v1.0.4` ([reactive-streams/reactive-streams-jvm](https://github.com/reactive-streams/reactive-streams-jvm/blob/v1.0.4/LICENSE)): MIT-0, with its copyright;
- `zstd-jni.txt`: the `LICENSE` of zstd-jni at `v1.5.5-11` ([luben/zstd-jni](https://github.com/luben/zstd-jni/blob/v1.5.5-11/LICENSE)): BSD-2-Clause, with its copyright;
- `lz4.txt`: LZ4's `lib/LICENSE` at `v1.10.0` ([lz4/lz4](https://github.com/lz4/lz4/blob/v1.10.0/lib/LICENSE)), BSD-2-Clause, for the LZ4 library and its xxHash; lz4-java 1.11.4's submodule `src/lz4` is at that release;
- `snappy.txt`: Snappy's `COPYING` at `1.1.10` ([google/snappy](https://github.com/google/snappy/blob/1.1.10/COPYING)), BSD-3-Clause, the version snappy-java 1.1.10.12 builds (its `VERSION` file);
- `bitshuffle.txt`: Bitshuffle's `LICENSE` at `0.3.4` ([kiyo-masui/bitshuffle](https://github.com/kiyo-masui/bitshuffle/blob/0.3.4/LICENSE)), MIT, which snappy-java 1.1.10.12 builds into the same native library;
- `zstd.txt`: Zstandard's `LICENSE` at `v1.5.5` ([facebook/zstd](https://github.com/facebook/zstd/blob/v1.5.5/LICENSE)), BSD-3-Clause, the version zstd-jni 1.5.5-11 bundles; Zstandard is also offered under the GPL-2.0, which is not taken.

## Images and tools the install runs

| Name | Upstream | Declared license | Used by | Notes |
| --- | --- | --- | --- | --- |
| `postgres` | `postgres:16-alpine` | PostgreSQL License | `install.sh`: a pod runs `psql` to create, or bring in line, the catalog's database user and database | PostgreSQL 16.15 on Alpine Linux 3.24.2, whose BusyBox is GPL-2.0 |
| `curl` | `curlimages/curl:8.16.0` | curl license (MIT-style); the image is labelled MIT | `install.sh`: the smoke check's probe pods | Alpine Linux 3.22.1, as above |
| `kubectl` | `kubectl` v1.36.5 from `dl.k8s.io` | Apache-2.0 | `scripts/install-tools.sh` | Checked against its published SHA-256 |
| `helm` | `helm` v3.19.0 from `get.helm.sh` | Apache-2.0 | `scripts/install-tools.sh` | Checked against its published SHA-256 |
| `crane` | `crane` v0.22.1 from google/go-containerregistry's releases | Apache-2.0 | `scripts/install-tools.sh` | Checked against the SHA-256 the script pins |
| RDS CA bundle | `<region>-bundle.pem` from `truststore.pki.rds.amazonaws.com` | No license stated (UNKNOWN) | `install.sh` (EKS form): the ConfigMap the catalog's JDBC verifies the database against | AWS's public CA certificates for RDS |

The images are pinned by digest in [`deploy/lib/catalog.sh`](deploy/lib/catalog.sh) and pulled by the cluster from their publishers; the tools are downloaded into the git-ignored `.tools/`. None of them is in this repository or in the server's image. The serverless form runs no image but the server's.

## Test environments

| Name | Upstream | Declared license | Used by | Notes |
| --- | --- | --- | --- | --- |
| `aws-load-balancer-controller` | chart 3.6.0 from `aws.github.io/eks-charts`, image `public.ecr.aws/eks/aws-load-balancer-controller:v3.6.0` | Apache-2.0 | [`tests/environment/`](tests/environment/README.md) | The chart checked by SHA-256, the image pinned by digest |
| `busybox` | `public.ecr.aws/docker/library/busybox` | GPL-2.0 | `tests/environment/`: the NetworkPolicy check | BusyBox 1.37.0 (glibc) on Debian 13; pinned by digest only |
| `python` | `public.ecr.aws/docker/library/python:3.13.18-alpine3.24` | PSF-2.0 (Python); Alpine Linux's packages, BusyBox among them (GPL-2.0) | [`tests/uat/`](tests/uat/README.md): the UAT harness's probe pods, which run its own `probe.py` with Python's standard library only | Pinned by the digest of its multi-architecture index; run, not redistributed |
| `postgres` | `postgres:16-alpine` | PostgreSQL License | `tests/uat/`: psql on the metadata database (the EKS form) | The image the install runs, pinned the same |
| `colvenna-catalog-test-driver` | the server's image, with the driver and `org.apache.paimon:paimon-hadoop-uber` 2.0.0 added | Apache-2.0 | [`tests/serverless/`](tests/serverless/run.sh): the serverless form's live test, in the maintainer's account | `paimon-hadoop-uber` is a shaded Hadoop bundle; what it bundles was not examined (NOT_RUN) |

## The compatibility matrix

The matrix ([`tests/compat/`](tests/compat/README.md), [`cells.json`](tests/compat/cells.json)) runs each engine's official image, pinned by digest, with the cell's Paimon jars from Maven Central, pinned by SHA-256, wherever its user runs it (Docker on a host, or pods of a cluster). None of them is in this repository or in the server's image. The flows in [`tests/compat/flows/`](tests/compat/flows/pom.xml) are this project's code; they compile against `paimon-bundle`, Flink and Spark as `provided` dependencies, which the images and the cell's jars supply when they run. So are the UAT harness's engine flows in [`tests/uat/flows/`](tests/uat/flows/pom.xml) (`paimon-bundle` 2.0.0, Flink 1.20.5 and Spark 3.5.9, `provided`); the harness runs them on the matrix's own images and jars, those of the cells `flink-1.20-paimon-2.0.0`, `spark-3.5-paimon-2.0.0` and `doris-4.1.5`. So are the UAT harness's Paimon tools ([`tests/uat/paimon/`](tests/uat/paimon/pom.xml)): they compile against `paimon-bundle` 2.0.0 and Flink 1.20.5's table API as `provided` dependencies and run in the cell `flink-1.20-paimon-2.0.0`'s image with its jars. So is the performance harness's load tool ([`tests/performance/load/`](tests/performance/load/pom.xml)): it compiles against `paimon-bundle` 2.0.0 as a `provided` dependency and runs in the same cell's image with its jars.

| Name | Upstream | Declared license | Notes |
| --- | --- | --- | --- |
| Flink 1.20 | `docker.io/library/flink:1.20.5-scala_2.12-java17` | Apache-2.0 | The official image ([apache/flink-docker](https://github.com/apache/flink-docker)); Java 17 from Eclipse Temurin (GPL-2.0 with the Classpath Exception) |
| Flink 2.2 | `docker.io/library/flink:2.2.1-scala_2.12-java17` | Apache-2.0 | As above |
| Spark 3.5 | `docker.io/apache/spark:3.5.9-scala2.12-java17-ubuntu` | Apache-2.0 | The official image ([apache/spark-docker](https://github.com/apache/spark-docker)), on Ubuntu |
| Spark 4.1 | `docker.io/apache/spark:4.1.3-scala2.13-java17-ubuntu` | Apache-2.0 | As above |
| Doris 4.1.5 FE | `docker.io/apache/doris:fe-4.1.5` | Apache-2.0 | The official image; it bundles Paimon 1.4.2 |
| Doris 4.1.5 BE | `docker.io/apache/doris:be-4.1.5` | Apache-2.0 | As above |
| StarRocks 4.1.6 | `docker.io/starrocks/allin1-ubuntu:4.1.6` | Apache-2.0 | The official all-in-one image ([StarRocks/starrocks](https://github.com/StarRocks/starrocks)); it bundles Paimon 1.3.1 |
| curl | `docker.io/curlimages/curl:8.16.0` | curl license (MIT-style) | The same image the install runs |
| Maven | `docker.io/library/maven:3.9.11-eclipse-temurin-21` | Apache-2.0 | Builds the flows where no Maven is installed; Eclipse Temurin JDK 21 (GPL-2.0 with the Classpath Exception) |

| Jar (Maven Central) | Versions | Declared license | Notes |
| --- | --- | --- | --- |
| `org.apache.paimon:paimon-flink-1.20` | 2.0.0, 2.1.0 | Apache-2.0 | Shaded; what it bundles was not examined (NOT_RUN) |
| `org.apache.paimon:paimon-flink-2.2` | 2.1.0 | Apache-2.0 | Shaded; what it bundles was not examined (NOT_RUN) |
| `org.apache.paimon:paimon-hadoop-uber` | 2.0.0, 2.1.0 | Apache-2.0 | A shaded Hadoop bundle; what it bundles was not examined (NOT_RUN) |
| `org.apache.paimon:paimon-s3` | 2.0.0, 2.1.0 | Apache-2.0 | Shaded: 2.0.0 is the server image's, its content recorded above; what 2.1.0 bundles was not examined (NOT_RUN) |
| `org.apache.paimon:paimon-spark-3.5_2.12` | 2.0.0, 2.1.0 | Apache-2.0 | Shaded; what it bundles was not examined (NOT_RUN) |
| `org.apache.paimon:paimon-spark-4.1_2.13` | 2.1.0 | Apache-2.0 | Shaded; what it bundles was not examined (NOT_RUN) |

## The CDK app's packages (deploy time)

The AWS CDK app in [`deploy/aws/`](deploy/aws/package.json) runs on the machine that installs: `install.sh` installs these packages from the npm registry with `npm ci`, as the lockfile pins them. They are not in the server's image. Some of them are deployed into the account, though:

- the serverless form's authorizer and ops functions (`deploy/aws/functions/`) are bundled by esbuild with `pg`, `@aws-sdk/rds-signer` and their dependencies, all MIT, Apache-2.0, ISC or 0BSD (the "dependency" rows below that the two packages pull in). Whether the bundles keep the packages' license comments was not checked (NOT_RUN);
- some packages are AWS Lambda layers that a stack using them uploads (the test environments' EKS cluster uses `@aws-cdk/lambda-layer-kubectl-v36`).

The scan's, from the lockfile:

<!-- licenses:npm:start -->

| Package | Version | Declared license | Installed as |
| --- | --- | --- | --- |
| `@aws-cdk/asset-awscli-v1` | 2.2.292 | Apache-2.0 | dependency |
| `@aws-cdk/asset-node-proxy-agent-v6` | 2.1.3 | Apache-2.0 | dependency |
| `@aws-cdk/cloud-assembly-api` | 2.5.0 | Apache-2.0 | bundled in aws-cdk-lib |
| `@aws-cdk/cloud-assembly-schema` | 55.2.0 | Apache-2.0 | dependency |
| `@aws-cdk/lambda-layer-kubectl-v36` | 2.0.1 | Apache-2.0 | dependency |
| `@aws-sdk/core` | 3.978.6 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-cognito-identity` | 3.972.76 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-env` | 3.972.77 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-http` | 3.972.79 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-ini` | 3.973.22 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-login` | 3.972.84 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-node` | 3.972.89 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-process` | 3.972.77 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-sso` | 3.973.21 | Apache-2.0 | dependency |
| `@aws-sdk/credential-provider-web-identity` | 3.972.83 | Apache-2.0 | dependency |
| `@aws-sdk/credential-providers` | 3.1158.0 | Apache-2.0 | dependency |
| `@aws-sdk/nested-clients` | 3.997.51 | Apache-2.0 | dependency |
| `@aws-sdk/rds-signer` | 3.1158.0 | Apache-2.0 | dependency |
| `@aws-sdk/signature-v4-multi-region` | 3.996.49 | Apache-2.0 | dependency |
| `@aws-sdk/token-providers` | 3.1157.0 | Apache-2.0 | dependency |
| `@aws-sdk/types` | 3.975.0 | Apache-2.0 | dependency |
| `@aws-sdk/xml-builder` | 3.972.41 | Apache-2.0 | dependency |
| `@aws/cloudformation-validate` | 1.12.1 | Apache-2.0 | bundled in aws-cdk-lib |
| `@aws/lambda-invoke-store` | 0.3.0 | Apache-2.0 | dependency |
| `@balena/dockerignore` | 1.0.2 | Apache-2.0 | bundled in aws-cdk-lib |
| `@cspotcode/source-map-support` | 0.8.1 | MIT | dev |
| `@esbuild/aix-ppc64` | 0.28.2 | MIT | dev |
| `@esbuild/android-arm` | 0.28.2 | MIT | dev |
| `@esbuild/android-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/android-x64` | 0.28.2 | MIT | dev |
| `@esbuild/darwin-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/darwin-x64` | 0.28.2 | MIT | dev |
| `@esbuild/freebsd-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/freebsd-x64` | 0.28.2 | MIT | dev |
| `@esbuild/linux-arm` | 0.28.2 | MIT | dev |
| `@esbuild/linux-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/linux-ia32` | 0.28.2 | MIT | dev |
| `@esbuild/linux-loong64` | 0.28.2 | MIT | dev |
| `@esbuild/linux-mips64el` | 0.28.2 | MIT | dev |
| `@esbuild/linux-ppc64` | 0.28.2 | MIT | dev |
| `@esbuild/linux-riscv64` | 0.28.2 | MIT | dev |
| `@esbuild/linux-s390x` | 0.28.2 | MIT | dev |
| `@esbuild/linux-x64` | 0.28.2 | MIT | dev |
| `@esbuild/netbsd-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/netbsd-x64` | 0.28.2 | MIT | dev |
| `@esbuild/openbsd-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/openbsd-x64` | 0.28.2 | MIT | dev |
| `@esbuild/openharmony-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/sunos-x64` | 0.28.2 | MIT | dev |
| `@esbuild/win32-arm64` | 0.28.2 | MIT | dev |
| `@esbuild/win32-ia32` | 0.28.2 | MIT | dev |
| `@esbuild/win32-x64` | 0.28.2 | MIT | dev |
| `@jridgewell/resolve-uri` | 3.1.2 | MIT | dev |
| `@jridgewell/sourcemap-codec` | 1.6.0 | MIT | dev |
| `@jridgewell/trace-mapping` | 0.3.9 | MIT | dev |
| `@smithy/core` | 3.37.0 | Apache-2.0 | dependency |
| `@smithy/credential-provider-imds` | 4.5.2 | Apache-2.0 | dependency |
| `@smithy/fetch-http-handler` | 5.8.0 | Apache-2.0 | dependency |
| `@smithy/node-http-handler` | 4.13.0 | Apache-2.0 | dependency |
| `@smithy/signature-v4` | 5.7.4 | Apache-2.0 | dependency |
| `@smithy/types` | 4.19.0 | Apache-2.0 | dependency |
| `@tsconfig/node10` | 1.0.13 | MIT | dev |
| `@tsconfig/node12` | 1.0.11 | MIT | dev |
| `@tsconfig/node14` | 1.0.3 | MIT | dev |
| `@tsconfig/node16` | 1.0.4 | MIT | dev |
| `@types/aws-lambda` | 8.10.164 | MIT | dev |
| `@types/node` | 22.18.6 | MIT | dev |
| `@types/pg` | 8.23.1 | MIT | dev |
| `acorn` | 8.19.0 | MIT | dev |
| `acorn-walk` | 8.3.5 | MIT | dev |
| `arg` | 4.1.3 | MIT | dev |
| `aws-cdk` | 2.1146.0 | Apache-2.0 | dev |
| `aws-cdk-lib` | 2.274.0 | Apache-2.0 | dependency |
| `balanced-match` | 4.0.4 | MIT | bundled in aws-cdk-lib |
| `bowser` | 2.15.0 | MIT | dependency |
| `brace-expansion` | 5.0.12 | MIT | bundled in aws-cdk-lib |
| `case` | 1.6.3 | (MIT OR GPL-3.0-or-later) | bundled in aws-cdk-lib |
| `cdk-nag` | 2.38.2 | Apache-2.0 | dependency |
| `constructs` | 10.8.1 | Apache-2.0 | dependency |
| `create-require` | 1.1.1 | MIT | dev |
| `diff` | 4.0.4 | BSD-3-Clause | dev |
| `esbuild` | 0.28.2 | MIT | dev |
| `fs-extra` | 11.4.1 | MIT | bundled in aws-cdk-lib |
| `graceful-fs` | 4.2.11 | ISC | bundled in aws-cdk-lib |
| `ignore` | 5.3.2 | MIT | bundled in aws-cdk-lib |
| `json-source-map` | 0.6.1 | MIT | bundled in aws-cdk-lib |
| `jsonfile` | 6.2.1 | MIT | bundled in aws-cdk-lib |
| `jsonschema` | 1.5.0 | MIT | bundled in @aws-cdk/cloud-assembly-schema |
| `jsonschema` | 1.5.0 | MIT | bundled in aws-cdk-lib |
| `make-error` | 1.3.6 | ISC | dev |
| `mime-db` | 1.52.0 | MIT | bundled in aws-cdk-lib |
| `mime-types` | 2.1.35 | MIT | bundled in aws-cdk-lib |
| `minimatch` | 10.2.6 | BlueOak-1.0.0 | bundled in aws-cdk-lib |
| `pg` | 8.23.1 | MIT | dependency |
| `pg-cloudflare` | 1.4.1 | MIT | dependency |
| `pg-connection-string` | 2.14.1 | MIT | dependency |
| `pg-int8` | 1.0.1 | ISC | dependency |
| `pg-pool` | 3.14.0 | MIT | dependency |
| `pg-protocol` | 1.16.1 | MIT | dependency |
| `pg-types` | 2.2.0 | MIT | dependency |
| `pgpass` | 1.0.5 | MIT | dependency |
| `postgres-array` | 2.0.0 | MIT | dependency |
| `postgres-bytea` | 1.0.1 | MIT | dependency |
| `postgres-date` | 1.0.7 | MIT | dependency |
| `postgres-interval` | 1.2.0 | MIT | dependency |
| `punycode` | 2.3.1 | MIT | bundled in aws-cdk-lib |
| `semver` | 7.8.5 | ISC | bundled in @aws-cdk/cloud-assembly-schema |
| `semver` | 7.8.5 | ISC | bundled in aws-cdk-lib |
| `split2` | 4.2.0 | ISC | dependency |
| `ts-node` | 10.9.2 | MIT | dev |
| `tslib` | 2.8.1 | 0BSD | dependency |
| `typescript` | 5.9.3 | Apache-2.0 | dev |
| `undici-types` | 6.21.0 | MIT | dev |
| `universalify` | 2.0.1 | MIT | bundled in aws-cdk-lib |
| `v8-compile-cache-lib` | 3.0.1 | MIT | dev |
| `xtend` | 4.0.2 | MIT | dependency |
| `yaml` | 1.10.3 | ISC | bundled in aws-cdk-lib |
| `yaml` | 2.9.1 | ISC | dependency |
| `yn` | 3.1.1 | MIT | dev |

<!-- licenses:npm:end -->
