"""Offline tests of redaction.py: what it finds of the local values (install configurations and
.state/redaction-values), the value-independent findings, the public addresses and their exceptions,
and the check of files as pre-commit runs it. Every value here is synthetic; the addresses, accounts
and real-looking ARNs are put together at run time, so that the check of this file finds nothing."""
import contextlib
import fnmatch
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import redaction  # noqa: E402

# A made-up deployment, as a maintainer's local files would hold it.
ACCOUNT = "98765" + "4321098"
OTHER = "56789" + "0123456"
DOMAIN = "aws.owner.example"
ROLE = "deployer_EC2_role"
BUCKET = "colvennacatalog-ex9-warehouse-1a2b3c"
ENDPOINT = ".".join(("ex9db", "cexample123", "us-west-2", "rds", "amazonaws", "com"))
# Internal and public addresses (redaction.py's value-independent findings).
PRIVATE = ".".join(("10", "84", "8", "23"))
PUBLIC_IP = ".".join(("54", "23", "8", "91"))
OTHER_PUBLIC = ".".join(("3", "120", "7", "4"))
VERSION = ".".join(("4", "1", "4", "1"))
# A name of more parts than MAX_PARTS: an Aurora endpoint whose cluster has a long name.
LONG = ".".join(("colvenna-catalog-ex9-metadata", "cluster-cexample123", "us-west-2", "rds", "amazonaws", "com"))
# Accounts of one repeated digit, which are nobody's.
ZEROS, ONES = "0" * 12, "1" * 12


def values(*items):
    v = redaction.Values()
    for value in items:
        v.add_value(value, value)
    return v


class FindTest(unittest.TestCase):
    def test_an_account_is_twelve_digits_with_no_digit_next_to_them(self):
        v = values(ACCOUNT)
        for line in (ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/x", f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com",
                     f"账号 `{ACCOUNT}`、区域", f"sha {ACCOUNT}abc"):
            self.assertEqual([ACCOUNT], v.find(line), line)
        for line in ("1" + ACCOUNT, ACCOUNT + "0", ACCOUNT[:6] + " " + ACCOUNT[6:], "111122223333"):
            self.assertEqual([], v.find(line), line)

    def test_a_domain_is_found_in_every_name_under_it_whatever_the_case(self):
        v = values("owner.example")
        for line in ("https://owner.example", f"https://console.{DOMAIN}/x", f"`*.colvenna.{DOMAIN}`", "ops@owner.example",
                     "Console.Colvenna-Catalog.AWS.Owner.Example.", f"{DOMAIN}，入口"):
            self.assertEqual(["owner.example"], v.find(line), line)
        for line in ("gh --repo owner/colvenna-catalog", "owner.examples.com", "myowner.example", "owner-example",
                     "owner.example_x"):
            self.assertEqual([], v.find(line), line)

    def test_names_and_ids_as_words_or_parts_of_one(self):
        v = values(ROLE, "i-0123456789abcdef0", ENDPOINT, "exec-role")
        self.assertEqual([ROLE, "i-0123456789abcdef0"],
                         v.find(f"arn:aws:sts::123456789012:assumed-role/{ROLE}/i-0123456789abcdef0"))
        self.assertEqual([ENDPOINT], v.find(f"jdbc:postgresql://{ENDPOINT.upper()}:5432/x"))
        self.assertEqual(["exec-role"], v.find("role colvenna-exec-role-2"))
        for line in (ROLE + "_2", "i-0123456789abcdef01", "execrole", "x.cexample123.us-west-2"):
            self.assertEqual([], v.find(line), line)

    def test_a_value_must_be_one_word(self):
        for value in ("arn:aws:iam::1:role/x", "two words", "https://owner.example", "secret+name", ""):
            with self.assertRaises(ValueError, msg=value):
                redaction.Values().add_value(value, "x")


class LocalTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, path, text):
        path = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_the_configurations_settings_and_the_extra_values(self):
        self.write("deploy/config/ex9.env", f"COLVENNA_ENV=ex9\nCOLVENNA_ACCOUNT={ACCOUNT}\nCOLVENNA_REGION=us-west-2\n"
                                            f"COLVENNA_BUCKET={BUCKET}\nCOLVENNA_DB_ENDPOINT={ENDPOINT}\n"
                                            "COLVENNA_SUBNET_IDS=subnet-0123abcd, subnet-4567abcd\n"
                                            f"COLVENNA_RELEASE_REGISTRY={LONG}\nCOLVENNA_VPC_ID=\n# COLVENNA_VPC_ID=vpc-x\n")
        self.write("deploy/config/example.env", "COLVENNA_ACCOUNT=444455556666\n")
        self.write(".state/" + redaction.EXTRA_VALUES, f"# the executor\n{ROLE}\n\nowner.example\n")
        v = redaction.Values().load_local([self.root, os.path.join(self.root, "missing")])
        found = v.find(f"{ACCOUNT} {BUCKET} {ENDPOINT} subnet-4567abcd {ROLE}")
        self.assertEqual(["COLVENNA_ACCOUNT of the local configuration ex9.env",
                          "COLVENNA_BUCKET of the local configuration ex9.env",
                          "COLVENNA_DB_ENDPOINT of the local configuration ex9.env",
                          "COLVENNA_SUBNET_IDS of the local configuration ex9.env",
                          "line 2 of .state/redaction-values"], found)
        # A value of more parts than words are compared in by default is found whole, as a part of a longer one.
        self.assertGreater(redaction.parts(LONG), redaction.MAX_PARTS)
        self.assertEqual(["COLVENNA_RELEASE_REGISTRY of the local configuration ex9.env"], v.find(f"jdbc://{LONG}.x/db"))
        self.assertEqual(["line 4 of .state/redaction-values"], v.find("https://owner.example"))
        # The environment's name and Region are fixtures of the tests, and example.env is no install's.
        self.assertEqual([], v.find("ex9 us-west-2 444455556666 vpc-x"))

    def test_the_worktrees_of_this_clone(self):
        roots = redaction.worktrees()
        self.assertIn(redaction.REPO, [os.path.realpath(r) for r in roots] + roots)


class PatternTest(unittest.TestCase):
    def test_arns_and_registries_of_real_accounts(self):
        for line in (f"arn:aws:iam::{OTHER}:role/x", f"arn:aws:sts::{OTHER}:assumed-role/r/i-0",
                     f"arn:aws-cn:s3:::b vs arn:aws:secretsmanager:us-west-2:{OTHER}:secret:x",
                     f"{OTHER}.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-release/colvenna-catalog"):
            self.assertEqual(1, len(redaction.patterns(line)), line)
        for line in ("arn:aws:iam::123456789012:role/x", "arn:aws:iam::111122223333:root", "arn:aws:iam::aws:policy/x",
                     f"arn:aws:iam::{ZEROS}:role/x", f"{ONES}.dkr.ecr.us-west-2.amazonaws.com",
                     "602401143452.dkr.ecr.us-west-2.amazonaws.com/amazon-k8s-cni:v1", "arn:aws:s3:::bucket/key",
                     "arn:aws:iam::${account}:role/x", "${COLVENNA_ACCOUNT}.dkr.ecr.${COLVENNA_REGION}.amazonaws.com"):
            self.assertEqual([], redaction.patterns(line), line)

    def test_an_account_ids_form_wherever_it_stands_but_inside_a_uuid_or_a_digest(self):
        for line in (OTHER, f"account `{OTHER}`", f"COLVENNA_ACCOUNT={OTHER}", f"账号{OTHER}。", f"x_{OTHER}"):
            self.assertEqual(["a 12-digit number, an AWS account ID's form: use <ACCOUNT_ID> or a synthetic one"],
                             redaction.patterns(line), line)
        uuid = f"0f1e2d3c-4b5a-6978-8a9b-{OTHER}"
        for line in (uuid, uuid.upper(), f"sha {OTHER}abc", f"ab{OTHER}", "1" + OTHER, "123456789012", "777788889999",
                     ZEROS, "20261030T0512" + "00Z"):
            self.assertEqual([], redaction.patterns(line), line)

    def test_internal_addresses_but_not_networks(self):
        for line in (PRIVATE, f"pod {PRIVATE}:9030", ".".join(("172", "31", "23", "138")) + ".",
                     ".".join(("192", "168", "0", "7")), "node ip-" + "-".join(("10", "0", "1", "23")) + ".ec2." + "internal",
                     "ip-" + "-".join(("172", "31", "23", "138"))):
            self.assertTrue(redaction.patterns(line), line)
        # Next to the private ranges (public: the address check of PublicTest finds them).
        near = [".".join(parts) for parts in (("172", "15", "0", "1"), ("172", "32", "0", "1"), ("1", "10", "0", "1"))]
        for line in ["10.84.0.0/16", "{10.84.0.0/24}", "203.0.113.7", "0.0.0.0/0", "10.1.2.300", "version 10.1.2.3.4",
                     "ip-" + "-".join(("203", "0", "113", "7"))] + near:
            self.assertEqual([], redaction.patterns(line), line)
        self.assertEqual(near, [a for line in near for a in redaction.public_addresses(line)])

    def test_a_line_may_allow_the_patterns_never_the_values(self):
        line = f"{PRIVATE} and arn:aws:iam::{OTHER}:root and {OTHER}  # redaction: allow (synthetic)"
        self.assertEqual([], redaction.patterns(line))
        self.assertEqual([ACCOUNT], values(ACCOUNT).find(f"{ACCOUNT}  # redaction: allow"))


class PublicTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_public_addresses_with_or_without_a_prefix_length(self):
        for line in (PUBLIC_IP, f"`{PUBLIC_IP}/32`", f"publicAccessCidrs: [{PUBLIC_IP}/32]", f"ip={PUBLIC_IP}:443",
                     f"deny-address-{PUBLIC_IP}-no-inbound", f"只对{PUBLIC_IP}/32", f"执行机{PUBLIC_IP}的出口",
                     VERSION, f"tag/{VERSION}", f"jdk-{VERSION}+1"):
            self.assertEqual([PUBLIC_IP if PUBLIC_IP in line else VERSION], redaction.public_addresses(line), line)
        self.assertEqual([PUBLIC_IP, OTHER_PUBLIC], redaction.public_addresses(f"{PUBLIC_IP} and {OTHER_PUBLIC}"))

    def test_documentation_private_and_special_addresses_are_not_public(self):
        for line in ("192.0.2.7", "198.51.100.9/32", "203.0.113.7", PRIVATE, "10.84.0.0/16",
                     ".".join(("172", "31", "0", "1")),
                     # 0.0.0.0 is test data of the address check (bandit B104): nothing binds to it.
                     ".".join(("100", "64", "0", "1")), "127.0.0.1", "169.254.169.254", "0.0.0.0/0", "0.0.0.0", "255.255.255.255",  # nosec B104 - test data, nothing binds
                     "224.0.0.1", "10.1.2.300", f"v{VERSION}", f"{VERSION}.2", f"fe{VERSION}", f"{VERSION}x"):
            self.assertEqual([], redaction.public_addresses(line), line)

    def tsv(self, text):
        path = os.path.join(self.dir, "public.tsv")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_the_exceptions_are_a_file_glob_one_public_address_and_a_reason(self):
        allowed = redaction.load_public(self.tsv(f"# a comment\n\ndocs/*.md\t{VERSION}\ta component's version\n"))
        self.assertEqual([("docs/*.md", VERSION, "a component's version")], allowed)
        for line in (f"docs/a.md\t{VERSION}\n", f"docs/a.md\t{VERSION}\t\n", f"docs/a.md\t{PRIVATE}\tx\n",
                     f"docs/a.md\t{VERSION}/32\tx\n", f"docs/a.md\t{VERSION} {PUBLIC_IP}\tx\n",
                     f"docs/a.md\t{VERSION}\tx\ty\n"):
            with self.assertRaises(ValueError, msg=line):
                redaction.load_public(self.tsv(line))

    def test_only_the_file_and_address_registered_pass_and_the_marker_spares_none(self):
        path = os.path.join(self.dir, "a.md")
        with open(path, "w") as f:
            f.write(f"Component {VERSION}\nendpoint {PUBLIC_IP}/32  # redaction: allow\n")
        name = redaction.repo_path(path)
        self.assertEqual([f"{path}:1", f"{path}:2"], [f.split(": ", 1)[0] for f in redaction.check(path, values())])
        found = redaction.check(path, values(), allowed=[(name, VERSION, "a version")])
        self.assertEqual([f"{path}:2"], [f.split(": ", 1)[0] for f in found])
        self.assertIn("a public IPv4 address", found[0])
        self.assertNotIn(PUBLIC_IP, "".join(found))
        self.assertEqual(2, len(redaction.check(path, values(), allowed=[("other/*.md", VERSION, "x")])))

    def test_the_repositorys_exceptions_load_and_each_one_is_used(self):
        allowed = redaction.load_public()
        self.assertTrue(allowed)
        try:
            tracked = subprocess.run(["git", "-C", redaction.REPO, "ls-files"], check=True, capture_output=True,
                                     text=True).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        for glob, address, reason in allowed:
            files = [p for p in tracked if fnmatch.fnmatchcase(p, glob)]
            self.assertTrue(files, f"{glob}: no tracked file ({reason})")
            self.assertTrue(any(address in redaction.public_addresses(line) for p in files
                                for line in redaction.read(os.path.join(redaction.REPO, p)).splitlines()),
                            f"{glob} {address}: unused ({reason})")
        self.assertEqual([], redaction.check(redaction.PUBLIC, values(), allowed=allowed))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.makedirs(os.path.join(self.dir, "deploy", "config"))
        with open(os.path.join(self.dir, "deploy", "config", "ex9.env"), "w") as f:
            f.write(f"COLVENNA_ACCOUNT={ACCOUNT}\nCOLVENNA_DB_ENDPOINT={ENDPOINT}\n")
        self.empty = os.path.join(self.dir, "empty.tsv")
        open(self.empty, "w").close()

    def file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_main(self, *paths, public=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redaction.main(["redaction.py", *paths], roots=[self.dir], public=public or self.empty)
        return code, out.getvalue()

    def test_the_registered_public_addresses_pass(self):
        allowed = os.path.join(self.dir, "public.tsv")
        planted = self.file("notices.md", f"Component {VERSION}\n".encode())
        with open(allowed, "w") as f:
            f.write(f"{redaction.repo_path(planted)}\t{VERSION}\ta component's version\n")
        self.assertEqual(1, self.run_main(planted)[0])
        self.assertEqual((0, ""), self.run_main(planted, public=allowed))
        with open(allowed, "w") as f:
            f.write(f"{redaction.repo_path(planted)}\t{PRIVATE}\tnot public\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, self.run_main(planted, public=allowed)[0])
        self.assertIn("public.tsv:1", err.getvalue())

    def test_clean_files_pass(self):
        clean = self.file("clean.md", "账号 `<ACCOUNT_ID>`，端点 `<instance>.<id>.<region>.rds.amazonaws.com`\n".encode())
        binary = self.file("image.png", b"\x89PNG\0\0" + ACCOUNT.encode())
        self.assertEqual((0, ""), self.run_main(clean, binary, os.path.join(self.dir, "deleted")))

    def test_a_planted_value_fails_without_showing_it(self):
        clean = self.file("clean.md", b"nothing here\n")
        planted = self.file("planted.md", f"one\n账号 `{ACCOUNT}`\njdbc:postgresql://{ENDPOINT}:5432/x\n{PRIVATE}\n"
                                          f"EKS 公网端点只对 `{PUBLIC_IP}/32`\n账号 {OTHER}\n".encode())
        code, out = self.run_main(clean, planted)
        self.assertEqual(1, code)
        self.assertIn(f"{planted}:2: COLVENNA_ACCOUNT of the local configuration ex9.env\n", out)
        self.assertIn(f"{planted}:3: COLVENNA_DB_ENDPOINT of the local configuration ex9.env\n", out)
        self.assertIn(f"{planted}:4: an internal IPv4 address", out)
        self.assertIn(f"{planted}:5: a public IPv4 address", out)
        self.assertIn(f"{planted}:6: a 12-digit number", out)
        self.assertNotIn("clean.md", out)
        for value in (ACCOUNT, ENDPOINT, PRIVATE, PUBLIC_IP, OTHER):
            self.assertNotIn(value, out)

    def test_a_setting_it_cannot_look_for_is_named_not_shown(self):
        with open(os.path.join(self.dir, "deploy", "config", "ex9.env"), "a") as f:
            f.write(f"COLVENNA_BUCKET=s3://{BUCKET}\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual((2, ""), self.run_main(self.file("clean.md", b"x\n")))
        self.assertIn("COLVENNA_BUCKET of the local configuration ex9.env", err.getvalue())
        self.assertNotIn(BUCKET, err.getvalue())

    def test_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, redaction.main(["redaction.py"]))
            self.assertEqual(2, redaction.main(["redaction.py", "--hash"]))
        self.assertIn("redaction.py --all", err.getvalue())


if __name__ == "__main__":
    unittest.main()
