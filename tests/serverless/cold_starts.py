"""Cold starts of the serverless form (#13), from the functions' own log lines.

usage: cold_starts.py <server-reports.json> <authorizer-reports.json> <cold.json>

The first two are `aws logs filter-log-events` output (REPORT, INIT_REPORT and RESTORE_REPORT lines
since the run started); cold.json is the driver's "cold" result, whose client latencies are added.
Per execution environment (log stream): Init Duration (on-demand), Restore Duration (SnapStart), the
first invocation's Duration, and whether an init timed out. Prints JSON.
"""
import json
import re
import statistics
import sys


def number(pattern, text):
    found = re.search(pattern, text)
    return float(found.group(1)) if found else None


def spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values)}


def environments(path):
    streams = {}
    for event in json.load(open(path)).get("events", []):
        streams.setdefault(event["logStreamName"], []).append(event)
    found = []
    for events in streams.values():
        events.sort(key=lambda e: e["timestamp"])
        text = "\n".join(e["message"] for e in events)
        reports = [e["message"] for e in events if e["message"].startswith("REPORT")]
        found.append({
            "initDurationMs": number(r"Init Duration: ([\d.]+) ms", text),
            "restoreDurationMs": number(r"Restore Duration: ([\d.]+) ms", text),
            "firstInvokeDurationMs": number(r"\tDuration: ([\d.]+) ms", reports[0]) if reports else None,
            "invocations": len(reports),
            "initTimedOut": "Status: timeout" in text,
            "maxMemoryUsedMb": number(r"Max Memory Used: (\d+) MB", text),
        })
    return found


def summary(path):
    envs = environments(path)
    cold = [e for e in envs if e["initDurationMs"] is not None or e["restoreDurationMs"] is not None]
    return {
        "environmentsSeen": len(envs),
        "coldEnvironments": len(cold),
        "initDurationMs": spread([e["initDurationMs"] for e in cold]),
        "restoreDurationMs": spread([e["restoreDurationMs"] for e in cold]),
        "firstInvokeDurationMs": spread([e["firstInvokeDurationMs"] for e in cold]),
        "initTimeouts": sum(e["initTimedOut"] for e in envs),
        "environments": cold,
    }


def main():
    server, authorizer, cold = sys.argv[1:4]
    try:
        requests = json.load(open(cold)).get("requests", [])
    except (OSError, ValueError):
        requests = []
    print(json.dumps({
        "server": summary(server),
        "authorizer": summary(authorizer),
        "client": {"statuses": [r["status"] for r in requests], "latencyMs": spread([r["ms"] for r in requests])},
    }, indent=1))


if __name__ == "__main__":
    main()
