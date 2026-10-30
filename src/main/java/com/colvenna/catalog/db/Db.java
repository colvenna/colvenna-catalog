package com.colvenna.catalog.db;

import com.zaxxer.hikari.HikariDataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.util.EnumMap;
import java.util.Map;
import java.util.Set;
import javax.annotation.Nullable;
import javax.sql.DataSource;

/**
 * PostgreSQL connection pools with transaction helpers.
 *
 * <p>Work is split across pools so that no kind of work can starve another:
 *
 * <ul>
 *   <li>{@link Pool#MAIN} runs requests;
 *   <li>{@link Pool#AUTONOMOUS} runs writes that must commit on their own while a request still
 *       holds a main connection and row locks (commit intents, fences);
 *   <li>{@link Pool#AUDIT} writes audit events;
 *   <li>{@link Pool#AUTH} looks up bearer tokens, so a flood of bad tokens cannot exhaust the
 *       connections that catalog work needs.
 * </ul>
 *
 * <p>Every connection sets {@code lock_timeout}: the main pool the configured one, the others 5
 * seconds. A commit waiting in Java for an autonomous write while holding a row lock is a wait
 * PostgreSQL cannot see; the timeout turns any such cycle into an error instead of a hang.
 *
 * <p>No pool connects before it is first used: building a {@code Db} opens no connection, so a
 * server can start (a Lambda init, a SnapStart snapshot) without reaching PostgreSQL.
 *
 * <p>The pools sign in with {@link Credentials}: a fixed user and password, or a password made anew
 * for each new connection ({@link RdsIamCredentials}, the serverless form's).
 */
public final class Db implements AutoCloseable {

    /** The connection pools; see the class comment. */
    public enum Pool {
        /** Its lock timeout is the configured one, 30 seconds by default. */
        MAIN(null),
        AUTONOMOUS("5s"),
        AUDIT("5s"),
        AUTH("5s");

        @Nullable private final String lockTimeout;

        Pool(@Nullable String lockTimeout) {
            this.lockTimeout = lockTimeout;
        }
    }

    /**
     * The main pool's lock timeout unless one is configured ({@code COLVENNA_JDBC_LOCK_TIMEOUT}).
     */
    public static final Duration DEFAULT_LOCK_TIMEOUT = Duration.ofSeconds(30);

    /** How long a request waits for a connection from any pool before it gets 503. */
    public static final Duration CONNECTION_TIMEOUT = Duration.ofSeconds(10);

    /**
     * Returns {@code lockTimeout} if PostgreSQL can apply it: at least 1 ms, since 0 would turn
     * {@code lock_timeout} off, and with it the protection against waits PostgreSQL cannot see.
     */
    public static Duration requireLockTimeout(Duration lockTimeout) {
        if (lockTimeout.toMillis() < 1) {
            throw new IllegalArgumentException(
                    "COLVENNA_JDBC_LOCK_TIMEOUT must be at least 1 ms: 0 would turn PostgreSQL's"
                            + " lock_timeout off, and with it the protection against waits it"
                            + " cannot see.");
        }
        return lockTimeout;
    }

    /** Who the pools sign in as. */
    public interface Credentials {
        @Nullable
        String user();

        /** The password of the next new connection; asked again for every one. */
        @Nullable
        String password();

        /** Whether {@link #password()} never changes, so that the pool may keep it. */
        boolean fixed();

        /** A user and a password that do not change. */
        static Credentials fixed(@Nullable String user, @Nullable String password) {
            return new Credentials() {
                @Override
                public @Nullable String user() {
                    return user;
                }

                @Override
                public @Nullable String password() {
                    return password;
                }

                @Override
                public boolean fixed() {
                    return true;
                }
            };
        }
    }

    /** Work run inside one transaction. */
    @FunctionalInterface
    public interface TxWork<T> {
        T run(Connection connection) throws Exception;
    }

    /**
     * PostgreSQL 16's SQLSTATEs of a session the server ended ("PostgreSQL Error Codes",
     * https://www.postgresql.org/docs/16/errcodes-appendix.html): {@code admin_shutdown} (as {@code
     * pg_terminate_backend}, a shutdown or a failover ends it), {@code crash_shutdown}, {@code
     * cannot_connect_now} (starting or recovering), {@code idle_session_timeout} and {@code
     * idle_in_transaction_session_timeout}.
     */
    private static final Set<String> SESSION_ENDED =
            Set.of("57P01", "57P02", "57P03", "57P05", "25P03");

    /**
     * Whether a SQLSTATE says the session is gone, and with it what its transaction had not
     * committed: ended by the server, or a connection exception (class {@code 08}). pgjdbc 42.7.13
     * reports an I/O error on the connection as {@code 08006} and a closed connection as {@code
     * 08003} ({@code org.postgresql.util.PSQLState} at tag {@code REL42.7.13}); HikariCP 7.1.0
     * evicts a connection on class {@code 08} and on {@code 57P01} to {@code 57P03}, among others
     * ({@code ProxyConnection.ERROR_STATES} at tag {@code HikariCP-7.1.0}).
     */
    public static boolean sessionLost(@Nullable String sqlState) {
        return sqlState != null && (sqlState.startsWith("08") || SESSION_ENDED.contains(sqlState));
    }

    /**
     * The session was lost while its transaction committed: the COMMIT may or may not have taken
     * effect, and nothing on this side can tell which.
     */
    public static final class CommitInDoubtException extends Exception {
        private static final long serialVersionUID = 1L;

        public CommitInDoubtException(SQLException cause) {
            super("The session was lost while its transaction committed", cause);
        }
    }

    private final Map<Pool, HikariDataSource> pools = new EnumMap<>(Pool.class);

