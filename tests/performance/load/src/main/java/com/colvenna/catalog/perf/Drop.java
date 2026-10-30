package com.colvenna.catalog.perf;

import java.util.List;
import org.apache.paimon.catalog.Catalog;

/**
 * The run's clean-up in the catalog: drops the databases {@code databases} with their tables, with
 * the owner's token, each tried again on a failure (a serverless install answers a large drop with
 * 503 after 10 s of dropping, to be asked again), then lists what is left of them. One record per
 * database ({@code drop.jsonl}); the fact {@code left} names those still there.
 */
public final class Drop {

    private Drop() {}

    static void run(Spec spec) throws Exception {
        Records records = Records.open("drop");
        try (Catalog catalog = Clients.catalog(spec.str("owner"))) {
            // A run's leftovers, by hand: every database whose name ends in _<run>.
            List<String> databases = new java.util.ArrayList<>(spec.list("databases"));
            String suffix = spec.str("suffix", "");
            if (!suffix.isEmpty()) {
                for (String db : catalog.listDatabases()) {
                    if (db.endsWith("_" + suffix) && !databases.contains(db)) {
                        databases.add(db);
                    }
                }
                Out.line("found", "databases", databases);
            }
            for (String db : databases) {
                long start = System.currentTimeMillis();
                String error = null;
                int attempts = 0;
                while (attempts < 10) {
                    attempts++;
                    try {
                        catalog.dropDatabase(db, true, true);
                        error = null;
                        break;
                    } catch (Exception e) {
                        error = Errors.describe(e);
                        Seed.sleep(2000L * Math.min(attempts, 5));
                    }
                }
                records.write(
                        "db",
                        db,
                        "ok",
                        error == null,
                        "attempts",
                        attempts,
                        "ms",
                        System.currentTimeMillis() - start,
                        "error",
                        error);
            }
            List<String> all = catalog.listDatabases();
            List<String> left = new java.util.ArrayList<>();
            for (String db : databases) {
                if (all.contains(db)) {
                    left.add(db);
                }
            }
            Out.line("left", "databases", left);
            if (!left.isEmpty()) {
                throw new IllegalStateException("not dropped: " + left);
            }
        }
    }
}
