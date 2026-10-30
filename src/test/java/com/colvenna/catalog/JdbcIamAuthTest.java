package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.db.Db;
import com.colvenna.catalog.db.RdsIamCredentials;
import com.colvenna.catalog.testing.TestServers;
import com.zaxxer.hikari.HikariDataSource;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.testcontainers.postgresql.PostgreSQLContainer;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;

/**
 * RDS IAM database authentication ({@code COLVENNA_JDBC_AUTH=iam}), the serverless form's way to
 * sign in to PostgreSQL without a password anywhere: a token for every new connection.
 */
class JdbcIamAuthTest {

    private static final String URL =
            "jdbc:postgresql://db.example.com:5433/colvenna_catalog?sslmode=verify-full"
                    + "&sslrootcert=/opt/colvenna-catalog/rds-global-bundle.pem";

    private static Map<String, String> iam() {
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_JDBC_URL", URL);
        env.put("COLVENNA_JDBC_USER", "colvenna_catalog");
        env.put("COLVENNA_JDBC_AUTH", "iam");
        env.put("AWS_REGION", "us-west-2");
        env.put("COLVENNA_WAREHOUSE_NAME", "colvenna");
        env.put("COLVENNA_WAREHOUSE_ROOT", "file:///tmp/w");
        return env;
    }

    @Test
    void passwordIsTheDefaultAndUnchanged() {
        Map<String, String> env = iam();
        env.remove("COLVENNA_JDBC_AUTH");
        env.put("COLVENNA_JDBC_PASSWORD", "secret");
        ServerConfig config = ServerConfig.fromEnvironment(env);
        assertThat(config.jdbcAuth()).isEqualTo(ServerConfig.JdbcAuth.PASSWORD);
        Db.Credentials credentials = config.dbCredentials();
        assertThat(credentials.fixed()).isTrue();
        assertThat(credentials.password()).isEqualTo("secret");
        env.put("COLVENNA_JDBC_AUTH", "PASSWORD");
        assertThat(ServerConfig.fromEnvironment(env).jdbcAuth())
                .isEqualTo(ServerConfig.JdbcAuth.PASSWORD);
    }

