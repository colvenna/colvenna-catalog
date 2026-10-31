"""Offline tests of manifest.py: the release manifest's schema and validation, the names of a
candidate's image and chart, the names install.sh --release takes (a candidate's or a release's), how
make-manifest.sh's gathered files become a manifest, the checks against a checkout, the required cases,
the release notes' draft, and the command line, for a candidate's manifest and a release's."""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
TOOL = os.path.join(HERE, "manifest.py")
sys.path.insert(0, HERE)
import manifest  # noqa: E402
import promotion  # noqa: E402

SHA = "b" * 40
IMAGE = "sha256:" + "1" * 64
CHART = "sha256:" + "2" * 64
OBJECTS = {"install.sh": "3" * 40, "uninstall.sh": "4" * 40, "deploy/lib": "5" * 40, "deploy/aws": "6" * 40,
           "deploy/helm/colvenna-catalog": "7" * 40}
CASES = {"commit": "8" * 40, "blob": "9" * 40}
TERMS = {"commit": "a" * 40, "blob": "c" * 40}


def inputs(**changes):
    """What assemble hands build, for candidate v1.0.0-rc.1, before the UAT cases exist (#19)."""
    value = {
        "candidate": "v1.0.0-rc.1",
        "built_from": SHA,
        "generated": "2026-10-28T06:00:00Z",
        "image_digest": IMAGE,
        "chart_digest": CHART,
        "objects": dict(OBJECTS),
        "uat_cases": None,
        "required_cases": [],
        "regions": ["us-west-2", "us-east-2"],
    }
    value.update(changes)
    return value


def with_cases(**changes):
    """What a candidate for GO has: the UAT cases with their required set, and the performance terms."""
    return inputs(**{"uat_cases": dict(CASES), "required_cases": ["UAT-CAT-01", "UAT-SEC-01"],
                     "performance": dict(TERMS), **changes})


class Names(unittest.TestCase):
    def test_a_candidate_is_v_major_minor_patch_rc_n(self):
        for name in ("v1.0.0-rc.1", "v0.0.0-rc.1", "v10.2.3-rc.12"):
            with self.subTest(name):
                self.assertEqual(manifest.candidate_problems(name), [])
        for name in ("1.0.0-rc.1", "v1.0.0", "v1.0.0-rc.0", "v1.0-rc.1", "v01.0.0-rc.1", "v1.0.0-dev.1",
                     "v1.0.0-rc.1\n", "v1.0.0-RC.1", "", None, 1):
            with self.subTest(repr(name)):
                self.assertNotEqual(manifest.candidate_problems(name), [])

    def test_the_image_is_tagged_with_the_candidate_and_the_chart_versioned_by_it(self):
        self.assertEqual(manifest.names("v1.0.0-rc.2"), [
            ("image", "colvenna-catalog-release/colvenna-catalog", "v1.0.0-rc.2"),
            ("chart", "colvenna-catalog-release/charts/colvenna-catalog", "1.0.0-rc.2"),
        ])
        with self.assertRaisesRegex(ValueError, "v<major>.<minor>.<patch>-rc.<n>"):
            manifest.names("v1.0.0")

    def test_install_sh_takes_a_candidate_s_name_or_a_release_s(self):
        for name, kind in (("v1.0.0-rc.1", "candidate"), ("v10.2.3-rc.12", "candidate"), ("v1.0.0", "release"),
                           ("v0.1.0", "release"), ("v10.2.3", "release")):
            with self.subTest(name):
                self.assertEqual(manifest.kind(name), kind)
        for name in ("1.0.0", "1.0.0-rc.1", "v1.0", "v01.0.0", "v1.0.0-rc.0", "v1.0.0-dev.1", "v1.0.0\n", "latest", "",
                     None):
            with self.subTest(repr(name)):
                with self.assertRaisesRegex(ValueError, "neither a candidate, .*, nor a release"):
                    manifest.kind(name)


def release_of(candidate):
    """The release promoted from the candidate (a candidate for GO of v1.0.0), as promote.sh assembles it."""
    return promotion.build(candidate, {
        "release": "v1.0.0", "generated": "2026-11-02T06:00:00Z", "tag_commit": "d" * 40, "manifest_blob": "e" * 40,
        "acceptance": {"commit": "f" * 40, "blob": "0" * 40}, "chart_digest": "sha256:" + "3" * 64, "chart_files": 10})


