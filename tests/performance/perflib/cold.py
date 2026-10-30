"""P4's forced cold starts (terms.md): before each round, new execution environments, as a deploy
makes them. The server function's configuration gets an environment variable of the test's own
(PERF_ROUND, which the server does not read), a new version is published from it and the alias
`live`, which the API invokes, is moved to it; the authorizer, which the API invokes unqualified,
gets the same variable, so that its next invocations start new environments too. Each round's
clients use tokens never used before, so that the gateway's authorizer cache (60 s) answers none.

Afterwards everything is put back and checked: the alias on the installed version, both functions'
configuration as it was, the test's versions deleted. SnapStart (P4b) is switched by install.sh
itself, with the install's own configuration and COLVENNA_SNAPSTART=on, and back."""

import json
import subprocess
import time

from .collect import function_name

VARIABLE = "PERF_ROUND"


def configuration(target, fn):
    return target.aws("lambda", "get-function-configuration", "--function-name", fn)


def state(target):
    """What P4 changes, recorded before and compared after: each function's environment and
    SnapStart, the server's versions and the alias."""
    server, authorizer = function_name(target, "Server"), function_name(target, "Authorizer")
    s, a = configuration(target, server), configuration(target, authorizer)
    alias = target.aws("lambda", "get-alias", "--function-name", server, "--name", "live")
    versions = target.aws("lambda", "list-versions-by-function", "--function-name", server)["Versions"]
    return {
        "server": {"name": server, "environment": (s.get("Environment") or {}).get("Variables", {}),
                   "snapStart": (s.get("SnapStart") or {}).get("ApplyOn"), "codeSha256": s.get("CodeSha256")},
        "authorizer": {"name": authorizer, "environment": (a.get("Environment") or {}).get("Variables", {})},
        "alias": alias.get("FunctionVersion"),
        "versions": sorted(v["Version"] for v in versions if v["Version"] != "$LATEST"),
    }


def _wait_updated(target, fn):
    target.aws.raw("lambda", "wait", "function-updated-v2", "--function-name", fn, timeout=900)


def _set_environment(target, fn, variables):
    path = target.scratch / f"env-{fn}.json"
    path.write_text(json.dumps({"Variables": variables}))
    path.chmod(0o600)
    _wait_updated(target, fn)
    target.aws("lambda", "update-function-configuration", "--function-name", fn, "--environment", f"file://{path}")
    _wait_updated(target, fn)
    path.unlink()


def new_environments(target, before, tag):
    """A new version of the server with PERF_ROUND=tag, the alias moved to it, and the authorizer's
    configuration changed the same way; returns the version and how long it took."""
    started = time.time()
    server, authorizer = before["server"]["name"], before["authorizer"]["name"]
    _set_environment(target, server, dict(before["server"]["environment"], **{VARIABLE: tag}))
    version = target.aws("lambda", "publish-version", "--function-name", server,
                         "--description", f"performance run {target.run}: {tag}")["Version"]
    # SnapStart takes its snapshot while the version is published: it is ready once Active.
    target.aws.raw("lambda", "wait", "published-version-active", "--function-name", server, "--qualifier", version,
                   timeout=1800)
    target.aws("lambda", "update-alias", "--function-name", server, "--name", "live", "--function-version", version)
    _set_environment(target, authorizer, dict(before["authorizer"]["environment"], **{VARIABLE: tag}))
    conf = target.aws("lambda", "get-function-configuration", "--function-name", server, "--qualifier", version)
    return {"version": version, "seconds": round(time.time() - started, 1),
            "snapStart": (conf.get("SnapStart") or {}).get("ApplyOn"),
            "optimization": (conf.get("SnapStart") or {}).get("OptimizationStatus")}


def restore(target, before, made):
    """The alias back on the installed version, both configurations as they were, the test's
    versions deleted; returns (ok, detail) from what is read back."""
    server, authorizer = before["server"]["name"], before["authorizer"]["name"]
    target.aws("lambda", "update-alias", "--function-name", server, "--name", "live",
               "--function-version", before["alias"])
    for fn, part in ((server, "server"), (authorizer, "authorizer")):
        if VARIABLE in (configuration(target, fn).get("Environment") or {}).get("Variables", {}):
            _set_environment(target, fn, before[part]["environment"])
    for version in made:
        if version != before["alias"] and version not in before["versions"]:
            target.aws.raw("lambda", "delete-function", "--function-name", server, "--qualifier", version, check=False)
    after = state(target)
    problems = []
    if after["alias"] != before["alias"]:
        problems.append(f"alias live on {after['alias']}, not {before['alias']}")
    for part in ("server", "authorizer"):
        if after[part]["environment"] != before[part]["environment"]:
            problems.append(f"{part}'s environment differs")
    if after["versions"] != before["versions"]:
        problems.append(f"versions {after['versions']}, before {before['versions']}")
    return not problems, "; ".join(problems) or (
        f"alias live on version {after['alias']}, both configurations as before, {len(made)} test versions deleted")


def install(target, checkout, source, snapstart):
    """install.sh of the checkout, with the install's configuration and COLVENNA_SNAPSTART set as
    given (a copy of the configuration in the run's scratch directory), its confirmation given by
    --yes; its output is kept, sanitized, in install-snapstart.log. Returns its exit status."""
    config = target.install.config_path.read_text(encoding="utf-8")
    lines = [line for line in config.splitlines() if not line.startswith("COLVENNA_SNAPSTART=")]
    lines.append(f"COLVENNA_SNAPSTART={snapstart}")
    path = target.scratch / f"{target.install.env}-snapstart-{snapstart}.env"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    args = [str(checkout / "install.sh"), "--config", str(path), "--yes"] + source
    try:
        p = subprocess.run(args, cwd=str(checkout), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=5400)
    finally:
        path.unlink()
    target.installs = getattr(target, "installs", "") + (
        f"$ install.sh --config <its configuration, COLVENNA_SNAPSTART={snapstart}> --yes {' '.join(source)}\n"
        f"{p.stdout}\n(exit {p.returncode})\n")
    target.keep("install-snapstart.log", target.installs)
    return p.returncode


def snapstart_of_live(target):
    server = function_name(target, "Server")
    conf = target.aws("lambda", "get-function-configuration", "--function-name", server, "--qualifier", "live")
    return (conf.get("SnapStart") or {}).get("ApplyOn")
