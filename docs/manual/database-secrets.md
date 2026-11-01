# Rotating the database secrets

Two Secrets Manager secrets hold the passwords of the catalog's metadata database. Neither has a rotation function, so Secrets Manager never changes them: the owner accepted that for 1.0 (2026-10-29, #49; [security scans](../security/scans.md), section 6.4). Rotate them by hand, as below, as often as your policy asks, and at once if one may have leaked.

| Secret | Whose password | What reads it | Form |
| --- | --- | --- | --- |
| `colvenna-catalog-<env>/db-admin`, made by the storage stack when `COLVENNA_DB=create` | The database's master user, `colvenna_admin` | `install.sh`, at each run, to create the catalog's own users and database or bring them in line; never the catalog | Both |
| `colvenna-catalog-<env>/db-user`, made by the EKS stack | The catalog's database user (`COLVENNA_DB_USER`) | `install.sh` sets it on that user and copies it into the Kubernetes Secret `colvenna-catalog-db`, which the catalog's pods read when they start | EKS |

`<env>` is `COLVENNA_ENV`. In the serverless form the functions sign in to PostgreSQL with RDS IAM tokens, so the catalog has no database password; the master user's is the only one. With `COLVENNA_DB=existing`, the admin secret is your own (`COLVENNA_DB_ADMIN_SECRET_ARN`): rotate it as you rotate that database's master user, keeping its `{"username": ..., "password": ...}` form, and `install.sh` reads the new one at its next run.

The catalog's bearer tokens are its own, not Secrets Manager's: [the admin API](admin-api.md#rotating-a-token) rotates an engine's, and `install.sh` issues the first admin a new token when less than 30 days are left ([the user manual](user.md), section 5).

These procedures follow what `install.sh` does. They ran as written on `v1.0.0-rc.2`'s and `v1.0.0-rc.4`'s installs, in both forms, each followed by `install.sh` and its smoke check ([`v1.0.0-rc.4`'s UAT report](../uat/reports/v1.0.0-rc.4/report.md), section 8).

## Before you start

You need the AWS CLI v2, `jq`, the configuration you installed with, and credentials of the account that may read and write the secrets (`secretsmanager:GetSecretValue`, `secretsmanager:PutSecretValue`, `secretsmanager:GetRandomPassword`) and, for the master user, change the instance (`rds:ModifyDBInstance`, `rds:DescribeDBInstances`). In the EKS form, also `kubectl` access to the cluster.

Passwords go through files only you can read, never through a command line, where other users of the host could see them in the process list. Each procedure below starts with a directory of its own for them, and removes it at the end. The commands use these settings:

```bash
export AWS_PROFILE=<profile> AWS_REGION=<region>
env_name=<env>
```

Secrets Manager keeps the value before each change as the version `AWSPREVIOUS` (`aws secretsmanager get-secret-value --version-stage AWSPREVIOUS`), should you need the old password back.

## The catalog's database user (EKS form)

1. Put a new password in the secret, keeping its user name:

   ```bash
   umask 077
   work=$(mktemp -d)
   name="colvenna-catalog-$env_name/db-user"
   aws secretsmanager get-random-password --exclude-punctuation --password-length 40 \
     --query RandomPassword --output text >"$work/password"
   aws secretsmanager get-secret-value --secret-id "$name" --query SecretString --output text >"$work/current.json"
   jq --rawfile pw "$work/password" '.password = ($pw | rtrimstr("\n"))' "$work/current.json" >"$work/next.json"
   aws secretsmanager put-secret-value --secret-id "$name" --secret-string "file://$work/next.json" >/dev/null
   ```

2. Run `install.sh` again, with the same configuration and the same source (`--from-source` on the same commit, or `--release <candidate>` for the candidate you run). Its `database` step signs in as the master user, sets the new password on the catalog's user, and writes it into the Kubernetes Secret `colvenna-catalog-db`.

   ```bash
   ./install.sh --config deploy/config/<env>.env --from-source
   ```

3. Restart the catalog's pods right away. They read the password only when they start, and `install.sh` rolls them only when their template changes (a new image, server certificate or configuration), not for a new password. Until they restart, the connections they hold keep working, but each new one, with the old password, is refused.

   ```bash
   kubectl -n colvenna-catalog rollout restart deployment/colvenna-catalog
   kubectl -n colvenna-catalog rollout status deployment/colvenna-catalog --timeout=10m
   ```

   If `install.sh` failed in step 2 at its `admin` or `smoke` step because a pod could not connect to the database, restart the pods and run `install.sh` once more.

4. Remove the files: `rm -rf "$work"`.

The namespace and the deployment are `COLVENNA_NAMESPACE` and `COLVENNA_RELEASE_NAME`, both `colvenna-catalog` by default.

## The master user (`COLVENNA_DB=create`, both forms)

The master user's password lives in two places, the RDS instance and the secret, and nothing changes both at once. Set the new one on the instance first, then in the secret, so that the two end up the same. The catalog does not use the master user, so it keeps running throughout.

1. Read the secret, which also names the instance, and make a new password:

   ```bash
   umask 077
   work=$(mktemp -d)
   name="colvenna-catalog-$env_name/db-admin"
   aws secretsmanager get-secret-value --secret-id "$name" --query SecretString --output text >"$work/current.json"
   id=$(jq -r .dbInstanceIdentifier "$work/current.json")
   aws secretsmanager get-random-password --exclude-punctuation --password-length 40 \
     --query RandomPassword --output text >"$work/password"
   ```

2. Set it on the instance, from a file, and wait (up to half an hour) until the instance has applied it:

   ```bash
   jq -n --arg id "$id" --rawfile pw "$work/password" \
     '{DBInstanceIdentifier: $id, MasterUserPassword: ($pw | rtrimstr("\n")), ApplyImmediately: true}' >"$work/modify.json"
   aws rds modify-db-instance --cli-input-json "file://$work/modify.json" >/dev/null
   for _ in $(seq 120); do
     state=$(aws rds describe-db-instances --db-instance-identifier "$id" --output text \
       --query 'DBInstances[0].[DBInstanceStatus, PendingModifiedValues.MasterUserPassword]')
     [[ "$state" == $'available\tNone' ]] && break
     sleep 15
   done
   echo "$state"   # "available" and "None" once the instance has the new password
   ```

   If it shows anything else, the instance has not applied the password yet: look at its status (`aws rds describe-db-instances`) before you go on.

3. Put it in the secret, keeping the rest of it (the user name, and the host, port, engine and instance the stack added):

   ```bash
   jq --rawfile pw "$work/password" '.password = ($pw | rtrimstr("\n"))' "$work/current.json" >"$work/next.json"
   aws secretsmanager put-secret-value --secret-id "$name" --secret-string "file://$work/next.json" >/dev/null
   ```

   If this step fails, the instance already has the new password: repeat it with the same `$work/password`, and do not make another. Until it succeeds, `install.sh` cannot sign in to the database.

4. Run `install.sh` again, as in the EKS steps above: its `database` step signs in as the master user (in the EKS form from a `psql` pod, in the serverless form from the ops function), which proves the secret and the instance agree. Then `rm -rf "$work"`.

Do not switch the instance to a master password RDS manages itself (`--manage-master-user-password`): RDS would then keep the password in a new secret of its own, and `install.sh` would go on reading the storage stack's, which no longer matches.

> Note: the issue and pull request numbers (such as #49) in this document are those of the project's early private repository; this repository has no pages for them.
