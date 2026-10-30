package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.net.URLEncoder;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.Test;

/**
 * Requests whose work grows with the data, bounded in time (#69): this class's server works 1 ms on
 * each answer ({@code COLVENNA_REQUEST_WORK_TIME}), so it takes one step each time. A table's
 * consistency check takes several answers; followed through its tokens, the last answer is the
 * report a server without a work time gives in one. A database's drop takes several requests.
 */
class RequestWorkTimeTest extends ServerTestBase {

    private static final Identifier T = Identifier.create("db", "t");
    private static final Identifier OTHER = Identifier.create("db", "other");

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_REQUEST_WORK_TIME", "PT0.001S");
    }

    @Test
    void theLastPageIsTheWholeReport() throws Exception {
        tableWithHistory(T);
        List<JsonNode> pages = pages(server.port(), "db", "t");
        assertThat(pages).hasSizeGreaterThan(2);
        for (JsonNode page : pages.subList(0, pages.size() - 1)) {
            assertThat(page.get("consistent").asBoolean()).as(page.toString()).isFalse();
            assertThat(page.get("nextPageToken").asText()).isNotBlank();
        }
        JsonNode last = pages.get(pages.size() - 1);
        assertThat(last.get("nextPageToken").isNull()).isTrue();
        assertThat(last.get("consistent").asBoolean()).as(last.toString()).isTrue();
        assertThat(last).isEqualTo(wholeReport("db", "t"));
        JsonNode main = last.get("branches").get(0);
        assertThat(main.get("snapshotsChecked").asInt()).isEqualTo(3);
        assertThat(main.get("tagsChecked").asInt()).isEqualTo(1);
        assertThat(main.get("dataFilesChecked").asInt()).isEqualTo(3);
        assertThat(last.get("branches")).hasSize(2);
    }

    @Test
    void aMissingFileFoundOnAnEarlierPageStaysInTheReport() throws Exception {
        Path root = tableWithHistory(T);
        try (Stream<Path> files = Files.walk(root)) {
            Files.delete(
                    files.filter(p -> p.getFileName().toString().startsWith("data-"))
                            .sorted()
                            .findFirst()
                            .orElseThrow());
        }
        List<JsonNode> pages = pages(server.port(), "db", "t");
        JsonNode last = pages.get(pages.size() - 1);
        assertThat(last.get("consistent").asBoolean()).isFalse();
        assertThat(last.get("problems").toString()).contains("data file missing");
        assertThat(last.get("branches").get(0).get("dataFilesMissing").asInt()).isPositive();
    }

    @Test
    void theCheckIsKeptBetweenPagesAndItsLastAnswerAfterIt() throws Exception {
        tableWithHistory(T);
        String token = page(server.port(), "db", "t", null).get("nextPageToken").asText();
        assertThat(queryColumn("SELECT id FROM consistency_checks")).containsExactly(token);
        // The same check, asked again after a lost answer: it goes on.
        assertThat(page(server.port(), "db", "t", token).get("nextPageToken").asText())
                .isEqualTo(token);
        JsonNode last = null;
        for (int i = 0; i < 100 && last == null; i++) {
            JsonNode next = page(server.port(), "db", "t", token);
            last = next.get("nextPageToken").isNull() ? next : null;
        }
        assertThat(last).isNotNull();
        assertThat(last.get("consistent").asBoolean()).isTrue();
        // A last answer lost on its way: the same, asked for again.
        assertThat(page(server.port(), "db", "t", token)).isEqualTo(last);
        assertThat(queryColumn("SELECT id FROM consistency_checks")).containsExactly(token);
    }

    @Test
    void aDatabaseDropAnswers503UntilItHasDroppedEveryTable() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.createDatabase("many", false);
            for (String name : List.of("a", "b", "c")) {
                Tables.createAppendTable(catalog, Identifier.create("many", name), Map.of());
            }
        }
        List<Integer> statuses = new ArrayList<>();
        do {
            statuses.add(
                    http("DELETE", "/v1/colvenna/databases/many", adminToken, null).statusCode());
            assertThat(statuses).hasSizeLessThan(10);
        } while (statuses.get(statuses.size() - 1) == 503);
        // One table a request, then the database with the last.
        assertThat(statuses).containsExactly(503, 503, 200);
        assertThat(queryColumn("SELECT table_name FROM table_purges ORDER BY id"))
                .isEmpty(); // inline: each dropped with its files
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThat(catalog.listDatabases()).doesNotContain("many");
        }
    }

    @Test
    void aTokenIsForItsTableAlone() throws Exception {
        tableWithHistory(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, OTHER, Map.of());
        }
        String token = page(server.port(), "db", "t", null).get("nextPageToken").asText();
        assertThat(status("db", "other", token)).isEqualTo(400);
        assertThat(status("db", "t", "not-a-token")).isEqualTo(400);
        assertThat(status("db", "t", java.util.UUID.randomUUID().toString())).isEqualTo(400);
        assertThat(status("db", "t", token.substring(0, token.length() / 2))).isEqualTo(400);
        // Dropped and made again between two answers: another table.
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            Tables.createAppendTable(catalog, T, Map.of());
        }
        assertThat(status("db", "t", token)).isEqualTo(400);
    }

    /** Three commits, a tag on the first and a branch from it. */
    private Path tableWithHistory(Identifier id) throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.createDatabase(id.getDatabaseName(), true);
            Tables.createAppendTable(catalog, id, Map.of());
            FileStoreTable table = (FileStoreTable) catalog.getTable(id);
            Tables.write(table, 1);
            catalog.createTag(id, "first", 1L, null, false);
            Tables.write(table, 2);
            Tables.write(table, 3);
            catalog.createBranch(id, "b1", "first");
            return Path.of(java.net.URI.create(table.location().toUri().toString()));
        }
    }

    /** The report of a server without a page time, in one answer. */
    private JsonNode wholeReport(String db, String table) throws Exception {
        ColvennaCatalogServer whole =
                ColvennaCatalogServer.create(
                        TestServers.withSettings(warehouse, externalLocationRoots(), Map.of()),
                        clock,
                        dataTokens());
        whole.start();
        try {
            List<JsonNode> pages = pages(whole.port(), db, table);
            assertThat(pages).hasSize(1);
            return pages.get(0);
        } finally {
            whole.close();
        }
    }

    private List<JsonNode> pages(int port, String db, String table) throws Exception {
        List<JsonNode> pages = new ArrayList<>();
        String token = null;
        do {
            JsonNode page = page(port, db, table, token);
            pages.add(page);
            token = page.get("nextPageToken").isNull() ? null : page.get("nextPageToken").asText();
            assertThat(pages).as("pages").hasSizeLessThan(100);
        } while (token != null);
        // The token is what differs between pages; the rest of the last one is the report.
        ((ObjectNode) pages.get(pages.size() - 1)).putNull("nextPageToken");
        return pages;
    }

    private JsonNode page(int port, String db, String table, String token) throws Exception {
        HttpResponse<String> response = http(port, "GET", path(db, table, token), adminToken, null);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body());
    }

    private int status(String db, String table, String token) throws Exception {
        return http("GET", path(db, table, token), adminToken, null).statusCode();
    }

    private static String path(String db, String table, String token) {
        return "/colvenna/admin/v1/consistency/"
                + db
                + "/"
                + table
                + (token == null
                        ? ""
                        : "?pageToken=" + URLEncoder.encode(token, StandardCharsets.UTF_8));
    }
}
