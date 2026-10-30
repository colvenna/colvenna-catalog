package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.requests.CreateDatabaseRequest;
import org.apache.paimon.rest.requests.CreateTableRequest;
import org.apache.paimon.rest.requests.RenameTableRequest;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.schema.SchemaChange;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Names and client-chosen locations can never make the server read, write or delete files outside
 * the table they belong to.
 */
class PathSafetyTest extends ServerTestBase {

    @TempDir Path outside;

    @Test
    void namesThatNavigateTheFileSystemAreRejected() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("db", false);
        for (String name :
                List.of(".", "..", "a/b", "a\\b", "tab\tname", "a?b", "a#b", "a%b", "*")) {
            HttpResponse<String> table =
                    http(
                            "POST",
                            "/v1/colvenna/databases/db/tables",
                            adminToken,
                            RESTApi.toJson(
                                    new CreateTableRequest(
                                            Identifier.create("db", name),
                                            simpleSchema(Map.of()))));
            assertThat(table.statusCode()).as("table %s", name).isEqualTo(400);
            HttpResponse<String> database =
                    http(
                            "POST",
                            "/v1/colvenna/databases",
                            adminToken,
                            RESTApi.toJson(new CreateDatabaseRequest(name, Map.of())));
            assertThat(database.statusCode()).as("database %s", name).isEqualTo(400);
        }
        assertThat(catalog.listTables("db")).isEmpty();
    }

    @Test
    void externalLocationsInsideTheWarehouseAreRejected() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, Identifier.create("db", "victim"), Map.of());
        String victim =
                ((FileStoreTable) catalog.getTable(Identifier.create("db", "victim")))
                        .location()
                        .toString();
        for (String option :
                List.of("path", "data-file.external-paths", "global-index.external-path")) {
            HttpResponse<String> response =
                    http(
                            "POST",
                            "/v1/colvenna/databases/db/tables",
                            adminToken,
                            RESTApi.toJson(
                                    new CreateTableRequest(
                                            Identifier.create("db", "attacker"),
                                            simpleSchema(Map.of(option, victim)))));
            assertThat(response.statusCode()).as(option).isEqualTo(400);
            assertThat(response.body()).contains("overlaps the catalog-managed warehouse");
        }
        Tables.createAppendTable(catalog, Identifier.create("db", "attacker"), Map.of());
        assertThatThrownBy(
                        () ->
                                catalog.alterTable(
                                        Identifier.create("db", "attacker"),
                                        List.of(
                                                SchemaChange.setOption(
                                                        "data-file.external-paths", victim)),
                                        false))
                .hasMessageContaining("overlaps the catalog-managed warehouse");
        catalog.dropTable(Identifier.create("db", "attacker"), false);
        assertThat(Files.exists(Path.of(java.net.URI.create(victim)))).isTrue();
        assertThat(Tables.ids(catalog.getTable(Identifier.create("db", "victim")))).isEmpty();
    }

    @Test
    void externalTableOutsideTheWarehouseIsAllowedAndKeptOnDrop() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("db", false);
        String location = outside.resolve("ext").toUri().toString();
        catalog.createTable(
                Identifier.create("db", "ext"), simpleSchema(Map.of("path", location)), false);
        Tables.write(catalog.getTable(Identifier.create("db", "ext")), 1);
        catalog.dropTable(Identifier.create("db", "ext"), false);
        assertThat(Files.exists(outside.resolve("ext").resolve("schema"))).isTrue();
    }

    @Test
    void registeringAPathThatOverlapsAnotherTableIsRejected() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, Identifier.create("db", "owner"), Map.of());
        String owned =
                ((FileStoreTable) catalog.getTable(Identifier.create("db", "owner")))
                        .location()
                        .toString();
        assertThatThrownBy(() -> catalog.registerTable(Identifier.create("db", "copy"), owned))
                .hasMessageContaining("overlaps");
        String outsideTable = outside.resolve("t").toUri().toString();
        assertThatThrownBy(
                        () -> catalog.registerTable(Identifier.create("db", "copy"), outsideTable))
                .hasMessageContaining("inside the warehouse");
    }

    @Test
    void branchTagAndConsumerNamesCannotNavigateToAnotherTable() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, Identifier.create("db", "mine"), Map.of());
        Tables.createAppendTable(catalog, Identifier.create("db", "victim"), Map.of());
        Tables.write(catalog.getTable(Identifier.create("db", "victim")), 7);
        String table = "/v1/colvenna/databases/db/tables/mine";
        List<HttpResponse<String>> attempts =
                List.of(
                        http(
                                "DELETE",
                                table + "/branches/x%2F..%2F..%2F..%2Fvictim",
                                adminToken,
                                null),
                        http("DELETE", table + "/branches/..", adminToken, null),
                        http("DELETE", table + "/tags/..%2F..%2Fvictim", adminToken, null),
                        http(
                                "POST",
                                table + "/branches",
                                adminToken,
                                "{\"branch\":\"../../../victim\"}"),
                        http("POST", table + "/tags", adminToken, "{\"tagName\":\"../../victim\"}"),
                        http(
                                "POST",
                                table + "/consumers/reset",
                                adminToken,
                                "{\"consumerId\":\"x/../../../victim/schema/schema-0\"}"),
                        http(
                                "GET",
                                "/v1/colvenna/databases/db/tables/mine%24branch_..%2F..%2Fvictim",
                                adminToken,
                                null));
        for (HttpResponse<String> attempt : attempts) {
            assertThat(attempt.statusCode()).as(attempt.uri().toString()).isEqualTo(400);
        }
        FileStoreTable victim =
                (FileStoreTable) catalog.getTable(Identifier.create("db", "victim"));
        assertThat(Tables.ids(victim)).containsExactly(7);
        assertThat(victim.schemaManager().latest()).isPresent();
    }

    @Test
    void locationsAreComparedAfterNormalization() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("db", false);
        String inside = java.net.URI.create(warehouse).getPath() + "db.db/sneaky";
        for (String location :
                List.of(inside, "FILE://" + inside, "file:" + inside + "/../sneaky2")) {
            HttpResponse<String> response =
                    http(
                            "POST",
                            "/v1/colvenna/databases/db/tables",
                            adminToken,
                            RESTApi.toJson(
                                    new CreateTableRequest(
                                            Identifier.create("db", "t"),
                                            simpleSchema(
                                                    Map.of(
                                                            "data-file.external-paths",
                                                            location)))));
            assertThat(response.statusCode()).as(location).isEqualTo(400);
        }
    }

    @Test
    void locationsThatContainTheWarehouseAreRejected() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("db", false);
        String parent =
                java.nio.file.Path.of(java.net.URI.create(warehouse))
                        .getParent()
                        .toUri()
                        .toString();
        HttpResponse<String> response =
                http(
                        "POST",
                        "/v1/colvenna/databases/db/tables",
                        adminToken,
                        RESTApi.toJson(
                                new CreateTableRequest(
                                        Identifier.create("db", "umbrella"),
                                        simpleSchema(Map.of("path", parent)))));
        assertThat(response.statusCode()).isEqualTo(400);
        assertThatThrownBy(() -> catalog.registerTable(Identifier.create("db", "root"), warehouse))
                .hasMessageContaining("inside the warehouse");
    }

    @Test
    void concurrentRegistrationsCannotNest() throws Exception {
        String root;
        try (org.apache.paimon.catalog.Catalog fs = fileSystemCatalog()) {
            Tables.createAppendTable(fs, Identifier.create("m1", "t"), Map.of());
            Tables.write(fs.getTable(Identifier.create("m1", "t")), 1);
            ((FileStoreTable) fs.getTable(Identifier.create("m1", "t"))).createBranch("b");
            root =
                    ((FileStoreTable) fs.getTable(Identifier.create("m1", "t")))
                            .location()
                            .toString();
        }
        String branch = root + "/branch/branch-b";
        RESTCatalog catalog = catalog(adminToken);
        catalog.createDatabase("x1", false);
        catalog.createDatabase("x2", false);
        java.util.concurrent.ExecutorService pool =
                java.util.concurrent.Executors.newFixedThreadPool(2);
        java.util.concurrent.CountDownLatch start = new java.util.concurrent.CountDownLatch(1);
        java.util.concurrent.Future<Boolean> a = pool.submit(() -> register(start, "x1", root));
        java.util.concurrent.Future<Boolean> b = pool.submit(() -> register(start, "x2", branch));
        start.countDown();
        assertThat(List.of(a.get(), b.get())).containsExactlyInAnyOrder(true, false);
        pool.shutdown();
    }

    @Test
    void dropAndRenameNameTheTableItself() throws Exception {
        RESTCatalog catalog = catalog(adminToken);
        Tables.createAppendTable(catalog, Identifier.create("db", "t"), Map.of());
        assertThat(
                        http(
                                        "DELETE",
                                        "/v1/colvenna/databases/db/tables/t%24branch_b",
                                        adminToken,
                                        null)
                                .statusCode())
                .isEqualTo(400);
        assertThat(
                        http(
                                        "DELETE",
                                        "/v1/colvenna/databases/db/tables/t%24snapshots",
                                        adminToken,
                                        null)
                                .statusCode())
                .isEqualTo(400);
        for (String[] names : new String[][] {{"t$branch_b", "u"}, {"t", "u$branch_b"}}) {
            assertThat(
                            http(
                                            "POST",
                                            "/v1/colvenna/tables/rename",
                                            adminToken,
                                            RESTApi.toJson(
                                                    new RenameTableRequest(
                                                            Identifier.create("db", names[0]),
                                                            Identifier.create("db", names[1]))))
                                    .statusCode())
                    .isEqualTo(400);
        }
        assertThat(catalog.listTables("db")).containsExactly("t");
    }

    private boolean register(java.util.concurrent.CountDownLatch start, String db, String path)
            throws Exception {
        start.await();
        try (RESTCatalog own = catalog(adminToken)) {
            own.registerTable(Identifier.create(db, "t"), path);
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }

    private org.apache.paimon.catalog.Catalog fileSystemCatalog() {
        org.apache.paimon.options.Options options = new org.apache.paimon.options.Options();
        options.set(org.apache.paimon.options.CatalogOptions.WAREHOUSE, warehouse);
        return org.apache.paimon.catalog.CatalogFactory.createCatalog(
                org.apache.paimon.catalog.CatalogContext.create(options));
    }

    private static Schema simpleSchema(Map<String, String> options) {
        Schema.Builder schema = Schema.newBuilder().column("id", DataTypes.INT());
        options.forEach(schema::option);
        return schema.build();
    }
}
