package com.colvenna.catalog.fs;

import java.io.FileNotFoundException;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import org.apache.paimon.Changelog;
import org.apache.paimon.Snapshot;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.FileStatus;
import org.apache.paimon.fs.Path;
import org.apache.paimon.tag.Tag;
import org.apache.paimon.utils.BranchManager;

/**
 * The files of a table that reference its other files (#93): each branch's snapshots, tags and
 * long-lived changelogs. Every data file, manifest, index and statistics file of a Paimon table is
 * reached from one of them; a file no root reaches is read by nothing.
 */
public final class Roots {

    private Roots() {}

    /** What a root is. A branch's latest schema is kept with its roots when it is deleted. */
    public enum Kind {
        SNAPSHOT("snapshot", "snapshot-"),
        TAG("tag", "tag-"),
        CHANGELOG("changelog", "changelog-"),
        SCHEMA("schema", "schema-");

        private final String directory;
        private final String prefix;

        Kind(String directory, String prefix) {
            this.directory = directory;
            this.prefix = prefix;
        }

        /** Whether it references other files: a schema does not. */
        public boolean references() {
            return this != SCHEMA;
        }
    }

    /** A root file as listed: which branch's, what it is, its name and when it was written. */
    public record RootFile(String branch, Kind kind, String name, long modified, Path path) {

        /** Tells a root from another, and from the same name written again. */
        public String key() {
            return branch + "/" + kind + "/" + name + "@" + modified;
        }

        /** The snapshot id of a snapshot or changelog, from its name. */
        public long id() {
            return Long.parseLong(name.substring(kind.prefix.length()));
        }

        /** A tag's name, from its file's. */
        public String tagName() {
            return name.substring(Kind.TAG.prefix.length());
        }
    }

    /** A root as recorded: its file's kind, name and time, and its content. */
    public record Root(Kind kind, String name, long modified, String content) {

        /** The snapshot it is or holds: a tag trimmed to its snapshot, a changelog as one. */
        public Snapshot snapshot() {
            return switch (kind) {
                case SNAPSHOT -> Snapshot.fromJson(content);
                case TAG -> Tag.fromJson(content).trimToSnapshot();
                case CHANGELOG -> Changelog.fromJson(content);
                case SCHEMA -> throw new IllegalStateException("A schema is no snapshot");
            };
        }
    }

    /** The directory of {@code branch}'s roots of {@code kind}. */
    public static Path directory(String tablePath, String branch, Kind kind) {
        return new Path(
                BranchManager.branchPath(new Path(tablePath), branch) + "/" + kind.directory);
    }

    /**
     * {@code branch}'s roots of the given kinds, as the files are now; a directory that does not
     * exist has none. Hint files and anything else in the directories are not roots.
     */
    public static List<RootFile> list(FileIO io, String tablePath, String branch, Kind... kinds)
            throws IOException {
        List<RootFile> roots = new ArrayList<>();
        for (Kind kind : kinds) {
            Path dir = directory(tablePath, branch, kind);
            FileStatus[] statuses;
            try {
                statuses = io.listStatus(dir);
            } catch (FileNotFoundException e) {
                continue;
            }
            if (statuses == null) {
                continue;
            }
            for (FileStatus status : statuses) {
                String name = status.getPath().getName();
                if (!status.isDir() && name.startsWith(kind.prefix) && valid(kind, name)) {
                    roots.add(
                            new RootFile(
                                    branch,
                                    kind,
                                    name,
                                    status.getModificationTime(),
                                    status.getPath()));
                }
            }
        }
        return roots;
    }

    private static boolean valid(Kind kind, String name) {
        String rest = name.substring(kind.prefix.length());
        if (rest.isEmpty()) {
            return false;
        }
        if (kind == Kind.TAG) {
            return true;
        }
        for (int i = 0; i < rest.length(); i++) {
            if (!Character.isDigit(rest.charAt(i))) {
                return false;
            }
        }
        return true;
    }
}
