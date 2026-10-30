package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;

import com.colvenna.catalog.testing.ServerTestBase;
import com.colvenna.catalog.testing.Tables;
import java.net.URLEncoder;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.dataformat.yaml.YAMLMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * The admin API's OpenAPI description, {@code docs/api/admin-v1.yaml}, against the server: the same
 * routes, each named by the audit action it records ({@code x-colvenna-action}); the same request
 * fields and query parameters; and every operation, run with the description's own examples,
 * answering with a documented status and a body of the documented shape, each write recorded
 * against a resource. Change the routes and the description together.
 *
 * <p>The routes' actions, and the request fields and query parameters each reads, are found in
 * {@code AdminRoutes}' source, as literals: {@code Route.read("ACTION", ...)}, the reads of the
 * request's JSON {@code body} ({@code text(body, "name")}, {@code body.path("ttl")}) and {@code
 * param("principal")}, each up to the next route.
 */
class AdminOpenApiTest extends ServerTestBase {

    private static final Path SPEC = Path.of("docs/api/admin-v1.yaml");
    private static final Path ROUTES =
            Path.of("src/main/java/com/colvenna/catalog/http/AdminRoutes.java");
    private static final String BASE = "/colvenna/admin/v1";

    /** The methods an OpenAPI path item can hold but {@code trace}, which no client sends here. */
    private static final List<String> METHODS =
            List.of("GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH");

    /** The action each route records: {@code Route.read("LIST_PRINCIPALS", ...)}. */
    private static final Pattern ACTION =
            Pattern.compile("Route\\.(?:read|write)\\(\\s*\"([A-Z_]+)\"");

    /** The action each write route records: {@code Route.write("CREATE_PRINCIPAL", ...)}. */
    private static final Pattern WRITE_ACTION =
            Pattern.compile("Route\\.write\\(\\s*\"([A-Z_]+)\"");

    /**
     * The request body fields a handler reads: {@code text(body, "name")}, {@code body.path(..)}.
     */
    private static final Pattern BODY_FIELD =
            Pattern.compile(
                    "(?:text|optional)\\(\\s*body,\\s*\"(\\w+)\""
                            + "|body\\s*\\.(?:path|get|hasNonNull)\\(\\s*\"(\\w+)\"\\)");

    /**
     * The request body fields a handler refuses to go without: {@code text(body, "name")}, and an
     * enum parsed from {@code body.path("scope").asText(null)}.
     */
    private static final Pattern REQUIRED_FIELD =
            Pattern.compile(
                    "\\btext\\(\\s*body,\\s*\"(\\w+)\""
                            + "|parse\\(\\s*[\\w.]+\\.class,\\s*\"\\w+\",\\s*body\\.path\\(\\s*\"(\\w+)\"\\)"
                            + "\\.asText\\(null\\)\\)");

    /** The query parameters a route reads: {@code q.param("principal")}. */
    private static final Pattern QUERY_PARAMETER = Pattern.compile("\\bparam\\(\\s*\"(\\w+)\"\\)");

    /** One operation of the description: a method on a path. */
    private record Operation(String method, String path, JsonNode node, JsonNode pathItem) {

        String id() {
            return node.path("operationId").asText();
        }

        String action() {
            return node.path("x-colvenna-action").asText();
        }

        @Override
        public String toString() {
            return method + " " + path + " (" + id() + ")";
        }
    }

    private JsonNode spec;

    @BeforeEach
    void readSpec() throws Exception {
        spec = new YAMLMapper().readTree(Files.readString(SPEC));
    }

    @Test
    void everyRouteIsDocumentedOnceAndNothingElseIs() throws Exception {
        List<String> routes = all(ACTION, Files.readString(ROUTES));
        assertThat(routes).as("actions of AdminRoutes").hasSizeGreaterThan(10);
        assertThat(routes).as("one route per action").doesNotHaveDuplicates();
        List<String> documented = operations().stream().map(Operation::action).toList();
        assertThat(documented).as("x-colvenna-action of each operation").doesNotHaveDuplicates();
        assertThat(documented).containsExactlyInAnyOrderElementsOf(routes);
        assertThat(operations().stream().map(Operation::id).toList()).doesNotHaveDuplicates();
    }

