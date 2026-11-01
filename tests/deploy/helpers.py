"""Run bash with the deploy libraries sourced, for the tests of deploy/lib and the entry points."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def run_bash(script, env=None, path_prepend=None, stdin=None):
    """bash -c '<source deploy/lib/*.sh>; <script>' in the repository, its output captured."""
    sources = " ".join(f'source "{p}";' for p in sorted((REPO / "deploy/lib").glob("*.sh")))
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLVENNA_")}
    environment.update(env or {})
    if path_prepend:
        environment["PATH"] = f"{path_prepend}:{environment['PATH']}"
    return subprocess.run(["bash", "-c", f"set -euo pipefail; {sources} {script}"], cwd=REPO, env=environment,
                          input=stdin, capture_output=True, text=True)


# sleep, as a script under test calls it (aws_retry, deploy/lib/aws.sh): no real wait, each one recorded in
# $STUB_DIR/slept (Stubs' directory) and the shell's clock moved on by it, so that the waits' bounds are
# reached at once.
CLOCK = 'sleep() { echo "$1" >>"$STUB_DIR/slept"; SECONDS=$((SECONDS + $1)); };'


def slept(stubs):
    """The waits a script under CLOCK made, in seconds, in order."""
    path = stubs.dir / "slept"
    return [int(s) for s in path.read_text().split()] if path.exists() else []


def throttled(code, operation):
    """A stub's answer (aws) for a throttled call, as the CLI prints it once its own attempts are spent (#94)."""
    return {"stderr": f"aws: [ERROR]: An error occurred ({code}) when calling the {operation} operation "
                      "(reached max retries: 2): Rate exceeded\n", "code": 254}


def write(path, text):
    path.write_text(text)
    return path


def stack_outputs(stack, region="us-west-2", **values):
    """Stub responses (aws) for cfn_output: the outputs of a deployed stack, one per key."""
    query = f"cloudformation describe-stacks --region {region} --stack-name {stack} --query"
    return {f"{query} Stacks[0].Outputs[?OutputKey=='{key}']": {"stdout": f"{value}\n"} for key, value in values.items()}


def parameters(force_ssl, *others):
    """A parameter group's parameters as describe-db-parameters answers in JSON, the CLI having joined
    its pages: rds.force_ssl among others (force_ssl None: not in the list)."""
    listed = [{"ParameterName": name, "ParameterValue": "x"} for name in ("max_connections", *others)]
    if force_ssl is not None:
        listed.append({"ParameterName": "rds.force_ssl", "ParameterValue": force_ssl})
    return {"stdout": {"Parameters": listed}}
