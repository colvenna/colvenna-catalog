"""Where the harness's pods run, offline: on the test environment's clients' nodes (#65), whatever
kind carries them, and never without the environment saying where those are."""

import unittest

from uatlib.install import TestEnvironment
from uatlib.kube import Kube, place, placement

CLIENTS = placement("colvenna-catalog/test-pool=clients", "colvenna-catalog/test-pool=clients:NoSchedule")


class Placement(unittest.TestCase):
    def test_selects_the_nodes_and_tolerates_their_taint(self):
        self.assertEqual(CLIENTS, {
            "nodeSelector": {"colvenna-catalog/test-pool": "clients"},
            "tolerations": [{"key": "colvenna-catalog/test-pool", "operator": "Equal", "value": "clients",
                             "effect": "NoSchedule"}]})

    def test_refuses_what_is_no_selector_or_taint(self):
        for selector, taint in (("pool", "pool=clients:NoSchedule"), ("pool=clients", "pool=clients"),
                                ("pool=clients", "pool:NoSchedule"), ("pool=clients", "pool=clients:Never")):
            with self.assertRaises(ValueError, msg=(selector, taint)):
                placement(selector, taint)

    def test_a_pod_and_a_workloads_template(self):
        pod = place({"kind": "Pod", "spec": {"containers": []}}, CLIENTS)
        self.assertEqual(pod["spec"]["nodeSelector"], CLIENTS["nodeSelector"])
        self.assertEqual(pod["spec"]["tolerations"], CLIENTS["tolerations"])
        deployment = place({"kind": "Deployment", "spec": {"template": {"spec": {"containers": []}}}}, CLIENTS)
        self.assertEqual(deployment["spec"]["template"]["spec"]["nodeSelector"], CLIENTS["nodeSelector"])
        self.assertEqual(deployment["spec"]["template"]["spec"]["tolerations"], CLIENTS["tolerations"])

    def test_keeps_what_the_pod_has_and_adds_nothing_twice(self):
        own = {"key": "x", "operator": "Exists"}
        pod = place({"kind": "Pod", "spec": {"nodeSelector": {"zone": "a"}, "tolerations": [own]}}, CLIENTS)
        self.assertEqual(pod["spec"]["nodeSelector"], {"zone": "a", "colvenna-catalog/test-pool": "clients"})
        self.assertEqual(pod["spec"]["tolerations"], [own] + CLIENTS["tolerations"])
        again = place(pod, CLIENTS)
        self.assertEqual(again["spec"]["tolerations"], [own] + CLIENTS["tolerations"])

    def test_other_kinds_and_no_placement_unchanged(self):
        for manifest in ({"kind": "Service", "spec": {"ports": []}}, {"kind": "NetworkPolicy", "spec": {"podSelector": {}}},
                         {"kind": "ConfigMap", "data": {}}):
            self.assertEqual(place(dict(manifest), CLIENTS), manifest)
        self.assertEqual(place({"kind": "Pod", "spec": {}}, None), {"kind": "Pod", "spec": {}})

    def test_apply_places_every_pod(self):
        applied = []
        kube = Kube("/dev/null", placement=CLIENTS)
        kube.kubectl = lambda *args, input=None, **kw: applied.append(input)
        kube.apply({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": []}})
        self.assertIn('"nodeSelector": {"colvenna-catalog/test-pool": "clients"}', applied[0])


class FakeAws:
    region = "us-west-2"

    def __init__(self, outputs):
        self._outputs = outputs

    def outputs(self, stack):
        return self._outputs


class ClientPlacement(unittest.TestCase):
    def test_from_the_environments_outputs(self):
        env = TestEnvironment("fx4", FakeAws({"ClusterName": "c", "ClientNodeSelector": "colvenna-catalog/test-pool=clients",
                                              "ClientNodeTaint": "colvenna-catalog/test-pool=clients:NoSchedule"}))
        self.assertEqual(env.client_placement(), CLIENTS)

    def test_an_environment_without_clients_nodes_stops_the_run(self):
        env = TestEnvironment("old", FakeAws({"ClusterName": "c"}))
        with self.assertRaisesRegex(ValueError, "no clients' node group.*tests/environment/up.sh old us-west-2"):
            env.client_placement()


if __name__ == "__main__":
    unittest.main()
