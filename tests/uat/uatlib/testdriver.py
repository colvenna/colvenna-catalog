"""The serverless live test's driver (tests/serverless/driver: the Paimon 2.0.0 client of the server's
own image), as the deployment cases use it beyond tests/serverless/run.sh:

  image(ctx, install)                 the driver image, built on the image the install runs and pushed
                                      into the install's own repository as test-driver-<hash> (as
                                      run.sh does); kept until tests/serverless/down.sh removes it
  run_pod(ctx, install, image, event) one action in a pod of the run's own client namespace (the EKS
                                      form's clients), with --local: the event (the admin's token in
                                      it) from a Secret, the result on its output
  Lambda(ctx, install, image, ...)    the driver as a function in given subnets (the test
                                      environment's client subnet, UAT-DEP-11), invoked with IAM;
                                      down() is tests/serverless/down.sh"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .install import REPO, stack_suffix
from .kube import hardened

DRIVER = REPO / "tests" / "serverless" / "driver"
JAVA = ["java", "-XX:MaxRAMPercentage=75", "-Djava.io.tmpdir=/tmp", "-cp",
        "/opt/colvenna-catalog-test/driver.jar:/opt/colvenna-catalog/lib/*:/opt/colvenna-catalog-test/"
        "paimon-hadoop-uber-2.0.0.jar", "com.colvenna.catalog.testdriver.DriverMain"]


def running_image(ctx, install):
    """repository@digest of the image the install runs."""
    if install.form == "eks":
        deploy = ctx.kube.json("get", "deployment", install.release, "-n", install.namespace)
        return deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    return install.output("Serverless", "ServerImage")


def build(ctx):
    """The driver's jars (tests/serverless/driver/build.sh) and Dockerfile, in a directory of the run."""
    work = Path(tempfile.mkdtemp(prefix="uat-driver-", dir=ctx.scratch))
    p = subprocess.run([str(DRIVER / "build.sh"), str(work)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    if p.returncode != 0:
        raise RuntimeError(f"the driver did not build: {p.stdout[-1500:]}")
    shutil.copy(DRIVER / "Dockerfile", work / "Dockerfile")
    return work


def image(ctx, install, work=None):
    """The driver image for the install, built (or found) in its repository: repository, tag, digest,
    ref (repository@digest) and base (the image the install runs)."""
    base = running_image(ctx, install)
    repository = base.split("@", 1)[0]
    work = work or build(ctx)
    digest_of = hashlib.sha256()
    for name in ("driver.jar", "Dockerfile"):
        digest_of.update((work / name).read_bytes())
    digest_of.update(base.encode())
    tag = "test-driver-" + digest_of.hexdigest()[:12]
    name = repository.split("/", 1)[1]
    have = ctx.aws.raw("ecr", "describe-images", "--repository-name", name, "--image-ids", f"imageTag={tag}",
                       "--query", "imageDetails[0].imageDigest", "--output", "text", check=False).stdout.strip()
    if not have.startswith("sha256:"):
        env = dict(os.environ, DOCKER_CONFIG=str(work / "docker"))
        password = ctx.aws.raw("ecr", "get-login-password").stdout
        subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", repository.split("/", 1)[0]],
                       input=password, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                       env=env, check=True)
        # A single-platform Docker v2 manifest, as Lambda takes it; only COPY steps, so any host builds it.
        b = subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false",
                            "--build-arg", f"BASE={base}", "--output",
                            f"type=registry,name={repository}:{tag},oci-mediatypes=false", str(work)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=env)
        if b.returncode != 0:
            raise RuntimeError(f"the driver image did not build: {b.stdout[-1500:]}")
        have = ctx.aws.raw("ecr", "describe-images", "--repository-name", name, "--image-ids", f"imageTag={tag}",
                           "--query", "imageDetails[0].imageDigest", "--output", "text").stdout.strip()
    shutil.rmtree(work / "docker", ignore_errors=True)
    return {"repository": repository, "tag": tag, "digest": have, "ref": f"{repository}@{have}", "base": base}


def event(install, action, token, **fields):
    body = {"action": action, "uri": install.uri, "warehouse": install.warehouse, "token": token}
    body.update(fields)
    return body


