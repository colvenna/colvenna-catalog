"""ensure_tls (deploy/lib/catalog.sh): the private CA and the server certificate carry the extensions that
clients checking X.509 strictly require (#58), a CA made without them is issued again from its own key,
and a rerun changes nothing. Real openssl; kubectl is a fake that keeps Secrets as files."""
import base64
import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import run_bash

KUBECTL = r'''#!/usr/bin/env python3
# A fake kubectl for ensure_tls: Secrets in the namespace, as JSON files; every call logged.
import base64, json, os, sys
here = os.environ["FAKE_KUBE"]
args = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write(" ".join(args) + "\n")
if args[:1] == ["-n"]:
    args = args[2:]
def path(name):
    return os.path.join(here, name + ".json")
def store(obj, must_exist):
    if os.path.exists(path(obj["metadata"]["name"])) != must_exist:
        sys.exit(f"secret {obj['metadata']['name']}: exists={not must_exist}")
    json.dump(obj, open(path(obj["metadata"]["name"]), "w"))
if args[:2] == ["get", "secret"]:
    if not os.path.exists(path(args[2])):
        sys.exit(f'Error from server (NotFound): secrets "{args[2]}" not found')
    if "-o" in args:
        key = args[args.index("-o") + 1][len("jsonpath={.data."):-1].replace("\\.", ".")
        sys.stdout.write(json.load(open(path(args[2])))["data"][key])
elif args[:3] == ["create", "secret", "generic"]:
    data = {}
    for a in args:
        if a.startswith("--from-file="):
            key, file = a[len("--from-file="):].split("=", 1)
            data[key] = base64.b64encode(open(file, "rb").read()).decode()
    obj = {"kind": "Secret", "metadata": {"name": args[3]}, "data": data}
    if "--dry-run=client" in args:
        print(json.dumps(obj))
    else:
        store(obj, False)
elif args[:2] in (["apply", "-f"], ["replace", "-f"]):
    obj = json.load(sys.stdin)
    store(obj, args[0] == "replace" or os.path.exists(path(obj["metadata"]["name"])))
else:
    sys.exit("fake kubectl: " + " ".join(sys.argv[1:]))
'''

NAMESPACE, RELEASE = "colvenna-catalog", "colvenna-catalog"
HOST = f"{RELEASE}.{NAMESPACE}.svc"


def openssl(*args, stdin=None):
    out = subprocess.run(["openssl", *args], capture_output=True, text=True, input=stdin)
    assert out.returncode == 0, out.stderr
    return out.stdout


def strict_context(ca_file):
    """What Python 3.13's ssl.create_default_context() does, on any Python 3: VERIFY_X509_STRICT and
    VERIFY_X509_PARTIAL_CHAIN on, the host name checked."""
    ctx = ssl.create_default_context(cafile=str(ca_file))
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


class Cluster:
    """The namespace's Secrets (files) and a work directory, as install.sh gives ensure_tls."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        kubectl = self.bin / "kubectl"
        kubectl.write_text(KUBECTL)
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)

    def ensure_tls(self, *extra):
        work = Path(tempfile.mkdtemp(dir=self.dir))
        (self.dir / "calls.log").unlink(missing_ok=True)
        out = run_bash(f'ensure_tls "{work}" {" ".join(extra)}; echo "fingerprint=$TLS_FINGERPRINT"', path_prepend=self.bin,
                       env={"FAKE_KUBE": str(self.dir), "COLVENNA_NAMESPACE": NAMESPACE, "COLVENNA_RELEASE_NAME": RELEASE})
        assert out.returncode == 0, out.stdout + out.stderr
        self.out = out.stdout
        self.calls = (self.dir / "calls.log").read_text().splitlines()
        return out.stdout.rsplit("fingerprint=", 1)[1].strip()

    def value(self, secret, key):
        return base64.b64decode(json.loads((self.dir / f"{secret}.json").read_text())["data"][key])

    def put(self, secret, **files):
        data = {k: base64.b64encode(Path(v).read_bytes()).decode() for k, v in files.items()}
        (self.dir / f"{secret}.json").write_text(json.dumps({"kind": "Secret", "metadata": {"name": secret}, "data": data}))

    def file(self, name, content):
        path = self.dir / name
        path.write_bytes(content)
        return path

    def ca(self, name="ca.crt"):
        return self.file(name, self.value("colvenna-catalog-ca-key", "ca.crt"))

    def server(self):
        """The server certificate's PEM file, and its chain and key as the server presents them."""
        p12 = self.file("keystore.p12", self.value("colvenna-catalog-tls", "keystore.p12"))
        password = self.file("password", self.value("colvenna-catalog-tls", "password"))
        args = ["pkcs12", "-in", str(p12), "-passin", f"file:{password}"]
        leaf = openssl(*args, "-nokeys", "-clcerts")
        chain = self.file("chain.pem", (leaf + openssl(*args, "-nokeys", "-cacerts")).encode())
        key = self.file("server.key", openssl(*args, "-nocerts", "-nodes").encode())
        os.chmod(key, 0o600)
        return self.file("server.crt", openssl("x509", stdin=leaf).encode()), chain, key

    def text(self, pem):
        return openssl("x509", "-in", str(pem), "-noout", "-text")


