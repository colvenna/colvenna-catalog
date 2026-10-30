package com.colvenna.catalog.http;

import com.colvenna.catalog.audit.AuditLog;
import com.colvenna.catalog.auth.Authorizer;
import com.colvenna.catalog.auth.Authorizer.Privilege;
import com.colvenna.catalog.auth.Authorizer.Scope;
import com.colvenna.catalog.auth.Authorizer.Target;
import com.colvenna.catalog.auth.Principal;
import com.colvenna.catalog.catalog.CatalogService;
import com.colvenna.catalog.catalog.CommitService;
import com.colvenna.catalog.catalog.Names;
import com.colvenna.catalog.catalog.Paging;
import com.colvenna.catalog.catalog.Paging.Page;
import com.colvenna.catalog.catalog.PartitionCatalog;
import com.colvenna.catalog.catalog.ViewFunctionCatalog;
import com.colvenna.catalog.token.DataTokenIssuer;
import com.colvenna.catalog.token.SessionPolicy;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import javax.annotation.Nullable;
import org.apache.paimon.PagedList;
import org.apache.paimon.Snapshot;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.requests.AlterDatabaseRequest;
import org.apache.paimon.rest.requests.AlterFunctionRequest;
import org.apache.paimon.rest.requests.AlterTableRequest;
import org.apache.paimon.rest.requests.AlterViewRequest;
import org.apache.paimon.rest.requests.AuthTableQueryRequest;
import org.apache.paimon.rest.requests.CommitTableRequest;
import org.apache.paimon.rest.requests.CreateBranchRequest;
import org.apache.paimon.rest.requests.CreateDatabaseRequest;
import org.apache.paimon.rest.requests.CreateFunctionRequest;
import org.apache.paimon.rest.requests.CreatePartitionsRequest;
import org.apache.paimon.rest.requests.CreateTableRequest;
import org.apache.paimon.rest.requests.CreateTagRequest;
import org.apache.paimon.rest.requests.CreateViewRequest;
import org.apache.paimon.rest.requests.DropPartitionsRequest;
import org.apache.paimon.rest.requests.ListPartitionsByFilterRequest;
import org.apache.paimon.rest.requests.ListPartitionsByNamesRequest;
import org.apache.paimon.rest.requests.MarkDonePartitionsRequest;
import org.apache.paimon.rest.requests.RegisterTableRequest;
import org.apache.paimon.rest.requests.RenameTableRequest;
import org.apache.paimon.rest.requests.ReplaceTableRequest;
import org.apache.paimon.rest.requests.ResetConsumerRequest;
import org.apache.paimon.rest.requests.RollbackSchemaRequest;
import org.apache.paimon.rest.requests.RollbackTableRequest;
import org.apache.paimon.rest.responses.CommitTableResponse;
import org.apache.paimon.rest.responses.GetTableResponse;
import org.apache.paimon.rest.responses.GetTableSnapshotResponse;
import org.apache.paimon.rest.responses.GetTableTokenResponse;
import org.apache.paimon.rest.responses.GetVersionSnapshotResponse;
import org.apache.paimon.rest.responses.ListBranchesResponse;
import org.apache.paimon.rest.responses.ListConsumersResponse;
import org.apache.paimon.rest.responses.ListDatabasesResponse;
import org.apache.paimon.rest.responses.ListFunctionDetailsResponse;
import org.apache.paimon.rest.responses.ListFunctionsGloballyResponse;
import org.apache.paimon.rest.responses.ListFunctionsResponse;
import org.apache.paimon.rest.responses.ListPartitionsResponse;
import org.apache.paimon.rest.responses.ListSnapshotsResponse;
import org.apache.paimon.rest.responses.ListTableDetailsResponse;
import org.apache.paimon.rest.responses.ListTablesGloballyResponse;
import org.apache.paimon.rest.responses.ListTablesResponse;
import org.apache.paimon.rest.responses.ListTagsResponse;
import org.apache.paimon.rest.responses.ListViewDetailsResponse;
import org.apache.paimon.rest.responses.ListViewsGloballyResponse;
import org.apache.paimon.rest.responses.ListViewsResponse;
import org.apache.paimon.schema.SchemaChange;

/**
 * Maps {@code /v1/{prefix}/...} paths of the Paimon 2.0.0 REST contract onto {@link
 * CatalogService}. Path segments arrive URL-decoded.
 */
public final class CatalogRoutes {

    private static final String MAX = RESTApi.MAX_RESULTS;
    private static final String PAGE_TOKEN = RESTApi.PAGE_TOKEN;

    private final CatalogService catalog;
    private final PartitionCatalog partitions;
    private final ViewFunctionCatalog objects;

    /**
     * What a page's body needs besides its elements: the field names, brackets and the next-page
     * token's quotes. The token itself is counted separately.
     */
    static final int PAGE_ENVELOPE_BYTES = 256;

    private final Authorizer authz;
    private final String prefix;
    private final int maxResponseBytes;

