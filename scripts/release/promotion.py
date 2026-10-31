#!/usr/bin/env python3
"""A release promoted from a GO candidate, and its manifest, release/<release>/manifest.json.

A release, v<major>.<minor>.<patch>, is one of its candidates (v<major>.<minor>.<patch>-rc.<n>, see
manifest.py) judged GO and promoted without rebuilding anything (docs/development.md, "Promoting a
GO candidate"). promote.sh makes it on top of the candidate's tag commit:

- the image is the candidate's, by the same digest, tagged <release> as well in the release registry;
- the chart is the candidate's package with only Chart.yaml's version and appVersion changed, both to
  <release> without its "v" (1.0.0), packaged again: a new digest, compared with the candidate's file
  by file, the comparison kept beside the manifest (release/<release>/chart-comparison.txt);
- the manifest is the candidate's under the release's names (release, source.tag, image.tag,
  chart.version, chart.appVersion and chart.digest), and `promotion` says where it came from: the
  candidate, its tag commit and manifest, the commit whose release/<candidate>/notes.md records its
  GO verdict (the acceptance), and the candidate's chart it was compared with;
- the release notes are the candidate's as accepted, under the release's name, their first section
  the release's identity.

A candidate is judged GO when its release notes' verdict table says so, with a basis that links the
release check (verdict).

publish-ghcr.sh copies a release's image and chart by digest to the GitHub Container Registry, and
only there: target says which repositories it may publish to.

usage:
  promotion.py names <candidate> <release>    image and chart: repository, the candidate's tag, the
                                              release's tag, tab-separated
  promotion.py published <release>            the tag GHCR gives the release's image and chart
  promotion.py verdict <notes.md>             GO, if the notes' release verdict is GO
  promotion.py rechart <package> <dir> <version> <appVersion>
                                              the package's files into <dir>, Chart.yaml's version and
                                              appVersion changed, nothing else
  promotion.py chart-diff <candidate's package> <release's package> <version> <appVersion>
                                              the two compared file by file, on stdout: only Chart.yaml's
                                              version and appVersion may differ, and be those
  promotion.py assemble <work>                the release's manifest (see assemble), on stdout
  promotion.py validate <manifest> [--release <release>]
  promotion.py check-tree <manifest> <repo> <commit>
                                              the manifest's git objects are the commit's
  promotion.py notes <manifest> < notes       the release notes, from the candidate's as accepted
  promotion.py registry <registry>            the release registry's kind: ecr (an ECR registry of a
                                              standard Region) or loopback (a rehearsal's)
  promotion.py target image|chart <repository> a publish target's kind: ghcr (the GHCR repository of
                                              the image or the chart) or loopback; never AWS ECR Public
Problems go to stderr, with exit status 1.
"""
import copy
import hashlib
import json
import os
import re
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

SCHEMA = 1
RELEASE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
# The GitHub Container Registry's repositories of a release (design 9.4): the image, and the chart
# as an OCI artifact.
GHCR_IMAGE = "ghcr.io/colvenna/colvenna-catalog"
GHCR_CHART = f"ghcr.io/colvenna/charts/{manifest.CHART_NAME}"
# A registry on this host, for a rehearsal or the offline tests: read and written over plain HTTP.
LOOPBACK = re.compile(r"^(?:localhost|127\.0\.0\.1):[0-9]{1,5}$")
LOOPBACK_TARGET = re.compile(r"^(?:localhost|127\.0\.0\.1):[0-9]{1,5}(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")
# AWS ECR Public, which nothing of this project is ever published to: its registry and its API.
ECR_PUBLIC = re.compile(r"(?:^|[./])public\.ecr\.aws(?:$|[/:])|ecr-public", re.IGNORECASE)
NEVER_ECR_PUBLIC = "AWS ECR Public is never a target: public artifacts go to GHCR only"
# The fields a release's manifest takes from its candidate's unchanged.
KEPT = ("installer", "cdk", "uat_cases", "required_cases", "performance", "configurations")
TOP = ("schema", "release", "promotion", "source", "generated", "image", "chart") + KEPT
# What the promotion changes in the chart: these two of Chart.yaml's fields.
CHANGED = ["version", "appVersion"]


def read(path):
    with open(path) as f:
        return f.read()


def release_problems(name):
    if isinstance(name, str) and RELEASE.fullmatch(name):
        return []
    return [f"release: {name!r} is not v<major>.<minor>.<patch>"]


