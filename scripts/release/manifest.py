#!/usr/bin/env python3
"""A release candidate's manifest, release/<candidate>/manifest.json: what the candidate pins.

make-manifest.sh writes it at the commit the candidate is built from (B); install.sh --release
installs from it. It pins the server's image and its Helm chart by digest, each in its repository of
the release registry (colvenna-catalog-release/colvenna-catalog, tagged <candidate>, and
colvenna-catalog-release/charts/colvenna-catalog, an OCI artifact tagged with the chart's version,
<candidate> without its "v"); the git objects of the chart directory, install.sh, uninstall.sh,
deploy/lib and the CDK app at B; the UAT cases document's version, or that it is absent (until #19
writes it), and the required cases it lists; the performance terms' version (the scenarios, method
and targets of the performance acceptance, confirmed by the requester), or that it is absent; and the
configurations the candidate is accepted in (the deployment forms, two or more standard Regions). A manifest names no registry and no AWS account: the release
registry, <account>.dkr.ecr.<region>.amazonaws.com, is the installer's setting
COLVENNA_RELEASE_REGISTRY, whichever account and Region hold a copy of the candidate.

A manifest is valid when its every field is as the schema says (problems); it is fit for GO only
when the UAT cases document and the performance terms exist and the required cases are fixed too
(go_problems).

usage:
  manifest.py assemble <work>                 the manifest, from what make-manifest.sh gathered in
                                              <work> (see assemble), on stdout
  manifest.py validate <manifest> [--candidate <candidate>] [--go]
                                              --go: also what a candidate for GO still lacks
  manifest.py check-tree <manifest> <repo> <commit>
                                              the manifest's git objects are the commit's
  manifest.py names <candidate>               image and chart: repository, tag, tab-separated
  manifest.py repositories                    the release registry's repositories, one per line
  manifest.py paths                           the paths whose git objects a manifest pins
  manifest.py forms <form>,<form>...          the deployment forms, one per line, if they are distinct
                                              forms of FORMS
  manifest.py regions <region>,<region>...    the Regions, one per line, if they are two or more
                                              distinct standard ones
  manifest.py registry <registry>             its account and Region, tab-separated, if it is an ECR
                                              registry of a standard Region (COLVENNA_RELEASE_REGISTRY)
  manifest.py stack                           the release registry's stack
  manifest.py get <manifest> <field.field...> one value
  manifest.py required-cases <cases.md>       the required cases, one per line
  manifest.py notes <manifest> < template     the release notes' draft, from the template
Problems go to stderr, with exit status 1.
"""
import json
import os
import re
import subprocess
import sys

SCHEMA = 1
# The release registry's stack (deploy/aws/src/app.ts) and its repositories (release-stack.ts).
STACK = "ColvennaCatalogRelease"
PREFIX = "colvenna-catalog-release"
IMAGE_REPOSITORY = f"{PREFIX}/colvenna-catalog"
CHART_NAME = "colvenna-catalog"
# helm push <chart> oci://<registry>/colvenna-catalog-release/charts puts the chart here.
CHART_REPOSITORY = f"{PREFIX}/charts/{CHART_NAME}"
CHART_PATH = f"deploy/helm/{CHART_NAME}"
# The repository the candidates are built in. Those made before it moved to the colvenna organization
# record its address of the time, github.com/<owner>/colvenna-catalog, and keep it as released.
SOURCE = "https://github.com/colvenna/colvenna-catalog"
BEFORE_THE_MOVE = ("v1.0.0-rc.1",)
SOURCE_BEFORE_THE_MOVE = re.compile(r"https://github\.com/[A-Za-z0-9-]+/colvenna-catalog")
# The nodes the image runs on (EKS's x86_64 Linux).
PLATFORM = "linux/amd64"
# The deployment forms a candidate may be accepted in: the EKS form, and the serverless one (#13).
FORMS = ("eks", "serverless")
CDK_PATH = "deploy/aws"
CASES_PATH = "docs/uat/cases.md"
PERFORMANCE_PATH = "docs/performance/terms.md"
# The documents a manifest pins by version, each present (its commit and blob at B) or absent:
# (field, path).
DOCUMENTS = (("uat_cases", CASES_PATH), ("performance", PERFORMANCE_PATH))
# Where in the manifest each pinned path's git object goes: (section, key or None, path, kind).
PINNED = (
    ("installer", "install", "install.sh", "blob"),
    ("installer", "uninstall", "uninstall.sh", "blob"),
    ("installer", "lib", "deploy/lib", "tree"),
    ("cdk", None, CDK_PATH, "tree"),
    ("chart", None, CHART_PATH, "tree"),
)
PINNED_PATHS = tuple(path for _, _, path, _ in PINNED)

