package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.fs.Reach;
import com.colvenna.catalog.fs.Roots.Kind;
import com.colvenna.catalog.fs.Roots.Root;
import com.colvenna.catalog.fs.Roots.RootFile;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.http.ApiException;
import com.colvenna.catalog.metrics.CatalogMetrics;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import javax.annotation.Nullable;
import org.apache.paimon.Snapshot;
import org.apache.paimon.blob.ManagedBlobReferenceFile;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.fs.Path;
import org.apache.paimon.manifest.ExpireFileEntry;
import org.apache.paimon.options.Options;
import org.apache.paimon.schema.TableSchema;
import org.apache.paimon.table.CatalogEnvironment;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.FileStoreTableFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The files that a rollback, a tag's deletion or a branch's deletion leaves unreferenced (#93).
 * Each operation records, before it removes them, the snapshot, tag and changelog files it removes
 * (its roots), and answers once its change is made: the snapshots after a rollback's target are
 * gone, the tag or the branch is gone. The files those roots reached are deleted afterwards, by the
 * request itself ({@link TablePurges.Mode#INLINE}, the EKS form) or by the purge function's sweeps
 * ({@link TablePurges.Mode#DEFERRED}, the serverless form), so that each request's time no longer
 * grows with the table's files.
 *
 * <p>A purge deletes only what no root of the table references when it deletes it: every snapshot,
 * tag and long-lived changelog of every branch, as their files are then, whatever the record says
 * ({@link Reach#referenced}). It reads them all again after the record was made, and once more
 * under the table's lock and every branch pointer's before each batch of deletions, so a tag or a
 * branch made since, a commit, or a root written straight to the files keeps what it references. So
 * a record is only ever a list of what may be deleted: one whose operation never took effect (the
 * request stopped after recording, or answered 503 to be sent again) finds its roots still there,
 * and deletes nothing.
 *
 * <p>Purges are idempotent and claimed as {@link TablePurges}' are: a claim holds a record for a
 * {@link TablePurges#LEASE}, renewed while the purge runs; a failure keeps the record, with its
 * error, for the next sweep. Files are deleted data first, then manifests, then manifest lists, so
 * a purge stopped half way is done again from the same roots.
 */
public final class FilePurges {

    private static final Logger LOG = LoggerFactory.getLogger(FilePurges.class);

    /** How long after it was made a record not applied is gone on with by a request sent again. */
    public static final Duration REUSE = TablePurges.LEASE.dividedBy(2);

    /** How many files one batch deletes under the table's locks. */
    static final int BATCH = 500;

    /** How often a batch tries for the locks before the purge fails, to be tried again. */
    private static final int LOCK_ATTEMPTS = 3;

    private static final int ERROR_LENGTH = 1000;

    /**
     * How long the files a rollback or a branch's deletion leaves wait before a purge may delete
     * them. A writer restoring from a checkpoint taken before the rollback commits those files
     * again: Paimon's committer re-commits a checkpoint's files that no snapshot holds, once it has
     * checked they are there ({@code TableCommitImpl.filterAndCommitMultiple}). Within this time
     * the re-commit finds them and the purge, reading the roots again, keeps them; after it, the
     * restore fails on the missing files rather than committing them. Paimon's own rollback and
     * branch deletion never delete these files; its orphan file cleaning waits a day for any file.
     * A tag's deletion leaves the files of an older snapshot, which no writer commits again, and
     * waits for nothing, as Paimon's own deletes them at once.
     */
    public static final Duration RECOMMIT_GRACE = Duration.ofHours(1);

    /** What removed the roots. */
    public enum Operation {
        ROLLBACK,
        DROP_TAG,
        DROP_BRANCH;

        /** How long its files wait, once it took effect, before a purge may take them. */
        public Duration delay() {
            return this == DROP_TAG ? Duration.ZERO : RECOMMIT_GRACE;
        }
    }

    /** One record, as the admin API lists it. */
    public record Pending(
            long id,
            UUID tableId,
            String table,
            String path,
            String branch,
            Operation operation,
            String target,
            Instant createdAt,
            String createdBy,
            @Nullable String requestId,
            boolean applied,
            int attempts,
            @Nullable Instant lastAttemptAt,
            @Nullable String lastError,
            Instant dueAt,
            Instant notBefore,
            int roots) {

        public Map<String, Object> describe() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("id", id);
            out.put("tableId", tableId.toString());
            out.put("table", table);
            out.put("path", path);
            out.put("branch", branch);
            out.put("operation", operation.name());
            out.put("target", target);
            out.put("createdAt", createdAt.toString());
            out.put("createdBy", createdBy);
            out.put("requestId", requestId);
            out.put("applied", applied);
            out.put("roots", roots);
            out.put("attempts", attempts);
            out.put("lastAttemptAt", lastAttemptAt == null ? null : lastAttemptAt.toString());
            out.put("lastError", lastError);
            out.put("dueAt", dueAt.toString());
            out.put("notBefore", notBefore.toString());
            return out;
        }
    }

    /** A record being made: its id and the keys of the roots it holds already. */
    public record Capture(long id, Set<String> captured) {

        boolean has(RootFile root) {
            return captured.contains(key(root.kind(), root.name(), root.modified()));
        }
    }

    /** What a sweep did and what is left, as {@link TablePurges.Sweep}. */
    public record Sweep(int purged, int failed, int skipped, int pending, long oldestSeconds) {}

    private final Db db;
    private final CatalogStore store;
    private final TableFiles files;
    private final Clock clock;
    private final CatalogMetrics metrics;
    private final Hooks hooks;

    /** What tests do between a purge's steps. */
    public interface Hooks {
        Hooks NONE = new Hooks() {};

        /** Before each batch of deletions takes the table's locks. */
        default void beforeBatch(Pending record) throws Exception {}
    }

    public FilePurges(
            Db db, CatalogStore store, TableFiles files, Clock clock, CatalogMetrics metrics) {
        this(db, store, files, clock, metrics, Hooks.NONE);
    }

    public FilePurges(
            Db db,
            CatalogStore store,
            TableFiles files,
            Clock clock,
            CatalogMetrics metrics,
            Hooks hooks) {
        this.db = db;
        this.store = store;
        this.files = files;
        this.clock = clock;
        this.metrics = metrics;
        this.hooks = hooks;
    }

    // ------------------------------------------------------------------ recording

    /**
     * Records {@code roots} for {@code operation} on {@code table}'s {@code branch}, in
     * transactions of their own, so that the record outlasts a request that fails after removing
     * them. A request sent again for the same {@code target} goes on with the record it began,
     * reading only the roots not recorded yet; {@code read} holds contents already read. When
     * {@code deadline} ({@link System#nanoTime()}, 0 for none) passes, what was read stays recorded
     * and the request answers a retryable 503.
     */
    Capture capture(
            TableRecord table,
            String branch,
            Operation operation,
            String target,
            RequestContext ctx,
            List<RootFile> roots,
            Map<RootFile, String> read,
            long deadline)
            throws Exception {
        Capture capture = open(table, branch, operation, target, ctx);
        Map<RootFile, String> known = new LinkedHashMap<>();
        List<RootFile> unread = new ArrayList<>();
        for (RootFile root : roots) {
            if (capture.has(root)) {
                continue;
            }
            String content = read.get(root);
            if (content != null) {
                known.put(root, content);
            } else {
                unread.add(root);
            }
        }
        add(capture, known);
        Map<RootFile, String> got = new LinkedHashMap<>();
        try {
            files.read(table.path(), unread, deadline, got);
        } catch (TableFiles.DeadlineException e) {
            add(capture, got);
            throw ApiException.unavailable(
                    "Recorded "
                            + capture.captured().size()
                            + " of the "
                            + roots.size()
                            + " files this "
                            + describe(operation)
                            + " removes before the request's time ran out; send it again to go"
                            + " on.",
                    null);
        }
        add(capture, got);
        return capture;
    }

    private static String describe(Operation operation) {
        return switch (operation) {
            case ROLLBACK -> "rollback";
            case DROP_TAG -> "tag's deletion";
            case DROP_BRANCH -> "branch's deletion";
        };
    }

    private Capture open(
            TableRecord table,
            String branch,
            Operation operation,
            String target,
            RequestContext ctx)
            throws Exception {
        Instant now = clock.instant();
        return db.inAuxTransaction(
                c -> {
                    try (PreparedStatement select =
                            c.prepareStatement(
                                    "SELECT id FROM file_purges WHERE table_id = ? AND branch = ?"
                                            + " AND operation = ? AND target = ? AND NOT applied"
                                            + " AND attempts = 0 AND created_at > ? ORDER BY id"
                                            + " DESC LIMIT 1 FOR UPDATE")) {
                        select.setObject(1, table.id());
                        select.setString(2, branch);
                        select.setString(3, operation.name());
                        select.setString(4, target);
                        // Half a lease: long past a client's retries, and long before a sweep
                        // may take the record (a lease after it was made).
                        select.setTimestamp(5, Timestamp.from(now.minus(REUSE)));
                        try (ResultSet rs = select.executeQuery()) {
                            if (rs.next()) {
                                long id = rs.getLong(1);
                                return new Capture(id, capturedKeys(c, id));
                            }
                        }
                    }
                    try (PreparedStatement insert =
                            c.prepareStatement(
                                    "INSERT INTO file_purges (table_id, table_name, path, branch,"
                                            + " operation, target, created_at, created_by,"
                                            + " request_id, due_at, not_before) VALUES (?, ?, ?, ?,"
                                            + " ?, ?, ?, ?, ?, ?, ?) RETURNING id")) {
                        insert.setObject(1, table.id());
                        insert.setString(2, table.fullName());
                        insert.setString(3, table.path());
                        insert.setString(4, branch);
                        insert.setString(5, operation.name());
                        insert.setString(6, target);
                        insert.setTimestamp(7, Timestamp.from(now));
                        insert.setString(8, ctx.who());
                        insert.setString(9, ctx.requestId());
                        // Not due until the operation takes effect; abandoned, a lease and the
                        // operation's delay later, for the request may have removed files.
                        Timestamp due =
                                Timestamp.from(now.plus(TablePurges.LEASE).plus(operation.delay()));
                        insert.setTimestamp(10, due);
                        insert.setTimestamp(11, due);
                        try (ResultSet rs = insert.executeQuery()) {
                            rs.next();
                            return new Capture(rs.getLong(1), new HashSet<>());
                        }
                    }
                });
    }

    private static Set<String> capturedKeys(Connection c, long id) throws SQLException {
        Set<String> keys = new HashSet<>();
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT kind, name, modified FROM file_purge_roots WHERE purge_id = ?")) {
            select.setLong(1, id);
            try (ResultSet rs = select.executeQuery()) {
                while (rs.next()) {
                    keys.add(key(Kind.valueOf(rs.getString(1)), rs.getString(2), rs.getLong(3)));
                }
            }
        }
        return keys;
    }

    private static String key(Kind kind, String name, long modified) {
        return kind + "/" + name + "@" + modified;
    }

    private void add(Capture capture, Map<RootFile, String> roots) throws Exception {
        if (roots.isEmpty()) {
            return;
        }
        db.inAuxTransaction(
                c -> {
                    try (PreparedStatement insert =
                            c.prepareStatement(
                                    "INSERT INTO file_purge_roots (purge_id, kind, name, modified,"
                                            + " content) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO"
                                            + " NOTHING")) {
                        for (Map.Entry<RootFile, String> root : roots.entrySet()) {
                            insert.setLong(1, capture.id());
                            insert.setString(2, root.getKey().kind().name());
                            insert.setString(3, root.getKey().name());
                            insert.setLong(4, root.getKey().modified());
                            insert.setString(5, root.getValue());
                            insert.addBatch();
                        }
                        insert.executeBatch();
                    }
                    return null;
                });
        for (RootFile root : roots.keySet()) {
            capture.captured().add(key(root.kind(), root.name(), root.modified()));
        }
    }

    /**
     * Marks the record applied in the operation's own transaction, so that it is due exactly when
     * the operation has taken effect, after its {@link Operation#delay}: at once, or with {@code
     * inline} and no delay claimed for the caller, who purges it next (the record is returned only
     * then). An operation with a delay also holds the table's other records back until it has
     * passed: one that never took effect, or another operation's, may reach the files this one
     * left. None without a capture.
     */
    Optional<Pending> applied(Connection c, Optional<Capture> capture, boolean inline)
            throws SQLException {
        if (capture.isEmpty()) {
            return Optional.empty();
        }
        Instant now = clock.instant();
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE file_purges SET applied = true, attempts = ?, last_attempt_at = ?,"
                                + " due_at = ?, not_before = ? WHERE id = ? RETURNING "
                                + COLUMNS)) {
            update.setLong(5, capture.get().id());
            // Which operation, from the record: the claim depends on its delay.
            Operation operation = operationOf(c, capture.get().id());
            boolean claim = inline && operation.delay().isZero();
            Instant due = now.plus(operation.delay());
            update.setInt(1, claim ? 1 : 0);
            update.setTimestamp(2, claim ? Timestamp.from(now) : null);
            update.setTimestamp(3, Timestamp.from(due));
            update.setTimestamp(4, Timestamp.from(claim ? now.plus(TablePurges.LEASE) : due));
            if (!operation.delay().isZero()) {
                try (PreparedStatement hold =
                        c.prepareStatement(
                                "UPDATE file_purges SET due_at = greatest(due_at, ?), not_before ="
                                        + " greatest(not_before, ?) WHERE table_id = ? AND id <>"
                                        + " ?")) {
                    hold.setTimestamp(1, Timestamp.from(due));
                    hold.setTimestamp(2, Timestamp.from(due));
                    hold.setObject(3, tableOf(c, capture.get().id()));
                    hold.setLong(4, capture.get().id());
                    hold.executeUpdate();
                }
            }
            try (ResultSet rs = update.executeQuery()) {
                if (!rs.next()) {
                    throw new IllegalStateException(
                            "Purge record " + capture.get().id() + " vanished");
                }
                Pending pending = pending(rs, capture.get().captured().size());
                return claim ? Optional.of(pending) : Optional.empty();
            }
        }
    }

    private static UUID tableOf(Connection c, long id) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement("SELECT table_id FROM file_purges WHERE id = ?")) {
            select.setLong(1, id);
            try (ResultSet rs = select.executeQuery()) {
                if (!rs.next()) {
                    throw new IllegalStateException("Purge record " + id + " vanished");
                }
                return rs.getObject(1, UUID.class);
            }
        }
    }

    private static Operation operationOf(Connection c, long id) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement("SELECT operation FROM file_purges WHERE id = ?")) {
            select.setLong(1, id);
            try (ResultSet rs = select.executeQuery()) {
                if (!rs.next()) {
                    throw new IllegalStateException("Purge record " + id + " vanished");
                }
                return Operation.valueOf(rs.getString(1));
            }
        }
    }

    // ------------------------------------------------------------------ purging

    /** How one purge ended. */
    public enum Outcome {
        /** What nothing references is gone, and the record. */
        PURGED,
        /** The record stays, with the error, for the next sweep. */
        FAILED,
        /** The table is gone, and with it every file under its root: only the record went. */
        SKIPPED;

        String metric() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    /** The table was dropped while its files were being purged. */
    private static final class TableGone extends Exception {
        private static final long serialVersionUID = 1L;
    }

    /**
     * Deletes what a claimed record's roots reached and nothing references, then the record,
     * renewing the claim while it runs. Never throws: a failure is logged and kept on the record.
     */
    public Outcome purge(Pending record) {
        TablePurges.Renewal renewal = new TablePurges.Renewal(() -> renew(record.id()));
        Outcome outcome;
        String error = null;
        long deleted = 0;
        try {
            deleted = run(record);
            outcome = Outcome.PURGED;
            LOG.info(
                    "Purged {} files of {} branch {} left by {} {}",
                    deleted,
                    record.table(),
                    record.branch(),
                    record.operation(),
                    record.target());
        } catch (TableGone gone) {
            LOG.info(
                    "Not purging the files {} {} left in {}: the table is dropped, and its root"
                            + " with them",
                    record.operation(),
                    record.target(),
                    record.table());
            outcome = Outcome.SKIPPED;
        } catch (Exception e) {
            if (e instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            LOG.warn(
                    "Purging the files {} {} left in {} failed; its record stays for the next"
                            + " sweep",
                    record.operation(),
                    record.target(),
                    record.table(),
                    e);
            String message = String.valueOf(e);
            error = message.length() > ERROR_LENGTH ? message.substring(0, ERROR_LENGTH) : message;
            outcome = Outcome.FAILED;
        }
        // No renewal after this, not even one under way: it would hold a failed record for
        // another lease instead of leaving it due at once.
        renewal.stop();
        String kept = error;
        boolean failure = outcome == Outcome.FAILED;
        try {
            db.inTransaction(c -> failure ? failed(c, record.id(), kept) : delete(c, record.id()));
        } catch (Exception recording) {
            LOG.warn("Recording the outcome of file purge {} failed", record.id(), recording);
        }
        metrics.filePurge(outcome.metric(), deleted);
        return outcome;
    }

    /** The purge itself; returns how many files it deleted. */
    private long run(Pending record) throws Exception {
        Optional<TableRecord> found = db.read(c -> store.tableById(c, record.tableId()));
        if (found.isEmpty() || !found.get().path().equals(record.path())) {
            throw new TableGone();
        }
        TableRecord table = found.get();
        List<Root> roots = roots(record.id());
        Map<String, Optional<List<ExpireFileEntry>>> cache = Reach.newCache();
        Map<String, Reach.Reached> reached =
                readersOfRemoved(table, record, roots, cache)
                        .reached(
                                roots.stream()
                                        .filter(r -> r.kind().references())
                                        .map(Root::snapshot)
                                        .toList());
        // Never a client-chosen location, nor a blob pack Paimon manages (as its own orphan
        // file cleaning leaves them).
        reached.values()
                .removeIf(
                        r ->
                                !files.insideRoot(table.path(), r.path())
                                        || r.name()
                                                .endsWith(
                                                        ManagedBlobReferenceFile
                                                                .MANAGED_BLOB_SUFFIX));
        if (reached.isEmpty()) {
            return 0;
        }
        Set<String> names = reached.keySet();
        Map<String, RootFile> seen = new HashMap<>();
        Set<String> pointers = new HashSet<>();
        Set<String> referenced = new HashSet<>();
        referenced.addAll(referencedBy(table, inventory(table, seen), names, cache));
        referenced.addAll(
                referencedByPointers(
                        table,
                        db.read(c -> store.branchNames(c, table.id())),
                        pointers,
                        names,
                        cache,
                        null));
        long deleted = 0;
        for (Reach.Order order : Reach.Order.values()) {
            List<Reach.Reached> doomed = new ArrayList<>();
            for (Reach.Reached r : reached.values()) {
                if (r.order() == order && !referenced.contains(r.name())) {
                    doomed.add(r);
                }
            }
            for (int from = 0; from < doomed.size(); from += BATCH) {
                List<Reach.Reached> batch =
                        doomed.subList(from, Math.min(doomed.size(), from + BATCH));
                hooks.beforeBatch(record);
                deleted += deleteBatch(table, batch, names, referenced, seen, pointers, cache);
            }
        }
        return deleted;
    }

    /**
     * Under the table's lock and every branch pointer's, so that no commit, tag, branch or rollback
     * through the catalog changes what is referenced meanwhile: reads the roots made since the last
     * look, and deletes what of {@code batch} still nothing references.
     */
    private long deleteBatch(
            TableRecord table,
            List<Reach.Reached> batch,
            Set<String> names,
            Set<String> referenced,
            Map<String, RootFile> seen,
            Set<String> pointers,
            Map<String, Optional<List<ExpireFileEntry>>> cache)
            throws Exception {
        for (int attempt = 1; ; attempt++) {
            try {
                return db.inTransaction(
                        c -> {
                            if (store.lockTableById(c, table.id()).isEmpty()) {
                                throw new TableGone();
                            }
                            store.lockBranches(c, table.id());
                            List<RootFile> fresh = inventory(table, seen);
                            if (!fresh.isEmpty()) {
                                referenced.addAll(referencedBy(table, fresh, names, cache));
                            }
                            referenced.addAll(
                                    referencedByPointers(
                                            table,
                                            store.branchNames(c, table.id()),
                                            pointers,
                                            names,
                                            cache,
                                            c));
                            List<Path> paths = new ArrayList<>();
                            for (Reach.Reached r : batch) {
                                if (!referenced.contains(r.name())) {
                                    paths.add(r.path());
                                }
                            }
                            files.deleteAll(table.path(), paths);
                            return (long) paths.size();
                        });
            } catch (SQLException e) {
                // A lock timeout: a request holds the locks; try again shortly.
                if (!CatalogStore.LOCK_NOT_AVAILABLE.equals(e.getSQLState())
                        || attempt >= LOCK_ATTEMPTS) {
                    throw e;
                }
                Thread.sleep(1000L * attempt);
            }
        }
    }

    /**
     * Every root of every branch now that {@code seen} does not hold yet, which it then does: a
     * root is known by its branch, kind, name and modification time.
     */
    private List<RootFile> inventory(TableRecord table, Map<String, RootFile> seen)
            throws Exception {
        List<RootFile> fresh = new ArrayList<>();
        for (String branch : files.branches(table.path())) {
            for (RootFile root :
                    files.roots(table.path(), branch, Kind.SNAPSHOT, Kind.TAG, Kind.CHANGELOG)) {
                if (seen.putIfAbsent(root.key(), root) == null) {
                    fresh.add(root);
                }
            }
        }
        return fresh;
    }

    /**
     * Of {@code names}, those the catalog's branch pointers reference: the snapshot each pointer
     * serves, from PostgreSQL, which {@code loadSnapshot} answers with even when its file is gone
     * (a rollback stopped half way, say). {@code pointers} holds those already read. Read with
     * {@code c} under the batch's locks, or in a transaction of its own without.
     */
    private Set<String> referencedByPointers(
            TableRecord table,
            List<String> branches,
            Set<String> pointers,
            Set<String> names,
            Map<String, Optional<List<ExpireFileEntry>>> cache,
            @Nullable Connection c)
            throws Exception {
        Set<String> out = new HashSet<>();
        for (String branch : branches) {
            Optional<Records.BranchRecord> pointer =
                    c != null
                            ? store.branch(c, table.id(), branch, CatalogStore.LockMode.NONE)
                            : db.read(
                                    r ->
                                            store.branch(
                                                    r,
                                                    table.id(),
                                                    branch,
                                                    CatalogStore.LockMode.NONE));
            if (pointer.isEmpty() || pointer.get().snapshotJson() == null) {
                continue;
            }
            String key =
                    branch + "/" + pointer.get().snapshotId() + "/" + pointer.get().snapshotUuid();
            if (pointers.add(key)) {
                out.addAll(
                        readersOf(table, branch, cache)
                                .referenced(
                                        List.of(Snapshot.fromJson(pointer.get().snapshotJson())),
                                        names));
            }
        }
        return out;
    }

    /** Of {@code names}, those {@code roots} reference, each branch's read with its readers. */
    private Set<String> referencedBy(
            TableRecord table,
            List<RootFile> roots,
            Set<String> names,
            Map<String, Optional<List<ExpireFileEntry>>> cache)
            throws Exception {
        Map<RootFile, String> contents = files.read(table.path(), roots);
        Map<String, List<Snapshot>> byBranch = new LinkedHashMap<>();
        for (Map.Entry<RootFile, String> root : contents.entrySet()) {
            RootFile file = root.getKey();
            byBranch.computeIfAbsent(file.branch(), b -> new ArrayList<>())
                    .add(
                            new Root(file.kind(), file.name(), file.modified(), root.getValue())
                                    .snapshot());
        }
        Set<String> out = new HashSet<>();
        for (Map.Entry<String, List<Snapshot>> branch : byBranch.entrySet()) {
            out.addAll(
                    readersOf(table, branch.getKey(), cache).referenced(branch.getValue(), names));
        }
        return out;
    }

    /** A branch's manifest readers; main's for a branch without a schema of its own. */
    private Reach readersOf(
            TableRecord table, String branch, Map<String, Optional<List<ExpireFileEntry>>> cache) {
        FileStoreTable fileTable;
        try {
            fileTable = files.table(table.path(), branch);
        } catch (RuntimeException e) {
            fileTable = files.table(table.path(), Identifier.DEFAULT_MAIN_BRANCH);
        }
        return new Reach(fileTable, cache, TableFiles.io());
    }

    /** The readers of the removed roots: the deleted branch's, from the schema it kept. */
    private Reach readersOfRemoved(
            TableRecord table,
            Pending record,
            List<Root> roots,
            Map<String, Optional<List<ExpireFileEntry>>> cache) {
        if (record.operation() == Operation.DROP_BRANCH) {
            for (Root root : roots) {
                if (root.kind() == Kind.SCHEMA) {
                    FileStoreTable fileTable =
                            FileStoreTableFactory.create(
                                    files.tableIO(table.path()),
                                    new Path(table.path()),
                                    TableSchema.fromJson(root.content()),
                                    new Options(),
                                    CatalogEnvironment.empty());
                    return new Reach(fileTable, cache, TableFiles.io());
                }
            }
            return readersOf(table, Identifier.DEFAULT_MAIN_BRANCH, cache);
        }
        return readersOf(table, record.branch(), cache);
    }

    private List<Root> roots(long id) throws Exception {
        return db.read(
                c -> {
                    List<Root> out = new ArrayList<>();
                    try (PreparedStatement select =
                            c.prepareStatement(
                                    "SELECT kind, name, modified, content FROM file_purge_roots"
                                            + " WHERE purge_id = ? ORDER BY kind, name")) {
                        select.setLong(1, id);
                        try (ResultSet rs = select.executeQuery()) {
                            while (rs.next()) {
                                out.add(
                                        new Root(
                                                Kind.valueOf(rs.getString(1)),
                                                rs.getString(2),
                                                rs.getLong(3),
                                                rs.getString(4)));
                            }
                        }
                    }
                    return out;
                });
    }

    // ------------------------------------------------------------------ sweeps and listing

    /**
     * Claims and purges due records one at a time, oldest first, each at most once, until none is
     * due or {@code deadline} (when given) is less than {@link TablePurges#MARGIN} away; then
     * reports what is left.
     */
    public Sweep sweep(@Nullable Instant deadline) throws Exception {
        Set<Long> tried = new HashSet<>();
        Map<Outcome, Integer> done = new LinkedHashMap<>();
        while (deadline == null || clock.instant().plus(TablePurges.MARGIN).isBefore(deadline)) {
            Optional<Pending> next = db.inTransaction(c -> claim(c, tried));
            if (next.isEmpty()) {
                break;
            }
            tried.add(next.get().id());
            done.merge(purge(next.get()), 1, Integer::sum);
        }
        Sweep sweep =
                db.inTransaction(
                        c -> {
                            // A record's age counts from when it is due.
                            try (PreparedStatement select =
                                            c.prepareStatement(
                                                    "SELECT count(*), min(due_at) FROM"
                                                            + " file_purges");
                                    ResultSet rs = select.executeQuery()) {
                                rs.next();
                                Timestamp oldest = rs.getTimestamp(2);
                                return new Sweep(
                                        done.getOrDefault(Outcome.PURGED, 0),
                                        done.getOrDefault(Outcome.FAILED, 0),
                                        done.getOrDefault(Outcome.SKIPPED, 0),
                                        rs.getInt(1),
                                        oldest == null
                                                ? 0
                                                : Math.max(
                                                        0,
                                                        Duration.between(
                                                                        oldest.toInstant(),
                                                                        clock.instant())
                                                                .toSeconds()));
                            }
                        });
        if (!done.isEmpty()) {
            LOG.info(
                    "File purge sweep: {} purged, {} failed, {} skipped; {} left, the oldest {} s",
                    sweep.purged(),
                    sweep.failed(),
                    sweep.skipped(),
                    sweep.pending(),
                    sweep.oldestSeconds());
        }
        metrics.filePurgesLeft(sweep.pending(), sweep.oldestSeconds());
        return sweep;
    }

    /** The records, oldest first, at most {@code limit} of them. */
    public List<Pending> list(int limit) throws Exception {
        return db.read(
                c -> {
                    List<Pending> out = new ArrayList<>();
                    try (PreparedStatement select =
                            c.prepareStatement(
                                    "SELECT "
                                            + COLUMNS
                                            + ", (SELECT count(*) FROM file_purge_roots r WHERE"
                                            + " r.purge_id = file_purges.id) AS roots FROM"
                                            + " file_purges ORDER BY id LIMIT ?")) {
                        select.setInt(1, limit);
                        try (ResultSet rs = select.executeQuery()) {
                            while (rs.next()) {
                                out.add(pending(rs, rs.getInt("roots")));
                            }
                        }
                    }
                    return out;
                });
    }

    /** How many records there are. */
    public int count() throws Exception {
        return db.read(
                c -> {
                    try (PreparedStatement select =
                                    c.prepareStatement("SELECT count(*) FROM file_purges");
                            ResultSet rs = select.executeQuery()) {
                        rs.next();
                        return rs.getInt(1);
                    }
                });
    }

    private static final String COLUMNS =
            "id, table_id, table_name, path, branch, operation, target, created_at, created_by,"
                    + " request_id, applied, attempts, last_attempt_at, last_error, due_at,"
                    + " not_before";

    private Optional<Pending> claim(Connection c, Collection<Long> tried) throws SQLException {
        Instant now = clock.instant();
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE file_purges SET attempts = attempts + 1, last_attempt_at = ?,"
                                + " not_before = ? WHERE id = (SELECT id FROM file_purges WHERE"
                                + " not_before <= ? AND NOT (id = ANY (?)) ORDER BY id LIMIT 1 FOR"
                                + " UPDATE SKIP LOCKED) RETURNING "
                                + COLUMNS)) {
            update.setTimestamp(1, Timestamp.from(now));
            update.setTimestamp(2, Timestamp.from(now.plus(TablePurges.LEASE)));
            update.setTimestamp(3, Timestamp.from(now));
            update.setArray(4, c.createArrayOf("bigint", tried.toArray()));
            try (ResultSet rs = update.executeQuery()) {
                return rs.next() ? Optional.of(pending(rs, 0)) : Optional.empty();
            }
        }
    }

    private void renew(long id) {
        try {
            db.inTransaction(
                    c -> {
                        try (PreparedStatement update =
                                c.prepareStatement(
                                        "UPDATE file_purges SET not_before = ? WHERE id = ?")) {
                            update.setTimestamp(
                                    1, Timestamp.from(clock.instant().plus(TablePurges.LEASE)));
                            update.setLong(2, id);
                            update.executeUpdate();
                        }
                        return null;
                    });
        } catch (Exception e) {
            LOG.warn("Renewing the claim of file purge {} failed", id, e);
        }
    }

    private Void failed(Connection c, long id, String error) throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE file_purges SET last_error = ?, not_before = ? WHERE id = ?")) {
            update.setString(1, error);
            update.setTimestamp(2, Timestamp.from(clock.instant()));
            update.setLong(3, id);
            update.executeUpdate();
        }
        return null;
    }

    private static Void delete(Connection c, long id) throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement("DELETE FROM file_purges WHERE id = ?")) {
            delete.setLong(1, id);
            delete.executeUpdate();
        }
        return null;
    }

    private static Pending pending(ResultSet rs, int roots) throws SQLException {
        Timestamp last = rs.getTimestamp("last_attempt_at");
        return new Pending(
                rs.getLong("id"),
                rs.getObject("table_id", UUID.class),
                rs.getString("table_name"),
                rs.getString("path"),
                rs.getString("branch"),
                Operation.valueOf(rs.getString("operation")),
                rs.getString("target"),
                rs.getTimestamp("created_at").toInstant(),
                rs.getString("created_by"),
                rs.getString("request_id"),
                rs.getBoolean("applied"),
                rs.getInt("attempts"),
                last == null ? null : last.toInstant(),
                rs.getString("last_error"),
                rs.getTimestamp("due_at").toInstant(),
                rs.getTimestamp("not_before").toInstant(),
                roots);
    }
}
