"""UAT-DEP-09 Uninstall deleting the data.

E1 and S1 run on the instance UAT-DEP-10 restored (their configuration's COLVENNA_DB=existing with
the bucket the install made, as the user manual's restore leaves it): uninstall.sh --yes --delete-data
refuses while it is there, naming it, before it asks for the name; the case then deletes it and its
log group as the deployment guide says (section 8). Then uninstall.sh --yes --delete-data without
the environment's name typed (nothing may be deleted), then with it on its standard input, as the deployment guide says (section 8); then the
account is checked for anything of the install: tests/serverless/verify-clean.sh, which names the
install's stacks, functions, roles, repositories, buckets, database instances (and snapshots),
secrets, log groups, identity pools, VPC endpoints and network interfaces, in either form (the driver
taken down first by tests/serverless/down.sh), and in the EKS form the Helm release and the
catalog's Secrets, the CA too. An install into existing resources (E3, S3) leaves them: the existing
database instance and bucket untouched, the catalog's database, users and prefix still in them;
the case then drops those, as the guide tells a user to (its section 8: the catalog's role granted to the
admin first, #73), with the statements uninstall.sh printed."""

import subprocess
import time

from uatlib import deploy

ID = "UAT-DEP-09"
FORMS = ("eks", "serverless")


def checks(form, args=None):
    return ["restored-instance-refused", "delete-data-needs-the-name", "uninstall-exit", "nothing-of-it-left", "existing-kept", "others-untouched"]


def add_arguments(parser):
    deploy.add_install_arguments(parser)


def judge_restored_refused(run, instance, untouched):
    """With the restored instance there: exit 3, naming it, before the name is asked for; nothing changed."""
    named = instance in run.text
    asked = any("Type the environment name" in t or "Not confirmed" in t for _, t in run.lines)
    said = [t for _, t in run.lines if instance in t][:1]
    return run.exit_code == 3 and named and not asked and untouched, (
        f"exit {run.exit_code}; names the instance: {named}; asked for the name: {asked}; "
        f"{'nothing changed' if untouched else 'something changed'}: {said}")


def judge_uninstall_exit(run, retry=None):
    """0 with the name; a failed run is judged as it ended (a rerun, #43, is for the clean-up)."""
    ok, detail = deploy.judge_exit(run)
    return ok, detail + (f"; run again (#43 says how): exit {retry.exit_code}" if retry else "")


def judge_needs_the_name(run, untouched):
    """Without the typed name: exit 2, and nothing deleted, even with --yes."""
    said = [t for _, t in run.lines if "Not confirmed" in t][:1]
    return run.exit_code == 2 and untouched, f"exit {run.exit_code}; {'nothing deleted' if untouched else 'something changed'}: {said}"


def judge_nothing_left(code, output, kube_left):
    """verify-clean.sh found nothing of the install, nor (EKS) its Helm release or Secrets."""
    left = [line for line in output.splitlines() if line.startswith("LEFT")]
    return code == 0 and not left and not kube_left, f"verify-clean.sh exit {code}; left: {left + kube_left or 'nothing'}"


def judge_existing_kept(same, present, prefix_there):
    """The existing instance and bucket as recorded, with the catalog's database, users and prefix in them."""
    return same and present and prefix_there, (
        f"the existing instance and bucket {'as recorded' if same else 'changed'}; the catalog's database and user "
        f"{'still there' if present else 'gone'}; its prefix {'still there' if prefix_there else 'empty'}")


def restored(install):
    """The instance UAT-DEP-10 restored, which E1 and S1 run on, or None: the configuration the user
    manual's restore leaves (6.2, step 4), COLVENNA_DB=existing with the bucket the install made, and
    an endpoint that is not the Storage stack's own instance's (which UAT-DEP-08's install onto the
    kept data names)."""
    own = deploy.names_of(install.env)[2]
    if install.get("COLVENNA_DB") != "existing" or not install.get("COLVENNA_BUCKET").startswith(own):
        return None
    instance = install.get("COLVENNA_DB_ENDPOINT").split(".", 1)[0].lower()
    stacks = (install.outputs("Storage").get("DbEndpoint") or "").split(".", 1)[0].lower()
    return instance if stacks and instance != stacks else None


def existing(install):
    """The existing resources the install was given and did not make: (instance, bucket), each or None.
    The instance UAT-DEP-10 restored is not one: the install's own data is in it."""
    own = deploy.names_of(install.env)[2]
    instance = bucket = None
    if install.get("COLVENNA_DB") == "existing" and not restored(install):
        endpoint = install.get("COLVENNA_DB_ENDPOINT")
        if not endpoint.startswith(own):
            instance = endpoint.split(".", 1)[0]
    b = install.get("COLVENNA_BUCKET")
    if b and not b.startswith(own):
        bucket = b
    return instance, bucket