    @Test
    void everyDocumentedOperationIsTheRouteItNamesAndNeedsAnAdmin() throws Exception {
        String service = issueToken("reader", "P1D").get("token").asText();
        for (Operation op : operations()) {
            String path = BASE + concrete(op, Map.of());
            assertThat(op.node().path("responses").has("401")).as(op + " documents 401").isTrue();
            assertThat(op.node().path("responses").has("403")).as(op + " documents 403").isTrue();

            HttpResponse<String> anonymous = http(op.method(), path, null, null);
            assertThat(anonymous.statusCode()).as(op + " without a token").isEqualTo(401);
            assertConforms(op + " 401", json(anonymous.body()), schemaRef("ErrorResponse"));

            // The 403 comes only once the route is found, and its audit event names the route.
            HttpResponse<String> denied = http(op.method(), path, service, null);
            assertThat(denied.statusCode()).as(op + " for a service identity").isEqualTo(403);
            assertConforms(op + " 403", json(denied.body()), schemaRef("ErrorResponse"));
            String requestId = denied.headers().firstValue("X-Request-Id").orElseThrow();
            assertThat(
                            queryColumn(
                                    "SELECT action FROM audit_events WHERE request_id = '"
                                            + UUID.fromString(requestId)
                                            + "'"))
                    .as(op + " records its action")
                    .containsExactly(op.action());
        }
        for (Map.Entry<String, JsonNode> path : fields(spec.get("paths"))) {
            Operation any = operationsOf(path.getKey(), path.getValue()).get(0);
            for (String method : METHODS) {
                if (path.getValue().has(method.toLowerCase(Locale.ROOT))) {
                    continue;
                }
                HttpResponse<String> response =
                        http(method, BASE + concrete(any, Map.of()), adminToken, null);
                assertThat(response.statusCode())
                        .as(method + " " + path.getKey() + " is no route")
                        .isEqualTo(404);
                if (!method.equals("HEAD")) {
                    assertThat(response.body()).contains("No such endpoint");
                }
            }
        }
    }

    @Test
    void eachOperationDocumentsTheRequestFieldsAndQueryParametersItsRouteReads() throws Exception {
        Map<String, String> routes = routeSources(Files.readString(ROUTES));
        for (Operation op : operations()) {
            String route = routes.get(op.action());
            assertThat(route).as(op + "'s route").isNotNull();
            Set<String> fields = new TreeSet<>();
            Set<String> required = new TreeSet<>();
            JsonNode body = op.node().path("requestBody");
            if (!body.isMissingNode()) {
                fields.addAll(names(requestSchema(op).path("properties")));
                requestSchema(op).path("required").forEach(f -> required.add(f.asText()));
            }
            Set<String> query = new TreeSet<>();
            for (JsonNode parameter : parameters(op)) {
                if (parameter.path("in").asText().equals("query")) {
                    query.add(parameter.path("name").asText());
                }
            }
            assertThat(fields)
                    .as(op + " request fields")
                    .isEqualTo(new TreeSet<>(all(BODY_FIELD, route)));
            assertThat(required)
                    .as(op + " required fields")
                    .containsAll(all(REQUIRED_FIELD, route));
            assertThat(query)
                    .as(op + " query parameters")
                    .isEqualTo(new TreeSet<>(all(QUERY_PARAMETER, route)));
        }
    }

