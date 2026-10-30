package com.colvenna.catalog.catalog;

import com.colvenna.catalog.http.ApiException;
import java.util.List;
import java.util.function.Function;
import javax.annotation.Nullable;
import org.apache.paimon.rest.RESTUtil;

/**
 * Name patterns and page tokens of the Paimon REST list endpoints.
 *
 * <p>Patterns are SQL LIKE prefixes ({@code abc%}) where {@code \} escapes the next character. A
 * page holds at most {@code maxResults} entries in name order; the next-page token is the last name
 * of a full page, matching the upstream test server.
 */
public final class Paging {

    /** One page and the token for the next one, or {@code null} when this page is not full. */
    public record Page<T>(List<T> elements, @Nullable String nextPageToken) {}

    private final int defaultPageSize;
    private final int maxPageSize;

    public Paging(int defaultPageSize, int maxPageSize) {
        this.defaultPageSize = defaultPageSize;
        this.maxPageSize = maxPageSize;
    }

    public int pageSize(@Nullable String maxResults) {
        if (maxResults == null || maxResults.isEmpty()) {
            return defaultPageSize;
        }
        int requested;
        try {
            requested = Integer.parseInt(maxResults);
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(
                    "Invalid input for queryParameter maxResults: " + maxResults);
        }
        if (requested <= 0) {
            return defaultPageSize;
        }
        return Math.min(requested, maxPageSize);
    }

    /**
     * Page token of catalog-wide listings: {@code database.name}, which is what the Paimon client
     * and its contract tests use. Database names never contain {@code .}, so the token splits at
     * its first dot without ambiguity.
     */
    public static String globalKey(String database, String name) {
        return database + "." + name;
    }

    /** Splits a {@link #globalKey} token into database and name. */
    public static String[] splitGlobalKey(String token) {
        int dot = token.indexOf('.');
        if (dot < 0) {
            throw ApiException.badRequest("Malformed page token " + token);
        }
        return new String[] {token.substring(0, dot), token.substring(dot + 1)};
    }

    public static <T> Page<T> page(List<T> rows, int pageSize, Function<T, String> key) {
        String next = rows.size() >= pageSize ? key.apply(rows.get(rows.size() - 1)) : null;
        return new Page<>(rows, next);
    }

    /**
     * Converts a REST name pattern into a PostgreSQL LIKE pattern using {@code \} as the escape
     * character, or returns {@code null} for "no filter".
     */
    @Nullable
    public static String likePattern(@Nullable String pattern) {
        if (pattern == null || pattern.isEmpty()) {
            return null;
        }
        try {
            RESTUtil.validatePrefixSqlPattern(pattern);
        } catch (IllegalArgumentException e) {
            throw ApiException.badRequest(e.getMessage());
        }
        StringBuilder like = new StringBuilder();
        boolean escaped = false;
        for (int i = 0; i < pattern.length(); i++) {
            char c = pattern.charAt(i);
            if (escaped) {
                appendLiteral(like, c);
                escaped = false;
            } else if (c == '\\') {
                escaped = true;
            } else if (c == '%') {
                like.append('%');
            } else {
                appendLiteral(like, c);
            }
        }
        return like.toString();
    }

    /** Matches {@code name} against a REST pattern in memory, for listings not backed by SQL. */
    public static boolean matches(String name, @Nullable String pattern) {
        String like = likePattern(pattern);
        if (like == null) {
            return true;
        }
        StringBuilder regex = new StringBuilder("^");
        for (int i = 0; i < like.length(); i++) {
            char c = like.charAt(i);
            if (c == '\\') {
                regex.append(java.util.regex.Pattern.quote(String.valueOf(like.charAt(++i))));
            } else if (c == '%') {
                regex.append(".*");
            } else {
                regex.append(java.util.regex.Pattern.quote(String.valueOf(c)));
            }
        }
        return name.matches(regex.append('$').toString());
    }

    private static void appendLiteral(StringBuilder like, char c) {
        if (c == '%' || c == '_' || c == '\\') {
            like.append('\\');
        }
        like.append(c);
    }
}
