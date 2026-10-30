package com.colvenna.catalog.token;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.colvenna.catalog.ServerConfig;
import com.colvenna.catalog.auth.Principal;
import com.colvenna.catalog.catalog.Records.TableRecord;
import com.colvenna.catalog.catalog.Records.TableState;
import com.colvenna.catalog.catalog.RequestContext;
import com.colvenna.catalog.http.ApiException;
import com.colvenna.catalog.testing.ServerTestBase.MutableClock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.apache.paimon.TableType;
import org.apache.paimon.rest.RESTApi;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.shade.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.AssumeRoleWithWebIdentityRequest;
import software.amazon.awssdk.services.sts.model.AssumeRoleWithWebIdentityResponse;
import software.amazon.awssdk.services.sts.model.Credentials;
import software.amazon.awssdk.services.sts.model.InvalidIdentityTokenException;
import software.amazon.awssdk.services.sts.model.StsException;

/** Session policy, key names, lifetime and caching of vended table credentials. */
class AwsStsTokenIssuerTest {

    private record Call(String role, String session, String policy, Duration duration) {}

    private final MutableClock clock = new MutableClock();
    private final List<Call> calls = new ArrayList<>();
    private final AwsStsTokenIssuer issuer =
            new AwsStsTokenIssuer(
                    "arn:aws:iam::111122223333:role/colvenna-lake-catalog-data",
                    "us-west-2",
                    Duration.ofHours(3),
                    (role, session, policy, duration) -> {
                        calls.add(new Call(role, session, policy, duration));
                        return Credentials.builder()
                                .accessKeyId("ASIA" + calls.size())
                                .secretAccessKey("secret" + calls.size())
                                .sessionToken("session" + calls.size())
                                .expiration(clock.instant().plus(duration))
                                .build();
                    },
                    clock);

    private static TableRecord table(String path, boolean external) {
        return new TableRecord(
                UUID.randomUUID(),
                "ods",
                "orders",
                path,
                external,
                TableType.TABLE,
                0,
                Schema.newBuilder().column("id", DataTypes.INT()).build(),
                TableState.ACTIVE,
                null,
                "etl",
                0,
                "etl",
                0,
                "etl");
    }

    private static RequestContext as(String principal) {
        return new RequestContext(new Principal(principal, false, UUID.randomUUID()), "req-1");
    }

    @Test
    void policyIsLimitedToTheTableRoot() throws Exception {
        TableRecord orders = table("s3://bucket/warehouse/ods.db/orders", false);
        issuer.issue(as("flink-ods"), orders, SessionPolicy.Access.READ_WRITE);
        JsonNode policy = RESTApi.OBJECT_MAPPER.readTree(calls.get(0).policy());
        String text = policy.toString();
        assertThat(text).contains("arn:aws:s3:::bucket/warehouse/ods.db/orders/*");
        assertThat(text).doesNotContain("\"Resource\":\"arn:aws:s3:::bucket/*\"");
        assertThat(text).doesNotContain("s3:*");
        JsonNode listing = statement(policy, "TableListing");
        assertThat(listing.at("/Condition/StringLike/s3:prefix").toString())
                .isEqualTo("[\"warehouse/ods.db/orders/*\"]");
        assertThat(statement(policy, "TableObjects").get("Resource").toString())
                .isEqualTo(
                        "[\"arn:aws:s3:::bucket/warehouse/ods.db/orders\","
                                + "\"arn:aws:s3:::bucket/warehouse/ods.db/orders/*\"]");
    }

    @Test
    void readOnlyTokensWriteOnlyConsumerProgress() throws Exception {
        issuer.issue(
                as("doris"),
                table("s3://bucket/warehouse/ods.db/orders", false),
                SessionPolicy.Access.READ);
        JsonNode policy = RESTApi.OBJECT_MAPPER.readTree(calls.get(0).policy());
        assertThat(statement(policy, "TableObjects").get("Action").toString())
                .isEqualTo("[\"s3:GetObject\"]");
        JsonNode consumer = statement(policy, "ConsumerProgress");
        assertThat(consumer.get("Resource").toString())
                .isEqualTo(
                        "[\"arn:aws:s3:::bucket/warehouse/ods.db/orders/consumer/*\","
                                + "\"arn:aws:s3:::bucket/warehouse/ods.db/orders/branch/*/consumer/*\"]");
        assertThat(policy.get("Statement")).hasSize(4);
    }

