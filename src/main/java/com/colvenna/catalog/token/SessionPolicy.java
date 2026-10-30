package com.colvenna.catalog.token;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.apache.paimon.rest.RESTApi;

/**
 * The STS session policy of a table credential: object access below the table root and listing of
 * that prefix only. The effective permissions are the intersection with the data role's own policy,
 * so a token can never reach more than one table even if this policy were wider.
 */
public final class SessionPolicy {

    /** What the holder may do with the table files. */
    public enum Access {
        READ(List.of("s3:GetObject")),
        READ_WRITE(
                List.of(
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts"));

        private final List<String> objectActions;

        Access(List<String> objectActions) {
            this.objectActions = objectActions;
        }
    }

    /** Bucket and key prefix of an {@code s3://bucket/prefix} table root. */
    public record Location(String bucket, String prefix) {
        /**
         * Parses the stored root as text, not as a URI, so no character of a name can end the path
         * early: {@code s3://bucket/key/prefix}, {@code s3a} accepted, slashes collapsed.
         */
        public static Location of(String tablePath) {
            String rest;
            String lower = tablePath.toLowerCase(Locale.ROOT);
            if (lower.startsWith("s3://")) {
                rest = tablePath.substring("s3://".length());
            } else if (lower.startsWith("s3a://")) {
                rest = tablePath.substring("s3a://".length());
            } else {
                throw new IllegalArgumentException("Not an S3 table path: " + tablePath);
            }
            int slash = rest.indexOf('/');
            String bucket = slash < 0 ? rest : rest.substring(0, slash);
            String prefix = slash < 0 ? "" : rest.substring(slash + 1).replaceAll("/{2,}", "/");
            prefix = prefix.replaceAll("^/|/$", "");
            if (bucket.isEmpty() || prefix.isEmpty()) {
                throw new IllegalArgumentException("Table path has no bucket or key: " + tablePath);
            }
            return new Location(bucket, prefix);
        }
    }

    private SessionPolicy() {}

    public static String json(Location location, Access access) {
        String bucketArn = "arn:aws:s3:::" + location.bucket();
        String prefix = literal(location.prefix());
        List<Object> statements = new ArrayList<>();
        // The root key itself too: S3A probes a directory with a HEAD on its key, and a 403 there
        // is an error, not "not found".
        statements.add(
                statement(
                        "TableObjects",
                        access.objectActions,
                        List.of(bucketArn + "/" + prefix, bucketArn + "/" + prefix + "/*"),
                        null));
        if (access == Access.READ) {
            // A streaming reader with a consumer id records its progress in the table's consumer
            // directories; that is the only place a read-only credential may write.
            statements.add(
                    statement(
                            "ConsumerProgress",
                            List.of("s3:PutObject", "s3:DeleteObject"),
                            List.of(
                                    bucketArn + "/" + prefix + "/consumer/*",
                                    bucketArn + "/" + prefix + "/branch/*/consumer/*"),
                            null));
        }
        if (access == Access.READ_WRITE) {
            statements.add(
                    statement(
                            "TableUploads",
                            List.of("s3:ListBucketMultipartUploads"),
                            bucketArn,
                            underPrefix(prefix)));
        }
        statements.add(
                statement(
                        "TableListing", List.of("s3:ListBucket"), bucketArn, underPrefix(prefix)));
        statements.add(
                statement("BucketLocation", List.of("s3:GetBucketLocation"), bucketArn, null));
        Map<String, Object> policy = new LinkedHashMap<>();
        policy.put("Version", "2012-10-17");
        policy.put("Statement", statements);
        try {
            return RESTApi.toJson(policy);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Keys below the table root. Not the bare prefix: "orders" would list "orders_v2" too. */
    private static Map<String, Object> underPrefix(String prefix) {
        return Map.of("StringLike", Map.of("s3:prefix", List.of(prefix + "/*")));
    }

    /** Escapes what IAM reads as wildcards or variables in ARNs and StringLike values. */
    static String literal(String key) {
        StringBuilder out = new StringBuilder(key.length());
        for (char ch : key.toCharArray()) {
            if (ch == '*' || ch == '?' || ch == '$') {
                out.append("${").append(ch).append('}');
            } else {
                out.append(ch);
            }
        }
        return out.toString();
    }

    private static Map<String, Object> statement(
            String sid, List<String> actions, Object resource, Map<String, Object> condition) {
        Map<String, Object> statement = new LinkedHashMap<>();
        statement.put("Sid", sid);
        statement.put("Effect", "Allow");
        statement.put("Action", actions);
        statement.put("Resource", resource);
        if (condition != null) {
            statement.put("Condition", condition);
        }
        return statement;
    }
}