def delete_restored(ctx, instance):
    """The restored instance and its log group deleted, as the deployment guide says (section 8):
    deletion protection off, no final snapshot, its automated backups too."""
    aws = ctx.aws
    d = aws("rds", "describe-db-instances", "--db-instance-identifier", instance)["DBInstances"][0]
    if d.get("DeletionProtection"):
        aws("rds", "modify-db-instance", "--db-instance-identifier", instance, "--no-deletion-protection",
            "--apply-immediately")
    aws("rds", "delete-db-instance", "--db-instance-identifier", instance, "--skip-final-snapshot",
        "--delete-automated-backups")
    aws.raw("rds", "wait", "db-instance-deleted", "--db-instance-identifier", instance, timeout=3600)
    group = f"/aws/rds/instance/{instance}/postgresql"
    p = aws.raw("logs", "delete-log-group", "--log-group-name", group, check=False)
    if p.returncode != 0 and "ResourceNotFoundException" not in p.stderr:
        raise RuntimeError(f"{group}: {p.stderr.strip()[:200]}")
    return f"{instance} deleted, and {group}"


def record_existing(ctx, instance, bucket):
    out = {}
    if instance:
        d = ctx.aws("rds", "describe-db-instances", "--db-instance-identifier", instance)["DBInstances"][0]
        out["instance"] = {k: d.get(k) for k in ("DBInstanceStatus", "DBParameterGroups", "VpcSecurityGroups",
                                                 "IAMDatabaseAuthenticationEnabled", "DBInstanceClass", "Engine")}
    if bucket:
        out["bucket"] = {"policy": ctx.aws.raw("s3api", "get-bucket-policy", "--bucket", bucket, "--query", "Policy",
                                               "--output", "text", check=False).stdout.strip(),
                         "publicAccess": ctx.aws.raw("s3api", "get-public-access-block", "--bucket", bucket,
                                                     "--output", "json", check=False).stdout.strip()}
    return out


def run(ctx):
    args, inst, aws = ctx.args, ctx.install, ctx.aws
    checkout = deploy.checkout_of(args)
    installer = deploy.Installer(ctx, checkout, args.config)
    instance, bucket = existing(inst)
    restored_instance = restored(inst)
    api_ids = inst.outputs("Serverless").get("ApiId", "") if ctx.form == "serverless" else ""
    with ctx.preparing("the install there; the account and what was given recorded") as notes:
        before_existing = record_existing(ctx, instance, bucket)
        account_before = deploy.account_inventory(aws)
        ctx.keep("account-before.json", account_before)
        stacks_before = deploy.stacks_of(aws, inst.env)
        if not stacks_before:
            raise RuntimeError("no stack of the install")
        if restored_instance:
            status = aws("rds", "describe-db-instances", "--db-instance-identifier", restored_instance,
                         )["DBInstances"][0]["DBInstanceStatus"]
            notes.append(f"on the instance UAT-DEP-10 restored ({status})")
        notes.append(f"{len(stacks_before)} stacks; given: database {bool(instance)}, bucket {bool(bucket)}")
    with ctx.preparing("the driver taken down first (tests/serverless/down.sh)") as notes:
        down = subprocess.run([str(checkout / "tests" / "serverless" / "down.sh"), inst.env, inst.region],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=aws.env())
        ctx.keep("driver-down.log", down.stdout)
        if down.returncode != 0:
            raise RuntimeError(f"exit {down.returncode}: {down.stdout.strip()[-300:]}")
        notes.append("down")
    said = []

    # --- step 1 (E1 and S1): while the restored instance is there ------------------------------------
    if restored_instance:
        first = installer.run("uninstall.sh", "--yes", "--delete-data", keep="uninstall-restored-there")
        said.append(first.text)
        untouched = not deploy.compare_resources(stacks_before, deploy.stacks_of(aws, inst.env))
        ctx.check("restored-instance-refused", *judge_restored_refused(first, restored_instance, untouched))
        with ctx.preparing("the restored instance and its log group deleted, as the deployment guide says") as notes:
            notes.append(delete_restored(ctx, restored_instance))
    else:
        ctx.check("restored-instance-refused", True, f"the install does not run on a restored instance (COLVENNA_DB="
                                                     f"{inst.get('COLVENNA_DB')}): step 1 is E1's and S1's, after UAT-DEP-10")

    # --- step 2 --------------------------------------------------------------------------------------
    refused = installer.run("uninstall.sh", "--yes", "--delete-data", keep="uninstall-no-name")
    said.append(refused.text)
    after_refusal = deploy.stacks_of(aws, inst.env)
    untouched = not deploy.compare_resources(stacks_before, after_refusal)
    ctx.check("delete-data-needs-the-name", *judge_needs_the_name(refused, untouched))

    done = installer.run("uninstall.sh", "--yes", "--delete-data", stdin_text=inst.env + "\n", keep="uninstall")
    said.append(done.text)
    retry = None
    if done.exit_code != 0:
        # #43: the access log bucket can refuse its deletion for a minute; the run is judged as it ended,
        # and then repeated for the clean-up, as the deployment guide says.
        time.sleep(60)
        retry = installer.run("uninstall.sh", "--yes", "--delete-data", stdin_text=inst.env + "\n", keep="uninstall-retry")
        said.append(retry.text)
    ctx.check("uninstall-exit", *judge_uninstall_exit(done, retry))

    # --- step 3 --------------------------------------------------------------------------------------
    env = dict(aws.env(), API_IDS=api_ids)
    clean = subprocess.run([str(checkout / "tests" / "serverless" / "verify-clean.sh"), inst.env, inst.region],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=env)
    ctx.keep("verify-clean.txt", clean.stdout)
    kube_left = []
    if ctx.form == "eks":
        k = deploy.kube_state(ctx.kube, inst.namespace, inst.release)
        if k["helm"] is not None:
            kube_left.append("the Helm release")
        kube_left += [s for s in k["secrets"] if s.startswith("colvenna-catalog-")]
    ctx.check("nothing-of-it-left", *judge_nothing_left(clean.returncode, clean.stdout, kube_left))

    if instance or bucket:
        after_existing = record_existing(ctx, instance, bucket)
        same = after_existing == before_existing
        db, user = inst.get("COLVENNA_DB_NAME"), inst.get("COLVENNA_DB_USER")
        code, out = deploy.existing_psql(ctx, inst, f"SELECT datname FROM pg_database WHERE datname = '{db}';\n"  # nosec B608 - the harness's own names, no outside input
                                                    f"SELECT rolname FROM pg_roles WHERE rolname LIKE '{user}%' ORDER BY 1;\n"
                                         ) if instance else (0, "")
        present = (db in out.split() and user in out.split()) if instance else True
        objects = aws.raw("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", inst.prefix + "/", "--max-keys", "1",
                          "--query", "KeyCount", "--output", "text").stdout.strip() if bucket else "0"
        prefix_there = (objects not in ("0", "None", "")) if bucket else True
        ctx.check("existing-kept", *judge_existing_kept(same, present, prefix_there))
        if done.exit_code == 0 or (retry and retry.exit_code == 0):
            # As the deployment guide tells a user (section 8), once the install is gone: the catalog's
            # database, users and prefix in what it was given.
            printed = printed_statements((retry or done).text)
            ctx.on_cleanup("the catalog's database, users and prefix dropped from what the install was given",
                           lambda: drop_what_was_given(ctx, inst, instance, bucket, printed))
    else:
        ctx.check("existing-kept", True, "the install was given no existing database or bucket: nothing of the user's to keep")

    account_after = deploy.account_inventory(aws)
    ctx.keep("account-after.json", account_after)
    ctx.check("others-untouched", *deploy.others_untouched(account_before, account_after, inst.env, ctx.testenv.stack,
                                                           said="\n".join(said)))


