package com.colvenna.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.catalog.TablePurges;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Parsing and validation of the settings the serverless form changes. */
class ServerConfigTest {

    private static Map<String, String> minimal() {
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_JDBC_URL", "jdbc:postgresql://localhost/x");
        env.put("COLVENNA_WAREHOUSE_NAME", "colvenna");
        env.put("COLVENNA_WAREHOUSE_ROOT", "file:///tmp/w");
        return env;
    }

    @Test
    void timeoutsDefaultToTheEksFormsValues() {
        ServerConfig.Timeouts timeouts = ServerConfig.fromEnvironment(minimal()).timeouts();
        assertThat(timeouts.lockTimeout()).isEqualTo(Duration.ofSeconds(30));
        assertThat(timeouts.s3CallTimeout()).isEqualTo(Duration.ofSeconds(30));
        assertThat(timeouts.s3CallAttemptTimeout()).isEqualTo(Duration.ofSeconds(10));
    }

    @Test
    void timeoutsCanBeSetBelowTheGatewaysTimeout() {
        Map<String, String> env = minimal();
        env.put("COLVENNA_JDBC_LOCK_TIMEOUT", "PT10S");
        env.put("COLVENNA_S3_CALL_TIMEOUT", "PT8S");
        env.put("COLVENNA_S3_CALL_ATTEMPT_TIMEOUT", "PT3S");
        ServerConfig.Timeouts timeouts = ServerConfig.fromEnvironment(env).timeouts();
        assertThat(timeouts)
                .isEqualTo(
                        new ServerConfig.Timeouts(
                                Duration.ofSeconds(10),
                                Duration.ofSeconds(8),
                                Duration.ofSeconds(3)));
    }

    @Test
    void timeoutsThatBreakAnInvariantAreRefused() {
        assertRefused("COLVENNA_JDBC_LOCK_TIMEOUT", "PT0S", "at least 1 ms");
        assertRefused("COLVENNA_JDBC_LOCK_TIMEOUT", "10s", "ISO-8601");
        assertRefused("COLVENNA_S3_CALL_TIMEOUT", "PT5M", "shorter than PT5M");
        assertRefused(
                "COLVENNA_S3_CALL_TIMEOUT", "PT5S", "not longer than COLVENNA_S3_CALL_TIMEOUT");
        assertRefused("COLVENNA_S3_CALL_ATTEMPT_TIMEOUT", "PT0S", "must be positive");
    }

    @Test
    void inLambdaTheTimeoutsDefaultBelowTheGatewaysTimeout() {
        Map<String, String> env = lambda();
        ServerConfig.Timeouts timeouts = ServerConfig.fromEnvironment(env).timeouts();
        assertThat(timeouts).isEqualTo(ServerConfig.Timeouts.LAMBDA_DEFAULTS);
        assertThat(timeouts.lockTimeout()).isEqualTo(Duration.ofSeconds(10));
        assertThat(timeouts.s3CallTimeout()).isEqualTo(Duration.ofSeconds(8));
        assertThat(timeouts.s3CallAttemptTimeout()).isEqualTo(Duration.ofSeconds(3));
        // The EKS form keeps its 30 seconds, which Lambda would refuse.
        Map<String, String> eks = minimal();
        eks.put("COLVENNA_JDBC_LOCK_TIMEOUT", "PT30S");
        assertThat(ServerConfig.fromEnvironment(eks).timeouts().lockTimeout())
                .isEqualTo(Duration.ofSeconds(30));
    }

