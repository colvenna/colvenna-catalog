package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

/**
 * The driver's actions. Every event carries the catalog's base URL ({@code uri}), the warehouse
 * name ({@code warehouse}) and the admin's bearer token ({@code token}), which no result contains.
 *
 * <ul>
 *   <li>{@code cycle}: create, commit and read with the Paimon 2.0.0 client ({@link Cycle});
 *   <li>{@code purges}: a table with many files, a rollback and a tag's and a branch's deletion,
 *       and what is left after the purge (#93, {@link Purges});
 *   <li>{@code refusals}: requests the entry must refuse, and the admin's that it must serve;
 *   <li>{@code cold}: {@code concurrency} simultaneous requests, to land on new environments;
 *   <li>{@code requests}, {@code peer}, {@code read}: the deployment cases' ({@link Checks}).
 *   <li>{@code sql}: statements on the metadata database ({@link Sql}), with no catalog URL or
 *       token.
 * </ul>
 *
 * <p>An event's optional {@code ca} (PEM) is a private CA the catalog's certificate is signed by,
 * trusted beside the JDK's CAs ({@link Trust}).
 */
final class Driver {

    private static final DateTimeFormatter STAMP =
            DateTimeFormatter.ofPattern("yyyyMMddHHmmss").withZone(ZoneOffset.UTC);

    private Driver() {}

    static Map<String, Object> handle(JsonNode event) throws Exception {
        String action = event.path("action").asText();
        if (action.equals("sql")) {
            // The UAT harness's: no catalog request, so no URL and no token (Sql).
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("action", action);
            out.put("startedAt", Instant.now().toString());
            out.putAll(Sql.run(event));
            return out;
        }
        String uri = required(event, "uri");
        String warehouse = required(event, "warehouse");
        String token = required(event, "token");
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("action", action);
        out.put("startedAt", Instant.now().toString());
        if (event.hasNonNull("ca")) {
            // A catalog behind a private CA (the EKS form's internal NLB): trusted beside the
            // JDK's.
            Trust.install(event.path("ca").asText());
        }
        switch (action) {
            case "cycle" -> out.putAll(Cycle.run(uri, warehouse, token, event));
            case "purges" -> out.putAll(Purges.run(uri, warehouse, token, event));
            case "requests" -> out.putAll(Checks.requests(new Api(uri), event));
            case "peer" -> out.putAll(Checks.peer(uri));
            case "read" -> out.putAll(Checks.read(uri, warehouse, token, event));
            case "refusals" -> out.putAll(refusals(new Api(uri), warehouse, token));
            case "cold" ->
                    out.putAll(
                            cold(
                                    new Api(uri),
                                    warehouse,
                                    token,
                                    event.path("concurrency").asInt(5)));
            default -> throw new IllegalArgumentException("Unknown action " + action);
        }
        return out;
    }

    /** A name unique to this run: a prefix, the UTC time and a few random hex digits. */
    static String unique(String prefix) {
        return prefix
                + "_"
                + STAMP.format(Instant.now())
                + "_"
                + UUID.randomUUID().toString().substring(0, 6);
    }

    private static String required(JsonNode event, String field) {
        String value = event.path(field).asText("");
        if (value.isEmpty()) {
            throw new IllegalArgumentException("The event needs " + field);
        }
        return value;
    }

    static String json(Object value) throws Exception {
        return DriverMain.JSON.writeValueAsString(value);
    }

