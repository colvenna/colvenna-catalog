package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.ThrowableProxy;
import ch.qos.logback.core.read.ListAppender;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BiPredicate;
import org.apache.paimon.fs.Path;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.core.exception.ApiCallTimeoutException;

/**
 * The snapshot publisher's S3 client ends its calls by the configured timeouts, not by the SDK's
 * defaults, which set no limit on a call and wait 30 seconds for each read.
 *
 * <p>Nothing here measures how long a call takes (#10), which depends on the host's load. A
 * stand-in for S3 decides which requests are never answered, and the tests read what ended each
 * call: the exception, which names the call timeout it ran out of, and the requests the store
 * received. The timeouts the tests rely on to end a call are seconds, so that an answer on a loaded
 * host still comes in time.
 */
class S3ClientFactoryTest {

    private static final Map<String, String> STATIC_KEYS =
            Map.of(
                    "s3.region", "us-west-2",
                    "s3.path.style.access", "true",
                    "s3.access-key", "AKIAEXAMPLE",
                    "s3.secret-key", "secret");
    private static final String SNAPSHOT = "s3://bucket/warehouse/db.db/t/snapshot/snapshot-1";
    private static final String PUT_SNAPSHOT = "PUT /bucket/warehouse/db.db/t/snapshot/snapshot-1";

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
    void aStoreThatNeverAnswersFailsThePublishByTheCallTimeout() throws Exception {
        ListAppender<ILoggingEvent> warnings = new ListAppender<>();
        Logger log = (Logger) LoggerFactory.getLogger(SnapshotPublisher.class);
        warnings.start();
        log.addAppender(warnings);
        try (Store store = new Store((request, n) -> true);
                SnapshotPublisher.S3ClientFactory factory =
                        new SnapshotPublisher.S3ClientFactory(
                                store.options(), Duration.ofSeconds(1), Duration.ofMillis(400))) {
            // The conditional write's probe, the first request, runs out of the call's 1 second.
            assertThatThrownBy(
                            () ->
                                    SnapshotPublisher.forWarehouse(factory)
                                            .createIfAbsent(null, new Path(SNAPSHOT), "{}"))
                    .isInstanceOf(ApiCallTimeoutException.class)
                    .hasMessageContaining("1000 millis");
            // Its clean-up was sent, and ran out of a call's 1 second as well.
            String probe = store.requests().get(0);
            assertThat(probe).startsWith("PUT ").contains(".colvenna-conditional-write-probe-");
            assertThat(store.received("DELETE " + probe.substring("PUT ".length()))).isTrue();
            assertThat(warnings.list)
                    .singleElement()
                    .satisfies(
                            warning -> {
                                assertThat(warning.getFormattedMessage())
                                        .startsWith("Could not delete probe object");
                                assertThat(
                                                ((ThrowableProxy) warning.getThrowableProxy())
                                                        .getThrowable())
                                        .isInstanceOf(ApiCallTimeoutException.class)
                                        .hasMessageContaining("1000 millis");
                            });
        } finally {
            log.detachAppender(warnings);
        }
    }

    @Test
    void anAttemptThatGetsNoAnswerIsRetriedWithinTheCall() throws Exception {
        // The snapshot's first put is never answered; the attempt timeout ends it, and the call
        // retries it. The call's 20 seconds are less than the HTTP client's 30-second read timeout,
        // so nothing else ends that attempt in time. Every request is held to the attempt timeout,
        // the answered ones too, so it is seconds: an answer on a loaded host is not taken for
        // none.
        try (Store store = new Store((request, n) -> request.equals(PUT_SNAPSHOT) && n == 1);
                SnapshotPublisher.S3ClientFactory factory =
                        new SnapshotPublisher.S3ClientFactory(
                                store.options(), Duration.ofSeconds(20), Duration.ofSeconds(5))) {
            assertThat(
                            SnapshotPublisher.forWarehouse(factory)
                                    .createIfAbsent(null, new Path(SNAPSHOT), "{}"))
                    .isEqualTo(SnapshotPublisher.Outcome.CREATED);
            assertThat(store.requests()).filteredOn(PUT_SNAPSHOT::equals).hasSize(2);
        }
    }

    /**
     * A stand-in for S3 that answers conditional puts and deletes, and never answers a request
     * {@code unanswered} names: it gets the request ({@code <method> <path>}) and how many times
     * the store has received it, this time included.
     */
    private static final class Store implements AutoCloseable {
        private final BiPredicate<String, Integer> unanswered;
        private final HttpServer server;
        private final CountDownLatch closing = new CountDownLatch(1);
        private final Set<String> objects = ConcurrentHashMap.newKeySet();
        private final List<String> requests = new ArrayList<>();

        Store(BiPredicate<String, Integer> unanswered) throws IOException {
            this.unanswered = unanswered;
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
            server.createContext("/", this::handle);
            server.start();
        }

        Map<String, String> options() {
            Map<String, String> options = new HashMap<>(STATIC_KEYS);
            options.put("s3.endpoint", "http://127.0.0.1:" + server.getAddress().getPort());
            return options;
        }

        List<String> requests() {
            synchronized (requests) {
                return List.copyOf(requests);
            }
        }

        /**
         * Whether the store has received {@code request}, waiting for it a while: a client that
         * gives up on a request has sent it, but its handler may not have run yet.
         */
        boolean received(String request) throws InterruptedException {
            synchronized (requests) {
                for (int i = 0; i < 600 && !requests.contains(request); i++) {
                    requests.wait(100);
                }
                return requests.contains(request);
            }
        }

        private void handle(HttpExchange exchange) throws IOException {
            String path = exchange.getRequestURI().getPath();
            String request = exchange.getRequestMethod() + " " + path;
            int times;
            synchronized (requests) {
                requests.add(request);
                times = Collections.frequency(requests, request);
                requests.notifyAll();
            }
            if (unanswered.test(request, times)) {
                try {
                    // Not answered: the client gives up on its own.
                    closing.await(5, TimeUnit.MINUTES);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return;
            }
            try (exchange) {
                exchange.getRequestBody().readAllBytes();
                if (exchange.getRequestMethod().equals("DELETE")) {
                    objects.remove(path);
                    exchange.sendResponseHeaders(204, -1);
                } else if (objects.add(path)) {
                    exchange.getResponseHeaders().add("ETag", "\"0\"");
                    exchange.sendResponseHeaders(200, -1);
                } else {
                    byte[] body =
                            ("<Error><Code>PreconditionFailed</Code><Message>At least one of the"
                                            + " preconditions you specified did not hold</Message>"
                                            + "</Error>")
                                    .getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().add("Content-Type", "application/xml");
                    exchange.sendResponseHeaders(412, body.length);
                    exchange.getResponseBody().write(body);
                }
            }
        }

        @Override
        public void close() {
            closing.countDown();
            server.stop(0);
        }
    }
}
