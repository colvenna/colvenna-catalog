package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.table.FileStoreTable;
import org.junit.jupiter.api.Test;

/**
 * Taking over a table that another catalog created in the same warehouse layout, as when Paimon
 * JDBC catalog tables move to colvenna-catalog.
 */
class RegisterTableTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("ods", "orders");

    @Test
    void registeredTableKeepsItsDataAndSnapshotLine() throws Exception {
        String path;
        try (Catalog previous = fileSystemCatalog()) {
            Tables.createKeyedTable(previous, TABLE, Map.of());
            Tables.write(previous.getTable(TABLE), 1, 2, 3);
            Tables.write(previous.getTable(TABLE), 4);
            path = ((FileStoreTable) previous.getTable(TABLE)).location().toString();
        }

        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("ods", false);
        catalog.registerTable(TABLE, path);

        FileStoreTable registered = (FileStoreTable) catalog.getTable(TABLE);
        assertThat(registered.location().toString()).isEqualTo(path);
        assertThat(Tables.ids(registered)).containsExactlyInAnyOrder(1, 2, 3, 4);
        assertThat(catalog.loadSnapshot(TABLE).get().snapshot().id()).isEqualTo(2);

        Tables.write(registered, 5);
        assertThat(Tables.ids(catalog.getTable(TABLE))).containsExactlyInAnyOrder(1, 2, 3, 4, 5);
        assertThat(catalog.loadSnapshot(TABLE).get().snapshot().id()).isEqualTo(3);
    }

    @Test
    void previousCatalogWritingAfterTheSwitchIsDetected() throws Exception {
        String path;
        try (Catalog previous = fileSystemCatalog()) {
            Tables.createKeyedTable(previous, TABLE, Map.of());
            Tables.write(previous.getTable(TABLE), 1);
            path = ((FileStoreTable) previous.getTable(TABLE)).location().toString();
        }
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("ods", false);
        catalog.registerTable(TABLE, path);

        // A writer that was not switched keeps committing through the old catalog.
        try (Catalog previous = fileSystemCatalog()) {
            Tables.write(previous.getTable(TABLE), 2);
        }
        FileStoreTable table =
                (FileStoreTable)
                        catalog.getTable(TABLE)
                                .copy(
                                        Map.of(
                                                "commit.max-retries",
                                                "1",
                                                "commit.min-retry-wait",
                                                "10 ms"));
        assertThatThrownBy(() -> Tables.write(table, 3)).hasStackTraceContaining("fenced");
    }

    @Test
    void registeringAPathWithoutATableFails() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("ods", false);
        assertThatThrownBy(() -> catalog.registerTable(TABLE, warehouse + "/nothing-here"))
                .hasMessageContaining("No Paimon schema");
    }

    private Catalog fileSystemCatalog() {
        Options options = new Options();
        options.set(CatalogOptions.WAREHOUSE, warehouse);
        return CatalogFactory.createCatalog(CatalogContext.create(options));
    }
}
