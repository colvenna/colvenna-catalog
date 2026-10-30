package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

/**
 * P5's load on one form (terms.md): its writers ({@code writers.*}, {@link Writers}), its reads
 * ({@code reads.*}, {@link Reads}) and its credential requests ({@code tokens.*}, {@link Tokens}),
 * all at once in one JVM, each part with its own records ({@code writers-commits.jsonl}, {@code
 * reads-reads.jsonl}, {@code tokens-tokens.jsonl}).
 */
public final class Mixed {

    private Mixed() {}

    interface Part {
        void run() throws Exception;
    }

    static void run(Spec spec) throws Exception {
        String start = String.valueOf(spec.start());
        List<Part> parts = new ArrayList<>();
        parts.add(() -> Writers.run(spec.part("writers").with("start", start), "writers-"));
        parts.add(() -> Reads.run(spec.part("reads").with("start", start), "reads-"));
        parts.add(() -> Tokens.run(spec.part("tokens").with("start", start), "tokens-"));
        ExecutorService pool = Executors.newFixedThreadPool(parts.size());
        List<Future<?>> futures = new ArrayList<>();
        for (Part p : parts) {
            futures.add(
                    pool.submit(
                            () -> {
                                p.run();
                                return null;
                            }));
        }
        Exception first = null;
        for (Future<?> f : futures) {
            try {
                f.get();
            } catch (Exception e) {
                Out.line("part-failed", "error", Errors.describe(e));
                first = first == null ? e : first;
            }
        }
        pool.shutdown();
        if (first != null) {
            throw first;
        }
    }
}
