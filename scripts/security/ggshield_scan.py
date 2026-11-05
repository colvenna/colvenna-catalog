#!/usr/bin/env python3
"""The publication scan (#113): ggshield, GitGuardian's command-line scanner, over everything the public can
fetch from the repository, with the .gitguardian.yaml of the scanned branch (main by default). Nothing is
made public, and no release is made, before it finds 0 (CONTRIBUTING.md#secret-scanning).

It scans:
  - history: an anonymous `git clone --mirror` (every branch, tag and refs/pull/*), plus each pull-request
    head that a force push replaced and GitHub still serves by its SHA; and the wiki, when it is enabled;
  - the GHCR packages of the repository (the image and the chart), every version: each manifest and blob
    fetched by its digest and checked against it, each tag checked to resolve to its version's digest.
    An image is scanned with `ggshield secret scan docker` too, with an empty layer cache;
  - release assets and workflow artifacts as published, the packages' blobs, and any local --path: each
    archive opened recursively (zip, jar, wheel, tar, gzip; the image's layers and the chart among them),
    each distinct file scanned once;
  - the GitHub text: issues and pull requests with their comments, reviews and review comments, release
    notes and commit comments.
Reports with matched values stay in --output, which must be private and outside any git work tree; the
summary holds none. It exits 0 only when every scan is complete and finds 0 incidents.

usage: scripts/security/ggshield_scan.py --output DIR [--repository OWNER/NAME] [--branch BRANCH]
                                         [--path PATH ...] [--ggshield BINARY] [--crane BINARY]
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ggshield_pin import require_pinned

ROOT = Path(__file__).resolve().parents[2]
ZIP_SUFFIXES = (".zip", ".whl", ".jar")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
MAX_DEPTH = 6
# ggshield skips a document over 1 MiB (its API's limit), and one whose JSON for the request is over
# 2.5 MiB (pygitguardian's MAXIMUM_PAYLOAD_SIZE; ggshield's secret_scanner.py, _document_payload_size), in
# which ASCII escaping takes a code point to at most 12 characters (a surrogate pair). Parts of 200,000
# characters stay under both: at most 800 kB of UTF-8, 2.4 MB of JSON. Parts overlap, so a value that
# crosses a cut is whole in one of them.
DOCUMENT_BYTES = 1_048_576
PART_CHARS = 200_000
OVERLAP_CHARS = 4_096


@dataclass
class Unpacked:
    files: int = 0
    distinct: int = 0
    large_binary: int = 0  # over DOCUMENT_BYTES and no text to ggshield's decoder: not sent, as ggshield would skip it
    too_deep: int = 0  # archives nested deeper than MAX_DEPTH, not opened: the scan is then incomplete


ARCHIVE_ERRORS = (OSError, EOFError, zlib.error, zipfile.BadZipFile, tarfile.TarError)


def unpack(sources, out, part_chars=PART_CHARS, overlap_chars=OVERLAP_CHARS, skip_extensions=frozenset(),
           excluded=lambda path: False, encoding=lambda data: None):
    """Open each source and every archive nested in it (zip, jar, wheel, tar, gzip); store each distinct file
    content once under out.
    - A text longer than part_chars is stored as overlapping parts, each in a folder that records its number
      and the line it starts in. Content that is not UTF-8 and over ggshield's document limit, which ggshield
      would skip unread, is decoded as encoding(data) finds (ggshield's decoder): text is stored in parts
      as UTF-8, and binary content is counted, not stored.
    - ggshield's path scan drops files by extension (skip_extensions) without reading them, so such a file
      gets ".txt" appended, and ggshield's decoder decides whether it is text.
    - A file ggshield would exclude by name (excluded, given the stored path) is stored under a neutral name.
    - A damaged archive is stored as a file."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result = Unpacked()
    seen = set()

    def expand(name, data, depth):
        """Visit the members of an archive; False when name is no archive this opens, or it is damaged."""
        lower = name.lower()
        if not lower.endswith((*ZIP_SUFFIXES, *TAR_SUFFIXES, ".gz")):
            return False
        if depth >= MAX_DEPTH:
            result.too_deep += 1
            return False
        try:
            if lower.endswith(ZIP_SUFFIXES) and zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    members = [(m.filename, archive.read(m)) for m in archive.infolist() if not m.is_dir()]
            elif lower.endswith(TAR_SUFFIXES):
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    members = [(m.name, archive.extractfile(m).read()) for m in archive.getmembers() if m.isfile()]
            elif lower.endswith(".gz"):
                members = [(name[:-3], gzip.decompress(data))]
            else:
                return False
        except ARCHIVE_ERRORS:
            return False
        for member, content in members:
            visit(member, content, depth + 1)
        return True

    def visit(name, data, depth):
        if expand(name, data, depth):
            return
        result.files += 1
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        result.distinct += 1
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None and len(data) > DOCUMENT_BYTES:
            found = encoding(data)
            if found is None:
                result.large_binary += 1
                return
            text = data.decode(found, errors="replace")
            data = text.encode()
        name = Path(name).name or "file"
        if Path(name).suffix.lower()[1:] in skip_extensions:
            name += ".txt"
        for index, (first_line, part) in enumerate(parts(data, text, part_chars, overlap_chars)):
            folder = out / (digest[:16] if index == 0 else f"{digest[:16]}.{index}.L{first_line}")
            folder.mkdir()
            target = folder / name
            if excluded(target):
                target = folder / (name.replace(".", "_") + ".txt")
            target.write_bytes(part)

    for source in map(Path, sources):
        for path in sorted(source.rglob("*")) if source.is_dir() else [source]:
            if path.is_file():
                visit(path.name, path.read_bytes(), 0)
    return result


def parts(data, text, part_chars, overlap_chars):
    """(first line, bytes) of data itself, or of windows of a text longer than part_chars that overlap by
    overlap_chars, as UTF-8."""
    if text is None or len(text) <= part_chars:
        return [(1, data)]
    step = part_chars - overlap_chars
    starts = range(0, len(text) - overlap_chars, step)
    return [(text.count("\n", 0, start) + 1, text[start:start + part_chars].encode()) for start in starts]


def file_line(path, line):
    """The stored file's place in its original: the content's folder and name, and the line there."""
    path = Path(path)
    digest, _, first = path.parent.name.partition(".L")
    return f"{digest}/{path.name}", line + int(first or 1) - 1


def pull_requests(page):
    return page["data"]["repository"]["pullRequests"]


def force_pushed_heads(pages):
    """The head SHA each force push in the GraphQL pages replaced, each once. Exits if a pull request has
    more force pushes than one page holds, which the query does not page through.

    GitHub keeps serving such a commit by its SHA once the repository is public, though no ref reaches it."""
    heads = []
    for page in pages:
        for pull in pull_requests(page)["nodes"]:
            if pull["timelineItems"]["pageInfo"]["hasNextPage"]:
                sys.exit(f"pull request #{pull['number']} has more force pushes than the scan pages through")
            for event in pull["timelineItems"]["nodes"]:
                sha = (event.get("beforeCommit") or {}).get("oid")
                if sha and sha not in heads:
                    heads.append(sha)
    return heads


@dataclass
class Scan:
    scope: str
    returncode: int
    report: dict
    stderr: str = ""
    unscanned: list = field(default_factory=list)  # (what, why) of what the scan left out, beyond ggshield's report


BINARY_SKIP = "can't detect encoding"
# `ggshield secret scan docker` remembers each layer it found clean and skips it in a later scan.
CACHED_LAYER = re.compile(r"^Skipping layer (\S+): already scanned$", re.M)


def skipped(stderr):
    """(file, reason) of each item ggshield skipped, from its "Skipped <url>: <reason>" lines."""
    for line in stderr.splitlines():
        if line.startswith("Skipped "):
            url, _, reason = line[len("Skipped "):].rpartition(": ")
            yield url.split("://", 1)[-1], reason


def has_errors(report):
    return bool(report.get("errors")) or any(has_errors(inner) for inner in report.get("scans") or [])


def located(filename, line):
    """A finding's file and line: as ggshield names it for a commit or an image layer, else as file_line
    places it."""
    return (filename, line) if not Path(filename).is_absolute() else file_line(filename, line)


def problems(scan):
    """Why a scan is not complete: ggshield failed, reported errors, skipped a file that is not binary or an
    image layer it had scanned before, or something was left out before it ran (unscanned)."""
    if scan.returncode not in (0, 1) or "total_incidents" not in scan.report:
        return [f"{scan.scope}: ggshield exited {scan.returncode}"]
    found = []
    if has_errors(scan.report) or "Scanning failed" in scan.stderr:
        found.append(f"{scan.scope}: incomplete, ggshield reported a failed scan (see its report)")
    found += [f"  - not scanned: {located(filename, 1)[0]} ({reason})"
              for filename, reason in skipped(scan.stderr) if reason != BINARY_SKIP]
    found += [f"  - not scanned: layer {layer} (ggshield's layer cache)" for layer in CACHED_LAYER.findall(scan.stderr)]
    found += [f"  - not scanned: {what} ({why})" for what, why in scan.unscanned]
    return found


def findings(scan):
    """Each incident's detector, commit (for a history), file, line and ignore SHA; never its value."""
    commits = scan.report.get("scans")
    for commit in commits if commits is not None else [scan.report]:
        where = f"commit {commit['id'][:12]}, " if commits is not None else ""
        for entity in commit.get("entities_with_incidents") or []:
            for found in entity["incidents"]:
                line = found["occurrences"][0]["line_start"] if found["occurrences"] else 1
                place, line = located(entity["filename"], line)
                yield f"{found['type']}, {where}{place}:{line}, ignore_sha {found['ignore_sha']}"


def summarize(scans):
    """A report of each scan that locates every incident without its value, and whether all are clean.

    A scan is clean when ggshield exits 0, reports no incident and no error, says nothing failed, and skips
    nothing but binary files (which it cannot decode)."""
    lines, clean = [], True
    for scan in scans:
        trouble = problems(scan)
        if "total_incidents" in scan.report:
            binary = sum(reason == BINARY_SKIP for _, reason in skipped(scan.stderr))
            lines.append(f"{scan.scope}: {scan.report['total_incidents']} incidents"
                         + (f"; {binary} binary files not scanned ({BINARY_SKIP})" if binary else ""))
            clean = clean and scan.returncode == 0 and scan.report["total_incidents"] == 0
        lines += trouble + [f"  - {item}" for item in findings(scan)]
        clean = clean and not trouble
    lines.append("VERDICT: CLEAN" if clean else "VERDICT: FINDINGS")
    return "\n".join(lines) + "\n", clean


FORCE_PUSHES = """query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number timelineItems(first: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {
        pageInfo { hasNextPage }
        nodes { ... on HeadRefForcePushedEvent { beforeCommit { oid } } } } }
    }
  }
}"""
ANONYMOUS_GIT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never prompt; no credential helper either
GIT = ["git", "-c", "credential.helper="]


def exit_if_failed(command, done, failure=None):
    """Exit with failure, or the command and its stderr, if the finished command failed."""
    if done.returncode:
        error = done.stderr if isinstance(done.stderr, str) else done.stderr.decode(errors="replace")
        sys.exit(failure or f"{' '.join(command[:4])} ... failed ({done.returncode}): {error.strip()}")


def run(command, failure=None, text=True, **options):
    """The command's output; on failure, exit with failure (or the command and its stderr)."""
    done = subprocess.run(command, capture_output=True, text=text, **options)
    exit_if_failed(command, done, failure)
    return done.stdout


def download(command, target):
    """Write the command's output to target; on failure, exit with the command and its stderr."""
    with open(target, "wb") as stream:
        done = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE)
    exit_if_failed(command, done)


