package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;

/**
 * One cold-start round (terms.md P4): one client per key of {@code keys} (each its own token, so
 * that the gateway's authorizer cache answers none of them), all released at once at {@code start};
 * each makes its first request, {@code GET /v1/config} (as RESTCatalog does when it is made), then
 * {@code getTable} of {@code database.table}. One record per client ({@code <prefix>cold.jsonl}):
 * each request's time and outcome, timed around the Paimon call, the client's own retries included;
 * the client's request log has each attempt.
 */
public final class Cold {

    private Cold() {}

    static void run(Spec spec) throws Exception {
        List<String> keys = spec.list("keys");
        Identifier id = Identifier.create(spec.str("database"), spec.str("table"));
        String round = spec.str("round");
        Records records = Records.open(spec.str("prefix", "") + "cold");
        CyclicBarrier barrier = new CyclicBarrier(keys.size());
        ExecutorService pool = Executors.newFixedThreadPool(keys.size());
        long start = spec.start();
        List<Future<?>> futures = new ArrayList<>();
        for (String key : keys) {
            futures.add(
                    pool.submit(
                            () -> {
                                // The token is read before the barrier: only the requests are
                                // timed.
                                java.util.Map<String, String> options = Clients.options(key);
                                barrier.await();
                                Writers.waitUntil(start);
                                long t1 = System.currentTimeMillis();
                                long n1 = System.nanoTime();
                                RESTApi api = null;
                                String configError = null;
                                String configStatus = null;
                                try {
                                    api =
                                            new RESTApi(
                                                    org.apache.paimon.options.Options.fromMap(
                                                            options));
                                } catch (Exception e) {
                                    configError = Errors.describe(e);
                                    configStatus = Errors.status(e);
                                }
                                long n2 = System.nanoTime();
                                String tableError = null;
                                String tableStatus = null;
                                if (api != null) {
                                    try {
                                        api.getTable(id);
                                    } catch (Exception e) {
                                        tableError = Errors.describe(e);
                                        tableStatus = Errors.status(e);
                                    }
                                }
                                long n3 = System.nanoTime();
                                records.write(
                                        "round", round,
                                        "key", key,
                                        "start", t1,
                                        "configUs", (n2 - n1) / 1000,
                                        "configOk", configError == null,
                                        "configStatus", configStatus,
                                        "configError", configError,
                                        "getTableUs", api == null ? null : (n3 - n2) / 1000,
                                        "getTableOk", api != null && tableError == null,
                                        "getTableStatus", tableStatus,
                                        "getTableError", tableError,
                                        "thread", Thread.currentThread().getName());
                                return null;
                            }));
        }
        for (Future<?> f : futures) {
            f.get();
        }
        pool.shutdown();
        Out.line("cold", "round", round, "clients", records.count());
    }
}
