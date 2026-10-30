package com.colvenna.catalog;

import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.TablePurges;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.RdsIamCredentials;
import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import javax.annotation.Nullable;
import software.amazon.awssdk.regions.Region;

/**
 * Server settings, read from {@code COLVENNA_*} environment variables.
 *
 * <p>Every variable prefixed with {@code COLVENNA_FILEIO_} is passed to the server-side Paimon
 * FileIO with the prefix removed, lower-cased and with {@code _} turned into {@code .}; for example
 * {@code COLVENNA_FILEIO_S3_AWS_CREDENTIALS_PROVIDER} becomes {@code s3.aws.credentials.provider}.
 * Use {@code COLVENNA_FILEIO_OPTIONS} for keys that do not survive that mapping (comma-separated
 * {@code k=v}).
 *
 * <p>The API port serves HTTPS from the PKCS12 key store {@code COLVENNA_TLS_KEYSTORE}. Plain HTTP
 * is refused unless {@code COLVENNA_ALLOW_PLAINTEXT=true}, which only tests and local development
 * set, or, in a Lambda execution environment only, {@code COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT=true},
 * which serves it on 127.0.0.1 alone. {@code COLVENNA_EXTERNAL_LOCATION_ROOTS} lists the roots
 * under which clients may place external tables and data paths (comma-separated; empty disables
 * client-chosen locations).
 *
 * <p>{@code serve} applies pending schema migrations before it listens unless {@code
 * COLVENNA_MIGRATE_ON_START=false}, which is the default in a Lambda execution environment (where
 * {@code AWS_LAMBDA_RUNTIME_API} is set): there the installer runs {@code migrate}, and requests
 * are refused until the schema is at this server's version.
 */