class Build(unittest.TestCase):
    def test_a_built_manifest_is_valid_and_pins_the_image_and_the_chart_by_digest(self):
        m = manifest.build(inputs())
        self.assertEqual(manifest.problems(m), [])
        self.assertEqual(m["schema"], 1)
        self.assertEqual(m["image"], {"repository": "colvenna-catalog-release/colvenna-catalog", "tag": "v1.0.0-rc.1",
                                      "digest": IMAGE, "platform": "linux/amd64"})
        self.assertEqual(m["chart"], {"name": "colvenna-catalog", "path": "deploy/helm/colvenna-catalog",
                                      "tree": OBJECTS["deploy/helm/colvenna-catalog"], "version": "1.0.0-rc.1",
                                      "appVersion": "v1.0.0-rc.1",
                                      "repository": "colvenna-catalog-release/charts/colvenna-catalog",
                                      "digest": CHART})
        self.assertEqual(m["installer"], {"install": {"path": "install.sh", "blob": OBJECTS["install.sh"]},
                                          "uninstall": {"path": "uninstall.sh", "blob": OBJECTS["uninstall.sh"]},
                                          "lib": {"path": "deploy/lib", "tree": OBJECTS["deploy/lib"]}})
        self.assertEqual(m["cdk"], {"path": "deploy/aws", "tree": OBJECTS["deploy/aws"]})
        self.assertEqual(m["source"], {"repository": "https://github.com/colvenna/colvenna-catalog", "built_from": SHA,
                                       "tag": "v1.0.0-rc.1 points at built_from with release/v1.0.0-rc.1/ added, nothing else"})
        self.assertEqual(m["configurations"], {"forms": ["eks"], "regions": ["us-west-2", "us-east-2"]})

    def test_no_registry_account_or_identity_in_the_manifest(self):
        # The release registry is the installer's setting COLVENNA_RELEASE_REGISTRY, never the manifest's.
        text = json.dumps(manifest.build(with_cases()))
        self.assertNotIn("dkr.ecr", text)
        self.assertNotIn("amazonaws.com", text)
        self.assertNotIn("arn:", text)
        self.assertIsNone(re.search(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])", text))

    def test_before_the_uat_cases_exist_it_is_valid_but_not_for_go(self):
        m = manifest.build(inputs())
        self.assertEqual(m["uat_cases"], {"path": "docs/uat/cases.md", "status": "absent"})
        self.assertEqual(m["required_cases"], [])
        self.assertEqual(manifest.problems(m), [])
        gaps = manifest.go_problems(m)
        self.assertTrue(any("docs/uat/cases.md" in g and "#19" in g for g in gaps), gaps)
        self.assertTrue(any(g.startswith("required_cases") for g in gaps), gaps)

    def test_with_the_uat_cases_and_the_performance_terms_it_is_valid_for_go(self):
        m = manifest.build(with_cases())
        self.assertEqual(m["uat_cases"], {"path": "docs/uat/cases.md", "status": "present", **CASES})
        self.assertEqual(m["required_cases"], ["UAT-CAT-01", "UAT-SEC-01"])
        self.assertEqual(m["performance"], {"path": "docs/performance/terms.md", "status": "present", **TERMS})
        self.assertEqual(manifest.problems(m), [])
        self.assertEqual(manifest.go_problems(m), [])

    def test_without_the_performance_terms_it_is_valid_but_not_for_go(self):
        m = manifest.build(with_cases(performance=None))
        self.assertEqual(m["performance"], {"path": "docs/performance/terms.md", "status": "absent"})
        self.assertEqual(manifest.problems(m), [])
        gaps = manifest.go_problems(m)
        self.assertEqual(len(gaps), 1, gaps)
        self.assertTrue(gaps[0].startswith("performance: docs/performance/terms.md is absent"), gaps)

    def test_a_missing_or_malformed_input_fails_the_build(self):
        with self.assertRaisesRegex(ValueError, "image.digest"):
            manifest.build(inputs(image_digest="latest"))
        with self.assertRaisesRegex(ValueError, "chart.digest"):
            manifest.build(inputs(chart_digest=""))
        objects = dict(OBJECTS)
        del objects["deploy/aws"]
        with self.assertRaisesRegex(ValueError, "deploy/aws"):
            manifest.build(inputs(objects=objects))
        with self.assertRaisesRegex(ValueError, "configurations.regions"):
            manifest.build(inputs(regions=["us-west-2"]))


