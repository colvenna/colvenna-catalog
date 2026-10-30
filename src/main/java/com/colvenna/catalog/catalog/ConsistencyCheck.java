package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.CatalogStore.LockMode;
import com.colvenna.catalog.catalog.Records.BranchRecord;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.http.ApiException;
import java.io.FileNotFoundException;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.annotation.Nullable;
import org.apache.paimon.CoreOptions;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.FileStatus;
import org.apache.paimon.fs.Path;
import org.apache.paimon.manifest.ManifestEntry;
import org.apache.paimon.options.MemorySize;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.apache.paimon.schema.TableSchema;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.core.type.TypeReference;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.utils.SegmentsCache;
import org.apache.paimon.utils.SnapshotManager;

/**
 * Cross-checks one table's PostgreSQL state against its files, as recovery evidence.
 *
 * <p>For every branch it records the pointer and the pending commit intents as found, then resolves
 * those intents, as any reader would, and reports:
 *
 * <ul>
 *   <li>the pointer, the highest {@code snapshot-N} file and the intents still pending;
 *   <li>whether PostgreSQL's copy of the published snapshot is the snapshot file;
 *   <li>whether every data file that the earliest and the head snapshot, an even sample of the
 *       snapshots between them, and every tag reference exists, and whether the head snapshot's
 *       files hold its record count;
 *   <li>metadata that cannot be read (a missing manifest, say) as a problem rather than an error.
 * </ul>
 *
 * For the main branch it also compares the schema PostgreSQL serves, id and content, with the
 * latest schema file. Any mismatch is listed under {@code problems}. A table whose files are gone,
 * as one dropped after a metadata restore's point, is reported so: no schema file, and the
 * pointers' snapshots without files.
 *
 * <p>The work is done in steps: each branch's pointer and head, then the data files of one retained
 * snapshot or tag at a time. Each data directory is listed once per answer instead of asking for
 * each file, and manifests are read in parallel. With a work time ({@code
 * COLVENNA_REQUEST_WORK_TIME}, the serverless form's 10 seconds, #69) the check takes no new step
 * once that time has passed, and answers with the report so far and a {@code nextPageToken}; asked
 * again with it, it goes on from there and answers the report so far again, until the answer
 * without a token, which is the whole report. Without one (the EKS form) the first answer is the
 * whole report. {@code consistent} is true only on that last answer. Between answers the check is
 * kept in PostgreSQL ({@code consistency_checks}), the token naming it, for a day; done, it is kept
 * so too, so that a last answer lost on its way is the same when asked for again. The retained
 * snapshots it samples and the tags it checks are fixed when it starts a branch, so later answers
 * check the same ones.
 *
 * <p>The report is evidence of a quiet table. While commits are running, a snapshot file can
 * briefly be ahead of the pointer: between the publish and the pointer update of a commit in
 * flight.
 */
public final class ConsistencyCheck {

    private static final Pattern SNAPSHOT_FILE = Pattern.compile("snapshot-(\\d+)");

    /** Retained snapshots whose data files are checked, the earliest and the head among them. */
    static final int SAMPLED_SNAPSHOTS = 20;

    /** Manifests read at once, and data directories listed at once. */
    static final int PARALLELISM = 16;

    /** Manifests kept in memory while one answer is worked on: sampled snapshots share many. */
    private static final MemorySize MANIFEST_CACHE = MemorySize.ofMebiBytes(64);

    /** A larger manifest is read again rather than kept. */
    private static final long MANIFEST_CACHE_ENTRY = MemorySize.ofMebiBytes(8).getBytes();

    /** Missing data files named in {@code problems}, for each branch; all are counted. */
    private static final int MISSING_NAMED = 5;

    /** How long a check answered in pages is kept between two of them. */
    static final Duration KEPT = Duration.ofDays(1);

    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() {};

    private final Db db;
    private final CatalogStore store;
    private final TableFiles files;
    private final CommitService commits;
    private final CatalogService catalog;
    private final Duration workTime;

