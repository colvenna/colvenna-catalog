#!/usr/bin/env python3
"""What the server's jars bundle, as a CycloneDX SBOM that vulnerability scanners can read (#18).

Apache Paimon's jars carry other projects' classes inside them: relocated (paimon-shade-*,
paimon-format, paimon-api) or in a plugin's own class path (paimon-s3's paimon-plugin-s3/). Most
of those copies keep no pom.properties, so an image scanner cannot see them, but each jar's
META-INF/NOTICE lists what it bundles as `- group:artifact:version` lines. This reads those lines
from every jar in a directory (the image's /opt/colvenna-catalog/lib, which `mvn package` makes as
target/lib) and writes one component per jar and coordinate, naming the jar it is bundled in.

usage: bundled_sbom.py <lib dir> <sbom.json> <components.tsv>
"""
import json
import os
import re
import sys
import urllib.parse
import zipfile

# A NOTICE line naming a bundled component: "- com.google.guava:guava:30.1.1-jre", or with the
# packaging between ("- group:artifact:jar:version"), possibly followed by a parenthesised remark.
COORDINATE = re.compile(
    r"^\s*-\s+([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):(?:[A-Za-z0-9_.\-]+:)?([A-Za-z0-9_.\-+]+)\s*(?:\(.*)?$")
NOTICE = re.compile(r"(^|/)META-INF/NOTICE(\.txt)?$")


def bundled(lib):
    """(jar, notice entry, group, artifact, version) for every coordinate the jars' notices list."""
    rows = []
    for name in sorted(os.listdir(lib)):
        if not name.endswith(".jar"):
            continue
        with zipfile.ZipFile(os.path.join(lib, name)) as jar:
            for entry in sorted(n for n in jar.namelist() if NOTICE.search(n)):
                for line in jar.read(entry).decode("utf-8", "replace").splitlines():
                    match = COORDINATE.match(line)
                    if match:
                        rows.append((name, entry, *match.groups()))
    return rows


def sbom(rows):
    """A CycloneDX 1.5 document with one component per jar and coordinate."""
    components, seen = [], set()
    for jar, _, group, artifact, version in rows:
        if (jar, group, artifact, version) in seen:
            continue
        seen.add((jar, group, artifact, version))
        purl = f"pkg:maven/{group}/{artifact}@{urllib.parse.quote(version, safe='.-_~')}"
        components.append({
            "type": "library", "group": group, "name": artifact, "version": version, "purl": purl,
            "bom-ref": f"{jar}|{purl}", "properties": [{"name": "bundled-in", "value": jar}],
        })
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {"type": "application", "name": "colvenna-catalog-bundled"}},
            "components": components}


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__.strip().splitlines()[-1])
    lib, out, table = argv[1:]
    rows = bundled(lib)
    document = sbom(rows)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=1)
    with open(table, "w", encoding="utf-8") as f:
        f.write("jar\tnotice\tgroup\tartifact\tversion\n")
        f.writelines("\t".join(row) + "\n" for row in rows)
    print(f"{len(document['components'])} bundled components in {len({r[0] for r in rows})} jars")


if __name__ == "__main__":
    main(sys.argv)
