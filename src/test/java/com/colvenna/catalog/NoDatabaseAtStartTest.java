package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.Migrations;
import com.colvenna.catalog.testing.ServerTestBase.MutableClock;
import com.colvenna.catalog.testing.TestServers;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.net.ServerSocket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.Duration;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.RESTCatalogOptions;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.postgresql.PostgreSQLContainer;

/**
 * The serverless form's start ({@code COLVENNA_MIGRATE_ON_START=false}): no database work until the
 * first request, and no request served before {@code migrate} has brought the schema to this
 * server's version.
 */
class NoDatabaseAtStartTest {

    @TempDir Path warehouseDir;

    @Nullable private String database;
    @Nullable private String databaseUrl;
    @Nullable private ColvennaCatalogServer server;

    @AfterEach
    void cleanUp() throws Exception {
        if (server != null) {
            server.close();
        }
        if (database != null) {
            try (Connection c = admin();
                    Statement s = c.createStatement()) {
                s.execute("DROP DATABASE " + database + " WITH (FORCE)");
            }
        }
    }

    @Test
    void theServerStartsWithoutADatabase() throws Exception {
        int closed;
        try (ServerSocket socket = new ServerSocket(0)) {
            closed = socket.getLocalPort();
        }
        ServerConfig config =
                config(
                        Map.of(
                                "COLVENNA_JDBC_URL",
                                "jdbc:postgresql://127.0.0.1:" + closed + "/none",
                                "COLVENNA_MIGRATE_ON_START",
                                "false"));
        long start = System.nanoTime();
        server = start(config);
        // Nothing waited for a connection: Hikari alone would have waited 10 seconds.
        assertThat(Duration.ofNanos(System.nanoTime() - start)).isLessThan(Duration.ofSeconds(5));
        // The Lambda Web Adapter's readiness check asks /healthz, which needs no database.
        assertThat(get("http://127.0.0.1:" + server.healthPort() + "/healthz", null).statusCode())
                .isEqualTo(200);
    }

    @Test
    void theFirstRequestConnectsAndWaitsForMigrate() throws Exception {
        String url = createDatabase();
        server = start(Map.of("COLVENNA_JDBC_URL", url, "COLVENNA_MIGRATE_ON_START", "false"));
        // Long enough for pools started at construction to have filled.
        Thread.sleep(500);
        assertThat(connections()).as("connections after the start").isZero();

        HttpResponse<String> early = config("colvenna_unknown");
        assertThat(early.statusCode()).isEqualTo(503);
        assertThat(early.body()).contains("must run migrate");
        assertThat(connections()).as("connections after the first request").isPositive();
        assertThat(tables()).as("tables the server created").isZero();

        // What the installer does: the migrate command, then the first admin.
        migrate(url);
        String token = server.tokens().bootstrapAdmin("admin", Duration.ofDays(1)).token();
        TestServers.grantCatalog(server, "admin");
        assertThat(config(token).statusCode()).isEqualTo(200);
        try (RESTCatalog catalog = catalog(token)) {
            catalog.createDatabase("db", false);
            assertThat(catalog.listDatabases()).contains("db");
        }
    }

    @Test
    void aNewerSchemaIsNotServed() throws Exception {
        String url = createDatabase();
        migrate(url);
        try (Connection c = DriverManager.getConnection(url, user(), password());
                Statement s = c.createStatement()) {
            s.execute(
                    "INSERT INTO colvenna_schema_version (version, script) VALUES (99, 'V99.sql')");
        }
        server = start(Map.of("COLVENNA_JDBC_URL", url, "COLVENNA_MIGRATE_ON_START", "false"));
        HttpResponse<String> refused = config("colvenna_unknown");
        assertThat(refused.statusCode()).isEqualTo(503);
        assertThat(refused.body()).contains("newer than this server");
    }

