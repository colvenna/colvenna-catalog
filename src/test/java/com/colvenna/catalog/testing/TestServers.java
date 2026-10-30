package com.colvenna.catalog.testing;

import com.colvenna.catalog.ColvennaCatalogServer;
import com.colvenna.catalog.ServerConfig;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.sql.Statement;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Real colvenna-catalog servers on a shared PostgreSQL container, for tests. */
public final class TestServers {

    /** Warehouse name the test servers serve; clients pass it as {@code warehouse}. */
    public static final String WAREHOUSE = "colvenna";

    /** The environment test servers run as; test grants are made for it. */
    public static final String ENVIRONMENT = "test";

    private static final PostgreSQLContainer POSTGRES =
            new PostgreSQLContainer("postgres:16-alpine")
                    .withDatabaseName("colvenna")
                    .withUsername("colvenna")
                    .withPassword("colvenna");

    private TestServers() {}

    public static synchronized PostgreSQLContainer postgres() {
        if (!POSTGRES.isRunning()) {
            POSTGRES.start();
        }
        return POSTGRES;
    }

    /** Clients may place external tables and data paths under the JUnit temporary directory. */
    public static final List<String> TEMP_EXTERNAL_ROOTS = List.of("file:///tmp");

    public static ServerConfig config(String warehouseRoot, Map<String, String> overrides) {
        return config(warehouseRoot, overrides, TEMP_EXTERNAL_ROOTS);
    }

    public static ServerConfig config(
            String warehouseRoot, Map<String, String> overrides, List<String> externalRoots) {
        Map<String, String> settings = new HashMap<>();
        if (!overrides.isEmpty()) {
            List<String> pairs = new ArrayList<>();
            overrides.forEach((k, v) -> pairs.add(k + "=" + v));
            settings.put("COLVENNA_CLIENT_OVERRIDES", String.join(",", pairs));
        }
        return withSettings(warehouseRoot, externalRoots, settings);
    }

    /**
     * A test server's configuration, read as the server reads its environment, with {@code
     * settings} ({@code COLVENNA_*} variables, and {@code AWS_*} ones such as {@code
     * AWS_LAMBDA_RUNTIME_API}) added or replacing the defaults.
     */
    public static ServerConfig withSettings(
            String warehouseRoot, List<String> externalRoots, Map<String, String> settings) {
        Map<String, String> env = new HashMap<>(settings(warehouseRoot, externalRoots));
        env.putAll(settings);
        return ServerConfig.fromEnvironment(env);
    }

    /** The environment of a test server: any port, plain HTTP, the shared PostgreSQL. */
    public static Map<String, String> settings(String warehouseRoot, List<String> externalRoots) {
        PostgreSQLContainer pg = postgres();
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_PORT", "0");
        env.put("COLVENNA_HEALTH_PORT", "0");
        env.put("COLVENNA_JDBC_URL", pg.getJdbcUrl());
        env.put("COLVENNA_JDBC_USER", pg.getUsername());
        env.put("COLVENNA_JDBC_PASSWORD", pg.getPassword());
        env.put("COLVENNA_JDBC_POOL_SIZE", "8");
        env.put("COLVENNA_JDBC_AUX_POOL_SIZE", "2");
        env.put("COLVENNA_WAREHOUSE_NAME", WAREHOUSE);
        env.put("COLVENNA_WAREHOUSE_ROOT", warehouseRoot);
        env.put("COLVENNA_EXTERNAL_LOCATION_ROOTS", String.join(",", externalRoots));
        env.put("COLVENNA_DEFAULT_PAGE_SIZE", "100");
        env.put("COLVENNA_MAX_PAGE_SIZE", "1000");
        env.put("COLVENNA_MAX_IN_FLIGHT_REQUESTS", "256");
        env.put("COLVENNA_AUTH_CACHE_TTL", "PT0S");
        env.put("COLVENNA_ALLOW_PLAINTEXT", "true");
        env.put("COLVENNA_ENVIRONMENT", ENVIRONMENT);
        // Tests close servers all the time: no drain, and a second for what is in flight.
        env.put("COLVENNA_SHUTDOWN_DRAIN", "PT0S");
        env.put("COLVENNA_SHUTDOWN_TIMEOUT", "PT1S");
        return env;
    }

    /** The terms test grants are made under. */
    public static Authorizer.Terms testTerms() {
        return new Authorizer.Terms(
                "tests",
                ENVIRONMENT,
                "test-approver",
                "TEST-1",
                Instant.now().plus(Duration.ofDays(30)));
    }

    /** Gives {@code principal} catalog-wide MANAGE, as tests acting as owners need. */
    public static void grantCatalog(ColvennaCatalogServer server, String principal)
            throws Exception {
        server.authorizer()
                .grant(
                        principal,
                        Authorizer.Scope.CATALOG,
                        null,
                        null,
                        Authorizer.Privilege.MANAGE,
                        Authorizer.Effect.ALLOW,
                        null,
                        testTerms(),
                        "test-setup");
    }

    /** Starts a server; the caller closes it. */
    public static ColvennaCatalogServer start(
            String warehouseRoot, DataTokenIssuer dataTokens, Clock clock) throws Exception {
        ColvennaCatalogServer server =
                ColvennaCatalogServer.create(config(warehouseRoot, Map.of()), clock, dataTokens);
        server.start();
        return server;
    }

    /** Removes every catalog row, so the next server starts from an empty catalog. */
    public static void reset() throws Exception {
        truncate(
                "audit_events, commit_intents, table_partitions, table_branches, catalog_tables,"
                        + " catalog_views, catalog_functions, catalog_databases, table_purges, consistency_checks, file_purges");
    }

    /** Also removes principals and tokens. */
    public static void resetAll() throws Exception {
        truncate(
                "audit_events, api_tokens, principals, commit_intents, table_partitions,"
                        + " table_branches, catalog_tables, catalog_views, catalog_functions,"
                        + " catalog_databases, table_purges, consistency_checks, file_purges");
    }

    private static void truncate(String tables) throws Exception {
        PostgreSQLContainer pg = postgres();
        try (Db db = new Db(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), 1, 1)) {
            com.colvenna.catalog.db.Migrations.apply(db);
            db.inTransaction(
                    c -> {
                        try (Statement statement = c.createStatement()) {
                            statement.execute("TRUNCATE " + tables + " CASCADE");
                        }
                        return null;
                    });
        }
    }
}
