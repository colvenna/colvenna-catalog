package com.colvenna.catalog.uat.flink;

import org.apache.flink.table.functions.ScalarFunction;

/** The scalar function the catalog flow registers in the catalog: {@code twice(x) = 2 * x}. */
public final class Twice extends ScalarFunction {

    public Long eval(Long value) {
        return value == null ? null : 2 * value;
    }
}
