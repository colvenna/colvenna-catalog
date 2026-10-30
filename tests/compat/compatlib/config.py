"""The matrix's committed configuration: cells.json (the cells, their images and jars) and
declared.tsv (what each cell, table type and operation is declared to be before any run)."""

import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CELLS_FILE = HERE / "cells.json"
DECLARED_FILE = HERE / "declared.tsv"

TABLE_TYPES = ("pk", "pk-dv", "append")
OPERATIONS = ("ddl", "write", "read", "credentials", "endpoints")
FORMS = ("eks", "serverless")
KINDS = ("flink", "spark", "doris", "starrocks")
WRITERS = ("flink", "spark")
DECLARATIONS = ("supported", "limitation")

PINNED_IMAGE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


class ConfigError(ValueError):
    """The configuration is wrong; the message lists every problem."""


def load_cells(path=CELLS_FILE):
    """cells.json, checked: every image pinned by digest, every jar by SHA-256, every reference
    resolvable, ids and database slugs unique."""
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    errors = []
    images = config.get("images", {})
    jars = config.get("jars", {})
    for name, ref in images.items():
        if not PINNED_IMAGE.match(ref):
            errors.append(f"image {name}: not <registry>/<repository>:<tag>@sha256:<digest>: {ref}")
    for name, jar in jars.items():
        if not name.endswith(".jar") or not jar.get("path", "").endswith("/" + name):
            errors.append(f"jar {name}: its path must end with /{name}")
        if not SHA256.match(jar.get("sha256", "")):
            errors.append(f"jar {name}: no SHA-256")
    for name in ("curl", "maven"):
        if name not in images:
            errors.append(f"images: {name} is required")
    ids, slugs = set(), set()
    for cell in config.get("cells", []):
        cid = cell.get("id", "")
        if not re.match(r"^[a-z0-9.-]+$", cid) or cid in ids:
            errors.append(f"cell {cid!r}: ids are unique, of [a-z0-9.-]")
        ids.add(cid)
        if not SLUG.match(cell.get("slug", "")) or cell.get("slug") in slugs:
            errors.append(f"cell {cid}: slug {cell.get('slug')!r} must be unique, [a-z][a-z0-9_]{{0,23}}")
        slugs.add(cell.get("slug"))
        if cell.get("kind") not in KINDS:
            errors.append(f"cell {cid}: kind must be one of {', '.join(KINDS)}")
        for image in cell.get("images", []):
            if image not in images:
                errors.append(f"cell {cid}: no image {image}")
        if not cell.get("images"):
            errors.append(f"cell {cid}: no images")
        for jar in cell.get("jars", []):
            if jar not in jars:
                errors.append(f"cell {cid}: no jar {jar}")
        if cell.get("kind") in WRITERS:
            if not cell.get("jars") or not cell.get("flow"):
                errors.append(f"cell {cid}: a writer needs its connector jars and its flow jar")
        for key in ("engine", "engineVersion", "paimon"):
            if not cell.get(key):
                errors.append(f"cell {cid}: {key} is required")
    fixture = config.get("fixtureWriter")
    writers = {c["id"] for c in config.get("cells", []) if c.get("kind") in WRITERS}
    if fixture not in writers:
        errors.append(f"fixtureWriter {fixture!r} is not a writer cell")
    if errors:
        raise ConfigError("\n".join(errors))
    return config


def cell_by_id(config, cid):
    for cell in config["cells"]:
        if cell["id"] == cid:
            return cell
    raise KeyError(cid)


def load_declarations(config, path=DECLARED_FILE):
    """declared.tsv as {(form, cell, table, op): (declared, reason)}, checked: one row for every
    form it names x every cell x every table type x every operation, nothing else, and a reason
    for every limitation."""
    errors = []
    declared = {}
    cells = {c["id"] for c in config["cells"]}
    with open(path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.reader(f, delimiter="\t") if r and not r[0].startswith("#")]
    header, rows = rows[0], rows[1:]
    if header != ["form", "cell", "table", "op", "declared", "reason"]:
        raise ConfigError(f"declared.tsv: header must be form, cell, table, op, declared, reason: {header}")
    for n, row in enumerate(rows, start=2):
        if len(row) == 5:
            row.append("")  # no reason: nothing after the declaration, not even the tab
        if len(row) != 6:
            errors.append(f"declared.tsv row {n}: six columns, not {len(row)}")
            continue
        form, cell, table, op, value, reason = row
        key = (form, cell, table, op)
        if form not in FORMS:
            errors.append(f"declared.tsv row {n}: form {form!r}")
        if cell not in cells:
            errors.append(f"declared.tsv row {n}: no cell {cell!r} in cells.json")
        if table not in TABLE_TYPES:
            errors.append(f"declared.tsv row {n}: table type {table!r}")
        if op not in OPERATIONS:
            errors.append(f"declared.tsv row {n}: operation {op!r}")
        if value not in DECLARATIONS:
            errors.append(f"declared.tsv row {n}: declared must be supported or limitation, not {value!r}")
        if value == "limitation" and not reason.strip():
            errors.append(f"declared.tsv row {n}: a limitation needs its reason")
        if key in declared:
            errors.append(f"declared.tsv row {n}: {key} declared twice")
        declared[key] = (value, reason.strip())
    for form in sorted({k[0] for k in declared}):
        for cell in sorted(cells):
            for table in TABLE_TYPES:
                for op in OPERATIONS:
                    if (form, cell, table, op) not in declared:
                        errors.append(f"declared.tsv: nothing declared for {form} {cell} {table} {op}")
    if errors:
        raise ConfigError("\n".join(errors))
    return declared
