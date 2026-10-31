#!/usr/bin/env python3
"""The compatibility matrix of Colvenna Catalog (tests/compat/README.md).

  matrix.py run --form eks --backend docker|kubernetes --uri <catalog> --token-file <file> [...]
      runs the selected cells (all by default) against the catalog and writes their results;
  matrix.py page <run dir>... [--out <file>]
      the matrix page (Markdown) from runs' results;
  matrix.py rejudge <run dir>...
      judges runs again from their kept raw output, after the judge's rules change;
  matrix.py check
      checks cells.json and declared.tsv (no catalog, no containers).

Exit status: 0 every declared-supported entry of the run passed (run), or done (page, check);
1 some did not, or the run could not finish; 2 wrong arguments or configuration."""

import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compatlib import config, page  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent


def parser():
    p = argparse.ArgumentParser(prog="matrix.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run cells against a catalog")
    r.add_argument("--form", required=True, choices=config.FORMS, help="the catalog's deployment form")
    r.add_argument("--backend", required=True, choices=("docker", "kubernetes"),
                   help="where the engines run: Docker on this host, or pods of the cluster kubectl reaches")
    r.add_argument("--uri", required=True, help="the catalog's REST URI, as the engines reach it")
    r.add_argument("--token-file", required=True,
                   help="a bearer token whose principal may create and drop databases (MANAGE on the catalog)")
    r.add_argument("--read-token-file", help="a token the readers (Doris, StarRocks) use instead; READ on the catalog")
    r.add_argument("--ca-file", help="the catalog's CA certificate (PEM), when its certificate is a private CA's")
    r.add_argument("--warehouse", default="colvenna", help="the warehouse name (default: colvenna)")
    r.add_argument("--cells", default="all", help="comma-separated cell ids (cells.json), or all")
    r.add_argument("--out", default=str(REPO / ".state" / "compat"), help="where runs' results go")
    r.add_argument("--s3-region", help="the warehouse bucket's region, for the probe of the engines' own identity")
    r.add_argument("--no-data-tokens", action="store_true",
                   help="for a catalog that vends no per-table credentials: engines then need storage access "
                        "of their own, and the credentials entries do not run")
    r.add_argument("--server-log", default="none",
                   help="the catalog's access log: kubectl:<namespace>:<label selector>, command:<command> or none")
    r.add_argument("--server-log-delay", type=int, default=0,
                   help="seconds to wait before reading a cell's access log (a log service's ingestion lag)")
    r.add_argument("--principal", help="the token's principal, to read only its requests from the access log")
    r.add_argument("--read-principal", help="the read token's principal")
    r.add_argument("--starrocks-s3-file",
                   help="JSON of StarRocks's own read-only S3 properties for the StarRocks cell: aws.s3.*, and with "
                        "short-lived credentials paimon.option.fs.s3a.* too (docs/manual/user.md, section 4.5)")
    r.add_argument("--starrocks-s3-description",
                   help="what StarRocks's own credentials are (stated in its cell), e.g. which role and what it may read")
    r.add_argument("--fixture-writer", help="the writer cell that writes the readers' tables (default: cells.json's)")
    r.add_argument("--redact", action="append", default=[],
                   help="a deployment value to keep out of the results (a bucket, a cluster name, a host)")
    r.add_argument("--region", help="the AWS region, recorded with the results")
    r.add_argument("--catalog-version", help="the catalog's version or commit, recorded with the results")
    k = r.add_argument_group("kubernetes")
    k.add_argument("--namespace",
                   help="the namespace the runner makes and deletes (default: colvenna-compat-<run>); an existing "
                        "one it did not make is refused")
    k.add_argument("--client-label", default="colvenna-catalog/client=true",
                   help="the label the catalog's NetworkPolicy admits client namespaces by")
    k.add_argument("--kubeconfig", help="the cluster's kubeconfig (default: kubectl's)")
    k.add_argument("--node-selector", metavar="KEY=VALUE",
                   help="the nodes the engines' pods run on, by a label (this repository's test environment: its "
                        "output ClientNodeSelector, the clients' nodes)")
    k.add_argument("--toleration", metavar="KEY=VALUE:EFFECT",
                   help="a taint the engines' pods tolerate (the test environment's output ClientNodeTaint)")
    k.add_argument("--keep", action="store_true", help="keep the namespace afterwards, with the run's token Secret")
    d = r.add_argument_group("docker")
    d.add_argument("--docker-network", default="bridge",
                   help="the network of each pod's first container (default bridge: nothing is published); host "
                        "exposes the engines' ports on the host's interfaces")
    d.add_argument("--docker-add-host", action="append",
                   help="a name:address every container resolves, e.g. the name in the catalog's certificate and "
                        "the bridge's gateway, where a port-forward to the catalog listens")
    d.add_argument("--docker-volume", action="append", help="an extra host:container mount for every container")
    d.add_argument("--docker-user", help="a uid:gid the writers run as instead of their images' (Doris and StarRocks "
                                         "stay root)")
    d.add_argument("--jar-cache", default="~/.cache/colvenna-compat/jars", help="where the Paimon jars are kept")

    g = sub.add_parser("page", help="the matrix page from runs' results")
    g.add_argument("runs", nargs="+", help="run directories, one per form")
    g.add_argument("--out", help="the page's file (default: standard output)")
    g.add_argument("--link-base", help="the runs' directory as the page links it (default: their paths)")

    j = sub.add_parser("rejudge", help="judge runs again from their kept raw output, by the current judge")
    j.add_argument("runs", nargs="+", help="run directories")

    sub.add_parser("check", help="check cells.json and declared.tsv")
    return p


def interrupted(signum, frame):
    # A run being stopped still drops its databases and removes its containers (the finally blocks).
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv=None):
    args = parser().parse_args(argv)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        if args.command == "check":
            cells = config.load_cells()
            declared = config.load_declarations(cells)
            forms = sorted({k[0] for k in declared})
            print(f"{len(cells['cells'])} cells, {len(declared)} declarations ({', '.join(forms)}): OK")
            return 0
        if args.command == "page":
            text = page.render(args.runs, args.link_base)
            if args.out:
                Path(args.out).write_text(text)
            else:
                sys.stdout.write(text)
            return 0
        if args.command == "rejudge":
            from compatlib.rejudge import rejudge
            from compatlib.sanitize import Sanitizer
            cells = config.load_cells()
            declared = config.load_declarations(cells)
            for run_dir in args.runs:
                changed = rejudge(run_dir, declared, cells, Sanitizer())
                print(f"{run_dir}: " + ("; ".join(f"{c}: {', '.join(f'{t} {o} {b}->{a}' for (t, o), (b, a) in d.items())}"
                                                  for c, d in changed.items()) or "no verdict changed"))
            return 0
        from compatlib.runner import run_matrix
        run, outcomes = run_matrix(args, log=lambda m: print(m, flush=True))
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    failed = [c for c, o in outcomes.items() if o != "pass"]
    print(f"{len(outcomes) - len(failed)} of {len(outcomes)} cells passed every declared-supported entry"
          + (f"; not: {', '.join(failed)}" if failed else ""))
    cleanup = run.cleanup.get("status") if run.cleanup else None
    if cleanup != "PASS":
        print(f"the clean-up did not pass: {run.cleanup}")
    return 1 if failed or len(outcomes) != len(run.cells) or cleanup != "PASS" else 0


if __name__ == "__main__":
    sys.exit(main())
