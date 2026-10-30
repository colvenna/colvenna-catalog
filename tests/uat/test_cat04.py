"""UAT-CAT-04's judging, offline: each call's answer, its audit event, the grants' events."""

import unittest

from cases import cat04

DB, SCRATCH = "authz_u1", "authz_probe_u1"
OWNER = "authz-owner-u1"


def event(**kw):
    e = {"id": 1, "time": "2026-10-29T00:00:00Z", "requestId": "r", "principal": OWNER,
         "action": "GET_TABLE", "resourceType": "TABLE", "resource": f"{DB}.orders", "outcome": "SUCCEEDED",
         "httpStatus": 200, "detail": {"policyVersion": 3, "privilege": "READ", "grants": [1]}}
    e.update(kw)
    return e


def answer(status, body=None):
    return {"status": status, "requestId": "r", "body": body}


class Calls(unittest.TestCase):
    def test_statuses(self):
        self.assertTrue(cat04.judge_call("get-table/denied", answer(403), DB)[0])
        self.assertFalse(cat04.judge_call("get-table/denied", answer(200), DB)[0])
        self.assertFalse(cat04.judge_call("get-table/denied", None, DB)[0])
        self.assertFalse(cat04.judge_call("get-table/denied", {"error": "timeout: x"}, DB)[0])

    def test_the_outsider_sees_default_but_not_the_database(self):
        self.assertTrue(cat04.judge_call("list-databases/outsider", answer(200, {"databases": ["default"]}), DB)[0])
        self.assertFalse(cat04.judge_call("list-databases/outsider", answer(200, {"databases": ["default", DB]}), DB)[0])
        self.assertFalse(cat04.judge_call("list-databases/outsider", answer(200, {"databases": []}), DB)[0])

    def test_every_page_names_only_orders(self):
        ok = answer(200, {"tables": ["orders"]})
        self.assertTrue(cat04.judge_call("list-tables/reader", ok, DB, listed=["orders"])[0])
        self.assertFalse(cat04.judge_call("list-tables/reader", ok, DB, listed=["orders", "ledger"])[0])

    def test_a_column_denial_names_the_column(self):
        self.assertTrue(cat04.judge_call("column-auth/denied", answer(403, {"message": "no permission for column amount"}), DB)[0])
        self.assertFalse(cat04.judge_call("column-auth/denied", answer(403, {"message": "denied"}), DB)[0])

    def test_by_id_names_no_table(self):
        self.assertTrue(cat04.judge_by_id(answer(404, {"message": "Table 9254 does not exist."}), "9254", DB)[0])
        self.assertFalse(cat04.judge_by_id(answer(404, {"message": f"Table {DB}.orders"}), "9254", DB)[0])
        self.assertFalse(cat04.judge_by_id(answer(403, {}), "9254", DB)[0])


