"""Offline tests of promote.sh and publish-ghcr.sh, end to end: each runs in a clone of its own against stub
OCI registries (registry_stub.py) on 127.0.0.1, with the pinned crane and Helm (scripts/install-tools.sh),
and with stub aws and gh that answer nothing, so that nothing reaches AWS, GitHub or a real registry.

promote.sh: the image is tagged, never copied or rebuilt; the chart is the candidate's with only its
version and appVersion changed; release/<release>/ alone is committed on the candidate's tag commit and
tagged; a candidate not judged GO, a tree that is not the tag's, and a chart or image that is not the
candidate's are refused before anything is written. publish-ghcr.sh: the release's image and chart are
copied by digest, verified as anyone pulls them (private until made public, exit 4), a tag holding another
digest is never moved, and an AWS ECR Public target is refused before anything is read."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TOOLS = REPO / ".tools"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests/deploy"))
import manifest  # noqa: E402
import promotion  # noqa: E402
import registry_stub  # noqa: E402
from stubs import Stubs  # noqa: E402

CANDIDATE, RELEASE = "v1.0.0-rc.2", "v1.0.0"
IMAGE_REPOSITORY, CHART_REPOSITORY = manifest.IMAGE_REPOSITORY, manifest.CHART_REPOSITORY
REVISION = "org.opencontainers.image.revision"
PUBLISHER = ("publisher", "not-a-real-token")  # pragma: allowlist secret - the stub GHCR's made-up credentials
# What a clone of its own needs of this one to run the two scripts.
COPIED = ("scripts/release/promote.sh", "scripts/release/publish-ghcr.sh", "scripts/release/promotion.py",
          "scripts/release/manifest.py", "scripts/release/registries.sh", "deploy/lib/release.sh", ".gitignore")
CHART_DIR = "deploy/helm/colvenna-catalog"
# The template's verdict row, and the release check a verdict's basis links.
VERDICT_ROW = "| Release verdict | <GO / NO-GO> | <the release check report's link> |"
BASIS = "[the release check](https://github.com/colvenna/colvenna-catalog/blob/" + "d" * 40 + "/docs/release/check.md)"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def scratch():
    path = Path(tempfile.mkdtemp())
    return path


def helm(*args, env=None):
    home = scratch()
    environment = {**os.environ, "HELM_REGISTRY_CONFIG": str(home / "registry.json"), "HELM_CACHE_HOME": str(home),
                   "DOCKER_CONFIG": str(home), **(env or {})}
    out = subprocess.run([str(TOOLS / "helm"), *args], capture_output=True, text=True, env=environment)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout + out.stderr


def chart_package(chart_dir, version, app, commit, change=None):
    """The chart directory as a release packages it (release_chart_source), with that version,
    appVersion and source commit; change(<chart dir>) alters it before it is packaged."""
    work = scratch()
    source = work / "chart/colvenna-catalog"
    subprocess.run(["bash", "-c", f'source "{REPO}/deploy/lib/release.sh"; release_chart_source "{chart_dir}" '
                    f'"{source}" {version} {app} {commit}'], check=True)
    if change:
        change(source)
    helm("package", str(source), "-d", str(work / "packaged"))
    return next((work / "packaged").glob("*.tgz"))


def push_chart(registry, package):
    """The package pushed into the stub registry as Helm pushes it; its digest."""
    out = helm("push", str(package), f"oci://{registry.host}/{CHART_REPOSITORY.rsplit('/', 1)[0]}", "--plain-http")
    return re.search(r"^Digest: (sha256:[0-9a-f]{64})$", out, re.MULTILINE)[1]


def held_package(registry, repository, tag):
    """The chart package a stub registry holds under the tag, as a file."""
    held = registry.manifests[repository][registry.tag(repository, tag)][1]
    path = scratch() / "held.tgz"
    path.write_bytes(registry.blobs[json.loads(held)["layers"][0]["digest"]])
    return path


def changed_template(chart):
    with open(chart / "templates/service.yaml", "a") as f:
        f.write("# changed\n")


def clone():
    """A clone of its own, with the scripts and a scripts/install-tools.sh that installs nothing."""
    repo = scratch()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    for path in COPIED:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / path, repo / path)
    tools = repo / "scripts/install-tools.sh"
    tools.write_text("#!/usr/bin/env bash\n# The tests put the pinned tools on PATH.\nexit 0\n")
    tools.chmod(0o755)
    return repo


def environment(stubs, **settings):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("COLVENNA_", "RELEASE_", "GHCR_", "PUBLISH_"))}
    return {**clean, **stubs.env, "PATH": f"{stubs.bin}:{TOOLS}:{clean['PATH']}", **settings}


def no_aws_no_gh():
    """aws and gh that answer nothing (status 97) and log every call."""
    return Stubs({"aws": {}, "gh": {}}, commands=("aws", "gh"))


class World:
    """A clone with B (the chart directory, the installer, the UAT cases and the performance terms), the
    candidate's tag commit C (release/<candidate>/ added to B, the tag annotated) and on top of it R,
    whose release/<candidate>/notes.md records the verdict; HEAD at C, detached. The release registry is
    a stub with immutable tags, holding the candidate's image (labelled image_revision, B by default) and
    chart (B's chart directory as a release packages it, changed by chart_change)."""

    def __init__(self, verdict="GO", image_revision=None, chart_change=None):
        self.repo = repo = clone()
        shutil.copytree(REPO / CHART_DIR, repo / CHART_DIR)
        for path in ("install.sh", "uninstall.sh", "deploy/aws/src/app.ts", manifest.CASES_PATH,
                     manifest.PERFORMANCE_PATH):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(f"{path}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "B")
        self.built_from = b = git(repo, "rev-parse", "HEAD")
        self.registry = registry_stub.Registry(immutable=True)
        self.image = self.registry.seed_image(IMAGE_REPOSITORY, CANDIDATE, {REVISION: image_revision or b})
        self.candidate_package = chart_package(repo / CHART_DIR, "1.0.0-rc.2", CANDIDATE, b, chart_change)
        self.chart = push_chart(self.registry, self.candidate_package)
        documents = {key: {"commit": git(repo, "log", "-1", "--format=%H", b, "--", path),
                           "blob": git(repo, "rev-parse", f"{b}:{path}")} for key, path in manifest.DOCUMENTS}
        self.manifest = manifest.build({
            "candidate": CANDIDATE, "built_from": b, "generated": "2026-10-30T06:00:00Z", "image_digest": self.image,
            "chart_digest": self.chart, "objects": {p: git(repo, "rev-parse", f"{b}:{p}") for p in manifest.PINNED_PATHS},
            "uat_cases": documents["uat_cases"], "required_cases": ["UAT-CAT-01", "UAT-SEC-01"],
            "performance": documents["performance"], "regions": ["us-west-2", "us-east-2"], "forms": ["eks"]})
        draft = manifest.notes(self.manifest, (REPO / "release/notes-template.md").read_text())
        (repo / f"release/{CANDIDATE}").mkdir(parents=True)
        (repo / f"release/{CANDIDATE}/manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")
        (repo / f"release/{CANDIDATE}/notes.md").write_text(draft)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"release: {CANDIDATE}")
        self.tag_commit = git(repo, "rev-parse", "HEAD")
        git(repo, "tag", "-a", CANDIDATE, "-m", CANDIDATE)
        assert VERDICT_ROW in draft
        (repo / f"release/{CANDIDATE}/notes.md").write_text(
            draft.replace(VERDICT_ROW, f"| Release verdict | {verdict} | {BASIS} |"))
        git(repo, "commit", "-q", "-am", "the verdict")
        self.record = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "--detach", self.tag_commit)
        self.registry.requests.clear()
        self.evidence = scratch()

    def promote(self, *args, **settings):
        self.stubs = no_aws_no_gh()
        settings = {"COLVENNA_RELEASE_REGISTRY": self.registry.host, "RELEASE_RECORD": self.record,
                    "RELEASE_EVIDENCE": str(self.evidence), **settings}
        return subprocess.run([str(self.repo / "scripts/release/promote.sh"), *(args or (CANDIDATE, RELEASE))],
                              capture_output=True, text=True, env=environment(self.stubs, **settings))

    def unchanged(self, test):
        """Nothing written: no request but reads to the registry, no tag, branch or release directory."""
        test.assertEqual(self.registry.writes(), [])
        test.assertEqual(git(self.repo, "tag", "--list", RELEASE), "")
        test.assertEqual(git(self.repo, "branch", "--list", f"release-{RELEASE}"), "")
        test.assertFalse((self.repo / f"release/{RELEASE}").exists())
        test.assertEqual(self.stubs.calls(), [])