    @Test
    void theDocumentedExamplesWorkAndAnswerAsDocumented() throws Exception {
        Map<String, Operation> ops = new LinkedHashMap<>();
        operations().forEach(op -> ops.put(op.id(), op));
        Run run = new Run(ops);

        // Principals and their tokens, with the description's own request bodies; what the
        // examples leave out takes its documented default.
        JsonNode principal =
                run.call("createPrincipal", Map.of(), null, example(ops.get("createPrincipal")));
        String name = principal.get("name").asText();
        JsonNode listed = null;
        for (JsonNode p : run.call("listPrincipals", Map.of(), null, null).get("principals")) {
            listed = p.get("name").asText().equals(name) ? p : listed;
        }
        assertThat(listed).as("the new principal, listed").isNotNull();
        assertThat(listed.get("admin").asBoolean())
                .isEqualTo(defaultOf("CreatePrincipalRequest", "admin").asBoolean());
        JsonNode issued =
                run.call("issueToken", Map.of("name", name), null, example(ops.get("issueToken")));
        assertThat(issued.get("token").asText()).startsWith("colvenna_");
        String tokenId = issued.get("id").asText();
        assertThat(
                        run.call("listTokens", Map.of("name", name), null, null)
                                .get("tokens")
                                .findValuesAsText("id"))
                .contains(tokenId);
        Instant before = clock.instant();
        Duration ttl = Duration.parse(defaultOf("IssueTokenRequest", "ttl").asText());
        JsonNode byDefault = run.call("issueToken", Map.of("name", name), null, null);
        assertThat(Duration.between(before, Instant.parse(byDefault.get("expiresAt").asText())))
                .isBetween(ttl, ttl.plusMinutes(1));

        // Every grant example, to a principal made first if it is another, and made to expire a
        // month from now whatever its date says.
        List<String> grants = new ArrayList<>();
        Set<String> principals = new TreeSet<>(Set.of(name));
        for (JsonNode grant : examples(ops.get("createGrant"))) {
            ObjectNode body = grant.deepCopy();
            if (principals.add(body.get("principal").asText())) {
                run.call(
                        "createPrincipal",
                        Map.of(),
                        null,
                        json("{\"name\":\"" + body.get("principal").asText() + "\"}"));
            }
            body.put("expiresAt", inAMonth());
            JsonNode made = run.call("createGrant", Map.of(), null, body);
            grants.add(made.get("id").asText());
            if (!body.has("effect")) {
                assertThat(made.get("effect").asText())
                        .isEqualTo(defaultOf("CreateGrantRequest", "effect").asText());
            }
            assertThat(made.get("environment").asText())
                    .isEqualTo(server.authorizer().environment());
        }
        assertThat(grants).isNotEmpty();
        assertThat(
                        run.call("listGrants", Map.of(), null, null)
                                .get("grants")
                                .findValuesAsText("id"))
                .containsAll(grants);
        assertThat(
                        run.call("listGrants", Map.of(), "principal=" + encode(name), null)
                                .get("grants")
                                .findValuesAsText("principal"))
                .isNotEmpty()
                .containsOnly(name);

        // A table to check and repair, at the description's example names.
        Map<String, String> table = pathExamples(ops.get("checkConsistency"));
        Identifier identifier = Identifier.create(table.get("database"), table.get("table"));
        try (RESTCatalog catalog = catalog(adminToken)) {
            Tables.createAppendTable(catalog, identifier, Map.of());
            Tables.write(catalog.getTable(identifier), 1);
        }
        assertThat(run.call("checkConsistency", table, null, null).get("consistent").asBoolean())
                .isTrue();
        assertThat(run.call("reconcileTable", table, null, null).get("stateBefore").asText())
                .isEqualTo("ACTIVE");

        // A dropped table's files still to be deleted, as a failed purge leaves them recorded.
        try (var c = server.db().dataSource().getConnection();
                var insert =
                        c.prepareStatement(
                                "INSERT INTO table_purges (table_id, table_name, path, dropped_at,"
                                        + " dropped_by, request_id, attempts, last_attempt_at,"
                                        + " last_error, not_before) VALUES (?, 'sales.gone', ?,"
                                        + " now(), 'admin', 'r-1', 1, now(), 'java.io.IOException:"
                                        + " Files remain', ?)")) {
            insert.setObject(1, UUID.randomUUID());
            // Due now by the server's clock, which the purges go by.
            insert.setTimestamp(3, java.sql.Timestamp.from(clock.instant()));
            insert.setString(
                    2, com.colvenna.catalog.fs.PathRules.normalize(warehouse) + "/sales.db/gone");
            insert.executeUpdate();
            c.commit();
        }
        assertThat(run.call("listPurges", Map.of(), null, null).get("purges")).hasSize(1);
        server.catalog().purges().sweep(null);
        assertThat(run.call("listPurges", Map.of(), null, null).get("purges")).isEmpty();

        // The audit log: every documented filter at its example value is accepted; one filter
        // finds what this test did.
        run.call("queryAudit", Map.of(), queryExamples(ops.get("queryAudit")), null);
        JsonNode events =
                run.call(
                                "queryAudit",
                                Map.of(),
                                "action=ISSUE_TOKEN&resource=" + encode(name) + "&limit=10",
                                null)
                        .get("events");
        assertThat(events).as("the two tokens issued").hasSize(2);
        assertThat(events.findValuesAsText("principal")).containsOnly("admin");
        // The principal's creation, and a second one that fails: both under its name.
        run.fails("createPrincipal", Map.of(), json("{\"name\":\"" + name + "\"}"), 409);
        JsonNode created =
                run.call(
                                "queryAudit",
                                Map.of(),
                                "action=CREATE_PRINCIPAL&resource=" + encode(name),
                                null)
                        .get("events");
        assertThat(created.findValuesAsText("outcome"))
                .as("the principal's creations, under its name")
                .containsExactlyInAnyOrder("SUCCEEDED", "FAILED");
        assertThat(created.findValuesAsText("resourceType")).containsOnly("PRINCIPAL");
        assertThat(created.findValuesAsText("principal")).containsOnly("admin");
        int limit = queryDefault(ops.get("queryAudit"), "limit").asInt();
        for (int i = 0; i <= limit; i++) {
            http("DELETE", BASE + "/tokens/" + UUID.randomUUID(), adminToken, null);
        }
        assertThat(run.call("queryAudit", Map.of(), null, null).get("events"))
                .as("events without a limit")
                .hasSize(limit);

        for (String grant : grants) {
            run.call("revokeGrant", Map.of("id", grant), null, null);
        }
        run.call("revokeToken", Map.of("id", tokenId), null, null);
        run.call("disablePrincipal", Map.of("name", name), null, null);
        run.call("unregisterTable", table, null, null);

        // Every write, done, is recorded against what it acted on.
        Set<String> writeActions = new TreeSet<>(all(WRITE_ACTION, Files.readString(ROUTES)));
        Set<String> recordedWrites = new TreeSet<>();
        List<String> unnamed = new ArrayList<>();
        for (JsonNode e :
                run.call(
                                "queryAudit",
                                Map.of(),
                                "principal=admin&outcome=SUCCEEDED&limit=1000",
                                null)
                        .get("events")) {
            if (writeActions.contains(e.get("action").asText())) {
                recordedWrites.add(e.get("action").asText());
                if (!e.hasNonNull("resource")) {
                    unnamed.add(e.toString());
                }
            }
        }
        assertThat(recordedWrites).as("writes recorded").isEqualTo(writeActions);
        assertThat(unnamed).as("writes recorded without their resource").isEmpty();

        // Documented failures answer with the documented error body; a request without one of
        // its required fields is refused.
        for (String id : List.of("createPrincipal", "createGrant")) {
            Operation op = ops.get(id);
            for (JsonNode required : requestSchema(op).path("required")) {
                ObjectNode body = example(op).deepCopy();
                if (body.has("expiresAt")) {
                    body.put("expiresAt", inAMonth());
                }
                body.remove(required.asText());
                assertThat(run.fails(id, Map.of(), body, 400).get("message").asText())
                        .as(id + " without " + required.asText())
                        .contains(required.asText());
            }
        }
        run.fails("disablePrincipal", Map.of("name", "no-such-principal"), null, 404);
        run.fails("issueToken", Map.of("name", name), json("{\"ttl\":\"P401D\"}"), 400);
        run.fails("revokeGrant", Map.of("id", "not-a-number"), null, 400);

        assertThat(run.exercised)
                .as("operations run")
                .isEqualTo(ops.keySet().stream().collect(Collectors.toCollection(TreeSet::new)));
    }

