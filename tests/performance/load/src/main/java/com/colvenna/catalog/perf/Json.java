package com.colvenna.catalog.perf;

import java.util.Iterator;
import java.util.Map;

/**
 * Just enough JSON for the load tool's records and facts: strings, numbers, booleans, lists, maps.
 */
public final class Json {

    private Json() {}

    public static String write(Object value) {
        StringBuilder out = new StringBuilder();
        append(out, value);
        return out.toString();
    }

    private static void append(StringBuilder out, Object value) {
        if (value == null) {
            out.append("null");
        } else if (value instanceof String) {
            string(out, (String) value);
        } else if (value instanceof Double || value instanceof Float) {
            double d = ((Number) value).doubleValue();
            out.append(Double.isFinite(d) ? value.toString() : "null");
        } else if (value instanceof Number || value instanceof Boolean) {
            out.append(value);
        } else if (value instanceof Map) {
            out.append('{');
            Iterator<? extends Map.Entry<?, ?>> entries = ((Map<?, ?>) value).entrySet().iterator();
            while (entries.hasNext()) {
                Map.Entry<?, ?> entry = entries.next();
                string(out, String.valueOf(entry.getKey()));
                out.append(':');
                append(out, entry.getValue());
                if (entries.hasNext()) {
                    out.append(',');
                }
            }
            out.append('}');
        } else if (value instanceof Iterable) {
            out.append('[');
            Iterator<?> items = ((Iterable<?>) value).iterator();
            while (items.hasNext()) {
                append(out, items.next());
                if (items.hasNext()) {
                    out.append(',');
                }
            }
            out.append(']');
        } else {
            string(out, String.valueOf(value));
        }
    }

    private static void string(StringBuilder out, String s) {
        out.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    out.append("\\\"");
                    break;
                case '\\':
                    out.append("\\\\");
                    break;
                case '\n':
                    out.append("\\n");
                    break;
                case '\r':
                    out.append("\\r");
                    break;
                case '\t':
                    out.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        out.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        out.append('"');
    }
}
