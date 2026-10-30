"""The catalog's admin API (/colvenna/admin/v1, docs/api/admin-v1.yaml), called from a probe pod with
the admin's token: the identities, tokens and grants a case makes for itself, and the audit it reads
back. Every grant a case makes names its purpose, approver, approval and expiry, as the API requires.
Tokens the API issues are held here, in memory, for the case's other requests; they are never
written anywhere but into the Secrets of the case's own pods."""

import urllib.parse
from datetime import datetime, timedelta, timezone

from .probe import describe

PURPOSE = "UAT (docs/uat/cases.md)"
APPROVER = "uat-operator@example.com"


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_hours(hours):
    return iso(datetime.now(timezone.utc) + timedelta(hours=hours))


class AdminError(RuntimeError):
    pass


class Admin:
    # What every grant names as its purpose and approver (the performance harness names its own).
    purpose = PURPOSE
    approver = APPROVER

    def __init__(self, probe, base, admin_token, case):
        self.probe = probe
        self.base = base.rstrip("/")  # https://<catalog>/colvenna/admin/v1
        self.tokens = {"admin": admin_token}
        self.case = case
        self.made_principals = []
        self.made_grants = []  # the case's grants still in place
        self.granted = []  # every grant the case made, deleted or not
        self.calls = 0

    def carry(self, old):
        """What another Admin of the run holds and made (its tokens but the admin's, its principals and
        grants), for an install made again; the old admin token stays a secret of the run (admin-before)."""
        self.tokens.update({k: v for k, v in old.tokens.items() if k != "admin"})
        self.tokens["admin-before"] = old.tokens.get("admin", "")
        self.made_principals += old.made_principals
        self.made_grants += old.made_grants
        self.granted += old.granted

    def call(self, method, path, body=None, token="admin", expect=(200,), name=None, timeout=None, full=False):
        """One request; returns its result (raises AdminError on an unexpected status). timeout: the
        probe's, in seconds, for a slow operation (its default otherwise); full: the whole body kept,
        however long (the probe cuts a long one short otherwise)."""
        self.calls += 1
        step = {"name": name or f"admin-{self.calls}", "method": method, "url": self.base + path, "token": token}
        if body is not None:
            step["body"] = body
        if timeout:
            step["timeout"] = timeout
        if full:
            step["full"] = True
        result = self.probe.run([step], tokens=self.tokens, keep="admin-calls.jsonl")[0]
        if expect and result.get("status") not in expect:
            raise AdminError(f"{method} {path}: expected {expect}, got {describe(result)}")
        return result

    # --- principals and tokens ------------------------------------------------------------------------

    def principal(self, name, description="UAT"):
        """Makes a principal (an existing one of that name is refused: names are the run's own)."""
        self.call("POST", "/principals", {"name": name, "description": description})
        self.made_principals.append(name)
        return name

    def issue(self, principal, ttl="PT2H", key=None):
        """Issues a token to the principal; held under key (default the principal's name). Returns the
        token's id."""
        body = {"ttl": ttl} if ttl else {}
        result = self.call("POST", f"/principals/{urllib.parse.quote(principal)}/tokens", body)
        self.tokens[key or principal] = result["body"]["token"]
        return result["body"].get("id")

    def adopt_principal(self, name):
        """A principal the case made by its own request: disabled at the clean-up as the others."""
        self.made_principals.append(name)

    def adopt_grant(self, gid):
        """A grant the case made by its own request: deleted at the clean-up as the others."""
        self.made_grants.append(gid)
        self.granted.append(gid)

    def forget_principal(self, name):
        """A principal the case hands to a later case, or that a restore took away: not disabled at the
        clean-up."""
        if name in self.made_principals:
            self.made_principals.remove(name)

    def forget_grant(self, gid):
        """A grant the case deleted by its own request, hands to a later case, or that a restore took away."""
        if gid in self.made_grants:
            self.made_grants.remove(gid)

    def disable(self, principal):
        return self.call("POST", f"/principals/{urllib.parse.quote(principal)}/disable", expect=(200, 404))

    def revoke(self, token_id):
        return self.call("DELETE", f"/tokens/{urllib.parse.quote(str(token_id))}")

    # --- grants -------------------------------------------------------------------------------------

    def grant(self, principal, scope, privilege, database=None, obj=None, effect="ALLOW", columns=None,
              expires=None, hours=24):
        body = {"principal": principal, "scope": scope, "privilege": privilege, "effect": effect,
                "purpose": f"{self.purpose}: {self.case}", "approvedBy": self.approver, "approval": self.case,
                "expiresAt": expires or in_hours(hours)}
        if database:
            body["database"] = database
        if obj:
            body["object"] = obj
        if columns:
            body["columns"] = columns
        result = self.call("POST", "/grants", body)
        gid = result["body"]["id"]
        self.made_grants.append(gid)
        self.granted.append(gid)
        return gid

    def delete_grant(self, gid):
        result = self.call("DELETE", f"/grants/{urllib.parse.quote(str(gid))}", expect=(200, 204, 404))
        if gid in self.made_grants:
            self.made_grants.remove(gid)
        return result

    def grants(self, principal=None):
        path = "/grants" + (f"?principal={urllib.parse.quote(principal)}" if principal else "")
        return self.call("GET", path)["body"]["grants"]

    # --- reading back -------------------------------------------------------------------------------

    def audit(self, **query):
        """Audit events matching the query (principal, action, resourceType, resource, outcome, since,
        until, limit), newest first."""
        query.setdefault("limit", 1000)
        path = "/audit?" + urllib.parse.urlencode(query)
        return self.call("GET", path, full=True)["body"]["events"]

    def consistency(self, database, table):
        """The table's consistency report, every page of it (raises AdminError on an answer other than
        200)."""
        result = self.consistency_report(database, table)
        if result.get("status") != 200:
            raise AdminError(f"GET /consistency/{database}/{table}: expected (200,), got {describe(result)}")
        return result["body"]

    def consistency_report(self, database, table, timeout=None, pages=100):
        """The answer of the table's consistency check, its pages followed: a server that bounds an
        answer's time (the serverless form's) answers a large table's check in pages, each with the next
        one's nextPageToken, and the last, without one, is the whole report (#69). An answer other than
        200 is returned as it came; the result's "pages" says how many answers the report took."""
        path = f"/consistency/{urllib.parse.quote(database)}/{urllib.parse.quote(table)}"
        token, n = None, 0
        while True:
            query = f"?pageToken={urllib.parse.quote(token, safe='')}" if token else ""
            result = self.call("GET", path + query, expect=None, timeout=timeout, full=True)
            n += 1
            body = result.get("body")
            token = body.get("nextPageToken") if result.get("status") == 200 and isinstance(body, dict) else None
            if not token or n >= pages:
                result["pages"] = n
                return result

    # --- clean-up -----------------------------------------------------------------------------------

    def delete_grants(self):
        """Deletes every grant of the case still in place."""
        for gid in list(self.made_grants):
            self.delete_grant(gid)

    def cleanup(self):
        """Deletes the grants the case made and disables its principals; returns what failed."""
        problems = []
        for gid in list(self.made_grants):
            try:
                self.delete_grant(gid)
            except Exception as e:  # noqa: BLE001 - every grant is tried
                problems.append(f"grant {gid}: {e}")
        for name in self.made_principals:
            try:
                self.disable(name)
            except Exception as e:  # noqa: BLE001
                problems.append(f"principal {name}: {e}")
        return problems