class Problems(unittest.TestCase):
    def setUp(self):
        self.good = manifest.build(with_cases())

    def changed(self, change):
        m = copy.deepcopy(self.good)
        change(m)
        return manifest.problems(m)

    def assertFound(self, change, text):
        found = self.changed(change)
        self.assertTrue(any(text in p for p in found), found)

    def test_not_an_object(self):
        for value in ([], "manifest", None, 1):
            with self.subTest(repr(value)):
                self.assertNotEqual(manifest.problems(value), [])

    def test_an_incomplete_manifest(self):
        for field in ("schema", "candidate", "source", "generated", "image", "chart", "installer", "cdk", "uat_cases",
                      "performance",
                      "required_cases", "configurations"):
            with self.subTest(field):
                self.assertFound(lambda m: m.pop(field), f"{field}: missing")
        self.assertFound(lambda m: m["image"].pop("digest"), "image.digest: missing")
        self.assertFound(lambda m: m["chart"].pop("digest"), "chart.digest: missing")
        self.assertFound(lambda m: m["installer"].pop("lib"), "installer.lib: missing")

    def test_unknown_or_misspelled_fields(self):
        self.assertFound(lambda m: m.update(imgae={}), "unknown field imgae")
        self.assertFound(lambda m: m["image"].update(reference="x"), "unknown field image.reference")
        self.assertFound(lambda m: m["chart"].update(tag="1.0.0-rc.1"), "unknown field chart.tag")
        self.assertFound(lambda m: m["cdk"].update(blob="6" * 40), "unknown field cdk.blob")

    def test_the_schema_is_the_number_one(self):
        self.assertFound(lambda m: m.update(schema=2), "schema")
        self.assertFound(lambda m: m.update(schema=True), "schema")
        self.assertFound(lambda m: m.update(schema="1"), "schema")

    def test_names_and_ids(self):
        self.assertFound(lambda m: m.update(candidate="v1.0.0"), "candidate")
        self.assertFound(lambda m: m["source"].update(built_from="main"), "source.built_from")
        self.assertFound(lambda m: m["source"].update(built_from=SHA + "\n"), "source.built_from")
        self.assertFound(lambda m: m["source"].update(repository="https://example.com/fork"), "source.repository")
        self.assertFound(lambda m: m["source"].update(tag="anything"), "source.tag")
        self.assertFound(lambda m: m.update(generated="yesterday"), "generated")
        self.assertFound(lambda m: m.update(generated="2026-10-28T06:00:00+00:00"), "generated")

    def test_a_candidate_made_before_the_move_keeps_the_address_of_the_time(self):
        # v1.0.0-rc.1 was built before the repository moved to the colvenna organization; its manifest
        # records the repository's address then, and stays valid as released.
        earlier = copy.deepcopy(self.good)
        earlier["source"]["repository"] = "https://github.com/example-owner/colvenna-catalog"
        self.assertEqual(manifest.problems(earlier, "v1.0.0-rc.1"), [])
        later = copy.deepcopy(earlier)
        later["candidate"] = "v1.0.0-rc.2"
        later["image"]["tag"] = "v1.0.0-rc.2"
        later["chart"].update(version="1.0.0-rc.2", appVersion="v1.0.0-rc.2")
        later["source"]["tag"] = manifest.source_tag("v1.0.0-rc.2")
        self.assertEqual([p for p in manifest.problems(later) if p.startswith("source.")],
                         ["source.repository: 'https://github.com/example-owner/colvenna-catalog', "
                          "not 'https://github.com/colvenna/colvenna-catalog'"])
        for repository in ("https://github.com/example-owner/another-repository", "https://example.com/colvenna-catalog"):
            earlier["source"]["repository"] = repository
            self.assertIn("source.repository", " ".join(manifest.problems(earlier)))

    def test_the_image(self):
        self.assertFound(lambda m: m["image"].update(digest="sha256:abc"), "image.digest")
        self.assertFound(lambda m: m["image"].update(digest=IMAGE + "\n"), "image.digest")
        self.assertFound(lambda m: m["image"].update(repository="colvenna-catalog-uat/colvenna-catalog"),
                         "image.repository")
        self.assertFound(lambda m: m["image"].update(tag="v1.0.0-rc.2"), "image.tag")
        self.assertFound(lambda m: m["image"].update(platform="linux/arm64"), "image.platform")

    def test_the_chart(self):
        self.assertFound(lambda m: m["chart"].update(version="0.1.0"), "chart.version")
        self.assertFound(lambda m: m["chart"].update(appVersion="0.1.0"), "chart.appVersion")
        self.assertFound(lambda m: m["chart"].update(name="other"), "chart.name")
        self.assertFound(lambda m: m["chart"].update(path="deploy/helm/other"), "chart.path")
        self.assertFound(lambda m: m["chart"].update(tree="HEAD"), "chart.tree")
        self.assertFound(lambda m: m["chart"].update(repository="colvenna-catalog-release/colvenna-catalog"),
                         "chart.repository")
        self.assertFound(lambda m: m["chart"].update(digest="sha256:" + "x" * 64), "chart.digest")

    def test_the_installer_and_the_cdk_app(self):
        self.assertFound(lambda m: m["installer"]["install"].update(path="deploy/install.sh"), "installer.install.path")
        self.assertFound(lambda m: m["installer"]["uninstall"].update(blob="HEAD"), "installer.uninstall.blob")
        self.assertFound(lambda m: m["installer"]["lib"].update(tree="5" * 39), "installer.lib.tree")
        self.assertFound(lambda m: m["cdk"].update(path="deploy/aws/src"), "cdk.path")

    def test_the_uat_cases(self):
        self.assertFound(lambda m: m["uat_cases"].update(status="draft"), "uat_cases.status")
        self.assertFound(lambda m: m["uat_cases"].pop("blob"), "uat_cases.blob: missing")
        self.assertFound(lambda m: m["uat_cases"].update(path="docs/cases.md"), "uat_cases.path")
        self.assertFound(lambda m: m.update(uat_cases={"path": "docs/uat/cases.md", "status": "absent", "commit": SHA}),
                         "unknown field uat_cases.commit")
        # The required cases come from the cases document: none without it, some with it.
        self.assertFound(lambda m: m.update(uat_cases={"path": "docs/uat/cases.md", "status": "absent"}),
                         "required_cases")
        self.assertFound(lambda m: m.update(required_cases=[]), "required_cases")
        self.assertFound(lambda m: m.update(required_cases=["UAT-CAT-1"]), "required_cases")
        self.assertFound(lambda m: m.update(required_cases=["UAT-CAT-01", "UAT-CAT-01"]), "required_cases")
        self.assertFound(lambda m: m.update(required_cases="UAT-CAT-01"), "required_cases")

    def test_the_performance_terms(self):
        self.assertFound(lambda m: m["performance"].update(status="proposed"), "performance.status")
        self.assertFound(lambda m: m["performance"].pop("commit"), "performance.commit: missing")
        self.assertFound(lambda m: m["performance"].update(path="docs/performance/v1.0.0-rc.2.md"), "performance.path")
        self.assertFound(lambda m: m["performance"].update(blob="HEAD"), "performance.blob")
        self.assertFound(lambda m: m.update(performance={"path": "docs/performance/terms.md", "status": "absent",
                                                         "blob": SHA}), "unknown field performance.blob")
        self.assertFound(lambda m: m.update(performance=["docs/performance/terms.md"]), "performance: not an object")

    def test_configurations(self):
        self.assertFound(lambda m: m["configurations"].update(forms=[]), "configurations.forms")
        self.assertFound(lambda m: m["configurations"].update(forms=["eks", "eks"]), "configurations.forms")
        self.assertFound(lambda m: m["configurations"].update(forms=["fargate"]), "configurations.forms")
        self.assertFound(lambda m: m["configurations"].update(forms="eks"), "configurations.forms")
        for regions in (["us-west-2"], ["us-west-2", "us-west-2"], ["us-west-2", "cn-north-1"],
                        ["us-west-2", "us-gov-west-1"], "us-west-2,us-east-2", [["us-west-2"], "us-east-2"]):
            with self.subTest(regions=regions):
                self.assertFound(lambda m: m["configurations"].update(regions=regions), "configurations.regions")
        self.assertEqual(self.changed(lambda m: m["configurations"].update(regions=["eu-west-1", "ap-southeast-2"])), [])
        # The serverless form (#13): a candidate may be accepted in it, alone or beside the EKS form.
        for forms in (["serverless"], ["eks", "serverless"], ["serverless", "eks"]):
            self.assertEqual(self.changed(lambda m: m["configurations"].update(forms=forms)), [], forms)
        self.assertFound(lambda m: m["configurations"].update(forms=["serverless", "serverless"]), "configurations.forms")

    def test_the_forms_a_candidate_is_built_for(self):
        self.assertEqual(manifest.build(inputs())["configurations"]["forms"], ["eks"], "the EKS form unless said")
        self.assertEqual(manifest.build({**inputs(), "forms": ["eks", "serverless"]})["configurations"]["forms"],
                         ["eks", "serverless"])
        with self.assertRaisesRegex(ValueError, "configurations.forms"):
            manifest.build({**inputs(), "forms": ["lambda"]})

    def test_no_account_anywhere(self):
        self.assertFound(lambda m: m.update(registry="123456789012.dkr.ecr.us-west-2.amazonaws.com"),
                         "unknown field registry")
        self.assertFound(lambda m: m["chart"].update(name="123456789012"), "1 AWS account id")
        self.assertFound(lambda m: m["source"].update(tag="arn:aws:iam::123456789012:root"), "1 AWS account id")
        # Twelve digits inside a digest or a SHA are not an account.
        self.assertEqual(self.changed(lambda m: m["image"].update(digest="sha256:" + "1" * 12 + "a" * 52)), [])

    def test_the_candidate_asked_for(self):
        self.assertEqual(manifest.problems(self.good, "v1.0.0-rc.1"), [])
        found = manifest.problems(self.good, "v1.0.0-rc.2")
        self.assertTrue(any("v1.0.0-rc.2" in p for p in found), found)


