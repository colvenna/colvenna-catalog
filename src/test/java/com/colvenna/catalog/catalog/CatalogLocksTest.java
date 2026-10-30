package com.colvenna.catalog.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.Migrations;
import com.colvenna.catalog.testing.TestServers;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.UUID;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.schema.Schema;
import org.junit.jupiter.api.Test;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** The row and advisory locks that keep concurrent catalog changes apart without deadlocks. */
class CatalogLocksTest {

    private static final String TABLE = "s3://bucket/wh/db.db/t1";

    private final CatalogStore store = new CatalogStore();

    @Test
    void onlyNestedClaimsWait() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        try (Connection holder = open(pg)) {
            store.claimPath(holder, TABLE);
            for (String unrelated :
                    new String[] {
                        "s3://bucket/wh/db.db/t2", "s3://bucket/wh/db.db/t10", TABLE + "0"
                    }) {
                try (Connection c = open(pg)) {
                    assertThatCode(() -> store.claimPath(c, unrelated))
                            .as(unrelated)
                            .doesNotThrowAnyException();
                }
            }
            for (String nested :
                    new String[] {
                        TABLE, TABLE + "/branch", "s3://bucket/wh/db.db", "s3://bucket"
                    }) {
                try (Connection c = open(pg)) {
                    assertThatThrownBy(() -> store.claimPath(c, nested))
                            .as(nested)
                            .isInstanceOfSatisfying(
                                    SQLException.class,
                                    e -> assertThat(e.getSQLState()).isEqualTo("55P03"));
                }
            }
            holder.rollback();
            try (Connection c = open(pg)) {
                assertThatCode(() -> store.claimPath(c, TABLE + "/branch"))
                        .doesNotThrowAnyException();
            }
        }
    }

    @Test
    void sameNameClaimsWait() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        try (Connection holder = open(pg);
                Connection other = open(pg)) {
            store.claimTableName(holder, "db", "t");
            assertThatCode(() -> store.claimTableName(other, "db", "u")).doesNotThrowAnyException();
            assertThatThrownBy(() -> store.claimTableName(other, "db", "t"))
                    .isInstanceOf(SQLException.class);
        }
    }

    /**
     * A commit holding a branch pointer inserts rows that reference the table; their foreign-key
     * locks must not wait behind a rollback that holds the table row and waits for that pointer.
     */
    @Test
    void tableLockDoesNotBlockReferencingInserts() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        try (Db db = new Db(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), 1, 1)) {
            Migrations.apply(db);
        }
        String database = "locks_" + UUID.randomUUID().toString().replace("-", "");
        UUID table = UUID.randomUUID();
        try (Connection setup = open(pg)) {
            execute(
                    setup,
                    "INSERT INTO catalog_databases (name, id, location, created_at, created_by,"
                            + " updated_at, updated_by) VALUES (?, ?, 'file:///x', 0, 't', 0, 't')",
                    database,
                    UUID.randomUUID());
            execute(
                    setup,
                    "INSERT INTO catalog_tables (id, database_name, name, path, external,"
                            + " table_type, schema_id, schema_json, created_at, created_by,"
                            + " updated_at, updated_by) VALUES (?, ?, 't', ?, false, 'table', 0,"
                            + " ?, 0, 't', 0, 't')",
                    table,
                    database,
                    "file:///x/" + database,
                    RESTApi.toJson(Schema.newBuilder().build()));
            setup.commit();
        }
        try (Connection rollback = open(pg);
                Connection commit = open(pg)) {
            assertThat(store.lockTableById(rollback, table)).isPresent();
            assertThatCode(
                            () ->
                                    execute(
                                            commit,
                                            "INSERT INTO table_partitions (table_id, branch,"
                                                    + " spec_name, spec_json, record_count,"
                                                    + " file_size_in_bytes, file_count,"
                                                    + " last_file_creation_time, total_buckets,"
                                                    + " created_at, created_by, updated_at,"
                                                    + " updated_by) VALUES (?, 'main', 'dt=1',"
                                                    + " '{}', 1, 1, 1, 0, 1, 0, 't', 0, 't')",
                                            table))
                    .doesNotThrowAnyException();
            commit.rollback();
            rollback.rollback();
        } finally {
            try (Connection cleanup = open(pg)) {
                execute(cleanup, "DELETE FROM catalog_tables WHERE id = ?", table);
                execute(cleanup, "DELETE FROM catalog_databases WHERE name = ?", database);
                cleanup.commit();
            }
        }
    }

    private static void execute(Connection c, String sql, Object... args) throws SQLException {
        try (PreparedStatement statement = c.prepareStatement(sql)) {
            for (int i = 0; i < args.length; i++) {
                statement.setObject(i + 1, args[i]);
            }
            statement.execute();
        }
    }

    private static Connection open(PostgreSQLContainer pg) throws SQLException {
        Connection c =
                DriverManager.getConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        c.setAutoCommit(false);
        try (Statement s = c.createStatement()) {
            s.execute("SET lock_timeout = '200ms'");
        }
        return c;
    }
}
