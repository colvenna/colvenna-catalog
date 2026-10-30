package com.colvenna.catalog.catalog;

import com.colvenna.catalog.audit.AuditLog;
import com.colvenna.catalog.audit.AuditLog.Outcome;
import com.colvenna.catalog.catalog.CatalogStore.LockMode;
import com.colvenna.catalog.catalog.CatalogStore.PartitionRow;
import com.colvenna.catalog.catalog.Records.BranchRecord;
import com.colvenna.catalog.catalog.Records.CommitIntent;
import com.colvenna.catalog.catalog.Records.IntentState;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.Records.TableState;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.fs.Roots;
import com.colvenna.catalog.fs.SnapshotPublisher;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.http.ApiException;
import com.colvenna.catalog.http.Resources;
import java.io.FileNotFoundException;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import javax.annotation.Nullable;
import org.apache.paimon.CoreOptions;
import org.apache.paimon.Snapshot;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.Path;
import org.apache.paimon.partition.PartitionStatistics;
import org.apache.paimon.partition.PartitionUtils;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.requests.CommitTableRequest;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.core.type.TypeReference;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Instant;
import org.apache.paimon.table.TableSnapshot;
import org.apache.paimon.tag.Tag;
import org.apache.paimon.utils.SnapshotManager;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Atomic snapshot publication for one table branch.
 *
 * <p>A commit runs under the branch's PostgreSQL row lock:
 *
 * <ol>
 *   <li>resolve intents left by earlier attempts, so the pointer reflects every snapshot file that
 *       was actually published;
 *   <li>compare the client's base snapshot UUID with the pointer (compare-and-set);
 *   <li>record a commit intent in its own transaction;
 *   <li>publish {@code snapshot-N} with an atomic create-if-absent;
 *   <li>advance the pointer and mark the intent published in the locked transaction.
 * </ol>
 *
 * <p>The snapshot file is the linearization point: once it exists the commit happened, whatever the
 * PostgreSQL transaction or the HTTP response did afterwards. A later commit or snapshot load rolls
 * the pointer forward to it. A snapshot file that no pending intent accounts for was written by a
 * writer outside this catalog; the table is fenced and every further commit is rejected.
 */
public final class CommitService {

    private static final Logger LOG = LoggerFactory.getLogger(CommitService.class);

    /**
     * After this long an unpublished pending intent can no longer land: it is longer than any S3
     * call timeout the configuration accepts ({@code COLVENNA_S3_CALL_TIMEOUT}).
     */
    public static final Duration ABANDON_AFTER = Duration.ofMinutes(5);

    private static final TypeReference<List<PartitionStatistics>> STATISTICS =
            new TypeReference<>() {};

    /** What happened to a commit request, with the facts the audit record keeps. */
    public record Result(boolean committed, String detail, Snapshot snapshot) {}

    /**
     * Points where tests stop a commit as a crashed replica would: before the snapshot file is
     * published, and after it is published but before PostgreSQL records it.
     */
    public interface Faults {
        Faults NONE = new Faults() {};

        default void beforePublish(TableRecord table, Snapshot snapshot) {}

        default void afterPublish(TableRecord table, Snapshot snapshot) {}
    }

    private final Db db;
    private final CatalogStore store;
    private final TableFiles files;
    private final SnapshotPublisher publisher;
    private final AuditLog audit;
    private final Clock clock;
    private final Faults faults;

    public CommitService(
            Db db,
            CatalogStore store,
            TableFiles files,
            SnapshotPublisher publisher,
            AuditLog audit,
            Clock clock,
            Faults faults) {
        this.db = db;
        this.store = store;
        this.files = files;
        this.publisher = publisher;
        this.audit = audit;
        this.clock = clock;
        this.faults = faults;
    }

