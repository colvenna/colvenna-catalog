package com.colvenna.catalog.token;

import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.RequestContext;
import com.colvenna.catalog.http.ApiException;
import java.util.Map;

/** Issues short-lived object-store credentials limited to one table's files. */
public interface DataTokenIssuer {

    /**
     * Properties merged into the client's FileIO options, when they stop working, and what the
     * audit record keeps about them (never the secrets).
     */
    record Token(Map<String, String> properties, long expiresAtMillis, Map<String, Object> detail) {

        /** Never the properties: they hold the secret key and session token. */
        @Override
        public String toString() {
            return "Token[keys="
                    + properties.keySet()
                    + ", expiresAtMillis="
                    + expiresAtMillis
                    + "]";
        }
    }

    Token issue(RequestContext ctx, TableRecord table, SessionPolicy.Access access)
            throws Exception;

    /** Used when the server is not configured to vend credentials. */
    static DataTokenIssuer disabled() {
        return (ctx, table, access) -> {
            throw ApiException.notImplemented(
                    "Data tokens are not enabled on this colvenna-catalog server.");
        };
    }
}
