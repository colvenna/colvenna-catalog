"""install.sh --release (deploy/lib/release.sh), and the release scripts' refusals (scripts/release):
the checkout must be the candidate's tag, the release registry must hold its image and chart by
digest, the image is copied by digest and the chart pulled by digest, and nothing is built."""
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, run_bash, write
from stubs import Stubs

sys.path.insert(0, str(REPO / "scripts" / "release"))
import manifest  # noqa: E402

CANDIDATE = "v1.0.0-rc.1"
REGISTRY = "444455556666.dkr.ecr.us-east-2.amazonaws.com"
OWN = "111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-uat/colvenna-catalog"
IMAGE = "sha256:" + "1" * 64
CHART = "sha256:" + "2" * 64
CONFIG = ("COLVENNA_ENV=uat\nCOLVENNA_ACCOUNT=111122223333\nCOLVENNA_REGION=us-west-2\nCOLVENNA_FORM=eks\n"  # pragma: allowlist secret - a placeholder install configuration: a test environment's name, AWS's documentation account
          "COLVENNA_EKS_CLUSTER=c1\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Checkout(unittest.TestCase):
    """release_checkout in a repository of its own: B, then the tag's commit C = B + release/<candidate>/."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / "scripts/release").mkdir(parents=True)
        shutil.copy(REPO / "scripts/release/manifest.py", self.repo / "scripts/release/manifest.py")
        for path in ("install.sh", "uninstall.sh", "deploy/lib/config.sh", "deploy/aws/src/app.ts",
                     "deploy/helm/colvenna-catalog/Chart.yaml"):
            self.write(path, f"{path}\n")
        self.built_from = self.commit()
        self.write(f"release/{CANDIDATE}/manifest.json", json.dumps(self.manifest_of(self.built_from)))
        self.tag = self.commit()

    def write(self, path, text):
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")
        return git(self.repo, "rev-parse", "HEAD")

    def manifest_of(self, commit, **changes):
        objects = {path: git(self.repo, "rev-parse", f"{commit}:{path}") for path in manifest.PINNED_PATHS}
        return manifest.build({"candidate": CANDIDATE, "built_from": commit, "generated": "2026-10-28T06:00:00Z",
                               "image_digest": IMAGE, "chart_digest": CHART, "objects": objects, "uat_cases": None,
                               "required_cases": [], "regions": ["us-west-2", "us-east-2"], **changes})

    def checkout(self, candidate=CANDIDATE):
        return run_bash(f'REPO="{self.repo}"; release_checkout {candidate}; echo "$SOURCE_SHA $RELEASE_MANIFEST"')

    def assertRefused(self, out, *needles):
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        for needle in needles:
            self.assertIn(needle, out.stderr)

    def test_the_tag_installs_as_the_commit_it_was_built_from(self):
        out = self.checkout()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [self.built_from, str(self.repo / f"release/{CANDIDATE}/manifest.json")])

    def test_a_commit_that_changes_anything_outside_the_candidate_s_directory(self):
        self.write("install.sh", "changed\n")
        self.commit()
        self.assertRefused(self.checkout(), "differs from", "install.sh", f"check out the tag {CANDIDATE}")

    def test_another_candidate_s_directory_is_outside_this_one_s(self):
        self.write("release/v1.0.0-rc.2/notes.md", "notes\n")
        self.commit()
        self.assertRefused(self.checkout(), "release/v1.0.0-rc.2/notes.md")

    def test_more_in_the_candidate_s_own_directory_is_fine(self):
        self.write(f"release/{CANDIDATE}/notes.md", "notes\n")
        self.commit()
        self.assertEqual(self.checkout().returncode, 0)

    def test_a_dirty_tree(self):
        self.write("install.sh", "edited, not committed\n")
        self.assertRefused(self.checkout(), "clean checkout")

    def test_no_manifest_here(self):
        self.assertRefused(self.checkout("v1.0.0-rc.2"), "No release/v1.0.0-rc.2/manifest.json", "git checkout v1.0.0-rc.2")

    def test_a_manifest_that_is_not_valid_or_not_the_candidate_s(self):
        broken = self.manifest_of(self.built_from)
        broken["image"]["digest"] = "latest"
        self.write(f"release/{CANDIDATE}/manifest.json", json.dumps(broken))
        self.commit()
        self.assertRefused(self.checkout(), "not a manifest this checkout can install", "image.digest")

    def test_a_commit_this_clone_lacks(self):
        self.write(f"release/{CANDIDATE}/manifest.json", json.dumps(self.manifest_of(self.built_from) | {
            "source": {**self.manifest_of(self.built_from)["source"], "built_from": "c" * 40}}))
        self.commit()
        self.assertRefused(self.checkout(), "lacks " + "c" * 40, "git fetch")

    def test_a_manifest_whose_objects_are_not_the_checkout_s(self):
        forged = self.manifest_of(self.built_from)
        forged["chart"]["tree"] = "d" * 40
        self.write(f"release/{CANDIDATE}/manifest.json", json.dumps(forged))
        self.commit()
        self.assertRefused(self.checkout(), "does not describe this checkout", "chart")


def release_env(**over):
    """A loaded configuration with a release manifest (RELEASE_MANIFEST) and scratch directories."""
    path = Path(tempfile.mkdtemp()) / "manifest.json"
    path.write_text(json.dumps(manifest.build({
        "candidate": CANDIDATE, "built_from": "b" * 40, "generated": "2026-10-28T06:00:00Z", "image_digest": IMAGE,
        "chart_digest": CHART, "objects": {p: "3" * 40 for p in manifest.PINNED_PATHS}, "uat_cases": None,
        "required_cases": [], "regions": ["us-west-2", "us-east-2"]})))
    return {"REPO": str(REPO), "RELEASE_MANIFEST": str(path), "COLVENNA_RELEASE_REGISTRY": REGISTRY, "COLVENNA_REGION": "us-west-2",
            "WORK": tempfile.mkdtemp(), "EVIDENCE": tempfile.mkdtemp(), **over}


class RegistryCheck(unittest.TestCase):
    def check(self, image, chart):
        stubs = Stubs({"aws": {
            "ecr batch-get-image --region us-east-2 --registry-id 444455556666 --repository-name "
            "colvenna-catalog-release/colvenna-catalog ": image,
            "ecr batch-get-image --region us-east-2 --registry-id 444455556666 --repository-name "
            "colvenna-catalog-release/charts/colvenna-catalog ": chart,
        }})
        return run_bash("release_registry_check", env={**release_env(), **stubs.env}, path_prepend=stubs.bin), stubs

    def test_both_by_digest(self):
        out, stubs = self.check({"stdout": f"{IMAGE}\tNone\n"}, {"stdout": f"{CHART}\tNone\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = stubs.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn(f"--image-ids imageDigest={IMAGE}", calls[0])
        self.assertIn(f"--image-ids imageDigest={CHART}", calls[1])

    def test_a_warning_on_stderr_is_not_the_answer(self):
        warning = "/usr/lib/python3/dist-packages/urllib3: InsecureRequestWarning\n"
        out, _ = self.check({"stdout": f"{IMAGE}\tNone\n", "stderr": warning},
                            {"stdout": f"{CHART}\tNone\n", "stderr": warning})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_chart_the_registry_lacks(self):
        out, _ = self.check({"stdout": f"{IMAGE}\tNone\n"}, {"stdout": "None\tImageNotFound\n"})
        self.assertEqual(out.returncode, 3)
        self.assertIn(f"chart: {REGISTRY} holds no colvenna-catalog-release/charts/colvenna-catalog@{CHART}", out.stderr)
        self.assertNotIn("image:", out.stderr)

    def test_a_registry_that_refuses_this_account(self):
        denied = {"stderr": "An error occurred (AccessDeniedException) when calling the BatchGetImage operation\n",
                  "code": 254}
        out, _ = self.check(denied, denied)
        self.assertEqual(out.returncode, 3)
        self.assertIn("image: cannot read", out.stderr)
        self.assertIn("chart: cannot read", out.stderr)
        self.assertIn("pull accounts", out.stderr)


def reading_stdin(stubs, command):
    """The stub command, reading its standard input as the real one does when told --password-stdin (a
    login reads the password piped to it; a stub that did not would break the pipe)."""
    hidden = stubs.dir / "hidden"
    hidden.mkdir(exist_ok=True)
    (stubs.bin / command).rename(hidden / command)
    wrapper = stubs.bin / command
    wrapper.write_text(f'#!/usr/bin/env bash\n[[ " $* " != *" --password-stdin "* ]] || cat >/dev/null\n'
                       f'exec "{hidden / command}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)


HELM = REPO / ".tools/helm"
HEAD = git(REPO, "rev-parse", "HEAD")


def fake_helm(directory, package=None, names=None):
    """A helm that answers registry login and pull (copying the package given, or writing empty files of
    the names given, where Helm 3.19 would: <chart>@sha256-<hex>.tgz), and runs the pinned Helm for the rest."""
    path = Path(directory) / "helm"
    if names:
        pull = f'for name in {names}; do printf chart >"$dir/$name"; done'
    else:
        pull = f'cp "{package}" "$dir/colvenna-catalog@sha256-${{ref##*@sha256:}}.tgz"'
    path.write_text(f"""#!/usr/bin/env bash
echo "helm $*" >>"$STUB_DIR/calls.log"
case "$1" in
  registry) cat >/dev/null ;;
  pull) ref="$2"; dir="${{@: -1}}"; {pull} ;;
  *) exec "{HELM}" "$@" ;;