    /** Publishes the snapshot, or reports a lost compare-and-set with {@code committed=false}. */
    public Result commit(
            RequestContext ctx, TableRecord table, String branch, CommitTableRequest request)
            throws Exception {
        Snapshot snapshot = request.getSnapshot();
        if (snapshot == null) {
            throw ApiException.badRequest("Commit request has no snapshot.");
        }
        if (!table.fileStore()) {
            throw ApiException.notImplemented(
                    "Table "
                            + table.fullName()
                            + " of type "
                            + table.tableType()
                            + " has no snapshots.");
        }
        if (request.getTableId() != null && !request.getTableId().equals(table.id().toString())) {
            // The writer loaded a table that has since been dropped and recreated under this name.
            throw ApiException.notFound(
                    ErrorResponse.RESOURCE_TYPE_TABLE,
                    table.name(),
                    "Table id " + request.getTableId() + " is not the current " + table.fullName());
        }
        rejectIfFenced(table);
        List<PartitionStatistics> statistics =
                request.getStatistics() == null ? List.of() : request.getStatistics();
        return db.inTransaction(
                c -> {
                    BranchRecord pointer =
                            resolvePending(c, ctx, table, branch, lockPointer(c, table, branch));
                    rejectIfFenced(
                            store.table(c, table.database(), table.name(), false).orElse(table));
                    if (!Objects.equals(pointer.snapshotUuid(), request.getBaseSnapshotUuid())) {
                        if (snapshot.uuid() != null
                                && snapshot.uuid().equals(pointer.snapshotUuid())) {
                            return new Result(true, "replay of a committed request", snapshot);
                        }
                        return new Result(
                                false,
                                "base snapshot "
                                        + request.getBaseSnapshotUuid()
                                        + " is not the latest "
                                        + pointer.snapshotUuid(),
                                snapshot);
                    }
                    if (pointer.snapshotId() != null && snapshot.id() != pointer.snapshotId() + 1) {
                        throw ApiException.badRequest(
                                "Snapshot "
                                        + snapshot.id()
                                        + " does not follow latest snapshot "
                                        + pointer.snapshotId()
                                        + " of "
                                        + table.fullName());
                    }
                    long intentId =
                            recordIntent(
                                    ctx,
                                    table,
                                    branch,
                                    snapshot,
                                    request.getBaseSnapshotUuid(),
                                    statistics);
                    Path path = snapshotPath(table, branch, snapshot.id());
                    FileIO fileIO = files.tableIO(table.path());
                    faults.beforePublish(table, snapshot);
                    SnapshotPublisher.Outcome outcome =
                            publisher.createIfAbsent(fileIO, path, snapshot.toJson());
                    if (outcome == SnapshotPublisher.Outcome.ALREADY_EXISTS) {
                        Snapshot existing = Snapshot.fromJson(fileIO.readFileUtf8(path));
                        if (!Objects.equals(existing.uuid(), snapshot.uuid())) {
                            // Another snapshot holds the slot. Adopt it if a pending intent of
                            // this catalog published it; otherwise another writer is committing.
                            resolveAutonomously(
                                    intentId,
                                    IntentState.ABORTED,
                                    "slot taken by " + existing.uuid());
                            if (!adoptOrFence(c, ctx, table, branch, pointer, existing)) {
                                throw fencedConflict(table, existing, branch);
                            }
                            return new Result(
                                    false,
                                    "slot "
                                            + snapshot.id()
                                            + " taken by snapshot "
                                            + existing.uuid(),
                                    snapshot);
                        }
                        // Our own write, retried by the S3 client after an unacknowledged success.
                    }
                    faults.afterPublish(table, snapshot);
                    writeLatestHint(table, branch, snapshot.id());
                    publish(c, table, branch, pointer, snapshot, statistics, ctx.who());
                    store.resolveIntent(c, intentId, IntentState.PUBLISHED, "committed");
                    return new Result(true, "published", snapshot);
                });
    }

    /**
     * A rollback sent again by the same caller within this long of the last one is taken for its
     * retry (#93): longer than Paimon's client spends retrying a request (5 retries over about 31
     * seconds, each answered within the gateway's 29).
     */
    public static final Duration ROLLBACK_RETRY_WINDOW = Duration.ofMinutes(5);

