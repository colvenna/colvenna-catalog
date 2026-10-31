# The admin API

Colvenna Catalog is administered over HTTP, with no command-line tool of its own: principals (service identities), their tokens, catalog grants, the audit log, and the repair of single tables. The API is under `/colvenna/admin/v1`, on the same port as the Apache Paimon REST API that engines use. Its reference is the OpenAPI description [`docs/api/admin-v1.yaml`](../api/admin-v1.yaml), which a test (`AdminOpenApiTest`) holds to the server's routes, fields and responses.

Only an admin principal may call it, and every request needs its bearer token. The examples here use `curl` and `jq` and read the token from a file: it never appears on a command line, where other users of the host could see it in the process list, nor in your shell's history.

## Before you start

You need `curl` 7.76 or later, `jq`, and `kubectl` access to the cluster the catalog is installed in (in the serverless form, the AWS CLI instead). The examples assume the defaults of `install.sh` in the EKS form: namespace and release `colvenna-catalog`. Change `-n colvenna-catalog` and `svc/colvenna-catalog` if your configuration names them otherwise. In the serverless form only these first steps differ; see [the serverless form](#the-serverless-form).

### Keep the CA and the token in files

`install.sh` keeps the catalog's private CA certificate and the first admin's token in two Kubernetes Secrets. Copy both into a directory only you can read, and write a `curl` configuration file that sends the token. Copy only the `ca.crt` key of `colvenna-catalog-ca-key`: that Secret also holds the CA's private key.

```bash
umask 077
mkdir -p ~/.colvenna-admin && cd ~/.colvenna-admin
kubectl -n colvenna-catalog get secret colvenna-catalog-ca-key -o jsonpath='{.data.ca\.crt}' | base64 -d >ca.crt
kubectl -n colvenna-catalog get secret colvenna-catalog-admin-token -o jsonpath='{.data.token}' | base64 -d >admin.token
{
  printf 'header = "Authorization: Bearer %s"\n' "$(cat admin.token)"
  printf 'cacert = "%s"\n' "$PWD/ca.crt"
  printf 'silent\nshow-error\nfail-with-body\n'
} >admin.curlrc
```

`printf` is built into the shell, so the token passes through no command's arguments. Every example below runs `curl -K admin.curlrc`: TLS checked against the catalog's CA, the admin's token, and an error status making `curl` fail, with the error body printed.

### Reach the API

The API port, 8181, is reachable inside the cluster, and from outside it only through an internal NLB, if the install turned one on. From your workstation, forward a local port to the catalog's Service; `kubectl` authenticates you with IAM and Kubernetes checks that you may port-forward in the namespace:

```bash
kubectl -n colvenna-catalog port-forward svc/colvenna-catalog 8181:8181 >/dev/null &
forward=$!
CATALOG=https://localhost:8181/colvenna/admin/v1
for _ in $(seq 30); do curl -s -o /dev/null --cacert ca.crt https://localhost:8181/ && break; sleep 1; done
```

The loop waits for the port to answer. It asks for `/`, which is no route: an unauthenticated request to a route would be recorded in the audit log as refused.

From a pod, the API is at `https://colvenna-catalog.colvenna-catalog.svc:8181`, if the pod's namespace carries the client label (`colvenna-catalog/client=true` by default; `COLVENNA_CLIENT_NAMESPACE_LABEL`). With the internal NLB on, clients in the subnets it admits use the NLB's DNS name. The server's certificate names all of these.

### The serverless form

The API is private: it is reachable only through the `execute-api` interface endpoints the install lists (the new VPC's, or `COLVENNA_API_ENDPOINT_IDS`), so run the examples on a host in a VPC that has one. Its address is the output `ApiUrl` of the stack `ColvennaCatalog<Env>Serverless`, `https://<api-id>.execute-api.<region>.amazonaws.com/catalog`; where the endpoint has no private DNS, use `ApiEndpointUrl`, with `<api-id>-<vpce-id>` in the host name. TLS ends at API Gateway, with a certificate your system already trusts, so there is no CA file. `install.sh` keeps the first admin's token in Secrets Manager:

```text
umask 077
mkdir -p ~/.colvenna-admin && cd ~/.colvenna-admin
aws secretsmanager get-secret-value --region <region> --secret-id colvenna-catalog-<env>/admin-token --query SecretString --output text >admin.token
{
  printf 'header = "Authorization: Bearer %s"\n' "$(cat admin.token)"
  printf 'silent\nshow-error\nfail-with-body\n'
} >admin.curlrc
CATALOG=https://<api-id>.execute-api.<region>.amazonaws.com/catalog/colvenna/admin/v1
```

The examples below then run as they are, without what only the EKS form has: `kubectl` (keep a job's token wherever the job reads it), the port-forward and its `kill`, and the CA (leave out `--cacert ca.crt`). API Gateway answers some requests itself, with a body of its own:

- 401 from its authorizer, to a request without a valid token. The server does not see it, so the audit log does not record it. The authorizer keeps each answer for 60 seconds, so a token revoked or expired in the last minute still reaches the server, which refuses it.
- 403 to a request from outside the API's VPC endpoints.
- 413 to a request body over about 6 MiB; nothing is done (`v1.0.0-rc.2` answers 503 instead, [#34](https://github.com/colvenna/colvenna-catalog/issues/34)).
- 503 when the authorizer cannot check the token.
- 429, and 503, to a request beyond what the install serves at once (`COLVENNA_LAMBDA_CONCURRENCY`); nothing is done, and it can be sent again.
- 503 when the server's function failed (a crash, say); the request may have been done. Sent again, a write is answered as after a lost answer (409 for a create), but a token issue issues a second token: list the principal's tokens and revoke the one you did not get.
- 504 when it gave up waiting at 29 seconds.

These examples have been run end to end in both forms (`v1.0.0-rc.2`'s UAT-CAT-05, both Regions).

A first call:

```bash
curl -K admin.curlrc "$CATALOG/principals" | jq .
```

Without the token, the same request is refused with 401:

```bash
curl -s -o /dev/null -w '%{http_code}\n' --cacert ca.crt "$CATALOG/principals"
```

## Principals

A principal is a service identity: a Flink or Spark job, a Doris catalog, a person's tooling. It authenticates with its tokens, and may do in the catalog what its grants allow. Names use letters, digits and `._:@-`, and start with a letter or a digit.

Create one:

```bash
curl -K admin.curlrc -X POST "$CATALOG/principals" -H 'Content-Type: application/json' \
  -d '{"name": "etl-writer", "description": "Nightly loads into sales"}'
```

An admin principal, for another operator, is made the same way with `"admin": true`. An admin holds no catalog privileges unless granted: the admin API and access to tables are granted apart.

List them, with whether each is an admin and whether it is disabled:

```bash
curl -K admin.curlrc "$CATALOG/principals" | jq -r '.principals[] | [.name, .admin, (.disabledAt != null)] | @tsv'
```

## Tokens

A token is shown once, when it is issued; the catalog keeps only its SHA-256. Write the response straight to a file, and keep the secret and the id apart:

```bash
curl -K admin.curlrc -X POST "$CATALOG/principals/etl-writer/tokens" -H 'Content-Type: application/json' \
  -d '{"ttl": "P30D", "description": "sales ETL, from 2026-10"}' -o issued.json
jq -r .token issued.json >etl-writer.token
jq -r .id issued.json >etl-writer.id
jq '{id, principal, expiresAt}' issued.json && rm issued.json
```

`ttl` is an ISO-8601 duration in days, hours, minutes and seconds (`P30D`, `PT12H`; not weeks, months or years), 90 days (`P90D`) if left out and 400 days at most. A Kubernetes job reads its token from a Secret in its namespace, `sales-jobs` here:

```bash
kubectl -n sales-jobs create secret generic etl-writer-catalog-token --from-file=token=etl-writer.token \
  --dry-run=client -o yaml | kubectl apply -f -
```

List a principal's tokens, without their secrets:

```bash
curl -K admin.curlrc "$CATALOG/principals/etl-writer/tokens" \
  | jq -r '.tokens[] | [.id, .description, (.expiresAt | floor | todate), (.revokedAt != null)] | @tsv'
```

In token and principal listings, instants are seconds since the Unix epoch (`jq`'s `todate` shows them); elsewhere they are ISO-8601 strings.

### Rotating a token

Paimon clients never refresh a bearer token: a job keeps the token it started with until it restarts. Before a token expires, or when it may have leaked, give the job a new one and then revoke the old one:

```bash
old=$(cat etl-writer.id)
curl -K admin.curlrc -X POST "$CATALOG/principals/etl-writer/tokens" -H 'Content-Type: application/json' \
  -d '{"ttl": "P30D", "description": "sales ETL, rotated"}' -o issued.json
jq -r .token issued.json >etl-writer.token && jq -r .id issued.json >etl-writer.id && rm issued.json
kubectl -n sales-jobs create secret generic etl-writer-catalog-token --from-file=token=etl-writer.token \
  --dry-run=client -o yaml | kubectl apply -f -
# Restart the jobs that read the Secret, so that they use the new token; then:
curl -K admin.curlrc -X DELETE "$CATALOG/tokens/$old"
```

A revoked token stops authenticating within 2 seconds (`COLVENNA_AUTH_CACHE_TTL`). Per-table credentials taken with it last until they expire, 3 hours by default (`COLVENNA_DATA_TOKEN_DURATION`).

## Grants

A grant allows or denies a principal one privilege on the whole catalog, a database, or one table, view or function. `install.sh` grants the first admin, `admin`, `MANAGE` on the whole catalog, for a year, and each run renews it when less than a month is left; it shows in the listings below. `READ` lets a principal read and take read-only per-table credentials; `WRITE` adds commits, tags, branches and partitions, with read-write credentials; `MANAGE` adds creating, altering and dropping. Each level includes the ones below it. A `DENY` forbids its level and those above, and wins over every `ALLOW`. A principal with no grants sees nothing but the database `default`.

Every grant names its purpose, who approved it, the approval's reference, and when it expires:

```bash
curl -K admin.curlrc -X POST "$CATALOG/grants" -H 'Content-Type: application/json' -d '{
  "principal": "etl-writer", "scope": "DATABASE", "database": "sales", "privilege": "WRITE",
  "purpose": "Nightly loads into sales", "approvedBy": "data-owner@example.com", "approval": "CHG-1042",
  "expiresAt": "2027-06-30T00:00:00Z"}' | jq .
```

A deny on one table, and a read limited to two columns of another, for a second principal:

```bash
curl -K admin.curlrc -X POST "$CATALOG/grants" -H 'Content-Type: application/json' -d '{
  "principal": "etl-writer", "scope": "TABLE", "database": "sales", "object": "customers",
  "privilege": "READ", "effect": "DENY", "purpose": "The ETL job needs no customer data",
  "approvedBy": "data-owner@example.com", "approval": "CHG-1042", "expiresAt": "2027-06-30T00:00:00Z"}'
curl -K admin.curlrc -X POST "$CATALOG/principals" -H 'Content-Type: application/json' -d '{"name": "bi-reader"}'
curl -K admin.curlrc -X POST "$CATALOG/grants" -H 'Content-Type: application/json' -d '{
  "principal": "bi-reader", "scope": "TABLE", "database": "sales", "object": "orders",
  "privilege": "READ", "columns": ["order_id", "amount"], "purpose": "Revenue dashboard",
  "approvedBy": "data-owner@example.com", "approval": "CHG-1043", "expiresAt": "2027-06-30T00:00:00Z"}'
```

List grants, all or one principal's, and revoke one by its id:

```bash
curl -K admin.curlrc "$CATALOG/grants?principal=etl-writer" \
  | jq -r '.grants[] | [.id, .scope, .database, .object, .privilege, .effect, .expiresAt] | @tsv'
grant=$(curl -K admin.curlrc "$CATALOG/grants?principal=etl-writer" \
  | jq -r '.grants[] | select(.scope == "TABLE" and .object == "customers") | .id')
curl -K admin.curlrc -X DELETE "$CATALOG/grants/$grant"
```

A grant stops counting when it expires. The same principal, scope, object, privilege and effect can be granted only once, even after the first grant expired (409): to renew a grant, revoke it and grant it again. Changes reach every replica within 2 seconds; per-table credentials already issued last until they expire. The listing also shows expired grants and those of other environments (`COLVENNA_ENVIRONMENT`), which take no part in decisions.

## The audit log

Every write to the catalog, every decision on a catalog object (reads and listings included; the catalog's configuration, `/v1/config`, is none), and every refused request to a route is an audit event; of the requests refused for want of a valid token, up to 20 a second. The admin API's reads, listings and checks, are recorded only when refused. Each event has the request's id (the `X-Request-Id` response header), its time, the principal, the action, the resource it was on (a created principal's name, say), the outcome (`SUCCEEDED`, `FAILED` or `DENIED`), the HTTP status, and a detail: for a decision, the policy version, the privilege needed (none for an allowed listing, which shows only what the caller may see; `READ` for a listing refused because the caller can see nothing of the database) and the grants it relied on, the allows that permitted it (for columns `/auth` refused, the allow that limits the columns) or the `DENY` grants that refused it; a grant's whole content; a commit's snapshot; an error. A lookup of a table by id that the caller may not read answers 404, as if there were no such table, and is recorded as `DENIED` under the id, never the table's name. Events come newest first, 100 by default and 1000 at most (`limit`).

A principal's latest events:

```bash
curl -K admin.curlrc "$CATALOG/audit?principal=etl-writer&limit=20" \
  | jq -r '.events[] | [.time, .action, .resourceType, .resource, .outcome, .httpStatus] | @tsv'
```

Refusals in the last hour (GNU `date`; on macOS, `date -u -v-1H +%Y-%m-%dT%H:%M:%SZ`):

```bash
since=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
curl -K admin.curlrc "$CATALOG/audit?outcome=DENIED&since=$since" \
  | jq -r '.events[] | [.time, .principal, .action, .resource, .httpStatus, .detail.error] | @tsv'
```

Changes to grants, and the commits to one table:

```bash
curl -K admin.curlrc "$CATALOG/audit?resourceType=GRANT" \
  | jq -r '.events[] | [.time, .principal, .action, .resource, .detail.approval] | @tsv'
curl -K admin.curlrc "$CATALOG/audit?action=COMMIT&resource=sales.orders&limit=10" | jq '.events[] | {time, principal, outcome, detail}'
```

Filters combine: `principal`, `action`, `resourceType`, `resource` (exactly: `sales.orders` for a table, which finds every event on it, failed and refused ones included, a create's and a rename's under the new table and the source; `sales.orders$branch_b` for a branch; a principal's name; a token's id; `<principal>@<scope>:<database>[.<object>]` for a grant), `outcome`, and the time range `since` (inclusive) to `until` (exclusive). An admin operation's action is its `x-colvenna-action` in the OpenAPI description; the Paimon API's are named after its operations (`CREATE_TABLE`, `COMMIT`, `ISSUE_DATA_TOKEN`, ...), and the server's own are `ROLL_FORWARD_COMMIT` and `FENCE_TABLE`.

## Checking and repairing a table

A consistency check compares a table's state in PostgreSQL with its files: the published snapshot, the pending commits, the data files of the retained snapshots and tags, and the schema. Every mismatch is in `problems` (missing data files up to 5 a branch; `dataFilesMissing` in `branches` counts them all). A table whose files are gone, one dropped after a metadata restore's point say, gets a report too: `no schema file in <path>, where the catalog serves schema <id>`, and `<branch>: pointer snapshot <n> has no file` for each branch that had a snapshot. Run it on a quiet table, after a metadata restore say; `sales.orders` here stands for your table.

In the serverless form an answer checks for 8 seconds at most: a large table's check then comes in pages, each the report so far with a `nextPageToken`; asked again with the token, the check goes on where it stopped (the server keeps it for a day, and once done answers the same token with the same report). The last page, without a token, is the whole report, and only it says `consistent: true`. In the EKS form the first answer is the whole report. Follow the tokens in either form:

```bash
page="$(curl -K admin.curlrc "$CATALOG/consistency/sales/orders")"
while token="$(jq -r '.nextPageToken // empty' <<<"$page")"; [[ -n "$token" ]]; do
  page="$(curl -K admin.curlrc "$CATALOG/consistency/sales/orders?pageToken=$token")"
done
jq '{table, state, consistent, problems}' <<<"$page"
```

Reconciling re-reads the table from its files and publishes what they hold: the snapshots beyond the catalog's pointer, branches the catalog lacks, and the schema; it also lifts a fence. A table without a schema file has nothing to reconcile from, and gets a 400: if its files are gone, unregister it. The catalog fences a table, refusing its commits with 409, when it finds a snapshot it did not publish: another catalog, or a job still writing through one, is committing to the same files. Stop that writer first, then reconcile. After a metadata restore, reconciling catches the catalog up with the files written since the backup:

```bash
curl -K admin.curlrc -X POST "$CATALOG/tables/sales/orders/reconcile" | jq .
```

Unregistering removes a table from the catalog and keeps its files, to hand the table to another catalog, or to register it here again with the Paimon API:

```bash
curl -K admin.curlrc -X POST "$CATALOG/tables/sales/orders/unregister"
```

Reconciling and unregistering are recorded in the audit log, and a reconcile's event keeps what it changed; a check, being a read, is not.

## Dropped tables' files

Dropping a table removes it from the catalog at once, and records its directory for deleting. In the EKS form the drop then deletes the files itself, before it answers; in the serverless form it answers first, and the purge function deletes them within about 15 minutes (the [deployment guide](install.md#42-the-serverless-form)). A purge that fails, say because a job still writes to the dropped table's directory, stays recorded with its error and is tried again: every 5 minutes by each replica in the EKS form, at each run of the purge function in the serverless form. Until its files are gone, no new table takes that directory: a table made again under the same name gets a directory of its own. Once a database's last table directory is gone, so is the database's directory, `<db>.db` under the warehouse, when nothing else is in it ([#13](https://github.com/colvenna/colvenna-catalog/issues/13)). The records still to be deleted, oldest first (`limit`, 100 by default and 1000 at most; `pending` counts them all):

```bash
curl -K admin.curlrc "$CATALOG/purges" | jq -r '.purges[] | [.table, .droppedAt, .attempts, .lastError // ""] | @tsv'
```

## Files rollbacks and deletions leave

A rollback, a tag's deletion and a branch's deletion work the same way: the request makes the change (the snapshots after the rollback's target, the tag, the branch are gone when it answers) after recording the snapshot, tag and changelog files it removes, and the data files, manifests and index files those referenced are deleted afterwards: a tag's by the request itself in the EKS form and by the purge function in the serverless form, a rollback's and a branch's an hour after the change (`dueAt`; such a change holds the table's other records back as long), when a job restoring from an older checkpoint can no longer commit them again, by each replica's sweep or the purge function. A purge deletes only what no snapshot, tag or changelog of any branch, nor any branch's current snapshot in the catalog, references when it deletes: a branch made from a deleted tag, or a tag written after a rollback, keeps its files. Unlike Paimon's own operations, the files a rollback or a branch's deletion leaves unreferenced are deleted too ([LLD](../design/colvenna-catalog-lld.md#无服务器形态)). The same answer lists these records in `filePurges` (`filePurgesPending` counts them all): the operation (`ROLLBACK`, `DROP_TAG`, `DROP_BRANCH`), what it was asked for (`target`), whether it took effect (`applied`; a request that stopped after recording leaves one that is not, and whose purge deletes nothing), and how its purges have gone:

```bash
curl -K admin.curlrc "$CATALOG/purges" | jq -r '.filePurges[] | [.table, .branch, .operation, .target, .applied, .attempts, .lastError // ""] | @tsv'
```

## Retiring a principal

Disable a principal that is no longer used. It is for good: no request enables a principal again. Its tokens stop authenticating within 2 seconds, and its grants no longer matter; revoke them to keep the list short.

```bash
curl -K admin.curlrc -X POST "$CATALOG/principals/bi-reader/disable"
```

## Errors

An error has Paimon's error body, `{"message", "resourceType", "resourceName", "code"}`, and the request's `X-Request-Id`, the key to the server's log and to the audit event.

| Status | Meaning |
| --- | --- |
| 400 | A malformed request: a missing or invalid field, a body that is not JSON, a name with `/ \ ? # % *` in a path segment |
| 401 | No token, or one that is unknown, expired or revoked, or of a disabled principal |
| 403 | The principal is not an admin |
| 404 | No such principal, token, grant, database or table; or no such endpoint |
| 409 | It exists already (a principal, a grant) |
| 500 | The server failed; its log has the details under the request id. If the message says the connection to the metadata database was lost while the request committed, whether it took effect is unknown: check (list the tokens, the grants) before repeating it |
| 503 | Retry: the server is at its request limit, the metadata database is unavailable, a lock wait timed out, or the connection to the metadata database was lost before the request committed (for a read, also while it committed); what the request had not committed was rolled back |

## When you are done

Stop the port-forward and remove the files that hold secrets:

```bash
kill "$forward"
rm -f ~/.colvenna-admin/admin.curlrc ~/.colvenna-admin/*.token ~/.colvenna-admin/*.id
```

> Note: the issue and pull request numbers (such as #42) in this document are those of the project's early private repository; this repository has no pages for them. Linked issue numbers (such as [#13](https://github.com/colvenna/colvenna-catalog/issues/13)) are this repository's current ones.
