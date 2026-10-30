package com.colvenna.catalog.uat.paimon;

import java.io.BufferedReader;
import java.io.InputStreamReader;
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
 * The catalog as every tool reaches it: Paimon's REST catalog with a bearer token and per-table
 * credentials ({@code data-token.enabled}). {@code UAT_URI} is the catalog's URI, {@code
 * UAT_WAREHOUSE} the warehouse (default {@code colvenna}); the token is read from the file {@code
 * UAT_TOKEN_FILE}, or from the first line of stdin when that is {@code -}, so that it is never an
 * argument or an environment variable.
 */
public final class Catalogs {

    private static String token;
    private static BufferedReader stdin;

    private Catalogs() {}

    /** The next line of stdin (after the token, when the token comes on stdin), or null. */
    static synchronized String nextLine() throws Exception {
        if (stdin == null) {
            stdin = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        }
        return stdin.readLine();
    }

    static synchronized String token() throws Exception {
        if (token == null) {
            String file = System.getenv().getOrDefault("UAT_TOKEN_FILE", "-");
            if (file.equals("-")) {
                String line = nextLine();
                token = line == null ? "" : line.trim();
            } else {
                token = Files.readString(Path.of(file), StandardCharsets.UTF_8).trim();
            }
            if (token.isEmpty()) {
                throw new IllegalArgumentException("no token on stdin or in UAT_TOKEN_FILE");
            }
        }
        return token;
    }

    /** The REST catalog's options. */
    public static Map<String, String> options() throws Exception {
        Map<String, String> options = new LinkedHashMap<>();
        options.put("metastore", "rest");
        options.put("uri", required("UAT_URI"));
        options.put("warehouse", System.getenv().getOrDefault("UAT_WAREHOUSE", "colvenna"));
        options.put("token.provider", "bear");
        options.put("token", token());
        options.put("data-token.enabled", "true");
        return options;
    }

    public static Catalog open() throws Exception {
        return CatalogFactory.createCatalog(CatalogContext.create(Options.fromMap(options())));
    }

    /** The catalog's Flink DDL, for the tools that use Flink's Table API. */
    public static String flinkDdl(String name) throws Exception {
        StringBuilder ddl = new StringBuilder("CREATE CATALOG " + name + " WITH ('type'='paimon'");
        for (Map.Entry<String, String> e : options().entrySet()) {
            ddl.append(", '").append(e.getKey()).append("'='").append(e.getValue()).append("'");
        }
        return ddl.append(")").toString();
    }

    static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }
}
