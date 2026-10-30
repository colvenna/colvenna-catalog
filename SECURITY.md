# Security

## Supported versions

Colvenna Catalog is in development toward 1.0, and no release candidate has been accepted for release yet. Security fixes are made on `main` and ship in the next release candidate. A candidate never changes once made (its tag, image and chart never move; see [Releasing](docs/development.md#releasing)), so an earlier candidate does not get the fix. Before 1.0, a new version does not guarantee an upgrade from, or a rollback to, an older one ([1.0 design](docs/design/v1.0-design.md), section 3, in Chinese).

How the server authenticates, authorizes, audits and issues per-table credentials is in the [LLD](docs/design/colvenna-catalog-lld.md) (in Chinese). The security scans a release depends on, their baseline and the findings left visible are in [docs/security/scans.md](docs/security/scans.md).

## Reporting a vulnerability

Do not put tokens, passwords, keys, cloud credentials, deployment values (account IDs, domains, ARNs, internal addresses) or exploit details in an ordinary issue or pull request.

For a confidential report, use GitHub's **Report a vulnerability** feature, on the repository's Security tab, when it is enabled for this repository. If it is not, contact the repository owner through an existing private channel before you share any detail. Name the deployment form (EKS or serverless), the release candidate or commit, the endpoint or component affected, and the steps to reproduce.

## How a report is handled

- The maintainers acknowledge the report, reproduce it, and agree with the reporter when it is disclosed. No response time is guaranteed. Please do not disclose a vulnerability before its fix is in a release candidate, or before a date agreed with the maintainers.
- The fix is delivered as any change is ([CONTRIBUTING.md](CONTRIBUTING.md)): its own issue and pull request, review, and CI on the exact commit merged. Until the fix is in a release candidate, they describe the problem without exploit details.
- The next release candidate's security record, `docs/security/<candidate>.md`, which its release notes link, names the fix.

## Security model in brief

- **No anonymous entry.** Every route of the API port, the Paimon REST API and the admin API alike, requires a bearer token the server issued. The server authenticates a request before it reads the body or resolves the route, so an unauthenticated request gets 401, or a 503 or 400 from the few checks that need no route, and learns nothing about which routes exist. The health endpoints listen only on the loopback interface ([LLD, 部署形态](docs/design/colvenna-catalog-lld.md#部署形态)). The installer's smoke check confirms that a request without a token is refused (in the serverless form, one with a forged token too), and on EKS that a namespace without the client label cannot connect.
- **TLS.** On EKS the server terminates TLS itself, with a certificate from the install's private CA, and refuses to start without one; plaintext is for local development and tests only. In the serverless form, API Gateway terminates TLS, and the server listens in plaintext on 127.0.0.1 only, which it allows only in a Lambda execution environment.
- **Tokens.** Tokens are opaque: `colvenna_` and 32 random bytes, returned once at issue. The server stores only their SHA-256, with the principal, the expiry and a revocation flag. Tokens expire (90 days by default, at most 400) and can be revoked. A revocation, or a disabled principal, takes effect on every replica within the authentication cache's lifetime (`COLVENNA_AUTH_CACHE_TTL`, 2 seconds by default) ([LLD, 认证与审计](docs/design/colvenna-catalog-lld.md#认证与审计)).
- **Grants and audit.** A principal without grants sees nothing, and an admin principal has no implicit access to the catalog's data. Grants give `READ`, `WRITE` or `MANAGE` on the catalog, a database or one object; a `DENY` overrides every `ALLOW`; each grant names its purpose, approver, approval and expiry. An action with no authorization rule is refused. Requests on catalog objects, allowed and denied, credential issues and admin changes are audited, and the audit can be queried ([LLD, 授权](docs/design/colvenna-catalog-lld.md#授权)).
- **Per-table credentials.** With per-table credentials on (`COLVENNA_DATA_TOKENS=aws-sts`, the installer's default), engines need no S3 access of their own. The server issues STS credentials whose session policy covers one table's prefix, valid for 3 hours by default. A principal with `READ` only gets read-only credentials, which can write only the table's `consumer/` directory, where a streaming read records its progress ([LLD, 按表数据凭证](docs/design/colvenna-catalog-lld.md#按表数据凭证), [授权](docs/design/colvenna-catalog-lld.md#授权)).
- **The EKS form's network.** The Service is a ClusterIP, and NetworkPolicy admits only namespaces labelled `colvenna-catalog/client=true`; the cluster must enforce NetworkPolicy. The optional internal Network Load Balancer admits only the CIDRs the install lists.
- **The serverless form's entry.** A private REST API that only the VPC's `execute-api` endpoints reach (its resource policy denies every other source), a Lambda authorizer that checks the bearer token before the server is called, and the server's own check behind it. No Lambda function URL is ever used: when it synthesizes, the CDK app's guard refuses function URLs, HTTP and WebSocket APIs, REST APIs that are not private, methods without authorization and internet-facing load balancers. The functions sign in to PostgreSQL with RDS IAM tokens, so no database password is stored in them ([LLD, 部署](docs/design/colvenna-catalog-lld.md#部署)).
- **Secrets.** The installer writes the first admin token straight into a Kubernetes Secret (EKS) or a Secrets Manager secret (serverless) and never prints it. An install's own values live only in its uncommitted configuration, `deploy/config/<env>.env`.

## Known limits

These are the security limits the LLD records, with the details.

- `/metrics` (port 9100, which the chart opens) does not authenticate. It holds no catalog data, and NetworkPolicy admits only namespaces labelled `colvenna-catalog/monitoring=true`.
- After a token is revoked or a grant removed, the per-table credentials already issued stay valid until they expire (3 hours by default). Revoking the data role's active sessions in IAM ends all of them at once, and interrupts every engine.
- Per-table credentials bound object paths, not columns or rows. A column-limited grant is enforced by the Paimon client (`query-auth.enabled=true`), not by an isolation boundary: an engine with the table's credentials can read every column's files. Row filters, column masks and end users' permissions are the engines' to enforce.
- Without per-table credentials, the table files are bounded only by the engines' own IAM roles, and a `READ` grant does not stop an S3 write.
- The server sees service identities (principals), not end users: the users of one engine reach it as one principal, and attributing an access to a user needs the engine's own audit log.
- A writer that can write to S3 directly can bypass the catalog's commits; such a snapshot is found only afterwards, when the table is fenced.
- Whoever can invoke the serverless form's maintenance function can issue an admin token, and whoever can invoke its ops function can forward requests through it (still with a token), as whoever can exec into a catalog pod on EKS. Only administrators should hold these permissions. An account administrator with wildcard permissions can also obtain per-table credentials through the identity pool, as they can read and write the warehouse bucket directly.
