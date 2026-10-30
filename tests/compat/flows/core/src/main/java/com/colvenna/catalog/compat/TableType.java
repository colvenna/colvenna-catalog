package com.colvenna.catalog.compat;

/**
 * The table types of every cell: a primary-key table without and with deletion vectors, and an
 * append table.
 */
public enum TableType {
    PK("pk", "t_pk", true, false),
    PK_DV("pk-dv", "t_pk_dv", true, true),
    APPEND("append", "t_append", false, false);

    private final String id;
    private final String table;
    private final boolean primaryKey;
    private final boolean deletionVectors;

    TableType(String id, String table, boolean primaryKey, boolean deletionVectors) {
        this.id = id;
        this.table = table;
        this.primaryKey = primaryKey;
        this.deletionVectors = deletionVectors;
    }

    /** The name results use: pk, pk-dv, append. */
    public String id() {
        return id;
    }

    /** The table's name in the cell's database. */
    public String table() {
        return table;
    }

    public boolean primaryKey() {
        return primaryKey;
    }

    public boolean deletionVectors() {
        return deletionVectors;
    }

    /**
     * The table's Paimon options, as both engines write them in their DDL. The deletion-vector
     * table reads its uncompacted level-0 files too ({@code deletion-vectors.merge-on-read}, Paimon
     * 2.0 and later): fresh reads, as a table under a streaming writer needs them.
     */
    public String options() {
        if (!primaryKey) {
            return "'bucket'='-1'";
        }
        if (!deletionVectors) {
            return "'bucket'='2', 'deletion-vectors.enabled'='false'";
        }
        return "'bucket'='2', 'deletion-vectors.enabled'='true',"
                + " 'deletion-vectors.merge-on-read'='true'";
    }
}
