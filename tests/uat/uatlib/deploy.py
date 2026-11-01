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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .aws import AwsError
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


# The signals a lost session or a stopped operator sends (#76). A process started under nohup, or as a
# background job of a non-interactive shell, inherits some of them ignored, and bash can neither trap nor
# reset a signal ignored on entry: the scripts the harness starts get them back at their default, as on
# an operator's host, whatever the harness itself inherited.
SESSION_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)


def reset_session_signals():
    """In a child about to exec: the session's signals at their default disposition (preexec_fn).
    Popen runs it between fork and exec, which Python's documentation warns against while other threads
    run: none does when a case starts a script (the one thread pool, stacks_elsewhere's, has ended by
    then). Popen has no thread-safe way to set the child's signals (posix_spawn's setsigdef is not
    reachable through it), and `env --default-signal` needs a coreutils some hosts lack."""
    for s in SESSION_SIGNALS:
        signal.signal(s, signal.SIG_DFL)


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
        session or host would end it. The script starts with the session's signals at their default
        (SESSION_SIGNALS), whatever the harness inherited."""
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
                             stderr=subprocess.STDOUT, env=self.env(), start_new_session=True,
                             preexec_fn=reset_session_signals)
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
    """{stack: [status, last change, its id]} of every ColvennaCatalog stack in the region: the installs',
    the test environments' and the release registry's. The id is the last part of the stack's StackId (a
    UUID, no account in it), which a stack deleted and made again under the same name does not keep (#62)."""
    summaries = aws("cloudformation", "list-stacks", "--query",
                    "StackSummaries[?starts_with(StackName, 'ColvennaCatalog') && StackStatus!='DELETE_COMPLETE']")
    return {s["StackName"]: [s["StackStatus"], s.get("LastUpdatedTime") or s.get("CreationTime"),
                             (s.get("StackId") or "").rsplit("/", 1)[-1]]
            for s in summaries or []}


def stack_uuid(value):
    """The stack's id as account_stacks records it, or None in a record made before it did (#62)."""
    return value[2] if len(value) > 2 and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                                       str(value[2])) else None


def stacks_elsewhere(aws):
    """{stack: [status, last change, its id, region]} of every ColvennaCatalog stack in the account's other enabled
    regions, read in parallel (#46). A region that refuses the call is left out: an install there is then
    judged by its roles and buckets alone, which can only make others-untouched stricter."""
    def read(region):
        try:
            return region, account_stacks(aws.in_region(region))
        except AwsError:
            return region, {}

    regions = sorted(r for r in aws("ec2", "describe-regions", "--query", "Regions[].RegionName") or [] if r != aws.region)
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(read, regions))
    return {stack: value + [region] for region, stacks in found for stack, value in stacks.items()}


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


def data_missing(storage, inventory, restored=None, had=None):
    """[the install's buckets and database instances that the account lacks]: the Storage stack's bucket
    and instance (inventory: the account's, after). For an install UAT-DEP-10 moved onto the instance it
    restored (restored: (its identifier, whether the account has it), as the inventory lists only the
    stacks' own), that instance; and the stack's own only if the account had it before (had: the inventory
    before), as the user manual's restore deletes it (#51)."""
    def names(inv):
        return set(inv.get("buckets", [])) | {d.split(" ")[0] for d in inv.get("databases", [])}

    have, before = names(inventory), names(had if had is not None else inventory)
    wanted = [physical for kind, physical in (storage or {}).get("resources", {}).values()
              if kind == "AWS::S3::Bucket" or (kind == "AWS::RDS::DBInstance" and (not restored or physical in before))]
    missing = [w for w in wanted if w not in have] + ([restored[0]] if restored and not restored[1] else [])
    return sorted(missing)


def kept(before, after, key):
    return before.get(key) == after.get(key)


