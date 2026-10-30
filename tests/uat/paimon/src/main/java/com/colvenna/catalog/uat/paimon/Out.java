package com.colvenna.catalog.uat.paimon;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The tools' output: one line {@code UAT <json>} per fact the harness reads, each with its kind and
 * the time it was made. Nothing written holds a token or a credential.
 */
public final class Out {

    private Out() {}

    /** A line of the given kind; the arguments are key, value pairs. */
    public static synchronized void line(String kind, Object... pairs) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("kind", kind);
        m.put("time", Instant.now().toString());
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            m.put(String.valueOf(pairs[i]), pairs[i + 1]);
        }
        System.out.println("UAT " + Json.write(m));
        System.out.flush();
    }

    /** A check's verdict, as the harness writes it to verdicts.tsv. */
    public static void check(String name, boolean ok, String detail) {
        line("check", "check", name, "ok", ok, "detail", detail);
    }

    /** The innermost cause's class and message. */
    @SuppressWarnings("ReferenceEquality") // a cause that is itself ends the chain
    public static String root(Throwable e) {
        Throwable root = e;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        return root.getClass().getSimpleName() + ": " + root.getMessage();
    }
}
