package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.fs.FileStatus;
import org.apache.paimon.io.DataFileMeta;
import org.apache.paimon.operation.LocalOrphanFilesClean;
import org.apache.paimon.options.Options;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.requests.RollbackTableRequest;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.table.sink.StreamTableCommit;
import org.apache.paimon.table.sink.StreamTableWrite;
import org.apache.paimon.table.sink.StreamWriteBuilder;
import org.apache.paimon.table.source.DataSplit;
import org.apache.paimon.table.source.Split;
import org.apache.paimon.types.DataTypes;

/**
 * The live check of #93: a table with many files, then a rollback, a tag's deletion and a branch's
 * deletion, each timed, and after the purge function's run every remaining snapshot, tag and branch
 * read in full, with Paimon's own orphan file cleaning finding nothing left unreferenced.
 *
 * <p>The table {@code <database>.t} is partitioned by {@code p} (4 values), so each commit adds 4
 * data files, and keeps at most {@link #RETAINED} snapshots (see {@link Size}). Main's history:
 * commits 1 to 200 add rows; tag {@code old} holds snapshot 200; commit 201 overwrites every
 * partition; commits 202 to {@link #MAIN} add rows, so that snapshots 1 to {@code MAIN - RETAINED}
 * expire and tag {@code old} alone keeps their files. Tags {@code keep} (330), {@code forc} (380)
 * and {@code forb} (400) are made as main passes them; branches {@code c} (from {@code forc}) and
 * {@code b} (from {@code forb}, with {@link #BRANCH} commits of its own) follow. Snapshot {@code k}
 * of main holds the rows of commits 201 to {@code k}: {@code 4 * (k - 200)} rows.
 *
 * <p>Phases (the event's {@code purges.phase}):
 *
 * <ul>
 *   <li>{@code setup}: makes the table or goes on making it, for at most {@code seconds}; answers
 *       {@code done} when the history above is complete (the host invokes it again until then);
 *   <li>{@code ops}: rolls main back to {@link #ROLLBACK_TO}, deletes tag {@code old} and branch
 *       {@code b}, each one plain HTTPS request, timed, with the files counted before and after;
 *   <li>{@code verify}: reads every snapshot and tag of every branch left and checks its rows, and
 *       runs Paimon's orphan file cleaning as a dry run.
 * </ul>
 */
final class Purges {

    static final int MAIN = 1100;
    static final int RETAINED = 800;
    static final int BRANCH = 200;
    static final long ROLLBACK_TO = 350;

    /**
     * The history's size: {@link #MAIN}, {@link #RETAINED} and {@link #BRANCH} unless the event's
     * {@code main}, {@code retained} and {@code branch} say otherwise (a small run to try the
     * driver: main at least 400, and main less retained below {@link #ROLLBACK_TO}).
     */
    private record Size(int main, int retained, int branch) {
        static Size of(JsonNode purges) {
            Size size =
                    new Size(
                            purges.path("main").asInt(MAIN),
                            purges.path("retained").asInt(RETAINED),
                            purges.path("branch").asInt(BRANCH));
            if (size.main < 400 || size.main - size.retained >= ROLLBACK_TO || size.branch < 1) {
                throw new IllegalArgumentException("Unworkable size " + size);
            }
            return size;
        }
    }

    private Purges() {}

    static Map<String, Object> run(String uri, String warehouse, String token, JsonNode event)
            throws Exception {
        JsonNode purges = event.path("purges");
        String database = purges.path("database").asText("");
        if (database.isEmpty()) {
            throw new IllegalArgumentException("The event needs purges.database");
        }
        Options options = new Options();
        options.setString("uri", uri);
        options.setString("warehouse", warehouse);
        options.setString("token.provider", "bear");
        options.setString("token", token);
        options.setString("data-token.enabled", "true");
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("database", database);
        try (RESTCatalog catalog = new RESTCatalog(CatalogContext.create(options))) {
            Identifier t = Identifier.create(database, "t");
            switch (purges.path("phase").asText()) {
                case "setup" ->
                        out.putAll(
                                setup(
                                        catalog,
                                        t,
                                        Size.of(purges),
                                        purges.path("seconds").asInt(780)));
                case "ops" -> out.putAll(ops(catalog, new Api(uri), warehouse, token, t));
                case "verify" -> out.putAll(verify(catalog, t));
                default ->
                        throw new IllegalArgumentException("purges.phase is setup, ops or verify");
            }
        }
        return out;
    }

    // ------------------------------------------------------------------ setup