def account_inventory(aws, elsewhere=True):
    """What UAT-DEP-08 records of the account in the region, by name: every ColvennaCatalog stack
    (status, last change, its id), and the buckets, database instances, secrets, roles, functions,
    repositories, log groups and VPC endpoints of the catalog's installs, test environments and
    release registry. elsewhere: also every ColvennaCatalog stack of the other regions
    (stacksElsewhere): the account lists its roles and buckets whatever the region, so an install of
    another region is in this inventory by them, and its stacks say whether its own run removed it (#46).
    The stacks are read again once the resources are (stacksAtEnd, stacksElsewhereAtEnd): an inventory takes a
    minute or more, and an install whose uninstall begins meanwhile has its resources going while its stacks,
    read first, still look as they were (#100); others_untouched judges by the later reading."""
    out = {"stacks": account_stacks(aws)}
    if elsewhere:
        out["stacksElsewhere"] = stacks_elsewhere(aws)
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
    out["stacksAtEnd"] = account_stacks(aws)
    if elsewhere:
        out["stacksElsewhereAtEnd"] = stacks_elsewhere(aws)
    return out


def names_of(env):
    """The patterns every name of an install carries."""
    return (f"ColvennaCatalog{stack_suffix(env)}", f"colvenna-catalog-{env}", f"colvennacatalog{env}")


PARTS = ("Storage", "Eks", "Network", "Images", "Serverless")
# deploy/lib/stacks.sh's: a cut of a stack's name at least this long, in a name CloudFormation generated,
# is of that stack's environment alone.
STACK_CUT_MIN = int(re.search(r"^STACK_CUT_MIN=(\d+)$", (REPO / "deploy/lib/stacks.sh").read_text(), re.M).group(1))


def named_after_stacks(name, stacks):
    """deploy/lib/stacks.sh's named_after_stacks, which uninstall.sh deletes the functions' log groups
    by: whether CloudFormation generated the name for one of the stacks, its first part (up to its
    first hyphen) being the stack's name or a cut of it at least STACK_CUT_MIN characters long."""
    head, hyphen, _ = name.partition("-")
    return bool(hyphen) and any(head == s or (len(head) >= STACK_CUT_MIN and s.startswith(head)) for s in stacks)


def of_install(item, env):
    """Whether an inventory entry is the install's (its stacks, its test driver's, buckets, functions,
    ...), and not another environment's whose name starts with this one's (ek52d and ek52dx): owner_of's
    reading of its name, so that the two never disagree (#84)."""
    return owner_of(item) == ("install", env)


# An inventory's second readings of the stacks, which say what happened to them, not what the account holds (#100).
READINGS = ("stacksAtEnd", "stacksElsewhereAtEnd")


def _entries(inventory, env, mine):
    """The inventory's entries that are the install's (mine) or not."""
    out = {}
    for key, value in inventory.items():
        if key in READINGS:  # a second reading of the stacks, not more entries (#100)
            continue
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
    for key in sorted((set(before) | set(after)) - set(READINGS)):
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


# --- whose an entry is (#84) ---------------------------------------------------------------------------
# Every entry of an inventory is an install's, a test environment's or the release registry's, by its name:
# its stack's (a stack's name says whose it is), or one CloudFormation generated for a resource of a stack
# (its first part, up to its first hyphen, the stack's name, lowercased in a bucket's or a database's, or a
# cut of it at least STACK_CUT_MIN long), or one the code gives (colvenna-catalog-<env>..., the test
# environments' colvenna-catalog-test-<name>..., the registry's colvenna-catalog-release...), or, for a VPC
# endpoint, the stack its aws:cloudformation:stack-name tag names. Names are 1 to NAME_MAX lowercase letters
# and digits, the first a letter (config.sh, tests/environment/lib.sh); an install may not be named test.
# A lowercased cut can be two environments' (stacks.sh: a and aserverl share 24 characters): the stacks of
# the records settle it when one of them has it, and it is no one's when two do.

NAME_MAX = 8
NAME = rf"[a-z][a-z0-9]{{0,{NAME_MAX - 1}}}"  # as config.sh and lib.sh check it
CAPITALIZED = rf"[A-Z][a-z0-9]{{0,{NAME_MAX - 1}}}"  # as it is in a stack's name