    /**
     * {@code maxResponseBytes} (0 for no limit) caps the escaped size of a listing's page; see
     * {@link #visiblePage}.
     */
    public CatalogRoutes(
            CatalogService catalog,
            PartitionCatalog partitions,
            ViewFunctionCatalog objects,
            Authorizer authz,
            String prefix,
            int maxResponseBytes) {
        this.catalog = catalog;
        this.partitions = partitions;
        this.objects = objects;
        this.authz = authz;
        this.prefix = prefix;
        this.maxResponseBytes = maxResponseBytes;
    }

    /** Whether the caller may see a listed element. */
    @FunctionalInterface
    private interface Visible<T> {
        boolean test(T element) throws Exception;
    }

    private boolean reads(Principal p, Scope scope, String database, String name) throws Exception {
        return authz.allows(p, Privilege.READ, scope, database, name);
    }

    /** Resolves a request whose segments start after {@code /v1}; {@code null} means 404. */
    @Nullable
    public Route resolve(Request r) {
        if (r.size() == 1 && r.segment(0).equals("config") && r.is("GET")) {
            return Route.read(
                    "GET_CONFIG",
                    Resources.CATALOG,
                    null,
                    q -> Response.ok(catalog.config(q.param("warehouse"))));
        }
        if (r.size() < 2 || !r.segment(0).equals(prefix)) {
            return null;
        }
        List<String> s = r.segments().subList(1, r.size());
        return switch (s.get(0)) {
            case "databases" -> databases(r, s);
            case "tables" -> globalTables(r, s);
            case "views" -> globalViews(r, s);
            case "functions" -> globalFunctions(r, s);
            default -> null;
        };
    }

    // ---------------------------------------------------------------- /databases

