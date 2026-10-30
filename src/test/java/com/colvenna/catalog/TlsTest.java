package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.testing.TestServers;
import com.colvenna.catalog.token.DataTokenIssuer;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManagerFactory;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.options.CatalogOptions;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.rest.RESTCatalogOptions;
import org.apache.paimon.rest.exceptions.RESTException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** The API port speaks TLS only; the Paimon client trusts it through the JVM trust store. */
class TlsTest {

    // pragma: allowlist nextline secret - the JDK's well-known default store password
    private static final String PASSWORD = "changeit";

    @TempDir Path dir;
    private ColvennaCatalogServer server;
    private String token;
    private Path trustStore;

    @BeforeEach
    void start() throws Exception {
        TestServers.resetAll();
        Path keyStore = dir.resolve("server.p12");
        trustStore = dir.resolve("trust.p12");
        keytool(
                "-genkeypair",
                "-alias",
                "colvenna",
                "-keyalg",
                "EC",
                "-groupname",
                "secp256r1",
                "-dname",
                "CN=localhost",
                "-ext",
                "SAN=dns:localhost,ip:127.0.0.1",
                "-validity",
                "2",
                "-keystore",
                keyStore.toString(),
                "-storetype",
                "PKCS12",
                "-storepass",
                PASSWORD);
        Path cert = dir.resolve("colvenna.crt");
        keytool(
                "-exportcert",
                "-alias",
                "colvenna",
                "-keystore",
                keyStore.toString(),
                "-storepass",
                PASSWORD,
                "-rfc",
                "-file",
                cert.toString());
        keytool(
                "-importcert",
                "-noprompt",
                "-alias",
                "colvenna",
                "-file",
                cert.toString(),
                "-keystore",
                trustStore.toString(),
                "-storetype",
                "PKCS12",
                "-storepass",
                PASSWORD);
        ServerConfig plain =
                TestServers.config(dir.resolve("warehouse").toUri().toString(), Map.of());
        ServerConfig tls =
                new ServerConfig(
                        plain.port(),
                        plain.healthPort(),
                        plain.metricsPort(),
                        new ServerConfig.Tls(keyStore.toString(), PASSWORD),
                        plain.jdbcUrl(),
                        plain.jdbcUser(),
                        plain.jdbcPassword(),
                        plain.jdbcPoolSize(),
                        plain.jdbcAuxPoolSize(),
                        plain.warehouseName(),
                        plain.warehouseRoot(),
                        plain.externalLocationRoots(),
                        plain.fileIoOptions(),
                        plain.clientOverrides(),
                        plain.defaultPageSize(),
                        plain.maxPageSize(),
                        plain.maxInFlightRequests(),
                        plain.authCacheTtl(),
                        false,
                        plain.dataTokens(),
                        plain.environment(),
                        plain.migrateOnStart(),
                        plain.lambdaRuntime(),
                        plain.timeouts(),
                        plain.maxResponseBytes(),
                        plain.lambdaLoopbackPlaintext(),
                        plain.jdbcAuth(),
                        plain.dropPurge(),
                        plain.requestWorkTime(),
                        plain.shutdown());
        server = ColvennaCatalogServer.create(tls, Clock.systemUTC(), DataTokenIssuer.disabled());
        server.start();
        token = server.tokens().bootstrapAdmin("admin", Duration.ofHours(1)).token();
        TestServers.grantCatalog(server, "admin");
    }

    @AfterEach
    void stop() {
        server.close();
    }

    // Paimon's REST client shares one HTTP client per JVM (HttpClientUtils.DEFAULT_HTTP_CLIENT)
    // and fixes its TLS context when that class first loads. So the trust store is given when the
    // JVM starts, as the user manual tells clients (-Djavax.net.ssl.trustStore). Setting it at run
    // time worked only while no earlier test of the same JVM had used a RESTCatalog; on GitHub's
    // runners the contract test runs first (#16). The client runs in a JVM of its own, so the
    // order of the test classes cannot matter.
    @Test
    void paimonClientConnectsOverTlsWithTheTrustStoreGivenAtJvmStart() throws Exception {
        Client run = paimonClient(true);
        assertThat(run.exit()).as(run.output()).isZero();
        assertThat(run.output()).contains(PaimonClient.DATABASES + "db\n");
    }

    @Test
    void paimonClientWithoutTheTrustStoreIsRefusedByTls() throws Exception {
        Client run = paimonClient(false);
        assertThat(run.exit()).as(run.output()).isEqualTo(PaimonClient.REFUSED);
        // Paimon drops the cause: the same JVM's HTTPS client shows it is the certificate check
        assertThat(run.output())
                .contains(PaimonClient.PROBE + "javax.net.ssl.SSLHandshakeException")
                .doesNotContain(PaimonClient.DATABASES);
    }

    private record Client(int exit, String output) {}

    private Client paimonClient(boolean trusting) throws Exception {
        List<String> command = new ArrayList<>();
        command.add(Path.of(System.getProperty("java.home"), "bin", "java").toString());
        if (trusting) {
            command.add("-Djavax.net.ssl.trustStore=" + trustStore);
            command.add("-Djavax.net.ssl.trustStorePassword=" + PASSWORD);
            command.add("-Djavax.net.ssl.trustStoreType=PKCS12");
        }
        command.addAll(
                List.of(
                        "-cp",
                        System.getProperty("java.class.path"),
                        PaimonClient.class.getName(),
                        "https://localhost:" + server.port(),
                        token,
                        TestServers.WAREHOUSE));
        Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
        CompletableFuture<String> output =
                CompletableFuture.supplyAsync(
                        () -> {
                            try (InputStream in = process.getInputStream()) {
                                return new String(in.readAllBytes(), StandardCharsets.UTF_8);
                            } catch (java.io.IOException e) {
                                throw new java.io.UncheckedIOException(e);
                            }
                        });
        if (!process.waitFor(2, TimeUnit.MINUTES)) {
            process.destroyForcibly();
            throw new AssertionError("the Paimon client did not finish within 2 minutes");
        }
        return new Client(process.exitValue(), output.get(30, TimeUnit.SECONDS));
    }

