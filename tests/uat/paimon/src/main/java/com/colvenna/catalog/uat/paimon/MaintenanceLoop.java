package com.colvenna.catalog.uat.paimon;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;

/**
 * Maintenance beside {@link CommitStress}'s writers (Colvenna Lake's MaintenanceLoop): compacts the
 * table and expires old snapshots in a loop through the same catalog, so that compaction commits
 * race the writers' appends. Each round is one line ({@code round}, ok or not); the loop runs for
 * the minutes given, or until the file {@code /work/stop} exists. Arguments: database, table,
 * minutes, seconds between rounds.
 */
public final class MaintenanceLoop {

    /** Commit retries of a compaction racing many writers: many quick ones, not Paimon's ten. */
    static final String COMPACT_RETRIES =
            "commit.max-retries=200,commit.min-retry-wait=50 ms,commit.max-retry-wait=500 ms";

    private MaintenanceLoop() {}

    static void run(String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException(
                    "usage: maintenance <database> <table> <minutes> <interval-seconds>");
        }
        String table = args[0] + "." + args[1];
        Instant end = Instant.now().plus(Duration.ofMinutes(Long.parseLong(args[2])));
        long interval = Long.parseLong(args[3]) * 1000;
        TableEnvironment batch = TableEnvironment.create(EnvironmentSettings.inBatchMode());
        batch.getConfig().set("table.dml-sync", "true");
        batch.getConfig().set("parallelism.default", "1");
        batch.executeSql(Catalogs.flinkDdl("uat"));
        int ok = 0;
        int failed = 0;
        Thread.sleep(interval);
        while (Instant.now().isBefore(end) && !Files.exists(Path.of("/work/stop"))) {
            Instant began = Instant.now();
            try {
                batch.executeSql(
                                "CALL uat.sys.compact(`table` => '"
                                        + table
                                        + "', options => '"
                                        + COMPACT_RETRIES
                                        + "')")
                        .await();
                batch.executeSql(
                                "CALL uat.sys.expire_snapshots(`table` => '"
                                        + table
                                        + "', retain_min => 5, retain_max => 30)")
                        .await();
                ok++;
                Out.line("round", "ok", true, "began", began.toString());
            } catch (Exception e) {
                failed++;
                Out.line("round", "ok", false, "began", began.toString(), "error", Out.root(e));
            }
            Thread.sleep(interval);
        }
        Out.line("rounds", "ok", ok, "failed", failed);
    }
}
