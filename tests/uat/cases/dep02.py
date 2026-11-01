"""UAT-DEP-02 Install into existing resources.

As UAT-DEP-01, with --yes, into what the test environment holds for it (tests/environment, section
3.1 of the cases): EKS (E3) with COLVENNA_DB=existing (the environment's PostgreSQL, its admin
secret) and an existing bucket; serverless (S3) also with COLVENNA_NETWORK=existing (the
environment's VPC, two of its private subnets, its execute-api endpoint). The existing resources are
recorded before and after: the VPC, its subnets, route tables and endpoints, the database instance
(its parameters, security groups and settings), and the bucket's configuration and policy, and what
is in them (the instance's databases and roles, the bucket's top-level prefixes): the install adds
only its own, and the stacks its run deployed (its CDK log) are its own. Another install that runs into
the same existing resources at the same time (E3 and S3 in one step of section 3.2) is named by its
configuration (--alongside, #43): what is added under its database, users and prefix is its, not this
install's. In the
EKS form, the time from the command's start to the client's first successful write and read is at
most 15 minutes (the press release's claim)."""

import json
import re
from datetime import timedelta
from pathlib import Path

from uatlib import deploy
from uatlib.install import EXAMPLE, parse_env
from cases import dep01

ID = "UAT-DEP-02"
FORMS = ("eks", "serverless")
MAKES_THE_INSTALL = True
LIMIT = timedelta(minutes=15)


def checks(form, args=None):
    own = ["install-exit", "every-step-done", "runs-the-candidate", "nothing-built", "admin-token-not-printed",
           "readable-writable", "existing-untouched"]
    return own + (["eks-ready-within-15-minutes"] if form == "eks" else [])


def add_arguments(parser):
    deploy.add_install_arguments(parser, release_required=True)
    parser.add_argument("--alongside", action="append", default=[], metavar="CONFIG",
                        help="the configuration of another install that runs into the same existing resources at the same "
                             "time (E3's for S3's run, S3's for E3's): what it adds under its own names is not this install's (#43)")


def record_existing(ctx):
    """The existing resources the install is given, as the account describes them."""
    inst, aws = ctx.install, ctx.aws
    vpc = inst.get("COLVENNA_VPC_ID") or ctx.testenv.output("VpcId")
    out = {"vpc": aws("ec2", "describe-vpcs", "--vpc-ids", vpc)["Vpcs"],
           "subnets": sorted((s["SubnetId"], s["CidrBlock"], s.get("MapPublicIpOnLaunch")) for s in
                             aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc}")["Subnets"]),
           "routeTables": sorted(json.dumps({"id": t["RouteTableId"], "routes": t.get("Routes"),
                                             "associations": sorted(a.get("SubnetId") or "main" for a in t.get("Associations", []))},
                                            sort_keys=True)
                                 for t in aws("ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc}")["RouteTables"]),
           "endpoints": sorted(json.dumps({k: e.get(k) for k in ("VpcEndpointId", "ServiceName", "State", "PolicyDocument",
                                                                 "PrivateDnsEnabled", "SubnetIds", "Groups")}, sort_keys=True)
                               for e in aws("ec2", "describe-vpc-endpoints", "--filters", f"Name=vpc-id,Values={vpc}")["VpcEndpoints"]
                               if not deploy.of_install(_tag(e, "aws:cloudformation:stack-name"), inst.env))}
    if inst.get("COLVENNA_DB") == "existing":
        instance = inst.get("COLVENNA_DB_ENDPOINT").split(".", 1)[0]
        d = aws("rds", "describe-db-instances", "--db-instance-identifier", instance)["DBInstances"][0]
        out["database"] = {k: d.get(k) for k in ("DBInstanceClass", "Engine", "EngineVersion",
                                                 "DBParameterGroups", "VpcSecurityGroups", "IAMDatabaseAuthenticationEnabled",
                                                 "PubliclyAccessible", "MultiAZ", "BackupRetentionPeriod", "DBSubnetGroup")}
        # Reported, not compared (#98): RDS's own activity, a backup in its window, changes it.
        out["databaseStatus"] = d.get("DBInstanceStatus")
        out["securityGroups"] = aws("ec2", "describe-security-groups", "--group-ids",
                                    *[g["VpcSecurityGroupId"] for g in d["VpcSecurityGroups"]])["SecurityGroups"]
    bucket = inst.get("COLVENNA_BUCKET")
    if bucket:
        out["bucket"] = {k: aws.raw("s3api", f"get-bucket-{k}", "--bucket", bucket, "--output", "json", check=False).stdout
                         for k in ("policy", "encryption", "versioning", "lifecycle-configuration", "logging",
                                   "ownership-controls", "notification-configuration")}
        out["bucket"]["publicAccessBlock"] = aws.raw("s3api", "get-public-access-block", "--bucket", bucket, "--output",
                                                     "json", check=False).stdout
    return out


