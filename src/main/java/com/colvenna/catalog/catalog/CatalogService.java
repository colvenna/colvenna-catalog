package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.CatalogStore.LockMode;
import com.colvenna.catalog.catalog.Paging.Page;
import com.colvenna.catalog.catalog.Records.BranchRecord;
import com.colvenna.catalog.catalog.Records.DatabaseRecord;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.Records.TableState;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.fs.PathRules;
import com.colvenna.catalog.fs.Roots;
import com.colvenna.catalog.fs.TableFiles;
import com.colvenna.catalog.http.ApiException;
import com.colvenna.catalog.metrics.CatalogMetrics;
import com.colvenna.catalog.token.DataTokenIssuer;
import com.colvenna.catalog.token.SessionPolicy;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.stream.Collectors;
import javax.annotation.Nullable;
import org.apache.paimon.CoreOptions;
import org.apache.paimon.PagedList;
import org.apache.paimon.Snapshot;
import org.apache.paimon.TableType;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.consumer.Consumer;
import org.apache.paimon.consumer.ConsumerInfo;
import org.apache.paimon.consumer.ConsumerManager;
import org.apache.paimon.fs.Path;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalogInternalOptions;
import org.apache.paimon.rest.requests.AlterDatabaseRequest;
import org.apache.paimon.rest.requests.CommitTableRequest;
import org.apache.paimon.rest.requests.CreateBranchRequest;
import org.apache.paimon.rest.requests.CreateDatabaseRequest;
import org.apache.paimon.rest.requests.CreateTableRequest;
import org.apache.paimon.rest.requests.CreateTagRequest;
import org.apache.paimon.rest.requests.RegisterTableRequest;
import org.apache.paimon.rest.requests.RenameTableRequest;
import org.apache.paimon.rest.requests.ResetConsumerRequest;
import org.apache.paimon.rest.responses.AlterDatabaseResponse;
import org.apache.paimon.rest.responses.AuthTableQueryResponse;
import org.apache.paimon.rest.responses.ConfigResponse;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.apache.paimon.rest.responses.GetDatabaseResponse;
import org.apache.paimon.rest.responses.GetTableResponse;
import org.apache.paimon.rest.responses.GetTagResponse;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.schema.SchemaChange;
import org.apache.paimon.schema.SchemaManager;
import org.apache.paimon.schema.TableSchema;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.TableSnapshot;
import org.apache.paimon.table.object.ObjectTable;
import org.apache.paimon.tag.Tag;
import org.apache.paimon.utils.BranchManager;
import org.apache.paimon.utils.LazyField;
import org.apache.paimon.utils.SnapshotManager;
import org.apache.paimon.utils.TimeUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The catalog operations behind the Paimon 2.0.0 REST endpoints.
 *
 * <p>PostgreSQL is the registry of databases, tables, views, functions, branch heads and
 * catalog-managed partitions. Table directories keep Paimon's own file layout: the server writes
 * schema and snapshot files and reads tags, branches and consumers from them, so engines that read
 * the files directly see the same table.
 */
public final class CatalogService {

    private static final Logger LOG = LoggerFactory.getLogger(CatalogService.class);

    private static final String MAIN = Identifier.DEFAULT_MAIN_BRANCH;

    /**
     * Client options the catalog's config answers as defaults ({@code GET /v1/config}): a client's
     * own setting of a key wins over them, and {@code COLVENNA_CLIENT_OVERRIDES} wins over both.
     * Paimon merges them into the options its FileIO is made with, per-table credentials included
     * ({@code RESTTokenFileIO}).
     *
     * <p>{@code fs.s3a.create.performance} (#1): without it, S3A (Hadoop 3.4.2, which the {@code
     * paimon-s3} of Paimon 2.0.0 and 2.1.0 bundles) sends a HEAD and a LIST before every file it
     * creates without overwrite, and a LIST before one it overwrites ({@code
     * S3AFileSystem.innerCreateFile}); with it, none. Paimon's client creates without overwrite
     * only files it names with a UUID of its own (data, manifest, manifest list, index and
     * statistics files, and the temporary file of an atomic write, whose rename still refuses an
     * existing target), and overwrites only files at paths that are never directories (hints, tags,
     * consumers, and index files of UUID names). Snapshots are not affected: the catalog publishes
     * them with a conditional write of its own. A commit creates four such files, and each retry on
     * a newer snapshot three.
     */
    static final Map<String, String> CLIENT_DEFAULTS = Map.of("fs.s3a.create.performance", "true");

    private final Db db;
    private final CatalogStore store;
    private final TableFiles files;
    private final CommitService commits;
    private final DataTokenIssuer dataTokens;
    private final Paging paging;
    private final Clock clock;
    private final String warehouseName;
    private final String warehouseRoot;
    private final PathRules paths;
    private final Map<String, String> clientOverrides;
    private final TablePurges purges;
    private final FilePurges filePurges;
    private final TablePurges.Mode dropPurge;
    private final Duration workTime;

    public CatalogService(
            Db db,
            CatalogStore store,
            TableFiles files,
            CommitService commits,
            DataTokenIssuer dataTokens,
            Paging paging,
            Clock clock,
            String warehouseName,
            String warehouseRoot,
            List<String> externalLocationRoots,
            Map<String, String> clientOverrides,
            TablePurges.Mode dropPurge,
            Duration workTime,
            CatalogMetrics metrics) {
        this.db = db;
        this.store = store;
        this.files = files;
        this.commits = commits;
        this.dataTokens = dataTokens;
        this.paging = paging;
        this.clock = clock;
        this.warehouseName = warehouseName;
        this.warehouseRoot = PathRules.normalize(warehouseRoot);
        this.paths = new PathRules(warehouseRoot, externalLocationRoots);
        this.clientOverrides = clientOverrides;
        this.dropPurge = dropPurge;
        this.workTime = workTime;
        this.purges = new TablePurges(db, store, files, paths, clock, metrics);
        this.filePurges = new FilePurges(db, store, files, clock, metrics);
    }

    public CommitService commits() {
        return commits;
    }

    /** The files of dropped tables still to be deleted. */
    public TablePurges purges() {
        return purges;
    }

    /** The files rollbacks and tag and branch deletions left, still to be deleted (#93). */
    public FilePurges filePurges() {
        return filePurges;
    }

    /** Whether a request deletes the files it leaves itself (the EKS form), or a sweep does. */
    private boolean inline() {
        return dropPurge == TablePurges.Mode.INLINE;
    }

    /**
     * When a request that began now must stop recording what it removes: its work time from now
     * ({@code COLVENNA_REQUEST_WORK_TIME}), in {@link System#nanoTime()}; 0 without a limit.
     */
    private long deadline() {
        if (workTime.isZero()) {
            return 0;
        }
        long deadline = System.nanoTime() + workTime.toNanos();
        return deadline == 0 ? 1 : deadline;
    }

    /** Purges now what a request recorded, when it purges inline; never throws. */
    private void purgeInline(Optional<FilePurges.Pending> recorded) {
        if (recorded.isPresent() && inline()) {
            filePurges.purge(recorded.get());
        }
    }

    public CommitService.Result commit(
            RequestContext ctx, Identifier identifier, CommitTableRequest request)
            throws Exception {
        return commits.commit(
                ctx, requireTableAndBranch(identifier), branchOf(identifier), request);
    }

