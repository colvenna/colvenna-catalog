package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** Who gets table credentials from {@code GET .../token}, and what the audit trail keeps. */
class DataTokenTest extends ServerTestBase {

    // pragma: allowlist nextline secret - a fake S3 secret key, from a stubbed STS
    private static final String SECRET = "test-secret-key";
    private static final String SESSION = "test-session-token";
    private static final String TOKEN_PATH = "/v1/colvenna/databases/db/tables/t/token";

    private final AtomicInteger issued = new AtomicInteger();
    private final List<String> accessIssued = new java.util.concurrent.CopyOnWriteArrayList<>();

    @Override
    protected DataTokenIssuer dataTokens() {
        return (ctx, table, access) -> {
            issued.incrementAndGet();
            accessIssued.add(access.name());
            return new DataTokenIssuer.Token(
                    Map.of(
                            "fs.s3a.access.key", "test-access-key",
                            "fs.s3a.secret.key", SECRET,
                            "fs.s3a.session.token", SESSION),
                    clock.millis() + Duration.ofHours(3).toMillis(),
                    Map.of("roleSessionName", "colvenna-" + ctx.who(), "access", access.name()));
        };
    }

    @BeforeEach
    void createTable() throws Exception {
        try (RESTCatalog admin = catalog(adminToken)) {
            admin.createDatabase("db", false);
            Tables.createAppendTable(admin, Identifier.create("db", "t"), Map.of());
        }
    }

    /** Creates {@code principal} with a token and a catalog-wide grant of {@code privilege}. */
    private String identity(String principal, Authorizer.Privilege privilege) throws Exception {
        String token = issueToken(principal, "P1D").get("token").asText();
        server.authorizer()
                .grant(
                        principal,
                        Authorizer.Scope.CATALOG,
                        null,
                        null,
                        privilege,
                        Authorizer.Effect.ALLOW,
                        null,
                        TestServers.testTerms(),
                        "test");
        return token;
    }

    @Test
    void validIdentityGetsATokenAndTheAuditKeepsNoSecret() throws Exception {
        String token = identity("etl-writer", Authorizer.Privilege.WRITE);
        HttpResponse<String> response = http("GET", TOKEN_PATH, token, null);
        assertThat(response.statusCode()).isEqualTo(200);
        assertThat(response.body()).contains(SESSION);
        assertThat(issued).hasValue(1);
        List<String> audit =
                queryColumn(
                        "SELECT principal || ':' || outcome || ':' || detail::text FROM audit_events"
                                + " WHERE action = 'ISSUE_DATA_TOKEN' ORDER BY id");
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0))
                .startsWith("etl-writer:SUCCEEDED:")
                .contains("colvenna-etl-writer")
                .doesNotContain(SECRET)
                .doesNotContain(SESSION);
    }

    @Test
    void expiredRevokedAndDisabledIdentitiesGetNoToken() throws Exception {
        String expiring = issueToken("short-lived", "PT1M").get("token").asText();
        JsonNode revocable = issueToken("revoked", "P1D");
        String disabled = issueToken("disabled", "P1D").get("token").asText();
        http(
                "DELETE",
                "/colvenna/admin/v1/tokens/" + revocable.get("id").asText(),
                adminToken,
                null);
        http("POST", "/colvenna/admin/v1/principals/disabled/disable", adminToken, null);
        clock.advance(Duration.ofMinutes(2));
        for (String token :
                List.of(expiring, revocable.get("token").asText(), disabled, "colvenna_forged")) {
            assertThat(http("GET", TOKEN_PATH, token, null).statusCode()).isEqualTo(401);
        }
        assertThat(http("GET", TOKEN_PATH, null, null).statusCode()).isEqualTo(401);
        assertThat(issued).hasValue(0);
    }

    @Test
    void credentialsFollowTheGrant() throws Exception {
        String reader = identity("bi-reader", Authorizer.Privilege.READ);
        String writer = identity("etl-writer", Authorizer.Privilege.WRITE);
        String nobody = issueToken("outsider", "P1D").get("token").asText();
        assertThat(http("GET", TOKEN_PATH, reader, null).statusCode()).isEqualTo(200);
        assertThat(http("GET", TOKEN_PATH, writer, null).statusCode()).isEqualTo(200);
        assertThat(http("GET", TOKEN_PATH, nobody, null).statusCode()).isEqualTo(403);
        assertThat(accessIssued).containsExactly("READ", "READ_WRITE");
    }

    @Test
    void missingTablesGetNoToken() throws Exception {
        String token = identity("etl-writer", Authorizer.Privilege.READ);
        assertThat(
                        http("GET", "/v1/colvenna/databases/db/tables/nope/token", token, null)
                                .statusCode())
                .isEqualTo(404);
        assertThat(issued).hasValue(0);
        // A failed issuance is audited too.
        assertThat(
                        queryColumn(
                                "SELECT principal || ':' || outcome || ':' || http_status FROM"
                                        + " audit_events WHERE action = 'ISSUE_DATA_TOKEN'"))
                .containsExactly("etl-writer:FAILED:404");
    }
}
