package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.catalog.CatalogStore;
import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.FilePurges;
import com.colvenna.catalog.catalog.TablePurges;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.metrics.CatalogMetrics;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.net.URI;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryRow;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.disk.IOManager;
import org.apache.paimon.index.IndexFileHandler;
import org.apache.paimon.manifest.IndexManifestEntry;
import org.apache.paimon.manifest.ManifestEntry;
import org.apache.paimon.operation.LocalOrphanFilesClean;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.requests.RollbackTableRequest;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Instant;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.table.sink.CommitMessage;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;
import org.apache.paimon.table.source.ScanMode;
import org.apache.paimon.table.source.Split;
import org.junit.jupiter.api.Test;

/**
 * Rollbacks, tag deletions and branch deletions leave their files to a purge (#93): each request
 * answers once its change is made and its removed snapshots, tags and changelogs are recorded; the
 * files they referenced go afterwards, by the request itself (inline, the EKS form) or by a sweep
 * (deferred, the serverless form), and only those that nothing references then. This class's server
 * defers, as the serverless form does.
 *
 * <p>Two oracles of Paimon's own judge the result: every remaining snapshot, tag and branch is read
 * in full, so a referenced file deleted fails the read; and Paimon's orphan file cleaning, as a dry
 * run, lists what no root references, which must be nothing once the purge is done.
 */
class FilePurgeTest extends ServerTestBase {

