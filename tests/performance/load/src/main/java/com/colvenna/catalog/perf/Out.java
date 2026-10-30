package com.colvenna.catalog.perf;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The tool's facts for the harness: one line {@code PERF <json>} on stdout per fact, with its kind
 * and time. Nothing written holds a token or a credential.
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
        System.out.println("PERF " + Json.write(m));
        System.out.flush();
    }
}
