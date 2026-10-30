"""Stub commands (aws, kubectl, ...) for the deployment scripts' tests: each answers by the longest
key of its responses that prefixes its arguments, and logs every call. A key's answer may be a list:
its answers, one per call in turn, the last one again once they run out."""
import json
import os
import stat
import tempfile
from pathlib import Path

STUB = r'''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
here = os.environ["STUB_DIR"]
args = " ".join(sys.argv[1:])
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write(f"{name} {args}\n")
responses = json.load(open(os.path.join(here, "responses.json"))).get(name, {})
match = max((k for k in responses if args.startswith(k)), key=len, default=None)
if match is None:
    print(f"stub {name}: no response for: {args}", file=sys.stderr)
    sys.exit(97)
answer = responses[match]
if isinstance(answer, list):
    counted = os.path.join(here, "counts.json")
    counts = json.load(open(counted)) if os.path.exists(counted) else {}
    turn = counts.get(f"{name} {match}", 0)
    counts[f"{name} {match}"] = turn + 1
    json.dump(counts, open(counted, "w"))
    answer = answer[min(turn, len(answer) - 1)]
sys.stdout.write(answer.get("stdout", "") if isinstance(answer.get("stdout", ""), str) else json.dumps(answer["stdout"]))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("code", 0))
'''


class Stubs:
    """A directory of stub commands; put .bin first on PATH and STUB_DIR in the environment."""

    def __init__(self, responses, commands=("aws", "kubectl")):
        self.dir = Path(tempfile.mkdtemp())
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.dir / "responses.json").write_text(json.dumps(responses))
        for command in commands:
            path = self.bin / command
            path.write_text(STUB)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    @property
    def env(self):
        return {"STUB_DIR": str(self.dir)}

    def calls(self):
        log = self.dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []
