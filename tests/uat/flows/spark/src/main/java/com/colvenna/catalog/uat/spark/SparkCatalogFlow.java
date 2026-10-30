package com.colvenna.catalog.uat.spark;

import static com.colvenna.catalog.uat.spark.Sessions.expect;
import static com.colvenna.catalog.uat.spark.Sessions.step;

import java.util.List;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

/**
 * UAT-CAT-01's Spark side (Colvenna Lake's SparkCatalogFlow): Spark 3.5 with the Paimon 2.0.0
 * connector reads the tables the Flink flow wrote, by snapshot id, tag and branch and through the
 * system tables, makes and changes a table of its own, and writes into Flink's: one commit
 * authority for both engines, on per-table data tokens. One {@code STEP} line per check.
 *
 * <p>Argument: the database the Flink flow made. Environment as {@link Sessions}.
 */
public final class SparkCatalogFlow {

    private final SparkSession spark;
    private final String db;

    private SparkCatalogFlow(SparkSession spark, String db) {
        this.spark = spark;
        this.db = db;
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: SparkCatalogFlow <database>");
        }
        SparkSession spark = Sessions.spark("colvenna-uat-spark-flow");
        try {
            new SparkCatalogFlow(spark, args[0]).run();
        } finally {
            spark.stop();
        }
    }

    private String t(String table) {
        return Sessions.NAME + "." + db + "." + table;
    }

    private void run() throws Exception {
        String orders = t("orders");
        String sales = t("spark_sales");
        step(
                "spark-read-flink-tables",
                () -> {
                    expect(98, count(orders), "orders");
                    expect(3000, count(t("events")), "events");
                    expect(1, count(orders + " WHERE note = 'evolved'"), "the evolved column");
                    return "Flink's orders and events";
                });
        step(
                "spark-time-travel",
                () -> {
                    expect(100, count(orders + " VERSION AS OF 1"), "snapshot 1");
                    expect(97, count(orders + " VERSION AS OF 'flow_tag'"), "tag flow_tag");
                    return "by snapshot id and by tag";
                });
        step(
                "spark-read-branch",
                () -> {
                    expect(98, count(t("`orders$branch_flow_branch`")), "the branch");
                    return "the branch Flink wrote";
                });
        step(
                "spark-system-tables",
                () -> {
                    positive(count(t("`orders$snapshots`")), "$snapshots");
                    positive(count(t("`orders$schemas`")), "$schemas");
                    expect(1, count(t("`orders$tags`") + " WHERE tag_name = 'flow_tag'"), "$tags");
                    positive(count(t("`orders$files`")), "$files");
                    return "$snapshots, $schemas, $tags, $files";
                });
        step(
                "spark-create-insert",
                () -> {
                    sql(
                            "CREATE TABLE "
                                    + sales
                                    + " (id BIGINT, region STRING, amount INT) TBLPROPERTIES"
                                    + " ('primary-key'='id', 'bucket'='2',"
                                    + " 'deletion-vectors.enabled'='true')");
                    sql(
                            "INSERT INTO "
                                    + sales
                                    + " VALUES (1, 'east', 10), (2, 'west', 20), (3, 'east', 30)");
                    expect(3, count(sales), "rows");
                    return "a primary-key table with deletion vectors, made by Spark";
                });
        step(
                "spark-update-delete-merge",
                () -> {
                    sql("UPDATE " + sales + " SET amount = amount + 1 WHERE region = 'east'");
                    sql("DELETE FROM " + sales + " WHERE id = 2");
                    sql(
                            "MERGE INTO "
                                    + sales
                                    + " t USING (SELECT 3 AS id, 'east' AS region, 300 AS amount"
                                    + " UNION ALL SELECT 4, 'north', 40) s ON t.id = s.id WHEN"
                                    + " MATCHED THEN UPDATE SET t.amount = s.amount WHEN NOT"
                                    + " MATCHED THEN INSERT *");
                    expect(3, count(sales), "rows");
                    expect(11, scalar("SELECT amount FROM " + sales + " WHERE id = 1"), "id 1");
                    expect(300, scalar("SELECT amount FROM " + sales + " WHERE id = 3"), "id 3");
                    return "UPDATE, DELETE and MERGE";
                });
        step(
                "spark-alter",
                () -> {
                    sql("ALTER TABLE " + sales + " ADD COLUMNS (channel STRING)");
                    sql("INSERT INTO " + sales + " VALUES (5, 'south', 50, 'web')");
                    expect(1, count(sales + " WHERE channel = 'web'"), "the new column");
                    return "a column added";
                });
        step(
                "spark-procedures",
                () -> {
                    sql(
                            "CALL "
                                    + Sessions.NAME
                                    + ".sys.create_tag(table => '"
                                    + db
                                    + ".spark_sales', tag => 's1')");
                    sql(
                            "CALL "
                                    + Sessions.NAME
                                    + ".sys.compact(table => '"
                                    + db
                                    + ".spark_sales')");
                    expect(1, count(t("`spark_sales$tags`") + " WHERE tag_name = 's1'"), "tag s1");
                    expect(4, count(sales), "rows after compaction");
                    return "create_tag and compact";
                });
        step(
                "spark-writes-flink-table",
                () -> {
                    sql("INSERT INTO " + orders + " VALUES (500, 5, 'from-spark', 'spark')");
                    expect(99, count(orders), "orders");
                    return "Spark's commit to Flink's table";
                });
        System.out.println("FLOW PASS " + db);
    }

    private void sql(String statement) {
        spark.sql(statement).collectAsList();
    }

    private long count(String from) {
        return scalar("SELECT count(*) FROM " + from);
    }

    private long scalar(String query) {
        List<Row> rows = spark.sql(query).collectAsList();
        if (rows.isEmpty()) {
            throw new AssertionError("no row for " + query);
        }
        Object value = rows.get(0).get(0);
        return value == null ? 0 : ((Number) value).longValue();
    }

    private static void positive(long actual, String what) {
        if (actual <= 0) {
            throw new AssertionError(what + ": expected rows, got " + actual);
        }
    }
}
