"""Offline tests of helm_upgrade (deploy/lib/helm.sh, from colvenna-lake): a release this workspace's killed run
left pending is recovered before the upgrade, any other pending revision is left alone, and two runs
of one workspace do not change a release at once. A stub helm keeps the release's history in a JSON
file and logs every call."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(os.path.dirname(os.path.dirname(HERE)), "deploy", "lib", "helm.sh")

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
from datetime import datetime, timezone
path = os.environ["HELM_STUB_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["calls"].append(" ".join(args[:2]))
history = state["history"]
def save():
    # Atomically: the test reads the state while a stub killed in its upgrade may be writing it.
    with open(path + ".part", "w") as f:
        json.dump(state, f)
    os.replace(path + ".part", path)
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
def add(status):
    history.append({"revision": max([r["revision"] for r in history] or [0]) + 1, "updated": now(), "status": status})
def done():
    for r in history[:-1]:
        if r["status"] == "deployed":
            r["status"] = "superseded"
    history[-1]["status"] = "deployed"
code = 0
if args[0] == "status":
    if os.environ.get("HELM_STUB_STATUS_ERROR"):
        print("Error: Kubernetes cluster unreachable", file=sys.stderr)
        code = 1
    elif history:
        print(json.dumps({"info": {"status": max(history, key=lambda r: r["revision"])["status"]}}))
    else:
        print("Error: release: not found", file=sys.stderr)
        code = 1
elif args[0] == "history":
    print(json.dumps(history))
elif args[0] in ("rollback", "upgrade"):
    add("pending-rollback" if args[0] == "rollback" else "pending-upgrade" if history else "pending-install")
    save()
    if args[0] == "upgrade" and os.environ.get("HELM_STUB_HANG"):
        time.sleep(60)
    state = json.load(open(path))
    history = state["history"]
    if args[0] == "upgrade" and os.environ.get("HELM_STUB_FAIL"):
        history[-1]["status"] = "failed"
        code = 1
    else:
        done()
elif args[0] == "uninstall":
    history.clear()
save()
sys.exit(code)
'''


class HelmUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "helm"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bin_dir, "helm"), 0o700)
        self.store = os.path.join(self.tmp.name, "release.json")
        self.record_file = os.path.join(self.state_dir, "helm-ns-rel.lock.run")
        self.env = {**os.environ, "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                    "STATE_DIR": self.state_dir, "HELM_STUB_STATE": self.store, "HELM_LOCK_WAIT": "2"}

    def tearDown(self):
        self.tmp.cleanup()

    def release(self, *statuses):
        """The release's history: one revision per status, oldest first."""
        history = [{"revision": i + 1, "updated": "2026-10-22T10:00:00.1Z", "status": s} for i, s in enumerate(statuses)]
        with open(self.store, "w") as f:
            json.dump({"history": history, "calls": []}, f)

    def record(self, line=None):
        """This workspace's record of its last run on the release: set it, or read it."""
        if line is not None:
            with open(self.record_file, "w") as f:
                f.write(line + "\n")
        if not os.path.exists(self.record_file):
            return []
        with open(self.record_file) as f:
            return f.read().split()

    def upgrade(self, **env):
        result = subprocess.run(["bash", "-c", f'source "{HELPER}"; helm_upgrade rel ./chart --namespace ns --wait'],
                                env={**self.env, **env}, capture_output=True, text=True, timeout=60)
        with open(self.store) as f:
            state = json.load(f)
        return result, state["calls"], state["history"]

    def statuses(self):
        with open(self.store) as f:
            return [r["status"] for r in json.load(f)["history"]]

    def test_a_deployed_release_is_upgraded_and_the_run_recorded_finished(self):
        self.release("superseded", "deployed")
        result, calls, history = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["status rel", "history rel", "upgrade --install"])
        self.assertEqual(history[-1], {**history[-1], "revision": 3, "status": "deployed"})
        self.assertEqual(self.record()[:2], ["finished", "3"])

    def test_a_first_install_is_an_install(self):
        self.release()
        result, calls, _ = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["status rel", "upgrade --install"])
        self.assertEqual(self.record()[:2], ["finished", "1"])

    def test_a_status_it_cannot_read_stops_it(self):
        self.release("deployed")
        result, calls, _ = self.upgrade(HELM_STUB_STATUS_ERROR="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["status rel"])
        self.assertIn("cannot read ns/rel", result.stderr)

    def test_a_run_hung_up_in_its_upgrade_is_rolled_back_by_the_next(self):
        # As when the operator's SSH session drops: SIGHUP to the whole session, which bash answers by
        # running its EXIT trap, so the record must not depend on one.
        self.kill_in_upgrade(signal.SIGHUP)

    def test_a_run_hung_up_is_rolled_back_even_when_this_test_inherited_sighup_ignored(self):
        # As under nohup (#76): this process ignores SIGHUP; the run it starts must still get it.
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            self.kill_in_upgrade(signal.SIGHUP)
        finally:
            signal.signal(signal.SIGHUP, previous)

    def test_a_run_killed_in_its_upgrade_is_rolled_back_by_the_next(self):
        # As when the host goes: SIGKILL to the whole session.
        self.kill_in_upgrade(signal.SIGKILL)

    def test_a_helm_that_returns_by_itself_finishes_the_record(self):
        # A failed upgrade is Helm's own outcome (failed, not pending): nothing for the next run to recover.
        self.release("superseded", "deployed")
        result, _, _ = self.upgrade(HELM_STUB_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.record()[:2], ["finished", "3"])

    def kill_in_upgrade(self, sig):
        # The first run's helm hangs in the upgrade, the release pending, and the session gets the signal.
        self.release("superseded", "deployed")
        # The run starts as on an operator's host, the signal at its default disposition, whatever this test
        # inherited: under nohup SIGHUP comes ignored, and bash cannot reset a signal ignored on entry (#76).
        first = subprocess.Popen(["bash", "-c", f'source "{HELPER}"; helm_upgrade rel ./chart --namespace ns --wait'],
                                 env={**self.env, "HELM_STUB_HANG": "1"}, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
        for _ in range(100):
            if self.statuses()[-1:] == ["pending-upgrade"]:
                break
            time.sleep(0.1)
        os.killpg(first.pid, sig)
        first.wait()
        self.assertEqual(self.record()[:2], ["running", "3"])
        result, calls, history = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-4:], ["status rel", "history rel", "rollback rel", "upgrade --install"])
        self.assertIn("revision 3 pending-upgrade, left by this workspace's killed run; rolling back to revision 2",
                      result.stderr)
        self.assertEqual([r["status"] for r in history][-2:], ["superseded", "deployed"])
        self.assertEqual(self.record()[:2], ["finished", "5"])

    def test_a_first_install_killed_is_uninstalled_and_installed_again(self):
        self.release("pending-install")
        self.record("running 1 4242 somehost")
        result, calls, history = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["status rel", "history rel", "uninstall rel", "upgrade --install"])
        self.assertIn("never deployed", result.stderr)
        self.assertEqual([r["status"] for r in history], ["deployed"])

    def test_a_killed_rollback_is_rolled_back_to_the_deployed_revision(self):
        # As Helm leaves it: revision 2 still deployed while the rollback that made 3 runs.
        self.release("superseded", "deployed", "pending-rollback")
        self.record("running 3 4242 somehost")
        result, calls, _ = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rolling back to revision 2", result.stderr)

    def test_a_pending_revision_after_this_workspace_s_finished_run_is_left_alone(self):
        # This workspace's last run made revision 2 and finished; someone else began revision 3 since.
        self.release("superseded", "deployed", "pending-upgrade")
        self.record("finished 2 4242 somehost")
        result, calls, history = self.upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["status rel", "history rel"])
        self.assertIn("left alone: revision 3 is pending-upgrade and not this workspace's", result.stderr)
        self.assertIn("helm rollback <release> 2", result.stderr)
        self.assertEqual(history[-1]["status"], "pending-upgrade")

    def test_a_refused_run_vouches_for_nothing_when_retried(self):
        self.release("deployed", "pending-upgrade")
        for _ in range(2):
            result, calls, _ = self.upgrade()
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("rollback rel", calls)
        self.assertEqual(self.record(), [])
        # Recovered by hand, the step runs again.
        self.release("superseded", "deployed")
        result, _, _ = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_killed_run_s_record_vouches_only_for_its_own_revision(self):
        # The killed run began revision 3; revision 4, pending now, is someone else's.
        self.release("deployed", "superseded", "failed", "pending-upgrade")
        self.record("running 3 4242 somehost")
        result, calls, _ = self.upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("rollback rel", calls)

    def test_a_pending_upgrade_with_nothing_deployed_is_left_for_a_person(self):
        self.release("failed", "pending-upgrade")
        self.record("running 2 4242 somehost")
        result, calls, _ = self.upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["status rel", "history rel"])
        self.assertIn("no deployed revision", result.stderr)

    def test_a_release_another_run_holds_is_waited_for_not_touched(self):
        self.release("deployed")
        lock = os.path.join(self.state_dir, "helm-ns-rel.lock")
        holder = subprocess.Popen(["flock", lock, "sleep", "10"])
        try:
            time.sleep(0.5)
            result, calls, _ = self.upgrade()
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])
        self.assertIn("waiting", result.stderr)
        self.assertIn("still locked", result.stderr)

    def test_a_namespace_is_required(self):
        result = subprocess.run(["bash", "-c", f'source "{HELPER}"; helm_upgrade rel ./chart --wait'],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no --namespace", result.stderr)


if __name__ == "__main__":
    unittest.main()
