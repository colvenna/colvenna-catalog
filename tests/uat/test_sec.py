"""UAT-SEC-01 to 04's judging, offline."""

import unittest

from cases import sec01, sec02, sec03, sec04


class Sec01(unittest.TestCase):
    def test_refusals_are_explicit_401s(self):
        self.assertTrue(sec01.judge_refused({"status": 401})[0])
        self.assertFalse(sec01.judge_refused({"status": 403})[0])
        self.assertFalse(sec01.judge_refused({"error": "timeout: timed out"})[0])
        self.assertFalse(sec01.judge_refused(None)[0])

    def test_plain_http_needs_a_connection_and_no_answer(self):
        self.assertTrue(sec01.judge_plain({"connected": True, "answer": "none"})[0])
        self.assertFalse(sec01.judge_plain({"connected": False, "answer": "none", "error": "connection: refused"})[0])
        self.assertFalse(sec01.judge_plain({"connected": True, "answer": "http", "first": "HTTP/1.1 400"})[0])

    def test_refused_before_the_server(self):
        gateway = {"a": {"status": 401, "serverRequestId": None}}
        self.assertTrue(sec01.judge_before_the_server(gateway, [])[0])
        self.assertFalse(sec01.judge_before_the_server({"a": {"status": 401, "serverRequestId": "x"}}, [])[0])
        self.assertFalse(sec01.judge_before_the_server(gateway, ["colvenna.access - x GET /v1/... 401"])[0])

    def test_every_route_with_every_credential(self):
        urls = sec01.routes("https://c/v1/colvenna", "https://c/colvenna/admin/v1", "db", "t")
        steps = sec01.requests("u1", "service", urls, {"lowercase": "colvenna_x"})
        self.assertEqual(len(steps), len(sec01.ROUTES) * len(sec01.CREDENTIALS))
        lower = [s for s in steps if s["name"].endswith("lowercase-bearer")]
        self.assertTrue(all(s["authorization"] == "bearer colvenna_x" for s in lower))
        self.assertEqual({s["name"] for s in steps}, set(sec01.checks("eks")) - {"eks-plain-http", "admin-control"})


class Sec02(unittest.TestCase):
    def test_statuses(self):
        self.assertTrue(sec02.judge({"status": 403}, 403)[0])
        self.assertFalse(sec02.judge({"status": 200}, 403)[0])
        self.assertFalse(sec02.judge({"error": "timeout"}, 403)[0])


