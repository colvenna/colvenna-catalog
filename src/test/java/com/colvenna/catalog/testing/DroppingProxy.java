package com.colvenna.catalog.testing;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Predicate;

/**
 * HTTP proxy in front of a test server that forwards every request but can drop the response of
 * selected ones after the server has processed them, the way a network failure or a client timeout
 * loses a commit acknowledgement.
 */
public final class DroppingProxy implements AutoCloseable {

    private static final Set<String> HOP_HEADERS =
            Set.of("connection", "content-length", "host", "transfer-encoding", "upgrade");

    private final HttpServer server;
    private final HttpClient client = HttpClient.newHttpClient();
    private final String target;
    private final AtomicInteger dropsLeft = new AtomicInteger();
    private final AtomicInteger dropped = new AtomicInteger();
    private volatile Predicate<String> dropWhen = path -> false;

    public DroppingProxy(int targetPort) throws IOException {
        this.target = "http://localhost:" + targetPort;
        this.server = HttpServer.create(new InetSocketAddress(0), 64);
        this.server.createContext("/", this::forward);
        this.server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        this.server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    /** Drops the responses of the next {@code count} requests whose path matches. */
    public void dropResponses(Predicate<String> pathMatches, int count) {
        dropWhen = pathMatches;
        dropsLeft.set(count);
    }

    public int droppedCount() {
        return dropped.get();
    }

    private void forward(HttpExchange exchange) throws IOException {
        try (exchange) {
            String path = exchange.getRequestURI().toString();
            byte[] body;
            try (InputStream in = exchange.getRequestBody()) {
                body = in.readAllBytes();
            }
            HttpRequest.Builder request =
                    HttpRequest.newBuilder(URI.create(target + path))
                            .method(
                                    exchange.getRequestMethod(),
                                    body.length == 0
                                            ? HttpRequest.BodyPublishers.noBody()
                                            : HttpRequest.BodyPublishers.ofByteArray(body));
            exchange.getRequestHeaders()
                    .forEach(
                            (name, values) -> {
                                if (!HOP_HEADERS.contains(
                                        name.toLowerCase(java.util.Locale.ROOT))) {
                                    values.forEach(v -> request.header(name, v));
                                }
                            });
            HttpResponse<byte[]> response;
            try {
                response = client.send(request.build(), HttpResponse.BodyHandlers.ofByteArray());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException(e);
            }
            if (dropWhen.test(exchange.getRequestURI().getPath())
                    && dropsLeft.getAndUpdate(n -> n > 0 ? n - 1 : 0) > 0) {
                dropped.incrementAndGet();
                // Close without a response: the server already processed the request.
                return;
            }
            response.headers()
                    .map()
                    .forEach(
                            (name, values) -> {
                                if (!HOP_HEADERS.contains(name.toLowerCase(java.util.Locale.ROOT))
                                        && !name.startsWith(":")) {
                                    values.forEach(v -> exchange.getResponseHeaders().add(name, v));
                                }
                            });
            byte[] responseBody = response.body();
            exchange.sendResponseHeaders(
                    response.statusCode(), responseBody.length == 0 ? -1 : responseBody.length);
            if (responseBody.length > 0) {
                try (OutputStream out = exchange.getResponseBody()) {
                    out.write(responseBody);
                }
            }
        }
    }

    @Override
    public void close() {
        server.stop(0);
    }
}
