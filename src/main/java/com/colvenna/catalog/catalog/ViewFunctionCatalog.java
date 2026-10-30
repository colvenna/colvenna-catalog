package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.CatalogStore.LockMode;
import com.colvenna.catalog.catalog.CatalogStore.ObjectKind;
import com.colvenna.catalog.catalog.CatalogStore.ObjectRow;
import com.colvenna.catalog.catalog.Paging.Page;
import com.colvenna.catalog.catalog.Records.DatabaseRecord;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.http.ApiException;
import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.function.Function;
import org.apache.paimon.function.FunctionChange;
import org.apache.paimon.function.FunctionDefinition;
import org.apache.paimon.function.FunctionImpl;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.rest.requests.AlterFunctionRequest;
import org.apache.paimon.rest.requests.AlterViewRequest;
import org.apache.paimon.rest.requests.CreateFunctionRequest;
import org.apache.paimon.rest.requests.CreateViewRequest;
import org.apache.paimon.rest.requests.RenameTableRequest;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.apache.paimon.rest.responses.GetFunctionResponse;
import org.apache.paimon.rest.responses.GetViewResponse;
import org.apache.paimon.view.ViewChange;
import org.apache.paimon.view.ViewSchema;

/** Views and functions, which live only in PostgreSQL. */
public final class ViewFunctionCatalog {

    private final Db db;
    private final CatalogStore store;
    private final Paging paging;
    private final Clock clock;

    public ViewFunctionCatalog(Db db, CatalogStore store, Paging paging, Clock clock) {
        this.db = db;
        this.store = store;
        this.paging = paging;
        this.clock = clock;
    }

    // ================================================================ views

    public Page<String> listViews(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(ObjectKind.VIEW, database, null, pattern, maxResults, pageToken);
        return new Page<>(
                page.elements().stream().map(ObjectRow::name).collect(Collectors.toList()),
                page.nextPageToken());
    }

    public Page<GetViewResponse> listViewDetails(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(ObjectKind.VIEW, database, null, pattern, maxResults, pageToken);
        List<GetViewResponse> views = new ArrayList<>();
        for (ObjectRow row : page.elements()) {
            views.add(viewResponse(row));
        }
        return new Page<>(views, page.nextPageToken());
    }

