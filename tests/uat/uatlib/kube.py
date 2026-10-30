"""kubectl, as the harness calls it on the test environment's cluster: namespaces of a run's own
(labelled so that the catalog admits them, or not, as the case needs), pods hardened as the cluster
admits them (no service account token, no AWS identity of their own, non-root where the image
allows), and their clean-up. A namespace the run did not make is never used, so never deleted.
Every pod the harness makes runs on the test environment's clients' nodes, never beside the
catalogs: apply gives each pod, and each workload's pod template, the placement (docs/uat/cases.md
3.1 and 3.2)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "compat"))
from compatlib.backends import placement  # noqa: E402,F401 - the matrix's, given to Kube as is

PART_OF = ("app.kubernetes.io/part-of", "colvenna-uat")
RUN_LABEL = "colvenna-uat/run"


class KubeError(RuntimeError):
    pass


def place(manifest, where):
    """The manifest with its pods placed where says (compatlib's placement(): a nodeSelector and
    tolerations): a Pod's own spec, a workload's pod template; other kinds as they are."""
    if not where:
        return manifest
    if manifest.get("kind") == "Pod":
        spec = manifest["spec"]
    elif isinstance(manifest.get("spec", {}).get("template", {}).get("spec"), dict):
        spec = manifest["spec"]["template"]["spec"]
    else:
        return manifest
    spec["nodeSelector"] = dict(spec.get("nodeSelector") or {}, **where["nodeSelector"])
    spec["tolerations"] = list(spec.get("tolerations") or []) + [
        t for t in where["tolerations"] if t not in (spec.get("tolerations") or [])]
    return manifest


class Kube:
    # The labels a run's namespaces carry, and their ingress policy's name (the performance harness
    # makes namespaces of its own runs with its own: tests/performance/perflib/context.py).
    part_of = PART_OF
    run_label = RUN_LABEL
    policy = "uat-ingress"

    def __init__(self, kubeconfig, log=print, placement=None):
        self.env = dict(os.environ, KUBECONFIG=str(kubeconfig))
        self.log = log
        self.made = []  # namespaces this run made, in order
        self.placement = placement  # where its pods run (placement()); None: wherever the scheduler puts them

    def kubectl(self, *args, input=None, check=True, timeout=300):
        p = subprocess.run(["kubectl", *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=self.env, timeout=timeout)
        if check and p.returncode != 0:
            raise KubeError(f"kubectl {' '.join(args[:4])} ... failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
        return p

    def json(self, *args, timeout=120):
        return json.loads(self.kubectl(*args, "-o", "json", timeout=timeout).stdout)

    def apply(self, manifest):
        self.kubectl("apply", "-f", "-", input=json.dumps(place(manifest, self.placement)))

    # --- namespaces ----------------------------------------------------------------------------------

    def namespace(self, name, run_id, labels=None, security="restricted", ingress="none"):
        """Makes a namespace of the run: its labels (the catalog's client label, say), the pod security
        level, and ingress: "none" (no pod of it may be reached), "namespace" (only from its own pods),
        or "any" (no policy). Refuses a namespace of that name that is not the run's."""
        existing = self.kubectl("get", "namespace", name, "-o", "json", check=False)
        if existing.returncode == 0:
            have = json.loads(existing.stdout).get("metadata", {}).get("labels", {})
            if have.get(self.run_label) != run_id:
                raise KubeError(f"namespace {name} exists and is not this run's: refusing to use it")
        elif "NotFound" not in existing.stderr and "not found" not in existing.stderr:
            # Not knowing whether it exists (no access, say) is no licence to label and then delete it.
            raise KubeError(f"cannot tell whether namespace {name} exists: {existing.stderr.strip()[-300:]}")
        all_labels = {self.part_of[0]: self.part_of[1], self.run_label: run_id,
                      "pod-security.kubernetes.io/enforce": security}
        all_labels.update(labels or {})
        self.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": all_labels}})
        if name not in self.made:
            self.made.append(name)
        if ingress == "none":
            spec = {"podSelector": {}, "policyTypes": ["Ingress"]}
        elif ingress == "namespace":
            spec = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {}}]}]}
        else:
            spec = None
        if spec:
            self.apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                        "metadata": {"name": self.policy, "namespace": name}, "spec": spec})
        return name

    def delete_namespace(self, name, run_id):
        """Deletes one namespace, only if it carries the run's label; returns whether it is gone."""
        p = self.kubectl("get", "namespace", name, "-o", "json", check=False)
        if p.returncode != 0:
            return "NotFound" in p.stderr or "not found" in p.stderr
        if json.loads(p.stdout).get("metadata", {}).get("labels", {}).get(self.run_label) != run_id:
            raise KubeError(f"namespace {name} is not this run's: not deleted")
        self.kubectl("delete", "namespace", name, "--wait=true", "--timeout=10m", check=False, timeout=700)
        return self.kubectl("get", "namespace", name, check=False).returncode != 0

    def delete_namespaces(self):
        """Deletes every namespace the run made (last made first) and waits until they are gone;
        returns those still there."""
        left = []
        for name in reversed(self.made):
            self.kubectl("delete", "namespace", name, "--ignore-not-found", "--wait=true", "--timeout=10m",
                         check=False, timeout=700)
            if self.kubectl("get", "namespace", name, check=False).returncode == 0:
                left.append(name)
        self.made = [n for n in self.made if n in left]
        return left

    def secret(self, namespace, name, files):
        """A Secret from {key: text}, through stdin only."""
        data = {k: _b64(v) for k, v in files.items()}
        self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace},
                    "type": "Opaque", "data": data})

    def configmap(self, namespace, name, files):
        self.apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace},
                    "data": files})

    # --- pods ----------------------------------------------------------------------------------------

    def wait_ready(self, namespace, pod, timeout=600):
        end = time.time() + timeout
        last = ""
        while time.time() < end:
            p = self.kubectl("get", "pod", pod, "-n", namespace, "-o", "json", check=False)
            if p.returncode == 0:
                status = json.loads(p.stdout).get("status", {})
                if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                    return
                if status.get("phase") in ("Failed", "Succeeded"):
                    raise KubeError(f"pod {namespace}/{pod} ended ({status.get('phase')}) before it was ready")
                waiting = [s.get("state", {}).get("waiting", {}).get("reason", "")
                           for s in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])]
                last = ",".join(w for w in waiting if w) or status.get("phase", "")
            time.sleep(3)
        raise KubeError(f"pod {namespace}/{pod} not ready after {timeout}s ({last})")

    def wait_done(self, namespace, pod, timeout):
        """Waits until the pod's containers ended; returns the first container's exit code (-1 if it
        did not end in time)."""
        end = time.time() + timeout
        while time.time() < end:
            p = self.kubectl("get", "pod", pod, "-n", namespace, "-o", "json", check=False)
            if p.returncode == 0:
                status = json.loads(p.stdout).get("status", {})
                if status.get("phase") in ("Succeeded", "Failed"):
                    states = status.get("containerStatuses", [])
                    term = states[0].get("state", {}).get("terminated", {}) if states else {}
                    return term.get("exitCode", -1)
            time.sleep(5)
        return -1

    def logs(self, namespace, pod, container=None):
        args = ["logs", pod, "-n", namespace] + (["-c", container] if container else ["--all-containers"])
        p = self.kubectl(*args, check=False, timeout=300)
        return p.stdout if p.returncode == 0 else p.stderr

    def exec(self, namespace, pod, command, input=None, timeout=600, container=None, check=True):
        args = ["exec", "-i", pod, "-n", namespace] + (["-c", container] if container else []) + ["--", *command]
        return self.kubectl(*args, input=input, timeout=timeout, check=check)


def hardened(uid, gid=None, read_only=True):
    """A container's security context as the restricted pod security level wants it."""
    return {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True,
            "runAsUser": uid, "runAsGroup": gid if gid is not None else uid, "readOnlyRootFilesystem": read_only,
            "seccompProfile": {"type": "RuntimeDefault"}}


def _b64(text):
    import base64

    return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()


def pod(namespace, name, containers, volumes, labels=None):
    """A pod as the harness runs every one: never restarted, no service account token, the runtime's
    seccomp profile."""
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": dict({"app": name}, **(labels or {}))},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": containers, "volumes": volumes}}
