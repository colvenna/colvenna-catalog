"""UAT-DEP-05 Refused configurations (from Colvenna Lake's run-refusals.sh and run-release-refusals.sh).

Each refusal runs install.sh (or uninstall.sh) with a configuration wrong in one key, for an
environment name of the case's own that has nothing in the account, and must exit with the status
install.sh documents (2 for arguments and configuration, 3 for preflight) and its message, before
changing anything. What a refusal needs that the test environment lacks, the case makes for itself
and removes: an Aurora PostgreSQL cluster without an instance and without IAM database
authentication (its reader endpoint; its writer endpoint for the serverless form), a bucket in
another Region, the checkouts that are not the candidate's (main, a dirty one, a commit on top), and
the switch of NetworkPolicy enforcement (tests/environment/network-policy.sh), turned on again and
verified (tests/environment/verify.sh). On E1 (--config) while enforcement is off,
--allow-unenforced-network-policy passes preflight and changes nothing, as UAT-DEP-03 judges a run.
Afterwards nothing of the case's names is in the Region, and E1 and the test environment are as they
were."""

import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from uatlib import deploy
from uatlib.aws import Aws
from uatlib.install import REPO
from cases import dep03

ID = "UAT-DEP-05"
FORMS = ("eks", "serverless")
OTHER_ACCOUNT = "111122223333"  # a placeholder account: never the caller's


def eks_refusals():
    return ["eks-no-cluster", "eks-no-matching-node", "eks-nlb-cidr-holds-pods", "eks-bucket-other-region",
            "eks-db-not-rds", "eks-db-reader-endpoint", "eks-db-admin-secret-unreadable"]


def serverless_refusals():
    return ["serverless-one-zone", "serverless-no-api-endpoint-in-the-vpc", "serverless-vpc-lacks-endpoints",
            "serverless-db-without-iam-auth", "serverless-concurrency-too-high"]


def checks(form, args=None):
    common = ["args-no-config", "args-unknown-option", "args-both-sources", "config-unknown-key", "config-other-forms-key",
              "config-env-name", "region-missing", "region-china", "region-china-northwest", "region-govcloud",
              "region-malformed", "account-not-the-callers", "not-confirmed"]
    own = (eks_refusals() + ["eks-network-policy-not-enforced", "flag-overrides-refusal"] if form == "eks"
           else serverless_refusals())
    release = ["release-no-registry", "release-not-the-tag", "release-dirty-tree", "release-differs-from-b",
               "release-registry-lacks-it", "release-form-not-accepted"]
    return common + own + release + ["uninstall-not-confirmed", "uninstall-other-account", "nothing-made"]


def add_arguments(parser):
    deploy.add_install_arguments(parser, release_required=True)
    parser.add_argument("--eks-only-release",
                        help="a candidate whose manifest names only eks, with --eks-only-checkout, when --release names "
                             "serverless too (a trial candidate: docs/development.md, Trial candidates)")
    parser.add_argument("--eks-only-checkout", help="that candidate's checkout")


def refused(run, status, words=()):
    """(ok, detail): the run exited with the status and its message says the words (any of them)."""
    text = run.text
    said = not words or any(re.search(w, text, re.I) for w in words)
    last = [t for _, t in run.lines if t.strip() and not t.startswith("== ")][-2:]
    return run.exit_code == status and said, f"exit {run.exit_code} (want {status}): {' | '.join(last)[:300]}"


def judge_no_config(code, output):
    """install.sh without --config: 2, saying it is required."""
    return code == 2 and "--config is required" in output, f"exit {code}: {output.strip()[-200:]}"


def judge_nothing_made(made, test_before, test_after, e1_same):
    """Nothing of the case's names in the Region; the test environment's stack as it was; E1 unchanged
    (e1_same None: the serverless form's run, which does not record E1)."""
    ok = not made and test_before == test_after and e1_same is not False
    e1 = "not this form's run" if e1_same is None else ("unchanged" if e1_same else "changed")
    return ok, (f"of the case's names: {made or 'nothing'}; the test environment's stack "
                f"{'as it was' if test_before == test_after else 'changed'}; E1 {e1}")