    @Test
    void inLambdaWaitsOfTheGatewaysTimeoutOrLongerAreRefused() {
        Map<String, String> lock = lambda();
        lock.put("COLVENNA_JDBC_LOCK_TIMEOUT", "PT29S");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(lock))
                .hasMessageContaining("COLVENNA_JDBC_LOCK_TIMEOUT is PT29S")
                .hasMessageContaining("PT29S integration timeout");
        Map<String, String> s3 = lambda();
        s3.put("COLVENNA_S3_CALL_TIMEOUT", "PT30S");
        s3.put("COLVENNA_S3_CALL_ATTEMPT_TIMEOUT", "PT10S");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(s3))
                .hasMessageContaining("COLVENNA_S3_CALL_TIMEOUT is PT30S");

        // Each under 29 s, but 10 + 12 + 8 = 30 in sequence.
        Map<String, String> sum = lambda();
        sum.put("COLVENNA_JDBC_LOCK_TIMEOUT", "PT12S");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(sum))
                .hasMessageContaining("connection wait (PT10S, fixed)")
                .hasMessageContaining("COLVENNA_JDBC_LOCK_TIMEOUT (PT12S)")
                .hasMessageContaining("COLVENNA_S3_CALL_TIMEOUT (PT8S) = PT30S");
        sum.put("COLVENNA_S3_CALL_TIMEOUT", "PT6S");
        assertThat(ServerConfig.fromEnvironment(sum).timeouts().lockTimeout())
                .isEqualTo(Duration.ofSeconds(12));
    }

    @Test
    void booleanAndDurationSettingsAreParsedStrictly() {
        assertRefused("COLVENNA_ALLOW_PLAINTEXT", "yes", "must be true or false");
        assertRefused("COLVENNA_AUTH_CACHE_TTL", "2s", "ISO-8601");
        Map<String, String> env = minimal();
        env.put("COLVENNA_ALLOW_PLAINTEXT", "TRUE");
        env.put("COLVENNA_AUTH_CACHE_TTL", "PT5S");
        ServerConfig config = ServerConfig.fromEnvironment(env);
        assertThat(config.plaintextAllowed()).isTrue();
        assertThat(config.authCacheTtl()).isEqualTo(Duration.ofSeconds(5));
    }

    private static Map<String, String> lambda() {
        Map<String, String> env = minimal();
        env.put("AWS_LAMBDA_RUNTIME_API", "127.0.0.1:9001");
        return env;
    }

    @Test
    void responsesAreUnlimitedUnlessALimitIsSet() {
        assertThat(ServerConfig.fromEnvironment(minimal()).maxResponseBytes()).isZero();
        Map<String, String> env = minimal();
        env.put("COLVENNA_MAX_RESPONSE_BYTES", "4194304");
        assertThat(ServerConfig.fromEnvironment(env).maxResponseBytes()).isEqualTo(4_194_304);
        assertRefused("COLVENNA_MAX_RESPONSE_BYTES", "-1", "0 (no limit)");
    }

    @Test
    void stopsWithADrainAndATimeoutUnlessInLambda() {
        ServerConfig.Shutdown eks = ServerConfig.fromEnvironment(minimal()).shutdown();
        assertThat(eks)
                .isEqualTo(
                        new ServerConfig.Shutdown(Duration.ofSeconds(5), Duration.ofSeconds(20)));
        // Within Kubernetes' default grace period of 30 seconds, even without the chart's.
        assertThat(eks.drain().plus(eks.timeout())).isLessThan(Duration.ofSeconds(30));
        assertThat(ServerConfig.fromEnvironment(lambda()).shutdown())
                .isEqualTo(new ServerConfig.Shutdown(Duration.ZERO, Duration.ZERO));
        Map<String, String> env = minimal();
        env.put("COLVENNA_SHUTDOWN_DRAIN", "PT0S");
        env.put("COLVENNA_SHUTDOWN_TIMEOUT", "PT1.5S");
        ServerConfig.Shutdown set = ServerConfig.fromEnvironment(env).shutdown();
        assertThat(set.drain()).isZero();
        // HttpServer.stop takes whole seconds: a part of one counts as one.
        assertThat(set.timeoutSeconds()).isEqualTo(2);
        assertThat(eks.timeoutSeconds()).isEqualTo(20);
        assertRefused("COLVENNA_SHUTDOWN_DRAIN", "PT-1S", "PT0S or positive");
        assertRefused("COLVENNA_SHUTDOWN_TIMEOUT", "PT-1S", "PT0S or positive");
        assertRefused("COLVENNA_SHUTDOWN_TIMEOUT", "20", "ISO-8601");
    }

    @Test
    void dropsPurgeInlineAndChecksAnswerWholeUnlessInLambda() {
        ServerConfig eks = ServerConfig.fromEnvironment(minimal());
        assertThat(eks.dropPurge()).isEqualTo(TablePurges.Mode.INLINE);
        assertThat(eks.requestWorkTime()).isZero();
        ServerConfig lambda = ServerConfig.fromEnvironment(lambda());
        assertThat(lambda.dropPurge()).isEqualTo(TablePurges.Mode.DEFERRED);
        assertThat(lambda.requestWorkTime()).isEqualTo(Duration.ofSeconds(8));
        Map<String, String> deferred = minimal();
        deferred.put("COLVENNA_DROP_PURGE", "deferred");
        deferred.put("COLVENNA_REQUEST_WORK_TIME", "PT1S");
        ServerConfig config = ServerConfig.fromEnvironment(deferred);
        assertThat(config.dropPurge()).isEqualTo(TablePurges.Mode.DEFERRED);
        assertThat(config.requestWorkTime()).isEqualTo(Duration.ofSeconds(1));
        assertRefused("COLVENNA_DROP_PURGE", "later", "inline or deferred");
        assertRefused("COLVENNA_REQUEST_WORK_TIME", "PT-1S", "PT0S (no limit)");
    }

    @Test
    void inLambdaADropNeverWaitsOnFilesAndACheckAnswersWithinTheGatewaysTimeout() {
        Map<String, String> inline = lambda();
        inline.put("COLVENNA_DROP_PURGE", "inline");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(inline))
                .hasMessageContaining("COLVENNA_DROP_PURGE must be deferred");
        for (String page : new String[] {"PT0S", "PT19S"}) {
            Map<String, String> env = lambda();
            env.put("COLVENNA_REQUEST_WORK_TIME", page);
            assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                    .as(page)
                    .hasMessageContaining("COLVENNA_REQUEST_WORK_TIME must be positive");
        }
        Map<String, String> env = lambda();
        env.put("COLVENNA_REQUEST_WORK_TIME", "PT18S");
        assertThat(ServerConfig.fromEnvironment(env).requestWorkTime())
                .isEqualTo(Duration.ofSeconds(18));
    }

    private static void assertRefused(String name, String value, String message) {
        Map<String, String> env = minimal();
        env.put(name, value);
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .as(name + "=" + value)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(message);
    }
}
