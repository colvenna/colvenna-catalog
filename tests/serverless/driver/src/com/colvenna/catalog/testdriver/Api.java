package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/** Plain HTTPS requests to the catalog's base URL (the private API's stage URL). */
final class Api {

    /** One answer: its status (0: no answer), how long it took, and its body. */
    record Answer(int status, long millis, String body) {
        JsonNode json() throws Exception {
            return DriverMain.JSON.readTree(body);
        }

        /** The answer as evidence: status, time, and the start of the body. */
        Map<String, Object> summary() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("status", status);
            out.put("ms", millis);
            out.put("body", body.length() > 200 ? body.substring(0, 200) : body);
            return out;
        }
    }

    private final String base;
    private final HttpClient client =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

    Api(String base) {
        this.base = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
    }

    /**
     * A request with the given Authorization header value (null: none). The body is JSON text or
     * null.
     */
    Answer call(String method, String path, String authorization, String body) {
        HttpRequest.Builder request =
                HttpRequest.newBuilder(URI.create(base + path))
                        .timeout(Duration.ofSeconds(60))
                        .method(
                                method,
                                body == null
                                        ? HttpRequest.BodyPublishers.noBody()
                                        : HttpRequest.BodyPublishers.ofString(
                                                body, StandardCharsets.UTF_8));
        if (body != null) {
            request.header("Content-Type", "application/json");
        }
        if (authorization != null) {
            request.header("Authorization", authorization);
        }
        long start = System.nanoTime();
        try {
            HttpResponse<String> response =
                    client.send(request.build(), HttpResponse.BodyHandlers.ofString());
            return new Answer(response.statusCode(), elapsed(start), response.body());
        } catch (Exception e) {
            return new Answer(
                    0, elapsed(start), e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }

    /** A request with a bearer token. */
    Answer bearer(String method, String path, String token, String body) {
        return call(method, path, "Bearer " + token, body);
    }

    private static long elapsed(long start) {
        return (System.nanoTime() - start) / 1_000_000;
    }
}