class Fixtures:
    """What some refusals need, made by the case and removed by its clean-up."""

    def __init__(self, ctx, name):
        self.ctx, self.name = ctx, name
        self.cluster = None
        self.bucket = None

    def aurora(self):
        """An Aurora PostgreSQL cluster in the test environment's VPC, without an instance (so without a
        cost but its storage) and without IAM database authentication: (writer, reader) endpoints."""
        aws, env = self.ctx.aws, self.ctx.testenv
        subnets = env.output("PrivateSubnetIds").split(",")
        group = f"{self.name}-aurora"
        aws("rds", "create-db-subnet-group", "--db-subnet-group-name", group, "--db-subnet-group-description",
            "UAT-DEP-05 refusals", "--subnet-ids", *subnets, "--tags", "Key=Purpose,Value=uat")
        self.ctx.on_cleanup(f"DB subnet group {group} deleted", lambda: self._delete_group(group))
        version = aws("rds", "describe-db-engine-versions", "--engine", "aurora-postgresql", "--query",
                      "DBEngineVersions[?starts_with(EngineVersion, '16.')].EngineVersion")[-1]
        aws("rds", "create-db-cluster", "--db-cluster-identifier", group, "--engine", "aurora-postgresql",
            "--engine-version", version, "--master-username", "uatadmin", "--manage-master-user-password",
            "--db-subnet-group-name", group, "--storage-encrypted", "--no-enable-iam-database-authentication",
            "--no-deletion-protection", "--backup-retention-period", "1", "--tags", "Key=Purpose,Value=uat")
        self.cluster = group
        self.ctx.on_cleanup(f"Aurora cluster {group} deleted", self._delete_cluster)
        for _ in range(90):
            c = aws("rds", "describe-db-clusters", "--db-cluster-identifier", group)["DBClusters"][0]
            if c["Status"] == "available":
                return c["Endpoint"], c["ReaderEndpoint"], c["Port"]
            time.sleep(10)
        raise RuntimeError(f"the Aurora cluster {group} is not available after 15 minutes")

    def _delete_cluster(self):
        aws = self.ctx.aws
        aws.raw("rds", "delete-db-cluster", "--db-cluster-identifier", self.cluster, "--skip-final-snapshot",
                "--delete-automated-backups", check=False)
        for _ in range(90):
            p = aws.raw("rds", "describe-db-clusters", "--db-cluster-identifier", self.cluster, check=False)
            if p.returncode != 0 and "DBClusterNotFound" in p.stderr:
                return f"{self.cluster} deleted"
            time.sleep(10)
        raise RuntimeError(f"the Aurora cluster {self.cluster} is still there")

    def _delete_group(self, group):
        for _ in range(30):
            p = self.ctx.aws.raw("rds", "delete-db-subnet-group", "--db-subnet-group-name", group, check=False)
            if p.returncode == 0 or "DBSubnetGroupNotFoundFault" in p.stderr:
                return f"{group} deleted"
            time.sleep(20)
        raise RuntimeError(f"the DB subnet group {group} is still there")

    def bucket_elsewhere(self):
        """A bucket of the case's own in another Region."""
        region = "us-east-2" if self.ctx.install.region != "us-east-2" else "us-west-2"
        name = f"{self.name}-elsewhere-{secrets.token_hex(3)}"
        other = aws_in(self.ctx, region)
        other("s3api", "create-bucket", "--bucket", name, "--create-bucket-configuration", f"LocationConstraint={region}")
        other("s3api", "put-public-access-block", "--bucket", name, "--public-access-block-configuration",
              "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")
        self.bucket = (name, region)
        self.ctx.on_cleanup(f"bucket {name} deleted", lambda: other.raw("s3api", "delete-bucket", "--bucket", name).returncode == 0
                            and f"{name} deleted")
        return name


def aws_in(ctx, region):
    """The run's AWS calls, in another Region."""
    return Aws(region, ctx.aws.profile)


