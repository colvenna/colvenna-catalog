package com.colvenna.catalog.uat.paimon;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.rest.RESTTokenFileIO;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.sink.CommitMessage;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.paimon.table.source.Split;

/**
 * The loops that run through an upgrade or a rollback (UAT-DEP-06, 07), each until the file {@code
 * /work/stop} exists or its seconds are over, one line per try:
 *
 * <ul>
 *   <li>{@code writer <db> <table> <every-ms> <seconds>}: commits 10 rows to an append table, each
 *       commit numbered ({@code write}: ok, snapshot id, the error after the client's own retries);
 *   <li>{@code reader <db> <tables> <every-ms> <seconds>}: loads each table (comma-separated) and
 *       reads its latest snapshot: what that snapshot added ({@code read}: ok, snapshot id, the
 *       rows planned and read);
 *   <li>{@code credentials <db> <tables> <every-ms> <seconds>}: for each table, new credentials
 *       from the catalog ({@code /token}, through a table object of its own) and a read with them
 *       of what the latest snapshot added ({@code token}: ok, the rows planned and read).
 * </ul>
 */
public final class Loops {

    private static final Path STOP = Path.of("/work/stop");

    private Loops() {}

    static void run(String mode, String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException(
                    "usage: " + mode + " <db> <table(s)> <every-ms> <seconds>");
        }
        String db = args[0];
        List<String> tables = List.of(args[1].split(",", -1));
        long every = Long.parseLong(args[2]);
        Instant end = Instant.now().plusSeconds(Long.parseLong(args[3]));
        try (Catalog catalog = Catalogs.open()) {
            switch (mode) {
                case "writer":
                    writer(catalog, Identifier.create(db, tables.get(0)), every, end);
                    break;
                case "reader":
                    reader(catalog, db, tables, every, end);
                    break;
                case "credentials":
                    credentials(catalog, db, tables, every, end);
                    break;
                default:
                    throw new IllegalArgumentException(mode);
            }
        }
    }

    private static boolean going(Instant end) {
        return Instant.now().isBefore(end) && !Files.exists(STOP);
    }

    private static void writer(Catalog catalog, Identifier id, long every, Instant end)
            throws Exception {
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        StreamWriteBuilder builder =
                table.newStreamWriteBuilder().withCommitUser("uat-loop-writer");
        long n = 0;
        try (StreamTableWrite write = builder.newWrite();
                StreamTableCommit commit = builder.newCommit()) {
            while (going(end)) {
                long began = System.currentTimeMillis();
                n++;
                for (int r = 0; r < 10; r++) {
                    write.write(
                            GenericRow.of(n, r, BinaryString.fromString("loop-" + n + "-" + r)));
                }
                List<CommitMessage> messages = write.prepareCommit(false, n);
                try {
                    commit.commit(n, messages);
                    Snapshot latest =
                            table.snapshotManager()
                                    .latestSnapshotOfUser("uat-loop-writer")
                                    .orElse(null);
                    Out.line(
                            "write",
                            "number",
                            n,
                            "ok",
                            true,
                            "snapshot",
                            latest == null ? null : latest.id(),
                            "ms",
                            System.currentTimeMillis() - began);
                } catch (Exception e) {
                    // After the client's own retries: the commit's outcome is not known; the next
                    // round finds out whether it landed (filterAndCommit skips it if it did).
                    Out.line("write", "number", n, "ok", false, "error", Out.root(e));
                    try {
                        Map<Long, List<CommitMessage>> retry = new HashMap<>();
                        retry.put(n, messages);
                        int applied = commit.filterAndCommit(retry);
                        Out.line("write-resolved", "number", n, "committedNow", applied);
                    } catch (Exception again) {
                        Out.line("write-resolved", "number", n, "error", Out.root(again));
                    }
                }
                Thread.sleep(Math.max(0, every - (System.currentTimeMillis() - began)));
            }
        }
        Out.line("writer-done", "commits", n);
    }

    private static void reader(
            Catalog catalog, String db, List<String> tables, long every, Instant end)
            throws Exception {
        while (going(end)) {
            long began = System.currentTimeMillis();
            for (String name : tables) {
                try {
                    FileStoreTable table =
                            (FileStoreTable) catalog.getTable(Identifier.create(db, name));
                    Snapshot latest = table.latestSnapshot().orElse(null);
                    long[] read = latestDelta(table);
                    Out.line(
                            "read",
                            "table",
                            name,
                            "ok",
                            true,
                            "snapshot",
                            latest == null ? null : latest.id(),
                            "plannedRows",
                            read[0],
                            "rowsRead",
                            read[1]);
                } catch (Exception e) {
                    Out.line("read", "table", name, "ok", false, "error", Out.root(e));
                }
            }
            Thread.sleep(Math.max(0, every - (System.currentTimeMillis() - began)));
        }
    }

    private static void credentials(
            Catalog catalog, String db, List<String> tables, long every, Instant end)
            throws Exception {
        while (going(end)) {
            long began = System.currentTimeMillis();
            for (String name : tables) {
                try {
                    // A table object of its own: its FileIO has no credentials yet, so it asks the
                    // catalog for them (GET .../token) and reads the table's files with them.
                    FileStoreTable table =
                            (FileStoreTable) catalog.getTable(Identifier.create(db, name));
                    FileIO io = table.fileIO();
                    String expires = null;
                    if (io instanceof RESTTokenFileIO) {
                        expires =
                                String.valueOf(
                                        ((RESTTokenFileIO) io).validToken().expireAtMillis());
                    }
                    // What the latest snapshot added, as the reader reads: a whole-table read
                    // grows with the table, and the loop would slow down as it runs.
                    long[] read = latestDelta(table);
                    Out.line(
                            "token",
                            "table",
                            name,
                            "ok",
                            true,
                            "plannedRows",
                            read[0],
                            "rowsRead",
                            read[1],
                            "expiresAtMillis",
                            expires);
                } catch (Exception e) {
                    Out.line("token", "table", name, "ok", false, "error", Out.root(e));
                }
            }
            Thread.sleep(Math.max(0, every - (System.currentTimeMillis() - began)));
        }
    }

    /**
     * Reads what the table's latest snapshot added: its delta alone (Paimon's {@code
     * incremental-between} the snapshot before and it), so that the cost of a try does not grow
     * with the table; the table's first snapshot is read whole. Returns the rows planned and read
     * (one split's at most).
     */
    static long[] latestDelta(FileStoreTable table) throws Exception {
        Snapshot latest = table.latestSnapshot().orElse(null);
        if (latest == null) {
            return new long[] {0, 0};
        }
        Long earliest = table.snapshotManager().earliestSnapshotId();
        FileStoreTable at =
                earliest != null && latest.id() > earliest
                        ? table.copy(
                                Map.of(
                                        "incremental-between",
                                        (latest.id() - 1) + "," + latest.id()))
                        : table.copy(Map.of("scan.snapshot-id", String.valueOf(latest.id())));
        ReadBuilder read = at.newReadBuilder();
        List<Split> splits = read.newScan().plan().splits();
        long planned = 0;
        for (Split split : splits) {
            planned += split.rowCount();
        }
        long rows = 0;
        if (!splits.isEmpty()) {
            try (RecordReader<InternalRow> reader = read.newRead().createReader(splits.get(0))) {
                RecordReader.RecordIterator<InternalRow> batch;
                while ((batch = reader.readBatch()) != null) {
                    while (batch.next() != null) {
                        rows++;
                    }
                    batch.releaseBatch();
                }
            }
        }
        return new long[] {planned, rows};
    }
}