public record ServerConfig(
        int port,
        int healthPort,
        int metricsPort,
        @Nullable Tls tls,
        String jdbcUrl,
        String jdbcUser,
        @Nullable String jdbcPassword,
        int jdbcPoolSize,
        int jdbcAuxPoolSize,
        String warehouseName,
        String warehouseRoot,
        List<String> externalLocationRoots,
        Map<String, String> fileIoOptions,
        Map<String, String> clientOverrides,
        int defaultPageSize,
        int maxPageSize,
        int maxInFlightRequests,
        Duration authCacheTtl,
        boolean plaintextAllowed,
        DataTokens dataTokens,
        String environment,
        boolean migrateOnStart,
        boolean lambdaRuntime,
        Timeouts timeouts,
        int maxResponseBytes,
        boolean lambdaLoopbackPlaintext,
        JdbcAuth jdbcAuth,
        TablePurges.Mode dropPurge,
        Duration requestWorkTime,
        Shutdown shutdown) {

    /**
     * How the server signs in to PostgreSQL ({@code COLVENNA_JDBC_AUTH}): {@code password}, the
     * default and the EKS form's, with {@code COLVENNA_JDBC_PASSWORD}; or {@code iam}, the
     * serverless form's, with an RDS IAM authentication token for each new connection, signed with
     * the server's own AWS credentials for the Region {@code COLVENNA_JDBC_IAM_REGION} (default
     * {@code AWS_REGION}). With {@code iam} no password is taken: {@code COLVENNA_JDBC_PASSWORD} is
     * refused, so that none is left in the environment.
     */
    public record JdbcAuth(Method method, @Nullable String region) {

        /** The two ways; see {@link JdbcAuth}. */
        public enum Method {
            PASSWORD,
            IAM;

            static Method parse(String value) {
                return switch (value.trim().toLowerCase(Locale.ROOT)) {
                    case "password" -> PASSWORD;
                    case "iam" -> IAM;
                    default ->
                            throw new IllegalArgumentException(
                                    "COLVENNA_JDBC_AUTH must be password or iam, not " + value);
                };
            }
        }

        public static final JdbcAuth PASSWORD = new JdbcAuth(Method.PASSWORD, null);

        public JdbcAuth {
            Objects.requireNonNull(method, "method");
            if (method == Method.IAM && (region == null || region.isBlank())) {
                throw new IllegalArgumentException(
                        "COLVENNA_JDBC_AUTH=iam needs COLVENNA_JDBC_IAM_REGION or AWS_REGION: the"
                                + " Region the database's tokens are signed for.");
            }
        }
    }

    /** The server certificate key store of the API port. */
    public record Tls(String keystorePath, String keystorePassword) {}

    /**
     * Waits that have to end before the entry in front of the server gives up (API Gateway's
     * integration timeout is 29 seconds): the main pool's PostgreSQL {@code lock_timeout} ({@code
     * COLVENNA_JDBC_LOCK_TIMEOUT}), and the call and per-attempt timeouts of the S3 client that
     * publishes snapshots ({@code COLVENNA_S3_CALL_TIMEOUT}, {@code
     * COLVENNA_S3_CALL_ATTEMPT_TIMEOUT}). {@link #DEFAULTS} are the EKS form's, {@link
     * #LAMBDA_DEFAULTS} those of a Lambda execution environment, where {@link #requireUnder} holds
     * the waits under the gateway's timeout.
     */
    public record Timeouts(
            Duration lockTimeout, Duration s3CallTimeout, Duration s3CallAttemptTimeout) {

        public static final Timeouts DEFAULTS =
                new Timeouts(
                        Db.DEFAULT_LOCK_TIMEOUT, Duration.ofSeconds(30), Duration.ofSeconds(10));

        /** With the connection wait, 10 + 10 + 8 = 28 seconds: under the gateway's 29. */
        public static final Timeouts LAMBDA_DEFAULTS =
                new Timeouts(Duration.ofSeconds(10), Duration.ofSeconds(8), Duration.ofSeconds(3));

        public Timeouts {
            Objects.requireNonNull(lockTimeout, "lockTimeout");
            Objects.requireNonNull(s3CallTimeout, "s3CallTimeout");
            Objects.requireNonNull(s3CallAttemptTimeout, "s3CallAttemptTimeout");
            Db.requireLockTimeout(lockTimeout);
            if (s3CallAttemptTimeout.toMillis() < 1
                    || s3CallTimeout.compareTo(s3CallAttemptTimeout) < 0) {
                throw new IllegalArgumentException(
                        "COLVENNA_S3_CALL_ATTEMPT_TIMEOUT must be positive and not longer than"
                                + " COLVENNA_S3_CALL_TIMEOUT.");
            }
            if (s3CallTimeout.compareTo(CommitService.ABANDON_AFTER) >= 0) {
                // A conditional put still running then could land after its intent was closed.
                throw new IllegalArgumentException(
                        "COLVENNA_S3_CALL_TIMEOUT must be shorter than "
                                + CommitService.ABANDON_AFTER
                                + ", after which a commit that published nothing is abandoned.");
            }
        }

        /**
         * Refuses waits that would outlast {@code gateway}, the timeout of the entry in front of
         * the server: each one on its own, and the longest a commit can wait in sequence, for a
         * connection ({@link Db#CONNECTION_TIMEOUT}, fixed), a lock and the S3 call. The server
         * then answers before the entry gives up.
         */
        public void requireUnder(Duration gateway) {
            Map<String, Duration> waits = new java.util.LinkedHashMap<>();
            waits.put("COLVENNA_JDBC_LOCK_TIMEOUT", lockTimeout);
            waits.put("COLVENNA_S3_CALL_TIMEOUT", s3CallTimeout);
            waits.put("COLVENNA_S3_CALL_ATTEMPT_TIMEOUT", s3CallAttemptTimeout);
            waits.forEach(
                    (name, wait) -> {
                        if (wait.compareTo(gateway) >= 0) {
                            throw new IllegalArgumentException(
                                    name
                                            + " is "
                                            + wait
                                            + ": in Lambda every wait must be shorter than API"
                                            + " Gateway's "
                                            + gateway
                                            + " integration timeout.");
                        }
                    });
            Duration sum = Db.CONNECTION_TIMEOUT.plus(lockTimeout).plus(s3CallTimeout);
            if (sum.compareTo(gateway) >= 0) {
                throw new IllegalArgumentException(
                        "In Lambda the connection wait ("
                                + Db.CONNECTION_TIMEOUT
                                + ", fixed) + COLVENNA_JDBC_LOCK_TIMEOUT ("
                                + lockTimeout
                                + ") + COLVENNA_S3_CALL_TIMEOUT ("
                                + s3CallTimeout
                                + ") = "
                                + sum
                                + " must be shorter than API Gateway's "
                                + gateway
                                + " integration timeout.");
            }
        }
    }

    /**
     * How the server stops when it is told to (SIGTERM; #64). Its readiness fails at once, and
     * every answer from then on asks its client to close the connection ({@code Connection:
     * close}), so that the client's next request opens a new one, to a replica that is not
     * stopping. It goes on taking connections for {@code drain} ({@code COLVENNA_SHUTDOWN_DRAIN}),
     * for clients still sent to it; then it stops taking them, and gives the requests in flight up
     * to {@code timeout} ({@code COLVENNA_SHUTDOWN_TIMEOUT}, in whole seconds, rounded up) to
     * finish before it closes what is left. {@link #DEFAULTS} are the EKS form's, which fit in
     * Kubernetes' default grace period of 30 seconds; in a Lambda execution environment ({@link
     * #LAMBDA_DEFAULTS}) nothing is sent to an environment that is shut down, nor in flight in it,
     * so neither waits.
     */
    public record Shutdown(Duration drain, Duration timeout) {

        public static final Shutdown DEFAULTS =
                new Shutdown(Duration.ofSeconds(5), Duration.ofSeconds(20));

        public static final Shutdown LAMBDA_DEFAULTS = new Shutdown(Duration.ZERO, Duration.ZERO);

        public Shutdown {
            Objects.requireNonNull(drain, "drain");
            Objects.requireNonNull(timeout, "timeout");
            if (drain.isNegative() || timeout.isNegative()) {
                throw new IllegalArgumentException(
                        "COLVENNA_SHUTDOWN_DRAIN and COLVENNA_SHUTDOWN_TIMEOUT must be PT0S or"
                                + " positive durations.");
            }
        }

        /** {@link #timeout} in whole seconds, rounded up: what {@code HttpServer.stop} takes. */
        public int timeoutSeconds() {
            long seconds = timeout.toSeconds();
            if (timeout.getNano() > 0) {
                seconds++;
            }
            return (int) Math.min(Integer.MAX_VALUE, seconds);
        }
    }

    /** API Gateway's REST integration timeout, which the Lambda form's waits stay under. */
    public static final Duration GATEWAY_TIMEOUT = Duration.ofSeconds(29);

    /**
     * How long, in a Lambda execution environment, a request works through what grows with the data
     * before it answers to be asked again ({@code COLVENNA_REQUEST_WORK_TIME}): a consistency
     * check's steps, then a page token; a database drop's tables, then a retryable 503 (#69). With
     * the connection wait and the step in hand, under the gateway's timeout (the LLD's "无服务器形态").
     */
    public static final Duration LAMBDA_REQUEST_WORK_TIME = Duration.ofSeconds(8);

    /**
     * Per-table credential vending: {@link Mode#DISABLED}, or {@link Mode#AWS_STS} assuming {@code
     * roleArn} in {@code region} for {@code duration}, with the web identity {@code webIdentity}
     * names: the IRSA token file, or a Cognito developer identity of {@code cognitoIdentityPoolId}
     * under the developer provider {@code cognitoDeveloperProvider}.
     */
    public record DataTokens(
            Mode mode,
            @Nullable String roleArn,
            @Nullable String region,
            Duration duration,
            WebIdentity webIdentity,
            @Nullable String cognitoIdentityPoolId,
            @Nullable String cognitoDeveloperProvider) {

        /** Values of {@code COLVENNA_DATA_TOKEN_MODE}. */
        public enum Mode {
            DISABLED("disabled"),
            AWS_STS("aws-sts");

            private final String value;

            Mode(String value) {
                this.value = value;
            }

            static Mode parse(String value) {
                for (Mode mode : values()) {
                    if (mode.value.equals(value)) {
                        return mode;
                    }
                }
                throw new IllegalArgumentException(
                        "COLVENNA_DATA_TOKEN_MODE must be disabled or aws-sts, not " + value);
            }
        }

        /**
         * Values of {@code COLVENNA_DATA_TOKEN_WEB_IDENTITY}: what the catalog presents to {@code
         * AssumeRoleWithWebIdentity}.
         */
        public enum WebIdentity {
            /** The token at {@code AWS_WEB_IDENTITY_TOKEN_FILE}: IRSA on EKS. */
            TOKEN_FILE("token-file"),
            /** An OpenID token of a Cognito developer identity: the serverless form. */
            COGNITO("cognito");

            private final String value;

            WebIdentity(String value) {
                this.value = value;
            }

            static WebIdentity parse(String value) {
                for (WebIdentity identity : values()) {
                    if (identity.value.equals(value)) {
                        return identity;
                    }
                }
                throw new IllegalArgumentException(
                        "COLVENNA_DATA_TOKEN_WEB_IDENTITY must be token-file or cognito, not "
                                + value);
            }
        }

        /** {@code <region>:<id>}, the form of a Cognito identity pool id. */
        private static final java.util.regex.Pattern POOL_ID =
                java.util.regex.Pattern.compile("[a-z]{2}(-[a-z]+)+-[0-9]+:[0-9a-f-]+");

        public DataTokens {
            Objects.requireNonNull(mode, "mode");
            Objects.requireNonNull(duration, "duration");
            Objects.requireNonNull(webIdentity, "webIdentity");
            if (mode == Mode.AWS_STS && (isBlank(roleArn) || isBlank(region))) {
                throw new IllegalArgumentException(
                        "COLVENNA_DATA_TOKEN_MODE=aws-sts needs COLVENNA_DATA_TOKEN_ROLE_ARN and"
                                + " COLVENNA_DATA_TOKEN_REGION (or AWS_REGION).");
            }
            if (mode == Mode.AWS_STS && webIdentity == WebIdentity.COGNITO) {
                if (isBlank(cognitoIdentityPoolId) || isBlank(cognitoDeveloperProvider)) {
                    throw new IllegalArgumentException(
                            "COLVENNA_DATA_TOKEN_WEB_IDENTITY=cognito needs"
                                    + " COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID and"
                                    + " COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER.");
                }
                if (!POOL_ID.matcher(cognitoIdentityPoolId).matches()) {
                    throw new IllegalArgumentException(
                            "COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID must be an identity pool"
                                    + " id, <region>:<id>, not "
                                    + cognitoIdentityPoolId);
                }
            }
        }

        /** Vending with the IRSA token file. */
        public DataTokens(
                Mode mode, @Nullable String roleArn, @Nullable String region, Duration duration) {
            this(mode, roleArn, region, duration, WebIdentity.TOKEN_FILE, null, null);
        }

        public static DataTokens disabled() {
            return new DataTokens(Mode.DISABLED, null, null, Duration.ofHours(3));
        }

        /** The region of the identity pool, which its id starts with. */
        public String cognitoRegion() {
            String pool = Objects.requireNonNull(cognitoIdentityPoolId, "cognitoIdentityPoolId");
            return pool.substring(0, pool.indexOf(':'));
        }

        private static boolean isBlank(@Nullable String value) {
            return value == null || value.isBlank();
        }
    }

    public ServerConfig {
        Objects.requireNonNull(environment, "COLVENNA_ENVIRONMENT");
        Objects.requireNonNull(dataTokens, "dataTokens");
        Objects.requireNonNull(timeouts, "timeouts");
        // Hikari would quietly use 10 connections for a size below 1, breaking a connection budget.
        if (jdbcPoolSize < 1 || jdbcAuxPoolSize < 1) {
            throw new IllegalArgumentException(
                    "COLVENNA_JDBC_POOL_SIZE and COLVENNA_JDBC_AUX_POOL_SIZE must be at least 1.");
        }
        if (maxResponseBytes < 0) {
            throw new IllegalArgumentException(
                    "COLVENNA_MAX_RESPONSE_BYTES must be 0 (no limit) or a number of bytes.");
        }
        Objects.requireNonNull(dropPurge, "dropPurge");
        Objects.requireNonNull(shutdown, "shutdown");
        Objects.requireNonNull(requestWorkTime, "requestWorkTime");
        if (requestWorkTime.isNegative()) {
            throw new IllegalArgumentException(
                    "COLVENNA_REQUEST_WORK_TIME must be PT0S (no limit) or a positive"
                            + " duration.");
        }
        if (lambdaRuntime) {
            timeouts.requireUnder(GATEWAY_TIMEOUT);
            if (dropPurge == TablePurges.Mode.INLINE) {
                throw new IllegalArgumentException(
                        "In Lambda COLVENNA_DROP_PURGE must be deferred: deleting a large table's"
                                + " files in the request outlasts API Gateway's "
                                + GATEWAY_TIMEOUT
                                + " integration timeout.");
            }
            if (requestWorkTime.isZero()
                    || Db.CONNECTION_TIMEOUT.plus(requestWorkTime).compareTo(GATEWAY_TIMEOUT)
                            >= 0) {
                throw new IllegalArgumentException(
                        "In Lambda COLVENNA_REQUEST_WORK_TIME must be positive, and with the"
                                + " connection wait ("
                                + Db.CONNECTION_TIMEOUT
                                + ") shorter than API Gateway's "
                                + GATEWAY_TIMEOUT
                                + " integration timeout, not "
                                + requestWorkTime
                                + ".");
            }
        }
        // The one place that keeps loopback-only plain HTTP from being combined with a listener
        // that would serve beyond loopback; whether this is Lambda, serve checks.
        if (lambdaLoopbackPlaintext && (tls != null || plaintextAllowed)) {
            throw new IllegalArgumentException(
                    "COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT excludes COLVENNA_TLS_KEYSTORE and"
                            + " COLVENNA_ALLOW_PLAINTEXT, which would serve beyond loopback.");
        }
        Objects.requireNonNull(jdbcUrl, "COLVENNA_JDBC_URL");
        Objects.requireNonNull(jdbcAuth, "jdbcAuth");
        if (jdbcAuth.method() == JdbcAuth.Method.IAM) {
            if (jdbcPassword != null) {
                throw new IllegalArgumentException(
                        "COLVENNA_JDBC_PASSWORD must not be set with COLVENNA_JDBC_AUTH=iam: the"
                                + " server signs in with IAM tokens, and a password would only sit"
                                + " in its environment.");
            }
            Objects.requireNonNull(jdbcUser, "COLVENNA_JDBC_USER");
            RdsIamCredentials.check(jdbcUrl);
        }
        Objects.requireNonNull(warehouseName, "COLVENNA_WAREHOUSE_NAME");
        Objects.requireNonNull(warehouseRoot, "COLVENNA_WAREHOUSE_ROOT");
        externalLocationRoots = List.copyOf(externalLocationRoots);
        fileIoOptions = Map.copyOf(fileIoOptions);
        clientOverrides = Map.copyOf(clientOverrides);
        if (warehouseRoot.endsWith("/")) {
            warehouseRoot = warehouseRoot.substring(0, warehouseRoot.length() - 1);
        }
    }

    public static ServerConfig fromEnvironment(Map<String, String> env) {
        // Lambda sets this in every execution environment; it is how the server knows its form.
        boolean lambda = !DataTokens.isBlank(env.get("AWS_LAMBDA_RUNTIME_API"));
        Timeouts timeoutDefaults = lambda ? Timeouts.LAMBDA_DEFAULTS : Timeouts.DEFAULTS;
        Map<String, String> fileIo = new HashMap<>();
        env.forEach(
                (key, value) -> {
                    if (key.startsWith("COLVENNA_FILEIO_")
                            && !key.equals("COLVENNA_FILEIO_OPTIONS")) {
                        String option =
                                key.substring("COLVENNA_FILEIO_".length())
                                        .toLowerCase(Locale.ROOT)
                                        .replace('_', '.');
                        fileIo.put(option, value);
                    }
                });
        fileIo.putAll(parsePairs(env.get("COLVENNA_FILEIO_OPTIONS")));
        Tls tls = null;
        String keystore = env.get("COLVENNA_TLS_KEYSTORE");
        if (!DataTokens.isBlank(keystore)) {
            tls =
                    new Tls(
                            keystore,
                            Objects.requireNonNull(
                                    env.get("COLVENNA_TLS_KEYSTORE_PASSWORD"),
                                    "COLVENNA_TLS_KEYSTORE_PASSWORD"));
        }
        return new ServerConfig(
                Integer.parseInt(env.getOrDefault("COLVENNA_PORT", "8181")),
                Integer.parseInt(env.getOrDefault("COLVENNA_HEALTH_PORT", "8182")),
                Integer.parseInt(env.getOrDefault("COLVENNA_METRICS_PORT", "-1")),
                tls,
                env.get("COLVENNA_JDBC_URL"),
                env.get("COLVENNA_JDBC_USER"),
                env.get("COLVENNA_JDBC_PASSWORD"),
                Integer.parseInt(env.getOrDefault("COLVENNA_JDBC_POOL_SIZE", "16")),
                Integer.parseInt(env.getOrDefault("COLVENNA_JDBC_AUX_POOL_SIZE", "4")),
                env.get("COLVENNA_WAREHOUSE_NAME"),
                env.get("COLVENNA_WAREHOUSE_ROOT"),
                List.copyOf(parseList(env.get("COLVENNA_EXTERNAL_LOCATION_ROOTS"))),
                fileIo,
                parsePairs(env.get("COLVENNA_CLIENT_OVERRIDES")),
                Integer.parseInt(env.getOrDefault("COLVENNA_DEFAULT_PAGE_SIZE", "100")),
                Integer.parseInt(env.getOrDefault("COLVENNA_MAX_PAGE_SIZE", "1000")),
                Integer.parseInt(env.getOrDefault("COLVENNA_MAX_IN_FLIGHT_REQUESTS", "512")),
                duration(env, "COLVENNA_AUTH_CACHE_TTL", Duration.ofSeconds(2)),
                flag(env, "COLVENNA_ALLOW_PLAINTEXT", false),
                new DataTokens(
                        DataTokens.Mode.parse(
                                env.getOrDefault("COLVENNA_DATA_TOKEN_MODE", "disabled")),
                        env.get("COLVENNA_DATA_TOKEN_ROLE_ARN"),
                        env.getOrDefault("COLVENNA_DATA_TOKEN_REGION", env.get("AWS_REGION")),
                        Duration.parse(env.getOrDefault("COLVENNA_DATA_TOKEN_DURATION", "PT3H")),
                        DataTokens.WebIdentity.parse(
                                env.getOrDefault("COLVENNA_DATA_TOKEN_WEB_IDENTITY", "token-file")),
                        env.get("COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID"),
                        env.get("COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER")),
                // Only grants made for this environment count here.
                env.getOrDefault("COLVENNA_ENVIRONMENT", "dev"),
                // In Lambda the installer runs migrate: an init must not wait on the database.
                flag(env, "COLVENNA_MIGRATE_ON_START", !lambda),
                lambda,
                new Timeouts(
                        duration(env, "COLVENNA_JDBC_LOCK_TIMEOUT", timeoutDefaults.lockTimeout()),
                        duration(env, "COLVENNA_S3_CALL_TIMEOUT", timeoutDefaults.s3CallTimeout()),
                        duration(
                                env,
                                "COLVENNA_S3_CALL_ATTEMPT_TIMEOUT",
                                timeoutDefaults.s3CallAttemptTimeout())),
                // 0: no limit. Lambda carries at most 6 MB of response; see ResponseSize.
                Integer.parseInt(env.getOrDefault("COLVENNA_MAX_RESPONSE_BYTES", "0")),
                flag(env, "COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT", false),
                jdbcAuth(env),
                // In Lambda a drop touches no file: the purge function deletes them (#69).
                TablePurges.Mode.parse(
                        env.getOrDefault("COLVENNA_DROP_PURGE", lambda ? "deferred" : "inline")),
                // PT0S: no limit, one answer whatever the table (the EKS form's).
                duration(
                        env,
                        "COLVENNA_REQUEST_WORK_TIME",
                        lambda ? LAMBDA_REQUEST_WORK_TIME : Duration.ZERO),
                shutdown(env, lambda ? Shutdown.LAMBDA_DEFAULTS : Shutdown.DEFAULTS));
    }

    private static Shutdown shutdown(Map<String, String> env, Shutdown defaults) {
        return new Shutdown(
                duration(env, "COLVENNA_SHUTDOWN_DRAIN", defaults.drain()),
                duration(env, "COLVENNA_SHUTDOWN_TIMEOUT", defaults.timeout()));
    }

    private static JdbcAuth jdbcAuth(Map<String, String> env) {
        String method = env.get("COLVENNA_JDBC_AUTH");
        if (DataTokens.isBlank(method)
                || JdbcAuth.Method.parse(method) == JdbcAuth.Method.PASSWORD) {
            return JdbcAuth.PASSWORD;
        }
        String region = env.get("COLVENNA_JDBC_IAM_REGION");
        return new JdbcAuth(
                JdbcAuth.Method.IAM, DataTokens.isBlank(region) ? env.get("AWS_REGION") : region);
    }

    /** How the server's pools sign in: {@link #jdbcAuth}'s way. */
    public Db.Credentials dbCredentials() {
        if (jdbcAuth.method() == JdbcAuth.Method.IAM) {
            return RdsIamCredentials.create(
                    jdbcUrl, jdbcUser, Region.of(Objects.requireNonNull(jdbcAuth.region())));
        }
        return Db.Credentials.fixed(jdbcUser, jdbcPassword);
    }

    /** An ISO-8601 duration setting such as {@code PT10S}. */
    static Duration duration(Map<String, String> env, String name, Duration defaultValue) {
        String value = env.get(name);
        if (DataTokens.isBlank(value)) {
            return defaultValue;
        }
        try {
            return Duration.parse(value.trim());
        } catch (java.time.format.DateTimeParseException e) {
            throw new IllegalArgumentException(
                    name + " must be an ISO-8601 duration such as PT10S, not " + value, e);
        }
    }

    /**
     * A {@code true} / {@code false} setting; anything else is refused rather than read as false.
     */
    static boolean flag(Map<String, String> env, String name, boolean defaultValue) {
        String value = env.get(name);
        if (DataTokens.isBlank(value)) {
            return defaultValue;
        }
        if (value.trim().equalsIgnoreCase("true")) {
            return true;
        }
        if (value.trim().equalsIgnoreCase("false")) {
            return false;
        }
        throw new IllegalArgumentException(name + " must be true or false, not " + value);
    }

    /**
     * What {@code serve} checks before it starts; the maintenance commands ({@code migrate}, {@code
     * bootstrap-admin}) open no port and skip it.
     */
    public void validateForServing() {
        requireTlsForServing();
        requireS3aRegion();
    }

    /**
     * Refuses to serve without TLS unless plain HTTP was explicitly allowed: on every interface
     * with {@code COLVENNA_ALLOW_PLAINTEXT} (local development and tests), or on loopback only with
     * {@code COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT} in a Lambda execution environment, where the
     * Lambda Web Adapter forwards requests over loopback and TLS ends at API Gateway.
     */
    public void requireTlsForServing() {
        if (lambdaLoopbackPlaintext) {
            if (!lambdaRuntime) {
                throw new IllegalArgumentException(
                        "COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT is for the Lambda form only, and"
                                + " AWS_LAMBDA_RUNTIME_API is not set: this is not a Lambda"
                                + " execution environment.");
            }
            return;
        }
        if (tls == null && !plaintextAllowed) {
            throw new IllegalArgumentException(
                    "COLVENNA_TLS_KEYSTORE is required; bearer tokens must not travel in plain HTTP."
                            + " Set COLVENNA_ALLOW_PLAINTEXT=true only for local development.");
        }
    }

    /**
     * An S3 warehouse needs the S3A region or endpoint, in either form (#9): Paimon's S3 FileIO
     * passes {@code s3.region} to S3A as {@code fs.s3a.region}, which S3A does not read, and S3A
     * 3.4 without a region or endpoint sends to us-east-2, which hangs in a VPC without internet
     * egress (the LLD's "S3A 区域"). The chart and the serverless stack set it to the install's
     * Region.
     */
    void requireS3aRegion() {
        String root = warehouseRoot.toLowerCase(Locale.ROOT);
        if (!(root.startsWith("s3://") || root.startsWith("s3a://"))) {
            return;
        }
        for (String prefix : List.of("fs.s3a.", "s3.", "s3a.")) {
            for (String key : List.of("endpoint.region", "endpoint")) {
                String value = fileIoOptions.get(prefix + key);
                if (value != null && !value.isBlank()) {
                    return;
                }
            }
        }
        throw new IllegalArgumentException(
                "An S3 warehouse needs COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION, the bucket's"
                        + " Region: COLVENNA_FILEIO_S3_REGION does not reach S3A, which then sends"
                        + " to us-east-2.");
    }

    static java.util.ArrayList<String> parseList(@Nullable String value) {
        java.util.ArrayList<String> items = new java.util.ArrayList<>();
        if (value != null) {
            for (String item : value.split(",", -1)) {
                if (!item.isBlank()) {
                    items.add(item.trim());
                }
            }
        }
        return items;
    }

    static Map<String, String> parsePairs(@Nullable String value) {
        Map<String, String> pairs = new HashMap<>();
        if (value == null || value.isBlank()) {
            return pairs;
        }
        for (String pair : value.split(",", -1)) {
            int eq = pair.indexOf('=');
            if (eq <= 0) {
                throw new IllegalArgumentException("Expected k=v in '" + pair + "'");
            }
            pairs.put(pair.substring(0, eq).trim(), pair.substring(eq + 1).trim());
        }
        return pairs;
    }
}
