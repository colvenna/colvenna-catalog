package com.colvenna.catalog.uat.paimon;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.types.DataTypes;

/**
 * Tables of a case's own, made and written through the catalog: {@code write <db> <table>
 * <append|pk> <commits> <rows>} makes the table if it is missing (and its database), then commits
 * that many times that many rows, one line per commit ({@code commit}: snapshot id). The rows are
 * synthetic: a sequence number, a commit number and a short text.
 */
public final class Tables {

    private Tables() {}

    static Schema schema(String kind) {
        Schema.Builder b = Schema.newBuilder();
        if (kind.equals("pk")) {
            return b.column("id", DataTypes.BIGINT().notNull())
                    .column("commit_no", DataTypes.BIGINT())
                    .column("payload", DataTypes.STRING())
                    .primaryKey("id")
                    .option("bucket", "2")
                    .build();
        }
        // The loops' writer (Loops) writes this shape too: n, r, payload.
        return b.column("id", DataTypes.BIGINT())
                .column("commit_no", DataTypes.INT())
                .column("payload", DataTypes.STRING())
                .option("bucket", "-1")
                .build();
    }

    static void run(String[] args) throws Exception {
        if (args.length != 5) {
            throw new IllegalArgumentException(
                    "usage: write <db> <table> <append|pk> <commits> <rows>");
        }
        Identifier id = Identifier.create(args[0], args[1]);
        String kind = args[2];
        int commits = Integer.parseInt(args[3]);
        int rows = Integer.parseInt(args[4]);
        try (Catalog catalog = Catalogs.open()) {
            catalog.createDatabase(args[0], true);
            catalog.createTable(id, schema(kind), true);
            List<Long> made = write(catalog, id, kind, commits, rows);
            Out.line("written", "table", id.getFullName(), "snapshots", made);
        }
    }

    /** Commits {@code commits} times {@code rows} rows; returns the snapshot ids made. */
    static List<Long> write(Catalog catalog, Identifier id, String kind, int commits, int rows)
            throws Exception {
        Table table = catalog.getTable(id);
        List<Long> made = new ArrayList<>();
        long base = System.currentTimeMillis() % 1_000_000L * 1000L;
        for (int c = 0; c < commits; c++) {
            BatchWriteBuilder builder = table.newBatchWriteBuilder();
            try (BatchTableWrite write = builder.newWrite();
                    BatchTableCommit commit = builder.newCommit()) {
                for (int r = 0; r < rows; r++) {
                    long seq = base + (long) c * rows + r;
                    String text = String.format(Locale.ROOT, "c%d-r%d", c, r);
                    write.write(
                            kind.equals("pk")
                                    ? GenericRow.of(seq, (long) c, BinaryString.fromString(text))
                                    : GenericRow.of(seq, c, BinaryString.fromString(text)));
                }
                commit.commit(write.prepareCommit());
            }
            Snapshot latest = ((FileStoreTable) catalog.getTable(id)).latestSnapshot().orElse(null);
            long sid = latest == null ? -1 : latest.id();
            made.add(sid);
            Out.line("commit", "table", id.getFullName(), "snapshot", sid);
        }
        return made;
    }
}
