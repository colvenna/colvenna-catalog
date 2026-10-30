"""Offline tests that the tree reads the same once it is public: no file links to a repository of the
series other than this one (they are private until each goes public, so the link would not resolve),
and every relative link of the Markdown files resolves. The plans in docs/superpowers/plans/ are
records of work done, kept with the names of their time, and are not checked; nor are the rename
record's naming of this repository's former name and the v1.0.0-rc.1 manifest, released with the
repository's address of the time."""
import json
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
KEPT_AS_WRITTEN = "docs/superpowers/plans/"
# The records that name this repository by an earlier name or address: the rename record, and the
# v1.0.0-rc.1 manifest, which stays as released.
EARLIER_RECORD = "release/v1.0.0-rc.1/manifest.json"
RECORDS = ("docs/rename.md", EARLIER_RECORD)
# The series' organization; this repository is in it.
ORGANIZATION = "colvenna"
OWN = f"{ORGANIZATION}/colvenna-catalog"
# A GitHub repository's address: https://github.com/<owner>/<name> or git@github.com:<owner>/<name>.
GITHUB = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+)")
# A Markdown link's or an HTML image's target.
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|(?:src|srcset)=\"([^\"]+)\"")


def origin():
    """owner/name of the clone's origin on GitHub, or None: before the move to the organization, this
    repository's address of the time, whose issues the documents still link."""
    out = subprocess.run(["git", "-C", REPO, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = GITHUB.search(out.stdout) if out.returncode == 0 else None
    name = m[2].removesuffix(".git") if m else None
    return f"{m[1]}/{name}" if name == "colvenna-catalog" else None


def earlier_address():
    """owner/name of this repository before the move, as the v1.0.0-rc.1 manifest records it, or None."""
    try:
        with open(os.path.join(REPO, EARLIER_RECORD), encoding="utf-8") as f:
            m = GITHUB.search(json.load(f)["source"]["repository"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return f"{m[1]}/{m[2].removesuffix('.git')}" if m else None


def owners(*addresses):
    """The owners whose repositories are of the series: the organization, and the owners of the
    addresses given (the clone's origin; this repository's before the move, so that once it has moved,
    a link to its old address is found too)."""
    return {ORGANIZATION} | {a.split("/")[0] for a in addresses if a}


def tracked():
    return subprocess.run(["git", "-C", REPO, "ls-files"], check=True, capture_output=True,
                          text=True).stdout.split()


def text_of(path):
    try:
        with open(os.path.join(REPO, path), encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        return None


def other_repositories(text, clone_origin=None, earlier=None):
    """The repositories of the series, other than this one, that the text names: a GitHub address of
    an owner of the series (owners), or, for an owner but the organization, <owner>/<name> as text too
    (the organization's name alone is also the start of the catalog's own paths, /colvenna/admin/v1).
    This repository is the organization's address and the clone's origin."""
    own = {OWN} | ({clone_origin} if clone_origin else set())
    series = owners(clone_origin, earlier)
    text_owners = series - {ORGANIZATION}
    pattern = GITHUB.pattern
    if text_owners:
        pattern += r"|(?<![\w.-])(" + "|".join(map(re.escape, sorted(text_owners))) + r")/([\w.-]+)"
    found = []
    for m in re.finditer(pattern, text):
        owner, name = (m[1], m[2]) if m[1] else (m[3], m[4])
        name = re.sub(r"\.git$", "", name.rstrip("."))
        if owner in series and f"{owner}/{name}" not in own:
            found.append(f"{owner}/{name}")
    return found


def relative_links(text):
    """The relative targets of a Markdown file's links and images, outside code blocks and spans."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    text = re.sub(r"`[^`\n]*`", "", text)
    targets = []
    for m in LINK.finditer(text):
        target = m[1] or m[2]
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            continue
        targets.append(target.split("#", 1)[0].split("?", 1)[0])
    return [t for t in targets if t]


class Links(unittest.TestCase):
    def test_no_file_names_another_repository_of_the_series(self):
        found = []
        clone_origin, earlier = origin(), earlier_address()
        for path in tracked():
            if path.startswith(KEPT_AS_WRITTEN) or path in RECORDS + ("scripts/release/test_links.py",):
                continue
            text = text_of(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                found += [f"{path}:{number}: {name}" for name in other_repositories(line, clone_origin, earlier)]
        self.assertEqual(found, [])

    def test_every_relative_link_of_the_markdown_resolves(self):
        broken = []
        for path in tracked():
            if not path.endswith(".md") or path.startswith(KEPT_AS_WRITTEN):
                continue
            for target in relative_links(text_of(path) or ""):
                if not os.path.exists(os.path.normpath(os.path.join(REPO, os.path.dirname(path), target))):
                    broken.append(f"{path}: {target}")
        self.assertEqual(broken, [])


class Patterns(unittest.TestCase):
    def test_what_counts_as_another_repository(self):
        # The series' repositories here are made up, under the organization and an owner of the origin.
        self.assertEqual(other_repositories("see https://github.com/colvenna/colvenna-example/blob/main/x.md"),
                         ["colvenna/colvenna-example"])
        self.assertEqual(other_repositories("[index](https://github.com/colvenna/series-index)."),
                         ["colvenna/series-index"])
        self.assertEqual(other_repositories("git@github.com:colvenna/colvenna-sample.git"), ["colvenna/colvenna-sample"])
        for own in ("https://github.com/colvenna/colvenna-catalog/blob/main/install.sh",
                    "SOURCE = \"https://github.com/colvenna/colvenna-catalog\"", "GET /colvenna/admin/v1/audit",
                    "colvenna-catalog/source-commit", "Colvenna Lake's catalog", "colvenna/colvenna-example as text",
                    "https://github.com/someone/colvenna-fork", "https://github.com/apache/paimon"):
            self.assertEqual(other_repositories(own), [], own)

    def test_the_origins_owner_is_of_the_series_and_its_address_is_this_repository(self):
        before = "early-owner/colvenna-catalog"
        self.assertEqual(other_repositories("https://github.com/early-owner/colvenna-catalog/issues/4", before), [])
        self.assertEqual(other_repositories("https://github.com/early-owner/colvenna-catalog/issues/4"),
                         [], "an owner the origin does not name is not of the series")
        self.assertEqual(other_repositories("as early-owner/colvenna-example's generator", before),
                         ["early-owner/colvenna-example"])
        self.assertEqual(other_repositories("https://github.com/early-owner/another.git", before), ["early-owner/another"])
        self.assertEqual(owners(None), {"colvenna"})
        self.assertEqual(owners(before, None), {"colvenna", "early-owner"})

    def test_once_moved_the_old_address_is_another_repository(self):
        # The clone's origin is the organization's: the address the rc.1 manifest records is not this
        # repository's any more, and its owner's repositories are of the series.
        moved, earlier = "colvenna/colvenna-catalog", "early-owner/colvenna-catalog"
        self.assertEqual(other_repositories("https://github.com/early-owner/colvenna-catalog/issues/4", moved, earlier),
                         ["early-owner/colvenna-catalog"])
        self.assertEqual(other_repositories("as early-owner/colvenna-example's generator", moved, earlier),
                         ["early-owner/colvenna-example"])
        self.assertEqual(other_repositories("https://github.com/colvenna/colvenna-catalog/issues/4", moved, earlier), [])
        recorded = earlier_address()
        self.assertTrue(recorded and recorded.endswith("/colvenna-catalog"), recorded)

    def test_what_counts_as_a_relative_link(self):
        text = """[a](docs/a.md#x) [b](https://example.com) [c](#top) ![d](img/d.png "title")
<img alt="e" src="assets/e.png"> <source srcset="assets/f.png">
```bash
[g](not/a/link.md)
```
[h](mailto:x@example.com) [i](../up.md?plain=1) `[j](fe) in a code span`"""
        self.assertEqual(relative_links(text), ["docs/a.md", "img/d.png", "assets/e.png", "assets/f.png", "../up.md"])


if __name__ == "__main__":
    unittest.main()
