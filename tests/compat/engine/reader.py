"""A reader cell's client, run in the engine's own image (Doris FE, StarRocks all-in-one) beside the
engine it drives: waits for the engine, creates the Paimon catalog on Colvenna Catalog, runs the
fixture's read checks and prints one COMPAT-READ line (JSON) per statement for the runner to judge.

Usage: reader.py doris|starrocks. The plan is the JSON in COMPAT_READER_PLAN (no secrets); the token
is in COMPAT_TOKEN_FILE; StarRocks's own S3 properties, if any, in COMPAT_S3_PROPERTIES_FILE. Only
the standard library, Python 3.8 or later: the images' own interpreter runs it.

COMPAT_READER_ROUNDS (default 1) runs the checks again in the same catalog, COMPAT_READER_ROUND_SECONDS
apart, each COMPAT-READ line then naming its round: UAT-CAT-03 reads across a data token's expiry.

The engine's root user has the run's random password (COMPAT_ENGINE_PASSWORD_FILE): Doris's from
its first start, StarRocks's as soon as its backend is up. Statements, which hold the token, go to
the mysql client on its standard input, never in its arguments."""

import json
import os
import subprocess
import sys
import time

MYSQL = ["mysql", "-h127.0.0.1", "-P9030", "-uroot", "--batch"]


ENV = dict(os.environ)


