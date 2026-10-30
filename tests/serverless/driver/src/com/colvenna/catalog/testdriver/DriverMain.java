package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The serverless form's live-test driver (#13), run as a Lambda function inside the catalog's VPC.
 * It is its own Lambda runtime: it takes invocations from the Runtime API and answers each with the
 * JSON result of {@link Driver#handle}. With {@code --local <event.json>} it runs one event outside
 * Lambda and prints the result, to try the actions against a local server.
 */
public final class DriverMain {

    static final ObjectMapper JSON = new ObjectMapper();

    private DriverMain() {}

    public static void main(String[] args) throws Exception {
        if (args.length == 2 && args[0].equals("--local")) {
            JsonNode event = JSON.readTree(Files.readString(Path.of(args[1])));
            System.out.println(
                    JSON.writerWithDefaultPrettyPrinter().writeValueAsString(Driver.handle(event)));
            return;
        }
        String api = System.getenv("AWS_LAMBDA_RUNTIME_API");
        if (api == null || api.isBlank()) {
            System.err.println("Not a Lambda execution environment; use --local <event.json>");
            System.exit(2);
        }
        String base = "http://" + api + "/2018-06-01/runtime/invocation/";
        HttpClient runtime = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
        while (true) {
            HttpResponse<String> next =
                    runtime.send(
                            HttpRequest.newBuilder(URI.create(base + "next")).GET().build(),
                            HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            String id = next.headers().firstValue("Lambda-Runtime-Aws-Request-Id").orElseThrow();
            String path;
            String body;
            try {
                body = JSON.writeValueAsString(Driver.handle(JSON.readTree(next.body())));
                path = id + "/response";
            } catch (Throwable t) {
                // The message names what failed; nothing secret goes into an exception here.
                Map<String, Object> error = new LinkedHashMap<>();
                error.put("errorMessage", String.valueOf(t));
                error.put("errorType", t.getClass().getName());
                body = JSON.writeValueAsString(error);
                path = id + "/error";
                t.printStackTrace();
            }
            runtime.send(
                    HttpRequest.newBuilder(URI.create(base + path))
                            .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                            .build(),
                    HttpResponse.BodyHandlers.discarding());
        }
    }
}
