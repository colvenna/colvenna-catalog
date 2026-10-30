package com.colvenna.catalog.compat;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * The scenario of a writer cell, run through the engine's SQL on each table type: create the
 * database and the table, write and commit (Dataset), read it back, probe that the engine's own
 * identity cannot reach the table's files, drop the table and the database. Each operation's result
 * is printed once the cell is over; see Report.
 */
public final class Scenario {

    @FunctionalInterface
    private interface Body {
        void run() throws Exception;
    }

    private final Engine engine;
    private final Settings settings;
    private final Report report = new Report();

    public Scenario(Engine engine, Settings settings) {
        this.engine = engine;
        this.settings = settings;
    }

    /** The whole lifecycle in database {@code db}, which the scenario creates and drops. */
    public void cell(String db) {
        try {
            String created = attempt(null, "create-database", () -> createDatabase(db));
            for (TableType type : TableType.values()) {
                if (created != null) {
                    blocked(type, "the database could not be created: " + created);
                } else {
                    table(db, type, true);
                }
            }
            if (created == null) {
                String dropped =
                        attempt(
                                null,
                                "drop-database",
                                () -> {
                                    engine.execute("DROP DATABASE " + Settings.CATALOG + "." + db);
                                    require(
                                            !engine.databases().contains(db),
                                            "the database is still listed after the drop");
                                });
                if (dropped != null) {
                    for (TableType type : TableType.values()) {
                        report.result(
                                type,
                                Op.DDL,
                                Report.FAIL,
                                "the database could not be dropped: " + dropped);
                    }
                }
            } else {
                for (TableType type : TableType.values()) {
                    report.result(type, Op.DDL, Report.FAIL, "create database failed: " + created);
                }
            }
        } finally {
            report.flush();
        }
    }

    /**
     * The tables other engines read: the database and each table type's table, written and read
     * back, never dropped (the runner drops them when the readers are done). Prints each table's
     * checks as a {@code COMPAT-FIXTURE} line.
     */
    public void fixture(String db) {
        try {
            String created = attempt(null, "create-database", () -> createDatabase(db));
            for (TableType type : TableType.values()) {
                if (created != null) {
                    blocked(type, "the database could not be created: " + created);
                    report.result(type, Op.DDL, Report.FAIL, "create database failed: " + created);
                } else {
                    table(db, type, false);
                }
            }
        } finally {
            report.flush();
        }
    }

    private void createDatabase(String db) throws Exception {
        engine.execute("CREATE DATABASE " + Settings.CATALOG + "." + db);
        require(engine.databases().contains(db), "the new database is not listed");
    }

    private void table(String db, TableType type, boolean drop) {
        String table = engine.qualified(db, type.table());
        String created =
                attempt(
                        type,
                        "create-table",
                        () -> {
                            engine.execute(engine.createTable(db, type));
                            require(
                                    engine.tables(db).contains(type.table()),
                                    "the new table is not listed");
                        });
        if (created != null) {
            report.result(type, Op.DDL, Report.FAIL, "create table failed: " + created);
            blocked(type, "the table could not be created");
            return;
        }

        String wrote = write(db, type, table);
        if (wrote != null) {
            report.result(type, Op.WRITE, Report.FAIL, wrote);
            report.result(type, Op.READ, Report.NOT_RUN, "blocked: the write failed");
        } else {
            String read = read(type, table);
            report.result(
                    type,
                    Op.READ,
                    read == null ? Report.PASS : Report.FAIL,
                    read == null ? describeChecks(type) : read);
            if (!drop && read == null) {
                Map<String, Object> fixture = new LinkedHashMap<>();
                fixture.put("table", type.id());
                fixture.put("name", type.table());
                fixture.put("checks", checks(type));
                Report.line("COMPAT-FIXTURE", fixture);
            }
        }
        credentials(db, type);

        if (!drop) {
            report.result(type, Op.DDL, Report.PASS, "created; kept for the readers");
            return;
        }
        String dropped =
                attempt(
                        type,
                        "drop-table",
                        () -> {
                            engine.execute("DROP TABLE " + table);
                            require(
                                    !engine.tables(db).contains(type.table()),
                                    "the table is still listed after the drop");
                        });
        report.result(
                type,
                Op.DDL,
                dropped == null ? Report.PASS : Report.FAIL,
                dropped == null
                        ? "create and drop of the database and the table"
                        : "drop table failed: " + dropped);
    }

