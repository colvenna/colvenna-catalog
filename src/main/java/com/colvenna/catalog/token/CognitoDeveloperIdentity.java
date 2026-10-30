package com.colvenna.catalog.token;

import java.time.Duration;
import java.util.Map;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.cognitoidentity.CognitoIdentityClient;
import software.amazon.awssdk.services.cognitoidentity.model.TooManyRequestsException;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.Credentials;

/**
 * The serverless form's web identity: a Cognito developer identity in place of the IRSA token.
 *
 * <p>A Lambda execution role's credentials are themselves a role session, and a role assumed from
 * one is chained and capped at an hour. So for each credential the catalog asks Cognito for an
 * OpenID token of a developer identity in its identity pool ({@code
 * GetOpenIdTokenForDeveloperIdentity}, signed with the execution role's own credentials), and then
 * assumes the data role with that token and the table's session policy ({@code
 * AssumeRoleWithWebIdentity}), for the configured 1.5 to 12 hours.
 *
 * <p>The login is the STS role session name: one Cognito identity per principal, so the identity id
 * in CloudTrail names the principal as the session name does. Nothing here is anonymous: the
 * identity pool, with no roles and no unauthenticated identities, hands out nothing itself, and
 * only a caller allowed {@code cognito-identity:GetOpenIdTokenForDeveloperIdentity} on it gets a
 * token.
 */
final class CognitoDeveloperIdentity implements AwsStsTokenIssuer.Gateway {

    private final CognitoIdentityClient cognito;
    private final StsClient sts;
    private final String identityPoolId;
    private final String developerProvider;

    CognitoDeveloperIdentity(
            CognitoIdentityClient cognito,
            StsClient sts,
            String identityPoolId,
            String developerProvider) {
        this.cognito = cognito;
        this.sts = sts;
        this.identityPoolId = identityPoolId;
        this.developerProvider = developerProvider;
    }

    /**
     * Clients for the pool's region and for STS in {@code stsRegion}; they live as long as the
     * process, like the issuer. Cognito is called with the default credential chain (the execution
     * role); {@code AssumeRoleWithWebIdentity} takes no credentials.
     */
    static CognitoDeveloperIdentity create(
            String identityPoolId, String developerProvider, String poolRegion, String stsRegion) {
        return new CognitoDeveloperIdentity(
                CognitoIdentityClient.builder().region(Region.of(poolRegion)).build(),
                AwsStsTokenIssuer.anonymousSts(stsRegion),
                identityPoolId,
                developerProvider);
    }

    CognitoIdentityClient cognito() {
        return cognito;
    }

    @Override
    public Credentials assume(String roleArn, String sessionName, String policy, Duration duration)
            throws Exception {
        String token;
        try {
            token =
                    cognito.getOpenIdTokenForDeveloperIdentity(
                                    r ->
                                            r.identityPoolId(identityPoolId)
                                                    .logins(Map.of(developerProvider, sessionName)))
                            .token();
        } catch (AwsServiceException e) {
            boolean throttled = e instanceof TooManyRequestsException || e.isThrottlingException();
            throw throttled ? AwsStsTokenIssuer.throttled("Amazon Cognito", e) : e;
        }
        return AwsStsTokenIssuer.assumeRoleWithWebIdentity(
                sts, roleArn, sessionName, token, policy, duration);
    }
}
