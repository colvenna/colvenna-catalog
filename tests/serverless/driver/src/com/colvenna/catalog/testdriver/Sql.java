package com.colvenna.catalog.testdriver;

import com.fasterxml.jackson.databind.JsonNode;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.rds.RdsUtilities;

/**
 * The {@code sql} action (the UAT harness's, #52): statements on the catalog's metadata database,
 * which admits only the catalog's functions, from inside its VPC. The driver signs in as the user
 * the event names with an RDS IAM authentication token it signs itself with its own role (which the
 * driver stack lets connect as that user only, when it is deployed with database access), over TLS
 * verified against the RDS CA bundle the server's image carries: no password is used or kept. The
 * statements run on one connection, in autocommit, as one script; the result is every row of every
 * result set, as strings.
 *
 * <p>Event: {@code host}, {@code port}, {@code database}, {@code user}, {@code region}, {@code
 * statements}.
 */
final class Sql {

    private static final String CA = "/opt/colvenna-catalog/rds-global-bundle.pem";

    private Sql() {}

    static Map<String, Object> run(JsonNode event) throws Exception {
        String host = required(event, "host");
        int port = event.path("port").asInt(5432);
        String user = required(event, "user");
        String database = required(event, "database");
        String statements = required(event, "statements");
        Region region = Region.of(required(event, "region"));
        String token =
                RdsUtilities.builder()
                        .region(region)
                        .credentialsProvider(DefaultCredentialsProvider.builder().build())
                        .build()
                        .generateAuthenticationToken(
                                b -> b.hostname(host).port(port).username(user));
        Properties props = new Properties();
        props.setProperty("user", user);
        props.setProperty("password", token);
        props.setProperty("sslmode", "verify-full");
        props.setProperty("sslrootcert", CA);
        props.setProperty("connectTimeout", "10");
        props.setProperty("ApplicationName", "colvenna-catalog-uat");
        List<List<String>> rows = new ArrayList<>();
        List<Integer> updates = new ArrayList<>();
        try (Connection c =
                        DriverManager.getConnection(
                                "jdbc:postgresql://" + host + ":" + port + "/" + database, props);
                Statement s = c.createStatement()) {
            boolean result = s.execute(statements);
            while (true) {
                if (result) {
                    try (ResultSet rs = s.getResultSet()) {
                        ResultSetMetaData meta = rs.getMetaData();
                        while (rs.next()) {
                            List<String> row = new ArrayList<>();
                            for (int i = 1; i <= meta.getColumnCount(); i++) {
                                row.add(rs.getString(i));
                            }
                            rows.add(row);
                        }
                    }
                } else if (s.getUpdateCount() == -1) {
                    break;
                } else {
                    updates.add(s.getUpdateCount());
                }
                result = s.getMoreResults();
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("rows", rows);
        out.put("updateCounts", updates);
        return out;
    }

    private static String required(JsonNode event, String field) {
        String value = event.path(field).asText("");
        if (value.isEmpty()) {
            throw new IllegalArgumentException("The sql event needs " + field);
        }
        return value;
    }
}
