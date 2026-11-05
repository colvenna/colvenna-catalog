#!/usr/bin/env python3
"""The publication scan (#113): ggshield, GitGuardian's command-line scanner, over everything the public can
fetch from the repository, with the .gitguardian.yaml of the scanned branch (main by default). Nothing is
made public, and no release is made, before it finds 0 (CONTRIBUTING.md#secret-scanning).

It scans:
  - history: an anonymous `git clone --mirror` (every branch, tag and refs/pull/*), plus each pull-request
    head that a force push replaced and GitHub still serves by its SHA; and the wiki, when it is enabled;
  - the GHCR packages linked to the repository (the image and the chart), every version: each manifest and
    blob fetched by its digest and checked against it, each tag checked to resolve to its version's digest.
    An image is scanned with `ggshield secret scan docker` too, with an empty layer cache;
  - release assets and workflow artifacts as published, the packages' blobs, and any local --path: each
    archive opened recursively (zip, jar, wheel, tar, gzip; the image's layers and the chart among them),
    each distinct file scanned once;
  - the GitHub text: issues and pull requests with their comments, reviews and review comments, release
    notes and commit comments.
Reports with matched values stay in --output, which must be private and outside the repository; the
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

from ggshield_pin import installed_version, pinned_version

ROOT = Path(__file__).resolve().parents[2]
ZIP_SUFFIXES = (".zip", ".whl", ".jar")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
MAX_DEPTH = 6
# ggshield skips a document over 1 MiB (its API's limit) and a request body over 2.5 MiB, in which JSON
# escaping can grow text up to 6 times; parts of 200,000 characters (at most 800 kB of UTF-8) stay under
# both. Parts overlap, so a value that crosses a cut is whole in one of them.
PART_CHARS = 200_000
OVERLAP_CHARS = 4_096


@dataclass
class Unpacked:
    files: int = 0
    distinct: int = 0
    too_deep: int = 0  # archives nested deeper than MAX_DEPTH, not opened: the scan is then incomplete


ARCHIVE_ERRORS = (OSError, EOFError, zlib.error, zipfile.BadZipFile, tarfile.TarError)


def unpack(sources, out, part_chars=PART_CHARS, overlap_chars=OVERLAP_CHARS, skip_extensions=frozenset(),
           excluded=lambda path: False):
    """Open each source and every archive nested in it (zip, jar, wheel, tar, gzip); store each distinct file
    content once under out. A UTF-8 text longer than part_chars is stored as overlapping parts, each in a
    folder that records its number and the line it starts in. ggshield's path scan drops files by extension
    (skip_extensions) without reading them, so such a file gets ".txt" appended, and ggshield's own decoder
    decides whether it is text. A file ggshield would exclude by name (excluded, given the stored path) is
    stored under a neutral name instead. A damaged archive is stored as a file."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result = Unpacked()
    seen = set()

    def opened(name, data, depth):
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
        if opened(name, data, depth):
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
    """(first line, bytes) of data itself, or of windows of a UTF-8 text longer than part_chars that overlap
    by overlap_chars."""
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


