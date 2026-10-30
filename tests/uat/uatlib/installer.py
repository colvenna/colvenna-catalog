"""install.sh as an upgrade, rollback or restore case runs it: from a given checkout (the candidate's
tag, the previous release's, or a commit from source), with a configuration file, its output kept,
and what UAT-DEP-01 calls install-exit, every-step-done and runs-the-candidate judged from it and
from what then runs. A checkout is given as <dir>; its mode is --release <candidate> when the
checkout's HEAD carries release/<candidate>/manifest.json for a tag at HEAD, else --from-source."""

import json
import os
import re
import subprocess
from pathlib import Path

STEP = re.compile(r"^== (.+)$", re.M)
FAILED = re.compile(r'failed at step "([^"]+)"')


class Checkout:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not (self.path / "install.sh").exists():
            raise ValueError(f"{path}: no install.sh, not a checkout of the catalog")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()

    @property
    def head(self):
        return self.git("rev-parse", "HEAD")

    @property
    def candidate(self):
        """The release candidate the checkout is the tag of, or None (a source checkout)."""
        for tag in self.git("tag", "--points-at", "HEAD").split():
            if (self.path / "release" / tag / "manifest.json").exists():
                return tag
        return None

    def manifest(self):
        c = self.candidate
        return json.loads((self.path / "release" / c / "manifest.json").read_text()) if c else None

    def migrations(self):
        """The released migrations (Migrations.VERSIONS) of the checkout's server."""
        text = (self.path / "src" / "main" / "java" / "com" / "colvenna" / "catalog" / "db" / "Migrations.java").read_text()
        m = re.search(r"VERSIONS\s*=\s*List\.of\(([^)]*)\)", text)
        return re.findall(r'"([^"]+)"', m.group(1)) if m else []

    def args(self):
        c = self.candidate
        return ["--release", c] if c else ["--from-source"]


def run(ctx, checkout, config, keep, extra=()):
    """Runs install.sh --yes from the checkout with the configuration; returns (exit code, output)."""
    env = dict(os.environ)
    env.setdefault("JAVA_HOME", "/usr/lib/jvm/java-21-openjdk-arm64")
    if Path(env["JAVA_HOME"]).is_dir():
        env["PATH"] = f"{env['JAVA_HOME']}/bin{os.pathsep}{env.get('PATH', '')}"
    args = ["./install.sh", "--config", str(Path(config).resolve()), *checkout.args(), "--yes", *extra]
    ctx.log(f"install.sh from {checkout.path.name} ({checkout.candidate or 'source ' + checkout.head[:12]}): "
            f"{' '.join(args[1:])}")
    p = subprocess.run(args, cwd=str(checkout.path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=7200)
    ctx.keep(keep, p.stdout)
    return p.returncode, p.stdout


def steps_done(output):
    """(ok, detail): every step announced, none failed, the install's last line printed."""
    steps = STEP.findall(output)
    failed = FAILED.findall(output)
    done = any("is installed" in line for line in output.splitlines()[-5:])
    return bool(steps) and not failed and done, f"steps {steps}; failed {failed}; installed line {done}"


def running(ctx, namespace, release):
    """[(pod, image, release annotation, source-sha annotation)] of the install's running pods."""
    pods = ctx.kube.json("get", "pods", "-n", namespace, "-l",
                         f"app.kubernetes.io/name=colvenna-catalog,app.kubernetes.io/instance={release}")["items"]
    out = []
    for p in pods:
        if p["metadata"].get("deletionTimestamp") or p["status"].get("phase") != "Running":
            continue
        ann = p["metadata"].get("annotations", {})
        image = next((c.get("imageID") or c.get("image") for c in p["status"].get("containerStatuses", [])
                      if c["name"] == "colvenna-catalog"), "")
        out.append((p["metadata"]["name"], image, ann.get("colvenna-catalog/release"), ann.get("colvenna-catalog/source-sha")))
    return out
