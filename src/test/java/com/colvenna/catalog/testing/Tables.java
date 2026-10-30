package com.colvenna.catalog.testing;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.paimon.types.DataTypes;

/** Small tables written and read through the real Paimon client. */
public final class Tables {

    private Tables() {}

    /** An append table {@code (id INT, payload STRING)}. */
    public static void createAppendTable(
            Catalog catalog, Identifier id, Map<String, String> options) throws Exception {
        catalog.createDatabase(id.getDatabaseName(), true);
        Schema.Builder schema =
                Schema.newBuilder()
                        .column("id", DataTypes.INT())
                        .column("payload", DataTypes.STRING());
        options.forEach(schema::option);
        catalog.createTable(id, schema.build(), false);
    }

    /** A primary-key table {@code (id INT PRIMARY KEY, payload STRING)} with one bucket. */
    public static void createKeyedTable(Catalog catalog, Identifier id, Map<String, String> options)
            throws Exception {
        catalog.createDatabase(id.getDatabaseName(), true);
        Schema.Builder schema =
                Schema.newBuilder()
                        .column("id", DataTypes.INT().notNull())
                        .column("payload", DataTypes.STRING())
                        .primaryKey("id")
                        .option("bucket", "1");
        options.forEach(schema::option);
        catalog.createTable(id, schema.build(), false);
    }

    public static void write(Table table, int... ids) throws Exception {
        BatchWriteBuilder builder = table.newBatchWriteBuilder();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            for (int id : ids) {
                write.write(GenericRow.of(id, BinaryString.fromString("v" + id)));
            }
            commit.commit(write.prepareCommit());
        }
    }

    public static List<Integer> ids(Table table) throws Exception {
        ReadBuilder read = table.newReadBuilder();
        List<Integer> ids = new ArrayList<>();
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan().splits())) {
            reader.forEachRemaining(row -> ids.add(row.getInt(0)));
        }
        return ids;
    }
}
