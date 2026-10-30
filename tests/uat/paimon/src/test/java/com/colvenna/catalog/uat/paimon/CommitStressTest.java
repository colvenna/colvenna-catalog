package com.colvenna.catalog.uat.paimon;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.paimon.rest.exceptions.ForbiddenException;
import org.apache.paimon.rest.exceptions.RESTException;
import org.junit.jupiter.api.Test;

class CommitStressTest {

    private static Map<String, CommitStress.Commit> commits(CommitStress.Commit... cs) {
        Map<String, CommitStress.Commit> m = new HashMap<>();
        for (CommitStress.Commit c : cs) {
            m.put(CommitStress.key(c.writer, c.number), c);
        }
        return m;
    }

    @Test
    void everyCommitPresentOnceWithAllItsRowsPasses() {
        Map<String, Integer> present = Map.of("1/1", 500, "1/2", 500);
        List<String> wrong =
                CommitStress.check(
                        commits(
                                new CommitStress.Commit(1, 1, CommitStress.Outcome.ACKED, 1, null),
                                new CommitStress.Commit(
                                        1,
                                        2,
                                        CommitStress.Outcome.FAILED_THEN_FOUND_COMMITTED,
                                        2,
                                        "x")),
                        present,
                        500);
        assertTrue(wrong.isEmpty(), wrong.toString());
    }

    @Test
    void aDuplicatedOrMissingCommitIsNamed() {
        Map<String, Integer> present = Map.of("1/1", 1000, "1/3", 500);
        List<String> wrong =
                CommitStress.check(
                        commits(
                                new CommitStress.Commit(1, 1, CommitStress.Outcome.ACKED, 1, null),
                                new CommitStress.Commit(1, 2, CommitStress.Outcome.ACKED, 1, null)),
                        present,
                        500);
        assertEquals(
                List.of(
                        "1/1 ACKED has 1000",
                        "1/1 has 1000 rows, expected 500",
                        "1/2 ACKED has 0",
                        "1/3 present but never written"),
                wrong);
    }

    @Test
    void anUnresolvedCommitMayBeAbsent() {
        List<String> wrong =
                CommitStress.check(
                        commits(
                                new CommitStress.Commit(
                                        2, 7, CommitStress.Outcome.UNRESOLVED, 30, "e")),
                        Map.of(),
                        500);
        assertTrue(wrong.isEmpty());
    }

    @Test
    void jsonOfTheResultLines() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("a", "x\"y\n");
        m.put("b", java.util.Arrays.asList(1, true, null, Double.NaN));
        assertEquals("{\"a\":\"x\\\"y\\n\",\"b\":[1,true,null,null]}", Json.write(m));
    }

    @Test
    void outagesAreTheCatalogUnreachableOrAnErrorAnswerNotItsRefusal() {
        assertTrue(
                CommitStress.catalogOutage(
                        new RuntimeException(
                                new RESTException("Error occurred while processing GET request"))));
        assertTrue(
                CommitStress.catalogOutage(
                        new RESTException("Unable to process: Internal server error")));
        assertFalse(CommitStress.catalogOutage(new ForbiddenException("not allowed")));
        assertFalse(CommitStress.catalogOutage(new IllegalStateException("partial")));
    }
}