    /** The Paimon client in a JVM of its own: creates a database, then lists the databases. */
    static final class PaimonClient {
        static final String DATABASES = "databases=";
        static final String PROBE = "probe=";
        static final int REFUSED = 3;

        public static void main(String[] args) {
            Options options = new Options();
            options.set(RESTCatalogOptions.URI, args[0]);
            options.set(RESTCatalogOptions.TOKEN_PROVIDER, "bear");
            options.set(RESTCatalogOptions.TOKEN, args[1]);
            options.set(CatalogOptions.WAREHOUSE, args[2]);
            int exit = 0;
            try (RESTCatalog catalog = new RESTCatalog(CatalogContext.create(options))) {
                catalog.createDatabase("db", false);
                System.out.println(DATABASES + String.join(",", catalog.listDatabases()));
            } catch (RESTException e) {
                System.out.println("refused: " + e.getMessage());
                System.out.println(PROBE + probe(args[0]));
                exit = REFUSED;
            } catch (Exception e) {
                e.printStackTrace(System.out);
                exit = 1;
            }
            System.out.flush();
            System.exit(exit);
        }

        /** What the JVM's own HTTPS client, with its default trust, gets from the address. */
        private static String probe(String uri) {
            try (HttpClient client = HttpClient.newHttpClient()) {
                client.send(
                        HttpRequest.newBuilder(URI.create(uri + "/v1/config")).build(),
                        HttpResponse.BodyHandlers.discarding());
                return "connected";
            } catch (Exception e) {
                return e.getClass().getName();
            }
        }
    }

    @Test
    void theApiPortSpeaksTls13AndFallsBackToTls12() throws Exception {
        assertThat(handshake()).isEqualTo("TLSv1.3");
        assertThat(handshake("TLSv1.2")).isEqualTo("TLSv1.2");
        assertThat(handshake("TLSv1.3")).isEqualTo("TLSv1.3");
    }

    @Test
    void unauthenticatedRequestOverTlsIsRejected() throws Exception {
        HttpResponse<String> response = https("/v1/config?warehouse=colvenna", null);
        assertThat(response.statusCode()).isEqualTo(401);
        assertThat(https("/v1/config?warehouse=colvenna", token).statusCode()).isEqualTo(200);
    }

    @Test
    void servingWithoutTlsIsRefusedUnlessPlainHttpIsAllowed() {
        ServerConfig plain =
                ServerConfig.fromEnvironment(
                        Map.of(
                                "COLVENNA_JDBC_URL", "jdbc:postgresql://localhost/x",
                                "COLVENNA_WAREHOUSE_NAME", "colvenna",
                                "COLVENNA_WAREHOUSE_ROOT", "file:///tmp/w"));
        assertThatThrownBy(plain::requireTlsForServing)
                .hasMessageContaining("COLVENNA_TLS_KEYSTORE");
    }

    @Test
    void plainHttpIsNotServedOnTheApiPort() {
        assertThatThrownBy(
                        () -> {
                            try (HttpClient client = HttpClient.newHttpClient()) {
                                client.send(
                                        HttpRequest.newBuilder(
                                                        URI.create(
                                                                "http://localhost:"
                                                                        + server.port()
                                                                        + "/v1/config?warehouse=colvenna"))
                                                .header("Authorization", "Bearer " + token)
                                                .timeout(Duration.ofSeconds(10))
                                                .build(),
                                        HttpResponse.BodyHandlers.ofString());
                            }
                        })
                .isInstanceOf(java.io.IOException.class);
    }

    /**
     * The protocol a handshake with the API port settles on, offering only those given (if any).
     */
    private String handshake(String... protocols) throws Exception {
        try (SSLSocket socket =
                (SSLSocket)
                        trusting().getSocketFactory().createSocket("localhost", server.port())) {
            if (protocols.length > 0) {
                socket.setEnabledProtocols(protocols);
            }
            socket.startHandshake();
            return socket.getSession().getProtocol();
        }
    }

    private SSLContext trusting() throws Exception {
        KeyStore trust = KeyStore.getInstance("PKCS12");
        try (InputStream in = Files.newInputStream(trustStore)) {
            trust.load(in, PASSWORD.toCharArray());
        }
        TrustManagerFactory factory =
                TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        factory.init(trust);
        SSLContext context = SSLContext.getInstance("TLSv1.3");
        context.init(null, factory.getTrustManagers(), null);
        return context;
    }

    private HttpResponse<String> https(String path, String bearer) throws Exception {
        SSLContext context = trusting();
        HttpRequest.Builder request =
                HttpRequest.newBuilder(URI.create("https://localhost:" + server.port() + path));
        if (bearer != null) {
            request.header("Authorization", "Bearer " + bearer);
        }
        try (HttpClient client = HttpClient.newBuilder().sslContext(context).build()) {
            return client.send(request.build(), HttpResponse.BodyHandlers.ofString());
        }
    }

    private static void keytool(String... args) throws Exception {
        List<String> command = new java.util.ArrayList<>();
        command.add(Path.of(System.getProperty("java.home"), "bin", "keytool").toString());
        command.addAll(List.of(args));
        Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
        String output = new String(process.getInputStream().readAllBytes());
        assertThat(process.waitFor()).as(output).isZero();
    }
}
