package com.colvenna.catalog.fs;

import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import javax.annotation.Nullable;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.Path;
import org.apache.paimon.fs.local.LocalFileIO;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3ClientBuilder;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.S3Exception;

/**
 * Publishes a snapshot file only if no file exists at its path yet.
 *
 * <p>For streaming readers the snapshot file is the commit: they discover snapshot N by its
 * existence. Create-if-absent therefore has to be atomic in the store itself, not only under the
 * server's PostgreSQL lock, so a delayed write from a replica that lost its lock can never replace
 * a snapshot that was published after it. On S3 this is a conditional {@code PutObject} with {@code
 * If-None-Match: *}; on a local file system it is a hard link of a fully written temporary file.
 */
public interface SnapshotPublisher {

    Logger LOG = LoggerFactory.getLogger(SnapshotPublisher.class);

    /** Result of a create-if-absent attempt. */
    enum Outcome {
        CREATED,
        ALREADY_EXISTS
    }

    Outcome createIfAbsent(FileIO fileIO, Path path, String content) throws IOException;

    /** Chooses the conditional write for the scheme of each path. */
    static SnapshotPublisher forWarehouse(S3ClientFactory s3) {
        return (jailed, path, content) -> {
            FileIO fileIO = jailed instanceof JailedFileIO jail ? jail.unwrapFor(path) : jailed;
            String scheme = path.toUri().getScheme();
            if (scheme == null || "file".equals(scheme) || fileIO instanceof LocalFileIO) {
                return createLocal(path, content);
            }
            if ("s3".equals(scheme) || "s3a".equals(scheme)) {
                return createS3(s3, path.toUri(), content);
            }
            throw new IOException(
                    "No atomic create-if-absent for scheme " + scheme + "; refusing to commit.");
        };
    }

    private static Outcome createLocal(Path path, String content) throws IOException {
        java.nio.file.Path target = Paths.get(path.toUri().getPath());
        Files.createDirectories(target.getParent());
        java.nio.file.Path temp =
                Files.createTempFile(target.getParent(), "." + target.getFileName(), ".tmp");
        try {
            Files.writeString(temp, content, StandardCharsets.UTF_8);
            Files.createLink(target, temp);
            return Outcome.CREATED;
        } catch (FileAlreadyExistsException e) {
            return Outcome.ALREADY_EXISTS;
        } finally {
            Files.deleteIfExists(temp);
        }
    }

    private static Outcome createS3(S3ClientFactory s3, URI uri, String content)
            throws IOException {
        String key = uri.getPath().startsWith("/") ? uri.getPath().substring(1) : uri.getPath();
        String directory = key.contains("/") ? key.substring(0, key.lastIndexOf('/')) : "";
        s3.verifyConditionalWrites(uri.getHost(), directory);
        return putIfAbsent(s3.client(), uri.getHost(), key, content)
                ? Outcome.CREATED
                : Outcome.ALREADY_EXISTS;
    }

    /** Returns {@code false} when the object already exists (HTTP 412). */
    private static boolean putIfAbsent(S3Client client, String bucket, String key, String content)
            throws IOException {
        try {
            client.putObject(
                    PutObjectRequest.builder()
                            .bucket(bucket)
                            .key(key)
                            .ifNoneMatch("*")
                            .contentType("application/json")
                            .build(),
                    RequestBody.fromString(content, StandardCharsets.UTF_8));
            return true;
        } catch (S3Exception e) {
            if (e.statusCode() == 412) {
                return false;
            }
            throw new IOException(
                    "Conditional put of s3://" + bucket + "/" + key + " failed: " + e.getMessage(),
                    e);
        }
    }

    /**
     * Lazily builds the S3 client used for conditional writes from the same endpoint, region,
     * path-style and static credential options the server FileIO uses; without static keys it uses
     * the default AWS credential chain (IRSA on EKS).
     */
    final class S3ClientFactory implements AutoCloseable {
        private final Map<String, String> options;
        private final Duration callTimeout;
        private final Duration attemptTimeout;
        private final Set<String> verifiedBuckets = ConcurrentHashMap.newKeySet();
        private volatile S3Client client;

