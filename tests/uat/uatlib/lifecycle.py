"""What the upgrade, going-back and restore cases (UAT-DEP-06, 07, 10) share: a record of an install's
state (the schema version rows, every table's snapshots and contents, the principals, tokens and
grants of the case, the audit's latest event) and its comparison; the loops that run through an
upgrade or a rollback, and their judging; and the metadata database's snapshots and restore, as the
user manual (docs/manual/user.md, section 6) says."""

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .install import parse_env
from .sql import Sql

GAP = 60  # seconds: the longest a loop may go without a success
# Seconds before the loops' stop where their window ends: a loop's last try may be cut short by it.
SETTLE = 5


def reach_previous_release(ctx):
    """The probe reaches the previous release (#49): in the EKS form its private CA may have no keyUsage
    (v1.0.0-rc.1's has none, until the candidate's install.sh issues it again with its extensions), which
    the probe's Python checks strictly by default, so the strict X.509 checks are off for the case; the
    chain and the names are still verified. The serverless form's API has a public certificate."""
    if ctx.form == "eks":
        ctx.probe.strict = False


def stamp(t=None):
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()


# --- the record ------------------------------------------------------------------------------------

# A read of the state whose tool broke off (#64: the kubectl exec's stream lost, toolExit 1, the output cut short
# after some snapshot) is read again, this many times in all, REREAD_PAUSE seconds apart. The breaks seen were
# a host's connection to the cluster, minutes long at worst (rc.2: 03:19 to 03:36): three reads half a minute
# apart outlast a short one; a longer one leaves the record incomplete, which the case then says.
READS = 3
REREAD_PAUSE = 30


def finished(code, facts):
    """Whether the state tool's read ended as the tool ends: exit 0 and its done line (Tool.java)."""
    return code == 0 and any(f.get("kind") == "done" and f.get("tool") == "state" for f in facts)


def record(ctx, tools, token_key, db, tables, principals):
    """The install's state as the case sees it. The tables' part is the state tool's read, taken again while it
    did not finish (READS in all); a record none of whose reads finished says so (incomplete), and no judge
    compares it (#64)."""
    out = {"time": time.time()}
    out["schema"] = [tuple(r) for r in Sql(ctx).query("SELECT version, script FROM colvenna_schema_version ORDER BY version")]
    for n in range(1, READS + 1):
        code, facts = tools.run(token_key, "state", db, ",".join(tables), keep="state.log")
        if finished(code, facts) or n == READS:
            break
        time.sleep(REREAD_PAUSE)
    out["reads"] = n
    out["incomplete"] = None if finished(code, facts) else (
        f"the state tool's output ended without its done line (exit {code}), {n} reads")
    out["listing"] = next((f.get("tables") for f in facts if f.get("kind") == "listing"), None)
    out["tables"] = {f["table"]: {k: f.get(k) for k in ("exists", "latestSnapshot", "earliestSnapshot", "schemaId", "tags",
                                                         "branches", "rows", "checksum", "error")}
                     for f in facts if f.get("kind") == "state"}
    out["snapshots"] = {}
    for f in facts:
        if f.get("kind") == "snapshot":
            out["snapshots"].setdefault(f["table"], {})[f["id"]] = {k: f.get(k) for k in (
                "exists", "commitKind", "commitUser", "plannedRows", "rows", "checksum")}
    admin = ctx.admin
    listed = {p["name"]: p for p in admin.call("GET", "/principals")["body"].get("principals", [])}
    out["principals"] = {p: {k: listed.get(p, {}).get(k) for k in ("name", "admin", "disabledAt")} for p in principals}
    out["tokens"] = {p: sorted((t.get("id"), t.get("expiresAt"), t.get("revokedAt"))
                               for t in admin.call("GET", f"/principals/{p}/tokens")["body"].get("tokens", []))
                     for p in principals}
    out["grants"] = {p: sorted((g.get("id"), g.get("scope"), g.get("database"), g.get("object"), g.get("privilege"),
                                g.get("effect"), g.get("expiresAt")) for g in admin.grants(p)) for p in principals}
    latest = admin.audit(limit=1)
    out["auditLatest"] = latest[0]["id"] if latest else None
    out["auditLatestTime"] = latest[0]["time"] if latest else None
    out["toolExit"] = code
    ctx.keep(f"record-{int(out['time'])}.json", {k: v for k, v in out.items()})
    return out