    @Test
    void lambdaDefaultsToNotMigratingAtStart() {
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_JDBC_URL", "jdbc:postgresql://localhost/x");
        env.put("COLVENNA_WAREHOUSE_NAME", "colvenna");
        env.put("COLVENNA_WAREHOUSE_ROOT", "file:///tmp/w");
        ServerConfig eks = ServerConfig.fromEnvironment(env);
        assertThat(eks.migrateOnStart()).isTrue();
        assertThat(eks.lambdaRuntime()).isFalse();

        env.put("AWS_LAMBDA_RUNTIME_API", "127.0.0.1:9001");
        ServerConfig lambda = ServerConfig.fromEnvironment(env);
        assertThat(lambda.migrateOnStart()).isFalse();
        assertThat(lambda.lambdaRuntime()).isTrue();

        env.put("COLVENNA_MIGRATE_ON_START", "TRUE");
        assertThat(ServerConfig.fromEnvironment(env).migrateOnStart()).isTrue();
        env.put("COLVENNA_MIGRATE_ON_START", "no");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("COLVENNA_MIGRATE_ON_START must be true or false");
    }

    private ColvennaCatalogServer start(Map<String, String> settings) throws Exception {
        return start(config(settings));
    }

    private ServerConfig config(Map<String, String> settings) {
        return TestServers.withSettings(
                warehouseDir.toUri().toString(), TestServers.TEMP_EXTERNAL_ROOTS, settings);
    }

    private static ColvennaCatalogServer start(ServerConfig config) throws Exception {
        ColvennaCatalogServer started =
                ColvennaCatalogServer.create(
                        config, new MutableClock(), DataTokenIssuer.disabled());
        started.start();
        return started;
    }

    /** A new, empty database in the shared PostgreSQL; returns its JDBC URL. */
    private String createDatabase() throws Exception {
        database =
                "fresh_" + UUID.randomUUID().toString().replace("-", "").toLowerCase(Locale.ROOT);
        try (Connection c = admin();
                Statement s = c.createStatement()) {
            s.execute("CREATE DATABASE " + database);
        }
        PostgreSQLContainer pg = TestServers.postgres();
        databaseUrl =
                "jdbc:postgresql://"
                        + pg.getHost()
                        + ":"
                        + pg.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT)
                        + "/"
                        + database;
        return databaseUrl;
    }

    private static void migrate(String url) throws Exception {
        try (Db db = new Db(url, user(), password(), 1, 1)) {
            Migrations.apply(db);
        }
    }

    private int connections() throws Exception {
        return count("SELECT count(*) FROM pg_stat_activity WHERE datname = ?");
    }

    private int tables() throws Exception {
        try (Connection c = DriverManager.getConnection(databaseUrl, user(), password());
                Statement s = c.createStatement();
                ResultSet rs =
                        s.executeQuery(
                                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private int count(String sql) throws Exception {
        try (Connection c = admin();
                PreparedStatement select = c.prepareStatement(sql)) {
            select.setString(1, database);
            try (ResultSet rs = select.executeQuery()) {
                rs.next();
                return rs.getInt(1);
            }
        }
    }

    private static Connection admin() throws Exception {
        return DriverManager.getConnection(TestServers.postgres().getJdbcUrl(), user(), password());
    }

    private static String user() {
        return TestServers.postgres().getUsername();
    }

    private static String password() {
        return TestServers.postgres().getPassword();
    }

    private HttpResponse<String> config(String token) throws Exception {
        return get(
                "http://localhost:" + server.port() + "/v1/config?warehouse=colvenna",
                "Bearer " + token);
    }

    private RESTCatalog catalog(String token) {
        Options options = new Options();
        options.set(RESTCatalogOptions.URI, "http://localhost:" + server.port());
        options.set(RESTCatalogOptions.TOKEN_PROVIDER, "bear");
        options.set(RESTCatalogOptions.TOKEN, token);
        options.set(CatalogOptions.WAREHOUSE, TestServers.WAREHOUSE);
        return new RESTCatalog(CatalogContext.create(options));
    }

    private static HttpResponse<String> get(String uri, @Nullable String authorization)
            throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(uri)).GET();
        if (authorization != null) {
            request.header("Authorization", authorization);
        }
        try (HttpClient client = HttpClient.newHttpClient()) {
            return client.send(request.build(), HttpResponse.BodyHandlers.ofString());
        }
    }
}
