"""UAT-SEC-03 No way around the intended entry; health endpoints not exposed (from Colvenna Lake's
verify-catalog.sh, and tests/serverless/outside.py).

With a valid admin token, so that only the path is wrong. EKS form: the API port from a namespace
without the client label, the health port from another pod and on the pod's own address from inside
it (and on 127.0.0.1 there, which answers), the metrics port with and without the monitoring label,
and no load balancer but the internal NLB. Serverless form: from this host, outside every VPC; through
an execute-api endpoint of the test environment's VPC that the install does not list, made for the
run and deleted after it; Lambda Invoke on the server and authorizer functions by an IAM role of the
run's own with no permission (deleted after it), and the functions' resource policies; the database's
security group, and a pod of the test environment trying to connect to it. The judging functions
take what was collected and nothing else (test_sec03.py)."""

import json
import re
import subprocess
import sys
import time

from uatlib.catalog import catalog_pods, db_endpoint, db_instance
from uatlib.install import API_PORT, HEALTH_PORT, METRICS_PORT, REPO
from uatlib.probe import Probe, by_name, describe

ID = "UAT-SEC-03"
FORMS = ("eks", "serverless")
NO_CONNECTION = ("timeout", "refused", "unreachable")
SCHEME = "service.beta.kubernetes.io/aws-load-balancer-scheme"


def checks(form, args=None):
    if form == "eks":
        return ["eks-unlabelled-namespace", "eks-health-port", "eks-metrics-port", "eks-no-public-load-balancer"]
    return ["serverless-outside-the-vpc", "serverless-unlisted-endpoint", "serverless-direct-invoke",
            "serverless-database-only-from-the-functions"]


# --- judging ------------------------------------------------------------------------------------------

def no_connection(result):
    return bool(result) and result.get("connect") in NO_CONNECTION and "status" not in result


def judge_unlabelled(results):
    """(negative) no TCP connection to the API port, nor an answer to a request, from the namespace."""
    ok = bool(results) and all(no_connection(r) or ("status" not in r and r.get("error")) for r in results.values())
    return ok, "; ".join(f"{k}: {v.get('connect') or v.get('status') or v.get('error')}" for k, v in results.items())


def judge_health(from_client, inside):
    """from_client: the TCP tries from a client pod; inside: [(pod, curl's code on the pod's own address,
    curl's exit there, curl's code on 127.0.0.1)]: no connection but on loopback, where it is 200."""
    ok = (bool(from_client) and all(no_connection(r) for r in from_client.values()) and bool(inside)
          and all(code in ("000", "") and rc != 0 and loop == "200" for _, code, rc, loop in inside))
    return ok, ("from another pod: " + ", ".join(str(v.get("connect")) for v in from_client.values())
                + "; inside each pod, on its own address / on 127.0.0.1: "
                + ", ".join(f"{code or '000'} (curl {rc}) / {loop}" for _, code, rc, loop in inside))


def judge_metrics(without, with_label):
    ok = (bool(without) and all(no_connection(r) for r in without.values()) and bool(with_label)
          and len(with_label) == len(without) and all(r.get("status") == 200 for r in with_label.values()))
    return ok, ("without the label: " + ", ".join(str(v.get("connect")) for v in without.values())
                + "; with it: " + ", ".join(str(v.get("status", v.get("error"))) for v in with_label.values()))


def judge_load_balancers(services, namespace, nlb_on, cluster_lbs):
    """services: the cluster's Services; cluster_lbs: {name: scheme} of the load balancers the cluster's
    controller made."""
    balancers = [s for s in services if s["spec"].get("type") == "LoadBalancer"]
    not_internal = [f"{s['metadata']['namespace']}/{s['metadata']['name']}" for s in balancers
                    if (s["metadata"].get("annotations") or {}).get(SCHEME) != "internal"]
    mine = [s for s in balancers if s["metadata"]["namespace"] == namespace]
    public = [n for n, scheme in cluster_lbs.items() if scheme != "internal"]
    ok = not not_internal and not public and len(mine) == (1 if nlb_on else 0)
    return ok, (f"{len(balancers)} Services of type LoadBalancer ({len(mine)} the install's; the internal NLB "
                f"{'on' if nlb_on else 'off'}); not internal: {not_internal}; the cluster's load balancers: "
                f"{len(cluster_lbs)}, internet-facing: {public}")


def judge_outside(returncode, output):
    """(negative) tests/serverless/outside.py's lines: each name does not resolve here, or resolves to
    private addresses only and gets no answer."""
    lines = [line for line in output.splitlines() if line.strip()]
    good = [line for line in lines if "does not resolve" in line
            or (re.search(r"resolves to \d+ private address", line) and "anonymous GET: no answer" in line)]
    ok = returncode == 0 and len(lines) == 2 and len(good) == 2
    return ok, f"outside.py exit {returncode}: " + " | ".join(lines)