    /**
     * @param workTime how long one answer works before it answers with a token; zero for no limit
     */
    public ConsistencyCheck(
            Db db,
            CatalogStore store,
            TableFiles files,
            CommitService commits,
            CatalogService catalog,
            Duration workTime) {
        this.db = db;
        this.store = store;
        this.files = files;
        this.commits = commits;
        this.catalog = catalog;
        this.workTime = workTime;
    }

    /** Where a check is between two answers. */
    private static final class Progress {
        /** The branches to check, as the first answer found them. */
        List<String> branches = new ArrayList<>();

        /** Whether the table has a schema file: without one its data files cannot be read. */
        boolean schemaFile = true;

        /** The branch in hand. */
        int branch;

        /** The next step of that branch: -1 its pointer and head, then its retained snapshots. */
        int step = -1;

        /** The branch's sample: its first and head snapshot, as its pointer step found them. */
        long first;

        long head;

        /** The snapshots the branch's tags hold, as its pointer step found them, in order. */
        List<Long> tags = new ArrayList<>();

        /** The report so far, or the whole one once the check is done. */
        Map<String, Object> report = new LinkedHashMap<>();

        /** Whether the check is done: kept so, its last answer can be asked for again. */
        boolean done;

        String toJson() throws Exception {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("branches", branches);
            out.put("schemaFile", schemaFile);
            out.put("branch", branch);
            out.put("step", step);
            out.put("first", first);
            out.put("head", head);
            out.put("tags", tags);
            out.put("report", report);
            out.put("done", done);
            return RESTApi.OBJECT_MAPPER.writeValueAsString(out);
        }

        static Progress fromJson(String json) throws Exception {
            Map<String, Object> map = RESTApi.OBJECT_MAPPER.readValue(json, MAP);
            Progress progress = new Progress();
            progress.branches = strings(map.get("branches"));
            progress.schemaFile = !Boolean.FALSE.equals(map.get("schemaFile"));
            progress.branch = intOf(map.get("branch"));
            progress.step = intOf(map.get("step"));
            progress.first = longOf(map.get("first"));
            progress.head = longOf(map.get("head"));
            if (map.get("tags") instanceof List<?> tags) {
                for (Object tag : tags) {
                    progress.tags.add(longOf(tag));
                }
            }
            progress.done = Boolean.TRUE.equals(map.get("done"));
            if (map.get("report") instanceof Map<?, ?> report) {
                for (Map.Entry<?, ?> e : report.entrySet()) {
                    progress.report.put(String.valueOf(e.getKey()), e.getValue());
                }
            }
            return progress;
        }
    }

    /** A check kept between two answers. */
    private record Kept(UUID table, String progress, int version) {}

    /** A branch's sample: its first and head snapshot. */
    private record Ends(long first, long head) {}

