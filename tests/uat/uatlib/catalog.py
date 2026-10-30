"""What several cases read of the install itself: the catalog's pods (EKS form), its access log of a
time window in either form, and its metadata database instance."""

import time

LOG_ARRIVAL = 60  # seconds a serverless function's log lines may take to reach CloudWatch Logs


def catalog_pods(ctx):
    """The install's running catalog pods (not those being deleted), EKS form."""
    inst = ctx.install
    selector = f"app.kubernetes.io/name=colvenna-catalog,app.kubernetes.io/instance={inst.release}"
    return [p for p in ctx.kube.json("get", "pods", "-n", inst.namespace, "-l", selector)["items"]
            if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]


def access_log(ctx, since, wait=True):
    """The catalog's access log lines since the time (epoch seconds): {replica: text} in the EKS form,
    {"server": text} in the serverless form (the server function's log group, read once the lines
    have had time to arrive, unless wait is False)."""
    if ctx.form == "eks":
        out = {}
        for p in catalog_pods(ctx):
            name = p["metadata"]["name"]
            out[name] = ctx.kube.kubectl("logs", name, "-n", ctx.install.namespace, "-c", "colvenna-catalog",
                                         "--since-time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 5)),
                                         check=False).stdout
        return out
    if wait:
        time.sleep(LOG_ARRIVAL)
    group = f"/aws/lambda/{ctx.install.output('Serverless', 'ServerFunctionName')}"
    events = ctx.aws("logs", "filter-log-events", "--log-group-name", group, "--start-time", str(int(since * 1000) - 5000),
                     "--filter-pattern", '"colvenna.access"', timeout=900)["events"]
    return {"server": "\n".join(e["message"] for e in events)}


def answered_by(lines, ids):
    """{replica: the request ids among these that its access log names}."""
    return {k: {i for i in ids if f"colvenna.access - {i} " in text} for k, text in lines.items()}


def db_endpoint(install):
    """(host, port) of the install's metadata database."""
    if install.get("COLVENNA_DB") == "existing":
        return install.get("COLVENNA_DB_ENDPOINT"), int(install.get("COLVENNA_DB_PORT"))
    return install.output("Storage", "DbEndpoint"), int(install.output("Storage", "DbPort"))


def db_instance(ctx):
    """The RDS instance (describe-db-instances' entry) the install's database endpoint is, or None."""
    host, _ = db_endpoint(ctx.install)
    found = [d for d in ctx.aws("rds", "describe-db-instances")["DBInstances"] if d.get("Endpoint", {}).get("Address") == host]
    return found[0] if found else None
