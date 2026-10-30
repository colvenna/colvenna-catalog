package com.colvenna.catalog.testing;

import com.colvenna.catalog.ColvennaCatalogServer;
import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.RESTCatalogOptions;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.io.TempDir;

/** One colvenna-catalog server per test, on a local warehouse and a clean catalog. */
public abstract class ServerTestBase {

    /** A clock tests can move forward. */
    public static final class MutableClock extends Clock {
        private final AtomicReference<Instant> now = new AtomicReference<>(Instant.now());

        public void advance(Duration duration) {
            now.updateAndGet(i -> i.plus(duration));
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now.get();
        }
    }

    @TempDir protected Path warehouseDir;
    protected final MutableClock clock = new MutableClock();
    protected ColvennaCatalogServer server;
    protected String adminToken;
    protected String warehouse;

    protected CommitService.Faults faults() {
        return CommitService.Faults.NONE;
    }

    protected List<String> externalLocationRoots() {
        return TestServers.TEMP_EXTERNAL_ROOTS;
    }

    protected DataTokenIssuer dataTokens() {
        return DataTokenIssuer.disabled();
    }

    /** Server settings ({@code COLVENNA_*}) a test class changes from the test defaults. */
    protected Map<String, String> settings() {
        return Map.of();
    }

    @BeforeEach
    void startServer() throws Exception {
        TestServers.resetAll();
        warehouse = warehouseDir.toUri().toString();
        server = startReplica();
        adminToken = server.tokens().bootstrapAdmin("admin", Duration.ofDays(1)).token();
        // Admins hold no implicit catalog privileges; tests acting as table owners get a grant.
        TestServers.grantCatalog(server, "admin");
    }

    @AfterEach
    void stopServer() {
        if (server != null) {
            server.close();
        }
    }

    /** Creates {@code principal} if needed and issues it a token; returns the issue response. */
    /** Another server on the same database and warehouse, as a second replica would be. */
    protected ColvennaCatalogServer startReplica() throws Exception {
        ColvennaCatalogServer replica =
                ColvennaCatalogServer.create(
                        TestServers.withSettings(warehouse, externalLocationRoots(), settings()),
                        clock,
                        dataTokens(),
                        faults());
        replica.start();
        return replica;
    }

    protected JsonNode issueToken(String principal, String ttl) throws Exception {
        HttpResponse<String> created =
                http(
                        "POST",
                        "/colvenna/admin/v1/principals",
                        adminToken,
                        "{\"name\":\"" + principal + "\"}");
        if (created.statusCode() != 200 && created.statusCode() != 409) {
            throw new AssertionError("create principal: " + created.statusCode());
        }
        HttpResponse<String> issued =
                http(
                        "POST",
                        "/colvenna/admin/v1/principals/" + principal + "/tokens",
                        adminToken,
                        "{\"ttl\":\"" + ttl + "\"}");
        if (issued.statusCode() != 200) {
            throw new AssertionError("issue token: " + issued.statusCode());
        }
        return RESTApi.OBJECT_MAPPER.readTree(issued.body());
    }

    /** The first column of every row {@code sql} returns from the catalog database. */
    protected List<String> queryColumn(String sql) throws Exception {
        List<String> rows = new ArrayList<>();
        try (Connection c = server.db().dataSource().getConnection();
                PreparedStatement select = c.prepareStatement(sql);
                ResultSet rs = select.executeQuery()) {
            while (rs.next()) {
                rows.add(rs.getString(1));
            }
        }
        return rows;
    }

    /** The audit events the admin API's query {@code filter} ({@code resource=db.t}, say) finds. */
    protected JsonNode auditEvents(String filter) throws Exception {
        return RESTApi.OBJECT_MAPPER
                .readTree(
                        http(
                                        "GET",
                                        "/colvenna/admin/v1/audit?limit=1000&" + filter,
                                        adminToken,
                                        null)
                                .body())
                .get("events");
    }

    /**
     * The audit event, among those {@code filter} finds, of the request that got {@code answer}.
     */
    protected JsonNode auditEventOf(HttpResponse<String> answer, String filter) throws Exception {
        String requestId = answer.headers().firstValue("X-Request-Id").orElseThrow();
        JsonNode events = auditEvents(filter);
        for (JsonNode e : events) {
            if (e.get("requestId").asText().equals(requestId)) {
                return e;
            }
        }
        throw new AssertionError(
                "No audit event of request " + requestId + " with " + filter + " in " + events);
    }

    protected RESTCatalog catalog(String token) {
        return catalog(token, server.port(), Map.of());
    }

    protected RESTCatalog catalog(String token, int port, Map<String, String> extra) {
        Options options = new Options();
        options.set(RESTCatalogOptions.URI, "http://localhost:" + port);
        options.set(RESTCatalogOptions.TOKEN_PROVIDER, "bear");
        options.set(RESTCatalogOptions.TOKEN, token);
        options.set(CatalogOptions.WAREHOUSE, TestServers.WAREHOUSE);
        extra.forEach(options::setString);
        return new RESTCatalog(CatalogContext.create(options));
    }

    protected HttpResponse<String> http(
            String method, String path, @Nullable String token, @Nullable String body)
            throws Exception {
        return http(server.port(), method, path, token, body);
    }

    protected static HttpResponse<String> http(
            int port, String method, String path, @Nullable String token, @Nullable String body)
            throws Exception {
        HttpRequest.Builder request =
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                        .method(
                                method,
                                body == null
                                        ? HttpRequest.BodyPublishers.noBody()
                                        : HttpRequest.BodyPublishers.ofString(body));
        if (token != null) {
            request.header("Authorization", "Bearer " + token);
        }
        if (body != null) {
            request.header("Content-Type", "application/json");
        }
        try (HttpClient client = HttpClient.newHttpClient()) {
            return client.send(request.build(), HttpResponse.BodyHandlers.ofString());
        }
    }
}