def candidate_of_problems(candidate, release):
    """Whether the candidate is one of the release's: <release>-rc.<n>."""
    found = manifest.candidate_problems(candidate) + release_problems(release)
    if not found and not candidate.startswith(f"{release}-rc."):
        found.append(f"{candidate} is not a candidate of {release}: it promotes to {candidate.split('-rc.')[0]}")
    return found


def chart_version(release):
    return release[1:]


def names(candidate, release):
    found = candidate_of_problems(candidate, release)
    if found:
        raise ValueError("; ".join(found))
    return [("image", manifest.IMAGE_REPOSITORY, candidate, release),
            ("chart", manifest.CHART_REPOSITORY, manifest.chart_version(candidate), chart_version(release))]


def published(release):
    """The tag GHCR gives the release's image and chart: the version, without the "v"."""
    found = release_problems(release)
    if found:
        raise ValueError(found[0])
    return chart_version(release)


def source_tag(release, candidate):
    return f"{release} points at {candidate}'s tag commit with release/{release}/ added, nothing else"


# The release notes' verdict row (release/notes-template.md, section 5): | Release verdict | <result> | <basis> |
VERDICT_ROW = re.compile(r"^\s*\|\s*Release verdict\s*\|")


def verdict(text):
    """GO when the notes' release verdict is GO (plain, bold or as code) and its basis links the release
    check; ValueError saying what it is otherwise: NO-GO, the template's placeholder, a basis that is
    still the template's or links nothing, no verdict row, or more than one."""
    rows = [line for line in text.splitlines() if VERDICT_ROW.match(line)]
    if len(rows) != 1:
        raise ValueError(f"the release notes have {len(rows)} 'Release verdict' rows, not one: the verdict is"
                         " recorded in section 5's table")
    cells = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    result = cells[1] if len(cells) > 1 else ""
    basis = cells[2] if len(cells) > 2 else ""
    if result.strip("*` ") != "GO":
        raise ValueError(f"the release verdict is {result or 'empty'}, not GO: only a candidate judged GO is"
                         " promoted")
    if re.fullmatch(r"<.*>", basis) or not re.search(r"\]\([^)\s]+\)", basis):
        raise ValueError(f"the release verdict's basis is {basis or 'empty'}: it links the release check that"
                         " judged the candidate GO")
    return "GO"


def safe_members(archive):
    """The package's members, each a regular file or a directory inside it; ValueError otherwise."""
    members = archive.getmembers()
    for member in members:
        parts = member.name.split("/")
        if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
            raise ValueError(f"the chart package holds {member.name!r}, which is not a file inside it")
    return members


def files(package):
    """{path: (mode, bytes)} of the package's regular files."""
    with tarfile.open(package, "r:gz") as archive:
        return {m.name: (m.mode & 0o7777, archive.extractfile(m).read()) for m in safe_members(archive)
                if m.isfile()}


def chart_yaml(package_files):
    """The path of the package's Chart.yaml (<chart>/Chart.yaml); ValueError unless there is one."""
    found = [p for p in package_files if re.fullmatch(r"[^/]+/Chart\.yaml", p)]
    if len(found) != 1:
        raise ValueError(f"the chart package has {len(found)} Chart.yaml at its top, not one")
    return found[0]


def field(line):
    """(key, value) of a top-level `key: value` line, the value unquoted; None for other lines."""
    found = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):\s*(.*?)\s*", line)
    if not found:
        return None
    value = found[2]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return found[1], value


def chart_lines(text):
    """(the lines of Chart.yaml's text but its version and appVersion, {version, appVersion: value})."""
    kept, changed = [], {}
    for line in text.splitlines():
        parsed = field(line)
        if parsed and parsed[0] in CHANGED:
            changed[parsed[0]] = parsed[1]
        else:
            kept.append(line)
    return kept, changed


def with_versions(text, version, app_version):
    """Chart.yaml's text with its top-level version and appVersion lines set; ValueError unless it has
    each once."""
    lines, seen = text.splitlines(), {}
    for i, line in enumerate(lines):
        parsed = field(line)
        if parsed and parsed[0] in CHANGED:
            seen[parsed[0]] = seen.get(parsed[0], 0) + 1
            lines[i] = f"{parsed[0]}: {version if parsed[0] == 'version' else app_version}"
    if any(seen.get(key) != 1 for key in CHANGED):
        raise ValueError(f"Chart.yaml has version and appVersion lines {seen}, not one of each")
    return "\n".join(lines) + "\n"


