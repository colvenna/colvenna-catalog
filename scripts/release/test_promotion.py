"""Offline tests of promotion.py: a release's names, the GO verdict it is promoted on, the chart re-versioned
and compared file by file, the release's manifest and its validation, the release notes, and the
registries promote.sh and publish-ghcr.sh accept (never AWS ECR Public)."""
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = Path(HERE).parents[1]
TOOL = os.path.join(HERE, "promotion.py")
HELM = REPO / ".tools/helm"
sys.path.insert(0, HERE)
import manifest  # noqa: E402
import promotion  # noqa: E402

B = "b" * 40
C = "c" * 40
RECORD = "d" * 40
IMAGE = "sha256:" + "1" * 64
# A release verdict's basis: the release check that judged the candidate.
BASIS = f"[the release check](https://github.com/colvenna/colvenna-catalog/blob/{'d' * 40}/docs/release/v1.0.0-rc.2.md)"
CANDIDATE_CHART = "sha256:" + "2" * 64
RELEASE_CHART = "sha256:" + "3" * 64
CANDIDATE, RELEASE = "v1.0.0-rc.2", "v1.0.0"
# The release's chart version and appVersion alike.
VERSION = "1.0.0"


def candidate_manifest(**changes):
    """A candidate for GO's manifest (the UAT cases, their required set and the performance terms)."""
    return manifest.build({
        "candidate": CANDIDATE, "built_from": B, "generated": "2026-10-30T06:00:00Z", "image_digest": IMAGE,
        "chart_digest": CANDIDATE_CHART, "objects": {p: "4" * 40 for p in manifest.PINNED_PATHS},
        "uat_cases": {"commit": "5" * 40, "blob": "6" * 40}, "required_cases": ["UAT-CAT-01", "UAT-SEC-01"],
        "performance": {"commit": "7" * 40, "blob": "8" * 40}, "regions": ["us-west-2", "us-east-2"],
        "forms": ["eks", "serverless"], **changes})


def params(**changes):
    return {"release": RELEASE, "generated": "2026-11-02T06:00:00Z", "tag_commit": C, "manifest_blob": "9" * 40,
            "acceptance": {"commit": RECORD, "blob": "a" * 40}, "chart_digest": RELEASE_CHART, "chart_files": 10,
            **changes}


def release_manifest(**changes):
    return promotion.build(candidate_manifest(), params(**changes))


def run(*args, stdin=None):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, input=stdin)


def package(directory, files, name="colvenna-catalog"):
    """A chart package (tar.gz) of these {path under the chart: text}, as Helm lays one out."""
    path = Path(directory) / f"{name}.tgz"
    with tarfile.open(path, "w:gz") as archive:
        for member, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{name}/{member}")
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    return path


CHART_YAML = (f"annotations:\n  colvenna-catalog/source-commit: {B}\napiVersion: v2\nappVersion: {CANDIDATE}\n"
              "description: A REST catalog server.\nname: colvenna-catalog\ntype: application\nversion: 1.0.0-rc.2\n")
CHART = {"Chart.yaml": CHART_YAML, "values.yaml": "replicas: 2\n", "templates/service.yaml": "kind: Service\n"}


class Names(unittest.TestCase):
    def test_a_release_is_its_candidate_s_version(self):
        self.assertEqual(promotion.names(CANDIDATE, RELEASE), [
            ("image", "colvenna-catalog-release/colvenna-catalog", CANDIDATE, RELEASE),
            ("chart", "colvenna-catalog-release/charts/colvenna-catalog", "1.0.0-rc.2", "1.0.0")])
        self.assertEqual(promotion.published(RELEASE), "1.0.0")

    def test_a_candidate_promotes_to_its_own_version_only(self):
        for candidate, release in (("v1.0.0-rc.2", "v1.0.1"), ("v1.0.0-rc.2", "v1.0.0-rc.3"), ("v1.0.0", "v1.0.0"),
                                   ("v1.0.0-rc.2", "1.0.0"), ("v1.0.0-rc.0", "v1.0.0"), ("v1.1.0-rc.1", "v1.0.0")):
            with self.subTest(candidate=candidate, release=release):
                with self.assertRaises(ValueError):
                    promotion.names(candidate, release)

    def test_only_a_release_is_published(self):
        for name in ("v1.0.0-rc.2", "1.0.0", "v1.0", "latest"):
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    promotion.published(name)


