"""UAT-CAT-07 Health, schema version and metrics (from Colvenna Lake's check-health.sh).

Reads the install's running state, each form its own, and changes nothing: in the EKS form the
Deployment, its pods and their nodes, the PodDisruptionBudget, the health port inside each pod and
the metrics port from a namespace with the monitoring label (whose text must name no database or
table of the catalog); in the serverless form the functions' configuration (memory, concurrency,
timeout, the alias the API invokes, SnapStart, no provisioned concurrency, no secret) and the server
function's log of the case's own requests. In both, the metadata database's schema version, read
with a read-only query, against the candidate's migrations. The judging functions take what was
collected and nothing else (test_cat07.py)."""

import re
import time

from uatlib.catalog import access_log, answered_by, catalog_pods
from uatlib.install import HEALTH_PORT, METRICS_PORT, REPO
from uatlib.probe import Probe, by_name, describe
from uatlib.sql import Sql

ID = "UAT-CAT-07"
FORMS = ("eks", "serverless")
METRICS = ("colvenna_catalog_requests_total", "colvenna_catalog_request_seconds", "colvenna_catalog_commits_total")
# A variable named like a password, a secret or a credential; a value that is a secret's ARN, an access
# key or a catalog token. A per-table credentials setting (COLVENNA_DATA_TOKEN_*), or the class that
# provides the S3 FileIO's credentials (COLVENNA_FILEIO_S3_AWS_CREDENTIALS_PROVIDER), is neither.
SECRET_NAME = re.compile(r"(?i)pass(word|phrase)?|secret|credential(?!s?_provider)|private.?key|access.?key|api.?key")
SECRET_VALUE = re.compile(r"arn:aws[a-z-]*:secretsmanager:|\b(AKIA|ASIA)[0-9A-Z]{16}\b|colvenna_[A-Za-z0-9_-]{16,}")


def checks(form, args=None):
    own = (["eks-replicas", "eks-readyz", "eks-metrics"] if form == "eks"
           else ["serverless-functions", "serverless-no-secret-in-functions", "serverless-logs"])
    return own + ["schema-version"]


def migrations():
    """The candidate's migrations: the scripts V<n>__*.sql it carries."""
    return sorted(p.name for p in (REPO / "src" / "main" / "resources" / "db" / "migration").glob("V*__*.sql"))


# --- judging ------------------------------------------------------------------------------------------

def judge_schema(rows, expected):
    versions = [int(r[0]) for r in rows]
    ok = bool(versions) and versions == list(range(1, len(expected) + 1))
    return ok, (f"schema version rows {versions} ({', '.join(r[1] for r in rows)}); the candidate's migrations: "
                f"{expected}")


def judge_replicas(deploy_status, ready_pods, pdbs, hpas):
    """ready_pods: [(name, node)]."""
    nodes = {n for _, n in ready_pods}
    min_available = [p.get("minAvailable") for p in pdbs]
    ok = deploy_status.get("readyReplicas") == 2 and len(ready_pods) == 2 and len(nodes) == 2 and min_available == [1] \
        and not hpas
    return ok, (f"{deploy_status.get('readyReplicas')} ready replicas on {len(nodes)} nodes; PodDisruptionBudget "
                f"minAvailable {min_available}; {len(hpas)} HorizontalPodAutoscaler")


def judge_readyz(answers):
    """answers: [(pod, what curl printed)]."""
    return len(answers) == 2 and all(a == "200" for _, a in answers), "; ".join(f"{n}: {a}" for n, a in answers)


