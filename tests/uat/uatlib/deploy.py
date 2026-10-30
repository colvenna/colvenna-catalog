"""What the deployment cases (UAT-DEP-*) share: install.sh and uninstall.sh run as their user runs
them, their output kept line by line with its time, and the account and the cluster recorded before
and after, so that a case can judge what a run did and what it left.

  Installer(ctx, checkout)   runs <checkout>/install.sh or uninstall.sh (the candidate's checkout:
                             the repository the harness runs from, unless --installer-checkout names
                             another), with the operator's profile; answers its confirmation on a
                             terminal when asked to, or gives it none; signals it on a condition
  Run                        a run's exit status, its output with times, the steps it announced
                             ("== <step>") and the step it failed in, its evidence directory
  inventory(...)             the install's stacks (status, times, tags, resources and their physical
                             ids), repositories and their images, functions and the alias, secrets,
                             the Helm release, the catalog's pods and Secrets, and the account's
                             other stacks; compare(...) says what changed

The judges are functions of these records alone, so that they are tested offline (test_dep.py)."""

import json
import os
import pty
import re
import select
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .install import REPO, stack_suffix
from .kube import hardened
from .sql import POSTGRES_IMAGE

STEP = re.compile(r"^== (.+)$")
FAILED = re.compile(r'^(install|uninstall)\.sh: failed at step "(.*)" \(status (\d+)\)')
PROMPT = "Type the environment name"
# The steps install.sh announces, in order (install.sh, deploy/lib/serverless.sh).
EKS_STEPS = ("tools", "preflight", "stacks", "image", "namespace", "database", "tls", "chart", "admin", "smoke")
SERVERLESS_STEPS = ("tools", "preflight", "stacks", "image", "functions", "database", "migrate", "admin", "smoke")
# A catalog token's shape (deploy/lib/catalog.sh: colvenna_ and URL-safe base64).
TOKEN = re.compile(r"colvenna_(?!catalog_[a-z_]+\b)[A-Za-z0-9_-]{20,}")  # not the server's metric names


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class Run:
    command: list
    exit_code: int
    lines: list = field(default_factory=list)  # (iso time, text)
    started: str = ""
    finished: str = ""
    evidence: str = ""  # the run's own evidence directory (.state/<env>/evidence/<kind>-<time>)
    signalled: str = ""  # the signal sent, and in which step

    @property
    def text(self):
        return "\n".join(t for _, t in self.lines)

    def steps(self):
        """The steps the run announced, in order."""
        return [m.group(1) for _, t in self.lines if (m := STEP.match(t))]

    def failed_step(self):
        for _, t in self.lines:
            m = FAILED.match(t)
            if m:
                return m.group(2), int(m.group(3))
        return None

    def index(self, pattern):
        """The position of the first line matching the pattern among the run's lines, or None."""
        for i, (_, t) in enumerate(self.lines):
            if re.search(pattern, t):
                return i
        return None

    def first(self, pattern):
        """The time of the first line matching the pattern, or None."""
        for when, t in self.lines:
            if re.search(pattern, t):
                return when
        return None

    def to_json(self):
        return {"command": self.command, "exitCode": self.exit_code, "started": self.started, "finished": self.finished,
                "evidence": self.evidence, "signalled": self.signalled, "steps": self.steps(),
                "failedStep": self.failed_step()}