esac
""")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def package(directory, commit=HEAD, version="1.0.0-rc.1", app="v1.0.0-rc.1", change=None):
    """This repository's chart at HEAD, packaged as a release packages it (release_chart_source), with
    the commit, version and appVersion given; change(<chart dir>) alters it before it is packaged."""
    work = Path(directory)
    (work / "source").mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD", "deploy/helm/colvenna-catalog"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(work / "source")], input=archive, check=True)
    out = run_bash(f'REPO="{REPO}"; release_chart_source "{work}/source/deploy/helm/colvenna-catalog" '
                   f'"{work}/chart/colvenna-catalog" {version} {app} {commit}')
    assert out.returncode == 0, out.stderr
    if change:
        change(work / "chart/colvenna-catalog")
    out = subprocess.run([str(HELM), "package", str(work / "chart/colvenna-catalog"), "-d", str(work / "packaged")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return next((work / "packaged").glob("*.tgz"))


def image_config(revision=HEAD, platform=("linux", "amd64")):
    labels = {} if revision is None else {"org.opencontainers.image.revision": revision}
    return {"os": platform[0], "architecture": platform[1], "config": {"Labels": labels}}


class ChartSource(unittest.TestCase):
    """release_chart_source and release_chart_differences: what make-manifest.sh packages, and how a
    verification compares a package with it."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.package = package(self.work)
        self.expected = self.work / "chart/colvenna-catalog"

    def differences(self, package, directory):
        out = run_bash(f'release_chart_differences "{package}" "{directory}"', path_prepend=str(REPO / ".tools"))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_a_package_it_cannot_read_is_a_failure_not_a_match(self):
        bad = self.work / "bad.tgz"
        bad.write_text("not a package")
        out = run_bash(f'release_chart_differences "{bad}" "{self.expected}"', path_prepend=str(REPO / ".tools"))
        self.assertNotEqual(out.returncode, 0)
        broken = Stubs({"helm": {}}, commands=("helm",))  # a helm that answers nothing, with status 97
        out = run_bash(f'release_chart_differences "{self.package}" "{self.expected}"', env=broken.env,
                       path_prepend=broken.bin)
        self.assertNotEqual(out.returncode, 0)

    def test_only_version_app_version_and_the_annotation_differ_from_the_chart_directory(self):
        out = subprocess.run(["diff", str(REPO / "deploy/helm/colvenna-catalog/Chart.yaml"), str(self.expected / "Chart.yaml")],
                             capture_output=True, text=True)
        changed = [line for line in out.stdout.splitlines() if line[:1] in "<>"]
        self.assertEqual(sorted(changed), sorted([
            '< version: 0.1.0', '< appVersion: "0.1.0"', '> version: 1.0.0-rc.1', '> appVersion: "v1.0.0-rc.1"',
            '> annotations:', f'>   colvenna-catalog/source-commit: "{HEAD}"']))
        out = subprocess.run(["diff", "-r", "--exclude=Chart.yaml", str(REPO / "deploy/helm/colvenna-catalog"),
                              str(self.expected)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout)
        shown = subprocess.run([str(HELM), "show", "chart", str(self.package)], capture_output=True, text=True).stdout
        self.assertIn(f"colvenna-catalog/source-commit: {HEAD}", shown)

    def test_the_package_holds_exactly_the_files(self):
        self.assertEqual(self.differences(self.package, self.expected), "")

    def test_a_changed_template_value_or_chart_field_is_a_difference(self):
        def template(chart):
            with open(chart / "templates/service.yaml", "a") as f:
                f.write("# changed\n")

        def values(chart):
            (chart / "values.yaml").write_text((chart / "values.yaml").read_text().replace("replicas: 2", "replicas: 3"))

        def description(chart):
            text = (chart / "Chart.yaml").read_text()
            (chart / "Chart.yaml").write_text(text.replace("description: ", "description: Not "))

        def extra(chart):
            (chart / "templates/extra.yaml").write_text("{}\n")

        for change in (template, values, description, extra):
            with self.subTest(change.__name__):
                other = package(tempfile.mkdtemp(), change=change)
                self.assertNotEqual(self.differences(other, self.expected), "")

    def test_a_chart_with_annotations_of_its_own_is_refused(self):
        chart = Path(tempfile.mkdtemp()) / "colvenna-catalog"
        chart.mkdir()
        (chart / "Chart.yaml").write_text("name: c\nversion: 0.1.0\nannotations:\n  a: b\n")
        out = run_bash(f'release_chart_source "{chart}" "{chart}-out/colvenna-catalog" 1.0.0-rc.1 v1.0.0-rc.1 {HEAD}')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("annotations of its own", out.stderr)


class Verify(unittest.TestCase):
    """release_verify_artifacts: the image and the chart the registry holds are built_from's."""

    def verify(self, config=None, chart=None, built_from=HEAD):
        stubs = Stubs({"aws": {"ecr get-login-password": {"stdout": "password\n"}},
                       "crane": {"auth login": {"stdout": ""}, "config": {"stdout": config or image_config()}}},
                      commands=("aws", "crane"))
        reading_stdin(stubs, "crane")
        fake_helm(stubs.bin, chart or package(tempfile.mkdtemp()))
        env = release_env(**stubs.env)
        data = json.loads(Path(env["RELEASE_MANIFEST"]).read_text())
        data["source"]["built_from"] = built_from
        Path(env["RELEASE_MANIFEST"]).write_text(json.dumps(data))
        out = run_bash('release_auth; release_verify_artifacts "$WORK/release"; echo "$RELEASE_CHART"', env=env,
                       path_prepend=stubs.bin)
        return out, env, stubs

    def assertRefused(self, out, *needles):
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        for needle in needles:
            self.assertIn(needle, out.stderr)

    def test_the_candidate_s_own_image_and_chart(self):
        out, env, stubs = self.verify()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().endswith(f"colvenna-catalog@sha256-{'2' * 64}.tgz"), out.stdout)
        notes = (Path(env["EVIDENCE"]) / "release.txt").read_text()
        self.assertIn(f"org.opencontainers.image.revision={HEAD}", notes)
        self.assertIn(f"colvenna-catalog/source-commit={HEAD}", notes)
        self.assertTrue(any(c.startswith(f"crane config {REGISTRY}/colvenna-catalog-release/colvenna-catalog@{IMAGE}")
                            for c in stubs.calls()))
        self.assertTrue(any(c.startswith(f"helm pull oci://{REGISTRY}/colvenna-catalog-release/charts/colvenna-catalog@{CHART} ")
                            for c in stubs.calls()))

    def test_an_image_of_another_commit_or_without_the_label(self):
        out, _, _ = self.verify(config=image_config(revision="c" * 40))
        self.assertRefused(out, f"image: its label org.opencontainers.image.revision is {'c' * 40}, not {HEAD}")
        out, _, _ = self.verify(config=image_config(revision=None))
        self.assertRefused(out, "org.opencontainers.image.revision is missing")

    def test_an_image_for_another_platform(self):
        out, _, _ = self.verify(config=image_config(platform=("linux", "arm64")))
        self.assertRefused(out, "image: it is for linux/arm64, not linux/amd64")

    def test_a_chart_of_another_commit(self):
        out, _, _ = self.verify(chart=package(tempfile.mkdtemp(), commit="c" * 40))
        self.assertRefused(out, f"colvenna-catalog/source-commit is {'c' * 40}, not {HEAD}")

    def test_a_chart_whose_files_are_not_the_commit_s(self):
        def change(chart):
            with open(chart / "templates/service.yaml", "a") as f:
                f.write("# changed\n")
        out, _, _ = self.verify(chart=package(tempfile.mkdtemp(), change=change))
        self.assertRefused(out, "chart: its files are not", "service.yaml")

    def test_a_chart_of_another_version(self):
        out, _, _ = self.verify(chart=package(tempfile.mkdtemp(), version="0.1.0", app="0.1.0"))
        self.assertRefused(out, "chart: not version 1.0.0-rc.1")

    def test_a_pull_that_leaves_no_single_package(self):
        stubs = Stubs({"aws": {"ecr get-login-password": {"stdout": "password\n"}},
                       "crane": {"auth login": {"stdout": ""}, "config": {"stdout": image_config()}}},
                      commands=("aws", "crane"))
        reading_stdin(stubs, "crane")
        fake_helm(stubs.bin, names="a.tgz b.tgz")
        out = run_bash('release_auth; release_verify_artifacts "$WORK/release"', env=release_env(**stubs.env),
                       path_prepend=stubs.bin)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no single package", out.stderr)


