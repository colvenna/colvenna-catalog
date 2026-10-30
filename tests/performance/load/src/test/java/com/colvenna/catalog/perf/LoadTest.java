package com.colvenna.catalog.perf;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Properties;
import org.apache.paimon.rest.exceptions.RESTException;
import org.apache.paimon.rest.exceptions.ServiceUnavailableException;
import org.junit.jupiter.api.Test;

class LoadTest {

    @Test
    void stepsParse() {
        List<OpenLoop.Step> steps = OpenLoop.steps("50:300,100:300:fast");
        assertEquals(2, steps.size());
        assertEquals(50.0, steps.get(0).rate);
        assertEquals(300, steps.get(0).seconds);
        assertEquals("50", steps.get(0).label);
        assertEquals("fast", steps.get(1).label);
    }

    @Test
    void errorsSayTheirStatus() {
        assertEquals(
                "503", Errors.status(new ServiceUnavailableException("Service unavailable: x")));
        assertEquals(
                "503", Errors.status(new RuntimeException(new ServiceUnavailableException("x"))));
        assertEquals(
                "io",
                Errors.status(new RESTException("Error occurred while processing GET request")));
        assertEquals(
                "rest", Errors.status(new RESTException("Unable to process: gateway timeout")));
        assertEquals("other", Errors.status(new IllegalStateException("x")));
    }

    @Test
    void openLoopRecordsEveryRequestFailedOrNot(
            @org.junit.jupiter.api.io.TempDir java.nio.file.Path dir) throws Exception {
        Records records = Records.open(dir.resolve("loop"));
        long sent =
                OpenLoop.run(
                        records,
                        OpenLoop.steps("200:1:a"),
                        8,
                        System.currentTimeMillis(),
                        10,
                        i -> java.util.Map.of("op", "x"),
                        (i, what) -> {
                            if (i % 2 == 0) {
                                throw new ServiceUnavailableException("Service unavailable: busy");
                            }
                            return null;
                        });
        records.close();
        List<String> lines = java.nio.file.Files.readAllLines(dir.resolve("loop.jsonl"));
        assertEquals(200, sent);
        assertEquals(200, lines.size());
        assertEquals(100, lines.stream().filter(l -> l.contains("\"status\":\"503\"")).count());
    }

    @Test
    void specParts() {
        Properties p = new Properties();
        p.setProperty("writers.key", "w");
        p.setProperty("reads.key", "r");
        p.setProperty("list", "a, b,,c");
        Spec spec = new Spec(p);
        assertEquals("w", spec.part("writers").str("key"));
        assertEquals(List.of("a", "b", "c"), spec.list("list"));
        assertEquals(7, spec.integer("missing", 7));
    }
}
