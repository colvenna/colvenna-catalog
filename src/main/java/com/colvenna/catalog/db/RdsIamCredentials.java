package com.colvenna.catalog.db;

import java.util.Objects;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.rds.RdsUtilities;

/**
 * RDS IAM database authentication ({@code COLVENNA_JDBC_AUTH=iam}, the serverless form's): the
 * password of each new connection is an authentication token presigned for the database's host,
 * port and user with the server's own AWS credentials. Signing is local (SigV4, no call to AWS); a
 * token is good for 15 minutes and only for opening a connection, which then lives on without it.
 * There is no password to store anywhere, and none in the server's environment.
 *
 * <p>The token is a credential, so the JDBC URL must name one host and verify its certificate and
 * name ({@code sslmode=verify-full}): RDS requires TLS for IAM authentication anyway.
 */
public final class RdsIamCredentials implements Db.Credentials {

    /** Makes a token for a host, port and user. */
    @FunctionalInterface
    public interface Tokens {
        String token(String host, int port, String user);
    }

    private static final Pattern URL =
            Pattern.compile(
                    "^jdbc:postgresql://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?/[^?/]+(\\?.*)?$");
    private static final Pattern VERIFY_FULL = Pattern.compile("[?&]sslmode=verify-full(&|$)");

    private final String host;
    private final int port;
    private final String user;
    private final Tokens tokens;

    public RdsIamCredentials(String jdbcUrl, String user, Tokens tokens) {
        Matcher url = check(jdbcUrl);
        this.host = url.group(1);
        this.port = url.group(2) == null ? 5432 : Integer.parseInt(url.group(2));
        this.user = Objects.requireNonNull(user, "COLVENNA_JDBC_USER");
        this.tokens = Objects.requireNonNull(tokens, "tokens");
    }

    /**
     * Tokens from the AWS SDK's {@link RdsUtilities}, signed with the default credential chain: the
     * execution role's credentials are in environment variables in an initialized Lambda execution
     * environment, and come from the container credentials endpoint in one restored from a
     * SnapStart snapshot.
     */
    public static RdsIamCredentials create(String jdbcUrl, String user, Region region) {
        return create(jdbcUrl, user, region, DefaultCredentialsProvider.builder().build());
    }

    /** As above, signed with the given credentials. */
    public static RdsIamCredentials create(
            String jdbcUrl, String user, Region region, AwsCredentialsProvider credentials) {
        RdsUtilities rds =
                RdsUtilities.builder().region(region).credentialsProvider(credentials).build();
        return new RdsIamCredentials(
                jdbcUrl,
                user,
                (host, port, name) ->
                        rds.generateAuthenticationToken(
                                b -> b.hostname(host).port(port).username(name)));
    }

    /** Refuses a JDBC URL a token must not be sent to; returns its parts otherwise. */
    public static Matcher check(String jdbcUrl) {
        Matcher url = URL.matcher(jdbcUrl);
        if (!url.matches()) {
            throw new IllegalArgumentException(
                    "COLVENNA_JDBC_AUTH=iam needs COLVENNA_JDBC_URL to name one host,"
                            + " jdbc:postgresql://<host>[:<port>]/<database>[?...]");
        }
        if (!VERIFY_FULL.matcher(jdbcUrl).find()) {
            throw new IllegalArgumentException(
                    "COLVENNA_JDBC_AUTH=iam needs sslmode=verify-full in COLVENNA_JDBC_URL: the"
                            + " token is a credential, sent only to a database whose certificate"
                            + " and name are verified.");
        }
        return url;
    }

    String host() {
        return host;
    }

    int port() {
        return port;
    }

    @Override
    public String user() {
        return user;
    }

    @Override
    public String password() {
        return tokens.token(host, port, user);
    }

    @Override
    public boolean fixed() {
        return false;
    }

    @Override
    public String toString() {
        return "RdsIamCredentials[" + user + "@" + host + ":" + port + "]";
    }
}
