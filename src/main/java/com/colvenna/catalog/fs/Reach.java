package com.colvenna.catalog.fs;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.NoSuchFileException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import javax.annotation.Nullable;
import org.apache.paimon.Snapshot;
import org.apache.paimon.fs.Path;
import org.apache.paimon.index.IndexFileHandler;
import org.apache.paimon.manifest.ExpireFileEntry;
import org.apache.paimon.manifest.FileKind;
import org.apache.paimon.manifest.IndexManifestEntry;
import org.apache.paimon.manifest.ManifestFile;
import org.apache.paimon.manifest.ManifestFileMeta;
import org.apache.paimon.manifest.ManifestList;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.utils.DataFilePathFactories;
import org.apache.paimon.utils.FileStorePathFactory;

/**
 * What snapshots reach, read with one table's manifest readers (#93): a snapshot reaches its
 * manifest lists, the manifests they list, the data files those name (with their extra files), its
 * index manifest and the index files it lists, and its statistics.
 *
 * <p>{@link #reached} is everything a removed root reaches, the files its removal may leave
 * unreferenced. {@link #referenced} is what roots still need, by Paimon's own rule of what a
 * snapshot reads: the data files live in it (its base and delta manifests' entries merged, an
 * {@code ADD} less a later {@code DELETE} of the same file, as {@code FileEntry.mergeEntries}
 * does), those its delta and changelog manifests add, and all of its metadata. A file named only by
 * a {@code DELETE} entry is read by nothing: Paimon's own expiry and tag deletion delete such files
 * too. Files are told apart by name, which Paimon makes unique (a random UUID per writer and a
 * counter), so a name another directory also holds counts as referenced.
 *
 * <p>Metadata that is missing reaches nothing, as Paimon's orphan file cleaning reads it: a
 * snapshot whose manifest list is gone cannot be read through it, whatever is deleted. Any other
 * failure to read is thrown, and nothing is decided on it.
 */
public final class Reach {

    /**
     * When a file may be deleted: data, index and statistics files first, then the manifests that
     * name the data files, then the lists that name the manifests. A purge stopped half way can so
     * be done again from its roots, which still reach whatever is left.
     */
    public enum Order {
        DATA,
        MANIFEST,
        LIST
    }

    /** A file a root reaches: its name, where it is, and when it may be deleted. */
    public record Reached(String name, Path path, Order order) {}

    private final ManifestList lists;
    private final ManifestFile manifests;
    private final IndexFileHandler index;
    private final FileStorePathFactory paths;
    private final DataFilePathFactories dataPaths;
    private final ExecutorService pool;
    private final Map<String, Optional<List<ManifestFileMeta>>> listCache =
            new ConcurrentHashMap<>();
    private final Map<String, Optional<List<ExpireFileEntry>>> entryCache;
    private final Map<String, Optional<List<IndexManifestEntry>>> indexCache =
            new ConcurrentHashMap<>();

    /**
     * Readers of {@code table}'s manifests. {@code entries} caches manifests' entries by name and
     * may be shared between the readers of one table's branches, whose manifests are one directory.
     */
    public Reach(
            FileStoreTable table,
            Map<String, Optional<List<ExpireFileEntry>>> entries,
            ExecutorService pool) {
        this.lists = table.store().manifestListFactory().create();
        this.manifests = table.store().manifestFileFactory().create();
        this.index = table.store().newIndexFileHandler();
        this.paths = table.store().pathFactory();
        this.dataPaths = new DataFilePathFactories(paths);
        this.entryCache = entries;
        this.pool = pool;
    }

    /** A new cache of manifest entries, for {@link #Reach}. */
    public static Map<String, Optional<List<ExpireFileEntry>>> newCache() {
        return new ConcurrentHashMap<>();
    }