    /**
     * Rolls a branch back to a snapshot or tag; {@code fromSnapshot} guards the current latest.
     *
     * <p>As Paimon's own rollback ({@code RollbackHelper.cleanLargerThan}), it removes the
     * snapshots, long-lived changelogs and tags after the target, and moves the latest hint; it
     * does so {@link TableFiles#PARALLELISM} files at a time, listing the directories once rather
     * than asking for each snapshot id. What they referenced and nothing else does is left to a
     * purge (#93), recorded first: its record is returned when the caller is to purge it now
     * ({@code inline}, and the operation waits for nothing: {@link FilePurges#applied}); otherwise
     * the sweeps take it. A request whose time ({@code deadline}, in {@link System#nanoTime()}, 0
     * for none) runs out while recording answers 503 having changed nothing; sent again, it goes on
     * recording where it stopped.
     *
     * <p>A rollback is applied once however often it is sent, when the same caller sends the same
     * rollback (branch, target and {@code fromSnapshot}) within {@link #ROLLBACK_RETRY_WINDOW}
     * after it took effect: while the branch is still where it left it, a guarded one answers as it
     * did and an unguarded one changes nothing; once commits have moved it, either answers 409
     * rather than removing them.
     */
    public Optional<FilePurges.Pending> rollback(
            RequestContext ctx,
            TableRecord table,
            String branch,
            Instant instant,
            @Nullable Long fromSnapshot,
            FilePurges purges,
            boolean inline,
            long deadline)
            throws Exception {
        rejectIfFenced(table);
        String target = rollbackTarget(instant);
        return db.inTransaction(
                c -> {
                    // The table row first, as tag changes do, then the branch pointer.
                    if (store.lockTableById(c, table.id()).isEmpty()) {
                        throw ApiException.notFound(
                                ErrorResponse.RESOURCE_TYPE_TABLE,
                                table.name(),
                                "Table " + table.fullName() + " does not exist.");
                    }
                    BranchRecord pointer =
                            resolvePending(c, ctx, table, branch, lockPointer(c, table, branch));
                    Optional<CatalogStore.Rollback> last =
                            store.lastRollback(c, table.id(), branch);
                    boolean again =
                            last.isPresent()
                                    && last.get().by().equals(ctx.who())
                                    && last.get().target().equals(target)
                                    && Objects.equals(last.get().fromSnapshot(), fromSnapshot)
                                    && last.get()
                                            .at()
                                            .plus(ROLLBACK_RETRY_WINDOW)
                                            .isAfter(clock.instant());
                    boolean moved =
                            again && !Objects.equals(pointer.snapshotId(), last.get().snapshotId());
                    if (moved) {
                        // Taken for that rollback sent again, after commits: applying it again
                        // would remove them, guard or not (a commit may have brought the pointer
                        // back to the guard's id).
                        throw ApiException.conflict(
                                ErrorResponse.RESOURCE_TYPE_SNAPSHOT,
                                String.valueOf(last.get().snapshotId()),
                                String.format(
                                        "Branch %s of %s was rolled back to %s by %s at %s and has"
                                                + " had commits since; this is taken for that"
                                                + " rollback sent again, and not applied again. To"
                                                + " roll back once more, wait %s%s.",
                                        branch,
                                        table.fullName(),
                                        target,
                                        last.get().by(),
                                        last.get().at(),
                                        ROLLBACK_RETRY_WINDOW,
                                        fromSnapshot == null
                                                ? ", or send fromSnapshot "
                                                        + pointer.snapshotId()
                                                        + " (the latest snapshot)"
                                                : ""));
                    }
                    if (fromSnapshot != null && !fromSnapshot.equals(pointer.snapshotId())) {
                        if (again) {
                            // This rollback's retry, nothing committed since: it took effect, and
                            // answers as it did.
                            return Optional.<FilePurges.Pending>empty();
                        }
                        throw new ApiException(
                                500,
                                null,
                                null,
                                String.format(
                                        "Latest snapshot %s is not %s",
                                        pointer.snapshotId(), fromSnapshot));
                    }
                    FileStoreTable fileTable = files.table(table.path(), branch);
                    Snapshot targetSnapshot;
                    boolean fromTag;
                    if (instant instanceof Instant.SnapshotInstant snapshotInstant) {
                        long id = snapshotInstant.getSnapshotId();
                        if (!fileTable.snapshotManager().snapshotExists(id)) {
                            throw ApiException.notFound(
                                    ErrorResponse.RESOURCE_TYPE_SNAPSHOT,
                                    String.valueOf(id),
                                    "Snapshot " + id + " does not exist.");
                        }
                        targetSnapshot = fileTable.snapshotManager().snapshot(id);
                        fromTag = false;
                    } else {
                        String tag = ((Instant.TagInstant) instant).getTagName();
                        if (!fileTable.tagManager().tagExists(tag)) {
                            throw ApiException.notFound(
                                    ErrorResponse.RESOURCE_TYPE_TAG,
                                    tag,
                                    "Tag " + tag + " does not exist.");
                        }
                        targetSnapshot = fileTable.tagManager().getOrThrow(tag).trimToSnapshot();
                        fromTag = true;
                    }
                    long kept = targetSnapshot.id();
                    // What the rollback removes: every snapshot, long-lived changelog and tag
                    // after the target.
                    List<Roots.RootFile> snapshots = new ArrayList<>();
                    List<Roots.RootFile> changelogs = new ArrayList<>();
                    boolean changelogsKept = false;
                    for (Roots.RootFile root :
                            files.roots(
                                    table.path(),
                                    branch,
                                    Roots.Kind.SNAPSHOT,
                                    Roots.Kind.CHANGELOG)) {
                        if (root.kind() == Roots.Kind.SNAPSHOT && root.id() > kept) {
                            snapshots.add(root);
                        } else if (root.kind() == Roots.Kind.CHANGELOG) {
                            if (root.id() > kept) {
                                changelogs.add(root);
                            } else {
                                changelogsKept = true;
                            }
                        }
                    }
                    Map<Roots.RootFile, String> tags =
                            files.read(
                                    table.path(),
                                    files.roots(table.path(), branch, Roots.Kind.TAG));
                    List<Roots.RootFile> removedTags = new ArrayList<>();
                    tags.forEach(
                            (root, content) -> {
                                if (Tag.fromJson(content).trimToSnapshot().id() > kept) {
                                    removedTags.add(root);
                                }
                            });
                    // Newest first, as Paimon removes them: stopped half way, what is left is the
                    // older snapshots.
                    snapshots.sort(Comparator.comparingLong(Roots.RootFile::id).reversed());
                    changelogs.sort(Comparator.comparingLong(Roots.RootFile::id).reversed());
                    List<Roots.RootFile> removed = new ArrayList<>(snapshots);
                    removed.addAll(changelogs);
                    removed.addAll(removedTags);
                    long recording = System.nanoTime();
                    Optional<FilePurges.Capture> capture =
                            removed.isEmpty()
                                    ? Optional.empty()
                                    : Optional.of(
                                            purges.capture(
                                                    table,
                                                    branch,
                                                    FilePurges.Operation.ROLLBACK,
                                                    target,
                                                    ctx,
                                                    removed,
                                                    tags,
                                                    deadline));
                    long deleting = System.nanoTime();
                    SnapshotManager snapshotManager = fileTable.snapshotManager();
                    snapshotManager.commitLatestHint(kept);
                    files.deleteAll(
                            table.path(), snapshots.stream().map(Roots.RootFile::path).toList());
                    if (!changelogs.isEmpty()) {
                        fileTable
                                .changelogManager()
                                .commitLongLivedChangelogLatestHint(changelogsKept ? kept : -1);
                        files.deleteAll(
                                table.path(),
                                changelogs.stream().map(Roots.RootFile::path).toList());
                    }
                    files.deleteAll(
                            table.path(), removedTags.stream().map(Roots.RootFile::path).toList());
                    if (fromTag && !snapshotManager.snapshotExists(kept)) {
                        // The tag outlived its snapshot: it becomes the earliest snapshot again.
                        fileTable
                                .fileIO()
                                .writeFile(
                                        snapshotManager.snapshotPath(kept),
                                        targetSnapshot.toJson(),
                                        false);
                        snapshotManager.commitEarliestHint(kept);
                    }
                    LOG.info(
                            "Rolled {} branch {} back to snapshot {}: {} snapshots, {} changelogs"
                                    + " and {} tags removed; recorded in {} ms, removed in {} ms",
                            table.fullName(),
                            branch,
                            kept,
                            snapshots.size(),
                            changelogs.size(),
                            removedTags.size(),
                            (deleting - recording) / 1_000_000,
                            (System.nanoTime() - deleting) / 1_000_000);
                    for (CommitIntent intent : store.pendingIntents(c, table.id(), branch)) {
                        store.resolveIntent(c, intent.id(), IntentState.ABORTED, "rolled back");
                    }
                    store.updateBranch(
                            c,
                            new BranchRecord(
                                    table.id(),
                                    branch,
                                    targetSnapshot.id(),
                                    targetSnapshot.uuid(),
                                    targetSnapshot.toJson(),
                                    targetSnapshot.totalRecordCount(),
                                    0,
                                    0,
                                    0),
                            now(),
                            ctx.who());
                    store.setLastRollback(
                            c,
                            table.id(),
                            branch,
                            new CatalogStore.Rollback(
                                    ctx.who(),
                                    target,
                                    fromSnapshot,
                                    targetSnapshot.id(),
                                    clock.instant()));
                    return purges.applied(c, capture, inline);
                });
    }