    // ------------------------------------------------------------------- running operations

    /** Calls operations as an admin and checks each answer against the description. */
    private final class Run {
        private final Map<String, Operation> ops;
        private final Set<String> exercised = new TreeSet<>();

        Run(Map<String, Operation> ops) {
            this.ops = ops;
        }

        /** Runs {@code id}, expects 200, and returns its JSON body (null for none). */
        @Nullable
        JsonNode call(
                String id,
                Map<String, String> path,
                @Nullable String query,
                @Nullable JsonNode body)
                throws Exception {
            HttpResponse<String> response = send(id, path, query, body);
            assertThat(response.statusCode()).as(id + ": " + response.body()).isEqualTo(200);
            return checked(id, response);
        }

        /** Runs {@code id}, expects {@code status}, and returns its error body. */
        JsonNode fails(String id, Map<String, String> path, @Nullable JsonNode body, int status)
                throws Exception {
            HttpResponse<String> response = send(id, path, null, body);
            assertThat(response.statusCode()).as(id + ": " + response.body()).isEqualTo(status);
            return checked(id, response);
        }

        private HttpResponse<String> send(
                String id,
                Map<String, String> path,
                @Nullable String query,
                @Nullable JsonNode body)
                throws Exception {
            Operation op = ops.get(id);
            assertThat(op).as("operation " + id).isNotNull();
            exercised.add(id);
            String target = BASE + concrete(op, path) + (query == null ? "" : "?" + query);
            return http(
                    op.method(),
                    target,
                    adminToken,
                    body == null ? null : RESTApi.OBJECT_MAPPER.writeValueAsString(body));
        }

