package com.colvenna.catalog.http;

import java.nio.charset.StandardCharsets;
import org.apache.paimon.rest.RESTApi;

/**
 * The size that counts against a response limit ({@code COLVENNA_MAX_RESPONSE_BYTES}): the body's
 * UTF-8 length once escaped as a JSON string, which is how the Lambda Web Adapter carries it in the
 * Lambda response. Each {@code "} and {@code \} takes one more byte there, each control character
 * one ({@code \n}) or five ({@code \}{@code u00XX}) more.
 */
final class ResponseSize {

    private ResponseSize() {}

    static long escaped(byte[] utf8) {
        long size = utf8.length;
        for (byte b : utf8) {
            if (b == '"' || b == '\\' || b == '\n' || b == '\r' || b == '\t' || b == '\b'
                    || b == '\f') {
                size += 1;
            } else if (b >= 0 && b < 0x20) {
                size += 5;
            }
        }
        return size;
    }

    /** The escaped size of {@code value} serialized as the server serializes response bodies. */
    static long escaped(Object value) throws Exception {
        return escaped(RESTApi.toJson(value).getBytes(StandardCharsets.UTF_8));
    }
}
