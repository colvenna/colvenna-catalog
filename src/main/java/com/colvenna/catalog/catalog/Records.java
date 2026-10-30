package com.colvenna.catalog.catalog;

import java.util.Map;
import java.util.UUID;
import javax.annotation.Nullable;
import org.apache.paimon.TableType;
import org.apache.paimon.schema.Schema;

/** Rows of the catalog metadata tables. */
public final class Records {

    private Records() {}

    /** Whether a table accepts commits. */
    public enum TableState {
        ACTIVE,
        /** Another writer published snapshots outside the catalog; commits are refused. */
        FENCED
    }

    /** Life cycle of a commit attempt. */
    public enum IntentState {
        PENDING,
        PUBLISHED,
        ABORTED
    }

    /** Paimon tables and materialized tables are file stores; other types are metadata only. */
    public static boolean isFileStore(TableType type) {
        return type == TableType.TABLE || type == TableType.MATERIALIZED_TABLE;
    }

    /** A registered database. */
    public record DatabaseRecord(
            String name,
            UUID id,
            String location,
            Map<String, String> options,
            @Nullable String owner,
            long createdAt,
            String createdBy,
            long updatedAt,
            String updatedBy) {}

    /**
     * A registered table. {@code schema} carries no {@code path} option; {@code path} is the table
     * root the server hands to clients.
     */
    public record TableRecord(
            UUID id,
            String database,
            String name,
            String path,
            boolean external,
            TableType tableType,
            long schemaId,
            Schema schema,
            TableState state,
            @Nullable String stateReason,
            @Nullable String owner,
            long createdAt,
            String createdBy,
            long updatedAt,
            String updatedBy) {

        public boolean fenced() {
            return state == TableState.FENCED;
        }

        /** Whether the table keeps Paimon schema and snapshot files, rather than only metadata. */
        public boolean fileStore() {
            return isFileStore(tableType);
        }

        public String fullName() {
            return database + "." + name;
        }
    }

    /** The latest published snapshot of one table branch. */
    public record BranchRecord(
            UUID tableId,
            String branch,
            @Nullable Long snapshotId,
            @Nullable String snapshotUuid,
            @Nullable String snapshotJson,
            long recordCount,
            long fileSizeInBytes,
            long fileCount,
            long lastFileCreationTime) {}

    /** A commit attempt recorded before its snapshot file is published. */
    public record CommitIntent(
            long id,
            UUID tableId,
            String branch,
            long snapshotId,
            @Nullable String snapshotUuid,
            IntentState state,
            java.time.Instant createdAt,
            @Nullable String statisticsJson) {}
}
