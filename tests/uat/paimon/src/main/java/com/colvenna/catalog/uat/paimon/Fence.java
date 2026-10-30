package com.colvenna.catalog.uat.paimon;

import java.util.HashMap;
import java.util.Map;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.options.Options;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.FileStoreTableFactory;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.utils.JsonSerdeUtil;

/**
 * A table written outside the catalog (UAT-FLT-03):
 *
 * <ul>
 *   <li>{@code outside <db> <table>}: a writer that bypasses the catalog. With the S3 credentials
 *       given on stdin after the token (a JSON line: accessKeyId, secretAccessKey, sessionToken,
 *       region: the identity allowed to write the table's prefix directly), it commits one snapshot
 *       through a Paimon filesystem table on the table's path, which publishes {@code snapshot-N}
 *       itself ({@code outside}: the snapshot id it wrote);
 *   <li>{@code fenced-commit <db> <table>}: a commit through the catalog with {@value #RETRIES}
 *       client retries ({@code fenced-commit}: whether it failed, the client's own message, which
 *       says after how many retries, and its innermost cause, the catalog's answer);
 *   <li>{@code reads <db> <table>}: what the catalog serves, its latest snapshot and the rows a
 *       batch read gives ({@code reads}).
 * </ul>
 */
public final class Fence {

    /** The fenced commit's {@code commit.max-retries}: it makes this many more commit requests. */
    static final int RETRIES = 2;

    private Fence() {}

    static void run(String mode, String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: " + mode + " <db> <table>");
        }
        Identifier id = Identifier.create(args[0], args[1]);
        try (Catalog catalog = Catalogs.open()) {
            FileStoreTable table = (FileStoreTable) catalog.getTable(id);
            switch (mode) {
                case "outside":
                    outside(table);
                    break;
                case "fenced-commit":
                    fencedCommit(catalog, id);
                    break;
                case "reads":
                    Snapshot latest = table.latestSnapshot().orElse(null);
                    Out.line(
                            "reads",
                            "snapshot",
                            latest == null ? null : latest.id(),
                            "rows",
                            CommitStress.count(table.newReadBuilder()));
                    break;
                default:
                    throw new IllegalArgumentException(mode);
            }
        }
    }

    private static void outside(FileStoreTable table) throws Exception {
        String line = Catalogs.nextLine();
        if (line == null || line.isBlank()) {
            throw new IllegalArgumentException(
                    "outside: the S3 credentials come on stdin after the token");
        }
        @SuppressWarnings("unchecked")
        Map<String, String> given = JsonSerdeUtil.fromJson(line, Map.class);
        Map<String, String> options = new HashMap<>();
        options.put("fs.s3a.access.key", given.get("accessKeyId"));
        options.put("fs.s3a.secret.key", given.get("secretAccessKey"));
        options.put("fs.s3a.session.token", given.get("sessionToken"));
        options.put(
                "fs.s3a.aws.credentials.provider",
                "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider");
        options.put("fs.s3a.endpoint.region", given.get("region"));
        FileIO io = FileIO.get(table.location(), CatalogContext.create(Options.fromMap(options)));
        // A Paimon table on the same path with nothing of the catalog: its commits write
        // snapshot-N directly, as a writer still on a filesystem catalog would.
        FileStoreTable outside = FileStoreTableFactory.create(io, table.location());
        BatchWriteBuilder builder = outside.newBatchWriteBuilder();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            for (int r = 0; r < 5; r++) {
                write.write(
                        GenericRow.of(9_000_000L + r, -1, BinaryString.fromString("outside-" + r)));
            }
            commit.commit(write.prepareCommit());
        }
        Long written = outside.snapshotManager().latestSnapshotIdFromFileSystem();
        Out.line("outside", "snapshot", written);
    }

    private static void fencedCommit(Catalog catalog, Identifier id) throws Exception {
        FileStoreTable table =
                ((FileStoreTable) catalog.getTable(id))
                        .copy(Map.of("commit.max-retries", String.valueOf(RETRIES)));
        BatchWriteBuilder builder = table.newBatchWriteBuilder();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            write.write(GenericRow.of(8_000_000L, -2, BinaryString.fromString("fenced")));
            commit.commit(write.prepareCommit());
            Out.line("fenced-commit", "failed", false);
        } catch (Exception e) {
            Out.line(
                    "fenced-commit",
                    "failed",
                    true,
                    "retries",
                    RETRIES,
                    "message",
                    String.valueOf(e.getMessage()),
                    "error",
                    Out.root(e));
        }
    }
}