    private static Map<String, Object> refusals(Api api, String warehouse, String admin)
            throws Exception {
        String config = "/v1/config?warehouse=" + warehouse;
        String prefix = "/v1/" + warehouse;
        String forged = "colvenna_" + UUID.randomUUID().toString().replace("-", "") + "forged";
        Map<String, Api.Answer> cases = new LinkedHashMap<>();
        cases.put("noAuthorizationConfig", api.call("GET", config, null, null));
        cases.put("noAuthorizationUnknownRoute", api.call("GET", "/nothing-here", null, null));
        cases.put(
                "noAuthorizationPostCommit",
                api.call("POST", prefix + "/databases/default/tables/t1/commit", null, "{}"));
        cases.put("unknownBearerToken", api.bearer("GET", config, forged, null));
        cases.put("basicScheme", api.call("GET", config, "Basic dXNlcjpwYXNzd29yZA==", null));
        cases.put("emptyBearer", api.call("GET", config, "Bearer ", null));
        cases.put("lowercaseBearerScheme", api.call("GET", config, "bearer " + admin, null));

        // A principal with no grants, two tokens of it (one revoked at once), and a database it
        // cannot see.
        String principal = unique("drv-nogrant").replace('_', '-');
        String database = unique("drv_refusals");
        List<String> tokenIds = new ArrayList<>();
        Map<String, Object> setup = new LinkedHashMap<>();
        setup.put(
                "createPrincipal",
                api.bearer(
                                "POST",
                                "/colvenna/admin/v1/principals",
                                admin,
                                json(
                                        Map.of(
                                                "name",
                                                principal,
                                                "description",
                                                "serverless live test, no grants")))
                        .status());
        setup.put(
                "createDatabase",
                api.bearer(
                                "POST",
                                prefix + "/databases",
                                admin,
                                json(Map.of("name", database, "options", Map.of())))
                        .status());
        String plain = issue(api, admin, principal, tokenIds);
        String revoked = issue(api, admin, principal, tokenIds);
        setup.put(
                "revoke",
                api.bearer("DELETE", "/colvenna/admin/v1/tokens/" + tokenIds.get(1), admin, null)
                        .status());
        try {
            cases.put("revokedToken", api.bearer("GET", config, revoked, null));
            cases.put(
                    "validTokenWithoutGrantOnDatabase",
                    api.bearer("GET", prefix + "/databases/" + database, plain, null));
            cases.put(
                    "validTokenWithoutGrantCreateTable",
                    api.bearer(
                            "POST",
                            prefix + "/databases/" + database + "/tables",
                            plain,
                            json(
                                    Map.of(
                                            "identifier",
                                            Map.of("database", database, "object", "t1"),
                                            "schema",
                                            Map.of(
                                                    "fields",
                                                    List.of(
                                                            Map.of(
                                                                    "id", 0, "name", "id", "type",
                                                                    "INT")),
                                                    "partitionKeys",
                                                    List.of(),
                                                    "primaryKeys",
                                                    List.of(),
                                                    "options",
                                                    Map.of())))));
            cases.put(
                    "nonAdminOnAdminApi",
                    api.bearer("GET", "/colvenna/admin/v1/principals", plain, null));
            cases.put("validTokenWithoutGrantConfig", api.bearer("GET", config, plain, null));
            cases.put("adminConfig", api.bearer("GET", config, admin, null));
            cases.put(
                    "adminDatabase",
                    api.bearer("GET", prefix + "/databases/" + database, admin, null));
            setup.put(
                    "disablePrincipal",
                    api.bearer(
                                    "POST",
                                    "/colvenna/admin/v1/principals/" + principal + "/disable",
                                    admin,
                                    null)
                            .status());
            // The token was used just before: each execution environment caches its principal for
            // COLVENNA_AUTH_CACHE_TTL (2 s), and the gateway's authorizer cache may still admit it,
            // but every server checks again once its own cache has run out.
            Thread.sleep(3000);
            cases.put("disabledPrincipalToken", api.bearer("GET", config, plain, null));
        } finally {
            for (String id : tokenIds) {
                api.bearer("DELETE", "/colvenna/admin/v1/tokens/" + id, admin, null);
            }
            setup.put(
                    "dropDatabase",
                    api.bearer("DELETE", prefix + "/databases/" + database, admin, null).status());
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("principal", principal);
        out.put("database", database);
        out.put("setup", setup);
        Map<String, Object> results = new LinkedHashMap<>();
        cases.forEach((name, answer) -> results.put(name, answer.summary()));
        out.put("cases", results);
        return out;
    }

    /** A one-hour token of the principal, its id kept for revoking; the token never leaves here. */
    private static String issue(Api api, String admin, String principal, List<String> ids)
            throws Exception {
        Api.Answer answer =
                api.bearer(
                        "POST",
                        "/colvenna/admin/v1/principals/" + principal + "/tokens",
                        admin,
                        json(Map.of("ttl", "PT1H", "description", "serverless live test")));
        if (answer.status() != 200) {
            throw new IllegalStateException(
                    "Issuing a token for " + principal + " got " + answer.status());
        }
        JsonNode issued = answer.json();
        ids.add(issued.path("id").asText());
        return issued.path("token").asText();
    }

    private static Map<String, Object> cold(
            Api api, String warehouse, String token, int concurrency) throws Exception {
        String path = "/v1/config?warehouse=" + warehouse;
        ExecutorService pool = Executors.newFixedThreadPool(concurrency);
        List<Future<Api.Answer>> sent = new ArrayList<>();
        for (int i = 0; i < concurrency; i++) {
            sent.add(pool.submit(() -> api.bearer("GET", path, token, null)));
        }
        List<Map<String, Object>> answers = new ArrayList<>();
        for (Future<Api.Answer> future : sent) {
            Api.Answer answer = future.get();
            Map<String, Object> one = new LinkedHashMap<>();
            one.put("status", answer.status());
            one.put("ms", answer.millis());
            answers.add(one);
        }
        pool.shutdown();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("concurrency", concurrency);
        out.put("requests", answers);
        out.put(
                "followUp",
                api.bearer("GET", "/v1/" + warehouse + "/databases", token, null).summary());
        return out;
    }
}
