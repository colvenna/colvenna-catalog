package com.colvenna.catalog.compat;

/** The operations a cell's flow judges itself; the runner adds the endpoint coverage check. */
public enum Op {
    /** Create and drop the database and the table. */
    DDL("ddl"),
    /** Write and commit through the catalog. */
    WRITE("write"),
    /** Read back what was written. */
    READ("read"),
    /**
     * Per-table credentials: the engine's own identity cannot reach the table's files, so its
     * writes and reads used the credentials the catalog vended.
     */
    CREDENTIALS("credentials");

    private final String id;

    Op(String id) {
        this.id = id;
    }

    public String id() {
        return id;
    }
}
