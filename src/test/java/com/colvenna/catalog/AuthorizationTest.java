package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.auth.Authorizer.Effect;
import com.colvenna.catalog.auth.Authorizer.Privilege;
import com.colvenna.catalog.auth.Authorizer.Scope;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Allow and deny decisions for different service identities across catalog queries, data access,
 * writes and management, and the audit trail they leave.
 */
class AuthorizationTest extends ServerTestBase {

    private static final Identifier T = Identifier.create("db", "t");
    private static final Identifier U = Identifier.create("db", "u");

    /** A denied commit is retried by the client like a conflict; keep the retries short. */
    private static final Map<String, String> FAST_RETRY =
            Map.of(
                    "commit.min-retry-wait", "10 ms",
                    "commit.max-retry-wait", "50 ms",
                    "commit.max-retries", "2");

    @BeforeEach
    void tables() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            Tables.createAppendTable(admin, T, FAST_RETRY);
            Tables.createAppendTable(admin, U, FAST_RETRY);
            admin.createDatabase("other", false);
        }
    }

    @Test
    void readersSeeAndReadButCannotWrite() throws Exception {
        String reader = identity("bi-reader", Scope.DATABASE, "db", null, Privilege.READ);
        try (RESTCatalog catalog = catalog(reader)) {
            assertThat(catalog.listDatabases()).containsExactly("db");
            assertThat(catalog.listTables("db")).containsExactlyInAnyOrder("t", "u");
            assertThat(Tables.ids(catalog.getTable(T))).isEmpty();
            assertThatThrownBy(() -> Tables.write(catalog.getTable(T), 1))
                    .hasStackTraceContaining("has no permission");
            assertThatThrownBy(() -> catalog.dropTable(T, false))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            assertThatThrownBy(() -> catalog.getDatabase("other"))
                    .isInstanceOf(Catalog.DatabaseNoPermissionException.class);
        }
    }

    @Test
    void writersCommitToTheirTableOnly() throws Exception {
        String writer = identity("etl-writer", Scope.TABLE, "db", "t", Privilege.WRITE);
        try (RESTCatalog catalog = catalog(writer)) {
            assertThat(catalog.listDatabases()).containsExactly("db");
            assertThat(catalog.listTables("db")).containsExactly("t");
            Tables.write(catalog.getTable(T), 1, 2);
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);
            assertThatThrownBy(() -> catalog.getTable(U))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            assertThatThrownBy(
                            () ->
                                    catalog.alterTable(
                                            T,
                                            List.of(
                                                    org.apache.paimon.schema.SchemaChange.addColumn(
                                                            "x", DataTypes.INT())),
                                            false))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            assertThatThrownBy(
                            () ->
                                    catalog.createTable(
                                            Identifier.create("db", "v"), schema(), false))
                    .hasStackTraceContaining("Database db has no permission");
        }
    }

    @Test
    void ownersManageTheirDatabaseOnly() throws Exception {
        String owner = identity("db-owner", Scope.DATABASE, "db", null, Privilege.MANAGE);
        try (RESTCatalog catalog = catalog(owner)) {
            catalog.createTable(Identifier.create("db", "v"), schema(), false);
            catalog.renameTable(Identifier.create("db", "v"), Identifier.create("db", "w"), false);
            catalog.dropTable(Identifier.create("db", "w"), false);
            assertThatThrownBy(() -> catalog.renameTable(T, Identifier.create("other", "t"), false))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            assertThatThrownBy(() -> catalog.createDatabase("third", false))
                    .isInstanceOf(Catalog.DatabaseNoPermissionException.class);
        }
    }

    @Test
    void aDenyWinsOverABroaderAllow() throws Exception {
        String manager = identity("ops", Scope.CATALOG, null, null, Privilege.MANAGE);
        grant("ops", Scope.TABLE, "db", "t", Privilege.WRITE, Effect.DENY, null);
        try (RESTCatalog catalog = catalog(manager)) {
            assertThat(Tables.ids(catalog.getTable(T))).isEmpty();
            assertThatThrownBy(() -> Tables.write(catalog.getTable(T), 1))
                    .hasStackTraceContaining("has no permission");
            assertThatThrownBy(() -> catalog.dropTable(T, false))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            Tables.write(catalog.getTable(U), 1);
            catalog.createDatabase("third", false);
        }
    }

    @Test
    void identitiesWithoutGrantsSeeNothing() throws Exception {
        String nobody = issueToken("nobody", "P1D").get("token").asText();
        try (RESTCatalog catalog = catalog(nobody)) {
            assertThat(catalog.listDatabases()).isEmpty();
            assertThatThrownBy(() -> catalog.getTable(T))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
            assertThatThrownBy(() -> catalog.listTables("db"))
                    .isInstanceOf(Catalog.DatabaseNoPermissionException.class);
        }
        assertThat(
                        http("GET", "/v1/colvenna/databases/db/tables/t/token", nobody, null)
                                .statusCode())
                .isEqualTo(403);
        assertThat(http("GET", "/colvenna/admin/v1/grants", nobody, null).statusCode())
                .isEqualTo(403);
    }

    @Test
    void columnGrantsGateQueryAuthorization() throws Exception {
        String analyst = issueToken("analyst", "P1D").get("token").asText();
        grant("analyst", Scope.TABLE, "db", "t", Privilege.READ, Effect.ALLOW, List.of("id"));
        assertThat(auth(analyst, List.of("id"))).isEqualTo(200);
        assertThat(auth(analyst, List.of("id", "payload"))).isEqualTo(403);
        assertThat(auth(analyst, null)).isEqualTo(403);
        assertThat(auth(adminToken, null)).isEqualTo(200);
    }

    @Test
    void grantsAreManagedThroughTheAdminApiAndTakeEffect() throws Exception {
        String reader = issueToken("bi-reader", "P1D").get("token").asText();
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", reader, null).statusCode())
                .isEqualTo(403);
        HttpResponse<String> granted =
                http(
                        "POST",
                        "/colvenna/admin/v1/grants",
                        adminToken,
                        "{\"principal\":\"bi-reader\",\"scope\":\"table\",\"database\":\"db\","
                                + "\"object\":\"t\",\"privilege\":\"read\",\"purpose\":\"BI dashboards\","
                                + "\"environment\":\"test\",\"approvedBy\":\"data-owner\","
                                + "\"approval\":\"CHG-42\",\"expiresAt\":\""
                                + java.time.Instant.now().plus(java.time.Duration.ofDays(7))
                                + "\"}");
        assertThat(granted.statusCode()).as(granted.body()).isEqualTo(200);
        long id = RESTApi.OBJECT_MAPPER.readTree(granted.body()).get("id").asLong();
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", reader, null).statusCode())
                .isEqualTo(200);
        JsonNode listed =
                RESTApi.OBJECT_MAPPER.readTree(
                        http(
                                        "GET",
                                        "/colvenna/admin/v1/grants?principal=bi-reader",
                                        adminToken,
                                        null)
                                .body());
        assertThat(listed.get("grants")).hasSize(1);
        assertThat(http("DELETE", "/colvenna/admin/v1/grants/" + id, adminToken, null).statusCode())
                .isEqualTo(200);
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", reader, null).statusCode())
                .isEqualTo(403);
        assertThat(
                        http(
                                        "POST",
                                        "/colvenna/admin/v1/grants",
                                        adminToken,
                                        "{\"principal\":\"bi-reader\",\"scope\":\"catalog\","
                                                + "\"database\":\"db\",\"privilege\":\"read\"}")
                                .statusCode())
                .isEqualTo(400);
    }

    @Test
    void theAuditTrailNamesWhoDidWhatToWhichResourceAndHowItEnded() throws Exception {
        String writer = identity("etl-writer", Scope.TABLE, "db", "t", Privilege.WRITE);
        try (RESTCatalog catalog = catalog(writer)) {
            Tables.write(catalog.getTable(T), 1);
            assertThatThrownBy(() -> catalog.dropTable(T, false))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
        }
        JsonNode events =
                RESTApi.OBJECT_MAPPER
                        .readTree(
                                http(
                                                "GET",
                                                "/colvenna/admin/v1/audit?principal=etl-writer&resource=db.t",
                                                adminToken,
                                                null)
                                        .body())
                        .get("events");
        assertThat(events.toString())
                .contains("\"action\":\"COMMIT\"")
                .contains("\"action\":\"DROP_TABLE\"");
        for (JsonNode e : events) {
            assertThat(e.get("principal").asText()).isEqualTo("etl-writer");
            assertThat(e.get("resource").asText()).isEqualTo("db.t");
            assertThat(e.hasNonNull("time")).isTrue();
            assertThat(e.hasNonNull("requestId")).isTrue();
            String expected =
                    e.get("action").asText().equals("DROP_TABLE") ? "DENIED" : "SUCCEEDED";
            if (e.get("action").asText().equals("DROP_TABLE")
                    || e.get("action").asText().equals("COMMIT")) {
                assertThat(e.get("outcome").asText()).as(e.toString()).isEqualTo(expected);
            }
        }
        JsonNode denied =
                RESTApi.OBJECT_MAPPER
                        .readTree(
                                http(
                                                "GET",
                                                "/colvenna/admin/v1/audit?outcome=denied&limit=10",
                                                adminToken,
                                                null)
                                        .body())
                        .get("events");
        assertThat(denied).isNotEmpty();
        denied.forEach(e -> assertThat(e.get("httpStatus").asInt()).isIn(401, 403));
    }

    @Test
    void aCreatedObjectIsAuditedUnderItsOwnName() throws Exception {
        String owner = identity("db-owner", Scope.DATABASE, "db", null, Privilege.MANAGE);
        String writer = identity("etl-writer", Scope.TABLE, "db", "t", Privilege.WRITE);
        Identifier created = Identifier.create("db", "created");
        Identifier refused = Identifier.create("db", "refused");
        try (RESTCatalog catalog = catalog(owner)) {
            Tables.createAppendTable(catalog, created, Map.of());
            // Again: allowed, and failed; still recorded under the table's own name.
            assertThatThrownBy(() -> Tables.createAppendTable(catalog, created, Map.of()))
                    .isInstanceOf(Catalog.TableAlreadyExistException.class);
        }
        Schema schema = Schema.newBuilder().column("id", DataTypes.INT()).build();
        try (RESTCatalog catalog = catalog(writer)) {
            assertThatThrownBy(() -> catalog.createTable(refused, schema, false))
                    .hasStackTraceContaining("no permission");
        }
        JsonNode events =
                RESTApi.OBJECT_MAPPER
                        .readTree(
                                http(
                                                "GET",
                                                "/colvenna/admin/v1/audit?action=CREATE_TABLE",
                                                adminToken,
                                                null)
                                        .body())
                        .get("events");
        assertThat(events.toString())
                .contains("\"principal\":\"db-owner\"")
                .contains("\"principal\":\"etl-writer\"")
                .contains("\"outcome\":\"FAILED\"");
        for (JsonNode e : events) {
            if (e.get("principal").asText().equals("db-owner")) {
                assertThat(e.get("resource").asText()).isEqualTo("db.created");
                assertThat(e.get("outcome").asText()).isIn("SUCCEEDED", "FAILED");
            } else if (e.get("principal").asText().equals("etl-writer")) {
                // Refused on the database, before the request names its table.
                assertThat(e.get("resource").asText()).isEqualTo("db");
                assertThat(e.get("outcome").asText()).isEqualTo("DENIED");
            }
        }
    }

    @Test
    void anAuthorizedRequestWithoutABodyIsABadRequestNotAnError() throws Exception {
        String owner = identity("db-owner", Scope.DATABASE, "db", null, Privilege.MANAGE);
        String writer = identity("etl-writer", Scope.TABLE, "db", "t", Privilege.WRITE);
        // Authorization comes first: MANAGE gets to the body check, WRITE does not.
        assertThat(http("POST", "/v1/colvenna/databases/db/tables/t", owner, "{}").statusCode())
                .isEqualTo(400);
        assertThat(http("POST", "/v1/colvenna/databases/db/tables/t", writer, "{}").statusCode())
                .isEqualTo(403);
        assertThat(
                        http("POST", "/v1/colvenna/databases/db/tables/t/commit", writer, "{}")
                                .statusCode())
                .isEqualTo(400);
    }

    @Test
    void everyPrincipalSeesTheDefaultDatabaseButNotItsTables() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            admin.createDatabase("default", true);
            Tables.createAppendTable(admin, Identifier.create("default", "private"), Map.of());
        }
        // A principal granted elsewhere only, as a Flink or Spark job starting its catalog.
        String writer = identity("etl-writer", Scope.TABLE, "db", "t", Privilege.WRITE);
        try (RESTCatalog catalog = catalog(writer)) {
            assertThat(catalog.getDatabase("default").name()).isEqualTo("default");
            assertThat(catalog.listTables("default")).isEmpty();
            assertThatThrownBy(() -> catalog.getTable(Identifier.create("default", "private")))
                    .isInstanceOf(Catalog.TableNoPermissionException.class);
        }
        // A READ deny still hides it.
        String denied = identity("denied", Scope.TABLE, "db", "t", Privilege.READ);
        grant("denied", Scope.DATABASE, "default", null, Privilege.READ, Effect.DENY, null);
        try (RESTCatalog catalog = catalog(denied)) {
            assertThatThrownBy(() -> catalog.getDatabase("default"))
                    .isInstanceOf(Catalog.DatabaseNoPermissionException.class);
        }
    }

    @Test
    void expiredGrantsStopAccessAndCredentialRenewal() throws Exception {
        String token = issueToken("temp-reader", "P1D").get("token").asText();
        server.authorizer()
                .grant(
                        "temp-reader",
                        Scope.TABLE,
                        "db",
                        "t",
                        Privilege.READ,
                        Effect.ALLOW,
                        null,
                        new com.colvenna.catalog.auth.Authorizer.Terms(
                                "one-off backfill",
                                TestServers.ENVIRONMENT,
                                "data-owner",
                                "CHG-7",
                                clock.instant().plus(java.time.Duration.ofHours(1))),
                        "test");
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", token, null).statusCode())
                .isEqualTo(200);
        clock.advance(java.time.Duration.ofHours(2));
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", token, null).statusCode())
                .isEqualTo(403);
        assertThat(
                        http("GET", "/v1/colvenna/databases/db/tables/t/token", token, null)
                                .statusCode())
                .isEqualTo(403);
    }

    @Test
    void grantsForAnotherEnvironmentDoNotCount() throws Exception {
        String token = issueToken("prod-reader", "P1D").get("token").asText();
        server.authorizer()
                .grant(
                        "prod-reader",
                        Scope.CATALOG,
                        null,
                        null,
                        Privilege.READ,
                        Effect.ALLOW,
                        null,
                        new com.colvenna.catalog.auth.Authorizer.Terms(
                                "dashboards",
                                "prod",
                                "data-owner",
                                "CHG-8",
                                clock.instant().plus(java.time.Duration.ofDays(1))),
                        "test");
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", token, null).statusCode())
                .isEqualTo(403);
    }

    @Test
    void grantsNameTheirTerms() throws Exception {
        issueToken("bi-reader", "P1D");
        String noApproval =
                "{\"principal\":\"bi-reader\",\"scope\":\"catalog\",\"privilege\":\"read\","
                        + "\"purpose\":\"x\",\"expiresAt\":\"2099-01-01T00:00:00Z\"}";
        assertThat(http("POST", "/colvenna/admin/v1/grants", adminToken, noApproval).statusCode())
                .isEqualTo(400);
        String expired =
                "{\"principal\":\"bi-reader\",\"scope\":\"catalog\",\"privilege\":\"read\","
                        + "\"purpose\":\"x\",\"approvedBy\":\"o\",\"approval\":\"C-1\","
                        + "\"expiresAt\":\"2000-01-01T00:00:00Z\"}";
        assertThat(http("POST", "/colvenna/admin/v1/grants", adminToken, expired).statusCode())
                .isEqualTo(400);
    }

    @Test
    void adminsHoldNoImplicitCatalogAccess() throws Exception {
        String root = server.tokens().bootstrapAdmin("root", java.time.Duration.ofHours(1)).token();
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", root, null).statusCode())
                .isEqualTo(403);
        assertThat(http("GET", "/colvenna/admin/v1/grants", root, null).statusCode())
                .isEqualTo(200);
    }

    @Test
    void namesWithDollarAreTheirOwnObjects() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            admin.createView(Identifier.create("db", "v$x"), view(), false);
            admin.createView(Identifier.create("db", "v"), view(), false);
        }
        String owner = identity("view-owner", Scope.VIEW, "db", "v", Privilege.MANAGE);
        assertThat(
                        http("DELETE", "/v1/colvenna/databases/db/views/v%24x", owner, null)
                                .statusCode())
                .isEqualTo(403);
        assertThat(http("DELETE", "/v1/colvenna/databases/db/views/v", owner, null).statusCode())
                .isEqualTo(200);
    }

    @Test
    void aDenyOnAnObjectHoldsAgainstCreatingAndDroppingItsDatabase() throws Exception {
        String owner = identity("db-owner", Scope.DATABASE, "db", null, Privilege.MANAGE);
        grant("db-owner", Scope.TABLE, "db", "t", Privilege.MANAGE, Effect.DENY, null);
        grant("db-owner", Scope.TABLE, "db", "protected", Privilege.MANAGE, Effect.DENY, null);
        try (RESTCatalog catalog = catalog(owner)) {
            assertThatThrownBy(
                            () ->
                                    catalog.createTable(
                                            Identifier.create("db", "protected"), schema(), false))
                    .hasStackTraceContaining("has no permission");
            assertThatThrownBy(() -> catalog.dropDatabase("db", false, true))
                    .hasStackTraceContaining("has no permission");
        }
        try (RESTCatalog admin = catalog(adminToken)) {
            assertThat(admin.listTables("db")).contains("t");
        }
    }

    @Test
    void aDatabaseNamedUnknownIsJudgedByItsName() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            Tables.createAppendTable(admin, Identifier.create("unknown", "t"), FAST_RETRY);
        }
        String reader = identity("u-reader", Scope.TABLE, "unknown", "t", Privilege.READ);
        assertThat(
                        http("GET", "/v1/colvenna/databases/unknown/tables/t", reader, null)
                                .statusCode())
                .isEqualTo(200);
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", reader, null).statusCode())
                .isEqualTo(403);
    }

    @Test
    void pagingNeverRevealsHiddenNames() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            for (String name : List.of("a", "b", "z")) {
                Tables.createAppendTable(admin, Identifier.create("db", name), FAST_RETRY);
            }
        }
        String reader = identity("t-reader", Scope.TABLE, "db", "t", Privilege.READ);
        java.util.Set<String> seen = new java.util.HashSet<>();
        String pageToken = null;
        do {
            JsonNode page =
                    RESTApi.OBJECT_MAPPER.readTree(
                            http(
                                            "GET",
                                            "/v1/colvenna/databases/db/tables?maxResults=1"
                                                    + (pageToken == null
                                                            ? ""
                                                            : "&pageToken=" + pageToken),
                                            reader,
                                            null)
                                    .body());
            page.get("tables").forEach(t -> seen.add(t.asText()));
            pageToken =
                    page.hasNonNull("nextPageToken") ? page.get("nextPageToken").asText() : null;
            if (pageToken != null) {
                seen.add(pageToken);
            }
        } while (pageToken != null);
        assertThat(seen).containsExactly("t");
        JsonNode databases =
                RESTApi.OBJECT_MAPPER.readTree(
                        http("GET", "/v1/colvenna/databases?maxResults=1", reader, null).body());
        assertThat(databases.get("databases").toString()).isEqualTo("[\"db\"]");
    }

    @Test
    void allowedReadsAreAuditedWithThePolicyVersion() throws Exception {
        String reader = identity("bi-reader", Scope.DATABASE, "db", null, Privilege.READ);
        assertThat(http("GET", "/v1/colvenna/databases/db/tables/t", reader, null).statusCode())
                .isEqualTo(200);
        JsonNode events =
                RESTApi.OBJECT_MAPPER
                        .readTree(
                                http(
                                                "GET",
                                                "/colvenna/admin/v1/audit?principal=bi-reader&action=GET_TABLE",
                                                adminToken,
                                                null)
                                        .body())
                        .get("events");
        assertThat(events).hasSize(1);
        JsonNode e = events.get(0);
        assertThat(e.get("outcome").asText()).isEqualTo("SUCCEEDED");
        assertThat(e.get("resource").asText()).isEqualTo("db.t");
        assertThat(e.get("detail").get("policyVersion").asLong()).isPositive();
        assertThat(e.get("detail").get("grants")).hasSize(1);
    }

    @Test
    void denialsCarryThePolicyVersionAndTheLevelNeeded() throws Exception {
        String reader = identity("bi-reader", Scope.TABLE, "db", "t", Privilege.READ);
        long deny = grant("bi-reader", Scope.TABLE, "db", "u", Privilege.READ, Effect.DENY, null);
        long version = policyVersion;
        // Refused by the route's own check: a level too low, a deny, a database not visible.
        HttpResponse<String> commit =
                http("POST", "/v1/colvenna/databases/db/tables/t/commit", reader, "{}");
        HttpResponse<String> denied =
                http("GET", "/v1/colvenna/databases/db/tables/u", reader, null);
        HttpResponse<String> listing =
                http("GET", "/v1/colvenna/databases/other/tables", reader, null);
        HttpResponse<String> create =
                http("POST", "/v1/colvenna/databases/db/tables", reader, "{}");
        for (var call :
                List.of(
                        Map.entry(commit, "WRITE"),
                        Map.entry(denied, "READ"),
                        Map.entry(listing, "READ"),
                        Map.entry(create, "MANAGE"))) {
            assertThat(call.getKey().statusCode()).isEqualTo(403);
            JsonNode e = eventOf(call.getKey(), "bi-reader");
            assertThat(e.get("outcome").asText()).isEqualTo("DENIED");
            assertThat(e.get("httpStatus").asInt()).isEqualTo(403);
            assertThat(e.get("detail").get("policyVersion").asLong())
                    .as(e.toString())
                    .isEqualTo(version);
            assertThat(e.get("detail").get("privilege").asText())
                    .as(e.toString())
                    .isEqualTo(call.getValue());
            assertThat(e.get("detail").get("error").asText()).contains("no permission");
        }
        // The deny that decided it is the grant the denial relied on.
        assertThat(eventOf(denied, "bi-reader").get("detail").get("grants").toString())
                .isEqualTo("[" + deny + "]");
    }

    @Test
    void aDenialInTheHandlerCarriesItsOwnDecision() throws Exception {
        String owner = identity("db-owner", Scope.DATABASE, "db", null, Privilege.MANAGE);
        long deny =
                grant(
                        "db-owner",
                        Scope.TABLE,
                        "db",
                        "protected",
                        Privilege.MANAGE,
                        Effect.DENY,
                        null);
        long version = policyVersion;
        // MANAGE on the database passes the route's check; the new name's deny refuses it.
        HttpResponse<String> create =
                http(
                        "POST",
                        "/v1/colvenna/databases/db/tables",
                        owner,
                        "{\"identifier\":{\"database\":\"db\",\"object\":\"protected\"},"
                                + "\"schema\":{\"fields\":[],\"partitionKeys\":[],"
                                + "\"primaryKeys\":[],\"options\":{}}}");
        assertThat(create.statusCode()).as(create.body()).isEqualTo(403);
        JsonNode e = eventOf(create, "db-owner");
        assertThat(e.get("outcome").asText()).isEqualTo("DENIED");
        assertThat(e.get("detail").get("policyVersion").asLong()).isEqualTo(version);
        assertThat(e.get("detail").get("privilege").asText()).isEqualTo("MANAGE");
        assertThat(e.get("detail").get("grants").toString()).isEqualTo("[" + deny + "]");
    }

    @Test
    void aCreatedDatabaseIsAuditedWithTheGrantThatAllowedIt() throws Exception {
        String creator = identity("db-creator", Scope.DATABASE, "fresh", null, Privilege.MANAGE);
        long allow = grantId;
        HttpResponse<String> created =
                http("POST", "/v1/colvenna/databases", creator, "{\"name\":\"fresh\"}");
        assertThat(created.statusCode()).as(created.body()).isEqualTo(200);
        JsonNode e = eventOf(created, "db-creator");
        assertThat(e.get("action").asText()).isEqualTo("CREATE_DATABASE");
        assertThat(e.get("resource").asText()).isEqualTo("fresh");
        assertThat(e.get("detail").get("privilege").asText()).isEqualTo("MANAGE");
        // The handler's own decision, not the route's, which decides nothing.
        assertThat(e.get("detail").get("grants").toString()).isEqualTo("[" + allow + "]");
    }

    @Test
    void lookupsByIdAreAuditedAndARefusalNamesOnlyTheId() throws Exception {
        Identifier secret = Identifier.create("db", "secret_orders");
        try (RESTCatalog admin = catalog(adminToken)) {
            Tables.createAppendTable(admin, secret, Map.of());
        }
        String tableId =
                RESTApi.OBJECT_MAPPER
                        .readTree(
                                http(
                                                "GET",
                                                "/v1/colvenna/databases/db/tables/secret_orders",
                                                adminToken,
                                                null)
                                        .body())
                        .get("id")
                        .asText();
        String reader = identity("t-reader", Scope.TABLE, "db", "secret_orders", Privilege.READ);
        String outsider = issueToken("outsider", "P1D").get("token").asText();
        long version = policyVersion;

        HttpResponse<String> allowed =
                http("GET", "/v1/colvenna/tables/id/" + tableId, reader, null);
        assertThat(allowed.statusCode()).isEqualTo(200);
        JsonNode a = eventOf(allowed, "t-reader");
        assertThat(a.get("action").asText()).isEqualTo("GET_TABLE_BY_ID");
        assertThat(a.get("outcome").asText()).isEqualTo("SUCCEEDED");
        assertThat(a.get("resource").asText()).isEqualTo("db.secret_orders");
        assertThat(a.get("detail").get("privilege").asText()).isEqualTo("READ");
        assertThat(a.get("detail").get("grants")).hasSize(1);

        HttpResponse<String> refused =
                http("GET", "/v1/colvenna/tables/id/" + tableId, outsider, null);
        assertThat(refused.statusCode()).isEqualTo(404);
        assertThat(refused.body()).doesNotContain("secret_orders");
        JsonNode r = eventOf(refused, "outsider");
        assertThat(r.get("action").asText()).isEqualTo("GET_TABLE_BY_ID");
        assertThat(r.get("outcome").asText()).isEqualTo("DENIED");
        assertThat(r.get("httpStatus").asInt()).isEqualTo(404);
        assertThat(r.get("resource").asText()).isEqualTo(tableId);
        assertThat(r.get("detail").get("policyVersion").asLong()).isEqualTo(version);
        assertThat(r.get("detail").get("privilege").asText()).isEqualTo("READ");
        assertThat(r.toString()).doesNotContain("secret_orders");
    }

    @Test
    void listingsAreAuditedButTheConfigIsNot() throws Exception {
        String reader = identity("bi-reader", Scope.DATABASE, "db", null, Privilege.READ);
        long version = policyVersion;
        HttpResponse<String> databases = http("GET", "/v1/colvenna/databases", reader, null);
        HttpResponse<String> tables = http("GET", "/v1/colvenna/tables", reader, null);
        for (var call :
                List.of(Map.entry(databases, "LIST_DATABASES"), Map.entry(tables, "LIST_TABLES"))) {
            assertThat(call.getKey().statusCode()).isEqualTo(200);
            JsonNode e = eventOf(call.getKey(), "bi-reader");
            assertThat(e.get("action").asText()).isEqualTo(call.getValue());
            assertThat(e.get("outcome").asText()).isEqualTo("SUCCEEDED");
            assertThat(e.get("httpStatus").asInt()).isEqualTo(200);
            assertThat(e.get("detail").get("policyVersion").asLong()).isEqualTo(version);
        }
        HttpResponse<String> config = http("GET", "/v1/config?warehouse=colvenna", reader, null);
        assertThat(config.statusCode()).isEqualTo(200);
        assertThat(eventsOf("bi-reader").toString())
                .doesNotContain(config.headers().firstValue("X-Request-Id").orElseThrow());
    }

    @Test
    void unclassifiedActionsAreRefused() throws Exception {
        com.colvenna.catalog.auth.Principal admin =
                new com.colvenna.catalog.auth.Principal("admin", true, null);
        assertThatThrownBy(
                        () ->
                                server.authorizer()
                                        .check(
                                                admin,
                                                "SOMETHING_NEW",
                                                List.of("colvenna", "databases", "db")))
                .hasMessageContaining("not authorized");
    }

    @Test
    void everyCatalogRouteIsClassified() throws Exception {
        String source =
                java.nio.file.Files.readString(
                        java.nio.file.Path.of(
                                "src/main/java/com/colvenna/catalog/http/CatalogRoutes.java"));
        java.util.regex.Matcher m =
                java.util.regex.Pattern.compile("Route\\.(read|write)\\(\\s*\"([A-Z_]+)\"")
                        .matcher(source);
        java.util.Set<String> actions = new java.util.TreeSet<>();
        while (m.find()) {
            actions.add(m.group(2));
        }
        assertThat(actions).hasSizeGreaterThan(40);
        assertThat(actions).allMatch(com.colvenna.catalog.auth.Authorizer::classified);
    }

    private String identity(
            String principal,
            Scope scope,
            @Nullable String db,
            @Nullable String object,
            Privilege p)
            throws Exception {
        String token = issueToken(principal, "P1D").get("token").asText();
        grant(principal, scope, db, object, p, Effect.ALLOW, null);
        return token;
    }

    /** The policy version the latest grant made, and the grant's id. */
    private long policyVersion;

    private long grantId;

    /** Grants and returns the grant's id. */
    private long grant(
            String principal,
            Scope scope,
            @Nullable String db,
            @Nullable String object,
            Privilege privilege,
            Effect effect,
            @Nullable List<String> columns)
            throws Exception {
        Map.Entry<com.colvenna.catalog.auth.Authorizer.Grant, Long> granted =
                server.authorizer()
                        .grant(
                                principal,
                                scope,
                                db,
                                object,
                                privilege,
                                effect,
                                columns,
                                TestServers.testTerms(),
                                "test");
        policyVersion = granted.getValue();
        grantId = granted.getKey().id();
        return grantId;
    }

    /** Every audit event of {@code principal}. */
    private JsonNode eventsOf(String principal) throws Exception {
        return auditEvents("principal=" + principal);
    }

    /** The audit event, among {@code principal}'s, of the request that got {@code response}. */
    private JsonNode eventOf(HttpResponse<String> response, String principal) throws Exception {
        return auditEventOf(response, "principal=" + principal);
    }

    private int auth(String token, @Nullable List<String> select) throws Exception {
        String body =
                select == null
                        ? "{}"
                        : "{\"select\":" + RESTApi.OBJECT_MAPPER.writeValueAsString(select) + "}";
        return http("POST", "/v1/colvenna/databases/db/tables/t/auth", token, body).statusCode();
    }

    private static org.apache.paimon.view.View view() {
        return new org.apache.paimon.view.ViewImpl(
                Identifier.create("db", "v"),
                List.of(new org.apache.paimon.types.DataField(0, "id", DataTypes.INT())),
                "SELECT 1",
                Map.of(),
                null,
                Map.of());
    }

    private static Schema schema() {
        return Schema.newBuilder().column("id", DataTypes.INT()).build();
    }
}
