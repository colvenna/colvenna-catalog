"""UAT-DEP-11 The internal NLB (EKS form only: the serverless form has no load balancer).

E3 run again with COLVENNA_INTERNAL_NLB=on and COLVENNA_NLB_ALLOWED_CIDRS the test environment's
client subnets; the client there is the test driver (tests/serverless/driver), deployed into those
subnets with the install's CA and egress to the API port. Through the NLB's name it runs the Paimon
2.0.0 client's cycle with per-table credentials, reports the certificate it verified, and makes
UAT-SEC-01's requests; a pod of the cluster, and this host outside the VPC, try the NLB too. Then E3
is run again with the NLB off, and its load balancer and target groups must be gone."""

import socket
import time
import urllib.parse

from uatlib import deploy, testdriver
from cases import sec01

ID = "UAT-DEP-11"
FORMS = ("eks",)
PORT = 8181


def checks(form, args=None):
    return ["nlb-internal", "nlb-cycle", "nlb-unauthenticated-refused", "nlb-only-allowed-cidrs", "nlb-removed"]


def add_arguments(parser):
    deploy.add_install_arguments(parser)


# --- judges ------------------------------------------------------------------------------------------

def judge_nlb_internal(lb):
    """The load balancer's scheme is internal, and it has no public address."""
    public = [a for z in (lb or {}).get("AvailabilityZones", []) for a in z.get("LoadBalancerAddresses", [])
              if a.get("IpAddress") and not a.get("PrivateIPv4Address")]
    ok = lb is not None and lb.get("Scheme") == "internal" and not public
    return ok, f"scheme {lb and lb.get('Scheme')}; public addresses {len(public)}; type {lb and lb.get('Type')}"


def judge_nlb_cycle(cycle, peer, host):
    """The client's cycle passed with per-table credentials, and the certificate it verified names the NLB."""
    names = (peer or {}).get("names") or []
    ok = (cycle.get("eventsMatch") is True and cycle.get("keyedMatch") is True and cycle.get("dataTokenEnabled") is True
          and (peer or {}).get("verified") is True and host in names)
    return ok, (f"rows match {cycle.get('eventsMatch')}/{cycle.get('keyedMatch')} with per-table credentials "
                f"({cycle.get('dataTokenEnabled')}); certificate verified {(peer or {}).get('verified')}, names the NLB: "
                f"{host in names}")


def judge_refused(answers, sent):
    """Each of UAT-SEC-01's requests through the NLB answered 401."""
    wrong = [f"{n}: {a.get('status')}" for n, a in answers.items() if a.get("status") != 401]
    return len(answers) == sent and not wrong, f"{len(answers)} of {sent} requests answered; not 401: {wrong or 'none'}"


def judge_only_allowed(pod, outside):
    """No connection from a pod of the cluster (outside the allowed CIDRs), nor from outside the VPC."""
    ok = pod.get("connect") in ("timeout", "refused", "unreachable") and outside == "no connection"
    return ok, f"from a pod of the cluster: {pod.get('connect')}; from this host, outside the VPC: {outside}"


def judge_nlb_removed(run, arn, groups, left):
    """With the NLB off, the install's run passed and the load balancer and its target groups are gone."""
    ok = run.exit_code == 0 and arn is not None and not left
    return ok, f"install with the NLB off: exit {run.exit_code}; load balancer and {len(groups)} target groups left: {len(left)}"


def nlb_of(ctx):
    """(hostname, load balancer) of the install's internal NLB Service."""
    inst = ctx.install
    svc = ctx.kube.json("get", "service", f"{inst.release}-nlb", "-n", inst.namespace)
    host = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress", [{}])[0].get("hostname")
    lbs = ctx.aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    lb = next((b for b in lbs if b.get("DNSName") == host), None)
    return host, lb


