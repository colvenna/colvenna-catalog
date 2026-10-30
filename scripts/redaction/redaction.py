#!/usr/bin/env python3
"""The repository carries none of the maintainers' deployment values (#99): no AWS account ID, ARN of
a real account, real domain, internal or public address, nor the role or instance that ran an install.
Documents, tests and commit messages use placeholders and synthetic values; the values themselves live
only in uncommitted local files, which this check reads to know what to look for, so that the
repository never has to name them, not even as a hash. It is ported from Colvenna Lake's check (its
deploy/scripts/redaction/, #248 and #300), with this repository's local files.

What it looks for:

- The values of every local install configuration (deploy/config/<env>.env, but example.env: the
  settings in CONFIGURATION) and of .state/redaction-values (one value per line, for those no
  configuration holds, such as the maintainers' domains and the executor's role and instance), in
  every worktree of this clone. The environment's name is not one of them: the test environments'
  names are the tests' fixtures too.
- Whatever the values:
  - a 12-digit number, an AWS account ID's form, that is neither synthetic nor AWS's own, with no
    letter or digit next to it and not inside a UUID; an ARN or ECR registry of such an account;
  - an internal IPv4 address (10/8, 172.16/12, 192.168/16, or an ip-10-... host name) that is not a
    CIDR.
  A line with "redaction: allow" is spared these, never the values.
- A public IPv4 address, with or without a prefix length: a globally reachable unicast address
  (Python's ipaddress: is_global, not is_multicast), so neither private, shared, loopback,
  link-local nor of the documentation ranges 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24
  (RFC 5737), which documents and tests use instead. A four-part number with a letter or a dot next
  to it (v1.2.3.4) is not read as an address. Only public-addresses.tsv allows one, by file and
  address, each with its reason; "redaction: allow" does not.

A value is matched as a word: an account as 12 digits with no digit next to them, anything else as a
run of letters, digits, "_", "." and "-", or a part of one between "." or "-" (so a domain is found in
every name under it). Case does not matter.

Usage: redaction.py FILE...   (pre-commit passes the staged files, or the commit message)
       redaction.py --all     (every file git tracks)
"""
import fnmatch
import ipaddress
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PUBLIC = os.path.join(HERE, "public-addresses.tsv")
# The settings of an install configuration (deploy/config/example.env lists them all) that are the
# deployment's own; a list's items are separated by commas. Not the environment's name or Region.
# (An admin secret's ARN is not one: its account is found by its form, and its name is the install's,
# made from the environment's name.)
CONFIGURATION = ("COLVENNA_ACCOUNT", "COLVENNA_BUCKET", "COLVENNA_DB_ENDPOINT", "COLVENNA_RELEASE_REGISTRY",
                 "COLVENNA_VPC_ID", "COLVENNA_SUBNET_IDS", "COLVENNA_API_ENDPOINT_IDS")
EXTRA_VALUES = "redaction-values"
ALLOW = "redaction: allow"

ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
# An account ID's form as a finding of its own: no letter or digit next to it (not a part of a digest).
ACCOUNT_FORM = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
WORD = re.compile(r"[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?")
PARTS = re.compile(r"([.-])")
# A word's parts that are compared together: as many as the longest value to look for has, and this
# many at least (a domain is found inside a longer name: colvenna.aws.example.com has four).
MAX_PARTS = 8

# Accounts that are not anybody's: AWS's documentation examples and the tests' synthetic ones.
SYNTHETIC = {"123456789012", "210987654321", "111122223333", "444455556666", "777788889999"}
# Accounts that are AWS's own, which public images and policies name.
AWS_OWNED = {"602401143452"}  # Amazon EKS's image registry (add-ons such as the VPC CNI)
ARN = re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12})(?!\d)")
ECR = re.compile(r"(?<!\d)(\d{12})\.dkr\.ecr(?:-fips)?\.[a-z0-9-]+\.amazonaws\.com")
IPV4 = re.compile(r"(?<![\d.])((?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3})(?![\d.]*\d)(?!/\d)")
INTERNAL_HOST = re.compile(r"\bip-(?:10(?:-\d{1,3}){3}|172-(?:1[6-9]|2\d|3[01])(?:-\d{1,3}){2}|192-168(?:-\d{1,3}){2})\b")
# Any IPv4 literal, a CIDR's too; not one with an ASCII letter, digit, "_" or "." next to it (a
# version, v1.2.3.4). Chinese text next to it does not hide it.
ANY_IPV4 = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:\.\d{1,3}){3})(?:/\d{1,2})?(?![A-Za-z0-9_]|\.\d)")


