"""A cell's containers: what each engine kind runs, with which images, users, resources and
environment. Writers (Flink, Spark) run their flow (tests/compat/flows) in a mode: cell (the
scenario in the cell's own database), fixture (the tables the readers read), drop (removes the
run's databases) or probe (whether the pod's own identity can read a database's tables). Readers
(Doris, StarRocks) run engine/reader.py beside the engine, after a probe of their own pod."""

import json
import re
import shlex

from .backends import Container, Pod

TOKEN = "token"
READ_TOKEN = "read-token"
ENGINE_PASSWORD = "engine-password"  # pragma: allowlist secret - the name of the file the engine's password is mounted as; the password is random for each run
S3_PROPERTIES = "starrocks-s3.json"


def pod_name(run_id, cell, mode):
    """A DNS-1123 name, unique to the run, the cell and the mode."""
    name = f"compat-{run_id[-4:]}-{cell['slug']}-{mode}".lower().replace("_", "-")
    return re.sub(r"[^a-z0-9-]", "-", name)[:63].strip("-")


def writer_env(settings, token=TOKEN, heap="2g"):
    env = {
        "COMPAT_URI": settings["uri"],
        "COMPAT_TOKEN_FILE": f"/secret/{token}",
        "COMPAT_WAREHOUSE": settings["warehouse"],
        "COMPAT_DATA_TOKENS": "true" if settings["dataTokens"] else "false",
        "COMPAT_HEAP": heap,
        "HOME": "/tmp",  # nosec B108 - the engine container's own /tmp
    }
    if settings.get("s3Region"):
        env["COMPAT_S3_REGION"] = settings["s3Region"]
    return env


def flow_container(config, settings, cell, mode, argument, name="flow", token=TOKEN, heap="2g", memory="3Gi",
                   main=True):
    image = config["images"][cell["images"][0]]
    env = writer_env(settings, token, heap)
    if cell["kind"] == "flink":
        command = ["sh", "/compat/flink.sh", mode, argument]
        user = "9999:9999"  # the image's flink user
    else:
        command = ["sh", "/compat/spark.sh", cell["flow"], mode, argument]
        user = "185:185"  # the image's spark user
        env["SPARK_USER"] = "spark"
    return Container(name, image, command, env, user=user, cpu="1", memory=memory, main=main)


def writer_pod(config, settings, run_id, cell, mode, argument):
    """The writer cell's flow in a mode (cell, fixture or drop) on its argument."""
    container = flow_container(config, settings, cell, mode, argument)
    return Pod(pod_name(run_id, cell, mode), [container], jars=list(cell["jars"]), secrets=[TOKEN], deadline=2400)


def reader_plan(settings, database, fixture):
    """What a reader checks: the fixture's tables and checks, as its writer printed them."""
    return {
        "uri": settings["uri"],
        "warehouse": settings["warehouse"],
        "database": database,
        "dataTokens": settings["dataTokens"],
        "tables": fixture,
    }


def reader_pod(config, settings, run_id, cell, plan, token, prober, has_s3):
    """The reader cell's engine and reader.py, after the prober (the fixture's writer cell) has
    probed, from this very pod, whether its own identity can read the fixture's tables."""
    env = {"COMPAT_READER_PLAN": json.dumps(plan, sort_keys=True), "COMPAT_TOKEN_FILE": f"/secret/{token}",
           "COMPAT_ENGINE_PASSWORD_FILE": f"/secret/{ENGINE_PASSWORD}"}
    secrets = [token, ENGINE_PASSWORD]
    probe = flow_container(config, settings, prober, "probe", plan["database"], name="probe", token=token,
                           heap="512m", memory="1Gi", main=False)
    # The probe's exit status is its output's to say; it must not stop the reader from running.
    probe.command = ["sh", "-c", shlex.join(probe.command) + " || echo COMPAT-PROBE-EXIT $?"]
    if cell["kind"] == "doris":
        fe, be = (config["images"][i] for i in cell["images"])
        containers = [
            Container("fe", fe, ["bash", "/compat/doris-fe.sh"], env, user="0:0", cpu="500m", memory="2Gi", main=True),
            Container("be", be, ["bash", "/compat/doris-be.sh"], {}, user="0:0", cpu="500m", memory="3Gi"),
        ]
    else:
        if has_s3:
            env["COMPAT_S3_PROPERTIES_FILE"] = f"/secret/{S3_PROPERTIES}"
            secrets.append(S3_PROPERTIES)
        image = config["images"][cell["images"][0]]
        containers = [Container("starrocks", image, ["/usr/bin/tini-static", "--", "bash", "/compat/starrocks.sh"],
                                env, user="0:0", cpu="1", memory="5Gi", main=True)]
    return Pod(pod_name(run_id, cell, "read"), containers, jars=list(prober["jars"]), secrets=secrets, init=[probe],
               deadline=1800)