def judge_unlisted(listed_ids, vpce, unlisted, listed):
    """(negative) through the unlisted endpoint, 403 from the API's resource policy (an explicit deny of
    the endpoint), while the listed one answers 200."""
    text = json.dumps((unlisted or {}).get("body"))
    by_policy = "not authorized" in text and "explicit deny" in text.lower()
    ok = vpce not in listed_ids and (unlisted or {}).get("status") == 403 and by_policy and (listed or {}).get("status") == 200
    return ok, f"through the unlisted endpoint: {describe(unlisted)[:220]}; through the listed one: {(listed or {}).get('status')}"


def principal_services(statement):
    principal = statement.get("Principal")
    if isinstance(principal, str):
        return [principal]
    service = (principal or {}).get("Service", [])
    others = [v for k, v in (principal or {}).items() if k != "Service"]
    return ([service] if isinstance(service, str) else list(service)) + [str(o) for o in others]


def judge_invoke(results, policies, api_id):
    """results: {function: what Invoke by the role without rights got}; policies: {function: statements}
    of each function and of the server's alias: only API Gateway, for this API."""
    wrong = []
    for fn, statements in policies.items():
        if not statements:
            wrong.append(f"{fn}: no resource policy")
        for st in statements:
            services = principal_services(st)
            if services != ["apigateway.amazonaws.com"] or api_id not in json.dumps(st.get("Condition", {})):
                wrong.append(f"{fn}: {st.get('Sid')}")
    ok = bool(results) and all(v == "AccessDenied" for v in results.values()) and not wrong
    return ok, f"Invoke by a role with no permission: {results}; statements admitting more than API Gateway for this API: {wrong}"


