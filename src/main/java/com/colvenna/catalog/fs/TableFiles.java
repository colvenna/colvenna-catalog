package com.colvenna.catalog.fs;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.DirectoryNotEmptyException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import javax.annotation.Nullable;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.FileStatus;
import org.apache.paimon.fs.Path;
import org.apache.paimon.options.Options;
import org.apache.paimon.schema.SchemaManager;
import org.apache.paimon.schema.TableSchema;
import org.apache.paimon.table.CatalogEnvironment;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.FileStoreTableFactory;
import org.apache.paimon.table.sink.TagCallback;
import org.apache.paimon.utils.BranchManager;
import org.apache.paimon.utils.IOUtils;
import org.apache.paimon.utils.SnapshotManager;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.services.s3.model.Delete;
import software.amazon.awssdk.services.s3.model.DeleteObjectsResponse;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.ObjectIdentifier;
import software.amazon.awssdk.services.s3.model.S3Error;
import software.amazon.awssdk.services.s3.model.S3Object;

/**
 * Server-side access to table directories in the warehouse.
 *
 * <p>The server reaches the files with its own credentials and builds tables without a catalog
 * environment, so tag, branch, rollback and schema operations act directly on the file system while
 * the caller holds the table's PostgreSQL lock. Every table operation goes through {@link
 * #tableIO}, which refuses paths outside that table's root (see {@link JailedFileIO}).
 */
public final class TableFiles {

    /**
     * How many file reads or deletions one operation runs at once (#93): rollbacks, branch
     * deletions and purges touch a file per snapshot or more, and each is a round trip to the
     * store.
     */
    public static final int PARALLELISM = 32;

    /** The threads that run them: one pool for the process, of daemon threads. */
    private static final ExecutorService IO =
            Executors.newFixedThreadPool(
                    PARALLELISM,
                    r -> {
                        Thread t = new Thread(r, "colvenna-catalog-file-io");
                        t.setDaemon(true);
                        return t;
                    });

    /** How many keys one S3 {@code DeleteObjects} request deletes at most (the API's limit). */
    static final int S3_DELETE_BATCH = 1000;

    private final CatalogContext context;
    private final Map<String, FileIO> fileIOs = new ConcurrentHashMap<>();
    @Nullable private final SnapshotPublisher.S3ClientFactory s3;

    public TableFiles(Map<String, String> fileIoOptions) {
        this(fileIoOptions, null);
    }

    /**
     * With {@code s3}, the many reads and deletions of {@link #read} and {@link #deleteAll} on S3
     * go through the S3 API itself (#93): one {@code GetObject} per file, where S3A first asks for
     * the file's status, and one {@code DeleteObjects} per thousand files, where S3A deletes them
     * one at a time and asks first. The paths are confined to the table's root as {@link
     * JailedFileIO} confines them.
     */
    public TableFiles(
            Map<String, String> fileIoOptions, @Nullable SnapshotPublisher.S3ClientFactory s3) {
        this.context = CatalogContext.create(Options.fromMap(fileIoOptions));
        this.s3 = s3;
    }

    /** Whether {@code path} is on S3 and the S3 API is to be used for it. */
    private boolean viaS3(Path path) {
        String scheme = path.toUri().getScheme();
        return s3 != null && ("s3".equals(scheme) || "s3a".equals(scheme));
    }

    private void requireInside(String tablePath, Path path) throws IOException {
        if (!insideRoot(tablePath, path)) {
            throw new IOException(
                    "Refusing to touch " + path + ": outside the table root " + tablePath + ".");
        }
    }

    private FileIO rawFileIO(Path path) {
        String key = path.toUri().getScheme() + "://" + path.toUri().getAuthority();
        return fileIOs.computeIfAbsent(
                key,
                ignored -> {
                    try {
                        return FileIO.get(path, context);
                    } catch (IOException e) {
                        throw new UncheckedIOException(e);
                    }
                });
    }

    /** File access confined to one table's root. */
    public FileIO tableIO(String tablePath) {
        return new JailedFileIO(rawFileIO(new Path(tablePath)), tablePath);
    }

    public SchemaManager schemaManager(String tablePath, String branch) {
        return new SchemaManager(tableIO(tablePath), new Path(tablePath), branch);
    }

    public Optional<TableSchema> latestSchema(String tablePath, String branch) {
        return schemaManager(tablePath, branch).latest();
    }