    /** Every file {@code roots} reach, by name. */
    public Map<String, Reached> reached(Collection<Snapshot> roots) throws IOException {
        load(roots);
        Map<String, Reached> out = new LinkedHashMap<>();
        for (Snapshot root : roots) {
            listFile(out, root.baseManifestList());
            listFile(out, root.deltaManifestList());
            listFile(out, root.changelogManifestList());
            for (ManifestFileMeta manifest : manifestsOf(root, true, true, true)) {
                put(
                        out,
                        manifest.fileName(),
                        paths.toManifestFilePath(manifest.fileName()),
                        Order.MANIFEST);
                for (ExpireFileEntry entry : entries(manifest)) {
                    if (entry.kind() == FileKind.ADD) {
                        addDataFile(out, entry);
                    }
                }
            }
            if (root.indexManifest() != null) {
                put(
                        out,
                        root.indexManifest(),
                        index.indexManifestFilePath(root.indexManifest()),
                        Order.LIST);
                for (IndexManifestEntry entry : indexEntries(root.indexManifest())) {
                    put(out, entry.indexFile().fileName(), index.filePath(entry), Order.DATA);
                }
            }
            if (root.statistics() != null) {
                put(
                        out,
                        root.statistics(),
                        paths.statsFileFactory().toPath(root.statistics()),
                        Order.DATA);
            }
        }
        return out;
    }

    /** Of {@code names}, those {@code roots} reference; see the class comment. */
    public Set<String> referenced(Collection<Snapshot> roots, Set<String> names)
            throws IOException {
        load(roots);
        Set<String> out = new HashSet<>();
        for (Snapshot root : roots) {
            keep(out, names, root.baseManifestList());
            keep(out, names, root.deltaManifestList());
            keep(out, names, root.changelogManifestList());
            keep(out, names, root.indexManifest());
            keep(out, names, root.statistics());
            for (ManifestFileMeta manifest : manifestsOf(root, true, true, true)) {
                keep(out, names, manifest.fileName());
            }
            // Live in the snapshot: added and not deleted again, by name, which a level's change
            // (a DELETE and an ADD of the same file) keeps live.
            Map<String, Integer> live = new HashMap<>();
            Map<String, ExpireFileEntry> added = new HashMap<>();
            for (ManifestFileMeta manifest : manifestsOf(root, true, true, false)) {
                for (ExpireFileEntry entry : entries(manifest)) {
                    if (names.contains(entry.fileName())) {
                        live.merge(
                                entry.fileName(),
                                entry.kind() == FileKind.ADD ? 1 : -1,
                                Integer::sum);
                        if (entry.kind() == FileKind.ADD) {
                            added.put(entry.fileName(), entry);
                        }
                    }
                }
            }
            live.forEach(
                    (name, count) -> {
                        if (count > 0) {
                            keepWithExtras(out, names, added.get(name));
                        }
                    });
            // What its delta and changelog manifests add, which incremental and changelog reads
            // read.
            for (ManifestFileMeta manifest : manifestsOf(root, false, true, true)) {
                for (ExpireFileEntry entry : entries(manifest)) {
                    if (entry.kind() == FileKind.ADD && names.contains(entry.fileName())) {
                        keepWithExtras(out, names, entry);
                    }
                }
            }
            if (root.indexManifest() != null) {
                for (IndexManifestEntry entry : indexEntries(root.indexManifest())) {
                    keep(out, names, entry.indexFile().fileName());
                }
            }
        }
        return out;
    }

    private static void keep(Set<String> out, Set<String> names, @Nullable String name) {
        if (name != null && names.contains(name)) {
            out.add(name);
        }
    }

    private static void keepWithExtras(Set<String> out, Set<String> names, ExpireFileEntry entry) {
        keep(out, names, entry.fileName());
        for (String extra : entry.extraFiles()) {
            keep(out, names, extra);
        }
    }

    private void listFile(Map<String, Reached> out, @Nullable String name) {
        if (name != null) {
            put(out, name, paths.toManifestListPath(name), Order.LIST);
        }
    }

    private void addDataFile(Map<String, Reached> out, ExpireFileEntry entry) {
        var factory = dataPaths.get(entry.partition(), entry.bucket());
        put(out, entry.fileName(), factory.toPath(entry), Order.DATA);
        for (String extra : entry.extraFiles()) {
            put(out, extra, factory.toAlignedPath(extra, entry), Order.DATA);
        }
    }

    private static void put(Map<String, Reached> out, String name, Path path, Order order) {
        out.putIfAbsent(name, new Reached(name, path, order));
    }

    /** The manifests of a root's base, delta and changelog lists, as chosen; missing lists none. */
    private List<ManifestFileMeta> manifestsOf(
            Snapshot root, boolean base, boolean delta, boolean changelog) {
        List<ManifestFileMeta> out = new ArrayList<>();
        if (base) {
            out.addAll(cachedList(root.baseManifestList()));
        }
        if (delta) {
            out.addAll(cachedList(root.deltaManifestList()));
        }
        if (changelog) {
            out.addAll(cachedList(root.changelogManifestList()));
        }
        return out;
    }

