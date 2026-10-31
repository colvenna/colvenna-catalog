package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.testing.ServerTestBase;
import java.net.http.HttpResponse;
import java.util.Map;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

/**
 * The client options the catalog's config answers as defaults (#1): a Paimon client takes them
 * unless it sets the key itself; {@link ClientOverridesTest} has the operator's overrides.
 */
class ClientDefaultsTest extends ServerTestBase {

    static final String KEY = "fs.s3a.create.performance";

    @Test
    void theConfigAnswersTheDefaults() throws Exception {
        HttpResponse<String> answer =
                http("GET", "/v1/config?warehouse=colvenna", adminToken, null);
        assertThat(answer.statusCode()).isEqualTo(200);
        JsonNode body = new ObjectMapper().readTree(answer.body());
        assertThat(body.path("defaults").path(KEY).asText()).isEqualTo("true");
        assertThat(body.path("overrides").has(KEY)).isFalse();
    }

    @Test
    void aClientTakesTheDefault() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThat(catalog.options()).containsEntry(KEY, "true");
        }
    }

    @Test
    void aClientsOwnSettingWins() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken, server.port(), Map.of(KEY, "false"))) {
            assertThat(catalog.options()).containsEntry(KEY, "false");
        }
    }
}
