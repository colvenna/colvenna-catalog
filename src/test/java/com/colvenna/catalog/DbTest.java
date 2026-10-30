package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.testing.TestServers;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Connection settings that the deadlock protection relies on. */
class DbTest {

    @Test
    void refusesAUrlThatSetsOptions() {
        assertThatThrownBy(
                        () ->
                                new Db(
                                        "jdbc:postgresql://localhost/x?options=-c%20a%3Db",
                                        "u", "p", 1, 1))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aSessionLostInItsCommitIsInDoubtUnlessTheTransactionOnlyRead() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        try (Db db = new Db(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), 1, 1)) {
            // The session ends after the transaction's last statement: the server learns it only
            // when it commits, and cannot tell whether the COMMIT took effect.
            assertThatThrownBy(
                            () ->
                                    db.inTransaction(
                                            c -> {
                                                endFromOutside(c);
                                                return null;
                                            }))
                    .isInstanceOf(Db.CommitInDoubtException.class);
            // A read-only transaction has nothing to commit: the same loss is no doubt.
            assertThatThrownBy(
                            () ->
                                    db.read(
                                            c -> {
                                                endFromOutside(c);
                                                return null;
                                            }))
                    .isInstanceOf(SQLException.class)
                    .isNotInstanceOf(Db.CommitInDoubtException.class)
                    .satisfies(
                            e ->
                                    assertThat(Db.sessionLost(((SQLException) e).getSQLState()))
                                            .isTrue());
            // And PostgreSQL holds it to reading.
            assertThatThrownBy(
                            () ->
                                    db.read(
                                            c -> {
                                                try (Statement s = c.createStatement()) {
                                                    s.execute("CREATE TABLE read_only (x int)");
                                                }
                                                return null;
                                            }))
                    .hasMessageContaining("read-only");
            Integer after = db.inTransaction(c -> 1);
            assertThat(after).isEqualTo(1);
        }
    }

    /** Ends the session of {@code c} from another connection, and waits until it has gone. */
    private static void endFromOutside(Connection c) throws Exception {
        int pid;
        try (Statement s = c.createStatement();
                ResultSet rs = s.executeQuery("SELECT pg_backend_pid()")) {
            rs.next();
            pid = rs.getInt(1);
        }
        PostgreSQLContainer pg = TestServers.postgres();
        try (Connection other =
                        DriverManager.getConnection(
                                pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
                Statement s = other.createStatement()) {
            s.execute("SELECT pg_terminate_backend(" + pid + ")");
            for (int i = 0; i < 500; i++) {
                try (ResultSet rs =
                        s.executeQuery(
                                "SELECT count(*) FROM pg_stat_activity WHERE pid = " + pid)) {
                    rs.next();
                    if (rs.getInt(1) == 0) {
                        return;
                    }
                }
                Thread.sleep(20);
            }
        }
        throw new AssertionError("Session " + pid + " did not end");
    }

    @Test
    void lockTimeoutSurvivesARolledBackFirstTransaction() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        // One connection per pool, so the second transaction reuses the rolled-back one.
        try (Db db = new Db(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), 1, 1)) {
            for (Db.Pool pool : Db.Pool.values()) {
                assertThatThrownBy(
                                () ->
                                        db.inTransaction(
                                                pool,
                                                c -> {
                                                    throw new IllegalStateException("roll back");
                                                }))
                        .isInstanceOf(IllegalStateException.class);
                String timeout =
                        db.inTransaction(
                                pool,
                                c -> {
                                    try (Statement s = c.createStatement();
                                            ResultSet rs = s.executeQuery("SHOW lock_timeout")) {
                                        rs.next();
                                        return rs.getString(1);
                                    }
                                });
                assertThat(timeout).as(pool.name()).isEqualTo(pool == Db.Pool.MAIN ? "30s" : "5s");
            }
        }
    }

    @Test
    void theMainPoolsLockTimeoutIsConfigurable() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        try (Db db =
                new Db(
                        pg.getJdbcUrl(),
                        pg.getUsername(),
                        pg.getPassword(),
                        1,
                        1,
                        Duration.ofMillis(1500))) {
            for (Db.Pool pool : Db.Pool.values()) {
                assertThat(lockTimeout(db, pool))
                        .as(pool.name())
                        .isEqualTo(pool == Db.Pool.MAIN ? "1500ms" : "5s");
            }
        }
        assertThatThrownBy(
                        () ->
                                new Db(
                                        pg.getJdbcUrl(),
                                        pg.getUsername(),
                                        pg.getPassword(),
                                        1,
                                        1,
                                        Duration.ZERO))
                .isInstanceOf(IllegalArgumentException.class);
    }

    private static String lockTimeout(Db db, Db.Pool pool) throws Exception {
        return db.inTransaction(
                pool,
                c -> {
                    try (Statement s = c.createStatement();
                            ResultSet rs = s.executeQuery("SHOW lock_timeout")) {
                        rs.next();
                        return rs.getString(1);
                    }
                });
    }
}
