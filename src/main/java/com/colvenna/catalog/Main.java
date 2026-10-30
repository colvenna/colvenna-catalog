package com.colvenna.catalog;

import com.colvenna.catalog.auth.TokenService;
import com.colvenna.catalog.catalog.CatalogStore;
import com.colvenna.catalog.catalog.FilePurges;
import com.colvenna.catalog.catalog.TablePurges;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.Migrations;
import com.colvenna.catalog.fs.PathRules;
import com.colvenna.catalog.fs.SnapshotPublisher;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.metrics.CatalogMetrics;
import com.colvenna.catalog.token.AwsStsTokenIssuer;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import org.apache.paimon.rest.RESTApi;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Command line entry point.
 *
 * <ul>
 *   <li>{@code serve} (default): apply migrations and serve the REST API.
 *   <li>{@code migrate}: apply migrations and exit.
 *   <li>{@code bootstrap-admin <name> <ttl> <token-file>}: create the admin principal if missing
 *       and write a new token for it to {@code token-file} (mode 600, must not exist).
 *   <li>{@code purge [--until <epoch-millis>]}: delete the files of dropped tables still recorded
 *       for purging (#69), then those rollbacks and tag and branch deletions left (#93), until the
 *       deadline; the serverless form's purge function runs it. Its last line on standard output is
 *       a JSON summary, with the metrics of the embedded metric format ({@link #summary}). Exits 1
 *       if a purge failed (its record stays, with the error, for the next run); 0 otherwise, also
 *       when the catalog schema is not at this server's version, when it does nothing.
 * </ul>
 */
public final class Main {

    private static final Logger LOG = LoggerFactory.getLogger(Main.class);

    private Main() {}

    public static void main(String[] args) throws Exception {
        String command = args.length == 0 ? "serve" : args[0];
        ServerConfig config = ServerConfig.fromEnvironment(System.getenv());
        switch (command) {
            case "serve" -> serve(config);
            case "migrate" -> migrate(config);
            case "bootstrap-admin" -> bootstrapAdmin(config, args);
            case "purge" -> System.exit(purge(config, args, Clock.systemUTC()));
            default -> {
                System.err.println("Unknown command " + command);
                System.exit(2);
            }
        }
    }

    private static void serve(ServerConfig config) throws Exception {
        config.validateForServing();
        ColvennaCatalogServer server =
                ColvennaCatalogServer.create(config, Clock.systemUTC(), dataTokens(config));
        CountDownLatch stopped = new CountDownLatch(1);
        Runtime.getRuntime()
                .addShutdownHook(
                        new Thread(
                                () -> {
                                    LOG.info("Stopping colvenna-catalog");
                                    server.close();
                                    stopped.countDown();
                                },
                                "colvenna-catalog-shutdown"));
        server.start();
        stopped.await();
    }

    static DataTokenIssuer dataTokens(ServerConfig config) {
        ServerConfig.DataTokens settings = config.dataTokens();
        return switch (settings.mode()) {
            case DISABLED -> DataTokenIssuer.disabled();
            case AWS_STS -> AwsStsTokenIssuer.create(settings, Clock.systemUTC());
        };
    }

    private static void migrate(ServerConfig config) throws Exception {
        try (Db db = open(config)) {
            LOG.info("Catalog schema is at version {}", Migrations.apply(db));
        }
    }

    private static void bootstrapAdmin(ServerConfig config, String[] args) throws Exception {
        if (args.length != 4) {
            System.err.println("Usage: bootstrap-admin <name> <ttl, e.g. P30D> <token-file>");
            System.exit(2);
        }
        Path out = Path.of(args[3]);
        try (Db db = open(config)) {
            Migrations.apply(db);
            TokenService tokens = new TokenService(db, Clock.systemUTC(), config.authCacheTtl());
            TokenService.IssuedToken issued =
                    tokens.bootstrapAdmin(args[1], Duration.parse(args[2]));
            Files.createFile(
                    out,
                    PosixFilePermissions.asFileAttribute(
                            PosixFilePermissions.fromString("rw-------")));
            Files.writeString(
                    out, issued.token(), StandardCharsets.UTF_8, StandardOpenOption.WRITE);
            LOG.info(
                    "Issued token {} for admin {} expiring {}; written to {}",
                    issued.id(),
                    issued.principal(),
                    issued.expiresAt(),
                    out);
        }
    }

    /** The namespace of the purge function's metrics (CloudWatch's embedded metric format). */
    static final String METRICS_NAMESPACE = "ColvennaCatalog";

    /** See the class comment; returns the exit status. */
    static int purge(ServerConfig config, String[] args, Clock clock) throws Exception {
        Instant until = null;
        if (args.length == 3 && args[1].equals("--until")) {
            until = Instant.ofEpochMilli(Long.parseLong(args[2]));
        } else if (args.length != 1) {
            System.err.println("Usage: purge [--until <epoch-millis>]");
            return 2;
        }
        try (Db db = open(config);
                SnapshotPublisher.S3ClientFactory s3 =
                        new SnapshotPublisher.S3ClientFactory(
                                config.fileIoOptions(),
                                config.timeouts().s3CallTimeout(),
                                config.timeouts().s3CallAttemptTimeout())) {
            int recorded = Migrations.recordedVersion(db);
            int needed = Migrations.serverVersion();
            if (recorded != needed) {
                // Mid-upgrade this function takes the new image before migrate has run. No metric:
                // an install left so for an hour sets off the purge function's backlog alarm.
                LOG.warn(
                        "Catalog schema is at version {}, this server needs {}: not purging",
                        recorded,
                        needed);
                System.out.println("{\"schemaVersion\":" + recorded + ",\"purged\":0}");
                return 0;
            }
            CatalogStore store = new CatalogStore();
            TableFiles files = new TableFiles(config.fileIoOptions(), s3);
            CatalogMetrics metrics = new CatalogMetrics();
            TablePurges purges =
                    new TablePurges(
                            db,
                            store,
                            files,
                            new PathRules(config.warehouseRoot(), config.externalLocationRoots()),
                            clock,
                            metrics);
            TablePurges.Sweep done = purges.sweep(until);
            FilePurges.Sweep filesDone =
                    new FilePurges(db, store, files, clock, metrics).sweep(until);
            System.out.println(
                    summary(done, filesDone, recorded, config.environment(), clock.millis()));
            return done.failed() > 0 || filesDone.failed() > 0 ? 1 : 0;
        }
    }

    /**
     * A purge run's summary, one line of JSON that is also a CloudWatch embedded metric format
     * document: in Lambda's log it becomes the metric {@code TablePurgeOldestSeconds} of {@link
     * #METRICS_NAMESPACE}, by {@code Environment}, which the purge function's backlog alarm
     * watches: the age of the oldest record of either kind, dropped tables' or the files rollbacks
     * and tag and branch deletions left (#93), so that one alarm watches both. The counts of each
     * kind are in the line besides.
     */
    static String summary(
            TablePurges.Sweep done,
            FilePurges.Sweep files,
            int schemaVersion,
            String environment,
            long now)
            throws Exception {
        Map<String, Object> metrics = new LinkedHashMap<>();
        metrics.put("Namespace", METRICS_NAMESPACE);
        metrics.put("Dimensions", List.of(List.of("Environment")));
        // One metric, the one the backlog alarm reads: each custom metric is billed by the month.
        metrics.put(
                "Metrics", List.of(Map.of("Name", "TablePurgeOldestSeconds", "Unit", "Seconds")));
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("_aws", Map.of("Timestamp", now, "CloudWatchMetrics", List.of(metrics)));
        line.put("Environment", environment);
        line.put("TablePurgeOldestSeconds", Math.max(done.oldestSeconds(), files.oldestSeconds()));
        line.put("purged", done.purged());
        line.put("failed", done.failed());
        line.put("skipped", done.skipped());
        line.put("pending", done.pending());
        line.put("oldestSeconds", done.oldestSeconds());
        Map<String, Object> filePurges = new LinkedHashMap<>();
        filePurges.put("purged", files.purged());
        filePurges.put("failed", files.failed());
        filePurges.put("skipped", files.skipped());
        filePurges.put("pending", files.pending());
        filePurges.put("oldestSeconds", files.oldestSeconds());
        line.put("filePurges", filePurges);
        line.put("schemaVersion", schemaVersion);
        return RESTApi.OBJECT_MAPPER.writeValueAsString(line);
    }

    private static Db open(ServerConfig config) {
        return new Db(
                config.jdbcUrl(), config.dbCredentials(), 2, 1, config.timeouts().lockTimeout());
    }
}