def rechart(package, out, version, app_version):
    """The package's files under out, as the package holds them (modes kept), with Chart.yaml's version
    and appVersion set and nothing else changed; the chart directory's path."""
    held = files(package)
    chart = chart_yaml(held)
    for path, (mode, data) in held.items():
        target = os.path.join(out, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if path == chart:
            data = with_versions(data.decode(), version, app_version).encode()
        with open(target, "wb") as f:
            f.write(data)
        os.chmod(target, mode)
    return os.path.join(out, os.path.dirname(chart))


def chart_differences(candidate, release, version, app_version):
    """(report, problems): the release's package compared with the candidate's, file by file. Every file
    must be in both, with the same mode and bytes; Chart.yaml may differ only in its version and
    appVersion lines, which must be the release's."""
    have, want = files(candidate), files(release)
    report, problems = [], []
    chart = chart_yaml(have)
    for path in sorted(set(have) | set(want)):
        if path not in want:
            problems.append(f"{path}: in the candidate's chart, not the release's")
            continue
        if path not in have:
            problems.append(f"{path}: in the release's chart, not the candidate's")
            continue
        (mode_a, a), (mode_b, b) = have[path], want[path]
        if mode_a != mode_b:
            problems.append(f"{path}: mode {mode_a:o} in the candidate's chart, {mode_b:o} in the release's")
        if path != chart:
            if a == b:
                report.append(f"same      {path} (sha256:{hashlib.sha256(a).hexdigest()})")
            else:
                problems.append(f"{path}: differs")
            continue
        (old, before), (new, after) = chart_lines(a.decode()), chart_lines(b.decode())
        if old != new:
            problems.append(f"{path}: differs beyond version and appVersion")
        wanted = {"version": version, "appVersion": app_version}
        for key in CHANGED:
            if after.get(key) != wanted[key]:
                problems.append(f"{path}: {key} is {after.get(key)!r}, not {wanted[key]!r}")
        report.append(f"changed   {path}: version {before.get('version')} -> {after.get('version')}, appVersion"
                      f" {before.get('appVersion')} -> {after.get('appVersion')}; every other line the same")
    report.append(f"{len(set(have) | set(want))} files compared: "
                  + ("only Chart.yaml's version and appVersion differ" if not problems else
                     f"{len(problems)} difference{'s' if len(problems) > 1 else ''} beyond them"))
    return report, problems


def build(candidate, params):
    """The release's manifest from its candidate's (valid and fit for GO) and what promote.sh did:
      release, generated          the release, and when (UTC, ...Z)
      tag_commit, manifest_blob   the candidate's tag commit C, and its manifest's blob there
      acceptance                  {commit, blob}: the commit whose release/<candidate>/notes.md records
                                  the verdict GO, and that file's blob
      chart_digest, chart_files   the release's chart in the release registry, and how many files the
                                  comparison with the candidate's found the same or changed
    ValueError if it is not a valid one."""
    found = manifest.problems(candidate) or manifest.go_problems(candidate)
    if found:
        raise ValueError("the candidate's manifest: " + "; ".join(found))
    name, release = candidate["candidate"], params["release"]
    m = {
        "schema": SCHEMA,
        "release": release,
        "promotion": {
            "candidate": name,
            "tag_commit": params["tag_commit"],
            "manifest": {"path": f"release/{name}/manifest.json", "blob": params["manifest_blob"]},
            "acceptance": {"path": f"release/{name}/notes.md", "commit": params["acceptance"]["commit"],
                           "blob": params["acceptance"]["blob"], "verdict": "GO"},
            "chart": {"version": candidate["chart"]["version"], "digest": candidate["chart"]["digest"],
                      "changed": list(CHANGED), "files": params["chart_files"]},
        },
        "source": {**candidate["source"], "tag": source_tag(release, name)},
        "generated": params["generated"],
        "image": {**candidate["image"], "tag": release},
        "chart": {**candidate["chart"], "version": chart_version(release), "appVersion": chart_version(release),
                  "digest": params["chart_digest"]},
        **{key: copy.deepcopy(candidate[key]) for key in KEPT},
    }
    found = problems(m)
    if found:
        raise ValueError("; ".join(found))
    return m


def assemble(work):
    """The release's manifest from what promote.sh gathered in the directory work: candidate.json (the
    candidate's manifest at its tag commit) and params.json (build's params)."""
    return build(json.loads(read(os.path.join(work, "candidate.json"))),
                 json.loads(read(os.path.join(work, "params.json"))))


def candidate_view(m):
    """The candidate's manifest as the release's records it (the candidate's names, its chart's version
    and digest): manifest.py checks the fields they share on it."""
    promotion = m["promotion"]
    name = promotion["candidate"]
    view = {key: copy.deepcopy(value) for key, value in m.items() if key not in ("release", "promotion")}
    view["candidate"] = name
    if isinstance(view.get("source"), dict):
        view["source"]["tag"] = manifest.source_tag(name)
    if isinstance(view.get("image"), dict):
        view["image"]["tag"] = name
    if isinstance(view.get("chart"), dict) and isinstance(promotion.get("chart"), dict):
        view["chart"].update(version=promotion["chart"].get("version"), appVersion=name,
                             digest=promotion["chart"].get("digest"))
    return view


def problems(m, release=None):
    """What is wrong with a release's manifest: an empty list when nothing is."""
    found = []

    def need(ok, what):
        if not ok:
            found.append(what)

    if not isinstance(m, dict):
        return ["the manifest: not an object"]
    for key in TOP:
        need(key in m, f"{key}: missing")
    for key in m:
        need(key in TOP, f"unknown field {key}")
    if found:
        return found
    need(type(m["schema"]) is int and m["schema"] == SCHEMA, f"schema: {m['schema']!r}, not {SCHEMA}")
    name = m["release"]
    found.extend(release_problems(name))
    if release is not None:
        need(name == release, f"release: the manifest is {name!r}'s, not {release}'s")
    promotion = m["promotion"]
    expected = {"candidate", "tag_commit", "manifest", "acceptance", "chart"}
    if not isinstance(promotion, dict) or set(promotion) != expected:
        return found + [f"promotion: not an object of {', '.join(sorted(expected))}"]
    candidate = promotion["candidate"]
    if not release_problems(name):
        found.extend(f"promotion.candidate: {p}" for p in candidate_of_problems(candidate, name))
    sha = manifest.SHA.fullmatch
    need(isinstance(promotion["tag_commit"], str) and sha(promotion["tag_commit"]) is not None,
         "promotion.tag_commit: not a full commit id")
    files_of = {"manifest": ("path", "blob"), "acceptance": ("path", "commit", "blob", "verdict"),
                "chart": ("version", "digest", "changed", "files")}
    for key, fields in files_of.items():
        value = promotion[key]
        if not isinstance(value, dict) or set(value) != set(fields):
            found.append(f"promotion.{key}: not an object of {', '.join(fields)}")
            continue
        for part in ("blob", "commit"):
            if part in fields:
                need(isinstance(value[part], str) and sha(value[part]) is not None,
                     f"promotion.{key}.{part}: not a full git object id")
    if not found:
        need(promotion["manifest"]["path"] == f"release/{candidate}/manifest.json",
             f"promotion.manifest.path: not release/{candidate}/manifest.json")
        acceptance = promotion["acceptance"]
        need(acceptance["path"] == f"release/{candidate}/notes.md",
             f"promotion.acceptance.path: not release/{candidate}/notes.md")
        need(acceptance["verdict"] == "GO", f"promotion.acceptance.verdict: {acceptance['verdict']!r}, not GO")
        chart = promotion["chart"]
        need(chart["version"] == manifest.chart_version(candidate),
             f"promotion.chart.version: {chart['version']!r}, not {manifest.chart_version(candidate)!r}")
        need(isinstance(chart["digest"], str) and manifest.DIGEST.fullmatch(chart["digest"]) is not None,
             "promotion.chart.digest: not sha256:<64 lowercase hex digits>")
        need(chart["changed"] == CHANGED, f"promotion.chart.changed: {chart['changed']!r}, not {CHANGED!r}")
        need(type(chart["files"]) is int and chart["files"] > 0, "promotion.chart.files: not a count of files")
    if found:
        return found
    if isinstance(m["source"], dict):
        need(m["source"].get("tag") == source_tag(name, candidate), f"source.tag: not {source_tag(name, candidate)!r}")
    if isinstance(m["image"], dict):
        need(m["image"].get("tag") == name, f"image.tag: {m['image'].get('tag')!r}, not {name!r}")
    if isinstance(m["chart"], dict):
        need(m["chart"].get("version") == chart_version(name),
             f"chart.version: {m['chart'].get('version')!r}, not {chart_version(name)!r}")
        need(m["chart"].get("appVersion") == chart_version(name),
             f"chart.appVersion: {m['chart'].get('appVersion')!r}, not {chart_version(name)!r}")
        digest = m["chart"].get("digest")
        need(isinstance(digest, str) and manifest.DIGEST.fullmatch(digest) is not None,
             f"chart.digest: {digest!r} is not sha256:<64 lowercase hex digits>")
        need(digest != promotion["chart"]["digest"], "chart.digest: the candidate's chart's, not a package of"
             " the release's own version")
    view = candidate_view(m)
    found.extend(manifest.problems(view, candidate))
    if not found:
        found.extend(manifest.go_problems(view))
    return found


def valid(path, release=None):
    m = manifest.load(path)
    found = problems(m, release)
    if found:
        raise ValueError(f"{path}:\n  " + "\n  ".join(found))
    return m


SECTION = re.compile(r"^## ", re.MULTILINE)
REPOSITORY = manifest.SOURCE


def identity(m):
    """The release notes' first section: the release, from its manifest."""
    p, image, chart, built_from = m["promotion"], m["image"], m["chart"], m["source"]["built_from"]
    candidate, release, acceptance = p["candidate"], m["release"], p["acceptance"]
    return f"""## 1. Release

| Item | Value |
| --- | --- |
| Release | `{release}` (annotated tag) |
| Promoted from | `{candidate}`, judged GO: its release notes as accepted, [`{acceptance['path']}`]({REPOSITORY}/blob/{acceptance['commit']}/{acceptance['path']}) at commit `{acceptance['commit']}`. Nothing was rebuilt. The sections after this one are those notes, unchanged |
| Built from | commit [`{built_from[:8]}`]({REPOSITORY}/commit/{built_from}) (`{built_from}`). The tag `{release}` points at a commit that adds only `release/{release}/` to `{candidate}`'s tag commit, `{p['tag_commit']}` |
| Manifest | [`release/{release}/manifest.json`]({REPOSITORY}/blob/{release}/release/{release}/manifest.json): `{candidate}`'s manifest under the release's names, and `promotion`, where it came from |
| Image | `{image['repository']}@{image['digest']}` (`{image['platform']}`): `{candidate}`'s image, tagged `{release}` as well, labelled `org.opencontainers.image.revision={built_from}` (its `org.opencontainers.image.version` label stays `{candidate}`, as it was built) |
| Helm chart | `oci://<registry>/{chart['repository']}@{chart['digest']}`, version `{chart['version']}`, appVersion `{chart['appVersion']}`: `{candidate}`'s chart (`{p['chart']['digest']}`, version `{p['chart']['version']}`) with only `Chart.yaml`'s version and appVersion changed, its {p['chart']['files']} files compared one by one ([`chart-comparison.txt`]({REPOSITORY}/blob/{release}/release/{release}/chart-comparison.txt)); annotated `colvenna-catalog/source-commit: {built_from}` |
| Public copies | By the same digests, `{GHCR_IMAGE}:{chart_version(release)}` and `oci://{GHCR_CHART}` version `{chart_version(release)}`, once `scripts/release/publish-ghcr.sh {release}` has published them |
| Generated | {m['generated'][:10]} |

A consumer that runs the chart itself fetches both by digest from the release registry:

```bash
crane copy <registry>/{image['repository']}@{image['digest']} <its own repository>:{release}
helm pull oci://<registry>/{chart['repository']}@{chart['digest']}
```

By hand, both of these print `{built_from}`:

```bash
crane config <registry>/{image['repository']}@{image['digest']} | jq -r '.config.Labels["org.opencontainers.image.revision"]'
helm show chart oci://<registry>/{chart['repository']}@{chart['digest']} | sed -n 's|^  colvenna-catalog/source-commit: ||p'
```

"""


def notes(m, text):
    """The release notes from the candidate's as accepted (text): the title and heading under the
    release's name, the date the promotion's, the status final (nothing is left to fill in: the verdict
    is GO), the template's comment replaced by one saying where the notes come from, and section 1 the
    release's identity; every other section unchanged."""
    candidate, release = m["promotion"]["candidate"], m["release"]
    title = f"Colvenna Catalog {candidate} release notes"
    heading = re.search(rf"^# {re.escape(title)}\n", text, re.MULTILINE)
    starts = [s.start() for s in SECTION.finditer(text)]
    first = re.search(r"^## 1\. Candidate\n", text, re.MULTILINE)
    if heading is None or first is None:
        raise ValueError(f"the candidate's notes need the heading '# {title}' and a section '## 1. Candidate'")
    after = [s for s in starts if s > first.start()]
    if not after:
        raise ValueError("the candidate's notes have no section after '## 1. Candidate'")
    head = text[:heading.start()]
    head = re.sub(rf'^title: "?{re.escape(title)}"?$', f'title: "Colvenna Catalog {release} release notes"', head,
                  count=1, flags=re.MULTILINE)
    head = re.sub(r"^date: .*$", f"date: {m['generated'][:10]}", head, count=1, flags=re.MULTILINE)
    head = re.sub(r"^status: .*$", "status: final", head, count=1, flags=re.MULTILINE)
    comment = (f"<!--\nscripts/release/promote.sh wrote these notes when it promoted {candidate} to {release}:"
               f" section 1 from\nrelease/{release}/manifest.json; every other section is {candidate}'s release"
               f" notes as accepted\n(release/{candidate}/notes.md at {m['promotion']['acceptance']['commit']}),"
               " unchanged.\n-->\n")
    head = re.sub(r"<!--.*?-->\n", lambda _: comment, head, count=1, flags=re.DOTALL)
    if comment not in head:
        head += "\n" + comment
    return (head + f"# Colvenna Catalog {release} release notes\n" + text[heading.end():first.start()]
            + identity(m) + text[after[0]:])


def registry_kind(registry):
    """ecr or loopback: the release registry, an ECR registry of a standard Region (as
    COLVENNA_RELEASE_REGISTRY), or a loopback one standing in for it in a rehearsal."""
    if ECR_PUBLIC.search(registry):
        raise ValueError(f"COLVENNA_RELEASE_REGISTRY={registry!r}: {NEVER_ECR_PUBLIC}, and the release registry"
                         " is the private one")
    if LOOPBACK.fullmatch(registry):
        return "loopback"
    manifest.registry_parts(registry)
    return "ecr"


def target_kind(what, repository):
    """ghcr or loopback: whether publish-ghcr.sh may copy the release's image or chart (what) into the
    repository, which is either its GHCR repository (GHCR_IMAGE, GHCR_CHART) or a loopback registry's (a
    rehearsal), without a tag or digest. Never AWS ECR Public."""
    expected = {"image": GHCR_IMAGE, "chart": GHCR_CHART}[what]
    if ECR_PUBLIC.search(repository):
        raise ValueError(f"{repository!r}: {NEVER_ECR_PUBLIC} ({expected})")
    if repository == expected:
        return "ghcr"
    if LOOPBACK_TARGET.fullmatch(repository):
        return "loopback"
    raise ValueError(f"{repository!r}: the {what} is published to GHCR only, as {expected}, or to a loopback"
                     " registry in a rehearsal")


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    if command == "names" and len(args) == 2:
        for row in names(*args):
            print("\t".join(row))
    elif command == "published" and len(args) == 1:
        print(published(args[0]))
    elif command == "verdict" and len(args) == 1:
        print(verdict(read(args[0])))
    elif command == "rechart" and len(args) == 4:
        print(rechart(*args))
    elif command == "chart-diff" and len(args) == 4:
        report, found = chart_differences(*args)
        print("\n".join(report + [f"DIFFERS   {p}" for p in found]))
        if found:
            raise ValueError("the release's chart is not the candidate's but for Chart.yaml's version and"
                             " appVersion:\n  " + "\n  ".join(found))
    elif command == "assemble" and len(args) == 1:
        print(json.dumps(assemble(args[0]), indent=2))
    elif command == "validate" and len(args) in (1, 3) and (len(args) == 1 or args[1] == "--release"):
        valid(args[0], args[2] if len(args) == 3 else None)
    elif command == "check-tree" and len(args) == 3:
        found = manifest.tree_problems(candidate_view(valid(args[0])), args[1], args[2])
        if found:
            raise ValueError(f"{args[0]}:\n  " + "\n  ".join(found))
    elif command == "notes" and len(args) == 1:
        print(notes(valid(args[0]), sys.stdin.read()), end="")
    elif command == "registry" and len(args) == 1:
        print(registry_kind(args[0]))
    elif command == "target" and len(args) == 2 and args[0] in ("image", "chart"):
        print(target_kind(*args))
    else:
        raise ValueError(__doc__.split("usage:\n", 1)[1].split("Problems", 1)[0].rstrip())


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
