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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.requests.RenameTableRequest;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Every audit event of a request on a table names the table as {@code <database>.<table>}, whether
 * the request succeeded, failed or was refused, so that a query by {@code resource=db.t} finds them
 * all.
 */
class AuditResourceTest extends ServerTestBase {

    private static final Identifier T = Identifier.create("db", "t");

    /** A refused or fenced commit is retried by the client; keep the retries short. */
    private static final Map<String, String> FAST_RETRY =
            Map.of(
                    "commit.min-retry-wait", "10 ms",
                    "commit.max-retry-wait", "50 ms",
                    "commit.max-retries", "1");

    @BeforeEach
    void table() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            Tables.createAppendTable(admin, T, FAST_RETRY);
            Tables.write(admin.getTable(T), 1);
        }
    }

    @Test
    void aFencedCommitIsAuditedUnderItsTable() throws Exception {
        // A writer on a file-system catalog commits to the same directory: the table is fenced.
        Options options = new Options();
        options.set(CatalogOptions.WAREHOUSE, warehouse);
        try (Catalog foreign = CatalogFactory.createCatalog(CatalogContext.create(options))) {
            Tables.write(foreign.getTable(T), 2);
        }
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThatThrownBy(() -> Tables.write(catalog.getTable(T), 3))
                    .hasStackTraceContaining("fenced");
        }
        List<String> fenced = new ArrayList<>();
        for (JsonNode e : auditEvents("action=COMMIT&resource=db.t")) {
            if (e.get("httpStatus").asInt() == 409) {
                fenced.add(e.get("outcome").asText());
            }
        }
        assertThat(fenced).isNotEmpty().containsOnly("FAILED");
        assertThat(auditEvents("resource=t")).isEmpty();
    }

    @Test
    void aRefusedCommitIsAuditedUnderItsTable() throws Exception {
        HttpResponse<String> refused =
                http("POST", "/v1/colvenna/databases/db/tables/t/commit", reader(), "{}");
        assertThat(refused.statusCode()).isEqualTo(403);
        JsonNode e = auditEventOf(refused, "action=COMMIT&resource=db.t");
        assertThat(e.get("outcome").asText()).isEqualTo("DENIED");
    }

    @Test
    void aFailedRequestOnATableIsAuditedUnderItsTable() throws Exception {
        HttpResponse<String> missing =
                http("GET", "/v1/colvenna/databases/db/tables/missing", adminToken, null);
        assertThat(missing.statusCode()).isEqualTo(404);
        assertThat(auditEventOf(missing, "resource=db.missing").get("outcome").asText())
                .isEqualTo("FAILED");
        HttpResponse<String> unregister =
                http("POST", "/colvenna/admin/v1/tables/db/missing/unregister", adminToken, null);
        assertThat(unregister.statusCode()).isEqualTo(404);
        assertThat(auditEventOf(unregister, "resource=db.missing").get("action").asText())
                .isEqualTo("UNREGISTER_TABLE");
    }

    @Test
    void aCreateThatFailsAfterItsAuthorizationIsAuditedUnderTheNewTable() throws Exception {
        // A location outside the allowed roots: refused (400) once authorized, naming nothing.
        HttpResponse<String> failed =
                http(
                        "POST",
                        "/v1/colvenna/databases/db/tables",
                        adminToken,
                        "{\"identifier\":{\"database\":\"db\",\"object\":\"bad\"},"
                                + "\"schema\":{\"fields\":[{\"id\":0,\"name\":\"a\","
                                + "\"type\":\"INT\"}],\"partitionKeys\":[],\"primaryKeys\":[],"
                                + "\"options\":{\"path\":\"file:///not-allowed/bad\"}}}");
        assertThat(failed.statusCode()).as(failed.body()).isEqualTo(400);
        assertThat(auditEventOf(failed, "resource=db.bad").get("action").asText())
                .isEqualTo("CREATE_TABLE");
    }

    @Test
    void aFailedRequestOnAViewIsAuditedUnderIt() throws Exception {
        HttpResponse<String> missing =
                http("GET", "/v1/colvenna/databases/db/views/nov", adminToken, null);
        assertThat(missing.statusCode()).isEqualTo(404);
        assertThat(auditEventOf(missing, "resource=db.nov").get("outcome").asText())
                .isEqualTo("FAILED");
    }

    @Test
    void aRenameIsAuditedUnderItsSourceTable() throws Exception {
        HttpResponse<String> renamed = rename("t", "u", adminToken);
        assertThat(renamed.statusCode()).as(renamed.body()).isEqualTo(200);
        assertThat(auditEventOf(renamed, "resource=db.t").get("outcome").asText())
                .isEqualTo("SUCCEEDED");
        HttpResponse<String> failed = rename("nothere", "w", adminToken);
        assertThat(failed.statusCode()).isEqualTo(404);
        assertThat(auditEventOf(failed, "resource=db.nothere").get("outcome").asText())
                .isEqualTo("FAILED");
        HttpResponse<String> refused = rename("u", "x", reader());
        assertThat(refused.statusCode()).isEqualTo(403);
        assertThat(auditEventOf(refused, "resource=db.u").get("outcome").asText())
                .isEqualTo("DENIED");
    }

    private HttpResponse<String> rename(String from, String to, String token) throws Exception {
        return http(
                "POST",
                "/v1/colvenna/tables/rename",
                token,
                RESTApi.toJson(
                        new RenameTableRequest(
                                Identifier.create("db", from), Identifier.create("db", to))));
    }

    /** A reader of the database {@code db}: it may not write. */
    private String reader() throws Exception {
        String token = issueToken("reader", "P1D").get("token").asText();
        server.authorizer()
                .grant(
                        "reader",
                        Scope.DATABASE,
                        "db",
                        null,
                        Privilege.READ,
                        Effect.ALLOW,
                        null,
                        TestServers.testTerms(),
                        "test");
        return token;
    }
}
