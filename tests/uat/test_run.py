"""A run's clean-up, offline: every step is tried, whatever stops one, and a signal meanwhile is
noted, not obeyed."""

import os
import signal
import tempfile
import unittest

from uatlib.run import Context


class Kube:
    def __init__(self):
        self.deleted = False

    def delete_namespaces(self):
        self.deleted = True
        return []


def context():
    ctx = object.__new__(Context)
    ctx.cleanups, ctx._admin, ctx.kube = [], None, Kube()
    ctx.scratch = tempfile.mkdtemp()
    return ctx


class Cleanup(unittest.TestCase):
    def test_every_step_runs_whatever_stops_one(self):
        ctx = context()
        ran = []
        ctx.on_cleanup("first", lambda: ran.append("first") or "done")

        def interrupted():
            raise KeyboardInterrupt("signal 15")

        ctx.on_cleanup("second", interrupted)
        result = ctx.cleanup()
        self.assertEqual(ran, ["first"])
        self.assertTrue(ctx.kube.deleted)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual([s["status"] for s in result["steps"]], ["FAIL", "PASS", "PASS"])
        self.assertFalse(os.path.exists(ctx.scratch))

    def test_a_signal_during_the_clean_up_is_noted(self):
        ctx = context()
        ctx.on_cleanup("signalled", lambda: os.kill(os.getpid(), signal.SIGTERM) or "went on")
        before = signal.getsignal(signal.SIGTERM)
        result = ctx.cleanup()
        self.assertEqual(result["status"], "PASS")
        self.assertIn("noted", result["steps"][-1]["detail"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