    /** What a rollback asks for, as its purge record and its retry name it. */
    private static String rollbackTarget(Instant instant) {
        if (instant instanceof Instant.SnapshotInstant snapshot) {
            return "snapshot:" + snapshot.getSnapshotId();
        }
        if (instant instanceof Instant.TagInstant tag) {
            Names.validate("tag", tag.getTagName());
            return "tag:" + tag.getTagName();
        }
        throw ApiException.badRequest("Unsupported rollback instant " + instant);
    }

    /**
     * Returns the latest published snapshot of a branch. Pending intents are resolved first when
     * the branch is not locked by a running commit, so a commit whose response was lost becomes
     * visible to the retrying client.
     */
    public Optional<BranchRecord> latest(RequestContext ctx, TableRecord table, String branch)
            throws Exception {
        return db.inTransaction(
                c -> {
                    if (store.hasPendingIntent(c, table.id(), branch)) {
                        Optional<BranchRecord> locked =
                                store.branch(c, table.id(), branch, LockMode.SKIP_LOCKED);
                        if (locked.isPresent()) {
                            return Optional.of(resolvePending(c, ctx, table, branch, locked.get()));
                        }
                    }
                    Optional<BranchRecord> pointer =
                            store.branch(c, table.id(), branch, LockMode.NONE);
                    if (pointer.isPresent()) {
                        return pointer;
                    }
                    return Optional.of(lockPointer(c, table, branch));
                });
    }

