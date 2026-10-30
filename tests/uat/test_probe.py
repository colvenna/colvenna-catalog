"""The probe (probe/probe.py), run here against a local server as it runs in a client pod: the
plan on stdin, one JSON line per result, tokens never on its command line."""

import json
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from uatlib.probe import Background, Probe as ProbePod

PROBE = Path(__file__).resolve().parent / "probe" / "probe.py"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the http.server interface
        auth = self.headers.get("Authorization", "")
        status = 200 if auth == "Bearer colvenna_good-token-0000000000" else 401
        body = json.dumps({"path": self.path, "auth": "yes" if auth else "no"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", self.headers.get("X-Request-Id", "none"))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET  # noqa: N815

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_plan(self, steps, tokens=None):
        plan = {"base": f"http://127.0.0.1:{self.port}", "ca": None, "tokens": tokens or {}, "steps": steps}
        p = subprocess.run([sys.executable, "-I", str(PROBE)], input=json.dumps(plan), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60, check=True)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def test_strict_x509_checks_on_unless_the_plan_says_otherwise(self):
        """#49: Python 3.13's default context checks X.509 strictly, which refuses a CA without keyUsage (a
        previous release's); a plan's "strict": false drops that check alone, the chain and the names still
        verified. The default context is made strict here, whatever this Python's default."""
        import importlib.util
        import ssl
        spec = importlib.util.spec_from_file_location("uat_probe", PROBE)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        made = ssl.create_default_context

        def strict_default():
            ctx = made()
            ctx.verify_flags |= ssl.VERIFY_X509_STRICT
            return ctx

        probe.ssl.create_default_context = strict_default
        try:
            self.assertTrue(probe.context(None, {}).verify_flags & ssl.VERIFY_X509_STRICT)  # no key: strict
            self.assertTrue(probe.context(None, {"strict": True}).verify_flags & ssl.VERIFY_X509_STRICT)
            lenient = probe.context(None, {"strict": False})
        finally:
            probe.ssl.create_default_context = made
        self.assertFalse(lenient.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertEqual(lenient.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(lenient.check_hostname)

    def test_the_plans_carry_the_probes_strictness(self):
        class Kube:
            def exec(self, namespace, name, command, input=None, timeout=None, check=True):
                self.plan = json.loads(input)
                return subprocess.CompletedProcess(command, 0, "", "")

            def secret(self, namespace, name, data):
                self.plan = json.loads(data["plan.json"])

            def configmap(self, namespace, name, data):
                pass

            def apply(self, manifest):
                pass

            def wait_ready(self, namespace, name, *args, **kwargs):
                pass

        kube = Kube()
        runner = ProbePod(kube, "ns")
        runner.run([])
        self.assertTrue(kube.plan["strict"])
        runner.strict = False
        runner.run([])
        self.assertFalse(kube.plan["strict"])
        Background(runner, "bg", [], {})  # a background pod's plan too
        self.assertFalse(kube.plan["strict"])

    def test_a_request_with_a_token_by_name_and_its_request_id(self):
        r = self.run_plan([{"name": "ok", "path": "/v1/config", "token": "admin", "requestId": "rid-1"},
                           {"name": "none", "path": "/v1/config"}],
                          tokens={"admin": "colvenna_good-token-0000000000"})
        self.assertEqual([x["status"] for x in r], [200, 401])
        self.assertEqual(r[0]["requestId"], "rid-1")
        self.assertEqual(r[0]["body"], {"path": "/v1/config", "auth": "yes"})
        self.assertNotIn("colvenna_good", json.dumps(r))

    def test_raw_authorization_and_bodies(self):
        r = self.run_plan([{"name": "basic", "method": "POST", "path": "/x", "authorization": "Basic Zm9vOmJhcg==",
                            "body": {}}, {"name": "big", "method": "POST", "path": "/y", "bytes": 1000}])
        self.assertEqual(r[0]["status"], 401)
        self.assertEqual(r[0]["body"]["auth"], "yes")
        self.assertEqual(r[1]["status"], 401)

    def test_refused_connections_are_errors_not_statuses(self):
        port = free_port()
        r = self.run_plan([{"name": "http", "url": f"http://127.0.0.1:{port}/", "timeout": 3},
                           {"name": "tcp", "kind": "tcp", "host": "127.0.0.1", "port": port, "timeout": 3},
                           {"name": "plain", "kind": "plain", "host": "127.0.0.1", "port": port, "timeout": 3}])
        self.assertNotIn("status", r[0])
        self.assertTrue(r[0]["error"].startswith(("refused", "connection")), r[0]["error"])
        self.assertEqual(r[1]["connect"], "refused")
        self.assertEqual(r[2]["answer"], "none")

    def test_plain_http_and_tcp_to_a_server(self):
        r = self.run_plan([{"name": "tcp", "kind": "tcp", "host": "127.0.0.1", "port": self.port},
                           {"name": "plain", "kind": "plain", "host": "127.0.0.1", "port": self.port},
                           {"name": "dns", "kind": "dns", "host": "localhost"}])
        self.assertEqual(r[0]["connect"], "connected")
        self.assertEqual(r[1]["answer"], "http")
        self.assertTrue(r[2]["addresses"])

    def test_repeat_numbers_its_request_ids(self):
        r = self.run_plan([{"name": "loop", "path": "/", "requestId": "loop", "repeat": {"every": 0.2, "for": 0.9}}])
        self.assertGreaterEqual(len(r), 3)
        self.assertEqual([x["requestId"] for x in r[:3]], ["loop-0", "loop-1", "loop-2"])


if __name__ == "__main__":
    unittest.main()
