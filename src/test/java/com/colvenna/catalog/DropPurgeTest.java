package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.catalog.FilePurges;
import com.colvenna.catalog.catalog.TablePurges;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.Test;

/**
 * Dropping a table and purging its files (#69): the table leaves the catalog at once, its root is
 * recorded, and the files go afterwards, by the request itself (inline, the EKS form) or by a sweep
 * (deferred, the serverless form); a failed purge stays recorded and is tried again; a root still
 * to be purged is taken by no new table. This class's server defers, as the serverless form does.
 */
class DropPurgeTest extends ServerTestBase {

    private static final Identifier T = Identifier.create("db", "t");

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_DROP_PURGE", "deferred");
    }

    @Test
    void aDeferredDropIsDoneAtOnceAndLeavesTheFilesToTheSweep() throws Exception {
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            assertThatThrownBy(() -> catalog.getTable(T))
                    .isInstanceOf(Catalog.TableNotExistException.class);
        }
        assertThat(root).exists();
        JsonNode purges = purges();
        assertThat(purges).hasSize(1);
        JsonNode record = purges.get(0);
        assertThat(record.get("table").asText()).isEqualTo("db.t");
        assertThat(Path.of(java.net.URI.create(record.get("path").asText()))).isEqualTo(root);
        assertThat(record.get("droppedBy").asText()).isEqualTo("admin");
        assertThat(record.get("attempts").asInt()).isZero();
        assertThat(record.get("lastError").isNull()).isTrue();

        assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        assertThat(root).doesNotExist();
        assertThat(purges()).isEmpty();
        // Idempotent: nothing is left to do.
        assertSweep(server.catalog().purges().sweep(null), 0, 0, 0);
    }

    @Test
    void anInlineDropDeletesTheFilesAsBeforeAndKeepsNoRecord() throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            Path root = tableWithCommits(catalog, T);
            catalog.dropTable(T, false);
            assertThat(root).doesNotExist();
            assertThat(purges()).isEmpty();
            assertThat(inline.metrics().render())
                    .contains("colvenna_catalog_table_purges_total{outcome=\"purged\"} 1");
        } finally {
            inline.close();
        }
    }

    @Test
    void aFailedPurgeIsKeptWithItsErrorAndTriedAgain() throws Exception {
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
        }
        Path data = dataDirectory(root);
        Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("r-xr-xr-x"));
        try {
            // Tried once in a sweep, not again in the same sweep.
            assertSweep(server.catalog().purges().sweep(null), 0, 1, 1);
            JsonNode record = purges().get(0);
            assertThat(record.get("attempts").asInt()).isEqualTo(1);
            assertThat(record.get("lastError").asText()).isNotBlank();
            assertThat(record.get("lastAttemptAt").asText()).isNotBlank();
        } finally {
            Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("rwxr-xr-x"));
        }
        assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        assertThat(root).doesNotExist();
    }

    @Test
    void anInlinePurgeThatFailsLeavesTheDropDoneAndTheRecordForTheSweep() throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        Path data;
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            Path root = tableWithCommits(catalog, T);
            data = dataDirectory(root);
            Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("r-xr-xr-x"));
            try {
                catalog.dropTable(T, false);
                assertThatThrownBy(() -> catalog.getTable(T))
                        .isInstanceOf(Catalog.TableNotExistException.class);
                assertThat(purges()).hasSize(1);
                assertThat(purges().get(0).get("attempts").asInt()).isEqualTo(1);
                assertThat(inline.metrics().render())
                        .contains("colvenna_catalog_table_purges_total{outcome=\"failed\"} 1");
            } finally {
                Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("rwxr-xr-x"));
            }
            assertSweep(inline.catalog().purges().sweep(null), 1, 0, 0);
            assertThat(root).doesNotExist();
        } finally {
            inline.close();
        }
    }

    @Test
    void aRecreatedTableTakesAnotherRootWhileTheOldOneWaits() throws Exception {
        Path old = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            Tables.createAppendTable(catalog, T, Map.of());
            FileStoreTable again = (FileStoreTable) catalog.getTable(T);
            Path root = Path.of(java.net.URI.create(again.location().toUri().toString()));
            assertThat(root).isNotEqualTo(old);
            assertThat(root.getFileName().toString()).startsWith("t-");
            Tables.write(again, 7);
            assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
            assertThat(old).doesNotExist();
            assertThat(Tables.ids(catalog.getTable(T))).containsExactly(7);
        }
    }

    @Test
    void aRootStillToBePurgedCannotBeRegistered() throws Exception {
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            assertThatThrownBy(() -> catalog.registerTable(T, root.toUri().toString()))
                    .hasMessageContaining("still to be deleted");
        }
        assertThat(root).exists();
    }

    @Test
    void aRecordARestoreBringsBackSparesATableMadeAtItsRootSince() throws Exception {
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
        }
        List<String> record =
                queryColumn(
                        "SELECT table_id || ',' || table_name || ',' || path || ',' || dropped_at"
                                + " FROM table_purges");
        assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        // Made again at the same root once the purge is done, then lost to the catalog, as a
        // table made after a metadata restore's point is: its files are there, its row is not.
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, T, Map.of());
            Tables.write(catalog.getTable(T), 9);
        }
        assertThat(root.resolve("schema")).exists();
        assertThat(
                        http("POST", "/colvenna/admin/v1/tables/db/t/unregister", adminToken, null)
                                .statusCode())
                .isEqualTo(200);
        // The restore brings back the record of the first drop, a minute before the table made
        // again
        // (deferred, a purge runs minutes after its drop; within the clocks' allowance it would not
        // count as newer).
        String[] old = record.get(0).split(",", 4);
        try (Connection c = server.db().dataSource().getConnection();
                PreparedStatement insert =
                        c.prepareStatement(
                                "INSERT INTO table_purges (table_id, table_name, path, dropped_at,"
                                        + " dropped_by, not_before) VALUES (?::uuid, ?, ?,"
                                        + " ?::timestamptz - interval '1 minute', 'admin', ?)")) {
            insert.setString(1, old[0]);
            insert.setString(2, old[1]);
            insert.setString(3, old[2]);
            insert.setString(4, old[3]);
            insert.setTimestamp(5, Timestamp.from(clock.instant()));
            insert.executeUpdate();
            c.commit();
        }
        TablePurges.Sweep sweep = server.catalog().purges().sweep(null);
        assertThat(sweep.skipped()).as(sweep.toString()).isEqualTo(1);
        assertSweep(sweep, 0, 0, 0);
        assertThat(root.resolve("schema")).exists();
        assertThat(server.metrics().render())
                .contains("colvenna_catalog_table_purges_total{outcome=\"skipped\"} 1");
    }

    @Test
    void aClaimedRecordWaitsForItsLease() throws Exception {
        tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
        }
        // As a sweep that claimed it and then stopped would leave it.
        try (Connection c = server.db().dataSource().getConnection();
                PreparedStatement claim =
                        c.prepareStatement("UPDATE table_purges SET not_before = ?")) {
            claim.setTimestamp(1, Timestamp.from(clock.instant().plus(TablePurges.LEASE)));
            assertThat(claim.executeUpdate()).isEqualTo(1);
            c.commit();
        }
        assertSweep(server.catalog().purges().sweep(null), 0, 0, 1);
        clock.advance(TablePurges.LEASE.plusSeconds(1));
        assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
    }

    @Test
    void thePurgeCommandSweepsUntilItsDeadlineAndExitsOneIfAPurgeFailed() throws Exception {
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
        }
        ServerConfig config =
                TestServers.withSettings(warehouse, externalLocationRoots(), settings());
        // A deadline closer than the margin: nothing is claimed.
        String soon = String.valueOf(clock.millis() + Duration.ofMinutes(1).toMillis());
        assertThat(Main.purge(config, new String[] {"purge", "--until", soon}, clock)).isZero();
        assertThat(root).exists();
        Path data = dataDirectory(root);
        Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("r-xr-xr-x"));
        try {
            assertThat(Main.purge(config, new String[] {"purge"}, clock)).isEqualTo(1);
        } finally {
            Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("rwxr-xr-x"));
        }
        String later = String.valueOf(Instant.now().plus(Duration.ofMinutes(10)).toEpochMilli());
        assertThat(Main.purge(config, new String[] {"purge", "--until", later}, clock)).isZero();
        assertThat(root).doesNotExist();
        assertThat(Main.purge(config, new String[] {"purge", "--until"}, clock)).isEqualTo(2);
    }

    @Test
    void aPurgeRunsSummaryIsAMetricOfCloudWatchsEmbeddedFormat() throws Exception {
        JsonNode line =
                RESTApi.OBJECT_MAPPER.readTree(
                        Main.summary(
                                new TablePurges.Sweep(2, 1, 0, 3, 4000),
                                new FilePurges.Sweep(0, 0, 0, 0, 0),
                                3,
                                "fx",
                                1000L));
        JsonNode directive = line.get("_aws").get("CloudWatchMetrics").get(0);
        assertThat(line.get("_aws").get("Timestamp").asLong()).isEqualTo(1000L);
        assertThat(directive.get("Namespace").asText()).isEqualTo("ColvennaCatalog");
        assertThat(directive.get("Dimensions").toString()).isEqualTo("[[\"Environment\"]]");
        assertThat(directive.get("Metrics").findValuesAsText("Name"))
                .containsExactly("TablePurgeOldestSeconds");
        assertThat(line.get("Environment").asText()).isEqualTo("fx");
        assertThat(line.get("TablePurgeOldestSeconds").asLong()).isEqualTo(4000);
        assertThat(
                        List.of(
                                line.get("purged").asInt(),
                                line.get("failed").asInt(),
                                line.get("pending").asInt()))
                .containsExactly(2, 1, 3);
    }

    @Test
    void onlyAdminsListPurges() throws Exception {
        assertThat(http("GET", "/colvenna/admin/v1/purges", null, null).statusCode())
                .isEqualTo(401);
        String writer = issueToken("etl-writer", "P1D").get("token").asText();
        assertThat(http("GET", "/colvenna/admin/v1/purges", writer, null).statusCode())
                .isEqualTo(403);
    }

    @Test
    void aDroppedDatabaseLeavesNoDirectoryOnceItsTablesArePurged() throws Exception {
        // Deferred, as in the serverless form: the drops touch no file, the sweeps purge (#13).
        Identifier one = Identifier.create("d", "one");
        Identifier two = Identifier.create("d", "two");
        Path directory = warehouseDir.resolve("d.db");
        tableWithCommits(one);
        Path second = tableWithCommits(two);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(one, false);
            assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
            // The other table's root is still there, and so the directory.
            assertThat(directory).isDirectory();
            assertThat(second).isDirectory();
            catalog.dropDatabase("d", false, true);
            assertThat(catalog.listDatabases()).doesNotContain("d");
            assertThat(second).isDirectory();
            assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        }
        assertThat(directory).doesNotExist();
    }

    @Test
    void aDatabaseWhoseLastTableIsPurgedKeepsNoDirectory() throws Exception {
        Path directory = warehouseDir.resolve("db.db");
        tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
            // The database stays, with no directory until it has a table again.
            assertThat(catalog.listDatabases()).contains("db");
            assertThat(directory).doesNotExist();
            Path again = tableWithCommits(catalog, T);
            assertThat(again).isDirectory();
            catalog.dropTable(T, false);
            catalog.dropDatabase("db", false, false);
            assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        }
        assertThat(directory).doesNotExist();
    }

    @Test
    void anInlineDatabaseDropLeavesNoDirectory() throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            tableWithCommits(catalog, Identifier.create("d", "one"));
            tableWithCommits(catalog, Identifier.create("d", "two"));
            catalog.dropDatabase("d", false, true);
            assertThat(warehouseDir.resolve("d.db")).doesNotExist();
            assertThat(purges()).isEmpty();
            // A database that never had a table has no directory, and its drop needs none.
            catalog.createDatabase("empty", false);
            catalog.dropDatabase("empty", false, false);
            assertThat(warehouseDir.resolve("empty.db")).doesNotExist();
        } finally {
            inline.close();
        }
    }

    @Test
    void aDatabaseDirectoryWithAnythingLeftInItStays() throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            // A table renamed into another database keeps its root, in the first one's directory.
            Identifier moved = Identifier.create("b", "t");
            Path root = tableWithCommits(catalog, Identifier.create("a", "t"));
            catalog.createDatabase("b", false);
            catalog.renameTable(Identifier.create("a", "t"), moved, false);
            // And a file the catalog does not know of.
            Path stray = Files.writeString(warehouseDir.resolve("a.db").resolve("notes"), "x");
            catalog.dropDatabase("a", false, true);
            assertThat(root).isDirectory();
            assertThat(stray).exists();
            assertThat(Tables.ids(catalog.getTable(moved))).containsExactlyInAnyOrder(1, 2);
            Files.delete(stray);
            catalog.dropTable(moved, false);
            // Its purge leaves the directory that held its root empty, and so removes it.
            assertThat(warehouseDir.resolve("a.db")).doesNotExist();
        } finally {
            inline.close();
        }
    }

    @Test
    void aDatabaseDirectoryThatCannotBeRemovedKeepsThePurgeRecord() throws Exception {
        Path directory = warehouseDir.resolve("db.db");
        Path root = tableWithCommits(T);
        try (RESTCatalog catalog = catalog(adminToken)) {
            catalog.dropTable(T, false);
            catalog.dropDatabase("db", false, false);
        }
        // The table's root can go, its database's directory not: the warehouse is read-only.
        Files.setPosixFilePermissions(warehouseDir, PosixFilePermissions.fromString("r-xr-xr-x"));
        try {
            assertSweep(server.catalog().purges().sweep(null), 0, 1, 1);
            assertThat(root).doesNotExist();
            assertThat(directory).isDirectory();
            assertThat(purges().get(0).get("lastError").asText()).contains("db.db");
        } finally {
            Files.setPosixFilePermissions(
                    warehouseDir, PosixFilePermissions.fromString("rwxr-xr-x"));
        }
        // Tried again, the root already gone.
        assertSweep(server.catalog().purges().sweep(null), 1, 0, 0);
        assertThat(directory).doesNotExist();
    }

    /** What a sweep purged and failed, and the records it left. */
    private static void assertSweep(TablePurges.Sweep sweep, int purged, int failed, int pending) {
        assertThat(List.of(sweep.purged(), sweep.failed(), sweep.pending()))
                .as(sweep.toString())
                .containsExactly(purged, failed, pending);
    }

    private ColvennaCatalogServer inlineReplica() throws Exception {
        ColvennaCatalogServer inline =
                ColvennaCatalogServer.create(
                        TestServers.withSettings(warehouse, externalLocationRoots(), Map.of()),
                        clock,
                        dataTokens());
        inline.start();
        return inline;
    }

    private JsonNode purges() throws Exception {
        HttpResponse<String> response = http("GET", "/colvenna/admin/v1/purges", adminToken, null);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body()).get("purges");
    }

    private Path tableWithCommits(Identifier id) throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            return tableWithCommits(catalog, id);
        }
    }

    private static Path tableWithCommits(RESTCatalog catalog, Identifier id) throws Exception {
        Tables.createAppendTable(catalog, id, Map.of());
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        Tables.write(table, 1);
        Tables.write(table, 2);
        return Path.of(java.net.URI.create(table.location().toUri().toString()));
    }

    /** A directory of the table's data files, which a purge cannot empty once it is read-only. */
    private static Path dataDirectory(Path root) throws Exception {
        try (var dirs = Files.list(root)) {
            return dirs.filter(p -> p.getFileName().toString().startsWith("bucket-"))
                    .findFirst()
                    .orElseThrow();
        }
    }
}
