package com.colvenna.catalog.perf;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The raw records of a command: one JSON line per operation, in {@code PERF_OUT/<name>.jsonl}.
 * Every time is the JVM's wall clock in epoch milliseconds, the same clock the client's request log
 * uses, and every duration is in microseconds.
 */
public final class Records implements AutoCloseable {

    private static final List<Records> OPEN = new ArrayList<>();

    private final BufferedWriter out;
    private long count;

    private Records(Path path) throws IOException {
        Files.createDirectories(path.getParent());
        this.out = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
    }

    /** The records file of that name, made afresh. */
    static synchronized Records open(String name) {
        return open(dir().resolve(name));
    }

    /** The records file {@code <path>.jsonl}, made afresh. */
    static synchronized Records open(Path path) {
        try {
            Records r = new Records(path.resolveSibling(path.getFileName() + ".jsonl"));
            OPEN.add(r);
            return r;
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    static Path dir() {
        return Path.of(System.getenv().getOrDefault("PERF_OUT", "/work/out"));
    }

    /** One record; the arguments are key, value pairs. */
    synchronized void write(Object... pairs) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            m.put(String.valueOf(pairs[i]), pairs[i + 1]);
        }
        try {
            out.write(Json.write(m));
            out.write('\n');
            if (++count % 500 == 0) {
                out.flush();
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    synchronized long count() {
        return count;
    }

    @Override
    public synchronized void close() throws IOException {
        out.close();
    }

    static synchronized void closeAll() {
        for (Records r : OPEN) {
            try {
                r.close();
            } catch (IOException e) {
                Out.line("records-not-closed", "error", e.toString());
            }
        }
        OPEN.clear();
    }
}
