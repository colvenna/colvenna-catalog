package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Table;

/**
 * The deployment cases' actions (#52): requests made as given, the certificate the client verified,
 * and tables read back as they are.
 *
 * <ul>
 *   <li>{@code requests}: each of {@code requests} ({@code name}, {@code method}, {@code path},
 *       {@code token} (a name in the event's {@code tokens}) or a raw {@code authorization}, and a
 *       JSON {@code body}), its status and the start of its answer;
 *   <li>{@code peer}: the TLS handshake with the catalog's host and port, the server's name
 *       checked: whether it was verified, and the names its certificate holds;
 *   <li>{@code read}: of {@code database} and each of {@code tables}, the rows (count and the
 *       SHA-256 of them sorted) and the snapshots' ids.
 * </ul>
 */
final class Checks {

    private Checks() {}

    static Map<String, Object> requests(Api api, JsonNode event) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (JsonNode r : event.path("requests")) {
            String authorization = null;
            if (r.hasNonNull("token")) {
                String name = r.path("token").asText();
                JsonNode token = event.path("tokens").path(name);
                if (!token.isTextual() || token.asText().isEmpty()) {
                    throw new IllegalArgumentException("The event's tokens hold no token " + name);
                }
                authorization = "Bearer " + token.asText();
            } else if (r.hasNonNull("authorization")) {
                authorization = r.path("authorization").asText();
            }
            String body = r.hasNonNull("body") ? r.path("body").toString() : null;
            Api.Answer answer =
                    api.call(
                            r.path("method").asText("GET"),
                            r.path("path").asText(),
                            authorization,
                            body);
            out.put(r.path("name").asText(), answer.summary());
        }
        return Map.of("requests", out);
    }

    static Map<String, Object> peer(String uri) {
        URI u = URI.create(uri);
        int port = u.getPort() < 0 ? 443 : u.getPort();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("host", u.getHost());
        out.put("port", port);
        try (SSLSocket socket =
                (SSLSocket)
                        SSLContext.getDefault()
                                .getSocketFactory()
                                .createSocket(u.getHost(), port)) {
            SSLParameters parameters = socket.getSSLParameters();
            parameters.setEndpointIdentificationAlgorithm("HTTPS");
            socket.setSSLParameters(parameters);
            socket.startHandshake();
            X509Certificate cert = (X509Certificate) socket.getSession().getPeerCertificates()[0];
            List<String> names = new ArrayList<>();
            if (cert.getSubjectAlternativeNames() != null) {
                for (List<?> name : cert.getSubjectAlternativeNames()) {
                    names.add(String.valueOf(name.get(1)));
                }
            }
            out.put("verified", true);
            out.put("names", names);
        } catch (Exception e) {
            out.put("verified", false);
            out.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        return out;
    }

    static Map<String, Object> read(String uri, String warehouse, String token, JsonNode event)
            throws Exception {
        Options options = new Options();
        options.setString("uri", uri);
        options.setString("warehouse", warehouse);
        options.setString("token.provider", "bear");
        options.setString("token", token);
        options.setString("data-token.enabled", "true");
        String database = event.path("database").asText();
        Map<String, Object> tables = new LinkedHashMap<>();
        try (RESTCatalog catalog = new RESTCatalog(CatalogContext.create(options))) {
            for (JsonNode name : event.path("tables")) {
                Table table = catalog.getTable(Identifier.create(database, name.asText()));
                List<String> rows = Cycle.read(table);
                MessageDigest sha = MessageDigest.getInstance("SHA-256");
                for (String row : rows) {
                    sha.update((row + "\n").getBytes(StandardCharsets.UTF_8));
                }
                List<Long> snapshots = new ArrayList<>();
                if (table instanceof FileStoreTable store) {
                    for (Snapshot s : store.snapshotManager().safelyGetAllSnapshots()) {
                        snapshots.add(s.id());
                    }
                    snapshots.sort(null); // the listing's order is the file system's
                }
                Map<String, Object> one = new LinkedHashMap<>();
                one.put("rows", rows.size());
                one.put("sha256", HexFormat.of().formatHex(sha.digest()));
                one.put("snapshots", snapshots);
                tables.put(name.asText(), one);
            }
        }
        return Map.of("database", database, "tables", tables);
    }
}
