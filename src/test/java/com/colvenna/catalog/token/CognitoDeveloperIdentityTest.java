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
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.apache.paimon.TableType;
import org.apache.paimon.schema.Schema;
import org.apache.paimon.types.DataTypes;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.auth.credentials.AnonymousCredentialsProvider;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.cognitoidentity.CognitoIdentityClient;
import software.amazon.awssdk.services.cognitoidentity.model.GetOpenIdTokenForDeveloperIdentityRequest;
import software.amazon.awssdk.services.cognitoidentity.model.GetOpenIdTokenForDeveloperIdentityResponse;
import software.amazon.awssdk.services.cognitoidentity.model.NotAuthorizedException;
import software.amazon.awssdk.services.cognitoidentity.model.TooManyRequestsException;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.AssumeRoleWithWebIdentityRequest;
import software.amazon.awssdk.services.sts.model.AssumeRoleWithWebIdentityResponse;
import software.amazon.awssdk.services.sts.model.Credentials;
import software.amazon.awssdk.services.sts.model.InvalidIdentityTokenException;
import software.amazon.awssdk.services.sts.model.StsException;

/**
 * Per-table credentials from a Cognito developer identity, against stubbed SDK clients: what is
 * asked of Cognito and STS, for how long, and what happens when either refuses.
 */
class CognitoDeveloperIdentityTest {

    private static final String POOL = "us-west-2:0f2c9a1e-5b6d-4c8e-9a7b-3d2e1f0a9b8c";
    private static final String PROVIDER = "catalog.colvenna.example.com";
    private static final String ROLE = "arn:aws:iam::111122223333:role/colvenna-catalog-data";
    private static final String TABLE_PATH = "s3://bucket/warehouse/ods.db/orders";

    private final MutableClock clock = new MutableClock();
    private final List<GetOpenIdTokenForDeveloperIdentityRequest> cognitoCalls = new ArrayList<>();
    private final List<AssumeRoleWithWebIdentityRequest> stsCalls = new ArrayList<>();
    private RuntimeException cognitoFailure;
    private RuntimeException stsFailure;

    private final CognitoIdentityClient cognito =
            new CognitoIdentityClient() {
                @Override
                public GetOpenIdTokenForDeveloperIdentityResponse
                        getOpenIdTokenForDeveloperIdentity(
                                GetOpenIdTokenForDeveloperIdentityRequest request) {
                    cognitoCalls.add(request);
                    if (cognitoFailure != null) {
                        throw cognitoFailure;
                    }
                    return GetOpenIdTokenForDeveloperIdentityResponse.builder()
                            .identityId("us-west-2:identity-" + cognitoCalls.size())
                            .token("openid-token-" + cognitoCalls.size())
                            .build();
                }

                @Override
                public String serviceName() {
                    return "cognito-identity";
                }

                @Override
                public void close() {}
            };