        /** The body, after checking that the status is documented and the body has its shape. */
        @Nullable
        private JsonNode checked(String id, HttpResponse<String> response) throws Exception {
            Operation op = ops.get(id);
            String status = String.valueOf(response.statusCode());
            JsonNode documented = resolve(op.node().path("responses").path(status));
            assertThat(documented.isMissingNode()).as(op + " documents " + status).isFalse();
            JsonNode schema = documented.path("content").path(JSON).path("schema");
            if (schema.isMissingNode()) {
                assertThat(response.body()).as(op + " " + status + " has no body").isEmpty();
                return null;
            }
            JsonNode body = json(response.body());
            assertConforms(op + " " + status, body, schema);
            return body;
        }
    }

    // ------------------------------------------------------------------- the description

    private static final String JSON = "application/json";

    private List<Operation> operations() {
        List<Operation> ops = new ArrayList<>();
        for (Map.Entry<String, JsonNode> path : fields(spec.get("paths"))) {
            ops.addAll(operationsOf(path.getKey(), path.getValue()));
        }
        assertThat(ops).as("operations in " + SPEC).isNotEmpty();
        return ops;
    }

    private static List<Operation> operationsOf(String path, JsonNode item) {
        List<Operation> ops = new ArrayList<>();
        for (String method : METHODS) {
            JsonNode node = item.get(method.toLowerCase(Locale.ROOT));
            if (node != null) {
                ops.add(new Operation(method, path, node, item));
            }
        }
        return ops;
    }

    /** The parameters of an operation: those of its path, then its own. */
    private List<JsonNode> parameters(Operation op) {
        List<JsonNode> parameters = new ArrayList<>();
        op.pathItem().path("parameters").forEach(p -> parameters.add(resolve(p)));
        op.node().path("parameters").forEach(p -> parameters.add(resolve(p)));
        return parameters;
    }

    /** The operation's path, each parameter at its example unless {@code values} gives one. */
    private String concrete(Operation op, Map<String, String> values) {
        Map<String, String> all = new HashMap<>(pathExamples(op));
        all.putAll(values);
        String path = op.path();
        for (Map.Entry<String, String> value : all.entrySet()) {
            path = path.replace("{" + value.getKey() + "}", encode(value.getValue()));
        }
        assertThat(path).as(op + " has an example for every path parameter").doesNotContain("{");
        return path;
    }

    private Map<String, String> pathExamples(Operation op) {
        Map<String, String> examples = new HashMap<>();
        for (JsonNode parameter : parameters(op)) {
            if (parameter.path("in").asText().equals("path")) {
                examples.put(parameter.get("name").asText(), parameter.path("example").asText());
            }
        }
        return examples;
    }