class FailsThenPasses(unittest.TestCase):
    """The issue's acceptance: the manifest's validation fails first, then passes."""

    def test_a_manifest_fails_until_every_problem_is_fixed(self):
        broken = manifest.build(inputs())
        broken["image"]["digest"] = "latest"
        broken["chart"].pop("digest")
        broken["generated"] = "123456789012"
        path = os.path.join(tempfile.mkdtemp(), "manifest.json")

        def validate():
            with open(path, "w") as f:
                json.dump(broken, f)
            return subprocess.run([sys.executable, TOOL, "validate", path, "--name", "v1.0.0-rc.1"],
                                  capture_output=True, text=True)

        out = validate()
        self.assertEqual(out.returncode, 1)
        for text in ("image.digest", "chart.digest: missing", "generated", "AWS account id"):
            self.assertIn(text, out.stderr)
        broken["image"]["digest"] = IMAGE
        broken["chart"]["digest"] = CHART
        self.assertEqual(validate().returncode, 1)
        broken["generated"] = "2026-10-28T06:00:00Z"
        out = validate()
        self.assertEqual((out.returncode, out.stderr), (0, ""))


class Assemble(unittest.TestCase):
    def work(self, params, cases=None):
        work = tempfile.mkdtemp()
        with open(os.path.join(work, "params.json"), "w") as f:
            json.dump(params, f)
        if cases is not None:
            with open(os.path.join(work, "cases.md"), "w") as f:
                f.write(cases)
        return work

    def params(self, **changes):
        value = {key: inputs()[key] for key in ("candidate", "built_from", "generated", "image_digest",
                                                "chart_digest", "objects", "uat_cases", "regions")}
        value["performance"] = None
        value.update(changes)
        return value

    def test_what_make_manifest_gathered_becomes_the_manifest(self):
        self.assertEqual(manifest.assemble(self.work(self.params())), manifest.build(inputs()))

    def test_the_required_cases_come_from_the_cases_document(self):
        cases = ("# Cases\n\n" + manifest.START + "\n\n| Case | Area |\n| --- | --- |\n| UAT-CAT-01 | catalog |\n"
                 "| [UAT-SEC-01](#uat-sec-01) | security |\n\n" + manifest.END + "\n")
        m = manifest.assemble(self.work(self.params(uat_cases=dict(CASES), performance=dict(TERMS)), cases))
        self.assertEqual(m, manifest.build(with_cases()))

    def test_cases_without_their_document_or_a_document_without_cases(self):
        with self.assertRaises((ValueError, OSError)):
            manifest.assemble(self.work(self.params(uat_cases=dict(CASES))))
        with self.assertRaisesRegex(ValueError, "required-cases"):
            manifest.assemble(self.work(self.params(uat_cases=dict(CASES)), "# Cases\n\nno markers\n"))