class Owner(NamedTuple):
    """Whose an entry is: kind "install" (name: the environment), "test" (the test environment) or "registry"."""
    kind: str
    name: str

    @property
    def label(self):
        return self.name if self.kind == "install" else f"the test environment {self.name}" if self.kind == "test" \
            else "the release registry"

    def named_in(self, said):
        """Whether the case's own scripts' output names one of its stacks: ColvennaCatalog<Env> then a component,
        or ColvennaCatalogTest<Name> alone or before a hyphen (not an install's test driver)."""
        stack = {"install": "ColvennaCatalog", "test": "ColvennaCatalogTest"}.get(self.kind)
        end = r"(?![a-z0-9])" if self.kind == "install" else r"(?![A-Za-z0-9])"
        return bool(stack) and re.search(re.escape(stack + stack_suffix(self.name)) + end, said) is not None


REGISTRY = Owner("registry", "release")
DRIVER_STACK = re.compile(rf"^ColvennaCatalogTest({CAPITALIZED})Driver$")
# A test environment's stack, ColvennaCatalogTest<Name>, and the stacks nested in it (ColvennaCatalogTest<Name>-...).
TEST_STACK = re.compile(rf"^ColvennaCatalogTest({CAPITALIZED})(?:-|$)")
INSTALL_STACK = re.compile(rf"^ColvennaCatalog({CAPITALIZED})(?:" + "|".join(PARTS) + r")$")
# A name CloudFormation generated: ColvennaCatalog<Env> and a cut of a component, then a hyphen.
GENERATED = re.compile(rf"^ColvennaCatalog({CAPITALIZED})[A-Za-z]*-")
# The names the code gives, in order: a test environment's, the registry's, an install's.
GIVEN = ((re.compile(rf"^colvenna-catalog-test-({NAME})(?=[-/]|$)"), "test"),
         (re.compile(r"^colvenna-catalog-(release)(?=[-/]|$)"), "registry"),
         (re.compile(rf"^colvenna-catalog-({NAME})(?=[-/]|$)"), "install"))
LOG_PREFIXES = ("/aws/lambda/", "/aws/rds/instance/", "/aws/apigateway/", "/aws/eks/")
ENV_NAME = re.compile(NAME)


def stack_owner(stack):
    """The Owner of the stack (an install's test driver's is the install's), or None."""
    if stack == "ColvennaCatalogRelease" or stack.startswith("ColvennaCatalogRelease-"):
        return REGISTRY
    for pattern, kind in ((DRIVER_STACK, "install"), (TEST_STACK, "test"), (INSTALL_STACK, "install")):
        m = pattern.match(stack)
        if m:
            return Owner(kind, m.group(1).lower())
    return None


def _lowercase_owner(head):
    """Whose a lowercased stack name, or a cut of one, is (a bucket's or a database instance's first part):
    colvennacatalog<env><component>, colvennacatalogtest<name>, colvennacatalogrelease. Its components end it,
    as the case of the letters no longer says where the name ends (#67: pfekstorage is pfek's, not pf's)."""
    if not head.startswith("colvennacatalog"):
        return None
    rest = head[len("colvennacatalog"):]
    if rest == "release":
        return REGISTRY
    for part in (p.lower() for p in PARTS):
        if rest.endswith(part) and ENV_NAME.fullmatch(rest[:-len(part)]):
            return Owner("install", rest[:-len(part)])
    if len(head) >= STACK_CUT_MIN:
        cuts = {rest[:k] for k in range(1, NAME_MAX + 1) if ENV_NAME.fullmatch(rest[:k]) and rest[k:]
                and any(p.lower().startswith(rest[k:]) for p in PARTS)}
        if len(cuts) == 1:
            return Owner("install", cuts.pop())
    if rest.startswith("test") and ENV_NAME.fullmatch(rest[len("test"):]):
        return Owner("test", rest[len("test"):])
    return None