    /**
     * One answer of the check: the whole report, or, when the work time ran out, the report so far
     * with a {@code nextPageToken} to go on with.
     */
    public Map<String, Object> check(
            RequestContext ctx, Identifier identifier, @Nullable String pageToken)
            throws Exception {
        // Time spent, not the time of day: a monotonic clock.
        long started = System.nanoTime();
        TableRecord table = catalog.requireTable(identifier);
        Progress progress;
        UUID kept = null;
        int version = 0;
        if (pageToken == null) {
            progress = start(table);
        } else {
            kept = parse(pageToken);
            UUID id = kept;
            Kept found =
                    db.inTransaction(c -> load(c, id))
                            .orElseThrow(
                                    () ->
                                            ApiException.badRequest(
                                                    "Unknown or expired pageToken: ask again"
                                                            + " without it."));
            if (!found.table().equals(table.id())) {
                throw ApiException.badRequest(
                        "The pageToken is of another table, or of one dropped since: ask again"
                                + " without it.");
            }
            progress = Progress.fromJson(found.progress());
            version = found.version();
            if (progress.done) {
                // The last answer again, as when it was lost on its way.
                return progress.report;
            }
        }
        List<String> problems = strings(progress.report.get("problems"));
        List<Map<String, Object>> branches = maps(progress.report.get("branches"));
        try (Work work = new Work(table)) {
            while (progress.branch < progress.branches.size()) {
                if (!workTime.isZero()
                        && work.steps > 0
                        && System.nanoTime() - started >= workTime.toNanos()) {
                    progress.report.put("problems", problems);
                    progress.report.put("branches", branches);
                    String token = keep(table, progress, kept, version);
                    Map<String, Object> answer = new LinkedHashMap<>(progress.report);
                    answer.put("consistent", false);
                    answer.put("nextPageToken", token);
                    return answer;
                }
                String branch = progress.branches.get(progress.branch);
                work.steps++;
                if (progress.step < 0) {
                    Map<String, Object> out = new LinkedHashMap<>();
                    branches.add(out);
                    Optional<Ends> ends =
                            checkBranch(ctx, branch, progress.schemaFile, out, problems, work);
                    if (ends.isEmpty()) {
                        progress.branch++;
                    } else {
                        progress.first = ends.get().first();
                        progress.head = ends.get().head();
                        progress.tags = new ArrayList<>(work.taggedSnapshots(branch).keySet());
                        progress.step = 0;
                    }
                    continue;
                }
                Map<String, Object> out = branches.get(branches.size() - 1);
                List<Unit> units = Work.units(progress);
                if (progress.step >= units.size()
                        || !checkUnit(work, branch, units, progress.step, out, problems)) {
                    progress.branch++;
                    progress.step = -1;
                } else {
                    progress.step++;
                }
            }
        }
        progress.report.put("problems", problems);
        if (table.fileStore()) {
            progress.report.put("branches", branches);
        }
        progress.report.put("consistent", problems.isEmpty());
        progress.report.put("nextPageToken", null);
        if (kept != null) {
            // Kept done until it expires: a last answer lost on its way is the same when asked
            // again.
            progress.done = true;
            try {
                keep(table, progress, kept, version);
            } catch (ApiException conflict) {
                // Another request finished it first: this answer is as good.
            }
        }
        return progress.report;
    }

    /** The report's start: the table and its schema, and the branches to check. */
    private Progress start(TableRecord table) throws Exception {
        Progress progress = new Progress();
        List<String> problems = new ArrayList<>();
        Map<String, Object> report = progress.report;
        report.put("table", table.fullName());
        report.put("path", table.path());
        report.put("state", table.state().name());
        if (table.state() == Records.TableState.FENCED) {
            problems.add("table is fenced: " + table.stateReason());
        }
        if (table.fileStore()) {
            progress.schemaFile = checkSchema(table, report, problems);
            progress.branches.addAll(db.inTransaction(c -> store.branchNames(c, table.id())));
            report.put("branches", new ArrayList<>());
        }
        report.put("problems", problems);
        return progress;
    }

    /** Compares the schemas; returns whether the table has a schema file at all. */
    private boolean checkSchema(
            TableRecord table, Map<String, Object> report, List<String> problems) {
        Optional<TableSchema> schema =
                files.latestSchema(table.path(), Identifier.DEFAULT_MAIN_BRANCH);
        report.put("schemaIdCatalog", table.schemaId());
        report.put("schemaIdFiles", schema.map(TableSchema::id).orElse(null));
        if (schema.isEmpty()) {
            problems.add(
                    "no schema file in "
                            + table.path()
                            + ", where the catalog serves schema "
                            + table.schemaId());
        } else if (schema.get().id() != table.schemaId()) {
            problems.add(
                    "catalog serves schema "
                            + table.schemaId()
                            + " but the latest schema file is "
                            + schema.map(TableSchema::id).orElse(null));
        } else if (!CatalogService.withoutPath(schema.get().toSchema()).equals(table.schema())) {
            problems.add(
                    "catalog serves schema "
                            + table.schemaId()
                            + " with other fields, keys or options than its schema file");
        }
        return schema.isPresent();
    }