    /** Every documented query parameter at its example value. */
    private String queryExamples(Operation op) {
        List<String> pairs = new ArrayList<>();
        for (JsonNode parameter : parameters(op)) {
            if (parameter.path("in").asText().equals("query")) {
                assertThat(parameter.has("example")).as(parameter + " has an example").isTrue();
                pairs.add(
                        encode(parameter.get("name").asText())
                                + "="
                                + encode(parameter.get("example").asText()));
            }
        }
        return String.join("&", pairs);
    }

    /**
     * The request body examples of an operation, its {@code example} or each of {@code examples},
     * each of its schema and with no field it does not list.
     */
    private List<JsonNode> examples(Operation op) {
        JsonNode media = resolve(op.node().path("requestBody")).path("content").path(JSON);
        List<JsonNode> examples = new ArrayList<>();
        if (media.has("example")) {
            examples.add(media.get("example"));
        }
        for (Map.Entry<String, JsonNode> named : fields(media.path("examples"))) {
            examples.add(resolve(named.getValue()).get("value"));
        }
        assertThat(examples).as(op + " has a request example").isNotEmpty();
        JsonNode schema = requestSchema(op);
        for (JsonNode example : examples) {
            assertConforms(op + " example", example, schema);
            assertThat(names(example))
                    .as(op + " example's fields")
                    .isSubsetOf(names(schema.path("properties")));
        }
        return examples;
    }

    private JsonNode example(Operation op) {
        return examples(op).get(0);
    }

    /** A month from the test's clock: an expiry any grant may take. */
    private String inAMonth() {
        return clock.instant().plus(Duration.ofDays(30)).toString();
    }

    /** The documented default of a query parameter. */
    private JsonNode queryDefault(Operation op, String name) {
        for (JsonNode parameter : parameters(op)) {
            if (parameter.path("in").asText().equals("query")
                    && parameter.path("name").asText().equals(name)) {
                JsonNode value = parameter.path("schema").path("default");
                assertThat(value.isMissingNode()).as(op + " " + name + " default").isFalse();
                return value;
            }
        }
        throw new AssertionError(op + " has no query parameter " + name);
    }

    private JsonNode requestSchema(Operation op) {
        return resolve(
                resolve(op.node().path("requestBody")).path("content").path(JSON).path("schema"));
    }

    /**
     * The source of each route, by action: from its action to the next route's, within {@code
     * AdminRoutes.resolve}, the method that holds them all.
     */
    private static Map<String, String> routeSources(String file) {
        int start = file.indexOf("public Route resolve(");
        assertThat(start).as("AdminRoutes.resolve").isNotNegative();
        String source = file.substring(start, file.indexOf("\n    }\n", start));
        Map<String, String> routes = new LinkedHashMap<>();
        Matcher m = ACTION.matcher(source);
        String action = null;
        int from = 0;
        while (m.find()) {
            if (action != null) {
                routes.put(action, source.substring(from, m.start()));
            }
            action = m.group(1);
            from = m.start();
        }
        if (action != null) {
            routes.put(action, source.substring(from));
        }
        return routes;
    }

    /** A {@code $ref} into this description, followed; anything else as it is. */
    private JsonNode resolve(JsonNode node) {
        while (node.has("$ref")) {
            String ref = node.get("$ref").asText();
            assertThat(ref).startsWith("#/");
            node = spec.at(ref.substring(1));
            assertThat(node.isMissingNode()).as(ref).isFalse();
        }
        return node;
    }

    private JsonNode schemaRef(String name) {
        return resolve(json("{\"$ref\":\"#/components/schemas/" + name + "\"}"));
    }

    /** The documented default of a property of a schema. */
    private JsonNode defaultOf(String schema, String property) {
        JsonNode value = schemaRef(schema).path("properties").path(property).path("default");
        assertThat(value.isMissingNode()).as(schema + "." + property + " has a default").isFalse();
        return value;
    }

    // ------------------------------------------------------------------- shapes

    private void assertConforms(String what, JsonNode value, JsonNode schema) {
        assertThat(departures(value, schema, "$")).as(what + ": " + value).isEmpty();
    }

