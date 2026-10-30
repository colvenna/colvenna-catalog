package com.colvenna.catalog.http;

import com.colvenna.catalog.auth.Principal;
import com.colvenna.catalog.catalog.RequestContext;
import java.util.List;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.paimon.rest.RESTApi;

/** A parsed HTTP request: decoded path segments, query parameters and body. */
public record Request(
        String method,
        List<String> segments,
        Map<String, String> query,
        String body,
        @Nullable Principal principal,
        String requestId) {

    public String segment(int index) {
        return segments.get(index);
    }

    public int size() {
        return segments.size();
    }

    @Nullable
    public String param(String name) {
        return query.get(name);
    }

    public RequestContext context() {
        return new RequestContext(principal, requestId);
    }

    public <T> T body(Class<T> type) {
        if (body == null || body.isEmpty()) {
            throw ApiException.badRequest("Request body is required.");
        }
        try {
            return RESTApi.fromJson(body, type);
        } catch (Exception e) {
            throw ApiException.badRequest("Malformed request body: " + e.getMessage());
        }
    }

    public boolean is(String expectedMethod) {
        return method.equals(expectedMethod);
    }

    /** The same request with its path re-rooted, e.g. below {@code /v1}. */
    public Request withSegments(List<String> newSegments) {
        return new Request(method, newSegments, query, body, principal, requestId);
    }

    /** The same request after authentication, with its body read. */
    public Request authenticated(Principal authenticated, String readBody) {
        return new Request(method, segments, query, readBody, authenticated, requestId);
    }
}
