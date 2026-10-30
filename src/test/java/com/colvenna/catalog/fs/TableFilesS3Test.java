package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.paimon.fs.Path;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * On S3, the reads and deletions of many files (#93) go through the S3 API itself: one {@code
 * GetObject} per file, one {@code DeleteObjects} per thousand; confined to the table's root. A
 * small stand-in for S3 answers them here.
 */
class TableFilesS3Test {

    private static final String ROOT = "s3://bucket/warehouse/db.db/t";
    private static final Pattern KEY = Pattern.compile("<Key>(.*?)</Key>");

    private final Map<String, String> objects = new ConcurrentHashMap<>();
    private final List<Integer> deleteRequests = new ArrayList<>();
    private volatile String refused;
    private HttpServer server;
    private SnapshotPublisher.S3ClientFactory s3;
    private TableFiles files;

    @BeforeEach
    void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext(
                "/",
                exchange -> {
                    String path = exchange.getRequestURI().getPath();
                    byte[] body;
                    int status;
                    if (exchange.getRequestMethod().equals("GET")) {
                        String content = objects.get(path.substring("/bucket/".length()));
                        status = content == null ? 404 : 200;
                        body =
                                (content == null
                                                ? "<Error><Code>NoSuchKey</Code><Message>none</Message></Error>"
                                                : content)
                                        .getBytes(StandardCharsets.UTF_8);
                    } else {
                        String request =
                                new String(
                                        exchange.getRequestBody().readAllBytes(),
                                        StandardCharsets.UTF_8);
                        Matcher keys = KEY.matcher(request);
                        StringBuilder errors = new StringBuilder();
                        int count = 0;
                        while (keys.find()) {
                            count++;
                            if (keys.group(1).equals(refused)) {
                                errors.append("<Error><Key>")
                                        .append(keys.group(1))
                                        .append("</Key><Code>AccessDenied</Code>")
                                        .append("<Message>Access Denied</Message></Error>");
                            } else {
                                objects.remove(keys.group(1));
                            }
                        }
                        synchronized (deleteRequests) {
                            deleteRequests.add(count);
                        }
                        status = 200;
                        body =
                                ("<DeleteResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                                                + errors
                                                + "</DeleteResult>")
                                        .getBytes(StandardCharsets.UTF_8);
                    }
                    exchange.getResponseHeaders().add("Content-Type", "application/xml");
                    exchange.sendResponseHeaders(status, body.length);
                    exchange.getResponseBody().write(body);
                    exchange.close();
                });
        server.start();
        Map<String, String> options =
                Map.of(
                        "s3.endpoint", "http://127.0.0.1:" + server.getAddress().getPort(),
                        "s3.region", "us-west-2",
                        "s3.path.style.access", "true",
                        "s3.access-key", "AKIAEXAMPLE",
                        "s3.secret-key", "secret");
        s3 =
                new SnapshotPublisher.S3ClientFactory(
                        options, Duration.ofSeconds(8), Duration.ofSeconds(3));
        files = new TableFiles(options, s3);
    }

    @AfterEach
    void stop() {
        s3.close();
        server.stop(0);
    }

    @Test
    void manyFilesAreDeletedAThousandToARequest() throws Exception {
        List<Path> paths = new ArrayList<>();
        for (int i = 0; i < 2500; i++) {
            String key = "warehouse/db.db/t/bucket-0/data-" + i + ".orc";
            objects.put(key, "x");
            paths.add(new Path("s3://bucket/" + key));
        }
        objects.put("warehouse/db.db/t/bucket-0/kept.orc", "x");
        files.deleteAll(ROOT, paths);
        assertThat(objects).containsOnlyKeys("warehouse/db.db/t/bucket-0/kept.orc");
        assertThat(deleteRequests).containsExactlyInAnyOrder(1000, 1000, 500);
        // An object already gone is deleted.
        files.deleteAll(ROOT, paths.subList(0, 3));
    }

    @Test
    void anObjectS3RefusesFailsTheDeletion() {
        objects.put("warehouse/db.db/t/snapshot/snapshot-1", "{}");
        refused = "warehouse/db.db/t/snapshot/snapshot-1";
        assertThatThrownBy(
                        () ->
                                files.deleteAll(
                                        ROOT, List.of(new Path(ROOT + "/snapshot/snapshot-1"))))
                .hasMessageContaining("Could not delete 1 of 1")
                .hasMessageContaining("AccessDenied");
    }

    @Test
    void nothingOutsideTheTableRootIsTouched() {
        objects.put("warehouse/db.db/other/data-1.orc", "x");
        assertThatThrownBy(
                        () ->
                                files.deleteAll(
                                        ROOT,
                                        List.of(
                                                new Path(
                                                        "s3://bucket/warehouse/db.db/other/data-1.orc"))))
                .hasMessageContaining("outside the table root");
        assertThat(objects).containsKey("warehouse/db.db/other/data-1.orc");
        assertThat(deleteRequests).isEmpty();
    }

    @Test
    void rootsAreReadOneRequestEachAndOneGoneIsLeftOut() throws Exception {
        objects.put("warehouse/db.db/t/snapshot/snapshot-1", "{\"id\":1}");
        Roots.RootFile one =
                new Roots.RootFile(
                        "main",
                        Roots.Kind.SNAPSHOT,
                        "snapshot-1",
                        1L,
                        new Path(ROOT + "/snapshot/snapshot-1"));
        Roots.RootFile gone =
                new Roots.RootFile(
                        "main",
                        Roots.Kind.SNAPSHOT,
                        "snapshot-2",
                        2L,
                        new Path(ROOT + "/snapshot/snapshot-2"));
        assertThat(files.read(ROOT, List.of(one, gone)))
                .containsExactly(Map.entry(one, "{\"id\":1}"));
    }
}