    @Test
    void tokenCarriesTheKeysOfEveryReader() throws Exception {
        DataTokenIssuer.Token token =
                issuer.issue(
                        as("flink-ods"),
                        table("s3://bucket/warehouse/ods.db/orders", false),
                        SessionPolicy.Access.READ_WRITE);
        Map<String, String> p = token.properties();
        assertThat(p)
                .containsEntry("fs.s3a.access.key", "ASIA1")
                .containsEntry("fs.s3a.secret.key", "secret1")
                .containsEntry("fs.s3a.session.token", "session1")
                .containsEntry(
                        "fs.s3a.aws.credentials.provider",
                        "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider")
                .containsEntry("s3.access-key-id", "ASIA1")
                .containsEntry("s3.secret-access-key", "secret1")
                .containsEntry("s3.session-token", "session1")
                .containsEntry("s3.region", "us-west-2");
        assertThat(token.detail()).containsEntry("access", "READ_WRITE");
        assertThat(token.detail().toString()).doesNotContain("secret1").doesNotContain("session1");
    }

    @Test
    void sessionsOutliveThePaimonRefreshMargin() throws Exception {
        DataTokenIssuer.Token token =
                issuer.issue(
                        as("flink-ods"),
                        table("s3://bucket/warehouse/ods.db/orders", false),
                        SessionPolicy.Access.READ_WRITE);
        assertThat(calls.get(0).duration()).isEqualTo(Duration.ofHours(3));
        assertThat(token.expiresAtMillis() - clock.millis())
                .isGreaterThan(Duration.ofHours(1).toMillis());
        assertThatThrownBy(
                        () ->
                                new AwsStsTokenIssuer(
                                        "arn", "us-west-2", Duration.ofHours(1), null, clock))
                .hasMessageContaining("PT1H30M");
    }

    @Test
    void cachedTokenIsReusedOnlyWhileItHasMoreThanTheMarginLeft() throws Exception {
        TableRecord orders = table("s3://bucket/warehouse/ods.db/orders", false);
        DataTokenIssuer.Token first =
                issuer.issue(as("flink-ods"), orders, SessionPolicy.Access.READ_WRITE);
        DataTokenIssuer.Token again =
                issuer.issue(as("flink-ods"), orders, SessionPolicy.Access.READ_WRITE);
        assertThat(again.properties()).isSameAs(first.properties());
        assertThat(first.detail()).containsEntry("cached", false);
        assertThat(again.detail())
                .containsEntry("cached", true)
                .containsEntry("accessKeyId", first.detail().get("accessKeyId"));
        // 180 minutes minus 115 leaves 65, under the 70-minute reuse margin.
        clock.advance(Duration.ofMinutes(115));
        DataTokenIssuer.Token second =
                issuer.issue(as("flink-ods"), orders, SessionPolicy.Access.READ_WRITE);
        assertThat(second.properties()).isNotSameAs(first.properties());
        assertThat(calls).hasSize(2);
        // Another principal never gets a token cached for someone else.
        issuer.issue(as("spark-batch"), orders, SessionPolicy.Access.READ_WRITE);
        assertThat(calls).hasSize(3);
        assertThat(calls.get(2).session()).isEqualTo("colvenna-spark-batch");
    }

    @Test
    void externalAndNonS3TablesGetNoToken() {
        assertThatThrownBy(
                        () ->
                                issuer.issue(
                                        as("flink-ods"),
                                        table("s3://elsewhere/x", true),
                                        SessionPolicy.Access.READ))
                .hasMessageContaining("managed tables only");
        assertThatThrownBy(
                        () ->
                                issuer.issue(
                                        as("flink-ods"),
                                        table("file:///tmp/x", false),
                                        SessionPolicy.Access.READ))
                .hasMessageContaining("Not an S3 table path");
        assertThat(calls).isEmpty();
    }

    @Test
    void sessionNamesAreValidForStsAndNeverShared() {
        assertThat(AwsStsTokenIssuer.sessionName("spark-batch")).isEqualTo("colvenna-spark-batch");
        List<String> names =
                List.of(
                        AwsStsTokenIssuer.sessionName("team:etl"),
                        AwsStsTokenIssuer.sessionName("team-etl"),
                        AwsStsTokenIssuer.sessionName("x".repeat(100)),
                        AwsStsTokenIssuer.sessionName("x".repeat(101)));
        assertThat(names).doesNotHaveDuplicates();
        assertThat(names).allMatch(n -> n.length() <= 64 && n.matches("[\\w+=,.@-]+"));
        assertThat(names.get(0)).startsWith("colvenna-team-etl-");
        // The longest a principal's name is kept as it is, and one character more.
        String fits = "p".repeat(64 - "colvenna-".length());
        assertThat(AwsStsTokenIssuer.sessionName(fits)).isEqualTo("colvenna-" + fits);
        assertThat(AwsStsTokenIssuer.sessionName(fits + "p"))
                .hasSize(64)
                .startsWith("colvenna-ppp");
    }

    @Test
    void tokensNeverPrintTheirSecrets() throws Exception {
        DataTokenIssuer.Token token =
                issuer.issue(
                        as("flink-ods"),
                        table("s3://bucket/warehouse/ods.db/orders", false),
                        SessionPolicy.Access.READ);
        assertThat(token.toString())
                .doesNotContain(token.properties().get("fs.s3a.secret.key"))
                .doesNotContain(token.properties().get("fs.s3a.session.token"));
    }

