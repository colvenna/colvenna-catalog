package com.colvenna.catalog.auth;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.http.ApiException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;
import javax.annotation.Nullable;

/**
 * API keys carried as {@code Authorization: Bearer <token>}.
 *
 * <p>A token is 32 random bytes, returned in plain text once when it is issued. PostgreSQL keeps
 * its SHA-256, the owning principal, an expiry and a revocation mark. Expired, revoked, unknown or
 * disabled-principal tokens fail with 401. Successful lookups are cached for {@code cacheTtl},
 * which bounds how long a revocation takes to reach every replica.
 */
public final class TokenService {

    /** Prefix that makes leaked colvenna-catalog tokens easy to recognize in secret scanners. */
    public static final String TOKEN_PREFIX = "colvenna_";

    /** A freshly issued token. {@code token} is the only copy of the secret. */
    public record IssuedToken(UUID id, String principal, String token, Instant expiresAt) {}

    /** Token metadata without the secret. */
    public record TokenInfo(
            UUID id,
            String principal,
            @Nullable String description,
            Instant createdAt,
            String createdBy,
            Instant expiresAt,
            @Nullable Instant revokedAt,
            @Nullable String revokedBy) {}

    /** A registered service identity. */
    public record PrincipalInfo(
            String name,
            @Nullable String description,
            boolean admin,
            Instant createdAt,
            String createdBy,
            @Nullable Instant disabledAt) {}

    private record CachedPrincipal(Principal principal, Instant expiresAt, long cachedUntilNanos) {}

    private static final SecureRandom RANDOM = new SecureRandom();

    private final Db db;
    private final Clock clock;
    private final Duration cacheTtl;
    private final Map<String, CachedPrincipal> cache = new ConcurrentHashMap<>();

    /**
     * Recently rejected token hashes, so repeating a bad token costs no database lookup. Bounded; a
     * flood of distinct random tokens still reaches the database, but only the auth pool.
     */
    private final Map<String, Rejection> rejected = new ConcurrentHashMap<>();

    /** Database lookups of uncached tokens in flight. */
    private final Semaphore lookups = new Semaphore(64);

    private record Rejection(long untilNanos, String message) {}

    static final Duration REJECTED_TTL = Duration.ofSeconds(30);
    static final int MAX_REJECTED = 10_000;

    public TokenService(Db db, Clock clock, Duration cacheTtl) {
        this.db = db;
        this.clock = clock;
        this.cacheTtl = cacheTtl;
    }

    /** Resolves the bearer token of a request, or fails with 401. */
    public Principal authenticate(@Nullable String authorizationHeader) throws Exception {
        if (authorizationHeader == null || !authorizationHeader.startsWith("Bearer ")) {
            throw ApiException.unauthorized("Missing bearer token.");
        }
        String token = authorizationHeader.substring("Bearer ".length()).trim();
        if (token.isEmpty()) {
            throw ApiException.unauthorized("Missing bearer token.");
        }
        String hash = hashHex(token);
        long now = System.nanoTime();
        CachedPrincipal cached = cache.get(hash);
        if (cached != null
                && cached.cachedUntilNanos - now > 0
                && cached.expiresAt.isAfter(clock.instant())) {
            return cached.principal;
        }
        cache.remove(hash);
        Rejection rejection = rejected.get(hash);
        if (rejection != null && rejection.untilNanos() - now > 0) {
            throw ApiException.unauthorized(rejection.message());
        }
        // Beyond the lookup limit an uncached token fails at once with a retryable 503: waiting
        // here would hold a request slot, and a flood of bad tokens could then fill them all.
        if (!lookups.tryAcquire()) {
            throw ApiException.unavailable("Too many token lookups in flight.", null);
        }
        try {
            CachedPrincipal fresh = lookup(token, now);
            rejected.remove(hash);
            cache.put(hash, fresh);
            return fresh.principal;
        } catch (ApiException e) {
            if (e.status() == 401) {
                if (rejected.size() >= MAX_REJECTED) {
                    rejected.clear();
                }
                rejected.put(hash, new Rejection(now + REJECTED_TTL.toNanos(), e.getMessage()));
            }
            throw e;
        } finally {
            lookups.release();
        }
    }

