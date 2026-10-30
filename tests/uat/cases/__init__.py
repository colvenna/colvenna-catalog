"""The cases this harness runs, by ID. Each module has ID, FORMS (the forms it runs in), checks(form,
args) (the concrete names of the checks it writes for the form: exactly the document's, templates
filled in) and run(ctx); OPTIONAL lists checks marked "only on failure", add_arguments(parser) the
case's own options."""

from . import (cat01, cat02, cat03, cat04, cat05, cat06, cat07, cat08, dep06, dep07, dep10, flt01, flt02, flt03,
               sec01, sec02, sec03, sec04)

CASES = {m.ID: m for m in (cat01, cat02, cat03, cat04, cat05, cat06, cat07, cat08, dep06, dep07, dep10, flt01, flt02,
                           flt03, sec01, sec02, sec03, sec04)}

# The deployment cases (#52's deploy slice).
from . import dep01, dep02, dep03, dep04, dep05, dep08, dep09, dep11  # noqa: E402

CASES.update({m.ID: m for m in (dep01, dep02, dep03, dep04, dep05, dep08, dep09, dep11)})