@dataclass
class GgshieldRules:
    """How ggshield 1.56.1 decides what to leave out without saying so, read from the Python of ggshield's
    environment: the extensions it drops, its default exclusions, and its decoder."""
    python: str
    skip_extensions: set  # the extensions its path scan drops unread
    patterns: list  # its default exclusions, which apply to every scan, history included

    def excluded(self, path):
        return any(pattern.search(PurePosixPath(path).as_posix()) for pattern in self.patterns)

    def encoding(self, data):
        """The encoding ggshield's decoder (charset_normalizer) finds in data, or None: then it is binary."""
        code = "import sys, charset_normalizer\nm = charset_normalizer.from_bytes(sys.stdin.buffer.read()).best()\n" \
               "print(m.encoding if m else '')"
        return run([self.python, "-I", "-c", code], input=data, text=False).decode().strip() or None


def ggshield_rules(binary):
    """The GgshieldRules of the ggshield at binary. --ignore-default-excludes is accepted but has no effect
    in 1.56.1, so the default exclusions cannot be turned off."""
    folder = Path(shutil.which(binary) or binary).resolve().parent
    code = ("import json\n"
            "from ggshield.cmd.secret.scan.secret_scan_common_options import IGNORED_DEFAULT_WILDCARDS as W\n"
            "from ggshield.core.filter import init_exclusion_regexes\n"
            "from ggshield.utils._binary_extensions import BINARY_EXTENSIONS as B\n"
            "print(json.dumps([sorted(B), sorted(r.pattern for r in init_exclusion_regexes(set(W)))]))")
    for python in (folder / "python3", folder / "python"):
        done = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True) \
            if python.exists() else None
        if done and done.returncode == 0:
            extensions, patterns = json.loads(done.stdout)
            return GgshieldRules(str(python), set(extensions), [re.compile(pattern) for pattern in patterns])
    sys.exit(f"cannot read what ggshield leaves out: no Python beside {folder / Path(binary).name} imports ggshield")