    /** The table type's writes, then the commits they made; null if all is well, else why not. */
    private String write(String db, TableType type, String table) {
        List<Dataset.Write> writes = Dataset.writes(type);
        for (Dataset.Write write : writes) {
            String failed =
                    attempt(
                            type,
                            write.name(),
                            () -> {
                                if (write.writeOnly()) {
                                    engine.execute(
                                            engine.setOption(
                                                    db, type.table(), "write-only", "true"));
                                }
                                engine.execute(write.sql(table));
                            });
            if (failed != null) {
                return write.name() + ": " + failed;
            }
        }
        long[] snapshots = new long[1];
        String counted =
                attempt(
                        type,
                        "count-snapshots",
                        () ->
                                snapshots[0] =
                                        scalar(
                                                "SELECT count(*) FROM "
                                                        + engine.qualified(
                                                                db, type.table() + "$snapshots")));
        if (counted != null) {
            return "the snapshots could not be listed: " + counted;
        }
        if (snapshots[0] < writes.size()) {
            return String.format(
                    Locale.ROOT, "%d writes left %d snapshots", writes.size(), snapshots[0]);
        }
        String vectors = "";
        if (type.deletionVectors()) {
            // The upsert and the delete must have left deletion vectors rather than rewritten
            // files: otherwise a reader of this table would never meet one.
            long[] indexes = new long[1];
            String listed =
                    attempt(
                            type,
                            "count-deletion-vectors",
                            () ->
                                    indexes[0] =
                                            scalar(
                                                    "SELECT count(*) FROM "
                                                            + engine.qualified(
                                                                    db,
                                                                    type.table() + "$table_indexes")
                                                            + " WHERE index_type ="
                                                            + " 'DELETION_VECTORS'"));
            if (listed != null) {
                return "the deletion vectors could not be listed: " + listed;
            }
            if (indexes[0] < 1) {
                return "the writes left no deletion vector";
            }
            // And the write-only insert must still be in level 0, uncompacted.
            long[] level0 = new long[1];
            String files =
                    attempt(
                            type,
                            "count-level-0-files",
                            () ->
                                    level0[0] =
                                            scalar(
                                                    "SELECT count(*) FROM "
                                                            + engine.qualified(
                                                                    db, type.table() + "$files")
                                                            + " WHERE `level` = 0"));
            if (files != null) {
                return "the table's files could not be listed: " + files;
            }
            if (level0[0] < 1) {
                return "the write-only insert left no level-0 file";
            }
            vectors =
                    String.format(
                            Locale.ROOT,
                            "; %d deletion vector index files, %d level-0 files",
                            indexes[0],
                            level0[0]);
        }
        report.result(
                type,
                Op.WRITE,
                Report.PASS,
                String.format(
                        Locale.ROOT,
                        "%d writes committed (%s); %d snapshots%s",
                        writes.size(),
                        String.join(
                                ", ",
                                writes.stream().map(Dataset.Write::name).toArray(String[]::new)),
                        snapshots[0],
                        vectors));
        return null;
    }

    /** The table type's read checks; null if every one gives what it must, else the mismatches. */
    private String read(TableType type, String table) {
        StringBuilder wrong = new StringBuilder();
        for (Dataset.Check check : Dataset.checks(type)) {
            long[] actual = new long[1];
            String failed =
                    attempt(
                            type,
                            "read-" + check.name(),
                            () -> actual[0] = scalar(check.sql(table)));
            if (failed != null) {
                wrong.append(check.name()).append(": ").append(failed).append("; ");
            } else if (actual[0] != check.expected()) {
                wrong.append(
                        String.format(
                                Locale.ROOT,
                                "%s: expected %d, got %d; ",
                                check.name(),
                                check.expected(),
                                actual[0]));
            }
        }
        return wrong.length() == 0 ? null : wrong.substring(0, wrong.length() - 2);
    }

    private void credentials(String db, TableType type) {
        if (!settings.dataTokens()) {
            report.result(
                    type,
                    Op.CREDENTIALS,
                    Report.NOT_RUN,
                    "the flow ran without the catalog's per-table credentials");
            return;
        }
        String[] refusal = new String[1];
        String failed =
                attempt(
                        type,
                        "probe-own-identity",
                        () -> refusal[0] = Paimon.ambientRefusal(settings, db, type.table()));
        if (failed != null) {
            report.result(type, Op.CREDENTIALS, Report.FAIL, "the probe failed: " + failed);
        } else if (refusal[0] != null) {
            report.result(type, Op.CREDENTIALS, Report.FAIL, refusal[0]);
        } else {
            report.result(
                    type,
                    Op.CREDENTIALS,
                    Report.PASS,
                    "the engine's own identity is refused the table's files; it wrote and read"
                            + " with data-token.enabled=true and no S3 keys of its own");
        }
    }

    private void blocked(TableType type, String why) {
        for (Op op : new Op[] {Op.WRITE, Op.READ, Op.CREDENTIALS}) {
            if (!report.has(type, op)) {
                report.result(type, op, Report.NOT_RUN, "blocked: " + why);
            }
        }
    }

    private static String describeChecks(TableType type) {
        StringBuilder out = new StringBuilder();
        for (Dataset.Check check : Dataset.checks(type)) {
            out.append(out.length() == 0 ? "" : ", ")
                    .append(check.name())
                    .append('=')
                    .append(check.expected());
        }
        return out.toString();
    }

    private static List<Map<String, Object>> checks(TableType type) {
        return Dataset.checks(type).stream()
                .map(Dataset.Check::describe)
                .collect(Collectors.toList());
    }

    private long scalar(String sql) throws Exception {
        List<Object> row = engine.firstRow(sql);
        if (row.isEmpty() || row.get(0) == null) {
            throw new IllegalStateException("no value for " + sql);
        }
        Object value = row.get(0);
        if (!(value instanceof Number)) {
            throw new IllegalStateException("not a number: " + value + " for " + sql);
        }
        return ((Number) value).longValue();
    }

    private static void require(boolean condition, String why) {
        if (!condition) {
            throw new IllegalStateException(why);
        }
    }

    /**
     * Runs one step: null if it succeeded, else what went wrong (its stack trace goes to the log).
     */
    private static String attempt(TableType type, String step, Body body) {
        long start = System.nanoTime();
        try {
            body.run();
            Report.step(type, step, true, (System.nanoTime() - start) / 1_000_000, "");
            return null;
        } catch (Exception | Error e) {
            String why = Report.describe(e);
            Report.step(type, step, false, (System.nanoTime() - start) / 1_000_000, why);
            e.printStackTrace(System.out);
            return why;
        }
    }
}
