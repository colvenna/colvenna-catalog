"""UAT-DEP-08 Uninstall keeping the data, and install again.

The case's own tables (the test driver's cycle, kept: an append and a primary-key table, read back
with their snapshots), a principal with a token and a grant; the account recorded; uninstall.sh
--yes, twice; recorded again; then the candidate installed again onto the kept bucket and database,
as the deployment guide says (section 6.4): COLVENNA_BUCKET and COLVENNA_DB=existing with the Storage
stack's endpoint and admin secret, which the case writes into the install's configuration (the old
one kept as evidence); a serverless install that made its own network keeps its configuration as it
was. Then every table reads as before, and the principal, its grant and its token work."""

import time
from pathlib import Path

from uatlib import deploy, testdriver
from uatlib.probe import describe

ID = "UAT-DEP-08"
FORMS = ("eks", "serverless")


def checks(form, args=None):
    return ["uninstall-exit", "uninstall-again-exit", "removed", "kept", "others-untouched", "reinstall-finds-everything"]


def add_arguments(parser):
    deploy.add_install_arguments(parser)


def judge_removed_kept(form, before, after, env, kube_after=None, balancers=None, data_missing=()):
    """(removed ok, detail), (kept ok, detail) from the install's stacks before and after; EKS: the
    cluster after (kube_after), and the internal NLB's load balancers that the account still has
    (balancers: {Service: whether its load balancer is still there}); the Storage stack's bucket and
    database instance that the account lacks (data_missing)."""
    stack = deploy.names_of(env)[0]
    have = set(after)
    if form == "eks":
        gone = [f"{stack}Eks"]
        keep = [f"{stack}Storage"]
    else:
        gone = [f"{stack}Serverless", f"{stack}Images"]
        storage = before.get(f"{stack}Storage", {})
        db_in_network = f"{stack}Network" in before and storage and any(
            t == "AWS::RDS::DBInstance" for t, _ in storage.get("resources", {}).values())
        keep = [f"{stack}Storage"] + ([f"{stack}Network"] if db_in_network else [])
        if not db_in_network:
            gone.append(f"{stack}Network")
    removed = [s for s in gone if s in before and s not in have]
    still = [s for s in gone if s in have]
    kept_ok = [s for s in keep if s in have and after[s]["status"].endswith("_COMPLETE")]
    detail_r = f"removed {removed}; still there {still}"
    ok_r = not still
    detail_k = f"kept {kept_ok} of {keep}; its bucket and database {'there' if not data_missing else f'missing: {list(data_missing)}'}"
    ok_k = len(kept_ok) == len([s for s in keep if s in before]) and not data_missing
    if form == "eks" and kube_after is not None:
        secrets = kube_after["secrets"]
        left = [s for s in ("colvenna-catalog-db", "colvenna-catalog-tls", "colvenna-catalog-admin-token") if s in secrets]
        helm = kube_after["helm"]
        lbs = balancers or {}
        still_lb = sorted(n for n, there in lbs.items() if there)
        ok_r = ok_r and not left and helm is None and not still_lb
        detail_r += (f"; Helm release {'gone' if helm is None else helm}; catalog Secrets left {left}; the internal NLB "
                     f"{'off' if not lbs else ('still there: ' + str(still_lb) if still_lb else 'gone')}")
        ca = "colvenna-catalog-ca-key" in secrets
        ok_k = ok_k and ca and kube_after.get("namespace", True)
        detail_k += f"; the CA {'kept' if ca else 'gone'}; the namespace {'kept' if kube_after.get('namespace', True) else 'gone'}"
    return (ok_r, detail_r), (ok_k, detail_k)


def judge_uninstall_again(run, more):
    """The second uninstall exited 0 and removed nothing more of the install."""
    return run.exit_code == 0 and not more, f"exit {run.exit_code}; what the second removed besides: {more or 'nothing'}"


def settled(tables):
    """The driver's read of tables with each one's snapshot ids in order (a listing's order is the file
    system's)."""
    return {t: dict(v, snapshots=sorted(v.get("snapshots") or [])) for t, v in (tables or {}).items()}