    /**
     * Re-reads the branch head from the file system after the server itself changed the snapshot
     * files under the branch lock (rollback, replace, fast-forward).
     */
    public void resetPointerFromFiles(Connection c, TableRecord table, String branch, String who)
            throws Exception {
        lockPointer(c, table, branch);
        for (CommitIntent intent : store.pendingIntents(c, table.id(), branch)) {
            store.resolveIntent(c, intent.id(), IntentState.ABORTED, "branch head replaced");
        }
        store.updateBranch(c, pointerFromFiles(table, branch), now(), who);
    }

    /** Locks the pointer row, creating it from the file system head when it does not exist yet. */
    BranchRecord lockPointer(Connection c, TableRecord table, String branch) throws Exception {
        Optional<BranchRecord> pointer = store.branch(c, table.id(), branch, LockMode.FOR_UPDATE);
        if (pointer.isPresent()) {
            return pointer.get();
        }
        store.insertBranch(c, pointerFromFiles(table, branch), now(), "catalog");
        return store.branch(c, table.id(), branch, LockMode.FOR_UPDATE)
                .orElseThrow(() -> new IllegalStateException("Branch pointer vanished"));
    }

    BranchRecord pointerFromFiles(TableRecord table, String branch) {
        Snapshot latest =
                table.fileStore()
                        ? snapshotManager(table, branch).latestSnapshotFromFileSystem()
                        : null;
        if (latest == null) {
            return new BranchRecord(table.id(), branch, null, null, null, 0, 0, 0, 0);
        }
        return new BranchRecord(
                table.id(),
                branch,
                latest.id(),
                latest.uuid(),
                latest.toJson(),
                latest.totalRecordCount(),
                0,
                0,
                0);
    }

