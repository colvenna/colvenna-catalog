package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.ServerConfig;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * The server's own S3A client sends to the install's Region only (#9).
 *
 * <p>Paimon's S3 FileIO hands S3A {@code s3.region} as {@code fs.s3a.region}, a key S3A 3.4 does
 * not read. Without {@code fs.s3a.endpoint.region} (or an endpoint) S3A sends to us-east-2 and
 * relies on cross-region redirects, which a VPC without internet egress cannot follow: its S3
 * gateway endpoint is available in its own Region only. Both forms therefore set {@code
 * COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION} to the install's Region (the chart's ConfigMap, the
 * serverless stack); {@code serve} refuses an S3 warehouse without it (LambdaListenerTest).
 *
 * <p>A stand-in HTTP proxy records the host each of S3A's connections is for, and refuses it, so
 * nothing leaves this host: S3A reaches S3 over HTTPS, so through a proxy it first asks for a
 * tunnel to the host it means, {@code CONNECT <host>:443}.
 */
class S3aRegionTest {

    private static final String WAREHOUSE = "s3://bucket/warehouse";

    /**
     * The JVM loads its name resolver at its first lookup, through the thread's context class
     * loader. Here that would be S3A's, inside Paimon's plugin class loader, where it fails (the
     * plugin's dnsjava provider is listed but not loadable); in the server the database's lookup
     * comes first.
     */
    @BeforeAll
    static void loadTheResolver() throws IOException {
        InetAddress.getByName("localhost");
    }

    /** The settings of both forms that concern S3A's Region, and a warehouse on S3. */
    private static Map<String, String> asInstalled(String region) {
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_JDBC_URL", "jdbc:postgresql://localhost/x");
        env.put("COLVENNA_WAREHOUSE_NAME", "colvenna");
        env.put("COLVENNA_WAREHOUSE_ROOT", WAREHOUSE);
        env.put("COLVENNA_FILEIO_S3_REGION", region);
        env.put("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION", region);
        return env;
    }

    @Test
    void theServersS3aClientSendsToTheInstallsRegionOnly() throws Exception {
        for (String region : List.of("us-west-2", "eu-central-1")) {
            assertThat(hostsS3aConnectsTo(asInstalled(region)))
                    .as(region)
                    .isNotEmpty()
                    .allMatch(host -> host.equals("bucket.s3." + region + ".amazonaws.com:443"));
        }
    }

    @Test
    void withTheRegionOnlyAsS3RegionS3aSendsToUsEast2() throws Exception {
        // The chart's settings before #9: S3A finds no Region of its own and falls back.
        Map<String, String> env = asInstalled("us-west-2");
        env.remove("COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION");
        assertThat(hostsS3aConnectsTo(env))
                .isNotEmpty()
                .allMatch(host -> host.equals("bucket.s3.us-east-2.amazonaws.com:443"));
    }

    /**
     * The hosts the server's S3A client connects to for one status lookup under the warehouse, with
     * the FileIO settings {@code env} gives the server, through the stand-in proxy, with static
     * keys and without retries.
     */
    private static List<String> hostsS3aConnectsTo(Map<String, String> env) throws Exception {
        try (RecordingProxy proxy = new RecordingProxy()) {
            Map<String, String> settings = new HashMap<>(env);
            settings.put(
                    "COLVENNA_FILEIO_OPTIONS",
                    String.join(
                            ",",
                            "fs.s3a.proxy.host=127.0.0.1",
                            "fs.s3a.proxy.port=" + proxy.port(),
                            "fs.s3a.aws.credentials.provider="
                                    + "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
                            "fs.s3a.access.key=AKIAEXAMPLE",
                            "fs.s3a.secret.key=secret",
                            "fs.s3a.attempts.maximum=0",
                            "fs.s3a.retry.limit=0",
                            "fs.s3a.retry.throttle.limit=0"));
            TableFiles files =
                    new TableFiles(ServerConfig.fromEnvironment(settings).fileIoOptions());
            assertThatThrownBy(() -> files.exists(WAREHOUSE + "/db.db"))
                    .as("the proxy refuses the tunnel, which S3A reports as S3's 403")
                    .hasMessageContaining("403");
            return proxy.targets();
        }
    }

    /** An HTTP proxy that records each tunnel's target and refuses it. */
    private static final class RecordingProxy implements AutoCloseable {
        private final ServerSocket socket =
                new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
        private final List<String> targets = new ArrayList<>();
        private final Thread acceptor = Thread.ofVirtual().start(this::serve);

        RecordingProxy() throws IOException {}

        int port() {
            return socket.getLocalPort();
        }

        synchronized List<String> targets() {
            return List.copyOf(targets);
        }

        private void serve() {
            while (!socket.isClosed()) {
                try (Socket client = socket.accept()) {
                    BufferedReader in =
                            new BufferedReader(
                                    new InputStreamReader(
                                            client.getInputStream(), StandardCharsets.US_ASCII));
                    String request = in.readLine();
                    if (request != null) {
                        // CONNECT <host>:443 HTTP/1.1, or the request itself if it is no tunnel
                        String[] parts = request.split(" ");
                        synchronized (this) {
                            targets.add(parts[0].equals("CONNECT") ? parts[1] : request);
                        }
                    }
                    for (String header = in.readLine();
                            header != null && !header.isEmpty();
                            header = in.readLine()) {
                        // the request's headers, unread
                    }
                    OutputStream out = client.getOutputStream();
                    out.write(
                            "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                                    .getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                } catch (IOException e) {
                    if (socket.isClosed()) {
                        return;
                    }
                }
            }
        }

        @Override
        public void close() throws Exception {
            socket.close();
            acceptor.join();
        }
    }
}