class Promote(unittest.TestCase):
    """A candidate judged GO, promoted: then published, as M3 will."""

    @classmethod
    def setUpClass(cls):
        cls.world = World()
        cls.out = cls.world.promote()
        cls.stubs = cls.world.stubs
        cls.release = json.loads(git(cls.world.repo, "show", f"{RELEASE}:release/{RELEASE}/manifest.json"))

    @classmethod
    def tearDownClass(cls):
        cls.world.registry.close()

    def test_it_succeeds_and_reads_nothing_from_aws_or_github(self):
        self.assertEqual(self.out.returncode, 0, self.out.stdout + self.out.stderr)
        self.assertEqual(self.stubs.calls(), [])

    def test_the_tag_adds_only_release_v1_0_0_to_the_candidate_s_tag_commit(self):
        repo, tagged = self.world.repo, git(self.world.repo, "rev-parse", f"{RELEASE}^{{commit}}")
        self.assertEqual(git(repo, "cat-file", "-t", f"refs/tags/{RELEASE}"), "tag")
        self.assertEqual(git(repo, "rev-parse", f"{tagged}^"), self.world.tag_commit)
        self.assertEqual(git(repo, "diff", "--name-only", self.world.tag_commit, tagged).splitlines(),
                         [f"release/{RELEASE}/chart-comparison.txt", f"release/{RELEASE}/manifest.json",
                          f"release/{RELEASE}/notes.md"])
        self.assertEqual(git(repo, "rev-parse", f"release-{RELEASE}"), tagged)
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_the_manifest_keeps_the_image_and_names_the_new_chart(self):
        m = self.release
        self.assertEqual(promotion.problems(m, RELEASE), [])
        self.assertEqual(m["image"]["digest"], self.world.image)
        self.assertEqual(m["chart"]["digest"], self.world.registry.tag(CHART_REPOSITORY, "1.0.0"))
        self.assertNotEqual(m["chart"]["digest"], self.world.chart)
        self.assertEqual(m["promotion"]["candidate"], CANDIDATE)
        self.assertEqual(m["promotion"]["tag_commit"], self.world.tag_commit)
        self.assertEqual(m["promotion"]["acceptance"]["commit"], self.world.record)
        self.assertEqual(m["promotion"]["acceptance"]["blob"],
                         git(self.world.repo, "rev-parse", f"{self.world.record}:release/{CANDIDATE}/notes.md"))
        self.assertEqual(m["promotion"]["chart"]["digest"], self.world.chart)
        self.assertEqual(m["source"]["built_from"], self.world.built_from)
        for key in promotion.KEPT:
            self.assertEqual(m[key], self.world.manifest[key])

    def test_the_image_is_tagged_never_copied_or_rebuilt(self):
        registry = self.world.registry
        self.assertEqual(registry.tag(IMAGE_REPOSITORY, RELEASE), self.world.image)
        self.assertEqual(registry.tag(IMAGE_REPOSITORY, CANDIDATE), self.world.image)
        written = [r for r in registry.writes() if f"/v2/{IMAGE_REPOSITORY}/" in r[1]]
        self.assertEqual([r[:2] for r in written], [("PUT", f"/v2/{IMAGE_REPOSITORY}/manifests/{RELEASE}")])

    def test_the_chart_is_the_candidate_s_with_only_version_and_app_version_changed(self):
        held = held_package(self.world.registry, CHART_REPOSITORY, "1.0.0")
        report, problems = promotion.chart_differences(str(self.world.candidate_package), str(held), "1.0.0", "1.0.0")
        self.assertEqual(problems, [], report)
        shown = subprocess.run([str(TOOLS / "helm"), "show", "chart", str(held)], capture_output=True, text=True).stdout
        self.assertIn("version: 1.0.0\n", shown)
        self.assertIn("appVersion: 1.0.0\n", shown)
        self.assertIn(f"colvenna-catalog/source-commit: {self.world.built_from}", shown)
        comparison = git(self.world.repo, "show", f"{RELEASE}:release/{RELEASE}/chart-comparison.txt")
        self.assertIn(f"{CHART_REPOSITORY}@{self.release['chart']['digest']} (version 1.0.0), pulled back", comparison)
        self.assertIn(f"{CHART_REPOSITORY}@{self.world.chart} (version 1.0.0-rc.2)", comparison)
        self.assertIn("changed   colvenna-catalog/Chart.yaml: version 1.0.0-rc.2 -> 1.0.0, appVersion v1.0.0-rc.2 ->"
                      " 1.0.0", comparison)
        self.assertIn(f"{self.release['promotion']['chart']['files']} files compared: only Chart.yaml's version and"
                      " appVersion differ", comparison)
        self.assertEqual((self.world.evidence / "chart-comparison.txt").read_text().rstrip("\n"), comparison)
        self.assertIn("only Chart.yaml's version and appVersion differ",
                      (self.world.evidence / "chart-packaged.txt").read_text())

    def test_the_notes_are_the_candidate_s_under_the_release_s_name(self):
        notes = git(self.world.repo, "show", f"{RELEASE}:release/{RELEASE}/notes.md")
        self.assertIn(f"# Colvenna Catalog {RELEASE} release notes", notes)
        self.assertIn("| Release verdict | GO |", notes)
        self.assertIn(f"blob/{self.world.record}/release/{CANDIDATE}/notes.md", notes)
        self.assertEqual(notes, promotion.notes(self.release, git(
            self.world.repo, "show", f"{self.world.record}:release/{CANDIDATE}/notes.md") + "\n").rstrip("\n"))

    def test_the_release_s_tag_passes_install_sh_release_s_checks(self):
        """install.sh --release v1.0.0 from a checkout of the tag (deploy/lib/release.sh): the checkout is the
        release's, promoted from the candidate as its manifest says, and the chart in the release registry is
        B's chart directory as a release of version 1.0.0 packages it."""
        repo = self.world.repo
        self.assertEqual(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", f"{RELEASE}^{{commit}}"))
        script = (f'set -euo pipefail; source "{REPO}/deploy/lib/preflight.sh"; REPO="{repo}"; '
                  f'source "{repo}/deploy/lib/release.sh"; release_checkout {RELEASE}; echo "$SOURCE_SHA"; '
                  f'release_chart_source "{repo / CHART_DIR}" "$1/colvenna-catalog" "$(release_value chart.version)" '
                  '"$(release_value chart.appVersion)" "$SOURCE_SHA"; release_chart_differences "$2" "$1/colvenna-catalog"')
        held = held_package(self.world.registry, CHART_REPOSITORY, "1.0.0")
        out = subprocess.run(["bash", "-c", script, "-", str(scratch()), str(held)], capture_output=True, text=True,
                             env={**environment(no_aws_no_gh()), "EVIDENCE": str(scratch())})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout, f"{self.world.built_from}\n")

    def test_a_rehearsal_s_commit_and_tag_say_so(self):
        """The stub is a loopback registry: a rehearsal, never to be pushed."""
        message = git(self.world.repo, "for-each-ref", "--format=%(contents)", f"refs/tags/{RELEASE}")
        self.assertIn(f"a rehearsal against the loopback registry {self.world.registry.host}, not a release: never"
                      " push it", message)
        self.assertIn("rehearsal", git(self.world.repo, "log", "-1", "--format=%s", RELEASE))
        self.assertIn(f"git tag -d {RELEASE}", self.out.stdout)
        self.assertNotIn("next: git push", self.out.stdout)

    def test_a_release_is_promoted_once(self):
        git(self.world.repo, "checkout", "-q", "--detach", self.world.tag_commit)
        try:
            out = self.world.promote()
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn(f"The tag {RELEASE} exists already", out.stderr)
        finally:
            git(self.world.repo, "checkout", "-q", f"release-{RELEASE}")

    def test_then_published_to_a_stub_ghcr_and_verified_as_anyone_pulls_it(self):
        ghcr = registry_stub.Registry(credentials=PUBLISHER, public=True)
        self.addCleanup(ghcr.close)
        out = publish(self.world.repo, self.world.registry, ghcr)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(ghcr.tag("colvenna/colvenna-catalog", "1.0.0"), self.world.image)
        self.assertEqual(ghcr.tag("colvenna/charts/colvenna-catalog", "1.0.0"), self.release["chart"]["digest"])


