package com.colvenna.catalog.db;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Applies the versioned SQL files under {@code db/migration} in order.
 *
 * <p>Replicas starting together serialize on a PostgreSQL advisory lock, so each version is applied
 * exactly once. A version never changes after it is released; schema changes add a new file.
 */
public final class Migrations {

    private static final Logger LOG = LoggerFactory.getLogger(Migrations.class);

    /** Released migrations, oldest first. */
    static final List<String> VERSIONS =
            List.of(
                    "V1__catalog.sql",
                    "V2__grants.sql",
                    "V3__table_purges_and_checks.sql",
                    "V4__file_purges.sql");

    /** "colvcatL" in ASCII: the key of the lock every replica takes before migrating. */
    private static final long ADVISORY_LOCK_KEY = 0x636f6c766361744cL;

    private Migrations() {}

    /** Returns the schema version after applying all pending migrations. */
    public static int apply(Db db) throws Exception {
        return db.inTransaction(
                connection -> {
                    try (Statement statement = connection.createStatement()) {
                        statement.execute(
                                "SELECT pg_advisory_xact_lock(" + ADVISORY_LOCK_KEY + ")");
                        statement.execute(
                                "CREATE TABLE IF NOT EXISTS colvenna_schema_version ("
                                        + "version integer PRIMARY KEY, "
                                        + "script text NOT NULL, "
                                        + "applied_at timestamptz NOT NULL DEFAULT now())");
                    }
                    int current = currentVersion(connection);
                    for (int i = current; i < VERSIONS.size(); i++) {
                        String script = VERSIONS.get(i);
                        LOG.info("Applying catalog schema migration {}", script);
                        try (Statement statement = connection.createStatement()) {
                            statement.execute(read(script));
                        }
                        try (PreparedStatement insert =
                                connection.prepareStatement(
                                        "INSERT INTO colvenna_schema_version (version, script) VALUES (?, ?)")) {
                            insert.setInt(1, i + 1);
                            insert.setString(2, script);
                            insert.executeUpdate();
                        }
                    }
                    return VERSIONS.size();
                });
    }

    /** The schema version this server's migrations produce. */
    public static int serverVersion() {
        return VERSIONS.size();
    }

    /**
     * The schema version recorded in the database, 0 before the first migration. Only reads: it
     * takes no lock and creates nothing, so a server that does not migrate can check the schema.
     */
    public static int recordedVersion(Db db) throws Exception {
        return db.inTransaction(
                connection -> {
                    try (Statement statement = connection.createStatement();
                            ResultSet rs =
                                    statement.executeQuery(
                                            "SELECT to_regclass('colvenna_schema_version') IS NULL")) {
                        rs.next();
                        if (rs.getBoolean(1)) {
                            return 0;
                        }
                    }
                    return readVersion(connection);
                });
    }

    static int currentVersion(Connection connection) throws Exception {
        int version = readVersion(connection);
        if (version > VERSIONS.size()) {
            throw new IllegalStateException(
                    "Catalog schema version "
                            + version
                            + " is newer than this server ("
                            + VERSIONS.size()
                            + "); refusing to start an older server on a newer schema.");
        }
        return version;
    }

    /** The highest version recorded in {@code colvenna_schema_version}, which must exist. */
    private static int readVersion(Connection connection) throws Exception {
        try (Statement statement = connection.createStatement();
                ResultSet rs =
                        statement.executeQuery(
                                "SELECT coalesce(max(version), 0) FROM colvenna_schema_version")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static String read(String script) {
        try (InputStream in =
                Migrations.class.getClassLoader().getResourceAsStream("db/migration/" + script)) {
            if (in == null) {
                throw new IllegalStateException("Missing migration " + script);
            }
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