class Installer:
    """install.sh and uninstall.sh of a checkout, run by the case."""

    def __init__(self, ctx, checkout, config):
        self.ctx = ctx
        self.checkout = Path(checkout)
        self.config = Path(config)
        self.n = 0

    def env(self):
        env = dict(os.environ, AWS_PROFILE=self.ctx.aws.profile)
        env.pop("KUBECONFIG", None)
        return env

    def run(self, script, *args, config=None, answer=None, stdin_text=None, signal_when=None, sig=None,
            timeout=5400, keep=None):
        """Runs <checkout>/<script> --config <config> <args>. answer: the environment's name, typed on a
        terminal when the script asks for it (no --yes); stdin_text: what its stdin holds instead (a
        pipe, so no terminal); otherwise stdin is empty (/dev/null). signal_when: a function of the
        output so far, polled; when it is true, sig goes to the script's whole process group, as a lost
        session or host would end it."""
        self.n += 1
        command = [str(self.checkout / script), "--config", str(config or self.config), *args]
        started = now()
        master = slave = None
        if answer is not None:
            master, slave = pty.openpty()
            stdin = slave
        elif stdin_text is not None:
            stdin = subprocess.PIPE
        else:
            stdin = subprocess.DEVNULL
        p = subprocess.Popen(command, cwd=str(self.checkout), stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=self.env(), start_new_session=True)
        session = _Session(p, master)
        try:
            if slave is not None:
                os.close(slave)
            elif stdin_text is not None:
                p.stdin.write(stdin_text.encode())
                p.stdin.close()
            os.set_blocking(p.stdout.fileno(), False)
            session.follow(answer, signal_when, sig, timeout)
        except BaseException:
            # The harness stopped (a signal, Ctrl-C): the script it started in a session of its own stops too.
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise
        finally:
            if master is not None:
                os.close(master)
            try:
                code = p.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                code = p.wait()
            p.stdout.close()
        lines = session.lines
        run = Run(command=[script, *args], exit_code=code, lines=lines, started=iso(started), finished=iso(now()),
                  signalled=session.signalled)
        run.evidence = str(self.evidence_since(script, started) or "")
        name = keep or f"{script.split('.')[0]}-{self.n}"
        self.ctx.keep(f"{name}.log", "".join(f"{t} {x}\n" for t, x in lines))
        self.ctx.keep(f"{name}.json", run.to_json())
        return run

    def state(self, env):
        return self.checkout / ".state" / env

    def evidence_since(self, script, started):
        """The evidence directory the run made: the newest <kind>-<time> made since it started."""
        env = re.search(r"^COLVENNA_ENV=(\S+)$", self.config.read_text(), re.M)
        if not env:
            return None
        kind = "install" if script.startswith("install") else "uninstall"
        root = self.state(env.group(1)) / "evidence"
        if not root.is_dir():
            return None
        made = [p for p in root.glob(f"{kind}-*") if p.stat().st_mtime >= started.timestamp() - 5]
        return max(made, key=lambda p: p.name) if made else None


@dataclass
class _Session:
    """A script running under the Installer: its process, its terminal (or None), the lines it wrote
    so far with their times, and what the Installer did to it."""
    p: subprocess.Popen
    master: int = None
    lines: list = field(default_factory=list)
    buffer: bytes = b""
    answered: bool = False
    signalled: str = ""

    def _line(self, text):
        self.lines.append((iso(now()), text))

    def follow(self, answer, signal_when, sig, timeout):
        """Reads the script's output until it ends: answers its question, signals it on the condition,
        kills it after the timeout."""
        p, master, deadline = self.p, self.master, time.time() + timeout
        while True:
            fds = [p.stdout] + ([master] if master is not None else [])
            ready, _, _ = select.select(fds, [], [], 2)
            eof = False
            if p.stdout in ready:
                chunk = p.stdout.read()
                eof = chunk == b""  # None: nothing to read yet; b"": the end of its output
                self.buffer += chunk or b""
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    self._line(line.decode("utf-8", "replace").rstrip("\r"))
                # The confirmation's prompt has no newline: it is a line of its own, answered on the terminal.
                if answer is not None and not self.answered and PROMPT.encode() in self.buffer:
                    self._line(self.buffer.decode("utf-8", "replace"))
                    self.buffer = b""
                    os.write(master, (answer + "\n").encode())
                    self._line(f"[harness] typed {answer} on the terminal")
                    self.answered = True
            if master is not None and master in ready:
                try:
                    os.read(master, 4096)  # the terminal's echo of what was typed
                except OSError:
                    pass
            if signal_when and not self.signalled and p.poll() is None and signal_when(self.lines):
                steps = [m.group(1) for _, t in self.lines if (m := STEP.match(t))]
                self.signalled = f"{signal.Signals(sig).name} in step {steps[-1] if steps else '-'} at {iso(now())}"
                self._line(f"[harness] {self.signalled}")
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    pass
            if eof or (p.poll() is not None and not ready):
                p.wait()
                for line in self.buffer.decode("utf-8", "replace").splitlines():
                    self._line(line)
                return
            if time.time() > deadline:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._line(f"[harness] killed after {timeout} s")
                p.wait()
                return


def write_config(path, base, **changes):
    """A configuration: base's lines, each key in changes set to its value (or removed when None),
    keys not in base added at the end."""
    lines, seen = [], set()
    for line in Path(base).read_text().splitlines():
        m = re.match(r"^(COLVENNA_[A-Z0-9_]+)=", line)
        if m and m.group(1) in changes:
            seen.add(m.group(1))
            if changes[m.group(1)] is not None:
                lines.append(f"{m.group(1)}={changes[m.group(1)]}")
            continue
        lines.append(line)
    lines += [f"{k}={v}" for k, v in changes.items() if k not in seen and v is not None]
    path = Path(path)
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


