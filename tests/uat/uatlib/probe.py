"""The client pods a case makes its requests from: tests/uat/probe/probe.py in a pinned Python image,
in a namespace of the run's own. A plan (the requests, and the tokens they use) goes to the pod on
stdin, never as an argument; the results come back as JSON lines and are kept, sanitized, as the
case's evidence."""

import json
import time
from pathlib import Path

from .kube import hardened, pod

# Python's standard library is all the probe needs; pinned by the digest of its multi-architecture index.
PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.13.18-alpine3.24@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48"
SCRIPT = Path(__file__).resolve().parent.parent / "probe" / "probe.py"


class Probe:
    """One probe pod: made once, used for many plans, deleted with its namespace."""

    def __init__(self, kube, namespace, name="probe", base="", ca=None, evidence=None, sanitizer=None, labels=None,
                 memory="256Mi"):
        self.kube, self.namespace, self.name = kube, namespace, name
        self.memory = memory  # a bigger probe makes bigger bodies (UAT-CAT-08)
        self.base, self.ca = base, ca
        self.evidence = Path(evidence) if evidence else None
        self.sanitizer = sanitizer
        self.labels = labels or {}
        self.started = False
        # Python's strict X.509 checks on (the probe's 3.13 has them by default); UAT-DEP-06 and 07 turn
        # them off in the EKS form to reach a previous release whose private CA has no keyUsage
        # (lifecycle.reach_previous_release, #49).
        self.strict = True

    def start(self):
        self.kube.configmap(self.namespace, f"{self.name}-script", {"probe.py": SCRIPT.read_text()})
        container = {
            "name": "probe", "image": PYTHON_IMAGE, "command": ["sleep", "86400"],
            "securityContext": hardened(65534),
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": self.memory}},
            "volumeMounts": [{"name": "script", "mountPath": "/uat", "readOnly": True},
                             {"name": "tmp", "mountPath": "/tmp"}]}  # nosec B108 - the pod's own /tmp, an emptyDir
        self.kube.apply(pod(self.namespace, self.name, [container],
                            [{"name": "script", "configMap": {"name": f"{self.name}-script"}}, {"name": "tmp", "emptyDir": {}}],
                            self.labels))
        self.kube.wait_ready(self.namespace, self.name)
        self.started = True
        return self

    def run(self, steps, tokens=None, keep=None, timeout=900, base=None, ca=None):
        """Runs the steps; returns their results ({name: result} when every name is unique, else the
        list, see results()). keep: a file name under the evidence directory for the results."""
        plan = {"base": base if base is not None else self.base, "ca": ca if ca is not None else self.ca,
                "tokens": tokens or {}, "strict": self.strict, "steps": steps}
        p = self.kube.exec(self.namespace, self.name, ["python3", "/uat/probe.py"], input=json.dumps(plan),
                           timeout=timeout, check=False)
        results = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
        if p.returncode != 0 and len(results) < len(steps):
            raise RuntimeError(f"the probe failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
        if keep and self.evidence:
            text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in results)
            if self.sanitizer:
                text = self.sanitizer.text(text)
            path = self.evidence / keep
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        return results


def by_name(results):
    """{name: result}, the last result of each name."""
    return {r["name"]: r for r in results}


def ok(result, status):
    return result is not None and result.get("status") == status


def describe(result):
    if result is None:
        return "no result"
    if "error" in result:
        return f"error {result['error']}"
    body = result.get("body")
    text = json.dumps(body) if not isinstance(body, str) else body
    return f"{result.get('status')} {text[:300]}"


def wait(seconds):
    time.sleep(seconds)


class Background:
    """A plan that runs on its own in a pod of the namespace (a reader that keeps reading, say), while
    the case goes on: its plan, tokens included, is a Secret the pod reads on stdin; its results are the
    pod's output, read back with results() (also once it was stopped)."""

    def __init__(self, probe, name, steps, tokens):
        """tokens: every token the case holds; the pod's Secret gets only those its steps use."""
        self.probe, self.name = probe, name
        kube, ns = probe.kube, probe.namespace
        used = {s["token"] for s in steps if s.get("token")}
        plan = {"base": probe.base, "ca": probe.ca, "tokens": {k: v for k, v in tokens.items() if k in used},
                "strict": probe.strict, "steps": steps}
        kube.secret(ns, f"{name}-plan", {"plan.json": json.dumps(plan)})
        container = {
            "name": "probe", "image": PYTHON_IMAGE, "command": ["sh", "-c", "python3 /uat/probe.py </plan/plan.json"],
            "securityContext": hardened(65534),
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "volumeMounts": [{"name": "script", "mountPath": "/uat", "readOnly": True},
                             {"name": "plan", "mountPath": "/plan", "readOnly": True},
                             {"name": "tmp", "mountPath": "/tmp"}]}  # nosec B108 - the pod's own /tmp, an emptyDir
        kube.configmap(ns, f"{probe.name}-script", {"probe.py": SCRIPT.read_text()})
        kube.apply(pod(ns, name, [container], [{"name": "script", "configMap": {"name": f"{probe.name}-script"}},
                                               {"name": "plan", "secret": {"secretName": f"{name}-plan"}},
                                               {"name": "tmp", "emptyDir": {}}]))
        kube.wait_ready(ns, name)

    def results(self):
        text = self.probe.kube.logs(self.probe.namespace, self.name, "probe")
        return [json.loads(line) for line in text.splitlines() if line.startswith("{")]

    def stop(self, keep=None):
        """Deletes the pod (after reading its results); returns them."""
        results = self.results()
        if keep and self.probe.evidence:
            text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in results)
            if self.probe.sanitizer:
                text = self.probe.sanitizer.text(text)
            (self.probe.evidence / keep).write_text(text)
        kube, ns = self.probe.kube, self.probe.namespace
        kube.kubectl("delete", "pod", self.name, "-n", ns, "--ignore-not-found", "--wait=true", "--grace-period=1",
                     check=False)
        kube.kubectl("delete", "secret", f"{self.name}-plan", "-n", ns, "--ignore-not-found", check=False)
        return results
