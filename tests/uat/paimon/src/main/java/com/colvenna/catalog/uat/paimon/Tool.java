package com.colvenna.catalog.uat.paimon;

import java.util.Arrays;

/**
 * The tools' entry point: {@code java -cp uat-paimon.jar:<Paimon jars>:<Flink's lib> ...Tool <tool>
 * <arguments>}. See each tool's class for its arguments and output.
 */
public final class Tool {

    private Tool() {}

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            throw new IllegalArgumentException(
                    "usage: Tool stress|maintenance|writer|reader|credentials|write|state|outside|fenced-commit|reads ...");
        }
        String[] rest = Arrays.copyOfRange(args, 1, args.length);
        switch (args[0]) {
            case "stress":
                CommitStress.run(rest);
                break;
            case "maintenance":
                MaintenanceLoop.run(rest);
                break;
            case "writer":
            case "reader":
            case "credentials":
                Loops.run(args[0], rest);
                break;
            case "write":
                Tables.run(rest);
                break;
            case "state":
                State.run(rest);
                break;
            case "outside":
            case "fenced-commit":
            case "reads":
                Fence.run(args[0], rest);
                break;
            default:
                throw new IllegalArgumentException("unknown tool " + args[0]);
        }
        Out.line("done", "tool", args[0]);
        // Paimon's and Flink's thread pools must not keep the JVM alive.
        System.exit(0);
    }
}
