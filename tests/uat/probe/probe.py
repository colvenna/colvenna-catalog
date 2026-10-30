#!/usr/bin/env python3
"""The UAT probe: runs inside a client pod (Python's standard library only) and makes the requests
of a plan, which it reads as JSON on stdin, printing one JSON line per result. The plan carries the
tokens, so that no token is ever an argument or an environment variable of any process:

  {"base": "https://<catalog>", "ca": "<PEM>" | null, "tokens": {"<name>": "<token>"},
   "strict": true | false, "steps": [<step>, ...]}

"strict" (default true) keeps Python's strict X.509 checks (VERIFY_X509_STRICT, which
ssl.create_default_context sets since Python 3.13: https://docs.python.org/3/library/ssl.html#ssl.create_default_context);
false drops them alone, the chain and the names still verified: for a previous release whose private CA
has no keyUsage (#49).

A step is one of:

  {"name": n, "method": "GET", "path": "/v1/config?warehouse=colvenna" | "url": "https://...",
   "token": "<name>" | null, "authorization": "<raw Authorization header>" | null,
   "body": <JSON> | "text": "<raw body>" | "bytes": <size of a generated body>,
   "headers": {...}, "requestId": "<X-Request-Id to send>", "timeout": 20,
   "repeat": {"every": <s>, "for": <s>}, "sleep": <s before the step>, "full": false}
      an HTTPS (or HTTP) request; prints its status, the X-Request-Id the server answered with
      ("serverRequestId", null when the answer had none; "requestId" is it, or the one sent), its body
      (parsed JSON when it is JSON, cut to 64 KiB unless "full"), and the server certificate's names. With "repeat", the request again and again, a result each time.
  {"name": n, "kind": "stream", "method": "POST", "url": u, "token": t, "bytes": <size>}
      a generated body of the size, sent in pieces while the answer is watched for, so that an answer
      the server gives before the body is all there (a body over its limit) is read whole: its status
      and its body, as for a request.
  {"name": n, "kind": "tcp", "host": h, "port": p, "timeout": 5}
      whether a TCP connection is made: "connected", "refused", "timeout" or "unreachable".
  {"name": n, "kind": "plain", "host": h, "port": p, "timeout": 5}
      plain HTTP to a port: whether a connection was made ("connected") and an HTTP response came
      back ("answer": "http") or not ("none", with why).
  {"name": n, "kind": "dns", "host": h}
      the addresses the name resolves to, or the error.
  {"name": n, "kind": "sleep", "seconds": s}

Every result has "name", "time" (UTC) and "ms"; an error is {"error": "<kind>: <message>"}, never
a status: a refused connection or a timeout is not an answer."""

import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

LIMIT = 64 * 1024


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(result):
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()


