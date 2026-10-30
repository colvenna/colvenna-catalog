"""UAT-DEP-01 Install from nothing.

install.sh --release <candidate>, without --yes, from the candidate's clean checkout, its
confirmation answered on a terminal with the environment's name once the summary is shown; then a
Paimon 2.0.0 client makes a database and tables, commits and reads back with per-table credentials
(EKS: the test driver in a client pod; serverless: tests/serverless/run.sh cycle, inside the catalog's
VPC). Judged from install.sh's output and evidence, the account and the cluster; the time of each
step is recorded, not judged. The install stays for the cases after it; UAT-DEP-09 removes it."""

import re
import subprocess
from pathlib import Path

from uatlib import deploy, testdriver
from uatlib.install import REPO
from uatlib.probe import describe

ID = "UAT-DEP-01"
FORMS = ("eks", "serverless")
MAKES_THE_INSTALL = True
# What release.txt (deploy/lib/release.sh's notes) holds once each release check passed.
RELEASE_NOTES = {"tree and manifest": r"^checkout: .* clean, differs from .* under", "form": r"^form: ",
                 "registry digests": r"^registry: ", "image label": r"^image: .* is labelled",
                 "chart label": r"^chart: .* annotated"}


def checks(form, args=None):
    return ["install-exit", "summary-before-change", "release-checks-first", "every-step-done", "installed-from-nothing",
            "runs-the-candidate", "nothing-built", "admin-token-not-printed", "readable-writable"]


def add_arguments(parser):
    deploy.add_install_arguments(parser, release_required=True)


# --- judges ------------------------------------------------------------------------------------------

def judge_release_checks_first(release_txt, run, chart_digest):
    """The release checks (tree, manifest, form, the registry's digests, the labels) passed, before
    preflight's checks of the cluster or network: their last line (the chart pulled, by the manifest's
    digest) is the first the preflight step prints, and that step ended. (The cluster's and network's
    checks print nothing when they pass; UAT-DEP-05's refusals show theirs after this line.)"""
    have = {k: len(re.findall(p, release_txt, re.M)) for k, p in RELEASE_NOTES.items()}
    steps = run.steps()
    passed = "preflight" in steps and steps.index("preflight") < len(steps) - 1
    start = run.index(r"^== preflight$")
    after = [t for _, t in run.lines[start + 1:]] if start is not None else []
    first = after[0] if after else ""
    chart_first = first.startswith("chart: ") and chart_digest in first
    ok = all(have.values()) and have["registry digests"] >= 2 and passed and chart_first
    return ok, f"release.txt: {have}; the preflight step's first line is the chart by digest: {chart_first}; preflight passed: {passed}"


def judge_nothing_built(texts, lines):
    """No Maven or Docker build ran: no Maven log in the install's evidence (a source install's mvn.log),
    no image build in its image log (a release install's holds crane's copy by digest), and no build
    line in its output."""
    built = sorted(set((["mvn.log"] if "mvn.log" in texts else []) +
        [name for name, text in texts.items() if re.search(r"docker buildx|^#\d+ \[internal\]|BUILD SUCCESS", text, re.M)]))
    building = [t for t in lines if re.search(r"BUILD SUCCESS|docker buildx|\[INFO\] Building ", t)]
    copied = "copied by digest" if any("Copying from" in text for text in texts.values()) else "no copy seen"
    return not built and not building, f"builds in the evidence: {built or 'none'}; build lines in the output: {len(building)}; {copied}"


def judge_admin_token_not_printed(texts, token, answer, where):
    """No token in the output or the evidence; the admin's token is where the form keeps it, and works."""
    leaks = deploy.token_leaks(texts, [token])
    works = answer is not None and answer.get("status") == 200
    return not leaks and works and bool(token), (f"files holding a token: {leaks or 'none'} of {len(texts)}; the admin's "
                                                 f"token in {where}, GET principals with it: {describe(answer)[:40]}")


def judge_cycle(result):
    """The test driver's cycle: rows written and read back, with per-table credentials (the
    credentials' own look, when made, reached the table and not another)."""
    r = (result or {}).get("result") or {}
    creds = r.get("perTableCredentials")
    s3 = (creds or {}).get("s3") or {}
    looked = creds is None or (creds.get("tokenStatus") == 200 and s3.get("getInTableRoot") == "allowed"
                               and s3.get("putInOtherTable") == "AccessDenied")
    ok = (result or {}).get("exitCode") == 0 and r.get("eventsMatch") is True and r.get("keyedMatch") is True \
        and r.get("dataTokenEnabled") is True and looked
    return ok, (f"exit {(result or {}).get('exitCode')}: rows {r.get('eventsRowsRead')}/{r.get('keyedRowsRead')} match "
                f"{r.get('eventsMatch')}/{r.get('keyedMatch')} with per-table credentials {r.get('dataTokenEnabled')}; "
                f"the credentials' look {(creds or {}).get('tokenStatus')} {s3 or ''}")