    private CachedPrincipal lookup(String token, long nowNanos) throws Exception {
        return db.read(
                Db.Pool.AUTH,
                connection -> {
                    try (PreparedStatement select =
                            connection.prepareStatement(
                                    "SELECT t.id, t.expires_at, t.revoked_at, p.name, p.is_admin,"
                                            + " p.disabled_at FROM api_tokens t JOIN principals p"
                                            + " ON p.name = t.principal WHERE t.token_hash = ?")) {
                        select.setBytes(1, hash(token));
                        try (ResultSet rs = select.executeQuery()) {
                            if (!rs.next()) {
                                throw ApiException.unauthorized("Unknown bearer token.");
                            }
                            UUID id = rs.getObject(1, UUID.class);
                            Instant expiresAt = rs.getTimestamp(2).toInstant();
                            if (rs.getTimestamp(3) != null) {
                                throw ApiException.unauthorized("Bearer token has been revoked.");
                            }
                            if (!expiresAt.isAfter(clock.instant())) {
                                throw ApiException.unauthorized("Bearer token has expired.");
                            }
                            if (rs.getTimestamp(6) != null) {
                                throw ApiException.unauthorized("Principal is disabled.");
                            }
                            return new CachedPrincipal(
                                    new Principal(rs.getString(4), rs.getBoolean(5), id),
                                    expiresAt,
                                    nowNanos + cacheTtl.toNanos());
                        }
                    }
                });
    }

    public void createPrincipal(
            String name, @Nullable String description, boolean admin, String createdBy)
            throws Exception {
        validatePrincipalName(name);
        db.inTransaction(
                connection -> {
                    try (PreparedStatement insert =
                            connection.prepareStatement(
                                    "INSERT INTO principals (name, description, is_admin, created_by)"
                                            + " VALUES (?, ?, ?, ?) ON CONFLICT (name) DO NOTHING")) {
                        insert.setString(1, name);
                        insert.setString(2, description);
                        insert.setBoolean(3, admin);
                        insert.setString(4, createdBy);
                        if (insert.executeUpdate() == 0) {
                            throw ApiException.conflict(
                                    "PRINCIPAL", name, "Principal " + name + " already exists.");
                        }
                    }
                    return null;
                });
    }

    /** Disables a principal; its tokens stop authenticating within the cache TTL. */
    public void disablePrincipal(String name, String disabledBy) throws Exception {
        db.inTransaction(
                connection -> {
                    try (PreparedStatement update =
                            connection.prepareStatement(
                                    "UPDATE principals SET disabled_at = now(), disabled_by = ?"
                                            + " WHERE name = ? AND disabled_at IS NULL")) {
                        update.setString(1, disabledBy);
                        update.setString(2, name);
                        if (update.executeUpdate() == 0 && !principalExists(connection, name)) {
                            throw ApiException.notFound(
                                    "PRINCIPAL", name, "Principal " + name + " does not exist.");
                        }
                    }
                    return null;
                });
        cache.clear();
    }

    public List<PrincipalInfo> listPrincipals() throws Exception {
        return db.inTransaction(
                connection -> {
                    List<PrincipalInfo> principals = new ArrayList<>();
                    try (PreparedStatement select =
                                    connection.prepareStatement(
                                            "SELECT name, description, is_admin, created_at,"
                                                    + " created_by, disabled_at FROM principals"
                                                    + " ORDER BY name");
                            ResultSet rs = select.executeQuery()) {
                        while (rs.next()) {
                            principals.add(
                                    new PrincipalInfo(
                                            rs.getString(1),
                                            rs.getString(2),
                                            rs.getBoolean(3),
                                            rs.getTimestamp(4).toInstant(),
                                            rs.getString(5),
                                            instantOrNull(rs.getTimestamp(6))));
                        }
                    }
                    return principals;
                });
    }

    public IssuedToken issue(
            String principal, @Nullable String description, Duration ttl, String createdBy)
            throws Exception {
        if (ttl.isNegative() || ttl.isZero()) {
            throw ApiException.badRequest("Token lifetime must be positive.");
        }
        byte[] secret = new byte[32];
        RANDOM.nextBytes(secret);
        String token =
                TOKEN_PREFIX + Base64.getUrlEncoder().withoutPadding().encodeToString(secret);
        UUID id = UUID.randomUUID();
        Instant expiresAt = clock.instant().plus(ttl);
        db.inTransaction(
                connection -> {
                    if (!principalExists(connection, principal)) {
                        throw ApiException.notFound(
                                "PRINCIPAL",
                                principal,
                                "Principal " + principal + " does not exist.");
                    }
                    try (PreparedStatement insert =
                            connection.prepareStatement(
                                    "INSERT INTO api_tokens (id, principal, token_hash, description,"
                                            + " created_by, expires_at) VALUES (?, ?, ?, ?, ?, ?)")) {
                        insert.setObject(1, id);
                        insert.setString(2, principal);
                        insert.setBytes(3, hash(token));
                        insert.setString(4, description);
                        insert.setString(5, createdBy);
                        insert.setTimestamp(6, Timestamp.from(expiresAt));
                        insert.executeUpdate();
                    }
                    return null;
                });
        return new IssuedToken(id, principal, token, expiresAt);
    }

