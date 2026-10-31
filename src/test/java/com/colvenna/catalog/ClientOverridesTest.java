package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.testing.ServerTestBase;
import java.util.Map;
import org.apache.paimon.rest.RESTCatalog;
import org.junit.jupiter.api.Test;

/**
 * {@code COLVENNA_CLIENT_OVERRIDES} wins over the catalog's client defaults and over the client's
 * own setting (#1): an operator who wants S3A's checks back on every client sets it.
 */
class ClientOverridesTest extends ServerTestBase {

    private static final String KEY = "fs.s3a.create.performance";

    @Override
    protected Map<String, String> settings() {
        return Map.of("COLVENNA_CLIENT_OVERRIDES", KEY + "=false");
    }

    @Test
    void theOverrideWinsOverTheDefaultAndTheClient() throws Exception {
        try (RESTCatalog catalog = catalog(adminToken, server.port(), Map.of(KEY, "true"))) {
            assertThat(catalog.options()).containsEntry(KEY, "false");
        }
    }
}