    public Db(
            String jdbcUrl,
            @Nullable String user,
            @Nullable String password,
            int poolSize,
            int auxPoolSize) {
        this(jdbcUrl, user, password, poolSize, auxPoolSize, DEFAULT_LOCK_TIMEOUT);
    }

    /** As above, with the main pool's {@code lock_timeout}; the other pools keep 5 seconds. */
    public Db(
            String jdbcUrl,
            @Nullable String user,
            @Nullable String password,
            int poolSize,
            int auxPoolSize,
            Duration mainLockTimeout) {
        this(jdbcUrl, Credentials.fixed(user, password), poolSize, auxPoolSize, mainLockTimeout);
    }

    /** As above, signing in with {@code credentials}. */
    public Db(
            String jdbcUrl,
            Credentials credentials,
            int poolSize,
            int auxPoolSize,
            Duration mainLockTimeout) {
        requireLockTimeout(mainLockTimeout);
        if (jdbcUrl.matches("(?s).*[?&]options=.*")) {
            throw new IllegalArgumentException(
                    "The JDBC URL must not set options; colvenna-catalog sets lock_timeout there.");
        }
        for (Pool pool : Pool.values()) {
            pools.put(
                    pool,
                    create(
                            pool,
                            jdbcUrl,
                            credentials,
                            pool == Pool.MAIN ? poolSize : auxPoolSize,
                            pool.lockTimeout != null
                                    ? pool.lockTimeout
                                    : mainLockTimeout.toMillis() + "ms"));
        }
    }

    private static HikariDataSource create(
            Pool pool, String jdbcUrl, Credentials credentials, int size, String lockTimeout) {
        // Configured through its setters, a data source starts its pool (and its connections and
        // housekeeping thread) on the first getConnection, not here.
        HikariDataSource source = new HikariDataSource();
        // A startup parameter, unlike SET, cannot be undone by the rollback of a first transaction.
        source.setJdbcUrl(
                jdbcUrl
                        + (jdbcUrl.contains("?") ? "&" : "?")
                        + "options=-c%20lock_timeout%3D"
                        + lockTimeout);
        if (credentials.fixed()) {
            source.setUsername(credentials.user());
            source.setPassword(credentials.password());
        } else {
            // Hikari asks for credentials each time it opens a connection, never caching them.
            source.setCredentialsProvider(
                    () ->
                            com.zaxxer.hikari.util.Credentials.of(
                                    credentials.user(), credentials.password()));
        }
        source.setMaximumPoolSize(size);
        source.setAutoCommit(false);
        source.setTransactionIsolation("TRANSACTION_READ_COMMITTED");
        source.setPoolName("colvenna-catalog-" + pool.name().toLowerCase(java.util.Locale.ROOT));
        // Fail fast while PostgreSQL is unreachable so callers see 503 instead of hanging.
        source.setConnectionTimeout(CONNECTION_TIMEOUT.toMillis());
        source.setInitializationFailTimeout(-1);
        return source;
    }

    public DataSource dataSource() {
        return pools.get(Pool.MAIN);
    }

    /** Runs {@code work} in a main-pool transaction and commits it; rolls back on any exception. */
    public <T> T inTransaction(TxWork<T> work) throws Exception {
        return inTransaction(Pool.MAIN, work);
    }

    /** Runs an autonomous write in its own transaction (commit intents, fences). */
    public <T> T inAuxTransaction(TxWork<T> work) throws Exception {
        return inTransaction(Pool.AUTONOMOUS, work);
    }

    /**
     * Runs {@code work} in a transaction of {@code pool} and commits it; rolls back on any
     * exception. A session lost while it commits is a {@link CommitInDoubtException}; lost before,
     * the transaction is rolled back and the failure is the driver's.
     */
    public <T> T inTransaction(Pool pool, TxWork<T> work) throws Exception {
        return run(pool, false, work);
    }

    /** Runs {@code work} in a read-only main-pool transaction; see {@link #read(Pool, TxWork)}. */
    public <T> T read(TxWork<T> work) throws Exception {
        return read(Pool.MAIN, work);
    }

    /**
     * Runs {@code work} in a read-only transaction of {@code pool} ({@code BEGIN READ ONLY}, which
     * refuses any write). It has nothing to commit, so a session lost while it commits is no {@link
     * CommitInDoubtException}: the work can simply be done again.
     */
    public <T> T read(Pool pool, TxWork<T> work) throws Exception {
        return run(pool, true, work);
    }

    private <T> T run(Pool pool, boolean readOnly, TxWork<T> work) throws Exception {
        try (Connection connection = pools.get(pool).getConnection()) {
            if (readOnly) {
                // HikariCP sets the connection back to read-write when it returns to the pool.
                connection.setReadOnly(true);
            }
            T result;
            try {
                result = work.run(connection);
            } catch (Exception | Error e) {
                rollbackQuietly(connection, e);
                throw e;
            }
            try {
                connection.commit();
            } catch (SQLException e) {
                if (!readOnly && sessionLost(e.getSQLState())) {
                    throw new CommitInDoubtException(e);
                }
                rollbackQuietly(connection, e);
                throw e;
            }
            return result;
        }
    }

    /** Checks that a connection can be obtained and used. */
    public boolean ping() {
        try (Connection connection = pools.get(Pool.MAIN).getConnection()) {
            boolean valid = connection.isValid(2);
            connection.rollback();
            return valid;
        } catch (SQLException e) {
            return false;
        }
    }

    private static void rollbackQuietly(Connection connection, Throwable cause) {
        try {
            connection.rollback();
        } catch (SQLException rollbackFailure) {
            cause.addSuppressed(rollbackFailure);
        }
    }

    @Override
    public void close() {
        pools.values().forEach(HikariDataSource::close);
    }
}
