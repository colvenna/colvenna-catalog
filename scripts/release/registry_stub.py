"""A stub OCI registry for the offline tests of promote.sh and publish-ghcr.sh: the distribution API's
pull and push endpoints that crane and Helm use (manifests, blobs, uploads, cross-repository mounts,
tag lists), held in memory and served over plain HTTP on 127.0.0.1, a loopback registry the release
scripts accept for a rehearsal.

  credentials  (user, password): every write needs them (HTTP basic), and so does every read unless
               the registry is public; None: anyone may read and write
  public       whether a read needs no credentials (a GHCR package made public); a private one answers
               401 to an anonymous read
  immutable    a tag is never moved to another digest (the release registry's ECR repositories are
               IMMUTABLE); writing the digest it holds again is fine

Every request is logged (requests: method, path, whether it carried valid credentials), so a test can
say what was read and written. seed_image and seed_blob put content in without the API."""
import base64
import hashlib
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def digest_of(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    def __init__(self, credentials=None, public=True, immutable=False):
        self.credentials, self.public, self.immutable = credentials, public, immutable
        self.blobs = {}
        self.manifests = {}  # repository -> {digest: (media type, bytes)}
        self.tags = {}  # repository -> {tag: digest}
        self.uploads = {}
        self.requests = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def seed_blob(self, data):
        digest = digest_of(data)
        self.blobs[digest] = data
        return digest

    def seed_manifest(self, repository, data, media_type=OCI_MANIFEST, tag=None):
        digest = digest_of(data)
        self.manifests.setdefault(repository, {})[digest] = (media_type, data)
        if tag:
            self.tags.setdefault(repository, {})[tag] = digest
        return digest

    def seed_image(self, repository, tag, labels, os="linux", architecture="amd64", layer=b"layer"):
        """A small image (one layer) with these labels and platform; its manifest's digest."""
        config = json.dumps({"architecture": architecture, "os": os, "config": {"Labels": labels},
                             "rootfs": {"type": "layers", "diff_ids": [digest_of(layer)]}}).encode()
        manifest = json.dumps({
            "schemaVersion": 2, "mediaType": OCI_MANIFEST,
            "config": {"mediaType": OCI_CONFIG, "digest": self.seed_blob(config), "size": len(config)},
            "layers": [{"mediaType": OCI_LAYER, "digest": self.seed_blob(layer), "size": len(layer)}],
        }).encode()
        return self.seed_manifest(repository, manifest, tag=tag)

    def tag(self, repository, tag):
        """The digest a tag holds, or None."""
        return self.tags.get(repository, {}).get(tag)

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "PATCH", "DELETE")]

    def authorized(self, header):
        if self.credentials is None:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return (user, password) == tuple(self.credentials)


