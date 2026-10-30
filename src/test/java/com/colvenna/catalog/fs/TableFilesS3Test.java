package com.colvenna.catalog.fs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
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
 * GetObject} per file, one {@code DeleteObjects} per thousand; confined to the table's root. So
 * does the removal of an empty database directory's marker (#13), one {@code ListObjectsV2} and one
 * {@code DeleteObject}. A small stand-in for S3 answers them here.
 */
class TableFilesS3Test {

    private static final String ROOT = "s3://bucket/warehouse/db.db/t";
    private static final Pattern KEY = Pattern.compile("<Key>(.*?)</Key>");

    private final Map<String, String> objects = new ConcurrentHashMap<>();
    private final List<Integer> deleteRequests = new ArrayList<>();
    private final List<String> requests = Collections.synchronizedList(new ArrayList<>());
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
                    String query = exchange.getRequestURI().getQuery();
                    requests.add(exchange.getRequestMethod() + " " + path);
                    byte[] body;
                    int status;
                    if (exchange.getRequestMethod().equals("GET")
                            && query != null
                            && query.contains("list-type=2")) {
                        status = 200;
                        body = list(query).getBytes(StandardCharsets.UTF_8);
                    } else if (exchange.getRequestMethod().equals("DELETE")) {
                        objects.remove(path.substring("/bucket/".length()));
                        exchange.sendResponseHeaders(204, -1);
                        exchange.close();
                        return;
                    } else if (exchange.getRequestMethod().equals("GET")) {
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

    /** ListObjectsV2 of the keys under {@code prefix=}, at most {@code max-keys=}, in key order. */
    private String list(String query) {
        Map<String, String> params = new HashMap<>();
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            params.put(
                    pair.substring(0, eq),
                    URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8));
        }
        String prefix = params.getOrDefault("prefix", "");
        int max = Integer.parseInt(params.getOrDefault("max-keys", "1000"));
        List<String> keys =
                objects.keySet().stream().filter(k -> k.startsWith(prefix)).sorted().toList();
        StringBuilder xml =
                new StringBuilder(
                        "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                                + "<Name>bucket</Name><Prefix>"
                                + prefix
                                + "</Prefix><KeyCount>"
                                + Math.min(max, keys.size())
                                + "</KeyCount><MaxKeys>"
                                + max
                                + "</MaxKeys><IsTruncated>"
                                + (keys.size() > max)
                                + "</IsTruncated>");
        for (String key : keys.subList(0, Math.min(max, keys.size()))) {
            xml.append("<Contents><Key>")
                    .append(key)
                    .append("</Key><Size>")
                    .append(objects.get(key).length())
                    .append("</Size></Contents>");
        }
        return xml.append("</ListBucketResult>").toString();
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

    @Test
    void anEmptyDatabaseDirectoryLosesItsMarkerThroughTheS3Api() throws Exception {
        // S3A's marker, all that is left of the directory once its last table root is purged (#13).
        objects.put("warehouse/db.db/", "");
        objects.put("warehouse/db.dbx/kept", "x");
        assertThat(files.removeIfEmpty("s3://bucket/warehouse/db.db")).isTrue();
        assertThat(objects).containsOnlyKeys("warehouse/db.dbx/kept");
        // The S3 API alone: S3A would write a marker for the warehouse in turn.
        assertThat(requests).containsExactly("GET /bucket", "DELETE /bucket/warehouse/db.db/");
        // Nothing there at all: nothing to do.
        requests.clear();
        assertThat(files.removeIfEmpty("s3://bucket/warehouse/db.db")).isTrue();
        assertThat(requests).containsExactly("GET /bucket");
    }

    @Test
    void aDatabaseDirectoryWithAnythingInItStays() throws Exception {
        objects.put("warehouse/db.db/", "");
        objects.put("warehouse/db.db/t2/schema/schema-0", "{}");
        assertThat(files.removeIfEmpty("s3://bucket/warehouse/db.db")).isFalse();
        // A marker that is not empty is no marker.
        objects.clear();
        objects.put("warehouse/db.db/", "x");
        assertThat(files.removeIfEmpty("s3://bucket/warehouse/db.db")).isFalse();
        assertThat(objects).containsOnlyKeys("warehouse/db.db/");
        assertThat(requests).allMatch(r -> r.startsWith("GET "));
    }
}
