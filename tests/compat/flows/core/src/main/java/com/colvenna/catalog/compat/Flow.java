package com.colvenna.catalog.compat;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The flows' command line, shared by the engines' entry points:
 *
 * <ul>
 *   <li>{@code cell <db>}: the writer scenario in a new database, dropped at the end;
 *   <li>{@code fixture <db>}: the tables readers of other engines read, kept;
 *   <li>{@code drop <prefix>}: drops every database whose name starts with the prefix, then lists
 *       the databases again;
 *   <li>{@code probe <db>}: whether this container's own identity can read the files of the
 *       database's tables (a reader's pod runs it beside the reader).
 * </ul>
 *
 * <p>Exit status 0 when the flow ran to its end (its results say what passed), 1 when it could not
 * run at all ({@code COMPAT-FATAL}), 2 for wrong arguments.
 */
public final class Flow {

    /** Opens the engine on the catalog. */
    @FunctionalInterface
    public interface EngineFactory {
        Engine open(Settings settings) throws Exception;
    }

    private Flow() {}

    public static void main(String[] args, EngineFactory engines) {
        if (args.length != 2 || !List.of("cell", "fixture", "drop", "probe").contains(args[0])) {
            System.err.println("usage: <flow> cell|fixture|probe <database> | drop <prefix>");
            System.exit(2);
        }
        int status;
        try {
            Settings settings = Settings.fromEnvironment();
            if (args[0].equals("drop")) {
                Map<String, Object> body = new LinkedHashMap<>();
                body.put("dropped", Paimon.dropDatabases(settings, args[1]));
                body.put("remaining", Paimon.databases(settings, args[1]));
                Report.line("COMPAT-DROPPED", body);
            } else if (args[0].equals("probe")) {
                Report report = new Report();
                for (TableType type : TableType.values()) {
                    String refusal = Paimon.ambientRefusal(settings, args[1], type.table());
                    report.result(
                            type,
                            Op.CREDENTIALS,
                            refusal == null ? Report.PASS : Report.FAIL,
                            refusal == null
                                    ? "this pod's own identity is refused the table's files"
                                    : refusal);
                }
                report.flush();
            } else {
                Engine engine = engines.open(settings);
                Map<String, Object> info = new LinkedHashMap<>();
                info.put("engine", engine.family());
                info.put("engineVersion", engine.version());
                info.putAll(Paimon.clientInfo());
                info.put("dataTokens", settings.dataTokens());
                Report.line("COMPAT-INFO", info);
                Scenario scenario = new Scenario(engine, settings);
                if (args[0].equals("cell")) {
                    scenario.cell(args[1]);
                } else {
                    scenario.fixture(args[1]);
                }
            }
            status = 0;
        } catch (Exception | Error e) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("detail", Report.describe(e));
            Report.line("COMPAT-FATAL", body);
            e.printStackTrace(System.out);
            status = 1;
        }
        System.out.flush();
        // Engines leave non-daemon threads behind; the flow is over.
        System.exit(status);
    }
}