def handshake(chain, key, ca_file):
    """A TLS handshake with a server presenting <chain>, by a client trusting <ca_file> that checks
    X.509 strictly and the host name: None, or why it failed."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(chain), str(key))
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with strict_context(ca_file).wrap_socket(raw, server_hostname=HOST):
                return None
    except ssl.SSLCertVerificationError as e:
        return e.verify_message
    finally:
        thread.join(10)
        listener.close()


def pre_58_install(cluster):
    """The CA and the server certificate as ensure_tls made them before #58: no keyUsage on either."""
    d = Path(tempfile.mkdtemp(dir=cluster.dir))
    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "825",
            "-subj", "/CN=colvenna-catalog private CA", "-keyout", str(d / "ca.key"), "-out", str(d / "ca.crt"))
    cluster.put("colvenna-catalog-ca-key", **{"ca.key": d / "ca.key", "ca.crt": d / "ca.crt"})
    old_server(cluster, d)
    return d / "ca.crt"


def old_server(cluster, d, ca_dir=None):
    """A server certificate as before #58 (subjectAltName and extendedKeyUsage only), signed by the CA
    in ca_dir (default: d), in the TLS Secret."""
    ca_dir = ca_dir or d
    sans = ",".join(f"DNS:{n}" for n in (RELEASE, f"{RELEASE}.{NAMESPACE}", HOST, f"{HOST}.cluster.local", "localhost"))
    ext = cluster.file("old.ext", f"subjectAltName={sans}\nextendedKeyUsage=serverAuth\n".encode())
    openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-subj", f"/CN={HOST}",
            "-keyout", str(d / "server.key"), "-out", str(d / "server.csr"))
    openssl("x509", "-req", "-in", str(d / "server.csr"), "-CA", str(ca_dir / "ca.crt"), "-CAkey", str(ca_dir / "ca.key"),
            "-CAcreateserial", "-days", "397", "-out", str(d / "server.crt"), "-extfile", str(ext))
    (d / "password").write_text("p" * 48)
    openssl("pkcs12", "-export", "-in", str(d / "server.crt"), "-inkey", str(d / "server.key"), "-certfile",
            str(ca_dir / "ca.crt"), "-name", "colvenna-catalog", "-out", str(d / "keystore.p12"), "-passout", f"file:{d / 'password'}")
    cluster.put("colvenna-catalog-tls", **{"keystore.p12": d / "keystore.p12", "password": d / "password"})


def public_key(pem):
    return openssl("x509", "-in", str(pem), "-noout", "-pubkey")


def changes(calls):
    return [c for c in calls if c.startswith(("apply", "replace")) or (c.startswith("create") and "--dry-run=client" not in c)]


class NewInstall(unittest.TestCase):
    def test_the_ca_and_the_certificate_pass_strict_checks(self):
        cluster = Cluster()
        cluster.ensure_tls()
        ca = cluster.ca()
        text = cluster.text(ca)
        self.assertRegex(text, r"X509v3 Basic Constraints: critical\s+CA:TRUE")
        self.assertRegex(text, r"X509v3 Key Usage: critical\s+Certificate Sign, CRL Sign")
        self.assertIn("X509v3 Subject Key Identifier", text)
        crt, chain, key = cluster.server()
        text = cluster.text(crt)
        self.assertRegex(text, r"X509v3 Basic Constraints: critical\s+CA:FALSE")
        self.assertRegex(text, r"X509v3 Key Usage: critical\s+Digital Signature\n")
        self.assertRegex(text, r"X509v3 Extended Key Usage: \s*TLS Web Server Authentication\n")
        self.assertRegex(text, rf"X509v3 Subject Alternative Name: \s*DNS:{RELEASE}, .*DNS:{HOST}, .*DNS:localhost")
        ski = cluster.text(ca).split("X509v3 Subject Key Identifier:")[1].split()[0]
        self.assertRegex(text, rf"X509v3 Authority Key Identifier: \s*(keyid:)?{ski}")
        self.assertIsNone(handshake(chain, key, ca))

    def test_a_rerun_changes_nothing(self):
        cluster = Cluster()
        first = cluster.ensure_tls()
        ca = cluster.value("colvenna-catalog-ca-key", "ca.crt")
        self.assertEqual(cluster.ensure_tls(), first)
        self.assertEqual(changes(cluster.calls), [])
        self.assertEqual(cluster.value("colvenna-catalog-ca-key", "ca.crt"), ca)
        self.assertNotIn("issued again", cluster.out)