    private static final Identifier T = Identifier.create("db", "t");

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_DROP_PURGE", "deferred");
    }

    @Test
    void aRollbackAnswersOnceItsSnapshotsAreGoneAndLeavesTheirFilesToTheSweep() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 6);
            Set<Path> before = dataFiles(root);
            assertThat(before).hasSize(6);

            catalog.rollbackTo(T, Instant.snapshot(3L));

            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 3);
            assertThat(snapshotFiles(root))
                    .containsExactly("snapshot-1", "snapshot-2", "snapshot-3");
            assertThat(dataFiles(root)).isEqualTo(before);
            JsonNode record = filePurges().get(0);
            assertThat(record.get("operation").asText()).isEqualTo("ROLLBACK");
            assertThat(record.get("target").asText()).isEqualTo("snapshot:3");
            assertThat(record.get("branch").asText()).isEqualTo("main");
            assertThat(record.get("table").asText()).isEqualTo("db.t");
            assertThat(record.get("applied").asBoolean()).isTrue();
            assertThat(record.get("roots").asInt()).isEqualTo(3);
            assertThat(orphans(catalog, T)).isNotEmpty();
            // A writer restoring from a checkpoint may commit them again for a while.
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 1);
            clock.advance(FilePurges.RECOMMIT_GRACE);

            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(filePurges()).isEmpty();
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(dataFiles(root)).hasSize(3);
            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1", List.of(1),
                                    "main snapshot-2", List.of(1, 2),
                                    "main snapshot-3", List.of(1, 2, 3)));
            // Idempotent: nothing is left to do.
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 0);
        }
    }

    @Test
    void onEksARollbackAnswersAsPaimonsDoesAndItsReplicasSweepTheFilesAfterTheGrace()
            throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            Path root = tableWithCommits(catalog, T, 5);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            // The snapshots are gone, the data files wait, as with Paimon's own rollback.
            assertThat(snapshotFiles(root)).containsExactly("snapshot-1", "snapshot-2");
            assertThat(dataFiles(root)).hasSize(5);
            assertThat(filePurges()).hasSize(1);
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(inline.catalog().filePurges().sweep(null), 1, 0, 0);
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1",
                                    List.of(1),
                                    "main snapshot-2",
                                    List.of(1, 2)));
            assertThat(inline.metrics().render())
                    .contains("colvenna_catalog_file_purges_total{outcome=\"purged\"} 1");
        } finally {
            inline.close();
        }
    }

    @Test
    void onEksATagsDeletionDeletesItsFilesBeforeItAnswersAndKeepsNoRecord() throws Exception {
        ColvennaCatalogServer inline = inlineReplica();
        try (RESTCatalog catalog = catalog(adminToken, inline.port(), Map.of())) {
            Path root = tableWithExpiredTag(catalog, T, false);
            catalog.deleteTag(T, "t2");
            assertThat(dataFiles(root)).hasSize(1);
            assertThat(filePurges()).isEmpty();
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(everyRoot(catalog, T)).isEqualTo(Map.of("main snapshot-4", List.of(4)));
        } finally {
            inline.close();
        }
    }

    @Test
    void aBranchMadeFromATagTheRollbackRemovesKeepsItsFiles() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 6);
            catalog.createTag(T, "t5", 5L, null, false);
            catalog.createBranch(T, "b", "t5");

            catalog.rollbackTo(T, Instant.snapshot(2L));
            // As Paimon's own rollback: the tags after the target go with the snapshots.
            assertThat(((FileStoreTable) catalog.getTable(T)).tagManager().tagExists("t5"))
                    .isFalse();
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1", List.of(1),
                                    "main snapshot-2", List.of(1, 2),
                                    "b snapshot-5", List.of(1, 2, 3, 4, 5),
                                    "b tag-t5", List.of(1, 2, 3, 4, 5)));
            // The sixth file only the removed snapshot 6 had is gone.
            assertThat(dataFiles(root)).hasSize(5);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aTagsDeletionAnswersAtOnceAndLeavesTheFilesOnlyItHeldToTheSweep() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithExpiredTag(catalog, T, false);
            assertThat(dataFiles(root)).hasSize(2);

            catalog.deleteTag(T, "t2");

            assertThat(((FileStoreTable) catalog.getTable(T)).tagManager().tagExists("t2"))
                    .isFalse();
            assertThat(dataFiles(root)).hasSize(2);
            JsonNode record = filePurges().get(0);
            assertThat(record.get("operation").asText()).isEqualTo("DROP_TAG");
            assertThat(record.get("target").asText()).isEqualTo("tag:t2");
            assertThat(record.get("roots").asInt()).isEqualTo(1);

            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(dataFiles(root)).hasSize(1);
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(everyRoot(catalog, T)).isEqualTo(Map.of("main snapshot-4", List.of(4)));
        }
    }

    @Test
    void aBranchMadeFromADeletedTagKeepsItsFiles() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithExpiredTag(catalog, T, true);

            catalog.deleteTag(T, "t2");
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            // Paimon's own tag deletion looks at the tag's branch only, and would delete them.
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-4", List.of(4),
                                    "b snapshot-2", List.of(2),
                                    "b tag-t2", List.of(2)));
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aTagWithASnapshotStillThereGoesAloneAndRecordsNothing() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 3);
            catalog.createTag(T, "t2", 2L, null, false);
            catalog.deleteTag(T, "t2");
            assertThat(filePurges()).isEmpty();
            assertThat(dataFiles(root)).hasSize(3);
            assertThat(everyRoot(catalog, T)).hasSize(3);
        }
    }

    @Test
    void aTagWrittenAfterTheRollbackKeepsWhatItReferences() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 6);
            FileStoreTable table = (FileStoreTable) catalog.getTable(T);
            // Read before the rollback, as a procedure making a tag would have.
            Snapshot five = table.snapshotManager().snapshot(5);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            // Written straight to the files after the rollback recorded its roots.
            table.tagManager().createTag(five, "late", null, List.of(), false);

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1", List.of(1),
                                    "main snapshot-2", List.of(1, 2),
                                    "main tag-late", List.of(1, 2, 3, 4, 5)));
            assertThat(dataFiles(root)).hasSize(5);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aTagMadeWhileThePurgeRunsKeepsWhatItReferences() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 6);
            FileStoreTable table = (FileStoreTable) catalog.getTable(T);
            Snapshot five = table.snapshotManager().snapshot(5);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            AtomicBoolean made = new AtomicBoolean();
            // After the purge has read every root, before it deletes anything.
            FilePurges purges =
                    new FilePurges(
                            server.db(),
                            new CatalogStore(),
                            new TableFiles(
                                    TestServers.withSettings(
                                                    warehouse, externalLocationRoots(), settings())
                                            .fileIoOptions()),
                            clock,
                            new CatalogMetrics(),
                            new FilePurges.Hooks() {
                                @Override
                                public void beforeBatch(FilePurges.Pending record) {
                                    if (made.compareAndSet(false, true)) {
                                        table.tagManager()
                                                .createTag(five, "during", null, List.of(), false);
                                    }
                                }
                            });

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(purges.sweep(null), 1, 0, 0);

            assertThat(made).isTrue();
            assertThat(everyRoot(catalog, T))
                    .containsEntry("main tag-during", List.of(1, 2, 3, 4, 5))
                    .hasSize(3);
            assertThat(dataFiles(root)).hasSize(5);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aBranchsDeletionAnswersOnceItIsGoneAndLeavesTheFilesOnlyItHadToTheSweep()
            throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 3);
            catalog.createTag(T, "t3", 3L, null, false);
            catalog.createBranch(T, "b", "t3");
            Identifier b = new Identifier("db", "t", "b");
            Tables.write(catalog.getTable(b), 10);
            Tables.write(catalog.getTable(b), 11);
            assertThat(dataFiles(root)).hasSize(5);

            catalog.dropBranch(T, "b");

            assertThat(catalog.listBranches(T)).isEmpty();
            assertThat(root.resolve("branch").resolve("branch-b")).doesNotExist();
            assertThat(dataFiles(root)).hasSize(5);
            JsonNode record = filePurges().get(0);
            assertThat(record.get("operation").asText()).isEqualTo("DROP_BRANCH");
            assertThat(record.get("target").asText()).isEqualTo("branch:b");
            assertThat(record.get("branch").asText()).isEqualTo("b");

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            // Paimon's own branch deletion leaves the branch's own files behind for good.
            assertThat(dataFiles(root)).hasSize(3);
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1", List.of(1),
                                    "main snapshot-2", List.of(1, 2),
                                    "main snapshot-3", List.of(1, 2, 3),
                                    "main tag-t3", List.of(1, 2, 3)));
            // A branch of the same name made again starts afresh.
            catalog.createBranch(T, "b", "t3");
            assertThat(everyRoot(catalog, T))
                    .containsEntry("b snapshot-3", List.of(1, 2, 3))
                    .hasSize(6);
        }
    }

    @Test
    void aRollbackOfABranchLeavesMainAlone() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 2);
            catalog.createTag(T, "t2", 2L, null, false);
            catalog.createBranch(T, "b", "t2");
            Identifier b = new Identifier("db", "t", "b");
            Tables.write(catalog.getTable(b), 10);
            Tables.write(catalog.getTable(b), 11);

            catalog.rollbackTo(b, Instant.snapshot(3L));
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(everyRoot(catalog, T))
                    .isEqualTo(
                            Map.of(
                                    "main snapshot-1", List.of(1),
                                    "main snapshot-2", List.of(1, 2),
                                    "main tag-t2", List.of(1, 2),
                                    "b snapshot-2", List.of(1, 2),
                                    "b snapshot-3", List.of(1, 2, 10),
                                    "b tag-t2", List.of(1, 2)));
            assertThat(dataFiles(root)).hasSize(3);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aRollbackWhoseTimeRunsOutWhileRecordingAnswers503AndGoesOnWhenSentAgain()
            throws Exception {
        ColvennaCatalogServer slow =
                replica(
                        Map.of(
                                "COLVENNA_DROP_PURGE",
                                "deferred",
                                "COLVENNA_REQUEST_WORK_TIME",
                                "PT0.001S"));
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 40);
            String body = RESTApi.toJson(new RollbackTableRequest(Instant.snapshot(2L), null));
            String path = "/v1/colvenna/databases/db/tables/t/rollback";

            HttpResponse<String> first = http(slow.port(), "POST", path, adminToken, body);
            assertThat(first.statusCode()).as(first.body()).isEqualTo(503);
            assertThat(first.body()).contains("send it again");
            // Nothing changed but the record: every snapshot is there.
            assertThat(snapshotFiles(root)).hasSize(40);
            assertThat(Tables.ids(catalog.getTable(T))).hasSize(40);

            HttpResponse<String> again = http(slow.port(), "POST", path, adminToken, body);
            assertThat(again.statusCode()).as(again.body()).isEqualTo(200);
            assertThat(snapshotFiles(root)).containsExactly("snapshot-1", "snapshot-2");
            JsonNode records = filePurges();
            assertThat(records).hasSize(1);
            assertThat(records.get(0).get("roots").asInt()).isEqualTo(38);

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(orphans(catalog, T)).isEmpty();
        } finally {
            slow.close();
        }
    }

    @Test
    void anUnguardedRollbackSentAgainAfterCommitsIsNotAppliedTwice() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            // A commit acknowledged after the rollback took effect.
            Tables.write(catalog.getTable(T), 7);
            String body = RESTApi.toJson(new RollbackTableRequest(Instant.snapshot(2L), null));
            String path = "/v1/colvenna/databases/db/tables/t/rollback";

            // The same rollback again, as a client whose answer was lost sends it.
            HttpResponse<String> again = http("POST", path, adminToken, body);
            assertThat(again.statusCode()).as(again.body()).isEqualTo(409);
            assertThat(again.body()).contains("not applied again");
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 7);

            // Nothing new since: sent again, it changes nothing either way.
            HttpResponse<String> guarded =
                    http(
                            "POST",
                            path,
                            adminToken,
                            RESTApi.toJson(new RollbackTableRequest(Instant.snapshot(2L), 3L)));
            assertThat(guarded.statusCode()).as(guarded.body()).isEqualTo(200);
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);

            // Past the window, the same rollback is a new one.
            Tables.write(catalog.getTable(T), 8);
            clock.advance(CommitService.ROLLBACK_RETRY_WINDOW.plusSeconds(1));
            catalog.rollbackTo(T, Instant.snapshot(2L));
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);
        }
    }

    @Test
    void anotherCallersRollbackIsItsOwn() throws Exception {
        String other = issueToken("operator", "P1D").get("token").asText();
        TestServers.grantCatalog(server, "operator");
        try (RESTCatalog catalog = catalog(adminToken);
                RESTCatalog operator = catalog(other)) {
            tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            Tables.write(catalog.getTable(T), 7);
            operator.rollbackTo(T, Instant.snapshot(2L));
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);
        }
    }

    @Test
    void aGuardedRollbackSentAgainAnswersAsItDid() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L), 4L);
            // Its retry, the answer lost: applied once, answered as it was.
            catalog.rollbackTo(T, Instant.snapshot(2L), 4L);
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);
            // Commits since: taken for its retry still, and not applied again.
            Tables.write(catalog.getTable(T), 7);
            assertThatThrownBy(() -> catalog.rollbackTo(T, Instant.snapshot(2L), 4L))
                    .hasMessageContaining("not applied again");
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 7);
            // A guard that does not hold, and no retry: refused as before.
            assertThatThrownBy(() -> catalog.rollbackTo(T, Instant.snapshot(1L), 4L))
                    .hasMessageContaining("Latest snapshot 3 is not 4");
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 7);
        }
    }

    @Test
    void aGuardedRollbackSentAgainAfterACommitBroughtItsGuardBackIsNotAppliedTwice()
            throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            tableWithCommits(catalog, T, 3);
            // As Paimon's committer rolls back a compaction: to latest - 1, from latest.
            catalog.rollbackTo(T, Instant.snapshot(2L), 3L);
            // Another writer's commit takes snapshot 3 again, and is acknowledged.
            Tables.write(catalog.getTable(T), 9);
            // The first rollback's retry finds its guard holding again.
            assertThatThrownBy(() -> catalog.rollbackTo(T, Instant.snapshot(2L), 3L))
                    .hasMessageContaining("not applied again");
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 9);
        }
    }

    @Test
    void aRollbackRecordedOnceAndAppliedByALaterRequestWaitsForTheLatersGrace() throws Exception {
        ColvennaCatalogServer slow =
                replica(
                        Map.of(
                                "COLVENNA_DROP_PURGE",
                                "deferred",
                                "COLVENNA_REQUEST_WORK_TIME",
                                "PT0.001S"));
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 40);
            String body = RESTApi.toJson(new RollbackTableRequest(Instant.snapshot(2L), null));
            String path = "/v1/colvenna/databases/db/tables/t/rollback";
            assertThat(http(slow.port(), "POST", path, adminToken, body).statusCode())
                    .isEqualTo(503);
            // Sent again later than a retry: a record of its own, applied at once.
            clock.advance(FilePurges.REUSE.plusMinutes(1));
            catalog.rollbackTo(T, Instant.snapshot(2L));
            assertThat(filePurges()).hasSize(2);
            // The first record never took effect; it waits as long as the second's grace.
            clock.advance(TablePurges.LEASE);
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 2);
            assertThat(dataFiles(root)).hasSize(40);
            clock.advance(FilePurges.RECOMMIT_GRACE);
            FilePurges.Sweep sweep = server.catalog().filePurges().sweep(null);
            assertThat(sweep.purged()).as(sweep.toString()).isEqualTo(2);
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(orphans(catalog, T)).isEmpty();
        } finally {
            slow.close();
        }
    }

    @Test
    void aRollbackHoldsBackTheTablesOtherRecordsForItsGrace() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithExpiredTag(catalog, T, false);
            catalog.deleteTag(T, "t2");
            catalog.createTag(T, "t4", 4L, null, false);
            Tables.write(catalog.getTable(T), 5);
            // Snapshot 4 has expired: back to its tag, which becomes the snapshot again.
            catalog.rollbackTo(T, Instant.tag("t4"), 5L);
            assertThat(filePurges().get(0).get("dueAt").asText())
                    .isEqualTo(filePurges().get(1).get("dueAt").asText());
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 2);
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 2, 0, 0);
            assertThat(dataFiles(root)).hasSize(1);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aRecordWhoseRollbackNeverTookEffectDeletesNothing() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 5);
            Path snapshots = root.resolve("snapshot");
            // The rollback records its roots, then cannot change the snapshot directory.
            Files.setPosixFilePermissions(snapshots, PosixFilePermissions.fromString("r-xr-xr-x"));
            try {
                assertThatThrownBy(() -> catalog.rollbackTo(T, Instant.snapshot(2L)))
                        .isInstanceOf(RuntimeException.class);
            } finally {
                Files.setPosixFilePermissions(
                        snapshots, PosixFilePermissions.fromString("rwxr-xr-x"));
            }
            JsonNode record = filePurges().get(0);
            assertThat(record.get("applied").asBoolean()).isFalse();
            assertThat(record.get("roots").asInt()).isEqualTo(3);
            // Not due until a lease and the grace have passed: a request sent again would go on
            // with it, and it may have removed files a writer commits again.
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 1);
            clock.advance(TablePurges.LEASE.plusSeconds(1));
            assertSweep(server.catalog().filePurges().sweep(null), 0, 0, 1);

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(dataFiles(root)).hasSize(5);
            assertThat(everyRoot(catalog, T)).hasSize(5);
        }
    }

    @Test
    void aFailedPurgeIsKeptWithItsErrorAndTriedAgain() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            Path data = bucket(root);
            Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("r-xr-xr-x"));
            try {
                clock.advance(FilePurges.RECOMMIT_GRACE);
                assertSweep(server.catalog().filePurges().sweep(null), 0, 1, 1);
                JsonNode record = filePurges().get(0);
                assertThat(record.get("attempts").asInt()).isEqualTo(1);
                assertThat(record.get("lastError").asText()).isNotBlank();
                assertThat(server.metrics().render())
                        .contains("colvenna_catalog_file_purges_total{outcome=\"failed\"} 1");
            } finally {
                Files.setPosixFilePermissions(data, PosixFilePermissions.fromString("rwxr-xr-x"));
            }
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aRecordOfADroppedTableGoesWithTheTable() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            catalog.dropTable(T, false);
            clock.advance(FilePurges.RECOMMIT_GRACE);
            FilePurges.Sweep sweep = server.catalog().filePurges().sweep(null);
            assertThat(sweep.skipped()).as(sweep.toString()).isEqualTo(1);
            assertSweep(sweep, 0, 0, 0);
            // The dropped table's own purge deletes its root.
            assertThat(server.catalog().purges().sweep(null).purged()).isEqualTo(1);
            assertThat(root).doesNotExist();
        }
    }

    @Test
    void thePurgeCommandPurgesTheseFilesToo() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 4);
            catalog.rollbackTo(T, Instant.snapshot(2L));
            ServerConfig config =
                    TestServers.withSettings(warehouse, externalLocationRoots(), settings());
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertThat(Main.purge(config, new String[] {"purge"}, clock)).isZero();
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(filePurges()).isEmpty();
        }
    }

    @Test
    void aRollbackStoppedHalfWayKeepsWhatTheCatalogsPointerServes() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 5);
            Path snapshots = root.resolve("snapshot");
            Files.setPosixFilePermissions(snapshots, PosixFilePermissions.fromString("r-xr-xr-x"));
            try {
                assertThatThrownBy(() -> catalog.rollbackTo(T, Instant.snapshot(2L)))
                        .isInstanceOf(RuntimeException.class);
            } finally {
                Files.setPosixFilePermissions(
                        snapshots, PosixFilePermissions.fromString("rwxr-xr-x"));
            }
            // As a rollback stopped after deleting the newest snapshot's file, before its catalog
            // change: the pointer still serves snapshot 5, from PostgreSQL.
            Files.delete(snapshots.resolve("snapshot-5"));
            clock.advance(TablePurges.LEASE.plus(FilePurges.RECOMMIT_GRACE).plusSeconds(1));
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(dataFiles(root)).hasSize(5);
            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2, 3, 4, 5);
        }
    }

    @Test
    void filesAWriterCommitsAgainWithinTheGraceAreKept() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 1);
            StreamWriteBuilder builder =
                    catalog.getTable(T).newStreamWriteBuilder().withCommitUser("job");
            List<CommitMessage> checkpoint;
            try (StreamTableWrite write = builder.newWrite();
                    StreamTableCommit commit = builder.newCommit()) {
                write.write(GenericRow.of(2, BinaryString.fromString("v2")));
                checkpoint = write.prepareCommit(false, 1);
                commit.commit(1, checkpoint);
            }
            catalog.rollbackTo(T, Instant.snapshot(1L));
            // The job restores from its checkpoint: no snapshot holds its commit now, so Paimon's
            // committer, having found its files there, commits them again.
            try (StreamTableCommit commit =
                    catalog.getTable(T).newStreamWriteBuilder().withCommitUser("job").newCommit()) {
                assertThat(commit.filterAndCommit(Map.of(1L, checkpoint))).isEqualTo(1);
            }
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);

            assertThat(Tables.ids(catalog.getTable(T))).containsExactlyInAnyOrder(1, 2);
            assertThat(dataFiles(root)).hasSize(2);
            assertThat(orphans(catalog, T)).isEmpty();
        }
    }

    @Test
    void aKeyedTablesCompactionsDeletionVectorsAndChangelogsAreJudgedAsPaimonReadsThem()
            throws Exception {
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createKeyedTable(
                    catalog,
                    T,
                    Map.of(
                            "deletion-vectors.enabled", "true",
                            "changelog-producer", "input",
                            "num-sorted-run.compaction-trigger", "2",
                            "snapshot.num-retained.min", "1",
                            "snapshot.num-retained.max", "3",
                            "changelog.num-retained.max", "30"));
            upsert(catalog, T, 0, java.util.stream.IntStream.rangeClosed(1, 20).toArray());
            fullCompaction(catalog, T);
            // Updates of rows of the compacted file mark them deleted in it: deletion vectors.
            for (int round = 1; round <= 4; round++) {
                upsert(catalog, T, round, 1, 2, 3);
            }
            catalog.createTag(T, "t", latest(catalog, T), null, false);
            for (int round = 5; round <= 10; round++) {
                upsert(catalog, T, round, 1, 2, 3);
            }
            for (int round = 11; round <= 14; round++) {
                upsert(catalog, T, round, 2, 3, 4);
            }
            FileStoreTable table = (FileStoreTable) catalog.getTable(T);
            // What the case is about: index files, changelog files and long-lived changelogs.
            assertThat(table.changelogManager().safelyGetAllChangelogs()).isNotEmpty();
            assertThat(indexManifests(table)).isNotEmpty();
            Path root = Path.of(URI.create(table.location().toUri().toString()));

            assertThat(missingFiles(catalog, T)).isEmpty();
            // Back to the earliest snapshot there is, past compactions and deletion vectors.
            long earliest = table.snapshotManager().earliestSnapshotId();
            assertThat(latest(catalog, T) - earliest).isGreaterThan(1);
            catalog.rollbackTo(T, Instant.snapshot(earliest));
            catalog.deleteTag(T, "t");
            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 2, 0, 0);

            assertThat(missingFiles(catalog, T)).isEmpty();
            assertThat(orphans(catalog, T)).isEmpty();
            assertThat(Tables.ids(catalog.getTable(T)))
                    .containsExactlyInAnyOrderElementsOf(
                            java.util.stream.IntStream.rangeClosed(1, 20).boxed().toList());
            assertThat(everyRoot(catalog, T)).isNotEmpty();
            assertThat(root).exists();
        }
    }

    @Test
    void aBranchsDeletionWhoseTimeRunsOutWhileRecordingAnswers503AndGoesOn() throws Exception {
        ColvennaCatalogServer slow =
                replica(
                        Map.of(
                                "COLVENNA_DROP_PURGE",
                                "deferred",
                                "COLVENNA_REQUEST_WORK_TIME",
                                "PT0.001S"));
        try (RESTCatalog catalog = catalog(adminToken)) {
            Path root = tableWithCommits(catalog, T, 1);
            catalog.createTag(T, "t1", 1L, null, false);
            catalog.createBranch(T, "b", "t1");
            Identifier b = new Identifier("db", "t", "b");
            for (int i = 0; i < 40; i++) {
                Tables.write(catalog.getTable(b), 100 + i);
            }
            String path = "/v1/colvenna/databases/db/tables/t/branches/b";

            HttpResponse<String> first = http(slow.port(), "DELETE", path, adminToken, null);
            assertThat(first.statusCode()).as(first.body()).isEqualTo(503);
            assertThat(catalog.listBranches(T)).containsExactly("b");

            HttpResponse<String> again = http(slow.port(), "DELETE", path, adminToken, null);
            assertThat(again.statusCode()).as(again.body()).isEqualTo(200);
            assertThat(catalog.listBranches(T)).isEmpty();
            assertThat(filePurges()).hasSize(1);
            // Every root once: 41 snapshots, a tag and the schema.
            assertThat(filePurges().get(0).get("roots").asInt()).isEqualTo(43);

            clock.advance(FilePurges.RECOMMIT_GRACE);
            assertSweep(server.catalog().filePurges().sweep(null), 1, 0, 0);
            assertThat(dataFiles(root)).hasSize(1);
            assertThat(orphans(catalog, T)).isEmpty();
        } finally {
            slow.close();
        }
    }

    // ------------------------------------------------------------------ helpers

    private static void assertSweep(FilePurges.Sweep sweep, int purged, int failed, int pending) {
        assertThat(List.of(sweep.purged(), sweep.failed(), sweep.pending()))
                .as(sweep.toString())
                .containsExactly(purged, failed, pending);
    }

    private JsonNode filePurges() throws Exception {
        HttpResponse<String> response = http("GET", "/colvenna/admin/v1/purges", adminToken, null);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body()).get("filePurges");
    }

    /** An append table with {@code commits} commits, each writing one row and one data file. */
    private static Path tableWithCommits(RESTCatalog catalog, Identifier id, int commits)
            throws Exception {
        Tables.createAppendTable(catalog, id, Map.of());
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        for (int i = 1; i <= commits; i++) {
            Tables.write(catalog.getTable(id), i);
        }
        return Path.of(URI.create(table.location().toUri().toString()));
    }

    /**
     * A table that keeps one snapshot ({@code snapshot.num-retained.max=1}), where each commit
     * overwrites the last: tag t2 holds snapshot 2, whose data file only the tag references once
     * snapshots 3 and 4 have expired it. With {@code branch}, branch b is made from the tag first.
     */
    private static Path tableWithExpiredTag(RESTCatalog catalog, Identifier id, boolean branch)
            throws Exception {
        Tables.createAppendTable(
                catalog,
                id,
                Map.of("snapshot.num-retained.min", "1", "snapshot.num-retained.max", "1"));
        overwrite(catalog, id, 1);
        overwrite(catalog, id, 2);
        catalog.createTag(id, "t2", 2L, null, false);
        if (branch) {
            catalog.createBranch(id, "b", "t2");
        }
        overwrite(catalog, id, 3);
        overwrite(catalog, id, 4);
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        assertThat(snapshotIds(table)).containsExactly(4L);
        return Path.of(URI.create(table.location().toUri().toString()));
    }

    private static void overwrite(RESTCatalog catalog, Identifier id, int value) throws Exception {
        BatchWriteBuilder builder = catalog.getTable(id).newBatchWriteBuilder().withOverwrite();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            write.write(GenericRow.of(value, BinaryString.fromString("v" + value)));
            commit.commit(write.prepareCommit());
        }
    }

    /** Writes {@code ids} to the keyed table with the payload of {@code round}. */
    private static void upsert(RESTCatalog catalog, Identifier id, int round, int... ids)
            throws Exception {
        BatchWriteBuilder builder = catalog.getTable(id).newBatchWriteBuilder();
        // Deletion vectors need lookups, which need a temporary directory.
        try (IOManager io = IOManager.create(System.getProperty("java.io.tmpdir"));
                BatchTableWrite write = builder.newWrite().withIOManager(io);
                BatchTableCommit commit = builder.newCommit()) {
            for (int key : ids) {
                write.write(GenericRow.of(key, BinaryString.fromString("r" + round)));
            }
            commit.commit(write.prepareCommit());
        }
    }

    private static void fullCompaction(RESTCatalog catalog, Identifier id) throws Exception {
        BatchWriteBuilder builder = catalog.getTable(id).newBatchWriteBuilder();
        try (IOManager io = IOManager.create(System.getProperty("java.io.tmpdir"));
                BatchTableWrite write = builder.newWrite().withIOManager(io);
                BatchTableCommit commit = builder.newCommit()) {
            write.compact(BinaryRow.EMPTY_ROW, 0, true);
            commit.commit(write.prepareCommit());
        }
    }

    private static long latest(RESTCatalog catalog, Identifier id) throws Exception {
        return catalog.getTable(id).latestSnapshot().orElseThrow().id();
    }

    /** The index manifests of the table's snapshots. */
    private static List<String> indexManifests(FileStoreTable table) throws Exception {
        List<String> out = new java.util.ArrayList<>();
        Iterator<Snapshot> snapshots = table.snapshotManager().snapshots();
        while (snapshots.hasNext()) {
            String index = snapshots.next().indexManifest();
            if (index != null) {
                out.add(index);
            }
        }
        return out;
    }

    /**
     * The files that a snapshot, tag or long-lived changelog of any branch names and that are not
     * there, as Paimon's own scans list them: each root's data files, the changelog files it adds,
     * and its index files.
     */
    private static List<String> missingFiles(RESTCatalog catalog, Identifier id) throws Exception {
        List<String> missing = new java.util.ArrayList<>();
        List<String> branches = new java.util.ArrayList<>(catalog.listBranches(id));
        branches.add(0, "main");
        for (String branch : branches) {
            FileStoreTable table =
                    (FileStoreTable)
                            catalog.getTable(
                                    branch.equals("main")
                                            ? id
                                            : new Identifier(
                                                    id.getDatabaseName(),
                                                    id.getTableName(),
                                                    branch));
            List<Snapshot> roots = new java.util.ArrayList<>();
            table.snapshotManager().snapshots().forEachRemaining(roots::add);
            roots.addAll(table.tagManager().taggedSnapshots());
            roots.addAll(table.changelogManager().safelyGetAllChangelogs());
            for (Snapshot root : roots) {
                for (ScanMode mode : new ScanMode[] {ScanMode.ALL, ScanMode.CHANGELOG}) {
                    if (mode == ScanMode.ALL && root instanceof org.apache.paimon.Changelog) {
                        continue;
                    }
                    for (ManifestEntry entry :
                            table.store()
                                    .newScan()
                                    .withSnapshot(root)
                                    .withKind(mode)
                                    .plan()
                                    .files()) {
                        org.apache.paimon.fs.Path path =
                                table.store()
                                        .pathFactory()
                                        .createDataFilePathFactory(
                                                entry.partition(), entry.bucket())
                                        .toPath(entry.file());
                        if (!table.fileIO().exists(path)) {
                            missing.add(
                                    branch + " " + root.id() + " " + mode + " " + path.getName());
                        }
                    }
                }
                // A long-lived changelog keeps its snapshot's other metadata only until Paimon's
                // expiry deletes it: it is read for its changelog files.
                if (root.indexManifest() != null
                        && !(root instanceof org.apache.paimon.Changelog)) {
                    IndexFileHandler index = table.store().newIndexFileHandler();
                    for (IndexManifestEntry entry : index.readManifest(root.indexManifest())) {
                        if (!index.existsIndexFile(entry)) {
                            missing.add(
                                    branch
                                            + " "
                                            + root.id()
                                            + " index "
                                            + entry.indexFile().fileName());
                        }
                    }
                }
            }
        }
        return missing;
    }

    private static Set<Path> dataFiles(Path root) throws Exception {
        try (Stream<Path> files = Files.walk(root)) {
            return files.filter(p -> p.getFileName().toString().startsWith("data-"))
                    .collect(Collectors.toCollection(TreeSet::new));
        }
    }

    private static List<String> snapshotFiles(Path root) throws Exception {
        try (Stream<Path> files = Files.list(root.resolve("snapshot"))) {
            return files.map(p -> p.getFileName().toString())
                    .filter(n -> n.startsWith("snapshot-"))
                    .sorted()
                    .toList();
        }
    }

    /** What Paimon's orphan file cleaning would delete now: the files no root references. */
    private static List<String> orphans(RESTCatalog catalog, Identifier id) throws Exception {
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        return new LocalOrphanFilesClean(table, System.currentTimeMillis() + 86_400_000L, true)
                .clean().getDeletedFilesPath().stream().map(p -> p.getName()).sorted().toList();
    }

    /**
     * Every snapshot and tag of every branch, read in full: "branch snapshot-N" or "branch
     * tag-name" to the ids it holds, in order. A referenced file that is gone fails the read.
     */
    private static Map<String, List<Integer>> everyRoot(RESTCatalog catalog, Identifier id)
            throws Exception {
        Map<String, List<Integer>> out = new TreeMap<>();
        List<String> branches = new java.util.ArrayList<>(catalog.listBranches(id));
        branches.add(0, "main");
        for (String branch : branches) {
            Identifier at =
                    branch.equals("main")
                            ? id
                            : new Identifier(id.getDatabaseName(), id.getTableName(), branch);
            FileStoreTable table = (FileStoreTable) catalog.getTable(at);
            Iterator<Snapshot> snapshots = table.snapshotManager().snapshots();
            while (snapshots.hasNext()) {
                Snapshot snapshot = snapshots.next();
                out.put(branch + " snapshot-" + snapshot.id(), ids(table, snapshot));
            }
            for (String tag : table.tagManager().allTagNames()) {
                out.put(
                        branch + " tag-" + tag,
                        ids(table, table.tagManager().getOrThrow(tag).trimToSnapshot()));
            }
        }
        return out;
    }

    private static List<Long> snapshotIds(FileStoreTable table) throws Exception {
        List<Long> ids = new java.util.ArrayList<>();
        table.snapshotManager().snapshots().forEachRemaining(s -> ids.add(s.id()));
        return ids;
    }

    /** The ids {@code snapshot} of {@code table} holds, read from its files, in order. */
    private static List<Integer> ids(FileStoreTable table, Snapshot snapshot) throws Exception {
        List<Split> splits = table.newSnapshotReader().withSnapshot(snapshot).read().splits();
        List<Integer> ids = new java.util.ArrayList<>();
        try (RecordReader<InternalRow> reader = table.newRead().createReader(splits)) {
            reader.forEachRemaining(row -> ids.add(row.getInt(0)));
        }
        return ids.stream().sorted().toList();
    }

    /** A data directory of the table's, which a purge cannot change once it is read-only. */
    private static Path bucket(Path root) throws Exception {
        try (Stream<Path> dirs = Files.list(root)) {
            return dirs.filter(p -> p.getFileName().toString().startsWith("bucket-"))
                    .findFirst()
                    .orElseThrow();
        }
    }

    /** Another server on the same database and warehouse, with {@code settings}. */
    private ColvennaCatalogServer replica(Map<String, String> settings) throws Exception {
        ColvennaCatalogServer replica =
                ColvennaCatalogServer.create(
                        TestServers.withSettings(warehouse, externalLocationRoots(), settings),
                        clock,
                        dataTokens());
        replica.start();
        return replica;
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
}