    private List<ManifestFileMeta> cachedList(@Nullable String name) {
        if (name == null) {
            return List.of();
        }
        Optional<List<ManifestFileMeta>> list = listCache.get(name);
        if (list == null) {
            throw new IllegalStateException("Manifest list " + name + " was not loaded");
        }
        return list.orElse(List.of());
    }

    private List<ExpireFileEntry> entries(ManifestFileMeta manifest) {
        Optional<List<ExpireFileEntry>> entries = entryCache.get(manifest.fileName());
        if (entries == null) {
            throw new IllegalStateException("Manifest " + manifest.fileName() + " was not loaded");
        }
        return entries.orElse(List.of());
    }

    private List<IndexManifestEntry> indexEntries(String name) {
        Optional<List<IndexManifestEntry>> entries = indexCache.get(name);
        if (entries == null) {
            throw new IllegalStateException("Index manifest " + name + " was not loaded");
        }
        return entries.orElse(List.of());
    }

    /**
     * Reads, in parallel, every list, manifest and index manifest of {@code roots} not read yet.
     */
    private void load(Collection<Snapshot> roots) throws IOException {
        Set<String> listNames = new HashSet<>();
        Set<String> indexNames = new HashSet<>();
        for (Snapshot root : roots) {
            for (String name :
                    new String[] {
                        root.baseManifestList(),
                        root.deltaManifestList(),
                        root.changelogManifestList()
                    }) {
                if (name != null && !listCache.containsKey(name)) {
                    listNames.add(name);
                }
            }
            if (root.indexManifest() != null && !indexCache.containsKey(root.indexManifest())) {
                indexNames.add(root.indexManifest());
            }
        }
        List<Callable<Void>> reads = new ArrayList<>();
        for (String name : listNames) {
            reads.add(
                    () -> {
                        listCache.put(name, missingIsEmpty(() -> lists.readWithIOException(name)));
                        return null;
                    });
        }
        for (String name : indexNames) {
            reads.add(
                    () -> {
                        indexCache.put(
                                name,
                                missingIsEmpty(() -> index.readManifestWithIOException(name)));
                        return null;
                    });
        }
        runAll(reads);
        Map<String, ManifestFileMeta> toRead = new HashMap<>();
        for (Snapshot root : roots) {
            for (ManifestFileMeta manifest : manifestsOf(root, true, true, true)) {
                if (!entryCache.containsKey(manifest.fileName())) {
                    toRead.putIfAbsent(manifest.fileName(), manifest);
                }
            }
        }
        reads.clear();
        for (ManifestFileMeta manifest : toRead.values()) {
            reads.add(
                    () -> {
                        entryCache.put(
                                manifest.fileName(),
                                missingIsEmpty(
                                        () ->
                                                manifests.readExpireFileEntries(
                                                        manifest.fileName(), manifest.fileSize())));
                        return null;
                    });
        }
        runAll(reads);
    }

    @FunctionalInterface
    private interface Read<T> {
        T read() throws Exception;
    }

    /** Empty when what is read is not there; any other failure is thrown. */
    private static <T> Optional<T> missingIsEmpty(Read<T> read) throws IOException {
        try {
            return Optional.of(read.read());
        } catch (Exception e) {
            for (Throwable t = e; t != null; t = t.getCause()) {
                if (t instanceof FileNotFoundException || t instanceof NoSuchFileException) {
                    return Optional.empty();
                }
            }
            if (e instanceof IOException io) {
                throw io;
            }
            throw new IOException(e.getMessage(), e);
        }
    }

    private void runAll(List<Callable<Void>> tasks) throws IOException {
        List<Future<Void>> futures = new ArrayList<>(tasks.size());
        for (Callable<Void> task : tasks) {
            futures.add(pool.submit(task));
        }
        IOException failure = null;
        for (Future<Void> future : futures) {
            try {
                future.get();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                futures.forEach(f -> f.cancel(true));
                throw new IOException("Interrupted while reading manifests", e);
            } catch (ExecutionException e) {
                Throwable cause = e.getCause();
                if (failure == null) {
                    failure =
                            cause instanceof IOException io
                                    ? io
                                    : cause instanceof UncheckedIOException u
                                            ? u.getCause()
                                            : new IOException(String.valueOf(cause), cause);
                }
            }
        }
        if (failure != null) {
            throw failure;
        }
    }
}