class PromoteRefuses(unittest.TestCase):
    def world(self, **options):
        world = World(**options)
        self.addCleanup(world.registry.close)
        return world

    def assertRefused(self, out, status, *needles):
        self.assertEqual(out.returncode, status, out.stdout + out.stderr)
        for needle in needles:
            self.assertIn(needle, out.stderr)

    def test_a_candidate_not_judged_go(self):
        world = self.world(verdict="NO-GO")
        self.assertRefused(world.promote(), 2, "the release verdict is NO-GO, not GO", f"{CANDIDATE} is not judged GO")
        world.unchanged(self)
        self.assertEqual(world.registry.requests, [])

    def test_the_tag_s_draft_notes_record_no_verdict(self):
        world = self.world()
        self.assertRefused(world.promote(RELEASE_RECORD=world.tag_commit), 2, "<GO / NO-GO>, not GO")
        world.unchanged(self)

    def test_a_record_that_is_not_after_the_candidate_s_tag(self):
        world = self.world()
        self.assertRefused(world.promote(RELEASE_RECORD=world.built_from), 2, "does not have")
        world.unchanged(self)

    def test_a_record_whose_manifest_is_not_the_tag_s(self):
        world = self.world()
        git(world.repo, "checkout", "-q", world.record)
        edited = dict(world.manifest, generated="2026-10-31T00:00:00Z")
        (world.repo / f"release/{CANDIDATE}/manifest.json").write_text(json.dumps(edited))
        git(world.repo, "commit", "-q", "-am", "changed")
        record = git(world.repo, "rev-parse", "HEAD")
        git(world.repo, "checkout", "-q", "--detach", world.tag_commit)
        self.assertRefused(world.promote(RELEASE_RECORD=record), 2, "is not the one")
        world.unchanged(self)

    def test_head_that_is_not_the_candidate_s_tag_commit(self):
        world = self.world()
        git(world.repo, "checkout", "-q", "--detach", world.record)
        self.assertRefused(world.promote(), 2, f"not {CANDIDATE}'s tag commit", f"git checkout {CANDIDATE}")
        world.unchanged(self)

    def test_a_dirty_tree(self):
        world = self.world()
        (world.repo / "install.sh").write_text("edited\n")
        self.assertRefused(world.promote(), 2, "Commit first")
        world.unchanged(self)

    def test_names_that_are_not_a_candidate_and_its_release(self):
        world = self.world()
        for args in ((CANDIDATE,), (CANDIDATE, "v1.0.1"), (CANDIDATE, "v1.0.0-rc.3"), (RELEASE, RELEASE),
                     (CANDIDATE, RELEASE, "extra")):
            with self.subTest(args):
                self.assertEqual(world.promote(*args).returncode, 2)
                world.unchanged(self)
        self.assertEqual(world.registry.requests, [])

    def test_aws_ecr_public_as_the_release_registry(self):
        world = self.world()
        self.assertRefused(world.promote(COLVENNA_RELEASE_REGISTRY="public.ecr.aws"), 2, "AWS ECR Public is never a target")
        world.unchanged(self)

    def test_a_candidate_s_chart_that_is_not_its_commit_s(self):
        world = self.world(chart_change=changed_template)
        self.assertRefused(world.promote(), 3, f"is not {world.built_from}'s chart directory", "service.yaml")
        world.unchanged(self)

    def test_a_candidate_s_image_built_from_another_commit(self):
        world = self.world(image_revision="e" * 40)
        self.assertRefused(world.promote(), 3, f"its label {REVISION} is {'e' * 40}, not {world.built_from}")
        world.unchanged(self)

    def test_an_image_tag_that_holds_another_digest(self):
        world = self.world()
        other = world.registry.seed_image(IMAGE_REPOSITORY, RELEASE, {REVISION: world.built_from}, layer=b"other")
        world.registry.requests.clear()
        self.assertRefused(world.promote(), 3, f"is already {other}", "tags never move")
        world.unchanged(self)

    def test_a_chart_version_that_holds_other_files(self):
        world = self.world()
        push_chart(world.registry, chart_package(world.repo / CHART_DIR, "1.0.0", "1.0.0", world.built_from,
                                                 changed_template))
        tampered = world.registry.tag(CHART_REPOSITORY, "1.0.0")
        world.registry.requests.clear()
        self.assertRefused(world.promote(), 3, f"holds chart 1.0.0 ({tampered})", "tags never move")
        self.assertEqual(world.registry.tag(CHART_REPOSITORY, "1.0.0"), tampered)
        world.unchanged(self)

    def test_a_registry_that_cannot_be_read_is_a_failure_not_a_mismatch(self):
        world = self.world()
        world.registry.denied = True
        out = world.promote()
        self.assertRefused(out, 1, "Cannot read", "DENIED")
        self.assertNotIn("has no", out.stderr)
        world.unchanged(self)

    def test_a_run_that_stopped_runs_again_and_keeps_what_it_pushed(self):
        world = self.world()
        subprocess.run([str(TOOLS / "crane"), "tag", f"{world.registry.host}/{IMAGE_REPOSITORY}@{world.image}", RELEASE],
                       check=True, capture_output=True, env={**os.environ, "DOCKER_CONFIG": str(scratch())})
        work = scratch()
        directory = promotion.rechart(str(world.candidate_package), str(work), "1.0.0", "1.0.0")
        helm("package", directory, "-d", str(work / "packaged"))
        earlier = push_chart(world.registry, work / "packaged/colvenna-catalog-1.0.0.tgz")
        world.registry.requests.clear()
        out = world.promote()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(world.registry.writes(), [])
        m = json.loads(git(world.repo, "show", f"{RELEASE}:release/{RELEASE}/manifest.json"))
        self.assertEqual(m["chart"]["digest"], earlier)
        self.assertEqual(m["image"]["digest"], world.image)