class CopyImage(unittest.TestCase):
    def run_copy(self, held, after=IMAGE):
        stubs = Stubs({"aws": {
            "ecr get-login-password": {"stdout": "password\n"},
            "ecr describe-images": held,
        }, "crane": {
            "auth login": {"stdout": ""},
            "copy": {"stdout": ""},
            "digest": {"stdout": after + "\n"},
        }}, commands=("aws", "crane"))
        reading_stdin(stubs, "crane")
        fake_helm(stubs.bin, names="unused.tgz")
        out = run_bash(f'release_auth; copy_release_image "{OWN}"; echo "$IMAGE_REPOSITORY@$IMAGE_DIGEST"',
                       env=release_env(**stubs.env), path_prepend=stubs.bin)
        return out, stubs

    def copied(self, stubs):
        return [c for c in stubs.calls() if c.startswith("crane copy")]

    def test_the_image_is_copied_by_digest_into_the_environment_s_repository(self):
        out, stubs = self.run_copy({"stdout": "None\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.copied(stubs), [
            f"crane copy {REGISTRY}/colvenna-catalog-release/colvenna-catalog@{IMAGE} {OWN}:{CANDIDATE}"])
        self.assertEqual(out.stdout.splitlines()[-1], f"{OWN}@{IMAGE}")
        logins = [c for c in stubs.calls() if c.startswith("crane auth login")]
        self.assertEqual([c.split()[3] for c in logins], [REGISTRY, OWN.split("/")[0]])
        regions = [c for c in stubs.calls() if c.startswith("aws ecr get-login-password")]
        self.assertEqual([c.split()[-1] for c in regions], ["us-east-2", "us-east-2", "us-west-2"])

    def test_an_image_already_there_is_not_copied_again(self):
        out, stubs = self.run_copy({"stdout": IMAGE + "\n"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.copied(stubs), [])

    def test_a_tag_holding_another_image_is_never_moved(self):
        out, stubs = self.run_copy({"stdout": "sha256:" + "9" * 64 + "\n"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is already sha256:" + "9" * 64, out.stderr)
        self.assertEqual(self.copied(stubs), [])

    def test_a_copy_of_another_digest_fails(self):
        out, _ = self.run_copy({"stdout": "None\n"}, after="sha256:" + "8" * 64)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("after the copy", out.stderr)


class CdkDependencies(unittest.TestCase):
    """A release install always installs the CDK app's packages from the lockfile; from source, only when
    they are missing."""

    def calls(self, mode, present):
        repo = Path(tempfile.mkdtemp())
        (repo / "deploy/aws").mkdir(parents=True)
        if present:
            (repo / "deploy/aws/node_modules").mkdir()
        stubs = Stubs({"npm": {"ci": {"stdout": ""}}}, commands=("npm",))
        out = run_bash(f'REPO="{repo}"; cdk_dependencies {mode}', env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        return stubs.calls()

    def test_exact_always_runs_npm_ci(self):
        self.assertEqual(self.calls("exact", present=True), ["npm ci --no-audit --no-fund"])
        self.assertEqual(self.calls("exact", present=False), ["npm ci --no-audit --no-fund"])

    def test_if_missing_keeps_what_is_there(self):
        self.assertEqual(self.calls("if-missing", present=True), [])
        self.assertEqual(self.calls("if-missing", present=False), ["npm ci --no-audit --no-fund"])

    def test_install_sh_asks_for_exact_with_release(self):
        text = (REPO / "install.sh").read_text()
        self.assertIn('cdk_dependencies "$([[ -n "$RELEASE_CANDIDATE" ]] && echo exact || echo if-missing)"', text)


class InstallArguments(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def install(self, args, config=CONFIG):
        stubs = Stubs({"aws": {}, "kubectl": {}, "docker": {}, "mvn": {}}, commands=("aws", "kubectl", "docker", "mvn"))
        out = run_bash(f'"{REPO}/install.sh" --config {write(self.dir / "uat.env", config)} {args}', env=stubs.env,
                       path_prepend=stubs.bin)
        return out, stubs

    def test_a_candidate_s_name(self):
        for args in ("--release", "--release 1.0.0-rc.1", "--release v1.0.0", "--release v1.0.0-rc.1 --from-source"):
            with self.subTest(args):
                out, stubs = self.install(args)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertEqual(stubs.calls(), [])

    def test_a_release_needs_its_registry(self):
        out, stubs = self.install(f"--release {CANDIDATE}")
        self.assertEqual(out.returncode, 2)
        self.assertIn("COLVENNA_RELEASE_REGISTRY: required with --release", out.stderr)
        self.assertEqual(stubs.calls(), [])

    def test_a_registry_that_is_not_one(self):
        out, stubs = self.install(f"--release {CANDIDATE}", CONFIG + "COLVENNA_RELEASE_REGISTRY=ghcr.io\n")
        self.assertEqual(out.returncode, 2)
        self.assertIn("COLVENNA_RELEASE_REGISTRY: the release registry", out.stderr)
        self.assertEqual(stubs.calls(), [])


if __name__ == "__main__":
    unittest.main()