# --- judging a run ------------------------------------------------------------------------------------

def every_step_done(run, form, nlb=False):
    """(ok, detail): every step announced in order, none failed, and the smoke check passed."""
    want = list(EKS_STEPS if form == "eks" else SERVERLESS_STEPS)
    if form == "eks" and nlb:
        want.insert(want.index("admin"), "internal NLB")
    steps = run.steps()
    # The serverless form's upgrade moves the server last: a step "server" may come after migrate.
    got = [s for s in steps if s != "server"]
    failed = run.failed_step()
    smoke = re.search(r"(anonymous=401 admin=200|anonymous=401 forged=401 admin=200)", run.text) is not None
    ok = got == want and not failed and run.exit_code == 0 and smoke
    return ok, (f"steps {', '.join(steps)}; failed {failed}; smoke {'passed' if smoke else 'not seen'}; "
                f"exit {run.exit_code}")


def summary_before_change(run, form, candidate=None, built_from=None):
    """(ok, detail): the summary printed and the confirmation asked and answered before the first step
    that changes anything ("stacks")."""
    names = ("account", "region", "cluster" if form == "eks" else "network", "version", "bucket", "database")
    have = {n: any(re.match(rf"^  {n}\s", t) for _, t in run.lines) for n in names}
    asked = run.index(PROMPT)
    stacks = run.index(r"^== stacks$")
    version = next((t for _, t in run.lines if re.match(r"^  version\s", t)), "")
    named = candidate is None or (f"release {candidate} (built from {built_from}" in version if built_from
                                   else f"release {candidate}" in version)
    ok = all(have.values()) and asked is not None and stacks is not None and asked < stacks and named
    return ok, (f"summary lines {sorted(k for k, v in have.items() if v)}; version line '{version.strip()}'; "
                f"asked at line {asked}, at {run.first(PROMPT)}; first change ('== stacks') at line {stacks}")


def token_leaks(texts, tokens=()):
    """The texts (name -> content) that hold a catalog token or one of the given tokens."""
    found = []
    for name, text in texts.items():
        if TOKEN.search(text) or any(t and t in text for t in tokens):
            found.append(name)
    return found


def evidence_texts(directory):
    """{relative path: text} of every file of an evidence directory."""
    out = {}
    root = Path(directory)
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if path.is_file():
            try:
                out[str(path.relative_to(root))] = path.read_text(errors="replace")
            except OSError:
                pass
    return out


# --- the account and the cluster ----------------------------------------------------------------------

def stacks_of(aws, env):
    """{stack: {status, created, updated, tags, resources: {logical id: (type, physical id)}}} of the
    install's stacks (ColvennaCatalog<Env><Part>)."""
    prefix = f"ColvennaCatalog{stack_suffix(env)}"
    out = {}
    summaries = aws("cloudformation", "list-stacks", "--query",
                    f"StackSummaries[?starts_with(StackName, '{prefix}') && StackStatus!='DELETE_COMPLETE']")
    for s in summaries or []:
        name = s["StackName"]
        part = name[len(prefix):]
        if part not in ("Storage", "Eks", "Network", "Images", "Serverless"):
            continue  # another environment whose name starts with this one's (ek52d and ek52dx, say)
        d = aws("cloudformation", "describe-stacks", "--stack-name", name)["Stacks"][0]
        res = aws("cloudformation", "list-stack-resources", "--stack-name", name)["StackResourceSummaries"]
        out[name] = {"status": d["StackStatus"], "created": d["CreationTime"], "updated": d.get("LastUpdatedTime"),
                     "tags": {t["Key"]: t["Value"] for t in d.get("Tags", [])},
                     "outputs": {o["OutputKey"]: o["OutputValue"] for o in d.get("Outputs", [])},
                     "resources": {r["LogicalResourceId"]: [r["ResourceType"], r.get("PhysicalResourceId")]
                                   for r in res}}
    return out


def account_stacks(aws):
    """{stack: [status, last change]} of every ColvennaCatalog stack in the region: the installs', the
    test environments' and the release registry's."""
    summaries = aws("cloudformation", "list-stacks", "--query",
                    "StackSummaries[?starts_with(StackName, 'ColvennaCatalog') && StackStatus!='DELETE_COMPLETE']")
    return {s["StackName"]: [s["StackStatus"], s.get("LastUpdatedTime") or s.get("CreationTime")]
            for s in summaries or []}


