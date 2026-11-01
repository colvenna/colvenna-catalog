package com.colvenna.catalog.perf;

import java.nio.file.Path;

/**
 * The load tool's entry point: {@code java -cp perf-load.jar:<Paimon jars>:<Flink's lib>
 * com.colvenna.catalog.perf.Load <command> <spec.properties>}.
 *
 * <ul>
 *   <li>{@code seed}: databases and tables of the run (and one snapshot each), {@link Seed};
 *   <li>{@code drop}: the run's databases, with their tables, {@link Drop};
 *   <li>{@code writers}: closed-loop committers, {@link Writers} (P1a, P1b, P1c);
 *   <li>{@code reads}: the open-loop metadata read mix, {@link Reads} (P2);
 *   <li>{@code tokens}: open-loop credential requests, {@link Tokens} (P3);
 *   <li>{@code cold}: one cold-start round, {@link Cold} (P4);
 *   <li>{@code mixed}: writers, reads and credential requests at once, {@link Mixed} (P5).
 * </ul>
 *
 * Every command reads its parameters from the spec (a properties file), its tokens from files in
 * {@code PERF_TOKENS} (one per key, never an argument or an environment variable), and writes its
 * records as JSON lines under {@code PERF_OUT}; the Paimon client's own request log goes to {@code
 * PERF_OUT/rest.log} (the launcher's log4j2 configuration). Its facts are lines {@code PERF <json>}
 * on stdout.
 */
public final class Load {

    private Load() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException(
                    "usage: Load seed|drop|writers|reads|tokens|cold|mixed <spec.properties>");
        }
        Spec spec = Spec.read(Path.of(args[1]));
        Out.line("start", "command", args[0], "spec", spec.asMap());
        int status = 0;
        try {
            switch (args[0]) {
                case "seed":
                    Seed.run(spec);
                    break;
                case "drop":
                    Drop.run(spec);
                    break;
                case "writers":
                    Writers.run(spec, "");
                    break;
                case "reads":
                    Reads.run(spec, "");
                    break;
                case "tokens":
                    Tokens.run(spec, "");
                    break;
                case "cold":
                    Cold.run(spec);
                    break;
                case "mixed":
                    Mixed.run(spec);
                    break;
                default:
                    throw new IllegalArgumentException("unknown command " + args[0]);
            }
        } catch (Exception e) {
            Out.line("failed", "command", args[0], "error", Errors.describe(e));
            status = 1;
        } finally {
            Records.closeAll();
        }
        Out.line("done", "command", args[0], "status", status);
        // Paimon's thread pools must not keep the JVM alive.
        System.exit(status);
    }
}
