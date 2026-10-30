package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.InputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.fs.Path;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.core.exception.ApiCallTimeoutException;

/** The snapshot publisher's S3 client ends its calls within the configured timeouts. */
class S3ClientFactoryTest {

    private static final Map<String, String> STATIC_KEYS =
            Map.of(
                    "s3.region", "us-west-2",
                    "s3.path.style.access", "true",
                    "s3.access-key", "AKIAEXAMPLE",
                    "s3.secret-key", "secret");

    @Test
    void theClientUsesTheConfiguredTimeouts() {
        try (SnapshotPublisher.S3ClientFactory factory =
                new SnapshotPublisher.S3ClientFactory(
                        STATIC_KEYS, Duration.ofSeconds(8), Duration.ofSeconds(3))) {
            ClientOverrideConfiguration override =
                    factory.client().serviceClientConfiguration().overrideConfiguration();
            assertThat(override.apiCallTimeout()).contains(Duration.ofSeconds(8));
            assertThat(override.apiCallAttemptTimeout()).contains(Duration.ofSeconds(3));
        }
    }

    @Test
    void aStoreThatNeverAnswersFailsThePublishWithinTheCallTimeout() throws Exception {
        List<Socket> held = java.util.Collections.synchronizedList(new ArrayList<>());
        try (ServerSocket silent = new ServerSocket(0)) {
            Thread acceptor =
                    Thread.ofVirtual()
                            .start(
                                    () -> {
                                        // Accept and read, never answer.
                                        while (!silent.isClosed()) {
                                            try {
                                                Socket socket = silent.accept();
                                                held.add(socket);
                                                InputStream in = socket.getInputStream();
                                                Thread.ofVirtual()
                                                        .start(
                                                                () -> {
                                                                    try {
                                                                        in.transferTo(
                                                                                java.io.OutputStream
                                                                                        .nullOutputStream());
                                                                    } catch (Exception ignored) {
                                                                        // closed by the test
                                                                    }
                                                                });
                                            } catch (Exception closed) {
                                                return;
                                            }
                                        }
                                    });
            Map<String, String> options = new java.util.HashMap<>(STATIC_KEYS);
            options.put("s3.endpoint", "http://127.0.0.1:" + silent.getLocalPort());
            try (SnapshotPublisher.S3ClientFactory factory =
                    new SnapshotPublisher.S3ClientFactory(
                            options, Duration.ofSeconds(1), Duration.ofMillis(400))) {
                long start = System.nanoTime();
                assertThatThrownBy(
                                () ->
                                        SnapshotPublisher.forWarehouse(factory)
                                                .createIfAbsent(
                                                        null,
                                                        new Path(
                                                                "s3://bucket/warehouse/db.db/t/snapshot/snapshot-1"),
                                                        "{}"))
                        .isInstanceOf(ApiCallTimeoutException.class);
                // The put and the clean-up of its probe object, one second each; not 30 + 30.
                assertThat(Duration.ofNanos(System.nanoTime() - start))
                        .isLessThan(Duration.ofSeconds(5));
            }
            acceptor.interrupt();
        } finally {
            for (Socket socket : held) {
                socket.close();
            }
        }
    }
}
