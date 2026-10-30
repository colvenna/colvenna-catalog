package com.colvenna.catalog.uat.flink;

import java.util.List;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.table.sink.CommitMessage;

/**
 * Writes synthetic rows into tables (id INT, v STRING) through the catalog with per-table data
 * tokens, one batch commit per table, with the Paimon Java API: the data a case's checks then aim
 * at. Prints {@code SEEDED <database>.<table> <rows>} per table.
 *
 * <p>Arguments: database, rows, table... Environment as {@link Catalogs}.
 */
public final class Seed {

    private Seed() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            throw new IllegalArgumentException("usage: Seed <database> <rows> <table>...");
        }
        String db = args[0];
        int rows = Integer.parseInt(args[1]);
        try (Catalog catalog = Catalogs.paimon(true)) {
            for (String name : List.of(args).subList(2, args.length)) {
                Table table = catalog.getTable(Identifier.create(db, name));
                BatchWriteBuilder builder = table.newBatchWriteBuilder();
                try (BatchTableWrite write = builder.newWrite();
                        BatchTableCommit commit = builder.newCommit()) {
                    for (int i = 1; i <= rows; i++) {
                        write.write(GenericRow.of(i, BinaryString.fromString("v" + i)));
                    }
                    List<CommitMessage> messages = write.prepareCommit();
                    commit.commit(messages);
                }
                System.out.println("SEEDED " + db + "." + name + " " + rows);
            }
        }
    }
}
