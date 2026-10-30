package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.fs.PathRules;
import com.colvenna.catalog.fs.TableFiles;
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
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import javax.annotation.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The files of dropped managed tables (#69). A drop removes the table from the catalog and records
 * its root in {@code table_purges}, in one transaction: every client sees the table gone at once,
 * and the record says what is left to delete. The files are deleted afterwards and the record goes
 * once they are gone; a purge that fails keeps its record, with its error, for the next sweep.
 *
 * <ul>
 *   <li>{@link Mode#INLINE} (the EKS form): the dropping request deletes the files itself, as it
 *       always did, and a sweep in each replica retries what failed.
 *   <li>{@link Mode#DEFERRED} (the serverless form): the request touches no file, so a drop answers
 *       well within API Gateway's 29 seconds however many files the table has; the purge function's
 *       sweeps ({@code Main purge}) delete them, or each replica's outside Lambda.
 * </ul>
 *
 * <p>A purge is idempotent: it deletes whatever is still under the root, and a root already empty
 * is purged. A claim holds a record for {@link #LEASE}, renewed while its purge runs, so two sweeps
 * never purge one root at once however long a purge takes, and a sweep that died mid-way leaves the
 * record to the next. While the record exists, no new table can take the root or anything
 * overlapping it, so a purge never deletes another table's files. The one way back to such a table
 * is a metadata restore to a point between a drop and its purge, when a table made at the root
 * since is not in the restored catalog but its record is: a root with a schema file written after
 * the drop is that table's, and its record is removed without deleting anything.
 */
public final class TablePurges {

    private static final Logger LOG = LoggerFactory.getLogger(TablePurges.class);

    /** How long a claim holds a record before another sweep may take it; renewed meanwhile. */
    public static final Duration LEASE = Duration.ofMinutes(20);

    /** How often each replica outside Lambda sweeps for purges that are due. */
    public static final Duration SWEEP_INTERVAL = Duration.ofMinutes(5);

    /**
     * A sweep with a deadline stops claiming this long before it: the purge in hand has that long.
     * The purge function's script gives the deadline a minute before Lambda's (maintenance.sh).
     */
    static final Duration MARGIN = Duration.ofMinutes(2);

    /**
     * A schema file is another table's only when it is this much newer than the drop: the clocks of
     * the database and the storage are not quite the same.
     */
    static final Duration SKEW = Duration.ofSeconds(5);

    /** At most this much of a failure's message is kept. */
    private static final int ERROR_LENGTH = 1000;

    /** The renewals of the claims of purges under way: one thread for the process. */
    private static final ScheduledExecutorService RENEWALS =
            Executors.newSingleThreadScheduledExecutor(
                    r -> {
                        Thread t = new Thread(r, "colvenna-catalog-purge-lease");
                        t.setDaemon(true);
                        return t;
                    });

    /** Where a drop's files are deleted: see the class comment. */
    public enum Mode {
        INLINE,
        DEFERRED;

        /** {@code COLVENNA_DROP_PURGE}: {@code inline} or {@code deferred}. */
        public static Mode parse(String value) {
            return switch (value.trim().toLowerCase(Locale.ROOT)) {
                case "inline" -> INLINE;
                case "deferred" -> DEFERRED;
                default ->
                        throw new IllegalArgumentException(
                                "COLVENNA_DROP_PURGE must be inline or deferred, not " + value);
            };
        }
    }

    /** One record: the root left to delete, and how its purge has gone so far. */
    public record Pending(
            long id,
            UUID tableId,
            String table,
            String path,
            Instant droppedAt,
            String droppedBy,
            @Nullable String requestId,
            int attempts,
            @Nullable Instant lastAttemptAt,
            @Nullable String lastError,
            Instant notBefore) {

        /** As the admin API lists it. */
        public Map<String, Object> describe() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("id", id);
            out.put("tableId", tableId.toString());
            out.put("table", table);
            out.put("path", path);
            out.put("droppedAt", droppedAt.toString());
            out.put("droppedBy", droppedBy);
            out.put("requestId", requestId);
            out.put("attempts", attempts);
            out.put("lastAttemptAt", lastAttemptAt == null ? null : lastAttemptAt.toString());
            out.put("lastError", lastError);
            out.put("notBefore", notBefore.toString());
            return out;
        }
    }

    /** How one purge ended. */
    public enum Outcome {
        /** The files are gone, and the record. */
        PURGED,
        /** The record stays, with the error, for the next sweep. */
        FAILED,
        /** A table registered at the root since the drop owns the files: only the record went. */
        SKIPPED;

        String metric() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    /**
     * What a sweep did, and what is left: the records still there and the age of the oldest, in
     * seconds (0 without any).
     */
    public record Sweep(int purged, int failed, int skipped, int pending, long oldestSeconds) {}

    private final Db db;
    private final CatalogStore store;
    private final TableFiles files;
    private final PathRules paths;
    private final Clock clock;
    private final CatalogMetrics metrics;

    public TablePurges(
            Db db,
            CatalogStore store,
            TableFiles files,
            PathRules paths,
            Clock clock,
            CatalogMetrics metrics) {
        this.db = db;
        this.store = store;
        this.files = files;
        this.paths = paths;
        this.clock = clock;
        this.metrics = metrics;
    }

    /**
     * Records the root of {@code table}, which the caller's transaction is dropping. With {@code
     * claimed} the record is held for the caller, who purges it next ({@link #purge}).
     */
    Pending record(Connection c, TableRecord table, RequestContext ctx, boolean claimed)
            throws SQLException {
        Instant now = clock.instant();
        // The drop's time is the database's at this insert, once the drop holds its locks: a
        // schema file written after it is another table's (see purge). Leases go by the clock.
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO table_purges (table_id, table_name, path, dropped_at,"
                                + " dropped_by, request_id, attempts, last_attempt_at, not_before)"
                                + " VALUES (?, ?, ?, clock_timestamp(), ?, ?, ?, ?, ?) RETURNING "
                                + COLUMNS)) {
            insert.setObject(1, table.id());
            insert.setString(2, table.fullName());
            insert.setString(3, table.path());
            insert.setString(4, ctx.who());
            insert.setString(5, ctx.requestId());
            insert.setInt(6, claimed ? 1 : 0);
            insert.setTimestamp(7, claimed ? Timestamp.from(now) : null);
            insert.setTimestamp(8, Timestamp.from(claimed ? now.plus(LEASE) : now));
            try (ResultSet rs = insert.executeQuery()) {
                rs.next();
                return pending(rs);
            }
        }
    }

    /**
     * The table name of a record whose root equals, contains or lies inside {@code path}: the files
     * of a dropped table still being deleted there.
     */
    Optional<String> overlapping(Connection c, String path) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT table_name FROM table_purges WHERE path = ? OR starts_with(?, path"
                                + " || '/') OR starts_with(path, ? || '/') LIMIT 1")) {
            select.setString(1, path);
            select.setString(2, path);
            select.setString(3, path);
            try (ResultSet rs = select.executeQuery()) {
                return rs.next() ? Optional.of(rs.getString(1)) : Optional.empty();
            }
        }
    }

    /**
     * Deletes the files of a claimed record and then the record, renewing the claim while it runs.
     * Never throws: a failure is logged and kept on the record (its error, and due again at once),
     * for the next sweep.
     */
    public Outcome purge(Pending record) {
        Renewal renewal = new Renewal(() -> renew(record.id()));
        Outcome outcome;
        String error = null;
        try {
            Optional<String> table =
                    db.inTransaction(c -> store.tableOverlapping(c, record.path()));
            if (table.isPresent()
                    || files.schemaWrittenAfter(record.path(), record.droppedAt().plus(SKEW))) {
                // Creates and registrations keep off a root with a record, so only a table the
                // catalog lost (a metadata restore) or a hand-made change gets here: its files.
                LOG.warn(
                        "Not purging {}: {} owns it since the drop of {}; removing the purge"
                                + " record only",
                        record.path(),
                        table.map(t -> "table " + t).orElse("a table made there"),
                        record.table());
                outcome = Outcome.SKIPPED;
            } else {
                if (!paths.belowWarehouse(record.path())) {
                    throw new IllegalStateException(
                            "Refusing to purge "
                                    + record.path()
                                    + ": not below the warehouse root.");
                }
                files.purgeTable(record.path());
                LOG.info(
                        "Purged the files of dropped table {} at {}",
                        record.table(),
                        record.path());
                outcome = Outcome.PURGED;
            }
        } catch (Exception e) {
            LOG.warn("Purging {} failed; its record stays for the next sweep", record.path(), e);
            String message = String.valueOf(e);
            error = message.length() > ERROR_LENGTH ? message.substring(0, ERROR_LENGTH) : message;
            outcome = Outcome.FAILED;
        }
        // No renewal after this, not even one already under way: it would hold a failed record
        // for another lease instead of leaving it due at once.
        renewal.stop();
        String kept = error;
        boolean failure = outcome == Outcome.FAILED;
        try {
            db.inTransaction(c -> failure ? failed(c, record.id(), kept) : delete(c, record.id()));
        } catch (Exception recording) {
            LOG.warn("Recording the outcome of purge {} failed", record.id(), recording);
        }
        metrics.purge(outcome.metric());
        return outcome;
    }

    /**
     * The renewal of a purge's claim while it runs, every quarter of a {@link #LEASE}; {@link
     * #stop} waits for one under way and lets none start after it. {@link FilePurges}' claims are
     * renewed so too.
     */
    static final class Renewal {
        private final Runnable renew;
        private final ScheduledFuture<?> task;
        private boolean stopped;

        Renewal(Runnable renew) {
            this.renew = renew;
            long every = LEASE.toSeconds() / 4;
            this.task = RENEWALS.scheduleWithFixedDelay(this::run, every, every, TimeUnit.SECONDS);
        }

        private synchronized void run() {
            if (!stopped) {
                renew.run();
            }
        }

        synchronized void stop() {
            stopped = true;
            task.cancel(false);
        }
    }

    /**
     * Claims and purges due records one at a time, oldest first, each at most once, until none is
     * due or {@code deadline} (when given) is less than {@link #MARGIN} away; then reports what is
     * left, to the log when anything was done and to the server's metrics.
     */
    public Sweep sweep(@Nullable Instant deadline) throws Exception {
        Set<Long> tried = new HashSet<>();
        Map<Outcome, Integer> done = new LinkedHashMap<>();
        while (deadline == null || clock.instant().plus(MARGIN).isBefore(deadline)) {
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
                            try (PreparedStatement select =
                                            c.prepareStatement(
                                                    "SELECT count(*), min(dropped_at) FROM"
                                                            + " table_purges");
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
                    "Purge sweep: {} purged, {} failed, {} skipped; {} left, the oldest {} s",
                    sweep.purged(),
                    sweep.failed(),
                    sweep.skipped(),
                    sweep.pending(),
                    sweep.oldestSeconds());
        }
        metrics.purgesLeft(sweep.pending(), sweep.oldestSeconds());
        return sweep;
    }

    /** The records, oldest first, at most {@code limit} of them. */
    public List<Pending> list(int limit) throws Exception {
        return db.inTransaction(
                c -> {
                    List<Pending> out = new ArrayList<>();
                    try (PreparedStatement select =
                            c.prepareStatement(
                                    "SELECT "
                                            + COLUMNS
                                            + " FROM table_purges ORDER BY id LIMIT ?")) {
                        select.setInt(1, limit);
                        try (ResultSet rs = select.executeQuery()) {
                            while (rs.next()) {
                                out.add(pending(rs));
                            }
                        }
                    }
                    return out;
                });
    }

    /** How many records there are. */
    public int count() throws Exception {
        return db.inTransaction(
                c -> {
                    try (PreparedStatement select =
                                    c.prepareStatement("SELECT count(*) FROM table_purges");
                            ResultSet rs = select.executeQuery()) {
                        rs.next();
                        return rs.getInt(1);
                    }
                });
    }

    private static final String COLUMNS =
            "id, table_id, table_name, path, dropped_at, dropped_by, request_id, attempts,"
                    + " last_attempt_at, last_error, not_before";

    private Optional<Pending> claim(Connection c, Set<Long> tried) throws SQLException {
        Instant now = clock.instant();
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE table_purges SET attempts = attempts + 1, last_attempt_at = ?,"
                                + " not_before = ? WHERE id = (SELECT id FROM table_purges WHERE"
                                + " not_before <= ? AND NOT (id = ANY (?)) ORDER BY id LIMIT 1 FOR"
                                + " UPDATE SKIP LOCKED) RETURNING "
                                + COLUMNS)) {
            update.setTimestamp(1, Timestamp.from(now));
            update.setTimestamp(2, Timestamp.from(now.plus(LEASE)));
            update.setTimestamp(3, Timestamp.from(now));
            update.setArray(4, c.createArrayOf("bigint", tried.toArray()));
            try (ResultSet rs = update.executeQuery()) {
                return rs.next() ? Optional.of(pending(rs)) : Optional.empty();
            }
        }
    }

    /** Holds a claim for another {@link #LEASE}; a failure to is only logged. */
    private void renew(long id) {
        try {
            db.inTransaction(
                    c -> {
                        try (PreparedStatement update =
                                c.prepareStatement(
                                        "UPDATE table_purges SET not_before = ? WHERE id = ?")) {
                            update.setTimestamp(1, Timestamp.from(clock.instant().plus(LEASE)));
                            update.setLong(2, id);
                            update.executeUpdate();
                        }
                        return null;
                    });
        } catch (Exception e) {
            LOG.warn("Renewing the claim of purge {} failed", id, e);
        }
    }

    /** Records why a purge failed, and makes the record due again at once. */
    private Void failed(Connection c, long id, String error) throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE table_purges SET last_error = ?, not_before = ? WHERE id = ?")) {
            update.setString(1, error);
            update.setTimestamp(2, Timestamp.from(clock.instant()));
            update.setLong(3, id);
            update.executeUpdate();
        }
        return null;
    }

    private static Void delete(Connection c, long id) throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement("DELETE FROM table_purges WHERE id = ?")) {
            delete.setLong(1, id);
            delete.executeUpdate();
        }
        return null;
    }

    private static Pending pending(ResultSet rs) throws SQLException {
        Timestamp last = rs.getTimestamp("last_attempt_at");
        return new Pending(
                rs.getLong("id"),
                rs.getObject("table_id", UUID.class),
                rs.getString("table_name"),
                rs.getString("path"),
                rs.getTimestamp("dropped_at").toInstant(),
                rs.getString("dropped_by"),
                rs.getString("request_id"),
                rs.getInt("attempts"),
                last == null ? null : last.toInstant(),
                rs.getString("last_error"),
                rs.getTimestamp("not_before").toInstant());
    }
}
