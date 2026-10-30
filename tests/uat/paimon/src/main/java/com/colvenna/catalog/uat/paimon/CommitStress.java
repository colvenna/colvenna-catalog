package com.colvenna.catalog.uat.paimon;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.rest.exceptions.RESTException;
import org.apache.paimon.rest.exceptions.ServiceFailureException;
import org.apache.paimon.rest.exceptions.ServiceUnavailableException;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.sink.CommitMessage;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.paimon.types.DataTypes;

/**
 * Concurrent writers commit to one append table through the catalog while the harness injects
 * faults; every commit's final state is then determined and checked (Colvenna Lake's CommitStress,
 * for UAT-FLT-01 and 02).
 *
 * <ul>
 *   <li>Each writer has its own commit user ({@code stress-w<n>}) and numbers its commits; a commit
 *       that fails is retried with {@code filterAndCommit}, which commits it only if no snapshot of
 *       that user and number exists, so a retry never duplicates rows.
 *   <li>A reader keeps loading the latest snapshot and checks it shows whole commits only.
 *   <li>At the end every acknowledged commit of this process's writers must be present exactly
 *       once, every outcome known, and the table's snapshot ids contiguous.
 * </ul>
 *
 * <p>Several processes may write the same table, each with writers of its own ({@code
 * first-writer}): each checks its own writers' commits. Arguments: database, table, first writer,
 * writers, commits per writer, rows per commit, pause in ms between commits.
 */
public final class CommitStress {

    /** How one commit ended, as the writer saw it and after resolution. */
    enum Outcome {
        ACKED,
        FAILED_THEN_FOUND_COMMITTED,
        FAILED_THEN_RETRIED,
        UNRESOLVED
    }

    /** One commit's final state. Not a record: the tools target the Flink image's Java 11. */
    static final class Commit {
        final int writer;
        final long number;
        final Outcome outcome;
        final int attempts;
        final String error;

        Commit(int writer, long number, Outcome outcome, int attempts, String error) {
            this.writer = writer;
            this.number = number;
            this.outcome = outcome;
            this.attempts = attempts;
            this.error = error;
        }
    }

    private static final int MAX_ATTEMPTS = 30;

    private final Catalog catalog;
    private final Identifier id;
    private final int rows;
    private final long pauseMillis;
    private final Map<String, Commit> commits = new ConcurrentHashMap<>();
    private final AtomicBoolean writing = new AtomicBoolean(true);
    private final AtomicLong readerChecks = new AtomicLong();
    private final List<String> readerViolations = Collections.synchronizedList(new ArrayList<>());
    private final AtomicLong readerOutages = new AtomicLong();

    private CommitStress(Catalog catalog, Identifier id, int rows, long pauseMillis) {
        this.catalog = catalog;
        this.id = id;
        this.rows = rows;
        this.pauseMillis = pauseMillis;
    }

    static void run(String[] args) throws Exception {
        if (args.length != 7) {
            throw new IllegalArgumentException(
                    "usage: stress <database> <table> <first-writer> <writers> <commits> <rows> <pause-ms>");
        }
        Identifier id = Identifier.create(args[0], args[1]);
        int first = Integer.parseInt(args[2]);
        int writers = Integer.parseInt(args[3]);
        int perWriter = Integer.parseInt(args[4]);
        int rows = Integer.parseInt(args[5]);
        long pause = Long.parseLong(args[6]);
        try (Catalog catalog = Catalogs.open()) {
            prepare(catalog, id);
            new CommitStress(catalog, id, rows, pause).run(first, writers, perWriter);
        }
    }

    /**
     * The database and table, made if missing: the case's preparation, not under test, so tried
     * again while the catalog answers as during a fault (many writer pods start at once, over a
     * serverless install's concurrency).
     */
    private static void prepare(Catalog catalog, Identifier id) throws Exception {
        for (int attempt = 1; ; attempt++) {
            try {
                catalog.createDatabase(id.getDatabaseName(), true);
                catalog.createTable(id, schema(), true);
                return;
            } catch (RuntimeException e) {
                if (attempt >= 10
                        || (!catalogOutage(e) && !(e.getCause() instanceof RESTException))) {
                    throw e;
                }
                Out.line("prepare-retried", "attempt", attempt, "error", Out.root(e));
                Thread.sleep(2_000L * attempt);
            }
        }
    }

    static Schema schema() {
        return Schema.newBuilder()
                .column("writer", DataTypes.INT())
                .column("commit_no", DataTypes.BIGINT())
                .column("row_no", DataTypes.INT())
                .column("payload", DataTypes.STRING())
                .option("bucket", "-1")
                .option("write-only", "true")
                .build();
    }