    private static Map<String, Object> setup(
            RESTCatalog catalog, Identifier t, Size size, int seconds) throws Exception {
        long stop = System.nanoTime() + seconds * 1_000_000_000L;
        Map<String, Object> out = new LinkedHashMap<>();
        if (!catalog.listDatabases().contains(t.getDatabaseName())) {
            catalog.createDatabase(t.getDatabaseName(), false);
        }
        if (!catalog.listTables(t.getDatabaseName()).contains(t.getTableName())) {
            catalog.createTable(
                    t,
                    Schema.newBuilder()
                            .column("id", DataTypes.INT())
                            .column("p", DataTypes.INT())
                            .column("payload", DataTypes.STRING())
                            .partitionKeys("p")
                            .option("snapshot.num-retained.min", "10")
                            .option("snapshot.num-retained.max", String.valueOf(size.retained()))
                            .build(),
                    false);
        }
        long started = System.nanoTime();
        int commits = 0;
        // Main first, the tags and branches as it passes them, then branch b's own commits.
        while (System.nanoTime() < stop) {
            long latest = latest(catalog.getTable(t));
            if (latest >= size.main()) {
                break;
            }
            if (latest == 200 && !tagExists(catalog, t, "old")) {
                catalog.createTag(t, "old", 200L, null, false);
            }
            if (latest == 200) {
                overwrite(catalog.getTable(t), 201);
                commits++;
                continue;
            }
            long target = latest < 200 ? 200 : stopAt(latest, size.main());
            commits += append(catalog.getTable(t), latest + 1, target, stop);
            long now = latest(catalog.getTable(t));
            if (now == 330 && !tagExists(catalog, t, "keep")) {
                catalog.createTag(t, "keep", 330L, null, false);
            }
            if (now == 380 && !tagExists(catalog, t, "forc")) {
                catalog.createTag(t, "forc", 380L, null, false);
                catalog.createBranch(t, "c", "forc");
            }
            if (now == 400 && !tagExists(catalog, t, "forb")) {
                catalog.createTag(t, "forb", 400L, null, false);
                catalog.createBranch(t, "b", "forb");
            }
        }
        Identifier b = new Identifier(t.getDatabaseName(), t.getTableName(), "b");
        if (latest(catalog.getTable(t)) >= size.main()) {
            long branchLatest = latest(catalog.getTable(b));
            if (branchLatest < 400 + size.branch() && System.nanoTime() < stop) {
                commits += append(catalog.getTable(b), branchLatest + 1, 400 + size.branch(), stop);
            }
        }
        long mainLatest = latest(catalog.getTable(t));
        long branchLatest = mainLatest >= 400 ? latest(catalog.getTable(b)) : 0;
        out.put("commits", commits);
        out.put("seconds", (System.nanoTime() - started) / 1_000_000_000L);
        out.put("mainLatest", mainLatest);
        out.put("branchLatest", branchLatest);
        out.put("done", mainLatest >= size.main() && branchLatest >= 400 + size.branch());
        return out;
    }

    /** Where main's appends stop next: at a tag's or branch's snapshot, or at the end. */
    private static long stopAt(long latest, long main) {
        for (long mark : new long[] {330, 380, 400, main}) {
            if (latest < mark) {
                return mark;
            }
        }
        return main;
    }

    /** Commits {@code from} to {@code to} (snapshot ids, one commit each), until {@code stop}. */
    private static int append(Table table, long from, long to, long stop) throws Exception {
        StreamWriteBuilder builder = table.newStreamWriteBuilder();
        int done = 0;
        try (StreamTableWrite write = builder.newWrite();
                StreamTableCommit commit = builder.newCommit()) {
            for (long k = from; k <= to && System.nanoTime() < stop; k++) {
                for (int p = 0; p < 4; p++) {
                    int id = (int) (k * 4 + p);
                    write.write(GenericRow.of(id, p, BinaryString.fromString("r" + id)));
                }
                commit.commit(k, write.prepareCommit(false, k));
                done++;
            }
        }
        return done;
    }

