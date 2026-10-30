"""The catalog's REST API (Paimon's, under /v1/<warehouse>), for what a case makes and removes for
itself without an engine: databases and tables of its own, with the admin's token."""

import urllib.parse

from .probe import describe


def q(name):
    return urllib.parse.quote(name, safe="")


def field(i, name, kind):
    return {"id": i, "name": name, "type": kind}


def table_body(database, table, fields, primary_keys=(), partition_keys=(), options=None):
    return {"identifier": {"database": database, "object": table},
            "schema": {"fields": [field(i, n, t) for i, (n, t) in enumerate(fields)],
                       "partitionKeys": list(partition_keys), "primaryKeys": list(primary_keys),
                       "options": dict(options or {})}}


class Rest:
    def __init__(self, probe, base, tokens):
        self.probe = probe
        self.base = base.rstrip("/")  # https://<catalog>/v1/<warehouse>
        self.tokens = tokens  # the admin's tokens dict, shared
        self.n = 0

    def call(self, method, path, body=None, token="admin", expect=(200,), timeout=None):
        self.n += 1
        step = {"name": f"rest-{self.n}", "method": method, "url": self.base + path, "token": token}
        if timeout:
            step["timeout"] = timeout
        if body is not None:
            step["body"] = body
        result = self.probe.run([step], tokens=self.tokens, keep="rest-calls.jsonl")[0]
        if expect and result.get("status") not in expect:
            raise RuntimeError(f"{method} {path}: expected {expect}, got {describe(result)}")
        return result

    def create_database(self, name):
        return self.call("POST", "/databases", {"name": name, "options": {}})

    def create_table(self, database, table, fields, **kw):
        return self.call("POST", f"/databases/{q(database)}/tables", table_body(database, table, fields, **kw))

    def get_table(self, database, table, token="admin"):
        return self.call("GET", f"/databases/{q(database)}/tables/{q(table)}", token=token)["body"]

    def tables(self, database):
        return self.call("GET", f"/databases/{q(database)}/tables?maxResults=1000")["body"].get("tables") or []

    def databases(self):
        return self.call("GET", "/databases?maxResults=1000")["body"].get("databases") or []

    def drop_table(self, database, table):
        return self.call("DELETE", f"/databases/{q(database)}/tables/{q(table)}", expect=(200, 204, 404))

    def drop_database(self, database):
        """Drops the database and every table of it; returns whether it is gone."""
        if database not in self.databases():
            return True
        for table in self.tables(database):
            self.drop_table(database, table)
        self.call("DELETE", f"/databases/{q(database)}", expect=(200, 204, 404))
        return database not in self.databases()