class Audit(unittest.TestCase):
    def judge(self, call, result, e, principal=OWNER, table_id=None):
        return cat04.judge_audit(call, result, e, DB, SCRATCH, principal, table_id)

    def test_a_call_and_its_event(self):
        self.assertTrue(self.judge("get-table/owner", answer(200), event())[0])

    def test_no_event_fails(self):
        ok, detail = self.judge("get-table/owner", answer(200), None)
        self.assertFalse(ok)
        self.assertIn("no audit event", detail)

    def test_the_event_names_the_caller(self):
        self.assertFalse(self.judge("get-table/owner", answer(200), event(principal="someone-else"))[0])

    def test_a_denial_needs_its_outcome_policy_version_and_level(self):
        denied = event(outcome="DENIED", httpStatus=403, principal="authz-denied-u1")
        self.assertTrue(self.judge("get-table/denied", answer(403), denied, "authz-denied-u1")[0])
        bare = event(outcome="DENIED", httpStatus=403, principal="authz-denied-u1", detail={"error": "no permission"})
        self.assertFalse(self.judge("get-table/denied", answer(403), bare, "authz-denied-u1")[0])

    def test_the_refused_lookup_by_id_is_a_denial_naming_only_the_id(self):
        outsider = "authz-outsider-u1"
        refused = event(action="GET_TABLE_BY_ID", outcome="DENIED", httpStatus=404, principal=outsider,
                        resource="9254", detail={"policyVersion": 3, "privilege": "READ", "grants": [],
                                                 "error": "Table 9254 does not exist."})
        self.assertTrue(self.judge(cat04.BY_ID, answer(404), refused, outsider, "9254")[0])
        self.assertFalse(self.judge(cat04.BY_ID, answer(404), dict(refused, outcome="FAILED"), outsider, "9254")[0])
        self.assertFalse(self.judge(cat04.BY_ID, answer(404), dict(refused, resource="other"), outsider, "9254")[0])
        for named in (dict(refused, resource=f"{DB}.orders"),
                      dict(refused, detail=dict(refused["detail"], error="Table orders does not exist."))):
            ok, detail = self.judge(cat04.BY_ID, answer(404), named, outsider, "9254")
            self.assertFalse(ok)
            self.assertIn("names the table", detail)

    def test_a_failure_recorded_as_success_is_wrong(self):
        ok, detail = self.judge("commit/owner", answer(400), event(action="COMMIT", httpStatus=400))
        self.assertFalse(ok)
        self.assertIn("outcome", detail)

    def test_the_credentials_access(self):
        token = event(action="ISSUE_DATA_TOKEN", principal="authz-writer-u1",
                      detail={"policyVersion": 1, "privilege": "WRITE", "access": "READ_WRITE"})
        self.assertTrue(self.judge("data-token/writer", answer(200), token, "authz-writer-u1")[0])
        read_only = dict(token, detail={"access": "READ"})
        self.assertFalse(self.judge("data-token/writer", answer(200), read_only, "authz-writer-u1")[0])

    def test_resources(self):
        self.assertEqual(cat04.expected_audit("create-table/writer", DB, SCRATCH), ("CREATE_TABLE", DB))
        self.assertEqual(cat04.expected_audit("create-table/owner", DB, SCRATCH), ("CREATE_TABLE", f"{DB}.{SCRATCH}"))
        self.assertEqual(cat04.expected_audit("list-tables/outsider", DB, SCRATCH), ("LIST_TABLES", DB))
        self.assertEqual(cat04.expected_audit("list-databases/owner", DB, SCRATCH), ("LIST_DATABASES", None))

    def test_every_call_is_audited_once(self):
        calls = cat04.audited_calls()
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len([c for c in cat04.checks("eks") if c.startswith("audit-") and "/" in c]), len(calls))

    def test_no_failure_recorded_as_success(self):
        self.assertTrue(cat04.judge_no_failure_as_success([event(), event(outcome="FAILED", httpStatus=400)])[0])
        self.assertFalse(cat04.judge_no_failure_as_success([event(httpStatus=403)])[0])
        self.assertFalse(cat04.judge_no_failure_as_success([])[0])


class Grants(unittest.TestCase):
    def grant_event(self, action, gid, **missing):
        detail = {f: "x" for f in cat04.GRANT_FIELDS}
        detail.update(grantId=gid, principal=OWNER)
        for k in missing:
            detail.pop(k)
        return {"id": gid, "action": action, "outcome": "SUCCEEDED", "detail": detail}

    def test_each_grant_made_and_deleted_with_every_field(self):
        events = [self.grant_event("GRANT", 1), self.grant_event("REVOKE_GRANT", 1)]
        self.assertTrue(cat04.judge_grants([1], events, {OWNER})[0])
        self.assertFalse(cat04.judge_grants([1, 2], events, {OWNER})[0])
        self.assertFalse(cat04.judge_grants([1], events[:1], {OWNER})[0])
        partial = [self.grant_event("GRANT", 1, approval=1), self.grant_event("REVOKE_GRANT", 1)]
        self.assertFalse(cat04.judge_grants([1], partial, {OWNER})[0])


if __name__ == "__main__":
    unittest.main()
