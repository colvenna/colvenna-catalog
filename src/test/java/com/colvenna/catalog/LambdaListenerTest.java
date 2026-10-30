package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.colvenna.catalog.testing.ServerTestBase.MutableClock;
import com.colvenna.catalog.testing.TestServers;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.net.ConnectException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import javax.annotation.Nullable;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The Lambda form's listener: plain HTTP on 127.0.0.1 only ({@code
 * COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT}), allowed only in a Lambda execution environment and never
 * together with the general plaintext switch; the S3A region it needs, as the EKS form does; and
 * pool sizes that a connection budget can rely on.
 */
class LambdaListenerTest {

    private static final String LAMBDA = "AWS_LAMBDA_RUNTIME_API";

    @TempDir Path warehouseDir;
    @Nullable private ColvennaCatalogServer server;

    @AfterEach
    void stop() {
        if (server != null) {
            server.close();
        }
    }

    /** The test server's settings as the Lambda form sets them: no TLS, no general plaintext. */
    private Map<String, String> lambda() {
        Map<String, String> env = new HashMap<>();
        env.put(LAMBDA, "127.0.0.1:9001");
        env.put("COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT", "true");
        env.put("COLVENNA_ALLOW_PLAINTEXT", "false");
        env.put("COLVENNA_MIGRATE_ON_START", "true");
        return env;
    }

    private ServerConfig config(Map<String, String> settings) {
        return TestServers.withSettings(
                warehouseDir.toUri().toString(), TestServers.TEMP_EXTERNAL_ROOTS, settings);
    }

    @Test
    void theApiServesPlainHttpOnLoopbackOnly() throws Exception {
        ServerConfig config = config(lambda());
        config.validateForServing();
        server =
                ColvennaCatalogServer.create(
                        config, new MutableClock(), DataTokenIssuer.disabled());
        server.start();
        assertThat(server.address().getAddress().getHostAddress()).isEqualTo("127.0.0.1");

        TestServers.resetAll();
        String token = server.tokens().bootstrapAdmin("admin", Duration.ofHours(1)).token();
        TestServers.grantCatalog(server, "admin");
        assertThat(
                        get(
                                "http://127.0.0.1:"
                                        + server.port()
                                        + "/v1/config?warehouse=colvenna",
                                token))
                .isEqualTo(200);
        // Loopback is no anonymous path: the token is still checked.
        assertThat(get("http://127.0.0.1:" + server.port() + "/v1/config?warehouse=colvenna", null))
                .isEqualTo(401);

        Optional<InetAddress> external = nonLoopbackAddress();
        assumeTrue(external.isPresent(), "this host has no non-loopback address to try");
        assertThatThrownBy(
                        () -> {
                            try (Socket socket = new Socket()) {
                                socket.connect(
                                        new InetSocketAddress(external.get(), server.port()), 2000);
                            }
                        })
                .isInstanceOf(ConnectException.class);
    }

    @Test
    void loopbackPlaintextIsRefusedOutsideLambda() {
        Map<String, String> settings = lambda();
        settings.remove(LAMBDA);
        assertThatThrownBy(() -> config(settings).validateForServing())
                .hasMessageContaining("Lambda form only");
    }

    @Test
    void loopbackPlaintextExcludesTheGeneralSwitchAndTls() {
        Map<String, String> general = lambda();
        general.put("COLVENNA_ALLOW_PLAINTEXT", "true");
        assertThatThrownBy(() -> config(general).validateForServing())
                .hasMessageContaining(
                        "excludes COLVENNA_TLS_KEYSTORE and COLVENNA_ALLOW_PLAINTEXT");

        Map<String, String> tls = lambda();
        tls.put("COLVENNA_TLS_KEYSTORE", "/tmp/server.p12");
        tls.put("COLVENNA_TLS_KEYSTORE_PASSWORD", "changeit");
        assertThatThrownBy(() -> config(tls).validateForServing())
                .hasMessageContaining("excludes COLVENNA_TLS_KEYSTORE");

        // Lambda without the setting still needs TLS, as any other form does.
        Map<String, String> neither = lambda();
        neither.remove("COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT");
        assertThatThrownBy(() -> config(neither).validateForServing())
                .hasMessageContaining("COLVENNA_TLS_KEYSTORE is required");
    }

    @Test
    void anS3WarehouseNeedsTheS3aRegionInEitherForm() {
        Map<String, String> settings = lambda();
        settings.put("COLVENNA_WAREHOUSE_ROOT", "s3://bucket/warehouse");
        settings.put("COLVENNA_FILEIO_S3_REGION", "us-west-2");
        assertThatThrownBy(() -> config(settings).validateForServing())
                .hasMessageContaining("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION");

        settings.put("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION", "us-west-2");
        config(settings).validateForServing();

        // The EKS form as well (#9): its nodes may have no internet egress either.
        Map<String, String> eks = new HashMap<>(settings);
        eks.remove(LAMBDA);
        eks.remove("COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT");
        eks.put("COLVENNA_ALLOW_PLAINTEXT", "true");
        config(eks).validateForServing();
        eks.remove("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION");
        assertThatThrownBy(() -> config(eks).validateForServing())
                .hasMessageContaining("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION");
        // An endpoint names the store, and with it its Region, as an S3-compatible store needs.
        eks.put("COLVENNA_FILEIO_S3_ENDPOINT", "https://s3.us-west-2.amazonaws.com");
        config(eks).validateForServing();
        // A warehouse on a local file system needs neither.
        eks.remove("COLVENNA_FILEIO_S3_ENDPOINT");
        eks.put("COLVENNA_WAREHOUSE_ROOT", warehouseDir.toUri().toString());
        config(eks).validateForServing();
    }

    @Test
    void poolSizesBelowOneAreRefused() {
        for (String pool :
                new String[] {"COLVENNA_JDBC_POOL_SIZE", "COLVENNA_JDBC_AUX_POOL_SIZE"}) {
            Map<String, String> settings = lambda();
            settings.put(pool, "0");
            assertThatThrownBy(() -> config(settings)).as(pool).hasMessageContaining("at least 1");
        }
        // One request per execution environment: the spike's pool sizes.
        Map<String, String> budget = lambda();
        budget.put("COLVENNA_JDBC_POOL_SIZE", "2");
        budget.put("COLVENNA_JDBC_AUX_POOL_SIZE", "1");
        ServerConfig config = config(budget);
        assertThat(config.jdbcPoolSize()).isEqualTo(2);
        assertThat(config.jdbcAuxPoolSize()).isEqualTo(1);
    }

    private static Optional<InetAddress> nonLoopbackAddress() throws Exception {
        for (NetworkInterface nic : Collections.list(NetworkInterface.getNetworkInterfaces())) {
            if (!nic.isUp() || nic.isLoopback()) {
                continue;
            }
            for (InetAddress address : Collections.list(nic.getInetAddresses())) {
                if (address instanceof Inet4Address && !address.isLoopbackAddress()) {
                    return Optional.of(address);
                }
            }
        }
        return Optional.empty();
    }

    private static int get(String uri, @Nullable String token) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(uri)).GET();
        if (token != null) {
            request.header("Authorization", "Bearer " + token);
        }
        try (HttpClient client = HttpClient.newHttpClient()) {
            return client.send(request.build(), HttpResponse.BodyHandlers.ofString()).statusCode();
        }
    }
}