    public Page<Identifier> listViewsGlobally(
            @Nullable String databasePattern,
            @Nullable String pattern,
            @Nullable String maxResults,
            @Nullable String pageToken)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(ObjectKind.VIEW, null, databasePattern, pattern, maxResults, pageToken);
        return new Page<>(
                page.elements().stream()
                        .map(r -> Identifier.create(r.database(), r.name()))
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    public GetViewResponse getView(Identifier identifier) throws Exception {
        return viewResponse(requireObject(ObjectKind.VIEW, identifier));
    }

    public void createView(RequestContext ctx, String database, CreateViewRequest request)
            throws Exception {
        Identifier identifier = request.getIdentifier();
        if (!identifier.getDatabaseName().equals(database)) {
            throw ApiException.badRequest(
                    "View " + identifier + " does not belong to database " + database + ".");
        }
        Names.validate("database", identifier.getDatabaseName());
        Names.validate("view", identifier.getObjectName());
        insertObject(ctx, ObjectKind.VIEW, identifier, RESTApi.toJson(request.getSchema()));
    }

    public void alterView(RequestContext ctx, Identifier identifier, AlterViewRequest request)
            throws Exception {
        db.inTransaction(
                c -> {
                    ObjectRow row = lockObject(c, ObjectKind.VIEW, identifier);
                    ViewSchema view = RESTApi.fromJson(row.json(), ViewSchema.class);
                    Map<String, String> dialects = new HashMap<>(view.dialects());
                    Map<String, String> options = new HashMap<>(view.options());
                    String comment = view.comment();
                    for (ViewChange change : request.viewChanges()) {
                        if (change instanceof ViewChange.SetViewOption set) {
                            options.put(set.key(), set.value());
                        } else if (change instanceof ViewChange.RemoveViewOption remove) {
                            options.remove(remove.key());
                        } else if (change instanceof ViewChange.UpdateViewComment update) {
                            comment = update.comment();
                        } else if (change instanceof ViewChange.AddDialect add) {
                            if (dialects.containsKey(add.dialect())) {
                                throw ApiException.conflict(
                                        ErrorResponse.RESOURCE_TYPE_DIALECT,
                                        add.dialect(),
                                        "Dialect " + add.dialect() + " already exists.");
                            }
                            dialects.put(add.dialect(), add.query());
                        } else if (change instanceof ViewChange.UpdateDialect update) {
                            if (!dialects.containsKey(update.dialect())) {
                                throw dialectMissing(update.dialect());
                            }
                            dialects.put(update.dialect(), update.query());
                        } else if (change instanceof ViewChange.DropDialect drop) {
                            if (dialects.remove(drop.dialect()) == null) {
                                throw dialectMissing(drop.dialect());
                            }
                        }
                    }
                    ViewSchema next =
                            new ViewSchema(view.fields(), view.query(), dialects, comment, options);
                    store.updateObject(
                            c,
                            ObjectKind.VIEW,
                            identifier.getDatabaseName(),
                            identifier.getObjectName(),
                            RESTApi.toJson(next),
                            clock.millis(),
                            ctx.who());
                    return null;
                });
    }

    public void renameView(RequestContext ctx, RenameTableRequest request) throws Exception {
        Names.validate("view", request.getDestination().getObjectName());
        Names.validate("database", request.getDestination().getDatabaseName());
        renameObject(ctx, ObjectKind.VIEW, request.getSource(), request.getDestination());
    }

    public void dropView(Identifier identifier) throws Exception {
        dropObject(ObjectKind.VIEW, identifier);
    }

    private GetViewResponse viewResponse(ObjectRow row) throws Exception {
        return new GetViewResponse(
                row.id().toString(),
                row.name(),
                RESTApi.fromJson(row.json(), ViewSchema.class),
                row.owner(),
                row.createdAt(),
                row.createdBy(),
                row.updatedAt(),
                row.updatedBy());
    }

    private static ApiException dialectMissing(String dialect) {
        return ApiException.notFound(
                ErrorResponse.RESOURCE_TYPE_DIALECT,
                dialect,
                "Dialect " + dialect + " does not exist.");
    }

    // ================================================================ functions

    public Page<String> listFunctions(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(ObjectKind.FUNCTION, database, null, pattern, maxResults, pageToken);
        return new Page<>(
                page.elements().stream().map(ObjectRow::name).collect(Collectors.toList()),
                page.nextPageToken());
    }

    public Page<GetFunctionResponse> listFunctionDetails(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(ObjectKind.FUNCTION, database, null, pattern, maxResults, pageToken);
        List<GetFunctionResponse> functions = new ArrayList<>();
        for (ObjectRow row : page.elements()) {
            functions.add(functionResponse(row));
        }
        return new Page<>(functions, page.nextPageToken());
    }

    public Page<Identifier> listFunctionsGlobally(
            @Nullable String databasePattern,
            @Nullable String pattern,
            @Nullable String maxResults,
            @Nullable String pageToken)
            throws Exception {
        Page<ObjectRow> page =
                objectPage(
                        ObjectKind.FUNCTION, null, databasePattern, pattern, maxResults, pageToken);
        return new Page<>(
                page.elements().stream()
                        .map(r -> Identifier.create(r.database(), r.name()))
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    public GetFunctionResponse getFunction(Identifier identifier) throws Exception {
        return functionResponse(requireObject(ObjectKind.FUNCTION, identifier));
    }

    public void createFunction(RequestContext ctx, String database, CreateFunctionRequest request)
            throws Exception {
        Identifier identifier = Identifier.create(database, request.name());
        Names.validate("function", request.name());
        Function function =
                new FunctionImpl(
                        identifier,
                        request.inputParams(),
                        request.returnParams(),
                        request.isDeterministic(),
                        request.definitions(),
                        request.comment(),
                        request.options());
        insertObject(
                ctx,
                ObjectKind.FUNCTION,
                identifier,
                RESTApi.toJson(new CreateFunctionRequest(function)));
    }

    public void alterFunction(
            RequestContext ctx, Identifier identifier, AlterFunctionRequest request)
            throws Exception {
        db.inTransaction(
                c -> {
                    ObjectRow row = lockObject(c, ObjectKind.FUNCTION, identifier);
                    CreateFunctionRequest stored =
                            RESTApi.fromJson(row.json(), CreateFunctionRequest.class);
                    Map<String, FunctionDefinition> definitions =
                            stored.definitions() == null
                                    ? new HashMap<>()
                                    : new HashMap<>(stored.definitions());
                    Map<String, String> options =
                            stored.options() == null
                                    ? new HashMap<>()
                                    : new HashMap<>(stored.options());
                    String comment = stored.comment();
                    for (FunctionChange change : request.changes()) {
                        if (change instanceof FunctionChange.SetFunctionOption set) {
                            options.put(set.key(), set.value());
                        } else if (change instanceof FunctionChange.RemoveFunctionOption remove) {
                            options.remove(remove.key());
                        } else if (change instanceof FunctionChange.UpdateFunctionComment update) {
                            comment = update.comment();
                        } else if (change instanceof FunctionChange.AddDefinition add) {
                            if (definitions.containsKey(add.name())) {
                                throw ApiException.conflict(
                                        ErrorResponse.RESOURCE_TYPE_DEFINITION,
                                        add.name(),
                                        "Definition " + add.name() + " already exists.");
                            }
                            definitions.put(add.name(), add.definition());
                        } else if (change instanceof FunctionChange.UpdateDefinition update) {
                            if (!definitions.containsKey(update.name())) {
                                throw definitionMissing(update.name());
                            }
                            definitions.put(update.name(), update.definition());
                        } else if (change instanceof FunctionChange.DropDefinition drop) {
                            if (definitions.remove(drop.name()) == null) {
                                throw definitionMissing(drop.name());
                            }
                        }
                    }
                    Function next =
                            new FunctionImpl(
                                    identifier,
                                    stored.inputParams(),
                                    stored.returnParams(),
                                    stored.isDeterministic(),
                                    definitions,
                                    comment,
                                    options);
                    store.updateObject(
                            c,
                            ObjectKind.FUNCTION,
                            identifier.getDatabaseName(),
                            identifier.getObjectName(),
                            RESTApi.toJson(new CreateFunctionRequest(next)),
                            clock.millis(),
                            ctx.who());
                    return null;
                });
    }

    public void dropFunction(Identifier identifier) throws Exception {
        dropObject(ObjectKind.FUNCTION, identifier);
    }

    private GetFunctionResponse functionResponse(ObjectRow row) throws Exception {
        CreateFunctionRequest f = RESTApi.fromJson(row.json(), CreateFunctionRequest.class);
        return new GetFunctionResponse(
                row.id().toString(),
                row.name(),
                f.inputParams(),
                f.returnParams(),
                f.isDeterministic(),
                f.definitions(),
                f.comment(),
                f.options(),
                row.owner(),
                row.createdAt(),
                row.createdBy(),
                row.updatedAt(),
                row.updatedBy());
    }

    private static ApiException definitionMissing(String name) {
        return ApiException.notFound(
                ErrorResponse.RESOURCE_TYPE_DEFINITION,
                name,
                "Definition " + name + " does not exist.");
    }

    // ================================================================ shared object helpers

    private Page<ObjectRow> objectPage(
            ObjectKind kind,
            @Nullable String database,
            @Nullable String databasePattern,
            @Nullable String pattern,
            @Nullable String maxResults,
            @Nullable String pageToken)
            throws Exception {
        int size = paging.pageSize(maxResults);
        String dbLike = Paging.likePattern(databasePattern);
        String like = Paging.likePattern(pattern);
        List<ObjectRow> rows =
                db.inTransaction(
                        c -> {
                            if (database != null) {
                                requireDatabase(c, database, LockMode.NONE);
                            }
                            return store.objects(c, kind, database, dbLike, like, pageToken, size);
                        });
        return Paging.page(
                rows,
                size,
                r -> database != null ? r.name() : Paging.globalKey(r.database(), r.name()));
    }

    private void insertObject(
            RequestContext ctx, ObjectKind kind, Identifier identifier, String json)
            throws Exception {
        long now = clock.millis();
        db.inTransaction(
                c -> {
                    requireDatabase(c, identifier.getDatabaseName(), LockMode.FOR_SHARE);
                    boolean inserted =
                            store.insertObject(
                                    c,
                                    kind,
                                    new ObjectRow(
                                            identifier.getDatabaseName(),
                                            identifier.getObjectName(),
                                            UUID.randomUUID(),
                                            json,
                                            ctx.who(),
                                            now,
                                            ctx.who(),
                                            now,
                                            ctx.who()));
                    if (!inserted) {
                        throw objectExists(kind, identifier);
                    }
                    return null;
                });
    }

    private ObjectRow requireObject(ObjectKind kind, Identifier identifier) throws Exception {
        return db.inTransaction(
                c ->
                        store.object(
                                        c,
                                        kind,
                                        identifier.getDatabaseName(),
                                        identifier.getObjectName(),
                                        false)
                                .orElseThrow(() -> objectMissing(kind, identifier)));
    }

    private ObjectRow lockObject(Connection c, ObjectKind kind, Identifier identifier)
            throws Exception {
        return store.object(c, kind, identifier.getDatabaseName(), identifier.getObjectName(), true)
                .orElseThrow(() -> objectMissing(kind, identifier));
    }

    private void renameObject(RequestContext ctx, ObjectKind kind, Identifier from, Identifier to)
            throws Exception {
        db.inTransaction(
                c -> {
                    lockObject(c, kind, from);
                    requireDatabase(c, to.getDatabaseName(), LockMode.NONE);
                    if (store.object(c, kind, to.getDatabaseName(), to.getObjectName(), false)
                            .isPresent()) {
                        throw objectExists(kind, to);
                    }
                    store.renameObject(
                            c,
                            kind,
                            from.getDatabaseName(),
                            from.getObjectName(),
                            to.getDatabaseName(),
                            to.getObjectName(),
                            clock.millis(),
                            ctx.who());
                    return null;
                });
    }

    private void dropObject(ObjectKind kind, Identifier identifier) throws Exception {
        db.inTransaction(
                c -> {
                    if (!store.deleteObject(
                            c, kind, identifier.getDatabaseName(), identifier.getObjectName())) {
                        throw objectMissing(kind, identifier);
                    }
                    return null;
                });
    }

    private static ApiException objectExists(ObjectKind kind, Identifier identifier) {
        String type =
                kind == ObjectKind.VIEW
                        ? ErrorResponse.RESOURCE_TYPE_VIEW
                        : ErrorResponse.RESOURCE_TYPE_DEFINITION;
        return ApiException.conflict(
                type, identifier.getObjectName(), identifier.getFullName() + " already exists.");
    }

    private static ApiException objectMissing(ObjectKind kind, Identifier identifier) {
        String type =
                kind == ObjectKind.VIEW
                        ? ErrorResponse.RESOURCE_TYPE_VIEW
                        : ErrorResponse.RESOURCE_TYPE_FUNCTION;
        return ApiException.notFound(
                type, identifier.getObjectName(), identifier.getFullName() + " does not exist.");
    }

    private DatabaseRecord requireDatabase(Connection c, String name, LockMode lock)
            throws Exception {
        return CatalogService.requireDatabase(store, c, name, lock);
    }
}
