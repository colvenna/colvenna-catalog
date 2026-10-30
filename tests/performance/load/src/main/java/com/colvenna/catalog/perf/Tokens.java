package com.colvenna.catalog.perf;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.SplittableRandom;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;

/**
 * Per-table credential requests ({@code GET .../tables/{table}/token}), open-loop (terms.md P3, and
 * P5's credential requests), by the principals of {@code keys}.
 *
 * <ul>
 *   <li>Misses: each request of a share {@code missShare} takes the next (principal, table) of the
 *       miss pool, {@code keys} x {@code miss.databases} x {@code miss.tables}, shuffled: a pair
 *       never asked before;
 *   <li>Hits: the others take a random pair of {@code keys} x {@code hit.tables} ({@code db.table}
 *       items), each asked once in order first ({@code prime=true}, recorded as step {@code prime})
 *       and then again through {@code warmup} seconds at the rate, so that every replica or
 *       execution environment is likely to hold it.
 * </ul>
 *
 * Whether a request was in fact a miss or a hit is the server's to say (its {@code
 * ISSUE_DATA_TOKEN} audit event, {@code cached}): the harness classifies each by its request id.
 * One record per request ({@code <prefix>tokens.jsonl}), with its principal key, table and intent.
 */
public final class Tokens {

    private Tokens() {}

    static final class Pair {
        final String key;
        final Identifier id;

        Pair(String key, Identifier id) {
            this.key = key;
            this.id = id;
        }
    }

    static void run(Spec spec, String prefix) throws Exception {
        List<String> keys = spec.list("keys");
        Map<String, RESTApi> apis = new HashMap<>();
        for (String k : keys) {
            apis.put(k, Clients.api(k));
        }
        long seed = spec.number("seed");
        double missShare = spec.decimal("missShare");
        List<Pair> hits = new ArrayList<>();
        for (String t : spec.list("hit.tables")) {
            String[] p = t.split("\\.");
            for (String k : keys) {
                hits.add(new Pair(k, Identifier.create(p[0], p[1])));
            }
        }
        List<Pair> misses = new ArrayList<>();
        int missTables = spec.integer("miss.tables", 0);
        for (String db : spec.list("miss.databases")) {
            for (int t = 0; t < missTables; t++) {
                for (String k : keys) {
                    misses.add(new Pair(k, Identifier.create(db, Seed.table(t))));
                }
            }
        }
        Collections.shuffle(misses, new Random(seed));
        Records records = Records.open(prefix + "tokens");
        if (spec.flag("prime", false)) {
            long i = 0;
            for (Pair p : hits) {
                long start = System.currentTimeMillis();
                long n = System.nanoTime();
                String error = null;
                try {
                    apis.get(p.key).loadTableToken(p.id);
                } catch (Exception e) {
                    error = Errors.describe(e);
                }
                records.write(
                        "i",
                        -(++i),
                        "step",
                        "prime",
                        "due",
                        start,
                        "start",
                        start,
                        "end",
                        System.currentTimeMillis(),
                        "lagUs",
                        0,
                        "latUs",
                        (System.nanoTime() - n) / 1000,
                        "ok",
                        error == null,
                        "error",
                        error,
                        "thread",
                        Thread.currentThread().getName(),
                        "key",
                        p.key,
                        "table",
                        p.id.getFullName(),
                        "intent",
                        "hit");
            }
        }
        List<OpenLoop.Step> steps = new ArrayList<>();
        double rate = spec.decimal("rate");
        long warmup = spec.number("warmup");
        if (warmup > 0) {
            steps.add(new OpenLoop.Step(rate, warmup, "warmup"));
        }
        steps.add(new OpenLoop.Step(rate, spec.number("duration"), "window"));
        long expected = 0;
        for (OpenLoop.Step s : steps) {
            expected += Math.round(s.rate * s.seconds);
        }
        // Which requests are misses, and which miss each takes, fixed before the load starts.
        Map<Long, Pair> plan = new HashMap<>();
        Map<Long, String> intent = new HashMap<>();
        SplittableRandom pick = new SplittableRandom(seed);
        int nextMiss = 0;
        for (long i = 0; i < expected; i++) {
            boolean miss = missShare >= 1.0 || (missShare > 0 && pick.nextDouble() < missShare);
            if (miss && nextMiss < misses.size()) {
                plan.put(i, misses.get(nextMiss++));
                intent.put(i, "miss");
            } else if (!hits.isEmpty()) {
                plan.put(i, hits.get(pick.nextInt(hits.size())));
                intent.put(i, "hit");
            } else {
                throw new IllegalArgumentException(
                        "the miss pool holds " + misses.size() + " pairs, fewer than the requests");
            }
        }
        long sent =
                OpenLoop.run(
                        records,
                        steps,
                        spec.integer("threads", 64),
                        spec.start(),
                        Long.parseLong(spec.str("drain", "240")),
                        i -> {
                            Pair p = plan.get(i);
                            Map<String, Object> m = new LinkedHashMap<>();
                            m.put("key", p.key);
                            m.put("table", p.id.getFullName());
                            m.put("intent", intent.get(i));
                            return m;
                        },
                        (i, what) -> {
                            Pair p = plan.get(i);
                            apis.get(p.key).loadTableToken(p.id);
                            return null;
                        });
        Out.line(
                "requested",
                "sent",
                sent,
                "records",
                records.count(),
                "missesPlanned",
                nextMiss,
                "missPool",
                misses.size());
    }
}
