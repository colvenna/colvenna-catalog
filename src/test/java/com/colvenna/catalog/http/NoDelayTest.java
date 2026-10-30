package com.colvenna.catalog.http;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.OutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.StandardSocketOptions;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;

/**
 * The server's connections have Nagle's algorithm off (#37): the JDK's HTTP server sets {@code
 * TCP_NODELAY} on them only when {@code sun.net.httpserver.nodelay} is true when its first server
 * is made, and left on, Nagle's algorithm holds each response's body until the client acknowledges
 * its headers (about 40 ms on Linux). Each check runs in a JVM of its own, since the JDK reads the
 * setting once a JVM; the same server code serves HTTPS ({@code HttpsServer}), the API port's.
 */
@Timeout(value = 3, unit = TimeUnit.MINUTES)
class NoDelayTest {

    @TempDir Path dir;

    @Test
    void theServersConnectionsHaveNagleOffOnceRestServerIsLoaded() throws Exception {
        Map<String, String> on = probe(true);
        assertThat(on).containsEntry("nodelay", "true").containsEntry("tcpNoDelay", "true");
        Map<String, String> off = probe(false);
        assertThat(off).containsEntry("tcpNoDelay", "false"); // the control: the JDK's default
    }

    @Test
    void soAnAnswerOnAKeptAliveConnectionNoLongerWaitsForTheDelayedAck() throws Exception {
        long on = Long.parseLong(probe(true).get("median"));
        long off = Long.parseLong(probe(false).get("median"));
        // Only where the host's TCP delays its ACKs, which makes Nagle's cost visible.
        assumeTrue(off >= 20_000, "no delayed-ACK stall without TCP_NODELAY here: " + off + " us");
        assertThat(on)
                .as("median %d us with Nagle off, %d us with it on", on, off)
                .isLessThan(off / 4);
    }

    private Map<String, String> probe(boolean loadRestServer) throws Exception {
        Path out = dir.resolve("probe-" + loadRestServer + "-" + System.nanoTime() + ".txt");
        List<String> command =
                List.of(
                        Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                        "--add-opens",
                        "jdk.httpserver/sun.net.httpserver=ALL-UNNAMED",
                        "-cp",
                        System.getProperty("java.class.path"),
                        Probe.class.getName(),
                        String.valueOf(loadRestServer));
        Process process =
                new ProcessBuilder(command)
                        .redirectErrorStream(true)
                        .redirectOutput(out.toFile())
                        .start();
        if (!process.waitFor(2, TimeUnit.MINUTES)) {
            process.destroyForcibly();
            throw new AssertionError("the probe did not end within 2 minutes");
        }
        String output = Files.readString(out, StandardCharsets.UTF_8);
        assertThat(process.exitValue()).as(output).isZero();
        return output.lines()
                .filter(l -> l.contains("="))
                .collect(Collectors.toMap(l -> l.split("=", 2)[0], l -> l.split("=", 2)[1]));
    }

    /**
     * In a fresh JVM: loads RestServer (or not), serves a 2 KiB answer, and prints the property,
     * whether the accepted connection has TCP_NODELAY, and the median of 40 answers on it.
     */
    static final class Probe {
        public static void main(String[] args) throws Exception {
            if (Boolean.parseBoolean(args[0])) {
                Class.forName(RestServer.class.getName());
            }
            AtomicReference<Boolean> noDelay = new AtomicReference<>();
            HttpServer server =
                    HttpServer.create(
                            new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            byte[] body = new byte[2048];
            Arrays.fill(body, (byte) 'x');
            server.createContext(
                    "/",
                    exchange -> {
                        noDelay.compareAndSet(null, tcpNoDelay(exchange));
                        exchange.sendResponseHeaders(200, body.length);
                        try (OutputStream o = exchange.getResponseBody()) {
                            o.write(body);
                        }
                    });
            server.start();
            try (HttpClient client =
                    HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()) {
                HttpRequest request =
                        HttpRequest.newBuilder(
                                        URI.create(
                                                "http://127.0.0.1:"
                                                        + server.getAddress().getPort()
                                                        + "/"))
                                .build();
                for (int i = 0; i < 5; i++) {
                    client.send(request, HttpResponse.BodyHandlers.ofByteArray());
                }
                long[] micros = new long[40];
                for (int i = 0; i < micros.length; i++) {
                    long start = System.nanoTime();
                    client.send(request, HttpResponse.BodyHandlers.ofByteArray());
                    micros[i] = (System.nanoTime() - start) / 1000;
                }
                Arrays.sort(micros);
                System.out.println("nodelay=" + System.getProperty(RestServer.NODELAY));
                System.out.println("tcpNoDelay=" + noDelay.get());
                System.out.println("median=" + micros[micros.length / 2]);
            } finally {
                server.stop(0);
            }
        }

        /** The exchange's socket's TCP_NODELAY, read through the JDK's own classes (opened). */
        private static Boolean tcpNoDelay(HttpExchange exchange) {
            try {
                Field impl = exchange.getClass().getDeclaredField("impl");
                impl.setAccessible(true);
                Object exchangeImpl = impl.get(exchange);
                Method connection = exchangeImpl.getClass().getDeclaredMethod("getConnection");
                connection.setAccessible(true);
                Object conn = connection.invoke(exchangeImpl);
                Method channel = conn.getClass().getDeclaredMethod("getChannel");
                channel.setAccessible(true);
                return ((SocketChannel) channel.invoke(conn))
                        .getOption(StandardSocketOptions.TCP_NODELAY);
            } catch (ReflectiveOperationException | java.io.IOException e) {
                throw new IllegalStateException(e);
            }
        }
    }
}
