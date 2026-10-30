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
 * A database session ended in the middle of a request ({@code pg_terminate_backend}, as a database
 * restart or a failover ends them all) answers 503, which the Paimon client's HTTP layer retries,
 * and a retried commit still takes effect once: what the session had not committed PostgreSQL
 * rolled back, and a snapshot published before the session ended is rolled forward by the retry,
 * which is then a replay.
 */
class SessionLossTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("db", "events");
    private static final String COMMIT_PATH = "/v1/colvenna/databases/db/tables/events/commit";

    /** When set, the publish of this snapshot ends the session of the commit that made it. */
    private final AtomicReference<String> endSessionAfterPublishOf = new AtomicReference<>();

    /** When set, the next publish of any snapshot ends the session of its commit, once. */
    private volatile boolean endSessionAtNextPublish;

    /** The connection holding a lock, until the test releases it. */
    @Nullable private Connection holder;

    @Override
    protected CommitService.Faults faults() {
        return new CommitService.Faults() {
            @Override
            public void afterPublish(TableRecord table, Snapshot snapshot) {
                if (snapshot.uuid().equals(endSessionAfterPublishOf.get())
                        || endSessionAtNextPublish) {
                    endSessionAtNextPublish = false;
                    // The session holding the branch pointer's row lock: the commit's own.
                    endSessions(
                            "SELECT pid FROM pg_locks WHERE relation = 'table_branches'::regclass"
                                    + " AND mode = 'RowShareLock' AND granted"
                                    + " AND pid <> pg_backend_pid()");
                }
            }
        };
    }

    @AfterEach
    void release() throws Exception {
        // The triggers first: left behind, they would end the sessions of later tests' writes.
        try (Connection c = connect();
                Statement s = c.createStatement()) {
            s.execute("DROP TRIGGER IF EXISTS end_session_at_commit ON table_branches");
            s.execute("DROP TRIGGER IF EXISTS end_session_at_commit ON catalog_databases");
            s.execute("DROP FUNCTION IF EXISTS end_session_at_commit()");
        } finally {
            releaseLock();
        }
    }

    /**
     * Ends the session that commits the next change to {@code table}, in its COMMIT: a deferred
     * trigger ends its own backend there, so the server loses the connection while it commits and
     * cannot know whether the transaction took effect (here it did not).
     */
    private static void endSessionAtCommit(String table) throws Exception {
        try (Connection c = connect();
                Statement s = c.createStatement()) {
            s.execute(
                    "CREATE OR REPLACE FUNCTION end_session_at_commit() RETURNS trigger"
                            + " LANGUAGE plpgsql AS $$ BEGIN"
                            + " PERFORM pg_terminate_backend(pg_backend_pid());"
                            + " PERFORM pg_sleep(5); RETURN NULL; END $$");
            s.execute(
                    "CREATE CONSTRAINT TRIGGER end_session_at_commit AFTER INSERT OR UPDATE ON "
                            + table
                            + " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW"
                            + " EXECUTE FUNCTION end_session_at_commit()");
        }
    }

    private static void stopEndingSessionsAtCommit(String table) throws Exception {
        try (Connection c = connect();
                Statement s = c.createStatement()) {
            s.execute("DROP TRIGGER end_session_at_commit ON " + table);
        }
    }

    @Test
    void aCommitWhoseSessionEndsInItsCommitAnswers503AndTheRetryIsAReplay() throws Exception {
        Commit commit = prepareSecondCommit();
        endSessionAtCommit("table_branches");
        HttpResponse<String> lost = http("POST", COMMIT_PATH, adminToken, commit.body());
        stopEndingSessionsAtCommit("table_branches");
        assertThat(lost.statusCode()).as(lost.body()).isEqualTo(503);
        assertThat(lost.body()).contains("Retry");
        // Published, not recorded: the retry rolls it forward and is a replay.
        assertThat(commit.table().snapshotManager().snapshotExists(2)).isTrue();
        assertThat(pointer()).isEqualTo("1");
        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(pointer()).isEqualTo("2");
        assertThat(commit.table().snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(commitAudit()).containsExactly("200:SUCCEEDED", "503:FAILED", "200:SUCCEEDED");
    }

    @Test
    void anotherWriteWhoseSessionEndsInItsCommitIsNotRetryable() throws Exception {
        endSessionAtCommit("catalog_databases");
        HttpResponse<String> lost =
                http("POST", "/v1/colvenna/databases", adminToken, "{\"name\":\"fresh\"}");
        stopEndingSessionsAtCommit("catalog_databases");
        // Whether it took effect is unknown to the server, and a repeated write is no replay.
        assertThat(lost.statusCode()).as(lost.body()).isEqualTo(500);
        assertThat(lost.body()).contains("unknown");
        assertThat(
                        column(
                                "SELECT http_status || ':' || outcome FROM audit_events"
                                        + " WHERE action = 'CREATE_DATABASE'"))
                .containsExactly("500:FAILED");
    }

    @Test
    void aSessionEndedBeforeAnythingWasWrittenAnswers503AndTheRetryCommitsOnce() throws Exception {
        Commit commit = prepareSecondCommit();
        hold("SELECT 1 FROM table_branches WHERE branch = 'main' FOR UPDATE");
        CompletableFuture<HttpResponse<String>> waiting =
                CompletableFuture.supplyAsync(
                        () -> {
                            try {
                                return http("POST", COMMIT_PATH, adminToken, commit.body());
                            } catch (Exception e) {
                                throw new IllegalStateException(e);
                            }
                        });
        // The commit waits for the branch lock; its session ends while it waits.
        endSessions(
                "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                        + " AND datname = current_database() AND pid <> pg_backend_pid()");
        HttpResponse<String> lost = waiting.get(30, TimeUnit.SECONDS);
        assertThat(lost.statusCode()).as(lost.body()).isEqualTo(503);
        assertThat(lost.body()).contains("\"code\":503").contains("Retry the request");
        // Nothing was recorded or written.
        assertThat(pointer()).isEqualTo("1");
        assertThat(commit.table().snapshotManager().snapshotExists(2)).isFalse();
        assertThat(column("SELECT count(*) FROM commit_intents WHERE snapshot_id = 2"))
                .containsExactly("0");
        releaseLock();

        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(pointer()).isEqualTo("2");
        assertThat(commit.table().snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(commitAudit())
                .containsExactly("200:SUCCEEDED", "503:FAILED", "200:SUCCEEDED", "200:SUCCEEDED");
    }

    @Test
    void aSessionEndedAfterThePublishAnswers503AndTheRetryIsAReplay() throws Exception {
        Commit commit = prepareSecondCommit();
        endSessionAfterPublishOf.set(commit.snapshot().uuid());
        HttpResponse<String> lost = http("POST", COMMIT_PATH, adminToken, commit.body());
        assertThat(lost.statusCode()).as(lost.body()).isEqualTo(503);
        // snapshot-2 is published, so the commit happened; PostgreSQL did not record it, and the
        // request was not answered as a success.
        assertThat(commit.table().snapshotManager().snapshotExists(2)).isTrue();
        assertThat(pointer()).isEqualTo("1");
        assertThat(column("SELECT state FROM commit_intents WHERE snapshot_id = 2"))
                .containsExactly("PENDING");
        endSessionAfterPublishOf.set(null);

        // The retry rolls the published snapshot forward and is then answered as a replay.
        assertThat(committed(http("POST", COMMIT_PATH, adminToken, commit.body()))).isTrue();
        assertThat(pointer()).isEqualTo("2");
        assertThat(commit.table().snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(
                        column(
                                "SELECT state || ':' || resolution FROM commit_intents"
                                        + " WHERE snapshot_id = 2"))
                .containsExactly("PUBLISHED:rolled forward");
        assertThat(commitAudit()).containsExactly("200:SUCCEEDED", "503:FAILED", "200:SUCCEEDED");
    }

    @Test
    void thePaimonClientRetriesASessionLossByItselfAndCommitsOnce() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        endSessionAtNextPublish = true;
        Tables.write(catalog.getTable(TABLE), 2);
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        assertThat(Tables.ids(table)).containsExactlyInAnyOrder(1, 2);
        assertThat(table.snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(commitAudit()).containsExactly("200:SUCCEEDED", "503:FAILED", "200:SUCCEEDED");
    }

    /** Ends the sessions {@code sql} selects, waiting until there is one and it has gone. */
    private static void endSessions(String sql) {
        try (Connection c = connect();
                Statement s = c.createStatement()) {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
            List<Integer> pids = new ArrayList<>();
            while (pids.isEmpty()) {
                if (System.nanoTime() > deadline) {
                    throw new AssertionError("No session to end: " + sql);
                }
                try (ResultSet rs = s.executeQuery(sql)) {
                    while (rs.next()) {
                        pids.add(rs.getInt(1));
                    }
                }
                if (pids.isEmpty()) {
                    Thread.sleep(20);
                }
            }
            for (int pid : pids) {
                try (ResultSet rs = s.executeQuery("SELECT pg_terminate_backend(" + pid + ")")) {
                    rs.next();
                }
            }
            for (int pid : pids) {
                while (true) {
                    try (ResultSet rs =
                            s.executeQuery(
                                    "SELECT count(*) FROM pg_stat_activity WHERE pid = " + pid)) {
                        rs.next();
                        if (rs.getInt(1) == 0) {
                            break;
                        }
                    }
                    if (System.nanoTime() > deadline) {
                        throw new AssertionError("Session " + pid + " did not end");
                    }
                    Thread.sleep(20);
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static Connection connect() throws Exception {
        return DriverManager.getConnection(
                TestServers.postgres().getJdbcUrl(),
                TestServers.postgres().getUsername(),
                TestServers.postgres().getPassword());
    }

    /** A table with one snapshot, and the request that commits a second one on top of it. */
    private record Commit(FileStoreTable table, Snapshot snapshot, String body) {}

    private Commit prepareSecondCommit() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        Snapshot first = table.snapshotManager().latestSnapshot();
        String json = first.toJson();
        Snapshot second =
                Snapshot.fromJson(
                        json.replaceFirst("\"id\"\\s*:\\s*" + first.id(), "\"id\" : 2")
                                .replace(first.uuid(), UUID.randomUUID().toString()));
        String body =
                RESTApi.toJson(
                        new CommitTableRequest(
                                table.uuid(), first.uuid(), second, Collections.emptyList()));
        return new Commit(table, second, body);
    }

    private static boolean committed(HttpResponse<String> response) throws Exception {
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return RESTApi.OBJECT_MAPPER.readTree(response.body()).get("success").asBoolean();
    }

    /** Takes a lock in a transaction of its own that stays open until {@link #releaseLock}. */
    private synchronized void hold(String sql) throws Exception {
        Connection c = connect();
        c.setAutoCommit(false);
        try (Statement s = c.createStatement()) {
            s.execute(sql);
        }
        holder = c;
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

    /** Status and outcome of the audited commits, in order. */
    private List<String> commitAudit() throws Exception {
        return column(
                "SELECT http_status || ':' || outcome FROM audit_events WHERE action = 'COMMIT'"
                        + " ORDER BY id");
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
