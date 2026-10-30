#!/usr/bin/env python3
"""The license scan, and the record it is checked against: THIRD_PARTY_NOTICES.md.

THIRD_PARTY_NOTICES.md holds three tables the scan produces, each between a pair of markers:

- java: every jar the server's image ships (pom.xml's compile and runtime closure, which
  `mvn package` copies into target/lib and the Dockerfile copies into the image), with the
  license its Maven POM declares, as an SPDX expression where one is known. Its start marker
  records the git blob of the pom.xml it was scanned at (pom=<blob>).
- bundled: what the shaded jars among them contain, as each jar's META-INF/NOTICE lists it, with
  the license that NOTICE names for it ("not stated" where it names none); and the projects a jar
  keeps a license file of under META-INF/licenses/<groupId>/<artifactId>-<version>/, with the
  license that file's wording shows; and the projects a jar keeps only the Maven pom.properties of.
- npm: every package of the CDK app's lockfile (deploy/aws/package-lock.json), with its declared
  license and how it is installed (a dependency, a dev dependency, or bundled in another package).

A fourth table, texts, is kept by hand: what the server's image carries without a license file of its
own (the jars the scan finds with none, and the Lambda Web Adapter's binary), each with the license
texts the Dockerfile copies for it from licenses/ into /licenses/third-party/. The image's /licenses/
holds those texts, this repository's LICENSE and NOTICE, and THIRD_PARTY_NOTICES.md itself.

scan resolves them afresh (Maven and the npm lockfile; it needs Maven, JDK 21 and Maven Central)
and compares them with the record, the texts table with the jars that carry no license file among
them; --update rewrites the three tables and the pom stamp from the scan instead, for review (the
texts table is left as it is). The offline tests (test_licenses.py) check what can be checked without
Maven: the stamp is the current pom.xml's, every dependency pom.xml declares is in the java table,
the npm table is the lockfile, every image and tool the tree pins is named in the record, and what the
Dockerfile copies into /licenses/ is what the texts table names, each text of the license it is
named for. image checks a built image's /licenses/ against the same.

usage:
  licenses.py scan [--update] [--out <dir>]
                      the scan's raw results go to <dir> (default .state/licenses/<UTC time>/, which
                      git ignores); exit 1 if the record differs from the scan (without --update)
  licenses.py image <image>
                      exit 1 if the local image's /licenses/ is not what the record and the tree say
                      (needs Docker; the image is not run)
  licenses.py blob <file>
                      the file's git blob id (the pom stamp)
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
NOTICES = os.path.join(REPO, "THIRD_PARTY_NOTICES.md")
POM = os.path.join(REPO, "pom.xml")
LOCK = os.path.join(REPO, "deploy", "aws", "package-lock.json")
DOCKERFILE = os.path.join(REPO, "Dockerfile")
# The server's image keeps its licenses under IMAGE_LICENSES: OWN_FILES, from the repository's root, and
# under third-party/ the texts the texts table names, from the tree's directory TEXTS_DIR.
IMAGE_LICENSES = "/licenses/"
OWN_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
TEXTS_DIR = "licenses"
LICENSE_PLUGIN = "org.codehaus.mojo:license-maven-plugin:2.5.0"

# The license names Maven POMs use, as SPDX identifiers. A name not here stays as written, so a new
# one shows up in the scan's differences rather than being guessed at.
SPDX = {
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "the apache software license, version 2.0": "Apache-2.0",
    "the apache license, version 2.0": "Apache-2.0",
    "mit": "MIT",
    "the mit license": "MIT",
    "mit license": "MIT",
    "mit-0": "MIT-0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "eclipse public license - v 1.0": "EPL-1.0",
    # Logback's POM names the LGPL without its version up to 1.5.21; logback's own terms are the LGPL 2.1.
    "gnu lesser general public license": "LGPL-2.1",
    # Logback's POM at 1.5.38 (#18) names its two licenses by their SPDX identifiers.
    "epl-2.0": "EPL-2.0",
    "lgpl-2.1-only": "LGPL-2.1-only",
}
POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def spdx(names):
    """One SPDX expression for a dependency's declared licenses (several: either may be chosen)."""
    ids = [SPDX.get(n.strip().lower(), n.strip()) for n in names]
    return " OR ".join(dict.fromkeys(ids))