class RequiredCases(unittest.TestCase):
    def table(self, rows):
        return f"intro\n{manifest.START}\n| ID | x |\n| --- | --- |\n{rows}{manifest.END}\n"

    def test_every_row_between_the_markers_in_order(self):
        text = self.table("| UAT-CAT-02 | a |\n| `UAT-CAT-01` | b |\n| [UAT-DEP-01](#d) | c |\n")
        self.assertEqual(manifest.required_cases(text), ["UAT-CAT-02", "UAT-CAT-01", "UAT-DEP-01"])

    def test_a_row_without_a_case_twice_listed_or_no_markers_is_refused(self):
        with self.assertRaisesRegex(ValueError, "names no case"):
            manifest.required_cases(self.table("| UAT-CAT-01 | a |\n| see below | b |\n"))
        with self.assertRaisesRegex(ValueError, "twice"):
            manifest.required_cases(self.table("| UAT-CAT-01 | a |\n| UAT-CAT-01 | b |\n"))
        with self.assertRaisesRegex(ValueError, "no case"):
            manifest.required_cases(self.table(""))
        with self.assertRaisesRegex(ValueError, "required-cases"):
            manifest.required_cases("| UAT-CAT-01 | a |\n")
        with self.assertRaisesRegex(ValueError, "before"):
            manifest.required_cases(f"{manifest.END}\n| UAT-CAT-01 |\n{manifest.START}\n")


