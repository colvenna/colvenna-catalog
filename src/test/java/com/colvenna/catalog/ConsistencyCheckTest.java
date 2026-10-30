package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.catalog.ConsistencyCheck;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.PreparedStatement;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.Test;

/** The admin consistency report that recovery evidence relies on. */
class ConsistencyCheckTest extends ServerTestBase {

    private static final Identifier ID = Identifier.create("db", "t");

    @Test
    void aCleanTableIsConsistent() throws Exception {
        tableWithTwoCommits();
        JsonNode report = report();
        assertThat(report.get("consistent").asBoolean()).as(report.toString()).isTrue();
        JsonNode main = report.get("branches").get(0);
        assertThat(main.get("branch").asText()).isEqualTo("main");
        assertThat(main.get("pointerSnapshot").asLong()).isEqualTo(2);
        assertThat(main.get("highestSnapshotFile").asLong()).isEqualTo(2);
        assertThat(main.get("pendingIntents").asInt()).isZero();
        assertThat(main.get("dataFilesChecked").asInt()).isEqualTo(2);
        assertThat(main.get("dataFilesMissing").asInt()).isZero();
    }

    @Test
    void aMissingDataFileAndAnUnpublishedSnapshotAreReported() throws Exception {
        Path root = tableWithTwoCommits();
        try (Stream<Path> files = Files.walk(root)) {
            Path data =
                    files.filter(p -> p.getFileName().toString().startsWith("data-"))
                            .findFirst()
                            .orElseThrow();
            Files.delete(data);
        }
        Path snapshots = root.resolve("snapshot");
        Files.copy(snapshots.resolve("snapshot-2"), snapshots.resolve("snapshot-3"));
        JsonNode report = report();
        assertThat(report.get("consistent").asBoolean()).isFalse();
        assertThat(report.get("problems").toString())
                .contains("data file missing")
                .contains("snapshot file 3 exists beyond the published pointer 2");
    }

    @Test
    void theCatalogsCopiesOfSnapshotAndSchemaAreComparedWithTheFiles() throws Exception {
        tableWithTwoCommits();
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement snapshot =
                        c.prepareStatement(
                                "UPDATE table_branches SET snapshot_json ="
                                        + " replace(snapshot_json, '\"commitIdentifier\"',"
                                        + " '\"commitIdentifier\" : 7, \"unused\"')");
                PreparedStatement schema =
                        c.prepareStatement(
                                "UPDATE catalog_tables SET schema_json ="
                                        + " replace(schema_json, '\"payload\"', '\"renamed\"')")) {
            assertThat(snapshot.executeUpdate()).isEqualTo(1);
            assertThat(schema.executeUpdate()).isEqualTo(1);
            c.commit();
        }
        JsonNode report = report();
        assertThat(report.get("problems").toString())
                .contains("the catalog's copy of snapshot 2 differs from its file")
                .contains("with other fields, keys or options than its schema file");
    }

    @Test
    void unreadableMetadataIsAProblemNotAnError() throws Exception {
        Path root = tableWithTwoCommits();
        try (Stream<Path> files = Files.walk(root.resolve("manifest"))) {
            for (Path manifest : files.filter(Files::isRegularFile).toList()) {
                Files.delete(manifest);
            }
        }
        JsonNode report = report();
        assertThat(report.get("consistent").asBoolean()).isFalse();
        assertThat(report.get("problems").toString()).contains("cannot read the table's metadata");
    }

    @Test
    void aTableWhoseFilesAreGoneIsReportedMissingAndCanBeUnregistered() throws Exception {
        Path root = tableWithTwoCommits();
        // As after a metadata restore: dropped since the restore point, and its files with it.
        deleteTree(root);
        JsonNode report = report();
        assertThat(report.get("consistent").asBoolean()).isFalse();
        assertThat(report.get("schemaIdFiles").isNull()).isTrue();
        assertThat(report.get("problems").toString())
                .contains("no schema file in " + report.get("path").asText())
                .contains("main: pointer snapshot 2 has no file");
        JsonNode main = report.get("branches").get(0);
        assertThat(main.get("pointerSnapshot").asLong()).isEqualTo(2);
        assertThat(main.get("highestSnapshotFile").isNull()).isTrue();

        // Nothing to reconcile from: said so, not a server error.
        HttpResponse<String> reconciled =
                http("POST", "/colvenna/admin/v1/tables/db/t/reconcile", adminToken, null);
        assertThat(reconciled.statusCode()).as(reconciled.body()).isEqualTo(400);
        assertThat(reconciled.body()).contains("No schema file").contains("unregister");
        HttpResponse<String> unregistered =
                http("POST", "/colvenna/admin/v1/tables/db/t/unregister", adminToken, null);
        assertThat(unregistered.statusCode()).as(unregistered.body()).isEqualTo(200);
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThat(catalog.listTables("db")).isEmpty();
        }
    }

    @Test
    void aTableWithoutItsSchemaFilesIsReportedNotAnError() throws Exception {
        Path root = tableWithTwoCommits();
        deleteTree(root.resolve("schema"));
        JsonNode report = report();
        assertThat(report.get("consistent").asBoolean()).isFalse();
        assertThat(report.get("problems").toString())
                .contains("no schema file in " + report.get("path").asText());
        assertThat(report.get("branches").get(0).get("pointerSnapshot").asLong()).isEqualTo(2);
    }

    @Test
    void theReportShowsWhatItFoundBeforeResolvingIntents() throws Exception {
        tableWithTwoCommits();
        JsonNode main = report().get("branches").get(0);
        assertThat(main.get("found").get("pointerSnapshot").asLong()).isEqualTo(2);
        assertThat(main.get("found").get("pendingIntents").asInt()).isZero();
        assertThat(main.get("snapshotsChecked").asInt()).isEqualTo(2);
        assertThat(main.get("headRecordCount").asLong())
                .isEqualTo(main.get("headRowsInDataFiles").asLong());
    }

    @Test
    void aLongHistoryIsSampledFromEndToEnd() {
        assertThat(ConsistencyCheck.sample(1, 5)).containsExactly(1L, 2L, 3L, 4L, 5L);
        List<Long> ids = ConsistencyCheck.sample(1, 720);
        assertThat(ids)
                .hasSize(20)
                .startsWith(1L)
                .endsWith(720L)
                .isSorted()
                .doesNotHaveDuplicates();
    }

    @Test
    void onlyAdminsMayAsk() throws Exception {
        tableWithTwoCommits();
        assertThat(http("GET", "/colvenna/admin/v1/consistency/db/t", null, null).statusCode())
                .isEqualTo(401);
        String writer = issueToken("etl-writer", "P1D").get("token").asText();
        assertThat(http("GET", "/colvenna/admin/v1/consistency/db/t", writer, null).statusCode())
                .isEqualTo(403);
    }

    private static void deleteTree(Path root) throws Exception {
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.sorted(Comparator.reverseOrder()).toList()) {
                Files.delete(p);
            }
        }
    }

    private Path tableWithTwoCommits() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.createDatabase("db", false);
            Tables.createAppendTable(catalog, ID, Map.of());
            FileStoreTable table = (FileStoreTable) catalog.getTable(ID);
            Tables.write(table, 1);
            Tables.write(table, 2);
            return Path.of(java.net.URI.create(table.location().toUri().toString()));
        }
    }

    private JsonNode report() throws Exception {
        HttpResponse<String> response =
                http("GET", "/colvenna/admin/v1/consistency/db/t", adminToken, null);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body());
    }
}
