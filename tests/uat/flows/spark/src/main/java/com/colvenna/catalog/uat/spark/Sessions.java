package com.colvenna.catalog.uat.spark;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.options.Options;
import org.apache.spark.sql.SparkSession;

/**
 * Colvenna Catalog as the Spark flows reach it: Paimon's Spark catalog {@code colvenna} on the REST
 * catalog with a bearer token and per-table data tokens. Environment: {@code UAT_URI}, {@code
 * UAT_TOKEN_FILE}, {@code UAT_WAREHOUSE} (default {@code colvenna}). Also the flows' output: one
 * {@code STEP <check> OK|FAIL} line per check.
 */
final class Sessions {

    static final String NAME = "colvenna";

    /** A step's body. */
    @FunctionalInterface
    interface Body {
        String run() throws Exception;
    }

    private Sessions() {}

    static String token() throws IOException {
        return Files.readString(Path.of(env("UAT_TOKEN_FILE")), StandardCharsets.UTF_8).trim();
    }

    static SparkSession spark(String app) throws IOException {
        String prefix = "spark.sql.catalog." + NAME;
        return SparkSession.builder()
                .appName(app)
                .config(
                        "spark.sql.extensions",
                        "org.apache.paimon.spark.extensions.PaimonSparkSessionExtensions")
                .config(prefix, "org.apache.paimon.spark.SparkCatalog")
                .config(prefix + ".metastore", "rest")
                .config(prefix + ".uri", env("UAT_URI"))
                .config(prefix + ".warehouse", warehouse())
                .config(prefix + ".token.provider", "bear")
                .config(prefix + ".token", token())
                .config(prefix + ".data-token.enabled", "true")
                .getOrCreate();
    }

    /** The Paimon Java API's catalog on the same options. */
    static Catalog paimon() throws IOException {
        Options options = new Options();
        options.set("metastore", "rest");
        options.set("uri", env("UAT_URI"));
        options.set("warehouse", warehouse());
        options.set("token.provider", "bear");
        options.set("token", token());
        options.set("data-token.enabled", "true");
        return CatalogFactory.createCatalog(CatalogContext.create(options));
    }

    static void step(String name, Body body) throws Exception {
        long start = System.nanoTime();
        String detail;
        try {
            detail = body.run();
        } catch (Throwable e) {
            System.out.println(
                    "STEP " + name + " FAIL " + String.valueOf(e).replaceAll("\\s+", " "));
            e.printStackTrace(System.out);
            throw e instanceof Exception ? (Exception) e : new RuntimeException(e);
        }
        System.out.println(
                String.format(
                        Locale.ROOT,
                        "STEP %s OK %dms %s",
                        name,
                        (System.nanoTime() - start) / 1_000_000,
                        String.valueOf(detail).replaceAll("\\s+", " ")));
    }

    static void expect(long expected, long actual, String what) {
        if (expected != actual) {
            throw new AssertionError(what + ": expected " + expected + " but was " + actual);
        }
    }

    private static String warehouse() {
        String value = System.getenv("UAT_WAREHOUSE");
        return value == null || value.isEmpty() ? "colvenna" : value;
    }

    private static String env(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalStateException(name + " is not set");
        }
        return value;
    }
}