def repository_images(aws, env):
    """{repository: sorted [digest tags]} of the install's repositories (colvenna-catalog-<env>/...)."""
    out = {}
    repos = aws("ecr", "describe-repositories", "--query",
                f"repositories[?starts_with(repositoryName, 'colvenna-catalog-{env}/')].repositoryName") or []
    for repo in repos:
        images = aws("ecr", "describe-images", "--repository-name", repo, "--query", "imageDetails[]") or []
        out[repo] = sorted(f"{i['imageDigest']} {','.join(sorted(i.get('imageTags') or []))}" for i in images)
    return out


def kube_state(kube, namespace, release):
    """The Helm release (revision, status, values), the catalog's pods (name, uid, node, image,
    annotations), the replicas its Deployment asks for, the namespace's Secrets (name,
    resourceVersion) and its load balancers (Services of type LoadBalancer: name, hostname)."""
    helm = subprocess.run(["helm", "status", release, "-n", namespace, "-o", "json"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=kube.env)
    status = json.loads(helm.stdout) if helm.returncode == 0 else None
    values = subprocess.run(["helm", "get", "values", release, "-n", namespace, "-o", "json"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, env=kube.env)
    pods = []
    p = kube.kubectl("get", "pods", "-n", namespace, "-l", f"app.kubernetes.io/instance={release}", "-o", "json",
                     check=False)
    if p.returncode == 0:
        for pod in json.loads(p.stdout)["items"]:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            pods.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                         "node": pod["spec"].get("nodeName"), "phase": pod["status"].get("phase"),
                         "image": [c["image"] for c in pod["spec"]["containers"]],
                         "annotations": {k: v for k, v in pod["metadata"].get("annotations", {}).items()
                                         if k.startswith("colvenna-catalog/")}})
    secrets = {}
    s = kube.kubectl("get", "secrets", "-n", namespace, "-o", "json", check=False)
    if s.returncode == 0:
        secrets = {i["metadata"]["name"]: i["metadata"]["resourceVersion"] for i in json.loads(s.stdout)["items"]}
    d = kube.kubectl("get", "deployment", release, "-n", namespace, "-o", "json", check=False)
    replicas = json.loads(d.stdout)["spec"].get("replicas") if d.returncode == 0 else None
    balancers = {}
    v = kube.kubectl("get", "services", "-n", namespace, "-o", "json", check=False)
    if v.returncode == 0:
        balancers = {i["metadata"]["name"]: ",".join(x.get("hostname") or x.get("ip") or "" for x in
                                                     (i.get("status", {}).get("loadBalancer", {}).get("ingress") or []))
                     for i in json.loads(v.stdout)["items"] if i["spec"].get("type") == "LoadBalancer"}
    return {"helm": None if status is None else {"revision": status.get("version"), "status": status["info"]["status"],
                                                 "chart": (status.get("chart") or {}).get("metadata", {}).get("version")},
            "values": json.loads(values.stdout) if values.returncode == 0 else None,
            "pods": sorted(pods, key=lambda x: x["name"]), "replicas": replicas, "secrets": secrets,
            "loadBalancers": balancers}


def functions_state(aws, outputs):
    """The serverless form's functions: each one's version, image and the server's alias live."""
    out = {}
    for key in ("ServerFunctionName", "MaintenanceFunctionName", "AuthorizerFunctionName", "OpsFunctionName"):
        name = outputs.get(key)
        if not name:
            continue
        conf = aws("lambda", "get-function-configuration", "--function-name", name)
        versions = aws("lambda", "list-versions-by-function", "--function-name", name, "--query", "Versions[].Version")
        out[name] = {"lastModified": conf.get("LastModified"), "codeSha256": conf.get("CodeSha256"),
                     "versions": versions}
    server = outputs.get("ServerFunctionName")
    if server:
        alias = aws("lambda", "get-alias", "--function-name", server, "--name", "live")
        out["alias"] = {"version": alias.get("FunctionVersion"), "revision": alias.get("RevisionId")}
    return out


def compare_resources(before, after):
    """What changed between two stacks_of() records: stacks made or gone, resources made, replaced
    (another physical id) or gone."""
    changes = []
    for stack in sorted(set(before) | set(after)):
        if stack not in after:
            changes.append(f"{stack}: gone")
            continue
        if stack not in before:
            changes.append(f"{stack}: made")
            continue
        b, a = before[stack]["resources"], after[stack]["resources"]
        for logical in sorted(set(b) | set(a)):
            if logical not in a:
                changes.append(f"{stack}/{logical}: gone")
            elif logical not in b:
                changes.append(f"{stack}/{logical}: made")
            elif b[logical][1] != a[logical][1]:
                changes.append(f"{stack}/{logical}: replaced")
    return changes