def migration_forward(before, after, expected):
    """(ok, detail): the rows before all still there, unchanged; any added higher; as many as the
    candidate's migrations."""
    b, a = list(before["schema"]), list(after["schema"])
    kept = a[:len(b)] == b
    added = a[len(b):]
    higher = all(int(v) > int(b[-1][0]) for v, _ in added) if b else True
    count = int(a[-1][0]) == len(expected) if a else False
    return kept and higher and count, (f"before {[v for v, _ in b]}, after {[v for v, _ in a]}; the candidate's "
                                       f"migrations {expected}")


def same_snapshot(a, b):
    """Whether two records of one snapshot agree: what it is and the rows its plan covers, and the rows
    read and their checksum where both records read them (the earliest, the latest, every 25th)."""
    if a is None or b is None:
        return False
    keys = ("exists", "commitKind", "commitUser", "plannedRows")
    if any(a.get(k) != b.get(k) for k in keys):
        return False
    if a.get("checksum") is not None and b.get("checksum") is not None:
        return a.get("rows") == b.get("rows") and a.get("checksum") == b.get("checksum")
    return True


def why_incomplete(r):
    """Why a record is incomplete, or None: its reads did not finish (record), or, in a record kept before
    record() said so (UAT-DEP-07 loads UAT-DEP-06's from its evidence), its tool's exit was not 0."""
    if not r:
        return None
    if r.get("incomplete"):
        return r["incomplete"]
    return f"the state tool exited {r['toolExit']}" if r.get("toolExit") not in (0, None) else None


def incomplete(before, after):
    """["<which>: <why>"] of the two records that are incomplete, [] when both are whole."""
    return [f"{which}: {why_incomplete(r)}" for which, r in (("before", before), ("after", after)) if why_incomplete(r)]


def not_compared(before, after):
    """Why two records are not compared (one of them incomplete, #64), or None."""
    missing = incomplete(before, after)
    return f"not compared, a record is incomplete ({'; '.join(missing)}): nothing is taken for lost" if missing else None


def same_snapshots(a, b, table):
    """Whether two records hold the same snapshots of the table, each one as same_snapshot judges it; never
    when either record is incomplete (#64)."""
    if incomplete(a, b):
        return False
    sa, sb = a["snapshots"].get(table, {}), b["snapshots"].get(table, {})
    return bool(sa) and sa.keys() == sb.keys() and all(same_snapshot(sa[k], sb[k]) for k in sa)


# Paimon 2.0.0's snapshot.num-retained.min, its default (CoreOptions): expiry leaves at least this many.
PAIMON_NUM_RETAINED_MIN = 10


def expired_snapshot_ids(before, now):
    """Of one table's snapshots ({id: record}), the ids Paimon's own expiry took (#56): missing now, all
    before the earliest snapshot still there, and PAIMON_NUM_RETAINED_MIN at least still there. In Paimon
    2.0.0 every commit expires the table's snapshots older than snapshot.time-retained (1 hour by default),
    earliest first, and stops short of the latest PAIMON_NUM_RETAINED_MIN (TableCommitImpl after the commit,
    ExpireSnapshotsImpl). So a table the loops write to for longer than that loses its first snapshots; one
    missing after the earliest, or a prefix gone with fewer left, is no expiry. The records hold no
    snapshot's time, so the hour itself is not checked."""
    if not now:
        return []
    ids = [int(k) for k in now]
    if max(ids) - min(ids) + 1 < PAIMON_NUM_RETAINED_MIN:
        return []
    return sorted((k for k in before if k not in now and int(k) < min(ids)), key=int)


def data_unchanged(before, after, writer_table=None, writer_user="uat-loop-writer"):
    """(ok, detail): every snapshot from before reads the same; the only new ones are the writer's; of
    the writer's table, the earliest may have been expired by Paimon meanwhile (reported, not judged). An
    incomplete record is not compared: the check fails for want of a record, not for data lost (#64)."""
    reason = not_compared(before, after)
    if reason:
        return False, reason
    problems, gone = [], []
    for table, snaps in before["snapshots"].items():
        now = after["snapshots"].get(table, {})
        expired = expired_snapshot_ids(snaps, now) if table == writer_table else []
        gone += [f"{table} {k}" for k in expired]
        for sid, s in snaps.items():
            if sid not in expired and not same_snapshot(s, now.get(sid)):
                problems.append(f"{table} snapshot {sid}: {s} then {now.get(sid)}")
        for sid, s in now.items():
            if sid not in snaps and not (table == writer_table and s.get("commitUser") == writer_user):
                problems.append(f"{table} snapshot {sid} new, by {s.get('commitUser')}")
    expiry = f"; expired by Paimon meanwhile (the earliest of the writer's table): {len(gone)} {gone[:3]}" if gone else ""
    compared = sum(len(s) for s in before["snapshots"].values()) - len(gone)
    return not problems, f"{compared} snapshots compared; problems {problems[:5]}{expiry}"