def contents(ctx):
    """What is in the existing database instance (its databases and roles, but PostgreSQL's and RDS's
    own) and bucket (its top-level prefixes)."""
    inst, out = ctx.install, {}
    if inst.get("COLVENNA_DB") == "existing":
        code, text = deploy.existing_psql(ctx, inst, "SELECT 'database ' || datname FROM pg_database;\n"
                                                     "SELECT 'role ' || rolname FROM pg_roles WHERE rolname !~ '^(pg_|rds)';\n")
        if code != 0:
            raise RuntimeError(f"psql exit {code}: {text.strip()[-300:]}")
        for kind in ("database", "role"):
            out[kind + "s"] = sorted(line.split(" ", 1)[1] for line in text.splitlines() if line.startswith(kind + " "))
    bucket = inst.get("COLVENNA_BUCKET")
    if bucket:
        out["prefixes"] = sorted(ctx.aws("s3api", "list-objects-v2", "--bucket", bucket, "--delimiter", "/", "--query",
                                         "CommonPrefixes[].Prefix") or [])
    return out


def names_in(get):
    """What an install of this configuration (get: its settings by key) adds to an existing instance and
    bucket: its database, its users (the authorizer's too, serverless) and its prefix."""
    user = get("COLVENNA_DB_USER")
    return {"databases": {get("COLVENNA_DB_NAME")}, "roles": {user, f"{user}_authz"},
            "prefixes": {get("COLVENNA_WAREHOUSE_PREFIX") + "/"}}


def own_names(inst, stacks):
    """What the install may add: its names (names_in) and its stacks."""
    return dict(names_in(inst.get), stacks=set(stacks))


def alongside_names(inst, paths):
    """What the installs alongside may add to the existing resources (#43): the database and users of each
    one into this one's existing database instance, and the prefix of each one into its bucket, from its
    configuration with the defaults this one's has (deploy/config/example.env). Refused (ValueError)
    unless each is another install into this one's existing database or bucket, under names none of
    which is this one's."""
    out = {"databases": set(), "roles": set(), "prefixes": set()}
    mine = own_names(inst, set())
    for path in paths:
        cfg = dict(parse_env(EXAMPLE.read_text(encoding="utf-8")), **parse_env(Path(path).read_text(encoding="utf-8")))
        name = cfg.get("COLVENNA_ENV")
        if name == inst.env:
            raise ValueError(f"--alongside {path}: that is this install's configuration ({name})")
        database = (cfg.get("COLVENNA_DB") == "existing" and inst.get("COLVENNA_DB") == "existing"
                    and cfg.get("COLVENNA_DB_ENDPOINT") == inst.get("COLVENNA_DB_ENDPOINT"))
        bucket = bool(cfg.get("COLVENNA_BUCKET")) and cfg.get("COLVENNA_BUCKET") == inst.get("COLVENNA_BUCKET")
        if not (database or bucket):
            raise ValueError(f"--alongside {path}: {name} installs into neither this install's existing database nor its bucket")
        theirs = names_in(cfg.get)
        if not database:
            theirs["databases"], theirs["roles"] = set(), set()
        if not bucket:
            theirs["prefixes"] = set()
        clash = sorted(n for k, v in theirs.items() for n in v & mine[k])
        if clash:
            raise ValueError(f"--alongside {path}: {name} has this install's names {clash}")
        for k, v in theirs.items():
            out[k] |= v
    return out


def deployed_stacks(texts):
    """The stacks the run's CDK log names."""
    return sorted(set(re.findall(r"\bColvennaCatalog[A-Za-z0-9]+", texts.get("cdk.log", ""))))


def _tag(resource, key):
    return next((t["Value"] for t in resource.get("Tags", []) if t["Key"] == key), "")


# --- judges ------------------------------------------------------------------------------------------

# What record_existing keeps that the judge reports and does not compare: the instance's status, which RDS's
# own automated backup in the instance's backup window turns to backing-up and back (#98), as maintenance does.
REPORTED = ("databaseStatus",)


def judge_existing_untouched(before, after, made, own, alongside=None):
    """The existing VPC, subnets, route tables, endpoints, database instance and bucket as recorded;
    what was added to them (made: databases, roles, prefixes) and the stacks the run deployed are the
    install's own (own: the same keys), or the names of the installs alongside (alongside: the same
    keys but stacks), which added theirs meanwhile (#43)."""
    alongside = alongside or {}
    changed = sorted(k for k in before if k not in REPORTED and before[k] != after.get(k))
    theirs = {k: sorted(set(v) & alongside.get(k, set()) - own.get(k, set())) for k, v in made.items()}
    theirs = {k: v for k, v in theirs.items() if v}
    foreign = {k: sorted(set(v) - own.get(k, set()) - alongside.get(k, set())) for k, v in made.items()}
    foreign = {k: v for k, v in foreign.items() if v}
    hint = "" if not foreign or alongside else " (another install into them at the same time? name it with --alongside)"
    status = ("" if before.get("databaseStatus") == after.get("databaseStatus")
              else f" (the instance's status {before.get('databaseStatus')} -> {after.get('databaseStatus')}, RDS's own: not compared)")
    return not changed and not foreign, (f"{'as recorded' if not changed else f'changed: {changed}'}{status}; added and deployed: "
                                         f"{made}; added by the installs alongside: {theirs or 'none'}; "
                                         f"not the install's: {foreign or 'none'}{hint}")


