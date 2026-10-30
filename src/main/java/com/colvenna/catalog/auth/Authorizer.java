package com.colvenna.catalog.auth;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.http.ApiException;
import com.colvenna.catalog.http.Resources;
import java.sql.Array;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.sql.Types;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Identifier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Service-identity grants and the decisions made from them.
 *
 * <p>A grant allows or denies one privilege level on the catalog, a database, or one object. Levels
 * nest ({@link Privilege#MANAGE} includes {@link Privilege#WRITE} includes {@link Privilege#READ}):
 * an allow permits its level and below, a deny forbids its level and above and wins over every
 * allow. Only unexpired grants of this server's environment count. Admin principals have no
 * implicit catalog privileges; their grants are recorded and reviewed like anyone's.
 *
 * <p>Every catalog action is classified in exactly one of {@link #REQUIRED}, {@link #LISTINGS},
 * {@link #UNTARGETED} and {@link #IN_HANDLER}; an unclassified action is refused, so a new route
 * cannot be served without a decision. The target comes from the request path, not from a re-parsed
 * resource string. A principal's grants are cached for the authentication cache period.
 */
public final class Authorizer {

    private static final Logger LOG = LoggerFactory.getLogger(Authorizer.class);

    /** Privilege levels, lowest first. */
    public enum Privilege {
        READ,
        WRITE,
        MANAGE;

        boolean covers(Privilege needed) {
            return compareTo(needed) >= 0;
        }
    }

    /** What a grant does. */
    public enum Effect {
        ALLOW,
        DENY
    }

    /** What a grant applies to, with the resource type its denials report. */
    public enum Scope {
        CATALOG(Resources.CATALOG, "Catalog"),
        DATABASE(Resources.DATABASE, "Database"),
        TABLE(Resources.TABLE, "Table"),
        VIEW(Resources.VIEW, "View"),
        FUNCTION(Resources.FUNCTION, "Function");

        private final String resourceType;
        private final String label;

        Scope(String resourceType, String label) {
            this.resourceType = resourceType;
            this.label = label;
        }

        public String resourceType() {
            return resourceType;
        }
    }

    /** The object a request acts on: a database, or an object in it. */
    public record Target(Scope scope, String database, @Nullable String object) {

        public String name() {
            return object == null ? database : database + "." + object;
        }
    }

    /**
     * What a grant binds, as the acceptance contract requires: purpose, environment, approver,
     * approval and expiry.
     */
    public record Terms(
            String purpose,
            String environment,
            String approvedBy,
            String approval,
            Instant expires) {}

    /** One stored grant. {@code columns} narrows a table read allow to those columns. */
    public record Grant(
            long id,
            String principal,
            Scope scope,
            @Nullable String database,
            @Nullable String object,
            Privilege privilege,
            Effect effect,
            @Nullable List<String> columns,
            String purpose,
            String environment,
            String approvedBy,
            String approval,
            String expiresAt,
            String createdAt,
            String createdBy) {

        boolean covers(Target target) {
            return switch (scope) {
                case CATALOG -> true;
                case DATABASE -> target.database().equals(database);
                default ->
                        scope == target.scope()
                                && target.database().equals(database)
                                && target.object() != null
                                && target.object().equals(object);
            };
        }

        /** Whether this is a deny of {@code needed} (its level or below) on {@code target}. */
        boolean forbids(Privilege needed, Target target) {
            return effect == Effect.DENY && covers(target) && needed.compareTo(privilege) >= 0;
        }

        /** What audit records keep about the grant. */
        public Map<String, Object> describe() {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("grantId", id);
            d.put("principal", principal);
            d.put("scope", scope.name());
            d.put("database", database);
            d.put("object", object);
            d.put("privilege", privilege.name());
            d.put("effect", effect.name());
            d.put("columns", columns);
            d.put("purpose", purpose);
            d.put("environment", environment);
            d.put("approvedBy", approvedBy);
            d.put("approval", approval);
            d.put("expiresAt", expiresAt);
            return d;
        }
    }

    /**
     * The outcome of a check, recorded with the request's audit event. {@code grants} are those it
     * relied on: the allows that permitted it, or the denies that refused it (none when no grant
     * allowed it).
     */
    public record Decision(
            @Nullable Target target,
            @Nullable Privilege needed,
            long policyVersion,
            List<Long> grants) {

        public Map<String, Object> auditDetail() {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("policyVersion", policyVersion);
            if (needed != null) {
                d.put("privilege", needed.name());
            }
            d.put("grants", grants);
            return d;
        }
    }

    /** Actions on one object or database, and the privilege each needs on it. */
    static final Map<String, Privilege> REQUIRED =
            Map.ofEntries(
                    Map.entry("GET_TABLE", Privilege.READ),
                    Map.entry("LOAD_SNAPSHOT", Privilege.READ),
                    Map.entry("LIST_SNAPSHOTS", Privilege.READ),
                    Map.entry("LIST_TAGS", Privilege.READ),
                    Map.entry("GET_TAG", Privilege.READ),
                    Map.entry("LIST_BRANCHES", Privilege.READ),
                    Map.entry("LIST_CONSUMERS", Privilege.READ),
                    Map.entry("LIST_PARTITIONS", Privilege.READ),
                    Map.entry("AUTH_TABLE_QUERY", Privilege.READ),
                    Map.entry("ISSUE_DATA_TOKEN", Privilege.READ),
                    Map.entry("GET_VIEW", Privilege.READ),
                    Map.entry("GET_FUNCTION", Privilege.READ),
                    Map.entry("COMMIT", Privilege.WRITE),
                    Map.entry("ROLLBACK", Privilege.WRITE),
                    Map.entry("CREATE_TAG", Privilege.WRITE),
                    Map.entry("DELETE_TAG", Privilege.WRITE),
                    Map.entry("CREATE_BRANCH", Privilege.WRITE),
                    Map.entry("DROP_BRANCH", Privilege.WRITE),
                    Map.entry("RENAME_BRANCH", Privilege.WRITE),
                    Map.entry("FAST_FORWARD_BRANCH", Privilege.WRITE),
                    Map.entry("CREATE_PARTITIONS", Privilege.WRITE),
                    Map.entry("DROP_PARTITIONS", Privilege.WRITE),
                    Map.entry("MARK_PARTITIONS_DONE", Privilege.WRITE),
                    Map.entry("RESET_CONSUMER", Privilege.WRITE),
                    Map.entry("ALTER_TABLE", Privilege.MANAGE),
                    Map.entry("REPLACE_TABLE", Privilege.MANAGE),
                    Map.entry("ROLLBACK_SCHEMA", Privilege.MANAGE),
                    Map.entry("DROP_TABLE", Privilege.MANAGE),
                    Map.entry("ALTER_VIEW", Privilege.MANAGE),
                    Map.entry("DROP_VIEW", Privilege.MANAGE),
                    Map.entry("ALTER_FUNCTION", Privilege.MANAGE),
                    Map.entry("DROP_FUNCTION", Privilege.MANAGE),
                    // On the database; the handlers also check the named or contained objects.
                    Map.entry("CREATE_TABLE", Privilege.MANAGE),
                    Map.entry("REGISTER_TABLE", Privilege.MANAGE),
                    Map.entry("CREATE_VIEW", Privilege.MANAGE),
                    Map.entry("CREATE_FUNCTION", Privilege.MANAGE),
                    Map.entry("ALTER_DATABASE", Privilege.MANAGE),
                    Map.entry("DROP_DATABASE", Privilege.MANAGE));

    /** Listings and database reads: the database must be visible; results are filtered. */
    static final Set<String> LISTINGS =
            Set.of(
                    "GET_DATABASE",
                    "LIST_DATABASES",
                    "LIST_TABLES",
                    "LIST_TABLE_DETAILS",
                    "LIST_VIEWS",
                    "LIST_VIEW_DETAILS",
                    "LIST_FUNCTIONS",
                    "LIST_FUNCTION_DETAILS");

    /** No catalog object involved. */
    static final Set<String> UNTARGETED = Set.of("GET_CONFIG");

    /** The target is in the body or found by the handler, which checks it. */
    static final Set<String> IN_HANDLER =
            Set.of("CREATE_DATABASE", "RENAME_TABLE", "RENAME_VIEW", "GET_TABLE_BY_ID");

    /** Whether a catalog action has a decision rule; every route's action must. */
    public static boolean classified(String action) {
        return REQUIRED.containsKey(action)
                || LISTINGS.contains(action)
                || UNTARGETED.contains(action)
                || IN_HANDLER.contains(action);
    }

    private record Loaded(List<Grant> grants, long policyVersion, long untilNanos) {}

    private final Db db;
    private final Clock clock;
    private final Duration cacheTtl;
    private final String environment;
    private final Map<String, Loaded> cache = new ConcurrentHashMap<>();

    public Authorizer(Db db, Clock clock, Duration cacheTtl, String environment) {
        this.db = db;
        this.clock = clock;
        this.cacheTtl = cacheTtl;
        this.environment = environment;
    }

    public String environment() {
        return environment;
    }

    // ------------------------------------------------------------------- decisions

    /**
     * Decides a catalog route. {@code path} is the request path after {@code /v1} as segments:
     * {@code [prefix, "databases", db, kind, name, ...]}. A refusal is a 403 carrying its {@link
     * ApiException#denial() decision}; an action that involves no catalog object has none ({@code
     * null}).
     */
    @Nullable
    public Decision check(Principal principal, String action, List<String> path) throws Exception {
        Target target = targetOf(path);
        Privilege needed = REQUIRED.get(action);
        if (needed != null) {
            if (target == null) {
                LOG.error("Action {} needs a target but the path names none: {}", action, path);
                throw ApiException.forbidden(Resources.CATALOG, null, "No target for " + action);
            }
            return require(principal, needed, target);
        }
        if (LISTINGS.contains(action)) {
            if (target != null && !visible(principal, target.database())) {
                throw refused(
                        principal,
                        loaded(principal),
                        Privilege.READ,
                        new Target(Scope.DATABASE, target.database(), null));
            }
            return new Decision(target, null, loaded(principal).policyVersion(), List.of());
        }
        if (UNTARGETED.contains(action)) {
            return null;
        }
        if (IN_HANDLER.contains(action)) {
            return new Decision(null, null, loaded(principal).policyVersion(), List.of());
        }
        // Fail closed: a route nobody classified is never served.
        LOG.error("Unclassified catalog action {}; refusing it", action);
        throw ApiException.forbidden(
                Resources.CATALOG, null, "Action " + action + " is not authorized.");
    }

    /**
     * Throws 403, carrying its {@link ApiException#denial() decision}, unless {@code principal}
     * holds {@code needed} on {@code target}.
     */
    public Decision require(Principal principal, Privilege needed, Target target) throws Exception {
        Loaded loaded = loaded(principal);
        List<Long> allowing = new ArrayList<>();
        for (Grant g : loaded.grants()) {
            if (g.forbids(needed, target)) {
                throw refused(principal, loaded, needed, target);
            }
            if (g.effect() == Effect.ALLOW && g.covers(target) && g.privilege().covers(needed)) {
                allowing.add(g.id());
            }
        }
        if (allowing.isEmpty()) {
            throw refused(principal, loaded, needed, target);
        }
        return new Decision(target, needed, loaded.policyVersion(), allowing);
    }

    public boolean allows(Principal principal, Privilege needed, Target target) throws Exception {
        try {
            require(principal, needed, target);
            return true;
        } catch (ApiException e) {
            if (e.status() == 403) {
                return false;
            }
            throw e;
        }
    }

    public boolean allows(
            Principal principal,
            Privilege needed,
            Scope scope,
            String database,
            @Nullable String name)
            throws Exception {
        return allows(principal, needed, new Target(scope, database, name));
    }

    /**
     * Paimon's default database. Flink and Spark catalogs look it up when they start, and fail if
     * it cannot be seen, so every principal sees it; its objects still need grants.
     */
    public static final String DEFAULT_DATABASE = "default";

    /**
     * Whether the database shows up for the principal: readable as a whole, holding at least one
     * object the principal may read, or the default database. A READ deny on the database or the
     * catalog hides it.
     */
    public boolean visible(Principal principal, String database) throws Exception {
        Target whole = new Target(Scope.DATABASE, database, null);
        if (allows(principal, Privilege.READ, whole)) {
            return true;
        }
        if (DEFAULT_DATABASE.equals(database)) {
            return loaded(principal).grants().stream()
                    .noneMatch(g -> g.forbids(Privilege.READ, whole));
        }
        for (Grant g : loaded(principal).grants()) {
            if (g.effect() == Effect.ALLOW
                    && database.equals(g.database())
                    && g.object() != null
                    && allows(
                            principal,
                            Privilege.READ,
                            new Target(g.scope(), database, g.object()))) {
                return true;
            }
        }
        return false;
    }

    /**
     * The columns the principal may read in a table, or empty for all of them. Only table-level
     * grants decide: a table READ allow with columns limits the principal to them, unless another
     * table-level allow without columns (such as WRITE) applies.
     */
    public Optional<List<String>> readableColumns(
            Principal principal, String database, String table) throws Exception {
        Target target = new Target(Scope.TABLE, database, table);
        List<String> columns = null;
        for (Grant g : loaded(principal).grants()) {
            if (g.scope() == Scope.TABLE && g.effect() == Effect.ALLOW && g.covers(target)) {
                if (g.columns() == null) {
                    return Optional.empty();
                }
                columns = g.columns();
            }
        }
        return Optional.ofNullable(columns);
    }

    /** The target named by a catalog path, if any; tables are judged as their base table. */
    @Nullable
    static Target targetOf(List<String> path) {
        if (path.size() < 3 || !path.get(1).equals("databases")) {
            return null;
        }
        String database = path.get(2);
        if (path.size() < 5) {
            return new Target(Scope.DATABASE, database, null);
        }
        String name = path.get(4);
        return switch (path.get(3)) {
            case "tables" ->
                    new Target(
                            Scope.TABLE,
                            database,
                            Identifier.create(database, name).getTableName());
            case "views" -> new Target(Scope.VIEW, database, name);
            case "functions" -> new Target(Scope.FUNCTION, database, name);
            default -> new Target(Scope.DATABASE, database, null);
        };
    }

    /** The 403 refusing {@code needed} on {@code target}, with the denies that decided it. */
    private static ApiException refused(
            Principal principal, Loaded loaded, Privilege needed, Target target) {
        return ApiException.refused(
                target.scope().resourceType(),
                target.name(),
                target.scope().label
                        + " "
                        + target.name()
                        + " has no permission for "
                        + principal.name()
                        + ".",
                new Decision(
                        target,
                        needed,
                        loaded.policyVersion(),
                        loaded.grants().stream()
                                .filter(g -> g.forbids(needed, target))
                                .map(Grant::id)
                                .toList()));
    }

    // ------------------------------------------------------------------- grants

    private Loaded loaded(Principal principal) throws Exception {
        long now = System.nanoTime();
        Loaded cached = cache.get(principal.name());
        if (cached != null && cached.untilNanos() - now > 0) {
            return cached;
        }
        Loaded fresh =
                db.read(
                        c ->
                                new Loaded(
                                        load(c, principal.name(), true),
                                        policyVersion(c),
                                        now + cacheTtl.toNanos()));
        cache.put(principal.name(), fresh);
        return fresh;
    }

    /** Stores a grant and returns it with the policy version it created. */
    public Map.Entry<Grant, Long> grant(
            String principal,
            Scope scope,
            @Nullable String database,
            @Nullable String object,
            Privilege privilege,
            Effect effect,
            @Nullable List<String> columns,
            Terms terms,
            String by)
            throws Exception {
        if ((scope == Scope.CATALOG) != (database == null)
                || (scope == Scope.CATALOG || scope == Scope.DATABASE) != (object == null)) {
            throw ApiException.badRequest(
                    "CATALOG grants name no database, DATABASE grants a database, and object"
                            + " grants a database and an object.");
        }
        if (columns != null
                && (scope != Scope.TABLE
                        || privilege != Privilege.READ
                        || effect != Effect.ALLOW)) {
            throw ApiException.badRequest("Only a table READ allow can name columns.");
        }
        Instant now = clock.instant();
        if (!terms.expires().isAfter(now)) {
            throw ApiException.badRequest("A grant must expire in the future.");
        }
        for (String field :
                List.of(
                        terms.purpose(),
                        terms.environment(),
                        terms.approvedBy(),
                        terms.approval())) {
            if (field.isBlank()) {
                throw ApiException.badRequest(
                        "A grant names its purpose, environment, approver and approval.");
            }
        }
        Map.Entry<Grant, Long> stored =
                db.inTransaction(
                        c ->
                                insert(
                                        c, principal, scope, database, object, privilege, effect,
                                        columns, terms, now, by));
        cache.remove(principal);
        return stored;
    }

    private Map.Entry<Grant, Long> insert(
            Connection c,
            String principal,
            Scope scope,
            @Nullable String database,
            @Nullable String object,
            Privilege privilege,
            Effect effect,
            @Nullable List<String> columns,
            Terms terms,
            Instant now,
            String by)
            throws SQLException {
        long id;
        try (PreparedStatement insert =
                c.prepareStatement(
                        "INSERT INTO catalog_grants (principal, scope, database_name,"
                                + " object_name, privilege, effect, columns, purpose, environment,"
                                + " approved_by, approval, expires_at, created_at, created_by)"
                                + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id")) {
            insert.setString(1, principal);
            insert.setString(2, scope.name());
            insert.setString(3, database);
            insert.setString(4, object);
            insert.setString(5, privilege.name());
            insert.setString(6, effect.name());
            if (columns == null) {
                insert.setNull(7, Types.ARRAY);
            } else {
                insert.setArray(7, c.createArrayOf("text", columns.toArray()));
            }
            insert.setString(8, terms.purpose());
            insert.setString(9, terms.environment());
            insert.setString(10, terms.approvedBy());
            insert.setString(11, terms.approval());
            insert.setTimestamp(12, Timestamp.from(terms.expires()));
            insert.setTimestamp(13, Timestamp.from(now));
            insert.setString(14, by);
            try (ResultSet rs = insert.executeQuery()) {
                rs.next();
                id = rs.getLong(1);
            }
        } catch (SQLException e) {
            if ("23505".equals(e.getSQLState())) {
                throw ApiException.conflict(
                        Resources.GRANT, principal, "This grant already exists.");
            }
            if ("23503".equals(e.getSQLState())) {
                throw ApiException.notFound(
                        Resources.PRINCIPAL,
                        principal,
                        "Principal " + principal + " does not exist.");
            }
            throw e;
        }
        Grant grant =
                new Grant(
                        id,
                        principal,
                        scope,
                        database,
                        object,
                        privilege,
                        effect,
                        columns == null ? null : List.copyOf(columns),
                        terms.purpose(),
                        terms.environment(),
                        terms.approvedBy(),
                        terms.approval(),
                        terms.expires().toString(),
                        now.toString(),
                        by);
        return Map.entry(grant, advancePolicy(c));
    }

    /** Removes a grant; returns it with the new policy version, or empty if there was none. */
    public Optional<Map.Entry<Grant, Long>> revoke(long id) throws Exception {
        Optional<Map.Entry<Grant, Long>> revoked =
                db.inTransaction(
                        c -> {
                            List<Grant> found =
                                    query(
                                            c,
                                            "SELECT "
                                                    + COLUMNS
                                                    + " FROM catalog_grants WHERE id = ?",
                                            id);
                            if (found.isEmpty()) {
                                return Optional.<Map.Entry<Grant, Long>>empty();
                            }
                            try (PreparedStatement delete =
                                    c.prepareStatement("DELETE FROM catalog_grants WHERE id = ?")) {
                                delete.setLong(1, id);
                                delete.executeUpdate();
                            }
                            return Optional.of(Map.entry(found.get(0), advancePolicy(c)));
                        });
        revoked.ifPresent(r -> cache.remove(r.getKey().principal()));
        return revoked;
    }

    /** Every grant, or those of one principal, expired ones included. */
    public List<Grant> list(@Nullable String principal) throws Exception {
        return db.inTransaction(
                c ->
                        principal == null
                                ? query(c, "SELECT " + COLUMNS + " FROM catalog_grants ORDER BY id")
                                : load(c, principal, false));
    }

    private static final String COLUMNS =
            "id, principal, scope, database_name, object_name, privilege, effect, columns, purpose,"
                    + " environment, approved_by, approval, expires_at, created_at, created_by";

    /** A principal's grants; {@code effective} keeps only unexpired ones of this environment. */
    private List<Grant> load(Connection c, String principal, boolean effective)
            throws SQLException {
        String sql = "SELECT " + COLUMNS + " FROM catalog_grants WHERE principal = ?";
        if (!effective) {
            return query(c, sql + " ORDER BY id", principal);
        }
        return query(
                c,
                sql + " AND environment = ? AND expires_at > ? ORDER BY id",
                principal,
                environment,
                Timestamp.from(clock.instant()));
    }

    private static List<Grant> query(Connection c, String sql, Object... args) throws SQLException {
        try (PreparedStatement select = c.prepareStatement(sql)) {
            for (int i = 0; i < args.length; i++) {
                select.setObject(i + 1, args[i]);
            }
            List<Grant> grants = new ArrayList<>();
            try (ResultSet rs = select.executeQuery()) {
                while (rs.next()) {
                    Array columns = rs.getArray(8);
                    grants.add(
                            new Grant(
                                    rs.getLong(1),
                                    rs.getString(2),
                                    Scope.valueOf(rs.getString(3)),
                                    rs.getString(4),
                                    rs.getString(5),
                                    Privilege.valueOf(rs.getString(6)),
                                    Effect.valueOf(rs.getString(7)),
                                    columns == null
                                            ? null
                                            : List.copyOf(
                                                    Arrays.asList((String[]) columns.getArray())),
                                    rs.getString(9),
                                    rs.getString(10),
                                    rs.getString(11),
                                    rs.getString(12),
                                    rs.getTimestamp(13).toInstant().toString(),
                                    rs.getTimestamp(14).toInstant().toString(),
                                    rs.getString(15)));
                }
            }
            return grants;
        }
    }

    private static long policyVersion(Connection c) throws SQLException {
        try (PreparedStatement select = c.prepareStatement("SELECT version FROM catalog_policy");
                ResultSet rs = select.executeQuery()) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static long advancePolicy(Connection c) throws SQLException {
        try (PreparedStatement update =
                        c.prepareStatement(
                                "UPDATE catalog_policy SET version = version + 1 RETURNING version");
                ResultSet rs = update.executeQuery()) {
            rs.next();
            return rs.getLong(1);
        }
    }

    /** Parses an enum value case-insensitively for the admin API. */
    public static <E extends Enum<E>> E parse(Class<E> type, String field, @Nullable String value) {
        if (value == null) {
            throw ApiException.badRequest("Missing " + field + ".");
        }
        try {
            return Enum.valueOf(type, value.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw ApiException.badRequest(
                    "Unknown "
                            + field
                            + " "
                            + value
                            + "; expected one of "
                            + Arrays.toString(type.getEnumConstants())
                            + ".");
        }
    }
}