def guide_statements(form, db, user):
    """The deployment guide's statements (section 8) that drop the catalog's database and users from the
    instance it was given: the catalog's role granted to the admin first, who is no superuser on RDS, so
    that it may drop the database the role owns (#73); the authorizer's user too in the serverless form."""
    roles = [user] + ([f"{user}_authz"] if form == "serverless" else [])
    return [f'GRANT "{user}" TO CURRENT_USER;', f'DROP DATABASE "{db}";'] + [f'DROP ROLE "{r}";' for r in roles]


def printed_statements(text):
    """The statements uninstall.sh --delete-data printed for the database it was given: the indented lines
    after the one that says where to run them."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "connected to the database postgres:" in line:
            out = []
            for following in lines[i + 1:]:
                if not following.startswith("    "):
                    break
                out.append(following.strip())
            return out
    return []


def drop_what_was_given(ctx, inst, instance, bucket, printed):
    """The catalog's database and users from the given instance, with the deployment guide's statements
    (section 8), and its prefix from the given bucket. They must also be what uninstall.sh printed; the
    clean-up fails if not, or if they fail. If they fail, the database is dropped all the same (its sessions
    ended, WITH (FORCE)), so that nothing is left for a later install of the name to take up (#73)."""
    db, user = inst.get("COLVENNA_DB_NAME"), inst.get("COLVENNA_DB_USER")
    if bucket:
        ctx.aws.raw("s3", "rm", f"s3://{bucket}/{inst.prefix}/", "--recursive", "--only-show-errors", timeout=1800)
    if instance:
        statements = guide_statements(ctx.form, db, user)
        code, out = deploy.existing_psql(ctx, inst, "\n".join(statements) + "\n")
        ctx.keep("dropped.txt", out)
        if code != 0:
            roles = [r.split('"')[1] for r in statements if r.startswith("DROP ROLE")]
            forced = (f'GRANT "{user}" TO CURRENT_USER;\nDROP DATABASE IF EXISTS "{db}" WITH (FORCE);\n'
                      + "".join(f'DROP ROLE IF EXISTS "{r}";\n' for r in roles))
            again, out_again = deploy.existing_psql(ctx, inst, forced)
            ctx.keep("dropped-forced.txt", out_again)
            raise RuntimeError(f"the deployment guide's statements failed (psql exit {code}: {out.strip()[-160:]}); "
                               f"dropped with its sessions ended: exit {again}")
        if printed != statements:
            raise RuntimeError(f"uninstall.sh printed {printed}, not the deployment guide's statements {statements}")
    return "dropped"
