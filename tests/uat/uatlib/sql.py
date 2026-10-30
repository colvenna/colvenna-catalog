"""SQL on the install's metadata database, for what a case reads or does there directly: the schema
version, the commit intents, ending sessions, closing the database to connections, holding a lock.

EKS form: psql in a pod of the run's own namespace (the database admits the cluster's nodes), as the
catalog's own user (the Secret the chart reads it from) or as the database's admin (the install's
admin secret), verifying the server's certificate against the RDS CA the install keeps in the
catalog's namespace. The passwords reach psql through its environment only. Serverless form: the
database admits only the catalog's functions, so the statements go through the test driver's
"sql" action inside the catalog's VPC (tests/serverless/driver)."""

import base64
import json
import subprocess

from .catalog import db_endpoint
from .kube import hardened, pod

# As deploy/lib/catalog.sh pins it.
POSTGRES_IMAGE = "postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
ROLES = ("catalog", "admin")


class SqlError(RuntimeError):
    pass


class Sql:
    def __init__(self, ctx):
        self.ctx = ctx
        self.install = ctx.install
        self.pods = {}

    def admin_secret_arn(self):
        if self.install.get("COLVENNA_DB") == "existing":
            return self.install.get("COLVENNA_DB_ADMIN_SECRET_ARN")
        return self.install.output("Storage", "DbAdminSecretArn")

    def _credentials(self, role):
        if role == "catalog":
            data = self.ctx.kube.json("get", "secret", "colvenna-catalog-db", "-n", self.install.namespace)["data"]
            return {k: base64.b64decode(v).decode() for k, v in data.items() if k in ("username", "password")}
        secret = self.ctx.aws.raw("secretsmanager", "get-secret-value", "--secret-id", self.admin_secret_arn(),
                                  "--query", "SecretString", "--output", "text").stdout
        value = json.loads(secret)
        return {"username": value["username"], "password": value["password"]}

    def _pod(self, role):
        if role in self.pods:
            return self.pods[role]
        kube = self.ctx.kube
        ns = self.ctx.namespace("sql", client=False)
        creds = self._credentials(role)
        self.ctx.sanitizer.secret_sources.append(lambda c=creds: [c["password"]])
        host, port = db_endpoint(self.install)
        database = self.install.get("COLVENNA_DB_NAME")
        name = f"psql-{role}"
        # A password file, never the environment: psql takes it from a volume of its own, in memory, where
        # the pod's own user made it with the mode psql wants (0600), from the Secret's copy.
        line = ":".join(v.replace("\\", "\\\\").replace(":", "\\:") for v in (host, str(port), "*", creds["username"],
                                                                         creds["password"]))
        kube.secret(ns, f"{name}-login", {"pgpass": line + "\n"})
        ca = kube.json("get", "configmap", "colvenna-catalog-db-ca", "-n", self.install.namespace)["data"]["ca.pem"]
        kube.configmap(ns, "db-ca", {"ca.pem": ca})
        container = {
            "name": "psql", "image": POSTGRES_IMAGE,
            "command": ["sh", "-c", "cp /login/pgpass /pgpass/.pgpass && chmod 600 /pgpass/.pgpass && exec sleep 86400"],
            "securityContext": hardened(70),
            "env": [{"name": "PGHOST", "value": host}, {"name": "PGPORT", "value": str(port)},
                    {"name": "PGUSER", "value": creds["username"]}, {"name": "PGDATABASE", "value": database},
                    {"name": "PGPASSFILE", "value": "/pgpass/.pgpass"}, {"name": "PGSSLMODE", "value": "verify-full"},
                    {"name": "PGSSLROOTCERT", "value": "/ca/ca.pem"}, {"name": "PGCONNECT_TIMEOUT", "value": "10"}],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "volumeMounts": [{"name": "ca", "mountPath": "/ca", "readOnly": True},
                             {"name": "login", "mountPath": "/login", "readOnly": True},
                             {"name": "pgpass", "mountPath": "/pgpass"},
                             {"name": "tmp", "mountPath": "/tmp"}]}  # nosec B108 - the pod's own /tmp, an emptyDir
        kube.apply(pod(ns, name, [container], [{"name": "ca", "configMap": {"name": "db-ca"}},
                                               {"name": "login", "secret": {"secretName": f"{name}-login",
                                                                            "defaultMode": 0o444}},
                                               {"name": "pgpass", "emptyDir": {"medium": "Memory"}},
                                               {"name": "tmp", "emptyDir": {}}]))
        kube.wait_ready(ns, name)
        self.pods[role] = (ns, name)
        return ns, name

    def query(self, statement, role="catalog", database=None, timeout=300, background=False):
        """The rows of the statement (lists of strings, tab-separated by psql). background: the
        kubectl process, for a statement that holds a lock while the case goes on."""
        if self.install.form != "eks":
            return self.ctx.driver.sql(statement, role=role, database=database, timeout=timeout)
        if role not in ROLES:
            raise ValueError(role)
        ns, name = self._pod(role)
        args = ["psql", "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"] + (["-d", database] if database else [])
        if background:
            return subprocess.Popen(["kubectl", "exec", "-i", name, "-n", ns, "--", *args], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                    env=self.ctx.kube.env)
        p = self.ctx.kube.exec(ns, name, args, input=statement, timeout=timeout, check=False)
        if p.returncode != 0:
            raise SqlError(f"psql failed ({p.returncode}): {p.stderr.strip()[-800:]}")
        return [line.split("\t") for line in p.stdout.splitlines() if line]
