package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.net.http.HttpResponse;
import java.sql.PreparedStatement;
import java.util.HashMap;
import java.util.Map;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.Test;

/**
 * The catalog's admin repairs: reconciling a table from its files after a foreign writer or a
 * metadata restore, and unregistering a table without touching its files.
 */
class ReconcileTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("db", "t");

    @Test
    void reconcilingAfterAForeignWriterLiftsTheFenceAndPublishesItsSnapshots() throws Exception {
        Map<String, String> options = new HashMap<>();
        options.put("commit.min-retry-wait", "10 ms");
        options.put("commit.max-retry-wait", "50 ms");
        options.put("commit.max-retries", "2");
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, TABLE, options);
            Tables.write(catalog.getTable(TABLE), 1);
        }
        // A writer still on a file-system (or JDBC) catalog commits to the same directory.
        try (Catalog foreign = fileSystemCatalog()) {
            Tables.write(foreign.getTable(TABLE), 2);
        }
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThatThrownBy(() -> Tables.write(catalog.getTable(TABLE), 3))
                    .hasStackTraceContaining("fenced");
        }
        JsonNode report = reconcile();
        assertThat(report.get("stateBefore").asText()).isEqualTo("FENCED");
        assertThat(report.get("branches").get(0).get("before").asLong()).isEqualTo(1);
        assertThat(report.get("branches").get(0).get("after").asLong()).isEqualTo(2);
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.write(catalog.getTable(TABLE), 3);
            assertThat(Tables.ids(catalog.getTable(TABLE))).containsExactlyInAnyOrder(1, 2, 3);
        }
        assertThat(consistency().get("consistent").asBoolean()).isTrue();
    }

    @Test
    void reconcilingAfterARestoreCatchesUpWithTheFiles() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, TABLE, Map.of());
            for (int i = 1; i <= 3; i++) {
                Tables.write(catalog.getTable(TABLE), i);
            }
        }
        // A metadata backup taken after the first commit is restored.
        FileStoreTable table;
        try (RESTCatalog catalog = catalog(adminToken)) {
            table = (FileStoreTable) catalog.getTable(TABLE);
        }
        Snapshot first = table.snapshotManager().snapshot(1);
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement rewind =
                        c.prepareStatement(
                                "UPDATE table_branches SET snapshot_id = ?, snapshot_uuid = ?,"
                                        + " snapshot_json = ?")) {
            rewind.setLong(1, 1);
            rewind.setString(2, first.uuid());
            rewind.setString(3, first.toJson());
            rewind.executeUpdate();
            c.commit();
        }
        assertThat(consistency().get("problems").toString())
                .contains("beyond the published pointer");
        reconcile();
        assertThat(consistency().get("consistent").asBoolean()).isTrue();
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThat(catalog.loadSnapshot(TABLE).get().snapshot().id()).isEqualTo(3);
            assertThat(Tables.ids(catalog.getTable(TABLE))).containsExactlyInAnyOrder(1, 2, 3);
        }
    }

    @Test
    void unregisteringKeepsTheFilesForAnotherCatalog() throws Exception {
        String location;
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, TABLE, Map.of());
            Tables.write(catalog.getTable(TABLE), 1, 2);
            location = ((FileStoreTable) catalog.getTable(TABLE)).location().toString();
        }
        HttpResponse<String> unregistered =
                http("POST", "/colvenna/admin/v1/tables/db/t/unregister", adminToken, null);
        assertThat(unregistered.statusCode()).as(unregistered.body()).isEqualTo(200);
        try (RESTCatalog catalog = catalog(adminToken)) {
            assertThat(catalog.listTables("db")).isEmpty();
        }
        // The rollback target sees every snapshot, the catalog's own included.
        try (Catalog fs = fileSystemCatalog()) {
            assertThat(Tables.ids(fs.getTable(TABLE))).containsExactlyInAnyOrder(1, 2);
        }
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.registerTable(TABLE, location);
            assertThat(Tables.ids(catalog.getTable(TABLE))).containsExactlyInAnyOrder(1, 2);
        }
    }

    @Test
    void onlyAdminsMayRepair() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, TABLE, Map.of());
        }
        String service = issueToken("etl-writer", "P1D").get("token").asText();
        assertThat(
                        http("POST", "/colvenna/admin/v1/tables/db/t/reconcile", service, null)
                                .statusCode())
                .isEqualTo(403);
        assertThat(
                        http("POST", "/colvenna/admin/v1/tables/db/t/unregister", null, null)
                                .statusCode())
                .isEqualTo(401);
    }

    private JsonNode reconcile() throws Exception {
        HttpResponse<String> response =
                http("POST", "/colvenna/admin/v1/tables/db/t/reconcile", adminToken, null);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body());
    }

    private JsonNode consistency() throws Exception {
        return RESTApi.OBJECT_MAPPER.readTree(
                http("GET", "/colvenna/admin/v1/consistency/db/t", adminToken, null).body());
    }

    private Catalog fileSystemCatalog() {
        Options options = new Options();
        options.set(CatalogOptions.WAREHOUSE, warehouse);
        return CatalogFactory.createCatalog(CatalogContext.create(options));
    }
}