class Sec03(unittest.TestCase):
    def test_unlabelled(self):
        self.assertTrue(sec03.judge_unlabelled({"svc": {"connect": "timeout"}, "req": {"error": "timeout: x"}})[0])
        self.assertFalse(sec03.judge_unlabelled({"svc": {"connect": "connected"}})[0])
        self.assertFalse(sec03.judge_unlabelled({"req": {"status": 401}})[0])

    def test_health(self):
        inside = [("p", "000", 7, "200")]
        self.assertTrue(sec03.judge_health({"h": {"connect": "timeout"}}, inside)[0])
        self.assertFalse(sec03.judge_health({"h": {"connect": "connected"}}, inside)[0])
        self.assertFalse(sec03.judge_health({"h": {"connect": "timeout"}}, [("p", "200", 0, "200")])[0])
        self.assertFalse(sec03.judge_health({"h": {"connect": "timeout"}}, [("p", "000", 7, "000")])[0])

    def test_metrics(self):
        self.assertTrue(sec03.judge_metrics({"m": {"connect": "timeout"}}, {"m": {"status": 200}})[0])
        self.assertFalse(sec03.judge_metrics({"m": {"connect": "connected"}}, {"m": {"status": 200}})[0])
        self.assertFalse(sec03.judge_metrics({"m": {"connect": "timeout"}}, {"m": {"error": "timeout"}})[0])

    def test_load_balancers(self):
        nlb = {"metadata": {"namespace": "colvenna-catalog", "name": "nlb",
                            "annotations": {sec03.SCHEME: "internal"}}, "spec": {"type": "LoadBalancer"}}
        public = {"metadata": {"namespace": "x", "name": "web", "annotations": {}}, "spec": {"type": "LoadBalancer"}}
        self.assertTrue(sec03.judge_load_balancers([], "colvenna-catalog", False, {})[0])
        self.assertTrue(sec03.judge_load_balancers([nlb], "colvenna-catalog", True, {"k8s-x": "internal"})[0])
        self.assertFalse(sec03.judge_load_balancers([nlb], "colvenna-catalog", False, {})[0])
        self.assertFalse(sec03.judge_load_balancers([public], "colvenna-catalog", False, {})[0])
        self.assertFalse(sec03.judge_load_balancers([], "colvenna-catalog", False, {"k8s-x": "internet-facing"})[0])

    def test_outside(self):
        good = ("private DNS name (<api>): does not resolve outside the VPC\n"
                "VPC endpoint name (<api>-<vpce>): resolves to 2 private address(es); anonymous GET: no answer (URLError)\n")
        self.assertTrue(sec03.judge_outside(0, good)[0])
        public = good.replace("does not resolve outside the VPC", "resolves to 3 public address(es); anonymous GET: HTTP 403")
        self.assertFalse(sec03.judge_outside(0, public)[0])
        self.assertFalse(sec03.judge_outside(1, good)[0])

    def test_unlisted(self):
        deny = {"status": 403, "body": {"Message": "User: anonymous is not authorized to perform: execute-api:Invoke on "
                                                   "resource: x with an explicit deny"}}
        self.assertTrue(sec03.judge_unlisted(["vpce-1"], "vpce-2", deny, {"status": 200})[0])
        self.assertFalse(sec03.judge_unlisted(["vpce-1"], "vpce-2", {"status": 403, "body": {"message": "Forbidden"}},
                                              {"status": 200})[0])
        self.assertFalse(sec03.judge_unlisted(["vpce-2"], "vpce-2", deny, {"status": 200})[0])

    def test_invoke(self):
        good = {"Sid": "a", "Principal": {"Service": "apigateway.amazonaws.com"},
                "Condition": {"ArnLike": {"AWS:SourceArn": "arn:aws:execute-api:r:a:api1/*"}}}
        self.assertTrue(sec03.judge_invoke({"s": "AccessDenied"}, {"s:live": [good]}, "api1")[0])
        self.assertFalse(sec03.judge_invoke({"s": "exit 0"}, {"s:live": [good]}, "api1")[0])
        star = dict(good, Principal="*")
        self.assertFalse(sec03.judge_invoke({"s": "AccessDenied"}, {"s:live": [star]}, "api1")[0])
        self.assertFalse(sec03.judge_invoke({"s": "AccessDenied"}, {"s:live": [good]}, "api2")[0])
        self.assertFalse(sec03.judge_invoke({"s": "AccessDenied"}, {"s:live": []}, "api1")[0])

    def test_database(self):
        group = [{"IpPermissions": [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                                     "UserIdGroupPairs": [{"GroupId": "sg-f"}], "IpRanges": []}]}]
        self.assertTrue(sec03.judge_database(group, 5432, "sg-f", {"connect": "timeout"})[0])
        self.assertFalse(sec03.judge_database(group, 5432, "sg-f", {"connect": "connected"})[0])
        cidr = [{"IpPermissions": [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [],
                                    "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}]}]
        self.assertFalse(sec03.judge_database(group + cidr, 5432, "sg-f", {"connect": "timeout"})[0])


class Sec04(unittest.TestCase):
    def test_function_urls(self):
        self.assertTrue(sec04.judge_function_urls(["f"], [], {"s": ["AWS::Lambda::Function"]})[0])
        self.assertFalse(sec04.judge_function_urls(["f"], ["f"], {"s": []})[0])
        self.assertFalse(sec04.judge_function_urls(["f"], [], {"s": ["AWS::Lambda::Url"]})[0])

    def test_rest_api(self):
        policy = {"Statement": [{"Effect": "Allow", "Condition": {"StringEquals": {"aws:SourceVpce": ["v"]}}},
                                {"Effect": "Deny", "Condition": {"StringNotEquals": {"aws:SourceVpce": ["v"]}}}]}
        import json
        rest = {"endpointConfiguration": {"types": ["PRIVATE"]}, "policy": json.dumps(policy).replace('"', '\\"')}
        self.assertTrue(sec04.judge_rest_api("serverless", rest, [("ANY", "/{proxy+}", "CUSTOM")], [])[0])
        self.assertFalse(sec04.judge_rest_api("serverless", rest, [("ANY", "/x", "NONE")], [])[0])
        self.assertFalse(sec04.judge_rest_api("serverless", dict(rest, endpointConfiguration={"types": ["REGIONAL"]}),
                                              [("ANY", "/x", "CUSTOM")], [])[0])
        self.assertTrue(sec04.judge_rest_api("eks", None, [], [])[0])
        self.assertFalse(sec04.judge_rest_api("eks", None, [], ["colvenna-catalog-e-http"])[0])

    def test_buckets(self):
        block = {k: True for k in sec04.BLOCKS}
        tls = {"Statement": [{"Effect": "Deny", "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}
        good = {"b": {"block": block, "policy": tls, "unsigned": {"list": 403, "get": 403}}}
        self.assertTrue(sec04.judge_buckets(good)[0])
        self.assertFalse(sec04.judge_buckets({"b": dict(good["b"], block=None)})[0])
        self.assertFalse(sec04.judge_buckets({"b": dict(good["b"], policy=None)})[0])
        self.assertFalse(sec04.judge_buckets({"b": dict(good["b"], unsigned={"list": 200})})[0])
        self.assertFalse(sec04.judge_buckets({})[0])

    def test_repositories(self):
        pull = {"Statement": [{"Principal": {"AWS": "arn:aws:iam::123456789012:root"}, "Action": sorted(sec04.PULLS)}]}
        self.assertTrue(sec04.judge_repositories({"colvenna-catalog-e/x": None, "colvenna-catalog-release/x": pull},
                                                 "colvenna-catalog-release/")[0])
        push = {"Statement": [{"Principal": {"AWS": "arn:aws:iam::123456789012:root"}, "Action": "ecr:PutImage"}]}
        self.assertFalse(sec04.judge_repositories({"colvenna-catalog-e/x": None, "colvenna-catalog-release/x": push},
                                                  "colvenna-catalog-release/")[0])
        star = {"Statement": [{"Principal": "*", "Action": "ecr:BatchGetImage"}]}
        self.assertFalse(sec04.judge_repositories({"colvenna-catalog-e/x": star}, "colvenna-catalog-release/")[0])
        self.assertFalse(sec04.judge_repositories({}, "colvenna-catalog-release/")[0])

    def test_secrets(self):
        self.assertTrue(sec04.judge_secrets({"a": None})[0])
        later = {"Statement": [{"Principal": {"AWS": "arn:aws:iam::1:root"}}, {"Principal": "*"}]}
        self.assertFalse(sec04.judge_secrets({"a": later})[0])
        self.assertFalse(sec04.judge_secrets({})[0])

    def test_database_and_pool(self):
        self.assertTrue(sec04.judge_database("eks", {"PubliclyAccessible": False})[0])
        self.assertFalse(sec04.judge_database("serverless", {"PubliclyAccessible": False})[0])
        self.assertFalse(sec04.judge_database("eks", None)[0])
        pool = {"AllowUnauthenticatedIdentities": False, "AllowClassicFlow": False, "DeveloperProviderName": "d"}
        closed = ((400, "NotAuthorizedException"), (400, "MissingAuthenticationTokenException"))
        self.assertTrue(sec04.judge_identity_pool(pool, {}, *closed)[0])
        self.assertFalse(sec04.judge_identity_pool(dict(pool, AllowUnauthenticatedIdentities=True), {}, *closed)[0])
        self.assertFalse(sec04.judge_identity_pool(pool, {"authenticated": "arn"}, *closed)[0])


if __name__ == "__main__":
    unittest.main()