def git_blob(path):
    """The git blob id of a file's content, as `git hash-object` gives it."""
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# --- the record --------------------------------------------------------------------------------

def _cell(text):
    return text.strip().replace("**", "").strip("`").strip()


def marked(name):
    """The pattern of a table between <!-- licenses:<name>:start ... --> and <!-- licenses:<name>:end -->."""
    return re.compile(r"<!-- licenses:%s:start(?P<attrs>[^>]*)-->(?P<body>.*?)<!-- licenses:%s:end -->"
                      % (name, name), re.S)


def unmarked(name):
    return ValueError(f"THIRD_PARTY_NOTICES.md has no {name} table between its markers")


def section(text, name):
    """The table between the markers of <name>: the start marker's key=value attributes, and the rows
    below the table's header, as lists of cells."""
    m = marked(name).search(text)
    if not m:
        raise unmarked(name)
    attrs = dict(re.findall(r"(\w+)=(\S+)", m["attrs"]))
    rows = [line for line in m["body"].splitlines() if line.startswith("|")]
    return attrs, [[_cell(c) for c in row.strip().strip("|").split("|")] for row in rows[2:]]


def record(path=NOTICES):
    """The record's three tables: {"pom": <blob>, "java": {(artifact, version, license)},
    "bundled": {(jar, license, component)}, "npm": {(package, version, license, kind)}}."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    attrs, java = section(text, "java")
    _, bundled = section(text, "bundled")
    _, npm = section(text, "npm")
    return {
        "pom": attrs.get("pom"),
        "java": {(r[0], r[1], r[2]) for r in java},
        "bundled": {(r[0], r[1], c.strip().strip("`")) for r in bundled for c in r[2].split(",") if c.strip()},
        "npm": {tuple(r[:4]) for r in npm},
    }


def texts_table(path=NOTICES):
    """The texts table: {(component, version, license): (text file under licenses/, ...)}."""
    with open(path, encoding="utf-8") as f:
        _, rows = section(f.read(), "texts")
    return {(r[0], r[1], r[2]): tuple(t.strip().strip("`") for t in r[3].split(",") if t.strip()) for r in rows}


# --- what the tree declares --------------------------------------------------------------------

def pom_dependencies(pom=POM):
    """pom.xml's dependencies that are not test-scoped, as (groupId:artifactId, version): property
    references resolved, and a version managed in dependencyManagement (directly, or by an imported
    BOM of the same groupId) filled in."""
    # This repository's own pom.xml (or a test's); ElementTree resolves no external entity.
    root = ET.parse(pom).getroot()  # nosec B314
    properties = root.find("m:properties", POM_NS)
    props = {} if properties is None else {
        e.tag.split("}", 1)[1]: (e.text or "").strip() for e in properties}

    def value(e, tag):
        found = e.find(f"m:{tag}", POM_NS)
        text = (found.text or "").strip() if found is not None else ""
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m[1], m[0]), text)

    managed = {}
    for d in root.findall("m:dependencyManagement/m:dependencies/m:dependency", POM_NS):
        group, artifact = value(d, "groupId"), value(d, "artifactId")
        managed[f"{group}:{artifact}"] = value(d, "version")
        if value(d, "scope") == "import":
            managed.setdefault(group, value(d, "version"))
    found = []
    for d in root.findall("m:dependencies/m:dependency", POM_NS):
        if value(d, "scope") in ("test", "provided", "system"):
            continue
        ga = f"{value(d, 'groupId')}:{value(d, 'artifactId')}"
        version = value(d, "version") or managed.get(ga) or managed.get(value(d, "groupId"), "")
        found.append((ga, version))
    return found


def npm_packages(lock=LOCK):
    """Every package of the lockfile: (name, version, license, kind), kind being "dependency",
    "dev" or "bundled in <package>"."""
    with open(lock, encoding="utf-8") as f:
        packages = json.load(f)["packages"]
    found = set()
    for path, p in packages.items():
        if not path:
            continue
        parts = path.split("node_modules/")
        name = parts[-1].rstrip("/")
        if p.get("inBundle"):
            kind = "bundled in " + parts[-2].rstrip("/")
        else:
            kind = "dev" if p.get("dev") else "dependency"
        found.add((name, p.get("version", ""), p.get("license", "UNKNOWN"), kind))
    return found


# --- the scan's raw results --------------------------------------------------------------------

THIRD_PARTY_LINE = re.compile(r"^\s*((?:\([^()]*\)\s*)+)(.*?)\s+\(([^:()\s]+):([^:()\s]+):([^\s()]+) - .*\)\s*$")


def parse_third_party(text):
    """license-maven-plugin's THIRD-PARTY.txt: {(groupId:artifactId, version, license)}."""
    found = set()
    for line in text.splitlines():
        m = THIRD_PARTY_LINE.match(line)
        if m:
            names = re.findall(r"\(([^()]*)\)", m[1])
            found.add((f"{m[3]}:{m[4]}", m[5], spdx(names)))
    return found


