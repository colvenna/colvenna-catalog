package com.colvenna.catalog.fs;

import java.io.IOException;
import java.time.Duration;
import java.util.Optional;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.FileStatus;
import org.apache.paimon.fs.Path;
import org.apache.paimon.fs.PositionOutputStream;
import org.apache.paimon.fs.RemoteIterator;
import org.apache.paimon.fs.SeekableInputStream;
import org.apache.paimon.fs.StorageType;
import org.apache.paimon.fs.TwoPhaseOutputStream;
import org.apache.paimon.fs.local.LocalFileIO;

/**
 * The server's file access for one table, refused anywhere outside that table's root.
 *
 * <p>Clients write a table's manifests with their own credentials, so a manifest can name any
 * absolute path. When the server later deletes or rewrites files on the table's behalf (rollback,
 * tag deletion, truncation, drop), every path goes through this jail; a path outside the table root
 * fails instead of letting a client use the server's credentials against another table.
 */
public final class JailedFileIO implements FileIO {

    private static final long serialVersionUID = 1L;

    private final FileIO delegate;
    private final String root;

    public JailedFileIO(FileIO delegate, String tableRoot) {
        this.delegate = delegate;
        this.root = comparable(tableRoot);
    }

    /**
     * Local file systems answer listings with {@code file:} paths whatever scheme opened them, so
     * on a local delegate only the path counts; object stores keep their scheme.
     */
    private String comparable(String location) {
        if (delegate instanceof LocalFileIO) {
            return PathRules.normalizePath(location);
        }
        return PathRules.normalize(location);
    }

    /** The unconfined FileIO, after checking that {@code path} is inside the jail. */
    public FileIO unwrapFor(Path path) throws IOException {
        check(path);
        return delegate;
    }

    /** Whether {@code path} is inside the jail. */
    public boolean contains(Path path) {
        String normalized = comparable(path.toString());
        return normalized.equals(root) || normalized.startsWith(root + "/");
    }

    private Path check(Path path) throws IOException {
        if (!contains(path)) {
            throw new IOException(
                    "Refusing to touch " + path + ": outside the table root " + root + ".");
        }
        return path;
    }

    @Override
    public boolean isObjectStore() {
        return delegate.isObjectStore();
    }

    @Override
    public void configure(CatalogContext context) {
        // The delegate is configured already.
    }

    @Override
    public SeekableInputStream newInputStream(Path path) throws IOException {
        return delegate.newInputStream(check(path));
    }

    @Override
    public PositionOutputStream newOutputStream(Path path, boolean overwrite) throws IOException {
        return delegate.newOutputStream(check(path), overwrite);
    }

    @Override
    public TwoPhaseOutputStream newTwoPhaseOutputStream(Path path, boolean overwrite)
            throws IOException {
        return delegate.newTwoPhaseOutputStream(check(path), overwrite);
    }

    @Override
    public FileStatus getFileStatus(Path path) throws IOException {
        return delegate.getFileStatus(check(path));
    }

    @Override
    public FileStatus[] listStatus(Path path) throws IOException {
        return delegate.listStatus(check(path));
    }

    @Override
    public FileStatus[] listFiles(Path path, boolean recursive) throws IOException {
        return delegate.listFiles(check(path), recursive);
    }

    @Override
    public RemoteIterator<FileStatus> listFilesIterative(Path path, boolean recursive)
            throws IOException {
        return delegate.listFilesIterative(check(path), recursive);
    }

    @Override
    public FileStatus[] listDirectories(Path path) throws IOException {
        return delegate.listDirectories(check(path));
    }

    @Override
    public boolean exists(Path path) throws IOException {
        return delegate.exists(check(path));
    }

    @Override
    public boolean delete(Path path, boolean recursive) throws IOException {
        return delegate.delete(check(path), recursive);
    }

    @Override
    public boolean mkdirs(Path path) throws IOException {
        return delegate.mkdirs(check(path));
    }

    @Override
    public boolean rename(Path src, Path dst) throws IOException {
        return delegate.rename(check(src), check(dst));
    }

    @Override
    public Optional<Path> archive(Path path, StorageType type) throws IOException {
        return delegate.archive(check(path), type);
    }

    @Override
    public void restoreArchive(Path path, Duration duration) throws IOException {
        delegate.restoreArchive(check(path), duration);
    }

    @Override
    public Optional<Path> unarchive(Path path, StorageType type) throws IOException {
        return delegate.unarchive(check(path), type);
    }
}
