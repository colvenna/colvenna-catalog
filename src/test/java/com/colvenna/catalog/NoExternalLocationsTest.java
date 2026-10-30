package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.testing.ServerTestBase;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.requests.CreateTableRequest;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** Without allowed external roots, the default, clients cannot choose any location. */
class NoExternalLocationsTest extends ServerTestBase {

    @TempDir Path outside;

    @Override
    protected List<String> externalLocationRoots() {
        return List.of();
    }

    @Test
    void everyClientChosenLocationIsRejected() throws Exception {
        catalog(adminToken).createDatabase("db", false);
        String location = outside.toUri().toString();
        for (String option :
                List.of("path", "data-file.external-paths", "global-index.external-path")) {
            Schema schema =
                    Schema.newBuilder()
                            .column("id", DataTypes.INT())
                            .option(option, location)
                            .build();
            HttpResponse<String> response =
                    http(
                            "POST",
                            "/v1/colvenna/databases/db/tables",
                            adminToken,
                            RESTApi.toJson(
                                    new CreateTableRequest(Identifier.create("db", "t"), schema)));
            assertThat(response.statusCode()).as(option).isEqualTo(400);
            assertThat(response.body()).contains("allows no client-chosen locations");
        }
        assertThat(catalog(adminToken, server.port(), Map.of()).listTables("db")).isEmpty();
    }
}
