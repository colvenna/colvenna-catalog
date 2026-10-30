package com.colvenna.catalog.http;

import com.colvenna.catalog.ServerConfig;
import com.colvenna.catalog.audit.AuditLog;
import com.colvenna.catalog.audit.AuditLog.Outcome;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.auth.Principal;
import com.colvenna.catalog.auth.TokenService;
import com.colvenna.catalog.catalog.Names;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.metrics.CatalogMetrics;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsParameters;
import com.sun.net.httpserver.HttpsServer;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.function.BooleanSupplier;
import javax.annotation.Nullable;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.RESTUtil;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * HTTP front end: the Paimon REST API under {@code /v1} and the admin API under {@code
 * /colvenna/admin/v1} on one port, liveness and readiness on a separate health port.
 *
 * <p>The API port serves HTTPS whenever a key store is configured. Every API request must carry a
 * valid bearer token, which is checked before the request body is read or the route is revealed;
 * the health port answers only {@code /healthz} and {@code /readyz} with no catalog data.
 */
public final class RestServer implements AutoCloseable {

    private static final Logger LOG = LoggerFactory.getLogger(RestServer.class);
    private static final Logger ACCESS = LoggerFactory.getLogger("colvenna.access");

    static final int MAX_BODY_BYTES = 64 << 20;

    /** 127.0.0.1, where the Lambda Web Adapter forwards requests. */
    private static final InetAddress LOOPBACK;