    /**
     * A branch's pointer and head; returns its sample's ends when its retained snapshots' data
     * files are to be checked next.
     */
    private Optional<Ends> checkBranch(
            RequestContext ctx,
            String branch,
            boolean schemaFile,
            Map<String, Object> out,
            List<String> problems,
            Work work)
            throws Exception {
        TableRecord table = work.table;
        out.put("branch", branch);
        // As found, before this check resolves anything: what a restore or a crash left behind.
        Map<String, Object> found = new LinkedHashMap<>();
        found.put(
                "pointerSnapshot",
                db.inTransaction(c -> store.branch(c, table.id(), branch, LockMode.NONE))
                        .map(BranchRecord::snapshotId)
                        .orElse(null));
        found.put(
                "pendingIntents",
                db.inTransaction(c -> store.pendingIntents(c, table.id(), branch).size()));
        out.put("found", found);

        BranchRecord pointer = commits.latest(ctx, table, branch).orElse(null);
        Long pointerId = pointer == null ? null : pointer.snapshotId();
        int pending = db.inTransaction(c -> store.pendingIntents(c, table.id(), branch).size());
        // Snapshot files are read without a schema: even a table whose files are gone is checked.
        SnapshotManager snapshots = files.snapshotManager(table.path(), branch);
        Long highestFile =
                highestSnapshotFile(files.tableIO(table.path()), snapshots.snapshotDirectory());
        out.put("pointerSnapshot", pointerId);
        out.put("highestSnapshotFile", highestFile);
        out.put("pendingIntents", pending);
        if (pending > 0) {
            problems.add(branch + ": " + pending + " commit intents still pending");
        }
        if (pointerId != null && highestFile != null && highestFile > pointerId) {
            problems.add(
                    branch
                            + ": snapshot file "
                            + highestFile
                            + " exists beyond the published pointer "
                            + pointerId);
        }
        if (pointerId == null) {
            return Optional.empty();
        }
        if (!snapshots.snapshotExists(pointerId)) {
            problems.add(branch + ": pointer snapshot " + pointerId + " has no file");
            return Optional.empty();
        }
        if (!schemaFile) {
            // Its data files cannot be read without a schema; the missing schema is a problem.
            return Optional.empty();
        }
        try {
            FileStoreTable fileTable = work.table(branch);
            Snapshot head = fileTable.snapshotManager().snapshot(pointerId);
            if (pointer.snapshotJson() != null
                    && !Snapshot.fromJson(pointer.snapshotJson()).equals(head)) {
                problems.add(
                        branch
                                + ": the catalog's copy of snapshot "
                                + head.id()
                                + " differs from its file");
            }
            long headRows = 0;
            for (ManifestEntry entry :
                    fileTable.store().newScan().withSnapshot(head).plan().files()) {
                headRows += entry.file().rowCount();
            }
            out.put("headRowsInDataFiles", headRows);
            out.put("headRecordCount", head.totalRecordCount());
            if (head.totalRecordCount() != headRows) {
                problems.add(
                        branch
                                + ": snapshot "
                                + head.id()
                                + " counts "
                                + head.totalRecordCount()
                                + " records but its data files hold "
                                + headRows);
            }
            // Retained snapshots and every tag: what expiry or a lost file could break. A busy
            // table retains thousands of snapshots, so beyond the earliest and the head a sample.
            Long earliest = fileTable.snapshotManager().earliestSnapshotId();
            long first = earliest == null ? head.id() : earliest;
            out.put("retainedSnapshots", head.id() - first + 1);
            out.put("snapshotsChecked", 0);
            out.put("tagsChecked", 0);
            out.put("dataFilesChecked", 0);
            out.put("dataFilesMissing", 0);
            return Optional.of(new Ends(first, head.id()));
        } catch (Exception e) {
            problems.add(branch + ": cannot read the table's metadata: " + e);
            return Optional.empty();
        }
    }

    /** One step of a branch's data files: a sampled snapshot, or a tagged one, by its id. */
    private record Unit(long snapshot, boolean tag) {}