BUNDLES = re.compile(r"bundles the following dependenc(?:y|ies) under (?:the )?(.+?)(?:\s*\(|\.\s*$|:\s*$|$)")
COMPONENT = re.compile(r"^- ([\w.-]+:[\w.-]+:[\w.-]+)")


def bundled_from_notice(jar, text):
    """What a jar's NOTICE says it bundles: {(jar, license as the NOTICE names it, component)}. A
    module's heading (its Copyright line) ends the license a list was under: a list that follows no
    "bundles the following dependencies under ..." line has its license "not stated"."""
    found, license = set(), "not stated"
    for line in text.splitlines():
        m = BUNDLES.search(line)
        if m:
            license = m[1].strip().rstrip(".")
            continue
        if line.startswith("Copyright") or line.startswith("This project includes"):
            license = "not stated"
            continue
        c = COMPONENT.match(line)
        if c:
            found.add((jar, license, c[1]))
    return found


LICENSE_FILE = re.compile(r"^META-INF/licenses/([^/]+)/([^/]+?)-(\d[^/]*)/(?:.+/)?LICENSE[^/]*$")


def recognized_license(text):
    """The license a license text is, by its wording, as an SPDX identifier (the LGPL 2.1 without "-only" or
    "-or-later": its text is the same for both); None for a text not recognized."""
    if "Apache License" in text and "Version 2.0" in text:
        return "Apache-2.0"
    if "Eclipse Public License - v 2.0" in text:
        return "EPL-2.0"
    if "GNU LESSER GENERAL PUBLIC LICENSE" in text and "Version 2.1, February 1999" in text:
        return "LGPL-2.1"
    if "Redistribution and use in source and binary forms" in text:
        return "BSD-3-Clause" if "Neither the name" in text else "BSD-2-Clause"
    if "Permission is hereby granted, free of charge" in text:
        # MIT-0 is MIT without the condition that the notice be kept.
        return "MIT" if "The above copyright notice and this permission notice shall be included" in text else "MIT-0"
    return None


def license_of_text(text):
    """The license a bundled project's license file holds, recognized by its wording."""
    found = recognized_license(text)
    return f"{found}, by its license file" if found else "a license file not recognized"


def bundled_from_license_files(jar, files):
    """What a jar says it bundles by keeping each bundled project's license file under
    META-INF/licenses/<groupId>/<artifactId>-<version>/ (as the PostgreSQL driver does):
    {(jar, license, component)}, from {entry: text}."""
    found = set()
    for entry, text in files.items():
        m = LICENSE_FILE.match(entry)
        if m:
            found.add((jar, license_of_text(text), f"{m[1]}:{m[2]}:{m[3]}"))
    return found


POM_PROPERTIES = re.compile(r"^META-INF/maven/([^/]+)/([^/]+)/pom\.properties$")
UNLISTED = "not listed in its NOTICE (a pom.properties only)"