def judge_metrics(results, names):
    """results: {pod: probe result of GET /metrics}; names: the catalog's databases and tables, none of
    which may be in the metrics' text."""
    problems = []
    for pod, r in results.items():
        text = r.get("body") if isinstance(r.get("body"), str) else ""
        if r.get("status") != 200:
            problems.append(f"{pod}: {describe(r)[:120]}")
            continue
        missing = [m for m in METRICS if m not in text]
        if missing:
            problems.append(f"{pod}: no {', '.join(missing)}")
        leaked = [n for n in names if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", text)]
        if leaked:
            problems.append(f"{pod}: names {len(leaked)} of the catalog's objects")
    ok = len(results) == 2 and not problems
    return ok, "; ".join(problems) or (f"{len(results)} pods: the three metrics; none of the catalog's {len(names)} "
                                       f"databases and tables named")


def judge_functions(config, settings):
    """config: what was read of the functions; settings: the install's COLVENNA_LAMBDA_* and SNAPSTART."""
    want_snap = "PublishedVersions" if settings["snapstart"] == "on" else "None"
    integrations = config["integrations"]
    via_alias = bool(integrations) and all(f"function:{config['server']}:live/invocations" in u for u in integrations)
    ok = (config["memory"] == settings["memory"] and config["reserved"] == settings["concurrency"]
          and config["timeout"] == 60 and via_alias and config["snapstart"] == want_snap and not config["provisioned"]
          and all(config["others"].values()) and len(config["others"]) == 4)
    return ok, (f"memory {config['memory']}, reserved concurrency {config['reserved']}, timeout {config['timeout']} s, "
                f"{len(integrations)} integrations through the alias live: {via_alias}, SnapStart {config['snapstart']} "
                f"(want {want_snap}), {len(config['provisioned'])} provisioned, the other functions {config['others']}")


def secret_variables(functions):
    """functions: {name: environment variables}; the variables that hold or name a secret."""
    return [f"{fn}: {k}" for fn, env in functions.items() for k, v in env.items()
            if SECRET_NAME.search(k) or SECRET_VALUE.search(str(v))]


def judge_logs(answered, logged):
    ids = [r.get("serverRequestId") for r in answered if r.get("serverRequestId")]
    ok = len(answered) == 3 and all(r.get("status") == 200 for r in answered) and len(ids) == 3 \
        and set(ids) <= logged
    return ok, (f"{len(answered)} requests answered {[r.get('status') for r in answered]}, {len(ids)} with the "
                f"server's request id; {len(set(ids) & logged)} of them in its log group's access log")


# --- the run ------------------------------------------------------------------------------------------

def run(ctx):
    (eks if ctx.form == "eks" else serverless)(ctx)
    try:
        rows = Sql(ctx).query("SELECT version, script FROM colvenna_schema_version ORDER BY version")
    except NotImplementedError as e:
        ctx.check("schema-version", False, f"not read: {e}")
        return
    ctx.check("schema-version", *judge_schema(rows, migrations()))


def catalog_names(ctx):
    """Every database the catalog lists (the admin's view) but the default one, and each of its tables
    as <database>.<table>: names a metric must not carry (a bare table name, t say, could be any word)."""
    names = []
    for db in ctx.rest.databases():
        if db != "default":
            names.append(db)
            names += [f"{db}.{t}" for t in ctx.rest.tables(db)]
    return sorted(set(names))


def eks(ctx):
    inst = ctx.install
    deploy = ctx.kube.json("get", "deployment", inst.release, "-n", inst.namespace)
    pods = catalog_pods(ctx)
    ready = [p for p in pods if any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]
    pdbs = [p["spec"] for p in ctx.kube.json("get", "pdb", "-n", inst.namespace)["items"]]
    hpas = ctx.kube.json("get", "hpa", "-n", inst.namespace)["items"]
    placed = [(p["metadata"]["name"], p["spec"].get("nodeName")) for p in ready]
    ctx.keep("eks-state.json", {"deployment": deploy.get("status"), "pods": placed, "pdb": pdbs, "hpa": len(hpas)})
    ctx.check("eks-replicas", *judge_replicas(deploy.get("status", {}), placed, pdbs, hpas))

    answers = []
    for p in ready:
        name = p["metadata"]["name"]
        out = ctx.kube.exec(inst.namespace, name, ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
                                                   f"http://127.0.0.1:{HEALTH_PORT}/readyz"], check=False,
                            container="colvenna-catalog")
        answers.append((name, out.stdout.strip() or out.stderr.strip()[-120:]))
    ctx.check("eks-readyz", *judge_readyz(answers))

    names = catalog_names(ctx)
    monitor_ns = ctx.namespace("monitor", client=False, monitoring=True)
    monitor = Probe(ctx.kube, monitor_ns, name="monitor", evidence=ctx.evidence, sanitizer=ctx.sanitizer).start()
    steps = [{"name": p["metadata"]["name"], "url": f"http://{p['status']['podIP']}:{METRICS_PORT}/metrics", "full": True}
             for p in ready]
    results = by_name(monitor.run(steps, keep="metrics.jsonl"))
    ctx.check("eks-metrics", *judge_metrics(results, names))


def serverless(ctx):
    inst, aws = ctx.install, ctx.aws
    out = inst.outputs("Serverless")
    server = out["ServerFunctionName"]
    conf = aws("lambda", "get-function-configuration", "--function-name", server, "--qualifier", "live")
    api = out["ApiId"]
    integrations = []
    for resource in aws("apigateway", "get-resources", "--rest-api-id", api)["items"]:
        for method in (resource.get("resourceMethods") or {}):
            integrations.append(aws("apigateway", "get-integration", "--rest-api-id", api, "--resource-id", resource["id"],
                                    "--http-method", method).get("uri", ""))
    others = {k: out.get(k) for k in ("AuthorizerFunctionName", "MaintenanceFunctionName", "PurgeFunctionName",
                                      "OpsFunctionName")}
    config = {
        "server": server, "memory": conf["MemorySize"], "timeout": conf["Timeout"],
        "reserved": (aws("lambda", "get-function-concurrency", "--function-name", server) or {}).get("ReservedConcurrentExecutions"),
        "provisioned": aws("lambda", "list-provisioned-concurrency-configs", "--function-name", server)["ProvisionedConcurrencyConfigs"],
        "snapstart": (conf.get("SnapStart") or {}).get("ApplyOn", "None"), "integrations": integrations,
        "others": {k: bool(v) and aws.raw("lambda", "get-function", "--function-name", v, check=False).returncode == 0
                   for k, v in others.items()},
    }
    ctx.keep("functions.json", config)
    settings = {"memory": int(inst.get("COLVENNA_LAMBDA_MEMORY")), "concurrency": int(inst.get("COLVENNA_LAMBDA_CONCURRENCY")),
                "snapstart": inst.get("COLVENNA_SNAPSTART")}
    ctx.check("serverless-functions", *judge_functions(config, settings))

    envs = {}
    for name in [server] + [v for v in others.values() if v]:
        envs[name] = (aws("lambda", "get-function-configuration", "--function-name", name).get("Environment") or {}).get("Variables") or {}
    found = secret_variables(envs)
    ctx.check("serverless-no-secret-in-functions", not found and len(envs) >= 4,
              f"{len(envs)} functions; variables named like a secret or holding one: {found}")

    since = time.time()
    answered = ctx.probe.run([{"name": f"request-{i}", "url": f"{inst.uri}/v1/config?warehouse={inst.warehouse}",
                               "token": "admin"} for i in range(3)], tokens=ctx.admin.tokens, keep="requests.jsonl")
    ids = {r["serverRequestId"] for r in answered if r.get("serverRequestId")}
    logged = set()
    for _ in range(8):  # the log arrives within a minute or two
        logged = set().union(*answered_by(access_log(ctx, since), ids).values())
        if ids and ids <= logged:
            break
    ctx.check("serverless-logs", *judge_logs(answered, logged))