class BeforeTheFix(unittest.TestCase):
    """An install whose CA and certificate ensure_tls made before #58."""

    def test_the_strict_check_refuses_it(self):
        cluster = Cluster()
        old_ca = pre_58_install(cluster)
        _, chain, key = cluster.server()
        self.assertEqual(handshake(chain, key, old_ca), "CA cert does not include key usage extension")

    def test_its_ca_is_issued_again_from_its_key_and_the_old_copy_still_verifies(self):
        cluster = Cluster()
        old_ca = pre_58_install(cluster)
        old_key = cluster.value("colvenna-catalog-ca-key", "ca.key")
        before = openssl("x509", "-in", str(cluster.server()[0]), "-noout", "-fingerprint", "-sha256")
        fingerprint = cluster.ensure_tls()
        self.assertIn("issued again from the same key and name", cluster.out)
        self.assertIn(f"-n {NAMESPACE} create secret generic colvenna-catalog-ca-key", "\n".join(cluster.calls))
        self.assertIn("replace -f -", cluster.calls)
        new_ca = cluster.ca("new-ca.crt")
        self.assertEqual(cluster.value("colvenna-catalog-ca-key", "ca.key"), old_key)
        self.assertEqual(public_key(new_ca), public_key(old_ca))
        self.assertEqual(openssl("x509", "-in", str(new_ca), "-noout", "-subject"),
                         openssl("x509", "-in", str(old_ca), "-noout", "-subject"))
        self.assertRegex(cluster.text(new_ca), r"X509v3 Key Usage: critical\s+Certificate Sign, CRL Sign")
        # The certificate is made again, under the CA's new certificate (and rolls the pods).
        crt, chain, key = cluster.server()
        self.assertNotEqual(openssl("x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"), before)
        self.assertEqual(len(fingerprint), 64)
        self.assertIsNone(handshake(chain, key, new_ca))
        # A client that kept the CA's earlier copy still verifies the new certificate (as Java and curl
        # do); only a strict check needs the new copy.
        self.assertIn(": OK", openssl("verify", "-purpose", "sslserver", "-CAfile", str(old_ca), str(crt)))
        self.assertEqual(handshake(chain, key, old_ca), "CA cert does not include key usage extension")
        # And the run after it changes nothing.
        self.assertEqual(cluster.ensure_tls(), fingerprint)
        self.assertEqual(changes(cluster.calls), [])

    def test_a_certificate_without_its_extensions_is_made_again_under_the_same_ca(self):
        cluster = Cluster()
        cluster.ensure_tls()
        ca = cluster.value("colvenna-catalog-ca-key", "ca.crt")
        d = Path(tempfile.mkdtemp(dir=cluster.dir))
        (d / "ca.crt").write_bytes(ca)
        (d / "ca.key").write_bytes(cluster.value("colvenna-catalog-ca-key", "ca.key"))
        old_server(cluster, d)
        cluster.ensure_tls()
        self.assertEqual(changes(cluster.calls), ["apply -f -"])
        self.assertEqual(cluster.value("colvenna-catalog-ca-key", "ca.crt"), ca)
        crt, chain, key = cluster.server()
        self.assertRegex(cluster.text(crt), r"X509v3 Key Usage: critical\s+Digital Signature\n")
        self.assertIsNone(handshake(chain, key, cluster.ca()))

    def test_a_certificate_of_another_ca_is_made_again(self):
        cluster = Cluster()
        cluster.ensure_tls()
        other = Path(tempfile.mkdtemp(dir=cluster.dir))
        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30",
                "-subj", "/CN=another CA", "-addext", "keyUsage=critical,keyCertSign", "-keyout", str(other / "ca.key"),
                "-out", str(other / "ca.crt"))
        old_server(cluster, Path(tempfile.mkdtemp(dir=cluster.dir)), ca_dir=other)
        cluster.ensure_tls()
        self.assertEqual(changes(cluster.calls), ["apply -f -"])
        _, chain, key = cluster.server()
        self.assertIsNone(handshake(chain, key, cluster.ca()))


if __name__ == "__main__":
    unittest.main()
