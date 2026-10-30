package com.colvenna.catalog.fs;

import com.colvenna.catalog.http.ApiException;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import javax.annotation.Nullable;
import org.apache.paimon.CoreOptions;
import org.apache.paimon.fs.Path;

/**
 * Which locations a table may name.
 *
 * <p>The warehouse root is the catalog's managed area: managed tables live there and the server's
 * credentials reach it. A client-chosen location ({@code path} of an external table, {@code
 * data-file.external-paths}, {@code global-index.external-path}) must lie under one of the
 * operator's allowed external roots and outside the warehouse. With no allowed roots, the default,
 * clients cannot choose locations at all.
 */
public final class PathRules {

    /** Table options that name locations chosen by the client. */
    public static final List<String> CLIENT_LOCATION_OPTIONS =
            List.of(
                    CoreOptions.PATH.key(),
                    CoreOptions.DATA_FILE_EXTERNAL_PATHS.key(),
                    CoreOptions.GLOBAL_INDEX_EXTERNAL_PATH.key());

    private final String warehouseRoot;
    private final List<String> externalRoots;

    public PathRules(String warehouseRoot, List<String> externalRoots) {
        this.warehouseRoot = normalize(warehouseRoot);
        this.externalRoots = externalRoots.stream().map(PathRules::normalize).toList();
    }

    /**
     * A comparable form: normalized URI, {@code s3a} as {@code s3}, no repeated or trailing slash.
     * A bucket or file-system root keeps no path at all ({@code s3://bucket}, {@code file://}), so
     * that {@code root + "/"} prefixes everything inside it.
     */
    public static String normalize(String location) {
        URI uri = new Path(location).toUri().normalize();
        String path = uri.getPath() == null ? "" : uri.getPath().replaceAll("/{2,}", "/");
        while (path.endsWith("/")) {
            path = path.substring(0, path.length() - 1);
        }
        // Paimon resolves a path without a scheme on the local file system.
        String scheme = uri.getScheme() == null ? "file" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (scheme.equals("s3a")) {
            scheme = "s3";
        }
        // file:/x and file:///x name the same file; both normalize to file:///x.
        String authority = uri.getAuthority() == null ? "" : uri.getAuthority();
        return scheme + "://" + authority + path;
    }

    /** The normalized path part alone, without scheme or authority. */
    public static String normalizePath(String location) {
        String path = new Path(location).toUri().normalize().getPath();
        if (path == null) {
            return "";
        }
        while (path.endsWith("/") && path.length() > 1) {
            path = path.substring(0, path.length() - 1);
        }
        return path;
    }

    /** Whether {@code a} and {@code b} are the same location or one contains the other. */
    public static boolean overlap(String a, String b) {
        String x = normalize(a);
        String y = normalize(b);
        return x.equals(y) || x.startsWith(y + "/") || y.startsWith(x + "/");
    }

    public boolean insideWarehouse(String location) {
        return within(normalize(location), warehouseRoot);
    }

    /** Whether {@code location} lies strictly below the warehouse root. */
    public boolean belowWarehouse(String location) {
        String normalized = normalize(location);
        return !normalized.equals(warehouseRoot) && within(normalized, warehouseRoot);
    }

    /**
     * The database directory {@code <warehouse>/<db>.db} that a managed table's root {@code
     * <warehouse>/<db>.db/<table>} lies in, as the location is written; empty for a root of any
     * other shape.
     */
    public Optional<String> databaseDirectory(String tableRoot) {
        String root = tableRoot;
        while (root.endsWith("/")) {
            root = root.substring(0, root.length() - 1);
        }
        int slash = root.lastIndexOf('/');
        if (slash <= 0) {
            return Optional.empty();
        }
        String parent = root.substring(0, slash);
        String name = parent.substring(parent.lastIndexOf('/') + 1);
        String grandparent = parent.substring(0, Math.max(0, parent.lastIndexOf('/')));
        if (name.length() <= ".db".length()
                || !name.endsWith(".db")
                || grandparent.isEmpty()
                || !normalize(grandparent).equals(warehouseRoot)) {
            return Optional.empty();
        }
        return Optional.of(parent);
    }

    private static boolean within(String normalized, String root) {
        return normalized.equals(root) || normalized.startsWith(root + "/");
    }

    /** Rejects client-chosen locations inside the managed warehouse area. */
    public void checkClientLocations(Map<String, String> options) {
        for (String key : CLIENT_LOCATION_OPTIONS) {
            for (String location : split(options.get(key))) {
                checkClientLocation(key, location);
            }
        }
    }

    public void checkClientLocation(String option, String location) {
        String normalized = normalize(location);
        if (externalRoots.stream().noneMatch(root -> within(normalized, root))) {
            throw ApiException.badRequest(
                    "Option "
                            + option
                            + " names "
                            + location
                            + ", which is not under an allowed external location root"
                            + (externalRoots.isEmpty()
                                    ? "; this catalog allows no client-chosen locations."
                                    : " " + externalRoots + "."));
        }
        if (insideWarehouse(location) || within(warehouseRoot, normalized)) {
            throw ApiException.badRequest(
                    "Option "
                            + option
                            + " names "
                            + location
                            + ", which overlaps the catalog-managed warehouse "
                            + warehouseRoot
                            + "; client-chosen locations must be outside it.");
        }
    }

    static List<String> split(@Nullable String value) {
        List<String> parts = new ArrayList<>();
        if (value != null) {
            for (String part : value.split(",", -1)) {
                if (!part.isBlank()) {
                    parts.add(part.trim());
                }
            }
        }
        return parts;
    }
}