def sql(statement, names=False, timeout=600):
    """(exit status, standard output, standard error) of one statement."""
    args = MYSQL + ([] if names else ["--skip-column-names"])
    try:
        p = subprocess.run(args, input=statement + ";\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, universal_newlines=True, env=ENV)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def emit(kind, body):
    print(kind + " " + json.dumps(body, sort_keys=True), flush=True)


def wait(what, check, seconds=600):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            emit("COMPAT-READ-STEP", {"step": what, "ok": True})
            return True
        time.sleep(5)
    emit("COMPAT-READ-STEP", {"step": what, "ok": False})
    return False


def alive_backends():
    rc, out, _ = sql("SHOW BACKENDS", names=True)
    if rc != 0 or not out:
        return 0
    lines = out.splitlines()
    header = lines[0].split("\t")
    if "Alive" not in header:
        return 0
    i = header.index("Alive")
    return sum(1 for row in lines[1:] if row.split("\t")[i].strip().lower() == "true")


def start(engine, password):
    if engine == "doris":
        ENV["MYSQL_PWD"] = password
    # Metadata only: Doris plans even SELECT 1 on a backend.
    if not wait("frontend", lambda: sql("SHOW FRONTENDS")[0] == 0):
        return False
    if engine == "doris":
        # The BE container shares the pod's (or the container's) network: it is on loopback.
        rc, out, err = sql("ALTER SYSTEM ADD BACKEND '127.0.0.1:9050'")
        emit("COMPAT-READ-STEP", {"step": "add-backend", "ok": rc == 0 or "exist" in err.lower(), "error": err})
    elif not wait("backend registered", lambda: alive_backends() > 0, seconds=120):
        sql("ALTER SYSTEM ADD BACKEND '127.0.0.1:9050'")
    if not wait("backend alive", lambda: alive_backends() > 0):
        return False
    if engine == "starrocks":
        rc, _, err = sql("SET PASSWORD FOR 'root' = PASSWORD(" + quote(password, "'") + ")")
        emit("COMPAT-READ-STEP", {"step": "root password", "ok": rc == 0, "error": err})
        if rc != 0:
            return False
        ENV["MYSQL_PWD"] = password
        # A cold FE on a one-CPU pod plans a first external query in more than the default 3 s.
        rc, _, err = sql("SET GLOBAL new_planner_optimize_timeout = 60000")
        emit("COMPAT-READ-STEP", {"step": "planner timeout 60 s", "ok": rc == 0, "error": err})
    return True


def quote(value, mark):
    return mark + str(value).replace("\\", "\\\\").replace(mark, "\\" + mark) + mark


def catalog_statement(engine, plan, token):
    if engine == "doris":
        props = {
            "type": "paimon",
            "paimon.catalog.type": "rest",
            "uri": plan["uri"],
            "warehouse": plan["warehouse"],
            "paimon.rest.token.provider": "bear",
            "paimon.rest.token": token,
            "paimon.rest.data-token.enabled": "true" if plan["dataTokens"] else "false",
        }
        head, mark = "CREATE CATALOG compat", "'"
    else:
        props = {
            "type": "paimon",
            "paimon.catalog.type": "rest",
            "paimon.catalog.warehouse": plan["warehouse"],
            "paimon.option.uri": plan["uri"],
            "paimon.option.token.provider": "bear",
            "paimon.option.token": token,
        }
        extra = os.environ.get("COMPAT_S3_PROPERTIES_FILE", "")
        if extra and os.path.exists(extra):
            with open(extra) as f:
                props.update(json.load(f))
        head, mark = "CREATE EXTERNAL CATALOG compat", '"'
    body = ", ".join(quote(k, mark) + "=" + quote(v, mark) for k, v in props.items())
    return f"{head} PROPERTIES ({body})", sorted(props)


def read_checks(engine, plan, round_):
    """One round of the plan's checks; round_ is named in each line when there are several."""
    for table in plan["tables"]:
        if engine == "doris" and round_ in (None, 1):
            # Which reader takes the table's splits: Doris's native one, or the Paimon SDK's (JNI).
            # The plan (COMPAT_READER_PLAN) is the matrix's own: the fixture its writer flow printed.
            rc, out, err = sql(f"EXPLAIN VERBOSE SELECT * FROM compat.`{plan['database']}`.`{table['name']}`")  # nosec B608
            splits = [line.strip() for line in out.splitlines() if "paimonNativeReadSplits" in line]
            emit("COMPAT-READ-PLAN", {"table": table["table"], "rc": rc, "splits": splits[:1], "error": err})
        for check in table["checks"]:
            query = f"SELECT {check['expression']} FROM compat.`{plan['database']}`.`{table['name']}`"  # nosec B608
            if check["where"]:
                query += " WHERE " + check["where"]
            rc, out, err = sql(query)
            line = {"table": table["table"], "check": check["name"], "sql": query, "expected": check["expected"],
                    "rc": rc, "output": out, "error": err}
            if round_ is not None:
                line["round"] = round_
            emit("COMPAT-READ", line)


def main():
    engine = sys.argv[1]
    plan = json.loads(os.environ["COMPAT_READER_PLAN"])
    with open(os.environ["COMPAT_TOKEN_FILE"]) as f:
        token = f.read().strip()
    with open(os.environ["COMPAT_ENGINE_PASSWORD_FILE"]) as f:
        password = f.read().strip()
    if not start(engine, password):
        emit("COMPAT-READ-FATAL", {"detail": f"{engine} did not come up"})
        return 1
    version = "SELECT @@version_comment" if engine == "doris" else "SELECT current_version()"
    rc, out, err = sql(version)
    emit("COMPAT-READ-INFO", {"engineVersion": out, "error": err})
    statement, keys = catalog_statement(engine, plan, token)
    rc, out, err = sql(statement)
    # The statement holds the token: only the property names are printed.
    emit("COMPAT-READ", {"table": "-", "check": "create-catalog", "properties": keys, "rc": rc,
                         "output": out, "error": err})
    if rc != 0:
        return 0
    rc, out, err = sql(f"SHOW TABLES FROM compat.`{plan['database']}`")
    emit("COMPAT-READ", {"table": "-", "check": "list-tables", "rc": rc, "output": out, "error": err})
    rounds = int(os.environ.get("COMPAT_READER_ROUNDS") or 1)
    for round_ in range(1, rounds + 1):
        if round_ > 1:
            time.sleep(int(os.environ.get("COMPAT_READER_ROUND_SECONDS") or 0))
        read_checks(engine, plan, round_ if rounds > 1 else None)
    if engine == "doris":
        # The same reads through the Paimon SDK alone (Doris's JNI reader, Paimon 1.4.2): evidence
        # of which reader handles what, beside the verdict, which is the default configuration's.
        for table in plan["tables"]:
            for check in table["checks"]:
                query = f"SELECT {check['expression']} FROM compat.`{plan['database']}`.`{table['name']}`"  # nosec B608
                if check["where"]:
                    query += " WHERE " + check["where"]
                rc, out, err = sql("SET force_jni_scanner = true;\n" + query)
                emit("COMPAT-READ-JNI", {"table": table["table"], "check": check["name"], "expected": check["expected"],
                                         "rc": rc, "output": out, "error": err})
    emit("COMPAT-READ-DONE", {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
