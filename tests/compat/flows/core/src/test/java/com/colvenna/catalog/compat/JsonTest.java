package com.colvenna.catalog.compat;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class JsonTest {

    @Test
    void writesWhatTheRunnerParses() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", "pk");
        body.put("detail", "a \"quoted\"\tline\nand\\more\u0001");
        body.put("n", 3);
        body.put("ok", true);
        body.put("none", null);
        body.put("list", List.of("a", 1));
        assertEquals(
                "{\"table\":\"pk\",\"detail\":\"a \\\"quoted\\\"\\tline\\nand\\\\more\\u0001\","
                        + "\"n\":3,\"ok\":true,\"none\":null,\"list\":[\"a\",1]}",
                Json.write(body));
    }

    @Test
    void describesAnExceptionOnOneLine() {
        Exception e = new IllegalStateException("outer\nsecond line", new RuntimeException("root"));
        assertEquals(
                "IllegalStateException: outer (cause: RuntimeException: root)", Report.describe(e));
    }
}