def duplicates(before, after):
    """What a run doubled: stacks made, and resource types of a stack with more resources than before.
    A resource replaced by one of its kind (a Lambda version a change publishes) is no duplicate."""
    out = [f"{s}: made" for s in after if s not in before]
    for stack in set(before) & set(after):
        count = lambda r: {t: sum(1 for x, _ in r.values() if x == t) for t, _ in r.values()}  # noqa: E731
        b, a = count(before[stack]["resources"]), count(after[stack]["resources"])
        out += [f"{stack}: {a[t]} {t} (was {b.get(t, 0)})" for t in sorted(a) if a[t] > b.get(t, 0)]
    return out


def created_after(stacks, start):
    """(ok, {stack: created}): every stack created at or after start."""
    created = {name: s["created"] for name, s in stacks.items()}
    ok = bool(created) and all(parse_time(c) >= parse_time(start) for c in created.values())
    return ok, created


# --- the candidate and the install ---------------------------------------------------------------------

def manifest(checkout, candidate):
    """The candidate's manifest (release/<candidate>/manifest.json of the installer's checkout)."""
    return json.loads((Path(checkout) / "release" / candidate / "manifest.json").read_text())


def add_install_arguments(parser, release_required=False):
    parser.add_argument("--release", required=release_required,
                        help="the candidate the install is of (install.sh --release); without it, --from-source")
    parser.add_argument("--installer-checkout",
                        help="the checkout install.sh and uninstall.sh run from: the candidate's (default: this one)")


def install_args(args):
    return ["--release", args.release] if getattr(args, "release", None) else ["--from-source"]


def checkout_of(args):
    return Path(args.installer_checkout).resolve() if getattr(args, "installer_checkout", None) else REPO


def candidate_state(install, aws, kube, candidate, checkout):
    """What runs_the_candidate judges, collected: the digest the install's repository holds under the
    candidate's name, and the running image(s), pod annotations and chart (EKS) or the server
    function's image (serverless)."""
    repo = f"colvenna-catalog-{install.env}/colvenna-catalog"
    tagged = aws.raw("ecr", "describe-images", "--repository-name", repo, "--image-ids", f"imageTag={candidate}",
                     "--query", "imageDetails[0].imageDigest", "--output", "text", check=False).stdout.strip()
    out = {"repository": repo, "tagged": tagged}
    if install.form == "eks":
        state = kube_state(kube, install.namespace, install.release)
        out["images"] = sorted({i for p in state["pods"] for i in p["image"]})
        out["annotations"] = {p["name"]: p["annotations"] for p in state["pods"]}
        out["chart"] = (install_json(install, checkout) or {}).get("chart")
    else:
        install.refresh()
        out["images"] = [install.output("Serverless", "ServerImage")]
    return out


def judge_runs_the_candidate(state, stacks, candidate, release, form):
    """(ok, detail): the running image is the manifest's digest, copied into the install's own
    repository under the candidate's name; EKS: the chart is the manifest's and the pods carry the
    candidate and B (colvenna-catalog/release, colvenna-catalog/source-sha); every stack is tagged
    SourceSha=B."""
    built_from, digest = release["source"]["built_from"], release["image"]["digest"]
    problems = []
    if state["tagged"] != digest:
        problems.append(f"{state['repository']}:{candidate} is {state['tagged'] or 'missing'}, not the manifest's {digest}")
    if not state["images"] or any(not i.endswith("@" + digest) for i in state["images"]):
        problems.append(f"it runs {state['images']}")
    if form == "eks":
        if not state["annotations"]:
            problems.append("no pod")
        for pod, a in state["annotations"].items():
            if a.get("colvenna-catalog/release") != candidate or a.get("colvenna-catalog/source-sha") != built_from:
                problems.append(f"pod {pod}: {a}")
        if state.get("chart") != release["chart"]["digest"]:
            problems.append(f"the chart installed is {state.get('chart')}, not the manifest's {release['chart']['digest']}")
    if not stacks:
        problems.append("no stack")
    for name, s in stacks.items():
        if s["tags"].get("SourceSha") != built_from:
            problems.append(f"{name} is tagged SourceSha={s['tags'].get('SourceSha')}")
    return not problems, "; ".join(problems) or (f"{digest[:19]}... in {state['repository']}:{candidate}; "
                                                 f"{len(stacks)} stacks tagged SourceSha={built_from[:12]}")