def judge_reinstall(run, before, after, answer, grant_there, token_there):
    """After the install onto the kept data, every table reads as before, its snapshots too; the
    principal's token reads, its grant and token are there."""
    same = settled(after) == settled(before) and bool(before)
    ok = run.exit_code == 0 and same and answer is not None and answer.get("status") == 200 and grant_there and token_there
    return ok, (f"install exit {run.exit_code}; tables {'as before, snapshots included' if same else after}; the principal's "
                f"token reads: {(answer or {}).get('status')}; its grant there: {grant_there}; its token there: {token_there}")


def reinstall_config(install, stacks):
    """The configuration's changes for the install onto the kept data (None: as it was)."""
    if install.form == "serverless" and install.get("COLVENNA_NETWORK") == "create":
        return {}
    storage = stacks.get(install.stack("Storage"), {}).get("outputs", {})
    changes = {"COLVENNA_DB": "existing"}
    if not install.get("COLVENNA_BUCKET"):
        changes["COLVENNA_BUCKET"] = storage["BucketName"]
    if install.get("COLVENNA_DB") == "create":
        changes.update(COLVENNA_DB_ENDPOINT=storage["DbEndpoint"], COLVENNA_DB_PORT=storage["DbPort"],
                       COLVENNA_DB_ADMIN_SECRET_ARN=storage["DbAdminSecretArn"])
    return changes


def driver(ctx):
    """How the case runs the driver, on the image installed now: a pod (EKS), or the driver's Lambda in
    the install's VPC (serverless), whose image is in a repository of the install's. Returns (call,
    the Lambda or None)."""
    inst = ctx.install
    image = testdriver.image(ctx, inst)
    if ctx.form == "eks":
        ns = ctx.namespace("driver")
        return lambda body, name: (testdriver.run_pod(ctx, ns, image, body, name=name.replace("_", "-"))["result"] or {}), None
    net = inst.outputs("Network")
    lam = testdriver.Lambda(ctx, inst, image, net["VpcId"], net["SubnetIds"].split(","), net["SubnetZones"].split(","))
    return lam.invoke, lam


def balancers_left(ctx, before):
    """{Service: whether the account still has its load balancer}, of the namespace's LoadBalancer
    Services before (the internal NLB)."""
    hosts = {n: h for n, h in (before or {}).items() if h}
    if not hosts:
        return {n: False for n in before or {}}
    names = {b["DNSName"] for b in ctx.aws("elbv2", "describe-load-balancers")["LoadBalancers"]}
    return {n: h in names for n, h in hosts.items()}


