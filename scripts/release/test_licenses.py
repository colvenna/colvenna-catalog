"""Offline tests of the third-party record, THIRD_PARTY_NOTICES.md and NOTICE, against the tree, and
of licenses.py's parsers. What needs Maven (the jars' declared licenses, what the shaded jars bundle,
and which carry no license file) is the scan's, `licenses.py scan`; these check that the record was
scanned at the current pom.xml, that it names every dependency, package, image and tool the tree
pins, and that the server image's build context gives /licenses/ what the record's texts table names."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import licenses  # noqa: E402

# An image reference pinned by digest, as a script, a Dockerfile or a template writes it.
PINNED = re.compile(r"(?<![\w.:/@-])[a-z0-9][a-z0-9._-]*(:[0-9]+)?(/[a-z0-9._-]+)*(:[A-Za-z0-9._-]+)?"
                    r"@sha256:[0-9a-f]{64}(?![0-9a-f])")
RESCAN = ("run `python3 scripts/release/licenses.py scan --update` (Maven, JDK 21), review "
          "THIRD_PARTY_NOTICES.md's tables and findings, and commit them")


def read(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


def tracked():
    return subprocess.run(["git", "-C", REPO, "ls-files"], check=True, capture_output=True,
                          text=True).stdout.split()


class Record(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = licenses.record()
        cls.text = read("THIRD_PARTY_NOTICES.md")
        cls.notice = read("NOTICE")

    def test_the_java_table_was_scanned_at_this_pom(self):
        self.assertEqual(self.record["pom"], licenses.git_blob(licenses.POM),
                         "pom.xml changed since the license scan: " + RESCAN)

    def test_every_dependency_the_pom_declares_is_in_the_java_table(self):
        recorded = {(artifact, version) for artifact, version, _ in self.record["java"]}
        for artifact, version in licenses.pom_dependencies():
            self.assertTrue((artifact, version) in recorded, f"{artifact} {version}: " + RESCAN)

    def test_the_java_table_names_its_licenses_by_spdx(self):
        known = set(licenses.SPDX.values())
        for artifact, _, license in self.record["java"]:
            for part in license.split(" OR "):
                self.assertIn(part, known, f"{artifact}: {license}")

    def test_the_npm_table_is_the_lockfile(self):
        self.assertEqual(self.record["npm"], licenses.npm_packages(), "deploy/aws/package-lock.json: " + RESCAN)

    def test_the_shaded_jars_of_the_java_table_have_their_bundled_content_recorded(self):
        # The Paimon jars of the image bundle other projects (their NOTICE files list them).
        jars = {jar for jar, _, _ in self.record["bundled"]}
        for artifact, _, _ in self.record["java"]:
            if artifact.split(":", 1)[1] in ("paimon-s3", "paimon-format", "paimon-common"):
                self.assertIn(artifact.split(":", 1)[1], jars, artifact)

    def test_every_image_the_tree_pins_is_named(self):
        for path in tracked():
            if path.endswith(".md") or path.startswith(("release/", "docs/", "spikes/serverless/results/")):
                continue
            try:
                text = read(path)
            except (UnicodeDecodeError, IsADirectoryError):
                continue
            stages = set()
            for number, line in enumerate(text.splitlines(), 1):
                for m in PINNED.finditer(line):
                    reference = m[0].split("@", 1)[0]
                    self.assertTrue(f"`{reference}`" in self.text, f"{path}:{number}: {reference}")
                if os.path.basename(path) == "Dockerfile" and line.startswith("FROM "):
                    # FROM [--platform=...] <image> [AS <stage>]: an earlier stage, or a build argument
                    # (the test driver's BASE, the server's own image), is no third-party image.
                    words = [w for w in line.split()[1:] if not w.startswith("--")]
                    reference = words[0].split("@", 1)[0]
                    if len(words) == 3 and words[1].upper() == "AS":
                        stages.add(words[2])
                    if reference not in stages and not reference.startswith("$"):
                        self.assertTrue(f"`{reference}`" in self.text, f"{path}:{number}: {reference}")

    def test_the_load_balancer_controller_is_named_at_its_version(self):
        version = re.search(r"^LBC_CHART_VERSION=(\S+)$", read("tests/environment/lib.sh"), re.M)[1]
        self.assertTrue(f"chart {version} from" in self.text, version)
        self.assertTrue(f"`public.ecr.aws/eks/aws-load-balancer-controller:v{version}`" in self.text, version)

    def test_every_downloaded_tool_is_named_at_its_version(self):
        pins = re.findall(r"^(\w+)_version=(v[0-9.]+)$", read("scripts/install-tools.sh"), re.M)
        self.assertEqual({tool for tool, _ in pins}, {"kubectl", "helm", "crane"})
        for tool, version in pins:
            self.assertTrue(re.search(rf"\| `{tool}` \| `{tool}` {re.escape(version)} ", self.text), f"{tool} {version}")

    def test_every_jar_the_matrix_fetches_is_named_at_its_version(self):
        cells = json.loads(read("tests/compat/cells.json"))
        for name, jar in cells["jars"].items():
            parts = jar["path"].split("/")
            artifact, version = ".".join(parts[:-3]) + ":" + parts[-3], parts[-2]
            self.assertTrue(re.search(rf"^\| `{re.escape(artifact)}` \| [^|]*\b{re.escape(version)}\b", self.text, re.M),
                            f"{name}: {artifact} {version}")

    def test_every_pip_install_is_named(self):
        for path in tracked():
            if not path.endswith(".sh"):
                continue
            for line in read(path).splitlines():
                if re.search(r"\bpip3? install\b", line):
                    for name, version in re.findall(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.]+)", line):
                        self.assertTrue(f"`{name}=={version}`" in self.text, f"{path}: {name}")

    def test_the_notice_names_what_it_attributes(self):
        for path in ("LICENSE", "THIRD_PARTY_NOTICES.md", "Dockerfile", "deploy/aws/test-env/lbc-iam-policy.json"):
            self.assertTrue(path in self.notice, path)
            self.assertTrue(os.path.exists(os.path.join(REPO, path)), path)
        # The Lambda Web Adapter the image copies in, and every Apache Paimon jar of it, by the name it
        # has in the java table.
        if "aws-lambda-adapter" in read("Dockerfile"):
            self.assertTrue("AWS Lambda Web Adapter" in self.notice)
        for artifact, _, _ in self.record["java"]:
            if artifact.startswith("org.apache.paimon:"):
                self.assertTrue(artifact.split(":", 1)[1] in self.notice, artifact)
        # Where the image keeps it.
        self.assertTrue("/licenses/" in self.notice)


class ImageLicenses(unittest.TestCase):
    """The server's image (both forms run it) holds LICENSE, NOTICE, THIRD_PARTY_NOTICES.md and the
    texts table's license texts in /licenses/, as its build context gives them."""

    @classmethod
    def setUpClass(cls):
        cls.texts = licenses.texts_table()
        cls.java = licenses.record()["java"]
        cls.dockerfile = read("Dockerfile")

    def test_the_dockerfile_copies_into_licenses_what_the_record_names(self):
        self.assertEqual(licenses.copied_into_licenses(), licenses.image_licenses(self.texts),
                         "the Dockerfile's /licenses/, against THIRD_PARTY_NOTICES.md's texts table and licenses/")

    def test_the_server_s_stage_takes_the_licenses(self):
        # The final stage, the image install.sh, the release and Lambda run; the texts are made readable in
        # the build platform's stage that copies them.
        final = self.dockerfile.rsplit("\nFROM ", 1)[1]
        self.assertTrue(final.startswith("runtime\n"), final.split("\n", 1)[0])
        self.assertRegex(final, r"(?m)^COPY --from=licenses /licenses/ /licenses/$")
        self.assertRegex(self.dockerfile, r"(?m)^FROM --platform=\$BUILDPLATFORM \S+ AS licenses$")
        self.assertRegex(self.dockerfile, r"(?m)^RUN chmod -R u=rwX,go=rX /licenses$")

    def test_each_row_is_a_jar_of_the_java_table_or_an_image_the_dockerfile_copies_from(self):
        for component, version, license in self.texts:
            if ":" in component:
                self.assertTrue((component, version, license) in self.java,
                                f"{component} {version} {license}: not so in the java table")
            else:
                self.assertIn(f"FROM {component}:{version}@sha256:", self.dockerfile, component)

    def test_each_row_has_a_text_of_every_license_it_may_be_taken_under(self):
        for (component, _, license), names in self.texts.items():
            found = {licenses.recognized_license(read(f"{licenses.TEXTS_DIR}/{name}")) for name in names}
            for part in licenses.spdx_parts(license):
                self.assertIn(part, found, f"{component}: no text of {part} among {names}")

    def test_each_text_is_what_its_name_or_source_says(self):
        expected = {"Apache-2.0.txt": "Apache-2.0", "EPL-2.0.txt": "EPL-2.0", "LGPL-2.1-only.txt": "LGPL-2.1",
                    "reactive-streams.txt": "MIT-0", "zstd-jni.txt": "BSD-2-Clause", "lz4.txt": "BSD-2-Clause",
                    "snappy.txt": "BSD-3-Clause", "bitshuffle.txt": "MIT", "zstd.txt": "BSD-3-Clause", "logback.txt": None}
        names = {name for texts in self.texts.values() for name in texts}
        self.assertEqual(names, set(expected))
        for name, license in expected.items():
            self.assertEqual(licenses.recognized_license(read(f"{licenses.TEXTS_DIR}/{name}")), license, name)

    def test_each_text_s_source_is_recorded(self):
        notices = read("THIRD_PARTY_NOTICES.md")
        for name in {name for texts in self.texts.values() for name in texts}:
            self.assertRegex(notices, rf"(?m)^- `{re.escape(name)}`: .*\bat `[^`]+`|^- `{re.escape(name)}`: [^\n]*https://", name)

    def test_the_jars_of_the_texts_table_are_those_the_findings_name(self):
        named = re.search(r"\*\*Jars without license files\.\*\* \d+ of the image's \d+ jars carry no license or "
                          r"notice file under `META-INF/`: (.*?)\.", read("THIRD_PARTY_NOTICES.md"))[1]
        findings = set(re.findall(r"`([^`]+)`", named))
        rows = {licenses.artifact_id(c) for c, _, _ in self.texts if ":" in c}
        self.assertEqual(rows, findings)