def install_json(install, checkout=None):
    """The install's own record (.state/<env>/install.json), when the checkout has it."""
    path = Path(checkout or getattr(install, "checkout", None) or REPO) / ".state" / install.env / "install.json"
    return json.loads(path.read_text()) if path.exists() else None


def record_install(ctx, with_catalog=True):
    """What UAT-DEP-03 records of an install: its stacks with each resource's physical id, its
    repositories' images, the admin token's version, and EKS: the Helm release, its values, the pods
    and their UIDs; serverless: the functions' versions and the alias; with_catalog: principals,
    grants, databases and tables as the admin API and the REST API list them."""
    inst, aws = ctx.install, ctx.aws
    out = {"stacks": stacks_of(aws, inst.env), "repositories": repository_images(aws, inst.env)}
    if inst.form == "eks":
        out["kube"] = kube_state(ctx.kube, inst.namespace, inst.release)
        out["adminToken"] = out["kube"]["secrets"].get("colvenna-catalog-admin-token")
    else:
        inst.refresh()
        outputs = inst.outputs("Serverless")
        out["functions"] = functions_state(aws, outputs)
        arn = outputs.get("AdminTokenSecretArn")
        if arn:
            meta = aws("secretsmanager", "describe-secret", "--secret-id", arn)
            out["adminToken"] = sorted(meta.get("VersionIdsToStages", {}))
    if with_catalog:
        admin = ctx.admin
        out["principals"] = sorted((p["name"], p.get("disabledAt")) for p in
                                   admin.call("GET", "/principals")["body"]["principals"])
        out["grants"] = sorted(g["id"] for g in admin.grants())
        rest = ctx.rest
        out["tables"] = {db: sorted(rest.tables(db)) for db in sorted(rest.databases())}
    return out


def judge_exit(run):
    """(ok, detail): the script exited 0; else the step it failed in."""
    return run.exit_code == 0, f"exit {run.exit_code}; failed {run.failed_step()}"


def data_missing(storage, inventory):
    """[the Storage stack's buckets and database instances that the account's inventory lacks]."""
    have = set(inventory.get("buckets", [])) | {d.split(" ")[0] for d in inventory.get("databases", [])}
    return sorted(physical for kind, physical in (storage or {}).get("resources", {}).values()
                  if kind in ("AWS::S3::Bucket", "AWS::RDS::DBInstance") and physical not in have)


def kept(before, after, key):
    return before.get(key) == after.get(key)


def account_inventory(aws):
    """What UAT-DEP-08 records of the account in the region, by name: every ColvennaCatalog stack
    (status, last change), and the buckets, database instances, secrets, roles, functions,
    repositories, log groups and VPC endpoints of the catalog's installs, test environments and
    release registry."""
    out = {"stacks": account_stacks(aws)}
    out["buckets"] = sorted(aws("s3api", "list-buckets", "--query",
                                "Buckets[?starts_with(Name, 'colvennacatalog')].Name") or [])
    out["databases"] = sorted(f"{d['DBInstanceIdentifier']} {d['DBInstanceStatus']}" for d in
                              aws("rds", "describe-db-instances", "--query", "DBInstances[]") or []
                              if d["DBInstanceIdentifier"].startswith("colvennacatalog"))
    out["secrets"] = sorted(aws("secretsmanager", "list-secrets", "--query",
                                "SecretList[?starts_with(Name, 'colvenna-catalog')].Name") or [])
    out["roles"] = sorted(aws("iam", "list-roles", "--query", "Roles[?starts_with(RoleName, 'colvenna-catalog') || "
                                                             "starts_with(RoleName, 'ColvennaCatalog')].RoleName") or [])
    out["functions"] = sorted(aws("lambda", "list-functions", "--query",
                                  "Functions[?starts_with(FunctionName, 'colvenna-catalog') || "
                                  "starts_with(FunctionName, 'ColvennaCatalog')].FunctionName") or [])
    out["repositories"] = sorted(aws("ecr", "describe-repositories", "--query",
                                     "repositories[?starts_with(repositoryName, 'colvenna-catalog')].repositoryName") or [])
    groups = []
    for prefix in ("/aws/lambda/colvenna-catalog", "/aws/lambda/ColvennaCatalog", "/aws/rds/instance/colvennacatalog",
                   "/aws/eks/colvenna-catalog", "/aws/apigateway/colvenna-catalog"):
        groups += aws("logs", "describe-log-groups", "--log-group-name-prefix", prefix, "--query",
                      "logGroups[].logGroupName") or []
    out["logGroups"] = sorted(groups)
    # Each endpoint as <its stack>/<id> (a stackless one: -/<id>), so that it is of the install that made it.
    out["endpoints"] = sorted(
        f"{next((t['Value'] for t in e.get('Tags', []) if t['Key'] == 'aws:cloudformation:stack-name'), '-')}/{e['VpcEndpointId']}"
        for e in aws("ec2", "describe-vpc-endpoints", "--filters", "Name=tag:Project,Values=colvenna-catalog",
                     "--query", "VpcEndpoints[?State!='deleted']") or [])
    return out


