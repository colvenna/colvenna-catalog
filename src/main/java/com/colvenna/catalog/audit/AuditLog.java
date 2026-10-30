package com.colvenna.catalog.audit;

import com.colvenna.catalog.db.Db;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.paimon.rest.RESTApi;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Durable record of who did what to which catalog resource, and how it ended.
 *
 * <p>Events are written in their own transaction after the outcome is known, so a request that
 * failed is never recorded as succeeded. A failure to write an event is logged; the request keeps
 * its own outcome.
 */
public final class AuditLog {

    private static final Logger LOG = LoggerFactory.getLogger(AuditLog.class);

    /** Final outcome of an audited request. */
    public enum Outcome {
        SUCCEEDED,
        FAILED,
        DENIED
    }

    /** One audited action. */
    public record Event(
            String requestId,
            @Nullable String principal,
            String action,
            String resourceType,
            @Nullable String resource,
            Outcome outcome,
            int httpStatus,
            Map<String, Object> detail) {}

    /** Filters of an audit query; {@code null} fields match everything. */
    public record Query(
            @Nullable String principal,
            @Nullable String action,
            @Nullable String resourceType,
            @Nullable String resource,
            @Nullable Outcome outcome,
            @Nullable Instant since,
            @Nullable Instant until,
            int limit) {}

    /** Most events one query returns. */
    public static final int MAX_QUERY_LIMIT = 1000;

    private final Db db;

    public AuditLog(Db db) {
        this.db = db;
    }

    /** Matching events, newest first. */
    public List<Map<String, Object>> query(Query q) throws Exception {
        StringBuilder sql =
                new StringBuilder(
                        "SELECT id, event_time, request_id, principal, action, resource_type,"
                                + " resource, outcome, http_status, detail::text FROM audit_events"
                                + " WHERE true");
        List<Object> args = new ArrayList<>();
        filter(sql, args, "principal = ?", q.principal());
        filter(sql, args, "action = ?", q.action());
        filter(sql, args, "resource_type = ?", q.resourceType());
        filter(sql, args, "resource = ?", q.resource());
        filter(sql, args, "outcome = ?", q.outcome() == null ? null : q.outcome().name());
        filter(sql, args, "event_time >= ?", q.since() == null ? null : Timestamp.from(q.since()));
        filter(sql, args, "event_time < ?", q.until() == null ? null : Timestamp.from(q.until()));
        sql.append(" ORDER BY event_time DESC, id DESC LIMIT ?");
        args.add(Math.min(Math.max(q.limit(), 1), MAX_QUERY_LIMIT));
        return db.inTransaction(
                Db.Pool.AUDIT,
                c -> {
                    try (PreparedStatement select = c.prepareStatement(sql.toString())) {
                        for (int i = 0; i < args.size(); i++) {
                            select.setObject(i + 1, args.get(i));
                        }
                        List<Map<String, Object>> events = new ArrayList<>();
                        try (ResultSet rs = select.executeQuery()) {
                            while (rs.next()) {
                                Map<String, Object> e = new LinkedHashMap<>();
                                e.put("id", rs.getLong(1));
                                e.put("time", rs.getTimestamp(2).toInstant().toString());
                                e.put("requestId", rs.getString(3));
                                e.put("principal", rs.getString(4));
                                e.put("action", rs.getString(5));
                                e.put("resourceType", rs.getString(6));
                                e.put("resource", rs.getString(7));
                                e.put("outcome", rs.getString(8));
                                e.put("httpStatus", rs.getInt(9));
                                e.put("detail", RESTApi.OBJECT_MAPPER.readTree(rs.getString(10)));
                                events.add(e);
                            }
                        }
                        return events;
                    }
                });
    }

    private static void filter(
            StringBuilder sql, List<Object> args, String clause, @Nullable Object value) {
        if (value != null) {
            sql.append(" AND ").append(clause);
            args.add(value);
        }
    }

    public void record(Event event) {
        try {
            db.inTransaction(
                    Db.Pool.AUDIT,
                    connection -> {
                        try (PreparedStatement insert =
                                connection.prepareStatement(
                                        "INSERT INTO audit_events (request_id, principal, action,"
                                                + " resource_type, resource, outcome, http_status,"
                                                + " detail) VALUES (?, ?, ?, ?, ?, ?, ?,"
                                                + " ?::jsonb)")) {
                            insert.setString(1, event.requestId());
                            insert.setString(2, event.principal());
                            insert.setString(3, event.action());
                            insert.setString(4, event.resourceType());
                            insert.setString(5, event.resource());
                            insert.setString(6, event.outcome().name());
                            insert.setInt(7, event.httpStatus());
                            insert.setString(8, RESTApi.toJson(event.detail()));
                            insert.executeUpdate();
                        }
                        return null;
                    });
        } catch (Exception e) {
            LOG.error("Failed to write audit event {}", event, e);
        }
    }
}
