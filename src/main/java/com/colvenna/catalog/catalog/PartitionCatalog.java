package com.colvenna.catalog.catalog;

import com.colvenna.catalog.catalog.CatalogStore.PartitionRow;
import com.colvenna.catalog.catalog.Paging.Page;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.http.ApiException;
import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import javax.annotation.Nullable;
import org.apache.paimon.CoreOptions;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.catalog.TableQueryAuthResult;
import org.apache.paimon.data.GenericRow;
import org.apache.paimon.partition.Partition;
import org.apache.paimon.partition.PartitionUtils;
import org.apache.paimon.predicate.Predicate;
import org.apache.paimon.rest.requests.CreatePartitionsRequest;
import org.apache.paimon.rest.requests.DropPartitionsRequest;
import org.apache.paimon.rest.requests.ListPartitionsByFilterRequest;
import org.apache.paimon.rest.responses.CreatePartitionsResponse;
import org.apache.paimon.rest.responses.DropPartitionsResponse;
import org.apache.paimon.rest.responses.ErrorResponse;
import org.apache.paimon.schema.TableSchema;
import org.apache.paimon.types.DataField;
import org.apache.paimon.types.RowType;
import org.apache.paimon.utils.InternalRowPartitionComputer;
import org.apache.paimon.utils.JsonSerdeUtil;

/**
 * Partitions of tables whose partitions live in the catalog ({@code
 * metastore.partitioned-table=true}), per branch. Other tables answer 501 and the Paimon client
 * lists their partitions from the files instead.
 */
public final class PartitionCatalog {

    private final Db db;
    private final CatalogStore store;
    private final CatalogService tables;
    private final Paging paging;
    private final Clock clock;

    public PartitionCatalog(
            Db db, CatalogStore store, CatalogService tables, Paging paging, Clock clock) {
        this.db = db;
        this.store = store;
        this.tables = tables;
        this.paging = paging;
        this.clock = clock;
    }

