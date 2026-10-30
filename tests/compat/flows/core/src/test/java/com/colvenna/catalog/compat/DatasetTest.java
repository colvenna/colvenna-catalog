package com.colvenna.catalog.compat;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;

/** The expectations agree with the writes: replays the writes' statements on plain Java tables. */
class DatasetTest {

    private static final Pattern ROW =
            Pattern.compile("\\(CAST\\((\\d+) AS BIGINT\\), '([a-z]\\d+)', (\\d+)\\)");

    @Test
    void everyCheckIsWhatTheWritesLeave() {
        for (TableType type : TableType.values()) {
            Map<Long, Long> byKey = new TreeMap<>();
            List<long[]> appended = new ArrayList<>();
            for (Dataset.Write write : Dataset.writes(type)) {
                String sql = write.sql("t");
                if (sql.startsWith("DELETE FROM t WHERE id <= ")) {
                    long upTo =
                            Long.parseLong(sql.substring("DELETE FROM t WHERE id <= ".length()));
                    byKey.keySet().removeIf(id -> id <= upTo);
                    continue;
                }
                assertTrue(sql.startsWith("INSERT INTO t VALUES "), sql);
                Matcher m = ROW.matcher(sql);
                while (m.find()) {
                    long id = Long.parseLong(m.group(1));
                    long amount = Long.parseLong(m.group(3));
                    byKey.put(id, amount);
                    appended.add(new long[] {id, amount});
                }
            }
            for (Dataset.Check check : Dataset.checks(type)) {
                long actual;
                if (type.primaryKey()) {
                    actual =
                            evaluate(
                                    check,
                                    byKey.entrySet().stream()
                                            .map(e -> new long[] {e.getKey(), e.getValue()})
                                            .collect(java.util.stream.Collectors.toList()));
                } else {
                    actual = evaluate(check, appended);
                }
                assertEquals(check.expected(), actual, type + " " + check.name());
            }
        }
    }

    @Test
    void onlyPrimaryKeyTablesDelete() {
        assertEquals(3, Dataset.writes(TableType.PK).size());
        assertEquals(4, Dataset.writes(TableType.PK_DV).size());
        assertTrue(Dataset.writes(TableType.PK_DV).get(3).writeOnly());
        assertEquals(2, Dataset.writes(TableType.APPEND).size());
    }

    @Test
    void theTableOptionsAreTheTypes() {
        assertEquals("'bucket'='-1'", TableType.APPEND.options());
        assertEquals("'bucket'='2', 'deletion-vectors.enabled'='false'", TableType.PK.options());
        assertEquals(
                "'bucket'='2', 'deletion-vectors.enabled'='true',"
                        + " 'deletion-vectors.merge-on-read'='true'",
                TableType.PK_DV.options());
    }

    private static long evaluate(Dataset.Check check, List<long[]> rows) {
        String sql = check.sql("t");
        List<long[]> selected = new ArrayList<>();
        for (long[] row : rows) {
            if (matches(sql, row[0])) {
                selected.add(row);
            }
        }
        if (sql.startsWith("SELECT count(*) ")) {
            return selected.size();
        }
        assertTrue(sql.startsWith("SELECT sum(amount) "), sql);
        return selected.stream().mapToLong(r -> r[1]).sum();
    }

    private static boolean matches(String sql, long id) {
        if (sql.endsWith(" WHERE id = 100")) {
            return id == 100;
        }
        if (sql.endsWith(" WHERE id <= 5")) {
            return id <= 5;
        }
        if (sql.endsWith(" WHERE id > 110")) {
            return id > 110;
        }
        assertTrue(!sql.contains(" WHERE "), "a condition the replay does not know: " + sql);
        return true;
    }
}