def bundled_from_pom_properties(jar, own, files, listed):
    """The other projects a shaded jar keeps the Maven pom.properties of, but its NOTICE and license
    files do not name: {(jar, UNLISTED, component)}. <own> is the jar's groupId:artifactId, <listed>
    what its NOTICE and license files already gave."""
    named = {component.rsplit(":", 1)[0] for _, _, component in listed}
    found = set()
    for entry, text in files.items():
        m = POM_PROPERTIES.match(entry)
        if m and f"{m[1]}:{m[2]}" not in named | {own}:
            version = re.search(r"^version=(.+)$", text, re.M)
            found.add((jar, UNLISTED, f"{m[1]}:{m[2]}:{version[1].strip() if version else 'UNKNOWN'}"))
    return found


def artifact_id(ga):
    """The artifactId of groupId:artifactId."""
    return ga.split(":", 1)[1]


def jar_name(ga, version):
    """The file name `mvn dependency:copy-dependencies` gives a jar: <artifactId>-<version>.jar."""
    return f"{artifact_id(ga)}-{version}.jar"


def jar_artifact(file_name, java):
    """The groupId:artifactId of a jar in target/lib, by its file name; None for a jar not in java."""
    for ga, version, _ in java:
        if file_name == jar_name(ga, version):
            return ga
    return None


