"""UAT-CAT-08's judging, offline."""

import json
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from cases import cat08

PROBE = Path(__file__).resolve().parent / "probe" / "probe.py"

LAMBDA_EVENT = 6 * 1024 * 1024
STATE = {"pointer": 1, "uuid": "u", "snapshots": 1, "intents": [["1", "PUBLISHED"]]}


class Bodies(unittest.TestCase):
    def test_the_commits_straddle_the_lambda_event_limit(self):
        # API Gateway hands the body to Lambda as a JSON string: escaped, it must fit 6 MB with the rest.
        under = cat08.commit_body("t", None, 1, cat08.UNDER)
        over = cat08.commit_body("t", None, 1, cat08.OVER)
        self.assertLess(len(json.dumps(under)) + 16 * 1024, LAMBDA_EVENT)
        self.assertGreater(len(json.dumps(over)), LAMBDA_EVENT)
        body = json.loads(under)
        self.assertEqual(len(body["statistics"]), cat08.UNDER)
        self.assertEqual(body["snapshot"]["id"], 1)


class Judged(unittest.TestCase):
    def test_body_over_the_limit(self):
        answer = {"status": 400, "body": {"message": "Request body exceeds 67108864 bytes."}}
        self.assertTrue(cat08.judge_body_over(answer, STATE, dict(STATE))[0])
        self.assertFalse(cat08.judge_body_over(answer, STATE, dict(STATE, pointer=2))[0])
        self.assertFalse(cat08.judge_body_over({"status": 400, "body": {"message": "bad"}}, STATE, STATE)[0])
        self.assertFalse(cat08.judge_body_over({"error": "connection: reset"}, STATE, STATE)[0])

    def test_commits(self):
        after = {"pointer": 2, "uuid": "v", "snapshots": 2, "intents": []}
        self.assertTrue(cat08.judge_commit_under({"status": 200}, STATE, after, 1)[0])
        self.assertFalse(cat08.judge_commit_under({"status": 200}, STATE, STATE, 1)[0])
        self.assertTrue(cat08.judge_commit_over({"status": 413}, STATE, dict(STATE), 1)[0])
        self.assertFalse(cat08.judge_commit_over({"status": 413}, STATE, after, 1)[0])
        self.assertFalse(cat08.judge_commit_over({"status": 502}, STATE, STATE, 1)[0])

    def test_pages(self):
        tables = ["t00", "t01", "t02"]
        self.assertTrue(cat08.judge_pages([3_000_000, 1_000_000], tables, [], tables)[0])
        self.assertFalse(cat08.judge_pages([4_194_304, 1], tables, [], tables)[0])
        self.assertFalse(cat08.judge_pages([1_000], tables, [], tables)[0])
        self.assertFalse(cat08.judge_pages([1, 1], tables[:2], [], tables)[0])
        self.assertFalse(cat08.judge_pages([1, 1], tables, ["page 2 is empty"], tables)[0])

    def test_item_over_the_limit(self):
        answer = {"status": 500, "body": {"message": "Table huge's details exceed the 4194304-byte response limit."}}
        self.assertTrue(cat08.judge_item_over({"status": 200}, answer, 1, "huge")[0])
        self.assertFalse(cat08.judge_item_over({"status": 200}, answer, 0, "huge")[0])
        self.assertFalse(cat08.judge_item_over({"status": 200}, {"status": 200, "body": {"tableDetails": []}}, 0, "huge")[0])


class EarlyAnswer(unittest.TestCase):
    """The probe's stream kind keeps an answer the server gives before the body is all there."""

    def test_the_status_and_body_of_an_early_refusal(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve():
            conn, _ = server.accept()
            conn.recv(65536)
            body = b'{"message": "Request body exceeds 67108864 bytes."}'
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nX-Request-Id: srv-1\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            conn.close()  # with the rest of the body unread

        threading.Thread(target=serve, daemon=True).start()
        plan = {"base": "", "ca": None, "tokens": {"admin": "colvenna_x"},
                "steps": [{"name": "big", "kind": "stream", "url": f"http://127.0.0.1:{port}/commit", "token": "admin",
                           "bytes": 32 * 1024 * 1024, "timeout": 30}]}
        p = subprocess.run([sys.executable, "-I", str(PROBE)], input=json.dumps(plan), stdout=subprocess.PIPE,
                           universal_newlines=True, timeout=120, check=True)
        r = json.loads(p.stdout.splitlines()[0])
        server.close()
        self.assertEqual(r["status"], 400)
        self.assertEqual(r["serverRequestId"], "srv-1")
        self.assertIn("67108864", r["body"]["message"])
        self.assertLess(r["sent"], 32 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