def force_pushed_heads(pages):
    """The head SHA each force push in the GraphQL pages replaced, each once.

    GitHub keeps serving such a commit by its SHA once the repository is public, though no ref reaches it."""
    heads = []
    for page in pages:
        for pull in page["data"]["repository"]["pullRequests"]["nodes"]:
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
      nodes { timelineItems(first: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {
        nodes { ... on HeadRefForcePushedEvent { beforeCommit { oid } } } } }
    }
  }
}"""
ANONYMOUS_GIT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never prompt; no credential helper either
GIT = ["git", "-c", "credential.helper="]


def run(command, failure=None, **options):
    """The command's output; on failure, exit with failure (or the command and its stderr)."""
    done = subprocess.run(command, capture_output=True, text=True, **options)
    if done.returncode:
        sys.exit(failure or f"{' '.join(command[:4])} ... failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout


def ggshield_rules(binary):
    """What ggshield 1.56.1 leaves out without saying so, read from the Python of ggshield's environment: the
    extensions its path scan drops unread, and a predicate for the paths its default exclusions skip (in
    every scan, history included). --ignore-default-excludes is accepted but has no effect in 1.56.1."""
    folder = Path(shutil.which(binary) or binary).resolve().parent
    code = ("import json\n"
            "from ggshield.cmd.secret.scan.secret_scan_common_options import IGNORED_DEFAULT_WILDCARDS as W\n"
            "from ggshield.core.filter import init_exclusion_regexes\n"
            "from ggshield.utils._binary_extensions import BINARY_EXTENSIONS as B\n"
            "print(json.dumps([sorted(B), sorted(r.pattern for r in init_exclusion_regexes(set(W)))]))")
    for python in (folder / "python3", folder / "python"):
        done = subprocess.run([str(python), "-c", code], capture_output=True, text=True) if python.exists() else None
        if done and done.returncode == 0:
            extensions, patterns = json.loads(done.stdout)
            regexes = [re.compile(pattern) for pattern in patterns]
            return set(extensions), lambda path: any(r.search(PurePosixPath(path).as_posix()) for r in regexes)
    sys.exit(f"cannot read what ggshield leaves out: no Python beside {folder / Path(binary).name} imports ggshield")


def excluded_history_paths(clone, excluded):
    """The paths any commit of the clone touches that ggshield's default exclusions skip (unquoted, so that a
    non-ASCII path is matched as ggshield sees it)."""
    listing = run([*GIT, "-C", str(clone), "-c", "core.quotePath=false", "log", "-z", "--all", "--format=",
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


def mirror(repository, clone):
    run([*GIT, "clone", "--quiet", "--mirror", f"https://github.com/{repository}.git", str(clone)],
        env=ANONYMOUS_GIT)
    run([*GIT, "-C", str(clone), "remote", "set-url", "--push", "origin", "no-push://read-only"])


def fetch_force_pushed(repository, clone):
    """Fetch into the clone each replaced pull-request head GitHub still serves; count them by outcome."""
    owner, name = repository.split("/", 1)
    pages, cursor = [], None
    while True:
        args = ["gh", "api", "graphql", "-f", "query=" + FORCE_PUSHES, "-F", "owner=" + owner, "-F", "name=" + name]
        pages.append(json.loads(run(args + (["-F", "cursor=" + cursor] if cursor else []))))
        info = pages[-1]["data"]["repository"]["pullRequests"]["pageInfo"]
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
        with open(out / f"{artifact_id}.zip", "wb") as stream:
            subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"],
                           check=True, stdout=stream)


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


def package_versions(repository):
    """(package, version digest, tags) of every version of each GHCR container package linked to the
    repository, tagged or not: anyone can pull a public package's version by its digest."""
    owner = repository.split("/", 1)[0]
    names = run(["gh", "api", "--paginate", f"orgs/{owner}/packages?package_type=container&per_page=100",
                 "--jq", f'.[] | select(.repository.full_name == "{repository}") | .name']).split()
    for name in names:
        path = f"orgs/{owner}/packages/container/{urllib.parse.quote(name, safe='')}/versions?per_page=100"
        for line in run(["gh", "api", "--paginate", path, "--jq",
                         '.[] | "\\(.name) \\(.metadata.container.tags | join(","))"']).splitlines():
            digest, _, tags = line.partition(" ")
            yield name, digest, [tag for tag in tags.split(",") if tag]


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
    return "sha256:" + digest.hexdigest()


def crane_output(crane, *args):
    done = subprocess.run([crane, *args], capture_output=True)
    if done.returncode:
        sys.exit(f"crane {' '.join(args)} failed ({done.returncode}): {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


IMAGE_CONFIGS = ("application/vnd.docker.container.image.v1+json", "application/vnd.oci.image.config.v1+json")


def fetch_artifact(crane, image, digest, out, seen):
    """Fetch the manifest at digest and each blob it names into out, each checked against its digest, and
    recurse into an index. Returns the digests of the image manifests (those Docker can pull) it held."""
    if digest in seen:
        return []
    seen.add(digest)
    data = crane_output(crane, "manifest", f"{image}@{digest}")
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        sys.exit(f"{image}@{digest}: the registry served a manifest of another digest")
    (out / f"{digest[7:]}.manifest.json").write_bytes(data)
    manifest = json.loads(data)
    if "manifests" in manifest:
        return [found for child in manifest["manifests"] for found in fetch_artifact(crane, image, child["digest"],
                                                                                        out, seen)]
    for blob in [manifest["config"], *manifest.get("layers", [])]:
        target = out / (blob["digest"][7:] + blob_suffix(blob.get("mediaType", "")))
        if not target.exists():
            with open(target, "wb") as stream:
                done = subprocess.run([crane, "blob", f"{image}@{blob['digest']}"], stdout=stream,
                                      stderr=subprocess.PIPE)
            if done.returncode:
                sys.exit(f"crane blob {image}@{blob['digest']} failed: {done.stderr.decode(errors='replace').strip()}")
            if sha256_file(target) != blob["digest"]:
                sys.exit(f"{image}@{blob['digest']}: the registry served a blob of another digest")
            if target.name.endswith(TAR_SUFFIXES) and not tarfile.is_tarfile(target):
                sys.exit(f"{image}@{blob['digest']}: a layer of media type {blob['mediaType']} is not a tar archive")
    layers = [layer.get("mediaType", "") for layer in manifest.get("layers", [])]
    is_image = manifest["config"].get("mediaType") in IMAGE_CONFIGS and all("tar" in kind for kind in layers)
    return [digest] if is_image else []


def fetch_packages(repository, crane, out):
    """Download every version of each GHCR package of the repository into out; the image manifests to scan
    with Docker, and a line for the summary per version."""
    out.mkdir(parents=True)
    owner = repository.split("/", 1)[0]
    images, described = [], []
    for name, digest, tags in package_versions(repository):
        image = f"ghcr.io/{owner}/{name}".lower()
        for tag in tags:
            served = crane_output(crane, "digest", f"{image}:{tag}").decode().strip()
            if served != digest:
                sys.exit(f"{image}:{tag} is {served}, but GitHub lists the tag on {digest}")
        held = fetch_artifact(crane, image, digest, out, set())
        images += [f"{image}@{found}" for found in held]
        described.append(f"{image}@{digest} ({', '.join(tags) or 'untagged'}; {len(held)} images)")
    return images, described


def ggshield(binary, config, out, scope, *command, env=None):
    report = out / f"{slug(scope)}.json"
    with open(report, "wb") as stdout, open(report.with_suffix(".stderr"), "wb") as stderr:
        # In out, so that ggshield's cache file (.cache_ggshield, which holds findings) stays there.
        returncode = subprocess.run([binary, "--config-path", str(config), "secret", "scan", *command, "--json"],
                                    stdout=stdout, stderr=stderr, cwd=out, env=env).returncode
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    return Scan(scope, returncode, data, report.with_suffix(".stderr").read_text(encoding="utf-8", errors="replace"))


def docker_scan(binary, config, out, image):
    """`ggshield secret scan docker` of an image, with an empty layer cache (GG_CACHE_DIR). It reads only the
    layers that COPY or ADD made and skips system paths (usr/, lib/, ...) and binary files, the jars among
    them: unpack() covers all of those."""
    if not shutil.which("docker"):
        sys.exit(f"Docker is needed to scan {image} with ggshield secret scan docker")
    present = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    cache = out / "ggshield-cache" / slug(image)
    cache.mkdir(parents=True)
    scan = ggshield(binary, config, out, f"image {image}", "docker", image,
                    env={**os.environ, "GG_CACHE_DIR": str(cache)})
    if not present:
        subprocess.run(["docker", "image", "rm", image], capture_output=True)
    return scan


def quota(binary):
    done = subprocess.run([binary, "quota"], capture_output=True, text=True)
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

    want, have = pinned_version(), installed_version(args.ggshield)
    if have != want:  # the completeness checks read this version's messages
        sys.exit(f"the scan needs ggshield {want}, installed from scripts/security/ggshield-requirements.lock "
                 f"(found: {have})")
    skip_extensions, excluded = ggshield_rules(args.ggshield)
    out = args.output.resolve()  # ggshield reports the paths it was given; file_line() maps absolute ones
    check_output(out, excluded)
    out.mkdir(mode=0o700, parents=True)
    header = [f"date: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
              f"repository: {args.repository}", f"ggshield: {have}", f"quota before: {quota(args.ggshield)}"]

    clone = out / "mirror.git"
    mirror(args.repository, clone)
    pushed = fetch_force_pushed(args.repository, clone)
    header.append(f"refs: {describe_refs(clone)}; force-pushed heads: "
                  + ", ".join(f"{number} {outcome}" for outcome, number in pushed.items()))
    branch_sha = run([*GIT, "-C", str(clone), "rev-parse", "--verify", f"refs/heads/{args.branch}"],
                     failure=f"the repository has no branch {args.branch}").strip()
    config = out / "gitguardian.yaml"
    config.write_text(run([*GIT, "-C", str(clone), "show", f"{branch_sha}:.gitguardian.yaml"],
                          failure=f"{args.branch} has no .gitguardian.yaml: the scan uses the repository's own "
                                  "configuration"))
    header.append(f"configuration: .gitguardian.yaml of {args.branch} {branch_sha}")

    scans = [ggshield(args.ggshield, config, out, "history", "repo", str(clone))]
    scans[-1].unscanned = excluded_history_paths(clone, excluded)
    settings = json.loads(run(["gh", "api", f"repos/{args.repository}"]))
    if settings["has_discussions"]:
        sys.exit("the repository's discussions are enabled, and the scan does not fetch them: disable them, or "
                 "extend the scan")
    if settings["has_wiki"]:
        wiki = out / "wiki.git"
        run([*GIT, "clone", "--quiet", "--mirror", f"https://github.com/{args.repository}.wiki.git", str(wiki)],
            failure="the wiki is enabled but has no clone to scan: disable it, or add a page and scan again",
            env=ANONYMOUS_GIT)
        header.append(f"wiki: {describe_refs(wiki)}")
        scans.append(ggshield(args.ggshield, config, out, "wiki", "repo", str(wiki)))
        scans[-1].unscanned = excluded_history_paths(wiki, excluded)
    images, described = fetch_packages(args.repository, args.crane, out / "packages")
    header.append("packages: " + ("; ".join(described) or "none"))
    scans += [docker_scan(args.ggshield, config, out, image) for image in images]
    download_releases(args.repository, out / "releases")
    download_workflow_artifacts(args.repository, out / "workflow-artifacts")
    texts = download_github_text(args.repository, out / "github-text")
    groups = [("release assets", [out / "releases"]), ("workflow artifacts", [out / "workflow-artifacts"]),
              ("packages", [out / "packages"]), ("GitHub text", [out / "github-text"])]
    groups += [("paths", args.path)] if args.path else []
    for scope, sources in groups:
        unpacked = out / "unpacked" / slug(scope)
        stats = unpack(sources, unpacked, skip_extensions=skip_extensions, excluded=excluded)
        header.append(f"{scope}: {stats.files} files, {stats.distinct} distinct contents scanned"
                      + (f" ({texts} documents)" if scope == "GitHub text" else ""))
        if stats.files:
            scans.append(ggshield(args.ggshield, config, out, scope, "path", "--recursive", "--yes", str(unpacked)))
            if stats.too_deep:
                scans[-1].unscanned = [(f"{stats.too_deep} archives nested deeper than {MAX_DEPTH}", "not opened")]

    text, clean = summarize(scans)
    header.append(f"quota after: {quota(args.ggshield)}")
    report = "\n".join(header) + "\n" + text
    (out / "summary.txt").write_text(report)
    print(report + f"reports: {out}")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
