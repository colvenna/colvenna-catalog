package com.colvenna.catalog.token;

import com.colvenna.catalog.ServerConfig;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.RequestContext;
import com.colvenna.catalog.http.ApiException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import software.amazon.awssdk.auth.credentials.AnonymousCredentialsProvider;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.Credentials;

/**
 * Vends per-table S3 credentials by assuming the data role with a session policy limited to the
 * table root.
 *
 * <p>The catalog assumes the role with its own web identity ({@code AssumeRoleWithWebIdentity}):
 * its service-account token on EKS, a Cognito developer identity in the serverless form ({@link
 * CognitoDeveloperIdentity}). It never chains from its own role session: chained sessions are
 * capped at one hour, and Paimon clients refresh any credential with less than an hour left, which
 * would turn every file operation into a token request. Sessions therefore last {@code duration}
 * (more than 90 minutes), and a cached credential is handed out again only while it still has more
 * than {@link #MIN_REMAINING} left.
 *
 * <p>The returned map carries the keys of both readers: {@code fs.s3a.*} for the Paimon S3 FileIO
 * of Flink and Spark, with the {@code s3.access-key} / {@code s3.secret-key} / {@code
 * s3.aws.credentials.provider} aliases it maps onto them; and {@code s3.access-key-id} / {@code
 * s3.secret-access-key} / {@code s3.session-token} / {@code s3.region} for Doris, whose native
 * reader recognises only those.
 */
public final class AwsStsTokenIssuer implements DataTokenIssuer {

    /** Paimon refreshes below one hour; ten more minutes keep a handed-out token stable. */
    static final Duration MIN_REMAINING = Duration.ofMinutes(70);

    static final String TEMPORARY_PROVIDER =
            "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider";

    /** Cached credentials beyond this count trigger a sweep of the expired ones. */
    static final int CACHE_SWEEP_SIZE = 1024;

    /** What every session name starts with: CloudTrail shows it for every object access. */
    static final String SESSION_PREFIX = "colvenna-";

    /** STS's limit on the length of a role session name. */
    static final int MAX_SESSION_NAME = 64;

    /**
     * The STS call with the catalog's web identity, separated so tests can observe the policy
     * without AWS.
     */
    interface Gateway {
        Credentials assume(String roleArn, String sessionName, String policy, Duration duration)
                throws Exception;
    }

    private record CacheKey(String principal, UUID tableId, SessionPolicy.Access access) {}

    private final String roleArn;
    private final String region;
    private final Duration duration;
    private final Gateway gateway;
    private final Clock clock;
    private final Map<CacheKey, Token> cache = new ConcurrentHashMap<>();

    AwsStsTokenIssuer(
            String roleArn, String region, Duration duration, Gateway gateway, Clock clock) {
        if (duration.compareTo(Duration.ofMinutes(90)) < 0
                || duration.compareTo(Duration.ofHours(12)) > 0) {
            throw new IllegalArgumentException(
                    "Data token duration must be between PT1H30M and PT12H; Paimon clients refresh"
                            + " tokens with less than an hour left.");
        }
        this.roleArn = roleArn;
        this.region = region;
        this.duration = duration;
        this.gateway = gateway;
        this.clock = clock;
    }

    /**
     * An issuer calling STS with the configured web identity: the token at {@code
     * AWS_WEB_IDENTITY_TOKEN_FILE}, or a Cognito developer identity ({@link
     * CognitoDeveloperIdentity}).
     */
    public static AwsStsTokenIssuer create(ServerConfig.DataTokens settings, Clock clock) {
        String region = Objects.requireNonNull(settings.region());
        Gateway gateway =
                switch (settings.webIdentity()) {
                    case TOKEN_FILE -> tokenFile(region);
                    case COGNITO ->
                            CognitoDeveloperIdentity.create(
                                    Objects.requireNonNull(settings.cognitoIdentityPoolId()),
                                    Objects.requireNonNull(settings.cognitoDeveloperProvider()),
                                    settings.cognitoRegion(),
                                    region);
                };
        return new AwsStsTokenIssuer(
                Objects.requireNonNull(settings.roleArn()),
                region,
                settings.duration(),
                gateway,
                clock);
    }

    /** {@code AssumeRoleWithWebIdentity} with the token at {@code AWS_WEB_IDENTITY_TOKEN_FILE}. */
    private static Gateway tokenFile(String region) {
        String tokenFile = System.getenv("AWS_WEB_IDENTITY_TOKEN_FILE");
        if (tokenFile == null || tokenFile.isBlank()) {
            throw new IllegalStateException(
                    "AWS_WEB_IDENTITY_TOKEN_FILE is not set; the catalog needs its IRSA token to"
                            + " assume the data role.");
        }
        return tokenFile(anonymousSts(region), Path.of(tokenFile));
    }

    /** As above with a given client; the file is read for every call, as IRSA rotates it. */
    static Gateway tokenFile(StsClient sts, Path tokenFile) {
        return (role, session, policy, sessionDuration) ->
                assumeRoleWithWebIdentity(
                        sts,
                        role,
                        session,
                        Files.readString(tokenFile, StandardCharsets.UTF_8),
                        policy,
                        sessionDuration);
    }

