package com.colvenna.catalog.http;

import com.colvenna.catalog.audit.AuditLog;
import java.util.Map;
import javax.annotation.Nullable;

/**
 * A handler result: status and an optional body serialized with the Paimon REST mapper.
 *
 * <p>{@code auditOutcome} overrides the outcome derived from the status, for operations that answer
 * 200 without doing what was asked, such as a commit that lost the compare-and-set.
 */
public record Response(
        int status,
        @Nullable Object body,
        @Nullable AuditLog.Outcome auditOutcome,
        Map<String, Object> auditDetail,
        @Nullable String auditResource) {

    public Response(
            int status,
            @Nullable Object body,
            @Nullable AuditLog.Outcome auditOutcome,
            Map<String, Object> auditDetail) {
        this(status, body, auditOutcome, auditDetail, null);
    }

    public static Response ok(@Nullable Object body) {
        return new Response(200, body, null, Map.of());
    }

    /** The same response, audited against {@code resource} when the route itself names none. */
    public Response withAuditResource(@Nullable String resource) {
        return new Response(status, body, auditOutcome, auditDetail, resource);
    }

    public static Response empty() {
        return ok(null);
    }

    /** The same response, audited with {@code outcome} and {@code detail}. */
    public Response audited(AuditLog.Outcome outcome, Map<String, Object> detail) {
        return new Response(status, body, outcome, detail, auditResource);
    }
}
