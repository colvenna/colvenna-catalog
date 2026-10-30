package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.Records.BranchRecord;
import com.colvenna.catalog.catalog.Records.CommitIntent;
import com.colvenna.catalog.catalog.Records.DatabaseRecord;
import com.colvenna.catalog.catalog.Records.IntentState;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.Records.TableState;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import javax.annotation.Nullable;
import org.apache.paimon.TableType;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.core.type.TypeReference;

/** SQL access to the catalog metadata tables. Every method runs on the caller's connection. */
public final class CatalogStore {

    private static final TypeReference<Map<String, String>> STRING_MAP = new TypeReference<>() {};

    private static final String TABLE_COLUMNS =
            "id, database_name, name, path, external, table_type, schema_id, schema_json, state,"
                    + " state_reason, owner, created_at, created_by, updated_at, updated_by";

    private static final String BRANCH_COLUMNS =
            "table_id, branch, snapshot_id, snapshot_uuid, snapshot_json, record_count,"
                    + " file_size_in_bytes, file_count, last_file_creation_time";

    // ---------------------------------------------------------------- databases

    public void insertDatabase(Connection c, DatabaseRecord db) throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO catalog_databases (name, id, location, options, owner,"
                                + " created_at, created_by, updated_at, updated_by) VALUES (?, ?, ?,"
                                + " ?::jsonb, ?, ?, ?, ?, ?)")) {
            insert.setString(1, db.name());
            insert.setObject(2, db.id());
            insert.setString(3, db.location());
            insert.setString(4, toJson(db.options()));
            insert.setString(5, db.owner());
            insert.setLong(6, db.createdAt());
            insert.setString(7, db.createdBy());
            insert.setLong(8, db.updatedAt());
            insert.setString(9, db.updatedBy());
            insert.executeUpdate();
        }
    }

    public Optional<DatabaseRecord> database(Connection c, String name, LockMode lock)
            throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT name, id, location, options::text, owner, created_at, created_by,"
                                + " updated_at, updated_by FROM catalog_databases WHERE name = ?"
                                + lock.sql)) {
            select.setString(1, name);
            try (ResultSet rs = select.executeQuery()) {
                return rs.next() ? Optional.of(databaseRow(rs)) : Optional.empty();
            }
        }
    }

    public List<String> databaseNames(
            Connection c, @Nullable String like, @Nullable String after, int limit)
            throws SQLException {
        StringBuilder sql = new StringBuilder("SELECT name FROM catalog_databases WHERE true");
        List<Object> args = new ArrayList<>();
        if (like != null) {
            sql.append(" AND name LIKE ? ESCAPE '\\'");
            args.add(like);
        }
        if (after != null) {
            sql.append(" AND name COLLATE \"C\" > ?");
            args.add(after);
        }
        sql.append(" ORDER BY name COLLATE \"C\" LIMIT ?");
        args.add(limit);
        return strings(c, sql.toString(), args);
    }

    public void updateDatabaseOptions(
            Connection c, String name, Map<String, String> options, long now, String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE catalog_databases SET options = ?::jsonb, updated_at = ?,"
                                + " updated_by = ? WHERE name = ?")) {
            update.setString(1, toJson(options));
            update.setLong(2, now);
            update.setString(3, by);
            update.setString(4, name);
            update.executeUpdate();
        }
    }

    public void deleteDatabase(Connection c, String name) throws SQLException {
        execute(c, "DELETE FROM catalog_functions WHERE database_name = ?", name);
        execute(c, "DELETE FROM catalog_views WHERE database_name = ?", name);
        execute(c, "DELETE FROM catalog_databases WHERE name = ?", name);
    }

    private DatabaseRecord databaseRow(ResultSet rs) throws SQLException {
        return new DatabaseRecord(
                rs.getString(1),
                rs.getObject(2, UUID.class),
                rs.getString(3),
                fromJson(rs.getString(4)),
                rs.getString(5),
                rs.getLong(6),
                rs.getString(7),
                rs.getLong(8),
                rs.getString(9));
    }

    // ---------------------------------------------------------------- tables

    public void insertTable(Connection c, TableRecord t) throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO catalog_tables ("
                                + TABLE_COLUMNS
                                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")) {
            insert.setObject(1, t.id());
            insert.setString(2, t.database());
            insert.setString(3, t.name());
            insert.setString(4, t.path());
            insert.setBoolean(5, t.external());
            insert.setString(6, t.tableType().toString());
            insert.setLong(7, t.schemaId());
            insert.setString(8, json(t.schema()));
            insert.setString(9, t.state().name());
            insert.setString(10, t.stateReason());
            insert.setString(11, t.owner());
            insert.setLong(12, t.createdAt());
            insert.setString(13, t.createdBy());
            insert.setLong(14, t.updatedAt());
            insert.setString(15, t.updatedBy());
            insert.executeUpdate();
        }
    }

    /**
     * The table row lock. Not FOR UPDATE: a commit holding a branch pointer inserts rows that
     * reference the table (partitions, branches, intents), and those foreign keys take FOR KEY
     * SHARE on it, which must not wait behind an operation that holds the table and waits for that
     * pointer.
     */
    private static final String TABLE_ROW_LOCK = " FOR NO KEY UPDATE";

    public Optional<TableRecord> table(
            Connection c, String database, String name, boolean forUpdate) throws SQLException {
        return oneTable(
                c,
                "SELECT "
                        + TABLE_COLUMNS
                        + " FROM catalog_tables WHERE database_name = ? AND name = ?"
                        + (forUpdate ? TABLE_ROW_LOCK : ""),
                database,
                name);
    }

    public Optional<TableRecord> tableById(Connection c, UUID id) throws SQLException {
        return oneTable(c, "SELECT " + TABLE_COLUMNS + " FROM catalog_tables WHERE id = ?", id);
    }

    /** Locks a table row by id, for operations that already hold the record. */
    public Optional<TableRecord> lockTableById(Connection c, UUID id) throws SQLException {
        return oneTable(
                c,
                "SELECT " + TABLE_COLUMNS + " FROM catalog_tables WHERE id = ?" + TABLE_ROW_LOCK,
                id);
    }

    /**
     * Claims a normalized table path for the rest of the transaction, so two concurrent creates or
     * registrations cannot both pass the overlap check and nest. Hierarchical advisory locks:
     * shared on every ancestor, from the bucket or file-system root down, then exclusive on the
     * path. Two claims conflict exactly when one path is, or contains, the other; unrelated tables,
     * even in the same database, proceed in parallel.
     */
    public void claimPath(Connection c, String normalizedPath) throws SQLException {
        int scheme = normalizedPath.indexOf("://");
        int next = normalizedPath.indexOf('/', scheme < 0 ? 0 : scheme + 3);
        try (PreparedStatement shared =
                        c.prepareStatement("SELECT pg_advisory_xact_lock_shared(?)");
                PreparedStatement exclusive =
                        c.prepareStatement("SELECT pg_advisory_xact_lock(?)")) {
            while (next >= 0) {
                shared.setLong(1, pathLockKey(normalizedPath.substring(0, next)));
                shared.execute();
                next = normalizedPath.indexOf('/', next + 1);
            }
            exclusive.setLong(1, pathLockKey(normalizedPath));
            exclusive.execute();
        }
    }

    /**
     * Claims a table name for the rest of the transaction, so a concurrent create, registration or
     * rename to the same name waits and then sees the winner instead of failing on the unique key.
     * Taken before any path claim.
     */
    public void claimTableName(Connection c, String database, String name) throws SQLException {
        try (PreparedStatement lock = c.prepareStatement("SELECT pg_advisory_xact_lock(?)")) {
            lock.setLong(1, lockKey("colvenna-table", database + "/" + name));
            lock.execute();
        }
    }

    private static long pathLockKey(String path) {
        return lockKey("colvenna-path", path);
    }

    private static long lockKey(String kind, String value) {
        try {
            byte[] digest =
                    MessageDigest.getInstance("SHA-256")
                            .digest((kind + ":" + value).getBytes(StandardCharsets.UTF_8));
            return ByteBuffer.wrap(digest).getLong();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * Returns a table whose root equals {@code path}, contains it, or lies inside it. Paths are
     * stored normalized (see {@code PathRules.normalize}).
     */
    public Optional<String> tableOverlapping(Connection c, String path) throws SQLException {
        List<String> names =
                strings(
                        c,
                        "SELECT database_name || '.' || name FROM catalog_tables WHERE path = ? OR"
                                + " starts_with(?, path || '/') OR starts_with(path, ? || '/')"
                                + " LIMIT 1",
                        List.of(path, path, path));
        return names.isEmpty() ? Optional.empty() : Optional.of(names.get(0));
    }

    /** Tables of one database in name order, filtered by name pattern and table type. */
    public List<TableRecord> tables(
            Connection c,
            String database,
            @Nullable String like,
            @Nullable String tableType,
            @Nullable String after,
            int limit)
            throws SQLException {
        StringBuilder sql =
                new StringBuilder(
                        "SELECT " + TABLE_COLUMNS + " FROM catalog_tables WHERE database_name = ?");
        List<Object> args = new ArrayList<>();
        args.add(database);
        if (like != null) {
            sql.append(" AND name LIKE ? ESCAPE '\\'");
            args.add(like);
        }
        if (tableType != null) {
            sql.append(" AND lower(table_type) = lower(?)");
            args.add(tableType);
        }
        if (after != null) {
            sql.append(" AND name COLLATE \"C\" > ?");
            args.add(after);
        }
        sql.append(" ORDER BY name COLLATE \"C\" LIMIT ?");
        args.add(limit);
        return tables(c, sql.toString(), args);
    }

    /** Tables of all databases in {@code database.table} order. */
    public List<TableRecord> tablesGlobally(
            Connection c,
            @Nullable String databaseLike,
            @Nullable String tableLike,
            @Nullable String after,
            int limit)
            throws SQLException {
        StringBuilder sql =
                new StringBuilder("SELECT " + TABLE_COLUMNS + " FROM catalog_tables WHERE true");
        List<Object> args = new ArrayList<>();
        if (databaseLike != null) {
            sql.append(" AND database_name LIKE ? ESCAPE '\\'");
            args.add(databaseLike);
        }
        if (tableLike != null) {
            sql.append(" AND name LIKE ? ESCAPE '\\'");
            args.add(tableLike);
        }
        if (after != null) {
            String[] key = Paging.splitGlobalKey(after);
            sql.append(" AND (database_name COLLATE \"C\", name COLLATE \"C\") > (?, ?)");
            args.add(key[0]);
            args.add(key[1]);
        }
        sql.append(" ORDER BY database_name COLLATE \"C\", name COLLATE \"C\" LIMIT ?");
        args.add(limit);
        return tables(c, sql.toString(), args);
    }

    public int countTables(Connection c, String database) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement("SELECT count(*) FROM catalog_tables WHERE database_name = ?")) {
            select.setString(1, database);
            try (ResultSet rs = select.executeQuery()) {
                rs.next();
                return rs.getInt(1);
            }
        }
    }

    public void updateTableSchema(
            Connection c, UUID id, long schemaId, Schema schema, long now, String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE catalog_tables SET schema_id = ?, schema_json = ?, updated_at = ?,"
                                + " updated_by = ? WHERE id = ?")) {
            update.setLong(1, schemaId);
            update.setString(2, json(schema));
            update.setLong(3, now);
            update.setString(4, by);
            update.setObject(5, id);
            update.executeUpdate();
        }
    }

    public void renameTable(
            Connection c, UUID id, String database, String name, long now, String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE catalog_tables SET database_name = ?, name = ?, updated_at = ?,"
                                + " updated_by = ? WHERE id = ?")) {
            update.setString(1, database);
            update.setString(2, name);
            update.setLong(3, now);
            update.setString(4, by);
            update.setObject(5, id);
            update.executeUpdate();
        }
    }

    public void setTableState(Connection c, UUID id, TableState state, @Nullable String reason)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE catalog_tables SET state = ?, state_reason = ? WHERE id = ?")) {
            update.setString(1, state.name());
            update.setString(2, reason);
            update.setObject(3, id);
            update.executeUpdate();
        }
    }

    public void deleteTable(Connection c, UUID id) throws SQLException {
        execute(c, "DELETE FROM catalog_tables WHERE id = ?", id);
    }

    private Optional<TableRecord> oneTable(Connection c, String sql, Object... args)
            throws SQLException {
        List<TableRecord> rows = tables(c, sql, List.of(args));
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    private List<TableRecord> tables(Connection c, String sql, List<Object> args)
            throws SQLException {
        try (PreparedStatement select = c.prepareStatement(sql)) {
            bind(select, args);
            try (ResultSet rs = select.executeQuery()) {
                List<TableRecord> rows = new ArrayList<>();
                while (rs.next()) {
                    rows.add(
                            new TableRecord(
                                    rs.getObject(1, UUID.class),
                                    rs.getString(2),
                                    rs.getString(3),
                                    rs.getString(4),
                                    rs.getBoolean(5),
                                    TableType.fromString(rs.getString(6)),
                                    rs.getLong(7),
                                    schema(rs.getString(8)),
                                    TableState.valueOf(rs.getString(9)),
                                    rs.getString(10),
                                    rs.getString(11),
                                    rs.getLong(12),
                                    rs.getString(13),
                                    rs.getLong(14),
                                    rs.getString(15)));
                }
                return rows;
            }
        }
    }

    // ---------------------------------------------------------------- branches

    public Optional<BranchRecord> branch(Connection c, UUID tableId, String branch, LockMode lock)
            throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT "
                                + BRANCH_COLUMNS
                                + " FROM table_branches WHERE table_id = ? AND branch = ?"
                                + lock.sql)) {
            select.setObject(1, tableId);
            select.setString(2, branch);
            try (ResultSet rs = select.executeQuery()) {
                if (!rs.next()) {
                    return Optional.empty();
                }
                return Optional.of(
                        new BranchRecord(
                                rs.getObject(1, UUID.class),
                                rs.getString(2),
                                (Long) rs.getObject(3),
                                rs.getString(4),
                                rs.getString(5),
                                rs.getLong(6),
                                rs.getLong(7),
                                rs.getLong(8),
                                rs.getLong(9)));
            }
        }
    }

    /** Creates the pointer row if missing; returns whether it was created. */
    public boolean insertBranch(Connection c, BranchRecord b, long now, String by)
            throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO table_branches ("
                                + BRANCH_COLUMNS
                                + ", updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                                + " ?) ON CONFLICT (table_id, branch) DO NOTHING")) {
            bindBranch(insert, b);
            insert.setLong(10, now);
            insert.setString(11, by);
            return insert.executeUpdate() == 1;
        }
    }

    public void updateBranch(Connection c, BranchRecord b, long now, String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE table_branches SET snapshot_id = ?, snapshot_uuid = ?,"
                                + " snapshot_json = ?, record_count = ?, file_size_in_bytes = ?,"
                                + " file_count = ?, last_file_creation_time = ?, updated_at = ?,"
                                + " updated_by = ? WHERE table_id = ? AND branch = ?")) {
            setNullableLong(update, 1, b.snapshotId());
            update.setString(2, b.snapshotUuid());
            update.setString(3, b.snapshotJson());
            update.setLong(4, b.recordCount());
            update.setLong(5, b.fileSizeInBytes());
            update.setLong(6, b.fileCount());
            update.setLong(7, b.lastFileCreationTime());
            update.setLong(8, now);
            update.setString(9, by);
            update.setObject(10, b.tableId());
            update.setString(11, b.branch());
            update.executeUpdate();
        }
    }

    /** The branches with a pointer row, main first. */
    public List<String> branchNames(Connection c, UUID tableId) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT branch FROM table_branches WHERE table_id = ?"
                                + " ORDER BY branch <> 'main', branch")) {
            select.setObject(1, tableId);
            List<String> names = new ArrayList<>();
            try (ResultSet rs = select.executeQuery()) {
                while (rs.next()) {
                    names.add(rs.getString(1));
                }
            }
            return names;
        }
    }

    /**
     * Locks every branch pointer of a table, in name order, waiting one {@code lock_timeout} in all
     * rather than one for each pointer (PostgreSQL times each lock on its own): the statement's
     * {@code statement_timeout} is the connection's {@code lock_timeout} meanwhile (#69). Running
     * out of it is a lock timeout ({@code 55P03}), retryable as any other.
     */
    public void lockBranches(Connection c, UUID tableId) throws SQLException {
        try (PreparedStatement bound =
                        c.prepareStatement(
                                "SELECT set_config('statement_timeout',"
                                        + " current_setting('lock_timeout'), true)");
                PreparedStatement select =
                        c.prepareStatement(
                                "SELECT branch FROM table_branches WHERE table_id = ? ORDER BY"
                                        + " branch FOR UPDATE");
                PreparedStatement unbound =
                        c.prepareStatement("SET LOCAL statement_timeout TO DEFAULT")) {
            bound.executeQuery().close();
            select.setObject(1, tableId);
            try {
                select.executeQuery().close();
            } catch (SQLException e) {
                if (QUERY_CANCELED.equals(e.getSQLState())) {
                    throw new SQLException(
                            "Timed out waiting for the table's branch pointers",
                            LOCK_NOT_AVAILABLE,
                            e);
                }
                throw e;
            }
            unbound.execute();
        }
    }

    /** SQLSTATE of a statement cancelled by its {@code statement_timeout}. */
    private static final String QUERY_CANCELED = "57014";

    /** SQLSTATE of a lock timeout. */
    static final String LOCK_NOT_AVAILABLE = "55P03";

    /**
     * A branch's last rollback (#93): who sent it, what it asked for ({@code snapshot:<id>} or
     * {@code tag:<name>}) and with which guard, the snapshot it left the branch at, and when.
     */
    public record Rollback(
            String by, String target, @Nullable Long fromSnapshot, long snapshotId, Instant at) {}

    /** The last rollback of a branch with a pointer, if it had one. */
    public Optional<Rollback> lastRollback(Connection c, UUID tableId, String branch)
            throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT last_rollback::text FROM table_branches WHERE table_id = ? AND"
                                + " branch = ? AND last_rollback IS NOT NULL")) {
            select.setObject(1, tableId);
            select.setString(2, branch);
            try (ResultSet rs = select.executeQuery()) {
                if (!rs.next()) {
                    return Optional.empty();
                }
                try {
                    var json = RESTApi.OBJECT_MAPPER.readTree(rs.getString(1));
                    return Optional.of(
                            new Rollback(
                                    json.get("by").asText(),
                                    json.get("target").asText(),
                                    json.path("fromSnapshot").isNumber()
                                            ? json.get("fromSnapshot").asLong()
                                            : null,
                                    json.get("snapshotId").asLong(),
                                    Instant.parse(json.get("at").asText())));
                } catch (Exception e) {
                    throw new SQLException("Unreadable last_rollback: " + e.getMessage(), e);
                }
            }
        }
    }

    /** Records a branch's rollback, replacing the one before. */
    public void setLastRollback(Connection c, UUID tableId, String branch, Rollback rollback)
            throws SQLException {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("by", rollback.by());
        json.put("target", rollback.target());
        json.put("fromSnapshot", rollback.fromSnapshot());
        json.put("snapshotId", rollback.snapshotId());
        json.put("at", rollback.at().toString());
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE table_branches SET last_rollback = ?::jsonb WHERE table_id = ? AND"
                                + " branch = ?")) {
            update.setString(1, json(json));
            update.setObject(2, tableId);
            update.setString(3, branch);
            update.executeUpdate();
        }
    }

    public void deleteBranch(Connection c, UUID tableId, String branch) throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement(
                        "DELETE FROM table_branches WHERE table_id = ? AND branch = ?")) {
            delete.setObject(1, tableId);
            delete.setString(2, branch);
            delete.executeUpdate();
        }
    }

    private static void bindBranch(PreparedStatement statement, BranchRecord b)
            throws SQLException {
        statement.setObject(1, b.tableId());
        statement.setString(2, b.branch());
        setNullableLong(statement, 3, b.snapshotId());
        statement.setString(4, b.snapshotUuid());
        statement.setString(5, b.snapshotJson());
        statement.setLong(6, b.recordCount());
        statement.setLong(7, b.fileSizeInBytes());
        statement.setLong(8, b.fileCount());
        statement.setLong(9, b.lastFileCreationTime());
    }

    /** Row locking used when reading a branch pointer. */
    public enum LockMode {
        NONE(""),
        FOR_SHARE(" FOR SHARE"),
        FOR_UPDATE(" FOR UPDATE"),
        SKIP_LOCKED(" FOR UPDATE SKIP LOCKED");

        private final String sql;

        LockMode(String sql) {
            this.sql = sql;
        }
    }

    // ---------------------------------------------------------------- commit intents

    public long insertIntent(
            Connection c,
            UUID tableId,
            String branch,
            long snapshotId,
            @Nullable String snapshotUuid,
            @Nullable String baseSnapshotUuid,
            @Nullable String commitUser,
            long commitIdentifier,
            @Nullable String statisticsJson,
            String principal,
            String requestId)
            throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO commit_intents (table_id, branch, snapshot_id,"
                                + " snapshot_uuid, base_snapshot_uuid, commit_user,"
                                + " commit_identifier, statistics_json, principal, request_id, state)"
                                + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING') RETURNING id")) {
            insert.setObject(1, tableId);
            insert.setString(2, branch);
            insert.setLong(3, snapshotId);
            insert.setString(4, snapshotUuid);
            insert.setString(5, baseSnapshotUuid);
            insert.setString(6, commitUser);
            insert.setLong(7, commitIdentifier);
            insert.setString(8, statisticsJson);
            insert.setString(9, principal);
            insert.setString(10, requestId);
            try (ResultSet rs = insert.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    public List<CommitIntent> pendingIntents(Connection c, UUID tableId, String branch)
            throws SQLException {
        return intents(
                c,
                "SELECT id, table_id, branch, snapshot_id, snapshot_uuid, state, created_at,"
                        + " statistics_json FROM commit_intents WHERE table_id = ? AND branch = ? AND state = 'PENDING'"
                        + " ORDER BY snapshot_id, id",
                tableId,
                branch);
    }

    public boolean hasPendingIntent(Connection c, UUID tableId, String branch) throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT 1 FROM commit_intents WHERE table_id = ? AND branch = ? AND state ="
                                + " 'PENDING' LIMIT 1")) {
            select.setObject(1, tableId);
            select.setString(2, branch);
            try (ResultSet rs = select.executeQuery()) {
                return rs.next();
            }
        }
    }

    /** Finds a recorded intent that produced the snapshot with {@code snapshotUuid}. */
    public Optional<CommitIntent> intentForSnapshot(
            Connection c, UUID tableId, String branch, long snapshotId, String snapshotUuid)
            throws SQLException {
        List<CommitIntent> rows =
                intents(
                        c,
                        "SELECT id, table_id, branch, snapshot_id, snapshot_uuid, state, created_at,"
                                + " statistics_json FROM commit_intents WHERE table_id = ? AND branch = ? AND"
                                + " snapshot_id = ? AND snapshot_uuid = ? ORDER BY (state ="
                                + " 'PENDING') DESC, id DESC LIMIT 1",
                        tableId,
                        branch,
                        snapshotId,
                        snapshotUuid);
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public void resolveIntent(Connection c, long id, IntentState state, String resolution)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE commit_intents SET state = ?, resolution = ?, resolved_at = now()"
                                + " WHERE id = ? AND state = 'PENDING'")) {
            update.setString(1, state.name());
            update.setString(2, resolution);
            update.setLong(3, id);
            update.executeUpdate();
        }
    }

    private List<CommitIntent> intents(Connection c, String sql, Object... args)
            throws SQLException {
        try (PreparedStatement select = c.prepareStatement(sql)) {
            bind(select, List.of(args));
            try (ResultSet rs = select.executeQuery()) {
                List<CommitIntent> rows = new ArrayList<>();
                while (rs.next()) {
                    rows.add(
                            new CommitIntent(
                                    rs.getLong(1),
                                    rs.getObject(2, UUID.class),
                                    rs.getString(3),
                                    rs.getLong(4),
                                    rs.getString(5),
                                    IntentState.valueOf(rs.getString(6)),
                                    rs.getTimestamp(7).toInstant(),
                                    rs.getString(8)));
                }
                return rows;
            }
        }
    }

    // ---------------------------------------------------------------- views and functions

    /** A stored view or function: its JSON body and audit columns. */
    public record ObjectRow(
            String database,
            String name,
            UUID id,
            String json,
            @Nullable String owner,
            long createdAt,
            String createdBy,
            long updatedAt,
            String updatedBy) {}

    /** The two catalog object kinds kept only in PostgreSQL. */
    public enum ObjectKind {
        VIEW("catalog_views", "schema_json"),
        FUNCTION("catalog_functions", "function_json");

        private final String table;
        private final String column;

        ObjectKind(String table, String column) {
            this.table = table;
            this.column = column;
        }
    }

    public boolean insertObject(Connection c, ObjectKind kind, ObjectRow row) throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO "
                                + kind.table
                                + " (database_name, name, id, "
                                + kind.column
                                + ", owner, created_at, created_by, updated_at, updated_by) VALUES"
                                + " (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (database_name, name) DO"
                                + " NOTHING")) {
            insert.setString(1, row.database());
            insert.setString(2, row.name());
            insert.setObject(3, row.id());
            insert.setString(4, row.json());
            insert.setString(5, row.owner());
            insert.setLong(6, row.createdAt());
            insert.setString(7, row.createdBy());
            insert.setLong(8, row.updatedAt());
            insert.setString(9, row.updatedBy());
            return insert.executeUpdate() == 1;
        }
    }

    public Optional<ObjectRow> object(
            Connection c, ObjectKind kind, String database, String name, boolean forUpdate)
            throws SQLException {
        List<ObjectRow> rows =
                objects(
                        c,
                        kind,
                        " WHERE database_name = ? AND name = ?" + (forUpdate ? " FOR UPDATE" : ""),
                        List.of(database, name));
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public List<ObjectRow> objects(
            Connection c,
            ObjectKind kind,
            @Nullable String database,
            @Nullable String databaseLike,
            @Nullable String like,
            @Nullable String after,
            int limit)
            throws SQLException {
        StringBuilder where = new StringBuilder(" WHERE true");
        List<Object> args = new ArrayList<>();
        if (database != null) {
            where.append(" AND database_name = ?");
            args.add(database);
        }
        if (databaseLike != null) {
            where.append(" AND database_name LIKE ? ESCAPE '\\'");
            args.add(databaseLike);
        }
        if (like != null) {
            where.append(" AND name LIKE ? ESCAPE '\\'");
            args.add(like);
        }
        if (after != null && database != null) {
            where.append(" AND name COLLATE \"C\" > ?");
            args.add(after);
        } else if (after != null) {
            String[] key = Paging.splitGlobalKey(after);
            where.append(" AND (database_name COLLATE \"C\", name COLLATE \"C\") > (?, ?)");
            args.add(key[0]);
            args.add(key[1]);
        }
        where.append(
                database != null
                        ? " ORDER BY name COLLATE \"C\" LIMIT ?"
                        : " ORDER BY database_name COLLATE \"C\", name COLLATE \"C\" LIMIT ?");
        args.add(limit);
        return objects(c, kind, where.toString(), args);
    }

    public void updateObject(
            Connection c,
            ObjectKind kind,
            String database,
            String name,
            String json,
            long now,
            String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE "
                                + kind.table
                                + " SET "
                                + kind.column
                                + " = ?, updated_at = ?, updated_by = ? WHERE database_name = ? AND"
                                + " name = ?")) {
            update.setString(1, json);
            update.setLong(2, now);
            update.setString(3, by);
            update.setString(4, database);
            update.setString(5, name);
            update.executeUpdate();
        }
    }

    public void renameObject(
            Connection c,
            ObjectKind kind,
            String fromDatabase,
            String fromName,
            String toDatabase,
            String toName,
            long now,
            String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE "
                                + kind.table
                                + " SET database_name = ?, name = ?, updated_at = ?, updated_by = ?"
                                + " WHERE database_name = ? AND name = ?")) {
            update.setString(1, toDatabase);
            update.setString(2, toName);
            update.setLong(3, now);
            update.setString(4, by);
            update.setString(5, fromDatabase);
            update.setString(6, fromName);
            update.executeUpdate();
        }
    }

    public boolean deleteObject(Connection c, ObjectKind kind, String database, String name)
            throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement(
                        "DELETE FROM " + kind.table + " WHERE database_name = ? AND name = ?")) {
            delete.setString(1, database);
            delete.setString(2, name);
            return delete.executeUpdate() == 1;
        }
    }

    private List<ObjectRow> objects(Connection c, ObjectKind kind, String where, List<Object> args)
            throws SQLException {
        try (PreparedStatement select =
                c.prepareStatement(
                        "SELECT database_name, name, id, "
                                + kind.column
                                + ", owner, created_at, created_by, updated_at, updated_by FROM "
                                + kind.table
                                + where)) {
            bind(select, args);
            try (ResultSet rs = select.executeQuery()) {
                List<ObjectRow> rows = new ArrayList<>();
                while (rs.next()) {
                    rows.add(
                            new ObjectRow(
                                    rs.getString(1),
                                    rs.getString(2),
                                    rs.getObject(3, UUID.class),
                                    rs.getString(4),
                                    rs.getString(5),
                                    rs.getLong(6),
                                    rs.getString(7),
                                    rs.getLong(8),
                                    rs.getString(9)));
                }
                return rows;
            }
        }
    }

    // ---------------------------------------------------------------- partitions

    /** A partition registered in the catalog for one branch. */
    public record PartitionRow(
            String specName,
            Map<String, String> spec,
            long recordCount,
            long fileSizeInBytes,
            long fileCount,
            long lastFileCreationTime,
            int totalBuckets,
            boolean done,
            Map<String, String> options,
            long createdAt,
            String createdBy,
            long updatedAt,
            String updatedBy) {}

    /** Partitions of a branch in descending name order, as the upstream server pages them. */
    public List<PartitionRow> partitions(
            Connection c,
            UUID tableId,
            String branch,
            @Nullable String like,
            @Nullable String before,
            int limit)
            throws SQLException {
        StringBuilder sql =
                new StringBuilder(
                        "SELECT spec_name, spec_json, record_count, file_size_in_bytes, file_count,"
                                + " last_file_creation_time, total_buckets, done, options::text,"
                                + " created_at, created_by, updated_at, updated_by FROM"
                                + " table_partitions WHERE table_id = ? AND branch = ?");
        List<Object> args = new ArrayList<>();
        args.add(tableId);
        args.add(branch);
        if (like != null) {
            sql.append(" AND spec_name LIKE ? ESCAPE '\\'");
            args.add(like);
        }
        if (before != null) {
            sql.append(" AND spec_name COLLATE \"C\" < ?");
            args.add(before);
        }
        sql.append(" ORDER BY spec_name COLLATE \"C\" DESC LIMIT ?");
        args.add(limit);
        try (PreparedStatement select = c.prepareStatement(sql.toString())) {
            bind(select, args);
            try (ResultSet rs = select.executeQuery()) {
                List<PartitionRow> rows = new ArrayList<>();
                while (rs.next()) {
                    rows.add(
                            new PartitionRow(
                                    rs.getString(1),
                                    fromJson(rs.getString(2)),
                                    rs.getLong(3),
                                    rs.getLong(4),
                                    rs.getLong(5),
                                    rs.getLong(6),
                                    rs.getInt(7),
                                    rs.getBoolean(8),
                                    fromJson(rs.getString(9)),
                                    rs.getLong(10),
                                    rs.getString(11),
                                    rs.getLong(12),
                                    rs.getString(13)));
                }
                return rows;
            }
        }
    }

    public Map<String, PartitionRow> partitionsByName(
            Connection c, UUID tableId, String branch, List<String> specNames) throws SQLException {
        Map<String, PartitionRow> found = new HashMap<>();
        for (PartitionRow row : partitions(c, tableId, branch, null, null, Integer.MAX_VALUE)) {
            if (specNames.contains(row.specName())) {
                found.put(row.specName(), row);
            }
        }
        return found;
    }

    /** Inserts a partition, or adds the deltas to an existing one. */
    public void upsertPartition(Connection c, UUID tableId, String branch, PartitionRow delta)
            throws SQLException {
        try (PreparedStatement upsert =
                c.prepareStatement(
                        "INSERT INTO table_partitions (table_id, branch, spec_name, spec_json,"
                                + " record_count, file_size_in_bytes, file_count,"
                                + " last_file_creation_time, total_buckets, done, options,"
                                + " created_at, created_by, updated_at, updated_by) VALUES (?, ?, ?,"
                                + " ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?) ON CONFLICT (table_id,"
                                + " branch, spec_name) DO UPDATE SET record_count ="
                                + " table_partitions.record_count + excluded.record_count,"
                                + " file_size_in_bytes = table_partitions.file_size_in_bytes +"
                                + " excluded.file_size_in_bytes, file_count ="
                                + " table_partitions.file_count + excluded.file_count,"
                                + " last_file_creation_time ="
                                + " greatest(table_partitions.last_file_creation_time,"
                                + " excluded.last_file_creation_time), total_buckets ="
                                + " excluded.total_buckets, updated_at = excluded.updated_at,"
                                + " updated_by = excluded.updated_by")) {
            upsert.setObject(1, tableId);
            upsert.setString(2, branch);
            upsert.setString(3, delta.specName());
            upsert.setString(4, toJson(delta.spec()));
            upsert.setLong(5, delta.recordCount());
            upsert.setLong(6, delta.fileSizeInBytes());
            upsert.setLong(7, delta.fileCount());
            upsert.setLong(8, delta.lastFileCreationTime());
            upsert.setInt(9, delta.totalBuckets());
            upsert.setBoolean(10, delta.done());
            upsert.setString(11, toJson(delta.options()));
            upsert.setLong(12, delta.createdAt());
            upsert.setString(13, delta.createdBy());
            upsert.setLong(14, delta.updatedAt());
            upsert.setString(15, delta.updatedBy());
            upsert.executeUpdate();
        }
    }

    /** Inserts an empty partition; returns {@code false} when it already exists. */
    public boolean insertEmptyPartition(
            Connection c,
            UUID tableId,
            String branch,
            String specName,
            Map<String, String> spec,
            long now,
            String by)
            throws SQLException {
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO table_partitions (table_id, branch, spec_name, spec_json,"
                                + " record_count, file_size_in_bytes, file_count,"
                                + " last_file_creation_time, total_buckets, done, created_at,"
                                + " created_by, updated_at, updated_by) VALUES (?, ?, ?, ?, 0, 0, 0,"
                                + " 0, -1, false, ?, ?, ?, ?) ON CONFLICT (table_id, branch,"
                                + " spec_name) DO NOTHING")) {
            insert.setObject(1, tableId);
            insert.setString(2, branch);
            insert.setString(3, specName);
            insert.setString(4, toJson(spec));
            insert.setLong(5, now);
            insert.setString(6, by);
            insert.setLong(7, now);
            insert.setString(8, by);
            return insert.executeUpdate() == 1;
        }
    }

    /** Drops partitions whose data a commit has removed entirely, as the upstream server does. */
    public void removeEmptyPartitions(Connection c, UUID tableId, String branch)
            throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement(
                        "DELETE FROM table_partitions WHERE table_id = ? AND branch = ? AND"
                                + " record_count <= 0 AND file_count <= 0 AND file_size_in_bytes <="
                                + " 0")) {
            delete.setObject(1, tableId);
            delete.setString(2, branch);
            delete.executeUpdate();
        }
    }

    public boolean deletePartition(Connection c, UUID tableId, String branch, String specName)
            throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement(
                        "DELETE FROM table_partitions WHERE table_id = ? AND branch = ? AND"
                                + " spec_name = ?")) {
            delete.setObject(1, tableId);
            delete.setString(2, branch);
            delete.setString(3, specName);
            return delete.executeUpdate() == 1;
        }
    }

    public void deleteBranchPartitions(Connection c, UUID tableId, String branch)
            throws SQLException {
        try (PreparedStatement delete =
                c.prepareStatement(
                        "DELETE FROM table_partitions WHERE table_id = ? AND branch = ?")) {
            delete.setObject(1, tableId);
            delete.setString(2, branch);
            delete.executeUpdate();
        }
    }

    public void markPartitionDone(
            Connection c, UUID tableId, String branch, String specName, long now, String by)
            throws SQLException {
        try (PreparedStatement update =
                c.prepareStatement(
                        "UPDATE table_partitions SET done = true, updated_at = ?, updated_by = ?"
                                + " WHERE table_id = ? AND branch = ? AND spec_name = ?")) {
            update.setLong(1, now);
            update.setString(2, by);
            update.setObject(3, tableId);
            update.setString(4, branch);
            update.setString(5, specName);
            update.executeUpdate();
        }
    }

    // ---------------------------------------------------------------- helpers

    private static List<String> strings(Connection c, String sql, List<Object> args)
            throws SQLException {
        try (PreparedStatement select = c.prepareStatement(sql)) {
            bind(select, args);
            try (ResultSet rs = select.executeQuery()) {
                List<String> rows = new ArrayList<>();
                while (rs.next()) {
                    rows.add(rs.getString(1));
                }
                return rows;
            }
        }
    }

    private static void execute(Connection c, String sql, Object arg) throws SQLException {
        try (PreparedStatement statement = c.prepareStatement(sql)) {
            statement.setObject(1, arg);
            statement.executeUpdate();
        }
    }

    private static void bind(PreparedStatement statement, List<Object> args) throws SQLException {
        for (int i = 0; i < args.size(); i++) {
            statement.setObject(i + 1, args.get(i));
        }
    }

    private static void setNullableLong(
            PreparedStatement statement, int index, @Nullable Long value) throws SQLException {
        if (value == null) {
            statement.setNull(index, Types.BIGINT);
        } else {
            statement.setLong(index, value);
        }
    }

    private static String json(Object value) {
        try {
            return RESTApi.toJson(value);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static Schema schema(String json) {
        try {
            return RESTApi.fromJson(json, Schema.class);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static String toJson(Map<String, String> map) {
        try {
            return RESTApi.toJson(map == null ? Map.of() : map);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static Map<String, String> fromJson(String json) {
        try {
            return RESTApi.OBJECT_MAPPER.readValue(json, STRING_MAP);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