    /**
     * The data files of the branch's step {@code index}; returns false when the branch's metadata
     * cannot be read, which ends its checks.
     */
    private static boolean checkUnit(
            Work work,
            String branch,
            List<Unit> units,
            int index,
            Map<String, Object> out,
            List<String> problems) {
        Unit unit = units.get(index);
        try {
            Optional<Snapshot> snapshot = work.snapshot(branch, unit);
            if (snapshot.isEmpty()) {
                return true;
            }
            work.markCounted(branch, units, index);
            Set<Path> referenced = new LinkedHashSet<>();
            for (Path path : work.dataFiles(branch, snapshot.get())) {
                if (work.counted.add(branch + "/" + path)) {
                    referenced.add(path);
                }
            }
            work.done.add(index);
            List<Path> missing = work.missing(work.table(branch).fileIO(), referenced);
            int named = intOf(out.get("dataFilesMissing"));
            for (Path path : missing) {
                String problem = branch + ": data file missing " + path;
                if (named++ < MISSING_NAMED && !problems.contains(problem)) {
                    problems.add(problem);
                }
            }
            String count = unit.tag() ? "tagsChecked" : "snapshotsChecked";
            out.put(count, intOf(out.get(count)) + 1);
            out.put("dataFilesChecked", intOf(out.get("dataFilesChecked")) + referenced.size());
            out.put("dataFilesMissing", intOf(out.get("dataFilesMissing")) + missing.size());
            return true;
        } catch (Exception e) {
            problems.add(branch + ": cannot read the table's metadata: " + e);
            return false;
        }
    }

    /**
     * What one answer keeps while it works: the tables it opened, their manifests and listed
     * directories, the data files it has counted, and the steps it did.
     */
    private final class Work implements AutoCloseable {
        final TableRecord table;
        int steps;

        /** Data files counted in this answer, by branch: each once, however many reference it. */
        final Set<String> counted = new HashSet<>();

        /** The steps of the branch in hand that this answer did. */
        final Set<Integer> done = new HashSet<>();

        /** The steps of earlier answers whose files this one marked as counted. */
        private final Set<Integer> marked = new HashSet<>();

        private String branchOfSteps = "";
        private final Map<String, Set<String>> listed = new HashMap<>();
        private final Map<String, FileStoreTable> tables = new HashMap<>();
        private final Map<String, Map<Long, Snapshot>> tagged = new HashMap<>();
        private final SegmentsCache<Path> manifests =
                SegmentsCache.create(MANIFEST_CACHE, MANIFEST_CACHE_ENTRY);
        @Nullable private ExecutorService listing;

        Work(TableRecord table) {
            this.table = table;
        }

        FileStoreTable table(String branch) {
            return tables.computeIfAbsent(
                    branch,
                    b -> {
                        FileStoreTable t =
                                files.table(
                                        table.path(),
                                        b,
                                        Map.of(
                                                CoreOptions.SCAN_MANIFEST_PARALLELISM.key(),
                                                String.valueOf(PARALLELISM)));
                        t.setManifestCache(manifests);
                        return t;
                    });
        }

        /** The snapshots the branch's tags hold, by id, in order. */
        Map<Long, Snapshot> taggedSnapshots(String branch) {
            return tagged.computeIfAbsent(
                    branch,
                    b -> {
                        Map<Long, Snapshot> out = new TreeMap<>();
                        for (Snapshot s : table(b).tagManager().tags().keySet()) {
                            out.put(s.id(), s);
                        }
                        return out;
                    });
        }

        /** The branch's steps: its sample between the ends, then its tagged snapshots. */
        static List<Unit> units(Progress progress) {
            List<Unit> out = new ArrayList<>();
            for (long n : sample(progress.first, progress.head)) {
                out.add(new Unit(n, false));
            }
            for (long n : progress.tags) {
                out.add(new Unit(n, true));
            }
            return out;
        }

        /** The snapshot of a step; empty when its file or tag is gone since. */
        Optional<Snapshot> snapshot(String branch, Unit unit) {
            if (unit.tag()) {
                return Optional.ofNullable(taggedSnapshots(branch).get(unit.snapshot()));
            }
            FileStoreTable t = table(branch);
            return t.snapshotManager().snapshotExists(unit.snapshot())
                    ? Optional.of(t.snapshotManager().snapshot(unit.snapshot()))
                    : Optional.empty();
        }

        /** The data files a snapshot references. */
        List<Path> dataFiles(String branch, Snapshot snapshot) {
            FileStoreTable t = table(branch);
            List<Path> paths = new ArrayList<>();
            for (ManifestEntry entry : t.store().newScan().withSnapshot(snapshot).plan().files()) {
                paths.add(
                        t.store()
                                .pathFactory()
                                .createDataFilePathFactory(entry.partition(), entry.bucket())
                                .toPath(entry.file()));
            }
            return paths;
        }

