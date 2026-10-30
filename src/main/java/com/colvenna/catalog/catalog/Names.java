package com.colvenna.catalog.catalog;

import com.colvenna.catalog.http.ApiException;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Identifier;

/**
 * Names that end up in file paths: databases, tables, views, functions, branches, tags and
 * consumers. None may navigate the file system, so separators, {@code .}, {@code ..} and control
 * characters are refused wherever a name enters the server, from the URL or from a request body. So
 * are {@code ? # %}, which a URI reads as query, fragment or escape and would cut or alter the
 * path, and {@code *}, which with {@code ?} is a wildcard in the IAM session policies that confine
 * table credentials to one table root.
 */
public final class Names {

    private static final String FORBIDDEN = "/\\?#%*";

    private Names() {}

    public static void validate(String kind, @Nullable String name) {
        if (name == null || name.isEmpty()) {
            throw ApiException.badRequest("The " + kind + " name must not be empty.");
        }
        if (name.equals(".") || name.equals("..")) {
            throw ApiException.badRequest("The " + kind + " name must not be " + name + ".");
        }
        for (int i = 0; i < name.length(); i++) {
            char ch = name.charAt(i);
            if (FORBIDDEN.indexOf(ch) >= 0 || Character.isISOControl(ch)) {
                throw ApiException.badRequest(
                        "The " + kind + " name " + name + " contains a forbidden character.");
            }
        }
    }

    /** Database names additionally exclude {@code .}, the separator of global page tokens. */
    public static void validateDatabase(String name) {
        validate("database", name);
        if (name.contains(".")) {
            throw ApiException.badRequest(
                    "Database names must not contain '.', which separates database and object in"
                            + " identifiers.");
        }
    }

    /** Validates every name an identifier carries: database, object, branch. */
    public static void validate(Identifier identifier) {
        validate("database", identifier.getDatabaseName());
        validate("table", identifier.getTableName());
        if (identifier.getBranchName() != null) {
            validate("branch", identifier.getBranchName());
        }
    }
}
