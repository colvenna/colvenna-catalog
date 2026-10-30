package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicLong;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.types.DataTypes;

/**
 * The run's fixtures, made through the catalog with the owner's token: the databases {@code
 * databases}, each with {@code tables} unpartitioned append tables {@code t000, t001, ...}, and,
 * with {@code snapshot=true}, one commit of {@code rows} rows in each (so that each table has one
 * snapshot). Not under test: each table is tried again on a failure, up to six times. One record
 * per table ({@code seed.jsonl}).
 */
public final class Seed {

    private Seed() {}

    /** The append table every scenario writes and reads: synthetic rows only. */
    static Schema schema() {
        return Schema.newBuilder()
                .column("id", DataTypes.BIGINT())
                .column("writer", DataTypes.INT())
                .column("commit_no", DataTypes.BIGINT())
                .column("payload", DataTypes.STRING())
                .option("bucket", "-1")
                .build();
    }

    static String table(int i) {
        return String.format(Locale.ROOT, "t%03d", i);
    }

    static void run(Spec spec) throws Exception {
        List<String> databases = spec.list("databases");
        int tables = spec.integer("tables");
        boolean snapshot = spec.flag("snapshot", false);
        int rows = spec.integer("rows", 10);
        int threads = spec.integer("threads", 8);
        Records records = Records.open("seed");
        AtomicLong failed = new AtomicLong();
        try (Catalog catalog = Clients.catalog(spec.str("owner"))) {
            for (String db : databases) {
                retry(() -> catalog.createDatabase(db, true));
            }
            ExecutorService pool = Executors.newFixedThreadPool(threads);
            List<Future<?>> futures = new ArrayList<>();
            for (String db : databases) {
                for (int t = 0; t < tables; t++) {
                    Identifier id = Identifier.create(db, table(t));
                    futures.add(
                            pool.submit(
                                    () -> {
                                        long start = System.currentTimeMillis();
                                        int attempts = 0;
                                        String error = null;
                                        while (attempts < 6) {
                                            attempts++;
                                            try {
                                                catalog.createTable(id, schema(), true);
                                                if (snapshot) {
                                                    writeOnce(catalog, id, rows);
                                                }
                                                error = null;
                                                break;
                                            } catch (Exception e) {
                                                error = Errors.describe(e);
                                                sleep(1000L * attempts);
                                            }
                                        }
                                        if (error != null) {
                                            failed.incrementAndGet();
                                        }
                                        records.write(
                                                "db",
                                                id.getDatabaseName(),
                                                "table",
                                                id.getObjectName(),
                                                "ok",
                                                error == null,
                                                "attempts",
                                                attempts,
                                                "ms",
                                                System.currentTimeMillis() - start,
                                                "error",
                                                error);
                                    }));
                }
            }
            for (Future<?> f : futures) {
                f.get();
            }
            pool.shutdown();
        }
        Out.line(
                "seeded",
                "databases",
                databases.size(),
                "tables",
                records.count(),
                "failed",
                failed.get());
        if (failed.get() > 0) {
            throw new IllegalStateException(failed.get() + " tables could not be made");
        }
    }

    /** One commit of {@code rows} rows, unless the table has a snapshot already. */
    static void writeOnce(Catalog catalog, Identifier id, int rows) throws Exception {
        Table table = catalog.getTable(id);
        if (((FileStoreTable) table).latestSnapshot().isPresent()) {
            return;
        }
        BatchWriteBuilder builder = table.newBatchWriteBuilder();
        // The table's file access (its per-table credentials and their S3 client) is closed once
        // written: a thousand tables' clients would not fit the tool's heap.
        try (FileIO io = table.fileIO();
                BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            for (int r = 0; r < rows; r++) {
                write.write(GenericRow.of((long) r, 0, 0L, BinaryString.fromString("seed-" + r)));
            }
            commit.commit(write.prepareCommit());
        }
    }

    interface Step {
        void run() throws Exception;
    }

    static void retry(Step step) throws Exception {
        for (int attempt = 1; ; attempt++) {
            try {
                step.run();
                return;
            } catch (Exception e) {
                if (attempt >= 6) {
                    throw e;
                }
                Out.line("retried", "attempt", attempt, "error", Errors.describe(e));
                sleep(1000L * attempt);
            }
        }
    }

    static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
