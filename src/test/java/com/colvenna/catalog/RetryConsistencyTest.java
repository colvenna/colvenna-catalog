package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.DroppingProxy;
import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.schema.SchemaChange;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Catalog changes whose response is lost: each is applied exactly once, and retrying the same call
 * gets the answer the resulting state implies (already exists, does not exist), never a second
 * object or a half-applied change.
 */
class RetryConsistencyTest extends ServerTestBase {

    private static final Identifier TABLE = Identifier.create("db", "t");

    private DroppingProxy proxy;
    private RESTCatalog viaProxy;

    @BeforeEach
    void startProxy() throws Exception {
        proxy = new DroppingProxy(server.port());
        viaProxy = catalog(adminToken, proxy.port(), Map.of());
    }

    @AfterEach
    void stopProxy() {
        proxy.close();
    }

    @Test
    void createDatabase() throws Exception {
        proxy.dropResponses(path -> path.endsWith("/databases"), 1);
        assertThatThrownBy(() -> viaProxy.createDatabase("db", false)).isNotNull();
        assertThatThrownBy(() -> viaProxy.createDatabase("db", false))
                .isInstanceOf(Catalog.DatabaseAlreadyExistException.class);
        viaProxy.createDatabase("db", true);
        assertThat(viaProxy.listDatabases()).containsExactly("db");
        // The ignoreIfExists call is answered 409 too; the client then ignores it.
        assertThat(auditOutcomes("CREATE_DATABASE"))
                .containsExactly("SUCCEEDED:200", "FAILED:409", "FAILED:409");
    }

    @Test
    void createTable() throws Exception {
        viaProxy.createDatabase("db", false);
        proxy.dropResponses(path -> path.endsWith("/tables"), 1);
        assertThatThrownBy(() -> Tables.createAppendTable(viaProxy, TABLE, Map.of())).isNotNull();
        assertThatThrownBy(() -> Tables.createAppendTable(viaProxy, TABLE, Map.of()))
                .isInstanceOf(Catalog.TableAlreadyExistException.class);
        assertThat(viaProxy.listTables("db")).containsExactly("t");
        assertThat(auditOutcomes("CREATE_TABLE")).containsExactly("SUCCEEDED:200", "FAILED:409");
    }

    @Test
    void alterTableAddColumn() throws Exception {
        Tables.createAppendTable(viaProxy, TABLE, Map.of());
        long before = ((FileStoreTable) viaProxy.getTable(TABLE)).schema().id();
        SchemaChange add = SchemaChange.addColumn("extra", DataTypes.STRING());
        proxy.dropResponses(path -> path.endsWith("/tables/t"), 1);
        assertThatThrownBy(() -> viaProxy.alterTable(TABLE, List.of(add), false)).isNotNull();
        assertThatThrownBy(() -> viaProxy.alterTable(TABLE, List.of(add), false))
                .isInstanceOf(Catalog.ColumnAlreadyExistException.class);
        FileStoreTable table = (FileStoreTable) viaProxy.getTable(TABLE);
        assertThat(table.schema().id()).isEqualTo(before + 1);
        assertThat(table.schema().fieldNames()).containsExactly("id", "payload", "extra");
    }

    @Test
    void renameTable() throws Exception {
        Tables.createAppendTable(viaProxy, TABLE, Map.of());
        Tables.write(viaProxy.getTable(TABLE), 1);
        Identifier renamed = Identifier.create("db", "renamed");
        proxy.dropResponses(path -> path.endsWith("/tables/rename"), 1);
        assertThatThrownBy(() -> viaProxy.renameTable(TABLE, renamed, false)).isNotNull();
        assertThatThrownBy(() -> viaProxy.renameTable(TABLE, renamed, false))
                .isInstanceOf(Catalog.TableNotExistException.class);
        assertThat(viaProxy.listTables("db")).containsExactly("renamed");
        assertThat(Tables.ids(viaProxy.getTable(renamed))).containsExactly(1);
    }

    @Test
    void dropTableIsRetriedByTheClientAndReportsTheTableGone() throws Exception {
        Tables.createAppendTable(viaProxy, TABLE, Map.of());
        proxy.dropResponses(path -> path.endsWith("/tables/t"), 1);
        // DELETE is idempotent, so the HTTP client resends it; the resend finds no table.
        assertThatThrownBy(() -> viaProxy.dropTable(TABLE, false))
                .isInstanceOf(Catalog.TableNotExistException.class);
        assertThat(proxy.droppedCount()).isEqualTo(1);
        viaProxy.dropTable(TABLE, true);
        assertThat(viaProxy.listTables("db")).isEmpty();
        assertThat(auditOutcomes("DROP_TABLE"))
                .containsExactly("SUCCEEDED:200", "FAILED:404", "FAILED:404");
    }

    @Test
    void lostReadResponseIsRetriedTransparently() throws Exception {
        Tables.createAppendTable(viaProxy, TABLE, Map.of());
        proxy.dropResponses(path -> path.endsWith("/tables/t"), 1);
        assertThat(viaProxy.getTable(TABLE).name()).isEqualTo("t");
        assertThat(proxy.droppedCount()).isEqualTo(1);
    }

    private List<String> auditOutcomes(String action) throws Exception {
        List<String> rows = new ArrayList<>();
        try (var c = server.db().dataSource().getConnection();
                PreparedStatement select =
                        c.prepareStatement(
                                "SELECT outcome || ':' || http_status FROM audit_events WHERE"
                                        + " action = ? ORDER BY id")) {
            select.setString(1, action);
            try (ResultSet rs = select.executeQuery()) {
                while (rs.next()) {
                    rows.add(rs.getString(1));
                }
            }
        }
        return rows;
    }
}
