"""One run of one case: its context (the install, the test environment's cluster, the run's own
namespaces, a probe pod, the admin API), its evidence directory with verdicts.tsv, and its result.

  <out>/<case>[-<n>]/verdicts.tsv   one line per check (verdicts.py)
  <out>/<case>[-<n>]/result.json    the result judged from them (PASS, FAIL, BLOCKED or NOT_RUN), why,
                                    the checks the case lists for the form, what ran and where
  <out>/<case>[-<n>]/run.log        what the run did, in order
  <out>/<case>[-<n>]/...            the case's own raw evidence (requests and answers, pods' logs)

A rerun never writes over an earlier run's evidence: it is <case>-2, -3, ... (cases.md, 3.3).
Whatever happens, the run's clean-up runs (its grants deleted, principals disabled, namespaces
deleted, then whatever the case registered), and is recorded in result.json."""

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import verdicts
from .admin import Admin
from .aws import Aws
from .install import REPO, Install, TestEnvironment
from .kube import Kube
from .probe import Probe
from .rest import Rest

sys.path.insert(0, str(REPO / "tests" / "compat"))
from compatlib.sanitize import Sanitizer  # noqa: E402

# The pinned kubectl and Helm of scripts/install-tools.sh, before any other on PATH.
if (REPO / ".tools").is_dir():
    os.environ["PATH"] = f"{REPO / '.tools'}{os.pathsep}{os.environ.get('PATH', '')}"


def run_id():
    """Letters and digits only (database names, principals and namespaces take it), unique per run."""
    return "u" + datetime.now(timezone.utc).strftime("%y%m%d%H%M") + secrets.token_hex(2)


def git_head():
    try:
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], stdout=subprocess.PIPE,
                               universal_newlines=True).stdout.strip() != ""
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def evidence_dir(out, case):
    """<out>/<case>, or <case>-<n> after the runs already there."""
    out = Path(out)
    path, n = out / case, 1
    while path.exists():
        n += 1
        path = out / f"{case}-{n}"
    path.mkdir(parents=True)
    os.chmod(path, 0o700)
    return path


class RunSanitizer:
    """The compatibility matrix's sanitizer, with the run's secrets as they become known (the tokens
    the case issues): every text written goes through it."""

    def __init__(self, values):
        self.values = values
        self.secret_sources = []

    def text(self, s):
        found = []
        for source in self.secret_sources:
            found += [v for v in source() if v]
        return Sanitizer(secrets=found, values=self.values).text(s)

    def data(self, value):
        found = []
        for source in self.secret_sources:
            found += [v for v in source() if v]
        return Sanitizer(secrets=found, values=self.values).data(value)