    /**
     * The snapshot files of a branch, readable without a schema file, so even when the table's
     * files are gone.
     */
    public SnapshotManager snapshotManager(String tablePath, String branch) {
        return new SnapshotManager(tableIO(tablePath), new Path(tablePath), branch, null, null);
    }

    /** Opens the table at {@code tablePath} on {@code branch} from its latest schema file. */
    public FileStoreTable table(String tablePath, String branch) {
        return table(tablePath, branch, Map.of());
    }

    /** As {@link #table(String, String)}, with dynamic table options (how it reads, say). */
    public FileStoreTable table(String tablePath, String branch, Map<String, String> options) {
        Path path = new Path(tablePath);
        TableSchema schema =
                schemaManager(tablePath, Identifier.DEFAULT_MAIN_BRANCH)
                        .latest()
                        .orElseThrow(
                                () -> new IllegalStateException("No schema file in " + tablePath));
        FileStoreTable table =
                FileStoreTableFactory.create(
                        tableIO(tablePath),
                        path,
                        schema,
                        Options.fromMap(options),
                        CatalogEnvironment.empty());
        if (!BranchManager.isMainBranch(branch)) {
            table = table.switchToBranch(branch);
        }
        return table;
    }

    /** Whether anything exists at {@code path}; used to keep new tables off leftover files. */
    public boolean exists(String path) {
        try {
            Path p = new Path(path);
            return rawFileIO(p).exists(p);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public void mkdirs(String tablePath) {
        try {
            tableIO(tablePath).mkdirs(new Path(tablePath));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /**
     * Whether a schema file under {@code tablePath} was written after {@code instant}: a table made
     * at that root since (#69).
     */
    public boolean schemaWrittenAfter(String tablePath, Instant instant) throws IOException {
        Path schemas = new Path(tablePath, "schema");
        FileIO io = tableIO(tablePath);
        if (!io.exists(schemas)) {
            return false;
        }
        for (FileStatus status : io.listStatus(schemas)) {
            if (status.getPath().getName().startsWith("schema-")
                    && status.getModificationTime() > instant.toEpochMilli()) {
                return true;
            }
        }
        return false;
    }

    /**
     * Deletes a managed table root, and fails if anything is left under it: a writer of the dropped
     * table may still be writing there. A root already gone is purged. Client-chosen external data
     * or index locations are never deleted with the server's credentials; their owner cleans them
     * up.
     */
    public void purgeTable(String tablePath) throws IOException {
        Path root = new Path(tablePath);
        FileIO io = tableIO(tablePath);
        io.delete(root, true);
        if (io.exists(root)) {
            throw new IOException(
                    "Files remain under "
                            + tablePath
                            + " after deleting it; is a writer of the dropped table still"
                            + " writing there?");
        }
    }

    /**
     * Removes a directory that holds nothing (#13): on S3 its zero-byte marker {@code <dir>/}, on a
     * local file system the empty directory; a directory with anything else in it stays as it is.
     * S3A writes such a marker for the parent of whatever it deletes, so purging a table's root
     * leaves one for its database's directory {@code <warehouse>/<db>.db}. The marker goes through
     * the S3 API: S3A deleting it would write one for its own parent in turn. Returns whether
     * nothing is left there.
     */
    public boolean removeIfEmpty(String directory) throws IOException {
        Path dir = new Path(directory);
        if (viaS3(dir)) {
            String bucket = dir.toUri().getHost();
            String marker = key(dir) + "/";
            try {
                List<S3Object> under =
                        s3.client()
                                .listObjectsV2(b -> b.bucket(bucket).prefix(marker).maxKeys(2))
                                .contents();
                if (under.isEmpty()) {
                    return true;
                }
                S3Object only = under.get(0);
                if (under.size() > 1 || !only.key().equals(marker) || only.size() != 0) {
                    return false;
                }
                s3.client().deleteObject(b -> b.bucket(bucket).key(marker));
                return true;
            } catch (SdkException e) {
                throw new IOException(
                        "Removing the empty directory " + directory + " failed: " + e.getMessage(),
                        e);
            }
        }
        String scheme = dir.toUri().getScheme();
        if (scheme == null || "file".equals(scheme)) {
            // One rmdir: a file written there meanwhile keeps the directory.
            try {
                Files.deleteIfExists(Paths.get(dir.toUri().getPath()));
                return true;
            } catch (DirectoryNotEmptyException e) {
                return false;
            }
        }
        FileIO io = rawFileIO(dir);
        if (!io.exists(dir)) {
            return true;
        }
        if (io.listStatus(dir).length > 0) {
            return false;
        }
        if (!io.delete(dir, false) && io.exists(dir)) {
            throw new IOException("Could not remove the empty directory " + directory);
        }
        return true;
    }

    /**
     * Whether {@code path} is inside the table root at {@code tablePath}, where the server may
     * delete; a client-chosen location outside it never is.
     */
    public boolean insideRoot(String tablePath, Path path) {
        return new JailedFileIO(rawFileIO(new Path(tablePath)), tablePath).contains(path);
    }

    /** The pool that reads and deletes many files at once; see {@link #PARALLELISM}. */
    public static ExecutorService io() {
        return IO;
    }

    /** The table's branches as its files have them, main first. */
    public List<String> branches(String tablePath) {
        List<String> out = new ArrayList<>();
        out.add(Identifier.DEFAULT_MAIN_BRANCH);
        out.addAll(table(tablePath, Identifier.DEFAULT_MAIN_BRANCH).branchManager().branches());
        return out;
    }

    /** {@code branch}'s roots of the given kinds (#93), as its files are now. */
    public List<Roots.RootFile> roots(String tablePath, String branch, Roots.Kind... kinds)
            throws IOException {
        return Roots.list(tableIO(tablePath), tablePath, branch, kinds);
    }

    /**
     * The contents of {@code roots}, read {@link #PARALLELISM} at a time, in their order; a root
     * gone since it was listed is left out. Stops with {@link DeadlineException} once {@code
     * deadline} (in {@link System#nanoTime()}, or 0 for none) has passed, keeping what was read.
     */
    public Map<Roots.RootFile, String> read(
            String tablePath,
            List<Roots.RootFile> roots,
            long deadline,
            Map<Roots.RootFile, String> into)
            throws IOException, DeadlineException {
        FileIO io = tableIO(tablePath);
        for (Roots.RootFile root : roots) {
            requireInside(tablePath, root.path());
        }
        int batch = PARALLELISM;
        for (int from = 0; from < roots.size(); from += batch) {
            // At least one batch each time, so that a request sent again makes progress.
            if (from > 0 && deadline != 0 && System.nanoTime() - deadline > 0) {
                throw new DeadlineException(into.size(), roots.size());
            }
            List<Roots.RootFile> part = roots.subList(from, Math.min(roots.size(), from + batch));
            List<Future<Optional<String>>> reads = new ArrayList<>(part.size());
            for (Roots.RootFile root : part) {
                reads.add(
                        IO.submit(
                                () -> {
                                    if (viaS3(root.path())) {
                                        return getS3(root.path());
                                    }
                                    try {
                                        return Optional.of(io.readFileUtf8(root.path()));
                                    } catch (FileNotFoundException e) {
                                        return Optional.empty();
                                    }
                                }));
            }
            for (int i = 0; i < part.size(); i++) {
                Optional<String> content = await(reads.get(i));
                if (content.isPresent()) {
                    into.put(part.get(i), content.get());
                }
            }
        }
        return into;
    }

    /** As {@link #read(String, List, long, Map)}, without a deadline. */
    public Map<Roots.RootFile, String> read(String tablePath, List<Roots.RootFile> roots)
            throws IOException {
        try {
            return read(tablePath, roots, 0, new LinkedHashMap<>());
        } catch (DeadlineException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The time to read what an operation removes ran out; what was read is kept. */
    public static final class DeadlineException extends Exception {
        private static final long serialVersionUID = 1L;

        private final int done;
        private final int of;

        DeadlineException(int done, int of) {
            super("Read " + done + " of " + of + " files before the request's time ran out");
            this.done = done;
            this.of = of;
        }

        public int done() {
            return done;
        }

        public int of() {
            return of;
        }
    }

    /**
     * Deletes {@code paths} under {@code tablePath}, {@link #PARALLELISM} at a time; a file already
     * gone is deleted. Throws the first failure after the others have run.
     */
    public void deleteAll(String tablePath, Collection<Path> paths) throws IOException {
        FileIO io = tableIO(tablePath);
        List<Future<Boolean>> deletes = new ArrayList<>(paths.size());
        Map<String, List<String>> s3Keys = new LinkedHashMap<>();
        List<Path> others = new ArrayList<>();
        for (Path path : paths) {
            requireInside(tablePath, path);
            if (viaS3(path)) {
                s3Keys.computeIfAbsent(path.toUri().getHost(), b -> new ArrayList<>())
                        .add(key(path));
            } else {
                others.add(path);
            }
        }
        for (Map.Entry<String, List<String>> bucket : s3Keys.entrySet()) {
            List<String> keys = bucket.getValue();
            for (int from = 0; from < keys.size(); from += S3_DELETE_BATCH) {
                List<String> part =
                        keys.subList(from, Math.min(keys.size(), from + S3_DELETE_BATCH));
                deletes.add(IO.submit(() -> deleteS3(bucket.getKey(), part)));
            }
        }
        for (Path path : others) {
            deletes.add(
                    IO.submit(
                            () -> {
                                // A local file system answers false rather than throwing.
                                if (!io.delete(path, false) && io.exists(path)) {
                                    throw new IOException("Could not delete " + path);
                                }
                                return true;
                            }));
        }
        IOException failure = null;
        for (Future<Boolean> delete : deletes) {
            try {
                await(delete);
            } catch (IOException e) {
                if (failure == null) {
                    failure = e;
                }
            }
        }
        if (failure != null) {
            throw failure;
        }
    }

    /**
     * Deletes a tag's file and tells the table's tag callbacks, as Paimon's own tag deletion does
     * once it has dealt with the tag's data files, which here are left to a purge (#93).
     */
    public void deleteTagFile(FileStoreTable table, String tagName) throws IOException {
        table.fileIO().delete(table.tagManager().tagPath(tagName), false);
        List<TagCallback> callbacks = table.store().createTagCallbacks(table);
        try {
            callbacks.forEach(callback -> callback.notifyDeletion(tagName));
        } finally {
            callbacks.forEach(IOUtils::closeQuietly);
        }
    }

    private static String key(Path path) {
        String key = path.toUri().getPath();
        return key.startsWith("/") ? key.substring(1) : key;
    }

    /** An object's content through the S3 API; empty when there is no such object. */
    private Optional<String> getS3(Path path) throws IOException {
        try {
            return Optional.of(
                    s3.client()
                            .getObjectAsBytes(b -> b.bucket(path.toUri().getHost()).key(key(path)))
                            .asUtf8String());
        } catch (NoSuchKeyException e) {
            return Optional.empty();
        } catch (SdkException e) {
            throw new IOException("Reading " + path + " failed: " + e.getMessage(), e);
        }
    }

    /** Deletes up to a thousand keys in one request; an object already gone is deleted. */
    private boolean deleteS3(String bucket, List<String> keys) throws IOException {
        List<ObjectIdentifier> objects = new ArrayList<>(keys.size());
        for (String key : keys) {
            objects.add(ObjectIdentifier.builder().key(key).build());
        }
        DeleteObjectsResponse response;
        try {
            response =
                    s3.client()
                            .deleteObjects(
                                    b ->
                                            b.bucket(bucket)
                                                    .delete(
                                                            Delete.builder()
                                                                    .objects(objects)
                                                                    .quiet(true)
                                                                    .build()));
        } catch (SdkException e) {
            throw new IOException(
                    "Deleting "
                            + keys.size()
                            + " objects of "
                            + bucket
                            + " failed: "
                            + e.getMessage(),
                    e);
        }
        if (response.hasErrors() && !response.errors().isEmpty()) {
            S3Error first = response.errors().get(0);
            throw new IOException(
                    "Could not delete "
                            + response.errors().size()
                            + " of "
                            + keys.size()
                            + " objects of "
                            + bucket
                            + ", s3://"
                            + bucket
                            + "/"
                            + first.key()
                            + " the first: "
                            + first.code()
                            + " "
                            + first.message());
        }
        return true;
    }

    private static <T> T await(Future<T> future) throws IOException {
        try {
            return future.get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("Interrupted", e);
        } catch (ExecutionException e) {
            Throwable cause = e.getCause();
            if (cause instanceof IOException io) {
                throw io;
            }
            if (cause instanceof UncheckedIOException u) {
                throw u.getCause();
            }
            if (cause instanceof RuntimeException r) {
                throw r;
            }
            throw new IOException(String.valueOf(cause), cause);
        }
    }
}
