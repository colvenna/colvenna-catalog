#!/usr/bin/env python3
"""The UAT harness of Colvenna Catalog (tests/uat/README.md): runs a case of docs/uat/cases.md on an
install, from the repository's own test environment, and judges it.

  uat.py run <case> --config deploy/config/<env>.env --test-env <name> --out <dir> [case options]
      runs the case on the install (the region is the configuration's) and writes its evidence
      to <dir>/<case>[-<n>]/: verdicts.tsv, result.json, run.log and the raw evidence;
  uat.py judge <case evidence dir>...
      judges runs again from their verdicts.tsv, against the cases document as it stands;
  uat.py list
      every case and whether this harness runs it;
  uat.py check
      the harness against the cases document: every case it runs lists exactly the document's checks.

Every AWS call uses AWS_PROFILE, which must be set. Exit status: 0 the case passed (run), every run
passed (judge), or done; 1 otherwise; 2 wrong arguments."""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES  # noqa: E402
from uatlib import doc, run, verdicts  # noqa: E402


def parser():
    p = argparse.ArgumentParser(prog="uat.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run a case on an install")
    cases = r.add_subparsers(dest="case", required=True)
    for case_id, module in sorted(CASES.items()):
        c = cases.add_parser(case_id, help=module.__doc__.split("\n")[0])
        c.add_argument("--config", required=True, help="the install's configuration, deploy/config/<env>.env")
        c.add_argument("--test-env", required=True, help="the test environment the clients run in (tests/environment)")
        c.add_argument("--region", help="the region (default: the configuration's)")
        c.add_argument("--out", required=True, help="where the case's evidence goes: <out>/<case>[-<n>]/")
        c.add_argument("--run", help="the run's id (letters and digits; default: a new one)")
        if hasattr(module, "add_arguments"):
            module.add_arguments(c)
    j = sub.add_parser("judge", help="judge runs again from their verdicts")
    j.add_argument("dirs", nargs="+")
    sub.add_parser("list", help="the cases and their harness")
    sub.add_parser("check", help="the harness against the cases document")
    return p


def region_of(args):
    if args.region:
        return args.region
    from uatlib.install import parse_env

    try:
        return parse_env(Path(args.config).read_text()).get("COLVENNA_REGION", "")
    except OSError:
        return ""


def stop(signum, frame):
    """SIGTERM and SIGHUP stop a run as Ctrl-C does: its clean-up and its result still run."""
    raise KeyboardInterrupt(f"signal {signum}")


def judge(dirs):
    status = 0
    for d in dirs:
        d = Path(d)
        body = json.loads((d / "result.json").read_text())
        module = CASES[body["case"]]
        expected = module.checks(body["form"], argparse.Namespace(config=None))
        result, reasons = verdicts.judge(verdicts.read(d / "verdicts.tsv"), expected, body["exitCode"],
                                         getattr(module, "OPTIONAL", ()))
        if body.get("cleanup", {}).get("status") == "FAIL" and result == "PASS":
            result, reasons = "FAIL", ["the clean-up failed: something of the run is left"]
        print(f"{d}: {body['case']} {result}" + (f" ({'; '.join(reasons)})" if reasons else ""))
        status |= result != "PASS"
    return status


def check():
    cases = doc.load()
    problems = []
    for case_id, module in CASES.items():
        if case_id not in cases:
            problems.append(f"{case_id}: not in the cases document")
            continue
        for form in module.FORMS:
            ns = argparse.Namespace(config=None)
            taken, stray, ambiguous = doc.assign(cases[case_id].for_form(form), module.checks(form, ns))
            problems += [f"{case_id} ({form}): the document's {c} is not checked" for c, n in taken.items() if not n]
            problems += [f"{case_id} ({form}): {n} is not a check of the document" for n in stray]
            problems += [f"{case_id} ({form}): {n} fills in more than one of the document's templates" for n in ambiguous]
    for line in problems:
        print(line)
    print(f"{len(CASES)} cases run by this harness: " + ("OK" if not problems else f"{len(problems)} problems"))
    return 1 if problems else 0


def main(argv):
    args = parser().parse_args(argv)
    if args.command == "list":
        cases = doc.load()
        for case_id, case in cases.items():
            print(f"{case_id}\t{'harness' if case_id in CASES else '-'}\t{case.title}")
        return 0
    if args.command == "check":
        return check()
    if args.command == "judge":
        return judge(args.dirs)
    args.region = region_of(args)
    if not args.region:
        print(f"{args.config}: no COLVENNA_REGION, and no --region", file=sys.stderr)
        return 2
    if not os.environ.get("AWS_PROFILE"):
        print("Set AWS_PROFILE: the harness never uses a default profile", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        return run.execute(CASES[args.case], args)
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