class Context:
    def __init__(self, case, args, log_file):
        self.case = case  # the case's module
        self.args = args
        self.id = case.ID
        self.run = args.run or run_id()
        self.started = datetime.now(timezone.utc)
        self.aws = Aws(args.region)
        self.scratch = Path(tempfile.mkdtemp(prefix="colvenna-uat-"))
        os.chmod(self.scratch, 0o700)
        self.testenv = TestEnvironment(args.test_env, self.aws)
        self.kube = Kube(self.testenv.kubeconfig(self.scratch / "kubeconfig"), log=self.log,
                         placement=self.testenv.client_placement())
        self.install = Install(args.config, self.aws, self.kube)
        self.form = self.install.form
        self.evidence = Path(args.evidence)
        self.log_file = log_file
        self.sanitizer = RunSanitizer({})
        self.know_install(not_yet=getattr(case, "MAKES_THE_INSTALL", False))
        self.verdicts = verdicts.Verdicts(self.evidence / "verdicts.tsv", self.sanitizer, self.log)
        self.cleanups = []  # (what, function), run last first
        self._probe = None
        self._admin = None

    def know_install(self, not_yet=False):
        """The install's URI and host among the sanitizer's values. not_yet: the case makes the install
        (MAKES_THE_INSTALL: UAT-DEP-01, 02), so a serverless install's stack may have no ApiUrl yet; the
        case calls this again once it has installed. For any other case, an install without them is
        one the run cannot start on."""
        self.install.refresh()
        try:
            self.sanitizer.values.update({self.install.uri: "<CATALOG_URI>", self.install.host: "<CATALOG_HOST>"})
        except KeyError:
            if not not_yet:
                raise
        return self.sanitizer.values

    def reconnect(self):
        """After the install was made again (UAT-DEP-08): its URI (a serverless install's REST API is a
        new one) and the admin's token (issued again) as they are now, for the probe and the admin API;
        the tokens, principals and grants the run made are carried over for its requests and clean-up."""
        self.know_install()
        if self._probe is not None:
            self._probe.base = self.install.uri
        old, self._admin = self._admin, None
        new = self.admin
        if old is not None:
            new.carry(old)
        return new

    # --- recording ------------------------------------------------------------------------------------

    def log(self, message):
        line = self.sanitizer.text(f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {message}")
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def check(self, name, ok, detail=""):
        return self.verdicts.check(name, ok, detail)

    def precondition(self, what, ok, detail=""):
        return self.verdicts.precondition(what, ok, detail)

    @contextlib.contextmanager
    def preparing(self, what):
        """A precondition the case makes for itself: an exception in the block blocks the case (its
        result BLOCKED), with the exception's text; a block that ends records the precondition, with
        the notes the block appended to the list it is given."""
        notes = []
        try:
            yield notes
        except verdicts.Blocked:
            raise
        except Exception as e:  # noqa: BLE001 - recorded: the case cannot be judged without it
            self.precondition(what, False, f"{type(e).__name__}: {e}")
        self.precondition(what, True, "; ".join(notes))

    def keep(self, name, text):
        """A file of raw evidence, sanitized."""
        path = self.evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sanitizer.text(text if isinstance(text, str) else json.dumps(text, indent=2, sort_keys=True)))
        return path

    def on_cleanup(self, what, fn):
        self.cleanups.append((what, fn))

    # --- what cases use -------------------------------------------------------------------------------

    def name(self, role):
        """A Kubernetes name of the run: uat-<run>-<role>."""
        return f"uat-{self.run}-{role}".lower()

    def namespace(self, role, client=True, monitoring=False, security="restricted", ingress="none", labels=None):
        """A namespace of the run's own. client: labelled so that the install admits it (EKS form; the
        serverless form's API is reached through the test environment's endpoint by any pod)."""
        all_labels = dict(labels or {})
        if self.install.form == "eks":
            if client:
                key, value = self.install.client_label
                all_labels[key] = value
            if monitoring:
                key, value = self.install.metrics_label
                all_labels[key] = value
        return self.kube.namespace(self.name(role), self.run, all_labels, security, ingress)

    @property
    def probe(self):
        if self._probe is None:
            ns = self.namespace("client")
            self._probe = Probe(self.kube, ns, base=self.install.uri, ca=self.install.ca(), evidence=self.evidence,
                                sanitizer=self.sanitizer).start()
        return self._probe

    @property
    def admin(self):
        if self._admin is None:
            self._admin = Admin(self.probe, self.install.admin_base, self.install.admin_token(), self.id)
            self.sanitizer.secret_sources.append(lambda: list(self._admin.tokens.values()))
        return self._admin

    @property
    def driver(self):
        """The serverless form's test driver inside the catalog's VPC (uatlib/driver.py), for SQL."""
        if getattr(self, "_driver", None) is None:
            from .driver import Driver

            self._driver = Driver(self)
        return self._driver

    @property
    def rest(self):
        """The REST API with the admin's (and the case's issued) tokens."""
        return Rest(self.probe, self.install.rest_base, self.admin.tokens)

    # --- the end --------------------------------------------------------------------------------------

    def cleanup(self):
        """Runs every clean-up, the case's (last registered first), the admin's, the namespaces'; returns
        {"status": PASS|FAIL, "steps": [...]}. Each step is tried whatever the others did; a signal
        that comes meanwhile is noted, not obeyed, so that the clean-up is not cut short (SIGKILL
        still stops it: then README.md says what to remove by hand)."""
        steps, failed = [], False
        noted = []
        previous = {s: signal.signal(s, lambda n, f: noted.append(n)) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

        def step(what, fn):
            """fn() -> (ok, detail)."""
            nonlocal failed
            try:
                ok, detail = fn()
            except BaseException as e:  # noqa: BLE001 - every clean-up is tried, whatever stops one
                ok, detail = False, f"{type(e).__name__}: {e}"
            failed |= not ok
            steps.append({"what": what, "status": "PASS" if ok else "FAIL", "detail": detail})

        def case_step(fn):
            detail = fn()
            return True, detail if isinstance(detail, str) else ""

        def admin_step():
            problems = self._admin.cleanup()
            return not problems, "; ".join(problems)

        def namespaces_step():
            left = self.kube.delete_namespaces()
            return not left, ("left: " + ", ".join(left)) if left else ""

        try:
            for what, fn in reversed(self.cleanups):
                step(what, lambda fn=fn: case_step(fn))
            if self._admin is not None:
                step("grants deleted, principals disabled", admin_step)
            step("namespaces deleted", namespaces_step)
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            shutil.rmtree(self.scratch, ignore_errors=True)
        if noted:
            steps.append({"what": "signals during the clean-up", "status": "PASS", "detail": f"noted, not obeyed: {noted}"})
        return {"status": "FAIL" if failed else "PASS", "steps": steps}


def configured_form(args):
    """The install's form as its configuration names it (for a run that could not start)."""
    from .install import EXAMPLE, parse_env

    try:
        config = dict(parse_env(EXAMPLE.read_text()), **parse_env(Path(args.config).read_text()))
        return config.get("COLVENNA_FORM") or "eks"
    except OSError:
        return None


def execute(case, args):
    """Runs the case; returns the process's exit status (0 when the result is PASS). A signal (SIGTERM,
    SIGHUP, Ctrl-C) stops the case, and its clean-up and result still run."""
    evidence = evidence_dir(args.out, case.ID)
    args.evidence = str(evidence)
    sha, dirty = git_head()
    exit_code, error, cleanup, ctx, stopped = None, "", {"status": "NOT_RUN", "steps": []}, None, None
    with open(evidence / "run.log", "a", encoding="utf-8") as log_file:
        try:
            try:
                ctx = Context(case, args, log_file)
            except Exception as e:  # noqa: BLE001 - the case did not run
                error = f"the run could not start: {type(e).__name__}: {Sanitizer().text(str(e))}"
                print(error, file=sys.stderr, flush=True)
            if ctx is not None:
                ctx.log(f"{case.ID} run {ctx.run} on {ctx.install.env} ({ctx.form}) in {args.region}, evidence {evidence}")
                try:
                    case.run(ctx)
                    exit_code = 0
                except verdicts.Blocked as e:
                    error = f"blocked: {e}"
                    exit_code = 1
                except Exception as e:  # noqa: BLE001 - recorded; the result says FAIL
                    error = "".join(traceback.format_exception_only(type(e), e)).strip()
                    ctx.log("the case stopped: " + error)
                    ctx.keep("traceback.txt", traceback.format_exc())
                    exit_code = 1
                except BaseException as e:  # a signal: the clean-up below still runs
                    stopped = e
                    error = f"stopped: {type(e).__name__}"
                    ctx.log("the case was stopped")
                finally:
                    try:
                        cleanup = ctx.cleanup()
                    except BaseException as e:  # noqa: BLE001 - recorded: what is left must be removed by hand
                        cleanup = {"status": "FAIL", "steps": [{"what": "the clean-up", "status": "FAIL",
                                                                "detail": f"{type(e).__name__}: {e}"}]}
                    ctx.log(f"clean-up: {cleanup['status']}")
                    ctx.verdicts.close()
        finally:
            form = ctx.form if ctx else configured_form(args)
            expected = case.checks(form, args) if form in case.FORMS else []
            lines = verdicts.read(evidence / "verdicts.tsv")
            result, reasons = verdicts.judge(lines, expected, exit_code, getattr(case, "OPTIONAL", ()), ran=ctx is not None)
            if form and form not in case.FORMS:
                result, reasons = "NOT_RUN", [f"{case.ID} has no {form} form"]
            if cleanup["status"] == "FAIL" and result == "PASS":
                result, reasons = "FAIL", ["the clean-up failed: something of the run is left"]
            body = {
                "case": case.ID, "run": ctx.run if ctx else args.run, "result": result, "reasons": reasons,
                "error": error, "exitCode": exit_code, "form": form, "region": args.region,
                "install": ctx.install.describe() if ctx else None, "testEnvironment": args.test_env,
                "started": ctx.started.isoformat() if ctx else None, "finished": datetime.now(timezone.utc).isoformat(),
                "harnessCommit": sha, "harnessTreeDirty": dirty, "checks": expected, "cleanup": cleanup,
            }
            text = json.dumps(body, indent=2, sort_keys=True) + "\n"
            (evidence / "result.json").write_text(ctx.sanitizer.text(text) if ctx else Sanitizer().text(text))
            line = f"{case.ID}: {result}" + (f" ({'; '.join(reasons[:6])})" if reasons else "")
            print(ctx.sanitizer.text(line) if ctx else line, flush=True)
    if stopped is not None:
        raise stopped
    return 0 if result == "PASS" else 1
