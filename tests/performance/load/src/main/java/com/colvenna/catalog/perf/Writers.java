package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.CommitMessage;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;

/**
 * Closed-loop writers (terms.md P1a, P1b, and P5's writers): {@code writers} writers, each with a
 * catalog, a commit user ({@code perf-w<n>}) and a stream writer of its own, as a Flink sink holds
 * them, commit {@code rows} rows (one data file) at a time to the table {@code tables[n]}, or all
 * to {@code tables[0]} with {@code shared=true}.
 *
 * <p>With {@code interval=0} each writer commits back to back; otherwise commit k of writer n is
 * due at {@code start + n * interval / writers + k * interval}, and is made when the previous one
 * returned, or when it is due if that is later. No commit starts after {@code start + (warmup +
 * duration) s}. One record per commit ({@code <prefix>commits.jsonl}): its due and start times, the
 * time spent writing the rows and preparing the commit ({@code prepUs}), the time of the commit
 * call itself, the client's conflict retries and its own retries included ({@code commitUs}),
 * whether it was in the warm-up, and how it ended.
 */
public final class Writers {

    private Writers() {}

    static void run(Spec spec, String prefix) throws Exception {
        String key = spec.str("key");
        String database = spec.str("database");
        List<String> tables = spec.list("tables");
        int writers = spec.integer("writers");
        boolean shared = spec.flag("shared", false);
        long interval = spec.number("interval");
        int rows = spec.integer("rows", 100);
        long start = spec.start();
        long warmupEnd = start + spec.number("warmup") * 1000;
        long end = warmupEnd + spec.number("duration") * 1000;
        Records records = Records.open(prefix + "commits");
        ExecutorService pool = Executors.newFixedThreadPool(writers);
        List<Future<?>> futures = new ArrayList<>();
        for (int w = 0; w < writers; w++) {
            int writer = w;
            Identifier id = Identifier.create(database, tables.get(shared ? 0 : w));
            futures.add(
                    pool.submit(
                            () -> {
                                write(
                                        key, id, writer, writers, interval, rows, start, warmupEnd,
                                        end, records);
                                return null;
                            }));
        }
        long failed = 0;
        for (Future<?> f : futures) {
            try {
                f.get();
            } catch (Exception e) {
                failed++;
                Out.line("writer-failed", "error", Errors.describe(e));
            }
        }
        pool.shutdown();
        Out.line("written", "records", records.count(), "writersFailed", failed);
    }

    private static void write(
            String key,
            Identifier id,
            int writer,
            int writers,
            long interval,
            int rows,
            long start,
            long warmupEnd,
            long end,
            Records records)
            throws Exception {
        try (Catalog catalog = Clients.catalog(key)) {
            Table table = catalog.getTable(id);
            String user = "perf-w" + writer;
            StreamWriteBuilder builder = table.newStreamWriteBuilder().withCommitUser(user);
            StreamTableWrite write = builder.newWrite();
            StreamTableCommit commit = builder.newCommit();
            String thread = Thread.currentThread().getName();
            long offset = interval * writer / Math.max(1, writers);
            waitUntil(start + offset);
            try {
                for (long k = 0; ; k++) {
                    long due =
                            interval > 0
                                    ? start + offset + k * interval
                                    : System.currentTimeMillis();
                    if (due >= end || System.currentTimeMillis() >= end) {
                        break;
                    }
                    waitUntil(due);
                    long t1 = System.currentTimeMillis();
                    long n1 = System.nanoTime();
                    long n2 = n1;
                    String error = null;
                    String status = null;
                    try {
                        for (int r = 0; r < rows; r++) {
                            write.write(
                                    GenericRow.of(
                                            k * rows + r,
                                            writer,
                                            k,
                                            BinaryString.fromString("w" + writer + "-" + k)));
                        }
                        List<CommitMessage> messages = write.prepareCommit(false, k);
                        n2 = System.nanoTime();
                        commit.commit(k, messages);
                    } catch (Exception e) {
                        error = Errors.describe(e);
                        status = Errors.status(e);
                        // A failed commit's writer and committer are not reused.
                        closeQuietly(write, commit);
                        builder = table.newStreamWriteBuilder().withCommitUser(user);
                        write = builder.newWrite();
                        commit = builder.newCommit();
                    }
                    long n3 = System.nanoTime();
                    records.write(
                            "w",
                            writer,
                            "k",
                            k,
                            "table",
                            id.getFullName(),
                            "warmup",
                            due < warmupEnd,
                            "due",
                            due,
                            "start",
                            t1,
                            "lateUs",
                            (t1 - due) * 1000,
                            "prepUs",
                            (n2 - n1) / 1000,
                            "commitStart",
                            t1 + (n2 - n1) / 1_000_000,
                            "commitUs",
                            (n3 - n2) / 1000,
                            "wholeUs",
                            (n3 - n1) / 1000,
                            "ok",
                            error == null,
                            "status",
                            status,
                            "error",
                            error,
                            "thread",
                            thread);
                }
            } finally {
                closeQuietly(write, commit);
            }
        }
    }

    static void waitUntil(long epochMillis) throws InterruptedException {
        long wait = epochMillis - System.currentTimeMillis();
        if (wait > 0) {
            Thread.sleep(wait);
        }
    }

    private static void closeQuietly(AutoCloseable... closeables) {
        for (AutoCloseable c : closeables) {
            try {
                c.close();
            } catch (Exception e) {
                Out.line("close-failed", "error", Errors.describe(e));
            }
        }
    }
}
