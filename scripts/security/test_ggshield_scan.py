"""Offline tests of the publication scan (ggshield_scan.py) and the ggshield hooks (ggshield_hook.py): what runs
without ggshield, GitHub or a registry (#113)."""
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ggshield_pin  # noqa: E402
import ggshield_scan  # noqa: E402


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def tar_bytes(members, mode="w:gz"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def stored_files(out):
    return sorted(path.read_bytes() for path in Path(out).rglob("*") if path.is_file())


class UnpackTest(unittest.TestCase):
    def test_nested_archives_are_opened_and_each_content_is_stored_once(self):
        lib = zip_bytes({"application.properties": b"catalog.mode = rest\n"})
        server = zip_bytes({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n", "lib/paimon-api.jar": lib})
        with tempfile.TemporaryDirectory() as work:
            first = Path(work, "layer-a.tar.gz")
            second = Path(work, "layer-b.tar.gz")
            first.write_bytes(tar_bytes({"opt/colvenna-catalog/colvenna-catalog.jar": server, "etc/motd": b"same\n"}))
            second.write_bytes(tar_bytes({"opt/colvenna-catalog/colvenna-catalog.jar": server, "etc/motd": b"same\n"}))
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([first, second], out)

            self.assertEqual(stored_files(out), [b"Manifest-Version: 1.0\n", b"catalog.mode = rest\n", b"same\n"])
            self.assertEqual(result.files, 6)
            self.assertEqual(result.distinct, 3)
            self.assertEqual(result.too_deep, 0)

    def test_member_names_never_place_a_file_outside_the_output(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work, "release.zip")
            inner = tar_bytes({"/etc/from-tar.txt": b"absolute\n", "../../tar-escape.txt": b"tar up\n"})
            source.write_bytes(zip_bytes({"../../zip-escape.txt": b"zip up\n", "bundle.tar.gz": inner}))
            out = Path(work, "nested", "unpacked")

            ggshield_scan.unpack([source], out)

            written = [path for path in Path(work).rglob("*") if path.is_file() and path != source]
            self.assertEqual(sorted(path.read_bytes() for path in written), [b"absolute\n", b"tar up\n", b"zip up\n"])
            for path in written:
                self.assertTrue(path.resolve().is_relative_to(out.resolve()), path)

    def test_a_directory_source_contributes_every_file_beneath_it(self):
        with tempfile.TemporaryDirectory() as work:
            build = Path(work, "build")
            (build / "chart").mkdir(parents=True)
            (build / "validation.log").write_bytes(b"checks passed\n")
            (build / "chart" / "colvenna-catalog-1.0.0.tgz").write_bytes(
                tar_bytes({"colvenna-catalog/values.yaml": b"replicas: 2\n"}))
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([build], out)

            self.assertEqual(stored_files(out), [b"checks passed\n", b"replicas: 2\n"])
            self.assertEqual(result.files, 2)

    def test_text_over_the_part_size_is_stored_in_overlapping_parts_and_gzip_is_opened(self):
        marker = "BEGIN-" + "x" * 30 + "-END"
        text = ("a" * 95 + marker + "b" * 160).encode()  # the marker crosses the first 100-character cut
        binary = bytes(range(256)) * 2  # not valid UTF-8, like a class file
        with tempfile.TemporaryDirectory() as work:
            Path(work, "openapi.json.gz").write_bytes(gzip.compress(text))
            Path(work, "Main.class").write_bytes(binary)
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([Path(work, "openapi.json.gz"), Path(work, "Main.class")], out,
                                          part_chars=100, overlap_chars=50)

            stored = stored_files(out)
            pieces = [data for data in stored if data != binary]
            self.assertIn(binary, stored)
            self.assertTrue(all(len(piece) <= 100 for piece in pieces))
            self.assertTrue(any(marker.encode() in piece for piece in pieces))
            self.assertGreater(len(pieces), 2)
            self.assertEqual(result.files, 2)

    def test_every_file_under_an_extension_ggshield_drops_gets_a_txt_name(self):
        # ggshield's own decoder then decides what is text: it reads more than UTF-8 (charset_normalizer).
        with tempfile.TemporaryDirectory() as work:
            Path(work, "logo.svg").write_bytes(b"<svg><!-- token: abc --></svg>\n")
            Path(work, "notes.pdf").write_bytes("café au lait\n".encode("cp1252"))
            Path(work, "photo.png").write_bytes(bytes(range(256)))
            Path(work, "broken.gz").write_bytes(b"\x1f\x8b\x08\x00" + bytes(range(256)))
            Path(work, "broken.zip").write_bytes(b"PK\x03\x04" + bytes(range(256)))
            Path(work, "values.yaml").write_bytes(b"replicas: 2\n")
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([Path(work)], out, skip_extensions={"svg", "pdf", "png", "gz", "zip"})

            names = sorted(path.name for path in out.rglob("*") if path.is_file())
            self.assertEqual(names, ["broken.gz.txt", "broken.zip.txt", "logo.svg.txt", "notes.pdf.txt",
                                     "photo.png.txt", "values.yaml"])
            self.assertEqual(result.files, 6)

    def test_each_part_records_the_line_it_starts_at(self):
        text = "".join(f"line {number}\n" for number in range(1, 41)).encode()
        with tempfile.TemporaryDirectory() as work:
            Path(work, "big.json").write_bytes(text)
            out = Path(work, "unpacked")

            ggshield_scan.unpack([Path(work, "big.json")], out, part_chars=100, overlap_chars=20)

            original = text.decode().splitlines()
            stored = sorted(out.rglob("big.json"))
            self.assertGreater(len(stored), 2)
            for part in stored:
                place, first = ggshield_scan.file_line(part, 1)
                self.assertTrue(original[first - 1].endswith(part.read_text().splitlines()[0]), part)
                self.assertEqual(ggshield_scan.file_line(part, 3)[1], first + 2)
                self.assertTrue(place.endswith("/big.json"), place)

    def test_a_name_that_ggshield_excludes_by_default_is_stored_under_a_neutral_name(self):
        with tempfile.TemporaryDirectory() as work:
            Path(work, "guide.mdx").write_bytes(b"token: abc\n")
            Path(work, "notes.md").write_bytes(b"plain\n")
            out = Path(work, "unpacked")

            ggshield_scan.unpack([Path(work)], out, excluded=lambda path: ".mdx" in path.name)

            names = sorted(path.name for path in out.rglob("*") if path.is_file())
            self.assertEqual(names, ["guide_mdx.txt", "notes.md"])

    def test_content_over_the_document_limit_that_is_not_utf8_is_decoded_or_counted_as_binary(self):
        # ggshield skips such content as "content is over" without reading it (over 4 MiB, before decoding):
        # ggshield's decoder (encoding) decides first. Text is stored as UTF-8 parts, binary content counted.
        original, ggshield_scan.DOCUMENT_BYTES = ggshield_scan.DOCUMENT_BYTES, 64
        self.addCleanup(setattr, ggshield_scan, "DOCUMENT_BYTES", original)
        latin = ("prix: 10 \u20ac, caf\u00e9\n" * 20).encode("cp1252")
        binary = bytes(range(256))
        with tempfile.TemporaryDirectory() as work:
            Path(work, "notes.txt").write_bytes(latin)
            Path(work, "libjvm.so").write_bytes(binary)
            Path(work, "small.bin").write_bytes(b"\xff\xfe\x00")
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([Path(work)], out, part_chars=100, overlap_chars=20,
                                          encoding=lambda data: "cp1252" if data == latin else None)

            stored = stored_files(out)
            self.assertNotIn(binary, stored)
            self.assertIn(b"\xff\xfe\x00", stored)  # under the limit: ggshield itself decides
            text = [data.decode("utf-8") for data in stored if data != b"\xff\xfe\x00"]
            self.assertGreater(len(text), 2)
            self.assertTrue(all("\u20ac" in part for part in text))
            self.assertEqual((result.files, result.distinct, result.large_binary), (3, 3, 1))

    def test_an_archive_nested_too_deep_is_counted_and_makes_the_scan_incomplete(self):
        archive = zip_bytes({"settings.properties": b"deep = 1\n"})
        for depth in range(ggshield_scan.MAX_DEPTH):
            archive = zip_bytes({f"level-{depth}.jar": archive})
        with tempfile.TemporaryDirectory() as work:
            Path(work, "outer.jar").write_bytes(archive)
            out = Path(work, "unpacked")

            result = ggshield_scan.unpack([Path(work, "outer.jar")], out)

            self.assertEqual(result.too_deep, 1)
            scan = ggshield_scan.Scan("packages", 0, {"type": "path_scan", "total_incidents": 0},
                                      unscanned=[("1 archives nested deeper than 6", "not opened")])
            text, clean = ggshield_scan.summarize([scan])
            self.assertFalse(clean)
            self.assertIn("  - not scanned: 1 archives nested deeper than 6 (not opened)", text)


def page(*pulls):
    return {"data": {"repository": {"pullRequests": {"nodes": [
        {"number": number, "timelineItems": {"pageInfo": {"hasNextPage": more}, "nodes": events}}
        for number, events, more in pulls]}}}}


class ForcePushedHeadsTest(unittest.TestCase):
    def test_lists_each_replaced_head_once_across_pages(self):
        pages = [
            page((3, [{"beforeCommit": {"oid": "a" * 40}}, {"beforeCommit": None}], False), (4, [], False)),
            page((9, [{"beforeCommit": {"oid": "d" * 40}}, {"beforeCommit": {"oid": "a" * 40}}], False)),
        ]

        self.assertEqual(ggshield_scan.force_pushed_heads(pages), ["a" * 40, "d" * 40])

    def test_a_pull_request_with_more_force_pushes_than_a_page_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            ggshield_scan.force_pushed_heads([page((7, [{"beforeCommit": {"oid": "b" * 40}}], True))])
        self.assertIn("pull request #7", str(refused.exception))


# A made-up value: the summary must never print what ggshield matched.
MATCHED = "s3cr3t-Value-0123456789abcdef"


def incident(detector, ignore_sha, line):
    return {"type": detector, "ignore_sha": ignore_sha, "validity": "no_checker", "total_occurrences": 1,
            "occurrences": [{"match": MATCHED, "type": "apikey", "line_start": line, "line_end": line,
                             "index_start": 10, "index_end": 39}]}


class SummarizeTest(unittest.TestCase):
    def test_no_incident_in_any_scope_is_clean(self):
        scans = [
            ggshield_scan.Scan("history", 0, {"type": "commit-range", "total_incidents": 0, "scans": []}),
            ggshield_scan.Scan("packages", 0, {"type": "path_scan", "total_incidents": 0,
                                               "entities_with_incidents": []}),
        ]

        text, clean = ggshield_scan.summarize(scans)

        self.assertTrue(clean)
        self.assertIn("history: 0 incidents", text)
        self.assertIn("packages: 0 incidents", text)
        self.assertTrue(text.rstrip().endswith("VERDICT: CLEAN"))

    def test_findings_are_located_without_their_values(self):
        history = {"type": "commit-range", "total_incidents": 1, "scans": [
            {"id": "c" * 40, "type": "commit", "total_incidents": 1, "entities_with_incidents": [
                {"mode": "FILE", "filename": "deploy/aws/functions/ops.ts", "total_incidents": 1,
                 "incidents": [incident("Generic High Entropy Secret", "1" * 64, 12)]}]}]}
        packages = {"type": "path_scan", "total_incidents": 1, "entities_with_incidents": [
            {"mode": "FILE", "filename": "/scan/unpacked/0123456789abcdef/values.yaml", "total_incidents": 1,
             "incidents": [incident("Generic Password", "2" * 64, 3)]}]}
        image = {"type": "scan_docker_archive", "total_incidents": 1, "entities_with_incidents": [
            {"mode": "FILE", "filename": "sha256:" + "e" * 64 + ":/etc/app.conf", "total_incidents": 1,
             "incidents": [incident("Generic Password", "3" * 64, 7)]}]}

        text, clean = ggshield_scan.summarize([ggshield_scan.Scan("history", 1, history),
                                               ggshield_scan.Scan("packages", 1, packages),
                                               ggshield_scan.Scan("image", 1, image)])

        self.assertFalse(clean)
        self.assertNotIn(MATCHED, text)
        self.assertIn("history: 1 incidents", text)
        self.assertIn("Generic High Entropy Secret, commit cccccccccccc, deploy/aws/functions/ops.ts:12, ignore_sha "
                      + "1" * 64, text)
        self.assertIn("Generic Password, 0123456789abcdef/values.yaml:3, ignore_sha " + "2" * 64, text)
        self.assertIn("Generic Password, sha256:" + "e" * 64 + ":/etc/app.conf:7, ignore_sha " + "3" * 64, text)
        self.assertTrue(text.rstrip().endswith("VERDICT: FINDINGS"))

    def test_an_incomplete_scan_is_not_clean_even_when_ggshield_exits_0(self):
        failed_chunk = ggshield_scan.Scan("history", 0, {"type": "commit-range", "total_incidents": 0, "scans": []},
                                          "Error: Scanning failed. Results may be incomplete.\n")
        report_errors = ggshield_scan.Scan("paths", 0, {"type": "path_scan", "total_incidents": 0,
                                                        "errors": [{"files": [], "description": "timeout"}]})
        nested_errors = ggshield_scan.Scan("wiki", 0, {"type": "commit-range", "total_incidents": 0, "scans": [
            {"id": "f" * 40, "type": "commit", "total_incidents": 0, "errors": [{"description": "timeout"}]}]})

        for scan in (failed_chunk, report_errors, nested_errors):
            text, clean = ggshield_scan.summarize([scan])
            self.assertFalse(clean, scan.scope)
            self.assertIn(f"{scan.scope}: incomplete", text)
            self.assertTrue(text.rstrip().endswith("VERDICT: FINDINGS"))

    def test_files_skipped_for_size_are_listed_and_binary_ones_only_counted(self):
        stderr = ("Skipped file:///scan/unpacked/0123456789abcdef/Main.class: can't detect encoding\n"
                  "Warning: Rate-limit hit, retrying in 1 seconds\n"
                  "Skipped file:///scan/unpacked/fedcba9876543210/big.json: content is over 1,048,576 bytes\n")
        binary_only = ggshield_scan.Scan("paths", 0, {"type": "path_scan", "total_incidents": 0},
                                         stderr.splitlines(keepends=True)[0])
        oversized = ggshield_scan.Scan("paths", 0, {"type": "path_scan", "total_incidents": 0}, stderr)

        text, clean = ggshield_scan.summarize([binary_only])
        self.assertTrue(clean)
        self.assertIn("paths: 0 incidents; 1 binary files not scanned (can't detect encoding)", text)

        text, clean = ggshield_scan.summarize([oversized])
        self.assertFalse(clean)
        self.assertIn("  - not scanned: fedcba9876543210/big.json (content is over 1,048,576 bytes)", text)

    def test_an_image_layer_skipped_as_scanned_before_is_not_clean(self):
        layer = "sha256:" + "b" * 64
        stderr = f"Scanning Docker config\nSkipping layer {layer}: already scanned\n"
        scan = ggshield_scan.Scan("image", 0, {"type": "scan_docker_archive", "total_incidents": 0}, stderr)

        text, clean = ggshield_scan.summarize([scan])

        self.assertFalse(clean)
        self.assertIn(f"  - not scanned: layer {layer} (ggshield's layer cache)", text)

    def test_paths_the_history_scan_excludes_by_default_are_listed_and_not_clean(self):
        scan = ggshield_scan.Scan("history", 0, {"type": "commit-range", "total_incidents": 0, "scans": []},
                                  unscanned=[("vendor/settings.py", "ggshield's default exclusions")])

        text, clean = ggshield_scan.summarize([scan])

        self.assertFalse(clean)
        self.assertIn("  - not scanned: vendor/settings.py (ggshield's default exclusions)", text)

    def test_a_failed_scan_is_not_clean_even_without_incidents(self):
        text, clean = ggshield_scan.summarize([ggshield_scan.Scan("history", 128, {})])

        self.assertFalse(clean)
        self.assertIn("history: ggshield exited 128", text)
        self.assertTrue(text.rstrip().endswith("VERDICT: FINDINGS"))


class ExclusionTest(unittest.TestCase):
    def test_history_paths_are_matched_unquoted_even_when_not_ascii(self):
        with tempfile.TemporaryDirectory() as work:
            git = ["git", "-C", work, "-c", "user.name=test", "-c", "user.email=test@example.com"]
            subprocess.run(["git", "init", "--quiet", work], check=True)
            for path in ("vendor/中文.py", "src/app.py"):
                Path(work, path).parent.mkdir(exist_ok=True)
                Path(work, path).write_text("x = 1\n")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "--quiet", "-m", "files"], check=True)

            found = ggshield_scan.excluded_history_paths(work, lambda path: path.startswith("vendor/"))

            self.assertEqual(found, [("vendor/中文.py", "ggshield's default exclusions")])

    def test_a_path_that_only_a_merge_commit_adds_is_matched(self):
        with tempfile.TemporaryDirectory() as work:
            def git(*args):
                subprocess.run(["git", "-C", work, "-c", "user.name=test", "-c", "user.email=test@example.com",
                                *args], check=True, capture_output=True)

            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", work], check=True)
            git("commit", "--quiet", "--allow-empty", "-m", "root")
            git("checkout", "--quiet", "-b", "side")
            Path(work, "b.txt").write_text("b\n")
            git("add", "b.txt")
            git("commit", "--quiet", "-m", "side")
            git("checkout", "--quiet", "main")
            Path(work, "a.txt").write_text("a\n")
            git("add", "a.txt")
            git("commit", "--quiet", "-m", "a")
            git("merge", "--quiet", "--no-ff", "--no-commit", "side")
            Path(work, "vendor").mkdir()
            Path(work, "vendor", "settings.py").write_text("x = 1\n")
            git("add", "vendor")
            git("commit", "--quiet", "-m", "merge, with a file of its own")

            found = ggshield_scan.excluded_history_paths(work, lambda path: path.startswith("vendor/"))

            self.assertEqual(found, [("vendor/settings.py", "ggshield's default exclusions")])


class OutputTest(unittest.TestCase):
    def test_an_output_directory_that_ggshield_would_exclude_is_refused(self):
        excluded = lambda path: "/vendor/" in str(path)  # noqa: E731
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(SystemExit) as refused:
                ggshield_scan.check_output(Path(work, "vendor", "scan"), excluded)
            self.assertIn("default exclusions", str(refused.exception))
            ggshield_scan.check_output(Path(work, "private", "scan"), excluded)

    def test_an_output_directory_in_a_git_work_tree_or_one_that_exists_is_refused(self):
        with tempfile.TemporaryDirectory() as work:
            subprocess.run(["git", "init", "--quiet", work], check=True)
            with self.assertRaises(SystemExit) as refused:
                ggshield_scan.check_output(Path(work, "reports", "scan"), lambda path: False)
            self.assertIn("inside a git work tree", str(refused.exception))
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(SystemExit) as refused:
                ggshield_scan.check_output(Path(work), lambda path: False)
            self.assertIn("exists", str(refused.exception))


def digest_of(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    """A stand-in for crane: a shell script that serves manifests and blobs by digest, and tags, from files."""

    def __init__(self, work):
        self.root = Path(work, "registry")
        self.root.mkdir()
        self.crane = Path(work, "crane")
        self.crane.write_text('#!/bin/sh\nroot="$(dirname "$0")/registry"\ncase "$1" in\n'
                              '  manifest | blob) exec cat "$root/${2##*@sha256:}" ;;\n'
                              '  digest) exec cat "$root/tag-${2##*:}" ;;\nesac\nexit 9\n')
        self.crane.chmod(0o755)

    def put(self, data):
        (self.root / digest_of(data)[7:]).write_bytes(data)
        return digest_of(data)

    def tag(self, tag, digest):
        (self.root / f"tag-{tag}").write_text(digest + "\n")

    def manifest(self, config_type, layers):
        config = json.dumps({"architecture": "amd64"}).encode()
        manifest = {"schemaVersion": 2, "config": {"mediaType": config_type, "digest": self.put(config)},
                    "layers": [{"mediaType": kind, "digest": self.put(data)} for kind, data in layers]}
        return self.put(json.dumps(manifest).encode())


class PackagesTest(unittest.TestCase):
    IMAGE = "application/vnd.oci.image.config.v1+json"
    LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.registry = Registry(self.work.name)
        self.out = Path(self.work.name, "packages")
        self.out.mkdir()

    def test_an_index_is_followed_and_each_blob_is_checked_and_named_to_be_opened(self):
        layer = tar_bytes({"opt/app/settings.yaml": b"mode: rest\n"})
        amd64 = self.registry.manifest(self.IMAGE, [(self.LAYER, layer)])
        arm64 = self.registry.manifest(self.IMAGE, [(self.LAYER, tar_bytes({"etc/arm": b"arm\n"}))])
        index = self.registry.put(json.dumps({"schemaVersion": 2, "manifests": [
            {"digest": amd64}, {"digest": arm64}]}).encode())

        held = ggshield_scan.fetch_artifact(str(self.registry.crane), "ghcr.io/example/app", index, self.out, set())

        self.assertEqual(held, [amd64, arm64])
        self.assertTrue((self.out / f"{digest_of(layer)[7:]}.tar.gz").is_file())
        self.assertEqual(len(list(self.out.glob("*.manifest.json"))), 3)
        stats = ggshield_scan.unpack([self.out], Path(self.work.name, "unpacked"))
        self.assertIn(b"mode: rest\n", stored_files(Path(self.work.name, "unpacked")))
        self.assertGreater(stats.distinct, 4)

    def test_a_chart_is_fetched_but_not_scanned_as_an_image(self):
        chart = tar_bytes({"colvenna-catalog/values.yaml": b"replicas: 2\n"})
        digest = self.registry.manifest("application/vnd.cncf.helm.config.v1+json",
                                        [("application/vnd.cncf.helm.chart.content.v1.tar+gzip", chart)])

        held = ggshield_scan.fetch_artifact(str(self.registry.crane), "ghcr.io/example/charts/app", digest, self.out,
                                            set())

        self.assertEqual(held, [])
        self.assertTrue((self.out / f"{digest_of(chart)[7:]}.tar.gz").is_file())

    def test_content_that_is_not_its_digest_is_refused(self):
        digest = self.registry.manifest(self.IMAGE, [(self.LAYER, tar_bytes({"etc/motd": b"hello\n"}))])
        layer = json.loads((self.registry.root / digest[7:]).read_bytes())["layers"][0]["digest"]
        (self.registry.root / layer[7:]).write_bytes(b"something else")

        with self.assertRaises(SystemExit) as refused:
            ggshield_scan.fetch_artifact(str(self.registry.crane), "ghcr.io/example/app", digest, self.out, set())
        self.assertIn("a blob of another digest", str(refused.exception))

        wrong = "sha256:" + "0" * 64
        (self.registry.root / wrong[7:]).write_bytes((self.registry.root / digest[7:]).read_bytes())
        with self.assertRaises(SystemExit) as refused:
            ggshield_scan.fetch_artifact(str(self.registry.crane), "ghcr.io/example/app", wrong, self.out, set())
        self.assertIn("a manifest of another digest", str(refused.exception))

    def test_a_layer_python_cannot_open_is_refused(self):
        self.assertEqual(ggshield_scan.blob_suffix("application/vnd.docker.image.rootfs.diff.tar.gzip"), ".tar.gz")
        self.assertEqual(ggshield_scan.blob_suffix("application/vnd.oci.image.layer.v1.tar"), ".tar")
        self.assertEqual(ggshield_scan.blob_suffix("application/vnd.cncf.helm.config.v1+json"), ".json")
        self.assertEqual(ggshield_scan.blob_suffix("application/vnd.cncf.helm.chart.provenance.v1.prov"), ".blob")
        with self.assertRaises(SystemExit):
            ggshield_scan.blob_suffix("application/vnd.oci.image.layer.v1.tar+zstd")
        digest = self.registry.manifest(self.IMAGE, [(self.LAYER, b"not a tar at all, though named one")])
        with self.assertRaises(SystemExit) as refused:
            ggshield_scan.fetch_artifact(str(self.registry.crane), "ghcr.io/example/app", digest, self.out, set())
        self.assertIn("is not a tar archive", str(refused.exception))

    def test_the_repositorys_packages_are_those_linked_to_it_or_named_after_it(self):
        packages = [{"name": "colvenna-catalog", "repository": "colvenna/colvenna-catalog"},
                    {"name": "charts/colvenna-catalog", "repository": None},
                    {"name": "tools", "repository": "colvenna/colvenna-catalog"},
                    {"name": "colvenna-lake", "repository": "colvenna/colvenna-lake"},
                    {"name": "charts/colvenna-lake", "repository": None}]

        self.assertEqual(ggshield_scan.repository_packages(packages, "colvenna/colvenna-catalog"),
                         ["colvenna-catalog", "charts/colvenna-catalog", "tools"])

    def test_only_sha256_digests_are_accepted(self):
        self.assertEqual(ggshield_scan.digest_hex("sha256:" + "a" * 64), "a" * 64)
        with self.assertRaises(SystemExit):
            ggshield_scan.digest_hex("sha512:" + "a" * 128)

    def test_a_tag_must_resolve_to_its_versions_digest(self):
        digest = self.registry.manifest(self.IMAGE, [(self.LAYER, tar_bytes({"etc/motd": b"hello\n"}))])
        self.registry.tag("1.0.0", "sha256:" + "f" * 64)
        listed = [("colvenna-catalog", digest, ["1.0.0"])]
        original, ggshield_scan.package_versions = ggshield_scan.package_versions, lambda repository: listed
        self.addCleanup(setattr, ggshield_scan, "package_versions", original)

        with self.assertRaises(SystemExit) as refused:
            ggshield_scan.fetch_packages("example/app", str(self.registry.crane), Path(self.work.name, "fetched"))
        self.assertIn("GitHub lists the tag", str(refused.exception))

        self.registry.tag("1.0.0", digest)
        images, described, digests = ggshield_scan.fetch_packages("example/app", str(self.registry.crane),
                                                                  Path(self.work.name, "fetched-again"))
        self.assertEqual(images, [f"ghcr.io/example/colvenna-catalog@{digest}"])
        self.assertEqual(described, [f"ghcr.io/example/colvenna-catalog@{digest} (1.0.0; 1 images)"])
        self.assertEqual(digests, {digest})


class ReleasesTest(unittest.TestCase):
    def test_each_release_but_no_candidate_is_matched_to_the_versions_scanned(self):
        def manifest(image, chart):
            return json.dumps({"image": {"digest": image}, "chart": {"digest": chart}})

        with tempfile.TemporaryDirectory() as work:
            git = ["git", "-C", work, "-c", "user.name=test", "-c", "user.email=test@example.com"]
            subprocess.run(["git", "init", "--quiet", work], check=True)
            for release, image, chart in (("v1.0.0", "sha256:" + "1" * 64, "sha256:" + "2" * 64),
                                          ("v1.0.1", "sha256:" + "3" * 64, "sha256:" + "4" * 64),
                                          ("v1.0.0-rc.1", "sha256:" + "5" * 64, "sha256:" + "6" * 64)):
                Path(work, "release", release).mkdir(parents=True)
                Path(work, "release", release, "manifest.json").write_text(manifest(image, chart))
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "--quiet", "-m", "releases"], check=True)
            sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

            lines = ggshield_scan.releases_on_ghcr(work, sha, {"sha256:" + "1" * 64, "sha256:" + "2" * 64,
                                                               "sha256:" + "3" * 64})

        self.assertEqual(lines, [
            f"release v1.0.0: image sha256:{'1' * 64} scanned, chart sha256:{'2' * 64} scanned",
            f"release v1.0.1: image sha256:{'3' * 64} scanned, chart sha256:{'4' * 64} NOT on GHCR"])


