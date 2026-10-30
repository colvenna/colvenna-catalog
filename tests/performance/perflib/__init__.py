"""The performance acceptance's harness (tests/performance/README.md): the scenarios of
docs/performance/terms.md, their load, what each run records, and the judging of its targets.
It reuses the UAT harness's library (tests/uat/uatlib: the install, the test environment's cluster,
the admin API through a probe pod, SQL on the metadata database) and the compatibility matrix's
sanitizer."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO / "tests" / "uat", REPO / "tests" / "compat"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
