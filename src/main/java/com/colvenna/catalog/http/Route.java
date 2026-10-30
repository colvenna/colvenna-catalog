package com.colvenna.catalog.http;

import javax.annotation.Nullable;

/**
 * A resolved endpoint: what it does to which resource, and the handler that does it.
 *
 * <p>{@code action} names the operation in audit records (for example {@code CREATE_TABLE}); routes
 * with {@code audited} set are recorded whatever their outcome.
 */
public record Route(
        String action,
        String resourceType,
        @Nullable String resource,
        boolean audited,
        Handler handler) {

    /** Runs the operation of a route. */
    @FunctionalInterface
    public interface Handler {
        Response handle(Request request) throws Exception;
    }

    public static Route read(String action, String type, @Nullable String resource, Handler h) {
        return new Route(action, type, resource, false, h);
    }

    public static Route write(String action, String type, @Nullable String resource, Handler h) {
        return new Route(action, type, resource, true, h);
    }
}