    private void run(int first, int writers, int perWriter) throws Exception {
        Instant start = Instant.now();
        // Daemon threads: a failed writer must not leave the JVM waiting on the others.
        ExecutorService pool =
                Executors.newFixedThreadPool(
                        writers + 1,
                        r -> {
                            Thread t = new Thread(r);
                            t.setDaemon(true);
                            return t;
                        });
        try {
            Future<?> reader = pool.submit(this::readLoop);
            List<Future<?>> futures = new ArrayList<>();
            for (int w = first; w < first + writers; w++) {
                int writer = w;
                futures.add(pool.submit(() -> write(writer, perWriter)));
            }
            try {
                for (Future<?> f : futures) {
                    f.get();
                }
            } finally {
                writing.set(false);
            }
            reader.get();
        } finally {
            pool.shutdownNow();
        }
        Duration took = Duration.between(start, Instant.now());
        Map<String, Integer> outcomes = new TreeMap<>();
        commits.values().forEach(c -> outcomes.merge(c.outcome.name(), 1, Integer::sum));
        for (Commit c : commits.values()) {
            if (c.outcome != Outcome.ACKED) {
                Out.line(
                        "retried",
                        "writer",
                        c.writer,
                        "number",
                        c.number,
                        "outcome",
                        c.outcome.name(),
                        "attempts",
                        c.attempts,
                        "error",
                        c.error);
            }
        }
        // Every commit of this process's writers must be present exactly once, with all its rows.
        Map<String, Integer> present = rowsPerCommit(first, writers);
        List<String> wrong = check(commits, present, rows);
        int unresolved = outcomes.getOrDefault(Outcome.UNRESOLVED.name(), 0);
        Out.check(
                "every-commit-once",
                wrong.isEmpty(),
                commits.size()
                        + " commits of writers "
                        + first
                        + ".."
                        + (first + writers - 1)
                        + ", "
                        + wrong.size()
                        + " mismatches "
                        + head(wrong));
        Out.check(
                "every-outcome-known",
                unresolved == 0,
                unresolved + " unresolved, outcomes " + outcomes);
        Out.check(
                "readers-see-whole-commits",
                readerViolations.isEmpty() && readerChecks.get() > 0,
                readerChecks.get()
                        + " reads ("
                        + readerOutages.get()
                        + " during outages), violations "
                        + head(readerViolations));
        List<Long> gaps = snapshotGaps();
        Out.check("snapshot-ids-contiguous", gaps.isEmpty(), "gaps " + gaps);
        Snapshot latest = latest();
        Out.line(
                "result",
                "commits",
                commits.size(),
                "rowsPerCommit",
                rows,
                "outcomes",
                outcomes,
                "readerChecks",
                readerChecks.get(),
                "readerOutages",
                readerOutages.get(),
                "seconds",
                took.toSeconds(),
                "latestSnapshot",
                latest == null ? null : latest.id());
    }

    /** The mismatches between the commits made and the rows present, per commit (writer/number). */
    static List<String> check(Map<String, Commit> commits, Map<String, Integer> present, int rows) {
        List<String> wrong = new ArrayList<>();
        for (Commit c : commits.values()) {
            int seen = present.getOrDefault(key(c.writer, c.number), 0);
            if (c.outcome != Outcome.UNRESOLVED && seen != rows) {
                wrong.add(key(c.writer, c.number) + " " + c.outcome + " has " + seen);
            }
        }
        for (Map.Entry<String, Integer> e : present.entrySet()) {
            if (!commits.containsKey(e.getKey())) {
                wrong.add(e.getKey() + " present but never written");
            } else if (e.getValue() != rows) {
                wrong.add(e.getKey() + " has " + e.getValue() + " rows, expected " + rows);
            }
        }
        Collections.sort(wrong);
        return wrong;
    }

    private void write(int writer, int count) {
        try {
            FileStoreTable table = (FileStoreTable) catalog.getTable(id);
            StreamWriteBuilder builder =
                    table.newStreamWriteBuilder().withCommitUser("stress-w" + writer);
            try (StreamTableWrite write = builder.newWrite();
                    StreamTableCommit commit = builder.newCommit()) {
                for (long n = 1; n <= count; n++) {
                    for (int r = 0; r < rows; r++) {
                        write.write(
                                GenericRow.of(
                                        writer,
                                        n,
                                        r,
                                        BinaryString.fromString(
                                                String.format(
                                                        Locale.ROOT,
                                                        "w%d-c%d-r%d",
                                                        writer,
                                                        n,
                                                        r))));
                    }
                    List<CommitMessage> messages = write.prepareCommit(false, n);
                    commits.put(key(writer, n), commitOnce(writer, n, commit, messages));
                    if (n % 10 == 0) {
                        Out.line("progress", "writer", writer, "commits", n);
                    }
                    Thread.sleep(pauseMillis);
                }
            }
        } catch (Exception e) {
            throw new RuntimeException("writer " + writer + " stopped", e);
        }
    }