    static {
        try {
            LOOPBACK = InetAddress.getByAddress(new byte[] {127, 0, 0, 1});
        } catch (java.net.UnknownHostException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    /** PostgreSQL's SQLSTATE when {@code lock_timeout} cancels a statement waiting for a lock. */
    static final String LOCK_NOT_AVAILABLE = "55P03";

    static final String REQUEST_ID_HEADER = "X-Request-Id";

    /** Unauthenticated denials written to the audit table per second; the rest are only logged. */
    static final int ANONYMOUS_AUDIT_PER_SECOND = 20;

    /** What the API port speaks: TLS 1.3, and 1.2 for clients without it; never older. */
    private static final String[] TLS_PROTOCOLS = {"TLSv1.3", "TLSv1.2"};

    /**
     * What every API request passes before anything else runs, authentication included; it fails
     * with an {@link ApiException} to refuse the request.
     */
    @FunctionalInterface
    public interface Admission {
        Admission OPEN = () -> {};

        void check() throws Exception;
    }

    private final CatalogRoutes catalog;
    private final AdminRoutes admin;
    private final Authorizer authz;
    private final TokenService tokens;
    private final AuditLog audit;
    private final BooleanSupplier ready;
    private final Admission admission;
    private final int maxResponseBytes;
    private final Semaphore inFlight;
    private final RateLimiter anonymousAudit = new RateLimiter(ANONYMOUS_AUDIT_PER_SECOND);
    private final HttpServer api;
    private final HttpServer health;
    private final HttpServer metricsServer;
    private final CatalogMetrics metrics;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final ServerConfig.Shutdown shutdown;

    /** Set once {@link #close} begins: readiness fails, and every answer closes its connection. */
    private volatile boolean draining;

    public RestServer(
            CatalogRoutes catalog,
            AdminRoutes admin,
            TokenService tokens,
            Authorizer authz,
            AuditLog audit,
            BooleanSupplier ready,
            Admission admission,
            ServerConfig config,
            CatalogMetrics metrics)
            throws Exception {
        this.catalog = catalog;
        this.metrics = metrics;
        this.admin = admin;
        this.tokens = tokens;
        this.authz = authz;
        this.audit = audit;
        this.ready = ready;
        this.admission = admission;
        this.maxResponseBytes = config.maxResponseBytes();
        this.inFlight = new Semaphore(config.maxInFlightRequests());
        this.shutdown = config.shutdown();
        // In the Lambda form's plain-HTTP mode only the adapter in the same environment may
        // connect, so the port is bound to 127.0.0.1 alone; nothing configures it otherwise.
        InetSocketAddress apiAddress =
                config.lambdaLoopbackPlaintext()
                        ? new InetSocketAddress(LOOPBACK, config.port())
                        : new InetSocketAddress(config.port());
        if (config.tls() != null) {
            HttpsServer https = HttpsServer.create(apiAddress, 256);
            https.setHttpsConfigurator(
                    new HttpsConfigurator(sslContext(config.tls())) {
                        @Override
                        public void configure(HttpsParameters params) {
                            SSLParameters ssl = getSSLContext().getDefaultSSLParameters();
                            ssl.setProtocols(TLS_PROTOCOLS);
                            params.setSSLParameters(ssl);
                        }
                    });
            this.api = https;
        } else {
            this.api = HttpServer.create(apiAddress, 256);
        }
        this.api.createContext("/", this::handle);
        this.api.setExecutor(executor);
        // Loopback only: the status endpoints are unauthenticated, so nothing outside the pod may
        // reach them; Kubernetes probes run curl inside the container.
        this.health =
                HttpServer.create(
                        new InetSocketAddress(
                                InetAddress.getLoopbackAddress(), config.healthPort()),
                        16);
        this.health.createContext("/", this::handleHealth);
        this.health.setExecutor(executor);
        // On the pod's address, for in-cluster scraping: the chart's NetworkPolicy admits only
        // pods of monitoring namespaces to this port. Off (-1) unless configured.
        if (config.metricsPort() >= 0) {
            this.metricsServer = HttpServer.create(new InetSocketAddress(config.metricsPort()), 16);
            this.metricsServer.createContext("/metrics", this::handleMetrics);
            this.metricsServer.setExecutor(executor);
        } else {
            this.metricsServer = null;
        }
    }

    private void handleMetrics(HttpExchange exchange) {
        try (exchange) {
            byte[] body = metrics.render().getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "text/plain; version=0.0.4");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
        } catch (IOException e) {
            LOG.debug("Writing metrics failed", e);
        }
    }

    /** What the server measured, for tests. */
    public CatalogMetrics metrics() {
        return metrics;
    }

    private static SSLContext sslContext(ServerConfig.Tls tls) throws Exception {
        char[] password = tls.keystorePassword().toCharArray();
        KeyStore store = KeyStore.getInstance("PKCS12");
        try (InputStream in = Files.newInputStream(Path.of(tls.keystorePath()))) {
            store.load(in, password);
        }
        KeyManagerFactory keys =
                KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        keys.init(store, password);
        SSLContext context = SSLContext.getInstance("TLSv1.3");
        context.init(keys.getKeyManagers(), null, null);
        return context;
    }

    public void start() {
        InetAddress bound = api.getAddress().getAddress();
        String mode;
        if (api instanceof HttpsServer) {
            mode = "https";
        } else if (bound.isAnyLocalAddress()) {
            mode = "plain http";
        } else {
            mode = "plain http on " + bound.getHostAddress();
        }
        api.start();
        health.start();
        if (metricsServer != null) {
            metricsServer.start();
        }
        LOG.info(
                "colvenna-catalog listening on {} ({}), health on {}",
                api.getAddress().getPort(),
                mode,
                health.getAddress().getPort());
    }

    public int port() {
        return api.getAddress().getPort();
    }

    /** The address the API port is bound to. */
    public InetSocketAddress address() {
        return api.getAddress();
    }

    public int healthPort() {
        return health.getAddress().getPort();
    }

    private void handle(HttpExchange exchange) {
        long start = System.nanoTime();
        String requestId = UUID.randomUUID().toString();
        String method = exchange.getRequestMethod();
        String rawPath = exchange.getRequestURI().getRawPath();
        Principal principal = null;
        Route route = null;
        int status;
        boolean admitted = inFlight.tryAcquire();
        try {
            if (!admitted) {
                // Nothing has run yet, so the client may safely retry.
                throw ApiException.unavailable("colvenna-catalog is at its request limit.", null);
            }
            admission.check();
            Request request = parse(exchange, requestId);
            try {
                principal =
                        tokens.authenticate(exchange.getRequestHeaders().getFirst("Authorization"));
            } catch (ApiException e) {
                Route attempted = resolve(rawPath, request);
                if (attempted != null && anonymousAudit.tryAcquire()) {
                    recordDenied(attempted, requestId, null, e);
                }
                throw e;
            }
            route = resolve(rawPath, request);
            if (route == null) {
                throw ApiException.notFound(
                        null, null, "No such endpoint: " + method + " " + rawPath);
            }
            if (rawPath.startsWith("/colvenna/admin/") && !principal.admin()) {
                ApiException denied =
                        ApiException.forbidden(
                                Resources.CATALOG,
                                null,
                                "The admin API requires an admin principal.");
                recordDenied(route, requestId, principal, denied);
                throw denied;
            }
            Authorizer.Decision decision = null;
            if (rawPath.startsWith("/v1/")) {
                List<String> segments = request.segments();
                try {
                    decision =
                            authz.check(
                                    principal,
                                    route.action(),
                                    segments.subList(1, segments.size()));
                } catch (ApiException denied) {
                    recordDenied(route, requestId, principal, denied);
                    throw denied;
                }
            }
            Request authenticated = request.authenticated(principal, readBody(exchange));
            Response response;
            byte[] body;
            try {
                response = route.handler().handle(authenticated);
                body = serialize(response.body());
            } catch (Exception | Error e) {
                ApiException mapped = map(e, replaySafe(method, route));
                record(
                        route,
                        requestId,
                        principal,
                        new Response(
                                        mapped.status(),
                                        null,
                                        mapped.denial() == null ? null : Outcome.DENIED,
                                        failureDetail(mapped))
                                .withAuditResource(
                                        mapped.auditResource() != null
                                                ? mapped.auditResource()
                                                : failedResource(route, mapped.resourceName())),
                        decision);
                if (route.action().equals("COMMIT")) {
                    metrics.commit(
                            String.valueOf(route.resource()), mapped.fenced() ? "fenced" : "error");
                }
                throw mapped;
            }
            record(route, requestId, principal, response, decision);
            if (route.action().equals("COMMIT")) {
                metrics.commit(
                        String.valueOf(route.resource()),
                        response.auditOutcome() == AuditLog.Outcome.FAILED
                                ? "conflict"
                                : "committed");
            }
            status = response.status();
            closeIfDraining(exchange);
            writeJson(exchange, requestId, status, body);
        } catch (Exception | Error e) {
            ApiException mapped = map(e, replaySafe(method, route));
            status = mapped.status();
            // Endpoints a client called that the server lacks, or declines by design (501, as for
            // partition listing of tables whose partitions live in files), are logged for review.
            if (status == 501) {
                LOG.warn(
                        "Request {} {} {} not implemented ({}): {}",
                        requestId,
                        method,
                        rawPath,
                        route == null ? "no route" : route.action(),
                        mapped.getMessage());
            } else if (status >= 500) {
                LOG.error("Request {} {} {} failed", requestId, method, rawPath, e);
            } else if (status == 404 && route == null && principal != null) {
                LOG.warn("Request {} {} {}: no such endpoint", requestId, method, rawPath);
            }
            try {
                closeIfDraining(exchange);
                write(exchange, requestId, status, mapped.toResponse());
            } catch (IOException io) {
                LOG.warn("Could not write error response of {}", requestId, io);
            }
        } finally {
            if (admitted) {
                inFlight.release();
            }
            exchange.close();
        }
        metrics.request(route == null ? "-" : route.action(), status, System.nanoTime() - start);
        ACCESS.info(
                "{} {} {} {} {}ms principal={} action={}",
                requestId,
                method,
                rawPath,
                status,
                (System.nanoTime() - start) / 1_000_000,
                principal == null ? "-" : principal.name(),
                route == null ? "-" : route.action());
    }

    @Nullable
    private Route resolve(String rawPath, Request request) {
        List<String> segments = request.segments();
        if (rawPath.startsWith("/v1/")) {
            return catalog.resolve(request.withSegments(segments.subList(1, segments.size())));
        }
        if (rawPath.startsWith("/colvenna/admin/v1/") && segments.size() > 3) {
            return admin.resolve(request.withSegments(segments.subList(3, segments.size())));
        }
        return null;
    }

    /** Parses the path and query; the body is read only after authentication. */
    private static Request parse(HttpExchange exchange, String requestId) {
        String rawPath = exchange.getRequestURI().getRawPath();
        List<String> segments = new ArrayList<>();
        String[] parts = rawPath.split("/", -1);
        for (int i = 1; i < parts.length; i++) {
            if (parts[i].isEmpty()) {
                if (i == parts.length - 1) {
                    continue;
                }
                throw ApiException.badRequest("Empty path segment in " + rawPath);
            }
            String segment = RESTUtil.decodeString(parts[i]);
            // A decoded %2F or .. would let a name navigate the file system.
            Names.validate("path segment", segment);
            segments.add(segment);
        }
        Map<String, String> query = new HashMap<>();
        String rawQuery = exchange.getRequestURI().getRawQuery();
        if (rawQuery != null && !rawQuery.isEmpty()) {
            for (String pair : rawQuery.split("&", -1)) {
                int eq = pair.indexOf('=');
                String key = eq < 0 ? pair : pair.substring(0, eq);
                String value = eq < 0 ? "" : pair.substring(eq + 1);
                query.putIfAbsent(RESTUtil.decodeString(key), RESTUtil.decodeString(value));
            }
        }
        return new Request(exchange.getRequestMethod(), segments, query, "", null, requestId);
    }

    private static String readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readNBytes(MAX_BODY_BYTES + 1);
            if (bytes.length > MAX_BODY_BYTES) {
                throw ApiException.badRequest("Request body exceeds " + MAX_BODY_BYTES + " bytes.");
            }
            return new String(bytes, StandardCharsets.UTF_8);
        }
    }

    /**
     * The body as JSON, or {@code null} for none. Over the response limit it is an error instead,
     * as clear as the client can get: an entry that cannot carry the body would answer with a bare
     * 502 instead. Only reads return bodies anywhere near the limit.
     */
    @Nullable
    private byte[] serialize(@Nullable Object body) throws IOException {
        if (body == null) {
            return null;
        }
        byte[] bytes = RESTApi.toJson(body).getBytes(StandardCharsets.UTF_8);
        if (maxResponseBytes > 0) {
            long size = ResponseSize.escaped(bytes);
            if (size > maxResponseBytes) {
                throw new ApiException(
                        500,
                        null,
                        null,
                        String.format(
                                "The response would take %d bytes, more than this server's"
                                        + " response limit of %d bytes"
                                        + " (COLVENNA_MAX_RESPONSE_BYTES).",
                                size, maxResponseBytes));
            }
        }
        return bytes;
    }

    private static void write(
            HttpExchange exchange, String requestId, int status, @Nullable Object body)
            throws IOException {
        writeJson(
                exchange,
                requestId,
                status,
                body == null ? null : RESTApi.toJson(body).getBytes(StandardCharsets.UTF_8));
    }

    private static void writeJson(
            HttpExchange exchange, String requestId, int status, @Nullable byte[] bytes)
            throws IOException {
        exchange.getResponseHeaders().add(REQUEST_ID_HEADER, requestId);
        if (bytes == null) {
            exchange.sendResponseHeaders(status, -1);
            return;
        }
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }

    private void handleHealth(HttpExchange exchange) {
        try {
            String path = exchange.getRequestURI().getPath();
            int status;
            if (path.equals("/healthz")) {
                status = 200;
            } else if (path.equals("/readyz")) {
                status = !draining && ready.getAsBoolean() ? 200 : 503;
            } else {
                status = 404;
            }
            exchange.sendResponseHeaders(status, -1);
        } catch (IOException e) {
            LOG.debug("Health probe write failed", e);
        } finally {
            exchange.close();
        }
    }

    /**
     * Records a request: every write, every decision on a catalog object (reads and listings
     * included, as the acceptance contract asks for allowed and denied catalog access), and every
     * denial. {@code decision} is the route's; a decision the handler made itself, recorded in the
     * response's detail, takes its place.
     */
    private void record(
            Route route,
            String requestId,
            Principal principal,
            Response response,
            @Nullable Authorizer.Decision decision) {
        int status = response.status();
        if (!route.audited() && decision == null && status != 403) {
            return;
        }
        Map<String, Object> detail = new LinkedHashMap<>();
        if (decision != null) {
            detail.putAll(decision.auditDetail());
        }
        detail.putAll(response.auditDetail());
        Outcome outcome = response.auditOutcome();
        if (outcome == null) {
            outcome =
                    status < 400
                            ? Outcome.SUCCEEDED
                            : status == 403 ? Outcome.DENIED : Outcome.FAILED;
        }
        audit.record(
                new AuditLog.Event(
                        requestId,
                        principal.name(),
                        route.action(),
                        route.resourceType(),
                        response.auditResource() != null
                                ? response.auditResource()
                                : route.resource() != null
                                        ? route.resource()
                                        : decision != null && decision.target() != null
                                                ? decision.target().name()
                                                : null,
                        outcome,
                        status,
                        detail));
    }

    /** Resource types of the routes that name one catalog object, {@code <database>.<name>}. */
    private static final Set<String> OBJECT_RESOURCE_TYPES =
            Set.of(Resources.TABLE, Resources.VIEW, Resources.FUNCTION);

    /**
     * What a failed or refused request's audit event names. A route that names one table, view or
     * function names it {@code <database>.<name>}, as its successes do, whatever the error names (a
     * fenced commit's names the bare table, a missing snapshot's another resource). On a route over
     * a database's collection (creating a table, a view or a function in it), the error names the
     * bare object, recorded with its database. Elsewhere, what the error names.
     */
    @Nullable
    private static String failedResource(Route route, @Nullable String name) {
        if (route.resource() != null && OBJECT_RESOURCE_TYPES.contains(route.resourceType())) {
            return route.resource();
        }
        if (name != null
                && Resources.DATABASE.equals(route.resourceType())
                && route.resource() != null
                && !name.equals(route.resource())
                && !name.contains(".")) {
            return route.resource() + "." + name;
        }
        return name;
    }

    /** A failure's audit detail: the decision that refused it, if one did, and the error. */
    private static Map<String, Object> failureDetail(ApiException e) {
        Map<String, Object> detail = new LinkedHashMap<>();
        if (e.denial() != null) {
            detail.putAll(e.denial().auditDetail());
        }
        detail.put("error", String.valueOf(e.getMessage()));
        return detail;
    }

    private void recordDenied(
            Route route, String requestId, @Nullable Principal principal, ApiException e) {
        audit.record(
                new AuditLog.Event(
                        requestId,
                        principal == null ? null : principal.name(),
                        route.action(),
                        route.resourceType(),
                        route.resource() != null ? route.resource() : e.resourceName(),
                        Outcome.DENIED,
                        e.status(),
                        failureDetail(e)));
    }

    /**
     * Whether repeating the request is safe even when its transaction's COMMIT may have taken
     * effect: a read (GET), whose writes only resolve commit intents, which a second time changes
     * nothing more; and a commit, which a retry finds already the head ({@code
     * CommitService.commit} answers a request whose snapshot is the pointer as a replay) or rolls
     * forward first.
     */
    static boolean replaySafe(String method, @Nullable Route route) {
        return method.equals("GET") || (route != null && route.action().equals("COMMIT"));
    }

    /** Maps a failure to the Paimon error response clients expect. */
    static ApiException map(Throwable error) {
        return map(error, false);
    }

    /**
     * As {@link #map(Throwable)}; {@code replaySafe} says whether the request may be repeated when
     * its COMMIT is in doubt ({@link #replaySafe}).
     */
    static ApiException map(Throwable error, boolean replaySafe) {
        for (Throwable t = error; t != null; t = t.getCause()) {
            if (t instanceof ApiException api) {
                return api;
            }
            if (t instanceof Db.CommitInDoubtException) {
                return replaySafe
                        ? ApiException.unavailable(
                                "The connection to the catalog metadata backend was lost while"
                                        + " this request committed. Retry the request: a commit"
                                        + " that took effect is answered as a replay, and a read"
                                        + " changes nothing more.",
                                t)
                        : new ApiException(
                                500,
                                "The connection to the catalog metadata backend was lost while"
                                        + " this request committed, so whether its changes took"
                                        + " effect is unknown. Check before repeating it.",
                                t);
            }
            if (t instanceof Catalog.TableNotExistException e) {
                return ApiException.notFound(
                        ErrorResponse.RESOURCE_TYPE_TABLE,
                        e.identifier().getTableName(),
                        e.getMessage());
            }
            if (t instanceof Catalog.DatabaseNotExistException e) {
                return ApiException.notFound(
                        ErrorResponse.RESOURCE_TYPE_DATABASE, e.database(), e.getMessage());
            }
            if (t instanceof Catalog.ColumnAlreadyExistException e) {
                return ApiException.conflict(
                        ErrorResponse.RESOURCE_TYPE_COLUMN, e.column(), e.getMessage());
            }
            if (t instanceof Catalog.ColumnNotExistException e) {
                return ApiException.notFound(
                        ErrorResponse.RESOURCE_TYPE_COLUMN, e.column(), e.getMessage());
            }
            if (t instanceof SQLException sql && LOCK_NOT_AVAILABLE.equals(sql.getSQLState())) {
                // lock_timeout: the transaction rolled back. Retrying is safe for every request: a
                // commit whose snapshot was already published is recognised as a replay.
                return ApiException.unavailable(
                        "Timed out waiting for a lock that another request holds; this request's"
                                + " catalog changes were rolled back. Retry the request.",
                        t);
            }
            if (t instanceof SQLTransientConnectionException) {
                // Nothing ran: the metadata backend could not hand out a connection.
                return ApiException.unavailable("Catalog metadata backend is unavailable.", t);
            }
            if (t instanceof SQLException sql && Db.sessionLost(sql.getSQLState())) {
                // Lost before its COMMIT (one lost in it is a CommitInDoubtException, above): the
                // transaction rolled back, as after a lock timeout. A commit whose snapshot was
                // already published is rolled forward by the retry and answered as a replay.
                return ApiException.unavailable(
                        "The connection to the catalog metadata backend was lost during this"
                                + " request; what it had not committed was rolled back. Retry the"
                                + " request.",
                        t);
            }
        }
        for (Throwable t = error; t != null; t = t.getCause()) {
            if (t instanceof IllegalArgumentException || t instanceof DateTimeParseException) {
                return ApiException.badRequest(String.valueOf(t.getMessage()));
            }
            if (t instanceof UnsupportedOperationException) {
                return ApiException.notImplemented(String.valueOf(t.getMessage()));
            }
            if (t instanceof IllegalStateException) {
                // Paimon reports rejected schema changes this way; clients show the message.
                return new ApiException(500, String.valueOf(t.getMessage()), t);
            }
        }
        // Internal details stay in the server log, keyed by the X-Request-Id response header.
        return new ApiException(500, "Internal server error.", error);
    }

    /**
     * While the server stops, asks the client to close the connection after this answer, so that
     * its next request opens a new connection, which goes to a replica that is not stopping.
     */
    private void closeIfDraining(HttpExchange exchange) {
        if (draining) {
            exchange.getResponseHeaders().set("Connection", "close");
        }
    }

    /**
     * Stops in the order {@link ServerConfig.Shutdown} gives (#64): readiness fails and answers
     * close their connections at once; the API port goes on taking connections for the drain; then
     * it stops taking them, and the requests in flight have up to the timeout to finish.
     */
    @Override
    public void close() {
        draining = true;
        boolean interrupted = false;
        if (!shutdown.drain().isZero()) {
            LOG.info(
                    "Stopping: not ready, closing each connection after its answer, and taking new"
                            + " ones for {} more",
                    shutdown.drain());
            try {
                Thread.sleep(shutdown.drain());
            } catch (InterruptedException e) {
                // Stop at once, and say so to the caller once the server has stopped.
                interrupted = true;
            }
        }
        LOG.info(
                "Stopping: no new connections; requests in flight have up to {} s to finish",
                shutdown.timeoutSeconds());
        // HttpServer.stop closes the listener at once, waits for the exchanges in progress (at
        // most the delay), then closes every connection.
        api.stop(shutdown.timeoutSeconds());
        health.stop(0);
        if (metricsServer != null) {
            metricsServer.stop(0);
        }
        executor.shutdownNow();
        if (interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    /** At most {@code perSecond} acquisitions per wall-clock second. */
    static final class RateLimiter {
        private final int perSecond;
        private long second;
        private int used;

        RateLimiter(int perSecond) {
            this.perSecond = perSecond;
        }

        synchronized boolean tryAcquire() {
            long now = System.currentTimeMillis() / 1000;
            if (now != second) {
                second = now;
                used = 0;
            }
            if (used >= perSecond) {
                return false;
            }
            used++;
            return true;
        }
    }
}
