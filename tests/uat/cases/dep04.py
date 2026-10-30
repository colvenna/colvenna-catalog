"""UAT-DEP-04 Resume after an interruption.

install.sh --yes with a change that makes its step take time, a new COLVENNA_DATA_TOKEN_DURATION
(the EKS pods roll; the serverless functions are updated), in a session of its own, interrupted in
that step: SIGHUP to its whole process group (the host's session lost) while the Helm release is
pending-upgrade (EKS, step chart) or the serverless stack's update is in progress (serverless, step
functions); then install.sh --yes again with the same configuration. Then the same with SIGKILL (the
host lost), changing the duration back. An interruption that came after the step had ended is no
interruption: the check interrupted fails, and the case must run again."""

import json
import signal
import subprocess
import time
import uuid

from uatlib import deploy
from uatlib.probe import describe

ID = "UAT-DEP-04"
FORMS = ("eks", "serverless")
STEP = {"eks": "chart", "serverless": "functions"}


def checks(form, args=None):
    return ["interrupted", "resumed", "converged"]


def add_arguments(parser):
    deploy.add_install_arguments(parser)


def duration_of(text):
    """Seconds of an ISO 8601 duration of hours and minutes (PT1H30M)."""
    import re

    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", text)
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60


class Watch:
    """The interruption's condition, polled while install.sh runs: its step announced, and the change
    under way (the Helm release pending-upgrade; the serverless stack's update in progress)."""

    def __init__(self, ctx):
        self.ctx, self.last, self.state = ctx, 0.0, ""

    def __call__(self, lines):
        if not any(t == f"== {STEP[self.ctx.form]}" for _, t in lines):
            return False
        if time.time() - self.last < 2:
            return False
        self.last = time.time()
        self.state = current_state(self.ctx)
        return self.state in ("pending-upgrade", "UPDATE_IN_PROGRESS")


