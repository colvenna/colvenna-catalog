package com.colvenna.catalog.metrics;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.LongAdder;

/**
 * colvenna-catalog's own measures (HLD 11.2), in the Prometheus text format: requests by action and
 * status with their latency, and commits by table and outcome, a commit conflict being a writer's
 * base snapshot no longer the latest, the purges of dropped tables' files by outcome (#69), and the
 * purges of the files rollbacks and tag and branch deletions leave (#93). Nothing in them is data:
 * actions, statuses and table names.
 */
public final class CatalogMetrics {
    /** Latency buckets, seconds. */
    static final List<Double> BUCKETS =
            List.of(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0);

    private final Map<String, LongAdder> requests = new ConcurrentHashMap<>();
    private final Map<String, Histogram> latency = new ConcurrentHashMap<>();
    private final Map<String, LongAdder> commits = new ConcurrentHashMap<>();
    private final Map<String, LongAdder> purges = new ConcurrentHashMap<>();
    private final AtomicLong purgesPending = new AtomicLong(-1);
    private final AtomicLong oldestPurgeSeconds = new AtomicLong(-1);
    private final Map<String, LongAdder> filePurges = new ConcurrentHashMap<>();
    private final LongAdder filesPurged = new LongAdder();
    private final AtomicLong filePurgesPending = new AtomicLong(-1);
    private final AtomicLong oldestFilePurgeSeconds = new AtomicLong(-1);

    private static final class Histogram {
        final LongAdder[] counts = new LongAdder[BUCKETS.size()];
        final LongAdder count = new LongAdder();
        final LongAdder nanos = new LongAdder();

        Histogram() {
            for (int i = 0; i < counts.length; i++) {
                counts[i] = new LongAdder();
            }
        }

        void observe(long elapsedNanos) {
            double seconds = elapsedNanos / 1e9;
            for (int i = 0; i < counts.length; i++) {
                if (seconds <= BUCKETS.get(i)) {
                    counts[i].increment();
                }
            }
            count.increment();
            nanos.add(elapsedNanos);
        }
    }

    /** One request: the route's action ("-" without one), its status, how long it took. */
    public void request(String action, int status, long elapsedNanos) {
        requests.computeIfAbsent(action + "\u0000" + status, k -> new LongAdder()).increment();
        latency.computeIfAbsent(action, k -> new Histogram()).observe(elapsedNanos);
    }

    /**
     * One commit of {@code table}: committed, conflict (a stale base), fenced (a snapshot no commit
     * of the catalog's made), or error.
     */
    public void commit(String table, String outcome) {
        commits.computeIfAbsent(table + "\u0000" + outcome, k -> new LongAdder()).increment();
    }

    /**
     * One purge of a dropped table's files: purged, failed (kept for the next sweep), or skipped (a
     * table made at the root since owns the files).
     */
    public void purge(String outcome) {
        purges.computeIfAbsent(outcome, k -> new LongAdder()).increment();
    }

    /** What the last sweep left: the records of dropped tables' files, and the oldest's age. */
    public void purgesLeft(long pending, long oldestSeconds) {
        purgesPending.set(pending);
        oldestPurgeSeconds.set(oldestSeconds);
    }

    /**
     * One purge of the files a rollback or a tag's or branch's deletion left (#93): purged, failed
     * (kept for the next sweep), or skipped (the table is dropped); and how many files it deleted.
     */
    public void filePurge(String outcome, long deleted) {
        filePurges.computeIfAbsent(outcome, k -> new LongAdder()).increment();
        filesPurged.add(deleted);
    }

    /** What the last sweep left of those records, and the oldest's age. */
    public void filePurgesLeft(long pending, long oldestSeconds) {
        filePurgesPending.set(pending);
        oldestFilePurgeSeconds.set(oldestSeconds);
    }