def identities_unchanged(before, after, new_tokens=()):
    """(ok, detail): principals, tokens and grants as recorded (plus the tokens named new)."""
    problems = []
    if before["principals"] != after["principals"]:
        problems.append(f"principals {before['principals']} then {after['principals']}")
    for p, toks in before["tokens"].items():
        now = [t for t in after["tokens"].get(p, []) if t[0] not in new_tokens]
        if now != toks:
            problems.append(f"{p}'s tokens changed")
    if before["grants"] != after["grants"]:
        problems.append("grants changed")
    return not problems, "; ".join(problems) or "principals, tokens and grants as recorded"


# --- the loops ---------------------------------------------------------------------------------------

class Loops:
    """The writer, reader and credential loops of tests/uat/paimon (Loops), as job pods."""

    def __init__(self, ctx, tools, writer_key, reader_key, db, append_table, tables, tag):
        self.ctx, self.tag = ctx, tag
        self.jobs = {
            "writer": tools.job(f"loop-writer-{tag}", writer_key, "writer", db, append_table, "2000", "7200", memory="1536Mi", cpu="100m"),
            "reader": tools.job(f"loop-reader-{tag}", reader_key, "reader", db, ",".join(tables), "1000", "7200",
                                memory="1536Mi", cpu="100m"),
            "credentials": tools.job(f"loop-credentials-{tag}", reader_key, "credentials", db, ",".join(tables), "5000",
                                     "7200", memory="1536Mi", cpu="100m"),
        }
        self.started = self.stopped = None

    def start(self):
        if not all(j.wait_running() for j in self.jobs.values()):
            raise RuntimeError("the loops did not start")
        time.sleep(20)
        self.started = time.time()
        return self

    def stop(self):
        """Stops the loops; returns their facts. stopped: when the stop was asked, the end of the
        window the loops are judged in (a loop in the middle of a try ends some seconds later)."""
        self.stopped = time.time()
        for j in self.jobs.values():
            j.stop()
        for j in self.jobs.values():
            j.wait(300)
            j.keep(self.ctx)
        return {k: j.facts() for k, j in self.jobs.items()}


def judge_loops(facts, since, until):
    """{check: (ok, detail)} of the loops in [since, until]: writes, reads and credentials."""
    out = {}

    def window(kind):
        return [f for f in facts.get(kind, []) if since <= parse(f["time"]) <= until]

    def longest_gap(items):
        times = sorted(parse(f["time"]) for f in items if f.get("ok"))
        edges = [since] + times + [until]
        return max(b - a for a, b in zip(edges, edges[1:])) if times else until - since

    writes = window("writer")
    w = [f for f in writes if f.get("kind") == "write"]
    failed = [f for f in w if not f.get("ok")]
    # A commit that failed is committed again in the next round (write-resolved, committedNow 1): its
    # snapshot is not reported, so it leaves one gap in the ids of the others.
    again = sum(1 for f in writes if f.get("kind") == "write-resolved" and f.get("committedNow") == 1)
    ids = sorted(f["snapshot"] for f in w if f.get("ok") and f.get("snapshot") is not None)
    missing = ids[-1] - ids[0] + 1 - len(set(ids)) if ids else 0
    contiguous = bool(ids) and len(set(ids)) == len(ids) and missing <= again
    gap = longest_gap(w)
    out["writes-uninterrupted"] = (bool(w) and not failed and gap <= GAP and contiguous,
                                   f"{len(w)} commits, {len(failed)} failed ({[f.get('error') for f in failed][:2]}), "
                                   f"{again} of them committed again in the next round; longest gap {gap:.0f} s; "
                                   f"snapshot ids each once, contiguous but for those: {contiguous}")
    reads = [f for f in window("reader") if f.get("kind") == "read"]
    failed = [f for f in reads if not f.get("ok")]
    older, last = [], {}
    for f in sorted((f for f in reads if f.get("ok")), key=lambda f: f["time"]):
        if (f.get("snapshot") or 0) < last.get(f["table"], 0):
            older.append(f"{f['table']} {f.get('snapshot')} after {last[f['table']]}")
        last[f["table"]] = max(last.get(f["table"], 0), f.get("snapshot") or 0)
    gap = longest_gap(reads)
    out["reads-uninterrupted"] = (bool(reads) and not failed and not older and gap <= GAP,
                                  f"{len(reads)} reads, {len(failed)} failed ({[f.get('error') for f in failed][:2]}); "
                                  f"older than the one before: {older[:3]}; longest gap {gap:.0f} s")
    tokens = [f for f in window("credentials") if f.get("kind") == "token"]
    failed = [f for f in tokens if not f.get("ok")]
    gap = longest_gap(tokens)
    out["tokens-uninterrupted"] = (bool(tokens) and not failed and gap <= GAP,
                                   f"{len(tokens)} credential requests and reads with them, {len(failed)} failed "
                                   f"({[f.get('error') for f in failed][:2]}); longest gap {gap:.0f} s")
    return out