def kind(value):
    """account or text: what a value is compared with. None when it can never be found as a word."""
    value = value.lower()
    if ACCOUNT.fullmatch(value):
        return "account"
    if WORD.fullmatch(value):
        return "text"
    return None


def parts(value):
    """How many parts between "." or "-" a word has."""
    return (len(PARTS.split(value)) + 1) // 2


def words(line, most=MAX_PARTS):
    """The words of a line and their parts between "." or "-", in all of their runs of up to most parts."""
    for word in WORD.findall(line):
        parts = PARTS.split(word)  # the parts at even indexes, their separators between
        n = (len(parts) + 1) // 2
        for i in range(n):
            for j in range(i, min(n, i + most)):
                yield "".join(parts[2 * i:2 * j + 1])


def synthetic(account):
    return account in SYNTHETIC or account in AWS_OWNED or len(set(account)) == 1


class Values:
    """The local values to look for, each with what it is: a finding names what, never the value."""

    def __init__(self):
        self.values = {"account": {}, "text": {}}  # kind -> value -> what
        self.most = MAX_PARTS  # the parts of a line's words compared together

    def add_value(self, value, what):
        value = value.strip().lower()
        kind_ = kind(value)
        if kind_ is None:
            raise ValueError("%s: a value to look for must be one word of letters, digits, '_', '.' and '-'" % what)
        self.values[kind_].setdefault(value, what)
        self.most = max(self.most, parts(value))

    def add_setting(self, text, what):
        """A setting's values: each item of a comma-separated list."""
        for item in (i.strip() for i in text.split(",")):
            if item:
                self.add_value(item, what)

    def load_local(self, roots=None):
        """The values of the local files in every worktree of this clone (or of the roots given)."""
        for root in worktrees() if roots is None else roots:
            config = os.path.join(root, "deploy", "config")
            if os.path.isdir(config):
                for name in sorted(os.listdir(config)):
                    if not name.endswith(".env") or name == "example.env":
                        continue
                    for line in read(os.path.join(config, name)).splitlines():
                        key, sep, value = line.partition("=")
                        if sep and key.strip() in CONFIGURATION and value.strip():
                            self.add_setting(value, "%s of the local configuration %s" % (key.strip(), name))
            extra = os.path.join(root, ".state", EXTRA_VALUES)
            if os.path.isfile(extra):
                for number, line in enumerate(read(extra).splitlines(), 1):
                    if line.strip() and not line.lstrip().startswith("#"):
                        self.add_value(line, "line %d of .state/%s" % (number, EXTRA_VALUES))
        return self

    def find(self, line):
        """What values a line holds: the description of each, once."""
        line = line.lower()
        found = []
        for what in [self.values["account"].get(a) for a in ACCOUNT.findall(line)] + \
                [self.values["text"].get(w) for w in words(line, self.most)]:
            if what and what not in found:
                found.append(what)
        return found