def publish(repo, source, ghcr, *args, **settings):
    """publish-ghcr.sh in the clone, from the stub release registry to the stub GHCR, with its credentials."""
    stubs = settings.pop("stubs", None) or no_aws_no_gh()
    settings = {"COLVENNA_RELEASE_REGISTRY": source.host, "PUBLISH_IMAGE": f"{ghcr.host}/colvenna/colvenna-catalog",
                "PUBLISH_CHART": f"{ghcr.host}/colvenna/charts/colvenna-catalog", "GHCR_USERNAME": PUBLISHER[0],
                "GHCR_TOKEN": PUBLISHER[1], "RELEASE_EVIDENCE": str(scratch()), **settings}
    out = subprocess.run([str(repo / "scripts/release/publish-ghcr.sh"), *(args or (RELEASE,))], capture_output=True,
                         text=True, env=environment(stubs, **settings))
    out.stubs = stubs
    return out


class Release:
    """A clone with the tag v1.0.0 (release/v1.0.0/manifest.json) and a stub release registry holding its
    image (labelled revision) and chart (annotated annotation), as promote.sh leaves them."""

    def __init__(self, revision="b" * 40, annotation="b" * 40):
        self.repo = clone()
        self.registry = registry_stub.Registry(immutable=True)
        self.image = self.registry.seed_image(IMAGE_REPOSITORY, RELEASE, {REVISION: revision})
        self.chart = push_chart(self.registry, chart_package(REPO / CHART_DIR, "1.0.0", "1.0.0", annotation))
        candidate = manifest.build({
            "candidate": CANDIDATE, "built_from": "b" * 40, "generated": "2026-10-30T06:00:00Z", "image_digest": self.image,
            "chart_digest": "sha256:" + "2" * 64, "objects": {p: "4" * 40 for p in manifest.PINNED_PATHS},
            "uat_cases": {"commit": "5" * 40, "blob": "6" * 40}, "required_cases": ["UAT-CAT-01"],
            "performance": {"commit": "7" * 40, "blob": "8" * 40}, "regions": ["us-west-2", "us-east-2"]})
        self.manifest = promotion.build(candidate, {
            "release": RELEASE, "generated": "2026-11-02T06:00:00Z", "tag_commit": "c" * 40, "manifest_blob": "9" * 40,
            "acceptance": {"commit": "d" * 40, "blob": "a" * 40}, "chart_digest": self.chart, "chart_files": 10})
        (self.repo / f"release/{RELEASE}").mkdir(parents=True)
        (self.repo / f"release/{RELEASE}/manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", RELEASE)
        git(self.repo, "tag", "-a", RELEASE, "-m", RELEASE)
        self.registry.requests.clear()