class Checkouts:
    """Checkouts that are not the candidate's: main, the candidate's with an uncommitted change, and a
    commit on top of the candidate's. Removed by the clean-up."""

    def __init__(self, ctx, candidate_checkout):
        self.ctx, self.source = ctx, Path(candidate_checkout)
        self.root = Path(tempfile.mkdtemp(prefix="uat-dep05-", dir=ctx.scratch))
        self.made = []
        ctx.on_cleanup("the case's checkouts removed", self.remove)

    def worktree(self, name, ref):
        path = self.root / name
        subprocess.run(["git", "-C", str(self.source), "worktree", "add", "--detach", str(path), ref],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        self.made.append(path)
        return path

    def remove(self):
        for path in self.made:
            subprocess.run(["git", "-C", str(self.source), "worktree", "remove", "--force", str(path)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        subprocess.run(["git", "-C", str(self.source), "worktree", "prune"], stdout=subprocess.PIPE)
        shutil.rmtree(self.root, ignore_errors=True)
        return f"{len(self.made)} checkouts removed"


def base_config(ctx, name, form):
    """A configuration of the case's own environment, right in every key: the test environment's
    cluster (EKS), its VPC, subnets and execute-api endpoint (serverless), E1's account and region."""
    inst, env = ctx.install, ctx.testenv
    keys = {"COLVENNA_ENV": name, "COLVENNA_ACCOUNT": inst.get("COLVENNA_ACCOUNT"), "COLVENNA_REGION": inst.region,
            "COLVENNA_FORM": form, "COLVENNA_DATA_TOKENS": "aws-sts",
            "COLVENNA_RELEASE_REGISTRY": inst.get("COLVENNA_RELEASE_REGISTRY")}
    if form == "eks":
        keys["COLVENNA_EKS_CLUSTER"] = env.cluster
    else:
        keys.update(COLVENNA_NETWORK="existing", COLVENNA_VPC_ID=env.output("VpcId"),
                    COLVENNA_SUBNET_IDS=env.output("PrivateSubnetIds"), COLVENNA_API_ENDPOINT_IDS=env.output("ApiEndpointId"))
    return keys


def run(ctx):
    args, inst, aws = ctx.args, ctx.install, ctx.aws
    form = ctx.form
    checkout = deploy.checkout_of(args)
    release = deploy.manifest(checkout, args.release)
    name = "rf" + ctx.run[-6:]
    configs = Path(tempfile.mkdtemp(prefix="uat-dep05-configs-", dir=ctx.scratch))
    installer = deploy.Installer(ctx, checkout, inst.config_path)  # for its environment only
    fixtures = Fixtures(ctx, f"uat-{ctx.run}".lower())
    with ctx.preparing("what the refusals need from outside the case: the account recorded, and E1 (EKS), "
                       "an eks-only candidate, an execute-api endpoint of another VPC (serverless)") as notes:
        base = base_config(ctx, name, form)
        test_stack_before = deploy.account_stacks(aws).get(ctx.testenv.stack)
        e1_before = None
        if form == "eks":
            e1_before = deploy.record_install(ctx, with_catalog=False)
            if not e1_before["stacks"] or e1_before["kube"]["helm"] is None:
                raise RuntimeError(f"E1 ({inst.env}, --config) is not installed: flag-overrides-refusal runs on it")
        eks_only = eks_only_candidate(args, release, checkout)
        foreign = foreign_api_endpoint(ctx) if form == "serverless" else None
        notes.append(f"eks-only candidate {eks_only[0]}" + (f"; E1 {inst.env}" if e1_before else "")
                     + ("; an endpoint of another VPC" if foreign else ""))

    def config(label, **changes):
        keys = dict(base, **changes)
        path = configs / f"{label}.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in keys.items() if v is not None))
        return path

    def attempt(check, *script_args, status, words=(), script="install.sh", cfg=None, where=None):
        """One refusal: the script of a checkout (the candidate's unless where) with a configuration
        (the base one, named after the check, unless cfg)."""
        r = deploy.Installer(ctx, where or checkout, cfg or config(check)).run(script, *script_args, keep=f"refusal-{check}")
        ok, detail = refused(r, status, words)
        ctx.check(check, ok, detail)
        return r

    rel = ["--release", args.release]
    # --- arguments, configuration, region, account, confirmation ---------------------------------------
    p = subprocess.run([str(checkout / "install.sh")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, env=installer.env(), stdin=subprocess.DEVNULL)
    ctx.keep("refusal-args-no-config.log", p.stdout)
    ctx.check("args-no-config", *judge_no_config(p.returncode, p.stdout))
    attempt("args-unknown-option", *rel, "--no-such-option", status=2, words=("Unknown argument",))
    attempt("args-both-sources", "--from-source", *rel, status=2, words=("Give one of",))
    attempt("config-unknown-key", *rel, "--yes", status=2, words=("COLVENNA_NO_SUCH_KEY",),
            cfg=config("config-unknown-key", COLVENNA_NO_SUCH_KEY="1"))
    other = {"COLVENNA_LAMBDA_MEMORY": "4096"} if form == "eks" else {"COLVENNA_EKS_CLUSTER": ctx.testenv.cluster}
    attempt("config-other-forms-key", *rel, "--yes", status=2, words=tuple(other),
            cfg=config("config-other-forms-key", **other))
    attempt("config-env-name", *rel, "--yes", status=2, words=("COLVENNA_ENV",), cfg=config("config-env-name", COLVENNA_ENV="Bad_Name"))
    for check, region in (("region-missing", None), ("region-china", "cn-north-1"), ("region-china-northwest", "cn-northwest-1"),
                          ("region-govcloud", "us-gov-west-1"), ("region-malformed", "uswest2")):
        attempt(check, *rel, "--yes", status=2, words=("COLVENNA_REGION",), cfg=config(check, COLVENNA_REGION=region))
    attempt("account-not-the-callers", *rel, "--yes", status=3, words=("credentials are of account",),
            cfg=config("account-not-the-callers", COLVENNA_ACCOUNT=OTHER_ACCOUNT))
    attempt("not-confirmed", *rel, status=2, words=("Not confirmed",), cfg=config("not-confirmed"))

    # --- the form's own refusals -------------------------------------------------------------------------
    if form == "eks":
        eks(ctx, attempt, config, fixtures, rel)
    else:
        serverless(ctx, attempt, config, fixtures, rel, foreign)

    # --- the release's -----------------------------------------------------------------------------------
    attempt("release-no-registry", *rel, "--yes", status=2, words=("COLVENNA_RELEASE_REGISTRY",),
            cfg=config("release-no-registry", COLVENNA_RELEASE_REGISTRY=None))
    with ctx.preparing("checkouts that are not the candidate's: main, a dirty one, a commit on top") as notes:
        checkouts = Checkouts(ctx, checkout)
        main = checkouts.worktree("main", "origin/main")
        dirty = checkouts.worktree("dirty", "HEAD")
        (dirty / "uat-dirty.txt").write_text("an uncommitted change\n")
        on_top = checkouts.worktree("on-top", "HEAD")
        (on_top / "uat-on-top.txt").write_text("a commit on top of the candidate\n")
        subprocess.run(["git", "-C", str(on_top), "add", "uat-on-top.txt"], check=True)
        subprocess.run(["git", "-C", str(on_top), "-c", "user.name=uat", "-c", "user.email=uat@example.com", "commit", "-q",
                        "-m", "UAT-DEP-05: a commit on top of the candidate"], check=True)
        notes.append("3 checkouts")
    attempt("release-not-the-tag", *rel, "--yes", status=3, words=("check out", "differs", "No release/"), where=main)
    attempt("release-dirty-tree", *rel, "--yes", status=3, words=("clean checkout",), where=dirty)
    attempt("release-differs-from-b", *rel, "--yes", status=3, words=("differs from",), where=on_top)
    account, region = release_registry(base["COLVENNA_RELEASE_REGISTRY"])
    elsewhere = f"{account}.dkr.ecr.{'us-east-2' if region != 'us-east-2' else 'us-west-2'}.amazonaws.com"
    attempt("release-registry-lacks-it", *rel, "--yes", status=3, words=("registry lacks",),
            cfg=config("release-registry-lacks-it", COLVENNA_RELEASE_REGISTRY=elsewhere))
    form_not_accepted(ctx, config, *eks_only)

    # --- uninstall.sh's ---------------------------------------------------------------------------------
    attempt("uninstall-not-confirmed", status=2, words=("Not confirmed",), script="uninstall.sh", cfg=config("uninstall-not-confirmed"))
    attempt("uninstall-other-account", "--yes", status=3, words=("credentials are of account",), script="uninstall.sh",
            cfg=config("uninstall-other-account", COLVENNA_ACCOUNT=OTHER_ACCOUNT))

    # --- nothing made --------------------------------------------------------------------------------------
    inventory = deploy.own(deploy.account_inventory(aws), name)
    made = {k: v for k, v in inventory.items() if v}
    test_stack_after = deploy.account_stacks(aws).get(ctx.testenv.stack)
    e1_same = None
    if form == "eks":
        e1_after = deploy.record_install(ctx, with_catalog=False)
        # Its stacks, Helm values and images; not its pods, which the case starts again with the switch.
        e1_same = (not deploy.compare_resources(e1_before["stacks"], e1_after["stacks"])
                   and e1_before["kube"]["values"] == e1_after["kube"]["values"]
                   and e1_before["repositories"] == e1_after["repositories"])
    ctx.check("nothing-made", *judge_nothing_made(made, test_stack_before, test_stack_after, e1_same))


def release_registry(registry):
    m = re.match(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com$", registry or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def eks(ctx, attempt, config, fixtures, rel):
    env, aws = ctx.testenv, ctx.aws
    attempt("eks-no-cluster", *rel, "--yes", status=3, words=("No EKS cluster",),
            cfg=config("eks-no-cluster", COLVENNA_EKS_CLUSTER="colvenna-catalog-test-nosuch"))
    attempt("eks-no-matching-node", *rel, "--yes", status=3, words=("No Ready node",),
            cfg=config("eks-no-matching-node", COLVENNA_NODE_SELECTOR="colvenna-uat/none=true"))
    subnets = env.output("PrivateSubnetIds").split(",")
    cidr = aws("ec2", "describe-subnets", "--subnet-ids", subnets[0])["Subnets"][0]["CidrBlock"]
    attempt("eks-nlb-cidr-holds-pods", *rel, "--yes", status=3, words=("must hold no pod address",),
            cfg=config("eks-nlb-cidr-holds-pods", COLVENNA_INTERNAL_NLB="on", COLVENNA_NLB_ALLOWED_CIDRS=cidr))
    with ctx.preparing("a bucket of the case's own in another Region") as notes:
        bucket = fixtures.bucket_elsewhere()
        notes.append("made")
    attempt("eks-bucket-other-region", *rel, "--yes", status=3, words=("is in us-",),
            cfg=config("eks-bucket-other-region", COLVENNA_BUCKET=bucket))
    secret = env.output("ExistingDbAdminSecretArn")
    db = {"COLVENNA_DB": "existing", "COLVENNA_DB_ADMIN_SECRET_ARN": secret, "COLVENNA_DB_NAME": f"rf_{ctx.run[-6:]}",
          "COLVENNA_DB_USER": f"rf_{ctx.run[-6:]}"}
    attempt("eks-db-not-rds", *rel, "--yes", status=3, words=("no RDS instance",),
            cfg=config("eks-db-not-rds", COLVENNA_DB_ENDPOINT="db.example.com", **db))
    with ctx.preparing("an Aurora PostgreSQL cluster of the case's own, without an instance") as notes:
        writer, reader, port = fixtures.aurora()
        notes.append("available")
    attempt("eks-db-reader-endpoint", *rel, "--yes", status=3, words=("reader endpoint",),
            cfg=config("eks-db-reader-endpoint", COLVENNA_DB_ENDPOINT=reader, COLVENNA_DB_PORT=str(port), **db))
    unreadable = re.sub(r":secret:.*$", f":secret:colvenna-uat-nosuch-{ctx.run}", secret)
    attempt("eks-db-admin-secret-unreadable", *rel, "--yes", status=3, words=("admin secret",),
            cfg=config("eks-db-admin-secret-unreadable", COLVENNA_DB_ENDPOINT=env.output("ExistingDbEndpoint"),
                       **dict(db, COLVENNA_DB_ADMIN_SECRET_ARN=unreadable)))

    switch = REPO / "tests" / "environment" / "network-policy.sh"
    installer = deploy.Installer(ctx, deploy.checkout_of(ctx.args), ctx.args.config)
    ctx.probe  # noqa: B018 - the run's probe pod, made while enforcement is on
    try:
        with ctx.preparing("NetworkPolicy enforcement switched off (tests/environment/network-policy.sh off)") as notes:
            off = subprocess.run([str(switch), env.name, ctx.install.region, "off"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, universal_newlines=True, env=ctx.aws.env())
            ctx.keep("network-policy-off.log", off.stdout)
            if off.returncode != 0:
                raise RuntimeError(f"exit {off.returncode}: {off.stdout.strip()[-300:]}")
            notes.append("off")
        attempt("eks-network-policy-not-enforced", *rel, "--yes", status=3, words=("enableNetworkPolicy",),
                cfg=config("eks-network-policy-not-enforced"))
        # With the agent off, pods that were running keep the eBPF programs it had attached, which no
        # longer learn new pods: a client made since is dropped. network-policy.sh off starts every
        # catalog Deployment again without them (#70); E1 is then an install of an unenforcing cluster.
        # It is recorded, run with the flag, recorded again.
        with ctx.preparing("E1's pods started again with enforcement off (network-policy.sh off)") as notes:
            restarted, detail = judge_restarted(off.stdout, deployment(ctx.install))
            if not restarted:
                raise RuntimeError(detail)
            notes.append("rolled out")
        before = deploy.record_install(ctx)
        flagged = installer.run("install.sh", *rel, "--yes", "--allow-unenforced-network-policy", keep="flag-overrides-refusal")
        after = deploy.record_install(ctx)
    finally:
        # It starts E1's pods once more, so the agent attaches their programs afresh, and verify.sh waits
        # until enforcement applies to pods made after the switch.
        on = subprocess.run([str(switch), env.name, ctx.install.region, "on"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, universal_newlines=True, env=ctx.aws.env())
        ctx.keep("network-policy-on.log", on.stdout)
    ctx.check("flag-overrides-refusal", *judge_flag_overrides(flagged, before, after))
    # "afterwards enforcement is on again (tests/environment/verify.sh)": network-policy.sh on runs it.
    back = judge_enforcement_back(on.returncode, on.stdout, deployment(ctx.install))
    if not back[0]:
        ctx.check("eks-network-policy-not-enforced", *back)


def deployment(install):
    """E1's Deployment, <namespace>/<release>, as network-policy.sh names it."""
    return f"{install.namespace}/{install.release}"


def judge_restarted(output, e1):
    """network-policy.sh says it started E1's catalog pods again (its Deployment e1 rolled out)."""
    line = f"Started the catalog pods of {e1} again"
    return line in output, (f"network-policy.sh: {line}" if line in output
                            else f"network-policy.sh did not start E1's pods again ({e1})")


def judge_enforcement_back(code, output, e1):
    """network-policy.sh on exited 0, having started E1's pods (Deployment e1) again, its verify.sh
    having proved enforcement in fact, for pods made after the switch too."""
    restarted, _ = judge_restarted(output, e1)
    ok = code == 0 and restarted and "NetworkPolicy enforced" in output
    return ok, (f"enforcement on again: exit {code}; E1's pods started again: {restarted}; "
                f"{(output.strip().splitlines() or [''])[-1][:200]}")


def judge_flag_overrides(run, before, after):
    """With enforcement off, --allow-unenforced-network-policy passes preflight, and the run changes
    nothing, as UAT-DEP-03 judges a run (all of its checks)."""
    results = dep03.judge("eks", False, run, before, after)
    bad = [f"{k}: {d}" for k, (ok, d) in results.items() if not ok]
    steps = run.steps()
    passed = "preflight" in steps and steps.index("preflight") < len(steps) - 1
    return passed and not bad, f"preflight passed: {passed}; as UAT-DEP-03 judges it: {bad or 'nothing made, replaced or restarted'}"


def serverless(ctx, attempt, config, fixtures, rel, foreign):
    env = ctx.testenv
    # Two subnets of one zone (configuration takes no fewer than two): a nodes subnet and the client
    # subnet beside it.
    nodes = dict(zip(env.output("PrivateSubnetZones").split(","), env.output("PrivateSubnetIds").split(",")))
    clients = dict(zip(env.output("ClientSubnetZones").split(","), env.output("ClientSubnetIds").split(",")))
    zone = sorted(set(nodes) & set(clients))[0]
    attempt("serverless-one-zone", *rel, "--yes", status=3, words=("two zones",),
            cfg=config("serverless-one-zone", COLVENNA_SUBNET_IDS=f"{nodes[zone]},{clients[zone]}"))
    attempt("serverless-no-api-endpoint-in-the-vpc", *rel, "--yes", status=3, words=("none is in",),
            cfg=config("serverless-no-api-endpoint-in-the-vpc", COLVENNA_API_ENDPOINT_IDS=foreign))
    attempt("serverless-vpc-lacks-endpoints", *rel, "--yes", status=3, words=("no interface endpoint",),
            cfg=config("serverless-vpc-lacks-endpoints", COLVENNA_SUBNET_IDS=env.output("ClientSubnetIds")))
    with ctx.preparing("an Aurora PostgreSQL cluster of the case's own, without an instance or IAM authentication") as notes:
        writer, reader, port = fixtures.aurora()
        notes.append("available")
    attempt("serverless-db-without-iam-auth", *rel, "--yes", status=3, words=("IAM database",),
            cfg=config("serverless-db-without-iam-auth", COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT=writer,
                       COLVENNA_DB_PORT=str(port), COLVENNA_DB_ADMIN_SECRET_ARN=env.output("ExistingDbAdminSecretArn"),
                       COLVENNA_BUCKET=env.output("ExistingBucket")))
    attempt("serverless-concurrency-too-high", *rel, "--yes", status=3, words=("COLVENNA_LAMBDA_CONCURRENCY",),
            cfg=config("serverless-concurrency-too-high", COLVENNA_LAMBDA_CONCURRENCY="1000"))


def foreign_api_endpoint(ctx):
    """An available execute-api endpoint of another VPC than the test environment's, for
    serverless-no-api-endpoint-in-the-vpc: S1's own, which COLVENNA_NETWORK=create made in its VPC
    (the case runs on S1 in the serverless form; #71). Never another one of the Region, which could
    be another repository's environment's."""
    vpc = ctx.testenv.output("VpcId")
    available = {e["VpcEndpointId"]: e["VpcId"] for e in ctx.aws(
        "ec2", "describe-vpc-endpoints", "--filters", f"Name=service-name,Values=com.amazonaws.{ctx.install.region}.execute-api",
        "Name=vpc-endpoint-state,Values=available")["VpcEndpoints"]}
    return choose_foreign_endpoint(available, vpc, own_endpoint(ctx.install))


def own_endpoint(install):
    """The execute-api endpoint the install's network stack made (COLVENNA_NETWORK=create), or None."""
    if install.get("COLVENNA_NETWORK") != "create":
        return None
    try:
        return install.output("Network", "ExecuteApiEndpointId")
    except KeyError:
        return None


def choose_foreign_endpoint(available, test_vpc, own):
    """available: {endpoint id: its VPC} of the Region's available execute-api endpoints; own: the
    install's own endpoint, or None."""
    if own and own in available and available[own] != test_vpc:
        return own
    raise RuntimeError(f"S1 has no available execute-api endpoint of its own outside the test environment's VPC ({own}): "
                       "run the case on S1, made by UAT-DEP-01 with COLVENNA_NETWORK=create")


def eks_only_candidate(args, release, checkout):
    """(candidate, checkout) whose manifest does not name serverless: the candidate itself when it names
    only eks, otherwise the eks-only trial candidate given."""
    if "serverless" not in release["configurations"]["forms"]:
        return args.release, checkout
    if args.eks_only_release and args.eks_only_checkout:
        return args.eks_only_release, Path(args.eks_only_checkout)
    raise RuntimeError("the candidate names serverless: give an eks-only trial candidate (--eks-only-release, "
                       "--eks-only-checkout), made as docs/development.md (Trial candidates) says")


def form_not_accepted(ctx, config, candidate, where):
    """A serverless install of a candidate whose manifest does not name serverless."""
    env = ctx.testenv
    cfg = config("release-form-not-accepted", COLVENNA_FORM="serverless", COLVENNA_EKS_CLUSTER=None,
                 COLVENNA_NETWORK="existing", COLVENNA_VPC_ID=env.output("VpcId"),
                 COLVENNA_SUBNET_IDS=env.output("PrivateSubnetIds"), COLVENNA_API_ENDPOINT_IDS=env.output("ApiEndpointId"))
    r = deploy.Installer(ctx, where, cfg).run("install.sh", "--release", candidate, "--yes", keep="refusal-release-form-not-accepted")
    ok, detail = refused(r, 3, ("accepted in the form",))
    ctx.check("release-form-not-accepted", ok, f"{candidate}: {detail}")