CANDIDATE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.[1-9][0-9]*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
CASE = re.compile(r"UAT-[A-Z]+-[0-9]{2}")
# A Region of the standard partition: three parts (GovCloud's have four), not China's.
REGION = re.compile(r"^(?!cn-)[a-z]{2}-[a-z]+-[1-9]$")
REGISTRY = re.compile(r"^([0-9]{12})\.dkr\.ecr\.((?!cn-)[a-z]{2}-[a-z]+-[1-9])\.amazonaws\.com$")
# An AWS account ID: twelve digits standing alone (inside a digest or a SHA they run on into others).
ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])")
START, END = "<!-- required-cases:start -->", "<!-- required-cases:end -->"
WHY_GO = "#19 writes it"


def read(path):
    with open(path) as f:
        return f.read()


def candidate_problems(name):
    if isinstance(name, str) and CANDIDATE.fullmatch(name):
        return []
    return [f"candidate: {name!r} is not v<major>.<minor>.<patch>-rc.<n> (n from 1)"]


def chart_version(candidate):
    """The chart's version, and so its tag, in a candidate: the candidate without its "v"."""
    return candidate[1:]


def names(candidate):
    """(what, repository, tag) of the candidate's image and chart in the release registry."""
    found = candidate_problems(candidate)
    if found:
        raise ValueError(found[0])
    return [("image", IMAGE_REPOSITORY, candidate), ("chart", CHART_REPOSITORY, chart_version(candidate))]


def source_tag(candidate):
    return f"{candidate} points at built_from with release/{candidate}/ added, nothing else"


def build(inputs):
    """The manifest from assemble's inputs; ValueError if it is not a valid one:
      candidate, built_from, generated   the candidate, the commit B and when (UTC, ...Z)
      image_digest, chart_digest         what make-manifest.sh pushed into the release registry
      objects                            {path: git object id at B} of each of PINNED_PATHS
      uat_cases                          None while docs/uat/cases.md is absent at B, else its
                                         {commit, blob}
      required_cases                     the case IDs it lists as required ([] without it)
      performance                        None (or no key) while docs/performance/terms.md is absent
                                         at B, else its {commit, blob}
      regions                            the Regions the candidate is accepted in
      forms                              the deployment forms it is accepted in (["eks"] if absent)
    """
    candidate, objects = inputs["candidate"], inputs["objects"]
    sections = {"installer": {}, "cdk": {}, "chart": {}}
    for section, key, path, kind in PINNED:
        if path not in objects:
            raise ValueError(f"no git object for {path}")
        value = {"path": path, kind: objects[path]}
        if key is None:
            sections[section].update(value)
        else:
            sections[section][key] = value
    documents = {}
    for key, path in DOCUMENTS:
        value = inputs.get(key)
        documents[key] = {"path": path, "status": "absent"} if value is None else \
            {"path": path, "status": "present", "commit": value["commit"], "blob": value["blob"]}
    manifest = {
        "schema": SCHEMA,
        "candidate": candidate,
        "source": {"repository": SOURCE, "built_from": inputs["built_from"], "tag": source_tag(candidate)},
        "generated": inputs["generated"],
        "image": {"repository": IMAGE_REPOSITORY, "tag": candidate, "digest": inputs["image_digest"],
                  "platform": PLATFORM},
        "chart": {"name": CHART_NAME, **sections["chart"], "version": chart_version(candidate),
                  "appVersion": candidate, "repository": CHART_REPOSITORY, "digest": inputs["chart_digest"]},
        "installer": sections["installer"],
        "cdk": sections["cdk"],
        "uat_cases": documents["uat_cases"],
        "required_cases": inputs["required_cases"],
        "performance": documents["performance"],
        "configurations": {"forms": list(inputs.get("forms", ["eks"])), "regions": inputs["regions"]},
    }
    found = problems(manifest)
    if found:
        raise ValueError("; ".join(found))
    return manifest


