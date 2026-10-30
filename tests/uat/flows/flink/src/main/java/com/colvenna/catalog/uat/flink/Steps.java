package com.colvenna.catalog.uat.flink;

import java.util.Locale;

/**
 * The flows' output: one line {@code STEP <check> OK|FAIL <ms>ms <detail>} per check of the case,
 * which the harness turns into its verdicts; the first failure stops the flow (later steps depend
 * on earlier ones, and a missing check fails the case).
 */
public final class Steps {

    /** A step's body. */
    @FunctionalInterface
    public interface Body {
        String run() throws Exception;
    }

    private Steps() {}

    /** Runs the step and prints its line; rethrows its failure. */
    public static void step(String name, Body body) throws Exception {
        long start = System.nanoTime();
        String detail;
        try {
            detail = body.run();
        } catch (Throwable e) {
            System.out.println("STEP " + name + " FAIL " + oneLine(e.toString()));
            e.printStackTrace(System.out);
            throw e instanceof Exception ? (Exception) e : new RuntimeException(e);
        }
        System.out.println(
                String.format(
                        Locale.ROOT,
                        "STEP %s OK %dms %s",
                        name,
                        (System.nanoTime() - start) / 1_000_000,
                        oneLine(detail)));
    }

    public static void expect(long expected, long actual, String what) {
        if (expected != actual) {
            throw new AssertionError(what + ": expected " + expected + " but was " + actual);
        }
    }

    static String oneLine(String text) {
        return text == null ? "" : text.replaceAll("\\s+", " ");
    }
}
