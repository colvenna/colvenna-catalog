package com.colvenna.catalog.http;

import static org.assertj.core.api.Assertions.assertThat;

import com.sun.net.httpserver.HttpServer;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/**
 * The server's connections have Nagle's algorithm off (#37): with it on, the JDK's HTTP server
 * holds each response's body until the client acknowledges its headers, about 40 ms on a kept-alive
 * connection. Measured in a JVM of its own, since the JDK reads the setting once.
 */
class NoDelayTest {

    /** Answers on one kept-alive connection take less than this at the median with Nagle off. */
    private static final long MEDIAN_LIMIT_MICROS = 20_000;

    @Test
    void loadingTheServerTurnsNagleOffForItsConnections() throws Exception {
        Class.forName(RestServer.class.getName()); // NODELAY is a constant: naming it loads nothing
        assertThat(System.getProperty(RestServer.NODELAY)).isEqualTo("true");
        Measured on = measure(true);
        assertThat(on.medianMicros()).as(on.output()).isLessThan(MEDIAN_LIMIT_MICROS);
    }

    @Test
    void withoutItAKeptAliveAnswerWaitsForTheDelayedAck() throws Exception {
        // The control: the same exchange, RestServer not loaded first, shows the delay the fix
        // removes.
        Measured off = measure(false);
        assertThat(off.medianMicros()).as(off.output()).isGreaterThanOrEqualTo(MEDIAN_LIMIT_MICROS);
    }

    private record Measured(long medianMicros, String output) {}

    private static Measured measure(boolean loadRestServer) throws Exception {
        List<String> command =
                List.of(
                        Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                        "-cp",
                        System.getProperty("java.class.path"),
                        Probe.class.getName(),
                        String.valueOf(loadRestServer));
        Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
        String output;
        try (InputStream in = process.getInputStream()) {
            output = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        assertThat(process.waitFor(2, TimeUnit.MINUTES)).as(output).isTrue();
        assertThat(process.exitValue()).as(output).isZero();
        String line = output.lines().filter(l -> l.startsWith("median=")).findFirst().orElseThrow();
        return new Measured(Long.parseLong(line.substring("median=".length())), output);
    }

    /** In a fresh JVM: optionally loads RestServer, then times 40 answers on one connection. */
    static final class Probe {
        public static void main(String[] args) throws Exception {
            if (Boolean.parseBoolean(args[0])) {
                Class.forName(RestServer.class.getName());
            }
            HttpServer server =
                    HttpServer.create(
                            new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            byte[] body = new byte[2048];
            Arrays.fill(body, (byte) 'x');
            server.createContext(
                    "/",
                    exchange -> {
                        exchange.sendResponseHeaders(200, body.length);
                        try (OutputStream out = exchange.getResponseBody()) {
                            out.write(body);
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
                System.out.println("median=" + micros[micros.length / 2]);
            } finally {
                server.stop(0);
            }
        }
    }
}
