package com.colvenna.catalog.uat.spark;

import static com.colvenna.catalog.uat.spark.Sessions.step;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

/**
 * UAT-CAT-03's Spark side (Colvenna Lake's SparkRenewalFlow): one Spark session writes and reads a
 * table, waits longer than its first data token lives, and writes and reads again, also through a
 * table object it obtained before the wait, so that the second round only succeeds if the client
 * renews its credentials. One {@code STEP} line per round.
 *
 * <p>Arguments: database, table (id BIGINT, at STRING, made by the harness), wait minutes.
 * Environment as {@link Sessions}.
 */
public final class SparkRenewalFlow {

    private SparkRenewalFlow() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException(
                    "usage: SparkRenewalFlow <database> <table> <wait-minutes>");
        }
        String db = args[0];
        String name = args[1];
        Duration wait = Duration.ofMinutes(Long.parseLong(args[2]));
        SparkSession spark = Sessions.spark("colvenna-uat-spark-renewal");
        String table = Sessions.NAME + "." + db + "." + name;
        try (Catalog paimon = Sessions.paimon()) {
            step("spark-before-the-wait", () -> round(spark, table, 1));
            // A table object held across the wait: its credentials are renewed in use, not
            // fetched afresh for a reloaded table.
            Table held = paimon.getTable(Identifier.create(db, name));
            step("spark-held-table-before", () -> held(held, 1));
            System.out.println("WAITING " + wait.toMinutes() + "min from " + Instant.now());
            Instant resume = Instant.now().plus(wait);
            while (Instant.now().isBefore(resume)) {
                Thread.sleep(60_000);
            }
            step(
                    "spark-after-the-wait",
                    () ->
                            round(spark, table, 2)
                                    + "; "
                                    + held(held, 2)
                                    + " (the same table object)");
        } finally {
            spark.stop();
        }
        System.out.println("FLOW PASS " + db);
    }

    private static String round(SparkSession spark, String table, long expected) {
        spark.sql("INSERT INTO " + table + " VALUES (" + expected + ", '" + Instant.now() + "')")
                .collectAsList();
        List<Row> rows = spark.sql("SELECT count(*) FROM " + table).collectAsList();
        Sessions.expect(expected, rows.get(0).getLong(0), "rows");
        return "write and read at " + Instant.now();
    }

    private static String held(Table held, long expected) throws Exception {
        ReadBuilder read = held.newReadBuilder();
        long count = 0;
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan())) {
            RecordReader.RecordIterator<InternalRow> batch;
            while ((batch = reader.readBatch()) != null) {
                while (batch.next() != null) {
                    count++;
                }
                batch.releaseBatch();
            }
        }
        Sessions.expect(expected, count, "rows through the held table");
        return "held table reads " + count;
    }
}
