package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import com.colvenna.catalog.testing.TestServers;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import javax.annotation.Nullable;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.requests.CommitTableRequest;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

/**
 * A lock wait that times out ({@code COLVENNA_JDBC_LOCK_TIMEOUT}) answers 503, which the Paimon
 * client's HTTP layer retries for any method, and a retried commit still takes effect once: the
 * branch lock's compare-and-set recognises a request whose snapshot is already the head as a replay
 * ({@code CommitService.commit}), and a snapshot published before the timeout is rolled forward by
 * the retry first.
 */
class LockTimeoutTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("db", "events");
    private static final String COMMIT_PATH = "/v1/colvenna/databases/db/tables/events/commit";

    /** When set, the publish of this snapshot is followed by a lock on its commit intent. */
    private final AtomicReference<String> lockIntentOf = new AtomicReference<>();

    /** The connection holding a lock, until the test releases it. */
    @Nullable private Connection holder;

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_JDBC_LOCK_TIMEOUT", "PT1S");
    }

    @Override
    protected CommitService.Faults faults() {
        return new CommitService.Faults() {
            @Override
            public void afterPublish(TableRecord table, Snapshot snapshot) {
                if (snapshot.uuid().equals(lockIntentOf.get())) {
                    // The commit then waits for its own intent row when it marks it published.
                    hold(
                            "SELECT id FROM commit_intents WHERE snapshot_uuid = '"
                                    + snapshot.uuid()
                                    + "' FOR UPDATE");
                }
            }
        };
    }

    @AfterEach
    void release() throws Exception {
        releaseLock();
    }

    @Test
    void aLockTimeoutAnswers503AndTheRetryCommitsOnce() throws Exception {
        Commit commit = prepareSecondCommit();
        hold("SELECT 1 FROM table_branches WHERE branch = 'main' FOR UPDATE");
        HttpResponse<String> timedOut = http("POST", COMMIT_PATH, adminToken, commit.body());
        assertThat(timedOut.statusCode()).isEqualTo(503);
        assertThat(timedOut.body())
                .contains("\"code\":503")
                .contains("Timed out waiting for a lock")
                .contains("Retry the request");
        // The wait was for the branch lock, before anything was recorded or written.
        assertThat(pointer()).isEqualTo("1");
        assertThat(commit.table().snapshotManager().snapshotExists(2)).isFalse();
        assertThat(column("SELECT count(*) FROM commit_intents WHERE snapshot_id = 2"))
                .containsExactly("0");
        releaseLock();

        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        // The same request once more, as after a lost answer: a replay, nothing new.
        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(pointer()).isEqualTo("2");
        assertThat(commit.table().snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(column("SELECT state FROM commit_intents WHERE snapshot_id = 2"))
                .containsExactly("PUBLISHED");
        assertThat(commitStatuses()).containsExactly("200", "503", "200", "200");
    }

    @Test
    void aLockTimeoutAfterThePublishIsAReplayOnRetry() throws Exception {
        Commit commit = prepareSecondCommit();
        lockIntentOf.set(commit.snapshot().uuid());
        HttpResponse<String> timedOut = http("POST", COMMIT_PATH, adminToken, commit.body());
        assertThat(timedOut.statusCode()).isEqualTo(503);
        // snapshot-2 is published, so the commit happened; PostgreSQL did not record it.
        assertThat(commit.table().snapshotManager().snapshotExists(2)).isTrue();
        assertThat(pointer()).isEqualTo("1");
        assertThat(column("SELECT state FROM commit_intents WHERE snapshot_id = 2"))
                .containsExactly("PENDING");
        releaseLock();
        lockIntentOf.set(null);

        // The retry rolls the published snapshot forward and is then answered as a replay.
        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(pointer()).isEqualTo("2");
        assertThat(commit.table().snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(
                        column(
                                "SELECT state || ':' || resolution FROM commit_intents"
                                        + " WHERE snapshot_id = 2"))
                .containsExactly("PUBLISHED:rolled forward");
        assertThat(column("SELECT count(*) FROM audit_events WHERE action = 'ROLL_FORWARD_COMMIT'"))
                .containsExactly("1");
    }

    @Test
    void thePaimonClientRetriesALockTimeoutByItselfAndCommitsOnce() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        hold("SELECT 1 FROM table_branches WHERE branch = 'main' FOR UPDATE");
        // Released once the server has answered 503; the client's next attempt then gets through.
        CompletableFuture<Void> releasing =
                CompletableFuture.runAsync(
                        () -> {
                            try {
                                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
                                while (!commitStatuses().contains("503")
                                        && System.nanoTime() < deadline) {
                                    Thread.sleep(50);
                                }
                                releaseLock();
                            } catch (Exception e) {
                                throw new IllegalStateException(e);
                            }
                        });
        Tables.write(catalog.getTable(TABLE), 2);
        releasing.get(30, TimeUnit.SECONDS);
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        assertThat(Tables.ids(table)).containsExactlyInAnyOrder(1, 2);
        assertThat(table.snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(commitStatuses()).containsExactly("200", "503", "200");
    }

    @Test
    void branchChangesTakeTheirLocksBeforeTheyTouchFiles() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        String branches = "/v1/colvenna/databases/db/tables/events/branches";
        // A commit to main holds main's pointer while a branch is created from main's head.
        hold("SELECT 1 FROM table_branches WHERE branch = 'main' FOR UPDATE");
        assertThat(http("POST", branches, adminToken, "{\"branch\":\"b1\"}").statusCode())
                .isEqualTo(503);
        assertThat(branchExists("b1")).isFalse();
        releaseLock();
        assertThat(http("POST", branches, adminToken, "{\"branch\":\"b1\"}").statusCode())
                .isEqualTo(200);
        assertThat(branchExists("b1")).isTrue();

        // A commit to the branch holds its pointer while the branch is dropped.
        hold("SELECT 1 FROM table_branches WHERE branch = 'b1' FOR UPDATE");
        assertThat(http("DELETE", branches + "/b1", adminToken, null).statusCode()).isEqualTo(503);
        assertThat(branchExists("b1")).isTrue();
        releaseLock();
        assertThat(http("DELETE", branches + "/b1", adminToken, null).statusCode()).isEqualTo(200);
        assertThat(branchExists("b1")).isFalse();
    }

    @Test
    void aDropWaitsOneLockTimeoutForAllItsBranchesNotOneEach() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        catalog.createTag(TABLE, "t", 1L, null, false);
        catalog.createBranch(TABLE, "b1", "t");
        catalog.createBranch(TABLE, "b2", "t");
        // Each pointer held a little under the 1 s lock timeout, one after the other: timed lock
        // by lock, the drop would wait 0.8 s three times and succeed after 2.4 s.
        List<Connection> holders = new ArrayList<>();
        for (String branch : List.of("b1", "b2", "main")) {
            Connection c =
                    DriverManager.getConnection(
                            TestServers.postgres().getJdbcUrl(),
                            TestServers.postgres().getUsername(),
                            TestServers.postgres().getPassword());
            c.setAutoCommit(false);
            try (Statement st = c.createStatement()) {
                st.execute(
                        "SELECT 1 FROM table_branches WHERE branch = '" + branch + "' FOR UPDATE");
            }
            holders.add(c);
        }
        Thread releaser =
                new Thread(
                        () -> {
                            try {
                                for (Connection c : holders) {
                                    Thread.sleep(800);
                                    c.rollback();
                                    c.close();
                                }
                            } catch (Exception e) {
                                throw new IllegalStateException(e);
                            }
                        });
        long started = System.nanoTime();
        releaser.start();
        HttpResponse<String> drop =
                http("DELETE", "/v1/colvenna/databases/db/tables/events", adminToken, null);
        long elapsed = (System.nanoTime() - started) / 1_000_000;
        releaser.join();
        assertThat(drop.statusCode()).as(drop.body()).isEqualTo(503);
        assertThat(elapsed).as("ms until the answer").isLessThan(2000);
        // Nothing was dropped: the retry, now unhindered, drops it.
        assertThat(catalog.listTables("db")).contains("events");
        assertThat(
                        http("DELETE", "/v1/colvenna/databases/db/tables/events", adminToken, null)
                                .statusCode())
                .isEqualTo(200);
    }

    private boolean branchExists(String branch) throws Exception {
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        return table.branchManager().branchExists(branch);
    }

    /** A table with one snapshot, and the request that commits a second one on top of it. */
    private record Commit(FileStoreTable table, Snapshot snapshot, String body) {}

    private Commit prepareSecondCommit() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        Snapshot first = table.snapshotManager().latestSnapshot();
        Snapshot second = copyWithId(first, 2);
        String body =
                RESTApi.toJson(
                        new CommitTableRequest(
                                table.uuid(), first.uuid(), second, Collections.emptyList()));
        return new Commit(table, second, body);
    }

    private static Snapshot copyWithId(Snapshot base, long id) {
        String json = base.toJson();
        Snapshot parsed = Snapshot.fromJson(json);
        String renamed =
                json.replaceFirst("\"id\"\\s*:\\s*" + parsed.id(), "\"id\" : " + id)
                        .replace(parsed.uuid(), UUID.randomUUID().toString());
        return Snapshot.fromJson(renamed);
    }

    private static boolean committed(HttpResponse<String> response) throws Exception {
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body()).get("success").asBoolean();
    }

    /** Takes a lock in a transaction of its own that stays open until {@link #releaseLock}. */
    private synchronized void hold(String sql) {
        try {
            Connection c =
                    DriverManager.getConnection(
                            TestServers.postgres().getJdbcUrl(),
                            TestServers.postgres().getUsername(),
                            TestServers.postgres().getPassword());
            c.setAutoCommit(false);
            try (Statement s = c.createStatement()) {
                s.execute(sql);
            }
            holder = c;
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private synchronized void releaseLock() throws Exception {
        if (holder != null) {
            holder.rollback();
            holder.close();
            holder = null;
        }
    }

    private String pointer() throws Exception {
        return column("SELECT snapshot_id FROM table_branches WHERE branch = 'main'").get(0);
    }

    /** HTTP statuses of the audited commits, in order. */
    private List<String> commitStatuses() throws Exception {
        return column("SELECT http_status FROM audit_events WHERE action = 'COMMIT' ORDER BY id");
    }

    private List<String> column(String sql) throws Exception {
        List<String> rows = new ArrayList<>();
        try (Connection c = server.db().dataSource().getConnection();
                PreparedStatement select = c.prepareStatement(sql);
                ResultSet rs = select.executeQuery()) {
            while (rs.next()) {
                rows.add(rs.getString(1));
            }
        }
        return rows;
    }
}