        /**
         * {@code callTimeout} bounds a whole call, retries included, and {@code attemptTimeout}
         * each attempt ({@code COLVENNA_S3_CALL_TIMEOUT}, {@code
         * COLVENNA_S3_CALL_ATTEMPT_TIMEOUT}).
         */
        public S3ClientFactory(
                Map<String, String> fileIoOptions, Duration callTimeout, Duration attemptTimeout) {
            this.options = Map.copyOf(fileIoOptions);
            this.callTimeout = callTimeout;
            this.attemptTimeout = attemptTimeout;
        }

        S3Client client() {
            S3Client current = client;
            if (current == null) {
                synchronized (this) {
                    current = client;
                    if (current == null) {
                        current = build();
                        client = current;
                    }
                }
            }
            return current;
        }

        private S3Client build() {
            S3ClientBuilder builder =
                    S3Client.builder()
                            .overrideConfiguration(
                                    c ->
                                            c.apiCallTimeout(callTimeout)
                                                    .apiCallAttemptTimeout(attemptTimeout));
            String region = first("s3.region", "s3.endpoint.region", "fs.s3a.endpoint.region");
            if (region == null) {
                region = System.getenv("AWS_REGION");
            }
            if (region != null) {
                builder.region(Region.of(region));
            }
            String endpoint = first("s3.endpoint", "fs.s3a.endpoint");
            if (endpoint != null) {
                builder.endpointOverride(
                        URI.create(endpoint.contains("://") ? endpoint : "https://" + endpoint));
            }
            String pathStyle =
                    first(
                            "s3.path.style.access",
                            "s3.path-style-access",
                            "fs.s3a.path.style.access");
            if (pathStyle != null) {
                builder.forcePathStyle(Boolean.parseBoolean(pathStyle));
            }
            String accessKey = first("s3.access-key", "s3.access.key", "fs.s3a.access.key");
            String secretKey = first("s3.secret-key", "s3.secret.key", "fs.s3a.secret.key");
            String sessionToken =
                    first("s3.session.token", "s3.session-token", "fs.s3a.session.token");
            if (accessKey != null && secretKey != null) {
                builder.credentialsProvider(
                        StaticCredentialsProvider.create(
                                sessionToken == null
                                        ? AwsBasicCredentials.create(accessKey, secretKey)
                                        : AwsSessionCredentials.create(
                                                accessKey, secretKey, sessionToken)));
            }
            return builder.build();
        }

        @Nullable
        private String first(String... keys) {
            for (String key : keys) {
                String value = options.get(key);
                if (value != null && !value.isBlank()) {
                    return value;
                }
            }
            return null;
        }

        /**
         * Proves once per bucket that the store honours {@code If-None-Match: *}: a second
         * conditional put of the same probe object must fail with 412. An S3-compatible store that
         * ignores the header would otherwise turn every commit into a blind overwrite.
         */
        void verifyConditionalWrites(String bucket, String directory) throws IOException {
            if (verifiedBuckets.contains(bucket)) {
                return;
            }
            String key =
                    (directory.isEmpty() ? "" : directory + "/")
                            + ".colvenna-conditional-write-probe-"
                            + UUID.randomUUID();
            try {
                if (!putIfAbsent(client(), bucket, key, "probe")) {
                    throw new IOException("Probe object s3://" + bucket + "/" + key + " exists");
                }
                if (putIfAbsent(client(), bucket, key, "probe")) {
                    throw new IOException(
                            "Bucket "
                                    + bucket
                                    + " accepted a second If-None-Match put of the same object;"
                                    + " it cannot publish snapshots atomically, refusing to"
                                    + " commit.");
                }
            } finally {
                try {
                    client().deleteObject(b -> b.bucket(bucket).key(key));
                } catch (RuntimeException e) {
                    LOG.warn("Could not delete probe object s3://{}/{}", bucket, key, e);
                }
            }
            verifiedBuckets.add(bucket);
        }

        @Override
        public synchronized void close() {
            if (client != null) {
                client.close();
                client = null;
            }
        }
    }
}