    public void rollback(
            RequestContext ctx,
            Identifier identifier,
            org.apache.paimon.table.Instant instant,
            @Nullable Long fromSnapshot)
            throws Exception {
        long deadline = deadline();
        purgeInline(
                commits.rollback(
                        ctx,
                        requireTableAndBranch(identifier),
                        branchOf(identifier),
                        instant,
                        fromSnapshot,
                        filePurges,
                        inline(),
                        deadline));
    }

    // ================================================================ config

    public ConfigResponse config(@Nullable String warehouse) {
        if (warehouse != null && !warehouse.isEmpty() && !warehouse.equals(warehouseName)) {
            throw ApiException.notFound(
                    null,
                    warehouse,
                    "Unknown warehouse "
                            + warehouse
                            + "; this catalog serves "
                            + warehouseName
                            + ".");
        }
        Map<String, String> overrides = new HashMap<>(clientOverrides);
        overrides.put(RESTCatalogInternalOptions.PREFIX.key(), warehouseName);
        return new ConfigResponse(CLIENT_DEFAULTS, overrides);
    }

    // ================================================================ databases

    public Page<String> listDatabases(
            @Nullable String maxResults, @Nullable String pageToken, @Nullable String pattern)
            throws Exception {
        int size = paging.pageSize(maxResults);
        String like = Paging.likePattern(pattern);
        return db.inTransaction(
                c -> Paging.page(store.databaseNames(c, like, pageToken, size), size, n -> n));
    }

    public void createDatabase(RequestContext ctx, CreateDatabaseRequest request) throws Exception {
        String name = request.getName();
        Names.validateDatabase(name);
        Map<String, String> options =
                request.getOptions() == null
                        ? new HashMap<>()
                        : new HashMap<>(request.getOptions());
        String location = options.getOrDefault(Catalog.DB_LOCATION_PROP, databaseDirectory(name));
        options.remove(Catalog.DB_LOCATION_PROP);
        long now = clock.millis();
        db.inTransaction(
                c -> {
                    if (store.database(c, name, LockMode.NONE).isPresent()) {
                        throw ApiException.conflict(
                                ErrorResponse.RESOURCE_TYPE_DATABASE,
                                name,
                                "Database " + name + " already exists.");
                    }
                    store.insertDatabase(
                            c,
                            new DatabaseRecord(
                                    name,
                                    UUID.randomUUID(),
                                    location,
                                    options,
                                    options.getOrDefault(Catalog.OWNER_PROP, ctx.who()),
                                    now,
                                    ctx.who(),
                                    now,
                                    ctx.who()));
                    return null;
                });
    }

    public GetDatabaseResponse getDatabase(String name) throws Exception {
        DatabaseRecord d = db.inTransaction(c -> requireDatabase(c, name, LockMode.NONE));
        return new GetDatabaseResponse(
                d.id().toString(),
                d.name(),
                d.location(),
                d.options(),
                d.owner(),
                d.createdAt(),
                d.createdBy(),
                d.updatedAt(),
                d.updatedBy());
    }

    public AlterDatabaseResponse alterDatabase(
            RequestContext ctx, String name, AlterDatabaseRequest request) throws Exception {
        List<String> removals = request.getRemovals() == null ? List.of() : request.getRemovals();
        Map<String, String> updates =
                request.getUpdates() == null ? Map.of() : request.getUpdates();
        return db.inTransaction(
                c -> {
                    DatabaseRecord d = requireDatabase(c, name, LockMode.FOR_UPDATE);
                    Map<String, String> options = new HashMap<>(d.options());
                    List<String> removed = new ArrayList<>();
                    List<String> missing = new ArrayList<>();
                    for (String key : removals) {
                        if (options.remove(key) != null) {
                            removed.add(key);
                        } else {
                            missing.add(key);
                        }
                    }
                    options.putAll(updates);
                    store.updateDatabaseOptions(c, name, options, clock.millis(), ctx.who());
                    return new AlterDatabaseResponse(
                            removed, new ArrayList<>(updates.keySet()), missing);
                });
    }

    /**
     * Drops a database with everything in it; the client refuses non-empty drops without cascade.
     * With a work time ({@code COLVENNA_REQUEST_WORK_TIME}, the serverless form's), a request that
     * has dropped tables for that long answers a retryable 503 instead of going on (#69): each
     * table dropped stays dropped, and the request sent again drops the rest, then the database.
     * The database's directory goes once nothing is left in it (#13): here when the request purges
     * inline, else with the purge of its last table.
     */
    public void dropDatabase(RequestContext ctx, String name) throws Exception {
        long started = System.nanoTime();
        List<TableRecord> tables =
                db.inTransaction(
                        c -> {
                            requireDatabase(c, name, LockMode.FOR_UPDATE);
                            return store.tables(c, name, null, null, null, Integer.MAX_VALUE);
                        });
        int dropped = 0;
        for (TableRecord table : tables) {
            if (!workTime.isZero()
                    && dropped > 0
                    && System.nanoTime() - started >= workTime.toNanos()) {
                throw ApiException.unavailable(
                        "Dropped "
                                + dropped
                                + " of the "
                                + tables.size()
                                + " tables of database "
                                + name
                                + " in this request; send it again to drop the rest.",
                        null);
            }
            dropTable(ctx, Identifier.create(name, table.name()));
            dropped++;
        }
        db.inTransaction(
                c -> {
                    requireDatabase(c, name, LockMode.FOR_UPDATE);
                    if (store.countTables(c, name) > 0) {
                        throw ApiException.conflict(
                                ErrorResponse.RESOURCE_TYPE_DATABASE,
                                name,
                                "Database " + name + " gained tables while being dropped.");
                    }
                    store.deleteDatabase(c, name);
                    return null;
                });
        if (dropPurge == TablePurges.Mode.INLINE) {
            removeDatabaseDirectory(name);
        }
    }

    /** The directory of a database's managed tables, {@code <warehouse>/<db>.db}. */
    private String databaseDirectory(String name) {
        return warehouseRoot + "/" + name + ".db";
    }

    /**
     * Removes a dropped database's directory {@code <warehouse>/<db>.db} if nothing is left in it
     * (#13), as purging its tables does (the deferred form leaves it to them): its zero-byte S3A
     * marker, or an empty local directory. Never its location option, which the client gave and
     * nothing here writes to, nor anything still in the directory, such as the root of a table
     * renamed into another database. The database is dropped whatever happens here.
     */
    private void removeDatabaseDirectory(String name) {
        String directory = databaseDirectory(name);
        try {
            if (!files.removeIfEmpty(directory)) {
                LOG.info(
                        "Kept the directory {} of dropped database {}: not empty", directory, name);
            }
        } catch (IOException | RuntimeException e) {
            LOG.warn(
                    "Could not remove the directory {} of dropped database {}", directory, name, e);
        }
    }

    private DatabaseRecord requireDatabase(Connection c, String name, LockMode lock)
            throws Exception {
        return requireDatabase(store, c, name, lock);
    }

    static DatabaseRecord requireDatabase(
            CatalogStore store, Connection c, String name, LockMode lock) throws Exception {
        return store.database(c, name, lock)
                .orElseThrow(
                        () ->
                                ApiException.notFound(
                                        ErrorResponse.RESOURCE_TYPE_DATABASE,
                                        name,
                                        "Database " + name + " does not exist."));
    }

    // ================================================================ tables