    private BranchRecord resolvePending(
            Connection c,
            RequestContext ctx,
            TableRecord table,
            String branch,
            BranchRecord pointer)
            throws Exception {
        BranchRecord current = pointer;
        for (CommitIntent intent : store.pendingIntents(c, table.id(), branch)) {
            if (current.snapshotId() != null && intent.snapshotId() <= current.snapshotId()) {
                store.resolveIntent(
                        c,
                        intent.id(),
                        IntentState.ABORTED,
                        "slot " + intent.snapshotId() + " already published");
                continue;
            }
            Path path = snapshotPath(table, branch, intent.snapshotId());
            FileIO fileIO = files.tableIO(table.path());
            Snapshot existing;
            try {
                existing = Snapshot.fromJson(fileIO.readFileUtf8(path));
            } catch (FileNotFoundException e) {
                if (intent.createdAt().plus(ABANDON_AFTER).isBefore(clock.instant())) {
                    store.resolveIntent(
                            c, intent.id(), IntentState.ABORTED, "snapshot never published");
                }
                continue;
            }
            if (Objects.equals(existing.uuid(), intent.snapshotUuid())) {
                LOG.warn(
                        "Rolling {} branch {} forward to snapshot {} published by intent {}",
                        table.fullName(),
                        branch,
                        existing.id(),
                        intent.id());
                current =
                        publish(
                                c,
                                table,
                                branch,
                                current,
                                existing,
                                statistics(intent.statisticsJson()),
                                ctx.who());
                store.resolveIntent(c, intent.id(), IntentState.PUBLISHED, "rolled forward");
                audit.record(
                        new AuditLog.Event(
                                ctx.requestId(),
                                ctx.who(),
                                "ROLL_FORWARD_COMMIT",
                                Resources.TABLE,
                                table.fullName(),
                                Outcome.SUCCEEDED,
                                200,
                                Map.of(
                                        "branch", branch,
                                        "snapshotId", existing.id(),
                                        "intentId", intent.id())));
            } else {
                store.resolveIntent(
                        c, intent.id(), IntentState.ABORTED, "slot taken by " + existing.uuid());
                if (adoptOrFence(c, ctx, table, branch, current, existing)) {
                    current = store.branch(c, table.id(), branch, LockMode.NONE).orElse(current);
                }
            }
        }
        return current;
    }

    /**
     * Takes over a snapshot that a still pending intent of this catalog published and returns
     * {@code true}, or fences the table and returns {@code false}. An intent that was already
     * closed (by a rollback, or because its slot was taken) is never adopted: a late write of it is
     * treated like any other foreign snapshot.
     */
    private boolean adoptOrFence(
            Connection c,
            RequestContext ctx,
            TableRecord table,
            String branch,
            BranchRecord pointer,
            Snapshot existing)
            throws Exception {
        Optional<CommitIntent> owner =
                existing.uuid() == null
                        ? Optional.empty()
                        : store.intentForSnapshot(
                                c, table.id(), branch, existing.id(), existing.uuid());
        boolean follows = pointer.snapshotId() == null || existing.id() == pointer.snapshotId() + 1;
        if (owner.isPresent() && owner.get().state() == IntentState.PENDING && follows) {
            publish(
                    c,
                    table,
                    branch,
                    pointer,
                    existing,
                    statistics(owner.get().statisticsJson()),
                    ctx.who());
            store.resolveIntent(
                    c, owner.get().id(), IntentState.PUBLISHED, "adopted late publication");
            return true;
        }
        String reason = foreignSnapshotReason(existing, branch);
        if (owner.isPresent()) {
            reason +=
                    " The snapshot belongs to commit intent "
                            + owner.get().id()
                            + ", which was "
                            + owner.get().state()
                            + " when it landed.";
        }
        fence(ctx, table, reason);
        return false;
    }

    private static String foreignSnapshotReason(Snapshot existing, String branch) {
        return "Snapshot "
                + existing.id()
                + " of branch "
                + branch
                + " (uuid "
                + existing.uuid()
                + ", commit user "
                + existing.commitUser()
                + ") was not published by colvenna-catalog; another catalog or writer is committing to"
                + " this table.";
    }

    private static ApiException fencedConflict(
            TableRecord table, Snapshot existing, String branch) {
        return ApiException.fenced(
                ErrorResponse.RESOURCE_TYPE_TABLE,
                table.name(),
                "Table "
                        + table.fullName()
                        + " is fenced: "
                        + foreignSnapshotReason(existing, branch));
    }