def judge_run_sh_cycle(code, output):
    """tests/serverless/run.sh cycle passed."""
    last = output.strip().splitlines()[-1:] if output.strip() else []
    return code == 0, f"tests/serverless/run.sh cycle: exit {code}; {last}"


# --- collection --------------------------------------------------------------------------------------

def admin_token_answer(ctx):
    """(token, the answer to GET principals with it, where the form keeps it)."""
    token = ctx.install.admin_token()
    ctx.sanitizer.secret_sources.append(lambda: [token])
    answer = ctx.probe.run([{"name": "admin", "url": ctx.install.admin_base + "/principals", "token": "admin"}],
                           tokens={"admin": token}, keep="admin-token.jsonl")[0]
    where = "its Kubernetes Secret" if ctx.form == "eks" else f"Secrets Manager (colvenna-catalog-{ctx.install.env}/admin-token)"
    return token, answer, where


def prepare_client(ctx):
    """The client's jars, built before the install (EKS: its image is made on the install's after)."""
    return testdriver.build(ctx) if ctx.form == "eks" else None


def readable_writable(ctx, prepared=None):
    """(ok, detail) of the Paimon 2.0.0 client's cycle with per-table credentials."""
    if ctx.form == "eks":
        image = testdriver.image(ctx, ctx.install, prepared)
        ns = ctx.namespace("driver")
        token = ctx.install.admin_token()
        ctx.sanitizer.secret_sources.append(lambda: [token])
        body = testdriver.event(ctx.install, "cycle", token, rows=200, ca=ctx.install.ca())
        out = testdriver.run_pod(ctx, ns, image, body)
        ctx.keep("cycle.json", out)
        return judge_cycle(out)
    # The harness's own run.sh (a release install's image is tagged with the candidate's name, which the
    # run.sh of candidates before #52 did not find); it reads the install's outputs from this checkout's
    # .state/<env>/, which install.sh writes in the installer's checkout: the same one, unless
    # --installer-checkout names another (then link its .state/<env> here).
    p = subprocess.run([str(REPO / "tests" / "serverless" / "run.sh"), ctx.install.env, ctx.install.region, "cycle"],
                       cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                       env=ctx.aws.env(), timeout=3600)
    ctx.keep("serverless-run-cycle.log", p.stdout)
    return judge_run_sh_cycle(p.returncode, p.stdout)


def run(ctx):
    args, inst = ctx.args, ctx.install
    checkout = deploy.checkout_of(args)
    with ctx.preparing("nothing of the install in the account; the candidate's checkout clean") as notes:
        release = deploy.manifest(checkout, args.release)
        before = deploy.stacks_of(ctx.aws, inst.env)
        repos = deploy.repository_images(ctx.aws, inst.env)
        clean = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"], stdout=subprocess.PIPE,
                               universal_newlines=True).stdout.strip() == ""
        if before or repos or not clean:
            raise RuntimeError(f"stacks {sorted(before)}; repositories {sorted(repos)}; checkout clean {clean}")
        prepared = prepare_client(ctx)
        notes.append(f"{args.release} built from {release['source']['built_from'][:12]}; nothing of {inst.env}")

    installer = deploy.Installer(ctx, checkout, args.config)
    run = installer.run("install.sh", *deploy.install_args(args), answer=inst.env, keep="install")
    evidence = Path(run.evidence) if run.evidence else None
    ctx.know_install()  # its URI and host, kept out of the evidence from now on
    ctx.keep("step-times.txt", "".join(f"{t} {x}\n" for t, x in run.lines if x.startswith("== ")))
    ctx.check("install-exit", *deploy.judge_exit(run))
    ctx.check("summary-before-change", *deploy.summary_before_change(run, ctx.form, args.release,
                                                                     release["source"]["built_from"]))
    release_txt = (evidence / "release.txt").read_text() if evidence and (evidence / "release.txt").exists() else ""
    ctx.check("release-checks-first", *judge_release_checks_first(release_txt, run, release["chart"]["digest"]))
    ctx.check("every-step-done", *deploy.every_step_done(run, ctx.form, inst.get("COLVENNA_INTERNAL_NLB") == "on"))
    stacks = deploy.stacks_of(ctx.aws, inst.env)
    ctx.keep("stacks.json", stacks)
    ok, created = deploy.created_after(stacks, run.started)
    ctx.check("installed-from-nothing", ok, f"run began {run.started}; stacks created {created}")
    state = deploy.candidate_state(inst, ctx.aws, ctx.kube, args.release, checkout)
    ctx.keep("candidate-state.json", state)
    ctx.check("runs-the-candidate", *deploy.judge_runs_the_candidate(state, stacks, args.release, release, ctx.form))
    ctx.check("nothing-built", *judge_nothing_built(deploy.evidence_texts(evidence) if evidence else {},
                                                    [t for _, t in run.lines]))
    token, answer, where = admin_token_answer(ctx)
    texts = dict(deploy.evidence_texts(evidence) if evidence else {}, output=run.text)
    ctx.check("admin-token-not-printed", *judge_admin_token_not_printed(texts, token, answer, where))
    ctx.check("readable-writable", *readable_writable(ctx, prepared))