    @Nullable
    private Route databases(Request r, List<String> s) {
        if (s.size() == 1) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_DATABASES",
                        Resources.CATALOG,
                        null,
                        q -> {
                            Page<String> page =
                                    visiblePage(
                                            token ->
                                                    catalog.listDatabases(
                                                            q.param(MAX),
                                                            token,
                                                            q.param(RESTApi.DATABASE_NAME_PATTERN)),
                                            q.param(MAX),
                                            q.param(PAGE_TOKEN),
                                            name -> authz.visible(q.principal(), name),
                                            name -> name);
                            return Response.ok(
                                    new ListDatabasesResponse(
                                            page.elements(), page.nextPageToken()));
                        });
            }
            if (r.is("POST")) {
                return Route.write(
                        "CREATE_DATABASE",
                        Resources.DATABASE,
                        null,
                        q -> {
                            CreateDatabaseRequest request = q.body(CreateDatabaseRequest.class);
                            Names.validateDatabase(request.getName());
                            Authorizer.Decision decision =
                                    authz.require(
                                            q.principal(),
                                            Privilege.MANAGE,
                                            new Target(Scope.DATABASE, request.getName(), null));
                            catalog.createDatabase(q.context(), request);
                            return Response.empty()
                                    .audited(AuditLog.Outcome.SUCCEEDED, decision.auditDetail())
                                    .withAuditResource(request.getName());
                        });
            }
            return null;
        }
        String database = s.get(1);
        if (s.size() == 2) {
            return switch (r.method()) {
                case "GET" ->
                        Route.read(
                                "GET_DATABASE",
                                Resources.DATABASE,
                                database,
                                q -> Response.ok(catalog.getDatabase(database)));
                case "POST" ->
                        Route.write(
                                "ALTER_DATABASE",
                                Resources.DATABASE,
                                database,
                                q ->
                                        Response.ok(
                                                catalog.alterDatabase(
                                                        q.context(),
                                                        database,
                                                        q.body(AlterDatabaseRequest.class))));
                case "DELETE" ->
                        Route.write(
                                "DROP_DATABASE",
                                Resources.DATABASE,
                                database,
                                q -> {
                                    // Dropping drops what it holds: each object must be droppable.
                                    for (Target object : contents(database)) {
                                        authz.require(q.principal(), Privilege.MANAGE, object);
                                    }
                                    catalog.dropDatabase(q.context(), database);
                                    return Response.empty();
                                });
                default -> null;
            };
        }
        String collection = s.get(2);
        return switch (collection) {
            case "register" ->
                    r.is("POST") && s.size() == 3
                            ? Route.write(
                                    "REGISTER_TABLE",
                                    Resources.DATABASE,
                                    database,
                                    q -> {
                                        RegisterTableRequest request =
                                                q.body(RegisterTableRequest.class);
                                        return auditedUnder(
                                                fullName(request.getIdentifier()),
                                                () -> {
                                                    requireNew(
                                                            q.principal(),
                                                            Scope.TABLE,
                                                            request.getIdentifier());
                                                    catalog.registerTable(
                                                            q.context(), database, request);
                                                });
                                    })
                            : null;
            case "tables" -> tables(r, database, s.subList(3, s.size()));
            case "table-details" ->
                    r.is("GET") && s.size() == 3
                            ? Route.read(
                                    "LIST_TABLE_DETAILS",
                                    Resources.DATABASE,
                                    database,
                                    q -> {
                                        Page<org.apache.paimon.rest.responses.GetTableResponse>
                                                page =
                                                        visiblePage(
                                                                token ->
                                                                        catalog.listTableDetails(
                                                                                database,
                                                                                q.param(MAX),
                                                                                token,
                                                                                q.param(
                                                                                        RESTApi
                                                                                                .TABLE_NAME_PATTERN),
                                                                                q.param(
                                                                                        RESTApi
                                                                                                .TABLE_TYPE)),
                                                                q.param(MAX),
                                                                q.param(PAGE_TOKEN),
                                                                t ->
                                                                        reads(
                                                                                q.principal(),
                                                                                Scope.TABLE,
                                                                                database,
                                                                                t.getName()),
                                                                t -> t.getName());
                                        return Response.ok(
                                                new ListTableDetailsResponse(
                                                        page.elements(), page.nextPageToken()));
                                    })
                            : null;
            case "views" -> views(r, database, s.subList(3, s.size()));
            case "view-details" ->
                    r.is("GET") && s.size() == 3
                            ? Route.read(
                                    "LIST_VIEW_DETAILS",
                                    Resources.DATABASE,
                                    database,
                                    q -> {
                                        var page =
                                                visiblePage(
                                                        token ->
                                                                objects.listViewDetails(
                                                                        database,
                                                                        q.param(MAX),
                                                                        token,
                                                                        q.param(
                                                                                RESTApi
                                                                                        .VIEW_NAME_PATTERN)),
                                                        q.param(MAX),
                                                        q.param(PAGE_TOKEN),
                                                        v ->
                                                                reads(
                                                                        q.principal(),
                                                                        Scope.VIEW,
                                                                        database,
                                                                        v.getName()),
                                                        v -> v.getName());
                                        return Response.ok(
                                                new ListViewDetailsResponse(
                                                        page.elements(), page.nextPageToken()));
                                    })
                            : null;
            case "functions" -> functions(r, database, s.subList(3, s.size()));
            case "function-details" ->
                    r.is("GET") && s.size() == 3
                            ? Route.read(
                                    "LIST_FUNCTION_DETAILS",
                                    Resources.DATABASE,
                                    database,
                                    q -> {
                                        var page =
                                                visiblePage(
                                                        token ->
                                                                objects.listFunctionDetails(
                                                                        database,
                                                                        q.param(MAX),
                                                                        token,
                                                                        q.param(
                                                                                RESTApi
                                                                                        .FUNCTION_NAME_PATTERN)),
                                                        q.param(MAX),
                                                        q.param(PAGE_TOKEN),
                                                        f ->
                                                                reads(
                                                                        q.principal(),
                                                                        Scope.FUNCTION,
                                                                        database,
                                                                        f.name()),
                                                        f -> f.name());
                                        return Response.ok(
                                                new ListFunctionDetailsResponse(
                                                        page.elements(), page.nextPageToken()));
                                    })
                            : null;
            default -> null;
        };
    }

    // ---------------------------------------------------------------- /databases/{db}/tables

    @Nullable
    private Route tables(Request r, String database, List<String> s) {
        if (s.isEmpty()) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_TABLES",
                        Resources.DATABASE,
                        database,
                        q -> {
                            Page<String> page =
                                    visiblePage(
                                            token ->
                                                    catalog.listTables(
                                                            database,
                                                            q.param(MAX),
                                                            token,
                                                            q.param(RESTApi.TABLE_NAME_PATTERN),
                                                            q.param(RESTApi.TABLE_TYPE)),
                                            q.param(MAX),
                                            q.param(PAGE_TOKEN),
                                            t -> reads(q.principal(), Scope.TABLE, database, t),
                                            t -> t);
                            return Response.ok(
                                    new ListTablesResponse(page.elements(), page.nextPageToken()));
                        });
            }
            if (r.is("POST")) {
                return Route.write(
                        "CREATE_TABLE",
                        Resources.DATABASE,
                        database,
                        q -> {
                            CreateTableRequest request = q.body(CreateTableRequest.class);
                            // Audited under the new table's name, not the database's.
                            return auditedUnder(
                                    fullName(request.getIdentifier()),
                                    () -> {
                                        requireNew(
                                                q.principal(),
                                                Scope.TABLE,
                                                request.getIdentifier());
                                        catalog.createTable(q.context(), database, request);
                                    });
                        });
            }
            return null;
        }
        Identifier id = Identifier.create(database, s.get(0));
        String resource = id.getFullName();
        if (s.size() == 1) {
            return switch (r.method()) {
                case "GET" ->
                        Route.read(
                                "GET_TABLE",
                                Resources.TABLE,
                                resource,
                                q -> Response.ok(catalog.getTable(id)));
                case "POST" ->
                        Route.write(
                                "ALTER_TABLE",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    List<SchemaChange> changes =
                                            q.body(AlterTableRequest.class).getChanges();
                                    if (changes == null) {
                                        throw ApiException.badRequest("Missing schema changes.");
                                    }
                                    catalog.alterTable(q.context(), id, changes);
                                    return Response.empty();
                                });
                case "DELETE" ->
                        Route.write(
                                "DROP_TABLE",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    catalog.dropTable(q.context(), id);
                                    return Response.empty();
                                });
                default -> null;
            };
        }
        String sub = s.get(1);
        if (s.size() == 2) {
            return switch (sub + " " + r.method()) {
                case "commit POST" ->
                        Route.write(
                                "COMMIT",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    CommitService.Result result =
                                            catalog.commit(
                                                    q.context(),
                                                    id,
                                                    q.body(CommitTableRequest.class));
                                    Map<String, Object> detail = new LinkedHashMap<>();
                                    detail.put("committed", result.committed());
                                    detail.put("detail", result.detail());
                                    detail.put("snapshotId", result.snapshot().id());
                                    detail.put("commitUser", result.snapshot().commitUser());
                                    detail.put(
                                            "commitIdentifier",
                                            result.snapshot().commitIdentifier());
                                    detail.put("commitKind", result.snapshot().commitKind().name());
                                    return Response.ok(new CommitTableResponse(result.committed()))
                                            .audited(
                                                    result.committed()
                                                            ? AuditLog.Outcome.SUCCEEDED
                                                            : AuditLog.Outcome.FAILED,
                                                    detail);
                                });
                case "rollback POST" ->
                        Route.write(
                                "ROLLBACK",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    RollbackTableRequest body = q.body(RollbackTableRequest.class);
                                    catalog.rollback(
                                            q.context(),
                                            id,
                                            body.getInstant(),
                                            body.getFromSnapshot());
                                    return Response.empty();
                                });
                case "rollback-schema POST" ->
                        Route.write(
                                "ROLLBACK_SCHEMA",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    catalog.rollbackSchema(
                                            q.context(),
                                            id,
                                            q.body(RollbackSchemaRequest.class).getSchemaId());
                                    return Response.empty();
                                });
                case "replace POST" ->
                        Route.write(
                                "REPLACE_TABLE",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    catalog.replaceTable(
                                            q.context(),
                                            id,
                                            q.body(ReplaceTableRequest.class).getSchema());
                                    return Response.empty();
                                });
                case "token GET" ->
                        Route.write(
                                "ISSUE_DATA_TOKEN",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    // Read-only credentials unless the caller may write the table.
                                    DataTokenIssuer.Token token =
                                            catalog.tableToken(
                                                    q.context(),
                                                    id,
                                                    authz.allows(
                                                                    q.principal(),
                                                                    Privilege.WRITE,
                                                                    Scope.TABLE,
                                                                    id.getDatabaseName(),
                                                                    id.getTableName())
                                                            ? SessionPolicy.Access.READ_WRITE
                                                            : SessionPolicy.Access.READ);
                                    return Response.ok(
                                                    new GetTableTokenResponse(
                                                            token.properties(),
                                                            token.expiresAtMillis()))
                                            .audited(AuditLog.Outcome.SUCCEEDED, token.detail());
                                });
                case "auth POST" ->
                        Route.read(
                                "AUTH_TABLE_QUERY",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    Optional<List<String>> readable =
                                            authz.readableColumns(
                                                    q.principal(),
                                                    id.getDatabaseName(),
                                                    id.getTableName());
                                    if (readable.isPresent()) {
                                        AuthTableQueryRequest request =
                                                q.body(AuthTableQueryRequest.class);
                                        catalog.requireColumns(
                                                id, request.select(), readable.get());
                                    }
                                    return Response.ok(catalog.authTableQuery(id));
                                });
                case "snapshot GET" ->
                        Route.read(
                                "LOAD_SNAPSHOT",
                                Resources.TABLE,
                                resource,
                                q ->
                                        Response.ok(
                                                new GetTableSnapshotResponse(
                                                        catalog.latestSnapshot(q.context(), id))));
                case "snapshots GET" ->
                        Route.read(
                                "LIST_SNAPSHOTS",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    PagedList<Snapshot> page =
                                            catalog.listSnapshots(
                                                    id, q.param(MAX), q.param(PAGE_TOKEN));
                                    return Response.ok(
                                            new ListSnapshotsResponse(
                                                    page.getElements(), page.getNextPageToken()));
                                });
                case "consumers GET" ->
                        Route.read(
                                "LIST_CONSUMERS",
                                Resources.TABLE,
                                resource,
                                q ->
                                        Response.ok(
                                                new ListConsumersResponse(
                                                        catalog.listConsumers(id), null)));
                case "partitions GET" ->
                        Route.read(
                                "LIST_PARTITIONS",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    var page =
                                            partitions.listPartitions(
                                                    id,
                                                    q.param(MAX),
                                                    q.param(PAGE_TOKEN),
                                                    q.param(RESTApi.PARTITION_NAME_PATTERN));
                                    return Response.ok(
                                            new ListPartitionsResponse(
                                                    page.elements(), page.nextPageToken()));
                                });
                case "partitions POST" ->
                        Route.write(
                                "CREATE_PARTITIONS",
                                Resources.TABLE,
                                resource,
                                q ->
                                        Response.ok(
                                                partitions.createPartitions(
                                                        q.context(),
                                                        id,
                                                        q.body(CreatePartitionsRequest.class))));
                case "branches GET" ->
                        Route.read(
                                "LIST_BRANCHES",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    // An empty list, not null: Paimon's client turns null into an
                                    // immutable list, which its orphan-file cleaning then adds to.
                                    return Response.ok(
                                            new ListBranchesResponse(catalog.listBranches(id)));
                                });
                case "branches POST" ->
                        Route.write(
                                "CREATE_BRANCH",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    catalog.createBranch(
                                            q.context(), id, q.body(CreateBranchRequest.class));
                                    return Response.empty();
                                });
                case "tags GET" ->
                        Route.read(
                                "LIST_TAGS",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    Page<String> page =
                                            catalog.listTags(
                                                    id,
                                                    q.param(MAX),
                                                    q.param(PAGE_TOKEN),
                                                    q.param(RESTApi.TAG_NAME_PREFIX));
                                    return Response.ok(
                                            new ListTagsResponse(
                                                    page.elements(), page.nextPageToken()));
                                });
                case "tags POST" ->
                        Route.write(
                                "CREATE_TAG",
                                Resources.TABLE,
                                resource,
                                q -> {
                                    catalog.createTag(
                                            q.context(), id, q.body(CreateTagRequest.class));
                                    return Response.empty();
                                });
                default -> null;
            };
        }
        String third = s.get(2);
        if (s.size() == 3) {
            Route fixed =
                    switch (sub + "/" + third + " " + r.method()) {
                        case "partitions/drop POST" ->
                                Route.write(
                                        "DROP_PARTITIONS",
                                        Resources.TABLE,
                                        resource,
                                        q ->
                                                Response.ok(
                                                        partitions.dropPartitions(
                                                                id,
                                                                q.body(
                                                                        DropPartitionsRequest
                                                                                .class))));
                        case "partitions/mark POST" ->
                                Route.write(
                                        "MARK_PARTITIONS_DONE",
                                        Resources.TABLE,
                                        resource,
                                        q -> {
                                            partitions.markDonePartitions(
                                                    q.context(),
                                                    id,
                                                    q.body(MarkDonePartitionsRequest.class)
                                                            .getPartitionSpecs());
                                            return Response.empty();
                                        });
                        case "partitions/list-by-names POST" ->
                                Route.read(
                                        "LIST_PARTITIONS",
                                        Resources.TABLE,
                                        resource,
                                        q ->
                                                Response.ok(
                                                        new ListPartitionsResponse(
                                                                partitions.listPartitionsByNames(
                                                                        id,
                                                                        q.body(
                                                                                        ListPartitionsByNamesRequest
                                                                                                .class)
                                                                                .getPartitionSpecs()),
                                                                null)));
                        case "partitions/list-by-filter POST" ->
                                Route.read(
                                        "LIST_PARTITIONS",
                                        Resources.TABLE,
                                        resource,
                                        q -> {
                                            var page =
                                                    partitions.listPartitionsByFilter(
                                                            id,
                                                            q.body(
                                                                    ListPartitionsByFilterRequest
                                                                            .class));
                                            return Response.ok(
                                                    new ListPartitionsResponse(
                                                            page.elements(), page.nextPageToken()));
                                        });
                        case "consumers/reset POST" ->
                                Route.write(
                                        "RESET_CONSUMER",
                                        Resources.TABLE,
                                        resource,
                                        q -> {
                                            catalog.resetConsumer(
                                                    id, q.body(ResetConsumerRequest.class));
                                            return Response.empty();
                                        });
                        default -> null;
                    };
            if (fixed != null) {
                return fixed;
            }
            if (sub.equals("snapshots") && r.is("GET")) {
                return Route.read(
                        "LOAD_SNAPSHOT",
                        Resources.TABLE,
                        resource,
                        q ->
                                Response.ok(
                                        new GetVersionSnapshotResponse(
                                                catalog.snapshotVersion(q.context(), id, third))));
            }
            if (sub.equals("branches") && r.is("DELETE")) {
                return Route.write(
                        "DROP_BRANCH",
                        Resources.TABLE,
                        resource,
                        q -> {
                            catalog.dropBranch(q.context(), id, third);
                            return Response.empty();
                        });
            }
            if (sub.equals("tags") && r.is("GET")) {
                return Route.read(
                        "GET_TAG",
                        Resources.TABLE,
                        resource,
                        q -> Response.ok(catalog.getTag(id, third)));
            }
            if (sub.equals("tags") && r.is("DELETE")) {
                return Route.write(
                        "DELETE_TAG",
                        Resources.TABLE,
                        resource,
                        q -> {
                            catalog.deleteTag(q.context(), id, third);
                            return Response.empty();
                        });
            }
            return null;
        }
        if (s.size() == 4 && sub.equals("branches") && r.is("POST")) {
            String action = s.get(3);
            if (action.equals("forward")) {
                return Route.write(
                        "FAST_FORWARD_BRANCH",
                        Resources.TABLE,
                        resource,
                        q -> {
                            catalog.fastForward(q.context(), id, third);
                            return Response.empty();
                        });
            }
            if (action.equals("rename")) {
                return Route.write(
                        "RENAME_BRANCH",
                        Resources.TABLE,
                        resource,
                        q -> {
                            throw ApiException.notImplemented(
                                    "Renaming branches is not supported.");
                        });
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- views and functions

    @Nullable
    private Route views(Request r, String database, List<String> s) {
        if (s.isEmpty()) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_VIEWS",
                        Resources.DATABASE,
                        database,
                        q -> {
                            Page<String> page =
                                    visiblePage(
                                            token ->
                                                    objects.listViews(
                                                            database,
                                                            q.param(MAX),
                                                            token,
                                                            q.param(RESTApi.VIEW_NAME_PATTERN)),
                                            q.param(MAX),
                                            q.param(PAGE_TOKEN),
                                            v -> reads(q.principal(), Scope.VIEW, database, v),
                                            v -> v);
                            return Response.ok(
                                    new ListViewsResponse(page.elements(), page.nextPageToken()));
                        });
            }
            if (r.is("POST")) {
                return Route.write(
                        "CREATE_VIEW",
                        Resources.DATABASE,
                        database,
                        q -> {
                            CreateViewRequest request = q.body(CreateViewRequest.class);
                            return auditedUnder(
                                    fullName(request.getIdentifier()),
                                    () -> {
                                        requireNew(
                                                q.principal(), Scope.VIEW, request.getIdentifier());
                                        objects.createView(q.context(), database, request);
                                    });
                        });
            }
            return null;
        }
        if (s.size() != 1) {
            return null;
        }
        Identifier id = Identifier.create(database, s.get(0));
        return switch (r.method()) {
            case "GET" ->
                    Route.read(
                            "GET_VIEW",
                            Resources.VIEW,
                            id.getFullName(),
                            q -> Response.ok(objects.getView(id)));
            case "POST" ->
                    Route.write(
                            "ALTER_VIEW",
                            Resources.VIEW,
                            id.getFullName(),
                            q -> {
                                objects.alterView(q.context(), id, q.body(AlterViewRequest.class));
                                return Response.empty();
                            });
            case "DELETE" ->
                    Route.write(
                            "DROP_VIEW",
                            Resources.VIEW,
                            id.getFullName(),
                            q -> {
                                objects.dropView(id);
                                return Response.empty();
                            });
            default -> null;
        };
    }

    @Nullable
    private Route functions(Request r, String database, List<String> s) {
        if (s.isEmpty()) {
            if (r.is("GET")) {
                return Route.read(
                        "LIST_FUNCTIONS",
                        Resources.DATABASE,
                        database,
                        q -> {
                            Page<String> page =
                                    visiblePage(
                                            token ->
                                                    objects.listFunctions(
                                                            database,
                                                            q.param(MAX),
                                                            token,
                                                            q.param(RESTApi.FUNCTION_NAME_PATTERN)),
                                            q.param(MAX),
                                            q.param(PAGE_TOKEN),
                                            f -> reads(q.principal(), Scope.FUNCTION, database, f),
                                            f -> f);
                            return Response.ok(
                                    new ListFunctionsResponse(
                                            page.elements(), page.nextPageToken()));
                        });
            }
            if (r.is("POST")) {
                return Route.write(
                        "CREATE_FUNCTION",
                        Resources.DATABASE,
                        database,
                        q -> {
                            CreateFunctionRequest request = q.body(CreateFunctionRequest.class);
                            if (request.name() == null) {
                                throw ApiException.badRequest("Missing function name.");
                            }
                            return auditedUnder(
                                    database + "." + request.name(),
                                    () -> {
                                        authz.require(
                                                q.principal(),
                                                Privilege.MANAGE,
                                                new Target(
                                                        Scope.FUNCTION, database, request.name()));
                                        objects.createFunction(q.context(), database, request);
                                    });
                        });
            }
            return null;
        }
        if (s.size() != 1) {
            return null;
        }
        Identifier id = Identifier.create(database, s.get(0));
        return switch (r.method()) {
            case "GET" ->
                    Route.read(
                            "GET_FUNCTION",
                            Resources.FUNCTION,
                            id.getFullName(),
                            q -> Response.ok(objects.getFunction(id)));
            case "POST" ->
                    Route.write(
                            "ALTER_FUNCTION",
                            Resources.FUNCTION,
                            id.getFullName(),
                            q -> {
                                objects.alterFunction(
                                        q.context(), id, q.body(AlterFunctionRequest.class));
                                return Response.empty();
                            });
            case "DELETE" ->
                    Route.write(
                            "DROP_FUNCTION",
                            Resources.FUNCTION,
                            id.getFullName(),
                            q -> {
                                objects.dropFunction(id);
                                return Response.empty();
                            });
            default -> null;
        };
    }

    // ---------------------------------------------------------------- global listings

    @Nullable
    private Route globalTables(Request r, List<String> s) {
        if (s.size() == 1 && r.is("GET")) {
            return Route.read(
                    "LIST_TABLES",
                    Resources.CATALOG,
                    null,
                    q -> {
                        Page<Identifier> page =
                                visiblePage(
                                        token ->
                                                catalog.listTablesGlobally(
                                                        q.param(RESTApi.DATABASE_NAME_PATTERN),
                                                        q.param(RESTApi.TABLE_NAME_PATTERN),
                                                        q.param(MAX),
                                                        token),
                                        q.param(MAX),
                                        q.param(PAGE_TOKEN),
                                        t ->
                                                reads(
                                                        q.principal(),
                                                        Scope.TABLE,
                                                        t.getDatabaseName(),
                                                        t.getTableName()),
                                        t ->
                                                Paging.globalKey(
                                                        t.getDatabaseName(), t.getObjectName()));
                        return Response.ok(
                                new ListTablesGloballyResponse(
                                        page.elements(), page.nextPageToken()));
                    });
        }
        if (s.size() == 2 && s.get(1).equals("rename") && r.is("POST")) {
            return Route.write(
                    "RENAME_TABLE",
                    Resources.CATALOG,
                    null,
                    q -> {
                        RenameTableRequest request = q.body(RenameTableRequest.class);
                        // Audited under the renamed object, the request's source.
                        return auditedUnder(
                                fullName(request.getSource()),
                                () -> {
                                    requireRename(q.principal(), Scope.TABLE, request);
                                    catalog.renameTable(q.context(), request);
                                });
                    });
        }
        if (s.size() == 3 && s.get(1).equals("id") && r.is("GET")) {
            String tableId = s.get(2);
            // Its own action: the target is known only after the lookup, which checks it.
            return Route.read(
                    "GET_TABLE_BY_ID",
                    Resources.TABLE,
                    tableId,
                    q -> {
                        GetTableResponse table = catalog.getTableById(tableId);
                        Target target =
                                new Target(Scope.TABLE, table.getDatabase(), table.getName());
                        Authorizer.Decision decision;
                        try {
                            decision = authz.require(q.principal(), Privilege.READ, target);
                        } catch (ApiException denied) {
                            if (denied.status() != 403 || denied.denial() == null) {
                                throw denied;
                            }
                            // Not found rather than forbidden: the caller named only an id. The
                            // refusal is audited under the id as well, never the table's name.
                            throw ApiException.notFound(
                                            Resources.TABLE,
                                            tableId,
                                            "Table " + tableId + " does not exist.")
                                    .deniedBy(denied.denial());
                        }
                        return Response.ok(table)
                                .audited(AuditLog.Outcome.SUCCEEDED, decision.auditDetail())
                                .withAuditResource(target.name());
                    });
        }
        return null;
    }

    @Nullable
    private Route globalViews(Request r, List<String> s) {
        if (s.size() == 1 && r.is("GET")) {
            return Route.read(
                    "LIST_VIEWS",
                    Resources.CATALOG,
                    null,
                    q -> {
                        Page<Identifier> page =
                                visiblePage(
                                        token ->
                                                objects.listViewsGlobally(
                                                        q.param(RESTApi.DATABASE_NAME_PATTERN),
                                                        q.param(RESTApi.VIEW_NAME_PATTERN),
                                                        q.param(MAX),
                                                        token),
                                        q.param(MAX),
                                        q.param(PAGE_TOKEN),
                                        v ->
                                                reads(
                                                        q.principal(),
                                                        Scope.VIEW,
                                                        v.getDatabaseName(),
                                                        v.getObjectName()),
                                        v ->
                                                Paging.globalKey(
                                                        v.getDatabaseName(), v.getObjectName()));
                        return Response.ok(
                                new ListViewsGloballyResponse(
                                        page.elements(), page.nextPageToken()));
                    });
        }
        if (s.size() == 2 && s.get(1).equals("rename") && r.is("POST")) {
            return Route.write(
                    "RENAME_VIEW",
                    Resources.CATALOG,
                    null,
                    q -> {
                        RenameTableRequest request = q.body(RenameTableRequest.class);
                        // Audited under the renamed object, the request's source.
                        return auditedUnder(
                                fullName(request.getSource()),
                                () -> {
                                    requireRename(q.principal(), Scope.VIEW, request);
                                    objects.renameView(q.context(), request);
                                });
                    });
        }
        return null;
    }

    @Nullable
    private Route globalFunctions(Request r, List<String> s) {
        if (s.size() == 1 && r.is("GET")) {
            return Route.read(
                    "LIST_FUNCTIONS",
                    Resources.CATALOG,
                    null,
                    q -> {
                        Page<Identifier> page =
                                visiblePage(
                                        token ->
                                                objects.listFunctionsGlobally(
                                                        q.param(RESTApi.DATABASE_NAME_PATTERN),
                                                        q.param(RESTApi.FUNCTION_NAME_PATTERN),
                                                        q.param(MAX),
                                                        token),
                                        q.param(MAX),
                                        q.param(PAGE_TOKEN),
                                        f ->
                                                reads(
                                                        q.principal(),
                                                        Scope.FUNCTION,
                                                        f.getDatabaseName(),
                                                        f.getObjectName()),
                                        f ->
                                                Paging.globalKey(
                                                        f.getDatabaseName(), f.getObjectName()));
                        return Response.ok(
                                new ListFunctionsGloballyResponse(
                                        page.elements(), page.nextPageToken()));
                    });
        }
        return null;
    }

    /** What a handler does once it has read the request's body. */
    @FunctionalInterface
    private interface Action {
        void run() throws Exception;
    }

    /**
     * Runs {@code action} of a request on the object {@code resource} names ({@code
     * <database>.<name>}: the object a create makes, a rename's source), whose audit event names it
     * whether the request succeeds, fails or is refused, rather than what an error names or the
     * route's database.
     */
    private static Response auditedUnder(@Nullable String resource, Action action)
            throws Exception {
        try {
            action.run();
        } catch (Exception e) {
            throw RestServer.map(e).auditedAs(resource);
        }
        return Response.empty().withAuditResource(resource);
    }

    @Nullable
    private static String fullName(@Nullable Identifier id) {
        return id == null ? null : id.getFullName();
    }

    /** Renaming needs MANAGE on the object and on its new name. */
    private void requireRename(Principal principal, Scope scope, RenameTableRequest request)
            throws Exception {
        if (request.getSource() == null || request.getDestination() == null) {
            throw ApiException.badRequest("A rename names its source and destination.");
        }
        authz.require(principal, Privilege.MANAGE, objectTarget(scope, request.getSource()));
        authz.require(principal, Privilege.MANAGE, objectTarget(scope, request.getDestination()));
    }

    /** Creating needs MANAGE on the new object itself, so a deny on its name holds. */
    private void requireNew(Principal principal, Scope scope, @Nullable Identifier id)
            throws Exception {
        if (id == null) {
            throw ApiException.badRequest("Missing identifier.");
        }
        Names.validate(id);
        authz.require(principal, Privilege.MANAGE, objectTarget(scope, id));
    }

    private static Target objectTarget(Scope scope, Identifier id) {
        return new Target(
                scope,
                id.getDatabaseName(),
                scope == Scope.TABLE ? id.getTableName() : id.getObjectName());
    }

    /** Every object a database holds, as authorization targets. */
    private List<Target> contents(String database) throws Exception {
        List<Target> objects = new ArrayList<>();
        for (String table : all(token -> catalog.listTables(database, "1000", token, null, null))) {
            objects.add(new Target(Scope.TABLE, database, table));
        }
        for (String view : all(token -> this.objects.listViews(database, "1000", token, null))) {
            objects.add(new Target(Scope.VIEW, database, view));
        }
        for (String fn : all(token -> this.objects.listFunctions(database, "1000", token, null))) {
            objects.add(new Target(Scope.FUNCTION, database, fn));
        }
        return objects;
    }

    /** One page of a listing, by page token. */
    @FunctionalInterface
    private interface PageSource<T> {
        Page<T> page(@Nullable String token) throws Exception;
    }

    private static <T> List<T> all(PageSource<T> source) throws Exception {
        List<T> all = new ArrayList<>();
        String token = null;
        do {
            Page<T> page = source.page(token);
            all.addAll(page.elements());
            token = page.nextPageToken();
        } while (token != null);
        return all;
    }

    /**
     * A page of what the caller may see. Pages are refilled from the next source pages until full,
     * and the next token is the key of the last element returned, so it never names an element the
     * caller may not see.
     *
     * <p>Under a response limit a page also ends before the element that would take its body over
     * the limit; the next page starts with that element. An element too large for a page by itself
     * is an error rather than an empty page, which would end the client's listing early.
     */
    private <T> Page<T> visiblePage(
            PageSource<T> source,
            @Nullable String maxResults,
            @Nullable String token,
            Visible<T> visible,
            Function<T, String> key)
            throws Exception {
        // Without a positive maxResults, a page is as long as the source's own pages.
        int requested = maxResults == null ? 0 : Integer.parseInt(maxResults);
        int max = requested > 0 ? requested : -1;
        List<T> kept = new ArrayList<>();
        long bytes = PAGE_ENVELOPE_BYTES;
        String next = token;
        boolean leftover = false;
        filling:
        do {
            Page<T> page = source.page(next);
            if (max < 0) {
                max = Math.max(1, page.elements().size());
            }
            List<T> elements = page.elements();
            int i = 0;
            for (; i < elements.size() && kept.size() < max; i++) {
                T element = elements.get(i);
                if (!visible.test(element)) {
                    continue;
                }
                if (maxResponseBytes > 0) {
                    // The element with its comma, and its key in case it ends the page.
                    long size = ResponseSize.escaped(element) + 1;
                    long asToken = ResponseSize.escaped(key.apply(element));
                    if (bytes + size + asToken > maxResponseBytes) {
                        if (kept.isEmpty()) {
                            throw tooLarge(key.apply(element), size);
                        }
                        leftover = true;
                        break filling;
                    }
                    bytes += size;
                }
                kept.add(element);
            }
            leftover = i < elements.size();
            next = page.nextPageToken();
        } while (next != null && kept.size() < max);
        boolean more = leftover || next != null;
        return new Page<>(
                kept, more && !kept.isEmpty() ? key.apply(kept.get(kept.size() - 1)) : null);
    }

    private ApiException tooLarge(String name, long size) {
        return new ApiException(
                500,
                null,
                null,
                String.format(
                        "%s takes %d bytes in a listing, more than this server's response limit of"
                                + " %d bytes (COLVENNA_MAX_RESPONSE_BYTES); it cannot be listed"
                                + " through this entry.",
                        name, size, maxResponseBytes));
    }
}