    private Commit commitOnce(
            int writer, long n, StreamTableCommit commit, List<CommitMessage> messages)
            throws InterruptedException {
        try {
            commit.commit(n, messages);
            return new Commit(writer, n, Outcome.ACKED, 1, null);
        } catch (Exception first) {
            String error = Out.root(first);
            for (int attempt = 2; attempt <= MAX_ATTEMPTS; attempt++) {
                Thread.sleep(Math.min(10_000, 500L * attempt));
                try {
                    Map<Long, List<CommitMessage>> retry = new HashMap<>();
                    retry.put(n, messages);
                    int committed = commit.filterAndCommit(retry);
                    return new Commit(
                            writer,
                            n,
                            committed == 0
                                    ? Outcome.FAILED_THEN_FOUND_COMMITTED
                                    : Outcome.FAILED_THEN_RETRIED,
                            attempt,
                            error);
                } catch (Exception again) {
                    error = Out.root(again);
                }
            }
            return new Commit(writer, n, Outcome.UNRESOLVED, MAX_ATTEMPTS, error);
        }
    }

    /**
     * Whole commits only: a snapshot's rows are a multiple of the rows per commit, and never fewer
     * than an earlier read saw. Reads alternate between the catalog's latest snapshot and the
     * highest {@code snapshot-N} file, which is how a streaming reader finds new snapshots. A
     * failed read counts as a violation unless the catalog itself was unavailable, as during a
     * fault.
     */
    private void readLoop() {
        long[] lastRows = new long[2];
        int source = 0;
        while (writing.get()) {
            try {
                FileStoreTable table = (FileStoreTable) catalog.getTable(id);
                Long n =
                        source == 0
                                ? table.latestSnapshot().map(Snapshot::id).orElse(null)
                                : table.snapshotManager().latestSnapshotIdFromFileSystem();
                if (n != null) {
                    long count =
                            count(
                                    table.copy(Map.of("scan.snapshot-id", String.valueOf(n)))
                                            .newReadBuilder());
                    readerChecks.incrementAndGet();
                    if (count % rows != 0 || count < lastRows[source]) {
                        readerViolations.add(
                                "snapshot "
                                        + n
                                        + (source == 0 ? " (catalog)" : " (files)")
                                        + " shows "
                                        + count
                                        + " rows");
                    }
                    lastRows[source] = Math.max(lastRows[source], count);
                }
                source = 1 - source;
                Thread.sleep(2_000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception e) {
                if (catalogOutage(e)) {
                    readerOutages.incrementAndGet();
                } else {
                    readerViolations.add("read failed: " + Out.root(e));
                }
                sleepQuietly();
            }
        }
    }

    /** The catalog unreachable or answering 5xx, as during a fault; nothing else excuses a read. */
    @SuppressWarnings("ReferenceEquality") // a cause that is itself ends the chain
    static boolean catalogOutage(Throwable e) {
        for (Throwable t = e; t != null; t = t.getCause() == t ? null : t.getCause()) {
            if (t instanceof ServiceFailureException || t instanceof ServiceUnavailableException) {
                return true;
            }
            // How the Paimon REST client reports a connection it could not make or finish, and an
            // error answer without the catalog's body: from what is in front of it (the serverless
            // form's gateway answering 500 for a throttled or failed function, say).
            String message = String.valueOf(t.getMessage());
            if (t.getClass() == RESTException.class
                    && (message.startsWith("Error occurred while processing")
                            || message.startsWith("Unable to process"))) {
                return true;
            }
        }
        return false;
    }

    private Map<String, Integer> rowsPerCommit(int first, int writers) throws Exception {
        Map<String, Integer> counts = new HashMap<>();
        ReadBuilder read = catalog.getTable(id).newReadBuilder();
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan())) {
            reader.forEachRemaining(
                    row -> {
                        int w = row.getInt(0);
                        if (w >= first && w < first + writers) {
                            counts.merge(key(w, row.getLong(1)), 1, Integer::sum);
                        }
                    });
        }
        return counts;
    }

    static long count(ReadBuilder read) throws Exception {
        AtomicLong n = new AtomicLong();
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan())) {
            reader.forEachRemaining(row -> n.incrementAndGet());
        }
        return n.get();
    }

    private List<Long> snapshotGaps() throws Exception {
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        Long earliest = table.snapshotManager().earliestSnapshotId();
        Snapshot latest = latest();
        List<Long> gaps = new ArrayList<>();
        if (earliest == null || latest == null) {
            return gaps;
        }
        for (long s = earliest; s <= latest.id(); s++) {
            if (!table.snapshotManager().snapshotExists(s)) {
                gaps.add(s);
            }
        }
        // The maintenance loop expires the oldest snapshots meanwhile: what it expired since the
        // first look is no gap.
        Long after = table.snapshotManager().earliestSnapshotId();
        if (after != null) {
            gaps.removeIf(s -> s < after);
        }
        return gaps;
    }

    private Snapshot latest() {
        try {
            return ((FileStoreTable) catalog.getTable(id)).latestSnapshot().orElse(null);
        } catch (Catalog.TableNotExistException e) {
            throw new IllegalStateException(e);
        }
    }

    static String key(int writer, long number) {
        return writer + "/" + number;
    }

    private static String head(List<String> items) {
        return items.size() <= 5 ? items.toString() : items.subList(0, 5) + "...";
    }

    private static void sleepQuietly() {
        try {
            Thread.sleep(2_000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