    @Test
    void inIamModeNoPasswordIsTaken() {
        ServerConfig config = ServerConfig.fromEnvironment(iam());
        assertThat(config.jdbcAuth().method()).isEqualTo(ServerConfig.JdbcAuth.Method.IAM);
        assertThat(config.jdbcAuth().region()).isEqualTo("us-west-2");
        assertThat(config.jdbcPassword()).isNull();
        assertThat(config.dbCredentials().fixed()).isFalse();
        assertThat(config.dbCredentials().user()).isEqualTo("colvenna_catalog");

        Map<String, String> withPassword = iam();
        withPassword.put("COLVENNA_JDBC_PASSWORD", "left-over");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(withPassword))
                .hasMessageContaining("COLVENNA_JDBC_PASSWORD must not be set")
                .hasMessageNotContaining("left-over");
    }

    @Test
    void iamModeSendsTokensOnlyOverVerifiedTlsToOneHost() {
        Map<String, String> env = iam();
        env.put("COLVENNA_JDBC_URL", URL.replace("verify-full", "require"));
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("sslmode=verify-full");
        env.put("COLVENNA_JDBC_URL", "jdbc:postgresql://a:5432,b:5432/x?sslmode=verify-full");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("one host");
        env.remove("COLVENNA_JDBC_URL");
        env.put(
                "COLVENNA_JDBC_URL",
                "jdbc:postgresql://db.example.com/x?ssl=true&sslmode=verify-full&a=b");
        assertThat(ServerConfig.fromEnvironment(env).jdbcAuth().method())
                .isEqualTo(ServerConfig.JdbcAuth.Method.IAM);
    }

    @Test
    void iamModeNeedsARegionAndAUser() {
        Map<String, String> env = iam();
        env.remove("AWS_REGION");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("COLVENNA_JDBC_IAM_REGION or AWS_REGION");
        env.put("COLVENNA_JDBC_IAM_REGION", "eu-west-1");
        assertThat(ServerConfig.fromEnvironment(env).jdbcAuth().region()).isEqualTo("eu-west-1");
        env.remove("COLVENNA_JDBC_USER");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("COLVENNA_JDBC_USER");
        Map<String, String> bad = iam();
        bad.put("COLVENNA_JDBC_AUTH", "token");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(bad))
                .hasMessageContaining("COLVENNA_JDBC_AUTH must be password or iam");
    }

    @Test
    void eachPasswordIsATokenForTheUrlsHostPortAndUser() {
        List<String> asked = new ArrayList<>();
        RdsIamCredentials credentials =
                new RdsIamCredentials(
                        URL,
                        "colvenna_catalog",
                        (host, port, user) -> {
                            asked.add(host + ":" + port + "/" + user);
                            return "token-" + asked.size();
                        });
        assertThat(credentials.password()).isEqualTo("token-1");
        assertThat(credentials.password()).isEqualTo("token-2");
        assertThat(asked)
                .containsExactly(
                        "db.example.com:5433/colvenna_catalog",
                        "db.example.com:5433/colvenna_catalog");
        assertThat(
                        new RdsIamCredentials(
                                        "jdbc:postgresql://db.example.com/x?sslmode=verify-full",
                                        "u",
                                        (h, p, u) -> h + ":" + p)
                                .password())
                .isEqualTo("db.example.com:5432");
        assertThat(credentials.toString()).doesNotContain("token");
    }

    @Test
    void theSdkSignsTheTokenLocally() {
        // Static credentials: no AWS call, no credentials of this host.
        RdsIamCredentials credentials =
                RdsIamCredentials.create(
                        URL,
                        "colvenna_catalog",
                        Region.US_WEST_2,
                        StaticCredentialsProvider.create(
                                AwsBasicCredentials.create("AKIDEXAMPLE", "not-a-secret")));
        String token = credentials.password();
        assertThat(token)
                .startsWith("db.example.com:5433/?")
                .contains("Action=connect")
                .contains("DBUser=colvenna_catalog")
                .contains("X-Amz-Credential=AKIDEXAMPLE%2F")
                .contains("%2Fus-west-2%2Frds-db%2Faws4_request")
                .contains("X-Amz-Signature=")
                .doesNotContain("not-a-secret");
    }

    @Test
    void everyNewConnectionAsksForAFreshPasswordAndAReusedOneDoesNot() throws Exception {
        PostgreSQLContainer pg = TestServers.postgres();
        int[] asked = {0};
        Db.Credentials counting =
                new Db.Credentials() {
                    @Override
                    public String user() {
                        return pg.getUsername();
                    }

                    @Override
                    public String password() {
                        asked[0]++;
                        return pg.getPassword();
                    }

                    @Override
                    public boolean fixed() {
                        return false;
                    }
                };
        try (Db db = new Db(pg.getJdbcUrl(), counting, 2, 1, Duration.ofSeconds(5))) {
            assertThat(asked[0]).as("building the pools opens nothing").isZero();
            HikariDataSource main = (HikariDataSource) db.dataSource();
            try (Connection first = main.getConnection();
                    Connection second = main.getConnection()) {
                assertThat(first.isValid(5)).isTrue();
                assertThat(second.isValid(5)).isTrue();
            }
            // Hikari may open its pool's connections in the background: each asks for its own.
            int total = main.getHikariPoolMXBean().getTotalConnections();
            assertThat(total).isEqualTo(2);
            int afterOpening = asked[0];
            assertThat(afterOpening)
                    .as("a password asked for each new connection")
                    .isGreaterThanOrEqualTo(total);
            for (int i = 0; i < 5; i++) {
                try (Connection again = main.getConnection()) {
                    assertThat(again.isValid(5)).isTrue();
                }
            }
            assertThat(asked[0])
                    .as("a pooled connection is reused without a new password")
                    .isEqualTo(afterOpening);
            main.getHikariPoolMXBean().softEvictConnections();
            try (Connection fresh = main.getConnection()) {
                assertThat(fresh.isValid(5)).isTrue();
            }
            assertThat(asked[0]).as("a connection made anew asks anew").isGreaterThan(afterOpening);
        }
    }
}
