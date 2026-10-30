package com.colvenna.catalog.db;

import com.colvenna.catalog.http.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Lets requests through once the catalog schema is at this server's version, for a server that does
 * not migrate when it starts ({@code COLVENNA_MIGRATE_ON_START=false}).
 *
 * <p>The check runs on the first request instead of at start, so the start needs no database. It
 * reads the recorded version without the migration lock. Until the version matches every request is
 * refused with 503, before anything else runs: an older server must not write a newer schema, and a
 * newer server finds none of the tables it needs before {@code migrate} has run.
 *
 * <p>Once the version matches it is not read again, as a server that migrated at start does not
 * read it again either. So "an older server never serves a newer schema" holds for what a server
 * finds when it starts serving (in Lambda, its execution environment's first request): a {@code
 * migrate} to a newer version while the server runs leaves it serving until it is replaced, as an
 * old replica keeps serving during a rolling upgrade on EKS.
 */
public final class SchemaGate {

    private static final Logger LOG = LoggerFactory.getLogger(SchemaGate.class);

    private final Db db;
    private volatile boolean current;

    public SchemaGate(Db db) {
        this.db = db;
    }

    /** Returns when the schema is current; otherwise fails with a retryable 503. */
    public void check() throws Exception {
        if (current) {
            return;
        }
        int recorded = Migrations.recordedVersion(db);
        int needed = Migrations.serverVersion();
        if (recorded == needed) {
            LOG.info("Catalog schema is at version {}", recorded);
            current = true;
            return;
        }
        LOG.warn(
                "Catalog schema is at version {}, this server needs {}; {}",
                recorded,
                needed,
                recorded < needed
                        ? "run migrate before serving"
                        : "refusing to serve an older server on a newer schema");
        throw ApiException.unavailable(
                recorded < needed
                        ? "The catalog schema is older than this server; an operator must run"
                                + " migrate."
                        : "The catalog schema is newer than this server; it does not serve it.",
                null);
    }
}