def run(ctx):
    args, inst, aws = ctx.args, ctx.install, ctx.aws
    checkout = deploy.checkout_of(args)
    installer = deploy.Installer(ctx, checkout, args.config)
    admin = ctx.admin
    who = f"dep08-{ctx.run}"
    ca = {"ca": inst.ca()} if ctx.form == "eks" else {}
    if ctx.form == "serverless":
        ctx.on_cleanup("the driver", lambda: testdriver.down(ctx, inst))
    with ctx.preparing("the test driver, on the image installed") as notes:
        call, lam = driver(ctx)
        notes.append("a pod" if lam is None else "a Lambda function in the install's VPC")
    with ctx.preparing("the case's tables, read back; a principal with a token and a grant") as notes:
        cycle = call(testdriver.event(inst, "cycle", inst.admin_token(), rows=150, keep=True, tokenCheck=False, **ca), "cycle")
        database = cycle.get("database")
        tables = ["events", "keyed"]
        ctx.on_cleanup(f"database {database} dropped", lambda: ctx.rest.drop_database(database) and "dropped")
        read_before = call(testdriver.event(inst, "read", inst.admin_token(), database=database, tables=tables, **ca),
                           "read-before")
        admin.principal(who, ID)
        token_id = admin.issue(who, "P1D", key=who)
        grant = admin.grant(who, "DATABASE", "READ", database=database)
        if not (cycle.get("eventsMatch") and cycle.get("keyedMatch") and all(
                (read_before.get("tables") or {}).get(t, {}).get("rows") for t in tables)):
            raise RuntimeError(f"the cycle: {cycle}; read back: {read_before}")
        notes.append(f"database {database}: {read_before.get('tables')}")

    account_before = deploy.account_inventory(aws)
    stacks_before = deploy.stacks_of(aws, inst.env)
    ctx.keep("account-before.json", account_before)
    lbs_before = deploy.kube_state(ctx.kube, inst.namespace, inst.release)["loadBalancers"] if ctx.form == "eks" else None
    if lam is not None:
        lam.down()  # its image is in the Images stack's repository, which the uninstall removes
    first = installer.run("uninstall.sh", "--yes", keep="uninstall")
    middle = deploy.account_inventory(aws)
    again = installer.run("uninstall.sh", "--yes", keep="uninstall-again")
    account_after = deploy.account_inventory(aws)
    stacks_after = deploy.stacks_of(aws, inst.env)
    ctx.keep("account-after.json", account_after)
    ctx.check("uninstall-exit", *deploy.judge_exit(first))
    more = deploy.differences(deploy.own(middle, inst.env), deploy.own(account_after, inst.env))
    ctx.check("uninstall-again-exit", *judge_uninstall_again(again, more))
    kube_after = None
    if ctx.form == "eks":
        kube_after = deploy.kube_state(ctx.kube, inst.namespace, inst.release)
        kube_after["namespace"] = ctx.kube.kubectl("get", "namespace", inst.namespace, check=False).returncode == 0
    lbs = balancers_left(ctx, lbs_before) if ctx.form == "eks" else None
    missing = deploy.data_missing(stacks_after.get(inst.stack("Storage")), account_after)
    (ok_r, d_r), (ok_k, d_k) = judge_removed_kept(ctx.form, stacks_before, stacks_after, inst.env, kube_after, lbs, missing)
    ctx.check("removed", ok_r, d_r)
    ctx.check("kept", ok_k, d_k)
    ctx.check("others-untouched", *deploy.others_untouched(account_before, account_after, inst.env, ctx.testenv.stack,
                                                           said=first.text + again.text))

    # --- step 4: installed again onto the kept data -----------------------------------------------
    changes = reinstall_config(inst, stacks_after)
    if changes:
        ctx.keep("config-before-dep08.env", Path(args.config).read_text())
        deploy.write_config(args.config, args.config, **changes)
        ctx.keep("config-changes.txt", "".join(f"{k}=<set>\n" if "ARN" in k or "ENDPOINT" in k or k == "COLVENNA_BUCKET"
                                               else f"{k}={v}\n" for k, v in changes.items()))
    back = installer.run("install.sh", *deploy.install_args(args), "--yes", keep="install-again")
    if back.exit_code != 0:
        ctx.check("reinstall-finds-everything", False, f"install {deploy.judge_exit(back)[1]}")
        return
    admin = ctx.reconnect()
    time.sleep(5)
    # The image repository went with the uninstall: the driver again, on the image installed now.
    with ctx.preparing("the test driver again, on the image installed now") as notes:
        call, lam = driver(ctx)
        notes.append("ready")
    read_after = call(testdriver.event(inst, "read", inst.admin_token(), database=database, tables=tables, **ca), "read-after")
    r = ctx.probe.run([{"name": "token", "url": f"{inst.rest_base}/databases/{database}/tables/events", "token": who}],
                      tokens=admin.tokens, keep="after-reinstall.jsonl")[0]
    grants = [g["id"] for g in admin.grants(who)]
    tokens = admin.call("GET", f"/principals/{who}/tokens")["body"].get("tokens", [])
    ctx.check("reinstall-finds-everything", *judge_reinstall(back, read_before.get("tables"), read_after.get("tables"), r,
                                                             grant in grants, any(t.get("id") == token_id for t in tokens)))