def current_state(ctx):
    inst = ctx.install
    if ctx.form == "eks":
        p = subprocess.run(["helm", "status", inst.release, "-n", inst.namespace, "-o", "json"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=ctx.kube.env)
        return json.loads(p.stdout)["info"]["status"] if p.returncode == 0 else "unknown"
    s = ctx.aws("cloudformation", "describe-stacks", "--stack-name", inst.stack("Serverless"))["Stacks"][0]
    return s["StackStatus"]


def issued_duration(ctx, db, table):
    """Seconds the credentials the install issues now last."""
    r = ctx.probe.run([{"name": "token", "url": f"{ctx.install.rest_base}/databases/{db}/tables/{table}/token",
                        "token": "admin", "requestId": f"{ctx.run}-{uuid.uuid4()}"}], tokens=ctx.admin.tokens)[0]
    expires = (r.get("body") or {}).get("expiresAtMillis") if isinstance(r.get("body"), dict) else None
    return (expires / 1000 - time.time()) if expires else None, r


# --- judges ------------------------------------------------------------------------------------------

def judge_interrupted(run, form, left):
    """The run was signalled while its step's change was under way, and ended non-zero in that step;
    the state it left is recorded."""
    step = run.steps()[-1] if run.steps() else ""
    ok = bool(run.signalled) and run.exit_code != 0 and step == STEP[form]
    return ok, (f"{run.signalled or 'not signalled: the step ended before its change was under way'}; exit "
                f"{run.exit_code}; last step {step}; state left: {left}")


def judge_resumed(run, form, nlb, state):
    """The next run exited 0 with every step done, and the release or stack it found is in line."""
    ok, detail = deploy.every_step_done(run, form, nlb)
    recovered = state in ("deployed", "UPDATE_COMPLETE", "CREATE_COMPLETE")
    return ok and recovered, f"{detail}; state {state}"


def judge_converged(seconds, want, doubled, single, smoke):
    """The install has the changed setting (credentials last as long as the duration says), nothing
    doubled (no stack made, no more resources of a kind than before), one release or alias, and the
    smoke check passed."""
    has = seconds is not None and abs(seconds - want) < 600
    return has and not doubled and single[0] and smoke, (
        f"credentials last {seconds and int(seconds)} s (want {want}); duplicates: {doubled or 'none'}; {single[1]}; "
        f"smoke passed: {smoke}")


def run(ctx):
    args, inst = ctx.args, ctx.install
    installer = deploy.Installer(ctx, deploy.checkout_of(args), args.config)
    original = inst.get("COLVENNA_DATA_TOKEN_DURATION")
    changed = "PT2H" if original != "PT2H" else "PT4H"
    db, table = f"dep04_{ctx.run}", "t"
    nlb = inst.get("COLVENNA_INTERNAL_NLB") == "on"
    with ctx.preparing("the install there, issuing credentials; a table of the case's own") as notes:
        rest = ctx.rest
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, table, [("id", "INT")])
        before = deploy.record_install(ctx, with_catalog=False)
        ctx.keep("before.json", before)
        seconds, answer = issued_duration(ctx, db, table)
        if not before["stacks"] or seconds is None:
            raise RuntimeError(f"{len(before['stacks'])} stacks; credentials {describe(answer)[:80]}")
        notes.append(f"credentials last {int(seconds)} s now")
    left_at = {"value": original, "converged": True}

    def as_it_was():
        # The install is left with its own setting: the SIGKILL round ends there; a run stopped
        # before it did not, so install.sh runs once more with the configuration as it is.
        if left_at["value"] == original and left_at["converged"]:
            return f"COLVENNA_DATA_TOKEN_DURATION {original or '(default)'}, as it was"
        r = installer.run("install.sh", *deploy.install_args(args), "--yes", keep="cleanup-as-it-was")
        if r.exit_code != 0:
            raise RuntimeError(f"install.sh with the install's own configuration: exit {r.exit_code}")
        return f"install.sh with the install's own configuration (COLVENNA_DATA_TOKEN_DURATION {original or '(default)'})"

    ctx.on_cleanup("the install's own setting again", as_it_was)
    for sig, value in ((signal.SIGHUP, changed), (signal.SIGKILL, original)):
        name = signal.Signals(sig).name
        left_at.update(value=value, converged=False)
        config = deploy.write_config(ctx.scratch / f"{inst.env}.env", args.config, COLVENNA_DATA_TOKEN_DURATION=value)
        first = installer.run("install.sh", *deploy.install_args(args), "--yes", config=config, signal_when=Watch(ctx),
                              sig=sig, keep=f"install-{name}")
        left = current_state(ctx)
        ctx.keep(f"left-by-{name}.txt", f"{first.signalled}\nstate when the run ended: {left}\n")
        ctx.check("interrupted", *judge_interrupted(first, ctx.form, left))
        second = installer.run("install.sh", *deploy.install_args(args), "--yes", config=config, keep=f"resume-{name}")
        left_at["converged"] = second.exit_code == 0
        ctx.check("resumed", *judge_resumed(second, ctx.form, nlb, current_state(ctx)))
        seconds, _ = issued_duration(ctx, db, table)
        after = deploy.record_install(ctx, with_catalog=False)
        doubled = deploy.duplicates(before["stacks"], after["stacks"])
        ctx.keep(f"after-{name}.json", after)
        if ctx.form == "eks":
            pods = len(after["kube"]["pods"])
            want = after["kube"].get("replicas", pods)
            single = (pods == want and after["kube"]["helm"] is not None, f"{pods} pods of {want}, Helm {after['kube']['helm']}")
        else:
            single = (after["functions"].get("alias") is not None, f"alias live on {after['functions'].get('alias')}")
        smoke = deploy.every_step_done(second, ctx.form, nlb)[0]
        ctx.check("converged", *judge_converged(seconds, duration_of(value), doubled, single, smoke))
