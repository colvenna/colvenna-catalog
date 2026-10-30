"""UAT-CAT-07's judging, offline."""

import unittest

from cases import cat07


class Judged(unittest.TestCase):
    def test_schema(self):
        self.assertTrue(cat07.judge_schema([["1", "V1__catalog.sql"], ["2", "V2__grants.sql"]], ["V1", "V2"])[0])
        self.assertFalse(cat07.judge_schema([["1", "V1__catalog.sql"]], ["V1", "V2"])[0])
        self.assertFalse(cat07.judge_schema([], ["V1"])[0])
        self.assertEqual(cat07.migrations()[:1], ["V1__catalog.sql"])

    def test_replicas(self):
        two = [("a", "node-1"), ("b", "node-2")]
        self.assertTrue(cat07.judge_replicas({"readyReplicas": 2}, two, [{"minAvailable": 1}], [])[0])
        self.assertFalse(cat07.judge_replicas({"readyReplicas": 2}, [("a", "n"), ("b", "n")], [{"minAvailable": 1}], [])[0])
        self.assertFalse(cat07.judge_replicas({"readyReplicas": 2}, two, [{"minAvailable": 1}], [{"hpa": 1}])[0])
        self.assertFalse(cat07.judge_replicas({"readyReplicas": 2}, two, [], [])[0])

    def test_metrics_name_no_table(self):
        text = "\n".join(f"{m}{{action=\"GET_TABLE\"}} 1" for m in cat07.METRICS)
        ok = {"a": {"status": 200, "body": text}, "b": {"status": 200, "body": text}}
        self.assertTrue(cat07.judge_metrics(ok, ["sales", "orders"])[0])
        leaky = {"a": {"status": 200, "body": text + '\nx{table="sales.orders"} 1'}, "b": ok["b"]}
        self.assertFalse(cat07.judge_metrics(leaky, ["sales", "orders"])[0])
        self.assertFalse(cat07.judge_metrics({"a": {"status": 200, "body": "nothing"}, "b": ok["b"]}, [])[0])

    def test_functions(self):
        config = {"server": "s", "memory": 2048, "timeout": 60, "reserved": 10, "provisioned": [], "snapstart": "None",
                  "integrations": ["arn:...:function:s:live/invocations"], "others": {"a": True, "m": True, "p": True, "o": True}}
        settings = {"memory": 2048, "concurrency": 10, "snapstart": "off"}
        self.assertTrue(cat07.judge_functions(config, settings)[0])
        self.assertFalse(cat07.judge_functions(dict(config, integrations=["arn:...:function:s/invocations"]), settings)[0])
        self.assertFalse(cat07.judge_functions(dict(config, provisioned=[{}]), settings)[0])
        self.assertFalse(cat07.judge_functions(config, dict(settings, snapstart="on"))[0])
        self.assertFalse(cat07.judge_functions(dict(config, others={"a": True, "m": True, "o": True}), settings)[0])

    def test_no_secret_in_functions(self):
        clean = {"f": {"COLVENNA_DATA_TOKEN_DURATION": "PT3H", "COLVENNA_DATA_TOKEN_ROLE_ARN": "arn:aws:iam::x:role/r",
                       "COLVENNA_FILEIO_S3_AWS_CREDENTIALS_PROVIDER": "software.amazon.awssdk.auth.credentials.Default"}}
        self.assertEqual(cat07.secret_variables(clean), [])
        self.assertTrue(cat07.secret_variables({"f": {"DB_PASSWORD": "x"}}))
        self.assertTrue(cat07.secret_variables({"f": {"S3_CREDENTIALS": "x"}}))
        self.assertTrue(cat07.secret_variables({"f": {"X": "arn:aws:secretsmanager:us-west-2:1:secret:a"}}))
        self.assertTrue(cat07.secret_variables({"f": {"X": "colvenna_AbCdEfGhIjKlMnOpQr"}}))  # pragma: allowlist secret - a made-up catalog token, which the check must find

    def test_logs(self):
        answered = [{"status": 200, "serverRequestId": f"id{i}"} for i in range(3)]
        self.assertTrue(cat07.judge_logs(answered, {"id0", "id1", "id2"})[0])
        self.assertFalse(cat07.judge_logs(answered, {"id0"})[0])
        self.assertFalse(cat07.judge_logs(answered[:2], {"id0", "id1"})[0])


if __name__ == "__main__":
    unittest.main()