    public String render() {
        StringBuilder out = new StringBuilder();
        out.append("# HELP colvenna_catalog_requests_total requests by route action and status\n");
        out.append("# TYPE colvenna_catalog_requests_total counter\n");
        for (var e : new TreeMap<>(requests).entrySet()) {
            String[] k = e.getKey().split("\u0000", 2);
            out.append("colvenna_catalog_requests_total{action=\"")
                    .append(escape(k[0]))
                    .append("\",status=\"")
                    .append(k[1])
                    .append("\"} ")
                    .append(e.getValue().sum())
                    .append('\n');
        }
        out.append("# HELP colvenna_catalog_request_seconds request latency by route action\n");
        out.append("# TYPE colvenna_catalog_request_seconds histogram\n");
        for (var e : new TreeMap<>(latency).entrySet()) {
            String action = escape(e.getKey());
            Histogram h = e.getValue();
            for (int i = 0; i < BUCKETS.size(); i++) {
                out.append("colvenna_catalog_request_seconds_bucket{action=\"")
                        .append(action)
                        .append("\",le=\"")
                        .append(BUCKETS.get(i))
                        .append("\"} ")
                        .append(h.counts[i].sum())
                        .append('\n');
            }
            out.append("colvenna_catalog_request_seconds_bucket{action=\"")
                    .append(action)
                    .append("\",le=\"+Inf\"} ")
                    .append(h.count.sum())
                    .append('\n');
            out.append("colvenna_catalog_request_seconds_sum{action=\"")
                    .append(action)
                    .append("\"} ")
                    .append(h.nanos.sum() / 1e9)
                    .append('\n');
            out.append("colvenna_catalog_request_seconds_count{action=\"")
                    .append(action)
                    .append("\"} ")
                    .append(h.count.sum())
                    .append('\n');
        }
        out.append("# HELP colvenna_catalog_commits_total commits by table and outcome\n");
        out.append("# TYPE colvenna_catalog_commits_total counter\n");
        for (var e : new TreeMap<>(commits).entrySet()) {
            String[] k = e.getKey().split("\u0000", 2);
            out.append("colvenna_catalog_commits_total{table=\"")
                    .append(escape(k[0]))
                    .append("\",outcome=\"")
                    .append(k[1])
                    .append("\"} ")
                    .append(e.getValue().sum())
                    .append('\n');
        }
        out.append(
                "# HELP colvenna_catalog_table_purges_total purges of dropped tables' files by"
                        + " outcome\n");
        out.append("# TYPE colvenna_catalog_table_purges_total counter\n");
        for (var e : new TreeMap<>(purges).entrySet()) {
            out.append("colvenna_catalog_table_purges_total{outcome=\"")
                    .append(escape(e.getKey()))
                    .append("\"} ")
                    .append(e.getValue().sum())
                    .append('\n');
        }
        if (purgesPending.get() >= 0) {
            out.append(
                    "# HELP colvenna_catalog_table_purges_pending dropped tables' files still to be"
                            + " deleted, at the last sweep\n");
            out.append("# TYPE colvenna_catalog_table_purges_pending gauge\n");
            out.append("colvenna_catalog_table_purges_pending ")
                    .append(purgesPending.get())
                    .append('\n');
            out.append(
                    "# HELP colvenna_catalog_table_purge_oldest_seconds age of the oldest of them,"
                            + " at the last sweep\n");
            out.append("# TYPE colvenna_catalog_table_purge_oldest_seconds gauge\n");
            out.append("colvenna_catalog_table_purge_oldest_seconds ")
                    .append(oldestPurgeSeconds.get())
                    .append('\n');
        }
        out.append(
                "# HELP colvenna_catalog_file_purges_total purges of the files rollbacks, tag"
                        + " deletions and branch deletions left, by outcome\n");
        out.append("# TYPE colvenna_catalog_file_purges_total counter\n");
        for (var e : new TreeMap<>(filePurges).entrySet()) {
            out.append("colvenna_catalog_file_purges_total{outcome=\"")
                    .append(escape(e.getKey()))
                    .append("\"} ")
                    .append(e.getValue().sum())
                    .append('\n');
        }
        out.append("# HELP colvenna_catalog_purged_files_total files those purges deleted\n");
        out.append("# TYPE colvenna_catalog_purged_files_total counter\n");
        out.append("colvenna_catalog_purged_files_total ").append(filesPurged.sum()).append('\n');
        if (filePurgesPending.get() >= 0) {
            out.append(
                    "# HELP colvenna_catalog_file_purges_pending their records still there, at"
                            + " the last sweep\n");
            out.append("# TYPE colvenna_catalog_file_purges_pending gauge\n");
            out.append("colvenna_catalog_file_purges_pending ")
                    .append(filePurgesPending.get())
                    .append('\n');
            out.append(
                    "# HELP colvenna_catalog_file_purge_oldest_seconds age of the oldest of them,"
                            + " at the last sweep\n");
            out.append("# TYPE colvenna_catalog_file_purge_oldest_seconds gauge\n");
            out.append("colvenna_catalog_file_purge_oldest_seconds ")
                    .append(oldestFilePurgeSeconds.get())
                    .append('\n');
        }
        return out.toString();
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n");
    }
}