def assemble(work):
    """The manifest from what make-manifest.sh gathered in the directory work, at the candidate's commit:
      params.json  candidate, built_from, generated, image_digest, chart_digest, objects, uat_cases,
                   performance, forms and regions, as build takes them
      cases.md     docs/uat/cases.md at that commit, when uat_cases is not null: its required cases
    """
    params = json.loads(read(os.path.join(work, "params.json")))
    cases = []
    if params["uat_cases"] is not None:
        cases = required_cases(read(os.path.join(work, "cases.md")))
    return build({**params, "required_cases": cases})


def problems(m, candidate=None):
    """What is wrong with a manifest: an empty list when nothing is."""
    found = []

    def need(ok, what):
        if not ok:
            found.append(what)

    def fields(value, where, expected):
        """Whether value is an object with exactly those fields; each one missing or extra is a problem."""
        if not isinstance(value, dict):
            found.append(f"{where or 'the manifest'}: not an object")
            return False
        prefix = f"{where}." if where else ""
        for name in expected:
            need(name in value, f"{prefix}{name}: missing")
        for name in value:
            need(name in expected, f"unknown field {prefix}{name}")
        return all(name in value for name in expected)

    def equal(value, where, expected):
        need(value == expected, f"{where}: {value!r}, not {expected!r}")

    def matches(value, where, pattern, what):
        need(isinstance(value, str) and pattern.fullmatch(value) is not None, f"{where}: {value!r} is not {what}")

    def sha(value, where):
        matches(value, where, SHA, "a full git object id (40 lowercase hex digits)")

    def digest(value, where):
        matches(value, where, DIGEST, "sha256:<64 lowercase hex digits>")

    top = ("schema", "candidate", "source", "generated", "image", "chart", "installer", "cdk", "uat_cases",
           "required_cases", "performance", "configurations")
    if not fields(m, "", top):
        return found
    need(type(m["schema"]) is int and m["schema"] == SCHEMA, f"schema: {m['schema']!r}, not {SCHEMA}")
    name = m["candidate"]
    found.extend(candidate_problems(name))
    if candidate is not None:
        need(name == candidate, f"candidate: the manifest is {name!r}'s, not {candidate}'s")
    named = isinstance(name, str) and CANDIDATE.fullmatch(name) is not None
    if fields(m["source"], "source", ("repository", "built_from", "tag")):
        repository = m["source"]["repository"]
        earlier = name in BEFORE_THE_MOVE and isinstance(repository, str) \
            and SOURCE_BEFORE_THE_MOVE.fullmatch(repository) is not None
        if not earlier:
            equal(repository, "source.repository", SOURCE)
        sha(m["source"]["built_from"], "source.built_from")
        if named:
            equal(m["source"]["tag"], "source.tag", source_tag(name))
    matches(m["generated"], "generated", UTC, "a UTC time, YYYY-MM-DDTHH:MM:SSZ")

    if fields(m["image"], "image", ("repository", "tag", "digest", "platform")):
        equal(m["image"]["repository"], "image.repository", IMAGE_REPOSITORY)
        if named:
            equal(m["image"]["tag"], "image.tag", name)
        digest(m["image"]["digest"], "image.digest")
        equal(m["image"]["platform"], "image.platform", PLATFORM)
    chart = m["chart"]
    if fields(chart, "chart", ("name", "path", "tree", "version", "appVersion", "repository", "digest")):
        equal(chart["name"], "chart.name", CHART_NAME)
        if named:
            equal(chart["version"], "chart.version", chart_version(name))
            equal(chart["appVersion"], "chart.appVersion", name)
        equal(chart["repository"], "chart.repository", CHART_REPOSITORY)
        digest(chart["digest"], "chart.digest")
    if fields(m["installer"], "installer", ("install", "uninstall", "lib")):
        for section, key, path, kind in PINNED:
            if section == "installer":
                where = f"installer.{key}"
                if fields(m["installer"][key], where, ("path", kind)):
                    equal(m["installer"][key]["path"], f"{where}.path", path)
                    sha(m["installer"][key][kind], f"{where}.{kind}")
    fields(m["cdk"], "cdk", ("path", "tree"))
    for section in ("cdk", "chart"):
        value = m[section]
        if isinstance(value, dict) and "path" in value and "tree" in value:
            path = next(p for s, _, p, _ in PINNED if s == section)
            equal(value["path"], f"{section}.path", path)
            sha(value["tree"], f"{section}.tree")

    def document(key, path):
        """Whether the pinned document is present (True), absent (False), or neither (None, a problem)."""
        value = m[key]
        if isinstance(value, dict) and value.get("status") in ("absent", "present"):
            present = value["status"] == "present"
            if fields(value, key, ("path", "status", "commit", "blob") if present else ("path", "status")):
                equal(value["path"], f"{key}.path", path)
                if present:
                    sha(value["commit"], f"{key}.commit")
                    sha(value["blob"], f"{key}.blob")
            return present
        found.append(f"{key}.status: {value.get('status')!r}, not absent or present" if isinstance(value, dict)
                     else f"{key}: not an object")
        return None

    present = document("uat_cases", CASES_PATH)
    document("performance", PERFORMANCE_PATH)
    required = m["required_cases"]
    if not isinstance(required, list) or not all(isinstance(c, str) and CASE.fullmatch(c) for c in required) \
            or len(set(required)) != len(required):
        found.append("required_cases: not a list of distinct UAT-<AREA>-<nn> IDs")
    elif present is True and not required:
        found.append(f"required_cases: none, though {CASES_PATH} is present: the cases it lists as required")
    elif present is False and required:
        found.append(f"required_cases: {len(required)}, though {CASES_PATH} is absent, which lists them")

    if fields(m["configurations"], "configurations", ("forms", "regions")):
        forms = m["configurations"]["forms"]
        need(isinstance(forms, list) and len(forms) > 0 and all(isinstance(f, str) for f in forms)
             and set(forms) <= set(FORMS) and len(set(forms)) == len(forms),
             f"configurations.forms: {forms!r} is not some of {', '.join(FORMS)}, each once")
        found.extend(f"configurations.{p}" for p in region_problems(m["configurations"]["regions"]))
    accounts = sorted(set(ACCOUNT_ID.findall(json.dumps(m))))
    need(not accounts, f"{len(accounts)} AWS account id{'s' if len(accounts) > 1 else ''}: a manifest names"
         " repositories and digests; the release registry is the installer's COLVENNA_RELEASE_REGISTRY")
    return found


