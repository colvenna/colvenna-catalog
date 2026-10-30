"""The Kubernetes pods the runner makes: what they may do and where their jars come from."""

import unittest

from compatlib import cells, config
from compatlib.backends import Kubernetes, placement

SETTINGS = {"uri": "https://catalog.example.com:8181", "warehouse": "colvenna", "dataTokens": True,
            "s3Region": "us-west-2"}


class Manifest(unittest.TestCase):
    def setUp(self):
        self.config = config.load_cells()
        self.k8s = Kubernetes(self.config, "/f", "/s", "/ca.crt", "colvenna-compat", "colvenna-catalog/client=true")

    def pod(self, cell_id):
        cell = config.cell_by_id(self.config, cell_id)
        if cell["kind"] in config.WRITERS:
            return cells.writer_pod(self.config, SETTINGS, "20261028T000000Z-ab12", cell, "cell", "db")
        prober = config.cell_by_id(self.config, self.config["fixtureWriter"])
        return cells.reader_pod(self.config, SETTINGS, "20261028T000000Z-ab12", cell, cells.reader_plan(SETTINGS, "db", []),
                                cells.READ_TOKEN, prober, has_s3=True)

    def test_the_jars_are_fetched_and_checked(self):
        m = self.k8s.manifest(self.pod("spark-4.1-paimon-2.1.0"))
        script = m["spec"]["initContainers"][0]["command"][2]
        for jar in ("paimon-spark-4.1_2.13-2.1.0.jar", "paimon-s3-2.1.0.jar"):
            sha = self.config["jars"][jar]["sha256"]
            self.assertIn(f"echo '{sha}  {jar}' | sha256sum -c -", script)
        self.assertTrue(script.startswith("set -eu"))

    def test_engines_have_no_identity_of_their_own(self):
        for cell in self.config["cells"]:
            spec = self.k8s.manifest(self.pod(cell["id"]))["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertNotIn("serviceAccountName", spec)

    def test_writers_run_unprivileged(self):
        spec = self.k8s.manifest(self.pod("flink-2.2-paimon-2.1.0"))["spec"]
        sc = spec["containers"][0]["securityContext"]
        self.assertEqual((sc["runAsUser"], sc["runAsNonRoot"], sc["allowPrivilegeEscalation"]), (9999, True, False))
        self.assertEqual(sc["capabilities"], {"drop": ["ALL"]})

    def test_the_token_is_a_file_never_an_argument_or_variable(self):
        m = self.k8s.manifest(self.pod("doris-4.1.5"))
        for c in m["spec"]["containers"]:
            self.assertNotIn("token", " ".join(c["command"]).lower())
            for e in c["env"]:
                self.assertTrue(e["name"] != "COMPAT_TOKEN" and "colvenna_" not in e["value"])
        self.assertEqual(m["spec"]["volumes"][2]["secret"]["secretName"], "compat-secret")

    def test_a_pod_mounts_only_its_own_secrets(self):
        def items(cell_id):
            volume = self.k8s.manifest(self.pod(cell_id))["spec"]["volumes"][2]
            return sorted(i["key"] for i in volume["secret"]["items"])
        self.assertEqual(items("flink-1.20-paimon-2.0.0"), ["token"])
        self.assertEqual(items("doris-4.1.5"), ["engine-password", "read-token"])
        self.assertEqual(items("starrocks-4.1.6"), ["engine-password", "read-token", "starrocks-s3.json"])

    def test_a_reader_probes_its_own_pod_first(self):
        spec = self.k8s.manifest(self.pod("starrocks-4.1.6"))["spec"]
        self.assertEqual([c["name"] for c in spec["initContainers"]], ["jars", "probe"])
        probe = spec["initContainers"][1]
        self.assertIn("sh /compat/flink.sh probe db", probe["command"][2])
        self.assertIn("|| echo COMPAT-PROBE-EXIT", probe["command"][2])
        self.assertIn({"name": "COMPAT_TOKEN_FILE", "value": "/secret/read-token"}, probe["env"])

    def test_images_by_digest(self):
        for cell in self.config["cells"]:
            m = self.k8s.manifest(self.pod(cell["id"]))
            for c in m["spec"]["initContainers"] + m["spec"]["containers"]:
                self.assertRegex(c["image"], r"@sha256:[0-9a-f]{64}$")

    def test_doris_shares_one_pod(self):
        names = [c["name"] for c in self.k8s.manifest(self.pod("doris-4.1.5"))["spec"]["containers"]]
        self.assertEqual(names, ["fe", "be"])

    def test_names_are_dns_labels(self):
        for cell in self.config["cells"]:
            for mode in ("cell", "fixture", "drop", "read"):
                name = cells.pod_name("20261028T000000Z-ab12", cell, mode)
                self.assertRegex(name, r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")



class Placement(unittest.TestCase):
    """--node-selector and --toleration: the engines' pods on the nodes of the clients (#65)."""

    def test_the_pods_select_the_nodes_and_tolerate_their_taint(self):
        where = placement("colvenna-catalog/test-pool=clients", "colvenna-catalog/test-pool=clients:NoSchedule")
        k8s = Kubernetes(config.load_cells(), "/f", "/s", None, "colvenna-compat", "colvenna-catalog/client=true",
                         placement=where)
        writer = config.cell_by_id(k8s.config, "flink-1.20-paimon-2.1.0")
        spec = k8s.manifest(cells.writer_pod(k8s.config, SETTINGS, "20261028T000000Z-ab12", writer, "cell", "db"))["spec"]
        self.assertEqual(spec["nodeSelector"], {"colvenna-catalog/test-pool": "clients"})
        self.assertEqual(spec["tolerations"], [{"key": "colvenna-catalog/test-pool", "operator": "Equal",
                                                "value": "clients", "effect": "NoSchedule"}])

    def test_none_given_none_set(self):
        self.assertEqual(placement(), {})
        k8s = Kubernetes(config.load_cells(), "/f", "/s", None, "colvenna-compat", "colvenna-catalog/client=true")
        writer = config.cell_by_id(k8s.config, "flink-1.20-paimon-2.1.0")
        spec = k8s.manifest(cells.writer_pod(k8s.config, SETTINGS, "20261028T000000Z-ab12", writer, "cell", "db"))["spec"]
        self.assertNotIn("nodeSelector", spec)
        self.assertNotIn("tolerations", spec)

    def test_refuses_what_is_no_selector_or_taint(self):
        for selector, toleration in (("pool", None), (None, "pool=clients"), (None, "pool=clients:Never")):
            with self.assertRaises(ValueError, msg=(selector, toleration)):
                placement(selector, toleration)


if __name__ == "__main__":
    unittest.main()
