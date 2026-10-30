package com.colvenna.catalog.compat.flink;

import com.colvenna.catalog.compat.Engine;
import com.colvenna.catalog.compat.Flow;
import com.colvenna.catalog.compat.Settings;
import com.colvenna.catalog.compat.TableType;
import java.util.ArrayList;
import java.util.List;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;
import org.apache.flink.types.Row;
import org.apache.flink.util.CloseableIterator;

/**
 * The scenario on Flink's Table API in batch mode, with the Paimon catalog created as a user does:
 * {@code CREATE CATALOG ... WITH ('type'='paimon', 'metastore'='rest', ...)}. Runs in a local Flink
 * cluster inside the container.
 */
public final class FlinkFlow implements Engine {

    private final TableEnvironment env;

    private FlinkFlow(Settings settings) {
        env = TableEnvironment.create(EnvironmentSettings.inBatchMode());
        env.getConfig().set("table.dml-sync", "true");
        StringBuilder ddl =
                new StringBuilder("CREATE CATALOG " + Settings.CATALOG + " WITH ('type'='paimon'");
        settings.catalogOptions()
                .forEach(
                        (k, v) ->
                                ddl.append(", '")
                                        .append(k)
                                        .append("'='")
                                        .append(v.replace("'", "''"))
                                        .append("'"));
        env.executeSql(ddl.append(")").toString());
        env.executeSql("USE CATALOG " + Settings.CATALOG);
    }

    public static void main(String[] args) {
        Flow.main(args, FlinkFlow::new);
    }

    @Override
    public String family() {
        return "flink";
    }

    @Override
    public String version() {
        try {
            // flink-runtime's, which the image has and the Table API does not depend on.
            return String.valueOf(
                    Class.forName("org.apache.flink.runtime.util.EnvironmentInformation")
                            .getMethod("getVersion")
                            .invoke(null));
        } catch (ReflectiveOperationException e) {
            return "unknown";
        }
    }

    @Override
    public void execute(String sql) throws Exception {
        env.executeSql(sql).await();
    }

    @Override
    public List<Object> firstRow(String sql) throws Exception {
        List<Object> fields = new ArrayList<>();
        try (CloseableIterator<Row> rows = env.executeSql(sql).collect()) {
            if (rows.hasNext()) {
                Row row = rows.next();
                for (int i = 0; i < row.getArity(); i++) {
                    fields.add(row.getField(i));
                }
            }
        }
        return fields;
    }

    @Override
    public List<String> tables(String db) throws Exception {
        return column("SHOW TABLES FROM " + Settings.CATALOG + ".`" + db + "`");
    }

    @Override
    public List<String> databases() throws Exception {
        return column("SHOW DATABASES");
    }

    private List<String> column(String sql) throws Exception {
        List<String> names = new ArrayList<>();
        try (CloseableIterator<Row> rows = env.executeSql(sql).collect()) {
            rows.forEachRemaining(r -> names.add(String.valueOf(r.getField(0))));
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
                + " (id BIGINT, name STRING, amount INT"
                + (type.primaryKey() ? ", PRIMARY KEY (id) NOT ENFORCED" : "")
                + ") WITH ("
                + type.options()
                + ")";
    }

    @Override
    public String setOption(String db, String table, String key, String value) {
        return "ALTER TABLE " + qualified(db, table) + " SET ('" + key + "'='" + value + "')";
    }
}
