# Compatibility matrix

Which engines, at which versions and with which Paimon clients, work with Colvenna Catalog, as a test anyone can run against their own catalog ([1.0 design](../../docs/design/v1.0-design.md), section 7). The published page is [docs/compat/matrix.md](../../docs/compat/matrix.md).

A **cell** is an engine version and the Paimon client it runs ([`cells.json`](cells.json)), against a catalog in one deployment form. Every cell covers three table types (a primary-key table without and with deletion vectors, and an append table) and five operations:

| Operation | Passes when |
| --- | --- |
| `ddl` | the engine creates a database and the table in its own SQL, lists them, drops both and no longer lists them |
| `write` | each write commits: insert ids 1..100, write ids 91..110 again (an upsert on a primary-key table), and on primary-key tables delete ids 1..5; the table's snapshots, and the commits in the catalog's access log, are at least as many as the writes; the deletion-vector table also gets ids 111..120 with `write-only` set, left uncompacted in level 0 as between a streaming writer's compactions, and must hold deletion vectors (`$table_indexes`) and level-0 files (`$files`) afterwards, so that its readers meet both |
| `read` | row count, sum and point reads give exactly what the writes leave ([`Dataset`](flows/core/src/main/java/com/colvenna/catalog/compat/Dataset.java)); on the deletion-vector table, created with `deletion-vectors.merge-on-read`, that includes the uncompacted rows |
| `credentials` | the engine runs with `data-token.enabled=true` and no S3 keys of its own; a probe in the same pod or container (in the writer's own JVM, or beside a reader before it starts) finds the pod's or container's own identity refused the table's files, yet the engine wrote and read them; and, when the run reads the access log, it shows the catalog issued the engine the table's credentials |
| `endpoints` | no request of the engine in the access log went to an endpoint the catalog does not serve: no route, or a 501 other than the partition listing it declines by design |

Writers (Flink, Spark) run the whole scenario in a database of their own and drop it. Readers (Doris, StarRocks) only read: one writer (`fixtureWriter` in `cells.json`, or `--fixture-writer`) writes the same three tables for them first, and the run drops that database at the end. Readers run before writers, so that short-lived credentials of their own last. Whatever happens, the run ends by dropping every database whose name has its prefix (`compat_<time>_<random>_`).

Before a run, every cell is declared, down to table type and operation, in [`declared.tsv`](declared.tsv): `supported`, which must pass, or `limitation`, an upstream incompatibility with its reason (the design's rule: limitations of the engines do not block a release; the catalog's own defects are never limitations). The run still runs what it can of a limitation and reports what it saw.

## Running it

What you need:

- a catalog URI the engines can reach, and its CA certificate if a private CA signed the catalog's;
- a token whose principal holds `MANAGE` on the catalog, for the writers; optionally a second one with `READ`, for the readers;
- Python 3.8 or later; Maven with JDK 21, or Docker, to build the flows;
- where the engines run: Docker on the host (`--backend docker`), or `kubectl` on a cluster (`--backend kubernetes`) in which a namespace with the catalog's client label (`--client-label`) reaches the catalog;
- for the endpoint check, the catalog's access log (`--server-log`): `kubectl:<namespace>:<label selector>` for the EKS form, or `command:<command>` printing the log between `$COMPAT_SINCE` and `$COMPAT_UNTIL` (CloudWatch Logs, for the serverless form); without it the endpoint entries do not run;
- for the StarRocks cell, read-only S3 credentials of its own, as JSON of StarRocks's `aws.s3.*` properties (`--starrocks-s3-file`).

On a host with Docker, against any catalog its containers reach (Docker's bridge network by default):

```bash
tests/compat/matrix.py run --form eks --backend docker \
  --uri https://catalog.example.com:8181 --token-file ~/writer.token --read-token-file ~/reader.token \
  --ca-file ~/catalog-ca.crt --s3-region us-west-2 --server-log 'command:<prints the access log>'
```

To reach an EKS install's catalog from such a host, forward its Service to the bridge's gateway (`docker0`'s address) and give the containers a name its certificate holds: `kubectl -n colvenna-catalog port-forward --address <gateway> svc/colvenna-catalog 18444:8181`, then `--uri https://colvenna-catalog.colvenna-catalog.svc:18444 --docker-add-host colvenna-catalog.colvenna-catalog.svc:<gateway>`.

In the cluster of an EKS install (the pods reach the catalog's Service; the runner makes a namespace of its own, `colvenna-compat-<run>`, and deletes it afterwards):

```bash
tests/compat/matrix.py run --form eks --backend kubernetes \
  --uri https://colvenna-catalog.colvenna-catalog.svc:8181 --token-file ~/writer.token \
  --read-token-file ~/reader.token --ca-file ~/catalog-ca.crt --s3-region us-west-2 \
  --server-log kubectl:colvenna-catalog:app.kubernetes.io/name=colvenna-catalog
```

On a cluster whose clients run on nodes of their own, `--node-selector <key>=<value>` and `--toleration <key>=<value>:<effect>` place the engines' pods there. In this repository's test environment they are its stack's outputs `ClientNodeSelector` and `ClientNodeTaint` (the clients' nodes, [`tests/environment/README.md`](../environment/README.md)), so that the engines never take the catalogs' nodes' room.

`--cells` picks cells by id (default: all). The engines run from their official images, pinned by digest; the Paimon jars come from Maven Central, checked against their pinned SHA-256 (the host fetches them for Docker, an init container for Kubernetes). The engines get no AWS identity of their own: Docker containers get no AWS variables and have the instance metadata service disabled; pods run as their namespace's default service account, with no IRSA role and no service account token, so that the node's own role is all they could reach (and the probes show it refused). Nothing the engines open is reachable from elsewhere: Docker publishes no port (`--docker-network host` would expose Doris's and StarRocks's ports on the host's interfaces), the runner's namespace admits no ingress, and Doris's and StarRocks's `root` has a random password for the run. A pod mounts only the secret files it needs; tokens and passwords reach the engines as files, never as arguments.

A run stopped with Ctrl-C or a signal still drops its databases, removes its containers or namespace and writes what it ran (`completed: false`). If the runner itself is killed, remove what it left by hand: `docker rm -f $(docker ps -aq --filter label=colvenna-compat=<run id>)`, or `kubectl delete namespace colvenna-compat-<run>`, and drop the databases named `compat_<time>_<random>_*`.

The run exits 0 when every declared-supported entry of every selected cell passed and the clean-up left nothing, and 1 otherwise.

## Results

Each run writes `<out>/<run id>/` (default `.state/compat/`, which git ignores):

- `run.json`: when, where (form, backend, region), the matrix's commit and the digests of `cells.json` and `declared.tsv`, the fixture and the clean-up;
- `cells/<cell>.json`: the cell's verdicts, one per table type and operation, each `PASS`, `FAIL` or `NOT_RUN` with its declaration, its detail and its evidence (the engine's requests to the catalog by endpoint and status, commits, credentials issued), and what the engine reported of itself (engine and Paimon versions, the Paimon jar loaded, an engine's bundled Paimon);
- `raw/<cell>/`: each container's output and the access log of the cell's time window;
- `summary.json`: every cell's outcome, whether the run completed, and its clean-up (it passes only when no database of the run is left).

Nothing written keeps a token, account ID, ARN, bucket, IP address or the catalog's URI: they are replaced with placeholders. The page comes from one or more runs, each its own section:

```bash
tests/compat/matrix.py page docs/compat/runs/<run id>... --link-base runs --out docs/compat/matrix.md
```

When a rule of the judge changes after a run, `tests/compat/matrix.py rejudge <run dir>...` judges it again from its kept raw output, nothing run again; each cell then records the commit that judged it and what changed, and the page says so. `tests/compat/matrix.py check` validates `cells.json` and `declared.tsv`; `python3 -m unittest discover -s tests/compat` tests the runner and `mvn -B -f tests/compat/flows/pom.xml verify` builds and tests the flows, as CI does (`compat-test`).

## Adding a cell

Add it to `cells.json` (images by digest, jars by SHA-256; a writer's `flow` is one of the flows' jars, `runner.FLOW_JARS`) and declare its fifteen entries for each form in `declared.tsv`, before running it. A new engine family needs an adapter beside [`FlinkFlow`](flows/flink/src/main/java/com/colvenna/catalog/compat/flink/FlinkFlow.java) (a writer) or a script beside [`reader.py`](engine/reader.py) (a reader).