    @Test
    void awsStsSettingsNeedARoleAndARegion() {
        for (String[] missing :
                new String[][] {{" ", "us-west-2"}, {"arn:aws:iam::1:role/r", null}}) {
            assertThatThrownBy(
                            () ->
                                    new ServerConfig.DataTokens(
                                            ServerConfig.DataTokens.Mode.AWS_STS,
                                            missing[0],
                                            missing[1],
                                            Duration.ofHours(3)))
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void namesCannotWidenThePolicy() throws Exception {
        assertThat(SessionPolicy.Location.of("s3://bucket/warehouse/d$x.db/a?b#c"))
                .isEqualTo(new SessionPolicy.Location("bucket", "warehouse/d$x.db/a?b#c"));
        String text =
                SessionPolicy.json(
                        SessionPolicy.Location.of("s3://bucket/warehouse/d$x.db/*"),
                        SessionPolicy.Access.READ);
        assertThat(text)
                .contains("arn:aws:s3:::bucket/warehouse/d${$}x.db/${*}/*")
                .doesNotContain("d$x");
    }

    @Test
    void tablePathsAreReadAsStored() {
        assertThat(SessionPolicy.Location.of("S3A://bucket//warehouse/ods.db/orders/"))
                .isEqualTo(new SessionPolicy.Location("bucket", "warehouse/ods.db/orders"));
    }

    @Test
    void theTokenFileIsPresentedAndThrottlingIsARetryable503() throws Exception {
        java.nio.file.Path tokenFile = java.nio.file.Files.createTempFile("irsa", ".token");
        java.nio.file.Files.writeString(tokenFile, "irsa-token");
        List<AssumeRoleWithWebIdentityRequest> requests = new ArrayList<>();
        AtomicReference<RuntimeException> failure = new AtomicReference<>();
        StsClient sts =
                new StsClient() {
                    @Override
                    public AssumeRoleWithWebIdentityResponse assumeRoleWithWebIdentity(
                            AssumeRoleWithWebIdentityRequest request) {
                        requests.add(request);
                        if (failure.get() != null) {
                            throw failure.get();
                        }
                        return AssumeRoleWithWebIdentityResponse.builder()
                                .credentials(
                                        Credentials.builder()
                                                .accessKeyId("ASIA1")
                                                .secretAccessKey("secret1")
                                                .sessionToken("session1")
                                                .expiration(
                                                        clock.instant().plus(Duration.ofHours(3)))
                                                .build())
                                .build();
                    }

                    @Override
                    public String serviceName() {
                        return "sts";
                    }

                    @Override
                    public void close() {}
                };
        try {
            AwsStsTokenIssuer fromFile =
                    new AwsStsTokenIssuer(
                            "arn:aws:iam::111122223333:role/colvenna-lake-catalog-data",
                            "us-west-2",
                            Duration.ofHours(3),
                            AwsStsTokenIssuer.tokenFile(sts, tokenFile),
                            clock);
            TableRecord orders = table("s3://bucket/warehouse/ods.db/orders", false);
            fromFile.issue(as("flink-ods"), orders, SessionPolicy.Access.READ);
            assertThat(requests.get(0).webIdentityToken()).isEqualTo("irsa-token");
            assertThat(requests.get(0).policy())
                    .isEqualTo(
                            SessionPolicy.json(
                                    SessionPolicy.Location.of(orders.path()),
                                    SessionPolicy.Access.READ));
            assertThat(requests.get(0).durationSeconds()).isEqualTo(3 * 3600);

            failure.set(
                    (RuntimeException)
                            StsException.builder()
                                    .awsErrorDetails(
                                            AwsErrorDetails.builder()
                                                    .errorCode("Throttling")
                                                    .errorMessage("Rate exceeded")
                                                    .build())
                                    .statusCode(400)
                                    .build());
            assertThatThrownBy(
                            () ->
                                    fromFile.issue(
                                            as("spark-batch"), orders, SessionPolicy.Access.READ))
                    .isInstanceOfSatisfying(
                            ApiException.class, e -> assertThat(e.status()).isEqualTo(503))
                    .hasMessageContaining("AWS STS is throttling");
            // Any other refusal keeps its own error, which the server answers with 500.
            failure.set(
                    InvalidIdentityTokenException.builder()
                            .message("expired")
                            .statusCode(400)
                            .build());
            assertThatThrownBy(
                            () ->
                                    fromFile.issue(
                                            as("spark-batch"), orders, SessionPolicy.Access.READ))
                    .isInstanceOf(InvalidIdentityTokenException.class);
        } finally {
            java.nio.file.Files.delete(tokenFile);
        }
    }

    private static JsonNode statement(JsonNode policy, String sid) {
        for (JsonNode statement : policy.get("Statement")) {
            if (sid.equals(statement.get("Sid").asText())) {
                return statement;
            }
        }
        throw new AssertionError("No statement " + sid);
    }
}