    /**
     * The STS client of {@code AssumeRoleWithWebIdentity}, which takes no credentials: the web
     * identity token is the proof. It lives as long as the process, like the issuer.
     */
    static StsClient anonymousSts(String region) {
        return StsClient.builder()
                .region(Region.of(region))
                .credentialsProvider(AnonymousCredentialsProvider.create())
                .build();
    }

    /**
     * Assumes {@code roleArn} with a web identity token and a session policy, whichever source the
     * token came from. Throttling by STS is a retryable 503: the token request has no side effects,
     * and Paimon's client repeats a 503 by itself.
     */
    static Credentials assumeRoleWithWebIdentity(
            StsClient sts,
            String roleArn,
            String sessionName,
            String webIdentityToken,
            String policy,
            Duration duration) {
        try {
            return sts.assumeRoleWithWebIdentity(
                            r ->
                                    r.roleArn(roleArn)
                                            .roleSessionName(sessionName)
                                            .webIdentityToken(webIdentityToken)
                                            .policy(policy)
                                            .durationSeconds((int) duration.toSeconds()))
                    .credentials();
        } catch (AwsServiceException e) {
            throw e.isThrottlingException() ? throttled("AWS STS", e) : e;
        }
    }

    /** The answer to a credential request that {@code service} throttled. */
    static ApiException throttled(String service, AwsServiceException e) {
        return ApiException.unavailable(
                service + " is throttling the catalog's credential requests; retry.", e);
    }

    @Override
    public Token issue(RequestContext ctx, TableRecord table, SessionPolicy.Access access)
            throws Exception {
        if (table.external()) {
            throw ApiException.badRequest(
                    "Data tokens cover managed tables only; "
                            + table.fullName()
                            + " lives outside the catalog warehouse.");
        }
        SessionPolicy.Location location;
        try {
            location = SessionPolicy.Location.of(table.path());
        } catch (IllegalArgumentException e) {
            throw ApiException.notImplemented(e.getMessage());
        }
        CacheKey key = new CacheKey(ctx.who(), table.id(), access);
        Instant now = clock.instant();
        Token cached = cache.get(key);
        if (cached != null
                && Instant.ofEpochMilli(cached.expiresAtMillis())
                        .isAfter(now.plus(MIN_REMAINING))) {
            Map<String, Object> detail = new LinkedHashMap<>(cached.detail());
            detail.put("cached", true);
            return new Token(cached.properties(), cached.expiresAtMillis(), detail);
        }
        String session = sessionName(ctx.who());
        Credentials credentials =
                gateway.assume(roleArn, session, SessionPolicy.json(location, access), duration);
        Map<String, String> properties = new LinkedHashMap<>();
        properties.put("fs.s3a.access.key", credentials.accessKeyId());
        properties.put("fs.s3a.secret.key", credentials.secretAccessKey());
        properties.put("fs.s3a.session.token", credentials.sessionToken());
        properties.put("fs.s3a.aws.credentials.provider", TEMPORARY_PROVIDER);
        properties.put("fs.s3a.endpoint.region", region);
        // The s3.* aliases that Paimon's S3 FileIO maps onto fs.s3a.*: set too, so that static
        // s3.* keys in a client's own catalog options cannot take precedence.
        properties.put("s3.access-key", credentials.accessKeyId());
        properties.put("s3.secret-key", credentials.secretAccessKey());
        properties.put("s3.aws.credentials.provider", TEMPORARY_PROVIDER);
        // The only keys the Doris native reader understands.
        properties.put("s3.access-key-id", credentials.accessKeyId());
        properties.put("s3.secret-access-key", credentials.secretAccessKey());
        properties.put("s3.session-token", credentials.sessionToken());
        properties.put("s3.region", region);
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("roleSessionName", session);
        // Not a secret; matches the CloudTrail data events of this credential to this issuance.
        detail.put("accessKeyId", credentials.accessKeyId());
        detail.put("cached", false);
        detail.put("access", access.name());
        detail.put("prefix", "s3://" + location.bucket() + "/" + location.prefix() + "/");
        detail.put("expiresAt", credentials.expiration().toString());
        Token token =
                new Token(Map.copyOf(properties), credentials.expiration().toEpochMilli(), detail);
        if (cache.size() >= CACHE_SWEEP_SIZE) {
            long nowMillis = now.toEpochMilli();
            cache.values().removeIf(t -> t.expiresAtMillis() <= nowMillis);
        }
        cache.put(key, token);
        return token;
    }

    /**
     * {@code colvenna-<principal>}, which CloudTrail shows for every object access. STS session
     * names allow {@code [\w+=,.@-]} up to 64 characters; a principal name that does not fit as is
     * (a {@code :}, or too long) gets a hash suffix, so two principals never share a session name.
     */
    static String sessionName(String principal) {
        String name = SESSION_PREFIX + principal;
        if (name.length() <= MAX_SESSION_NAME && name.matches("[\\w+=,.@-]+")) {
            return name;
        }
        String cleaned = principal.replaceAll("[^\\w+=,.@-]", "-");
        String hash = HexFormat.of().formatHex(sha256(principal)).substring(0, 8);
        // The prefix, as much of the name as fits, "-" and the hash: 64 characters at most.
        int kept = MAX_SESSION_NAME - SESSION_PREFIX.length() - 1 - hash.length();
        return SESSION_PREFIX + cleaned.substring(0, Math.min(cleaned.length(), kept)) + "-" + hash;
    }

    private static byte[] sha256(String value) {
        try {
            return MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
