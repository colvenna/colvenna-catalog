"""The response-dropping proxy (probe/proxy.py), against a local upstream: every request passed on,
every n-th commit sent twice, the client given the second answer, and one line per dropped commit."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROXY = Path(__file__).resolve().parent / "probe" / "proxy.py"
SEEN = []


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SEEN.append((self.path, self.headers.get("X-Request-Id"), body))
        n = sum(1 for p, _, _ in SEEN if p.endswith("/commit"))
        answer = json.dumps({"success": True, "n": n}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proxy(unittest.TestCase):
    def test_every_third_commit_is_sent_again(self):
        upstream = HTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        port = free_port()
        env = dict(os.environ, UAT_UPSTREAM=f"http://127.0.0.1:{upstream.server_address[1]}", UAT_PROXY_PORT=str(port),
                   UAT_DROP_EVERY="3")
        proc = subprocess.Popen([sys.executable, "-I", str(PROXY)], env=env, stdout=subprocess.PIPE,
                                universal_newlines=True)
        try:
            self.assertIn("listening", proc.stdout.readline())
            body = json.dumps({"snapshot": {"commitUser": "u", "commitIdentifier": 7, "id": 3}}).encode()
            answers = []
            for i in range(4):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/w/databases/d/tables/t/commit", data=body,
                                             headers={"X-Request-Id": f"r{i}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310 - a local test server
                    answers.append(json.loads(r.read()))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/config", timeout=10) as r:  # nosec B310
                self.assertEqual(r.status, 200)
            dropped = json.loads(proc.stdout.readline())
        finally:
            proc.terminate()
            upstream.shutdown()
            upstream.server_close()
        commits = [s for s in SEEN if s[0].endswith("/commit")]
        self.assertEqual(len(commits), 5)  # the third commit twice
        self.assertEqual([a["n"] for a in answers], [1, 2, 4, 5])  # the client got the second answer
        self.assertEqual(commits[3][1], "r2-again")
        self.assertEqual((dropped["kind"], dropped["commitUser"], dropped["commitIdentifier"], dropped["snapshotId"]),
                         ("dropped", "u", 7, 3))
        self.assertEqual((dropped["first"], dropped["again"]), (200, 200))

    def test_an_unreachable_catalog_ends_the_connection_with_no_answer(self):
        port, gone = free_port(), free_port()
        env = dict(os.environ, UAT_UPSTREAM=f"http://127.0.0.1:{gone}", UAT_PROXY_PORT=str(port), UAT_DROP_EVERY="3")
        proc = subprocess.Popen([sys.executable, "-I", str(PROXY)], env=env, stdout=subprocess.PIPE,
                                universal_newlines=True)
        try:
            self.assertIn("listening", proc.stdout.readline())
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/v1/config")
                conn.getresponse()
            self.assertEqual(json.loads(proc.stdout.readline())["kind"], "error")
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
