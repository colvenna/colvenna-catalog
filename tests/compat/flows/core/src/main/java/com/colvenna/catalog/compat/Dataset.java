package com.colvenna.catalog.compat;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The synthetic data every table gets, and what reading it must give back. Each table has the
 * columns {@code id BIGINT, name STRING, amount INT}:
 *
 * <ol>
 *   <li>insert ids 1..100, with amount = id;
 *   <li>insert ids 91..110, with amount = 1000 + id: an upsert of 91..100 on a primary-key table,
 *       twenty more rows on an append table;
 *   <li>primary-key tables only: delete ids 1..5;
 *   <li>the deletion-vector table only: insert ids 111..120 with {@code write-only} set first, so
 *       that they stay in uncompacted level-0 files, which a deletion-vector table's batch reads
 *       include only with {@code deletion-vectors.merge-on-read} (Paimon 2.0 and later): the state
 *       such a table is in between a streaming writer's compactions.
 * </ol>
 *
 * <p>Readers of other engines (Doris, StarRocks) get the same checks from the fixture's output, so
 * this class is the one place the expectations live.
 */
public final class Dataset {

    private Dataset() {}

    /**
     * One write: a name for the results and the statement, given the qualified table name; a
     * write-only one sets the table's {@code write-only} option first, so that it compacts nothing.
     */
    public static final class Write {
        private final String name;
        private final String template;
        private final boolean writeOnly;

        Write(String name, String template) {
            this(name, template, false);
        }

        Write(String name, String template, boolean writeOnly) {
            this.name = name;
            this.template = template;
            this.writeOnly = writeOnly;
        }

        public String name() {
            return name;
        }

        public boolean writeOnly() {
            return writeOnly;
        }

        public String sql(String qualifiedTable) {
            return template.replace("@T@", qualifiedTable);
        }
    }

    /**
     * One read check: {@code SELECT <expression> FROM <table> [WHERE <where>]} must give expected.
     */
    public static final class Check {
        private final String name;
        private final String expression;
        private final String where;
        private final long expected;

        Check(String name, String expression, String where, long expected) {
            this.name = name;
            this.expression = expression;
            this.where = where;
            this.expected = expected;
        }

        public String name() {
            return name;
        }

        public long expected() {
            return expected;
        }

        public String sql(String qualifiedTable) {
            return "SELECT "
                    + expression
                    + " FROM "
                    + qualifiedTable
                    + (where.isEmpty() ? "" : " WHERE " + where);
        }

        /** The check as the fixture prints it for readers of other engines. */
        public Map<String, Object> describe() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("name", name);
            out.put("expression", expression);
            out.put("where", where);
            out.put("expected", expected);
            return out;
        }
    }

    public static List<Write> writes(TableType type) {
        List<Write> writes = new ArrayList<>();
        writes.add(
                new Write("insert ids 1..100", "INSERT INTO @T@ VALUES " + rows(1, 100, 0, "n")));
        writes.add(
                new Write(
                        type.primaryKey() ? "upsert ids 91..110" : "insert ids 91..110",
                        "INSERT INTO @T@ VALUES " + rows(91, 110, 1000, "u")));
        if (type.primaryKey()) {
            writes.add(new Write("delete ids 1..5", "DELETE FROM @T@ WHERE id <= 5"));
        }
        if (type.deletionVectors()) {
            writes.add(
                    new Write(
                            "insert ids 111..120, left uncompacted",
                            "INSERT INTO @T@ VALUES " + rows(111, 120, 0, "l"),
                            true));
        }
        return Collections.unmodifiableList(writes);
    }

    public static List<Check> checks(TableType type) {
        List<Check> checks = new ArrayList<>();
        // ids 91..110 at 1000 + id: 20 rows summing to 22010.
        long upserted = 22010;
        if (type.primaryKey()) {
            // ids 6..90 keep amount = id (4080), 91..110 are the upserted ones; the
            // deletion-vector table also has ids 111..120 at amount = id (1155), uncompacted.
            long uncompacted = type.deletionVectors() ? 10 : 0;
            checks.add(new Check("rows", "count(*)", "", 105 + uncompacted));
            checks.add(
                    new Check(
                            "sum",
                            "sum(amount)",
                            "",
                            4080 + upserted + (type.deletionVectors() ? 1155 : 0)));
            checks.add(new Check("upserted", "sum(amount)", "id = 100", 1100));
            checks.add(new Check("deleted", "count(*)", "id <= 5", 0));
            if (type.deletionVectors()) {
                checks.add(new Check("uncompacted", "count(*)", "id > 110", uncompacted));
            }
        } else {
            checks.add(new Check("rows", "count(*)", "", 120));
            checks.add(new Check("sum", "sum(amount)", "", 5050 + upserted));
            checks.add(new Check("duplicates", "count(*)", "id = 100", 2));
        }
        return Collections.unmodifiableList(checks);
    }

    private static String rows(int from, int to, int offset, String prefix) {
        StringBuilder values = new StringBuilder();
        for (int id = from; id <= to; id++) {
            if (id > from) {
                values.append(", ");
            }
            values.append(
                    String.format(
                            Locale.ROOT,
                            "(CAST(%d AS BIGINT), '%s%d', %d)",
                            id,
                            prefix,
                            id,
                            offset + id));
        }
        return values.toString();
    }
}
