"""The serverless form's test driver (tests/serverless/driver) as the harness's instrument inside the
catalog's VPC: its "sql" action runs statements on the metadata database, which admits only the
catalog's functions. tests/serverless/run.sh deploys it (action deploy, DRIVER_DATABASE_ACCESS=on):
then it also joins the functions' security group, and its role may sign in to the database as the
catalog's user with RDS IAM authentication, so no password is used or held. The driver stays for the
install's other cases; tests/serverless/down.sh removes it before the install is uninstalled."""

import json
import os
import subprocess

from .install import REPO


class DriverError(RuntimeError):
    pass


class Driver:
    def __init__(self, ctx):
        self.ctx = ctx
        self.install = ctx.install
        self.function = f"colvenna-catalog-{self.install.env}-test-driver"
        self.ready = False

    def endpoint(self):
        if self.install.get("COLVENNA_DB") == "existing":
            return self.install.get("COLVENNA_DB_ENDPOINT"), int(self.install.get("COLVENNA_DB_PORT") or 5432)
        return self.install.output("Storage", "DbEndpoint"), int(self.install.output("Storage", "DbPort"))

    def resource_id(self):
        host, _ = self.endpoint()
        for db in self.ctx.aws("rds", "describe-db-instances")["DBInstances"]:
            if (db.get("Endpoint") or {}).get("Address") == host:
                return db["DbiResourceId"]
        raise DriverError(f"no RDS instance at the install's database endpoint")

    def ensure(self):
        """Deploys the driver with database access unless it is there with it already."""
        if self.ready:
            return
        group = self.install.output("Network", "CatalogSecurityGroupId")
        conf = self.ctx.aws.raw("lambda", "get-function-configuration", "--function-name", self.function,
                                "--output", "json", check=False)
        if conf.returncode == 0 and group in json.loads(conf.stdout).get("VpcConfig", {}).get("SecurityGroupIds", []):
            self.ready = True
            return
        self._deploy()

    def repoint(self):
        """Deploys the driver again for the database the install runs on now: after a restore, another
        instance, whose resource id its role must be allowed to sign in to."""
        self.ready = False
        self._deploy()

    def _deploy(self):
        env = dict(os.environ, DRIVER_DATABASE_ACCESS="on", DRIVER_DB_RESOURCE_ID=self.resource_id(),
                   DRIVER_DB_USER=self.install.get("COLVENNA_DB_USER"))
        self.ctx.log(f"deploying the test driver {self.function} with database access")
        p = subprocess.run([str(REPO / "tests" / "serverless" / "run.sh"), self.install.env, self.install.region, "deploy"],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                           timeout=1800)
        self.ctx.keep("driver-deploy.log", p.stdout)
        if p.returncode != 0:
            raise DriverError(f"the test driver did not deploy ({p.returncode}): {p.stdout[-600:]}")
        self.ready = True

    def sql(self, statement, role="catalog", database=None, timeout=300):
        """The rows of the statements (lists of strings), run as the catalog's user (the serverless
        form's database admin is not the catalog database's owner; the catalog's user is)."""
        self.ensure()
        host, port = self.endpoint()
        event = {"action": "sql", "host": host, "port": port, "user": self.install.get("COLVENNA_DB_USER"),
                 "database": database or self.install.get("COLVENNA_DB_NAME"), "region": self.install.region,
                 "statements": statement}
        path = self.ctx.scratch / "sql-event.json"
        out = self.ctx.scratch / "sql-out.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(event, f)
        p = self.ctx.aws.raw("lambda", "invoke", "--function-name", self.function, "--cli-read-timeout", str(timeout + 60),
                             "--cli-binary-format", "raw-in-base64-out", "--payload", f"fileb://{path}", str(out),
                             "--output", "json", timeout=timeout + 120, check=False)
        if p.returncode != 0:
            raise DriverError(f"invoking the driver failed: {p.stderr[-400:]}")
        meta = json.loads(p.stdout or "{}")
        body = json.loads(out.read_text() or "{}")
        if meta.get("FunctionError"):
            raise DriverError(f"the sql action failed: {body.get('errorMessage', body)}")
        return body.get("rows", [])
