package com.colvenna.catalog.perf;

import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.TreeMap;

/** A command's parameters: a properties file the harness writes, {@code key=value} lines. */
public final class Spec {

    private final Properties props;
    private Long start;

    Spec(Properties props) {
        this.props = props;
    }

    static Spec read(Path path) throws Exception {
        Properties p = new Properties();
        try (Reader r = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            p.load(r);
        }
        return new Spec(p);
    }

    /** A spec of the keys under {@code prefix.} (prefix taken off), for one part of a command. */
    Spec part(String prefix) {
        Properties p = new Properties();
        for (String k : props.stringPropertyNames()) {
            if (k.startsWith(prefix + ".")) {
                p.setProperty(k.substring(prefix.length() + 1), props.getProperty(k));
            }
        }
        return new Spec(p);
    }

    String str(String key) {
        String v = props.getProperty(key);
        if (v == null || v.isEmpty()) {
            throw new IllegalArgumentException("the spec has no " + key);
        }
        return v.trim();
    }

    String str(String key, String fallback) {
        String v = props.getProperty(key);
        return v == null || v.isEmpty() ? fallback : v.trim();
    }

    int integer(String key) {
        return Integer.parseInt(str(key));
    }

    int integer(String key, int fallback) {
        return Integer.parseInt(str(key, String.valueOf(fallback)));
    }

    long number(String key) {
        return Long.parseLong(str(key));
    }

    double decimal(String key) {
        return Double.parseDouble(str(key));
    }

    boolean flag(String key, boolean fallback) {
        return Boolean.parseBoolean(str(key, String.valueOf(fallback)));
    }

    /** A comma-separated list (empty items dropped). */
    List<String> list(String key) {
        List<String> out = new ArrayList<>();
        for (String s : str(key, "").split(",")) {
            if (!s.trim().isEmpty()) {
                out.add(s.trim());
            }
        }
        return out;
    }

    /** A copy with one key set. */
    Spec with(String key, String value) {
        Properties p = new Properties();
        p.putAll(props);
        p.setProperty(key, value);
        return new Spec(p);
    }

    /**
     * When the load starts (epoch ms): {@code start} if the spec gives it (so that two forms' loads
     * start together), else {@code startDelay} seconds (default 20) after the first call.
     */
    synchronized long start() {
        if (start == null) {
            String given = props.getProperty("start");
            start =
                    given != null && !given.isEmpty()
                            ? Long.parseLong(given.trim())
                            : System.currentTimeMillis() + 1000L * integer("startDelay", 20);
            Out.line("schedule", "start", start);
        }
        return start;
    }

    Map<String, String> asMap() {
        Map<String, String> m = new TreeMap<>();
        for (String k : props.stringPropertyNames()) {
            m.put(k, props.getProperty(k));
        }
        return m;
    }
}
