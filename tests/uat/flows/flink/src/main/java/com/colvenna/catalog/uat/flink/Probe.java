package com.colvenna.catalog.uat.flink;

import java.io.InputStream;
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
import org.apache.paimon.table.FileStoreTable;

/**
 * Whether this pod's own identity (no catalog credentials: the node's role, which a pod without an
 * IRSA role falls back to) can read or list a table's files. Each table is located through the
 * catalog with data tokens off, so the file system the client gets is the ambient one. Prints one
 * line per table and access: {@code PROBE <database>.<table> read|list denied|allowed|error <why>}.
 *
 * <p>Arguments: database, table... Environment as {@link Catalogs}; UAT_S3_REGION, the bucket's.
 */
public final class Probe {

    /** What a refusal by S3, or the absence of any credentials, looks like in an exception. */
    private static final Pattern DENIED =
            Pattern.compile(
                    "(?i)(access ?denied|\\b403\\b|forbidden|not authorized|no aws credentials"
                            + "|unable to load (aws )?credentials|noauthwithaws"
                            + "|credentials? (are |were )?not (found|provided|available))");

    private Probe() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            throw new IllegalArgumentException("usage: Probe <database> <table>...");
        }
        String db = args[0];
        Map<String, String> options = Catalogs.options(false);
        String region = System.getenv("UAT_S3_REGION");
        if (region != null && !region.isEmpty()) {
            options.put("fs.s3a.endpoint.region", region);
        }
        // One attempt: a refusal is the answer, there is nothing to retry.
        options.put("fs.s3a.attempts.maximum", "1");
        options.put("fs.s3a.retry.limit", "1");
        try (Catalog catalog =
                CatalogFactory.createCatalog(CatalogContext.create(Options.fromMap(options)))) {
            for (String name : List.of(args).subList(1, args.length)) {
                FileStoreTable table =
                        (FileStoreTable) catalog.getTable(Identifier.create(db, name));
                FileIO io = table.fileIO();
                Path root = table.location();
                report(db + "." + name, "read", () -> read(io, new Path(root, "schema/schema-0")));
                report(db + "." + name, "list", () -> io.listStatus(root).length);
            }
        }
    }

    @FunctionalInterface
    private interface Access {
        Object run() throws Exception;
    }

    private static int read(FileIO io, Path path) throws Exception {
        try (InputStream in = io.newInputStream(path)) {
            return in.read();
        }
    }

    private static void report(String table, String access, Access body) {
        String outcome;
        try {
            body.run();
            outcome = "allowed";
        } catch (Exception e) {
            StringBuilder text = new StringBuilder(String.valueOf(e));
            Throwable cause = e.getCause();
            for (int depth = 0; cause != null && depth < 10; depth++) {
                text.append(" / ").append(cause);
                cause = cause.getCause();
            }
            outcome =
                    (DENIED.matcher(text).find() ? "denied " : "error ")
                            + text.toString().replaceAll("\\s+", " ");
        }
        System.out.println("PROBE " + table + " " + access + " " + outcome);
    }
}
