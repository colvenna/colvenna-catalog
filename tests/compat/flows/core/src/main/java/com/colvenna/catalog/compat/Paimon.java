package com.colvenna.catalog.compat;

import java.io.InputStream;
import java.net.URL;
import java.security.CodeSource;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import org.apache.paimon.catalog.Catalog;
import org.apache.paimon.catalog.CatalogContext;
import org.apache.paimon.catalog.CatalogFactory;
import org.apache.paimon.catalog.Identifier;
import org.apache.paimon.fs.FileIO;
import org.apache.paimon.fs.Path;
import org.apache.paimon.options.Options;
import org.apache.paimon.rest.RESTCatalog;
import org.apache.paimon.table.FileStoreTable;
import org.apache.paimon.table.Table;

/**
 * What the flow does with the Paimon client directly rather than through the engine: report the
 * client's version, probe the engine's own identity, and drop the run's databases.
 */
public final class Paimon {

    /** What a refusal by S3, or the absence of any credentials, looks like in an exception. */
    private static final Pattern DENIED =
            Pattern.compile(
                    "(?i)(access ?denied|\\b403\\b|forbidden|not authorized|no aws credentials"
                            + "|unable to load (aws )?credentials|noauthwithaws"
                            + "|credentials? (are |were )?not (found|provided|available))");

    private Paimon() {}

    /** The Paimon client the engine loaded: its version and the jar it came from. */
    public static Map<String, Object> clientInfo() {
        Map<String, Object> info = new LinkedHashMap<>();
        Package p = RESTCatalog.class.getPackage();
        info.put("paimonVersion", p == null ? null : p.getImplementationVersion());
        CodeSource source = RESTCatalog.class.getProtectionDomain().getCodeSource();
        URL location = source == null ? null : source.getLocation();
        String jar = location == null ? "" : location.getPath();
        info.put("paimonJar", jar.substring(jar.lastIndexOf('/') + 1));
        return info;
    }

    static Catalog catalog(Map<String, String> options) {
        return CatalogFactory.createCatalog(CatalogContext.create(Options.fromMap(options)));
    }

    /**
     * Whether the engine's own identity (no catalog credentials: the container's environment,
     * instance profile or pod identity) can read the table's first schema file. The table is
     * located through the catalog with data tokens off, so the file system it gets is the ambient
     * one. Returns {@code null} when the identity is refused, as it must be, and otherwise why not.
     */
    static String ambientRefusal(Settings settings, String db, String table) throws Exception {
        Map<String, String> options = settings.catalogOptions(false);
        if (!settings.s3Region().isEmpty()) {
            options.put("fs.s3a.endpoint.region", settings.s3Region());
        }
        // One attempt: a refusal is the answer, there is nothing to retry.
        options.put("fs.s3a.attempts.maximum", "1");
        options.put("fs.s3a.retry.limit", "1");
        try (Catalog catalog = catalog(options)) {
            Table loaded = catalog.getTable(Identifier.create(db, table));
            if (!(loaded instanceof FileStoreTable)) {
                return "the catalog returned a " + loaded.getClass().getSimpleName();
            }
            FileStoreTable fileStore = (FileStoreTable) loaded;
            FileIO io = fileStore.fileIO();
            Path schema = new Path(fileStore.location(), "schema/schema-0");
            try {
                if (!io.exists(schema)) {
                    return "inconclusive: the engine's own identity saw no " + "schema-0 file";
                }
                try (InputStream in = io.newInputStream(schema)) {
                    in.read();
                }
                return "the engine's own identity read the table's schema file";
            } catch (Exception e) {
                String text = Report.describe(e);
                if (DENIED.matcher(text).find()) {
                    Report.line("COMPAT-INFO", Map.of("table", table, "ambientRefusal", text));
                    return null;
                }
                return "inconclusive: " + text;
            }
        }
    }

    /** The catalog's databases whose names start with the prefix. */
    static List<String> databases(Settings settings, String prefix) throws Exception {
        List<String> found = new ArrayList<>();
        try (Catalog catalog = catalog(settings.catalogOptions(false))) {
            for (String db : catalog.listDatabases()) {
                if (db.startsWith(prefix)) {
                    found.add(db);
                }
            }
        }
        return found;
    }

    /** Drops every database whose name starts with the prefix, with its tables. */
    static List<String> dropDatabases(Settings settings, String prefix) throws Exception {
        List<String> dropped = new ArrayList<>();
        try (Catalog catalog = catalog(settings.catalogOptions(false))) {
            for (String db : catalog.listDatabases()) {
                if (db.startsWith(prefix)) {
                    catalog.dropDatabase(db, true, true);
                    dropped.add(db);
                }
            }
        }
        return dropped;
    }
}
