"""Offline tests of bundled_sbom.py: the jars' NOTICE lines become SBOM components."""
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bundled_sbom  # noqa: E402

NOTICE = """Apache Paimon
Copyright 2023-2026 The Apache Software Foundation

This project bundles the following dependencies under the Apache Software License 2.0

- com.fasterxml.jackson.core:jackson-core:2.12.7
- org.apache.hadoop:hadoop-aws:3.4.2 (bundled in the plugin)
- not a coordinate
- org.yaml:snakeyaml
  - org.xerial.snappy:snappy-java:1.1.10.8
- io.netty:netty-codec:jar:4.1.100.Final
"""


class BundledSbomTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.lib = os.path.join(self.dir.name, "lib")
        os.mkdir(self.lib)

    def jar(self, name, entries):
        with zipfile.ZipFile(os.path.join(self.lib, name), "w") as jar:
            for entry, text in entries.items():
                jar.writestr(entry, text)

    def test_every_coordinate_line_of_every_notice_is_a_component(self):
        self.jar("paimon-s3-2.0.0.jar", {"META-INF/NOTICE": NOTICE, "paimon-plugin-s3/META-INF/NOTICE.txt": NOTICE})
        self.jar("plain-1.0.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"})
        rows = bundled_sbom.bundled(self.lib)
        self.assertEqual(
            sorted({(r[2], r[3], r[4]) for r in rows}),
            [("com.fasterxml.jackson.core", "jackson-core", "2.12.7"), ("io.netty", "netty-codec", "4.1.100.Final"),
             ("org.apache.hadoop", "hadoop-aws", "3.4.2"), ("org.xerial.snappy", "snappy-java", "1.1.10.8")])
        self.assertEqual({r[1] for r in rows}, {"META-INF/NOTICE", "paimon-plugin-s3/META-INF/NOTICE.txt"})
        self.assertEqual({r[0] for r in rows}, {"paimon-s3-2.0.0.jar"})

    def test_one_component_per_jar_and_coordinate_naming_the_jar(self):
        self.jar("a.jar", {"META-INF/NOTICE": NOTICE, "x/META-INF/NOTICE": NOTICE})
        self.jar("b.jar", {"META-INF/NOTICE": "- com.fasterxml.jackson.core:jackson-core:2.12.7\n"})
        document = bundled_sbom.sbom(bundled_sbom.bundled(self.lib))
        refs = [c["bom-ref"] for c in document["components"]]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(len(refs), 5)
        core = [c for c in document["components"] if c["name"] == "jackson-core"]
        self.assertEqual({c["properties"][0]["value"] for c in core}, {"a.jar", "b.jar"})
        self.assertEqual(core[0]["purl"], "pkg:maven/com.fasterxml.jackson.core/jackson-core@2.12.7")
        self.assertEqual(document["bomFormat"], "CycloneDX")

    def test_a_purl_version_is_percent_encoded(self):
        document = bundled_sbom.sbom([("a.jar", "META-INF/NOTICE", "g", "a", "1.0+build")])
        self.assertEqual(document["components"][0]["purl"], "pkg:maven/g/a@1.0%2Bbuild")

    def test_main_writes_the_sbom_and_the_table(self):
        self.jar("a.jar", {"META-INF/NOTICE": NOTICE})
        out, table = os.path.join(self.dir.name, "sbom.json"), os.path.join(self.dir.name, "rows.tsv")
        bundled_sbom.main(["bundled_sbom.py", self.lib, out, table])
        with open(out, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["components"]), 4)
        with open(table, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "jar\tnotice\tgroup\tartifact\tversion")
        self.assertEqual(len(lines), 5)


if __name__ == "__main__":
    unittest.main()
