package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.InetAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTCatalog;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

/**
 * How a replica stops (#64): readiness fails and every answer closes its connection at once; the
 * API port goes on taking connections for the drain; then it takes none, and what is in flight has
 * up to the timeout to finish.
 */
class ShutdownTest extends ServerTestBase {

    private static final Duration DRAIN = Duration.ofSeconds(3);
    private static final Duration TIMEOUT = Duration.ofSeconds(6);
    private static final Identifier TABLE = Identifier.create("db", "events");
    private static final String CONFIG = "/v1/config?warehouse=" + TestServers.WAREHOUSE;

    private final CountDownLatch publishHeld = new CountDownLatch(1);
    private final CountDownLatch releasePublish = new CountDownLatch(1);
    private volatile boolean hold;
    private final ExecutorService pool = Executors.newCachedThreadPool();

    @Override
    protected Map<String, String> settings() {
        return Map.of(
                "COLVENNA_SHUTDOWN_DRAIN", DRAIN.toString(),
                "COLVENNA_SHUTDOWN_TIMEOUT", TIMEOUT.toString());
    }

    @Override
    protected CommitService.Faults faults() {
        return new CommitService.Faults() {
            @Override
            public void beforePublish(TableRecord table, Snapshot snapshot) {
                if (!hold) {
                    return;
                }
                publishHeld.countDown();
                try {
                    releasePublish.await(60, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        };
    }

    @AfterEach
    void releaseEverything() {
        releasePublish.countDown();
        pool.shutdownNow();
    }

    /** Takes the server from the base class, which then does not close it: the test does. */
    private ColvennaCatalogServer takeServer() {
        ColvennaCatalogServer stopping = server;
        server = null;
        return stopping;
    }

    @Test
    void readinessFailsFirstAndTheDrainStillTakesConnectionsThatItCloses() throws Exception {
        int port = server.port();
        int health = server.healthPort();
        assertThat(http(health, "GET", "/readyz", null, null).statusCode()).isEqualTo(200);
        try (Socket kept = new Socket(InetAddress.getLoopbackAddress(), port)) {
            // A client's pooled connection, made before the stop: kept open after its answer.
            RawAnswer before = get(kept, CONFIG, adminToken);
            assertThat(before.status).isEqualTo(200);
            assertThat(before.header("connection")).isNull();
            ColvennaCatalogServer stopping = takeServer();
            long began = System.nanoTime();
            Future<?> closed = pool.submit(stopping::close);
            awaitReadyz(health, 503);
            // Its next request is answered, and the answer closes it: the client reconnects.
            RawAnswer next = get(kept, CONFIG, adminToken);
            assertThat(next.status).isEqualTo(200);
            assertThat(next.header("connection")).isEqualTo("close");
            assertThat(next.closedAfter).isTrue();
            // A connection made during the drain is taken and answered, and the answer closes it.
            RawAnswer answer;
            try (Socket fresh = new Socket(InetAddress.getLoopbackAddress(), port)) {
                answer = get(fresh, CONFIG, adminToken);
            }
            assertThat(elapsed(began)).isLessThan(DRAIN);
            assertThat(answer.status).isEqualTo(200);
            assertThat(answer.header("connection")).isEqualTo("close");
            assertThat(answer.closedAfter).isTrue();
            awaitClosedThenRefused(closed, began, port);
        }
    }

    private void awaitClosedThenRefused(Future<?> closed, long began, int port) throws Exception {
        closed.get(30, TimeUnit.SECONDS);
        // Nothing was in flight: the drain, then at once no connection is taken.
        assertThat(elapsed(began)).isGreaterThanOrEqualTo(DRAIN).isLessThan(DRAIN.plus(TIMEOUT));
        assertThatThrownBy(() -> new Socket(InetAddress.getLoopbackAddress(), port).close())
                .isInstanceOf(ConnectException.class);
    }

    @Test
    void aCommitInFlightAfterTheDrainFinishesAndIsAnswered() throws Exception {
        int port = server.port();
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        hold = true;
        Future<?> write = pool.submit(() -> write(catalog));
        assertThat(publishHeld.await(30, TimeUnit.SECONDS)).isTrue();
        ColvennaCatalogServer stopping = takeServer();
        long began = System.nanoTime();
        Future<?> closed = pool.submit(stopping::close);
        // The drain ends: new connections are refused while the commit is still in flight.
        awaitRefused(port, DRAIN.plusSeconds(2));
        assertThat(elapsed(began)).isGreaterThanOrEqualTo(DRAIN);
        assertThat(closed.isDone()).isFalse();
        releasePublish.countDown();
        closed.get(30, TimeUnit.SECONDS);
        assertThat(elapsed(began)).isLessThan(DRAIN.plus(TIMEOUT));
        // The commit took effect and was answered before the connections were closed; the
        // client's next request, on a new connection, finds the port closed.
        write.get(30, TimeUnit.SECONDS);
        server = startReplica();
        assertThat(commitAudit()).containsExactly("SUCCEEDED 200");
    }

    @Test
    void whatIsStillInFlightAtTheTimeoutIsCut() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        hold = true;
        Future<?> write = pool.submit(() -> write(catalog));
        assertThat(publishHeld.await(30, TimeUnit.SECONDS)).isTrue();
        ColvennaCatalogServer stopping = takeServer();
        long began = System.nanoTime();
        stopping.close();
        Duration took = elapsed(began);
        // The server interrupted the commit's handler and closed its connection: the client's
        // write ends.
        write.get(30, TimeUnit.SECONDS);
        assertThat(took)
                .isGreaterThanOrEqualTo(DRAIN.plus(TIMEOUT))
                .isLessThan(Duration.ofSeconds(20));
    }

    /** Writes a row, whatever becomes of the client's requests after the commit. */
    private static Void write(RESTCatalog catalog) {
        try {
            Tables.write(catalog.getTable(TABLE), 1);
        } catch (Exception e) {
            // The commit's answer closes its connection; what the client asks next finds no port.
        }
        return null;
    }

    private List<String> commitAudit() throws Exception {
        List<String> out = new ArrayList<>();
        auditEvents("resource=db.events")
                .forEach(
                        e -> {
                            if (e.get("action").asText().equals("COMMIT")) {
                                out.add(
                                        e.get("outcome").asText()
                                                + " "
                                                + e.get("httpStatus").asInt());
                            }
                        });
        return out;
    }

    private static Duration elapsed(long began) {
        return Duration.ofNanos(System.nanoTime() - began);
    }

    private static void awaitReadyz(int health, int status) throws Exception {
        long until = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (http(health, "GET", "/readyz", null, null).statusCode() != status) {
            assertThat(System.nanoTime()).as("/readyz answering %d", status).isLessThan(until);
            Thread.sleep(20);
        }
    }

    private static void awaitRefused(int port, Duration within) throws Exception {
        long until = System.nanoTime() + within.toNanos();
        while (true) {
            try (Socket ignored = new Socket(InetAddress.getLoopbackAddress(), port)) {
                assertThat(System.nanoTime()).as("connections refused").isLessThan(until);
                Thread.sleep(50);
            } catch (ConnectException refused) {
                return;
            }
        }
    }

    /** An HTTP/1.1 answer read off a raw socket, and whether the server then closed it. */
    private static final class RawAnswer {
        int status;
        final Map<String, String> headers = new HashMap<>();
        boolean closedAfter;

        String header(String name) {
            return headers.get(name);
        }
    }

    /**
     * GET on the keep-alive connection {@code socket}; reads the answer, and, if it asked to close
     * the connection, whether the server then closed it.
     */
    private static RawAnswer get(Socket socket, String path, String token) throws IOException {
        socket.setSoTimeout(10_000);
        OutputStream out = socket.getOutputStream();
        out.write(
                ("GET "
                                + path
                                + " HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                                + token
                                + "\r\n\r\n")
                        .getBytes(StandardCharsets.US_ASCII));
        out.flush();
        BufferedReader in =
                new BufferedReader(
                        new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
        RawAnswer raw = new RawAnswer();
        raw.status = Integer.parseInt(in.readLine().split(" ", 3)[1]);
        for (String line = in.readLine(); !line.isEmpty(); line = in.readLine()) {
            int colon = line.indexOf(':');
            raw.headers.put(
                    line.substring(0, colon).trim().toLowerCase(Locale.ROOT),
                    line.substring(colon + 1).trim());
        }
        int length = Integer.parseInt(raw.headers.getOrDefault("content-length", "0"));
        for (int i = 0; i < length; i++) {
            in.read();
        }
        raw.closedAfter = "close".equals(raw.header("connection")) && in.read() == -1;
        return raw;
    }
}
