package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.IntStream;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.data.BinaryString;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.data.InternalRow;
import org.apache.paimon.options.Options;
import org.apache.paimon.reader.RecordReader;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.table.Table;
import org.apache.paimon.table.sink.BatchTableCommit;
import org.apache.paimon.table.sink.BatchTableWrite;
import org.apache.paimon.table.sink.BatchWriteBuilder;
import org.apache.paimon.table.source.ReadBuilder;
import org.apache.paimon.types.DataTypes;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.S3Exception;
import software.amazon.awssdk.services.sts.StsClient;

/**
 * The create, commit and read cycle through the Paimon 2.0.0 client, then the evidence that the
 * table's data was reached with per-table credentials only: the driver's own role reaches no object
 * at all, and the credentials the catalog vends reach this table's root and nothing beside it.
 */
final class Cycle {

    private Cycle() {}

    static Map<String, Object> run(String uri, String warehouse, String token, JsonNode event)
            throws Exception {
        int rows = event.path("rows").asInt(100);
        boolean dataToken = event.path("dataToken").asBoolean(true);
        Map<String, Object> out = new LinkedHashMap<>();
        Map<String, Long> timings = new LinkedHashMap<>();
        String database = Driver.unique("drv_cycle");
        Identifier events = Identifier.create(database, "events");
        Identifier keyed = Identifier.create(database, "keyed");
        out.put("database", database);
        out.put("dataTokenEnabled", dataToken);

        Options options = new Options();
        options.setString("uri", uri);
        options.setString("warehouse", warehouse);
        options.setString("token.provider", "bear");
        options.setString("token", token);
        options.setString("data-token.enabled", String.valueOf(dataToken));
        long start = System.nanoTime();
        try (RESTCatalog catalog = new RESTCatalog(CatalogContext.create(options))) {
            timings.put("openCatalogMs", millis(start));
            start = System.nanoTime();
            catalog.createDatabase(database, false);
            catalog.createTable(
                    events,
                    Schema.newBuilder()
                            .column("id", DataTypes.INT())
                            .column("payload", DataTypes.STRING())
                            .build(),
                    false);
            catalog.createTable(
                    keyed,
                    Schema.newBuilder()
                            .column("id", DataTypes.INT().notNull())
                            .column("payload", DataTypes.STRING())
                            .primaryKey("id")
                            .option("bucket", "1")
                            .build(),
                    false);
            timings.put("createDatabaseAndTablesMs", millis(start));

            int[] ids = IntStream.rangeClosed(1, rows).toArray();
            start = System.nanoTime();
            write(catalog.getTable(events), "v", ids);
            write(catalog.getTable(keyed), "v", ids);
            // A second commit on the keyed table updates the first half of its rows.
            write(catalog.getTable(keyed), "u", IntStream.rangeClosed(1, rows / 2).toArray());
            timings.put("writeAndCommitMs", millis(start));

            start = System.nanoTime();
            List<String> readEvents = read(catalog.getTable(events));
            List<String> readKeyed = read(catalog.getTable(keyed));
            timings.put("readMs", millis(start));
            List<String> expectedEvents = new ArrayList<>();
            List<String> expectedKeyed = new ArrayList<>();
            for (int id : ids) {
                expectedEvents.add(id + "=v" + id);
                expectedKeyed.add(id + "=" + (id <= rows / 2 ? "u" : "v") + id);
            }
            Collections.sort(expectedEvents);
            Collections.sort(expectedKeyed);
            out.put("rowsWritten", rows);
            out.put("eventsRowsRead", readEvents.size());
            out.put("eventsMatch", readEvents.equals(expectedEvents));
            out.put("keyedRowsRead", readKeyed.size());
            out.put("keyedMatch", readKeyed.equals(expectedKeyed));
            out.put("eventsSnapshot", snapshot(catalog.getTable(events)));
            out.put("keyedSnapshot", snapshot(catalog.getTable(keyed)));
            out.put("tables", catalog.listTables(database));

            if (event.path("tokenCheck").asBoolean(dataToken)) {
                out.put(
                        "perTableCredentials",
                        credentials(new Api(uri), warehouse, token, database));
            }
            if (!event.path("keep").asBoolean(false)) {
                start = System.nanoTime();
                catalog.dropDatabase(database, false, true);
                timings.put("dropMs", millis(start));
                out.put("dropped", true);
            }
        }
        out.put("timings", timings);
        return out;
    }

    private static void write(Table table, String tag, int... ids) throws Exception {
        BatchWriteBuilder builder = table.newBatchWriteBuilder();
        try (BatchTableWrite write = builder.newWrite();
                BatchTableCommit commit = builder.newCommit()) {
            for (int id : ids) {
                write.write(GenericRow.of(id, BinaryString.fromString(tag + id)));
            }
            commit.commit(write.prepareCommit());
        }
    }

    /** Every row as "id=payload", sorted. */
    static List<String> read(Table table) throws Exception {
        ReadBuilder read = table.newReadBuilder();
        List<String> rows = new ArrayList<>();
        try (RecordReader<InternalRow> reader =
                read.newRead().createReader(read.newScan().plan().splits())) {
            reader.forEachRemaining(row -> rows.add(row.getInt(0) + "=" + row.getString(1)));
        }
        Collections.sort(rows);
        return rows;
    }