# --- the metadata database: snapshots and the restore ----------------------------------------------

def instance(ctx):
    """The install's RDS instance identifier: the storage stack's DBInstance (user manual 6.1), or the
    instance at the configured endpoint for a database the install did not make."""
    if ctx.install.get("COLVENNA_DB") == "existing":
        host = ctx.install.get("COLVENNA_DB_ENDPOINT")
        for db in ctx.aws("rds", "describe-db-instances")["DBInstances"]:
            if (db.get("Endpoint") or {}).get("Address") == host:
                return db["DBInstanceIdentifier"]
        raise RuntimeError("no RDS instance at the configured endpoint")
    return ctx.aws.text("cloudformation", "describe-stack-resources", "--stack-name", ctx.install.stack("Storage"),
                        "--query", "StackResources[?ResourceType=='AWS::RDS::DBInstance'].PhysicalResourceId")


def snapshot(ctx, name, tags):
    """A manual snapshot of the install's instance (user manual 6.1), tagged; waits until it is available."""
    inst = instance(ctx)
    ctx.aws("rds", "create-db-snapshot", "--db-instance-identifier", inst, "--db-snapshot-identifier", name,
            "--tags", *[f"Key={k},Value={v}" for k, v in tags.items()])
    ctx.aws.raw("rds", "wait", "db-snapshot-available", "--db-snapshot-identifier", name, timeout=3600)
    return inst, name


def delete_snapshot(ctx, name):
    p = ctx.aws.raw("rds", "delete-db-snapshot", "--db-snapshot-identifier", name, check=False)
    if p.returncode != 0 and "DBSnapshotNotFound" not in p.stderr:
        raise RuntimeError(f"snapshot {name} not deleted: {p.stderr[-300:]}")
    return f"snapshot {name} deleted"


def tagged_snapshots(ctx, key, value):
    """The manual snapshots of the install's instance tagged key=value, newest first."""
    inst = instance(ctx)
    snaps = ctx.aws("rds", "describe-db-snapshots", "--db-instance-identifier", inst, "--snapshot-type", "manual")["DBSnapshots"]
    out = []
    for s in snaps:
        tags = {t["Key"]: t["Value"] for t in s.get("TagList", [])}
        if tags.get(key) == value:
            out.append(s)
    return sorted(out, key=lambda s: s.get("SnapshotCreateTime", ""), reverse=True)


def restore(ctx, snapshot_name, new_instance):
    """The user manual's restore (6.2, step 2 and 3): a new instance from the snapshot with the running
    instance's settings; returns its endpoint."""
    inst = instance(ctx)
    d = ctx.aws("rds", "describe-db-instances", "--db-instance-identifier", inst)["DBInstances"][0]
    args = ["rds", "restore-db-instance-from-db-snapshot", "--db-instance-identifier", new_instance,
            "--db-snapshot-identifier", snapshot_name, "--db-instance-class", d["DBInstanceClass"],
            "--db-subnet-group-name", d["DBSubnetGroup"]["DBSubnetGroupName"],
            "--vpc-security-group-ids", *[g["VpcSecurityGroupId"] for g in d["VpcSecurityGroups"]],
            "--db-parameter-group-name", d["DBParameterGroups"][0]["DBParameterGroupName"], "--no-publicly-accessible",
            "--enable-iam-database-authentication", "--backup-retention-period", "7", "--deletion-protection",
            "--enable-cloudwatch-logs-exports", "postgresql",
            "--tags", "Key=Purpose,Value=uat-restore", f"Key=colvenna-uat/run,Value={ctx.run}"]
    ctx.aws(*args)
    ctx.aws.raw("rds", "wait", "db-instance-available", "--db-instance-identifier", new_instance, timeout=3600)
    endpoint = ctx.aws.text("rds", "describe-db-instances", "--db-instance-identifier", new_instance,
                            "--query", "DBInstances[0].Endpoint.Address")
    ctx.aws.raw("logs", "put-retention-policy", "--log-group-name", f"/aws/rds/instance/{new_instance}/postgresql",
                "--retention-in-days", "365", check=False)
    return endpoint


