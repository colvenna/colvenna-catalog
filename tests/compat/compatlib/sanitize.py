"""What the runner keeps never holds a deployment value: every log and result it writes goes
through a Sanitizer, which replaces the run's secrets and URI, and any account ID, ARN, IP address,
bucket, AWS host name or access key ID it finds, with a placeholder. An account ID is twelve digits
standing alone; the same digits as the last group of a UUID (a request id) are left as they are."""

import re

# A UUID's shape: 8-4-4-4-12 hexadecimal digits, standing alone.
UUID = r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
# Addresses that are no deployment's: loopback and the unspecified address.
KEEP_IPS = {"127.0.0.1", "0.0.0.0"}  # nosec B104 - addresses left unredacted, nothing binds to them


def _address(s):
    """<IP> for an IPv4 address of a deployment; loopback, the unspecified address and what is no
    address (an octet over 255) are left as they are."""
    return s if s in KEEP_IPS or not all(0 <= int(part) <= 255 for part in s.split(".")) else "<IP>"


PATTERNS = [
    # ARNs first: they hold account IDs and names.
    (re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:[^\s\"'\],)}]+"), "<ARN>"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "<ACCESS_KEY_ID>"),
    # A catalog token; not the server's own metric names (colvenna_catalog_requests_total, ...).
    (re.compile(r"\bcolvenna_(?!catalog_[a-z_]+(?![A-Za-z0-9_-]))[A-Za-z0-9_-]{16,}"), "<TOKEN>"),
    # An STS session token wherever it stands (Doris's BE logs one with each S3 client it makes).
    (re.compile(r"\b(?:IQoJ|FwoG)[A-Za-z0-9+/=]{40,}"), "<REDACTED>"),
    # Short credential keys and their values, as engines log them: (ak=..., token=...), and a token
    # after a colon, as Doris's BE logs its heartbeat token ("get token. token: <uuid>", "set new
    # auth token: <uuid>").
    (re.compile(r"(?i)\b((?:ak|sk|access_?key|secret_?key)\s*=\s*|token\s*[=:]\s*)[^\s,;)}\]\"']{6,}"),
     r"\1<REDACTED>"),
    # A credential printed as a key and its value (fs.s3a.secret.key=..., "s3.session-token": "...").
    (re.compile(r"(?i)((?:secret[-._]?(?:access[-._]?)?key|session[-._]?token|password)[\"']?\s*[=:]\s*[\"']?)"
                r"[^\s\"',;}]{8,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)\b(s3a?|s3n)://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"), r"\1://<BUCKET>"),
    # Host names of AWS endpoints that carry a deployment's identifiers (a bucket, a cluster, a
    # database); the regional service endpoints themselves are left as they are.
    (re.compile(r"\b[a-z0-9][a-z0-9.-]*\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com"), "<BUCKET>.s3.amazonaws.com"),
    (re.compile(r"\b[a-z0-9.-]+\.(eks|rds|elb|cache)\.[a-z0-9-]*\.?amazonaws\.com"), "<AWS_HOST>"),
    (re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.elb\.amazonaws\.com"), "<AWS_HOST>"),
    (re.compile(r"\b\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com"), "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com"),
    # An account ID as API Gateway masks it in its messages (********1234): its last digits are still its.
    (re.compile(r"\*{8}\d{4}(?!\d)"), "<ACCOUNT_ID>"),
    (re.compile(r"\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}(\.[a-z0-9-]+)*\.(compute\.)?internal\b"), "<HOST>"),
    # Not a version number's tail (jdk-17.0.20.1): an address stands after a space, "/", "=", ...
    (re.compile(r"(?<![\w.-])(\d{1,3}\.){3}\d{1,3}(?![\d.])"), lambda m: _address(m.group(0))),
    # Last, an account ID: twelve digits standing alone, not inside a hexadecimal digest or commit.
    # A UUID (a request id, say) may end in twelve digits: it is matched whole first and kept (#52).
    (re.compile(rf"(?P<uuid>{UUID})|(?<![0-9A-Za-z])\d{{12}}(?![0-9A-Za-z])"),
     lambda m: m.group(0) if m.group("uuid") else "<ACCOUNT_ID>"),
]


class Sanitizer:
    def __init__(self, secrets=(), values=None):
        """secrets: strings to replace with <REDACTED> (tokens, keys); values: {string:
        placeholder} for the run's own identifiers (the catalog's URI, say)."""
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)
        self.values = sorted(((k, v) for k, v in (values or {}).items() if k), key=lambda kv: -len(kv[0]))

    def text(self, s):
        for secret in self.secrets:
            s = s.replace(secret, "<REDACTED>")
        for value, placeholder in self.values:
            s = s.replace(value, placeholder)
        for pattern, replacement in PATTERNS:
            s = pattern.sub(replacement, s)
        return s

    def data(self, value):
        """A JSON-like value with every string sanitized."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(v) for v in value]
        if isinstance(value, dict):
            return {self.text(str(k)): self.data(v) for k, v in value.items()}
        return value