    private final StsClient sts =
            new StsClient() {
                @Override
                public AssumeRoleWithWebIdentityResponse assumeRoleWithWebIdentity(
                        AssumeRoleWithWebIdentityRequest request) {
                    stsCalls.add(request);
                    if (stsFailure != null) {
                        throw stsFailure;
                    }
                    return AssumeRoleWithWebIdentityResponse.builder()
                            .credentials(
                                    Credentials.builder()
                                            .accessKeyId("ASIA" + stsCalls.size())
                                            .secretAccessKey("secret" + stsCalls.size())
                                            .sessionToken("session" + stsCalls.size())
                                            .expiration(
                                                    clock.instant()
                                                            .plusSeconds(request.durationSeconds()))
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

    private AwsStsTokenIssuer issuer(Duration duration) {
        return new AwsStsTokenIssuer(
                ROLE,
                "us-west-2",
                duration,
                new CognitoDeveloperIdentity(cognito, sts, POOL, PROVIDER),
                clock);
    }

    @Test
    void theDeveloperIdentitysTokenAssumesTheDataRoleWithTheTablePolicy() throws Exception {
        DataTokenIssuer.Token token =
                issuer(Duration.ofHours(3))
                        .issue(as("flink-ods"), table(), SessionPolicy.Access.READ_WRITE);

        assertThat(cognitoCalls).hasSize(1);
        assertThat(cognitoCalls.get(0).identityPoolId()).isEqualTo(POOL);
        // One developer identity per principal: its login is the STS session name.
        assertThat(cognitoCalls.get(0).logins())
                .containsExactly(Map.entry(PROVIDER, "colvenna-flink-ods"));

        assertThat(stsCalls).hasSize(1);
        AssumeRoleWithWebIdentityRequest assumed = stsCalls.get(0);
        assertThat(assumed.roleArn()).isEqualTo(ROLE);
        assertThat(assumed.roleSessionName()).isEqualTo("colvenna-flink-ods");
        assertThat(assumed.webIdentityToken()).isEqualTo("openid-token-1");
        assertThat(assumed.policy())
                .isEqualTo(
                        SessionPolicy.json(
                                SessionPolicy.Location.of(TABLE_PATH),
                                SessionPolicy.Access.READ_WRITE));
        assertThat(assumed.durationSeconds()).isEqualTo(3 * 3600);

        assertThat(token.properties())
                .containsEntry("fs.s3a.access.key", "ASIA1")
                .containsEntry("fs.s3a.session.token", "session1")
                .containsEntry("s3.region", "us-west-2");
        assertThat(token.detail()).containsEntry("roleSessionName", "colvenna-flink-ods");
    }

    @Test
    void sessionsLastTheConfiguredDurationWithinTheRolesLimits() throws Exception {
        issuer(Duration.ofHours(12)).issue(as("spark"), table(), SessionPolicy.Access.READ);
        assertThat(stsCalls.get(0).durationSeconds()).isEqualTo(43_200);
        assertThat(stsCalls.get(0).policy())
                .isEqualTo(
                        SessionPolicy.json(
                                SessionPolicy.Location.of(TABLE_PATH), SessionPolicy.Access.READ));
        assertThatThrownBy(() -> issuer(Duration.ofHours(1)))
                .hasMessageContaining("between PT1H30M and PT12H");
        assertThatThrownBy(() -> issuer(Duration.ofHours(13)))
                .hasMessageContaining("between PT1H30M and PT12H");
    }

    @Test
    void aRefusalByCognitoReachesNoStsAndCachesNothing() throws Exception {
        AwsStsTokenIssuer issuer = issuer(Duration.ofHours(3));
        cognitoFailure =
                NotAuthorizedException.builder()
                        .message("not allowed on the pool")
                        .statusCode(400)
                        .build();
        assertThatThrownBy(() -> issuer.issue(as("flink-ods"), table(), SessionPolicy.Access.READ))
                .isInstanceOf(NotAuthorizedException.class);
        assertThat(stsCalls).isEmpty();
        cognitoFailure = null;
        issuer.issue(as("flink-ods"), table(), SessionPolicy.Access.READ);
        assertThat(cognitoCalls).hasSize(2);
        assertThat(stsCalls).hasSize(1);
    }

    @Test
    void aRefusedTokenFailsTheIssue() {
        stsFailure =
                InvalidIdentityTokenException.builder()
                        .message("Token is not from a supported provider")
                        .statusCode(400)
                        .build();
        assertThatThrownBy(
                        () ->
                                issuer(Duration.ofHours(3))
                                        .issue(as("flink-ods"), table(), SessionPolicy.Access.READ))
                .isInstanceOf(InvalidIdentityTokenException.class);
    }

    @Test
    void throttlingIsARetryable503() {
        cognitoFailure =
                TooManyRequestsException.builder().message("Rate exceeded").statusCode(400).build();
        assertUnavailable("Amazon Cognito");
        cognitoFailure = null;
        stsFailure =
                (RuntimeException)
                        StsException.builder()
                                .awsErrorDetails(
                                        AwsErrorDetails.builder()
                                                .errorCode("Throttling")
                                                .errorMessage("Rate exceeded")
                                                .build())
                                .statusCode(400)
                                .build();
        assertUnavailable("AWS STS");
    }

    private void assertUnavailable(String service) {
        assertThatThrownBy(
                        () ->
                                issuer(Duration.ofHours(3))
                                        .issue(as("flink-ods"), table(), SessionPolicy.Access.READ))
                .isInstanceOfSatisfying(
                        ApiException.class, e -> assertThat(e.status()).isEqualTo(503))
                .hasMessageContaining(service)
                .hasCauseInstanceOf(AwsServiceException.class);
    }

    @Test
    void theSettingSelectsTheSourceAndItsCallsAreSigned() {
        Map<String, String> env = new HashMap<>();
        env.put("COLVENNA_JDBC_URL", "jdbc:postgresql://localhost/x");
        env.put("COLVENNA_WAREHOUSE_NAME", "colvenna");
        env.put("COLVENNA_WAREHOUSE_ROOT", "s3://bucket/warehouse");
        env.put("COLVENNA_DATA_TOKEN_MODE", "aws-sts");
        env.put("COLVENNA_DATA_TOKEN_ROLE_ARN", ROLE);
        env.put("COLVENNA_DATA_TOKEN_REGION", "us-west-2");
        assertThat(ServerConfig.fromEnvironment(env).dataTokens().webIdentity())
                .isEqualTo(ServerConfig.DataTokens.WebIdentity.TOKEN_FILE);

        env.put("COLVENNA_DATA_TOKEN_WEB_IDENTITY", "cognito");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID");
        env.put("COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID", "not-a-pool");
        env.put("COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER", PROVIDER);
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("<region>:<id>");
        env.put("COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID", POOL);
        ServerConfig.DataTokens settings = ServerConfig.fromEnvironment(env).dataTokens();
        assertThat(settings.webIdentity()).isEqualTo(ServerConfig.DataTokens.WebIdentity.COGNITO);
        assertThat(settings.cognitoRegion()).isEqualTo("us-west-2");
        // Built without AWS_WEB_IDENTITY_TOKEN_FILE and without calling AWS.
        assertThat(AwsStsTokenIssuer.create(settings, clock)).isNotNull();

        CognitoDeveloperIdentity gateway =
                CognitoDeveloperIdentity.create(POOL, PROVIDER, "us-west-2", "us-west-2");
        assertThat(gateway.cognito().serviceClientConfiguration().region())
                .isEqualTo(Region.US_WEST_2);
        assertThat(gateway.cognito().serviceClientConfiguration().credentialsProvider())
                .isNotInstanceOf(AnonymousCredentialsProvider.class);

        env.put("COLVENNA_DATA_TOKEN_WEB_IDENTITY", "anonymous");
        assertThatThrownBy(() -> ServerConfig.fromEnvironment(env))
                .hasMessageContaining("token-file or cognito");
    }

    private static TableRecord table() {
        return new TableRecord(
                UUID.randomUUID(),
                "ods",
                "orders",
                TABLE_PATH,
                false,
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
}
