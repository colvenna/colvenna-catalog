package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.ServerTestBase;
import java.net.ConnectException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.http.HttpResponse;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.Duration;
import java.util.Collections;
import java.util.List;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

/** API-key authentication of every catalog and admin route. */
class AuthTest extends ServerTestBase {

    private static final String CONFIG = "/v1/config?warehouse=colvenna";

    @Test
    void requestWithoutTokenIsRejected() throws Exception {
        assertThat(http("GET", CONFIG, null, null).statusCode()).isEqualTo(401);
        assertThat(http("GET", "/v1/colvenna/databases", null, null).statusCode()).isEqualTo(401);
        assertThat(http("GET", "/colvenna/admin/v1/principals", null, null).statusCode())
                .isEqualTo(401);
    }

    @Test
    void unknownTokenIsRejected() throws Exception {
        assertThat(http("GET", CONFIG, "colvenna_not-a-real-token", null).statusCode())
                .isEqualTo(401);
    }

    @Test
    void unknownRouteNeedsAuthenticationFirst() throws Exception {
        assertThat(http("GET", "/v1/colvenna/no-such-thing", null, null).statusCode())
                .isEqualTo(401);
        assertThat(http("GET", "/v1/colvenna/no-such-thing", adminToken, null).statusCode())
                .isEqualTo(404);
    }

    @Test
    void validTokenIsAccepted() throws Exception {
        assertThat(http("GET", CONFIG, adminToken, null).statusCode()).isEqualTo(200);
    }

    @Test
    void expiredTokenIsRejected() throws Exception {
        String token = issue("etl-writer", "PT1M");
        assertThat(http("GET", CONFIG, token, null).statusCode()).isEqualTo(200);
        clock.advance(Duration.ofMinutes(2));
        HttpResponse<String> response = http("GET", CONFIG, token, null);
        assertThat(response.statusCode()).isEqualTo(401);
        assertThat(response.body()).contains("expired");
    }

    @Test
    void revokedTokenIsRejected() throws Exception {
        JsonNode issued = issueToken("etl-writer", "P1D");
        String token = issued.get("token").asText();
        assertThat(http("GET", CONFIG, token, null).statusCode()).isEqualTo(200);
        HttpResponse<String> revoke =
                http(
                        "DELETE",
                        "/colvenna/admin/v1/tokens/" + issued.get("id").asText(),
                        adminToken,
                        null);
        assertThat(revoke.statusCode()).isEqualTo(200);
        HttpResponse<String> response = http("GET", CONFIG, token, null);
        assertThat(response.statusCode()).isEqualTo(401);
        assertThat(response.body()).contains("revoked");
    }

    @Test
    void disabledPrincipalIsRejected() throws Exception {
        String token = issue("etl-writer", "P1D");
        assertThat(
                        http(
                                        "POST",
                                        "/colvenna/admin/v1/principals/etl-writer/disable",
                                        adminToken,
                                        null)
                                .statusCode())
                .isEqualTo(200);
        assertThat(http("GET", CONFIG, token, null).statusCode()).isEqualTo(401);
    }

    @Test
    void serviceIdentityCannotUseAdminApi() throws Exception {
        String token = issue("etl-writer", "P1D");
        assertThat(http("GET", "/colvenna/admin/v1/principals", token, null).statusCode())
                .isEqualTo(403);
        assertThat(
                        http("POST", "/colvenna/admin/v1/principals/etl-writer/tokens", token, "{}")
                                .statusCode())
                .isEqualTo(403);
    }

    @Test
    void tokenSecretIsNotStoredAndIsReturnedOnce() throws Exception {
        String token = issue("etl-writer", "P1D");
        HttpResponse<String> list =
                http("GET", "/colvenna/admin/v1/principals/etl-writer/tokens", adminToken, null);
        assertThat(list.statusCode()).isEqualTo(200);
        assertThat(list.body()).doesNotContain(token);
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement select =
                        c.prepareStatement(
                                "SELECT count(*) FROM api_tokens WHERE token_hash = ?")) {
            select.setBytes(1, token.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            try (ResultSet rs = select.executeQuery()) {
                rs.next();
                assertThat(rs.getInt(1)).isZero();
            }
        }
    }

    @Test
    void healthPortServesNoCatalogData() throws Exception {
        int health = server.healthPort();
        assertThat(http(health, "GET", "/healthz", null, null).statusCode()).isEqualTo(200);
        assertThat(http(health, "GET", "/readyz", null, null).statusCode()).isEqualTo(200);
        HttpResponse<String> config = http(health, "GET", CONFIG, null, null);
        assertThat(config.statusCode()).isEqualTo(404);
        assertThat(config.body()).isEmpty();
    }

    @Test
    void healthPortListensOnLoopbackOnly() throws Exception {
        InetAddress external =
                Collections.list(NetworkInterface.getNetworkInterfaces()).stream()
                        .flatMap(i -> Collections.list(i.getInetAddresses()).stream())
                        .filter(a -> a instanceof Inet4Address && !a.isLoopbackAddress())
                        .findFirst()
                        .orElse(null);
        Assumptions.assumeTrue(external != null, "no non-loopback address");
        try (Socket socket = new Socket()) {
            assertThatThrownBy(
                            () ->
                                    socket.connect(
                                            new InetSocketAddress(external, server.healthPort()),
                                            2000))
                    .isInstanceOf(ConnectException.class);
        }
    }

    @Test
    void rejectedRequestsAreAuditedAsDenied() throws Exception {
        http("POST", "/v1/colvenna/databases", null, "{\"name\":\"x\",\"options\":{}}");
        String token = issue("etl-writer", "P1D");
        http("GET", "/colvenna/admin/v1/principals", token, null);
        List<String> rows =
                queryColumn(
                        "SELECT action || ':' || outcome || ':' || http_status || ':' ||"
                                + " coalesce(principal, '-') FROM audit_events WHERE outcome = 'DENIED'"
                                + " ORDER BY id");
        assertThat(rows)
                .containsExactly(
                        "CREATE_DATABASE:DENIED:401:-", "LIST_PRINCIPALS:DENIED:403:etl-writer");
    }

    private String issue(String principal, String ttl) throws Exception {
        return issueToken(principal, ttl).get("token").asText();
    }
}
