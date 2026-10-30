package com.colvenna.catalog.uat.paimon;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.paimon.table.source.Split;
import org.apache.paimon.types.DataField;
import org.apache.paimon.utils.InternalRowUtils;
import org.apache.paimon.utils.SnapshotManager;

/**
 * A table's state as the catalog serves it, to compare before and after an upgrade or a restore
 * (Colvenna Lake's CatalogStateDump and SnapshotHistory): {@code state <db> <tables>} prints, per
 * table, one {@code state} line (latest and earliest snapshot, schema id, tags, branches, row count
 * and an order-independent checksum of the rows) and one {@code snapshot} line per snapshot (id,
 * commit kind, commit user, the rows its plan covers, and for the first 100 from the earliest, the
 * latest and every 25th the rows read at it and their checksum). A table the catalog does not list
 * is a {@code state} line with {@code exists: false}.
 */
public final class State {

    private State() {}

    static void run(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: state <db> <table,...>");
        }
        try (Catalog catalog = Catalogs.open()) {
            List<String> listed = catalog.listTables(args[0]);
            Out.line(
                    "listing",
                    "database",
                    args[0],
                    "tables",
                    new ArrayList<>(new TreeSet<>(listed)));
            for (String name : args[1].split(",", -1)) {
                Identifier id = Identifier.create(args[0], name);
                if (!listed.contains(name)) {
                    Out.line("state", "table", name, "exists", false);
                    continue;
                }
                try {
                    dump(catalog, id);
                } catch (Exception e) {
                    Out.line("state", "table", name, "exists", true, "error", Out.root(e));
                }
            }
        }
    }

    private static void dump(Catalog catalog, Identifier id) throws Exception {
        FileStoreTable table = (FileStoreTable) catalog.getTable(id);
        Snapshot latest = table.latestSnapshot().orElse(null);
        SnapshotManager snapshots = table.snapshotManager();
        Long earliest = snapshots.earliestSnapshotId();
        Map<String, Object> rows = read(table.newReadBuilder(), table.rowType().getFields());
        Out.line(
                "state",
                "table",
                id.getObjectName(),
                "exists",
                true,
                "latestSnapshot",
                latest == null ? null : latest.id(),
                "earliestSnapshot",
                earliest,
                "schemaId",
                table.schema().id(),
                "tags",
                new ArrayList<>(new TreeSet<>(table.tagManager().allTagNames())),
                "branches",
                new ArrayList<>(new TreeSet<>(table.branchManager().branches())),
                "rows",
                rows.get("rows"),
                "checksum",
                rows.get("checksum"));
        if (latest == null || earliest == null) {
            return;
        }
        // Every snapshot's plan (its manifests: what it holds); its rows read and summed only for
        // the
        // first READ_ALL from the earliest (a small table's every one), the latest and every 25th,
        // so
        // that a table of many snapshots is not read again and again (Colvenna Lake's
        // SnapshotHistory).
        ExecutorService pool = Executors.newFixedThreadPool(THREADS);
        try {
            List<Future<Map<String, Object>>> lines = new ArrayList<>();
            long first = earliest;
            long last = latest.id();
            for (long n = first; n <= last; n++) {
                long snapshotId = n;
                boolean read = n < first + READ_ALL || n == last || n % 25 == 0;
                lines.add(pool.submit(() -> snapshot(table, snapshots, snapshotId, read)));
            }
            for (Future<Map<String, Object>> line : lines) {
                Map<String, Object> m = line.get();
                Out.line("snapshot", flatten(id.getObjectName(), m));
            }
        } finally {
            pool.shutdownNow();
        }
    }

    private static final int THREADS = 8;
    private static final int READ_ALL = 100;

    private static Object[] flatten(String table, Map<String, Object> m) {
        List<Object> pairs = new ArrayList<>();
        pairs.add("table");
        pairs.add(table);
        m.forEach(
                (k, v) -> {
                    pairs.add(k);
                    pairs.add(v);
                });
        return pairs.toArray();
    }

    private static Map<String, Object> snapshot(
            FileStoreTable table, SnapshotManager snapshots, long n, boolean readRows)
            throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", n);
        if (!snapshots.snapshotExists(n)) {
            m.put("exists", false);
            return m;
        }
        Snapshot s = snapshots.snapshot(n);
        ReadBuilder read =
                table.copy(Map.of("scan.snapshot-id", Long.toString(n))).newReadBuilder();
        List<Split> splits = read.newScan().plan().splits();
        long planned = 0;
        for (Split split : splits) {
            planned += split.rowCount();
        }
        m.put("exists", true);
        m.put("commitKind", s.commitKind().name());
        m.put("commitUser", s.commitUser());
        m.put("plannedRows", planned);
        if (readRows) {
            Map<String, Object> rows = read(read, table.rowType().getFields());
            m.put("rows", rows.get("rows"));
            m.put("checksum", rows.get("checksum"));
        }
        return m;
    }

    /** The rows a read gives: their number and the sum of their 64-bit FNV-1a hashes. */
    static Map<String, Object> read(ReadBuilder read, List<DataField> fields) throws Exception {
        long rows = 0;
        long checksum = 0;
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan())) {
            RecordReader.RecordIterator<InternalRow> batch;
            while ((batch = reader.readBatch()) != null) {
                InternalRow row;
                while ((row = batch.next()) != null) {
                    rows++;
                    StringBuilder text = new StringBuilder();
                    for (int i = 0; i < fields.size(); i++) {
                        text.append(InternalRowUtils.get(row, i, fields.get(i).type()))
                                .append('\u0001');
                    }
                    checksum += fnv1a(text.toString());
                }
                batch.releaseBatch();
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("rows", rows);
        out.put("checksum", Long.toHexString(checksum));
        return out;
    }

    static long fnv1a(String text) {
        long hash = 0xcbf29ce484222325L;
        for (byte b : text.getBytes(StandardCharsets.UTF_8)) {
            hash ^= b & 0xff;
            hash *= 0x100000001b3L;
        }
        return hash;
    }
}