class Publish(unittest.TestCase):
    def setUp(self):
        self.release = Release()
        self.addCleanup(self.release.registry.close)

    def ghcr(self, public=True):
        ghcr = registry_stub.Registry(credentials=PUBLISHER, public=public)
        self.addCleanup(ghcr.close)
        return ghcr

    def publish(self, ghcr, *args, **settings):
        return publish(self.release.repo, self.release.registry, ghcr, *args, **settings)

    def assertPublished(self, ghcr):
        self.assertEqual(ghcr.tag("colvenna/colvenna-catalog", "1.0.0"), self.release.image)
        self.assertEqual(ghcr.tag("colvenna/charts/colvenna-catalog", "1.0.0"), self.release.chart)

    def anonymous_reads(self, ghcr):
        return [r[1] for r in ghcr.requests if not r[2] and r[0] in ("GET", "HEAD") and r[1] != "/v2/"]

    def test_both_by_digest_and_verified_as_anyone_pulls_them(self):
        ghcr = self.ghcr()
        out = self.publish(ghcr)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertPublished(ghcr)
        self.assertEqual(out.stubs.calls(), [])
        reads = self.anonymous_reads(ghcr)
        for path in ("/v2/colvenna/colvenna-catalog/manifests/1.0.0", f"/v2/colvenna/colvenna-catalog/manifests/{self.release.image}",
                     "/v2/colvenna/charts/colvenna-catalog/manifests/1.0.0"):
            self.assertIn(path, reads)
        self.assertTrue(all(r[2] for r in ghcr.writes()))
        self.assertEqual(self.release.registry.writes(), [])
        self.assertIn(f"{RELEASE} published", out.stdout)

    def test_private_until_made_public_then_run_again(self):
        ghcr = self.ghcr(public=False)
        out = self.publish(ghcr)
        self.assertEqual(out.returncode, 4, out.stdout + out.stderr)
        self.assertIn("GHCR makes a package private when it is first pushed", out.stdout)
        self.assertIn("gh api /orgs/colvenna/packages/container/colvenna-catalog --jq .visibility", out.stdout)
        self.assertIn("gh api /orgs/colvenna/packages/container/charts%2Fcolvenna-catalog --jq .visibility", out.stdout)
        self.assertIn("not readable without credentials yet", out.stdout)
        self.assertPublished(ghcr)
        written = len(ghcr.writes())
        ghcr.public = True
        out = self.publish(ghcr)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(len(ghcr.writes()), written)
        self.assertIn("is " + self.release.image + " already", out.stdout)

    def test_another_login_file_never_makes_a_private_package_look_public(self):
        """crane falls back on REGISTRY_AUTH_FILE (Podman's) when it finds no configuration: the check
        without credentials gives it an empty one."""
        ghcr = self.ghcr(public=False)
        auth = scratch() / "auth.json"
        token = base64.b64encode(":".join(PUBLISHER).encode()).decode()
        auth.write_text(json.dumps({"auths": {ghcr.host: {"auth": token}}}))
        out = self.publish(ghcr, REGISTRY_AUTH_FILE=str(auth))
        self.assertEqual(out.returncode, 4, out.stdout + out.stderr)

    def test_a_tag_holding_another_digest_is_never_moved(self):
        ghcr = self.ghcr()
        other = ghcr.seed_image("colvenna/colvenna-catalog", "1.0.0", {REVISION: "b" * 40}, layer=b"other")
        out = self.publish(ghcr)
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn(f"is already {other}, not {RELEASE}'s {self.release.image}: tags never move", out.stderr)
        self.assertEqual(ghcr.writes(), [])
        self.assertEqual(ghcr.tag("colvenna/colvenna-catalog", "1.0.0"), other)

    def test_an_image_or_chart_that_is_not_the_release_s(self):
        for options, needle in (({"revision": "e" * 40}, f"{REVISION} is {'e' * 40}, not {'b' * 40}"),
                                ({"annotation": "e" * 40}, f"colvenna-catalog/source-commit is {'e' * 40}")):
            with self.subTest(options):
                release = Release(**options)
                self.addCleanup(release.registry.close)
                ghcr = self.ghcr()
                out = publish(release.repo, release.registry, ghcr)
                self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
                self.assertIn(needle, out.stderr)
                self.assertEqual(ghcr.writes(), [])

    def test_a_release_registry_that_cannot_be_read(self):
        ghcr = self.ghcr()
        self.release.registry.denied = True
        out = self.publish(ghcr)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("Cannot read", out.stderr)
        self.assertEqual(ghcr.writes(), [])

    def test_never_aws_ecr_public(self):
        ghcr = self.ghcr()
        for settings in ({"PUBLISH_IMAGE": "public.ecr.aws/colvenna/colvenna-catalog"},
                         {"PUBLISH_CHART": "public.ecr.aws/colvenna/charts/colvenna-catalog"},
                         {"PUBLISH_IMAGE": "PUBLIC.ECR.AWS/colvenna/colvenna-catalog"},
                         {"PUBLISH_CHART": "oci://public.ecr.aws/colvenna/charts/colvenna-catalog"},
                         {"PUBLISH_IMAGE": "api.ecr-public.us-east-1.amazonaws.com/colvenna/colvenna-catalog"},
                         {"COLVENNA_RELEASE_REGISTRY": "public.ecr.aws"}):
            with self.subTest(settings):
                out = self.publish(ghcr, **settings)
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn("AWS ECR Public is never a target", out.stderr)
                self.assertEqual(out.stubs.calls(), [])
        self.assertEqual(ghcr.requests, [])
        self.assertEqual(self.release.registry.requests, [])

    def test_only_ghcr(self):
        ghcr = self.ghcr()
        for target in ("docker.io/colvenna/colvenna-catalog", "444455556666.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog",
                       "ghcr.io/colvenna/colvenna-catalog:1.0.0"):
            with self.subTest(target):
                out = self.publish(ghcr, PUBLISH_IMAGE=target)
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn("is published to GHCR only", out.stderr)
        self.assertEqual(ghcr.requests, [])

    def test_a_release_s_name_and_its_tag(self):
        ghcr = self.ghcr()
        for args, needle in (((CANDIDATE,), "is not v<major>.<minor>.<patch>"), (("v1.0.1",), "No tag v1.0.1"),
                             ((), "usage")):
            with self.subTest(args):
                out = self.publish(ghcr, *args) if args else subprocess.run(
                    [str(self.release.repo / "scripts/release/publish-ghcr.sh")], capture_output=True, text=True)
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn(needle, out.stderr)
        self.assertEqual(ghcr.requests, [])

    def test_the_release_registry_is_required(self):
        out = self.publish(self.ghcr(), COLVENNA_RELEASE_REGISTRY="")
        self.assertEqual(out.returncode, 2)
        self.assertIn("COLVENNA_RELEASE_REGISTRY", out.stderr)

    def test_a_token_needs_its_user(self):
        out = self.publish(self.ghcr(), GHCR_USERNAME="")
        self.assertEqual(out.returncode, 2)
        self.assertIn("GHCR_USERNAME", out.stderr)

    def test_gh_s_token_needs_write_packages(self):
        """GHCR itself, with gh's token: refused before anything is read from a registry, since gh's token
        lacks write:packages. crane and Helm are stubs here too, so nothing could reach ghcr.io."""
        headers = "HTTP/2.0 200 OK\r\nX-Oauth-Scopes: gist, read:org, repo, workflow\r\n\r\n{}"
        stubs = Stubs({"aws": {}, "gh": {"api -i /user": {"stdout": headers}}, "crane": {}, "helm": {}},
                      commands=("aws", "gh", "crane", "helm"))
        out = publish(self.release.repo, self.release.registry, self.ghcr(), stubs=stubs, PUBLISH_IMAGE="",
                      PUBLISH_CHART="", GHCR_USERNAME="", GHCR_TOKEN="")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("not write:packages: gh auth refresh -h github.com -s write:packages", out.stderr)
        self.assertEqual(stubs.calls(), ["gh api -i /user"])
        self.assertEqual(self.release.registry.requests, [])


