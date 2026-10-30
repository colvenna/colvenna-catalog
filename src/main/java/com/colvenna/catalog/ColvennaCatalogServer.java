package com.colvenna.catalog;

import com.colvenna.catalog.audit.AuditLog;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.auth.TokenService;
import com.colvenna.catalog.catalog.CatalogService;
import com.colvenna.catalog.catalog.CatalogStore;
import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.ConsistencyCheck;
import com.colvenna.catalog.catalog.Paging;
import com.colvenna.catalog.catalog.PartitionCatalog;
import com.colvenna.catalog.catalog.TablePurges;
import com.colvenna.catalog.catalog.ViewFunctionCatalog;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.Migrations;
import com.colvenna.catalog.db.SchemaGate;
import com.colvenna.catalog.fs.SnapshotPublisher;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.http.AdminRoutes;
import com.colvenna.catalog.http.CatalogRoutes;
import com.colvenna.catalog.http.RestServer;
import com.colvenna.catalog.metrics.CatalogMetrics;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.time.Clock;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import javax.annotation.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Wires the catalog components together; one instance per server process. */
public final class ColvennaCatalogServer implements AutoCloseable {

    private static final Logger LOG = LoggerFactory.getLogger(ColvennaCatalogServer.class);

    private final Db db;
    private final SnapshotPublisher.S3ClientFactory s3;
    private final TokenService tokens;
    private final Authorizer authz;
    private final CatalogService catalog;
    private final RestServer http;
    private final boolean sweeps;
    @Nullable private ScheduledExecutorService sweeper;

    private ColvennaCatalogServer(
            Db db,
            SnapshotPublisher.S3ClientFactory s3,
            TokenService tokens,
            Authorizer authz,
            CatalogService catalog,
            RestServer http,
            boolean sweeps) {
        this.db = db;
        this.s3 = s3;
        this.tokens = tokens;
        this.authz = authz;
        this.catalog = catalog;
        this.http = http;
        this.sweeps = sweeps;
    }

    /**
     * Builds a server that is not yet listening, after applying pending schema migrations unless
     * the configuration says not to migrate at start.
     */
    public static ColvennaCatalogServer create(
            ServerConfig config, Clock clock, DataTokenIssuer dataTokens) throws Exception {
        return create(config, clock, dataTokens, CommitService.Faults.NONE);
    }

    /** As {@link #create(ServerConfig, Clock, DataTokenIssuer)}, with commit fault injection. */
    public static ColvennaCatalogServer create(
            ServerConfig config,
            Clock clock,
            DataTokenIssuer dataTokens,
            CommitService.Faults faults)
            throws Exception {
        Db db =
                new Db(
                        config.jdbcUrl(),
                        config.dbCredentials(),
                        config.jdbcPoolSize(),
                        config.jdbcAuxPoolSize(),
                        config.timeouts().lockTimeout());
        SnapshotPublisher.S3ClientFactory s3 = null;
        try {
            RestServer.Admission admission;
            if (config.migrateOnStart()) {
                Migrations.apply(db);
                admission = RestServer.Admission.OPEN;
            } else {
                // No database work at start: the installer migrates, and the first request checks.
                LOG.info(
                        "Not migrating at start (COLVENNA_MIGRATE_ON_START=false); requests wait for"
                                + " catalog schema version {}",
                        Migrations.serverVersion());
                admission = new SchemaGate(db)::check;
            }
            CatalogStore store = new CatalogStore();
            AuditLog audit = new AuditLog(db);
            s3 =
                    new SnapshotPublisher.S3ClientFactory(
                            config.fileIoOptions(),
                            config.timeouts().s3CallTimeout(),
                            config.timeouts().s3CallAttemptTimeout());
            TableFiles files = new TableFiles(config.fileIoOptions(), s3);
            SnapshotPublisher publisher = SnapshotPublisher.forWarehouse(s3);
            CommitService commits =
                    new CommitService(db, store, files, publisher, audit, clock, faults);
            Paging paging = new Paging(config.defaultPageSize(), config.maxPageSize());
            CatalogMetrics metrics = new CatalogMetrics();
            CatalogService catalog =
                    new CatalogService(
                            db,
                            store,
                            files,
                            commits,
                            dataTokens,
                            paging,
                            clock,
                            config.warehouseName(),
                            config.warehouseRoot(),
                            config.externalLocationRoots(),
                            config.clientOverrides(),
                            config.dropPurge(),
                            config.requestWorkTime(),
                            metrics);
            TokenService tokens = new TokenService(db, clock, config.authCacheTtl());
            Authorizer authz =
                    new Authorizer(db, clock, config.authCacheTtl(), config.environment());
            RestServer http =
                    new RestServer(
                            new CatalogRoutes(
                                    catalog,
                                    new PartitionCatalog(db, store, catalog, paging, clock),
                                    new ViewFunctionCatalog(db, store, paging, clock),
                                    authz,
                                    config.warehouseName(),
                                    config.maxResponseBytes()),
                            new AdminRoutes(
                                    tokens,
                                    new ConsistencyCheck(
                                            db,
                                            store,
                                            files,
                                            commits,
                                            catalog,
                                            config.requestWorkTime()),
                                    catalog,
                                    authz,
                                    audit),
                            tokens,
                            authz,
                            audit,
                            db::ping,
                            admission,
                            config,
                            metrics);
            // Each replica sweeps: inline, for what the dropping request failed to purge; deferred,
            // for everything. In Lambda the purge function sweeps instead: an execution environment
            // runs nothing between requests.
            return new ColvennaCatalogServer(
                    db, s3, tokens, authz, catalog, http, !config.lambdaRuntime());
        } catch (Exception | Error e) {
            if (s3 != null) {
                s3.close();
            }
            db.close();
            throw e;
        }
    }

    public void start() {
        http.start();
        if (sweeps) {
            sweeper =
                    Executors.newSingleThreadScheduledExecutor(
                            r -> {
                                Thread t = new Thread(r, "colvenna-catalog-purge-sweep");
                                t.setDaemon(true);
                                return t;
                            });
            long every = TablePurges.SWEEP_INTERVAL.toSeconds();
            sweeper.scheduleWithFixedDelay(this::sweep, every, every, TimeUnit.SECONDS);
        }
    }

    /**
     * One sweep of the purges that are due, dropped tables' first, then what rollbacks and tag and
     * branch deletions left (#93); never throws.
     */
    private void sweep() {
        try {
            catalog.purges().sweep(null);
        } catch (Exception e) {
            LOG.warn("Purge sweep failed", e);
        }
        try {
            catalog.filePurges().sweep(null);
        } catch (Exception e) {
            LOG.warn("File purge sweep failed", e);
        }
    }

    public int port() {
        return http.port();
    }

    /** The address the API port is bound to. */
    public java.net.InetSocketAddress address() {
        return http.address();
    }

    public int healthPort() {
        return http.healthPort();
    }

    /** What the server measured (its /metrics). */
    public CatalogMetrics metrics() {
        return http.metrics();
    }

    public TokenService tokens() {
        return tokens;
    }

    public Authorizer authorizer() {
        return authz;
    }

    public CatalogService catalog() {
        return catalog;
    }

    public Db db() {
        return db;
    }

    @Override
    public void close() {
        if (sweeper != null) {
            sweeper.shutdownNow();
        }
        http.close();
        s3.close();
        db.close();
    }
}