def names_of(env):
    """The patterns every name of an install carries."""
    return (f"ColvennaCatalog{stack_suffix(env)}", f"colvenna-catalog-{env}", f"colvennacatalog{env}")


PARTS = ("Storage", "Eks", "Network", "Images", "Serverless")


def of_install(item, env):
    """Whether an inventory entry is the install's (its stacks, its test driver's, buckets, functions,
    ...), and not another environment's whose name starts with this one's (ek52d and ek52dx)."""
    stack, name, lower = names_of(env)
    if item == f"ColvennaCatalogTest{stack_suffix(env)}Driver":
        return True
    for prefix in (stack, "/aws/lambda/" + stack):
        if item.startswith(prefix):
            rest = item[len(prefix):]
            # CloudFormation may cut a stack's name short in the names it makes (Storag-...).
            return rest[:1] in ("", "-") or rest.startswith(PARTS) or rest.startswith(tuple(p[:5] for p in PARTS))
    for prefix in (name, "/aws/lambda/" + name, "/aws/apigateway/" + name):
        if item.startswith(prefix):
            return item[len(prefix):][:1] in ("", "-", "/")
    for prefix in (lower, "/aws/rds/instance/" + lower):
        if item.startswith(prefix):
            return item[len(prefix):].startswith(tuple(p.lower()[:5] for p in PARTS))
    return False


def _entries(inventory, env, mine):
    """The inventory's entries that are the install's (mine) or not."""
    out = {}
    for key, value in inventory.items():
        if isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if of_install(k, env) == mine}
        else:
            out[key] = [v for v in value if of_install(v.split(" ")[0], env) == mine]
    return out


def others(inventory, env):
    """The inventory without the install's own entries."""
    return _entries(inventory, env, False)


def own(inventory, env):
    """The inventory's entries that are the install's."""
    return _entries(inventory, env, True)


def differences(before, after):
    """[what differs] between two inventories."""
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if isinstance(b, dict) and isinstance(a, dict):
            for k in sorted(set(b) | set(a)):
                if b.get(k) != a.get(k):
                    out.append(f"{key} {k}: {b.get(k)} -> {a.get(k)}")
        elif b != a:
            gone = sorted(set(b or []) - set(a or []))
            made = sorted(set(a or []) - set(b or []))
            out.append(f"{key}: gone {gone}, made {made}")
    return out


ENV_OF = (re.compile(r"ColvennaCatalog([A-Z][a-z0-9]{0,7}?)(?=Storage|Eks|Network|Images|Serverless|Stora|Serve|Netwo|Image)"),
          re.compile(r"colvenna-catalog-([a-z][a-z0-9]{0,7})(?=[-/]|$)"),
          re.compile(r"colvennacatalog([a-z][a-z0-9]{0,7}?)(?=storage|eks|network|images|serverless|stora|serve)"))


DRIVER_STACK = re.compile(r"^ColvennaCatalogTest([A-Z][a-z0-9]{0,7})Driver$")


def env_of(item):
    """The environment whose name an inventory entry carries (its test driver's stack too), or None
    (the test environment's, the release registry's, or no catalog's)."""
    m = DRIVER_STACK.match(item)
    if m:
        return m.group(1).lower()
    if item.startswith(("ColvennaCatalogTest", "ColvennaCatalogRelease", "colvenna-catalog-test-",
                        "colvenna-catalog-release")):
        return None
    for pattern in ENV_OF:
        m = pattern.search(item)
        if m:
            return m.group(1).lower()
    return None