def worktrees():
    """This clone's worktrees (the main one first), or just this one outside git."""
    try:
        out = subprocess.run(["git", "-C", REPO, "worktree", "list", "--porcelain"],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return [REPO]
    roots = [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]
    return roots or [REPO]


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def patterns(line):
    """The value-independent findings of a line, but its public addresses."""
    if ALLOW in line:
        return []
    found = []
    reported = set()  # where the accounts of ARNs and registries start: each is reported once
    for rx, what in ((ARN, "an ARN of a real account: use a synthetic one (123456789012) or <ARN>"),
                     (ECR, "an ECR registry of a real account: use a synthetic one or <ACCOUNT_ID>")):
        for m in rx.finditer(line):
            if not synthetic(m.group(1)):
                found.append(what)
                reported.add(m.start(1))
    scrubbed = UUID.sub(lambda u: "u" * len(u.group(0)), line)
    for m in ACCOUNT_FORM.finditer(scrubbed):
        if not synthetic(m.group(0)) and m.start() not in reported:
            found.append("a 12-digit number, an AWS account ID's form: use <ACCOUNT_ID> or a synthetic one")
    for m in IPV4.finditer(line):
        if octets(m.group(1)):
            found.append("an internal IPv4 address: use a placeholder")
    if INTERNAL_HOST.search(line):
        found.append("an internal host name (ip-10-...): use a placeholder")
    return found


def octets(value):
    """Whether a dotted quad's four numbers are octets (10.1.2.300 is no address)."""
    return all(int(octet) <= 255 for octet in value.split("."))


def public_addresses(line):
    """The public IPv4 addresses of a line (without their prefix lengths), in order."""
    found = []
    for m in ANY_IPV4.finditer(line):
        value = m.group(1)
        if octets(value):
            address = ipaddress.ip_address(value)
            if address.is_global and not address.is_multicast:
                found.append(value)
    return found


def load_public(path=PUBLIC):
    """The public addresses allowed: (path glob, address, reason) of each line of public-addresses.tsv."""
    allowed = []
    for number, line in enumerate(read(path).splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3 or not all(field.strip() for field in fields) or \
                public_addresses(fields[1]) != [fields[1]]:
            raise ValueError("%s:%d: not '<path glob> TAB <public IPv4 address> TAB <reason>'" % (path, number))
        allowed.append(tuple(fields))
    return allowed


def repo_path(path):
    """A path as the exceptions name it: relative to the repository, with "/"."""
    full = os.path.abspath(path)
    if full == REPO or full.startswith(REPO + os.sep):
        path = os.path.relpath(full, REPO)
    return path.replace(os.sep, "/")


def check(path, values, allowed=()):
    """path:line: finding, for each finding of a text file (a binary one has none). allowed: the
    public addresses allowed, as load_public gives them."""
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return []
    name = repo_path(path)
    # public-addresses.tsv itself holds every address it allows.
    allowed_here = {address for glob, address, _ in allowed
                    if name == repo_path(PUBLIC) or fnmatch.fnmatchcase(name, glob)}
    out = []
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        for what in values.find(line):
            out.append("%s:%d: %s" % (path, number, what))
        for what in patterns(line):
            out.append("%s:%d: %s" % (path, number, what))
        if any(address not in allowed_here for address in public_addresses(line)):
            out.append("%s:%d: a public IPv4 address: use a placeholder or a documentation address"
                       " (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24)" % (path, number))
    return out


def tracked():
    """Every file git tracks, relative to the repository."""
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], check=True, capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]


def main(argv, roots=None, public=PUBLIC):
    """roots: where the local files are (by default every worktree of this clone)."""
    if not argv[1:] or (argv[1].startswith("-") and argv[1:] != ["--all"]):
        print(__doc__.strip().rsplit("\n\n", 1)[-1], file=sys.stderr)
        return 2
    try:
        values = Values().load_local(roots)
        allowed = load_public(public)
    except ValueError as e:  # a setting or a line it cannot look for: say which, not its value
        print(e, file=sys.stderr)
        return 2
    paths = argv[1:]
    if paths == ["--all"]:
        os.chdir(REPO)
        paths = tracked()
    found = [f for path in paths if os.path.isfile(path) for f in check(path, values, allowed)]
    if found:
        print("Deployment values in the repository (#99; the values are not shown):")
        print("\n".join(found))
        print("Write placeholders (<ACCOUNT_ID>, <ARN>, example.com) or synthetic values (123456789012, "
              "192.0.2.10) instead; see CONTRIBUTING.md. A public address that is not one (a version) goes in "
              "scripts/redaction/public-addresses.tsv, with its reason.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