    public Page<Partition> listPartitions(
            Identifier identifier,
            @Nullable String maxResults,
            @Nullable String pageToken,
            @Nullable String pattern)
            throws Exception {
        TableRecord table = requireMetastorePartitioned(identifier);
        int size = paging.pageSize(maxResults);
        String like = Paging.likePattern(pattern);
        List<PartitionRow> rows =
                db.inTransaction(
                        c ->
                                store.partitions(
                                        c,
                                        table.id(),
                                        branchOf(identifier),
                                        like,
                                        pageToken,
                                        size));
        Page<PartitionRow> page = Paging.page(rows, size, PartitionRow::specName);
        return new Page<>(
                page.elements().stream()
                        .map(PartitionCatalog::toPartition)
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    public List<Partition> listPartitionsByNames(
            Identifier identifier, List<Map<String, String>> specs) throws Exception {
        TableRecord table = requireMetastorePartitioned(identifier);
        List<String> names =
                specs.stream().map(PartitionUtils::buildPartitionName).collect(Collectors.toList());
        Map<String, PartitionRow> found =
                db.inTransaction(
                        c -> store.partitionsByName(c, table.id(), branchOf(identifier), names));
        return names.stream()
                .filter(found::containsKey)
                .map(n -> toPartition(found.get(n)))
                .collect(Collectors.toList());
    }

    /**
     * Lists partitions matching a predicate. A predicate the server cannot restore or evaluate
     * matches, so the result is a superset of the matching partitions and never misses one.
     */
    public Page<Partition> listPartitionsByFilter(
            Identifier identifier, ListPartitionsByFilterRequest request) throws Exception {
        if (request.getFilter() == null || request.getFilter().isEmpty()) {
            throw ApiException.badRequest("filter is required");
        }
        TableRecord table = requireMetastorePartitioned(identifier);
        RowType partitionType =
                TableSchema.create(table.schemaId(), table.schema()).logicalPartitionType();
        String defaultName = new CoreOptions(table.schema().options()).partitionDefaultName();
        Predicate predicate;
        try {
            predicate =
                    TableQueryAuthResult.remapPredicate(
                            JsonSerdeUtil.fromJson(request.getFilter(), Predicate.class),
                            partitionType);
        } catch (Exception e) {
            predicate = null;
        }
        int size =
                paging.pageSize(
                        request.getMaxResults() == null
                                ? null
                                : request.getMaxResults().toString());
        String like = Paging.likePattern(request.getPartitionNamePattern());
        List<PartitionRow> all =
                db.inTransaction(
                        c ->
                                store.partitions(
                                        c,
                                        table.id(),
                                        branchOf(identifier),
                                        like,
                                        request.getPageToken(),
                                        Integer.MAX_VALUE));
        List<PartitionRow> matching = new ArrayList<>();
        for (PartitionRow row : all) {
            if (matching.size() >= size) {
                break;
            }
            if (matchesPredicate(predicate, row.spec(), partitionType, defaultName)) {
                matching.add(row);
            }
        }
        Page<PartitionRow> page = Paging.page(matching, size, PartitionRow::specName);
        return new Page<>(
                page.elements().stream()
                        .map(PartitionCatalog::toPartition)
                        .collect(Collectors.toList()),
                page.nextPageToken());
    }

    public CreatePartitionsResponse createPartitions(
            RequestContext ctx, Identifier identifier, CreatePartitionsRequest request)
            throws Exception {
        TableRecord table = requireMetastorePartitioned(identifier);
        long now = clock.millis();
        return db.inTransaction(
                c -> {
                    lockTable(c, identifier);
                    List<Map<String, String>> specs = request.getPartitionSpecs();
                    Set<String> existing =
                            store.partitionsByName(
                                            c,
                                            table.id(),
                                            branchOf(identifier),
                                            specs.stream()
                                                    .map(PartitionUtils::buildPartitionName)
                                                    .collect(Collectors.toList()))
                                    .keySet();
                    if (!request.ignoreIfExists()) {
                        Set<String> seen = new java.util.HashSet<>(existing);
                        for (Map<String, String> spec : specs) {
                            String name = PartitionUtils.buildPartitionName(spec);
                            if (!seen.add(name)) {
                                throw ApiException.conflict(
                                        ErrorResponse.RESOURCE_TYPE_PARTITION,
                                        name,
                                        String.format("Partition %s already exists.", name));
                            }
                        }
                    }
                    List<Map<String, String>> created = new ArrayList<>();
                    List<Map<String, String>> existed = new ArrayList<>();
                    for (Map<String, String> spec : specs) {
                        String name = PartitionUtils.buildPartitionName(spec);
                        if (store.insertEmptyPartition(
                                c, table.id(), branchOf(identifier), name, spec, now, ctx.who())) {
                            created.add(spec);
                        } else {
                            existed.add(spec);
                        }
                    }
                    return new CreatePartitionsResponse(created, existed);
                });
    }

    public DropPartitionsResponse dropPartitions(
            Identifier identifier, DropPartitionsRequest request) throws Exception {
        TableRecord table = requireMetastorePartitioned(identifier);
        return db.inTransaction(
                c -> {
                    lockTable(c, identifier);
                    List<Map<String, String>> specs = request.getPartitionSpecs();
                    Map<String, PartitionRow> existing =
                            store.partitionsByName(
                                    c,
                                    table.id(),
                                    branchOf(identifier),
                                    specs.stream()
                                            .map(PartitionUtils::buildPartitionName)
                                            .collect(Collectors.toList()));
                    List<Map<String, String>> missing = new ArrayList<>();
                    for (Map<String, String> spec : specs) {
                        if (!existing.containsKey(PartitionUtils.buildPartitionName(spec))) {
                            missing.add(spec);
                        }
                    }
                    if (!request.ignoreIfNotExists() && !missing.isEmpty()) {
                        List<String> names =
                                missing.stream()
                                        .map(PartitionUtils::buildPartitionName)
                                        .collect(Collectors.toList());
                        throw ApiException.notFound(
                                ErrorResponse.RESOURCE_TYPE_PARTITION,
                                names.get(0),
                                String.format("Partitions %s do not exist.", names));
                    }
                    List<Map<String, String>> dropped = new ArrayList<>();
                    for (Map<String, String> spec : specs) {
                        if (store.deletePartition(
                                c,
                                table.id(),
                                branchOf(identifier),
                                PartitionUtils.buildPartitionName(spec))) {
                            dropped.add(spec);
                        }
                    }
                    return new DropPartitionsResponse(dropped, missing);
                });
    }

    public void markDonePartitions(
            RequestContext ctx, Identifier identifier, List<Map<String, String>> specs)
            throws Exception {
        TableRecord table = requireMetastorePartitioned(identifier);
        db.inTransaction(
                c -> {
                    for (Map<String, String> spec : specs) {
                        store.markPartitionDone(
                                c,
                                table.id(),
                                branchOf(identifier),
                                PartitionUtils.buildPartitionName(spec),
                                clock.millis(),
                                ctx.who());
                    }
                    return null;
                });
    }

    /** Partition endpoints apply to tables whose partitions live in the catalog; others get 501. */
    private TableRecord requireMetastorePartitioned(Identifier identifier) throws Exception {
        TableRecord table = requireTableAndBranch(identifier);
        if (!CoreOptions.fromMap(table.schema().options()).partitionedTableInMetastore()) {
            throw ApiException.notImplemented(
                    "Partitions of " + table.fullName() + " are not managed by the catalog.");
        }
        return table;
    }

    private static Partition toPartition(PartitionRow row) {
        return new Partition(
                new LinkedHashMap<>(row.spec()),
                row.recordCount(),
                row.fileSizeInBytes(),
                row.fileCount(),
                row.lastFileCreationTime(),
                row.totalBuckets(),
                row.done(),
                row.createdAt(),
                row.createdBy(),
                row.updatedAt(),
                row.updatedBy(),
                new HashMap<>(row.options()));
    }

    private static boolean matchesPredicate(
            @Nullable Predicate predicate,
            Map<String, String> spec,
            @Nullable RowType partitionType,
            String defaultPartName) {
        if (predicate == null || partitionType == null) {
            return true;
        }
        try {
            LinkedHashMap<String, String> ordered = new LinkedHashMap<>();
            for (DataField field : partitionType.getFields()) {
                ordered.put(field.name(), spec.get(field.name()));
            }
            GenericRow row =
                    InternalRowPartitionComputer.convertSpecToInternalRow(
                            ordered, partitionType, defaultPartName);
            return predicate.test(row);
        } catch (Exception e) {
            return true;
        }
    }

    private static String branchOf(Identifier identifier) {
        return CatalogService.branchOf(identifier);
    }

    private TableRecord requireTableAndBranch(Identifier identifier) throws Exception {
        return tables.requireTableAndBranch(identifier);
    }

    private TableRecord lockTable(Connection c, Identifier identifier) throws Exception {
        return tables.lockTable(c, identifier);
    }
}
