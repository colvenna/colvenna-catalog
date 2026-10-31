package com.colvenna.catalog.http;

import static org.assertj.core.api.Assertions.assertThat;

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
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
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
 *
 * <p>The checks read {@code TCP_NODELAY} on the connections the server accepted, as it answers on
 * them, which is what turns Nagle's algorithm off ({@link StandardSocketOptions#TCP_NODELAY}). They
 * do not time the answers: what Nagle's algorithm costs depends on the host's delayed ACKs and its
 * load, and a comparison of times failed on a busy host (#66).
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

    /**
     * The answers #37 found 40 ms slower are those after the first on a kept-alive connection: each
     * of them, and each on a new connection, goes out on a connection with TCP_NODELAY.
     */
    @Test
    void soEveryAnswerGoesOutWithNagleOffOnAKeptAliveConnectionOrANewOne() throws Exception {
        String answers = String.valueOf(Probe.KEPT_ALIVE + Probe.NEW_CONNECTIONS);
        Map<String, String> on = probe(true);
        assertThat(on)
                .containsEntry("answers", answers)
                .containsEntry("connections", String.valueOf(1 + Probe.NEW_CONNECTIONS))
                .containsEntry("keptAlive", String.valueOf(Probe.KEPT_ALIVE))
                .containsEntry("tcpNoDelays", "true");
        Map<String, String> off = probe(false);
        assertThat(off) // the control: the JDK's default, on every connection
                .containsEntry("answers", answers)
                .containsEntry("tcpNoDelays", "false");
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
     * In a fresh JVM: loads RestServer (or not), serves a 2 KiB answer {@link #KEPT_ALIVE} times on
     * one kept-alive connection and once on each of {@link #NEW_CONNECTIONS} new ones, and prints
     * the property, whether the first accepted connection has TCP_NODELAY, and, over every answer,
     * how many there were, on how many connections, how many on the one kept alive longest, and the
     * TCP_NODELAY values their connections had as each was answered.
     */
    static final class Probe {
        static final int KEPT_ALIVE = 45;
        static final int NEW_CONNECTIONS = 3;

        public static void main(String[] args) throws Exception {
            if (Boolean.parseBoolean(args[0])) {
                Class.forName(RestServer.class.getName());
            }
            List<Answer> answers = new CopyOnWriteArrayList<>();
            HttpServer server =
                    HttpServer.create(
                            new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            byte[] body = new byte[2048];
            Arrays.fill(body, (byte) 'x');
            server.createContext(
                    "/",
                    exchange -> {
                        answers.add(answer(exchange));
                        exchange.sendResponseHeaders(200, body.length);
                        try (OutputStream o = exchange.getResponseBody()) {
                            o.write(body);
                        }
                    });
            server.start();
            try {
                HttpRequest request =
                        HttpRequest.newBuilder(
                                        URI.create(
                                                "http://127.0.0.1:"
                                                        + server.getAddress().getPort()
                                                        + "/"))
                                .build();
                // One client reuses its connection; each new client opens one of its own.
                try (HttpClient client = client()) {
                    for (int i = 0; i < KEPT_ALIVE; i++) {
                        client.send(request, HttpResponse.BodyHandlers.ofByteArray());
                    }
                }
                for (int i = 0; i < NEW_CONNECTIONS; i++) {
                    try (HttpClient client = client()) {
                        client.send(request, HttpResponse.BodyHandlers.ofByteArray());
                    }
                }
                // The server's own channel of each connection, compared by identity: one per
                // connection it accepted (the answers hold them, so none is reclaimed).
                IdentityHashMap<SocketChannel, Integer> perConnection = new IdentityHashMap<>();
                answers.forEach(a -> perConnection.merge(a.channel(), 1, Integer::sum));
                System.out.println("nodelay=" + System.getProperty(RestServer.NODELAY));
                System.out.println("tcpNoDelay=" + answers.get(0).noDelay());
                System.out.println("answers=" + answers.size());
                System.out.println("connections=" + perConnection.size());
                System.out.println("keptAlive=" + Collections.max(perConnection.values()));
                System.out.println(
                        "tcpNoDelays="
                                + answers.stream()
                                        .map(a -> String.valueOf(a.noDelay()))
                                        .distinct()
                                        .sorted()
                                        .collect(Collectors.joining(",")));
            } finally {
                server.stop(0);
            }
        }

        private static HttpClient client() {
            return HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
        }

        /**
         * An answer: the server's channel of the connection it went out on, and its TCP_NODELAY.
         */
        record Answer(SocketChannel channel, boolean noDelay) {}

        /**
         * The exchange's connection and its TCP_NODELAY, read through the JDK's own classes
         * (opened).
         */
        private static Answer answer(HttpExchange exchange) {
            try {
                Field impl = exchange.getClass().getDeclaredField("impl");
                impl.setAccessible(true);
                Object exchangeImpl = impl.get(exchange);
                Method connection = exchangeImpl.getClass().getDeclaredMethod("getConnection");
                connection.setAccessible(true);
                Object conn = connection.invoke(exchangeImpl);
                Method getChannel = conn.getClass().getDeclaredMethod("getChannel");
                getChannel.setAccessible(true);
                SocketChannel channel = (SocketChannel) getChannel.invoke(conn);
                return new Answer(channel, channel.getOption(StandardSocketOptions.TCP_NODELAY));
            } catch (ReflectiveOperationException | java.io.IOException e) {
                throw new IllegalStateException(e);
            }
        }
    }
}
