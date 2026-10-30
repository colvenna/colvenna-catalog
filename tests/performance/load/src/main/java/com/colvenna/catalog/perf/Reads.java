package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SplittableRandom;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;

/**
 * The metadata read mix, open-loop (terms.md P2, and P5's reads): {@code getTable}, the latest
 * snapshot ({@code GET .../snapshot}) and {@code listTables} (a page of {@code page} tables) in the
 * shares {@code mix} gives ({@code getTable:60,snapshot:30,listTables:10}), each of a random table
 * of {@code databases} x {@code tables}, with one reader's token ({@code key}). {@code steps} is
 * {@code rate:seconds[:label],...}, the first preceded by {@code warmup} seconds at its rate. One
 * record per request ({@code <prefix>reads.jsonl}), with its operation, table and step.
 */
public final class Reads {

    private Reads() {}

    static void run(Spec spec, String prefix) throws Exception {
        RESTApi api = Clients.api(spec.str("key"));
        List<String> databases = spec.list("databases");
        int tables = spec.integer("tables");
        int page = spec.integer("page", 100);
        long seed = spec.number("seed");
        List<String> ops = new ArrayList<>();
        List<Integer> weights = new ArrayList<>();
        int total = 0;
        for (String part : spec.list("mix")) {
            String[] p = part.split(":");
            ops.add(p[0]);
            total += Integer.parseInt(p[1]);
            weights.add(total);
        }
        int sum = total;
        List<OpenLoop.Step> steps = new ArrayList<>(OpenLoop.steps(spec.str("steps")));
        long warmup = spec.number("warmup");
        if (warmup > 0) {
            steps.add(0, new OpenLoop.Step(steps.get(0).rate, warmup, "warmup"));
        }
        Records records = Records.open(prefix + "reads");
        long sent =
                OpenLoop.run(
                        records,
                        steps,
                        spec.integer("threads", 256),
                        spec.start(),
                        Long.parseLong(spec.str("drain", "240")),
                        i -> {
                            SplittableRandom r = new SplittableRandom(seed * 1_000_003L + i);
                            int pick = r.nextInt(sum);
                            String op = ops.get(0);
                            for (int k = 0; k < ops.size(); k++) {
                                if (pick < weights.get(k)) {
                                    op = ops.get(k);
                                    break;
                                }
                            }
                            Map<String, Object> m = new LinkedHashMap<>();
                            m.put("op", op);
                            m.put("db", databases.get(r.nextInt(databases.size())));
                            m.put("table", Seed.table(r.nextInt(tables)));
                            return m;
                        },
                        (i, what) -> {
                            String op = (String) what.get("op");
                            Identifier id =
                                    Identifier.create(
                                            (String) what.get("db"), (String) what.get("table"));
                            switch (op) {
                                case "getTable":
                                    api.getTable(id);
                                    break;
                                case "snapshot":
                                    api.loadSnapshot(id);
                                    break;
                                case "listTables":
                                    api.listTablesPaged(
                                            id.getDatabaseName(), page, null, null, null);
                                    break;
                                default:
                                    throw new IllegalArgumentException("unknown operation " + op);
                            }
                            return null;
                        });
        Out.line("read", "sent", sent, "records", records.count());
    }
}
