package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.testing.DroppingProxy;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.net.http.HttpResponse;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Collectors;
import java.util.stream.IntStream;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryRow;
import org.apache.paimon.fs.Path;
import org.apache.paimon.fs.local.LocalFileIO;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.exceptions.AlreadyExistsException;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.sink.CommitMessage;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;
import org.apache.paimon.utils.SnapshotManager;
import org.junit.jupiter.api.Test;

/**
 * The commit protocol under concurrency and failures: every acknowledged commit is published once,
 * an unacknowledged commit resolves to a definite outcome, and snapshots the catalog did not
 * publish fence the table.
 */
class CommitProtocolTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("db", "events");

    /** Commit-retry options that keep failing tests short. */
    private static final Map<String, String> FAST_RETRY =
            Map.of("commit.min-retry-wait", "10 ms", "commit.max-retry-wait", "100 ms");

    /** The commit users of the two conflicting compactions. */
    private static final String COMPACT_A = "compact-a";

    private static final String COMPACT_B = "compact-b";

    /** How Paimon gives up a commit on a conflict (not "there maybe exist commit conflicts"). */
    private static final String GAVE_UP_ON_CONFLICT =
            "(File deletion|LSM) conflicts detected! Give up committing\\.";

    private final AtomicInteger failBeforePublish = new AtomicInteger();
    private final AtomicInteger failAfterPublish = new AtomicInteger();
    private final AtomicReference<Snapshot> stoppedBeforePublish = new AtomicReference<>();
    private final AtomicReference<String> holdPublishOf = new AtomicReference<>();
    private final CountDownLatch publishHeld = new CountDownLatch(1);
    private final CountDownLatch releasePublish = new CountDownLatch(1);

    @Override
    protected CommitService.Faults faults() {
        return new CommitService.Faults() {
            @Override
            public void beforePublish(TableRecord table, Snapshot snapshot) {
                if (snapshot.commitUser().equals(holdPublishOf.get())) {
                    publishHeld.countDown();
                    try {
                        if (!releasePublish.await(30, TimeUnit.SECONDS)) {
                            throw new IllegalStateException("publish never released");
                        }
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        throw new IllegalStateException(e);
                    }
                }
                if (failBeforePublish.getAndUpdate(n -> Math.max(0, n - 1)) > 0) {
                    stoppedBeforePublish.set(snapshot);
                    throw new IllegalStateException("injected crash before publish");
                }
            }

            @Override
            public void afterPublish(TableRecord table, Snapshot snapshot) {
                if (failAfterPublish.getAndUpdate(n -> Math.max(0, n - 1)) > 0) {
                    throw new IllegalStateException("injected crash after publish");
                }
            }
        };
    }

    @Test
    void concurrentWritersLoseNoCommit() throws Exception {
        Tables.createAppendTable(catalog(adminToken), TABLE, contended());
        writeConcurrently(server.port());
    }

    @Test
    void writersOnTwoReplicasLoseNoCommit() throws Exception {
        Tables.createAppendTable(catalog(adminToken), TABLE, contended());
        try (ColvennaCatalogServer second = startReplica()) {
            // Alternate replicas, as the Service would; they share only PostgreSQL and files.
            writeConcurrently(server.port(), second.port());
        }
        assertThat(pendingIntents()).isZero();
    }

    @Test
    void ofTwoConflictingCompactionsOnlyOneIsPublished() throws Exception {
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", "3");
        RESTCatalog catalog = catalog(adminToken);
        Tables.createKeyedTable(catalog, TABLE, options);
        Tables.write(catalog.getTable(TABLE), 1);
        Tables.write(catalog.getTable(TABLE), 2);
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        StreamWriteBuilder a = table.newStreamWriteBuilder().withCommitUser(COMPACT_A);
        StreamWriteBuilder b = table.newStreamWriteBuilder().withCommitUser(COMPACT_B);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        String loser;
        try (StreamTableWrite writeA = a.newWrite();
                StreamTableWrite writeB = b.newWrite();
                StreamTableCommit commitA = a.newCommit();
                StreamTableCommit commitB = b.newCommit()) {
            // Both compact the same two files from the same base.
            writeA.compact(BinaryRow.EMPTY_ROW, 0, true);
            writeB.compact(BinaryRow.EMPTY_ROW, 0, true);
            List<CommitMessage> compactedByA = writeA.prepareCommit(true, 1);
            List<CommitMessage> compactedByB = writeB.prepareCommit(true, 1);
            // A stops just before publishing, holding the branch; B commits on the same base.
            holdPublishOf.set(COMPACT_A);
            Future<?> first =
                    pool.submit(
                            () -> {
                                commitA.commit(1, compactedByA);
                                return null;
                            });
            assertThat(publishHeld.await(30, TimeUnit.SECONDS)).isTrue();
            Future<?> second =
                    pool.submit(
                            () -> {
                                commitB.commit(1, compactedByB);
                                return null;
                            });
            // A streaming commit is two snapshots: an empty append (it records the commit
            // identifier), then the compaction. The hold pauses A's append inside the catalog's
            // transaction, which holds the branch lock. Release it only once B's first attempt, its
            // own empty append, waits for that lock: then B is refused on its stale base.
            awaitLockWait();
            releasePublish.countDown();
            // Either may win. After A's append, A's compaction and B's retried append and
            // compaction race for the next slots, and the compaction published first wins. The
            // other either reaches the catalog on a base the winner moved, is refused, and finds
            // the conflict on its retry; or finds the conflict before its compaction is sent.
            Map<String, Throwable> failures = new HashMap<>();
            failures.put(COMPACT_A, failureOf(first));
            failures.put(COMPACT_B, failureOf(second));
            List<String> lost =
                    failures.entrySet().stream()
                            .filter(e -> e.getValue() != null)
                            .map(Map.Entry::getKey)
                            .collect(Collectors.toList());
            assertThat(lost).as("the compactions that gave up").hasSize(1);
            loser = lost.get(0);
            assertThat(stackTrace(failures.get(loser))).containsPattern(GAVE_UP_ON_CONFLICT);
        } finally {
            pool.shutdownNow();
        }
        String winner = loser.equals(COMPACT_A) ? COMPACT_B : COMPACT_A;
        // B's first attempt, made while A held the branch, was refused on its stale base.
        assertThat(commits(COMPACT_B)).first().isEqualTo("FAILED APPEND");
        // The winner's compaction was published once; the loser's never: only its empty append
        // went in.
        assertThat(commits(winner)).containsOnlyOnce("SUCCEEDED COMPACT");
        assertThat(commits(loser)).contains("SUCCEEDED APPEND").doesNotContain("SUCCEEDED COMPACT");
        long refusedCompactions =
                commits(loser).stream().filter(r -> r.equals("FAILED COMPACT")).count();
        System.out.println(
                "compaction race: "
                        + winner
                        + " won; "
                        + loser
                        + (refusedCompactions > 0
                                ? "'s compaction reached the catalog and was refused "
                                        + refusedCompactions
                                        + " time(s)"
                                : " gave up before its compaction reached the catalog"));
        // And counted, for the conflict rate (HLD 11.2).
        assertThat(server.metrics().render())
                .containsPattern(
                        "colvenna_catalog_commits_total\\{table=\"[^\"]*\",outcome=\"conflict\"} [1-9]")
                .containsPattern(
                        "colvenna_catalog_commits_total\\{table=\"[^\"]*\",outcome=\"committed\"} [1-9]")
                .contains("colvenna_catalog_request_seconds_bucket{action=\"COMMIT\"");
        FileStoreTable after = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        List<String> compactions = new ArrayList<>();
        for (long n = 1; n <= after.snapshotManager().latestSnapshotId(); n++) {
            Snapshot snapshot = after.snapshotManager().snapshot(n);
            if (snapshot.commitKind() == Snapshot.CommitKind.COMPACT) {
                compactions.add(snapshot.commitUser());
            }
        }
        assertThat(compactions).containsExactly(winner);
        assertThat(Tables.ids(after)).containsExactlyInAnyOrder(1, 2);
        assertThat(pendingIntents()).isZero();
    }

    @Test
    void lostCommitResponseResolvesToOneCommit() throws Exception {
        try (DroppingProxy proxy = new DroppingProxy(server.port())) {
            RESTCatalog viaProxy = catalog(adminToken, proxy.port(), Map.of());
            Tables.createAppendTable(viaProxy, TABLE, FAST_RETRY);
            Tables.write(viaProxy.getTable(TABLE), 1);
            proxy.dropResponses(path -> path.endsWith("/commit"), 1);
            Tables.write(viaProxy.getTable(TABLE), 2);
            assertThat(proxy.droppedCount()).isEqualTo(1);
            FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
            assertThat(Tables.ids(table)).containsExactlyInAnyOrder(1, 2);
            assertThat(table.snapshotManager().latestSnapshotId()).isEqualTo(2L);
        }
    }

    @Test
    void crashAfterPublishRollsTheCommitForward() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, FAST_RETRY);
        Tables.write(catalog.getTable(TABLE), 1);
        failAfterPublish.set(1);
        Tables.write(catalog.getTable(TABLE), 2);
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        assertThat(Tables.ids(table)).containsExactlyInAnyOrder(1, 2);
        assertThat(table.snapshotManager().latestSnapshotId()).isEqualTo(2L);
        assertThat(intentResolutions()).contains("PUBLISHED:rolled forward");
        assertThat(audit("ROLL_FORWARD_COMMIT")).isEqualTo(1);
        // The recovery evidence agrees: pointer, files and intents line up again.
        HttpResponse<String> report =
                http(
                        "GET",
                        "/colvenna/admin/v1/consistency/"
                                + TABLE.getDatabaseName()
                                + "/"
                                + TABLE.getTableName(),
                        adminToken,
                        null);
        assertThat(report.body()).contains("\"consistent\":true");
    }

    @Test
    void crashBeforePublishLeavesNoSnapshotAndTheRetryCommits() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, FAST_RETRY);
        failBeforePublish.set(1);
        Tables.write(catalog.getTable(TABLE), 7);
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        assertThat(Tables.ids(table)).containsExactly(7);
        assertThat(table.snapshotManager().latestSnapshotId()).isEqualTo(1L);
        // The unpublished attempt is closed once its slot is taken by the retry.
        Tables.write(table, 8);
        assertThat(intentResolutions()).contains("ABORTED:slot 1 already published");
        assertThat(pendingIntents()).isZero();
    }

    @Test
    void replayedCommitRequestIsIdempotent() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, Map.of());
        Tables.write(catalog.getTable(TABLE), 1);
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        Snapshot first = table.snapshotManager().latestSnapshot();
        Snapshot second = copyWithId(first, 2);
        assertThat(
                        catalog.commitSnapshot(
                                TABLE, table.uuid(), first.uuid(), second, Collections.emptyList()))
                .isTrue();
        // The same request again, as after a lost response: still a success, still one snapshot.
        assertThat(
                        catalog.commitSnapshot(
                                TABLE, table.uuid(), first.uuid(), second, Collections.emptyList()))
                .isTrue();
        assertThat(catalog.loadSnapshot(TABLE).get().snapshot().id()).isEqualTo(2);
        // A different snapshot on the stale base loses the race.
        assertThat(
                        catalog.commitSnapshot(
                                TABLE,
                                table.uuid(),
                                first.uuid(),
                                copyWithId(first, 2),
                                Collections.emptyList()))
                .isFalse();
        assertThat(strings("SELECT outcome FROM audit_events WHERE action = 'COMMIT' ORDER BY id"))
                .containsExactly("SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "FAILED");
    }

    @Test
    void lateWriteOfAClosedIntentFencesTheTable() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", "0");
        Tables.createAppendTable(catalog, TABLE, options);
        failBeforePublish.set(1);
        assertThatThrownBy(() -> Tables.write(catalog.getTable(TABLE), 1)).isNotNull();
        clock.advance(Duration.ofMinutes(10));
        assertThat(catalog.loadSnapshot(TABLE)).isEmpty();
        assertThat(intentResolutions()).contains("ABORTED:snapshot never published");
        // The replica that wrote the attempt was only partitioned away; its write lands now.
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        Snapshot late = stoppedBeforePublish.get();
        SnapshotManager files =
                new SnapshotManager(
                        LocalFileIO.create(),
                        new Path(table.location().toString()),
                        "main",
                        null,
                        null);
        LocalFileIO.create().writeFile(files.snapshotPath(late.id()), late.toJson(), false);
        assertThatThrownBy(() -> Tables.write(catalog.getTable(TABLE), 2))
                .hasStackTraceContaining("fenced");
        assertThat(tableState()).isEqualTo("FENCED");
    }

    @Test
    void snapshotWrittenOutsideTheCatalogFencesTheTable() throws Exception {
        int retries = 2;
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", String.valueOf(retries));
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, TABLE, options);
        Tables.write(catalog.getTable(TABLE), 1);
        FileStoreTable table = (FileStoreTable) catalog.getTable(TABLE);
        // A JDBC-catalog writer still pointed at the same directory publishes snapshot 2 itself.
        Snapshot foreign = copyWithId(table.snapshotManager().latestSnapshot(), 2);
        SnapshotManager files =
                new SnapshotManager(
                        LocalFileIO.create(),
                        new Path(table.location().toString()),
                        "main",
                        null,
                        null);
        LocalFileIO.create().writeFile(files.snapshotPath(2), foreign.toJson(), false);

        // The Paimon client takes each commit request's 409 (AlreadyExistsException) for an
        // outcome it cannot know and tries again, commit.max-retries times (FileStoreCommitImpl's
        // tryCommit and tryCommitOnce): each request is refused, then the client fails. The first
        // request fences the table; the others find it fenced.
        assertThatThrownBy(() -> Tables.write(catalog.getTable(TABLE), 2))
                .hasMessageContaining("with " + retries + " retries")
                .rootCause()
                .isInstanceOf(AlreadyExistsException.class)
                .hasMessageContaining("fenced and accepts no commits");
        assertThat(
                        count(
                                "SELECT count(*) FROM audit_events"
                                        + " WHERE action = 'COMMIT' AND http_status = 409"))
                .isEqualTo(retries + 1);
        assertThat(tableState()).isEqualTo("FENCED");
        assertThat(audit("FENCE_TABLE")).isEqualTo(1);
        // Readers still get the last snapshot the catalog published.
        assertThat(catalog.loadSnapshot(TABLE).get().snapshot().id()).isEqualTo(1);
    }

    @Test
    void writerOfADroppedTableCannotCommitToItsReplacement() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", "1");
        Tables.createAppendTable(catalog, TABLE, options);
        FileStoreTable stale = (FileStoreTable) catalog.getTable(TABLE);
        catalog.dropTable(TABLE, false);
        Tables.createAppendTable(catalog, TABLE, options);
        assertThatThrownBy(() -> Tables.write(stale, 1))
                .hasStackTraceContaining(Catalog.TableNotExistException.class.getSimpleName());
        assertThat(Tables.ids(catalog.getTable(TABLE))).isEmpty();
    }

    @Test
    void abandonedIntentIsClosedAfterItsTimeout() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", "0");
        Tables.createAppendTable(catalog, TABLE, options);
        failBeforePublish.set(1);
        assertThatThrownBy(() -> Tables.write(catalog.getTable(TABLE), 1)).isNotNull();
        assertThat(pendingIntents()).isEqualTo(1);
        clock.advance(Duration.ofMinutes(10));
        assertThat(catalog.loadSnapshot(TABLE)).isEmpty();
        assertThat(pendingIntents()).isZero();
        assertThat(intentResolutions()).contains("ABORTED:snapshot never published");
    }

    /**
     * Four writers commit ten ids each, writer {@code w} through {@code ports[w % ports.length]};
     * afterwards every id is there exactly once and every commit is a published intent.
     */
    private void writeConcurrently(int... ports) throws Exception {
        int writers = 4;
        int commitsPerWriter = 10;
        ExecutorService pool = Executors.newFixedThreadPool(writers);
        List<Future<?>> done = new ArrayList<>();
        for (int w = 0; w < writers; w++) {
            int writer = w;
            int port = ports[w % ports.length];
            done.add(
                    pool.submit(
                            () -> {
                                try (RESTCatalog own = catalog(adminToken, port, Map.of())) {
                                    for (int i = 0; i < commitsPerWriter; i++) {
                                        Tables.write(own.getTable(TABLE), writer * 1000 + i);
                                    }
                                }
                                return null;
                            }));
        }
        for (Future<?> f : done) {
            f.get();
        }
        pool.shutdown();
        FileStoreTable table = (FileStoreTable) catalog(adminToken).getTable(TABLE);
        List<Integer> expected =
                IntStream.range(0, writers)
                        .boxed()
                        .flatMap(
                                w ->
                                        IntStream.range(0, commitsPerWriter)
                                                .mapToObj(i -> w * 1000 + i))
                        .collect(Collectors.toList());
        assertThat(Tables.ids(table)).containsExactlyInAnyOrderElementsOf(expected);
        assertThat(table.snapshotManager().latestSnapshotId())
                .isEqualTo((long) writers * commitsPerWriter);
        assertThat(publishedIntents()).isEqualTo(writers * commitsPerWriter);
    }

    private static Snapshot copyWithId(Snapshot base, long id) {
        String json = base.toJson();
        Snapshot parsed = Snapshot.fromJson(json);
        String renamed =
                json.replaceFirst("\"id\"\\s*:\\s*" + parsed.id(), "\"id\" : " + id)
                        .replace(parsed.uuid(), java.util.UUID.randomUUID().toString());
        return Snapshot.fromJson(renamed);
    }

    private int publishedIntents() throws Exception {
        return count("SELECT count(*) FROM commit_intents WHERE state = 'PUBLISHED'");
    }

    private int pendingIntents() throws Exception {
        return count("SELECT count(*) FROM commit_intents WHERE state = 'PENDING'");
    }

    private int audit(String action) throws Exception {
        return count("SELECT count(*) FROM audit_events WHERE action = '" + action + "'");
    }

    private String tableState() throws Exception {
        return strings("SELECT state FROM catalog_tables").get(0);
    }

    private List<String> intentResolutions() throws Exception {
        return strings(
                "SELECT state || ':' || resolution FROM commit_intents WHERE resolution IS NOT NULL");
    }

    private int count(String sql) throws Exception {
        return Integer.parseInt(strings(sql).get(0));
    }

    /**
     * Fast retries, and enough of them for writers that contend for every slot: on a starved CPU
     * one of four writers can lose Paimon's default ten rounds in a row and give up, which is not a
     * lost commit. These tests check what was committed, so they let every writer get through.
     */
    private static Map<String, String> contended() {
        Map<String, String> options = new HashMap<>(FAST_RETRY);
        options.put("commit.max-retries", "100");
        return options;
    }

    /** Waits until a catalog transaction waits for a lock another one holds. */
    private void awaitLockWait() throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        while (count("SELECT count(*) FROM pg_locks WHERE NOT granted") == 0) {
            assertThat(System.nanoTime())
                    .as("a commit waiting for the branch lock")
                    .isLessThan(deadline);
            Thread.sleep(20);
        }
    }

    /** The audited commits of a commit user, in order: "<outcome> <commit kind>". */
    private List<String> commits(String commitUser) throws Exception {
        List<String> rows = new ArrayList<>();
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement select =
                        c.prepareStatement(
                                "SELECT outcome || ' ' || (detail->>'commitKind') FROM audit_events"
                                        + " WHERE action = 'COMMIT' AND detail->>'commitUser' = ?"
                                        + " ORDER BY id")) {
            select.setString(1, commitUser);
            try (ResultSet rs = select.executeQuery()) {
                while (rs.next()) {
                    rows.add(rs.getString(1));
                }
            }
        }
        return rows;
    }

    private static String stackTrace(Throwable failure) {
        StringWriter trace = new StringWriter();
        failure.printStackTrace(new PrintWriter(trace));
        return trace.toString();
    }

    /** What ended a commit run on the pool: null when it committed, else its cause. */
    private static Throwable failureOf(Future<?> commit) throws Exception {
        try {
            commit.get(60, TimeUnit.SECONDS);
            return null;
        } catch (ExecutionException e) {
            return e.getCause();
        }
    }

    private List<String> strings(String sql) throws Exception {
        List<String> rows = new ArrayList<>();
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement select = c.prepareStatement(sql);
                ResultSet rs = select.executeQuery()) {
            while (rs.next()) {
                rows.add(rs.getString(1));
            }
        }
        return rows;
    }
}
