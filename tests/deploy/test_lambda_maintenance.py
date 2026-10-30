"""deploy/lambda/maintenance.sh, the maintenance and purge functions' runtime (the image's
/opt/colvenna-catalog/lambda-maintenance.sh): against a fake Lambda Runtime API and a fake java."""
import base64
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helpers import REPO

SCRIPT = REPO / "deploy/lambda/maintenance.sh"
TOKEN = "colvenna_" + "A1b2_-" * 7

# The fake java: records the server command it was given; bootstrap-admin writes the token file it is
# handed (or a bad one), and FAKE_EXIT sets the exit status.
JAVA = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
command = args[args.index("com.colvenna.catalog.Main") + 1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(command) + "\n")
print("catalog says: " + " ".join(command) + ' "quoted" \\ backslash')
if command[0] == "bootstrap-admin":
    with open(command[3], "w") as f:
        f.write(os.environ.get("FAKE_TOKEN", "colvenna_x"))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class FakeRuntime(BaseHTTPRequestHandler):
    """GET .../invocation/next hands out the queued events (with the deadline header when one is
    queued with it), then 500 (which ends the loop); POST .../invocation/<id>/response and
    .../invocation/<id>/error record the answer, and the error type header of an error."""

    events: list = []
    answers: dict = {}
    error_types: dict = {}

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/2018-06-01/runtime/invocation/next" or not FakeRuntime.events:
            self.send_response(500)
            self.end_headers()
            return
        request_id, body, *deadline = FakeRuntime.events.pop(0)
        self.send_response(200)
        self.send_header("Lambda-Runtime-Aws-Request-Id", request_id)
        if deadline:
            self.send_header("Lambda-Runtime-Deadline-Ms", str(deadline[0]))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        FakeRuntime.answers[self.path] = json.loads(self.rfile.read(length))
        if self.path.endswith("/error"):
            FakeRuntime.error_types[self.path] = self.headers.get("Lambda-Runtime-Function-Error-Type")
        self.send_response(202)
        self.end_headers()