class Registries(unittest.TestCase):
    """registries.sh, which both scripts source, against a stub registry."""

    def held(self, registry, reference):
        script = (f'source "{REPO}/deploy/lib/release.sh"; source "{REPO}/scripts/release/registries.sh"; '
                  f'registry_held "{registry.host}/{reference}"')
        return subprocess.run(["bash", "-c", f"set -euo pipefail; {script}"], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{TOOLS}:{os.environ['PATH']}", "DOCKER_CONFIG": str(scratch())})

    def test_a_tag_s_digest_and_none_for_a_tag_not_there(self):
        registry = registry_stub.Registry()
        self.addCleanup(registry.close)
        digest = registry.seed_image("a/image", "1.0.0", {REVISION: "b" * 40})
        out = self.held(registry, "a/image:1.0.0")
        self.assertEqual((out.returncode, out.stdout), (0, digest + "\n"), out.stderr)
        for reference in ("a/image:2.0.0", "b/image:1.0.0"):
            with self.subTest(reference):
                out = self.held(registry, reference)
                self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)

    def test_crane_s_warnings_are_not_its_answer(self):
        """A registry that answers HEAD with 405: crane warns on stderr, then reads with GET."""
        registry = registry_stub.Registry(no_head=True)
        self.addCleanup(registry.close)
        digest = registry.seed_image("a/image", "1.0.0", {REVISION: "b" * 40})
        warned = subprocess.run([str(TOOLS / "crane"), "digest", f"{registry.host}/a/image:1.0.0"], capture_output=True,
                                text=True, env={**os.environ, "DOCKER_CONFIG": str(scratch())})
        self.assertEqual(warned.stdout, digest + "\n")
        self.assertIn("falling back on GET", warned.stderr)
        out = self.held(registry, "a/image:1.0.0")
        self.assertEqual((out.returncode, out.stdout), (0, digest + "\n"), out.stderr)

    def test_a_registry_that_will_not_be_read(self):
        registry = registry_stub.Registry(denied=True)
        self.addCleanup(registry.close)
        out = self.held(registry, "a/image:1.0.0")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertIn("DENIED", out.stderr)


