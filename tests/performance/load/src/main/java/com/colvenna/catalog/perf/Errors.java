package com.colvenna.catalog.perf;

import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.paimon.rest.exceptions.AlreadyExistsException;
import org.apache.paimon.rest.exceptions.BadRequestException;
import org.apache.paimon.rest.exceptions.ForbiddenException;
import org.apache.paimon.rest.exceptions.NoSuchResourceException;
import org.apache.paimon.rest.exceptions.NotAuthorizedException;
import org.apache.paimon.rest.exceptions.NotImplementedException;
import org.apache.paimon.rest.exceptions.RESTException;
import org.apache.paimon.rest.exceptions.ServiceFailureException;
import org.apache.paimon.rest.exceptions.ServiceUnavailableException;

/**
 * What an operation that failed to its caller (after the client's own retries) answered, as the
 * Paimon client tells it: the HTTP status its exception stands for ({@code DefaultErrorHandler} in
 * {@code paimon-api} 2.0.0), {@code io} for a request that got no answer (a timeout, a refused
 * connection), or {@code other}.
 */
public final class Errors {

    private static final Pattern CODE = Pattern.compile("\\b(?:code|status)[\"=: ]+(\\d{3})\\b");

    private Errors() {}

    public static String status(Throwable e) {
        for (Throwable t = e; t != null; t = t.getCause() == t ? null : t.getCause()) {
            if (t instanceof BadRequestException) {
                return "400";
            } else if (t instanceof NotAuthorizedException) {
                return "401";
            } else if (t instanceof ForbiddenException) {
                return "403";
            } else if (t instanceof NoSuchResourceException) {
                return "404";
            } else if (t instanceof AlreadyExistsException) {
                return "409";
            } else if (t instanceof ServiceFailureException) {
                return "500";
            } else if (t instanceof NotImplementedException) {
                return "501";
            } else if (t instanceof ServiceUnavailableException) {
                return "503";
            } else if (t instanceof RESTException) {
                String message = String.valueOf(t.getMessage());
                Matcher m = CODE.matcher(message);
                if (m.find()) {
                    return m.group(1);
                }
                if (message.startsWith("Error occurred while processing")) {
                    return "io";
                }
                return "rest";
            }
        }
        return "other";
    }

    /** The innermost cause's class and message, cut short; never a token (none is in a message). */
    public static String describe(Throwable e) {
        Throwable root = e;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        String text = root.getClass().getSimpleName() + ": " + root.getMessage();
        if (root != e) {
            text = e.getClass().getSimpleName() + " / " + text;
        }
        text = text.replace('\n', ' ');
        return text.length() > 300 ? text.substring(0, 300) : text;
    }
}