def go_problems(m):
    """What a valid manifest still lacks for a candidate that can be GO (design 9.1 to 9.3): the UAT
    cases document, the required case set fixed before UAT runs, and the performance terms fixed
    before the performance acceptance runs."""
    found = []
    if m["uat_cases"]["status"] != "present":
        found.append(f"uat_cases: {CASES_PATH} is absent at built_from: a candidate for GO needs the UAT cases"
                     f" document ({WHY_GO})")
    if not m["required_cases"]:
        found.append("required_cases: empty: a candidate for GO needs its required case set, fixed before UAT"
                     f" ({WHY_GO})")
    if m["performance"]["status"] != "present":
        found.append(f"performance: {PERFORMANCE_PATH} is absent at built_from: a candidate for GO needs the"
                     " performance terms its acceptance is judged by, confirmed before it runs (design 9.3)")
    return found


def region_problems(regions):
    """What is wrong with the Regions a candidate is accepted in: two or more distinct standard ones."""
    if not (isinstance(regions, list) and all(isinstance(r, str) for r in regions)):
        return [f"regions: {regions!r} is not a list of Region names"]
    bad = [r for r in regions if REGION.fullmatch(r) is None]
    if bad:
        return [f"regions: not standard AWS Regions: {', '.join(bad)}"]
    if len(set(regions)) != len(regions) or len(regions) < 2:
        return [f"regions: {', '.join(regions) or 'none'}: not two or more distinct Regions"]
    return []