    /**
     * Where {@code value} departs from {@code schema}, an OpenAPI 3.0 schema object: its type,
     * {@code nullable}, {@code enum}, the {@code date-time} and {@code uuid} formats, an object's
     * {@code required} and listed properties and, where {@code additionalProperties} is false, no
     * others.
     */
    private List<String> departures(JsonNode value, JsonNode schema, String at) {
        JsonNode s = resolve(schema);
        List<String> out = new ArrayList<>();
        if (value.isNull()) {
            if (!s.path("nullable").asBoolean(false)) {
                out.add(at + " is null but not nullable");
            }
            return out;
        }
        if (s.has("enum")) {
            boolean listed = false;
            for (JsonNode allowed : s.get("enum")) {
                listed |= allowed.equals(value);
            }
            if (!listed) {
                out.add(at + " = " + value + " is not one of " + s.get("enum"));
            }
        }
        String type = s.path("type").asText("");
        switch (type) {
            case "object" -> {
                if (!value.isObject()) {
                    out.add(at + " is not an object");
                    return out;
                }
                for (JsonNode required : s.path("required")) {
                    if (!value.has(required.asText())) {
                        out.add(at + " lacks " + required.asText());
                    }
                }
                JsonNode properties = s.path("properties");
                JsonNode additional = s.path("additionalProperties");
                for (Map.Entry<String, JsonNode> field : fields(value)) {
                    String where = at + "." + field.getKey();
                    if (properties.has(field.getKey())) {
                        out.addAll(
                                departures(
                                        field.getValue(), properties.get(field.getKey()), where));
                    } else if (additional.isObject()) {
                        out.addAll(departures(field.getValue(), additional, where));
                    } else if (additional.isBoolean() && !additional.asBoolean()) {
                        out.add(where + " is not documented");
                    }
                }
            }
            case "array" -> {
                if (!value.isArray()) {
                    out.add(at + " is not an array");
                    return out;
                }
                int i = 0;
                for (JsonNode item : value) {
                    out.addAll(departures(item, s.path("items"), at + "[" + i++ + "]"));
                }
            }
            case "string" -> {
                if (!value.isTextual()) {
                    out.add(at + " is not a string");
                } else if (s.path("format").asText().equals("date-time")) {
                    try {
                        Instant.parse(value.asText());
                    } catch (DateTimeParseException e) {
                        out.add(at + " is not a date-time");
                    }
                } else if (s.path("format").asText().equals("uuid")) {
                    try {
                        UUID.fromString(value.asText());
                    } catch (IllegalArgumentException e) {
                        out.add(at + " is not a uuid");
                    }
                }
            }
            case "integer" -> {
                if (!value.isIntegralNumber()) {
                    out.add(at + " is not an integer");
                }
            }
            case "number" -> {
                if (!value.isNumber()) {
                    out.add(at + " is not a number");
                }
            }
            case "boolean" -> {
                if (!value.isBoolean()) {
                    out.add(at + " is not a boolean");
                }
            }
            default -> out.add(at + ": the schema names no type");
        }
        return out;
    }

    // ------------------------------------------------------------------- helpers

    private static List<String> all(Pattern pattern, String text) {
        List<String> found = new ArrayList<>();
        Matcher m = pattern.matcher(text);
        while (m.find()) {
            for (int g = 1; g <= m.groupCount(); g++) {
                if (m.group(g) != null) {
                    found.add(m.group(g));
                }
            }
        }
        return found;
    }

    private static List<Map.Entry<String, JsonNode>> fields(JsonNode node) {
        List<Map.Entry<String, JsonNode>> fields = new ArrayList<>();
        if (node != null) {
            Iterator<Map.Entry<String, JsonNode>> it = node.fields();
            it.forEachRemaining(fields::add);
        }
        return fields;
    }

    private static List<String> names(JsonNode node) {
        List<String> names = new ArrayList<>();
        if (node != null && node.isObject()) {
            node.fieldNames().forEachRemaining(names::add);
        }
        return names;
    }

    private static JsonNode json(String text) {
        try {
            return RESTApi.OBJECT_MAPPER.readTree(text);
        } catch (Exception e) {
            throw new AssertionError("not JSON: " + text, e);
        }
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }
}
