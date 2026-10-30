"""Offline tests of the Paimon tools' pods (uatlib/tools.py)."""
import types
import unittest

from uatlib import tools


def fake_tools(has_ca=True):
    """A Tools without a cluster: what _spec reads."""
    t = tools.Tools.__new__(tools.Tools)
    t.ctx = types.SimpleNamespace(install=types.SimpleNamespace(uri="https://catalog.example.com:8181", warehouse="colvenna"))
    t.image, t.curl, t.jars = "flink@sha256:1", "curl@sha256:2", [("a.jar", "https://repo.example.com/a.jar", "0" * 64)]
    t.has_ca, t.namespace = has_ca, "uat-r1-tools"
    return t


class ProxySidecar(unittest.TestCase):
    def test_the_proxy_is_a_sidecar_the_tool_waits_for(self):
        # #38: as the tool's second container the proxy could listen after the tool's first request (its image
        # pulled on a new node); as a native sidecar with a startup probe it listens before the tool starts.
        spec = fake_tools()._spec("stress-b", ["sh", "/uat/launch.sh"], {"UAT_URI": "http://127.0.0.1:18181/"}, proxy=10)["spec"]
        self.assertEqual([c["name"] for c in spec["containers"]], ["tool"])
        init = spec["initContainers"]
        self.assertEqual([c["name"] for c in init], ["jars", "proxy"])  # the jars first, then the proxy
        proxy = init[1]
        self.assertEqual(proxy["restartPolicy"], "Always")
        # Inside the pod: the proxy listens on 127.0.0.1 alone, which a tcpSocket probe (from the node) never reaches.
        probe = proxy["startupProbe"]["exec"]["command"]
        self.assertEqual(probe[:2], ["python3", "-c"])
        self.assertIn(f"('127.0.0.1', {tools.PROXY_PORT})", probe[2])
        self.assertNotIn("tcpSocket", proxy["startupProbe"])
        self.assertIn({"name": "UAT_DROP_EVERY", "value": "10"}, proxy["env"])
        self.assertIn("/ca", [m["mountPath"] for m in proxy["volumeMounts"]])

    def test_without_a_proxy_only_the_jars_come_first(self):
        spec = fake_tools(has_ca=False)._spec("stress-a", ["sh", "/uat/launch.sh"], {"UAT_URI": "https://x"})["spec"]
        self.assertEqual([c["name"] for c in spec["initContainers"]], ["jars"])
        self.assertEqual([c["name"] for c in spec["containers"]], ["tool"])


if __name__ == "__main__":
    unittest.main()