def git(repo, *args):
    """git's output, or None when it fails (an object that is not there)."""
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def tree_problems(m, repo, commit):
    """Where the manifest's git objects are not the commit's: install.sh --release runs from a
    checkout of the candidate's tag, which must hold what the manifest says it pins."""
    found = []
    for section, key, path, kind in PINNED:
        where = f"{section}.{key}" if key else section
        value = m[section][key] if key else m[section]
        have = git(repo, "rev-parse", f"{commit}:{path}")
        if have != value[kind]:
            found.append(f"{where}: {path} is {have or 'not there'} at {commit}, not the manifest's {value[kind]}")
    for key, path in DOCUMENTS:
        have = git(repo, "rev-parse", f"{commit}:{path}")
        value = m[key]
        if value["status"] == "absent" and have is not None:
            found.append(f"{key}: {path} is at {commit}, but the manifest says it is absent")
        elif value["status"] == "present":
            if have != value["blob"]:
                found.append(f"{key}: {path} is {have or 'not there'} at {commit}, not the manifest's {value['blob']}")
            last = git(repo, "log", "-1", "--format=%H", commit, "--", path)
            if last != value["commit"]:
                found.append(f"{key}.commit: {path} last changed in {last or 'no commit'} up to {commit},"
                             f" not the manifest's {value['commit']}")
    return found


def required_cases(text):
    """The case IDs in the first column of the table between the markers, in order. Every row there
    but the table's header and its rule must begin with a case ID: no row is skipped."""
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError(f"the cases need {START} and {END}, once each, around the required cases' table")
    begin, end = text.index(START), text.index(END)
    if end < begin:
        raise ValueError(f"{END} comes before {START}")
    # A row's first cell: UAT-CAT-01, `UAT-CAT-01` or [UAT-CAT-01](#...).
    first = re.compile(r"\[?`?(" + CASE.pattern + r")`?\]?(\([^)]*\))?")
    rows = [line.strip() for line in text[begin + len(START):end].splitlines() if line.strip().startswith("|")]
    cells = [row.strip("|").split("|") for row in rows]
    rule = [all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in row) for row in cells]
    cases = []
    for i, row in enumerate(cells):
        if rule[i] or (i + 1 < len(cells) and rule[i + 1]):
            continue
        found = first.fullmatch(row[0].strip())
        if not found:
            raise ValueError(f"between the required-cases markers, a row that names no case: {rows[i]}")
        if found[1] in cases:
            raise ValueError(f"{found[1]} is listed twice among the required cases")
        cases.append(found[1])
    if not cases:
        raise ValueError(f"no case between {START} and {END}")
    return cases


