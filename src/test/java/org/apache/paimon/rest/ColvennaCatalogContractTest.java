package org.apache.paimon.rest;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.ColvennaCatalogServer;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.testing.TestServers;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.sql.PreparedStatement;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.predicate.Predicate;
import org.apache.paimon.predicate.Transform;
import org.apache.paimon.rest.auth.AuthProviderEnum;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.AfterEachCallback;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;

/**
 * Runs the upstream Paimon 2.0.0 {@code RESTCatalogTest} and {@code CatalogTestBase} contract
 * suites against a real colvenna-catalog server backed by PostgreSQL.
 *
 * <p>The upstream mock server starts empty for every catalog the test creates; this harness does
 * the same by clearing the catalog tables and starting a new server. Tests that exercise behavior
 * outside colvenna-catalog's contract are listed with their reason in {@link UpstreamExclusions}.
 */
@ExtendWith(UpstreamExclusions.class)
class ColvennaCatalogContractTest extends RESTCatalogTest {

    private static final String CONTRACT_PRINCIPAL = "contract";

    private final List<ColvennaCatalogServer> servers = new ArrayList<>();
    private String dataPath;
    private String token;

    @BeforeEach
    @Override
    public void setUp() throws Exception {
        super.setUp();
        TestServers.resetAll();
        token = null;
        dataPath = warehouse.replaceFirst("file", RESTFileIOTestLoader.SCHEME);
        restCatalog = initCatalog(false, Map.of());
        catalog = restCatalog;
    }

    /** Stops the servers after the upstream tear-down, which still talks to the catalog. */
    @RegisterExtension
    final AfterEachCallback stopServers =
            context -> {
                servers.forEach(ColvennaCatalogServer::close);
                servers.clear();
            };

    private RESTCatalog initCatalog(boolean dataToken, Map<String, String> extraOptions)
            throws IOException {
        try {
            TestServers.reset();
            ColvennaCatalogServer server =
                    TestServers.start(dataPath, testDataTokens(), Clock.systemUTC());
            servers.add(server);
            if (token == null) {
                token =
                        server.tokens()
                                .bootstrapAdmin(CONTRACT_PRINCIPAL, Duration.ofHours(2))
                                .token();
                TestServers.grantCatalog(server, CONTRACT_PRINCIPAL);
            }
            options.set(RESTCatalogOptions.TOKEN.key(), token);
            options.set(
                    RESTCatalogOptions.TOKEN_PROVIDER.key(), AuthProviderEnum.BEAR.identifier());
            options.set(CatalogOptions.WAREHOUSE.key(), TestServers.WAREHOUSE);
            options.set(RESTCatalogOptions.URI, "http://localhost:" + server.port());
            options.set(RESTTokenFileIO.DATA_TOKEN_ENABLED.key(), String.valueOf(dataToken));
            options.set(RESTTestFileIO.DATA_PATH_CONF_KEY, dataPath);
            extraOptions.forEach(options::set);
            return new RESTCatalog(CatalogContext.create(options));
        } catch (IOException e) {
            throw e;
        } catch (Exception e) {
            throw new IOException(e);
        }
    }

    /** Vends the upstream test tokens that {@code RESTTestFileIO} checks on every file access. */
    private static DataTokenIssuer testDataTokens() {
        return (ctx, table, access) -> {
            String fullName = Identifier.create(table.database(), table.name()).getFullName();
            RESTToken token = DataTokenStore.getDataToken(TestServers.WAREHOUSE, fullName);
            if (token == null) {
                long expiresAt = System.currentTimeMillis() + 4_000_000L;
                token =
                        new RESTToken(
                                Map.of(
                                        "akId",
                                        "akId" + expiresAt,
                                        "akSecret",
                                        "akSecret" + expiresAt),
                                expiresAt);
                DataTokenStore.putDataToken(TestServers.WAREHOUSE, fullName, token);
            }
            return new DataTokenIssuer.Token(token.token(), token.expireAtMillis(), Map.of());
        };
    }

    /** Paimon's orphan-file cleaning adds the main branch to the list the catalog returns. */
    @Test
    void theBranchesOfATableWithoutAreAnEmptyListTheClientMayAddTo() throws Exception {
        Identifier id = Identifier.create("colvenna_branches", "t");
        catalog.createDatabase(id.getDatabaseName(), false);
        catalog.createTable(id, Schema.newBuilder().column("a", DataTypes.INT()).build(), false);
        List<String> branches = catalog.listBranches(id);
        assertThat(branches).isEmpty();
        branches.add("main");
        assertThat(branches).containsExactly("main");
    }