def owner_of(item, stacks=()):
    """The Owner of an inventory entry, or None (no catalog install's, test environment's or the registry's, or a
    cut two of the stacks share). stacks: the stacks of the records; a name CloudFormation generated for one of
    them is that stack's, which settles a cut two owners could have when one of them is in the records."""
    name = item.split(" ")[0]
    stack, slash, rest = name.partition("/")
    if slash and rest.startswith("vpce-"):
        return stack_owner(stack)  # a VPC endpoint, of the stack its tag names ("-": none)
    if name in stacks:
        return stack_owner(name)
    bare = next((name[len(p):] for p in LOG_PREFIXES if name.startswith(p)), name)
    head, hyphen, _ = bare.partition("-")
    if hyphen:
        exact = {stack_owner(s) for s in stacks if head in (s, s.lower())} - {None}
        cut = {stack_owner(s) for s in stacks if len(head) >= STACK_CUT_MIN
               and (s.startswith(head) or s.lower().startswith(head))} - {None}
        found = exact | cut  # a stack's whole name is also a cut of it
        if len(found) > 1:
            return None  # one stack's name or cut and another's cut too (test environments abcdef and abcdefgh)
        if found:
            return found.pop()
    owner = stack_owner(name)
    if owner:
        return owner
    for pattern, kind in GIVEN:
        m = pattern.match(bare)
        if m:
            return REGISTRY if kind == "registry" else Owner(kind, m.group(1))
    if hyphen:
        owner = stack_owner(head)
        if owner:
            return owner
        m = GENERATED.match(bare)
        if m and named_after_stacks(bare, [f"ColvennaCatalog{m.group(1)}{p}" for p in PARTS]):
            return Owner("install", m.group(1).lower())
        return _lowercase_owner(head)
    return None


def env_of(item):
    """The install whose an inventory entry is (its test driver's too), or None (a test environment's, the
    release registry's, or no catalog's)."""
    owner = owner_of(item)
    return owner.name if owner and owner.kind == "install" else None


# The statuses an existing instance may move between without being changed (#98): "backing-up", "The DB instance is
# currently being backed up" (https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/accessing-monitoring.html#Overview.DBInstance.Status),
# as it was in v1.0.0-rc.4's UAT within the instance's backup window (RDS's events: "Backing up DB instance"). A
# backup changes none of its settings. Any other change of status (modifying, rebooting, ...) is the instance changed.
RDS_OWN_STATUSES = ("available", "backing-up")


def instance_status(before, after):
    """(same, note) of an existing RDS instance's status recorded before and after a case: the same, or only
    being backed up and back (RDS_OWN_STATUSES), which the note reports."""
    if before == after:
        return True, ""
    if before in RDS_OWN_STATUSES and after in RDS_OWN_STATUSES:
        return True, f"the instance's status {before} -> {after}: being backed up (RDS's documented backing-up), not counted"
    return False, f"the instance's status {before} -> {after}"


DATA_KEYS = ("buckets", "databases")  # the inventory's data of an install, which no update of it removes (#52)


def stacks_between(before, after):
    """(before, after): every stack of the two inventories, this region's and the others', as the window between
    them has them (#100). Before: as first read, or, for a stack made while the before inventory was read, as
    read at its end. After: as read at the after inventory's end, once its resources were. A record without
    the second reading has its first."""
    def reading(record, key):
        return record[key + "AtEnd"] if key + "AtEnd" in record else (record.get(key) or {})
    first = dict(before.get("stacksElsewhere") or {}, **before["stacks"])
    at_end = dict(reading(before, "stacksElsewhere"), **reading(before, "stacks"))
    return dict(at_end, **first), dict(reading(after, "stacksElsewhere"), **reading(after, "stacks"))


