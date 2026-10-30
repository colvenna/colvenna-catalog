package com.colvenna.catalog.http;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.db.Db;
import java.io.UncheckedIOException;
import java.net.SocketException;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * Which failures of the metadata database the client may retry (503) and which it may not (500).
 */
class ErrorMappingTest {

    /**
     * PostgreSQL 16's SQLSTATEs for a session the server ended (57P01-57P03, 57P05 and 25P03) and
     * the connection exceptions (class 08), from its appendix "PostgreSQL Error Codes"
     * (https://www.postgresql.org/docs/16/errcodes-appendix.html); pgjdbc 42.7.13 reports an I/O
     * error on the connection as 08006 and a closed connection as 08003 (PSQLState at REL42.7.13).
     */
    @ParameterizedTest
    @ValueSource(
            strings = {
                "57P01", "57P02", "57P03", "57P05", "25P03", "08000", "08001", "08003", "08004",
                "08006", "08007"
            })
    void aLostSessionIsRetryable(String sqlState) {
        ApiException mapped = RestServer.map(new SQLException("session lost", sqlState));
        assertThat(mapped.status()).isEqualTo(503);
        assertThat(mapped.getMessage()).contains("Retry the request");
    }

    @Test
    void anIoErrorOnABrokenConnectionIsRetryableWhereverItIsInTheCauses() {
        SQLException io =
                new SQLException(
                        "An I/O error occurred while sending to the backend.",
                        "08006",
                        new SocketException("Connection reset"));
        assertThat(RestServer.map(new IllegalStateException("commit failed", io)).status())
                .isEqualTo(503);
        assertThat(RestServer.map(new UncheckedIOException(new java.io.IOException(io))).status())
                .isEqualTo(503);
    }

    @Test
    void aCommitLostInDoubtIsRetryableOnlyWhereARetryIsAReplay() {
        Db.CommitInDoubtException lost =
                new Db.CommitInDoubtException(new SQLException("terminating", "57P01"));
        ApiException unsafe = RestServer.map(lost, false);
        assertThat(unsafe.status()).isEqualTo(500);
        assertThat(unsafe.getMessage()).contains("unknown");
        ApiException replay = RestServer.map(new IllegalStateException(lost), true);
        assertThat(replay.status()).isEqualTo(503);
        assertThat(replay.getMessage()).contains("Retry");
        // A session lost before the commit is retryable whatever the request.
        assertThat(RestServer.map(new SQLException("terminating", "57P01"), false).status())
                .isEqualTo(503);
    }

    @Test
    void whatAlreadyAnswered503StillDoes() {
        assertThat(RestServer.map(new SQLException("lock", RestServer.LOCK_NOT_AVAILABLE)).status())
                .isEqualTo(503);
        assertThat(RestServer.map(new SQLTransientConnectionException("pool")).status())
                .isEqualTo(503);
    }

    @ParameterizedTest
    @ValueSource(strings = {"23505", "42P01", "XX000", "0A000"})
    void otherDatabaseErrorsAreNot(String sqlState) {
        assertThat(RestServer.map(new SQLException("error", sqlState)).status()).isEqualTo(500);
    }

    @Test
    void anErrorWithoutAStateIsNot() {
        assertThat(RestServer.map(new SQLException("error")).status()).isEqualTo(500);
    }
}
