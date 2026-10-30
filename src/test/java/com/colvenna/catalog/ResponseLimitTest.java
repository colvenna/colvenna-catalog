package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.ServerTestBase;
import java.net.URLEncoder;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.table.Table;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.Test;

/**
 * Under {@code COLVENNA_MAX_RESPONSE_BYTES} no response body is larger than the limit, counted as
 * the Lambda response carries it: list pages are cut short and continue on the next page, and a
 * response that cannot fit is a clear error instead of the entry's bare 502.
 */
class ResponseLimitTest extends ServerTestBase {

    private static final int LIMIT = 30_000;
    private static final int TABLES = 12;

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_MAX_RESPONSE_BYTES", String.valueOf(LIMIT));
    }

    @Test
    void listPagesAreCutToTheLimitAndTheClientReadsThemAll() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("many", false);
        List<String> names = new ArrayList<>();
        for (int i = 0; i < TABLES; i++) {
            names.add(String.format("t%02d", i));
            catalog.createTable(Identifier.create("many", names.get(i)), schema(100), false);
        }

        List<String> listed = new ArrayList<>();
        String token = null;
        int pages = 0;
        do {
            HttpResponse<String> page =
                    http(
                            "GET",
                            "/v1/colvenna/databases/many/table-details"
                                    + (token == null
                                            ? ""
                                            : "?pageToken="
                                                    + URLEncoder.encode(
                                                            token, StandardCharsets.UTF_8)),
                            adminToken,
                            null);
            assertThat(page.statusCode()).as(page.body()).isEqualTo(200);
            assertThat(escaped(page.body())).isLessThanOrEqualTo(LIMIT);
            JsonNode body = RESTApi.OBJECT_MAPPER.readTree(page.body());
            assertThat(body.get("tableDetails")).isNotEmpty();
            body.get("tableDetails").forEach(t -> listed.add(t.get("name").asText()));
            JsonNode next = body.get("nextPageToken");
            token = next == null || next.isNull() ? null : next.asText();
            pages++;
        } while (token != null);
        assertThat(pages).isGreaterThan(1);
        assertThat(listed).containsExactlyElementsOf(names);

        // Paimon's own listing follows the tokens across the shorter pages.
        assertThat(catalog.listTableDetails("many")).extracting(Table::name).isEqualTo(names);
    }

    @Test
    void aResponseOverTheLimitIsAClearError() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("wide", false);
        catalog.createTable(Identifier.create("wide", "events"), schema(1_000), false);

        HttpResponse<String> table =
                http("GET", "/v1/colvenna/databases/wide/tables/events", adminToken, null);
        assertThat(table.statusCode()).isEqualTo(500);
        assertThat(table.body())
                .contains("response limit of " + LIMIT + " bytes")
                .contains("COLVENNA_MAX_RESPONSE_BYTES");
        assertThatThrownBy(() -> catalog.getTable(Identifier.create("wide", "events")))
                .hasStackTraceContaining("COLVENNA_MAX_RESPONSE_BYTES");

        // A listing whose first element cannot fit says which element, not an empty page.
        HttpResponse<String> listing =
                http("GET", "/v1/colvenna/databases/wide/table-details", adminToken, null);
        assertThat(listing.statusCode()).isEqualTo(500);
        assertThat(listing.body()).contains("events takes").contains("COLVENNA_MAX_RESPONSE_BYTES");
        assertThat(
                        queryColumn(
                                "SELECT outcome || ' ' || http_status FROM audit_events"
                                        + " WHERE action = 'GET_TABLE'"))
                .containsOnly("FAILED 500");
        // Small responses are unaffected.
        assertThat(http("GET", "/v1/colvenna/databases/wide", adminToken, null).statusCode())
                .isEqualTo(200);
    }

    private static Schema schema(int columns) {
        Schema.Builder schema = Schema.newBuilder();
        for (int i = 0; i < columns; i++) {
            schema.column(
                    String.format("a_column_with_a_rather_long_name_%04d", i), DataTypes.STRING());
        }
        return schema.build();
    }

    /** The body's size once JSON-escaped; compact JSON has no raw control characters. */
    private static long escaped(String body) {
        long size = body.getBytes(StandardCharsets.UTF_8).length;
        for (int i = 0; i < body.length(); i++) {
            char c = body.charAt(i);
            if (c == '"' || c == '\\') {
                size++;
            }
        }
        return size;
    }
}