def delete_instance(ctx, name, final_snapshot=None):
    """Deletes an instance as the user manual says (protection off, then delete; a final snapshot when
    named), and waits until it is gone."""
    p = ctx.aws.raw("rds", "modify-db-instance", "--db-instance-identifier", name, "--no-deletion-protection",
                    "--apply-immediately", check=False)
    if p.returncode != 0 and "DBInstanceNotFound" in p.stderr:
        return f"instance {name} already gone"
    args = ["rds", "delete-db-instance", "--db-instance-identifier", name]
    args += ["--final-db-snapshot-identifier", final_snapshot] if final_snapshot else ["--skip-final-snapshot"]
    args += ["--delete-automated-backups"]
    ctx.aws(*args)
    ctx.aws.raw("rds", "wait", "db-instance-deleted", "--db-instance-identifier", name, timeout=3600)
    ctx.aws.raw("logs", "delete-log-group", "--log-group-name", f"/aws/rds/instance/{name}/postgresql", check=False)
    return f"instance {name} deleted"


# --- the install pointed at a restored instance ------------------------------------------------------

LOOP_WRITER = "uat-loop-writer"  # the commit user of Loops' writer


def write_config(ctx, path, cfg, header):
    """An install's configuration written (0600), a copy kept in the evidence."""
    text = f"# {header}\n" + "\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n"
    path.write_text(text)
    path.chmod(0o600)
    ctx.keep(path.name, text)
    return path


def restored_config(ctx, endpoint, original, path, bucket=True, known=None):
    """The install's configuration pointed at a restored instance (user manual 6.2, step 4), written to
    path: COLVENNA_DB=existing, its endpoint and port, the Storage stack's admin secret, and its bucket
    when the install made it; without COLVENNA_BUCKET when bucket is false (the configuration
    install.sh must refuse); known: only these keys, as an older release takes them."""
    cfg = parse_env(original.read_text())
    cfg.update(COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT=endpoint, COLVENNA_DB_PORT="5432",
               COLVENNA_DB_ADMIN_SECRET_ARN=ctx.install.output("Storage", "DbAdminSecretArn"))
    if bucket:
        cfg.setdefault("COLVENNA_BUCKET", ctx.install.output("Storage", "BucketName"))
    else:
        cfg.pop("COLVENNA_BUCKET", None)
    if known is not None:
        cfg = {k: v for k, v in cfg.items() if k in known}
    return write_config(ctx, path, cfg, "The install pointed at a restored database (user manual 6.2)")


def fresh_sql(ctx):
    """SQL on the instance the install runs on now: the EKS form's psql pods know the old endpoint
    (a pod's environment does not change), so they go; the serverless form's driver is deployed again
    for the new instance."""
    if ctx.form == "eks":
        ns = ctx.namespace("sql", client=False)
        ctx.kube.kubectl("delete", "pod", "psql-catalog", "psql-admin", "-n", ns, "--ignore-not-found", "--wait=true")
    else:
        ctx.driver.repoint()
    return Sql(ctx)


def helm_json(ctx, *args):
    p = subprocess.run(["helm", *args, "--namespace", ctx.install.namespace, "-o", "json"], stdout=subprocess.PIPE,
                       universal_newlines=True, env=ctx.kube.env, timeout=120, check=True)
    return json.loads(p.stdout or "null")


def load_record(path):
    """A record that record() kept, as record() returned it: snapshot ids as numbers, rows as tuples."""
    r = json.loads(Path(path).read_text())
    r["schema"] = [tuple(x) for x in r.get("schema") or []]
    r["snapshots"] = {t: {int(k): v for k, v in snaps.items()} for t, snaps in (r.get("snapshots") or {}).items()}
    for key in ("tokens", "grants"):
        r[key] = {p: [tuple(x) for x in v] for p, v in (r.get(key) or {}).items()}
    return r