def notes_with(result, basis=BASIS):
    """The candidate's notes with the release verdict and its basis filled in."""
    template = (REPO / "release/notes-template.md").read_text()
    text = manifest.notes(candidate_manifest(), template)
    row = "| Release verdict | <GO / NO-GO> | <the release check report's link> |"
    assert row in text
    return text.replace(row, f"| Release verdict | {result} | {basis} |")


class Verdict(unittest.TestCase):
    def test_go_plain_bold_or_as_code(self):
        for result in ("GO", "**GO**", "`GO`"):
            with self.subTest(result):
                self.assertEqual(promotion.verdict(notes_with(result)), "GO")

    def test_anything_but_go_is_refused(self):
        for result in ("NO-GO", "**NO-GO**", "<GO / NO-GO>", "GO (pending)", "", "go"):
            with self.subTest(result):
                with self.assertRaises(ValueError) as caught:
                    promotion.verdict(notes_with(result))
                self.assertIn("not GO", str(caught.exception))

    def test_the_basis_links_the_release_check(self):
        for basis in ("<the release check report's link>", "", "the release check, by hand", "[the check]()"):
            with self.subTest(basis):
                with self.assertRaises(ValueError) as caught:
                    promotion.verdict(notes_with("GO", basis))
                self.assertIn("it links the release check", str(caught.exception))

    def test_the_template_s_draft_is_not_a_verdict(self):
        draft = manifest.notes(candidate_manifest(), (REPO / "release/notes-template.md").read_text())
        with self.assertRaises(ValueError):
            promotion.verdict(draft)

    def test_one_verdict_row(self):
        for text in ("# notes\n", notes_with("GO") + f"\n| Release verdict | GO | {BASIS} |\n"):
            with self.subTest(text[:10]):
                with self.assertRaises(ValueError) as caught:
                    promotion.verdict(text)
                self.assertIn("'Release verdict' rows", str(caught.exception))

    def test_the_command(self):
        path = Path(tempfile.mkdtemp()) / "notes.md"
        path.write_text(notes_with("GO"))
        self.assertEqual(run("verdict", str(path)).stdout, "GO\n")
        path.write_text(notes_with("NO-GO"))
        out = run("verdict", str(path))
        self.assertEqual(out.returncode, 1)
        self.assertIn("NO-GO", out.stderr)