        /**
         * Before step {@code index}: marks as counted the files that steps done in earlier answers
         * may share with it, so that each file counts once however many answers the check takes. A
         * data file is referenced by a contiguous run of a branch's snapshots, so what a sampled
         * snapshot shares with those before it, the sample just before has; and what a tagged one
         * shares with the samples and the tags before it, the samples on either side of it and the
         * tag just before have. Only steps already done are marked, so no file is left unchecked,
         * and a step plans at most three snapshots more.
         */
        void markCounted(String branch, List<Unit> units, int index) {
            if (!branch.equals(branchOfSteps)) {
                branchOfSteps = branch;
                done.clear();
                marked.clear();
            }
            Set<Integer> before = new LinkedHashSet<>();
            Unit unit = units.get(index);
            if (!unit.tag()) {
                before.add(index - 1);
            } else {
                int lower = -1;
                int upper = -1;
                for (int i = 0; i < units.size() && !units.get(i).tag(); i++) {
                    if (units.get(i).snapshot() <= unit.snapshot()) {
                        lower = i;
                    }
                    if (units.get(i).snapshot() >= unit.snapshot() && upper < 0) {
                        upper = i;
                    }
                }
                before.add(lower);
                before.add(upper);
                if (index > 0 && units.get(index - 1).tag()) {
                    before.add(index - 1);
                }
            }
            for (int i : before) {
                if (i < 0 || i >= index || done.contains(i) || !marked.add(i)) {
                    continue;
                }
                try {
                    Optional<Snapshot> snapshot = snapshot(branch, units.get(i));
                    if (snapshot.isPresent()) {
                        for (Path path : dataFiles(branch, snapshot.get())) {
                            counted.add(branch + "/" + path);
                        }
                    }
                } catch (RuntimeException e) {
                    // Then its files count again: the step reads them itself.
                }
            }
        }

        /**
         * The paths without a file, each data directory listed once in this answer rather than each
         * file asked for: a directory of a thousand files is one request, not a thousand.
         */
        List<Path> missing(FileIO io, Set<Path> paths) throws Exception {
            Map<String, Path> directories = new LinkedHashMap<>();
            for (Path path : paths) {
                Path parent = path.getParent();
                if (!listed.containsKey(parent.toString())) {
                    directories.put(parent.toString(), parent);
                }
            }
            if (!directories.isEmpty()) {
                if (listing == null) {
                    listing = Executors.newFixedThreadPool(PARALLELISM);
                }
                Map<String, Future<Set<String>>> lists = new LinkedHashMap<>();
                for (Map.Entry<String, Path> d : directories.entrySet()) {
                    lists.put(d.getKey(), listing.submit(() -> names(io, d.getValue())));
                }
                for (Map.Entry<String, Future<Set<String>>> l : lists.entrySet()) {
                    listed.put(l.getKey(), unwrap(l.getValue()));
                }
            }
            List<Path> missing = new ArrayList<>();
            for (Path path : paths) {
                if (!listed.get(path.getParent().toString()).contains(path.getName())) {
                    missing.add(path);
                }
            }
            return missing;
        }

        @Override
        public void close() {
            if (listing != null) {
                listing.shutdownNow();
            }
        }
    }

    private static Set<String> names(FileIO io, Path directory) throws Exception {
        Set<String> names = new HashSet<>();
        try {
            FileStatus[] statuses = io.listStatus(directory);
            if (statuses != null) {
                for (FileStatus status : statuses) {
                    names.add(status.getPath().getName());
                }
            }
        } catch (FileNotFoundException e) {
            // No directory: none of its files exists.
        }
        return names;
    }

    private static <T> T unwrap(Future<T> future) throws Exception {
        try {
            return future.get();
        } catch (ExecutionException e) {
            if (e.getCause() instanceof Exception cause) {
                throw cause;
            }
            throw e;
        }
    }

