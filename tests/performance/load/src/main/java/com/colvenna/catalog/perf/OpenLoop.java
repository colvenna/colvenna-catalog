package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.LockSupport;

/**
 * Requests sent open-loop at a fixed rate (terms.md section 3): request i of a step is due at the
 * step's start plus i / rate, and is handed to a pool of {@code threads} threads at that time,
 * whatever the earlier ones are doing, so a slow catalog cannot slow the load down. Its latency is
 * measured from the time it was due ({@code latUs}); the time from due to its start on a thread
 * ({@code lagUs}) says whether it was sent on time. One record per request.
 */
final class OpenLoop {

    /** One step of the load: {@code rate} requests per second for {@code seconds}. */
    static final class Step {
        final double rate;
        final long seconds;
        final String label;

        Step(double rate, long seconds, String label) {
            this.rate = rate;
            this.seconds = seconds;
            this.label = label;
        }
    }

    /**
     * One request: makes it as {@code what} (its description) says, and returns what the record
     * says of it besides the times and the description.
     */
    interface Request {
        Map<String, Object> call(long index, Map<String, Object> what) throws Exception;
    }

    /** What the request was, before it is made (its operation, table, principal...). */
    interface Describe {
        Map<String, Object> of(long index);
    }

    private OpenLoop() {}

    /** Parses {@code rate:seconds[:label],...}. */
    static List<Step> steps(String text) {
        List<Step> steps = new ArrayList<>();
        for (String s : text.split(",")) {
            String[] p = s.trim().split(":");
            steps.add(
                    new Step(
                            Double.parseDouble(p[0]),
                            Long.parseLong(p[1]),
                            p.length > 2 ? p[2] : p[0]));
        }
        return steps;
    }

    /**
     * Runs the steps one after the other from {@code start} (epoch ms), then waits up to {@code
     * drainSeconds} for the requests still running; those that did not end are recorded as {@code
     * unfinished}. Returns the number of requests sent.
     */
    static long run(
            Records records,
            List<Step> steps,
            int threads,
            long start,
            long drainSeconds,
            Describe describe,
            Request request)
            throws Exception {
        ThreadPoolExecutor pool =
                new ThreadPoolExecutor(
                        threads, threads, 60, TimeUnit.SECONDS, new LinkedBlockingQueue<>());
        pool.prestartAllCoreThreads();
        AtomicLong pending = new AtomicLong();
        Writers.waitUntil(start);
        // One clock for the whole schedule: the wall clock at its start plus the monotonic clock.
        long baseMillis = System.currentTimeMillis();
        long baseNanos = System.nanoTime();
        long index = 0;
        long stepNanos = 0;
        for (Step step : steps) {
            long n = Math.round(step.rate * step.seconds);
            double every = 1e9 / step.rate;
            for (long i = 0; i < n; i++) {
                long dueNanos = baseNanos + stepNanos + Math.round(i * every);
                long wait = dueNanos - System.nanoTime();
                if (wait > 0) {
                    LockSupport.parkNanos(wait);
                }
                long due = baseMillis + (dueNanos - baseNanos) / 1_000_000;
                long dueNano = dueNanos;
                long idx = index++;
                Map<String, Object> what = describe.of(idx);
                pending.incrementAndGet();
                pool.execute(
                        () -> {
                            long started = System.nanoTime();
                            String error = null;
                            String status = null;
                            Map<String, Object> extra = null;
                            try {
                                extra = request.call(idx, what);
                            } catch (Exception e) {
                                error = Errors.describe(e);
                                status = Errors.status(e);
                            }
                            long ended = System.nanoTime();
                            Object[] pairs = {
                                "i",
                                idx,
                                "step",
                                step.label,
                                "due",
                                due,
                                "start",
                                baseMillis + (started - baseNanos) / 1_000_000,
                                "end",
                                baseMillis + (ended - baseNanos) / 1_000_000,
                                "lagUs",
                                (started - dueNano) / 1000,
                                "latUs",
                                (ended - dueNano) / 1000,
                                "svcUs",
                                (ended - started) / 1000,
                                "ok",
                                error == null,
                                "status",
                                status,
                                "error",
                                error,
                                "thread",
                                Thread.currentThread().getName()
                            };
                            List<Object> all = new ArrayList<>(Arrays.asList(pairs));
                            for (Map<String, Object> m : Arrays.asList(what, extra)) {
                                if (m != null) {
                                    m.forEach(
                                            (k, v) -> {
                                                all.add(k);
                                                all.add(v);
                                            });
                                }
                            }
                            try {
                                records.write(all.toArray());
                            } catch (RuntimeException e) {
                                Out.line("not-recorded", "i", idx, "error", Errors.describe(e));
                            } finally {
                                pending.decrementAndGet();
                            }
                        });
            }
            stepNanos += step.seconds * 1_000_000_000L;
        }
        pool.shutdown();
        if (!pool.awaitTermination(drainSeconds, TimeUnit.SECONDS)) {
            Out.line("unfinished", "requests", pending.get());
            pool.shutdownNow();
        }
        return index;
    }
}