class Rechart(unittest.TestCase):
    """The candidate's package re-versioned: only Chart.yaml's version and appVersion change."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.candidate = package(self.dir, CHART)

    def release_package(self, changes=None, extra=None, missing=None, version=VERSION, app=VERSION):
        files = dict(CHART)
        files["Chart.yaml"] = promotion.with_versions(CHART_YAML, version, app)
        files.update(changes or {})
        files.update(extra or {})
        for path in missing or ():
            files.pop(path)
        out = Path(tempfile.mkdtemp())
        return package(out, files)

    def differences(self, release, version=VERSION, app=VERSION):
        return promotion.chart_differences(str(self.candidate), str(release), version, app)

    def test_rechart_changes_only_the_two_fields(self):
        out = Path(tempfile.mkdtemp())
        directory = Path(promotion.rechart(str(self.candidate), str(out), VERSION, VERSION))
        self.assertEqual(directory, out / "colvenna-catalog")
        chart = (directory / "Chart.yaml").read_text().splitlines()
        self.assertIn("version: 1.0.0", chart)
        self.assertIn("appVersion: 1.0.0", chart)
        self.assertIn(f"  colvenna-catalog/source-commit: {B}", chart)
        self.assertEqual([line for line in chart if not line.startswith(("version:", "appVersion:"))],
                         [line for line in CHART_YAML.splitlines() if not line.startswith(("version:", "appVersion:"))])
        self.assertEqual((directory / "templates/service.yaml").read_text(), CHART["templates/service.yaml"])
        self.assertEqual(oct((directory / "values.yaml").stat().st_mode & 0o777), "0o644")

    def test_the_re_versioned_package_is_the_candidate_s_but_for_the_two_fields(self):
        report, problems = self.differences(self.release_package())
        self.assertEqual(problems, [])
        self.assertIn("changed   colvenna-catalog/Chart.yaml: version 1.0.0-rc.2 -> 1.0.0, appVersion v1.0.0-rc.2 ->"
                      " 1.0.0; every other line the same", report)
        self.assertEqual(sum(line.startswith("same      ") for line in report), 2)
        self.assertEqual(report[-1], "3 files compared: only Chart.yaml's version and appVersion differ")

    def test_any_other_difference_is_refused(self):
        cases = {
            "a template": {"changes": {"templates/service.yaml": "kind: Service\n# changed\n"}},
            "the values": {"changes": {"values.yaml": "replicas: 3\n"}},
            "another field of Chart.yaml": {"changes": {"Chart.yaml": promotion.with_versions(
                CHART_YAML.replace("A REST", "Not a REST"), VERSION, VERSION)}},
            "the annotation": {"changes": {"Chart.yaml": promotion.with_versions(
                CHART_YAML.replace(B, C), VERSION, VERSION)}},
            "a file more": {"extra": {"templates/extra.yaml": "{}\n"}},
            "a file less": {"missing": ["values.yaml"]},
            "the candidate's version kept": {"version": "1.0.0-rc.2", "app": CANDIDATE},
        }
        for name, case in cases.items():
            with self.subTest(name):
                report, problems = self.differences(self.release_package(**case))
                self.assertNotEqual(problems, [], report)

    def test_another_version_than_the_release_s(self):
        _, problems = self.differences(self.release_package(), version="1.0.1", app="1.0.1")
        self.assertIn("colvenna-catalog/Chart.yaml: version is '1.0.0', not '1.0.1'", problems)

    def test_a_mode_that_differs(self):
        out = Path(tempfile.mkdtemp()) / "colvenna-catalog.tgz"
        with tarfile.open(self.candidate) as source, tarfile.open(out, "w:gz") as archive:
            for member in source.getmembers():
                data = source.extractfile(member).read()
                if member.name.endswith("Chart.yaml"):
                    data = promotion.with_versions(data.decode(), VERSION, VERSION).encode()
                    member.size = len(data)
                if member.name.endswith("values.yaml"):
                    member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
        _, problems = self.differences(out)
        self.assertEqual(problems, ["colvenna-catalog/values.yaml: mode 644 in the candidate's chart, 755 in the release's"])

    def test_a_package_with_a_link_or_a_path_outside_is_refused(self):
        for name, kind in (("colvenna-catalog/link", tarfile.SYMTYPE), ("../outside", tarfile.REGTYPE)):
            with self.subTest(name):
                bad = Path(tempfile.mkdtemp()) / "bad.tgz"
                with tarfile.open(bad, "w:gz") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    archive.addfile(info, io.BytesIO(b""))
                with self.assertRaises(ValueError):
                    promotion.rechart(str(bad), tempfile.mkdtemp(), VERSION, VERSION)

    def test_chart_yaml_needs_each_field_once(self):
        for text in ("name: c\nversion: 1\n", "version: 1\nappVersion: a\nversion: 2\n"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    promotion.with_versions(text, VERSION, VERSION)

    def test_with_helm_s_own_packaging(self):
        """The pinned Helm packages the re-versioned directory with nothing else changed."""
        candidate = REPO / "deploy/helm/colvenna-catalog"
        work = Path(tempfile.mkdtemp())
        script = (f'source "{REPO}/deploy/lib/release.sh"; release_chart_source "{candidate}" "{work}/source/colvenna-catalog"'
                  f' 1.0.0-rc.2 {CANDIDATE} {B}')
        subprocess.run(["bash", "-c", script], check=True)
        subprocess.run([str(HELM), "package", str(work / "source/colvenna-catalog"), "-d", str(work / "candidate")],
                       check=True, capture_output=True)
        before = work / "candidate/colvenna-catalog-1.0.0-rc.2.tgz"
        directory = promotion.rechart(str(before), str(work / "release"), VERSION, VERSION)
        subprocess.run([str(HELM), "package", directory, "-d", str(work / "packaged")], check=True, capture_output=True)
        after = work / "packaged/colvenna-catalog-1.0.0.tgz"
        out = run("chart-diff", str(before), str(after), VERSION, VERSION)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("only Chart.yaml's version and appVersion differ", out.stdout)
        shown = subprocess.run([str(HELM), "show", "chart", str(after)], capture_output=True, text=True).stdout
        self.assertIn(f"colvenna-catalog/source-commit: {B}", shown)
        self.assertIn("version: 1.0.0\n", shown)
        out = run("chart-diff", str(before), str(before), VERSION, VERSION)
        self.assertEqual(out.returncode, 1)
        self.assertIn("DIFFERS", out.stdout)


class Build(unittest.TestCase):
    def test_the_candidate_s_manifest_under_the_release_s_names(self):
        candidate, m = candidate_manifest(), release_manifest()
        self.assertEqual(promotion.problems(m), [])
        self.assertEqual(promotion.problems(m, RELEASE), [])
        self.assertEqual(m["release"], RELEASE)
        self.assertEqual(m["image"], {**candidate["image"], "tag": RELEASE})
        self.assertEqual(m["image"]["digest"], IMAGE)
        self.assertEqual(m["chart"], {**candidate["chart"], "version": "1.0.0", "appVersion": "1.0.0",
                                      "digest": RELEASE_CHART})
        self.assertEqual(m["source"], {**candidate["source"], "tag": f"{RELEASE} points at {CANDIDATE}'s tag commit"
                                       f" with release/{RELEASE}/ added, nothing else"})
        for key in promotion.KEPT:
            self.assertEqual(m[key], candidate[key])
        self.assertEqual(m["promotion"], {
            "candidate": CANDIDATE, "tag_commit": C,
            "manifest": {"path": f"release/{CANDIDATE}/manifest.json", "blob": "9" * 40},
            "acceptance": {"path": f"release/{CANDIDATE}/notes.md", "commit": RECORD, "blob": "a" * 40, "verdict": "GO"},
            "chart": {"version": "1.0.0-rc.2", "digest": CANDIDATE_CHART, "changed": ["version", "appVersion"], "files": 10}})

    def test_only_a_candidate_for_go_is_promoted(self):
        for changes in ({"uat_cases": None, "required_cases": []}, {"performance": None}):
            with self.subTest(changes):
                with self.assertRaises(ValueError) as caught:
                    promotion.build(candidate_manifest(**changes), params())
                self.assertIn("the candidate's manifest", str(caught.exception))

    def test_the_chart_is_a_new_package(self):
        with self.assertRaises(ValueError) as caught:
            release_manifest(chart_digest=CANDIDATE_CHART)
        self.assertIn("chart.digest: the candidate's chart's", str(caught.exception))

    def test_another_release_than_the_candidate_s(self):
        with self.assertRaises(ValueError):
            release_manifest(release="v1.0.1")

    def test_assemble_reads_what_promote_sh_gathered(self):
        work = Path(tempfile.mkdtemp())
        (work / "candidate.json").write_text(json.dumps(candidate_manifest()))
        (work / "params.json").write_text(json.dumps(params()))
        out = run("assemble", str(work))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), release_manifest())


class Validate(unittest.TestCase):
    def broken(self, change):
        m = release_manifest()
        change(m)
        return promotion.problems(m)

    def test_each_field(self):
        cases = {
            "the image's digest": (lambda m: m["image"].update(digest="latest"), "image.digest"),
            "the image's tag": (lambda m: m["image"].update(tag=CANDIDATE), "image.tag"),
            "the chart's version": (lambda m: m["chart"].update(version="1.0.0-rc.2"), "chart.version"),
            "the chart's appVersion": (lambda m: m["chart"].update(appVersion=RELEASE), "chart.appVersion"),
            "the source's tag": (lambda m: m["source"].update(tag="x"), "source.tag"),
            "a candidate of another release": (lambda m: m["promotion"].update(candidate="v1.0.1-rc.1"),
                                               "promotion.candidate"),
            "the verdict": (lambda m: m["promotion"]["acceptance"].update(verdict="NO-GO"), "promotion.acceptance.verdict"),
            "the acceptance's commit": (lambda m: m["promotion"]["acceptance"].update(commit="main"),
                                        "promotion.acceptance.commit"),
            "what changed in the chart": (lambda m: m["promotion"]["chart"].update(changed=["version"]),
                                          "promotion.chart.changed"),
            "a field more": (lambda m: m.update(extra=1), "unknown field extra"),
            "a field less": (lambda m: m.pop("performance"), "performance: missing"),
            "an account ID": (lambda m: m["image"].update(repository="444455556666.dkr.ecr.us-west-2.amazonaws.com/x"),  # redaction: allow - a test that a manifest refuses an account ID
                              "AWS account id"),
            "the documents of GO": (lambda m: m.update(uat_cases={"path": manifest.CASES_PATH, "status": "absent"},
                                                       required_cases=[]), "uat_cases"),
        }
        for name, (change, needle) in cases.items():
            with self.subTest(name):
                found = self.broken(change)
                self.assertTrue(any(needle in p for p in found), found)

    def test_the_release_s_own(self):
        self.assertTrue(promotion.problems(release_manifest(), "v1.0.1"))

    def test_the_command(self):
        path = Path(tempfile.mkdtemp()) / "manifest.json"
        path.write_text(json.dumps(release_manifest()))
        self.assertEqual(run("validate", str(path), "--release", RELEASE).returncode, 0)
        out = run("validate", str(path), "--release", "v2.0.0")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not v2.0.0's", out.stderr)
        # A candidate's manifest is not a release's.
        path.write_text(json.dumps(candidate_manifest()))
        self.assertEqual(run("validate", str(path)).returncode, 1)

    def test_check_tree_holds_the_release_to_the_commit_and_where_it_was_promoted_from(self):
        """B; the candidate's tag commit C, B + release/<candidate>/; the record on C, its notes judging it GO;
        and the release's commit R, C + release/<release>/."""
        repo = Path(tempfile.mkdtemp())

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

        def commit(files):
            for path, text in files.items():
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(text)
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "c")
            return git("rev-parse", "HEAD")
        git("init", "-q")
        b = commit({path: path for path in ("install.sh", "uninstall.sh", "deploy/lib/a.sh", "deploy/aws/app.ts",
                                            "deploy/helm/colvenna-catalog/Chart.yaml", manifest.CASES_PATH,
                                            manifest.PERFORMANCE_PATH)})
        objects = {p: git("rev-parse", f"{b}:{p}") for p in manifest.PINNED_PATHS}
        documents = {key: {"commit": b, "blob": git("rev-parse", f"{b}:{path}")} for key, path in manifest.DOCUMENTS}
        candidate = candidate_manifest(built_from=b, objects=objects, uat_cases=documents["uat_cases"],
                                       performance=documents["performance"])
        draft = manifest.notes(candidate, (REPO / "release/notes-template.md").read_text())
        c = commit({f"release/{CANDIDATE}/manifest.json": json.dumps(candidate), f"release/{CANDIDATE}/notes.md": draft})
        record = commit({f"release/{CANDIDATE}/notes.md": notes_with("GO")})
        git("checkout", "-q", "--detach", c)
        m = promotion.build(candidate, params(
            tag_commit=c, manifest_blob=git("rev-parse", f"{c}:release/{CANDIDATE}/manifest.json"),
            acceptance={"commit": record, "blob": git("rev-parse", f"{record}:release/{CANDIDATE}/notes.md")}))
        commit({f"release/{RELEASE}/manifest.json": json.dumps(m)})
        path = repo.parent / f"{repo.name}-manifest.json"
        path.write_text(json.dumps(m))
        self.assertEqual(run("check-tree", str(path), str(repo), "HEAD").returncode, 0)
        # Before promote.sh commits the release, HEAD is C.
        self.assertEqual(run("check-tree", str(path), str(repo), c).returncode, 0)
        # A file moved into release/<release>/ is a change outside it, where it was taken from.
        git("mv", "install.sh", f"release/{RELEASE}/install.sh")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "moved")
        out = run("check-tree", str(path), str(repo), "HEAD")
        self.assertEqual(out.returncode, 1)
        self.assertIn(f"differs from {c}, {CANDIDATE}'s tag commit, outside release/{RELEASE}/: install.sh", out.stderr)
        git("checkout", "-q", "--detach", "HEAD^")
        for name, change, needle in (
                ("a pinned tree", lambda m: m["chart"].update(tree="e" * 40), "chart: deploy/helm/colvenna-catalog is"),
                ("a tag commit this clone lacks", lambda m: m["promotion"].update(tag_commit="e" * 40),
                 f"this clone lacks {'e' * 40}"),
                ("a record that judged it otherwise", lambda m: m["promotion"]["acceptance"].update(
                    blob=git("rev-parse", f"{c}:release/{CANDIDATE}/notes.md"), commit=c),
                 "the release verdict is <GO / NO-GO>, not GO")):
            with self.subTest(name):
                changed = copy.deepcopy(m)
                change(changed)
                path.write_text(json.dumps(changed))
                out = run("check-tree", str(path), str(repo), "HEAD")
                self.assertEqual(out.returncode, 1)
                self.assertIn(needle, out.stderr)


