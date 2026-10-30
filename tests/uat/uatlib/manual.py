"""The admin API manual's examples (docs/manual/admin-api.md), run as written for UAT-CAT-05: its
```bash blocks in order, as one script, with only its example names made the run's own (etl-writer,
bi-reader, the sales database and the sales-jobs namespace) so that a run never meets an earlier
one's, and the objects they assume (the sales database's orders table, the namespace) made first.

EKS form: on this host, as the manual's reader would run them: a port-forward to the Service, the
CA and the admin's token from their Secrets, curl and jq. Serverless form: the manual's serverless
steps instead (the token from Secrets Manager, ApiUrl), without what only the EKS form has (kubectl,
the port-forward, the CA), as the manual says; the private API is reached only from inside a VPC
with a listed endpoint, so curl is relayed (curlshim.py) to the case's client pod there."""

import re
from pathlib import Path

from .install import REPO

MANUAL = REPO / "docs" / "manual" / "admin-api.md"
SHIM = Path(__file__).resolve().parent / "curlshim.py"
BLOCK = re.compile(r"^```(bash|text)\n(.*?)^```$", re.S | re.M)


def blocks(path=MANUAL):
    """[(kind, text)] of the manual's code blocks, in order."""
    return [(m.group(1), m.group(2)) for m in BLOCK.finditer(Path(path).read_text(encoding="utf-8"))]


def own_names(text, run):
    """The manual's example names, made the run's own."""
    text = text.replace("etl-writer", f"etl-writer-{run}").replace("bi-reader", f"bi-reader-{run}")
    text = text.replace("sales-jobs", f"sales-jobs-{run}")
    return re.sub(r"(?<![\w-])sales(?![\w-])", f"sales_{run}", text)


def names(run):
    return {"principals": [f"etl-writer-{run}", f"bi-reader-{run}"], "database": f"sales_{run}",
            "namespace": f"sales-jobs-{run}"}


def setup(run, form):
    """What the examples assume exists, made with the admin's token once admin.curlrc is written."""
    lines = ["echo '### setup (not in the manual): the sales database and its orders table'",
             "paimon=\"${CATALOG%/colvenna/admin/v1}/v1/colvenna\""]
    if form == "eks":
        lines.append(f"kubectl create namespace sales-jobs-{run}")
        lines.append(f"kubectl label namespace sales-jobs-{run} colvenna-uat/run={run}")
    lines += [
        f"curl -K admin.curlrc -X POST \"$paimon/databases\" -H 'Content-Type: application/json' "
        f"-d '{{\"name\": \"sales_{run}\", \"options\": {{}}}}'",
        f"curl -K admin.curlrc -X POST \"$paimon/databases/sales_{run}/tables\" -H 'Content-Type: application/json' "
        f"-d '{{\"identifier\": {{\"database\": \"sales_{run}\", \"object\": \"orders\"}}, \"schema\": {{\"fields\": "
        f"[{{\"id\": 0, \"name\": \"order_id\", \"type\": \"BIGINT\"}}, {{\"id\": 1, \"name\": \"amount\", "
        f"\"type\": \"DECIMAL(10, 2)\"}}], \"partitionKeys\": [], \"primaryKeys\": [], \"options\": {{}}}}}}'",
    ]
    return "\n".join(lines) + "\n"


def script(run, form, fill):
    """The examples as one bash script. fill: the serverless block's placeholders (<region>, <env>,
    <api-id>). Each block is announced by a line '### manual block <n>'."""
    parts = ["set -euo pipefail", "trap 'kill $(jobs -p) 2>/dev/null || true' EXIT"]
    if form == "serverless":
        # Only the EKS form has these (the manual: "without what only the EKS form has"). A stand-in that reads
        # its input (-f -) reads it whole first, so that the command piped into it ends normally (#32: a
        # pipeline into an unread stand-in died of SIGPIPE under pipefail).
        parts += ["kubectl() { case \" $* \" in *' -f - '*) cat >/dev/null;; esac; "
                  "echo \"(the EKS form's only, not run: kubectl $*)\"; }",
                  f"curl() {{ python3 -I {SHIM} \"$@\"; }}"]
    n, wrote_curlrc = 0, False
    for kind, text in blocks():
        if form == "eks" and kind != "bash":
            continue
        if form == "serverless":
            if kind == "text" and "secretsmanager" in text:
                for key, value in fill.items():
                    text = text.replace(key, value)
            elif kind != "bash" or "port-forward" in text or "colvenna-catalog-ca-key" in text:
                continue
            text = text.replace(" --cacert ca.crt", "")
            # No port-forward ran, so none is stopped.
            text = "\n".join(line for line in text.splitlines() if 'kill "$forward"' not in line) + "\n"
        n += 1
        parts.append(f"echo '### manual block {n}'")
        parts.append(own_names(text, run))
        if not wrote_curlrc and "admin.curlrc" in text and "header =" in text:
            wrote_curlrc = True
        if wrote_curlrc and "CATALOG=" in "\n".join(parts) and "### setup" not in "\n".join(parts):
            parts.append(setup(run, form))
    return "\n".join(parts) + "\n"