    private ColvennaCatalogServer server() {
        return servers.get(servers.size() - 1);
    }

    @Override
    protected Catalog newRestCatalogWithDataToken() throws IOException {
        return initCatalog(true, Map.of());
    }

    @Override
    protected Catalog newRestCatalogWithDataToken(Map<String, String> extraOptions)
            throws IOException {
        return initCatalog(true, extraOptions);
    }

    @Override
    protected void revokeTablePermission(Identifier identifier) {
        deny(Authorizer.Scope.TABLE, identifier.getDatabaseName(), identifier.getObjectName());
    }

    @Override
    protected void revokeViewPermission(Identifier identifier) {
        deny(Authorizer.Scope.VIEW, identifier.getDatabaseName(), identifier.getObjectName());
    }

    @Override
    protected void authTableColumns(Identifier identifier, List<String> columns) {
        grant(
                Authorizer.Scope.TABLE,
                identifier.getDatabaseName(),
                identifier.getObjectName(),
                Authorizer.Effect.ALLOW,
                columns);
    }

    @Override
    protected void revokeDatabasePermission(String database) {
        deny(Authorizer.Scope.DATABASE, database, null);
    }

    /** A READ deny forbids every level for the test's principal, as the upstream mock does. */
    private void deny(Authorizer.Scope scope, String database, String object) {
        grant(scope, database, object, Authorizer.Effect.DENY, null);
    }

    private void grant(
            Authorizer.Scope scope,
            String database,
            String object,
            Authorizer.Effect effect,
            List<String> columns) {
        try {
            server().authorizer()
                    .grant(
                            CONTRACT_PRINCIPAL,
                            scope,
                            database,
                            object,
                            Authorizer.Privilege.READ,
                            effect,
                            columns,
                            TestServers.testTerms(),
                            "contract-test");
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @Override
    protected RESTToken getDataTokenFromRestServer(Identifier identifier) {
        return DataTokenStore.getDataToken(TestServers.WAREHOUSE, identifier.getFullName());
    }

    @Override
    protected void setDataTokenToRestServerForMock(
            Identifier identifier, RESTToken expiredDataToken) {
        DataTokenStore.putDataToken(
                TestServers.WAREHOUSE, identifier.getFullName(), expiredDataToken);
    }

    @Override
    protected void resetDataTokenOnRestServer(Identifier identifier) {
        DataTokenStore.removeDataToken(TestServers.WAREHOUSE, identifier.getFullName());
    }

    @Override
    protected void updateSnapshotOnRestServer(
            Identifier identifier,
            Snapshot snapshot,
            long recordCount,
            long fileSizeInBytes,
            long fileCount,
            long lastFileCreationTime) {
        try {
            server().db()
                    .inTransaction(
                            c -> {
                                try (PreparedStatement update =
                                        c.prepareStatement(
                                                "UPDATE table_branches b SET snapshot_id = ?,"
                                                        + " snapshot_uuid = ?, snapshot_json = ?,"
                                                        + " record_count = ?, file_size_in_bytes = ?,"
                                                        + " file_count = ?, last_file_creation_time"
                                                        + " = ? FROM catalog_tables t WHERE"
                                                        + " t.id = b.table_id AND b.branch = 'main'"
                                                        + " AND t.database_name = ? AND t.name ="
                                                        + " ?")) {
                                    update.setLong(1, snapshot.id());
                                    update.setString(2, snapshot.uuid());
                                    update.setString(3, snapshot.toJson());
                                    update.setLong(4, recordCount);
                                    update.setLong(5, fileSizeInBytes);
                                    update.setLong(6, fileCount);
                                    update.setLong(7, lastFileCreationTime);
                                    update.setString(8, identifier.getDatabaseName());
                                    update.setString(9, identifier.getTableName());
                                    update.executeUpdate();
                                }
                                return null;
                            });
        } catch (Exception e) {
            throw new UncheckedIOException(new IOException(e));
        }
    }

    @Override
    protected void setColumnMasking(Identifier identifier, Map<String, Transform> columnMasking) {
        throw new UnsupportedOperationException(
                "Column masking is outside colvenna-catalog's contract.");
    }

    @Override
    protected void setRowFilter(Identifier identifier, List<Predicate> rowFilter) {
        throw new UnsupportedOperationException(
                "Row filters are outside colvenna-catalog's contract.");
    }
}