class Notes(unittest.TestCase):
    def setUp(self):
        self.text = notes_with("GO").replace("<Limitations this candidate found, beyond the user manual's; each with"
                                             " its issue.>", "None found.")
        self.notes = promotion.notes(release_manifest(), self.text)

    def test_the_release_s_name_and_identity(self):
        self.assertIn('title: "Colvenna Catalog v1.0.0 release notes"', self.notes)
        self.assertIn("date: 2026-11-02", self.notes)
        self.assertIn("status: final", self.notes)
        self.assertNotIn("status: draft", self.notes)
        self.assertIn("\n# Colvenna Catalog v1.0.0 release notes\n", self.notes)
        self.assertNotIn("# Colvenna Catalog v1.0.0-rc.2 release notes", self.notes)
        self.assertIn("## 1. Release\n", self.notes)
        self.assertNotIn("## 1. Candidate", self.notes)
        section = self.notes.split("## 1. Release", 1)[1].split("## 2.", 1)[0]
        self.assertIn(f"| Release | `{RELEASE}` (annotated tag) |", section)
        self.assertIn(f"https://github.com/colvenna/colvenna-catalog/blob/{RECORD}/release/{CANDIDATE}/notes.md", section)
        self.assertIn(f"`colvenna-catalog-release/colvenna-catalog@{IMAGE}`", section)
        self.assertIn(f"colvenna-catalog-release/charts/colvenna-catalog@{RELEASE_CHART}`, version `1.0.0`", section)
        self.assertIn(f"`{CANDIDATE}`'s chart (`{CANDIDATE_CHART}`, version `1.0.0-rc.2`)", section)
        self.assertIn("`ghcr.io/colvenna/colvenna-catalog:1.0.0`", section)
        self.assertIn("its 10 files compared one by one ([`chart-comparison.txt`](https://github.com/colvenna/"
                      f"colvenna-catalog/blob/{RELEASE}/release/{RELEASE}/chart-comparison.txt))", section)
        self.assertIn("version `1.0.0`, appVersion `1.0.0`", section)

    def test_every_other_section_is_the_candidate_s_as_accepted(self):
        after = self.text[self.text.index("## 2. Deliverables"):]
        self.assertTrue(self.notes.endswith(after))
        self.assertIn(f"| Release verdict | GO | {BASIS} |", self.notes)
        self.assertIn("None found.", self.notes)

    def test_the_template_s_comment_says_where_the_notes_come_from(self):
        self.assertNotIn("The release notes' template.", self.notes)
        self.assertIn(f"scripts/release/promote.sh wrote these notes when it promoted {CANDIDATE} to {RELEASE}", self.notes)
        self.assertEqual(self.notes.count("<!--"), 1)

    def test_notes_that_are_not_the_candidate_s(self):
        for text in (self.text.replace("## 1. Candidate", "## 1. Something"),
                     self.text.replace(f"# Colvenna Catalog {CANDIDATE} release notes", "# Other notes")):
            with self.subTest(text[:5]):
                with self.assertRaises(ValueError):
                    promotion.notes(release_manifest(), text)

    def test_the_command_and_markdownlint_s_rules(self):
        path = Path(tempfile.mkdtemp()) / "manifest.json"
        path.write_text(json.dumps(release_manifest()))
        out = run("notes", str(path), stdin=self.text)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, self.notes)
        self.assertNotIn("\n\n\n", self.notes)
        self.assertTrue(self.notes.endswith("\n") and not self.notes.endswith("\n\n"))