    /** The ids from first to last whose data files are checked: both ends and an even sample. */
    public static List<Long> sample(long first, long last) {
        List<Long> ids = new ArrayList<>();
        long span = last - first;
        if (span < SAMPLED_SNAPSHOTS) {
            for (long n = first; n <= last; n++) {
                ids.add(n);
            }
            return ids;
        }
        for (int i = 0; i < SAMPLED_SNAPSHOTS; i++) {
            ids.add(first + span * i / (SAMPLED_SNAPSHOTS - 1));
        }
        return ids;
    }

    @Nullable
    private static Long highestSnapshotFile(FileIO io, Path directory) throws Exception {
        if (!io.exists(directory)) {
            return null;
        }
        Long highest = null;
        for (FileStatus status : io.listStatus(directory)) {
            Matcher m = SNAPSHOT_FILE.matcher(status.getPath().getName());
            if (m.matches()) {
                long id = Long.parseLong(m.group(1));
                highest = highest == null ? id : Math.max(highest, id);
            }
        }
        return highest;
    }

    // ---- the check kept between answers --------------------------------------------------------

    /**
     * Keeps the check for its next answer and returns the token that names it: a new one for a
     * first answer, the same one after. Another request that went on with it since is a conflict.
     */
    private String keep(TableRecord table, Progress progress, @Nullable UUID kept, int version)
            throws Exception {
        String json = progress.toJson();
        UUID id = kept == null ? UUID.randomUUID() : kept;
        boolean saved =
                db.inTransaction(
                        c -> {
                            if (kept == null) {
                                try (PreparedStatement old =
                                                c.prepareStatement(
                                                        "DELETE FROM consistency_checks WHERE"
                                                                + " updated_at < now() - ? *"
                                                                + " interval '1 second'");
                                        PreparedStatement insert =
                                                c.prepareStatement(
                                                        "INSERT INTO consistency_checks (id,"
                                                                + " table_id, progress, version)"
                                                                + " VALUES (?, ?, ?, 1)")) {
                                    old.setLong(1, KEPT.toSeconds());
                                    old.executeUpdate();
                                    insert.setObject(1, id);
                                    insert.setObject(2, table.id());
                                    insert.setString(3, json);
                                    insert.executeUpdate();
                                    return true;
                                }
                            }
                            try (PreparedStatement update =
                                    c.prepareStatement(
                                            "UPDATE consistency_checks SET progress = ?, version ="
                                                    + " version + 1, updated_at = now() WHERE id ="
                                                    + " ? AND version = ?")) {
                                update.setString(1, json);
                                update.setObject(2, id);
                                update.setInt(3, version);
                                return update.executeUpdate() == 1;
                            }
                        });
        if (!saved) {
            throw ApiException.conflict(
                    ErrorResponse.RESOURCE_TYPE_TABLE,
                    table.fullName(),
                    "This check went on in another request: ask again with the same pageToken.");
        }
        return id.toString();
    }

    private static Optional<Kept> load(Connection c, UUID id) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT table_id, progress, version FROM consistency_checks WHERE id = ?"
                                + " AND updated_at >= now() - ? * interval '1 second'")) {
            select.setObject(1, id);
            select.setLong(2, KEPT.toSeconds());
            try (ResultSet rs = select.executeQuery()) {
                return rs.next()
                        ? Optional.of(
                                new Kept(
                                        rs.getObject(1, UUID.class), rs.getString(2), rs.getInt(3)))
                        : Optional.empty();
            }
        }
    }

    private static UUID parse(String token) {
        try {
            return UUID.fromString(token);
        } catch (IllegalArgumentException e) {
            throw ApiException.badRequest("Malformed pageToken: ask again without it.");
        }
    }

    private static int intOf(@Nullable Object value) {
        return value instanceof Number n ? n.intValue() : 0;
    }

    private static long longOf(@Nullable Object value) {
        return value instanceof Number n ? n.longValue() : 0;
    }

    private static List<String> strings(@Nullable Object value) {
        List<String> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object item : list) {
                out.add(String.valueOf(item));
            }
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> maps(@Nullable Object value) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object item : list) {
                if (item instanceof Map<?, ?> m) {
                    out.add(new LinkedHashMap<>((Map<String, Object>) m));
                }
            }
        }
        return out;
    }
}
