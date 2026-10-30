package com.colvenna.catalog.uat.flink;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.options.Options;

/**
 * Colvenna Catalog as every flow reaches it: the REST catalog, a bearer token, and per-table data
 * tokens. The environment names it: {@code UAT_URI}, {@code UAT_TOKEN_FILE} (the token is read from
 * a file, never an argument), {@code UAT_WAREHOUSE} (default {@code colvenna}) and {@code
 * UAT_DATA_TOKENS} (default {@code true}).
 */
public final class Catalogs {

    /** The catalog's name in Flink SQL. */
    public static final String NAME = "colvenna";

    private Catalogs() {}

    /** The catalog's options, with or without per-table data tokens. */
    public static Map<String, String> options(boolean dataTokens) throws IOException {
        Map<String, String> options = new LinkedHashMap<>();
        options.put("metastore", "rest");
        options.put("uri", env("UAT_URI", null));
        options.put("warehouse", env("UAT_WAREHOUSE", "colvenna"));
        options.put("token.provider", "bear");
        options.put(
                "token",
                Files.readString(Path.of(env("UAT_TOKEN_FILE", null)), StandardCharsets.UTF_8)
                        .trim());
        options.put("data-token.enabled", String.valueOf(dataTokens));
        return options;
    }

    public static Map<String, String> options() throws IOException {
        return options(Boolean.parseBoolean(env("UAT_DATA_TOKENS", "true")));
    }

    /** {@code CREATE CATALOG colvenna WITH ('type'='paimon', ...)}, as a user writes it. */
    public static String ddl() throws IOException {
        StringBuilder ddl = new StringBuilder("CREATE CATALOG " + NAME + " WITH ('type'='paimon'");
        options()
                .forEach(
                        (k, v) ->
                                ddl.append(", '")
                                        .append(k)
                                        .append("'='")
                                        .append(v.replace("'", "''"))
                                        .append("'"));
        return ddl.append(")").toString();
    }

    /** The Paimon Java API's catalog on the same options. */
    public static Catalog paimon(boolean dataTokens) throws IOException {
        return CatalogFactory.createCatalog(
                CatalogContext.create(Options.fromMap(options(dataTokens))));
    }

    static String env(String name, String fallback) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            if (fallback == null) {
                throw new IllegalStateException(name + " is not set");
            }
            return fallback;
        }
        return value;
    }
}
