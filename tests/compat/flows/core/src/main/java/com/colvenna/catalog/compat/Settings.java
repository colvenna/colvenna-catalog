package com.colvenna.catalog.compat;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * How the flow reaches the catalog, from its environment (the runner sets it):
 *
 * <ul>
 *   <li>{@code COMPAT_URI}: the catalog's REST endpoint;
 *   <li>{@code COMPAT_TOKEN_FILE}: a file holding the bearer token (never an argument or a log);
 *   <li>{@code COMPAT_WAREHOUSE}: the warehouse name, {@code colvenna} by default;
 *   <li>{@code COMPAT_DATA_TOKENS}: {@code true} (default) to read and write table files with the
 *       credentials the catalog vends, {@code false} for a catalog that vends none;
 *   <li>{@code COMPAT_S3_REGION}: the warehouse bucket's region, for the probe of the engine's own
 *       identity (optional).
 * </ul>
 */
public final class Settings {

    /** The catalog's name in the engines' SQL. */
    public static final String CATALOG = "compat";

    private final String uri;
    private final String token;
    private final String warehouse;
    private final boolean dataTokens;
    private final String s3Region;

    private Settings(
            String uri, String token, String warehouse, boolean dataTokens, String s3Region) {
        this.uri = uri;
        this.token = token;
        this.warehouse = warehouse;
        this.dataTokens = dataTokens;
        this.s3Region = s3Region;
    }

    public static Settings fromEnvironment() throws IOException {
        String uri = required("COMPAT_URI");
        String token =
                Files.readString(Path.of(required("COMPAT_TOKEN_FILE")), StandardCharsets.UTF_8)
                        .trim();
        if (token.isEmpty()) {
            throw new IllegalArgumentException("COMPAT_TOKEN_FILE holds no token");
        }
        String warehouse = System.getenv().getOrDefault("COMPAT_WAREHOUSE", "colvenna");
        String dataTokens =
                System.getenv().getOrDefault("COMPAT_DATA_TOKENS", "true").toLowerCase(Locale.ROOT);
        if (!dataTokens.equals("true") && !dataTokens.equals("false")) {
            throw new IllegalArgumentException(
                    "COMPAT_DATA_TOKENS: true or false, not " + dataTokens);
        }
        return new Settings(
                uri,
                token,
                warehouse,
                dataTokens.equals("true"),
                System.getenv().getOrDefault("COMPAT_S3_REGION", ""));
    }

    private static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }

    public boolean dataTokens() {
        return dataTokens;
    }

    public String s3Region() {
        return s3Region;
    }

    /**
     * The Paimon REST catalog's options, as a user would configure the engine: {@code
     * metastore=rest} with the bearer token provider, and the vended per-table credentials unless
     * {@code dataTokens} is false. No S3 keys: with data tokens on, the catalog's are the only
     * ones.
     */
    public Map<String, String> catalogOptions(boolean withDataTokens) {
        Map<String, String> options = new LinkedHashMap<>();
        options.put("metastore", "rest");
        options.put("uri", uri);
        options.put("warehouse", warehouse);
        options.put("token.provider", "bear");
        options.put("token", token);
        options.put("data-token.enabled", String.valueOf(withDataTokens));
        return options;
    }

    public Map<String, String> catalogOptions() {
        return catalogOptions(dataTokens);
    }
}
