"""Offline tests of how ASH's false positives are suppressed (#72): no entry of .ash/.ash.yaml names a
line, which would stop matching when the code moves; the entries that suppress a rule for a whole file
hold for the whole file; and every mark on a line says why. docs/security/scans.md, section 3, has the
procedure."""
import json
import os
import re
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
CONFIG = os.path.join(REPO, ".ash", ".ash.yaml")
# detect-secrets' inline allowlist (which checkov's secret checks honour too), and the reason after it.
ALLOWLIST = re.compile(r"pragma: ?allowlist[ -](?:nextline[ -])?secret")
WITH_REASON = re.compile(r"pragma: ?allowlist[ -](?:nextline[ -])?secret - \S")
# detect-secrets honours a mark only right after a comment's start (its ALLOWLIST_REGEX: the comment token, then
# blanks, then the pragma): "# note; pragma: ..." is ignored, and the finding stays.
AT_COMMENT_START = re.compile(r"(?:#|//|/\*|<!--|--)[ \t]*pragma: ?allowlist[ -](?:nextline[ -])?secret")


def read(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


def suppressions():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)["global_settings"]["suppressions"]


def suppressed():
    """(rule, path) of every suppression."""
    return [(e["rule_id"], e["path"]) for e in suppressions()]


def instructions(dockerfile):
    """A Dockerfile's instructions, each with its continuation lines joined; comment and empty lines,
    which Docker skips inside an instruction too, left out."""
    joined, current = [], ""
    for line in (line.strip() for line in dockerfile.splitlines()):
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1].strip() + " "
        else:
            joined.append(current + line)
            current = ""
    return joined


def arrays_under(node, path=()):
    """The paths of every array schema under node (a parsed YAML document)."""
    if isinstance(node, dict):
        if node.get("type") == "array":
            yield path
        for key, value in node.items():
            yield from arrays_under(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from arrays_under(value, path + (i,))


class Suppressions(unittest.TestCase):
    def test_no_entry_names_a_line_and_each_gives_its_reason(self):
        for entry in suppressions():
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"rule_id", "path", "reason"})
                self.assertTrue(str(entry["reason"]).strip())

    def test_the_dockerfiles_one_add_is_pinned_by_checksum(self):
        # CKV_DOCKER_4 is suppressed for the whole Dockerfile: each ADD must fetch an https URL whose
        # content the build checks.
        self.assertIn(("CKV_DOCKER_4", "Dockerfile"), suppressed())
        adds = [i for i in instructions(read("Dockerfile")) if i.split()[0].upper() == "ADD"]
        self.assertTrue(adds)
        for add in adds:
            with self.subTest(add=add):
                self.assertRegex(add, r"(?:^| )--checksum=sha256:[0-9a-f]{64}(?: |$)")
                self.assertRegex(add, r" https://\S+ /\S+$")

    def test_the_admin_apis_arrays_under_paths_are_listings_it_answers(self):
        # CKV_OPENAPI_21 is suppressed for the whole description: it reads the paths block only, where
        # every array must be in a GET operation's response.
        self.assertIn(("CKV_OPENAPI_21", "docs/api/admin-v1.yaml"), suppressed())
        paths = yaml.safe_load(read("docs/api/admin-v1.yaml"))["paths"]
        arrays = list(arrays_under(paths))
        self.assertTrue(arrays)
        for where in arrays:
            with self.subTest(where="/".join(map(str, where))):
                self.assertEqual(where[1:3], ("get", "responses"))

    def test_the_one_minimatch_is_aws_cdk_libs_bundled_copy(self):
        # GHSA-g3hv-75wg-m249 (CVE-2026-118764, #85) is suppressed for the CDK app's lockfile and the packages
        # npm audit names after it: its premise is that the lockfile's one minimatch is the copy aws-cdk-lib
        # bundles, whose callers in aws-cdk-lib never set optimizationLevel. Another copy, or none, needs this
        # looked at again (and a version past 11.0.1 leaves the entries unused, to be removed).
        lock = json.loads(read("deploy/aws/package-lock.json"))
        copies = {k: v for k, v in lock["packages"].items() if k == "node_modules/minimatch" or k.endswith("/node_modules/minimatch")}
        self.assertEqual(list(copies), ["node_modules/aws-cdk-lib/node_modules/minimatch"])
        self.assertTrue(copies["node_modules/aws-cdk-lib/node_modules/minimatch"].get("inBundle"))
        rules = {(e["rule_id"], e["path"]) for e in suppressions()}
        for entry in (("GHSA-g3hv-75wg-m249", "node_modules/aws-cdk-lib/minimatch/package.json"),
                      ("GHSA-g3hv-75wg-m249-minimatch", "deploy/aws/package-lock.json")):
            self.assertIn(entry, rules)
        # Trivy's entry for it names the same advisory and the same file.
        trivy = yaml.safe_load(read(".trivyignore.yaml"))
        self.assertIn({"id": "CVE-2026-118764", "paths": ["deploy/aws/package-lock.json"]},
                      [{k: v for k, v in e.items() if k != "statement"} for e in trivy.get("vulnerabilities", [])])

    def test_every_mark_on_a_line_says_why(self):
        tracked = subprocess.run(["git", "-C", REPO, "ls-files"], check=True, capture_output=True,
                                 text=True).stdout.split()
        bare, ignored = [], []
        for path in tracked:
            try:
                text = read(path)
            except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if ALLOWLIST.search(line) and not WITH_REASON.search(line):
                    bare.append(f"{path}:{number}")
                found = ALLOWLIST.search(line)
                # A quoted or backticked pragma is a description of the syntax (scans.md, .ash.yaml), not a mark.
                quoted = found and line[max(found.start() - 1, 0)] in "\"'`"
                if found and not quoted and not AT_COMMENT_START.search(line):
                    ignored.append(f"{path}:{number}")
        self.assertEqual(bare, [])
        # A mark after other text in its comment is one detect-secrets does not see.
        self.assertEqual(ignored, [])

    def test_the_patterns(self):
        self.assertEqual(instructions("FROM x\n# c\nADD --checksum=sha256:ab \\  \n  # inside\n\n  https://e.example/f /f\n"
                                      "RUN y\n"),
                         ["FROM x", "ADD --checksum=sha256:ab https://e.example/f /f", "RUN y"])
        self.assertEqual(list(arrays_under({"a": {"type": "array", "items": {"type": "array"}}})),
                         [("a",), ("a", "items")])
        # Put together here, so that this file's own lines hold no mark.
        mark, nextline = "pragma: allowlist" + " secret", "pragma: allowlist nextline" + " secret"
        for marked in (f"x = 'v'  # {mark} - a made-up value", f"// {nextline} - the next line's fake"):
            self.assertTrue(WITH_REASON.search(marked), marked)
        for bare in (f"x = 'v'  # {mark}", f"x = 'v'  # {mark}-", f"<!-- {mark} -->"):
            self.assertTrue(ALLOWLIST.search(bare) and not WITH_REASON.search(bare), bare)
        for seen in (f"x = 'v'  # {mark} - why", f"x = 'v'  #{mark} - why", f"// {nextline} - why", f"<!-- {mark} - why -->"):
            self.assertTrue(AT_COMMENT_START.search(seen), seen)
        for unseen in (f"x = 'v'  # a note; {mark} - why", f"x = 'v'  # see {nextline} - why"):
            self.assertFalse(AT_COMMENT_START.search(unseen), unseen)


if __name__ == "__main__":
    unittest.main()
