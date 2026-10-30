package com.colvenna.catalog.http;

import org.apache.paimon.rest.responses.ErrorResponse;

/** Resource types named in audit events and error responses. */
public final class Resources {

    public static final String CATALOG = "CATALOG";
    public static final String DATABASE = ErrorResponse.RESOURCE_TYPE_DATABASE;
    public static final String TABLE = ErrorResponse.RESOURCE_TYPE_TABLE;
    public static final String VIEW = ErrorResponse.RESOURCE_TYPE_VIEW;
    public static final String FUNCTION = ErrorResponse.RESOURCE_TYPE_FUNCTION;
    public static final String PRINCIPAL = "PRINCIPAL";
    public static final String TOKEN = "TOKEN";
    public static final String GRANT = "GRANT";

    private Resources() {}
}