def others_untouched(before, after, env, test_stack, said=""):
    """(ok, detail): what is not the install's is as recorded: the test environment's stack and the
    release registry as they were, and nothing of another install gone. The account is shared while
    a case runs: what others add or change meanwhile is not judged, and an install another run
    removes (one of its stacks, or its test driver's, deleted or being deleted meanwhile) takes its
    own resources with it, unless the output of the case's own scripts (said) names that install's
    stacks."""
    problems = []
    for stack in (test_stack, "ColvennaCatalogRelease"):
        if before["stacks"].get(stack) != after["stacks"].get(stack):
            problems.append(f"{stack}: {before['stacks'].get(stack)} -> {after['stacks'].get(stack)}")
    deleting = {env_of(s) for s, v in after["stacks"].items() if str(v[0]).startswith("DELETE_")}
    removed = ({env_of(s) for s in before["stacks"] if s not in after["stacks"]} | deleting) - {None, env}
    named = sorted(e for e in removed if names_of(e)[0] in said)
    if named:
        problems.append(f"the case's own scripts named the stacks of {named}, removed meanwhile")
    removed -= set(named)
    b, a = others(before, env), others(after, env)
    for key in sorted(b):
        old = {x.split(" ")[0] for x in b[key]}
        new = {x.split(" ")[0] for x in a.get(key, [])}
        gone = sorted(x for x in old - new if env_of(x) not in removed)
        if gone:
            problems.append(f"{key} gone: {gone}")
    detail = "; ".join(problems) or "the test environment and the release registry as recorded; nothing of the others gone"
    if removed:
        detail += f" (uninstalled meanwhile by their own runs: {sorted(removed)})"
    return not problems, detail


def existing_psql(ctx, install, statements):
    """Runs statements as the admin of the database instance the install was given (its configuration's
    COLVENNA_DB_ENDPOINT and admin secret), from a pod of the run (the test environment's database
    admits its VPC), connected to the database postgres: (psql's exit status, its output)."""
    secret = json.loads(ctx.aws.raw("secretsmanager", "get-secret-value", "--secret-id",
                                    install.get("COLVENNA_DB_ADMIN_SECRET_ARN"), "--query", "SecretString",
                                    "--output", "text").stdout)
    ctx.sanitizer.secret_sources.append(lambda: [secret["password"]])
    url = f"https://truststore.pki.rds.amazonaws.com/{install.region}/{install.region}-bundle.pem"
    with urllib.request.urlopen(url, timeout=60) as r:  # nosec B310 - the fixed https URL of AWS's RDS bundle
        bundle = r.read().decode()
    ns = ctx.namespace("psql", client=False)
    host, port = install.get("COLVENNA_DB_ENDPOINT"), install.get("COLVENNA_DB_PORT")
    # A password file, never the environment (as uatlib/sql.py does), in a volume of its own.
    line = ":".join(v.replace("\\", "\\\\").replace(":", "\\:") for v in (host, str(port), "*", secret["username"],
                                                                         secret["password"]))
    ctx.kube.secret(ns, "login", {"pgpass": line + "\n"})
    ctx.kube.configmap(ns, "ca", {"ca.pem": bundle})
    pod = f"psql-{int(time.time())}"
    ctx.kube.apply({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "namespace": ns},
                    "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                             "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                             "containers": [{"name": "psql", "image": POSTGRES_IMAGE,
                                             "command": ["sh", "-c", "cp /login/pgpass /pgpass/.pgpass "
                                                         "&& chmod 600 /pgpass/.pgpass && exec sleep 3600"],
                                             "securityContext": hardened(70),
                                             "env": [{"name": "PGHOST", "value": host}, {"name": "PGPORT", "value": str(port)},
                                                     {"name": "PGUSER", "value": secret["username"]},
                                                     {"name": "PGDATABASE", "value": "postgres"},
                                                     {"name": "PGPASSFILE", "value": "/pgpass/.pgpass"},
                                                     {"name": "PGSSLMODE", "value": "verify-full"},
                                                     {"name": "PGSSLROOTCERT", "value": "/ca/ca.pem"}],
                                             "volumeMounts": [{"name": "ca", "mountPath": "/ca"},
                                                              {"name": "login", "mountPath": "/login", "readOnly": True},
                                                              {"name": "pgpass", "mountPath": "/pgpass"},
                                                              {"name": "tmp", "mountPath": "/tmp"}]}],  # nosec B108 - the pod's own
                             "volumes": [{"name": "ca", "configMap": {"name": "ca"}},
                                         {"name": "login", "secret": {"secretName": "login", "defaultMode": 0o444}},  # pragma: allowlist secret - the name of the Kubernetes Secret the pod mounts, not a value
                                         {"name": "pgpass", "emptyDir": {"medium": "Memory"}},
                                         {"name": "tmp", "emptyDir": {}}]}})
    ctx.kube.wait_ready(ns, pod)
    p = ctx.kube.exec(ns, pod, ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1"], input=statements, check=False)
    ctx.kube.kubectl("delete", "pod", pod, "-n", ns, "--wait=false", check=False)
    return p.returncode, p.stdout + p.stderr