class MaintenanceLoop(unittest.TestCase):
    def run_loop(self, events, **env):
        FakeRuntime.events = list(events)
        FakeRuntime.answers = {}
        FakeRuntime.error_types = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fake = Path(tempfile.mkdtemp())
        java = fake / "java"
        java.write_text(JAVA)
        java.chmod(java.stat().st_mode | stat.S_IEXEC)
        environment = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_LOG": str(fake / "calls.log"),
                       "AWS_LAMBDA_RUNTIME_API": f"127.0.0.1:{server.server_address[1]}", "TMPDIR": str(fake), **env}
        out = subprocess.run(["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=60)
        calls = (fake / "calls.log").read_text().splitlines() if (fake / "calls.log").exists() else []
        return out, FakeRuntime.answers, calls

    def answer(self, answers, request_id):
        return answers[f"/2018-06-01/runtime/invocation/{request_id}/response"]

    def error(self, answers, request_id):
        return answers[f"/2018-06-01/runtime/invocation/{request_id}/error"]

    def test_migrate(self):
        out, answers, calls = self.run_loop([("r-1", '"migrate"')])
        # The loop ends only when the Runtime API fails it (Lambda then starts another environment).
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertEqual(calls, ["migrate"])
        answer = self.answer(answers, "r-1")
        self.assertEqual((answer["command"], answer["exitCode"]), ("migrate", 0))
        self.assertNotIn("token", answer)
        # The output travels as base64, so quotes and backslashes in it cannot break the JSON.
        self.assertIn('catalog says: migrate "quoted" \\ backslash', base64.b64decode(answer["log"]).decode())

    def test_a_failed_command_answers_its_exit_status(self):
        _, answers, _ = self.run_loop([("r-1", ' "migrate"\n')], FAKE_EXIT="3")
        self.assertEqual(self.answer(answers, "r-1")["exitCode"], 3)

    def test_bootstrap_admin_returns_the_token_once_and_keeps_no_file(self):
        _, answers, calls = self.run_loop([("r-1", '"bootstrap-admin"'), ("r-2", '"migrate"')], FAKE_TOKEN=TOKEN)
        self.assertEqual(calls[0].split()[:3], ["bootstrap-admin", "admin", "P90D"])
        token_file = calls[0].split()[3]
        self.assertFalse(os.path.exists(token_file), "the token file is removed with the invocation's directory")
        first = self.answer(answers, "r-1")
        self.assertEqual((first["command"], first["exitCode"], first["token"]), ("bootstrap-admin", 0, TOKEN))
        self.assertNotIn(TOKEN, base64.b64decode(first["log"]).decode())
        self.assertNotIn("token", self.answer(answers, "r-2"))

    def test_something_that_is_not_a_token_is_never_returned(self):
        _, answers, _ = self.run_loop([("r-1", '"bootstrap-admin"')], FAKE_TOKEN='colvenna_x","admin":true')
        answer = self.answer(answers, "r-1")
        self.assertEqual(answer["exitCode"], 1)
        self.assertNotIn("token", answer)
        self.assertIn("wrote no token", base64.b64decode(answer["log"]).decode())

    def test_any_other_payload_runs_nothing(self):
        _, answers, calls = self.run_loop([("r-1", '"serve"'), ("r-2", '{"command": "migrate"}'), ("r-3", "migrate; rm -rf /")])
        self.assertEqual(calls, [])
        for request_id in ("r-1", "r-2", "r-3"):
            self.assertEqual((self.answer(answers, request_id)["command"], self.answer(answers, request_id)["exitCode"]),
                             ("unknown", 2))

    def test_only_the_commands_the_function_runs(self):
        # The maintenance function's default: migrate and bootstrap-admin, never purge.
        _, answers, calls = self.run_loop([("r-1", '"purge"'), ("r-2", '"migrate"')])
        self.assertEqual(calls, ["migrate"])
        refused = self.answer(answers, "r-1")
        self.assertEqual((refused["command"], refused["exitCode"]), ("unknown", 2))
        self.assertIn("this function runs migrate, bootstrap-admin", base64.b64decode(refused["log"]).decode())
        # The purge function's: purge alone.
        _, answers, calls = self.run_loop([("r-1", '"migrate"'), ("r-2", '"bootstrap-admin"'), ("r-3", '"purge"')],
                                          COLVENNA_MAINTENANCE_COMMANDS="purge", FAKE_TOKEN=TOKEN)
        self.assertEqual(calls, ["purge"])
        for request_id in ("r-1", "r-2"):
            answer = self.answer(answers, request_id)
            self.assertEqual((answer["command"], answer["exitCode"]), ("unknown", 2))
            self.assertNotIn("token", answer)
        self.assertIn("this function runs purge", base64.b64decode(self.answer(answers, "r-1")["log"]).decode())

    def test_purge_stops_a_minute_before_the_invocations_deadline(self):
        out, answers, calls = self.run_loop([("r-1", '"purge"', 1767225600000), ("r-2", '"purge"')],
                                            COLVENNA_MAINTENANCE_COMMANDS="purge")
        # A scheduled run's answer goes nowhere: its output is in the function's log too.
        self.assertEqual(out.stdout.count("catalog says: purge"), 2, out.stdout)
        # Without the header there is no deadline to keep.
        self.assertEqual(calls, ["purge --until 1767225540000", "purge"])
        for request_id in ("r-1", "r-2"):
            self.assertEqual((self.answer(answers, request_id)["command"], self.answer(answers, request_id)["exitCode"]),
                             ("purge", 0))

    def test_a_failed_purge_is_the_invocations_error(self):
        _, answers, calls = self.run_loop([("r-1", '"purge"', 1767225600000), ("r-2", '"purge"')],
                                          COLVENNA_MAINTENANCE_COMMANDS="purge", FAKE_EXIT="1")
        self.assertEqual(len(calls), 2, "the loop goes on to the next invocation")
        for request_id in ("r-1", "r-2"):
            self.assertNotIn(f"/2018-06-01/runtime/invocation/{request_id}/response", answers)
            error = self.error(answers, request_id)
            self.assertEqual((error["errorType"], error["errorMessage"], error["exitCode"]),
                             ("PurgeFailed", "purge exited 1", 1))
            self.assertIn("catalog says: purge", base64.b64decode(error["log"]).decode())
            self.assertEqual(FakeRuntime.error_types[f"/2018-06-01/runtime/invocation/{request_id}/error"], "PurgeFailed")

    def test_a_failed_migrate_still_answers(self):
        # install.sh reads the exit status from the answer: only a purge reports an error.
        _, answers, _ = self.run_loop([("r-1", '"migrate"')], FAKE_EXIT="1")
        self.assertEqual(self.answer(answers, "r-1")["exitCode"], 1)
        self.assertNotIn("/2018-06-01/runtime/invocation/r-1/error", answers)

    def test_refuses_to_run_outside_lambda(self):
        out = subprocess.run(["bash", str(SCRIPT)], env={k: v for k, v in os.environ.items() if k != "AWS_LAMBDA_RUNTIME_API"},
                             capture_output=True, text=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not a Lambda execution environment", out.stderr)


if __name__ == "__main__":
    unittest.main()
