"""Each scenario of docs/performance/terms.md as one run: its fixtures (principals, grants, databases
and tables of the run's own, synthetic data only), its load, what it collects, its judged metrics,
and its clean-up, verified. `execute(scenario, args)` writes result.json and summary.md;
`leftovers(args)` removes what a killed run left."""

import json
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import collect, cold, judge, raw, report, stats, terms
from .context import Run, install_source, iso, now
from .load import Loads


def ms_now():
    return int(time.time() * 1000)


def dt(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


class Scenario:
    """The parts every scenario shares, around one run."""

    def __init__(self, run):
        self.run = run
        self.args = run.args
        self.scale = run.args.duration_scale
        self.loads = {}
        self.pods = {}  # target form -> [load pod names]
        self.results = {}  # form -> metrics
        self.load = {}  # form -> the load's parameters
        self.collected = {}
        self.extra = {}

    def seconds(self, s, floor=10):
        return terms.scaled(s, self.scale, floor)

    # --- fixtures ---------------------------------------------------------------------------------

    def owner(self, target):
        """The run's owner principal (CATALOG MANAGE: it makes and drops the run's databases), its
        tools pod, and the clean-up that drops every database the run made, verified."""
        if "owner" in target.principals:
            return
        target.principal("owner", [("CATALOG", "MANAGE", None)])
        self.run.on_cleanup(f"[{target.form}] the run's databases dropped, their files gone",
                            lambda t=target: self.drop(t))

    def loads_of(self, target):
        if target.form not in self.loads:
            self.loads[target.form] = Loads(target)
        return self.loads[target.form]

    def tools(self, target, keys=("owner",)):
        return self.loads_of(target).tools(list(keys))

    def seed(self, target, databases, tables, snapshot, threads=None):
        threads = threads or (16 if target.form == "eks" else 8)
        code, out = self.tools(target).run("seed", {"owner": "owner", "databases": databases, "tables": tables,
                                                    "snapshot": snapshot, "threads": threads, "rows": 10},
                                           timeout=3600)
        target.keep(f"seed-{databases[0]}.log", out)
        seeded = [f for f in raw.facts(out) if f.get("kind") == "seeded"]
        if code != 0 or not seeded or seeded[-1].get("failed"):
            raise RuntimeError(f"the fixtures were not made ({code}): {out[-800:]}")
        target.log(f"seeded {len(databases)} databases x {tables} tables (snapshot: {snapshot})")

    def drop(self, target):
        if not target.databases:
            return True, "no database made"
        if self.loads_of(target).tools_gone():
            target.log("the tools pod is gone; a new one drops the run's databases")
        code, out = self.tools(target).run("drop", {"owner": "owner", "databases": target.databases}, timeout=3600)
        target.keep("drop.log", out)
        left = [f for f in raw.facts(out) if f.get("kind") == "left"]
        problems = []
        if code != 0 or not left or left[-1].get("databases"):
            problems.append(f"databases left: {left[-1].get('databases') if left else 'unknown'} ({code})")
        files = self.files_left(target)
        if files:
            problems.append(f"objects left under {len(files)} databases' prefixes: {sorted(files)[:5]}")
        return not problems, "; ".join(problems) or (
            f"{len(target.databases)} databases dropped and listed as gone; no object left under their prefixes")

    def files_left(self, target):
        """{database: objects} of the run's databases' prefixes in the warehouse. Every object
        counts, the zero-byte `<database>.db/` marker S3A writes for an empty directory too: the
        pilot (#20) found one left after each drop, which the server removes since #13; how many
        such markers there were is reported apart as well (`directoryMarkersLeft`). The serverless
        form deletes a dropped table's files later (its purge function, every 15 minutes), so the
        purge function is run first, as UAT-CAT-01 does."""
        from uatlib import purges

        inst = target.install
        if target.form == "serverless":
            bucket, prefix = inst.output("Serverless", "WarehouseRoot")[len("s3://"):].split("/", 1)
            for _ in range(3):
                result = purges.now(target)
                if not result or not result.get("error"):
                    break
                time.sleep(30)
        else:
            bucket, prefix = inst.bucket, inst.prefix
        left, markers = {}, 0
        for db in target.databases:
            out = target.aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", f"{prefix}/{db}.db/",
                             "--max-items", "100") or {}
            for o in out.get("Contents", []):
                if o["Key"].endswith("/") and not o.get("Size"):
                    markers += 1
                left[db] = left.get(db, 0) + 1
        self.extra.setdefault("directoryMarkersLeft", {})[target.form] = markers
        return left

    def keep_out(self, target, pod, name):
        """Copies the pod's output into the evidence (sanitized), returns {file: text}."""
        dest = target.scratch / f"out-{target.form}-{name}"
        pod.fetch(dest)
        out = {}
        for path in sorted(dest.iterdir()):
            text = path.read_text(encoding="utf-8", errors="replace")
            out[path.name] = text
            target.keep(f"{name}/{path.name}", text)
        return out

    def run_job(self, target, name, command, spec, keys):
        loads = self.loads_of(target)
        pod = loads.start(name, command, spec, keys)
        self.pods.setdefault(target.form, []).append({"pod": name, "node": pod.node(), "command": command})
        return pod

    def finish_job(self, target, pod, name, wait_s):
        code = pod.wait(wait_s)
        files = self.keep_out(target, pod, name)
        pod.delete()
        if code != 0:
            raise RuntimeError(f"{name} ended with {code}: {files.get('tool.log', '')[-1500:]}")
        return files

    # --- what every run records ---------------------------------------------------------------------

    def collect(self, target, since, until, poller, audit_principals=(), audit_action=None, gateway=False,
                reports=False):
        """The server's access log, the audit, the database's and functions' metrics of a window
        (epoch ms); kept, and returned parsed."""
        out = {}
        text = poller.stop() if target.form == "eks" else collect.server_log(target, since - 5000, until + 60000)
        target.keep("server-access.log", text)
        out["access"] = raw.access_lines(text)
        if target.form == "eks" and poller.errors:
            target.keep("server-access-errors.txt", "\n".join(poller.errors))
        if audit_principals:
            events = collect.audit(target, dt(since - 5000), dt(until + 60000), audit_principals, audit_action)
            target.keep("audit.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
            out["audit"] = events
        db = collect.database_metrics(target, dt(since), dt(until))
        target.keep("database-metrics.json", db)
        out["database"] = db
        if target.form == "serverless":
            fm = collect.function_metrics(target, dt(since), dt(until))
            target.keep("function-metrics.json", fm)
            out["functions"] = fm
            if gateway:
                g = collect.gateway_log(target, since - 5000, until + 60000)
                target.keep("gateway-access.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in g))
                out["gateway"] = g
            if reports:
                for part in ("Server", "Authorizer"):
                    events = collect.reports(target, part, since - 5000, until + 60000)
                    target.keep(f"{part.lower()}-reports.json", events)
                    out[f"{part.lower()}Envs"] = raw.environments(events)
        return out

    def database_rows(self, target, data):
        db = data.get("database") or {}
        return [stats.observed("metadata database", {
            "class": db.get("class"), "engine": db.get("engine"),
            "cpuMaxPercent": collect.peak(db.get("CPUUtilization")),
            "creditBalanceLowest": collect.lowest(db.get("CPUCreditBalance")),
            "connectionsMax": collect.peak(db.get("DatabaseConnections"))})]

    def function_rows(self, target, data):
        if target.form != "serverless":
            return []
        f = data.get("functions") or {}
        rows = [stats.observed("server function and API (CloudWatch)", {
            "invocations": collect.total(f.get("server.Invocations")),
            "concurrencyMax": collect.peak(f.get("server.ConcurrentExecutions")),
            "throttles": collect.total(f.get("server.Throttles")),
            "errors": collect.total(f.get("server.Errors")),
            "api4xx": collect.total(f.get("api.4XXError")), "api5xx": collect.total(f.get("api.5XXError"))})]
        if data.get("gateway") is not None:
            rows.append(stats.observed("gateway answers by status", judge.counts(data["gateway"], "status")))
        envs = data.get("serverEnvs")
        if envs is not None:
            cold_envs = [e for e in envs if e.get("initDurationMs") is not None or e.get("restoreDurationMs") is not None]
            rows.append(stats.observed("cold starts during the run", {"environments": len(cold_envs), "initDurationMs":
                                       stats.summary([e["initDurationMs"] for e in cold_envs
                                                      if e.get("initDurationMs") is not None])}))
        return rows

    def catalog_metrics(self, target):
        """The catalog's own counters (EKS: every pod's /metrics, from a namespace with the
        monitoring label); None in the serverless form, whose metrics are CloudWatch's."""
        if target.form != "eks":
            return None
        if target.monitor is None:
            from uatlib.probe import Probe

            ns = target.namespace("monitor", client=False, monitoring=True)
            target.monitor = Probe(target.kube, ns, name="monitor", evidence=target.evidence,
                                   sanitizer=target.sanitizer).start()
        return collect.catalog_metrics(target, target.monitor)

    def counters_row(self, target, before):
        """The catalog's own counters over the run (EKS), observed."""
        if before is None:
            return []
        after = self.catalog_metrics(target)
        target.keep("catalog-metrics.json", {"before": before, "after": after})
        return [stats.observed("catalog's own counters over the run (summed over the pods)", {
            "commits": collect.metrics_delta(before, after, "colvenna_catalog_commits_total"),
            "requests": collect.metrics_delta(before, after, "colvenna_catalog_requests_total")})]

    def reduced(self):
        """Whether the run's load is below the terms' (a trial of the harness, never an acceptance)."""
        a = self.args
        p4 = terms.LOAD["P4"]
        return self.scale != 1 or any(getattr(a, k, None) not in (None, p4[v]) for k, v in (
            ("rounds", "rounds"), ("idle_samples", "idle_samples"), ("idle_minutes", "idle_minutes")))

    def environment(self):
        run = self.run
        record = {
            "run": run.id, "scenario": run.scenario, "region": run.args.region, "account": "<ACCOUNT_ID>",
            "testEnvironment": run.args.test_env, "harnessCommit": run.harness_commit,
            "harnessTreeDirty": run.harness_dirty, "durationScale": self.scale, "reduced": self.reduced(),
            "client": terms.CLIENT, "loadPods": self.pods,
            "cluster": collect.cluster(run.targets[0]), "nodes": collect.nodes(run.kube), "installs": {},
        }
        for t in run.targets:
            try:
                record["installs"][t.form] = dict(t.describe(), **install_source(t), database=collect.database(t))
            except Exception as e:  # noqa: BLE001 - recorded: the record is incomplete, the run goes on
                record["installs"][t.form] = {"error": f"{type(e).__name__}: {e}"}
        for form, loads in self.loads.items():
            record.setdefault("loadTool", loads.describe())
        record["conditions"] = conditions(run.scenario, record)
        return record


# --- P1a, P1b, P1c ----------------------------------------------------------------------------------------

def p1(sc, variant, judged):
    t = sc.run.targets[0]
    p = terms.LOAD[variant]
    sc.owner(t)
    db = t.database(variant.lower())
    tables = ["t000"] if p["shared"] else [f"t{i:03d}" for i in range(p["writers"])]
    sc.seed(t, [db], len(tables), snapshot=False)
    t.principal("writer", [("DATABASE", "WRITE", db)])
    spec = {"key": "writer", "database": db, "tables": tables, "writers": p["writers"], "shared": p["shared"],
            "interval": terms.per_form(p["interval"], t.form), "rows": p["rows"], "warmup": sc.seconds(p["warmup"]),
            "duration": sc.seconds(terms.per_form(p["duration"], t.form), 60), "startDelay": 20}
    sc.load[t.form] = dict(spec, key="<writer's token>")
    before = sc.catalog_metrics(t)
    poller = collect.AccessLog(t).start()
    since = ms_now()
    pod = sc.run_job(t, "writers", "writers", spec, ["writer"])
    files = sc.finish_job(t, pod, "writers", spec["warmup"] + spec["duration"] + 900)
    until = ms_now()
    data = sc.collect(t, since, until, poller, audit_principals=[t.principals["writer"]], audit_action="COMMIT",
                      gateway=t.form == "serverless", reports=t.form == "serverless")
    counters = sc.counters_row(t, before)
    start = next(f["start"] for f in raw.facts(files.get("tool.log", "")) if f.get("kind") == "schedule")
    spec["start"] = start
    commits = raw.records(files.get("commits.jsonl", ""))
    rest = raw.rest_lines(files.get("rest.log", ""))
    window = judge.commit_window(spec)
    n_conflicts = judge.conflicts(data.get("audit", []), window)
    metrics = judged(t.form, spec, commits, rest, data["access"], conflicts=n_conflicts)
    metrics += counters + sc.database_rows(t, data) + sc.function_rows(t, data)
    sc.results[t.form] = metrics
    sc.extra["window"] = {"start": iso(dt(window[0])), "end": iso(dt(window[1]))}


# --- P2 ------------------------------------------------------------------------------------------------------

def p2_fixture(sc, t, label="p2"):
    p = terms.LOAD["P2"]
    dbs = [t.database(f"{label}{i:02d}") for i in range(p["databases"])]
    sc.seed(t, dbs, p["tables"], snapshot=True)
    return dbs


def p2(sc):
    t = sc.run.targets[0]
    p = terms.LOAD["P2"]
    sc.owner(t)
    dbs = p2_fixture(sc, t)
    t.principal("reader", [("CATALOG", "READ", None)])
    rates = p["steps"][t.form]
    step = sc.seconds(p["step"], 30)
    spec = {"key": "reader", "databases": dbs, "tables": p["tables"], "mix": p["mix"], "page": p["page"],
            "steps": ",".join(f"{r}:{step}" for r in rates), "warmup": sc.seconds(p["warmup"]), "threads": 256,
            "seed": 20, "drain": 240, "startDelay": 20}
    sc.load[t.form] = dict(spec, key="<reader's token>", step=step, databases=f"{len(dbs)} databases")
    before = sc.catalog_metrics(t)
    poller = collect.AccessLog(t).start()
    since = ms_now()
    pod = sc.run_job(t, "reads", "reads", spec, ["reader"])
    files = sc.finish_job(t, pod, "reads", spec["warmup"] + step * len(rates) + 900)
    until = ms_now()
    data = sc.collect(t, since, until, poller, gateway=t.form == "serverless", reports=t.form == "serverless")
    reads = raw.records(files.get("reads.jsonl", ""))
    counters = sc.counters_row(t, before)
    rest = raw.rest_lines(files.get("rest.log", ""))
    metrics = judge.p2(t.form, {"steps": rates, "step": step}, reads, rest)
    metrics.append(stats.observed("client request lines by kind", judge.counts(rest, "kind")))
    metrics.append(stats.observed("attempts by server status", judge.attempt_statuses(rest, data["access"])))
    metrics += counters + sc.database_rows(t, data) + sc.function_rows(t, data)
    sc.results[t.form] = metrics


# --- P3 ------------------------------------------------------------------------------------------------------

def p3(sc):
    t = sc.run.targets[0]
    p = terms.LOAD["P3"]
    sc.owner(t)
    miss_duration = sc.seconds(p["miss"]["duration"], 60)
    pairs = round(p["miss"]["rate"] * miss_duration)
    tables = -(-pairs // 2)  # two principals: each table gives two pairs
    per_db = 90
    miss_dbs = [t.database(f"p3m{i}") for i in range(-(-tables // per_db))]
    hit_db = t.database("p3h")
    sc.seed(t, miss_dbs, per_db, snapshot=False)
    sc.seed(t, [hit_db], p["hit"]["tables"], snapshot=False)
    t.principal("writer", [("DATABASE", "WRITE", db) for db in miss_dbs + [hit_db]])
    t.principal("reader", [("DATABASE", "READ", db) for db in miss_dbs + [hit_db]])
    hit_tables = [f"{hit_db}.t{i:03d}" for i in range(p["hit"]["tables"])]
    common = {"keys": ["writer", "reader"], "threads": 64, "drain": 240, "startDelay": 20}
    miss_spec = dict(common, **{"missShare": 1.0, "miss.databases": miss_dbs, "miss.tables": per_db,
                                "rate": p["miss"]["rate"], "warmup": 0, "duration": miss_duration, "seed": 31})
    hit_spec = dict(common, **{"missShare": 0.0, "hit.tables": hit_tables, "prime": True,
                               "rate": p["hit"]["rate"], "warmup": sc.seconds(p["hit"]["warmup"]),
                               "duration": sc.seconds(p["hit"]["duration"], 60), "seed": 32})
    sc.load[t.form] = {"P3a": dict(miss_spec, pairs=pairs, tables=tables), "P3b": hit_spec}
    before = sc.catalog_metrics(t)
    poller = collect.AccessLog(t).start()
    since = ms_now()
    pod = sc.run_job(t, "misses", "tokens", miss_spec, ["writer", "reader"])
    miss_files = sc.finish_job(t, pod, "misses", miss_duration + 900)
    pod = sc.run_job(t, "hits", "tokens", hit_spec, ["writer", "reader"])
    hit_files = sc.finish_job(t, pod, "hits", hit_spec["warmup"] + hit_spec["duration"] + 900)
    until = ms_now()
    data = sc.collect(t, since, until, poller, audit_principals=[t.principals["writer"], t.principals["reader"]],
                      audit_action="ISSUE_DATA_TOKEN", gateway=t.form == "serverless", reports=t.form == "serverless")
    by_id = {e.get("requestId"): e for e in data.get("audit", [])}
    groups = []
    for files in (miss_files, hit_files):
        rest = raw.rest_lines(files.get("rest.log", ""))
        index = raw.ThreadIndex(rest)
        ops = raw.join(raw.records(files.get("tokens.jsonl", "")), index, {"token"}, data["access"])
        groups.append(judge.classify(ops, by_id))
    metrics = judge.p3(t.form, miss_spec, groups[0], hit_spec, groups[1])
    metrics += sc.counters_row(t, before) + sc.database_rows(t, data) + sc.function_rows(t, data)
    sc.results[t.form] = metrics


# --- P4a, P4b --------------------------------------------------------------------------------------------------

def p4(sc, variant):
    t = sc.run.targets[0]
    if t.form != "serverless":
        raise RuntimeError(f"{variant} is of the serverless form only")
    p = terms.LOAD["P4"]
    rounds, clients = sc.args.rounds or p["rounds"], p["clients"]
    idle_samples = p["idle_samples"] if sc.args.idle_samples is None else sc.args.idle_samples
    idle_minutes = p["idle_minutes"] if sc.args.idle_minutes is None else sc.args.idle_minutes
    sc.load[t.form] = {"rounds": rounds, "clients": clients, "idleSamples": idle_samples, "idleMinutes": idle_minutes,
                       "requests": "GET /v1/config, then getTable", "variant": variant}
    if variant == "P4b":
        snapstart_on(sc, t)
    sc.owner(t)
    db = t.database("p4")
    # Every token the rounds use is in the tools pod's Secret, made with it: the reader first, its
    # grant once the database is there.
    t.principal("reader", [], ttl="PT2H")
    keys = []
    for r in range(rounds):
        for i in range(clients):
            key = f"c{r}x{i}"
            t.admin.issue(t.principals["reader"], ttl="PT3H", key=key)
            keys.append(key)
    idle_keys = []
    for n in range(idle_samples):
        key = f"idle{n}"
        t.admin.issue(t.principals["reader"], ttl=f"PT{idle_minutes * (idle_samples + 1) // 60 + 3}H", key=key)
        idle_keys.append(key)
    tools = sc.tools(t, ["owner"] + keys + idle_keys)
    sc.seed(t, [db], 1, snapshot=True)
    t.admin.grant(t.principals["reader"], "DATABASE", "READ", database=db)
    before = cold.state(t)
    t.keep("functions-before.json", before)
    made = []
    sc.run.on_cleanup("[serverless] the alias, the functions' configuration and versions as before",
                      lambda: cold.restore(t, before, made))
    since = ms_now()
    plan = []
    for r in range(rounds):
        info = cold.new_environments(t, before, f"{sc.run.id}-{r}")
        made.append(info["version"])
        spec = {"keys": keys[r * clients:(r + 1) * clients], "database": db, "table": "t000", "round": r,
                "prefix": f"r{r}-", "startDelay": 3}
        code, out = tools.run("cold", spec, timeout=900)
        t.keep(f"round-{r}.log", out)
        at = next((f["start"] for f in raw.facts(out) if f.get("kind") == "schedule"), ms_now())
        plan.append(dict(info, round=r, requestAt=at, exit=code))
        t.log(f"round {r}: version {info['version']} ({info['seconds']} s), exit {code}")
        # The next round's configuration change must not land while this round's environments start.
        time.sleep(20)
    until = ms_now()
    ok, detail = cold.restore(t, before, made)
    t.log(f"restored: {detail}")
    idle = []
    for n in range(idle_samples):
        t.log(f"idle sample {n + 1} of {idle_samples}: waiting {idle_minutes} minutes without traffic")
        time.sleep(idle_minutes * 60)
        spec = {"keys": [idle_keys[n]], "database": db, "table": "t000", "round": f"idle{n}", "prefix": f"idle{n}-",
                "startDelay": 2}
        code, out = tools.run("cold", spec, timeout=900)
        t.keep(f"idle-{n}.log", out)
        idle.append({"sample": n, "exit": code, "at": next((f["start"] for f in raw.facts(out)
                                                             if f.get("kind") == "schedule"), ms_now())})
    end = ms_now()
    files = sc.keep_out(t, tools, "cold")
    rows = [r for name, text in files.items() if name.startswith("r") and name.endswith("cold.jsonl")
            for r in raw.records(text)]
    rest = raw.rest_lines(files.get("rest.log", ""))
    time.sleep(collect.LOG_ARRIVAL)
    server = collect.reports(t, "Server", since - 5000, ms_now())
    authorizer = collect.reports(t, "Authorizer", since - 5000, ms_now())
    t.keep("server-reports.json", server)
    t.keep("authorizer-reports.json", authorizer)
    gateway = collect.gateway_log(t, since - 5000, ms_now())
    t.keep("gateway-access.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in gateway))
    server_envs, auth_envs = raw.environments(server), raw.environments(authorizer)
    idle_rows = []
    for s in idle:
        recs = [r for name, text in files.items() if name == f"idle{s['sample']}-cold.jsonl" for r in raw.records(text)]
        env = [e for e in server_envs if s["at"] - 2000 <= (e.get("firstInvokeAt") or 0) <= s["at"] + 60000]
        idle_rows.append({"sample": s["sample"], "cold": bool(env and (env[0].get("initDurationMs") is not None
                                                                       or env[0].get("restoreDurationMs") is not None)),
                          "initDurationMs": env[0].get("initDurationMs") if env else None,
                          "restoreDurationMs": env[0].get("restoreDurationMs") if env else None,
                          "configMs": judge.ms(recs[0]["configUs"]) if recs else None,
                          "ok": bool(recs and recs[0].get("configOk") and recs[0].get("getTableOk"))})
    db_data = collect.database_metrics(t, dt(since), dt(until))
    t.keep("database-metrics.json", db_data)
    metrics = judge.p4(variant, plan, server_envs, auth_envs, gateway, rows, rest, idle=idle_rows if idle else None)
    metrics.append(stats.observed("rounds", plan))
    metrics.append(stats.observed("restored after the rounds", {"ok": ok, "detail": detail}))
    metrics += sc.database_rows(t, {"database": db_data})
    sc.results[t.form] = metrics


def snapstart_on(sc, t):
    """P4b: S1 again with COLVENNA_SNAPSTART=on (install.sh, the installer's checkout and source the
    arguments name), and, registered first, the clean-up that sets it back to off and checks it."""
    if not sc.args.installer_checkout or not (sc.args.from_source or sc.args.release):
        raise RuntimeError("P4b runs install.sh twice: give --installer-checkout and --from-source or --release")
    from pathlib import Path

    checkout = Path(sc.args.installer_checkout)
    source = ["--from-source"] if sc.args.from_source else ["--release", sc.args.release]

    def off():
        c = cold.install(t, checkout, source, "off")
        live = cold.snapstart_of_live(t)
        return c == 0 and live in (None, "None"), f"install.sh with COLVENNA_SNAPSTART=off: exit {c}; live version's " \
                                                  f"SnapStart {live}"

    sc.run.on_cleanup("[serverless] SnapStart off again (install.sh with the install's configuration)", off)
    t.log("install.sh with COLVENNA_SNAPSTART=on")
    code = cold.install(t, checkout, source, "on")
    if code != 0 or cold.snapstart_of_live(t) != "PublishedVersions":
        raise RuntimeError(f"install.sh with COLVENNA_SNAPSTART=on: exit {code}, live version's SnapStart "
                           f"{cold.snapstart_of_live(t)}")


# --- P5 ------------------------------------------------------------------------------------------------------

def p5(sc):
    forms = {t.form for t in sc.run.targets}
    if forms != {"eks", "serverless"}:
        raise RuntimeError("P5 needs one install of each form: give --config twice")
    p = terms.LOAD["P5"]
    duration, warmup = sc.seconds(p["duration"], 60), sc.seconds(p["warmup"])
    specs, pollers, samplers = {}, {}, {}
    for t in sc.run.targets:
        sc.owner(t)
        dbs = p2_fixture(sc, t, "p5r")
        wdb = t.database("p5w")
        sc.seed(t, [wdb], p["writers"], snapshot=False)
        t.principal("writer", [("DATABASE", "WRITE", wdb)] + [("DATABASE", "WRITE", d) for d in dbs])
        t.principal("reader", [("CATALOG", "READ", None)])
        hit = [f"{dbs[0]}.t{i:03d}" for i in range(p["hit_tables"])]
        specs[t.form] = {
            "writers.key": "writer", "writers.database": wdb, "writers.tables": [f"t{i:03d}" for i in range(p["writers"])],
            "writers.writers": p["writers"], "writers.interval": p["interval"], "writers.rows": p["rows"],
            "writers.warmup": warmup, "writers.duration": duration,
            "reads.key": "reader", "reads.databases": dbs, "reads.tables": terms.LOAD["P2"]["tables"],
            "reads.mix": terms.LOAD["P2"]["mix"], "reads.page": 100, "reads.steps": f"{p['read_rate']}:{duration}:window",
            "reads.warmup": warmup, "reads.threads": 128, "reads.seed": 50, "reads.drain": 240,
            "tokens.keys": ["writer", "reader"], "tokens.hit.tables": hit, "tokens.prime": True,
            "tokens.miss.databases": dbs[1:], "tokens.miss.tables": terms.LOAD["P2"]["tables"],
            "tokens.missShare": p["miss_share"], "tokens.rate": p["token_rate"], "tokens.warmup": warmup,
            "tokens.duration": duration, "tokens.seed": 51, "tokens.threads": 32, "tokens.drain": 240,
        }
        sc.load[t.form] = {k: v for k, v in specs[t.form].items() if not k.endswith(".databases")}
    befores = {}
    for t in sc.run.targets:
        samplers[t.form] = ConnectionSampler(t).start()
        befores[t.form] = sc.catalog_metrics(t)
    # Both forms' loads start at the same instant, in the same window.
    start = ms_now() + 180_000
    pods = {}
    since = ms_now()
    for t in sc.run.targets:
        pollers[t.form] = collect.AccessLog(t).start()
        pods[t.form] = sc.run_job(t, "mixed", "mixed", dict(specs[t.form], start=start), ["writer", "reader"])
    if ms_now() > start - 15_000:
        raise RuntimeError("the load pods were not running 15 s before the common start: start them again")
    results = {}
    for t in sc.run.targets:
        results[t.form] = sc.finish_job(t, pods[t.form], "mixed", (start - ms_now()) / 1000 + warmup + duration + 900)
    until = ms_now()
    data = {}
    for t in sc.run.targets:
        data[t.form] = sc.collect(t, since, until, pollers[t.form],
                                  audit_principals=[t.principals["writer"], t.principals["reader"]],
                                  audit_action="ISSUE_DATA_TOKEN", gateway=t.form == "serverless",
                                  reports=t.form == "serverless")
        conns = samplers[t.form].stop()
        t.keep("connections.json", conns)
        files = results[t.form]
        rest = raw.rest_lines(files.get("rest.log", ""))
        index = raw.ThreadIndex(rest)
        reads = raw.join(raw.records(files.get("reads-reads.jsonl", "")), index,
                         set(judge.READ_OPS), data[t.form]["access"])
        by_id = {e.get("requestId"): e for e in data[t.form].get("audit", [])}
        tokens = judge.classify(raw.join(raw.records(files.get("tokens-tokens.jsonl", "")), index, {"token"},
                                         data[t.form]["access"]), by_id)
        commits = raw.records(files.get("writers-commits.jsonl", ""))
        spec = {"writers": {"start": start, "warmup": warmup, "duration": duration, "interval": p["interval"],
                            "writers": p["writers"]},
                "reads": {"rate": p["read_rate"], "duration": duration},
                "tokens": {"rate": p["token_rate"], "duration": duration}}
        cold_ids = cold_request_ids(t, data[t.form], start + warmup * 1000, start + (warmup + duration) * 1000)
        metrics = judge.p5(t.form, spec, commits, rest, reads, tokens, cold_ids)
        if t.form == "serverless":
            # terms.md P4: how many cold requests also waited on a lock or on S3. A commit takes the
            # commit lock and writes to S3; a credential request calls STS and Cognito, not S3.
            kinds = judge.counts([data[t.form]["access"].get(i, {"action": "unknown"}) for i in cold_ids], "action")
            metrics.append(stats.observed("P5 cold requests by action (COMMIT: the commit lock and S3)", kinds))
        metrics.append(stats.observed("database connections by user (peak of the samples)", peak_by_user(conns)))
        metrics += sc.counters_row(t, befores[t.form]) + sc.database_rows(t, data[t.form]) + \
            sc.function_rows(t, data[t.form])
        sc.results[t.form] = metrics
    sc.extra["ratios"] = judge.ratios(sc.results["eks"], sc.results["serverless"])
    sc.extra["window"] = {"start": iso(dt(start + warmup * 1000)), "end": iso(dt(start + (warmup + duration) * 1000))}


def cold_request_ids(t, data, lo, hi):
    """The serverless form's requests served first by an execution environment that started in the
    window: the first access line of each such log stream."""
    if t.form != "serverless":
        return set()
    envs = [e for e in data.get("serverEnvs", []) if e.get("initDurationMs") is not None
            and lo <= (e.get("firstEventAt") or 0) <= hi]
    streams = {e["stream"] for e in envs}
    if not streams:
        return set()
    events = collect.log_events(t, f"/aws/lambda/{collect.function_name(t, 'Server')}", lo - 60000, hi + 60000,
                                '"colvenna.access"')
    first = {}
    for e in sorted(events, key=lambda e: e["timestamp"]):
        if e["logStreamName"] in streams and e["logStreamName"] not in first:
            m = raw.ACCESS.search(e["message"])
            if m:
                first[e["logStreamName"]] = m.group(1)
    return set(first.values())


class ConnectionSampler:
    """The metadata database's sessions by user, every 30 s while the load runs (P5): psql in a pod
    (EKS), or the serverless test driver's sql action inside the catalog's VPC, deployed for the
    run if it is not there, and removed with it."""

    QUERY = ("SELECT usename, count(*) FROM pg_stat_activity WHERE datname = current_database() "
             "AND pid <> pg_backend_pid() GROUP BY usename ORDER BY usename;")

    def __init__(self, target, every=30):
        import threading

        from uatlib.sql import Sql

        self.target, self.every = target, every
        self.sql = Sql(target)
        self.samples, self.errors = [], []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        t = self.target
        if t.form == "serverless":
            p = t.aws.raw("lambda", "get-function-configuration", "--function-name", collect.driver_name(t),
                          check=False)
            had = p.returncode == 0
            t.driver.ensure()
            if not had:
                t.runner.on_cleanup("[serverless] the test driver removed", lambda: remove_driver(t))
        self._thread.start()
        return self

    def _loop(self):
        while True:
            try:
                rows = self.sql.query(self.QUERY)
                # A background process of the server has no user: "-".
                self.samples.append({"t": iso(now()), "byUser": {(r[0] or "-"): int(r[1]) for r in rows
                                                                 if len(r) >= 2}})
            except Exception as e:  # noqa: BLE001 - recorded; observed only
                self.errors.append(f"{type(e).__name__}: {e}")
            if self._stop.wait(self.every):
                return

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=300)
        return {"samples": self.samples, "errors": self.errors[-20:]}


def remove_driver(t):
    import subprocess

    from . import REPO

    p = subprocess.run([str(REPO / "tests" / "serverless" / "down.sh"), t.install.env, t.install.region],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=1800)
    t.keep("driver-down.log", p.stdout)
    gone = t.aws.raw("lambda", "get-function-configuration", "--function-name", collect.driver_name(t),
                     check=False).returncode != 0
    return p.returncode == 0 and gone, f"down.sh exit {p.returncode}; the driver function gone: {gone}"


def peak_by_user(conns):
    out = {}
    for s in conns.get("samples", []):
        for user, n in s.get("byUser", {}).items():
            out[user] = max(out.get(user, 0), n)
    return {"peak": out, "samples": len(conns.get("samples", []))}


# --- the terms' conditions -----------------------------------------------------------------------------

def conditions(scenario, record):
    """The default sizes terms.md section 2 measures, checked against the run's record: {condition:
    (ok, what was found)}. A run on other sizes is reported but is not the candidate's acceptance."""
    out = {}

    def check(name, ok, found):
        out[name] = {"ok": bool(ok), "found": found}

    for form, inst in (record.get("installs") or {}).items():
        if "error" in inst:
            check(f"{form}: recorded", False, inst["error"])
            continue
        check(f"{form}: per-table credentials (COLVENNA_DATA_TOKENS=aws-sts)", inst.get("dataTokens") == "aws-sts",
              inst.get("dataTokens"))
        db = inst.get("database") or {}
        check(f"{form}: metadata database db.t4g.small, single-AZ", db.get("class") == "db.t4g.small"
              and db.get("multiAz") is False, db)
        if form == "eks":
            res = [p.get("resources") for p in inst.get("pods", [])]
            want = {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}}
            check("eks: 2 replicas", inst.get("replicas") == 2, inst.get("replicas"))
            check("eks: requests 0.5 CPU and 1 GiB, limits 2 CPU and 2 GiB", res and all(r == want for r in res), res)
            check("eks: COLVENNA_JDBC_POOL_SIZE 16", str(inst.get("jdbcPoolSize")) == "16", inst.get("jdbcPoolSize"))
            catalog_nodes = [n for n in record.get("nodes", []) if n.get("pool") == "catalog"]
            check("eks: the catalogs' two t3.large nodes", len(catalog_nodes) == 2
                  and all(n.get("instanceType") == "t3.large" for n in catalog_nodes),
                  [n.get("instanceType") for n in catalog_nodes])
        else:
            server = inst.get("server") or {}
            snap = "PublishedVersions" if scenario == "P4b" else None
            check("serverless: the server function at 2048 MB, x86_64", server.get("memoryMb") == 2048
                  and server.get("architectures") == ["x86_64"], server)
            check("serverless: reserved concurrency 10", server.get("reservedConcurrency") == 10,
                  server.get("reservedConcurrency"))
            check(f"serverless: SnapStart {'on' if snap else 'off'}", (server.get("snapStart") or None) in (
                  (snap,) if snap else (None, "None")), server.get("snapStart"))
    return out


# --- one run -------------------------------------------------------------------------------------------------

RUNNERS = {"P1a": lambda sc: p1(sc, "P1a", judge.p1a), "P1b": lambda sc: p1(sc, "P1b", judge.p1b),
           "P1c": lambda sc: p1(sc, "P1c", judge.p1c), "P2": p2, "P3": p3,
           "P4a": lambda sc: p4(sc, "P4a"), "P4b": lambda sc: p4(sc, "P4b"), "P5": p5}


def execute(scenario, args):
    """Runs the scenario; returns the process's exit status (0 when its verdict is PASS, or OBSERVED for a
    scenario that is never judged). A signal (Ctrl-C, SIGTERM, SIGHUP) stops the scenario; its clean-up and result still run, then it is
    raised again. The clean-up runs whatever happens before it (it notes, and does not obey, a
    signal while it runs)."""
    run = Run(scenario, args)
    sc = Scenario(run)
    error, environment, stopped, cleanup = "", {}, None, None
    try:
        run.log(f"{scenario} run {run.id} on {', '.join(t.install.env + ' (' + t.form + ')' for t in run.targets)} "
                f"in {args.region}, evidence {run.evidence}")
        try:
            RUNNERS[scenario](sc)
        except Exception as e:  # noqa: BLE001 - recorded; the result says FAIL
            error = "".join(traceback.format_exception_only(type(e), e)).strip()
            run.log("the run stopped: " + error)
            run.keep("traceback.txt", traceback.format_exc())
        except BaseException as e:  # a signal: the clean-up below still runs
            stopped, error = e, f"stopped: {type(e).__name__}"
            run.log("the run was stopped")
        try:
            environment = sc.environment()
        except BaseException as e:  # noqa: BLE001 - the record is incomplete; the clean-up still runs
            environment = {"error": f"{type(e).__name__}: {e}"}
            stopped = stopped or (e if not isinstance(e, Exception) else None)
        run.keep("environment.json", environment)
    finally:
        cleanup = run.cleanup()
        run.log(f"clean-up: {cleanup['status']}")
    body = report.result(scenario, run, sc, environment, cleanup, error)
    run.keep("result.json", body)
    run.keep("summary.md", report.summary(body))
    run.log(f"{scenario}: {body['verdict']}" + (f" ({'; '.join(body['reasons'])})" if body["reasons"] else ""))
    run.close()
    if stopped is not None:
        raise stopped
    return 0 if body["verdict"] in (stats.PASS, stats.OBSERVED) else 1


# --- a run's leftovers ------------------------------------------------------------------------------------

def leftovers(args):
    """Removes what a run that was killed left (README.md, "Clean-up"), in each install the
    configurations name, and reads it back: its namespaces, its databases (`*_<run>`, dropped by a
    principal of this clean-up's own with MANAGE, which its own clean-up disables), their files, its
    principals' grants; its principals disabled. P4's functions are put back by hand (README.md)."""
    import urllib.parse

    from .context import RUN_LABEL

    run = Run("cleanup", args)
    sc = Scenario(run)
    old = args.leftover
    ok, results = True, {}
    try:
        for t in run.targets:
            try:
                t.principal("clean", [("CATALOG", "MANAGE", None)])
                code, out = sc.tools(t, ["clean"]).run("drop", {"owner": "clean", "suffix": old}, timeout=3600)
                t.keep("drop.log", out)
                found = next((f["databases"] for f in raw.facts(out) if f.get("kind") == "found"), [])
                left = next((f["databases"] for f in raw.facts(out) if f.get("kind") == "left"), None)
                t.databases = list(found)
                files = sc.files_left(t)
                principals = [p["name"] for p in t.admin.call("GET", "/principals", full=True)["body"]
                              .get("principals", []) if p["name"].endswith(f"-{old}")]
                for name in principals:
                    for g in t.admin.call("GET", f"/grants?principal={urllib.parse.quote(name)}")["body"].get("grants", []):
                        t.admin.delete_grant(g["id"])
                    t.admin.disable(name)
                results[t.form] = {"databases": found, "databasesLeft": left, "filesLeft": files,
                                   "principals": principals}
                ok &= code == 0 and not left and not files
            except Exception as e:  # noqa: BLE001 - recorded; the other install is still cleaned
                results[t.form] = {"error": f"{type(e).__name__}: {e}"}
                ok = False
        run.kube.kubectl("delete", "namespace", "-l", f"{RUN_LABEL}={old}", "--wait=true", "--timeout=10m",
                         check=False, timeout=700)
        ns = run.kube.kubectl("get", "namespaces", "-l", f"{RUN_LABEL}={old}", "-o", "name", check=False).stdout.split()
        results["namespacesLeft"] = ns
        ok &= not ns
        run.keep("leftovers.json", dict(results, run=old))
        run.log(f"leftovers of {old}: {results}")
    finally:
        cleanup = run.cleanup()
        run.log(f"clean-up of the clean-up: {cleanup['status']}")
        run.close()
    return 0 if ok and cleanup["status"] == "PASS" else 1