class Scripts(unittest.TestCase):
    def test_neither_script_has_an_aws_ecr_public_command(self):
        for script in ("promote.sh", "publish-ghcr.sh"):
            text = (HERE / script).read_text()
            with self.subTest(script):
                self.assertIsNone(re.search(r"\baws\s+ecr-public\b", text))
                self.assertNotIn("public.ecr.aws", text)
        text = (HERE / "publish-ghcr.sh").read_text()
        self.assertIn('image_target="${PUBLISH_IMAGE:-ghcr.io/colvenna/colvenna-catalog}"', text)
        self.assertIn('chart_target="${PUBLISH_CHART:-ghcr.io/colvenna/charts/colvenna-catalog}"', text)
        self.assertEqual((promotion.GHCR_IMAGE, promotion.GHCR_CHART),
                         ("ghcr.io/colvenna/colvenna-catalog", "ghcr.io/colvenna/charts/colvenna-catalog"))

    def test_the_stub_registry_s_chart_package_is_a_helm_package(self):
        registry = registry_stub.Registry()
        self.addCleanup(registry.close)
        package = chart_package(REPO / CHART_DIR, "1.0.0", "1.0.0", "b" * 40)
        push_chart(registry, package)
        with tarfile.open(held_package(registry, CHART_REPOSITORY, "1.0.0")) as held, tarfile.open(package) as pushed:
            self.assertEqual(sorted(held.getnames()), sorted(pushed.getnames()))


if __name__ == "__main__":
    unittest.main()
