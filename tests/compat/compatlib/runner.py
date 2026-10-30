"""One run of the matrix: builds the flows, runs the selected cells one after another (the
fixture and the readers, then the writers), drops every database the run made, and writes the
results:

  <out>/<run id>/run.json            the run: when, where, what (images, jars, the matrix's commit)
  <out>/<run id>/cells/<cell>.json   a cell's verdicts, with their declarations and evidence
  <out>/<run id>/raw/<cell>/...      its containers' output and the catalog's access log of its window
  <out>/<run id>/summary.json        every cell's outcome

Everything written goes through the Sanitizer first: no token, account ID, ARN, bucket, address or
URI of the deployment is kept."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import urllib.parse
from datetime import timedelta
from pathlib import Path

from . import accesslog, cells, config, judge
from .backends import Docker, Kubernetes, ServerLog, now, placement, write_mounted
from .sanitize import Sanitizer

HERE = config.HERE
FLOWS = HERE / "flows"
ENGINE = HERE / "engine"
FLOW_JARS = {"compat-flink.jar": "flink", "compat-spark-3.5.jar": "spark-3.5", "compat-spark-4.1.jar": "spark-4.1"}


def build_flows(cells_config, log):
    """The flow jars, built from tests/compat/flows with Maven: the host's if it has one, else in
    the pinned Maven image."""
    if shutil.which("mvn"):
        args = ["mvn", "-B", "-f", str(FLOWS / "pom.xml"), "package", "-DskipTests"]
    else:
        m2 = Path.home() / ".m2"
        m2.mkdir(exist_ok=True)
        args = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "MAVEN_CONFIG=/m2",
                "-v", f"{m2}:/m2", "-v", f"{FLOWS}:/flows", "-w", "/flows", cells_config["images"]["maven"],
                "mvn", "-B", "-Dmaven.repo.local=/m2/repository", "package", "-DskipTests"]
    log(f"building the flows: {' '.join(args[:3])} ...")
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    # The exit status alone is not the verdict: the build must say BUILD SUCCESS.
    if p.returncode != 0 or "[INFO] BUILD SUCCESS" not in p.stdout.splitlines():
        raise RuntimeError("the flows did not build:\n" + p.stdout[-4000:])
    return {name: FLOWS / module / "target" / name for name, module in FLOW_JARS.items()}


def git_head():
    try:
        sha = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(HERE), "status", "--porcelain", "--", "."],
                               stdout=subprocess.PIPE, universal_newlines=True).stdout.strip() != ""
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Run:
    def __init__(self, args, log=print):
        self.args = args
        self.log = log
        self.config = config.load_cells()
        self.declared = config.load_declarations(self.config)
        self.started = now()
        self.id = self.started.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)
        # Databases: compat_<time>_<random>_<cell slug>, lowercase, as every engine accepts.
        self.prefix = f"compat_{self.started.strftime('%Y%m%d%H%M%S')}_{self.id[-4:]}_"
        self.out = Path(args.out) / self.id
        self.cells = self.select(args.cells)
        # Every selected cell is declared for the form before anything runs.
        missing = [c["id"] for c in self.cells
                   if (args.form, c["id"], config.TABLE_TYPES[0], config.OPERATIONS[0]) not in self.declared]
        if missing:
            raise config.ConfigError(f"declared.tsv declares nothing for the {args.form} form of: {', '.join(missing)}")
        token = Path(args.token_file).read_text().strip()
        read_token = Path(args.read_token_file).read_text().strip() if args.read_token_file else ""
        s3 = json.loads(Path(args.starrocks_s3_file).read_text()) if args.starrocks_s3_file else {}
        # The engines' root user: a random password for the run, never printed.
        engine_password = secrets.token_hex(16)
        host = urllib.parse.urlsplit(args.uri).hostname or ""
        self.sanitizer = Sanitizer(
            # Every StarRocks property but its region, endpoint and switches is a credential.
            secrets=[token, read_token, engine_password] + [str(v) for v in s3.values() if len(str(v)) >= 16
                                                            and not str(v).startswith(("http", "org.apache"))],
            values=dict({v: "<REDACTED>" for v in args.redact or []},
                        **{args.uri: "<CATALOG_URI>"}, **({host: "<CATALOG_HOST>"} if host else {})))
        self.settings = {"uri": args.uri, "warehouse": args.warehouse, "dataTokens": not args.no_data_tokens,
                         "s3Region": args.s3_region or ""}
        self.scratch = Path(tempfile.mkdtemp(prefix="colvenna-compat-"))
        os.chmod(self.scratch, 0o700)
        self.files = self.scratch / "files"
        self.secret = self.scratch / "secret"
        self.files.mkdir()
        self.secret.mkdir()
        # Inside the 0700 scratch directory: the containers' users read the mounted files, no other
        # user of this host can reach them.
        os.chmod(self.secret, 0o755)  # nosec B103
        # The CA as a copy every container's user can read, whatever the mode of the file given.
        self.ca = ""
        if args.ca_file:
            self.ca = str(self.scratch / "ca.crt")
            write_mounted(self.ca, Path(args.ca_file).read_text())
        write_mounted(self.secret / cells.TOKEN, token)
        if read_token:
            write_mounted(self.secret / cells.READ_TOKEN, read_token)
        write_mounted(self.secret / cells.ENGINE_PASSWORD, engine_password)
        if s3:
            write_mounted(self.secret / cells.S3_PROPERTIES, json.dumps(s3))
        self.reader_token = cells.READ_TOKEN if read_token else cells.TOKEN
        # Whose requests the access log is read for: a cell's principal if the run knows it, else
        # anyone's (a read token whose principal is not given).
        self.writer_principals = [args.principal] if args.principal else None
        if read_token:
            self.reader_principals = [args.read_principal] if args.read_principal else None
        else:
            self.reader_principals = self.writer_principals
        self.fixture_writes = {}
        self.cleanup = None
        # The matrix's own code as the run starts: what runs is what is recorded.
        self.commit, self.dirty = git_head()

    def select(self, wanted):
        every = [c["id"] for c in self.config["cells"]]
        ids = every if wanted in (None, "", "all") else [w.strip() for w in wanted.split(",")]
        unknown = [i for i in ids if i not in every]
        if unknown:
            raise config.ConfigError(f"--cells: no such cell: {', '.join(unknown)}; the cells: {', '.join(every)}")
        chosen = [config.cell_by_id(self.config, i) for i in every if i in ids]
        return chosen

    def backend(self):
        a = self.args
        ca = self.ca
        if a.backend == "docker":
            return Docker(self.config, self.id, self.files, self.secret, ca, Path(a.jar_cache).expanduser(),
                          a.docker_network, volumes=a.docker_volume or [], user=a.docker_user,
                          add_hosts=a.docker_add_host or [])
        # A namespace of the run's own unless one is named: two runs never share one.
        namespace = a.namespace or f"colvenna-compat-{self.id[-4:]}"
        return Kubernetes(self.config, self.files, self.secret, ca, namespace, a.client_label, a.kubeconfig, a.keep,
                          placement=placement(getattr(a, "node_selector", None), getattr(a, "toleration", None)))

    # ---------------------------------------------------------------------------------------------

    def execute(self):
        """Runs the cells; whatever happens, the scratch directory (it holds the tokens) goes."""
        try:
            return self.run_cells()
        finally:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def run_cells(self):
        writers = [c for c in self.cells if c["kind"] in config.WRITERS]
        readers = [c for c in self.cells if c["kind"] not in config.WRITERS]
        fixture_cell = config.cell_by_id(self.config, self.args.fixture_writer or self.config["fixtureWriter"])
        if fixture_cell["kind"] not in config.WRITERS:
            raise config.ConfigError(f"--fixture-writer: {fixture_cell['id']} is not a writer cell")
        jars = build_flows(self.config, self.log)
        for name, path in jars.items():
            shutil.copy(path, self.files / name)
        for path in ENGINE.iterdir():
            if path.is_file():
                shutil.copy(path, self.files / path.name)
        # Readable by every container's user, whatever this host's umask (inside the 0700 scratch).
        os.chmod(self.files, 0o755)  # nosec B103
        for path in self.files.iterdir():
            os.chmod(path, 0o644)
        self.out.mkdir(parents=True)
        (self.out / "cells").mkdir()
        (self.out / "raw").mkdir()
        backend = self.backend()
        server_log = ServerLog(self.args.server_log, self.args.kubeconfig, self.args.server_log_delay)
        self.log(f"run {self.id}: {len(self.cells)} cells on {backend.kind}, databases {self.prefix}*")
        outcomes, extra = {}, {"engineIdentity": backend.identity}
        finished = False
        try:
            backend.prepare()
            # Readers first: StarRocks's own credentials may be short-lived (an hour, for a role's).
            if readers:
                fixture, probe = self.fixture(backend, fixture_cell)
                extra["fixture"] = {"writer": fixture_cell["id"], "database": self.prefix + "fixture",
                                    "tables": [t["table"] for t in fixture], "writes": self.fixture_writes,
                                    "ownIdentityProbe": probe}
                for cell in readers:
                    outcomes[cell["id"]] = self.reader_cell(backend, server_log, cell, fixture, fixture_cell)
            for cell in writers:
                outcomes[cell["id"]] = self.writer_cell(backend, server_log, cell)
            finished = True
        finally:
            try:
                extra["cleanup"] = self.drop(backend, fixture_cell)
            finally:
                try:
                    backend.cleanup()
                finally:
                    # A run stopped half-way still says what it ran, and that it did not finish.
                    extra["completed"] = finished
                    self.finish(outcomes, extra, backend.kind)
        return outcomes

    def writer_cell(self, backend, server_log, cell):
        db = self.prefix + cell["slug"]
        self.log(f"{cell['id']}: the writer scenario in {db}")
        pod = cells.writer_pod(self.config, self.settings, self.id, cell, "cell", db)
        result = backend.run_pod(pod)
        requests, log_text = self.requests(server_log, result, self.writer_principals)
        verdicts = judge.writer(result.logs.get("flow", ""), result.problem, requests, db, self.prefix)
        return self.record(cell, result, verdicts, log_text, db)

    def fixture(self, backend, cell):
        db = self.prefix + "fixture"
        self.log(f"fixture: {cell['id']} writes {db} for the readers")
        result = backend.run_pod(cells.writer_pod(self.config, self.settings, self.id, cell, "fixture", db))
        text = result.logs.get("flow", "")
        self.keep_raw("fixture", result, None)
        fixture = judge.lines(text, "COMPAT-FIXTURE")
        results = judge.flow_results(text)
        probe = {t: v for (t, op), v in results.items() if op == "credentials"}
        # What each table holds, for the readers' verdicts: deletion vectors in pk-dv, say.
        self.fixture_writes = {t: v[1] for (t, op), v in results.items() if op == "write"}
        if not fixture:
            self.log("fixture: no table was written; the readers' checks will fail")
        return fixture, probe

    def reader_cell(self, backend, server_log, cell, fixture, prober):
        db = self.prefix + "fixture"
        self.log(f"{cell['id']}: reads {db}")
        plan = cells.reader_plan(self.settings, db, fixture)
        pod = cells.reader_pod(self.config, self.settings, self.id, cell, plan, self.reader_token, prober,
                               has_s3=(self.secret / cells.S3_PROPERTIES).exists())
        result = backend.run_pod(pod)
        requests, log_text = self.requests(server_log, result, self.reader_principals)
        main = next(c.name for c in pod.containers if c.main)
        # The probe of this pod's own identity, run in it before the engine started.
        probe = {t: v for (t, op), v in judge.flow_results(result.logs.get("probe", "")).items() if op == "credentials"}
        planned = {t["table"]: [c["name"] for c in t["checks"]] for t in fixture}
        verdicts = judge.reader(cell["kind"], result.logs.get(main, ""), result.problem, requests, db, self.prefix,
                                probe, own_credentials=self.args.starrocks_s3_description or "", planned=planned)
        judge.what_the_tables_hold(verdicts, self.fixture_writes)
        return self.record(cell, result, verdicts, log_text, db)

    def drop(self, backend, cell):
        """Drops every database of the run, whatever happened before, then lists them again: the
        clean-up passes only when none is left."""
        self.log(f"cleanup: dropping the databases {self.prefix}*")
        try:
            result = backend.run_pod(cells.writer_pod(self.config, self.settings, self.id, cell, "drop", self.prefix))
            self.keep_raw("cleanup", result, None)
            text = result.logs.get("flow", "")
            dropped = judge.lines(text, "COMPAT-DROPPED")
            if not dropped:
                fatal = judge.lines(text, "COMPAT-FATAL")
                return {"status": "FAIL", "detail": (fatal[0]["detail"] if fatal else result.problem)
                        or "the drop printed nothing"}
            remaining = dropped[0].get("remaining", [])
            return {"status": "FAIL" if remaining else "PASS", "dropped": dropped[0]["dropped"], "remaining": remaining}
        except Exception as e:  # noqa: BLE001 - recorded, and the run says so
            return {"status": "FAIL", "detail": str(e)}

    def requests(self, server_log, result, principals):
        """The access log's requests of the pod's time window (by the principals, if known), and
        the raw lines kept as evidence; (None, why) when there is no log to read."""
        if not server_log.available:
            return None, None
        # The pod's own span, with a few seconds for the clocks of this host and the catalog's.
        since = result.started - timedelta(seconds=3)
        until = result.finished + timedelta(seconds=5)
        try:
            text = server_log.read(since, until)
            parsed = accesslog.window(accesslog.parse(text), since, until, principals)
        except Exception as e:  # noqa: BLE001 - the cell's endpoint entries say so
            self.log(f"the catalog's access log could not be read: {e}")
            return None, f"# the catalog's access log could not be read: {e}\n"
        # Only the lines judged: no other principal's requests are kept.
        ids = {r.id for r in parsed}
        lines = [line for line in text.splitlines()
                 if (m := accesslog.LINE.match(line.strip())) and m.group("id") in ids]
        return parsed, "\n".join(lines) + "\n"

    def keep_raw(self, name, result, access_log):
        raw = self.out / "raw" / name
        raw.mkdir(parents=True, exist_ok=True)
        pod = {"pod": result.name, "exitCode": result.exit_code, "problem": result.problem,
               "started": result.started.isoformat(), "finished": result.finished.isoformat()}
        (raw / "pod.json").write_text(json.dumps(self.sanitizer.data(pod), indent=2) + "\n")
        for container, text in result.logs.items():
            (raw / f"{container}.log").write_text(self.sanitizer.text(text or ""))
        if access_log is not None:
            (raw / "catalog-access.log").write_text(self.sanitizer.text(access_log))

    def record(self, cell, result, verdicts, access_log, db):
        failed = judge.declare(verdicts, self.declared, self.args.form, cell["id"])
        info = (judge.lines(result.logs.get("flow", ""), "COMPAT-INFO")
                or judge.lines("\n".join(result.logs.values()), "COMPAT-READ-INFO"))
        bundled = [line.split(" ", 2)[2] for text in result.logs.values() for line in (text or "").splitlines()
                   if line.startswith("COMPAT-READER-INFO bundled ")]
        body = {
            "cell": cell["id"], "form": self.args.form, "engine": cell["engine"],
            "engineVersion": cell["engineVersion"], "paimon": cell["paimon"],
            "images": [self.config["images"][i] for i in cell["images"]], "jars": cell.get("jars", []),
            "database": db, "started": result.started.isoformat(), "finished": result.finished.isoformat(),
            "exitCode": result.exit_code, "problem": result.problem,
            "observed": dict(info[0], **({"bundledPaimonJars": bundled} if bundled else {})) if info else
            ({"bundledPaimonJars": bundled} if bundled else {}),
            "outcome": "pass" if not failed else "fail", "failedSupported": failed, "results": verdicts,
        }
        (self.out / "cells" / f"{cell['id']}.json").write_text(json.dumps(self.sanitizer.data(body), indent=2) + "\n")
        self.keep_raw(cell["id"], result, access_log)
        self.log(f"{cell['id']}: {body['outcome']}" + (f" ({', '.join(failed)})" if failed else ""))
        return body["outcome"]

    def finish(self, outcomes, extra, backend_kind):
        sha, dirty = self.commit, self.dirty
        meta = {
            "run": self.id, "form": self.args.form, "backend": backend_kind, "region": self.args.region or None,
            "catalog": self.args.catalog_version or None, "started": self.started.isoformat(),
            "finished": now().isoformat(), "matrixCommit": sha, "matrixTreeDirty": dirty,
            "cellsSha256": file_sha256(config.CELLS_FILE), "declaredSha256": file_sha256(config.DECLARED_FILE),
            "dataTokens": self.settings["dataTokens"], "databasePrefix": self.prefix,
            "serverLog": self.args.server_log.split(":")[0] if self.args.server_log else "none",
            "cells": [c["id"] for c in self.cells], **extra,
        }
        (self.out / "run.json").write_text(json.dumps(self.sanitizer.data(meta), indent=2) + "\n")
        summary = {"run": self.id, "form": self.args.form, "completed": extra.get("completed"),
                   "outcomes": outcomes, "cleanup": extra.get("cleanup", {}).get("status")}
        (self.out / "summary.json").write_text(json.dumps(self.sanitizer.data(summary), indent=2) + "\n")
        self.cleanup = extra.get("cleanup")
        self.log(f"results in {self.out}")


def run_matrix(args, log=print):
    r = Run(args, log)
    return r, r.execute()