def scan(out):
    """Runs the scan into <out>; returns {"pom", "java", "bundled", "npm"} as record has them, and the jars
    of target/lib ("jars"), and those that carry no license or notice file ("without_license_files")."""
    os.makedirs(out, exist_ok=True)
    lib = os.path.join(out, "lib")
    log = os.path.join(out, "maven.log")
    command = ["mvn", "-B", f"{LICENSE_PLUGIN}:add-third-party", "dependency:copy-dependencies",
               "-Dlicense.excludedScopes=test,provided,system", f"-Dlicense.outputDirectory={out}",
               "-Dlicense.thirdPartyFilename=maven-runtime.txt", "-Dlicense.sortArtifactByName=true",
               "-DincludeScope=runtime", f"-DoutputDirectory={lib}"]
    with open(log, "w", encoding="utf-8") as f:
        status = subprocess.run(command, cwd=REPO, stdout=f, stderr=subprocess.STDOUT).returncode
    with open(log, encoding="utf-8") as f:
        succeeded = "[INFO] BUILD SUCCESS" in f.read().splitlines()
    if status != 0 or not succeeded:
        raise RuntimeError(f"the Maven scan failed; see {log}")
    with open(os.path.join(out, "maven-runtime.txt"), encoding="utf-8") as f:
        java = parse_third_party(f.read())
    bundled, jars, without_license_files = set(), sorted(os.listdir(lib)), []
    for name in jars:
        artifact = jar_artifact(name, java)
        # A jar the license list lacks keeps its file name here; differences reports it.
        jar = artifact_id(artifact) if artifact else name
        with zipfile.ZipFile(os.path.join(lib, name)) as z:
            entries = z.namelist()
            if not any(re.search(r"(license|notice|copying)", e, re.I) for e in entries):
                without_license_files.append(name)
            kept = {e: z.read(e).decode("utf-8", "replace") for e in entries
                    if re.search(r"(^|/)META-INF/NOTICE(\.txt|\.md)?$", e) or LICENSE_FILE.match(e)
                    or POM_PROPERTIES.match(e)}
        listed = set()
        for entry, text in kept.items():
            target = os.path.join(out, "notices", name, entry)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
            if not LICENSE_FILE.match(entry) and not POM_PROPERTIES.match(entry):
                listed |= bundled_from_notice(jar, text)
        listed |= bundled_from_license_files(jar, kept)
        bundled |= listed | bundled_from_pom_properties(jar, artifact, kept, listed)
    shutil.rmtree(lib)
    result = {"pom": git_blob(POM), "java": java, "bundled": bundled, "npm": npm_packages(), "jars": jars,
              "without_license_files": without_license_files}
    with open(os.path.join(out, "scan.json"), "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) if isinstance(v, set) else v for k, v in result.items()}, f, indent=1)
    return result


def differences(found, recorded):
    """What the scan found that the record lacks, and the other way round, one line each."""
    lines = []
    if found["pom"] != recorded["pom"]:
        lines.append(f"pom: the record was scanned at blob {recorded['pom']}, pom.xml is {found['pom']}")
    for table in ("java", "bundled", "npm"):
        for row in sorted(found[table] - recorded[table]):
            lines.append(f"{table}: not in THIRD_PARTY_NOTICES.md: {' | '.join(row)}")
        for row in sorted(recorded[table] - found[table]):
            lines.append(f"{table}: in THIRD_PARTY_NOTICES.md, not found by the scan: {' | '.join(row)}")
    names = {jar_name(ga, v) for ga, v, _ in found["java"]}
    for jar in sorted(set(found["jars"]) ^ names):
        lines.append(f"java: target/lib and the license list disagree on {jar}")
    return lines


def text_differences(found, texts):
    """Where the texts table and the scan disagree: a jar of the image with no license or notice file
    of its own that the table gives no text, and a jar the table gives one that has its own."""
    java = {(ga, version) for ga, version, _ in found["java"]}
    rows = {jar_name(component, version) for component, version, _ in texts if (component, version) in java}
    lines = [f"texts: {jar} carries no license or notice file, and the texts table gives it no text"
             for jar in sorted(set(found["without_license_files"]) - rows)]
    lines += [f"texts: {jar} carries a license or notice file of its own; the texts table need not give it one"
              for jar in sorted(rows - set(found["without_license_files"]))]
    return lines


# --- the image's /licenses/ --------------------------------------------------------------------

def spdx_parts(expression):
    """The licenses an SPDX expression of the record may be taken under, as recognized_license names them."""
    return [re.sub(r"-(only|or-later)$", "", part.strip("() ")) for part in expression.split(" OR ")]


def image_licenses(texts=None):
    """What the server's image holds under /licenses/, by the record: {path under /licenses/: path in the
    tree}."""
    files = {name: name for name in OWN_FILES}
    for names in (texts_table() if texts is None else texts).values():
        files.update({f"third-party/{name}": f"{TEXTS_DIR}/{name}" for name in names})
    return files


def copied_into_licenses(dockerfile=DOCKERFILE, root=REPO):
    """What the Dockerfile copies from the build context to under /licenses/ ({path under /licenses/:
    path in the context}): its COPY instructions without --from, a source ending in "/" being the
    directory's files."""
    with open(dockerfile, encoding="utf-8") as f:
        lines = f.read().splitlines()
    found = {}
    for line in lines:
        words = line.split()
        if not words or words[0].upper() != "COPY" or any(w.startswith("--from=") for w in words):
            continue
        *sources, destination = [w for w in words[1:] if not w.startswith("--")]
        if not destination.startswith(IMAGE_LICENSES):
            continue
        under = destination[len(IMAGE_LICENSES):]
        for source in sources:
            if source.endswith("/"):
                found.update({under + name: source + name for name in sorted(os.listdir(os.path.join(root, source)))})
            else:
                found[under + os.path.basename(source)] = source
    return found


def image_files(image):
    """The content of a local image's /licenses/, without running it: {path under /licenses/: (mode,
    bytes)} for its files, and {path: mode} for its directories ("" for /licenses/ itself); both empty
    for an image without /licenses/."""
    container = subprocess.run(["docker", "create", image], check=True, capture_output=True, text=True).stdout.strip()
    try:
        copied = subprocess.run(["docker", "cp", f"{container}:{IMAGE_LICENSES}", "-"], capture_output=True, check=False)
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False)
    files, directories = {}, {}
    if copied.returncode != 0:
        error = copied.stderr.decode("utf-8", "replace").strip()
        # Docker's answer when the path is not in the container; anything else is a failure to look.
        if re.search(r"Could not find the file|No such container:path", error):
            return files, directories
        raise RuntimeError(f"docker cp {IMAGE_LICENSES} out of {image}: {error}")
    with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as t:
        for member in t.getmembers():
            path = member.name.split("/", 1)[1] if "/" in member.name else ""
            if member.isdir():
                directories[path] = member.mode
            elif member.isfile():
                files[path] = (member.mode, t.extractfile(member).read())
            else:
                files[path] = (member.mode, None)
    return files, directories


