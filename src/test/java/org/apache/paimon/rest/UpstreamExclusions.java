package org.apache.paimon.rest;

import java.util.Map;
import org.junit.jupiter.api.extension.ConditionEvaluationResult;
import org.junit.jupiter.api.extension.ExecutionCondition;
import org.junit.jupiter.api.extension.ExtensionContext;

/**
 * Upstream contract tests that do not apply to colvenna-catalog, each with the reason. A test is
 * only listed here when it checks behavior outside colvenna-catalog's contract or a property of the
 * upstream mock server itself; a colvenna-catalog defect is fixed, never excluded.
 */
final class UpstreamExclusions implements ExecutionCondition {

    private static final String ROW_COLUMN_POLICY =
            "Row filters and column masks are outside colvenna-catalog (HLD 6.2: deferred until a"
                    + " released Management API).";

    static final Map<String, String> EXCLUDED =
            Map.ofEntries(
                    Map.entry(
                            "testAllTablesAndAllPartitionsTable",
                            "Asserts the mock server's literal owner \"owner\"; colvenna-catalog"
                                    + " records the creating principal."),
                    Map.entry(
                            "testReadPartitionsTable",
                            "Asserts the mock server's literal createdBy \"created\";"
                                    + " colvenna-catalog records the creating principal."),
                    Map.entry(
                            "testRollbackSchemaFromTable",
                            "Asserts that GetTable still returns the rolled-back schema id 1,"
                                    + " which is the mock server's stale metadata; colvenna-catalog"
                                    + " returns the actual latest schema 0."),
                    Map.entry(
                            "testDropTableWithExternalPaths",
                            "Expects the server to delete client-chosen external data paths on"
                                    + " drop; colvenna-catalog never deletes client-chosen locations"
                                    + " with its own credentials (LLD: client-chosen locations)."),
                    Map.entry("testColumnMaskingAndRowFilter", ROW_COLUMN_POLICY),
                    Map.entry("testColumnMaskingApplyOnRead", ROW_COLUMN_POLICY),
                    Map.entry("testRowFilter", ROW_COLUMN_POLICY),
                    Map.entry("testRowFilterReadBuilderLimitAfterAuth", ROW_COLUMN_POLICY),
                    Map.entry("testRowFilterReadLimitSkippedWithTopN", ROW_COLUMN_POLICY),
                    Map.entry(
                            "testRowFilterWithLimitReadsPastUnauthorizedFiles", ROW_COLUMN_POLICY),
                    Map.entry("testRowFilterWithTopNKeepsAuthorizedSplits", ROW_COLUMN_POLICY));

    @Override
    public ConditionEvaluationResult evaluateExecutionCondition(ExtensionContext context) {
        return context.getTestMethod()
                .map(m -> EXCLUDED.get(m.getName()))
                .map(ConditionEvaluationResult::disabled)
                .orElse(ConditionEvaluationResult.enabled("in colvenna-catalog's contract"));
    }
}