def run(ctx):
    args, inst, aws, env = ctx.args, ctx.install, ctx.aws, ctx.testenv
    checkout = deploy.checkout_of(args)
    clients = env.output("ClientSubnetCidrs")
    config_on = deploy.write_config(ctx.scratch / f"{inst.env}-nlb-on.env", args.config, COLVENNA_INTERNAL_NLB="on",
                                    COLVENNA_NLB_ALLOWED_CIDRS=clients)
    config_off = deploy.write_config(ctx.scratch / f"{inst.env}-nlb-off.env", args.config, COLVENNA_INTERNAL_NLB="off",
                                     COLVENNA_NLB_ALLOWED_CIDRS=None)

    def nlb_off():
        """The clean-up: the NLB off again, if a failure left it on."""
        if ctx.kube.kubectl("get", "service", f"{inst.release}-nlb", "-n", inst.namespace, check=False).returncode != 0:
            return "the NLB is off"
        r = deploy.Installer(ctx, checkout, config_off).run("install.sh", *deploy.install_args(args), "--yes",
                                                            keep="cleanup-nlb-off")
        if r.exit_code != 0:
            raise RuntimeError(f"install.sh with the NLB off failed: exit {r.exit_code}")
        return "the NLB turned off"

    ctx.on_cleanup("the internal NLB off", nlb_off)
    with ctx.preparing("E3 run again with the internal NLB on, for the client subnets") as notes:
        on = deploy.Installer(ctx, checkout, config_on).run("install.sh", *deploy.install_args(args), "--yes",
                                                            keep="install-nlb-on")
        if on.exit_code != 0:
            raise RuntimeError(f"exit {on.exit_code}; failed {on.failed_step()}")
        notes.append("the NLB on")
    host, lb = nlb_of(ctx)
    ctx.keep("nlb.json", {"host": host, "loadBalancer": lb})
    ctx.check("nlb-internal", *judge_nlb_internal(lb))

    # --- the client in the client subnets ------------------------------------------------------------
    admin = ctx.admin  # the admin's token among the run's secrets before any answer is kept
    ctx.sanitizer.values[host] = "<NLB_HOST>"
    ctx.on_cleanup("the driver in the client subnets", lambda: testdriver.down(ctx, inst))
    with ctx.preparing("the test driver in the client subnets, with the install's CA and egress to the API port") as notes:
        image = testdriver.image(ctx, inst)
        driver = testdriver.Lambda(ctx, inst, image, env.output("VpcId"), env.output("ClientSubnetIds").split(","),
                                   env.output("ClientSubnetZones").split(","), api_port=PORT)
        notes.append("deployed")
    uri = f"https://{host}:{PORT}"
    base = {"uri": uri, "warehouse": inst.warehouse, "token": admin.tokens["admin"], "ca": inst.ca()}
    peer = driver.invoke(dict(base, action="peer"), "peer")
    # The client subnets reach nothing but the VPC and S3: no STS, so the cycle's own look at the
    # credentials (an STS call) is left out; its reads and writes still use the per-table credentials.
    cycle = driver.invoke(dict(base, action="cycle", rows=100, tokenCheck=False), "cycle")
    ctx.check("nlb-cycle", *judge_nlb_cycle(cycle, peer, host))

    # --- UAT-SEC-01's requests, through the NLB ----------------------------------------------------------
    db, table = f"dep11_{ctx.run}", "t"
    rest = ctx.rest
    with ctx.preparing("a table of the case's own; UAT-SEC-01's tokens") as notes:
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT")])
        issued = sec01.prepare_tokens(ctx, admin, db)
        notes.append(f"{db}.{table}")
    time.sleep(max(0, issued + sec01.EXPIRED_WAIT - time.time()))
    urls = sec01.routes(f"{uri}/v1/{inst.warehouse}", f"{uri}/colvenna/admin/v1", db, table)
    steps = sec01.requests(ctx.run, "nlb", urls, admin.tokens)
    requests = []
    for s in steps:
        path = urllib.parse.urlsplit(s["url"])
        item = {"name": s["name"], "method": s.get("method", "GET"), "path": path.path + (f"?{path.query}" if path.query else "")}
        if s.get("token"):
            item["token"] = s["token"]
        elif s.get("authorization") is not None:
            item["authorization"] = s["authorization"]
        if "body" in s:
            item["body"] = s["body"]
        requests.append(item)
    tokens = {k: v for k, v in admin.tokens.items() if k in ("forged", "expired", "revoked", "disabled")}
    answers = driver.invoke(dict(base, action="requests", requests=requests, tokens=tokens), "requests").get("requests", {})
    ctx.check("nlb-unauthenticated-refused", *judge_refused(answers, len(steps)))

    # --- from the cluster's pods, and from outside the allowed CIDRs (this host) -------------------------
    r = ctx.probe.run([{"name": "pod", "kind": "tcp", "host": host, "port": PORT, "timeout": 8}], keep="nlb-from-pod.jsonl")[0]
    try:
        addresses = sorted({i[4][0] for i in socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as e:
        addresses = [f"does not resolve: {e}"]
    outside = "no connection"
    for address in addresses:
        try:
            with socket.create_connection((address, PORT), timeout=8):
                outside = f"connected to {address}"
        except (OSError, ValueError):
            pass
    ctx.check("nlb-only-allowed-cidrs", *judge_only_allowed(r, outside))

    # --- the NLB off again --------------------------------------------------------------------------------
    arn = (lb or {}).get("LoadBalancerArn")
    groups = [g["TargetGroupArn"] for g in aws("elbv2", "describe-target-groups", "--load-balancer-arn", arn)["TargetGroups"]] if arn else []
    off = deploy.Installer(ctx, checkout, config_off).run("install.sh", *deploy.install_args(args), "--yes", keep="install-nlb-off")
    left = []
    for _ in range(30):
        left = [a for a in [arn] + groups if a and target_exists(aws, a)]
        if not left:
            break
        time.sleep(10)
    ctx.check("nlb-removed", *judge_nlb_removed(off, arn, groups, left))


def target_exists(aws, arn):
    kind = "describe-load-balancers" if ":loadbalancer/" in arn else "describe-target-groups"
    key = "--load-balancer-arns" if kind == "describe-load-balancers" else "--target-group-arns"
    return aws.raw("elbv2", kind, key, arn, check=False).returncode == 0