    public Page<String> listTables(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern,
            @Nullable String tableType)
            throws Exception {
        Page<TableRecord> page = tablePage(database, maxResults, pageToken, pattern, tableType);
        return new Page<>(
                page.elements().stream().map(TableRecord::name).collect(Collectors.toList()),
                page.nextPageToken());
    }

    public Page<GetTableResponse> listTableDetails(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern,
            @Nullable String tableType)
            throws Exception {
        Page<TableRecord> page = tablePage(database, maxResults, pageToken, pattern, tableType);
        return new Page<>(
                page.elements().stream()
                        .map(t -> tableResponse(t, t.name(), t.schemaId(), t.schema()))
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    private Page<TableRecord> tablePage(
            String database,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern,
            @Nullable String tableType)
            throws Exception {
        int size = paging.pageSize(maxResults);
        String like = Paging.likePattern(pattern);
        String type = tableType == null || tableType.isEmpty() ? null : tableType;
        return db.inTransaction(
                c -> {
                    requireDatabase(c, database, LockMode.NONE);
                    return Paging.page(
                            store.tables(c, database, like, type, pageToken, size),
                            size,
                            TableRecord::name);
                });
    }

    public Page<Identifier> listTablesGlobally(
            @Nullable String databasePattern,
            @Nullable String tablePattern,
            @Nullable String maxResults,
            @Nullable String pageToken)
            throws Exception {
        int size = paging.pageSize(maxResults);
        String dbLike = Paging.likePattern(databasePattern);
        String tableLike = Paging.likePattern(tablePattern);
        List<TableRecord> rows =
                db.inTransaction(c -> store.tablesGlobally(c, dbLike, tableLike, pageToken, size));
        Page<TableRecord> page =
                Paging.page(rows, size, t -> Paging.globalKey(t.database(), t.name()));
        return new Page<>(
                page.elements().stream()
                        .map(t -> Identifier.create(t.database(), t.name()))
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    public GetTableResponse getTable(Identifier identifier) throws Exception {
        TableRecord table = requireTable(identifier);
        String branch = branchOf(identifier);
        if (BranchManager.isMainBranch(branch)) {
            return tableResponse(
                    table, identifier.getObjectName(), table.schemaId(), table.schema());
        }
        TableSchema branchSchema =
                files.latestSchema(table.path(), branch)
                        .orElseThrow(
                                () ->
                                        ApiException.notFound(
                                                ErrorResponse.RESOURCE_TYPE_TABLE,
                                                identifier.getObjectName(),
                                                "Branch "
                                                        + branch
                                                        + " of "
                                                        + table.fullName()
                                                        + " does not exist."));
        return tableResponse(
                table,
                identifier.getObjectName(),
                branchSchema.id(),
                withoutPath(branchSchema.toSchema()));
    }

    public GetTableResponse getTableById(String id) throws Exception {
        UUID uuid;
        try {
            uuid = UUID.fromString(id);
        } catch (IllegalArgumentException e) {
            throw ApiException.notFound(ErrorResponse.RESOURCE_TYPE_TABLE, id, "Table not exist.");
        }
        TableRecord table =
                db.inTransaction(c -> store.tableById(c, uuid))
                        .orElseThrow(
                                () ->
                                        ApiException.notFound(
                                                ErrorResponse.RESOURCE_TYPE_TABLE,
                                                id,
                                                "Table not exist."));
        return tableResponse(table, table.name(), table.schemaId(), table.schema());
    }

    public void createTable(RequestContext ctx, String database, CreateTableRequest request)
            throws Exception {
        Identifier identifier = request.getIdentifier();
        if (!identifier.getDatabaseName().equals(database)) {
            throw ApiException.badRequest(
                    "Table " + identifier + " does not belong to database " + database + ".");
        }
        if (identifier.getBranchName() != null || identifier.isSystemTable()) {
            throw ApiException.badRequest("Cannot create branch or system table " + identifier);
        }
        Names.validate(identifier);
        Schema schema = request.getSchema();
        Map<String, String> options = new HashMap<>(schema.options());
        paths.checkClientLocations(options);
        TableType type = Options.fromMap(options).get(CoreOptions.TYPE);
        String explicitPath =
                options.containsKey(CoreOptions.PATH.key())
                        ? PathRules.normalize(options.get(CoreOptions.PATH.key()))
                        : null;
        boolean external = explicitPath != null;
        long now = clock.millis();
        db.inTransaction(
                c -> {
                    requireDatabase(c, identifier.getDatabaseName(), LockMode.FOR_SHARE);
                    store.claimTableName(
                            c, identifier.getDatabaseName(), identifier.getTableName());
                    if (store.table(
                                    c,
                                    identifier.getDatabaseName(),
                                    identifier.getTableName(),
                                    false)
                            .isPresent()) {
                        throw tableExists(identifier);
                    }
                    String path;
                    if (external) {
                        store.claimPath(c, explicitPath);
                        path = explicitPath;
                    } else {
                        path = claimManagedPath(c, identifier);
                    }
                    rejectOverlap(c, path);
                    Schema stored;
                    long schemaId;
                    switch (type) {
                        case TABLE, MATERIALIZED_TABLE -> {
                            TableSchema created = createSchemaFiles(path, schema, external);
                            schemaId = created.id();
                            stored = withoutPath(created.toSchema());
                        }
                        case FORMAT_TABLE, ICEBERG_TABLE, LANCE_TABLE -> {
                            if (!files.exists(path)) {
                                files.mkdirs(path);
                            }
                            schemaId = 0;
                            stored = withoutPath(TableSchema.create(0, schema).toSchema());
                        }
                        case OBJECT_TABLE -> {
                            schemaId = 1;
                            stored =
                                    withoutPath(
                                            new Schema(
                                                    ObjectTable.SCHEMA.getFields(),
                                                    schema.partitionKeys(),
                                                    schema.primaryKeys(),
                                                    schema.options(),
                                                    schema.comment()));
                        }
                        default ->
                                throw ApiException.notImplemented(
                                        "Table type "
                                                + type
                                                + " is not supported by colvenna-catalog.");
                    }
                    UUID id = UUID.randomUUID();
                    store.insertTable(
                            c,
                            new TableRecord(
                                    id,
                                    identifier.getDatabaseName(),
                                    identifier.getTableName(),
                                    path,
                                    external,
                                    type,
                                    schemaId,
                                    stored,
                                    TableState.ACTIVE,
                                    null,
                                    options.getOrDefault(Catalog.OWNER_PROP, ctx.who()),
                                    now,
                                    ctx.who(),
                                    now,
                                    ctx.who()));
                    store.insertBranch(
                            c,
                            new BranchRecord(id, MAIN, null, null, null, 0, 0, 0, 0),
                            now,
                            ctx.who());
                    if (Records.isFileStore(type) && external) {
                        // An external table may already hold snapshots.
                        TableRecord inserted =
                                store.tableById(c, id).orElseThrow(IllegalStateException::new);
                        commits.resetPointerFromFiles(c, inserted, MAIN, ctx.who());
                    }
                    return null;
                });
    }

    /**
     * Registers an existing Paimon table directory, for example a table created by a Paimon JDBC
     * catalog. The catalog takes ownership: the table is managed and its head comes from the files.
     */
    public void registerTable(RequestContext ctx, String database, RegisterTableRequest request)
            throws Exception {
        Identifier identifier = request.getIdentifier();
        Names.validate(identifier);
        if (!identifier.getDatabaseName().equals(database)) {
            throw ApiException.badRequest("Register path database does not match " + identifier);
        }
        String path = PathRules.normalize(request.getPath());
        if (!paths.belowWarehouse(path)) {
            throw ApiException.badRequest(
                    "Only tables inside the warehouse "
                            + warehouseRoot
                            + " can be registered; create an external table for "
                            + path
                            + ".");
        }
        paths.checkClientLocations(
                withoutPath(
                                files.latestSchema(path, Identifier.DEFAULT_MAIN_BRANCH)
                                        .map(TableSchema::toSchema)
                                        .orElseThrow(
                                                () ->
                                                        ApiException.badRequest(
                                                                "No Paimon schema under " + path)))
                        .options());
        TableSchema schema =
                files.latestSchema(path, MAIN)
                        .orElseThrow(
                                () -> ApiException.badRequest("No Paimon schema under " + path));
        long now = clock.millis();
        db.inTransaction(
                c -> {
                    requireDatabase(c, database, LockMode.FOR_SHARE);
                    store.claimTableName(c, database, identifier.getTableName());
                    if (store.table(c, database, identifier.getTableName(), false).isPresent()) {
                        throw tableExists(identifier);
                    }
                    store.claimPath(c, path);
                    rejectOverlap(c, path);
                    UUID id = UUID.randomUUID();
                    TableRecord table =
                            new TableRecord(
                                    id,
                                    database,
                                    identifier.getTableName(),
                                    path,
                                    false,
                                    TableType.TABLE,
                                    schema.id(),
                                    withoutPath(schema.toSchema()),
                                    TableState.ACTIVE,
                                    null,
                                    schema.options().getOrDefault(Catalog.OWNER_PROP, ctx.who()),
                                    now,
                                    ctx.who(),
                                    now,
                                    ctx.who());
                    store.insertTable(c, table);
                    store.insertBranch(c, commits.pointerFromFiles(table, MAIN), now, ctx.who());
                    return null;
                });
    }

    public void alterTable(RequestContext ctx, Identifier identifier, List<SchemaChange> changes)
            throws Exception {
        for (SchemaChange change : changes) {
            if (change instanceof SchemaChange.SetOption set
                    && PathRules.CLIENT_LOCATION_OPTIONS.contains(set.key())) {
                if (set.key().equals(CoreOptions.PATH.key())) {
                    throw ApiException.badRequest("The table path cannot be changed.");
                }
                paths.checkClientLocations(Map.of(set.key(), set.value()));
            }
        }
        String branch = branchOf(identifier);
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    TableSchema next;
                    if (table.fileStore()) {
                        SchemaManager schemas = files.schemaManager(table.path(), branch);
                        next = schemas.commitChanges(changes);
                    } else {
                        TableSchema current = TableSchema.create(table.schemaId(), table.schema());
                        next =
                                SchemaManager.generateTableSchema(
                                        current,
                                        changes,
                                        new LazyField<>(() -> false),
                                        new LazyField<>(() -> identifier));
                    }
                    if (BranchManager.isMainBranch(branch)) {
                        store.updateTableSchema(
                                c,
                                table.id(),
                                next.id(),
                                withoutPath(next.toSchema()),
                                clock.millis(),
                                ctx.who());
                    }
                    return null;
                });
    }

    public void rollbackSchema(RequestContext ctx, Identifier identifier, long schemaId)
            throws Exception {
        String branch = branchOf(identifier);
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    FileStoreTable fileTable = files.table(table.path(), branch);
                    try {
                        fileTable
                                .schemaManager()
                                .rollbackTo(
                                        schemaId,
                                        fileTable.snapshotManager(),
                                        fileTable.tagManager(),
                                        fileTable.changelogManager());
                    } catch (Exception e) {
                        throw new ApiException(500, e.getMessage(), e);
                    }
                    TableSchema latest =
                            files.latestSchema(table.path(), branch)
                                    .orElseThrow(IllegalStateException::new);
                    if (BranchManager.isMainBranch(branch)) {
                        store.updateTableSchema(
                                c,
                                table.id(),
                                latest.id(),
                                withoutPath(latest.toSchema()),
                                clock.millis(),
                                ctx.who());
                    }
                    return null;
                });
    }