    private static void overwrite(Table table, long k) throws Exception {
        BatchWriteBuilder builder = table.newBatchWriteBuilder().withOverwrite();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            for (int p = 0; p < 4; p++) {
                int id = (int) (k * 4 + p);
                write.write(GenericRow.of(id, p, BinaryString.fromString("r" + id)));
            }
            commit.commit(write.prepareCommit());
        }
    }

    private static long latest(Table table) {
        return table.latestSnapshot().map(Snapshot::id).orElse(0L);
    }

    private static boolean tagExists(RESTCatalog catalog, Identifier t, String tag)
            throws Exception {
        return ((FileStoreTable) catalog.getTable(t)).tagManager().tagExists(tag);
    }

    // ------------------------------------------------------------------ ops

    private static Map<String, Object> ops(
            RESTCatalog catalog, Api api, String warehouse, String token, Identifier t)
            throws Exception {
        Map<String, Object> out = new LinkedHashMap<>();
        FileStoreTable table = (FileStoreTable) catalog.getTable(t);
        out.put("filesBefore", files(table));
        out.put("rootsBefore", rootNames(catalog, t));
        String base = "/v1/" + warehouse + "/databases/" + t.getDatabaseName() + "/tables/" + "t";
        Map<String, Object> requests = new LinkedHashMap<>();
        String rollback =
                RESTApi.toJson(
                        new RollbackTableRequest(
                                org.apache.paimon.table.Instant.snapshot(ROLLBACK_TO), null));
        requests.put("rollback", sent(api, "POST", base + "/rollback", token, rollback));
        requests.put("deleteTag", sent(api, "DELETE", base + "/tags/old", token, null));
        requests.put("deleteBranch", sent(api, "DELETE", base + "/branches/b", token, null));
        out.put("requests", requests);
        out.put("filesAfter", files((FileStoreTable) catalog.getTable(t)));
        out.put(
                "records",
                api.bearer("GET", "/colvenna/admin/v1/purges", token, null)
                        .json()
                        .path("filePurges"));
        return out;
    }

    /**
     * One request, sent again on 503 as Paimon's client sends it (5 times at most, after 1, 2, 4, 8
     * and 16 seconds): the last answer, and every attempt's status and time.
     */
    private static Map<String, Object> sent(
            Api api, String method, String path, String token, String body) throws Exception {
        List<Map<String, Object>> attempts = new ArrayList<>();
        Api.Answer answer = api.bearer(method, path, token, body);
        attempts.add(answer.summary());
        for (int retry = 0; retry < 5 && answer.status() == 503; retry++) {
            Thread.sleep(1000L << retry);
            answer = api.bearer(method, path, token, body);
            attempts.add(answer.summary());
        }
        Map<String, Object> out = new LinkedHashMap<>(answer.summary());
        out.put("attempts", attempts);
        return out;
    }

    /** The table's files by kind (data, manifest, snapshot, tag, branch, other), as listed now. */
    private static Map<String, Integer> files(FileStoreTable table) throws Exception {
        Map<String, Integer> out = new TreeMap<>();
        Iterator<FileStatus> files =
                List.of(table.fileIO().listFiles(table.location(), true)).iterator();
        String root = table.location().toString();
        while (files.hasNext()) {
            FileStatus file = files.next();
            String path = file.getPath().toString();
            String relative = path.startsWith(root) ? path.substring(root.length()) : path;
            String name = file.getPath().getName();
            String kind =
                    relative.startsWith("/branch/")
                            ? "branch"
                            : name.startsWith("data-")
                                    ? "data"
                                    : name.startsWith("manifest-list-")
                                            ? "manifestList"
                                            : name.startsWith("manifest-")
                                                    ? "manifest"
                                                    : name.startsWith("snapshot-")
                                                            ? "snapshot"
                                                            : name.startsWith("tag-")
                                                                    ? "tag"
                                                                    : "other";
            out.merge(kind, 1, Integer::sum);
        }
        return out;
    }

    // ------------------------------------------------------------------ verify

    private static Map<String, Object> verify(RESTCatalog catalog, Identifier t) throws Exception {
        Map<String, Object> out = new LinkedHashMap<>();
        FileStoreTable main = (FileStoreTable) catalog.getTable(t);
        // Every file there is, listed once.
        Set<String> present = new HashSet<>();
        for (FileStatus file : main.fileIO().listFiles(main.location(), true)) {
            present.add(file.getPath().toUri().getPath());
        }
        long start = System.nanoTime();
        Map<String, Snapshot> roots = rootSnapshots(catalog, t);
        // Every data file each root reads, from its manifests: each must be there.
        Map<String, Integer> missing = new TreeMap<>();
        int referenced = 0;
        for (Map.Entry<String, Snapshot> root : roots.entrySet()) {
            FileStoreTable table = tableOf(catalog, t, root.getKey());
            for (Split split :
                    table.newSnapshotReader().withSnapshot(root.getValue()).read().splits()) {
                for (DataFileMeta file : ((DataSplit) split).dataFiles()) {
                    referenced++;
                    String path =
                            new org.apache.paimon.fs.Path(
                                            table.store()
                                                    .pathFactory()
                                                    .bucketPath(
                                                            ((DataSplit) split).partition(),
                                                            ((DataSplit) split).bucket()),
                                            file.fileName())
                                    .toUri()
                                    .getPath();
                    if (!present.contains(path)) {
                        missing.merge(root.getKey(), 1, Integer::sum);
                    }
                }
            }
        }
        out.put("referencedDataFiles", referenced);
        out.put("referencedDataFilesMissing", missing);
        // And every root read in full, 16 at a time: its rows are the history's.
        ExecutorService pool = Executors.newFixedThreadPool(16);
        Map<String, Future<List<Integer>>> reads = new TreeMap<>();
        List<String> wrong = new ArrayList<>();
        Map<String, Integer> rows = new TreeMap<>();
        try {
            for (Map.Entry<String, Snapshot> root : roots.entrySet()) {
                FileStoreTable table = tableOf(catalog, t, root.getKey());
                reads.put(root.getKey(), pool.submit(() -> ids(table, root.getValue())));
            }
            check(reads, roots, rows, wrong);
        } finally {
            pool.shutdownNow();
        }
        out.put("readAllMs", (System.nanoTime() - start) / 1_000_000);
        out.put("roots", rows);
        out.put("rootsWithWrongRows", wrong);
        start = System.nanoTime();
        List<String> orphans = new ArrayList<>();
        new LocalOrphanFilesClean(main, System.currentTimeMillis() + 86_400_000L, true)
                .clean()
                .getDeletedFilesPath()
                .forEach(p -> orphans.add(p.toString()));
        out.put("orphanCheckMs", (System.nanoTime() - start) / 1_000_000);
        out.put("orphans", orphans.size());
        out.put("orphanSample", orphans.subList(0, Math.min(10, orphans.size())));
        out.put("files", files(main));
        out.put("checkedAt", Instant.now().toString());
        return out;
    }

    /** Each root's rows, as read, against the history's: snapshot k holds commits 201 to k. */
    private static void check(
            Map<String, Future<List<Integer>>> reads,
            Map<String, Snapshot> roots,
            Map<String, Integer> rows,
            List<String> wrong) {
        for (Map.Entry<String, Future<List<Integer>>> read : reads.entrySet()) {
            List<Integer> ids;
            try {
                ids = read.getValue().get();
            } catch (Exception e) {
                wrong.add(read.getKey() + ": " + e.getCause());
                continue;
            }
            rows.put(read.getKey(), ids.size());
            long snapshot = roots.get(read.getKey()).id();
            List<Integer> expected = new ArrayList<>();
            for (long k = 201; k <= snapshot; k++) {
                for (int p = 0; p < 4; p++) {
                    expected.add((int) (k * 4 + p));
                }
            }
            if (!ids.equals(expected)) {
                wrong.add(read.getKey());
            }
        }
    }

    /** The table at the branch a root's name begins with. */
    private static FileStoreTable tableOf(RESTCatalog catalog, Identifier t, String root)
            throws Exception {
        String branch = root.substring(0, root.indexOf(' '));
        return (FileStoreTable)
                catalog.getTable(
                        branch.equals("main")
                                ? t
                                : new Identifier(t.getDatabaseName(), t.getTableName(), branch));
    }

    /** Every root's name, as {@link #rootSnapshots} names them. */
    private static List<String> rootNames(RESTCatalog catalog, Identifier t) throws Exception {
        return new ArrayList<>(rootSnapshots(catalog, t).keySet());
    }

    /**
     * Every snapshot and tag of every branch, by "branch snapshot-N" or "branch tag-name", with the
     * snapshot it is or holds.
     */
    private static Map<String, Snapshot> rootSnapshots(RESTCatalog catalog, Identifier t)
            throws Exception {
        Map<String, Snapshot> out = new TreeMap<>();
        List<String> branches = new ArrayList<>(catalog.listBranches(t));
        branches.add(0, "main");
        for (String branch : branches) {
            Identifier at =
                    branch.equals("main")
                            ? t
                            : new Identifier(t.getDatabaseName(), t.getTableName(), branch);
            FileStoreTable table = (FileStoreTable) catalog.getTable(at);
            Iterator<Snapshot> snapshots = table.snapshotManager().snapshots();
            while (snapshots.hasNext()) {
                Snapshot snapshot = snapshots.next();
                out.put(branch + " snapshot-" + snapshot.id(), snapshot);
            }
            for (String tag : table.tagManager().allTagNames()) {
                out.put(
                        branch + " tag-" + tag,
                        table.tagManager().getOrThrow(tag).trimToSnapshot());
            }
        }
        return out;
    }

    private static List<Integer> ids(FileStoreTable table, Snapshot snapshot) throws Exception {
        List<Split> splits = table.newSnapshotReader().withSnapshot(snapshot).read().splits();
        List<Integer> ids = new ArrayList<>();
        try (RecordReader<InternalRow> reader = table.newRead().createReader(splits)) {
            reader.forEachRemaining(row -> ids.add(row.getInt(0)));
        }
        ids.sort(null);
        return ids;
    }
}
