package com.colvenna.catalog.uat.flink;

import static com.colvenna.catalog.uat.flink.Steps.expect;
import static com.colvenna.catalog.uat.flink.Steps.step;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import org.apache.flink.api.common.JobStatus;
import org.apache.flink.core.execution.JobClient;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;
import org.apache.flink.table.api.TableResult;
import org.apache.flink.table.api.bridge.java.StreamTableEnvironment;
import org.apache.flink.types.Row;
import org.apache.flink.util.CloseableIterator;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.table.FileStoreTable;

/**
 * UAT-CAT-01's Flink side (Colvenna Lake's FlinkCatalogFlow): the catalog operations Flink jobs
 * rely on, through Colvenna Catalog with per-table data tokens, submitted to a session cluster so
 * that the TaskManagers fetch their own credentials. Each step prints its check's line (Steps); the
 * tables are left for the Spark flow.
 *
 * <p>Environment: as {@link Catalogs}. Argument: the case's database, flow_&lt;run&gt;.
 */
public final class FlinkCatalogFlow {

    private final String db;
    private final TableEnvironment batch;

    private FlinkCatalogFlow(String db, TableEnvironment batch) {
        this.db = db;
        this.batch = batch;
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: FlinkCatalogFlow <database>");
        }
        TableEnvironment batch = TableEnvironment.create(EnvironmentSettings.inBatchMode());
        batch.getConfig().set("table.dml-sync", "true");
        batch.executeSql(Catalogs.ddl());
        new FlinkCatalogFlow(args[0], batch).run();
    }

    private String t(String table) {
        return Catalogs.NAME + "." + db + "." + table;
    }

    private void run() throws Exception {
        String orders = t("orders");
        String events = t("events");
        step(
                "create-database",
                () -> {
                    sql("CREATE DATABASE IF NOT EXISTS " + Catalogs.NAME + "." + db);
                    return db;
                });
        step(
                "create-primary-key-table",
                () -> {
                    // Not deletion vectors: their readers skip level-0 files, so a write-only table
                    // would read empty until the compaction step. Spark's spark_sales has them.
                    sql(
                            "CREATE TABLE "
                                    + orders
                                    + " (id BIGINT, amount INT, status STRING, PRIMARY KEY (id) NOT"
                                    + " ENFORCED) WITH ('bucket'='2', 'changelog-producer'='input',"
                                    + " 'write-only'='true')");
                    return "input changelog, write-only, merge on read";
                });
        step(
                "create-append-table",
                () -> {
                    sql(
                            "CREATE TABLE "
                                    + events
                                    + " (dt STRING, id BIGINT, payload STRING) PARTITIONED BY (dt)"
                                    + " WITH ('bucket'='-1', 'metastore.partitioned-table'='true',"
                                    + " 'write-only'='true')");
                    return "unaware-bucket, partitions managed by the catalog";
                });
        step(
                "batch-insert",
                () -> {
                    StringBuilder values = new StringBuilder();
                    for (int i = 1; i <= 100; i++) {
                        values.append(i == 1 ? "" : ", ")
                                .append("(")
                                .append(i)
                                .append(", ")
                                .append(i)
                                .append(", 'new')");
                    }
                    sql("INSERT INTO " + orders + " VALUES " + values);
                    expect(100, count(orders), "rows");
                    return "100 rows";
                });
        step(
                "upsert-update-delete",
                () -> {
                    sql("INSERT INTO " + orders + " VALUES (1, 1000, 'paid'), (2, 2000, 'paid')");
                    sql("UPDATE " + orders + " SET status = 'shipped' WHERE id = 3");
                    sql("DELETE FROM " + orders + " WHERE id >= 96");
                    expect(95, count(orders), "rows");
                    expect(
                            1000,
                            scalar("SELECT amount FROM " + orders + " WHERE id = 1"),
                            "amount");
                    expect(1, count(orders + " WHERE status = 'shipped'"), "shipped");
                    return "2 upserts, 1 update, 5 deletes: 95 rows";
                });
        step(
                "alter-add-column",
                () -> {
                    sql("ALTER TABLE " + orders + " ADD note STRING");
                    sql("INSERT INTO " + orders + " VALUES (200, 7, 'new', 'evolved')");
                    expect(1, count(orders + " WHERE note = 'evolved'"), "evolved rows");
                    expect(96, count(orders), "rows");
                    return "a column through alterTable, readable";
                });
        step("streaming-append-commits", this::streamingAppend);
        step(
                "catalog-partitions",
                () -> {
                    expect(3, count(t("`events$partitions`")), "partitions");
                    return "3 partitions listed by the catalog";
                });
        step("consumer-id-stream-read", this::consumerRead);
        step(
                "tag-create-and-read",
                () -> {
                    sql(
                            "CALL "
                                    + Catalogs.NAME
                                    + ".sys.create_tag(`table` => '"
                                    + db
                                    + ".orders', tag => 'flow_tag')");
                    expect(1, count(t("`orders$tags`") + " WHERE tag_name = 'flow_tag'"), "tags");
                    sql(
                            "INSERT INTO "
                                    + orders
                                    + " VALUES (300, 3, 'after-tag', CAST(NULL AS STRING))");
                    expect(
                            97,
                            count(orders + " /*+ OPTIONS('scan.tag-name'='flow_tag') */"),
                            "rows at the tag");
                    expect(98, count(orders), "rows");
                    return "the tag holds the rows before the next insert";
                });
        step(
                "time-travel-by-snapshot",
                () -> {
                    expect(
                            100,
                            count(orders + " /*+ OPTIONS('scan.snapshot-id'='1') */"),
                            "rows of snapshot 1");
                    return "snapshot 1 holds the first insert";
                });
        step(
                "branch-write-and-read",
                () -> {
                    // Positional: create_branch is overloaded, and Flink refuses named arguments
                    // then.
                    sql(
                            "CALL "
                                    + Catalogs.NAME
                                    + ".sys.create_branch('"
                                    + db
                                    + ".orders', 'flow_branch', 'flow_tag')");
                    sql(
                            "INSERT INTO "
                                    + t("`orders$branch_flow_branch`")
                                    + " VALUES (400, 4, 'branch', CAST(NULL AS STRING))");
                    expect(98, count(t("`orders$branch_flow_branch`")), "rows of the branch");
                    expect(98, count(orders), "rows of main");
                    expect(0, count(orders + " WHERE id = 400"), "the branch's row in main");
                    return "a branch from the tag; its write does not reach main";
                });
        step(
                "compact-procedure",
                () -> {
                    sql("CALL " + Catalogs.NAME + ".sys.compact(`table` => '" + db + ".orders')");
                    expect(
                            1,
                            count(t("`orders$snapshots`") + " WHERE commit_kind = 'COMPACT'"),
                            "compaction commits");
                    expect(98, count(orders), "rows");
                    return "a compaction commit of its own";
                });
        step(
                "expire-snapshots",
                () -> {
                    sql(
                            "CALL "
                                    + Catalogs.NAME
                                    + ".sys.expire_snapshots(`table` => '"
                                    + db
                                    + ".events', retain_min => 1, retain_max => 2)");
                    expect(2, count(t("`events$snapshots`")), "snapshots kept");
                    expect(3000, count(events), "rows");
                    return "2 snapshots kept";
                });
        step("rollback-on-scratch-table", this::rollback);
        step(
                "view",
                () -> {
                    sql(
                            "CREATE VIEW "
                                    + t("big_orders")
                                    + " AS SELECT id, amount FROM "
                                    + orders
                                    + " WHERE amount >= 1000");
                    expect(2, count(t("big_orders")), "rows of the view");
                    return "a view in the catalog";
                });
        step("catalog-function", this::function);
        step("rename-and-drop", this::renameAndDrop);
        System.out.println("FLOW PASS " + db);
    }

    /**
     * Checkpointed streaming writes through the catalog: a bounded datagen input of 3,000 rows. At
     * parallelism 1: Paimon 2.0.0 and 2.1.0 can drop the last data of a sink subtask that finishes
     * later when Flink writes a bounded input in streaming mode with checkpoints at a parallelism
     * above 1 (colvenna-lake #297, upstream). The step checks several commits and no row lost.
     */
    private String streamingAppend() throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(3_000);
        StreamTableEnvironment stream =
                StreamTableEnvironment.create(env, EnvironmentSettings.inStreamingMode());
        stream.executeSql(Catalogs.ddl());
        stream.executeSql(
                "CREATE TEMPORARY TABLE gen (id BIGINT, payload STRING) WITH ('connector'='datagen',"
                        + " 'number-of-rows'='3000', 'rows-per-second'='300',"
                        + " 'fields.id.kind'='sequence', 'fields.id.start'='1',"
                        + " 'fields.id.end'='3000', 'fields.payload.length'='16')");
        stream.executeSql(
                        "INSERT INTO "
                                + t("events")
                                + " SELECT CONCAT('2026-10-1', CAST(MOD(id, 3) AS STRING)), id,"
                                + " payload FROM gen")
                .await();
        expect(3000, count(t("events")), "rows");
        long commits = scalar("SELECT count(*) FROM " + t("`events$snapshots`"));
        if (commits < 2) {
            throw new AssertionError("expected more than one checkpoint commit, got " + commits);
        }
        return "3000 rows by " + commits + " commits at parallelism 1";
    }

    /**
     * A streaming read with a consumer id copies orders into orders_dwd; stopped and started again,
     * it resumes from the progress the consumer recorded: a row inserted while it was stopped
     * arrives, and the consumer's next snapshot moves on from where it was. At parallelism 1, so
     * that the session cluster keeps slots for the checks' batch queries.
     */
    private String consumerRead() throws Exception {
        sql(
                "CREATE TABLE "
                        + t("orders_dwd")
                        + " (id BIGINT, amount INT, status STRING, note STRING, PRIMARY KEY (id) NOT"
                        + " ENFORCED) WITH ('bucket'='2')");
        long first = streamUntil(count(t("orders")), "the first read");
        sql("INSERT INTO " + t("orders") + " VALUES (600, 6, 'while-stopped', 'resume')");
        long second = streamUntil(count(t("orders")), "the resumed read");
        if (second <= first) {
            throw new AssertionError(
                    "the consumer's progress did not move on: " + first + " then " + second);
        }
        expect(1, count(t("orders_dwd") + " WHERE note = 'resume'"), "the row from while stopped");
        return "consumer 'flow' at snapshot " + first + ", resumed to " + second;
    }

    /** Runs the consumer's stream until orders_dwd holds the rows; returns its next snapshot id. */
    private long streamUntil(long rows, String what) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(3_000);
        StreamTableEnvironment stream =
                StreamTableEnvironment.create(env, EnvironmentSettings.inStreamingMode());
        stream.executeSql(Catalogs.ddl());
        TableResult job =
                stream.executeSql(
                        "INSERT INTO "
                                + t("orders_dwd")
                                + " SELECT * FROM "
                                + t("orders")
                                + " /*+ OPTIONS('consumer-id'='flow',"
                                + " 'consumer.expiration-time'='1 d') */");
        JobClient client = job.getJobClient().orElseThrow();
        try {
            Instant deadline = Instant.now().plus(Duration.ofMinutes(5));
            while (true) {
                long copied = count(t("orders_dwd"));
                long next =
                        scalar(
                                "SELECT max(next_snapshot_id) FROM "
                                        + t("`orders$consumers`")
                                        + " WHERE consumer_id = 'flow'");
                if (copied == rows && next > 0) {
                    // A checkpoint after the copy, so that the consumer's progress is recorded.
                    Thread.sleep(8_000);
                    return scalar(
                            "SELECT max(next_snapshot_id) FROM "
                                    + t("`orders$consumers`")
                                    + " WHERE consumer_id = 'flow'");
                }
                JobStatus status = client.getJobStatus().get();
                if (status.isGloballyTerminalState() || Instant.now().isAfter(deadline)) {
                    throw new AssertionError(
                            what + " stalled: copied " + copied + " of " + rows + ", " + status);
                }
                Thread.sleep(3_000);
            }
        } finally {
            client.cancel().get();
        }
    }

    private String rollback() throws Exception {
        String scratch = t("scratch");
        sql("CREATE TABLE " + scratch + " (id INT)");
        sql("INSERT INTO " + scratch + " VALUES (1)");
        sql("INSERT INTO " + scratch + " VALUES (2)");
        sql("INSERT INTO " + scratch + " VALUES (3)");
        sql(
                "CALL "
                        + Catalogs.NAME
                        + ".sys.rollback_to(`table` => '"
                        + db
                        + ".scratch', snapshot_id => 1)");
        expect(1, count(scratch), "rows after the rollback");
        sql("INSERT INTO " + scratch + " VALUES (4)");
        expect(2, count(scratch), "rows after a new insert");
        sql("DROP TABLE " + scratch);
        return "rolled back to snapshot 1, then written";
    }

    private String function() throws Exception {
        String fn = t("twice");
        sql("CREATE FUNCTION " + fn + " AS '" + Twice.class.getName() + "' LANGUAGE JAVA");
        expect(
                2000,
                scalar(
                        "SELECT "
                                + fn
                                + "(CAST(amount AS BIGINT)) FROM "
                                + t("orders")
                                + " WHERE id = 1"),
                "twice(1000)");
        List<String> functions = column("SHOW USER FUNCTIONS IN " + Catalogs.NAME + "." + db);
        if (!functions.contains("twice")) {
            throw new AssertionError("the function is not listed: " + functions);
        }
        sql("DROP FUNCTION " + fn);
        if (column("SHOW USER FUNCTIONS IN " + Catalogs.NAME + "." + db).contains("twice")) {
            throw new AssertionError("the function is still listed after its drop");
        }
        return "made, called, listed and dropped";
    }

    /**
     * Rename changes only metadata (the table's files stay where they are); drop removes the
     * managed table. The dropped table's location is printed (PATH) for the harness, which checks
     * that no file is left there.
     */
    private String renameAndDrop() throws Exception {
        sql("CREATE TABLE " + t("scratch2") + " (id INT)");
        sql("INSERT INTO " + t("scratch2") + " VALUES (1), (2)");
        String before;
        String after;
        try (Catalog paimon = Catalogs.paimon(true)) {
            before = location(paimon, "scratch2");
            sql("ALTER TABLE " + t("scratch2") + " RENAME TO " + t("scratch3"));
            after = location(paimon, "scratch3");
        }
        if (!before.equals(after)) {
            throw new AssertionError("the rename moved the table: " + before + " to " + after);
        }
        expect(2, count(t("scratch3")), "rows after the rename");
        sql("DROP TABLE " + t("scratch3"));
        List<String> tables = column("SHOW TABLES FROM " + Catalogs.NAME + "." + db);
        if (tables.stream().anyMatch(n -> n.startsWith("scratch"))) {
            throw new AssertionError("scratch tables are left: " + tables);
        }
        System.out.println("PATH scratch3 " + after);
        return "the rename kept the files; the drop removed the table";
    }

    private String location(Catalog paimon, String table) throws Exception {
        return ((FileStoreTable) paimon.getTable(Identifier.create(db, table)))
                .location()
                .toString();
    }

    // ---------------------------------------------------------------- helpers

    private void sql(String statement) throws Exception {
        batch.executeSql(statement).await();
    }

    private long count(String from) throws Exception {
        return scalar("SELECT count(*) FROM " + from);
    }

    private long scalar(String query) throws Exception {
        try (CloseableIterator<Row> rows = batch.executeSql(query).collect()) {
            if (!rows.hasNext()) {
                throw new AssertionError("no row for " + query);
            }
            Object value = rows.next().getField(0);
            return value == null ? 0 : ((Number) value).longValue();
        }
    }

    private List<String> column(String query) throws Exception {
        List<String> out = new ArrayList<>();
        try (CloseableIterator<Row> rows = batch.executeSql(query).collect()) {
            rows.forEachRemaining(r -> out.add(String.valueOf(r.getField(0))));
        }
        return out;
    }
}
