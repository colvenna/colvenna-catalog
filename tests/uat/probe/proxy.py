#!/usr/bin/env python3
"""The response-dropping proxy of UAT-FLT-01: a sidecar beside a writer, on loopback only, between
the writer's Paimon client (UAT_URI=http://127.0.0.1:<port>/...) and the catalog (UAT_UPSTREAM,
over TLS, verified against /ca/ca.crt when there is one, else the system's CAs). Every request is
passed on as it is; every UAT_DROP_EVERY-th commit (POST .../commit) is passed on, and once the
catalog has answered it, that answer is dropped and the same request sent again at once, as a
client whose response was lost sends it: the writer gets the second answer, which must be the
catalog recognising a replay. When the catalog cannot be reached, the client's connection is closed
with no answer, as its own connection would have failed. One JSON line per dropped commit, with the
commit's user, identifier and snapshot, and both answers. Python's standard library only."""

import http.client
import json
import os
import ssl
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = urllib.parse.urlsplit(os.environ["UAT_UPSTREAM"])
PORT = int(os.environ.get("UAT_PROXY_PORT", "18181"))
EVERY = int(os.environ.get("UAT_DROP_EVERY", "10"))
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "host",
       "content-length"}
LOCK = threading.Lock()
COMMITS = [0]


def context():
    ctx = ssl.create_default_context()
    if os.path.exists("/ca/ca.crt"):
        ctx.load_verify_locations(cafile="/ca/ca.crt")
    return ctx


CONTEXT = context()


def emit(record):
    record["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def forward(method, path, headers, body):
    if UPSTREAM.scheme == "http":  # the proxy's own tests only: the catalog is always reached over TLS
        conn = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=120)
    else:
        conn = http.client.HTTPSConnection(UPSTREAM.hostname, UPSTREAM.port or 443, context=CONTEXT, timeout=120)
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, [(k, v) for k, v in r.getheaders() if k.lower() not in HOP], r.read()
    finally:
        conn.close()


def commit_of(body):
    try:
        snapshot = json.loads(body or b"{}").get("snapshot") or {}
        return {"commitUser": snapshot.get("commitUser"), "commitIdentifier": snapshot.get("commitIdentifier"),
                "snapshotId": snapshot.get("id")}
    except ValueError:
        return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = UPSTREAM.netloc
        drop = False
        if self.command == "POST" and self.path.split("?", 1)[0].endswith("/commit"):
            with LOCK:
                COMMITS[0] += 1
                drop = COMMITS[0] % EVERY == 0
        try:
            status, out_headers, data = forward(self.command, self.path, headers, body)
            if drop:
                first = (status, data.decode("utf-8", "replace")[:300])
                # The answer is lost on its way back; the client sends the same request again.
                again = dict(headers)
                rid = headers.get("X-Request-Id") or headers.get("x-request-id")
                if rid:
                    again.pop("x-request-id", None)
                    again["X-Request-Id"] = rid + "-again"
                status, out_headers, data = forward(self.command, self.path, again, body)
                emit(dict({"kind": "dropped", "path": self.path, "first": first[0], "firstBody": first[1],
                           "again": status, "againBody": data.decode("utf-8", "replace")[:300],
                           "requestId": rid}, **commit_of(body)))
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            emit({"kind": "error", "path": self.path, "error": str(e)})
            # The client sees what it would without the proxy: its connection ends with no answer (an
            # answer of the proxy's own would be a server error, which the client does not retry).
            self.close_connection = True
            return
        self.send_response(status)
        for k, v in out_headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = handle_any  # noqa: N815

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    emit({"kind": "listening", "port": PORT, "upstream": UPSTREAM.hostname, "every": EVERY})
    server.serve_forever()


if __name__ == "__main__":
    main()