def judge_ready_within(started, ready, worked, limit=LIMIT):
    """From the command's start to the client's first successful write and read, at most the limit."""
    took = deploy.parse_time(ready) - deploy.parse_time(started)
    return worked and took <= limit, (f"{int(took.total_seconds())} s from the command's start to the client's first "
                                      f"write and read (at most {int(limit.total_seconds())}); the cycle passed: {worked}")


def run(ctx):
    args, inst = ctx.args, ctx.install
    checkout = deploy.checkout_of(args)
    with ctx.preparing("nothing of the install in the account; existing resources given") as notes:
        release = deploy.manifest(checkout, args.release)
        alongside = alongside_names(inst, args.alongside)
        if args.alongside:
            notes.append(f"alongside: {sorted(n for v in alongside.values() for n in v)}")
        before = deploy.stacks_of(ctx.aws, inst.env)
        given = inst.get("COLVENNA_DB") == "existing" and inst.get("COLVENNA_BUCKET") and (
            ctx.form == "eks" or inst.get("COLVENNA_NETWORK") == "existing")
        if before or not given:
            raise RuntimeError(f"stacks {sorted(before)}; COLVENNA_DB={inst.get('COLVENNA_DB')}, bucket given "
                               f"{bool(inst.get('COLVENNA_BUCKET'))}, network {inst.get('COLVENNA_NETWORK')}")
        recorded = record_existing(ctx)
        ctx.keep("existing-before.json", recorded)
        held = contents(ctx)
        there = sorted(set(held.get("databases", [])) & {inst.get("COLVENNA_DB_NAME")} |
                       set(held.get("roles", [])) & own_names(inst, {})["roles"] |
                       set(held.get("prefixes", [])) & {inst.prefix + "/"})
        if there:
            raise RuntimeError(f"the existing resources hold the install's names already (an earlier install's): {there}")
        # The client's build before the clock starts: only its image is made after the install.
        prepared = dep01.prepare_client(ctx)
        notes.append(f"existing database, bucket{' and network' if ctx.form == 'serverless' else ''} recorded")

    installer = deploy.Installer(ctx, checkout, args.config)
    run = installer.run("install.sh", *deploy.install_args(args), "--yes", keep="install")
    evidence = Path(run.evidence) if run.evidence else None
    ctx.know_install()  # its URI and host, kept out of the evidence from now on
    ctx.keep("step-times.txt", "".join(f"{t} {x}\n" for t, x in run.lines if x.startswith("== ")))
    ctx.check("install-exit", *deploy.judge_exit(run))
    ctx.check("every-step-done", *deploy.every_step_done(run, ctx.form, inst.get("COLVENNA_INTERNAL_NLB") == "on"))
    stacks = deploy.stacks_of(ctx.aws, inst.env)
    ctx.keep("stacks.json", stacks)
    state = deploy.candidate_state(inst, ctx.aws, ctx.kube, args.release, checkout)
    ctx.keep("candidate-state.json", state)
    ctx.check("runs-the-candidate", *deploy.judge_runs_the_candidate(state, stacks, args.release, release, ctx.form))
    ctx.check("nothing-built", *dep01.judge_nothing_built(deploy.evidence_texts(evidence) if evidence else {},
                                                          [t for _, t in run.lines]))
    token, answer, where = dep01.admin_token_answer(ctx)
    texts = dict(deploy.evidence_texts(evidence) if evidence else {}, output=run.text)
    ctx.check("admin-token-not-printed", *dep01.judge_admin_token_not_printed(texts, token, answer, where))
    worked, detail = dep01.readable_writable(ctx, prepared)
    ready = deploy.iso(deploy.now())
    ctx.check("readable-writable", worked, detail)
    after = record_existing(ctx)
    ctx.keep("existing-after.json", after)
    now_held = contents(ctx)
    made = {k: sorted(set(v) - set(held.get(k, []))) for k, v in now_held.items()}
    made["stacks"] = deployed_stacks(deploy.evidence_texts(evidence) if evidence else {})
    ctx.keep("existing-contents.json", {"before": held, "after": now_held, "deployed": made["stacks"]})
    ctx.check("existing-untouched", *judge_existing_untouched(recorded, after, made, own_names(inst, stacks), alongside))
    if ctx.form == "eks":
        ctx.check("eks-ready-within-15-minutes", *judge_ready_within(run.started, ready, worked))
    else:
        ctx.keep("ready-time.txt", judge_ready_within(run.started, ready, worked)[1] + " (recorded only)\n")
