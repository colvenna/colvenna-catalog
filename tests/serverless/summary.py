"""The serverless live test's verdict (#13), from the evidence run.sh wrote.

usage: summary.py <evidence-dir>

One line per check, PASS or FAIL against what the entry must do, then the cold-start figures.
Exits 1 if any check fails.
"""
import json
import os
import sys

# What each request of the driver's "refusals" must get.
REFUSALS = {
    "noAuthorizationConfig": 401, "noAuthorizationUnknownRoute": 401, "noAuthorizationPostCommit": 401,
    "unknownBearerToken": 401, "basicScheme": 401, "emptyBearer": 401, "lowercaseBearerScheme": 401,
    "revokedToken": 401, "disabledPrincipalToken": 401,
    "validTokenWithoutGrantOnDatabase": 403, "validTokenWithoutGrantCreateTable": 403, "nonAdminOnAdminApi": 403,
    "validTokenWithoutGrantConfig": 200, "adminConfig": 200, "adminDatabase": 200,
}
# What the events table's credentials may and may not reach.
S3 = {
    "putInTableRoot": "allowed", "getInTableRoot": "allowed", "listTableRoot": "allowed", "deleteInTableRoot": "allowed",
    "putInSiblingPrefix": "AccessDenied", "putInOtherTable": "AccessDenied", "listDatabasePrefix": "AccessDenied",
}


def load(evidence, name):
    path = os.path.join(evidence, name)
    return json.load(open(path)) if os.path.exists(path) else None


def main():
    evidence = sys.argv[1]
    lines, failed = [], False

    def verdict(ok, text):
        nonlocal failed
        failed |= not ok
        lines.append(f"{'PASS' if ok else 'FAIL'}  {text}")

    cycle = load(evidence, "cycle.json")
    if cycle is not None:
        verdict(cycle.get("eventsMatch") is True and cycle.get("keyedMatch") is True,
                f"cycle: {cycle.get('rowsWritten')} rows written, read back append={cycle.get('eventsRowsRead')} "
                f"keyed={cycle.get('keyedRowsRead')}, snapshots {cycle.get('eventsSnapshot')} {cycle.get('keyedSnapshot')}")
        creds = cycle.get("perTableCredentials")
        if creds is None:
            verdict(False, f"per-table credentials: not checked (data-token.enabled={cycle.get('dataTokenEnabled')})")
        else:
            identity = creds.get("callerIdentity", "")
            verdict(creds.get("tokenStatus") == 200 and identity.startswith("assumed-role/") and "/colvenna-" in identity,
                    f"per-table credentials: token {creds.get('tokenStatus')}, {identity}, {creds.get('secondsLeft')} s left")
            for check, want in S3.items():
                got = (creds.get("s3") or {}).get(check)
                verdict(got == want, f"per-table credentials, S3 {check}: {got} (want {want})")
    refusals = load(evidence, "refusals.json")
    if refusals is not None:
        for case, want in REFUSALS.items():
            got = ((refusals.get("cases") or {}).get(case) or {}).get("status")
            verdict(got == want, f"refusals {case}: {got} (want {want})")
    cold = load(evidence, "cold.json")
    if cold is not None:
        statuses = [r["status"] for r in cold.get("requests", [])]
        verdict(bool(statuses) and all(s == 200 for s in statuses),
                f"cold: {len(statuses)} requests at once: {statuses}, client ms {[r['ms'] for r in cold['requests']]}")
    starts = load(evidence, "cold-starts.json")
    if starts is not None:
        for fn in ("server", "authorizer"):
            s = starts[fn]
            lines.append(f"info  {fn}: {s['coldEnvironments']} cold environment(s); init {s['initDurationMs']}; "
                         f"restore {s['restoreDurationMs']}; first invoke {s['firstInvokeDurationMs']}; "
                         f"init timeouts {s['initTimeouts']}")
    setup = load(evidence, "purges-setup.json")
    if setup is not None:
        verdict(setup.get("done") is True,
                f"purges setup (#93): {setup.get('commits')} commits in {setup.get('seconds')} s, main at "
                f"{setup.get('mainLatest')}, branch b at {setup.get('branchLatest')}, done {setup.get('done')}")
    ops = load(evidence, "purges-ops.json")
    if ops is not None:
        for name, answer in (ops.get("requests") or {}).items():
            # Each request, a 503's resend included, within the serverless form's request work time,
            # well within the gateway's 29 s.
            attempts = answer.get("attempts") or [answer]
            verdict(answer.get("status") == 200 and all(a.get("ms", 99999) <= 8000 for a in attempts),
                    f"purges {name}: {[(a.get('status'), a.get('ms')) for a in attempts]} "
                    f"(want 200, each request within 8000 ms)")
        lines.append(f"info  purges files before {ops.get('filesBefore')}, after the requests {ops.get('filesAfter')}")
    verify = load(evidence, "purges-verify.json")
    if verify is not None:
        roots = verify.get("roots") or {}
        verdict(verify.get("referencedDataFilesMissing") == {},
                f"purges: {verify.get('referencedDataFiles')} data files the roots left read, missing "
                f"{verify.get('referencedDataFilesMissing')}")
        verdict(bool(roots) and not verify.get("rootsWithWrongRows"),
                f"purges: every root left read in full, {len(roots)} roots, wrong {verify.get('rootsWithWrongRows')}")
        verdict(verify.get("orphans") == 0,
                f"purges: Paimon's orphan file cleaning finds {verify.get('orphans')} unreferenced files "
                f"{verify.get('orphanSample')}; files {verify.get('files')}")
    outside = os.path.join(evidence, "outside-vpc.txt")
    if os.path.exists(outside):
        for line in open(outside).read().splitlines():
            verdict("HTTP 2" not in line, f"outside the VPC: {line}")
    print("\n".join(lines))
    print(f"evidence: {evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