def image_differences(files, directories, expected, root=REPO):
    """Where an image's /licenses/ (image_files) is not what <expected> (image_licenses) says, its files
    as they are in the tree <root>, readable by the image's unprivileged user."""
    lines = [f"image: /licenses/{path} is missing (the record's {expected[path]})" for path in sorted(set(expected) - set(files))]
    lines += [f"image: /licenses/{path} is not in the record" for path in sorted(set(files) - set(expected))]
    for path in sorted(set(files) & set(expected)):
        mode, data = files[path]
        with open(os.path.join(root, expected[path]), "rb") as f:
            if data != f.read():
                lines.append(f"image: /licenses/{path} is not {expected[path]} as it is in the tree")
        if mode & 0o444 != 0o444:
            lines.append(f"image: /licenses/{path} is not readable by every user (mode {mode:o})")
    for path, mode in sorted(directories.items()):
        if mode & 0o555 != 0o555:
            lines.append(f"image: /licenses/{path} is a directory not every user can read (mode {mode:o})")
    return lines


# --- writing the tables ------------------------------------------------------------------------

def tables(found):
    """The three tables' Markdown, from a scan."""
    java = ["| Artifact | Version | Declared license |", "| --- | --- | --- |"]
    java += [f"| `{ga}` | {v} | {lic} |" for ga, v, lic in sorted(found["java"])]
    groups = {}
    for jar, lic, component in found["bundled"]:
        groups.setdefault((jar, lic), []).append(component)
    bundled = ["| Jar | License, as the jar names it | Bundled |", "| --- | --- | --- |"]
    bundled += [f"| `{jar}` | {lic} | {', '.join(f'`{c}`' for c in sorted(cs))} |"
                for (jar, lic), cs in sorted(groups.items())]
    npm = ["| Package | Version | Declared license | Installed as |", "| --- | --- | --- | --- |"]
    npm += [f"| `{n}` | {v} | {lic} | {kind} |" for n, v, lic, kind in sorted(found["npm"])]
    return {"java": java, "bundled": bundled, "npm": npm}


def update(found, path=NOTICES):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for name, lines in tables(found).items():
        attrs = f" pom={found['pom']} " if name == "java" else " "
        replacement = f"<!-- licenses:{name}:start{attrs}-->\n\n" + "\n".join(lines) + f"\n\n<!-- licenses:{name}:end -->"
        text, count = marked(name).subn(lambda _: replacement, text)
        if count != 1:
            raise unmarked(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--update", action="store_true")
    s.add_argument("--out")
    i = sub.add_parser("image")
    i.add_argument("image")
    b = sub.add_parser("blob")
    b.add_argument("file")
    args = parser.parse_args(argv)
    if args.command == "blob":
        print(git_blob(args.file))
        return 0
    if args.command == "image":
        expected = image_licenses()
        lines = image_differences(*image_files(args.image), expected)
        for line in lines:
            print(line, file=sys.stderr)
        if lines:
            return 1
        print(f"{args.image}: /licenses/ holds the record's {len(expected)} files, as the tree has them")
        return 0
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.abspath(args.out or os.path.join(REPO, ".state", "licenses", stamp))
    found = scan(out)
    print(f"scanned: {len(found['java'])} jars, {len(found['bundled'])} bundled components, "
          f"{len(found['npm'])} npm packages; raw results in {out}")
    print(f"jars with no license or notice file ({len(found['without_license_files'])}; the findings and the texts table name them): "
          + ", ".join(found["without_license_files"]))
    if args.update:
        update(found)
        print("THIRD_PARTY_NOTICES.md: the java, bundled and npm tables rewritten; review the diff and the findings")
        return 0
    lines = differences(found, record()) + text_differences(found, texts_table())
    for line in lines:
        print(line, file=sys.stderr)
    if lines:
        return 1
    print("THIRD_PARTY_NOTICES.md is the scan's")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