def notes(m, template):
    """The release notes' draft: the template's {{placeholders}} filled in from the manifest."""
    cases, terms = m["uat_cases"], m["performance"]
    gaps = go_problems(m)
    values = {
        "candidate": m["candidate"],
        "built_from": m["source"]["built_from"],
        "built_from_short": m["source"]["built_from"][:8],
        "date": m["generated"][:10],
        "image_repository": m["image"]["repository"],
        "image_digest": m["image"]["digest"],
        "chart_repository": m["chart"]["repository"],
        "chart_version": m["chart"]["version"],
        "chart_digest": m["chart"]["digest"],
        "chart_tree": m["chart"]["tree"],
        "forms": ", ".join(f"`{f}`" for f in m["configurations"]["forms"]),
        "regions": ", ".join(f"`{r}`" for r in m["configurations"]["regions"]),
        "uat_cases": (f"`{CASES_PATH}` at commit `{cases['commit']}` (blob `{cases['blob']}`), with"
                      f" {len(m['required_cases'])} required cases" if cases["status"] == "present"
                      else f"`{CASES_PATH}` is absent at the commit the candidate is built from ({WHY_GO})"),
        "performance": (f"`{PERFORMANCE_PATH}` at commit `{terms['commit']}` (blob `{terms['blob']}`): the"
                        " scenarios, method and targets the performance acceptance is judged by"
                        if terms["status"] == "present"
                        else f"`{PERFORMANCE_PATH}` is absent at the commit the candidate is built from"),
        "go_gaps": "\n".join(f"- {g}" for g in gaps) if gaps else "- None: the manifest has what a candidate for GO needs.",
    }

    def fill(found):
        if found[1] not in values:
            raise ValueError(f"the notes' template has an unknown placeholder {{{{{found[1]}}}}}")
        return values[found[1]]
    return re.sub(r"\{\{(\w+)\}\}", fill, template)


def load(path):
    try:
        return json.loads(read(path))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: not JSON ({error})") from None


def valid(path, candidate=None, go=False):
    m = load(path)
    found = problems(m, candidate)
    if not found and go:
        found = go_problems(m)
    if found:
        raise ValueError(f"{path}:\n  " + "\n  ".join(found))
    return m


def registry_parts(registry):
    """(account, Region) of an ECR registry in a standard Region; ValueError if it is not one."""
    found = REGISTRY.fullmatch(registry)
    if found is None:
        raise ValueError(f"COLVENNA_RELEASE_REGISTRY={registry!r}: the release registry,"
                         " <account>.dkr.ecr.<region>.amazonaws.com in a standard Region")
    return found[1], found[2]


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    flags = [a for a in args if a == "--go"]
    rest = [a for a in args if a != "--go"]
    if command == "assemble" and len(args) == 1:
        print(json.dumps(assemble(args[0]), indent=2))
    elif command == "validate" and len(rest) in (1, 3) and (len(rest) == 1 or rest[1] == "--candidate"):
        valid(rest[0], rest[2] if len(rest) == 3 else None, go=bool(flags))
    elif command == "check-tree" and len(args) == 3:
        found = tree_problems(valid(args[0]), args[1], args[2])
        if found:
            raise ValueError(f"{args[0]}:\n  " + "\n  ".join(found))
    elif command == "names" and len(args) == 1:
        for row in names(args[0]):
            print("\t".join(row))
    elif command == "repositories" and not args:
        print(IMAGE_REPOSITORY)
        print(CHART_REPOSITORY)
    elif command == "paths" and not args:
        print("\n".join(PINNED_PATHS))
    elif command == "forms" and len(args) == 1:
        forms = args[0].split(",")
        if not (set(forms) <= set(FORMS) and len(set(forms)) == len(forms)):
            raise ValueError(f"forms: {args[0]!r} is not some of {', '.join(FORMS)}, each once")
        print("\n".join(forms))
    elif command == "regions" and len(args) == 1:
        found = region_problems(args[0].split(","))
        if found:
            raise ValueError("; ".join(found))
        print("\n".join(args[0].split(",")))
    elif command == "registry" and len(args) == 1:
        print("\t".join(registry_parts(args[0])))
    elif command == "stack" and not args:
        print(STACK)
    elif command == "get" and len(args) == 2:
        value = load(args[0])
        for field in args[1].split("."):
            if not isinstance(value, dict) or field not in value:
                raise ValueError(f"{args[0]}: no {args[1]}")
            value = value[field]
        print(value if isinstance(value, str) else json.dumps(value))
    elif command == "required-cases" and len(args) == 1:
        print("\n".join(required_cases(read(args[0]))))
    elif command == "notes" and len(args) == 1:
        print(notes(valid(args[0]), sys.stdin.read()), end="")
    else:
        raise ValueError(__doc__.split("usage:\n", 1)[1].split("Problems", 1)[0].rstrip())


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