    /** Renames in metadata only; the table directory keeps its path. */
    public void renameTable(RequestContext ctx, RenameTableRequest request) throws Exception {
        Identifier from = request.getSource();
        Identifier to = request.getDestination();
        Names.validate(from);
        Names.validate(to);
        rejectBranchOrSystem(from, "rename");
        rejectBranchOrSystem(to, "rename");
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, from);
                    requireDatabase(c, to.getDatabaseName(), LockMode.NONE);
                    store.claimTableName(c, to.getDatabaseName(), to.getTableName());
                    if (store.table(c, to.getDatabaseName(), to.getTableName(), false)
                            .isPresent()) {
                        throw tableExists(to);
                    }
                    store.renameTable(
                            c,
                            table.id(),
                            to.getDatabaseName(),
                            to.getTableName(),
                            clock.millis(),
                            ctx.who());
                    return null;
                });
    }

    /**
     * Drops a table: in one transaction the table leaves the catalog and, if managed, its root is
     * recorded for purging (#69), so every client sees it gone at once. Inline (the EKS form) the
     * request then deletes the files itself; deferred (the serverless form) it touches no file and
     * the purge function's sweeps do. A purge that fails keeps its record for the next sweep; the
     * drop has happened either way.
     */
    public void dropTable(RequestContext ctx, Identifier identifier) throws Exception {
        rejectBranchOrSystem(identifier, "drop");
        boolean inline = dropPurge == TablePurges.Mode.INLINE;
        Optional<TablePurges.Pending> purge =
                db.inTransaction(
                        c -> {
                            TableRecord locked = lockTable(c, identifier);
                            // The pointers before the DELETE, whose cascade would otherwise wait
                            // on a commit that in turn waits on this row for its foreign keys.
                            store.lockBranches(c, locked.id());
                            store.deleteTable(c, locked.id());
                            // Client-chosen locations are never deleted.
                            return locked.external()
                                    ? Optional.empty()
                                    : Optional.of(purges.record(c, locked, ctx, inline));
                        });
        if (purge.isPresent() && inline) {
            purges.purge(purge.get());
        }
    }

    /**
     * Re-reads every branch head and the schema of a table from its files, and lifts a fence
     * (admin). For after an operator stopped a foreign writer, or restored the metadata database
     * from a backup older than the files: the snapshot files are what readers already see, so they
     * are what the catalog publishes. Branches only the files know get a pointer; pointers whose
     * branch directory is gone are removed.
     */
    public Map<String, Object> reconcileTable(RequestContext ctx, Identifier identifier)
            throws Exception {
        rejectBranchOrSystem(identifier, "reconcile");
        long now = clock.millis();
        return db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    if (!table.fileStore()) {
                        throw ApiException.badRequest(
                                "Only Paimon file tables have snapshot files to reconcile.");
                    }
                    // Before anything else reads the files: a table whose files are gone (dropped
                    // after a metadata restore's point) has nothing to reconcile from.
                    TableSchema schema =
                            files.latestSchema(table.path(), MAIN)
                                    .orElseThrow(
                                            () ->
                                                    ApiException.badRequest(
                                                            "No schema file in "
                                                                    + table.path()
                                                                    + ": nothing to reconcile"
                                                                    + " from. If the table's"
                                                                    + " files are gone,"
                                                                    + " unregister it."));
                    store.lockBranches(c, table.id());
                    Map<String, Object> out = new LinkedHashMap<>();
                    out.put("table", table.fullName());
                    out.put("stateBefore", table.state().name());
                    Set<String> onFiles = new TreeSet<>();
                    onFiles.add(MAIN);
                    onFiles.addAll(files.table(table.path(), MAIN).branchManager().branches());
                    Set<String> inCatalog = new TreeSet<>(store.branchNames(c, table.id()));
                    List<Map<String, Object>> branches = new ArrayList<>();
                    for (String branch : onFiles) {
                        Map<String, Object> b = new LinkedHashMap<>();
                        b.put("branch", branch);
                        b.put(
                                "before",
                                store.branch(c, table.id(), branch, LockMode.NONE)
                                        .map(BranchRecord::snapshotId)
                                        .orElse(null));
                        if (inCatalog.contains(branch)) {
                            commits.resetPointerFromFiles(c, table, branch, ctx.who());
                        } else {
                            store.insertBranch(
                                    c, commits.pointerFromFiles(table, branch), now, ctx.who());
                        }
                        b.put(
                                "after",
                                store.branch(c, table.id(), branch, LockMode.NONE)
                                        .map(BranchRecord::snapshotId)
                                        .orElse(null));
                        branches.add(b);
                    }
                    for (String gone : inCatalog) {
                        if (!onFiles.contains(gone)) {
                            store.deleteBranch(c, table.id(), gone);
                            branches.add(Map.of("branch", gone, "removed", true));
                        }
                    }
                    if (schema.id() != table.schemaId()) {
                        store.updateTableSchema(
                                c,
                                table.id(),
                                schema.id(),
                                withoutPath(schema.toSchema()),
                                now,
                                ctx.who());
                    }
                    store.setTableState(c, table.id(), TableState.ACTIVE, null);
                    out.put("schemaIdBefore", table.schemaId());
                    out.put("schemaIdAfter", schema.id());
                    out.put("branches", branches);
                    return out;
                });
    }

    /**
     * Removes a table from the catalog and keeps its files (admin). The way back from the catalog
     * to another one: the directory can be registered elsewhere, or here again.
     */
    public void unregisterTable(RequestContext ctx, Identifier identifier) throws Exception {
        rejectBranchOrSystem(identifier, "unregister");
        db.inTransaction(
                c -> {
                    TableRecord locked = lockTable(c, identifier);
                    store.lockBranches(c, locked.id());
                    store.deleteTable(c, locked.id());
                    return null;
                });
    }

    /**
     * Replaces the schema of a Paimon table in place: the visible data is truncated by a commit and
     * a new schema follows, so time travel still reads old snapshots with their old schema. Other
     * table types, or a change of table type, are dropped and recreated. Partition keys cannot
     * change.
     */
    public void replaceTable(RequestContext ctx, Identifier identifier, Schema newSchema)
            throws Exception {
        paths.checkClientLocations(newSchema.options());
        TableType newType = Options.fromMap(newSchema.options()).get(CoreOptions.TYPE);
        TableRecord existing = requireTable(identifier);
        if (!existing.fileStore() || newType != TableType.TABLE) {
            // As Paimon's own catalogs do: drop, then create. Not atomic; a failed create leaves
            // the name free and the caller sees the error.
            dropTable(ctx, identifier);
            createTable(
                    ctx,
                    identifier.getDatabaseName(),
                    new CreateTableRequest(identifier, newSchema));
            return;
        }
        if (!existing.schema().partitionKeys().equals(newSchema.partitionKeys())) {
            throw ApiException.notImplemented(
                    "replaceTable does not support changing partition keys (old="
                            + existing.schema().partitionKeys()
                            + ", new="
                            + newSchema.partitionKeys()
                            + "). Drop and re-create the table instead.");
        }
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    commits.lockPointer(c, table, Identifier.DEFAULT_MAIN_BRANCH);
                    FileStoreTable fileTable =
                            files.table(table.path(), Identifier.DEFAULT_MAIN_BRANCH);
                    try (var commit = fileTable.newCommit("replace-table-" + UUID.randomUUID())) {
                        commit.truncateTable();
                    }
                    SchemaManager schemas =
                            files.schemaManager(table.path(), Identifier.DEFAULT_MAIN_BRANCH);
                    TableSchema staged;
                    do {
                        TableSchema latest =
                                schemas.latestOrThrow("Cannot replace: schema chain is empty.");
                        staged = TableSchema.create(latest.id() + 1, withoutPath(newSchema));
                    } while (!schemas.commit(staged));
                    store.updateTableSchema(
                            c,
                            table.id(),
                            staged.id(),
                            withoutPath(staged.toSchema()),
                            clock.millis(),
                            ctx.who());
                    store.deleteBranchPartitions(c, table.id(), Identifier.DEFAULT_MAIN_BRANCH);
                    commits.resetPointerFromFiles(
                            c, table, Identifier.DEFAULT_MAIN_BRANCH, ctx.who());
                    return null;
                });
    }

    private TableSchema createSchemaFiles(String path, Schema schema, boolean external)
            throws Exception {
        Schema toCreate = withoutPath(schema);
        SchemaManager schemas = files.schemaManager(path, MAIN);
        try {
            return schemas.createTable(toCreate, external);
        } catch (IllegalStateException e) {
            throw ApiException.conflict(ErrorResponse.RESOURCE_TYPE_TABLE, path, e.getMessage());
        } catch (ApiException e) {
            throw e;
        } catch (RuntimeException e) {
            // Paimon rejects a schema that disagrees with the files already at an external path;
            // any other failure (I/O, for instance) keeps its own status.
            if (e.getMessage() != null && e.getMessage().startsWith("New schema")) {
                throw ApiException.badRequest(e.getMessage());
            }
            throw e;
        }
    }

    /**
     * Picks the conventional {@code <warehouse>/<db>.db/<table>} root, or a suffixed sibling when a
     * renamed table or leftover files already occupy it.
     */
    private String claimManagedPath(Connection c, Identifier identifier) throws Exception {
        String base =
                databaseDirectory(identifier.getDatabaseName()) + "/" + identifier.getTableName();
        String candidate = base;
        for (int attempt = 0; attempt < 10; attempt++) {
            store.claimPath(c, candidate);
            // A dropped table's root stays taken until its files are purged.
            if (store.tableOverlapping(c, candidate).isEmpty()
                    && purges.overlapping(c, candidate).isEmpty()
                    && !files.exists(candidate)) {
                return candidate;
            }
            candidate = base + "-" + UUID.randomUUID().toString().substring(0, 8);
        }
        throw ApiException.conflict(
                ErrorResponse.RESOURCE_TYPE_TABLE,
                identifier.getTableName(),
                "No free path for " + identifier + " under " + base + "; another table covers it.");
    }

    private static void rejectBranchOrSystem(Identifier identifier, String operation) {
        if (identifier.getBranchName() != null || identifier.isSystemTable()) {
            throw ApiException.badRequest(
                    "Cannot "
                            + operation
                            + " "
                            + identifier.getObjectName()
                            + "; name the table itself.");
        }
    }

    private void rejectOverlap(Connection c, String path) throws Exception {
        Optional<String> other = store.tableOverlapping(c, path);
        if (other.isPresent()) {
            throw ApiException.badRequest(
                    "Path " + path + " overlaps the files of table " + other.get() + ".");
        }
        Optional<String> dropped = purges.overlapping(c, path);
        if (dropped.isPresent()) {
            throw ApiException.badRequest(
                    "Path "
                            + path
                            + " overlaps the files of the dropped table "
                            + dropped.get()
                            + ", which are still to be deleted.");
        }
    }

    TableRecord requireTable(Identifier identifier) throws Exception {
        return db.inTransaction(c -> requireTable(c, identifier, false));
    }

    TableRecord lockTable(Connection c, Identifier identifier) throws Exception {
        return requireTable(c, identifier, true);
    }

    /**
     * Locks the table and returns its path, failing if the name now belongs to another table: one
     * dropped and recreated since {@code seen} was read.
     */
    private String lockSame(Connection c, Identifier identifier, TableRecord seen)
            throws Exception {
        TableRecord locked = lockTable(c, identifier);
        if (!locked.id().equals(seen.id())) {
            throw ApiException.notFound(
                    ErrorResponse.RESOURCE_TYPE_TABLE,
                    identifier.getObjectName(),
                    "Table " + identifier.getFullName() + " was replaced; retry.");
        }
        return locked.path();
    }

    private TableRecord requireTable(Connection c, Identifier identifier, boolean forUpdate)
            throws Exception {
        Optional<TableRecord> table =
                store.table(c, identifier.getDatabaseName(), identifier.getTableName(), forUpdate);
        if (table.isPresent()) {
            return table.get();
        }
        boolean databaseExists =
                store.database(c, identifier.getDatabaseName(), LockMode.NONE).isPresent();
        return table.orElseThrow(
                () -> {
                    if (!databaseExists) {
                        return ApiException.notFound(
                                ErrorResponse.RESOURCE_TYPE_DATABASE,
                                identifier.getDatabaseName(),
                                "Database " + identifier.getDatabaseName() + " does not exist.");
                    }
                    return tableMissing(identifier);
                });
    }

    /** Fails with 404 unless {@code identifier} names an existing table (and branch). */
    public TableRecord requireTableAndBranch(Identifier identifier) throws Exception {
        TableRecord table = requireTable(identifier);
        String branch = branchOf(identifier);
        if (!BranchManager.isMainBranch(branch)
                && files.latestSchema(table.path(), branch).isEmpty()) {
            throw tableMissing(identifier);
        }
        return table;
    }

    private GetTableResponse tableResponse(
            TableRecord table, String objectName, long schemaId, Schema schema) {
        return new GetTableResponse(
                table.id().toString(),
                table.database(),
                objectName,
                table.path(),
                table.external(),
                schemaId,
                schema,
                table.owner(),
                table.createdAt(),
                table.createdBy(),
                table.updatedAt(),
                table.updatedBy());
    }

    // ================================================================ snapshots

    public TableSnapshot latestSnapshot(RequestContext ctx, Identifier identifier)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        BranchRecord pointer =
                commits.latest(ctx, table, branchOf(identifier))
                        .filter(p -> p.snapshotId() != null)
                        .orElseThrow(() -> noSnapshot(identifier));
        return CommitService.toTableSnapshot(pointer);
    }

    public Snapshot snapshotVersion(RequestContext ctx, Identifier identifier, String version)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        String branch = branchOf(identifier);
        FileStoreTable fileTable = files.table(table.path(), branch);
        SnapshotManager snapshots = fileTable.snapshotManager();
        Snapshot snapshot = null;
        try {
            if ("EARLIEST".equals(version)) {
                snapshot = snapshots.earliestSnapshot();
            } else if ("LATEST".equals(version)) {
                BranchRecord pointer = commits.latest(ctx, table, branch).orElse(null);
                if (pointer != null && pointer.snapshotJson() != null) {
                    snapshot = Snapshot.fromJson(pointer.snapshotJson());
                }
            } else {
                try {
                    snapshot = snapshots.tryGetSnapshot(Long.parseLong(version));
                } catch (NumberFormatException e) {
                    Optional<Tag> tag = fileTable.tagManager().get(version);
                    if (tag.isPresent()) {
                        snapshot = tag.get().trimToSnapshot();
                    }
                }
            }
        } catch (FileNotFoundException e) {
            snapshot = null;
        }
        if (snapshot == null) {
            throw noSnapshot(identifier);
        }
        return snapshot;
    }

    public PagedList<Snapshot> listSnapshots(
            Identifier identifier, @Nullable String maxResults, @Nullable String pageToken)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        int size = paging.pageSize(maxResults);
        long after = pageToken == null ? Long.MIN_VALUE : Long.parseLong(pageToken);
        List<Snapshot> page = new ArrayList<>();
        var iterator =
                files.table(table.path(), branchOf(identifier)).snapshotManager().snapshots();
        while (iterator.hasNext() && page.size() < size) {
            Snapshot snapshot = iterator.next();
            if (snapshot.id() > after) {
                page.add(snapshot);
            }
        }
        String next = page.size() >= size ? String.valueOf(page.get(page.size() - 1).id()) : null;
        return new PagedList<>(page, next);
    }

    // ================================================================ consumers

    public List<ConsumerInfo> listConsumers(Identifier identifier) throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        Path root = new Path(table.path());
        ConsumerManager consumers =
                new ConsumerManager(files.tableIO(table.path()), root, branchOf(identifier));
        return consumers.consumers().entrySet().stream()
                .sorted(Map.Entry.comparingByKey())
                .map(e -> new ConsumerInfo(e.getKey(), e.getValue()))
                .collect(Collectors.toList());
    }

    public void resetConsumer(Identifier identifier, ResetConsumerRequest request)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        String branch = branchOf(identifier);
        Path root = new Path(table.path());
        Names.validate("consumer", request.consumerId());
        ConsumerManager consumers = new ConsumerManager(files.tableIO(table.path()), root, branch);
        if (request.nextSnapshotId() != null) {
            files.table(table.path(), branch).snapshotManager().snapshot(request.nextSnapshotId());
            consumers.resetConsumer(request.consumerId(), new Consumer(request.nextSnapshotId()));
        } else {
            consumers.deleteConsumer(request.consumerId());
        }
    }

    // ================================================================ tags

    public Page<String> listTags(
            Identifier identifier,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String prefix)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        int size = paging.pageSize(maxResults);
        List<String> tags =
                files.table(table.path(), branchOf(identifier)).tagManager().allTagNames().stream()
                        .filter(
                                t ->
                                        prefix == null
                                                || prefix.isEmpty()
                                                || Paging.matches(t, prefix))
                        .filter(t -> pageToken == null || t.compareTo(pageToken) > 0)
                        .sorted()
                        .limit(size)
                        .collect(Collectors.toList());
        return Paging.page(tags, size, t -> t);
    }

    public GetTagResponse getTag(Identifier identifier, String tagName) throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        Tag tag =
                files.table(table.path(), branchOf(identifier))
                        .tagManager()
                        .get(tagName)
                        .orElseThrow(() -> tagMissing(identifier, tagName));
        Long createTime =
                tag.getTagCreateTime() == null
                        ? null
                        : tag.getTagCreateTime()
                                .atZone(ZoneId.systemDefault())
                                .toInstant()
                                .toEpochMilli();
        String retained =
                tag.getTagTimeRetained() == null ? null : tag.getTagTimeRetained().toString();
        return new GetTagResponse(tagName, tag.trimToSnapshot(), createTime, retained);
    }

    public void createTag(RequestContext ctx, Identifier identifier, CreateTagRequest request)
            throws Exception {
        Names.validate("tag", request.tagName());
        TableRecord table = requireTableAndBranch(identifier);
        String branch = branchOf(identifier);
        Duration retained = null;
        if (request.timeRetained() != null) {
            try {
                retained = TimeUtils.parseDuration(request.timeRetained());
            } catch (Exception e) {
                throw ApiException.badRequest(
                        "Invalid timeRetained format: " + request.timeRetained());
            }
        }
        Long snapshotId = request.snapshotId();
        if (snapshotId == null) {
            Snapshot latest = latestSnapshotOrNull(ctx, table, branch);
            snapshotId = latest == null ? null : latest.id();
        }
        Long tagged = snapshotId;
        Duration timeRetained = retained;
        // Every check runs under the table lock, which rollback and branch creation also take.
        db.inTransaction(
                c -> {
                    FileStoreTable fileTable = files.table(lockSame(c, identifier, table), branch);
                    if (tagged == null || !fileTable.snapshotManager().snapshotExists(tagged)) {
                        throw ApiException.notFound(
                                ErrorResponse.RESOURCE_TYPE_SNAPSHOT,
                                String.valueOf(request.snapshotId()),
                                String.format(
                                        "Snapshot %s in table %s doesn't exist.",
                                        request.snapshotId(), identifier.getFullName()));
                    }
                    if (fileTable.tagManager().tagExists(request.tagName())) {
                        throw ApiException.conflict(
                                ErrorResponse.RESOURCE_TYPE_TAG,
                                request.tagName(),
                                "Tag " + request.tagName() + " already exists.");
                    }
                    fileTable.createTag(request.tagName(), tagged, timeRetained);
                    return null;
                });
    }

    /**
     * Deletes a tag. Its file goes in the request; the data and manifests only it referenced are
     * left to a purge, recorded first (#93), as {@link #dropTable} leaves a table's files. Paimon's
     * own tag deletion deletes them in the request, reading the tag's neighbours to tell which; the
     * purge reads every root of every branch, so a branch made from the tag keeps its files. A tag
     * whose snapshot is still there references nothing that snapshot does not: only its file goes,
     * as with Paimon.
     */
    public void deleteTag(RequestContext ctx, Identifier identifier, String tagName)
            throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        String branch = branchOf(identifier);
        Optional<FilePurges.Pending> recorded =
                db.inTransaction(
                        c -> {
                            FileStoreTable fileTable =
                                    files.table(lockSame(c, identifier, table), branch);
                            if (!fileTable.tagManager().tagExists(tagName)) {
                                throw tagMissing(identifier, tagName);
                            }
                            Optional<Roots.RootFile> file =
                                    files.roots(table.path(), branch, Roots.Kind.TAG).stream()
                                            .filter(r -> r.tagName().equals(tagName))
                                            .findFirst();
                            Map<Roots.RootFile, String> content =
                                    file.isEmpty()
                                            ? Map.of()
                                            : files.read(table.path(), List.of(file.get()));
                            if (content.isEmpty()) {
                                throw tagMissing(identifier, tagName);
                            }
                            Roots.RootFile root = file.get();
                            Snapshot tagged = Tag.fromJson(content.get(root)).trimToSnapshot();
                            SnapshotManager snapshots = fileTable.snapshotManager();
                            boolean held =
                                    snapshots.snapshotExists(tagged.id())
                                            && snapshots.snapshot(tagged.id()).equals(tagged);
                            Optional<FilePurges.Capture> capture =
                                    held
                                            ? Optional.empty()
                                            : Optional.of(
                                                    filePurges.capture(
                                                            table,
                                                            branch,
                                                            FilePurges.Operation.DROP_TAG,
                                                            "tag:" + tagName,
                                                            ctx,
                                                            List.of(root),
                                                            content,
                                                            0));
                            files.deleteTagFile(fileTable, tagName);
                            return filePurges.applied(c, capture, inline());
                        });
        purgeInline(recorded);
    }

    @Nullable
    private Snapshot latestSnapshotOrNull(RequestContext ctx, TableRecord table, String branch)
            throws Exception {
        BranchRecord pointer = commits.latest(ctx, table, branch).orElse(null);
        return pointer == null || pointer.snapshotJson() == null
                ? null
                : Snapshot.fromJson(pointer.snapshotJson());
    }

    // ================================================================ branches

    public List<String> listBranches(Identifier identifier) throws Exception {
        TableRecord table = requireTable(identifier);
        return files.table(table.path(), MAIN).branchManager().branches();
    }

    public void createBranch(RequestContext ctx, Identifier identifier, CreateBranchRequest request)
            throws Exception {
        String branch = request.branch();
        Names.validate("branch", branch);
        if (request.fromTag() != null) {
            Names.validate("tag", request.fromTag());
        }
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    // Every lock before any file changes: a lock timeout (a retryable 503) then
                    // leaves no branch directory behind for the retry to find.
                    BranchRecord mainHead =
                            request.fromTag() == null
                                    ? commits.lockPointer(c, table, Identifier.DEFAULT_MAIN_BRANCH)
                                    : null;
                    FileStoreTable fileTable = files.table(table.path(), MAIN);
                    BranchManager branches = fileTable.branchManager();
                    if (branches.branchExists(branch)) {
                        throw ApiException.conflict(
                                ErrorResponse.RESOURCE_TYPE_BRANCH,
                                branch,
                                "Branch name '" + branch + "' already exists.");
                    }
                    if (request.fromTag() != null
                            && !fileTable.tagManager().tagExists(request.fromTag())) {
                        throw tagMissing(identifier, request.fromTag());
                    }
                    try {
                        if (mainHead != null) {
                            branches.createBranch(branch);
                            startBranchFromMainHead(table, branch, mainHead);
                        } else {
                            branches.createBranch(branch, request.fromTag());
                        }
                    } catch (IllegalArgumentException e) {
                        throw ApiException.badRequest(e.getMessage());
                    }
                    store.deleteBranch(c, table.id(), branch);
                    store.insertBranch(
                            c, commits.pointerFromFiles(table, branch), clock.millis(), ctx.who());
                    return null;
                });
    }

    /**
     * A branch created without a tag starts at main's current head, as with the upstream REST
     * server. The head snapshot is copied into the branch directory so that readers of the branch
     * find it on the file system. The branch holds no tag, so main's snapshot expiration does not
     * protect the files that this snapshot shares with main.
     */
    private void startBranchFromMainHead(TableRecord table, String branch, BranchRecord main)
            throws Exception {
        if (main.snapshotJson() == null) {
            return;
        }
        Snapshot head = Snapshot.fromJson(main.snapshotJson());
        SnapshotManager branchSnapshots = files.snapshotManager(table.path(), branch);
        files.tableIO(table.path())
                .writeFile(branchSnapshots.snapshotPath(head.id()), head.toJson(), false);
        branchSnapshots.commitLatestHint(head.id());
        branchSnapshots.commitEarliestHint(head.id());
    }

    /**
     * Deletes a branch: its directory goes in the request, which is what makes it gone; the data
     * and manifests only its snapshots, tags and changelogs referenced, which Paimon's own branch
     * deletion leaves behind for good, are left to a purge, recorded first with the branch's latest
     * schema to read its manifests by (#93). A request whose time runs out while recording answers
     * 503 having deleted nothing; sent again, it goes on where it stopped.
     */
    public void dropBranch(RequestContext ctx, Identifier identifier, String branch)
            throws Exception {
        long deadline = deadline();
        Optional<FilePurges.Pending> recorded =
                db.inTransaction(
                        c -> {
                            TableRecord table = lockTable(c, identifier);
                            // The branch's pointer before its files, which a commit to it may
                            // hold.
                            store.branch(c, table.id(), branch, LockMode.FOR_UPDATE);
                            FileStoreTable fileTable = files.table(table.path(), MAIN);
                            if (!fileTable.branchManager().branchExists(branch)) {
                                throw ApiException.notFound(
                                        ErrorResponse.RESOURCE_TYPE_BRANCH,
                                        branch,
                                        "Branch name '" + branch + "' doesn't exist.");
                            }
                            requireDroppable(fileTable, branch);
                            List<Roots.RootFile> roots =
                                    new ArrayList<>(
                                            files.roots(
                                                    table.path(),
                                                    branch,
                                                    Roots.Kind.SNAPSHOT,
                                                    Roots.Kind.TAG,
                                                    Roots.Kind.CHANGELOG));
                            long recording = System.nanoTime();
                            Optional<FilePurges.Capture> capture = Optional.empty();
                            if (!roots.isEmpty()) {
                                files.roots(table.path(), branch, Roots.Kind.SCHEMA).stream()
                                        .max(Comparator.comparingLong(Roots.RootFile::id))
                                        .ifPresent(roots::add);
                                capture =
                                        Optional.of(
                                                filePurges.capture(
                                                        table,
                                                        branch,
                                                        FilePurges.Operation.DROP_BRANCH,
                                                        "branch:" + branch,
                                                        ctx,
                                                        roots,
                                                        Map.of(),
                                                        deadline));
                            }
                            long deleting = System.nanoTime();
                            try {
                                fileTable.deleteBranch(branch);
                            } catch (IllegalArgumentException e) {
                                throw ApiException.badRequest(e.getMessage());
                            }
                            LOG.info(
                                    "Deleted branch {} of {}: {} roots recorded in {} ms, its"
                                            + " directory deleted in {} ms",
                                    branch,
                                    table.fullName(),
                                    roots.size(),
                                    (deleting - recording) / 1_000_000,
                                    (System.nanoTime() - deleting) / 1_000_000);
                            // Paimon only logs a failure to delete the directory.
                            if (fileTable.branchManager().branchExists(branch)) {
                                throw new IOException(
                                        "Branch "
                                                + branch
                                                + " of "
                                                + table.fullName()
                                                + " was not deleted: files remain in its"
                                                + " directory.");
                            }
                            store.deleteBranch(c, table.id(), branch);
                            store.deleteBranchPartitions(c, table.id(), branch);
                            return filePurges.applied(c, capture, inline());
                        });
        purgeInline(recorded);
    }

    /**
     * Paimon refuses to delete a branch the table reads as its fallback or primary branch; so does
     * this, before anything is recorded or deleted.
     */
    private static void requireDroppable(FileStoreTable table, String branch) {
        Options options = table.coreOptions().toConfiguration();
        for (var option :
                List.of(CoreOptions.SCAN_FALLBACK_BRANCH, CoreOptions.SCAN_PRIMARY_BRANCH)) {
            if (branch.equals(options.get(option))) {
                throw ApiException.badRequest(
                        String.format(
                                "Cannot delete branch '%s' because it is configured as '%s'."
                                        + " Unset '%s' first.",
                                branch, option.key(), option.key()));
            }
        }
    }

    public void fastForward(RequestContext ctx, Identifier identifier, String branch)
            throws Exception {
        db.inTransaction(
                c -> {
                    TableRecord table = lockTable(c, identifier);
                    commits.lockPointer(c, table, MAIN);
                    FileStoreTable fileTable = files.table(table.path(), MAIN);
                    if (!fileTable.branchManager().branchExists(branch)) {
                        throw ApiException.notFound(
                                ErrorResponse.RESOURCE_TYPE_BRANCH,
                                branch,
                                "Branch name '" + branch + "' doesn't exist.");
                    }
                    fileTable.fastForward(branch);
                    commits.resetPointerFromFiles(c, table, MAIN, ctx.who());
                    TableSchema latest =
                            files.latestSchema(table.path(), MAIN)
                                    .orElseThrow(IllegalStateException::new);
                    store.updateTableSchema(
                            c,
                            table.id(),
                            latest.id(),
                            withoutPath(latest.toSchema()),
                            clock.millis(),
                            ctx.who());
                    return null;
                });
    }

    // ================================================================ data tokens and auth

    /** Credentials for the files of one table, at the access level the caller's grants allow. */
    public DataTokenIssuer.Token tableToken(
            RequestContext ctx, Identifier identifier, SessionPolicy.Access access)
            throws Exception {
        TableRecord table = requireTable(identifier);
        return dataTokens.issue(ctx, table, access);
    }

    /**
     * Refuses a query that selects columns outside {@code readable}; no selection means all
     * columns. The client applies this only for tables with {@code query-auth.enabled}.
     */
    public void requireColumns(
            Identifier identifier, @Nullable List<String> select, List<String> readable)
            throws Exception {
        List<String> wanted =
                select == null || select.isEmpty()
                        ? requireTable(identifier).schema().fields().stream()
                                .map(org.apache.paimon.types.DataField::name)
                                .toList()
                        : select;
        for (String column : wanted) {
            if (!readable.contains(column)) {
                throw ApiException.forbidden(
                        ErrorResponse.RESOURCE_TYPE_TABLE,
                        identifier.getFullName(),
                        "Table "
                                + identifier.getFullName()
                                + " has no permission for column "
                                + column
                                + ".");
            }
        }
    }

    /** Table-level decision only: no row filter or column mask is enforced by the catalog. */
    public AuthTableQueryResponse authTableQuery(Identifier identifier) throws Exception {
        requireTable(identifier);
        return new AuthTableQueryResponse(List.of(), Map.of());
    }

    // ================================================================ misc helpers

    public static String branchOf(Identifier identifier) {
        String branch = identifier.getBranchName();
        if (branch != null) {
            Names.validate("branch", branch);
        }
        return branch == null
                ? Identifier.DEFAULT_MAIN_BRANCH
                : BranchManager.normalizeBranch(branch);
    }

    static Schema withoutPath(Schema schema) {
        Map<String, String> options = new HashMap<>(schema.options());
        options.remove(CoreOptions.PATH.key());
        return new Schema(
                schema.fields(),
                schema.partitionKeys(),
                schema.primaryKeys(),
                options,
                schema.comment());
    }

    private static ApiException tableExists(Identifier identifier) {
        return ApiException.conflict(
                ErrorResponse.RESOURCE_TYPE_TABLE,
                identifier.getTableName(),
                "Table " + identifier.getFullName() + " already exists.");
    }

    private static ApiException tableMissing(Identifier identifier) {
        return ApiException.notFound(
                ErrorResponse.RESOURCE_TYPE_TABLE,
                identifier.getTableName(),
                "Table " + identifier.getFullName() + " does not exist.");
    }

    private static ApiException tagMissing(Identifier identifier, String tag) {
        return ApiException.notFound(
                ErrorResponse.RESOURCE_TYPE_TAG,
                tag,
                String.format("Tag %s in table %s doesn't exist.", tag, identifier.getFullName()));
    }

    private static ApiException noSnapshot(Identifier identifier) {
        return ApiException.notFound(
                ErrorResponse.RESOURCE_TYPE_SNAPSHOT, identifier.getDatabaseName(), "No Snapshot");
    }
}
