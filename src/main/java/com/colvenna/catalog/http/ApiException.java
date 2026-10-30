package com.colvenna.catalog.http;

import com.colvenna.catalog.auth.Authorizer;
import javax.annotation.Nullable;
import org.apache.paimon.rest.responses.ErrorResponse;

/** A request failure that maps to one Paimon REST {@link ErrorResponse}. */
public final class ApiException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    private final int status;
    @Nullable private final String resourceType;
    @Nullable private final String resourceName;
    private final boolean fenced;
    @Nullable private final transient Authorizer.Decision denial;
    @Nullable private final String auditResource;

    public ApiException(
            int status,
            @Nullable String resourceType,
            @Nullable String resourceName,
            String message) {
        this(status, resourceType, resourceName, message, false, null, null, null);
    }

    private ApiException(
            int status,
            @Nullable String resourceType,
            @Nullable String resourceName,
            String message,
            boolean fenced,
            @Nullable Authorizer.Decision denial,
            @Nullable String auditResource,
            @Nullable Throwable cause) {
        super(message == null ? "Unknown error." : message, cause);
        this.status = status;
        this.resourceType = resourceType;
        this.resourceName = resourceName;
        this.fenced = fenced;
        this.denial = denial;
        this.auditResource = auditResource;
    }

    public ApiException(int status, String message, Throwable cause) {
        this(status, null, null, message, false, null, null, cause);
    }

    public int status() {
        return status;
    }

    /** Whether the table is fenced: a snapshot no commit of the catalog's made was found. */
    public boolean fenced() {
        return fenced;
    }

    @Nullable
    public String resourceName() {
        return resourceName;
    }

    /**
     * The authorization decision that refused the request, if one did: its audit event is {@code
     * DENIED} with the decision's detail, whatever the status says to the caller.
     */
    @Nullable
    public Authorizer.Decision denial() {
        return denial;
    }

    /** The same failure, as the refusal {@code decision} made. */
    public ApiException deniedBy(Authorizer.Decision decision) {
        return copy(decision, auditResource);
    }

    /**
     * The resource the failure's audit event names, when the route's handler knows it better than
     * the error does (a rename's source table); {@code null} for what the error names.
     */
    @Nullable
    public String auditResource() {
        return auditResource;
    }

    /** The same failure, audited under {@code resource}. */
    public ApiException auditedAs(@Nullable String resource) {
        return copy(denial, resource);
    }

    private ApiException copy(@Nullable Authorizer.Decision decision, @Nullable String resource) {
        ApiException copy =
                new ApiException(
                        status,
                        resourceType,
                        resourceName,
                        getMessage(),
                        fenced,
                        decision,
                        resource,
                        getCause());
        copy.setStackTrace(getStackTrace());
        return copy;
    }

    public ErrorResponse toResponse() {
        return new ErrorResponse(resourceType, resourceName, getMessage(), status);
    }

    public static ApiException badRequest(String message) {
        return new ApiException(400, null, null, message);
    }

    public static ApiException unauthorized(String message) {
        return new ApiException(401, null, null, message);
    }

    public static ApiException forbidden(String resourceType, String name, String message) {
        return new ApiException(403, resourceType, name, message);
    }

    /** A 403 that {@code decision} of the authorizer made. */
    public static ApiException refused(
            String resourceType, String name, String message, Authorizer.Decision decision) {
        return new ApiException(403, resourceType, name, message, false, decision, null, null);
    }

    public static ApiException notFound(String resourceType, String name, String message) {
        return new ApiException(404, resourceType, name, message);
    }

    public static ApiException conflict(String resourceType, String name, String message) {
        return new ApiException(409, resourceType, name, message);
    }

    /** A 409 for a table that is fenced and accepts no commits. */
    public static ApiException fenced(String resourceType, String name, String message) {
        return new ApiException(409, resourceType, name, message, true, null, null, null);
    }

    public static ApiException notImplemented(String message) {
        return new ApiException(501, null, null, message);
    }

    public static ApiException unavailable(String message, Throwable cause) {
        return new ApiException(503, message, cause);
    }
}