class Parsers(unittest.TestCase):
    def test_a_third_party_list(self):
        text = """
Lists of 3 third-party dependencies.
     (Apache-2.0) Apache Commons IO (commons-io:commons-io:2.20.0 - https://commons.apache.org/proper/commons-io/)
     (Eclipse Public License - v 1.0) (GNU Lesser General Public License) Logback Classic Module (ch.qos.logback:logback-classic:1.5.21 - http://logback.qos.ch/logback-classic)
     (Apache License, Version 2.0) AWS Java SDK :: Core :: Protocols :: Json Utils (software.amazon.awssdk:json-utils:2.55.15 - https://aws.amazon.com/sdkforjava)
"""
        self.assertEqual(licenses.parse_third_party(text), {
            ("commons-io:commons-io", "2.20.0", "Apache-2.0"),
            ("ch.qos.logback:logback-classic", "1.5.21", "EPL-1.0 OR LGPL-2.1"),
            ("software.amazon.awssdk:json-utils", "2.55.15", "Apache-2.0")})

    def test_an_unknown_license_name_is_kept_as_written(self):
        self.assertEqual(licenses.spdx(["The New License"]), "The New License")
        self.assertEqual(licenses.spdx(["MIT", "The MIT License"]), "MIT")

    def test_what_a_notice_says_a_jar_bundles(self):
        text = """Apache Paimon
Copyright 2023-2026 The Apache Software Foundation

paimon-s3-impl
Copyright 2023-2025 The Apache Software Foundation

This project bundles the following dependencies under the Apache Software License 2.0 (http://www.apache.org/licenses/LICENSE-2.0.txt)
- org.apache.hadoop:hadoop-aws:3.4.2

This project bundles the following dependencies under the CDDL 1.1 license.
You find it under licenses/LICENSE.jaxb.

- javax.xml.bind:jaxb-api:2.3.1

paimon-hadoop-shaded
Copyright 2023-2025 The Apache Software Foundation

This project includes software developed at
The Apache Software Foundation (http://www.apache.org/).

- org.apache.hadoop:hadoop-common:3.4.2
- XML parsing from JetS3t - Copyright 2006-2009 James Murty.
"""
        self.assertEqual(licenses.bundled_from_notice("paimon-s3", text), {
            ("paimon-s3", "Apache Software License 2.0", "org.apache.hadoop:hadoop-aws:3.4.2"),
            ("paimon-s3", "CDDL 1.1 license", "javax.xml.bind:jaxb-api:2.3.1"),
            ("paimon-s3", "not stated", "org.apache.hadoop:hadoop-common:3.4.2")})

    def test_the_pom_s_dependencies(self):
        pom = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties><x.version>1.2</x.version><sdk.version>2.5</sdk.version></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>sdk</groupId><artifactId>bom</artifactId><version>${sdk.version}</version>
      <type>pom</type><scope>import</scope></dependency>
    <dependency><groupId>j</groupId><artifactId>pinned</artifactId><version>9</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>x</groupId><artifactId>core</artifactId><version>${x.version}</version></dependency>
    <dependency><groupId>sdk</groupId><artifactId>s3</artifactId></dependency>
    <dependency><groupId>j</groupId><artifactId>pinned</artifactId></dependency>
    <dependency><groupId>t</groupId><artifactId>junit</artifactId><version>5</version><scope>test</scope></dependency>
  </dependencies>
