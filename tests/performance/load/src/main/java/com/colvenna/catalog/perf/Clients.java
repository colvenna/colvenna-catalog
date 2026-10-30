package com.colvenna.catalog.perf;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTApi;

/**
 * The catalog as the load reaches it: Paimon's REST catalog with a bearer token and per-table
 * credentials, as terms.md section 2 fixes it ({@code metastore=rest}, {@code token.provider=bear},
 * {@code data-token.enabled=true}). {@code PERF_URI} is the catalog's URI and {@code
 * PERF_WAREHOUSE} its warehouse; the token of a key is the file {@code PERF_TOKENS/<key>}. Nothing
 * else of the client is set: its timeouts and retries are Paimon 2.0.0's own (HttpClientUtils: 3
 * minutes, ExponentialHttpRequestRetryStrategy(5)).
 */
public final class Clients {

    private Clients() {}

    static String token(String key) throws Exception {
        Path dir = Path.of(System.getenv().getOrDefault("PERF_TOKENS", "/secret"));
        String token = Files.readString(dir.resolve(key), StandardCharsets.UTF_8).trim();
        if (token.isEmpty()) {
            throw new IllegalArgumentException("no token for " + key);
        }
        return token;
    }

    static Map<String, String> options(String key) throws Exception {
        Map<String, String> options = new LinkedHashMap<>();
        options.put("metastore", "rest");
        options.put("uri", required("PERF_URI"));
        options.put("warehouse", System.getenv().getOrDefault("PERF_WAREHOUSE", "colvenna"));
        options.put("token.provider", "bear");
        options.put("token", token(key));
        options.put("data-token.enabled", "true");
        return options;
    }

    /** A REST catalog of the key's principal. */
    static Catalog catalog(String key) throws Exception {
        return CatalogFactory.createCatalog(CatalogContext.create(Options.fromMap(options(key))));
    }

    /**
     * The REST API of the key's principal, as RESTCatalog makes it: its constructor asks the
     * catalog's config ({@code GET /v1/config}).
     */
    static RESTApi api(String key) throws Exception {
        return new RESTApi(Options.fromMap(options(key)));
    }

    static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }
}
