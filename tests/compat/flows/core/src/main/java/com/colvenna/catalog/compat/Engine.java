package com.colvenna.catalog.compat;

import java.util.List;

/** What the scenario needs of an engine: its SQL dialect and a way to run statements. */
public interface Engine {

    /** {@code flink} or {@code spark}. */
    String family();

    /** The engine's version, as it reports it at run time. */
    String version();

    /** Runs a statement to completion. */
    void execute(String sql) throws Exception;

    /** The first row of a query's result. */
    List<Object> firstRow(String sql) throws Exception;

    /** The names of the database's tables, as the engine lists them. */
    List<String> tables(String db) throws Exception;

    /** The names of the catalog's databases, as the engine lists them. */
    List<String> databases() throws Exception;

    /** The table's name in the engine's SQL. */
    String qualified(String db, String table);

    /** The statement that creates the table type's table, with Dataset's columns. */
    String createTable(String db, TableType type);

    /** The statement that sets one of the table's options. */
    String setOption(String db, String table, String key, String value);
}
