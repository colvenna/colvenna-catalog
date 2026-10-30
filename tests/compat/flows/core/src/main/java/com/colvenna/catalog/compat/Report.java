package com.colvenna.catalog.compat;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * What the flow prints for the runner: one {@code COMPAT-RESULT <json>} line per table type and
 * operation, {@code COMPAT-STEP} lines as it goes, and {@code COMPAT-INFO} and {@code
 * COMPAT-FIXTURE} lines. Everything else on standard output is the engine's own log.
 */
public final class Report {

    public static final String PASS = "PASS";
    public static final String FAIL = "FAIL";
    public static final String NOT_RUN = "NOT_RUN";

    private final Map<String, Map<String, Object>> results = new LinkedHashMap<>();

    public static void line(String kind, Map<String, Object> body) {
        System.out.println(kind + " " + Json.write(body));
        System.out.flush();
    }

    public static void step(TableType type, String step, boolean ok, long ms, String detail) {
        System.out.printf(
                Locale.ROOT,
                "COMPAT-STEP %s %s %s %dms %s%n",
                type == null ? "-" : type.id(),
                step,
                ok ? "OK" : "FAIL",
                ms,
                detail);
        System.out.flush();
    }

    /** Records the result of an operation on a table type; a later record replaces it. */
    public void result(TableType type, Op op, String status, String detail) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", type.id());
        body.put("op", op.id());
        body.put("status", status);
        body.put("detail", detail);
        results.put(type.id() + "/" + op.id(), body);
    }

    public boolean has(TableType type, Op op) {
        return results.containsKey(type.id() + "/" + op.id());
    }

    public boolean passed(TableType type, Op op) {
        Map<String, Object> body = results.get(type.id() + "/" + op.id());
        return body != null && PASS.equals(body.get("status"));
    }

    /** Prints every recorded result. */
    public void flush() {
        for (Map<String, Object> body : results.values()) {
            line("COMPAT-RESULT", body);
        }
    }

    /** An exception as one line: its class and the first line of its message, at most 400 chars. */
    @SuppressWarnings(
            "ReferenceEquality") // the cause chain ends where a throwable is its own cause
    public static String describe(Throwable e) {
        Throwable root = e;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        String text = firstLine(e) + (root == e ? "" : " (cause: " + firstLine(root) + ")");
        return text.length() > 400 ? text.substring(0, 400) + "..." : text;
    }

    private static String firstLine(Throwable e) {
        String message = String.valueOf(e.getMessage());
        int newline = message.indexOf('\n');
        return e.getClass().getSimpleName()
                + ": "
                + (newline < 0 ? message : message.substring(0, newline));
    }
}