class Registries(unittest.TestCase):
    def test_the_release_registry(self):
        self.assertEqual(promotion.registry_kind("444455556666.dkr.ecr.us-west-2.amazonaws.com"), "ecr")
        self.assertEqual(promotion.registry_kind("127.0.0.1:5000"), "loopback")
        self.assertEqual(promotion.registry_kind("localhost:5000"), "loopback")
        for registry in ("public.ecr.aws", "ghcr.io", "444455556666.dkr.ecr.cn-north-1.amazonaws.com", "192.0.2.10:5000",
                         "127.0.0.1"):
            with self.subTest(registry):
                with self.assertRaises(ValueError):
                    promotion.registry_kind(registry)

    def test_ghcr_or_a_loopback_registry(self):
        for what, target, kind in (("image", promotion.GHCR_IMAGE, "ghcr"), ("chart", promotion.GHCR_CHART, "ghcr"),
                                   ("image", "127.0.0.1:5000/colvenna/x", "loopback"),
                                   ("chart", "localhost:5000/colvenna/charts/x", "loopback")):
            with self.subTest(target):
                self.assertEqual(promotion.target_kind(what, target), kind)
                self.assertEqual(run("target", what, target).stdout, kind + "\n")

    def test_never_aws_ecr_public(self):
        for target in ("public.ecr.aws/colvenna/colvenna-catalog", "PUBLIC.ECR.AWS/colvenna/charts/colvenna-catalog",
                       "oci://public.ecr.aws/colvenna/colvenna-catalog", "https://public.ecr.aws/x/y",
                       "public.ecr.aws:443/colvenna/colvenna-catalog", "api.ecr-public.us-east-1.amazonaws.com/x/y",
                       "registry.public.ecr.aws/x"):
            for what in ("image", "chart"):
                with self.subTest(target=target, what=what):
                    with self.assertRaises(ValueError) as caught:
                        promotion.target_kind(what, target)
                    self.assertIn(promotion.NEVER_ECR_PUBLIC, str(caught.exception))
                    out = run("target", what, target)
                    self.assertEqual(out.returncode, 1)
                    self.assertIn("AWS ECR Public is never a target", out.stderr)
        with self.assertRaises(ValueError) as caught:
            promotion.registry_kind("public.ecr.aws")
        self.assertIn(promotion.NEVER_ECR_PUBLIC, str(caught.exception))

    def test_nothing_else(self):
        cases = [("image", t) for t in (
            "docker.io/colvenna/colvenna-catalog", "444455556666.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog",
            "quay.io/colvenna/colvenna-catalog", "ghcr.io/colvenna/colvenna-catalog:1.0.0",
            "ghcr.io/colvenna/colvenna-catalog@" + IMAGE, "ghcr.io/another-owner/colvenna-catalog", promotion.GHCR_CHART,
            "oci://ghcr.io/colvenna/colvenna-catalog", "ghcr.io.example.com/colvenna/colvenna-catalog")]
        cases.append(("chart", promotion.GHCR_IMAGE))
        for what, target in cases:
            with self.subTest(what=what, target=target):
                with self.assertRaises(ValueError) as caught:
                    promotion.target_kind(what, target)
                self.assertIn(f"the {what} is published to GHCR only", str(caught.exception))


class Usage(unittest.TestCase):
    def test_an_unknown_command_prints_the_usage(self):
        out = run("publish")
        self.assertEqual(out.returncode, 1)
        self.assertIn("promotion.py names <candidate> <release>", out.stderr)

    def test_the_candidate_manifest_is_never_changed(self):
        m = candidate_manifest()
        kept = copy.deepcopy(m)
        promotion.build(m, params())
        self.assertEqual(m, kept)


if __name__ == "__main__":
    unittest.main()