class Registry(unittest.TestCase):
    def test_an_ecr_registry_of_a_standard_region(self):
        self.assertEqual(manifest.registry_parts("111122223333.dkr.ecr.us-west-2.amazonaws.com"),
                         ("111122223333", "us-west-2"))
        for registry in ("111122223333.dkr.ecr.cn-north-1.amazonaws.com.cn", "111122223333.dkr.ecr.cn-north-1.amazonaws.com",
                         "111122223333.dkr.ecr.us-gov-west-1.amazonaws.com", "1111.dkr.ecr.us-west-2.amazonaws.com",
                         "111122223333.dkr.ecr.us-west-2.amazonaws.com/colvenna-catalog-release", "ghcr.io",
                         "https://111122223333.dkr.ecr.us-west-2.amazonaws.com", ""):
            with self.subTest(registry):
                with self.assertRaisesRegex(ValueError, "COLVENNA_RELEASE_REGISTRY"):
                    manifest.registry_parts(registry)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


class Tree(unittest.TestCase):
    """tree_problems: what the manifest says of the tree is what a commit holds."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        for path in ("install.sh", "uninstall.sh", "deploy/lib/config.sh", "deploy/aws/src/app.ts",
                     "deploy/helm/colvenna-catalog/Chart.yaml"):
            self.write(path, path)
        self.commit()

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")
        return git(self.repo, "rev-parse", "HEAD")

    def pinned(self, commit, path):
        return {"commit": git(self.repo, "log", "-1", "--format=%H", commit, "--", path),
                "blob": git(self.repo, "rev-parse", f"{commit}:{path}")}

    def manifest_of(self, commit, cases=False, terms=False):
        objects = {path: git(self.repo, "rev-parse", f"{commit}:{path}") for path in manifest.PINNED_PATHS}
        extra = {}
        if cases:
            extra = {"uat_cases": self.pinned(commit, manifest.CASES_PATH), "required_cases": ["UAT-CAT-01"]}
        if terms:
            extra["performance"] = self.pinned(commit, manifest.PERFORMANCE_PATH)
        return manifest.build(inputs(built_from=commit, objects=objects, **extra))

    def test_the_commit_it_was_made_from(self):
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(manifest.tree_problems(self.manifest_of(head), self.repo, head), [])

    def test_a_changed_file_or_directory_is_named(self):
        m = self.manifest_of(git(self.repo, "rev-parse", "HEAD"))
        self.write("install.sh", "changed")
        self.write("deploy/lib/new.sh", "new")
        later = self.commit()
        found = manifest.tree_problems(m, self.repo, later)
        self.assertTrue(any(p.startswith("installer.install") for p in found), found)
        self.assertTrue(any(p.startswith("installer.lib") for p in found), found)
        self.assertFalse(any(p.startswith("cdk") or p.startswith("chart") for p in found), found)

    def test_the_cases_document_as_the_manifest_says(self):
        m = self.manifest_of(git(self.repo, "rev-parse", "HEAD"))
        self.write(manifest.CASES_PATH, "cases")
        later = self.commit()
        found = manifest.tree_problems(m, self.repo, later)
        self.assertTrue(any(p.startswith("uat_cases") for p in found), found)
        good = self.manifest_of(later, cases=True)
        self.assertEqual(manifest.tree_problems(good, self.repo, later), [])
        # The commit that last changed it, not any commit.
        good["uat_cases"]["commit"] = git(self.repo, "rev-parse", "HEAD~1")
        found = manifest.tree_problems(good, self.repo, later)
        self.assertTrue(any(p.startswith("uat_cases.commit") for p in found), found)


    def test_the_performance_terms_as_the_manifest_says(self):
        m = self.manifest_of(git(self.repo, "rev-parse", "HEAD"))
        self.write(manifest.PERFORMANCE_PATH, "terms")
        later = self.commit()
        found = manifest.tree_problems(m, self.repo, later)
        self.assertEqual([p for p in found if p.startswith("performance")], found)
        self.assertTrue(found)
        good = self.manifest_of(later, terms=True)
        self.assertEqual(manifest.tree_problems(good, self.repo, later), [])
        self.write(manifest.PERFORMANCE_PATH, "terms, changed")
        found = manifest.tree_problems(good, self.repo, self.commit())
        self.assertTrue(any(p.startswith("performance:") for p in found), found)
        self.assertTrue(any(p.startswith("performance.commit") for p in found), found)


class Notes(unittest.TestCase):
    def template(self):
        return manifest.read(os.path.join(REPO, "release", "notes-template.md"))

    def test_the_draft_fills_every_placeholder(self):
        text = manifest.notes(manifest.build(with_cases()), self.template())
        self.assertNotIn("{{", text)
        for needle in ("v1.0.0-rc.1", SHA, IMAGE, CHART, "1.0.0-rc.1", "`us-west-2`", "`us-east-2`",
                       "docs/performance/terms.md", TERMS["commit"]):
            self.assertIn(needle, text)
        self.assertIsNone(re.search(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])", text))

    def test_the_draft_says_what_a_candidate_for_go_still_lacks(self):
        text = manifest.notes(manifest.build(inputs()), self.template())
        self.assertIn("docs/uat/cases.md", text)
        self.assertIn("#19", text)

    def test_an_unknown_placeholder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "nothing"):
            manifest.notes(manifest.build(inputs()), "{{nothing}}")


class Cli(unittest.TestCase):
    def run_tool(self, *args, stdin=None):
        return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, input=stdin)

    def file(self, value):
        path = os.path.join(tempfile.mkdtemp(), "manifest.json")
        with open(path, "w") as f:
            json.dump(value, f)
        return path

    def test_validate_and_validate_for_go(self):
        absent = self.file(manifest.build(inputs()))
        self.assertEqual(self.run_tool("validate", absent).returncode, 0)
        out = self.run_tool("validate", absent, "--go")
        self.assertEqual(out.returncode, 1)
        self.assertIn("#19", out.stderr)
        present = self.file(manifest.build(with_cases()))
        self.assertEqual(self.run_tool("validate", present, "--name", "v1.0.0-rc.1", "--go").returncode, 0)
        self.assertEqual(self.run_tool("validate", present, "--name", "v1.0.0-rc.2").returncode, 1)
        not_json = os.path.join(tempfile.mkdtemp(), "manifest.json")
        with open(not_json, "w") as f:
            f.write("{")
        out = self.run_tool("validate", not_json)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not JSON", out.stderr)

    def test_the_small_commands(self):
        self.assertEqual(self.run_tool("names", "v1.0.0-rc.1").stdout,
                         "image\tcolvenna-catalog-release/colvenna-catalog\tv1.0.0-rc.1\n"
                         "chart\tcolvenna-catalog-release/charts/colvenna-catalog\t1.0.0-rc.1\n")
        self.assertEqual(self.run_tool("names", "1.0.0").returncode, 1)
        self.assertEqual(self.run_tool("stack").stdout, "ColvennaCatalogRelease\n")
        self.assertEqual(self.run_tool("repositories").stdout,
                         "colvenna-catalog-release/colvenna-catalog\ncolvenna-catalog-release/charts/colvenna-catalog\n")
        self.assertEqual(self.run_tool("regions", "us-west-2,us-east-2").stdout, "us-west-2\nus-east-2\n")
        self.assertEqual(self.run_tool("regions", "us-west-2").returncode, 1)
        self.assertEqual(self.run_tool("forms", "eks,serverless").stdout, "eks\nserverless\n")
        for bad in ("eks,eks", "lambda", "eks,"):
            self.assertEqual(self.run_tool("forms", bad).returncode, 1, bad)
        self.assertEqual(self.run_tool("registry", "111122223333.dkr.ecr.us-east-2.amazonaws.com").stdout,
                         "111122223333\tus-east-2\n")
        self.assertEqual(self.run_tool("registry", "ghcr.io").returncode, 1)
        self.assertEqual(self.run_tool("paths").stdout.split(), list(manifest.PINNED_PATHS))
        path = self.file(manifest.build(inputs()))
        self.assertEqual(self.run_tool("get", path, "chart.digest").stdout, CHART + "\n")
        self.assertEqual(self.run_tool("get", path, "configurations.regions").stdout, '["us-west-2", "us-east-2"]\n')
        self.assertEqual(self.run_tool("get", path, "chart.nothing").returncode, 1)
        self.assertEqual(self.run_tool("bogus").returncode, 1)

    def test_a_candidate_s_manifest_or_a_release_s(self):
        """What install.sh --release reads of either: valid for the name given, the name, and the directories
        the tag adds to the commit it was built from."""
        candidate = manifest.build(with_cases(candidate="v1.0.0-rc.2"))
        rc, release = self.file(candidate), self.file(release_of(candidate))
        for path, name, added in ((rc, "v1.0.0-rc.2", "release/v1.0.0-rc.2\n"),
                                  (release, "v1.0.0", "release/v1.0.0-rc.2\nrelease/v1.0.0\n")):
            with self.subTest(name):
                self.assertEqual(self.run_tool("validate", path, "--name", name).returncode, 0)
                self.assertEqual(self.run_tool("validate", path, "--name", name, "--go").returncode, 0)
                self.assertEqual(self.run_tool("name", path).stdout, name + "\n")
                self.assertEqual(self.run_tool("added", path).stdout, added)
        for path, name, mine in ((rc, "v1.0.0", "v1.0.0-rc.2"), (release, "v1.0.0-rc.2", "v1.0.0"),
                                 (release, "v1.0.1", "v1.0.0")):
            with self.subTest(f"{mine}'s as {name}'s"):
                out = self.run_tool("validate", path, "--name", name)
                self.assertEqual(out.returncode, 1)
                self.assertIn(f"the manifest is '{mine}''s, not {name}'s", out.stderr)
        self.assertEqual(self.run_tool("kind", "v1.0.0").stdout, "release\n")
        self.assertEqual(self.run_tool("kind", "v1.0.0-rc.2").stdout, "candidate\n")
        self.assertEqual(self.run_tool("kind", "1.0.0").returncode, 1)

    def test_a_release_s_manifest_is_held_to_promotion_py(self):
        broken = release_of(manifest.build(with_cases(candidate="v1.0.0-rc.2")))
        broken["promotion"]["acceptance"]["verdict"] = "NO-GO"
        out = self.run_tool("validate", self.file(broken), "--name", "v1.0.0")
        self.assertEqual(out.returncode, 1)
        self.assertIn("promotion.acceptance.verdict: 'NO-GO', not GO", out.stderr)
        self.assertEqual(self.run_tool("name", self.file(broken)).returncode, 1)

    def test_notes_from_the_command_line(self):
        out = self.run_tool("notes", self.file(manifest.build(inputs())), stdin="{{candidate}} {{chart_digest}}\n")
        self.assertEqual(out.stdout, f"v1.0.0-rc.1 {CHART}\n")


class Consistency(unittest.TestCase):
    """The names manifest.py uses are the release stack's, the chart's and the tree's."""

    def test_the_release_stack_holds_the_repositories(self):
        stack = manifest.read(os.path.join(REPO, "deploy", "aws", "src", "release-stack.ts"))
        self.assertIn(f"'{manifest.PREFIX}'", stack)
        self.assertIn(manifest.IMAGE_REPOSITORY[len(manifest.PREFIX) + 1:], stack)
        self.assertIn(manifest.CHART_REPOSITORY[len(manifest.PREFIX) + 1:], stack)
        self.assertIn(f"'{manifest.STACK}'", manifest.read(os.path.join(REPO, "deploy", "aws", "src", "app.ts")))

    def test_the_one_chart(self):
        helm = os.path.join(REPO, "deploy", "helm")
        self.assertEqual(os.listdir(helm), [manifest.CHART_NAME])
        chart = manifest.read(os.path.join(REPO, manifest.CHART_PATH, "Chart.yaml"))
        self.assertRegex(chart, rf"(?m)^name: {manifest.CHART_NAME}$")

    def test_every_pinned_path_is_in_the_tree(self):
        for path in (*manifest.PINNED_PATHS, manifest.CDK_PATH, manifest.CHART_PATH):
            self.assertTrue(os.path.exists(os.path.join(REPO, path)), path)


if __name__ == "__main__":
    unittest.main()