    private static Object snapshot(Table table) {
        return table.latestSnapshot()
                .map(s -> Map.of("id", s.id(), "commitKind", String.valueOf(s.commitKind())))
                .orElse(null);
    }

    /**
     * The events table's credentials as the client gets them, used directly: who they are (STS),
     * and what they reach. Only the access key's first four characters are reported.
     */
    private static Map<String, Object> credentials(
            Api api, String warehouse, String token, String database) throws Exception {
        String tables = "/v1/" + warehouse + "/databases/" + database + "/tables/";
        Api.Answer vended = api.bearer("GET", tables + "events/token", token, null);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tokenStatus", vended.status());
        if (vended.status() != 200) {
            out.put("tokenBody", vended.summary().get("body"));
            return out;
        }
        JsonNode response = vended.json();
        JsonNode keys = response.path("token");
        long expiresAt = response.path("expiresAtMillis").asLong();
        out.put("expiresAt", Instant.ofEpochMilli(expiresAt).toString());
        out.put("secondsLeft", (expiresAt - System.currentTimeMillis()) / 1000);
        String accessKey = keys.path("fs.s3a.access.key").asText();
        out.put("accessKeyIdPrefix", accessKey.length() >= 4 ? accessKey.substring(0, 4) : "");
        Region region = Region.of(keys.path("fs.s3a.endpoint.region").asText());
        StaticCredentialsProvider provider =
                StaticCredentialsProvider.create(
                        AwsSessionCredentials.create(
                                accessKey,
                                keys.path("fs.s3a.secret.key").asText(),
                                keys.path("fs.s3a.session.token").asText()));
        try (StsClient sts =
                StsClient.builder().region(region).credentialsProvider(provider).build()) {
            // arn:aws:sts::<account>:assumed-role/<role>/<session>: the role and the session only.
            String[] arn = sts.getCallerIdentity().arn().split(":", 6);
            out.put("callerIdentity", arn.length == 6 ? arn[5] : "unparsed");
        }
        URI eventsRoot =
                URI.create(
                        api.bearer("GET", tables + "events", token, null)
                                .json()
                                .path("path")
                                .asText());
        URI keyedRoot =
                URI.create(
                        api.bearer("GET", tables + "keyed", token, null)
                                .json()
                                .path("path")
                                .asText());
        String bucket = eventsRoot.getHost();
        String root = eventsRoot.getPath().replaceFirst("^/", "");
        String probe = "drv-probe/" + UUID.randomUUID() + ".txt";
        Map<String, Object> s3 = new LinkedHashMap<>();
        try (S3Client client =
                S3Client.builder().region(region).credentialsProvider(provider).build()) {
            s3.put(
                    "putInTableRoot",
                    attempt(
                            () ->
                                    client.putObject(
                                            b -> b.bucket(bucket).key(root + "/" + probe),
                                            RequestBody.fromString("probe"))));
            s3.put(
                    "getInTableRoot",
                    attempt(
                            () ->
                                    client.getObjectAsBytes(
                                            b -> b.bucket(bucket).key(root + "/" + probe))));
            s3.put(
                    "listTableRoot",
                    attempt(() -> client.listObjectsV2(b -> b.bucket(bucket).prefix(root + "/"))));
            s3.put(
                    "deleteInTableRoot",
                    attempt(
                            () ->
                                    client.deleteObject(
                                            b -> b.bucket(bucket).key(root + "/" + probe))));
            s3.put(
                    "putInSiblingPrefix",
                    attempt(
                            () ->
                                    client.putObject(
                                            b -> b.bucket(bucket).key(root + "_x/" + probe),
                                            RequestBody.fromString("probe"))));
            s3.put(
                    "putInOtherTable",
                    attempt(
                            () ->
                                    client.putObject(
                                            b ->
                                                    b.bucket(bucket)
                                                            .key(
                                                                    keyedRoot
                                                                                    .getPath()
                                                                                    .replaceFirst(
                                                                                            "^/",
                                                                                            "")
                                                                            + "/"
                                                                            + probe),
                                            RequestBody.fromString("probe"))));
            String parent = root.substring(0, root.lastIndexOf('/') + 1);
            s3.put(
                    "listDatabasePrefix",
                    attempt(() -> client.listObjectsV2(b -> b.bucket(bucket).prefix(parent))));
        }
        out.put("s3", s3);
        return out;
    }

    private interface Call {
        Object call() throws Exception;
    }

    /** "allowed", "AccessDenied" (or another S3 error code), or the exception's class. */
    private static String attempt(Call call) {
        try {
            call.call();
            return "allowed";
        } catch (S3Exception e) {
            return e.awsErrorDetails() == null
                    ? "S3Exception " + e.statusCode()
                    : e.awsErrorDetails().errorCode();
        } catch (Exception e) {
            return e.getClass().getSimpleName();
        }
    }

    private static long millis(long start) {
        return (System.nanoTime() - start) / 1_000_000;
    }
}