def others_untouched(before, after, env, test_stack, said=""):
    """(ok, detail): what is not the install's is as recorded: the test environment's stack and the
    release registry as they were, and nothing of another's gone that the install's run could have
    touched. The account is shared while a case runs, and each entry is judged by whose it is (owner_of,
    #84): an install's, a test environment's, the registry's. What others add or change meanwhile is not
    judged. An install or a test environment another run removes (one of its stacks, its test driver's
    or one nested in it, deleted or being deleted meanwhile, in this region or another, #46; or deleted
    and made again under the same name, its stack's id another, #62; a test environment taken down by
    its own session, #84) takes its own resources with it; one another run updates (one of its stacks
    changed meanwhile: UAT-DEP-06's upgrade, UAT-DEP-07's way back, which may remove what one version has
    and the other not, #52; up.sh again; its test driver's stack does not count) may lose its functions,
    roles and the like, never its data (DATA_KEYS: its buckets and database instances). Either, unless
    the output of the case's own scripts (said) names that owner's stacks. Never excused: the case's own
    test environment, the release registry, and an entry of no install, test environment or the registry
    (owner_of None: the inventory lists the catalog's names and tags alone, so it is no other project's)."""
    problems = []
    stacks_before, stacks_after = stacks_between(before, after)
    for stack in (test_stack, "ColvennaCatalogRelease"):
        if stacks_before.get(stack) != stacks_after.get(stack):
            problems.append(f"{stack}: {stacks_before.get(stack)} -> {stacks_after.get(stack)}")
    known = sorted(set(stacks_before) | set(stacks_after))

    def owner(item):
        return owner_of(item, known)

    never_excused = {None, Owner("install", env), stack_owner(test_stack), REGISTRY}

    def happened(stack):
        """What happened to a stack meanwhile: "removed" (gone, or being deleted, not as it was before: a
        DELETE_FAILED stack left as it was did nothing), "remade" (deleted and made again under the same
        name, #62: another id), "updated" (another status or last change), or None."""
        b, a = stacks_before.get(stack), stacks_after.get(stack)
        if a is None:
            return "removed" if b is not None else None
        if str(a[0]).startswith("DELETE_") and (b or [None])[:2] != a[:2]:
            return "removed"
        if b is None:
            return None
        if None not in (stack_uuid(a), stack_uuid(b)) and stack_uuid(a) != stack_uuid(b):
            return "remade"
        return "updated" if a[:2] != b[:2] else None

    removed, remade, updated = set(), set(), set()
    for stack in sorted(set(stacks_before) | set(stacks_after)):
        what, o = happened(stack), owner(stack)
        if what is None or o in never_excused:
            continue
        if DRIVER_STACK.match(stack):
            if what == "removed":
                removed.add(o)  # its test driver taken down with it; made again or updated, no change of it (#52)
        elif TEST_STACK.match(stack) and "-" in stack:
            updated.add(o)  # a stack nested in a test environment: whatever happened to it, the environment's update
        elif what == "updated":
            updated.add(o)
        else:
            removed.add(o)
            if what == "remade":
                remade.add(o)
    updated -= removed
    named = sorted(o for o in removed | updated if o.named_in(said))
    if named:
        problems.append(f"the case's own scripts named the stacks of {[o.label for o in named]}, removed or updated meanwhile")
    removed -= set(named)
    updated -= set(named)
    b, a = others(before, env), others(after, env)
    for key in sorted(b):
        old = {x.split(" ")[0] for x in b[key]}
        new = {x.split(" ")[0] for x in a.get(key, [])}
        gone = sorted(x for x in old - new
                      if owner(x) not in removed and (key in DATA_KEYS or owner(x) not in updated))
        if gone:
            problems.append(f"{key} gone: {gone}")
    detail = "; ".join(problems) or "the test environment and the release registry as recorded; nothing of the others gone"

    def names(kind, owners):
        return sorted(o.name for o in owners if o.kind == kind)

    for kind, owners, what in (("install", removed, "uninstalled meanwhile by their own runs"),
                               ("install", removed & remade, "of them installed again under the same names"),
                               ("install", updated, "updated meanwhile by their own runs"),
                               ("test", removed, "test environments taken down meanwhile by their own runs"),
                               ("test", updated, "test environments updated meanwhile by their own runs")):
        if names(kind, owners):
            detail += f" ({what}: {names(kind, owners)})"
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