    private BranchRecord publish(
            Connection c,
            TableRecord table,
            String branch,
            BranchRecord pointer,
            Snapshot snapshot,
            List<PartitionStatistics> statistics,
            String who)
            throws Exception {
        long recordCount = pointer.recordCount();
        long fileSize = pointer.fileSizeInBytes();
        long fileCount = pointer.fileCount();
        long lastCreation = pointer.lastFileCreationTime();
        for (PartitionStatistics stats : statistics) {
            recordCount += stats.recordCount();
            fileSize += stats.fileSizeInBytes();
            fileCount += stats.fileCount();
            lastCreation = Math.max(lastCreation, stats.lastFileCreationTime());
        }
        BranchRecord next =
                new BranchRecord(
                        table.id(),
                        branch,
                        snapshot.id(),
                        snapshot.uuid(),
                        snapshot.toJson(),
                        recordCount,
                        fileSize,
                        fileCount,
                        lastCreation);
        long now = now();
        store.updateBranch(c, next, now, who);
        if (CoreOptions.fromMap(table.schema().options()).partitionedTableInMetastore()) {
            for (PartitionStatistics stats : statistics) {
                store.upsertPartition(
                        c,
                        table.id(),
                        branch,
                        new PartitionRow(
                                PartitionUtils.buildPartitionName(stats.spec()),
                                new LinkedHashMap<>(stats.spec()),
                                stats.recordCount(),
                                stats.fileSizeInBytes(),
                                stats.fileCount(),
                                stats.lastFileCreationTime(),
                                stats.totalBuckets(),
                                false,
                                Collections.emptyMap(),
                                now,
                                who,
                                now,
                                who));
            }
            store.removeEmptyPartitions(c, table.id(), branch);
        }
        return next;
    }

    private long recordIntent(
            RequestContext ctx,
            TableRecord table,
            String branch,
            Snapshot snapshot,
            @Nullable String baseUuid,
            List<PartitionStatistics> statistics)
            throws Exception {
        String statisticsJson = RESTApi.toJson(new ArrayList<>(statistics));
        return db.inAuxTransaction(
                c ->
                        store.insertIntent(
                                c,
                                table.id(),
                                branch,
                                snapshot.id(),
                                snapshot.uuid(),
                                baseUuid,
                                snapshot.commitUser(),
                                snapshot.commitIdentifier(),
                                statisticsJson,
                                ctx.who(),
                                ctx.requestId()));
    }

    private void resolveAutonomously(long intentId, IntentState state, String resolution) {
        try {
            db.inAuxTransaction(
                    c -> {
                        store.resolveIntent(c, intentId, state, resolution);
                        return null;
                    });
        } catch (Exception e) {
            LOG.warn("Could not resolve commit intent {}", intentId, e);
        }
    }

    /**
     * Marks the table fenced in its own transaction, so the fence holds even when the request that
     * found the foreign snapshot fails. Commits keep checking the state; a fence that cannot be
     * written still rejects this commit and is retried by the next one.
     */
    private void fence(RequestContext ctx, TableRecord table, String reason) throws Exception {
        LOG.error("Fencing table {}: {}", table.fullName(), reason);
        db.inAuxTransaction(
                c -> {
                    store.setTableState(c, table.id(), TableState.FENCED, reason);
                    return null;
                });
        audit.record(
                new AuditLog.Event(
                        ctx.requestId(),
                        ctx.who(),
                        "FENCE_TABLE",
                        Resources.TABLE,
                        table.fullName(),
                        Outcome.SUCCEEDED,
                        409,
                        Map.of("reason", reason)));
    }

    private static void rejectIfFenced(TableRecord table) {
        if (table.fenced()) {
            throw ApiException.fenced(
                    ErrorResponse.RESOURCE_TYPE_TABLE,
                    table.name(),
                    "Table "
                            + table.fullName()
                            + " is fenced and accepts no commits: "
                            + table.stateReason());
        }
    }

    private void writeLatestHint(TableRecord table, String branch, long snapshotId) {
        try {
            snapshotManager(table, branch).commitLatestHint(snapshotId);
        } catch (Exception e) {
            // The hint only speeds up file-system listing; the snapshot file is authoritative.
            LOG.warn("Could not update LATEST hint of {} branch {}", table.fullName(), branch, e);
        }
    }

    private SnapshotManager snapshotManager(TableRecord table, String branch) {
        return files.snapshotManager(table.path(), branch);
    }

    private Path snapshotPath(TableRecord table, String branch, long snapshotId) {
        return snapshotManager(table, branch).snapshotPath(snapshotId);
    }

    static TableSnapshot toTableSnapshot(BranchRecord pointer) {
        return new TableSnapshot(
                Snapshot.fromJson(pointer.snapshotJson()),
                pointer.recordCount(),
                pointer.fileSizeInBytes(),
                pointer.fileCount(),
                pointer.lastFileCreationTime());
    }

    private static List<PartitionStatistics> statistics(@Nullable String json) throws Exception {
        if (json == null) {
            return List.of();
        }
        return RESTApi.OBJECT_MAPPER.readValue(json, STATISTICS);
    }

    private long now() {
        return clock.millis();
    }
}