def judge_database(groups, port, functions_group, tried):
    """(negative) the database's security groups admit its port only from the functions' security
    group, from no CIDR; a pod of the test environment cannot connect."""
    admitted, cidrs = [], []
    for g in groups:
        for perm in g.get("IpPermissions", []):
            if perm["IpProtocol"] != "-1" and not (perm.get("FromPort", 0) <= port <= perm.get("ToPort", 0)):
                continue
            admitted += [pair["GroupId"] for pair in perm.get("UserIdGroupPairs", [])]
            cidrs += [r["CidrIp"] for r in perm.get("IpRanges", [])] + [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", [])]
    ok = bool(groups) and set(admitted) == {functions_group} and not cidrs and no_connection(tried)
    return ok, (f"port {port} admitted from {'the functions’ group only' if set(admitted) == {functions_group} else admitted}"
                f", from CIDRs {cidrs}; a pod of the test environment: {(tried or {}).get('connect')}")


# --- EKS ----------------------------------------------------------------------------------------------

def run(ctx):
    (eks if ctx.form == "eks" else serverless)(ctx)


def eks(ctx):
    inst = ctx.install
    pods = catalog_pods(ctx)
    ips = [p["status"]["podIP"] for p in pods]
    with ctx.preparing("a namespace without the client label, with a probe"):
        outsider = Probe(ctx.kube, ctx.namespace("outsider", client=False), name="outsider", base=inst.uri, ca=inst.ca(),
                         evidence=ctx.evidence, sanitizer=ctx.sanitizer).start()
    steps = [{"name": "service", "kind": "tcp", "host": inst.host, "port": API_PORT, "timeout": 5}]
    steps += [{"name": f"pod-{i}", "kind": "tcp", "host": ip, "port": API_PORT, "timeout": 5} for i, ip in enumerate(ips)]
    steps.append({"name": "request", "path": f"/v1/config?warehouse={inst.warehouse}", "token": "admin", "timeout": 8})
    ctx.check("eks-unlabelled-namespace", *judge_unlabelled(by_name(outsider.run(steps, tokens=ctx.admin.tokens,
                                                                                  keep="outsider.jsonl"))))

    from_client = by_name(ctx.probe.run([{"name": f"health-{i}", "kind": "tcp", "host": ip, "port": HEALTH_PORT, "timeout": 5}
                                         for i, ip in enumerate(ips)], keep="health.jsonl"))
    inside = []
    for p, ip in zip(pods, ips):
        name = p["metadata"]["name"]
        curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5"]
        own = ctx.kube.exec(inst.namespace, name, curl + [f"http://{ip}:{HEALTH_PORT}/readyz"], check=False,
                            container="colvenna-catalog")
        loop = ctx.kube.exec(inst.namespace, name, curl + [f"http://127.0.0.1:{HEALTH_PORT}/readyz"], check=False,
                             container="colvenna-catalog")
        inside.append((name, own.stdout.strip(), own.returncode, loop.stdout.strip()))  # curl prints 000: no connection
    ctx.check("eks-health-port", *judge_health(from_client, inside))

    without = by_name(outsider.run([{"name": f"metrics-{i}", "kind": "tcp", "host": ip, "port": METRICS_PORT, "timeout": 5}
                                    for i, ip in enumerate(ips)], keep="metrics-without.jsonl"))
    with ctx.preparing("a namespace with the monitoring label, with a probe"):
        monitor = Probe(ctx.kube, ctx.namespace("monitor", client=False, monitoring=True), name="monitor",
                        evidence=ctx.evidence, sanitizer=ctx.sanitizer).start()
    with_label = by_name(monitor.run([{"name": f"metrics-{i}", "url": f"http://{ip}:{METRICS_PORT}/metrics"}
                                      for i, ip in enumerate(ips)], keep="metrics-with.jsonl"))
    ctx.check("eks-metrics-port", *judge_metrics(without, with_label))

    services = ctx.kube.json("get", "services", "-A")["items"]
    elbs = ctx.aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    arns = [e["LoadBalancerArn"] for e in elbs]
    tagged = set()
    for i in range(0, len(arns), 20):
        for d in ctx.aws("elbv2", "describe-tags", "--resource-arns", *arns[i:i + 20])["TagDescriptions"]:
            if any(t["Key"] == "elbv2.k8s.aws/cluster" and t["Value"] == ctx.testenv.cluster for t in d["Tags"]):
                tagged.add(d["ResourceArn"])
    cluster_lbs = {e["LoadBalancerName"]: e["Scheme"] for e in elbs if e["LoadBalancerArn"] in tagged}
    ctx.check("eks-no-public-load-balancer", *judge_load_balancers(services, inst.namespace,
                                                                   inst.get("COLVENNA_INTERNAL_NLB") == "on", cluster_lbs))


# --- serverless -------------------------------------------------------------------------------------

def serverless(ctx):
    out = ctx.install.outputs("Serverless")
    p = subprocess.run([sys.executable, "-I", str(REPO / "tests" / "serverless" / "outside.py"), out["ApiUrl"],
                        out["ApiEndpointUrl"], ctx.install.warehouse], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=300)
    ctx.keep("outside-vpc.txt", p.stdout)
    ctx.check("serverless-outside-the-vpc", *judge_outside(p.returncode, p.stdout))
    unlisted_endpoint(ctx, out)
    direct_invoke(ctx, out)
    database(ctx)


def unlisted_endpoint(ctx, out):
    aws, env = ctx.aws, ctx.testenv
    with ctx.preparing("an execute-api endpoint in the test environment's VPC that the install does not list") as notes:
        existing = aws("ec2", "describe-vpc-endpoints", "--vpc-endpoint-ids", env.output("ApiEndpointId"))["VpcEndpoints"][0]
        made = aws("ec2", "create-vpc-endpoint", "--vpc-id", env.output("VpcId"), "--vpc-endpoint-type", "Interface",
                   "--service-name", f"com.amazonaws.{aws.region}.execute-api", "--no-private-dns-enabled",
                   "--subnet-ids", *existing["SubnetIds"], "--security-group-ids", *[g["GroupId"] for g in existing["Groups"]],
                   "--tag-specifications",
                   f"ResourceType=vpc-endpoint,Tags=[{{Key=Purpose,Value=test-environment}},{{Key=colvenna-uat/run,Value={ctx.run}}}]"
                   )["VpcEndpoint"]
        vpce = made["VpcEndpointId"]

        def delete():
            aws("ec2", "delete-vpc-endpoints", "--vpc-endpoint-ids", vpce)
            for _ in range(60):
                left = aws("ec2", "describe-vpc-endpoints", "--filters", f"Name=vpc-endpoint-id,Values={vpce}")["VpcEndpoints"]
                if not left or left[0]["State"].lower() == "deleted":
                    return f"endpoint {vpce} deleted"
                time.sleep(10)
            raise RuntimeError(f"endpoint {vpce} still there")

        ctx.on_cleanup("the unlisted execute-api endpoint deleted", delete)
        state = {}
        for _ in range(60):
            state = aws("ec2", "describe-vpc-endpoints", "--vpc-endpoint-ids", vpce)["VpcEndpoints"][0]
            if state["State"].lower() == "available":
                break
            time.sleep(10)
        if state.get("State", "").lower() != "available":
            raise RuntimeError(f"endpoint {vpce} is {state.get('State')}")
        notes.append("made, available")
    dns = state["DnsEntries"][0]["DnsName"]  # <vpce-id>-<suffix>.execute-api.<region>.vpce.amazonaws.com
    path = f"/catalog/v1/config?warehouse={ctx.install.warehouse}"
    # A new endpoint takes its connections a minute or so after it is available: until then, timeouts.
    for _ in range(18):
        r = by_name(ctx.probe.run([{"name": "unlisted", "url": f"https://{dns}{path}", "token": "admin",
                                    "headers": {"x-apigw-api-id": out["ApiId"]}},
                                   {"name": "listed", "url": f"{ctx.install.uri}/v1/config?warehouse={ctx.install.warehouse}",
                                    "token": "admin"}], tokens=ctx.admin.tokens, keep="unlisted-endpoint.jsonl"))
        if "status" in r["unlisted"]:
            break
        time.sleep(10)
    ctx.check("serverless-unlisted-endpoint", *judge_unlisted(ctx.install.get("COLVENNA_API_ENDPOINT_IDS").split(","), vpce,
                                                              r.get("unlisted"), r.get("listed")))


def direct_invoke(ctx, out):
    aws = ctx.aws
    account = aws.account()
    role = f"colvenna-uat-norights-{ctx.run}"
    session = {}
    with ctx.preparing("an IAM role of the run's own with no permission, and a session of it"):
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                         "Principal": {"AWS": f"arn:aws:iam::{account}:root"}}]}
        aws("iam", "create-role", "--role-name", role, "--assume-role-policy-document", json.dumps(trust),
            "--description", "UAT-SEC-03: an identity with no permission; deleted with the run", "--max-session-duration",
            "3600", "--tags", "Key=Purpose,Value=test-environment", f"Key=colvenna-uat/run,Value={ctx.run}")
        ctx.on_cleanup(f"IAM role {role} deleted", lambda: aws("iam", "delete-role", "--role-name", role) or f"{role} deleted")
        ctx.sanitizer.secret_sources.append(lambda: [session.get("secretAccessKey"), session.get("sessionToken")])
        for _ in range(12):  # IAM's eventual consistency
            p = aws.raw("sts", "assume-role", "--role-arn", f"arn:aws:iam::{account}:role/{role}", "--role-session-name",
                        f"uat-{ctx.run}", "--output", "json", check=False)
            if p.returncode == 0:
                c = json.loads(p.stdout)["Credentials"]
                session.update(accessKeyId=c["AccessKeyId"], secretAccessKey=c["SecretAccessKey"], sessionToken=c["SessionToken"])
                break
            time.sleep(10)
        if not session:
            raise RuntimeError("the role could not be assumed")
    results = {}
    for fn in (out["ServerFunctionName"], out["AuthorizerFunctionName"]):
        for _ in range(6):  # a new session's first calls may still be refused as unknown
            p = aws.raw("lambda", "invoke", "--function-name", fn, "--payload", "e30=", "/dev/null", credentials=session,
                        check=False)
            results[fn] = "AccessDenied" if "AccessDenied" in p.stderr else (p.stderr.strip()[-160:] or f"exit {p.returncode}")
            if "UnrecognizedClient" not in p.stderr and "InvalidClientTokenId" not in p.stderr:
                break
            time.sleep(10)
    policies = {}
    for fn, qualifier in ((out["ServerFunctionName"], None), (out["ServerFunctionName"], "live"),
                          (out["AuthorizerFunctionName"], None)):
        args = ["lambda", "get-policy", "--function-name", fn] + (["--qualifier", qualifier] if qualifier else [])
        p = aws.raw(*args, "--output", "json", check=False)
        key = fn + (f":{qualifier}" if qualifier else "")
        policies[key] = json.loads(json.loads(p.stdout)["Policy"])["Statement"] if p.returncode == 0 else []
    # The server function itself may carry no policy when only its alias is invoked: judged together.
    server = out["ServerFunctionName"]
    if not policies[server] and policies[f"{server}:live"]:
        policies.pop(server)
    ctx.keep("function-policies.json", policies)
    ctx.check("serverless-direct-invoke", *judge_invoke(results, policies, out["ApiId"]))


def database(ctx):
    host, port = db_endpoint(ctx.install)
    db = db_instance(ctx)
    groups = []
    if db:
        ids = [g["VpcSecurityGroupId"] for g in db["VpcSecurityGroups"]]
        groups = ctx.aws("ec2", "describe-security-groups", "--group-ids", *ids)["SecurityGroups"]
    tried = ctx.probe.run([{"name": "database", "kind": "tcp", "host": host, "port": port, "timeout": 8}], keep="database.jsonl")[0]
    ctx.check("serverless-database-only-from-the-functions",
              *judge_database(groups, port, ctx.install.output("Network", "CatalogSecurityGroupId"), tried))