def _handler(registry):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", ""):
                data = b""
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return data
                    data += self.rfile.read(size)
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def answer(self, status, body=b"", headers=None, head=False):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def error(self, status, code, message, headers=None):
            body = json.dumps({"errors": [{"code": code, "message": message}]}).encode()
            self.answer(status, body, {"Content-Type": "application/json", **(headers or {})},
                        head=self.command == "HEAD")

        def challenge(self):
            self.error(401, "UNAUTHORIZED", "authentication required",
                       {"WWW-Authenticate": 'Basic realm="registry_stub"'})

        def handle_one(self):
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            data = self.body() if self.command in ("POST", "PUT", "PATCH") else b""
            authorized = registry.authorized(self.headers.get("Authorization"))
            with registry.lock:
                registry.requests.append((self.command, path, authorized))
            write = self.command in ("POST", "PUT", "PATCH", "DELETE")
            if path in ("/v2", "/v2/"):
                return self.answer(200, b"{}") if authorized else self.challenge()
            if not authorized and (write or not registry.public):
                return self.challenge()
            found = re.fullmatch(r"/v2/(.+)/blobs/uploads/([^/]*)", path)
            if found:
                return self.upload(found[1], found[2], query, data)
            found = re.fullmatch(r"/v2/(.+)/blobs/(sha256:[0-9a-f]{64})", path)
            if found and self.command in ("GET", "HEAD"):
                blob = registry.blobs.get(found[2])
                if blob is None:
                    return self.error(404, "BLOB_UNKNOWN", found[2])
                return self.answer(200, blob, {"Content-Type": "application/octet-stream",
                                               "Docker-Content-Digest": found[2]}, head=self.command == "HEAD")
            found = re.fullmatch(r"/v2/(.+)/manifests/([^/]+)", path)
            if found:
                return self.manifest(found[1], found[2], data)
            found = re.fullmatch(r"/v2/(.+)/tags/list", path)
            if found and self.command == "GET":
                tags = sorted(registry.tags.get(found[1], {}))
                if found[1] not in registry.manifests:
                    return self.error(404, "NAME_UNKNOWN", found[1])
                return self.answer(200, json.dumps({"name": found[1], "tags": tags}).encode(),
                                   {"Content-Type": "application/json"})
            return self.error(404, "UNSUPPORTED", f"{self.command} {path}")

        def upload(self, repository, session, query, data):
            if self.command == "POST" and not session:
                mount = query.get("mount", [None])[0]
                if mount and mount in registry.blobs:
                    return self.answer(201, b"", {"Location": f"/v2/{repository}/blobs/{mount}",
                                                  "Docker-Content-Digest": mount})
                digest = query.get("digest", [None])[0]
                if digest:
                    return self.complete(repository, digest, data)
                session = uuid.uuid4().hex
                registry.uploads[session] = b""
                return self.answer(202, b"", {"Location": f"/v2/{repository}/blobs/uploads/{session}",
                                              "Docker-Upload-UUID": session, "Range": "0-0"})
            if session not in registry.uploads:
                return self.error(404, "BLOB_UPLOAD_UNKNOWN", session)
            registry.uploads[session] += data
            if self.command == "PATCH":
                size = len(registry.uploads[session])
                return self.answer(202, b"", {"Location": f"/v2/{repository}/blobs/uploads/{session}",
                                              "Docker-Upload-UUID": session, "Range": f"0-{max(size - 1, 0)}"})
            if self.command == "PUT":
                return self.complete(repository, query.get("digest", [""])[0], registry.uploads.pop(session))
            return self.error(405, "UNSUPPORTED", self.command)

        def complete(self, repository, digest, data):
            if digest != digest_of(data):
                return self.error(400, "DIGEST_INVALID", f"{digest} is not the content's {digest_of(data)}")
            registry.blobs[digest] = data
            return self.answer(201, b"", {"Location": f"/v2/{repository}/blobs/{digest}",
                                          "Docker-Content-Digest": digest})

        def manifest(self, repository, reference, data):
            held = registry.manifests.get(repository, {})
            if self.command in ("GET", "HEAD"):
                digest = reference if DIGEST.fullmatch(reference) else registry.tags.get(repository, {}).get(reference)
                if digest not in held:
                    return self.error(404, "MANIFEST_UNKNOWN", f"{repository}:{reference}")
                media_type, body = held[digest]
                return self.answer(200, body, {"Content-Type": media_type, "Docker-Content-Digest": digest},
                                   head=self.command == "HEAD")
            if self.command != "PUT":
                return self.error(405, "UNSUPPORTED", self.command)
            digest = digest_of(data)
            if DIGEST.fullmatch(reference) and reference != digest:
                return self.error(400, "DIGEST_INVALID", reference)
            try:
                parsed = json.loads(data)
            except ValueError:
                return self.error(400, "MANIFEST_INVALID", "not JSON")
            for blob in [parsed.get("config", {})] + parsed.get("layers", []):
                if blob.get("digest") and blob["digest"] not in registry.blobs:
                    return self.error(400, "MANIFEST_BLOB_UNKNOWN", blob["digest"])
            tags = registry.tags.setdefault(repository, {})
            if not DIGEST.fullmatch(reference) and registry.immutable and tags.get(reference, digest) != digest:
                return self.error(400, "TAG_INVALID", f"{repository}:{reference} is immutable")
            media_type = self.headers.get("Content-Type") or parsed.get("mediaType") or OCI_MANIFEST
            with registry.lock:
                registry.manifests.setdefault(repository, {})[digest] = (media_type, data)
                if not DIGEST.fullmatch(reference):
                    tags[reference] = digest
            return self.answer(201, b"", {"Location": f"/v2/{repository}/manifests/{digest}",
                                          "Docker-Content-Digest": digest})

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = handle_one

    return Handler