    public void revoke(UUID tokenId, String revokedBy) throws Exception {
        db.inTransaction(
                connection -> {
                    try (PreparedStatement update =
                            connection.prepareStatement(
                                    "UPDATE api_tokens SET revoked_at = now(), revoked_by = ?"
                                            + " WHERE id = ?")) {
                        update.setString(1, revokedBy);
                        update.setObject(2, tokenId);
                        if (update.executeUpdate() == 0) {
                            throw ApiException.notFound(
                                    "TOKEN", tokenId.toString(), "Token does not exist.");
                        }
                    }
                    return null;
                });
        cache.clear();
    }

    public List<TokenInfo> listTokens(String principal) throws Exception {
        return db.inTransaction(
                connection -> {
                    List<TokenInfo> tokens = new ArrayList<>();
                    try (PreparedStatement select =
                            connection.prepareStatement(
                                    "SELECT id, principal, description, created_at, created_by,"
                                            + " expires_at, revoked_at, revoked_by FROM api_tokens"
                                            + " WHERE principal = ? ORDER BY created_at")) {
                        select.setString(1, principal);
                        try (ResultSet rs = select.executeQuery()) {
                            while (rs.next()) {
                                tokens.add(
                                        new TokenInfo(
                                                rs.getObject(1, UUID.class),
                                                rs.getString(2),
                                                rs.getString(3),
                                                rs.getTimestamp(4).toInstant(),
                                                rs.getString(5),
                                                rs.getTimestamp(6).toInstant(),
                                                instantOrNull(rs.getTimestamp(7)),
                                                rs.getString(8)));
                            }
                        }
                    }
                    return tokens;
                });
    }

    /**
     * Creates the admin principal if it is missing and issues it a token. Used once per install to
     * hand the first credential to an operator.
     */
    public IssuedToken bootstrapAdmin(String name, Duration ttl) throws Exception {
        db.inTransaction(
                connection -> {
                    try (PreparedStatement insert =
                            connection.prepareStatement(
                                    "INSERT INTO principals (name, description, is_admin, created_by)"
                                            + " VALUES (?, 'bootstrap administrator', true,"
                                            + " 'bootstrap') ON CONFLICT (name) DO NOTHING")) {
                        insert.setString(1, name);
                        insert.executeUpdate();
                    }
                    try (PreparedStatement check =
                            connection.prepareStatement(
                                    "SELECT is_admin FROM principals WHERE name = ?")) {
                        check.setString(1, name);
                        try (ResultSet rs = check.executeQuery()) {
                            if (!rs.next() || !rs.getBoolean(1)) {
                                throw new IllegalStateException(
                                        "Principal " + name + " exists but is not an admin.");
                            }
                        }
                    }
                    return null;
                });
        return issue(name, "bootstrap", ttl, "bootstrap");
    }

    private static boolean principalExists(Connection connection, String name) throws Exception {
        try (PreparedStatement select =
                connection.prepareStatement("SELECT 1 FROM principals WHERE name = ?")) {
            select.setString(1, name);
            try (ResultSet rs = select.executeQuery()) {
                return rs.next();
            }
        }
    }

    static void validatePrincipalName(String name) {
        if (name == null || !name.matches("[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")) {
            throw ApiException.badRequest(
                    "Principal names use letters, digits and ._:@- and start with a letter or"
                            + " digit.");
        }
    }

    @Nullable
    private static Instant instantOrNull(@Nullable Timestamp timestamp) {
        return timestamp == null ? null : timestamp.toInstant();
    }

    static byte[] hash(String token) {
        try {
            return MessageDigest.getInstance("SHA-256")
                    .digest(token.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hashHex(String token) {
        return java.util.HexFormat.of().formatHex(hash(token));
    }
}