</project>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(pom)
        try:
            self.assertEqual(licenses.pom_dependencies(f.name), [("x:core", "1.2"), ("sdk:s3", "2.5"), ("j:pinned", "9")])
        finally:
            os.unlink(f.name)

    def test_the_lockfile_s_packages(self):
        lock = {"packages": {"": {"name": "app"},
                             "node_modules/a": {"version": "1.0.0", "license": "MIT"},
                             "node_modules/a/node_modules/b": {"version": "2.0.0", "license": "ISC", "inBundle": True},
                             "node_modules/@s/c": {"version": "3.0.0", "license": "Apache-2.0", "dev": True},
                             "node_modules/d": {"version": "4.0.0"}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(lock, f)
        try:
            self.assertEqual(licenses.npm_packages(f.name), {
                ("a", "1.0.0", "MIT", "dependency"), ("b", "2.0.0", "ISC", "bundled in a"),
                ("@s/c", "3.0.0", "Apache-2.0", "dev"), ("d", "4.0.0", "UNKNOWN", "dependency")})
        finally:
            os.unlink(f.name)

    def test_a_git_blob_id(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"hello\n")
        try:
            expected = subprocess.run(["git", "hash-object", f.name], check=True, capture_output=True,
                                      text=True).stdout.strip()
            self.assertEqual(licenses.git_blob(f.name), expected)
        finally:
            os.unlink(f.name)

    def test_update_writes_what_record_reads_and_differences_name_each_change(self):
        found = {"pom": "a" * 40, "jars": ["core-1.0.jar", "dual-2.0.jar"],
                 "java": {("g:core", "1.0", "Apache-2.0"), ("g:dual", "2.0", "EPL-1.0 OR LGPL-2.1")},
                 "bundled": {("core", "BSD license", "x:y:1"), ("core", "BSD license", "x:z:2")},
                 "npm": {("a", "1.0.0", "MIT", "dependency"), ("b", "2.0.0", "ISC", "bundled in a")}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notices.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Notices\n\n" + "\n\n".join(f"<!-- licenses:{n}:start -->\n<!-- licenses:{n}:end -->"
                                                      for n in ("java", "bundled", "npm")) + "\n")
            licenses.update(found, path)
            recorded = licenses.record(path)
        self.assertEqual(recorded, {k: v for k, v in found.items() if k != "jars"})
        self.assertEqual(licenses.differences(found, dict(recorded)), [])
        changed = dict(found, pom="b" * 40, java=found["java"] | {("g:new", "3.0", "MIT")},
                       jars=found["jars"] + ["new-3.0.jar", "stray.jar"])
        lines = licenses.differences(changed, recorded)
        self.assertIn("java: not in THIRD_PARTY_NOTICES.md: g:new | 3.0 | MIT", lines)
        self.assertIn("java: target/lib and the license list disagree on stray.jar", lines)
        self.assertTrue(any(line.startswith("pom: ") for line in lines), lines)
        self.assertEqual(len(lines), 3)

    def test_what_a_jar_keeps_only_the_pom_properties_of(self):
        files = {"META-INF/maven/org.apache.paimon/paimon-format/pom.properties": "version=2.0.0\n",
                 "META-INF/maven/org.apache.orc/orc-core/pom.properties": "groupId=org.apache.orc\nversion=1.9.2\n",
                 "META-INF/maven/commons-io/commons-io/pom.properties": "version=2.16.1\n",
                 "META-INF/maven/x/y/pom.properties": "artifactId=y\n"}
        listed = {("paimon-format", "Apache Software License 2.0", "org.apache.orc:orc-core:1.9.2")}
        self.assertEqual(licenses.bundled_from_pom_properties("paimon-format", "org.apache.paimon:paimon-format",
                                                              files, listed), {
            ("paimon-format", licenses.UNLISTED, "commons-io:commons-io:2.16.1"),
            ("paimon-format", licenses.UNLISTED, "x:y:UNKNOWN")})

    def test_what_a_jar_s_license_files_say_it_bundles(self):
        bsd2 = "Copyright (c) 2017 OnGres, Inc.\n\nRedistribution and use in source and binary forms, with or without"
        files = {"META-INF/licenses/com.ongres.scram/scram-client-3.2/META-INF/LICENSE": bsd2,
                 "META-INF/licenses/org.x/y-1.0.1/LICENSE.txt": "Permission is hereby granted, free of charge, ... "
                 "The above copyright notice and this permission notice shall be included in all copies",
                 "META-INF/licenses/LICENSE.antlr-runtime": "Neither the name ...",
                 "META-INF/LICENSE": "Apache License\nVersion 2.0"}
        self.assertEqual(licenses.bundled_from_license_files("postgresql", files), {
            ("postgresql", "BSD-2-Clause, by its license file", "com.ongres.scram:scram-client:3.2"),
            ("postgresql", "MIT, by its license file", "org.x:y:1.0.1")})
        self.assertEqual(licenses.license_of_text("Redistribution and use in source and binary forms ... Neither the name"),
                         "BSD-3-Clause, by its license file")
        self.assertEqual(licenses.license_of_text("Apache License\n Version 2.0, January 2004"), "Apache-2.0, by its license file")
        self.assertEqual(licenses.license_of_text("All rights reserved."), "a license file not recognized")

    def test_what_license_a_text_is(self):
        self.assertEqual(licenses.recognized_license("Eclipse Public License - v 2.0\n THE ACCOMPANYING PROGRAM"), "EPL-2.0")
        self.assertEqual(licenses.recognized_license("GNU LESSER GENERAL PUBLIC LICENSE\n Version 2.1, February 1999"), "LGPL-2.1")
        self.assertEqual(licenses.recognized_license("MIT No Attribution ... Permission is hereby granted, free of charge"), "MIT-0")
        # A statement naming licenses is not their text.
        self.assertIsNone(licenses.recognized_license("dual-licensed under the Eclipse Public License v2.0 or the GNU Lesser "
                                                      "General Public License version 2.1"))

    def test_the_licenses_an_expression_may_be_taken_under(self):
        self.assertEqual(licenses.spdx_parts("EPL-2.0 OR LGPL-2.1-only"), ["EPL-2.0", "LGPL-2.1"])
        self.assertEqual(licenses.spdx_parts("(MIT OR GPL-3.0-or-later)"), ["MIT", "GPL-3.0"])
        self.assertEqual(licenses.spdx_parts("Apache-2.0"), ["Apache-2.0"])

    def test_the_texts_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notices.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("<!-- licenses:texts:start -->\n\n| Component | Version | License | Texts |\n| --- | --- | --- | --- |\n"
                        "| `g:dual` | 2.0 | EPL-2.0 OR LGPL-2.1-only | `dual.txt`, `EPL-2.0.txt`, `LGPL-2.1-only.txt` |\n"
                        "| `registry.example.com/x` | 1.0 | Apache-2.0 | `Apache-2.0.txt` |\n\n<!-- licenses:texts:end -->\n")
            texts = licenses.texts_table(path)
        self.assertEqual(texts, {("g:dual", "2.0", "EPL-2.0 OR LGPL-2.1-only"): ("dual.txt", "EPL-2.0.txt", "LGPL-2.1-only.txt"),
                                 ("registry.example.com/x", "1.0", "Apache-2.0"): ("Apache-2.0.txt",)})
        self.assertEqual(licenses.image_licenses(texts), {
            "LICENSE": "LICENSE", "NOTICE": "NOTICE", "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
            "third-party/dual.txt": "licenses/dual.txt", "third-party/EPL-2.0.txt": "licenses/EPL-2.0.txt",
            "third-party/LGPL-2.1-only.txt": "licenses/LGPL-2.1-only.txt", "third-party/Apache-2.0.txt": "licenses/Apache-2.0.txt"})
        # The scan's check: a jar without license files needs a row; a row's jar must be one.
        found = {"java": {("g:dual", "2.0", "EPL-2.0 OR LGPL-2.1-only"), ("g:own", "1.0", "MIT"), ("g:bare", "3.0", "MIT")},
                 "without_license_files": ["bare-3.0.jar"]}
        self.assertEqual(licenses.text_differences(found, texts), [
            "texts: bare-3.0.jar carries no license or notice file, and the texts table gives it no text",
            "texts: dual-2.0.jar carries a license or notice file of its own; the texts table need not give it one"])
        found["without_license_files"] = ["dual-2.0.jar"]
        self.assertEqual(licenses.text_differences(found, texts), [])

    def test_what_a_dockerfile_copies_into_licenses(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "texts"))
            for name in ("texts/a.txt", "texts/b.txt", "LICENSE", "NOTICE"):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(name)
            with open(os.path.join(d, "Dockerfile"), "w", encoding="utf-8") as f:
                f.write("FROM x AS licenses\nCOPY LICENSE NOTICE /licenses/\ncopy --chmod=644 texts/ /licenses/third-party/\n"
                        "COPY LICENSE /opt/\nFROM y\nCOPY --from=licenses /licenses/ /licenses/\n")
            self.assertEqual(licenses.copied_into_licenses(os.path.join(d, "Dockerfile"), d), {
                "LICENSE": "LICENSE", "NOTICE": "NOTICE", "third-party/a.txt": "texts/a.txt", "third-party/b.txt": "texts/b.txt"})

    def test_an_image_s_licenses_against_the_record(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("LICENSE", b"own"), ("t/a.txt", b"a")):
                os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
                with open(os.path.join(d, name), "wb") as f:
                    f.write(text)
            expected = {"LICENSE": "LICENSE", "third-party/a.txt": "t/a.txt"}
            self.assertEqual(licenses.image_differences({"LICENSE": (0o644, b"own"), "third-party/a.txt": (0o644, b"a")},
                                                        {"": 0o755, "third-party": 0o755}, expected, d), [])
            self.assertEqual(licenses.image_differences(
                {"LICENSE": (0o600, b"own"), "third-party/a.txt": (0o644, b"changed"), "stray": (0o644, b"")},
                {"": 0o755, "third-party": 0o700}, dict(expected, NOTICE="NOTICE"), d), [
                "image: /licenses/NOTICE is missing (the record's NOTICE)",
                "image: /licenses/stray is not in the record",
                "image: /licenses/LICENSE is not readable by every user (mode 600)",
                "image: /licenses/third-party/a.txt is not t/a.txt as it is in the tree",
                "image: /licenses/third-party is a directory not every user can read (mode 700)"])


if __name__ == "__main__":
    unittest.main()