def excluded_history_paths(clone, excluded):
    """(path, why) of each path any commit of the clone touches that ggshield's default exclusions skip:
    unquoted, so that a non-ASCII path is matched as ggshield sees it, and merge commits' paths included
    (-m: against each parent, a superset of the combined diff ggshield scans)."""
    listing = run([*GIT, "-C", str(clone), "-c", "core.quotePath=false", "log", "-m", "-z", "--all", "--format=",
                   "--name-only"])
    paths = {path.strip("\n") for path in listing.split("\0")} - {""}
    return [(path, "ggshield's default exclusions") for path in sorted(paths) if excluded(path)]


def check_output(out, excluded):
    """Refuse an output directory that already exists, that is inside a git work tree (this repository's
    among them: the reports hold the matched values, and ggshield adds its cache file to the .gitignore of
    the work tree it runs in), or that is under a path ggshield's default exclusions match: its path scans
    match them against the absolute path, so every file there would be dropped without notice."""
    out = Path(out)
    if out.exists():
        sys.exit(f"{out} exists: give a new directory")
    if excluded(out / "probe" / "file.txt"):
        sys.exit(f"{out} is under a path ggshield's default exclusions skip (such as vendor/ or .venv/): "
                 "choose another output directory")
    parent = next(folder for folder in out.parents if folder.is_dir())
    inside = subprocess.run(["git", "-C", str(parent), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                            text=True).stdout.strip()
    if inside == "true":
        sys.exit(f"{out} is inside a git work tree: choose a private directory outside it")


def slug(scope):
    return re.sub(r"[^A-Za-z0-9.@_-]+", "-", scope)


def owner_and_name(repository):
    """The owner and the name of a repository given as OWNER/NAME."""
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        sys.exit(f"{repository}: give the repository as OWNER/NAME")
    return owner, name


def mirror(url, clone, failure=None):
    run([*GIT, "clone", "--quiet", "--mirror", url, str(clone)], failure=failure, env=ANONYMOUS_GIT)
    run([*GIT, "-C", str(clone), "remote", "set-url", "--push", "origin", "no-push://read-only"])


def fetch_force_pushed(repository, clone):
    """Fetch into the clone each replaced pull-request head GitHub still serves; count them by outcome."""
    owner, name = owner_and_name(repository)
    pages, cursor = [], None
    while True:
        args = ["gh", "api", "graphql", "-f", "query=" + FORCE_PUSHES, "-F", "owner=" + owner, "-F", "name=" + name]
        pages.append(json.loads(run(args + (["-F", "cursor=" + cursor] if cursor else []))))
        info = pull_requests(pages[-1])["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
    status = {"fetched": 0, "still reachable": 0, "not served": 0}
    git = [*GIT, "-C", str(clone)]
    for sha in force_pushed_heads(pages):
        if subprocess.run([*git, "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0:
            status["still reachable"] += 1
        elif subprocess.run([*git, "fetch", "--quiet", "origin", f"{sha}:refs/force-pushed/{sha}"],
                            capture_output=True, env=ANONYMOUS_GIT).returncode == 0:
            status["fetched"] += 1
        else:
            status["not served"] += 1
    return status


def describe_refs(clone):
    refs = run([*GIT, "-C", str(clone), "for-each-ref", "--format=%(refname)"]).split()
    count = {kind: sum(ref.startswith(f"refs/{kind}/") for ref in refs) for kind in ("heads", "tags", "pull")}
    commits = run([*GIT, "-C", str(clone), "rev-list", "--all", "--count"]).strip()
    return f"{count['heads']} branches, {count['tags']} tags, {count['pull']} refs/pull; {commits} commits"


def download_releases(repository, out):
    out.mkdir(parents=True)
    tags = run(["gh", "api", "--paginate", f"repos/{repository}/releases?per_page=100",
                "--jq", ".[] | select(.assets | length > 0) | .tag_name"])
    for tag in tags.split():
        (out / tag).mkdir()
        run(["gh", "release", "download", tag, "-R", repository, "-D", str(out / tag)])


def download_workflow_artifacts(repository, out):
    out.mkdir(parents=True)
    artifact_ids = run(["gh", "api", "--paginate", f"repos/{repository}/actions/artifacts?per_page=100",
                        "--jq", ".artifacts[] | select(.expired | not) | .id"])
    for artifact_id in artifact_ids.split():
        download(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"], out / f"{artifact_id}.zip")


GITHUB_TEXT = [  # (API path, jq that emits one {name, text} object a line)
    ("issues?state=all&per_page=100",
     '.[] | {name: "\\(if .pull_request then "pull" else "issue" end)-\\(.number).md", '
     'text: "\\(.title)\\n\\n\\(.body // "")"}'),
    ("issues/comments?per_page=100", '.[] | {name: "comment-\\(.id).md", text: (.body // "")}'),
    ("pulls/comments?per_page=100",
     '.[] | {name: "review-comment-\\(.id).md", text: "\\(.diff_hunk // "")\\n\\n\\(.body // "")"}'),
    ("releases?per_page=100", '.[] | {name: "release-\\(.tag_name).md", text: "\\(.name // "")\\n\\n\\(.body // "")"}'),
    ("comments?per_page=100", '.[] | {name: "commit-comment-\\(.id).md", text: (.body // "")}'),
]


def download_github_text(repository, out):
    """Write each issue, pull request, comment, review, release note and commit comment to its own file."""
    out.mkdir(parents=True)
    documents = []
    for path, jq in GITHUB_TEXT:
        documents += run(["gh", "api", "--paginate", f"repos/{repository}/{path}", "--jq", jq]).splitlines()
    pulls = run(["gh", "api", "--paginate", f"repos/{repository}/pulls?state=all&per_page=100",
                 "--jq", ".[].number"]).split()
    for number in pulls:
        documents += run(["gh", "api", "--paginate", f"repos/{repository}/pulls/{number}/reviews?per_page=100",
                          "--jq", f'.[] | select(.body != "") | {{name: "pull-{number}-review-\\(.id).md", '
                                  'text: .body}']).splitlines()
    for line in documents:
        document = json.loads(line)
        (out / document["name"]).write_text(document["text"], encoding="utf-8")
    return len(documents)


def repository_packages(packages, repository):
    """The names of the packages ({name, repository}) that are the repository's: linked to it, or named after
    it (<name>, charts/<name>) though not linked."""
    name = owner_and_name(repository)[1]
    return [package["name"] for package in packages
            if package["repository"] == repository or package["name"] in (name, f"charts/{name}")]


def package_versions(repository):
    """(package, version digest, tags) of every version of each GHCR container package of the repository,
    tagged or not (anyone can pull a public package's version by its digest)."""
    owner = owner_and_name(repository)[0]
    listed = run(["gh", "api", "--paginate", f"orgs/{owner}/packages?package_type=container&per_page=100",
                  "--jq", ".[] | {name, repository: .repository.full_name}"])
    for package in repository_packages([json.loads(line) for line in listed.splitlines()], repository):
        path = f"orgs/{owner}/packages/container/{urllib.parse.quote(package, safe='')}/versions?per_page=100"
        for line in run(["gh", "api", "--paginate", path, "--jq",
                         '.[] | "\\(.name) \\(.metadata.container.tags | join(","))"']).splitlines():
            digest, _, tags = line.partition(" ")
            yield package, digest, [tag for tag in tags.split(",") if tag]


def blob_suffix(media_type):
    """The name a blob is stored under, so that unpack() opens a layer: by the blob's media type."""
    if "zstd" in media_type:
        sys.exit(f"a blob of media type {media_type}: Python cannot open zstd, so it cannot be scanned")
    if media_type.endswith(("tar+gzip", "tar.gzip")):
        return ".tar.gz"
    if media_type.endswith("tar"):
        return ".tar"
    return ".json" if media_type.endswith("json") else ".blob"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_hex(digest):
    """The hexadecimal part of an OCI digest, "sha256:<hex>", which names a stored blob."""
    algorithm, _, hexadecimal = digest.partition(":")
    if algorithm != "sha256":
        sys.exit(f"{digest}: only sha256 digests are checked")
    return hexadecimal


IMAGE_CONFIGS = ("application/vnd.docker.container.image.v1+json", "application/vnd.oci.image.config.v1+json")


def fetch_artifact(crane, image, digest, out, seen):
    """Fetch the manifest at digest and each blob it names into out, each checked against its digest, and
    recurse into an index. Returns the digests of the image manifests (those Docker can pull) it held."""
    if digest in seen:
        return []
    seen.add(digest)
    data = run([crane, "manifest", f"{image}@{digest}"], text=False)
    expected = digest_hex(digest)
    if hashlib.sha256(data).hexdigest() != expected:
        sys.exit(f"{image}@{digest}: the registry served a manifest of another digest")
    (out / f"{expected}.manifest.json").write_bytes(data)
    manifest = json.loads(data)
    if "manifests" in manifest:
        return [found for child in manifest["manifests"]
                for found in fetch_artifact(crane, image, child["digest"], out, seen)]
    for blob in [manifest["config"], *manifest.get("layers", [])]:
        expected = digest_hex(blob["digest"])
        target = out / (expected + blob_suffix(blob.get("mediaType", "")))
        if not target.exists():
            download([crane, "blob", f"{image}@{blob['digest']}"], target)
            if sha256_file(target) != expected:
                sys.exit(f"{image}@{blob['digest']}: the registry served a blob of another digest")
            if target.name.endswith(TAR_SUFFIXES) and not tarfile.is_tarfile(target):
                sys.exit(f"{image}@{blob['digest']}: a layer of media type {blob['mediaType']} is not a tar archive")
    layers = [layer.get("mediaType", "") for layer in manifest.get("layers", [])]
    is_image = manifest["config"].get("mediaType") in IMAGE_CONFIGS and all("tar" in kind for kind in layers)
    return [digest] if is_image else []


def fetch_packages(repository, crane, out):
    """Download every version of each GHCR package of the repository into out. Returns the image manifests
    to scan with Docker, a line for the summary per version, and the versions' digests."""
    out.mkdir(parents=True)
    owner = owner_and_name(repository)[0]
    images, described, digests = [], [], set()
    for name, digest, tags in package_versions(repository):
        image = f"ghcr.io/{owner}/{name}".lower()
        for tag in tags:
            served = run([crane, "digest", f"{image}:{tag}"]).strip()
            if served != digest:
                sys.exit(f"{image}:{tag} is {served}, but GitHub lists the tag on {digest}")
        image_digests = fetch_artifact(crane, image, digest, out, set())
        images += [f"{image}@{found}" for found in image_digests]
        digests.add(digest)
        described.append(f"{image}@{digest} ({', '.join(tags) or 'untagged'}; {len(image_digests)} images)")
    return images, described, digests


def releases_on_ghcr(clone, sha, digests):
    """For each release the branch at sha records (release/v<x.y.z>/manifest.json; not a candidate's), whether
    the image and the chart it names, by digest, are among the GHCR versions scanned."""
    names = run([*GIT, "-C", str(clone), "ls-tree", "--name-only", f"{sha}:release"],
                failure=f"{sha} has no release/ directory").split()
    lines = []
    for release in sorted(name for name in names if re.fullmatch(r"v\d+\.\d+\.\d+", name)):
        manifest = json.loads(run([*GIT, "-C", str(clone), "show", f"{sha}:release/{release}/manifest.json"]))
        state = {part: "scanned" if manifest[part]["digest"] in digests else "NOT on GHCR"
                 for part in ("image", "chart")}
        lines.append(f"release {release}: image {manifest['image']['digest']} {state['image']}, "
                     f"chart {manifest['chart']['digest']} {state['chart']}")
    return lines


@dataclass
class Ggshield:
    """ggshield at binary, with the configuration config, writing its reports into out."""
    binary: str
    config: Path
    out: Path

    def scan(self, scope, *command, env=None, unscanned=()):
        report = self.out / f"{slug(scope)}.json"
        with open(report, "wb") as stdout, open(report.with_suffix(".stderr"), "wb") as stderr:
            # In out, so that ggshield's cache file (.cache_ggshield, which holds findings) stays there.
            returncode = subprocess.run([self.binary, "--config-path", str(self.config), "secret", "scan", *command,
                                         "--json"], stdout=stdout, stderr=stderr, cwd=self.out, env=env).returncode
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        stderr = report.with_suffix(".stderr").read_text(encoding="utf-8", errors="replace")
        return Scan(scope, returncode, data, stderr, list(unscanned))

    def docker(self, image):
        """`ggshield secret scan docker` of an image, with an empty layer cache (GG_CACHE_DIR). It reads only
        the layers that COPY or ADD made, and skips system paths (usr/, lib/, ...) and binary files, the jars
        among them: unpack() covers all of those."""
        if not shutil.which("docker"):
            sys.exit(f"Docker is needed to scan {image} with ggshield secret scan docker")
        present = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
        cache = self.out / "ggshield-cache" / slug(image)
        cache.mkdir(parents=True)
        scan = self.scan(f"image {image}", "docker", image, env={**os.environ, "GG_CACHE_DIR": str(cache)})
        if not present:
            subprocess.run(["docker", "image", "rm", image], capture_output=True)
        return scan

    def quota(self):
        done = subprocess.run([self.binary, "quota"], capture_output=True, text=True)
        return done.stdout.splitlines()[0] if done.returncode == 0 and done.stdout else "unavailable"


def default_crane():
    tools = ROOT / ".tools" / "crane"  # scripts/install-tools.sh
    return str(tools) if tools.exists() else shutil.which("crane") or "crane"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True, help="a new private directory for the reports")
    parser.add_argument("--repository", default="colvenna/colvenna-catalog")
    parser.add_argument("--branch", default="main", help="the branch whose .gitguardian.yaml applies")
    parser.add_argument("--path", type=Path, action="append", default=[], help="a local file or directory")
    parser.add_argument("--ggshield", default=shutil.which("ggshield") or "ggshield")
    parser.add_argument("--crane", default=default_crane(), help="crane (default: scripts/install-tools.sh's)")
    args = parser.parse_args(argv)

    version = require_pinned(args.ggshield, "the scan")  # its completeness checks read this version's messages
    rules = ggshield_rules(args.ggshield)
    out = args.output.resolve()  # ggshield reports the paths it was given; file_line() maps absolute ones
    check_output(out, rules.excluded)
    out.mkdir(mode=0o700, parents=True)
    ggshield = Ggshield(args.ggshield, out / "gitguardian.yaml", out)
    header = [f"date: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
              f"repository: {args.repository}", f"ggshield: {version}", f"quota before: {ggshield.quota()}"]

    clone = out / "mirror.git"
    mirror(f"https://github.com/{args.repository}.git", clone)
    pushed = fetch_force_pushed(args.repository, clone)
    header.append(f"refs: {describe_refs(clone)}; force-pushed heads: "
                  + ", ".join(f"{number} {outcome}" for outcome, number in pushed.items()))
    branch_sha = run([*GIT, "-C", str(clone), "rev-parse", "--verify", f"refs/heads/{args.branch}"],
                     failure=f"the repository has no branch {args.branch}").strip()
    ggshield.config.write_text(run([*GIT, "-C", str(clone), "show", f"{branch_sha}:.gitguardian.yaml"],
                                   failure=f"{args.branch} has no .gitguardian.yaml: the scan uses the repository's "
                                           "own configuration"))
    header.append(f"configuration: .gitguardian.yaml of {args.branch} {branch_sha}")

    scans = [ggshield.scan("history", "repo", str(clone), unscanned=excluded_history_paths(clone, rules.excluded))]
    settings = json.loads(run(["gh", "api", f"repos/{args.repository}"]))
    if settings["has_discussions"]:
        sys.exit("the repository's discussions are enabled, and the scan does not fetch them: disable them, or "
                 "extend the scan")
    if settings["has_wiki"]:
        wiki = out / "wiki.git"
        mirror(f"https://github.com/{args.repository}.wiki.git", wiki,
               failure="the wiki is enabled but has no clone to scan: disable it, or add a page and scan again")
        header.append(f"wiki: {describe_refs(wiki)}")
        scans.append(ggshield.scan("wiki", "repo", str(wiki), unscanned=excluded_history_paths(wiki, rules.excluded)))
    images, described, digests = fetch_packages(args.repository, args.crane, out / "packages")
    header.append("packages: " + ("; ".join(described) or "none"))
    header += releases_on_ghcr(clone, branch_sha, digests)
    scans += [ggshield.docker(image) for image in images]
    download_releases(args.repository, out / "releases")
    download_workflow_artifacts(args.repository, out / "workflow-artifacts")
    texts = download_github_text(args.repository, out / "github-text")
    groups = [("release assets", [out / "releases"]), ("workflow artifacts", [out / "workflow-artifacts"]),
              ("packages", [out / "packages"]), ("GitHub text", [out / "github-text"])]
    groups += [("paths", args.path)] if args.path else []
    for scope, sources in groups:
        unpacked = out / "unpacked" / slug(scope)
        stats = unpack(sources, unpacked, skip_extensions=rules.skip_extensions, excluded=rules.excluded,
                       encoding=rules.encoding)
        header.append(f"{scope}: {stats.files} files, {stats.distinct} distinct contents"
                      + (f" ({texts} documents)" if scope == "GitHub text" else "")
                      + (f"; {stats.large_binary} binary files over 1 MiB not sent (ggshield's decoder finds no "
                         "text in them)" if stats.large_binary else ""))
        if stats.files:
            too_deep = [(f"{stats.too_deep} archives nested deeper than {MAX_DEPTH}", "not opened")]
            scans.append(ggshield.scan(scope, "path", "--recursive", "--yes", str(unpacked),
                                       unscanned=too_deep if stats.too_deep else ()))

    text, clean = summarize(scans)
    header.append(f"quota after: {ggshield.quota()}")
    report = "\n".join(header) + "\n" + text
    (out / "summary.txt").write_text(report)
    print(report + f"reports: {out}")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