def run_pod(ctx, namespace, driver_image, body, name="driver", timeout=1200):
    """One action of the driver in a pod of the namespace; returns its result (the JSON it printed)."""
    kube = ctx.kube
    kube.secret(namespace, f"{name}-event", {"event.json": json.dumps(body)})
    container = {"name": "driver", "image": driver_image["ref"], "command": JAVA + ["--local", "/event/event.json"],
                 "securityContext": hardened(1000),
                 # A small CPU request, beside the other clients on the test environment's clients' nodes.
                 "resources": {"requests": {"cpu": "200m", "memory": "2Gi"}, "limits": {"memory": "2Gi"}},
                 "volumeMounts": [{"name": "event", "mountPath": "/event", "readOnly": True},
                                  {"name": "tmp", "mountPath": "/tmp"}]}  # nosec B108 - the pod's own /tmp
    kube.kubectl("delete", "pod", name, "-n", namespace, "--ignore-not-found", "--wait=true", check=False)
    kube.apply({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace},
                "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                         "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [container],
                         "volumes": [{"name": "event", "secret": {"secretName": f"{name}-event"}},
                                     {"name": "tmp", "emptyDir": {}}]}})
    code = kube.wait_done(namespace, name, timeout)
    out = kube.logs(namespace, name, "driver")
    kube.kubectl("delete", "secret", f"{name}-event", "-n", namespace, "--ignore-not-found", check=False)
    start = out.find("{\n")
    result = None
    if start >= 0:
        try:
            result = json.loads(out[start:])
        except ValueError:
            result = None
    return {"exitCode": code, "result": result, "output": out[-4000:] if result is None else ""}


class Lambda:
    """The driver deployed as ColvennaCatalogTest<Env>Driver in the given subnets (driver-app.ts)."""

    def __init__(self, ctx, install, driver_image, vpc_id, subnet_ids, zones, api_port=None):
        self.ctx, self.install, self.image = ctx, install, driver_image
        self.state = ctx.scratch / "driver-cdk"
        self.function = f"colvenna-catalog-{install.env}-test-driver"
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout.strip()
        context = {"env": install.env, "account": ctx.aws.account(), "region": install.region, "sourceSha": sha,
                   "vpcId": vpc_id, "subnetIds": ",".join(subnet_ids), "subnetZones": ",".join(zones),
                   "repositoryName": driver_image["repository"].split("/", 1)[1], "imageDigest": driver_image["digest"]}
        if api_port:
            context["apiPort"] = str(api_port)
        args = []
        for k, v in context.items():
            args += ["-c", f"{k}={v}"]
        stack = f"ColvennaCatalogTest{stack_suffix(install.env)}Driver"
        aws_dir = REPO / "deploy" / "aws"
        if not (aws_dir / "node_modules").is_dir():
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=str(aws_dir), check=True,
                           stdout=subprocess.DEVNULL)
        p = subprocess.run(["npx", "cdk", "-a", "npx ts-node test-env/driver-app.ts", "deploy", stack,
                            "--require-approval", "never", "--no-notices", "--no-version-reporting",
                            "--output", str(self.state)] + args,
                           cwd=str(aws_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                           env=ctx.aws.env())
        ctx.keep("driver-deploy.log", p.stdout)
        if p.returncode != 0:
            raise RuntimeError(f"the driver stack did not deploy: {p.stdout[-1500:]}")

    def invoke(self, body, name):
        """One action; returns its result. The event goes through a file only this host's user reads."""
        work = Path(tempfile.mkdtemp(prefix="uat-invoke-", dir=self.ctx.scratch))
        (work / "event.json").write_text(json.dumps(body))
        os.chmod(work / "event.json", 0o600)
        out = work / "out.json"
        meta = self.ctx.aws.raw("lambda", "invoke", "--function-name", self.function, "--cli-read-timeout", "960",
                                "--cli-binary-format", "raw-in-base64-out", "--payload", f"fileb://{work / 'event.json'}",
                                str(out), "--output", "json")
        text = out.read_text()
        shutil.rmtree(work, ignore_errors=True)
        self.ctx.keep(f"driver-{name}.json", text)
        if "FunctionError" in meta.stdout:
            raise RuntimeError(f"the driver failed on {name}: {text[:400]}")
        return json.loads(text)

    def down(self):
        return down(self.ctx, self.install)


def down(ctx, install):
    """tests/serverless/down.sh: the driver's stack (whatever state a failed deploy left it in), its log
    group and the test-driver-* images. A case registers it before it deploys the driver."""
    p = subprocess.run([str(REPO / "tests" / "serverless" / "down.sh"), install.env, install.region],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=ctx.aws.env())
    ctx.keep("driver-down.log", p.stdout)
    if p.returncode != 0:
        raise RuntimeError(f"tests/serverless/down.sh failed: {p.stdout[-800:]}")
    return "the driver stack, its log group and its images removed"
