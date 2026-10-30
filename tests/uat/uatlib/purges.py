"""The files of dropped tables (#69). A drop removes the table from the catalog at once. In the EKS form
the dropping request then deletes the table's files itself; in the serverless form the purge function
does, on its schedule (every 15 minutes), and a failed purge stays recorded (GET /purges) for its next
run. A case that looks for a dropped table's files runs the purge function first (`now`), as an
operator can: an IAM-authenticated Invoke, never anything that reaches the catalog's network."""

import base64
import json
import re
import time


def summary(log):
    """The purge command's summary, its last line of JSON on standard output, or None."""
    for line in reversed((log or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"purged"' in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def now(ctx, tries=8, wait=15):
    """Serverless: one run of the purge function, now, and what it said: {"exitCode", "error", "summary"}
    (error: the function's error type, PurgeFailed when a purge failed and stays recorded). EKS: None, a
    drop having deleted its files. The function runs one invocation at a time: a run of its schedule's
    at the same moment refuses this one (TooManyRequestsException), which is asked again."""
    if ctx.form != "serverless":
        return None
    function = ctx.install.output("Serverless", "PurgeFunctionName")
    out = ctx.scratch / "purge-answer.json"
    last = ""
    for _ in range(tries):
        p = ctx.aws.raw("lambda", "invoke", "--function-name", function, "--cli-read-timeout", "960",
                        "--cli-binary-format", "raw-in-base64-out", "--payload", '"purge"', str(out),
                        "--output", "json", timeout=1000, check=False)
        if p.returncode == 0:
            meta = json.loads(p.stdout or "{}")
            body = json.loads(out.read_text() or "{}")
            log = base64.b64decode(body.get("log", "") or "").decode("utf-8", "replace")
            ctx.keep("purge-run.log", log)
            return {"exitCode": body.get("exitCode"), "error": meta.get("FunctionError") and body.get("errorType"),
                    "summary": summary(log)}
        last = p.stderr
        if "TooManyRequestsException" not in last:
            break
        time.sleep(wait)
    raise RuntimeError(f"invoking the purge function failed: {last.strip()[-400:]}")


def objects_under(ctx, path, most=10):
    """How many objects are under a table's location (s3://bucket/key), up to most, by the operator's
    identity; None for a location not on S3."""
    m = re.match(r"^s3a?://([^/]+)/(.+?)/?$", path or "")
    if not m:
        return None
    out = ctx.aws("s3api", "list-objects-v2", "--bucket", m.group(1), "--prefix", m.group(2) + "/",
                  "--max-items", str(most))
    return len((out or {}).get("Contents", []))
