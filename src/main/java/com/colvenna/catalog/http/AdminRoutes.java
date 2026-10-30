package com.colvenna.catalog.http;

import com.colvenna.catalog.audit.AuditLog;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.auth.TokenService;
import com.colvenna.catalog.auth.TokenService.IssuedToken;
import com.colvenna.catalog.catalog.CatalogService;
import com.colvenna.catalog.catalog.ConsistencyCheck;
import com.colvenna.catalog.catalog.FilePurges;
import com.colvenna.catalog.catalog.TablePurges;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;

/**
 * colvenna-catalog's own management API under {@code /colvenna/admin/v1}, versioned separately from
 * the Paimon REST contract. Every route requires an admin principal.
 *
 * <ul>
 *   <li>{@code GET|POST /principals}, {@code POST /principals/{name}/disable}
 *   <li>{@code GET|POST /principals/{name}/tokens}, {@code DELETE /tokens/{id}}
 *   <li>{@code GET /consistency/{database}/{table}}: PostgreSQL against the table files, in pages
 *       when the server bounds each answer's time ({@code pageToken})
 *   <li>{@code POST /tables/{database}/{table}/reconcile}: re-read heads from files, lift a fence
 *   <li>{@code POST /tables/{database}/{table}/unregister}: remove from the catalog, keep files
 *   <li>{@code GET|POST /grants}, {@code DELETE /grants/{id}}: service-identity grants
 *   <li>{@code GET /audit}: audit events, newest first, filtered by principal, action, resource
 *       type, resource, outcome and a {@code since}/{@code until} time range
 *   <li>{@code GET /purges}: the files of dropped tables still to be deleted, oldest first, and how
 *       their purges have gone; and those rollbacks and tag and branch deletions left (#93)
 * </ul>
 */
public final class AdminRoutes {

    /** Upper bound on API key lifetime accepted from the admin API. */
    static final Duration MAX_TOKEN_TTL = Duration.ofDays(400);

    private final TokenService tokens;
    private final ConsistencyCheck consistency;
    private final CatalogService catalog;
    private final Authorizer authz;
    private final AuditLog audit;

    public AdminRoutes(
            TokenService tokens,
            ConsistencyCheck consistency,
            CatalogService catalog,
            Authorizer authz,
            AuditLog audit) {
        this.tokens = tokens;
        this.consistency = consistency;
        this.catalog = catalog;
        this.authz = authz;
        this.audit = audit;
    }

    /** Resolves a request whose segments start after {@code /colvenna/admin/v1}. */
    @Nullable
    public Route resolve(Request r) {
        List<String> s = r.segments();
        if (s.size() == 1 && s.get(0).equals("principals")) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_PRINCIPALS",
                        Resources.PRINCIPAL,
                        null,
                        q -> Response.ok(Map.of("principals", tokens.listPrincipals())));
            }
            if (r.is("POST")) {
                return Route.write(
                        "CREATE_PRINCIPAL",
                        Resources.PRINCIPAL,
                        null,
                        q -> {
                            JsonNode body = json(q);
                            String name = text(body, "name");
                            tokens.createPrincipal(
                                    name,
                                    body.hasNonNull("description")
                                            ? body.get("description").asText()
                                            : null,
                                    body.path("admin").asBoolean(false),
                                    q.principal().name());
                            return Response.ok(Map.of("name", name)).withAuditResource(name);
                        });
            }
            return null;
        }
        if (s.size() == 3 && s.get(0).equals("principals")) {
            String name = s.get(1);
            if (s.get(2).equals("disable") && r.is("POST")) {
                return Route.write(
                        "DISABLE_PRINCIPAL",
                        Resources.PRINCIPAL,
                        name,
                        q -> {
                            tokens.disablePrincipal(name, q.principal().name());
                            return Response.empty();
                        });
            }
            if (s.get(2).equals("tokens") && r.is("GET")) {
                return Route.read(
                        "LIST_TOKENS",
                        Resources.PRINCIPAL,
                        name,
                        q -> Response.ok(Map.of("tokens", tokens.listTokens(name))));
            }
            if (s.get(2).equals("tokens") && r.is("POST")) {
                return Route.write(
                        "ISSUE_TOKEN",
                        Resources.PRINCIPAL,
                        name,
                        q -> {
                            JsonNode body = json(q);
                            Duration ttl = Duration.parse(body.path("ttl").asText("P90D"));
                            if (ttl.compareTo(MAX_TOKEN_TTL) > 0) {
                                throw ApiException.badRequest(
                                        "Token lifetime is limited to " + MAX_TOKEN_TTL + ".");
                            }
                            IssuedToken issued =
                                    tokens.issue(
                                            name,
                                            body.hasNonNull("description")
                                                    ? body.get("description").asText()
                                                    : null,
                                            ttl,
                                            q.principal().name());
                            Map<String, Object> out = new LinkedHashMap<>();
                            out.put("id", issued.id().toString());
                            out.put("principal", issued.principal());
                            out.put("token", issued.token());
                            out.put("expiresAt", issued.expiresAt().toString());
                            return Response.ok(out);
                        });
            }
            return null;
        }
        if (s.size() == 4 && s.get(0).equals("tables") && r.is("POST")) {
            Identifier table = Identifier.create(s.get(1), s.get(2));
            if (s.get(3).equals("reconcile")) {
                return Route.write(
                        "RECONCILE_TABLE",
                        Resources.TABLE,
                        table.getFullName(),
                        q -> {
                            Map<String, Object> report = catalog.reconcileTable(q.context(), table);
                            return Response.ok(report).audited(AuditLog.Outcome.SUCCEEDED, report);
                        });
            }
            if (s.get(3).equals("unregister")) {
                return Route.write(
                        "UNREGISTER_TABLE",
                        Resources.TABLE,
                        table.getFullName(),
                        q -> {
                            catalog.unregisterTable(q.context(), table);
                            return Response.empty();
                        });
            }
            return null;
        }
        if (s.size() == 3 && s.get(0).equals("consistency") && r.is("GET")) {
            Identifier table = Identifier.create(s.get(1), s.get(2));
            return Route.read(
                    "CHECK_CONSISTENCY",
                    Resources.TABLE,
                    table.getFullName(),
                    q -> Response.ok(consistency.check(q.context(), table, q.param("pageToken"))));
        }
        if (s.size() == 1 && s.get(0).equals("purges") && r.is("GET")) {
            return Route.read(
                    "LIST_PURGES",
                    Resources.CATALOG,
                    null,
                    q -> {
                        int limit = limit(q.param("limit"));
                        Map<String, Object> out = new LinkedHashMap<>();
                        out.put(
                                "purges",
                                catalog.purges().list(limit).stream()
                                        .map(TablePurges.Pending::describe)
                                        .toList());
                        out.put("pending", catalog.purges().count());
                        out.put(
                                "filePurges",
                                catalog.filePurges().list(limit).stream()
                                        .map(FilePurges.Pending::describe)
                                        .toList());
                        out.put("filePurgesPending", catalog.filePurges().count());
                        return Response.ok(out);
                    });
        }
        if (s.size() == 1 && s.get(0).equals("grants")) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_GRANTS",
                        Resources.PRINCIPAL,
                        r.param("principal"),
                        q -> Response.ok(Map.of("grants", authz.list(q.param("principal")))));
            }
            if (r.is("POST")) {
                return Route.write(
                        "GRANT",
                        Resources.GRANT,
                        null,
                        q -> {
                            JsonNode body = json(q);
                            List<String> columns = null;
                            if (body.hasNonNull("columns")) {
                                columns = new ArrayList<>();
                                for (JsonNode column : body.get("columns")) {
                                    columns.add(column.asText());
                                }
                            }
                            String expires = text(body, "expiresAt");
                            Instant expiresAt = instant(expires);
                            Authorizer.Terms terms =
                                    new Authorizer.Terms(
                                            text(body, "purpose"),
                                            optional(body, "environment", authz.environment()),
                                            text(body, "approvedBy"),
                                            text(body, "approval"),
                                            expiresAt);
                            Map.Entry<Authorizer.Grant, Long> stored =
                                    authz.grant(
                                            text(body, "principal"),
                                            Authorizer.parse(
                                                    Authorizer.Scope.class,
                                                    "scope",
                                                    body.path("scope").asText(null)),
                                            optional(body, "database", null),
                                            optional(body, "object", null),
                                            Authorizer.parse(
                                                    Authorizer.Privilege.class,
                                                    "privilege",
                                                    body.path("privilege").asText(null)),
                                            Authorizer.parse(
                                                    Authorizer.Effect.class,
                                                    "effect",
                                                    body.path("effect").asText("ALLOW")),
                                            columns,
                                            terms,
                                            q.principal().name());
                            Authorizer.Grant grant = stored.getKey();
                            Map<String, Object> detail = new LinkedHashMap<>(grant.describe());
                            detail.put("policyVersion", stored.getValue());
                            return Response.ok(grant)
                                    .audited(AuditLog.Outcome.SUCCEEDED, detail)
                                    .withAuditResource(grantResource(grant));
                        });
            }
            return null;
        }
        if (s.size() == 2 && s.get(0).equals("grants") && r.is("DELETE")) {
            String id = s.get(1);
            return Route.write(
                    "REVOKE_GRANT",
                    Resources.GRANT,
                    id,
                    q -> {
                        long grant;
                        try {
                            grant = Long.parseLong(id);
                        } catch (NumberFormatException e) {
                            throw ApiException.badRequest("Malformed grant id " + id);
                        }
                        Map.Entry<Authorizer.Grant, Long> revoked =
                                authz.revoke(grant)
                                        .orElseThrow(
                                                () ->
                                                        ApiException.notFound(
                                                                Resources.GRANT,
                                                                id,
                                                                "Grant "
                                                                        + id
                                                                        + " does not exist."));
                        // The row is gone; the audit event keeps what it granted.
                        Map<String, Object> detail =
                                new LinkedHashMap<>(revoked.getKey().describe());
                        detail.put("policyVersion", revoked.getValue());
                        return Response.empty()
                                .audited(AuditLog.Outcome.SUCCEEDED, detail)
                                .withAuditResource(grantResource(revoked.getKey()));
                    });
        }
        if (s.size() == 1 && s.get(0).equals("audit") && r.is("GET")) {
            return Route.read(
                    "QUERY_AUDIT",
                    Resources.CATALOG,
                    null,
                    q ->
                            Response.ok(
                                    Map.of(
                                            "events",
                                            audit.query(
                                                    new AuditLog.Query(
                                                            q.param("principal"),
                                                            q.param("action"),
                                                            q.param("resourceType"),
                                                            q.param("resource"),
                                                            q.param("outcome") == null
                                                                    ? null
                                                                    : Authorizer.parse(
                                                                            AuditLog.Outcome.class,
                                                                            "outcome",
                                                                            q.param("outcome")),
                                                            instant(q.param("since")),
                                                            instant(q.param("until")),
                                                            q.param("limit") == null
                                                                    ? 100
                                                                    : Integer.parseInt(
                                                                            q.param("limit")))))));
        }
        if (s.size() == 2 && s.get(0).equals("tokens") && r.is("DELETE")) {
            String id = s.get(1);
            return Route.write(
                    "REVOKE_TOKEN",
                    Resources.TOKEN,
                    id,
                    q -> {
                        UUID uuid;
                        try {
                            uuid = UUID.fromString(id);
                        } catch (IllegalArgumentException e) {
                            throw ApiException.badRequest("Malformed token id " + id);
                        }
                        tokens.revoke(uuid, q.principal().name());
                        return Response.empty();
                    });
        }
        return null;
    }

    /** A listing's {@code limit}: 100 by default, values outside 1 to 1000 taken as the nearer. */
    private static int limit(@Nullable String value) {
        if (value == null) {
            return 100;
        }
        try {
            return Math.max(1, Math.min(1000, Integer.parseInt(value)));
        } catch (NumberFormatException e) {
            throw ApiException.badRequest("Expected a whole number for limit, not " + value);
        }
    }

    /** The audit resource of a grant: whom it binds to what. */
    private static String grantResource(Authorizer.Grant grant) {
        String target =
                grant.database() == null
                        ? "*"
                        : grant.object() == null
                                ? grant.database()
                                : grant.database() + "." + grant.object();
        return grant.principal() + "@" + grant.scope().name() + ":" + target;
    }

    @Nullable
    private static String optional(JsonNode body, String field, @Nullable String fallback) {
        return body.hasNonNull(field) ? body.get(field).asText() : fallback;
    }

    @Nullable
    private static Instant instant(@Nullable String value) {
        if (value == null) {
            return null;
        }
        try {
            return Instant.parse(value);
        } catch (DateTimeParseException e) {
            throw ApiException.badRequest("Expected an ISO-8601 instant, not " + value);
        }
    }

    private static JsonNode json(Request q) {
        try {
            return RESTApi.OBJECT_MAPPER.readTree(q.body() == null ? "{}" : q.body());
        } catch (Exception e) {
            throw ApiException.badRequest("Malformed request body: " + e.getMessage());
        }
    }

    private static String text(JsonNode body, String field) {
        if (!body.hasNonNull(field)) {
            throw ApiException.badRequest("Field " + field + " is required.");
        }
        return body.get(field).asText();
    }
}
