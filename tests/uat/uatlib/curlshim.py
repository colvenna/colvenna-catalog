#!/usr/bin/env python3
"""curl, as the admin API manual's examples call it, relayed to a client pod inside a VPC that
reaches the serverless form's private API (UAT-CAT-05; manual.py defines curl() to call this). It
takes the options the examples use: -K <curlrc> (its header, silent, show-error and fail-with-body
lines), -s, -S, -X, -H, -d/--data, -o, -w '%{http_code}...', and the URL; the request is made by
the probe (tests/uat/probe/probe.py) in the pod named by UAT_PROBE_NAMESPACE and UAT_PROBE_POD,
through KUBECONFIG. Like curl, it writes the body to stdout or -o, and exits 22 for an HTTP error
with --fail-with-body; the token stays in the plan on stdin."""

import json
import os
import re
import subprocess
import sys


def parse(argv):
    opts = {"method": None, "headers": [], "data": None, "out": None, "write": None, "fail": False, "url": None,
            "silent": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-K":
            for line in open(argv[i + 1], encoding="utf-8"):
                line = line.strip()
                m = re.match(r'^header\s*=\s*"(.*)"$', line)
                if m:
                    opts["headers"].append(m.group(1))
                elif line == "fail-with-body":
                    opts["fail"] = True
                elif line == "silent":
                    opts["silent"] = True
            i += 2
        elif a in ("-X", "--request"):
            opts["method"] = argv[i + 1]
            i += 2
        elif a in ("-H", "--header"):
            opts["headers"].append(argv[i + 1])
            i += 2
        elif a in ("-d", "--data", "--data-raw"):
            opts["data"] = argv[i + 1]
            i += 2
        elif a in ("-o", "--output"):
            opts["out"] = argv[i + 1]
            i += 2
        elif a in ("-w", "--write-out"):
            opts["write"] = argv[i + 1]
            i += 2
        elif a == "--cacert":
            i += 2
        elif a in ("--fail-with-body",):
            opts["fail"] = True
            i += 1
        elif a.startswith("-") and not a.startswith("http"):
            if "s" in a.lstrip("-") and not a.startswith("--"):
                opts["silent"] = True
            i += 1
        else:
            opts["url"] = a
            i += 1
    return opts


def main(argv):
    opts = parse(argv)
    headers, auth = {}, None
    for h in opts["headers"]:
        name, _, value = h.partition(":")
        if name.strip().lower() == "authorization":
            auth = value.strip()
        else:
            headers[name.strip()] = value.strip()
    step = {"name": "curl", "method": opts["method"] or ("POST" if opts["data"] is not None else "GET"),
            "url": opts["url"], "headers": headers, "full": True}
    if auth is not None:
        step["authorization"] = auth
    if opts["data"] is not None:
        step["text"] = opts["data"]
    plan = {"base": "", "ca": None, "tokens": {}, "steps": [step]}
    p = subprocess.run(["kubectl", "exec", "-i", os.environ["UAT_PROBE_POD"], "-n", os.environ["UAT_PROBE_NAMESPACE"], "--",
                        "python3", "/uat/probe.py"], input=json.dumps(plan), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=120)
    lines = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
    if not lines:
        sys.stderr.write(f"curl (relayed): the probe failed: {p.stderr[-500:]}\n")
        return 7
    r = lines[0]
    if "status" not in r:
        sys.stderr.write(f"curl (relayed): {r.get('error')}\n")
        return 7
    body = r.get("body")
    text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
    if opts["out"]:
        with open(opts["out"], "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    if opts["write"]:
        sys.stdout.write(opts["write"].replace("%{http_code}", str(r["status"])).replace("\\n", "\n"))
    if opts["fail"] and r["status"] >= 400:
        sys.stderr.write(f"curl: (22) The requested URL returned error: {r['status']}\n")
        return 22
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
