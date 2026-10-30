package com.colvenna.catalog.compat.spark;

import com.colvenna.catalog.compat.Engine;
import com.colvenna.catalog.compat.Flow;
import com.colvenna.catalog.compat.Settings;
import com.colvenna.catalog.compat.TableType;
import java.util.ArrayList;
import java.util.List;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

/**
 * The scenario on Spark SQL, with the Paimon catalog configured as a user does: {@code
 * spark.sql.catalog.<name>=org.apache.paimon.spark.SparkCatalog} and its options, and Paimon's
 * session extensions for row-level changes. Runs in local mode (spark-submit --master local[n]).
 */
public final class SparkFlow implements Engine {

    private final SparkSession spark;

    private SparkFlow(Settings settings) {
        var builder =
                SparkSession.builder()
                        .appName("colvenna-compat")
                        .config(
                                "spark.sql.extensions",
                                "org.apache.paimon.spark.extensions.PaimonSparkSessionExtensions")
                        .config(
                                "spark.sql.catalog." + Settings.CATALOG,
                                "org.apache.paimon.spark.SparkCatalog");
        for (var option : settings.catalogOptions().entrySet()) {
            builder =
                    builder.config(
                            "spark.sql.catalog." + Settings.CATALOG + "." + option.getKey(),
                            option.getValue());
        }
        spark = builder.getOrCreate();
    }

    public static void main(String[] args) {
        Flow.main(args, SparkFlow::new);
    }

    @Override
    public String family() {
        return "spark";
    }

    @Override
    public String version() {
        return spark.version();
    }

    @Override
    public void execute(String sql) {
        spark.sql(sql).collectAsList();
    }

    @Override
    public List<Object> firstRow(String sql) {
        List<Object> fields = new ArrayList<>();
        List<Row> rows = spark.sql(sql).collectAsList();
        if (!rows.isEmpty()) {
            Row row = rows.get(0);
            for (int i = 0; i < row.size(); i++) {
                fields.add(row.get(i));
            }
        }
        return fields;
    }

    @Override
    public List<String> tables(String db) {
        // SHOW TABLES: namespace, tableName, isTemporary.
        return column("SHOW TABLES IN " + Settings.CATALOG + ".`" + db + "`", 1);
    }

    @Override
    public List<String> databases() {
        return column("SHOW NAMESPACES IN " + Settings.CATALOG, 0);
    }

    private List<String> column(String sql, int index) {
        List<String> names = new ArrayList<>();
        for (Row row : spark.sql(sql).collectAsList()) {
            names.add(String.valueOf(row.get(index)));
        }
        return names;
    }

    @Override
    public String qualified(String db, String table) {
        return Settings.CATALOG + ".`" + db + "`.`" + table + "`";
    }

    @Override
    public String createTable(String db, TableType type) {
        return "CREATE TABLE "
                + qualified(db, type.table())
                + " (id BIGINT, name STRING, amount INT) TBLPROPERTIES ("
                + (type.primaryKey() ? "'primary-key'='id', " : "")
                + type.options()
                + ")";
    }

    @Override
    public String setOption(String db, String table, String key, String value) {
        return "ALTER TABLE "
                + qualified(db, table)
                + " SET TBLPROPERTIES ('"
                + key
                + "'='"
                + value
                + "')";
    }
}