def context(ca_file, plan):
    # The system's CAs and, when given, the catalog's own: the same client reaches both.
    ctx = ssl.create_default_context()
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    if not plan.get("strict", True):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def names(cert):
    if not cert:
        return []
    out = [v for k, v in cert.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    for rdn in cert.get("subject", ()):
        out += [v for k, v in rdn if k == "commonName"]
    return out


class Peer(urllib.request.HTTPSHandler):
    """An HTTPS handler that remembers the certificate of the server it reached."""

    def __init__(self, ctx):
        super().__init__(context=ctx)
        self.cert = None

    def https_open(self, req):
        handler = self

        class Connection(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                try:
                    handler.cert = self.sock.getpeercert()
                except (OSError, ValueError):
                    handler.cert = None

        return self.do_open(Connection, req, context=self._context)


def body_of(step):
    if "body" in step and step["body"] is not None:
        return json.dumps(step["body"]).encode()
    if "text" in step:
        return step["text"].encode()
    if "bytes" in step:
        # A JSON object of the given size: {"x":"aaaa..."}.
        n = int(step["bytes"])
        return b'{"x":"' + b"a" * max(0, n - 8) + b'"}'
    return None


def request(plan, step, ca_file):
    url = step.get("url") or plan["base"].rstrip("/") + step["path"]
    data = body_of(step)
    headers = dict(step.get("headers") or {})
    rid = step.get("requestId") or str(uuid.uuid4())
    headers.setdefault("X-Request-Id", rid)
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    if step.get("token"):
        headers["Authorization"] = "Bearer " + plan["tokens"][step["token"]]
    elif step.get("authorization") is not None:
        headers["Authorization"] = step["authorization"]
    req = urllib.request.Request(url, data=data, headers=headers, method=step.get("method", "GET"))
    peer = Peer(context(ca_file, plan))
    opener = urllib.request.build_opener(peer)
    started = time.monotonic()
    result = {"name": step["name"], "time": stamp(), "sentRequestId": rid}
    try:
        with opener.open(req, timeout=step.get("timeout", 20)) as r:
            status, raw, got = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, got = e.code, e.headers
        try:
            raw = e.read()
        except OSError as gone:  # the answer came, then the connection went (a body over a server's limit)
            raw = b""
            result["bodyError"] = f"connection: {gone}"
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        reason = getattr(e, "reason", e)
        kind = ("timeout" if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason)
                else "refused" if isinstance(reason, ConnectionRefusedError)
                else "tls" if isinstance(reason, ssl.SSLError) else "connection")
        result.update(error=f"{kind}: {reason}", ms=int((time.monotonic() - started) * 1000))
        return result
    result["ms"] = int((time.monotonic() - started) * 1000)
    result["status"] = status
    # The catalog names each request it answers with an id of its own (X-Request-Id), whatever the
    # client sent: its access log and audit know the request by that id. An answer without one came
    # from something in front of the catalog (API Gateway, say).
    result["serverRequestId"] = got.get("X-Request-Id")
    result["requestId"] = result["serverRequestId"] or rid
    result["headers"] = {k.lower(): v for k, v in got.items()
                         if k.lower() in ("content-type", "content-length", "x-request-id", "x-amzn-requestid",
                                          "x-amzn-errortype", "www-authenticate", "retry-after")}
    result["bytes"] = len(raw)
    text = raw.decode("utf-8", "replace")
    if not step.get("full") and len(text) > LIMIT:
        text = text[:LIMIT]
        result["truncated"] = True
    try:
        result["body"] = json.loads(text)
    except ValueError:
        result["body"] = text
    result["peer"] = names(peer.cert)
    return result


def stream(plan, step, ca_file):
    """A big generated body over a socket of its own, sent in 1 MiB pieces; between pieces, whatever
    the server has answered is read, so that an early answer is kept before the server closes."""
    import select

    url = urllib.parse.urlsplit(step["url"])
    size = int(step["bytes"])
    head = (f"{step.get('method', 'POST')} {url.path}{'?' + url.query if url.query else ''} HTTP/1.1\r\n"
            f"Host: {url.hostname}\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n"
            f"X-Request-Id: {step.get('requestId') or uuid.uuid4()}\r\nConnection: close\r\n")
    if step.get("token"):
        head += "Authorization: Bearer " + plan["tokens"][step["token"]] + "\r\n"
    started = time.monotonic()
    result = {"name": step["name"], "time": stamp()}
    raw = socket.create_connection((url.hostname, url.port or 443), timeout=step.get("timeout", 300))
    sock = context(ca_file, plan).wrap_socket(raw, server_hostname=url.hostname) if url.scheme == "https" else raw
    answer, sent = b"", 0
    try:
        sock.sendall((head + "\r\n").encode() + b'{"x":"')
        sent = 6
        piece = b"a" * (1 << 20)
        while sent < size - 2:
            n = min(len(piece), size - 2 - sent)
            try:
                sock.sendall(piece[:n])
            except OSError as e:
                result["sendError"] = f"{type(e).__name__}: {e}"
                break
            sent += n
            if select.select([sock], [], [], 0)[0]:
                # Readable may be TLS's own records (a session ticket): only application data is an answer.
                sock.setblocking(False)
                try:
                    early = sock.recv(65536)
                except (ssl.SSLWantReadError, BlockingIOError):
                    early = None
                except OSError as e:
                    result["readError"] = f"{type(e).__name__}: {e}"
                    early = b""
                sock.settimeout(step.get("timeout", 300))
                if early is not None:
                    answer += early
                    break
        else:
            sock.sendall(b'"}')
            sent = size
        sock.settimeout(30)
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError as e:
                result.setdefault("readError", f"{type(e).__name__}: {e}")
                break
            if not chunk:
                break
            answer += chunk
    finally:
        sock.close()
    result.update(ms=int((time.monotonic() - started) * 1000), sent=sent)
    head_part, _, body = answer.partition(b"\r\n\r\n")
    lines = head_part.decode("latin-1").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        result["error"] = "connection: no answer" + (f" ({result.get('readError') or result.get('sendError')})")
        return result
    result["status"] = int(lines[0].split(" ")[1])
    headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    result["serverRequestId"] = headers.get("X-Request-Id") or headers.get("x-request-id")
    if headers.get("Transfer-Encoding", "").lower() == "chunked":
        out, rest = b"", body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            n = int(size_line.split(b";")[0] or b"0", 16)
            if n == 0:
                break
            out, rest = out + rest[:n], rest[n + 2:]
        body = out
    text = body.decode("utf-8", "replace")
    try:
        result["body"] = json.loads(text)
    except ValueError:
        result["body"] = text
    return result


def tcp(step):
    started = time.monotonic()
    result = {"name": step["name"], "time": stamp()}
    try:
        with socket.create_connection((step["host"], int(step["port"])), timeout=step.get("timeout", 5)):
            result["connect"] = "connected"
    except ConnectionRefusedError:
        result["connect"] = "refused"
    except (socket.timeout, TimeoutError):
        result["connect"] = "timeout"
    except OSError as e:
        result["connect"] = "unreachable"
        result["error"] = f"connection: {e}"
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def plain(step):
    """Plain HTTP to the port: an HTTP status line back, or none."""
    started = time.monotonic()
    result = {"name": step["name"], "time": stamp()}
    result["connected"] = False
    try:
        with socket.create_connection((step["host"], int(step["port"])), timeout=step.get("timeout", 5)) as s:
            result["connected"] = True
            s.sendall(f"GET / HTTP/1.1\r\nHost: {step['host']}\r\nConnection: close\r\n\r\n".encode())
            s.settimeout(step.get("timeout", 5))
            data = b""
            try:
                while len(data) < 4096:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except (socket.timeout, TimeoutError):
                pass
            except OSError as e:
                result["error"] = f"connection: {e}"
        first = data.split(b"\r\n", 1)[0].decode("latin-1")
        result["answer"] = "http" if first.startswith("HTTP/") else "none"
        result["first"] = first[:200]
    except (socket.timeout, TimeoutError):
        result.update(answer="none", error="timeout: no connection")
    except OSError as e:
        result.update(answer="none", error=f"connection: {e}")
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def dns(step):
    result = {"name": step["name"], "time": stamp()}
    try:
        infos = socket.getaddrinfo(step["host"], None, proto=socket.IPPROTO_TCP)
        result["addresses"] = sorted({i[4][0] for i in infos})
    except socket.gaierror as e:
        result["error"] = f"dns: {e}"
    return result


def main():
    plan = json.load(sys.stdin)
    ca_file = None
    if plan.get("ca"):
        fd, ca_file = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(plan["ca"])
    try:
        for step in plan["steps"]:
            if step.get("sleep"):
                time.sleep(step["sleep"])
            kind = step.get("kind", "http")
            if kind == "tcp":
                emit(tcp(step))
            elif kind == "stream":
                emit(stream(plan, step, ca_file))
            elif kind == "plain":
                emit(plain(step))
            elif kind == "dns":
                emit(dns(step))
            elif kind == "sleep":
                time.sleep(step["seconds"])
                emit({"name": step["name"], "time": stamp(), "ms": int(step["seconds"] * 1000)})
            elif step.get("repeat"):
                end = time.monotonic() + step["repeat"]["for"]
                n = 0
                while time.monotonic() < end:
                    began = time.monotonic()
                    # Each request its own id: the given one numbered, or a new one.
                    one = dict(step, requestId=f"{step['requestId']}-{n}" if step.get("requestId") else None)
                    emit(request(plan, one, ca_file))
                    n += 1
                    time.sleep(max(0.0, step["repeat"]["every"] - (time.monotonic() - began)))
            else:
                emit(request(plan, step, ca_file))
    finally:
        if ca_file:
            os.unlink(ca_file)


if __name__ == "__main__":
    main()
