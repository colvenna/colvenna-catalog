#!/usr/bin/env python3
"""The performance acceptance's harness (tests/performance/README.md): one subcommand per scenario of
docs/performance/terms.md, run against installs of Colvenna Catalog from this repository's test
environment, each run judged against the confirmed targets and its clean-up verified.

  AWS_PROFILE=<profile> tests/performance/perf.py run P1a --config deploy/config/<env>.env \\
      --test-env <name> --out <evidence>
  tests/performance/perf.py list            the scenarios, their load and their targets
  tests/performance/perf.py check           the harness's targets against terms.md, cell by cell
  tests/performance/perf.py summary <run>   a run's summary.md again, from its result.json
  AWS_PROFILE=<profile> tests/performance/perf.py cleanup <run> --config ... --test-env <name> --out <evidence>
                                            what a killed run left, removed and read back
"""

import argparse
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perflib import report, terms  # noqa: E402


def parser():
    p = argparse.ArgumentParser(prog="perf.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one scenario")
    run.add_argument("scenario", choices=terms.SCENARIOS)
    run.add_argument("--config", action="append", required=True,
                     help="the install's configuration (deploy/config/<env>.env); P5: once per form")
    run.add_argument("--test-env", required=True, help="the test environment whose cluster runs the load")
    run.add_argument("--region", help="the installs' region (default: the configuration's COLVENNA_REGION)")
    run.add_argument("--out", required=True, help="the private evidence directory")
    run.add_argument("--run", help="the run's id (default: a new one)")
    run.add_argument("--duration-scale", type=float, default=1.0,
                     help="scale every warm-up, window and step (a trial of the harness; the run is marked reduced)")
    run.add_argument("--rounds", type=int, help="P4: rounds of forced cold starts (default 8)")
    run.add_argument("--idle-samples", type=int, help="P4: requests after an idle wait (default 6)")
    run.add_argument("--idle-minutes", type=int, help="P4: minutes without traffic before each (default 30)")
    run.add_argument("--installer-checkout", help="P4b: the candidate's checkout whose install.sh switches SnapStart")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--from-source", action="store_true", help="P4b: the install is from source")
    source.add_argument("--release", help="P4b: the install is of this candidate")
    clean = sub.add_parser("cleanup", help="remove what a killed run left, and read it back")
    clean.add_argument("leftover", help="the run's id")
    clean.add_argument("--config", action="append", required=True, help="the install's configuration")
    clean.add_argument("--test-env", required=True)
    clean.add_argument("--region")
    clean.add_argument("--out", required=True, help="the evidence directory (cleanup/ is made in it)")
    clean.add_argument("--run", help=argparse.SUPPRESS)
    clean.add_argument("--duration-scale", type=float, default=1.0, help=argparse.SUPPRESS)
    sub.add_parser("list", help="the scenarios and their targets")
    sub.add_parser("check", help="the harness's targets against terms.md")
    s = sub.add_parser("summary", help="a run's summary.md again from its result.json")
    s.add_argument("evidence")
    return p


def region_of(configs):
    from perflib import context  # noqa: F401 - puts the UAT library on the path
    from uatlib.install import EXAMPLE, parse_env

    regions = set()
    for c in configs:
        conf = dict(parse_env(EXAMPLE.read_text()), **parse_env(Path(c).read_text()))
        regions.add(conf.get("COLVENNA_REGION"))
    if len(regions) != 1:
        raise SystemExit(f"the configurations name {len(regions)} regions; give one, the same")
    return regions.pop()


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "check":
        problems = terms.check()
        for p in problems:
            print(p)
        print("terms.md and the harness agree" if not problems else f"{len(problems)} problems")
        return 1 if problems else 0
    if args.command == "list":
        for section, targets in terms.TARGETS.items():
            print(section)
            for t in targets:
                print(f"  {t.row}: {t.cells}")
        print(json.dumps(terms.LOAD, indent=1))
        return 0
    if args.command == "summary":
        body = json.loads((Path(args.evidence) / "result.json").read_text())
        (Path(args.evidence) / "summary.md").write_text(report.summary(body))
        return 0
    if args.command == "cleanup":
        args.region = args.region or region_of(args.config)
        from perflib import scenarios

        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, stopped)
        return scenarios.leftovers(args)
    problems = terms.check()
    if problems:
        print("the harness's targets are not terms.md's:\n" + "\n".join(problems), file=sys.stderr)
        return 2
    if args.scenario != "P5" and len(args.config) != 1:
        print(f"{args.scenario} runs on one install: give --config once", file=sys.stderr)
        return 2
    if args.scenario == "P5" and len(args.config) != 2:
        print("P5 runs on both forms at once: give --config twice", file=sys.stderr)
        return 2
    args.region = args.region or region_of(args.config)
    from perflib import scenarios

    # SIGTERM and SIGHUP stop a run as Ctrl-C does: its clean-up and result still run.
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stopped)
    return scenarios.execute(args.scenario, args)


def stopped(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


if __name__ == "__main__":
    sys.exit(main())
