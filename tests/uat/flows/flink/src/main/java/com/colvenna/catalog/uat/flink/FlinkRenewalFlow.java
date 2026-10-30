package com.colvenna.catalog.uat.flink;

import static com.colvenna.catalog.uat.flink.Steps.step;

import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Locale;
import org.apache.flink.api.common.JobStatus;
import org.apache.flink.api.common.restartstrategy.RestartStrategies;
import org.apache.flink.core.execution.JobClient;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;
import org.apache.flink.table.api.TableResult;
import org.apache.flink.table.api.bridge.java.StreamTableEnvironment;
import org.apache.flink.types.Row;
import org.apache.flink.util.CloseableIterator;

/**
 * UAT-CAT-03's Flink side (Colvenna Lake's FlinkRenewalFlow): a checkpointed streaming write that
 * lasts longer than its first data token, on a session cluster, so that it only keeps committing if
 * the TaskManagers renew their credentials. The input is a rate-limited, bounded datagen sequence
 * (minutes x rate rows), written at parallelism 1: at a higher parallelism Paimon 2.0.0 can drop a
 * sink subtask's last data of a bounded input in streaming mode (colvenna-lake #297, upstream). No
 * restart is allowed: a writer whose credentials lapse fails the run rather than recover on a token
 * fetched afresh. Afterwards the table must hold exactly the rows the input made, and the job must
 * have committed after the first token expired.
 *
 * <p>Arguments: database, table (an append table (id BIGINT, payload STRING) the harness made),
 * minutes, rows per second, the data token's lifetime in minutes. Environment as {@link Catalogs}.
 */
public final class FlinkRenewalFlow {

    private static final DateTimeFormatter LITERAL =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss", Locale.ROOT)
                    .withZone(ZoneOffset.UTC);

    private FlinkRenewalFlow() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 5) {
            throw new IllegalArgumentException(
                    "usage: FlinkRenewalFlow <database> <table> <minutes> <rows-per-second>"
                            + " <token-minutes>");
        }
        String db = args[0];
        String table = Catalogs.NAME + "." + db + "." + args[1];
        long minutes = Long.parseLong(args[2]);
        long rate = Long.parseLong(args[3]);
        Duration lifetime = Duration.ofMinutes(Long.parseLong(args[4]));
        long rows = minutes * 60 * rate;
        TableEnvironment batch = TableEnvironment.create(EnvironmentSettings.inBatchMode());
        batch.getConfig().set("table.dml-sync", "true");
        batch.executeSql(Catalogs.ddl());

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(30_000);
        env.setRestartStrategy(RestartStrategies.noRestart());
        StreamTableEnvironment stream =
                StreamTableEnvironment.create(env, EnvironmentSettings.inStreamingMode());
        stream.executeSql(Catalogs.ddl());
        stream.executeSql(
                "CREATE TEMPORARY TABLE gen (id BIGINT, payload STRING) WITH ('connector'='datagen',"
                        + " 'number-of-rows'='"
                        + rows
                        + "', 'rows-per-second'='"
                        + rate
                        + "', 'fields.id.kind'='sequence', 'fields.id.start'='1',"
                        + " 'fields.id.end'='"
                        + rows
                        + "', 'fields.payload.length'='16')");
        Instant start = Instant.now();
        System.out.println("STARTED " + start);
        TableResult job = stream.executeSql("INSERT INTO " + table + " SELECT * FROM gen");
        JobClient client = job.getJobClient().orElseThrow();
        Instant deadline = start.plus(Duration.ofMinutes(minutes + 20));
        JobStatus status = client.getJobStatus().get();
        while (!status.isGloballyTerminalState() && Instant.now().isBefore(deadline)) {
            Thread.sleep(60_000);
            status = client.getJobStatus().get();
            System.out.println(
                    "PROGRESS "
                            + Duration.between(start, Instant.now()).toMinutes()
                            + "min "
                            + status);
        }
        JobStatus end = status;
        Instant expiry = start.plus(lifetime);
        step(
                "flink-writes-across-expiry",
                () -> {
                    if (end != JobStatus.FINISHED) {
                        throw new AssertionError("the job ended " + end + ", not FINISHED");
                    }
                    long count = scalar(batch, "SELECT count(*) FROM " + table);
                    long distinct = scalar(batch, "SELECT count(DISTINCT id) FROM " + table);
                    long late =
                            scalar(
                                    batch,
                                    "SELECT count(*) FROM "
                                            + Catalogs.NAME
                                            + "."
                                            + db
                                            + ".`"
                                            + table.substring(table.lastIndexOf('.') + 1)
                                            + "$snapshots` WHERE commit_time > TIMESTAMP '"
                                            + LITERAL.format(expiry)
                                            + "'");
                    if (count != rows || distinct != rows) {
                        throw new AssertionError(
                                count + " rows (" + distinct + " distinct), expected " + rows);
                    }
                    if (late < 3) {
                        throw new AssertionError(
                                "only " + late + " commits after the first token expired");
                    }
                    return String.format(
                            Locale.ROOT,
                            "FINISHED after %d min, no restart; %d rows, as the input made; %d"
                                    + " commits after %s (the first token's expiry)",
                            Duration.between(start, Instant.now()).toMinutes(),
                            count,
                            late,
                            expiry);
                });
        System.out.println("FLOW PASS " + db);
    }

    private static long scalar(TableEnvironment env, String query) throws Exception {
        try (CloseableIterator<Row> rows = env.executeSql(query).collect()) {
            Object value = rows.next().getField(0);
            return value == null ? 0 : ((Number) value).longValue();
        }
    }
}