class HookTest(unittest.TestCase):
    """The hooks run ggshield only at the version ggshield-requirements.lock pins."""

    def run_hook(self, version, *args):
        with tempfile.TemporaryDirectory() as work:
            fake = Path(work, "ggshield")
            fake.write_text("#!/bin/sh\n"
                            f'if [ "$1" = --version ]; then echo "ggshield, version {version}"; exit 0; fi\n'
                            'echo "ran: $*"; exit 3\n')
            fake.chmod(0o755)
            return subprocess.run([sys.executable, str(HERE / "ggshield_hook.py"), *args], capture_output=True,
                                  text=True, env={**os.environ, "PATH": f"{work}:/usr/bin:/bin"})

    def test_the_pinned_version_scans_the_staged_changes_or_the_pushed_commits(self):
        self.assertEqual(ggshield_pin.pinned_version(), "1.56.1")  # the version whose messages the scan reads

        for mode in ("pre-commit", "pre-push"):
            done = self.run_hook(ggshield_pin.pinned_version(), mode)

            self.assertEqual((done.returncode, done.stdout), (3, f"ran: secret scan {mode}\n"))

    def test_another_version_is_refused_with_the_reason(self):
        done = self.run_hook("0.0.1", "pre-commit")

        self.assertEqual(done.returncode, 1)
        self.assertIn("needs ggshield", done.stderr)
        self.assertIn("found: 0.0.1", done.stderr)
        self.assertIn("SKIP=ggshield", done.stderr)

    def test_every_pin_of_the_lock_has_a_hash(self):
        pins = [line for line in (HERE / "ggshield-requirements.lock").read_text().splitlines()
                if line and not line.startswith("#")]

        self.assertTrue(pins)
        for pin in pins:
            self.assertRegex(pin, r"^[a-z0-9-]+==\S+( --hash=sha256:[0-9a-f]{64})+$")


if __name__ == "__main__":
    unittest.main()
