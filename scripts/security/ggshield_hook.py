#!/usr/bin/env python3
"""The ggshield pre-commit hooks (#113): `ggshield_hook.py pre-commit` scans the staged changes, and
`ggshield_hook.py pre-push` the commits about to be pushed (in a public repository, a push publishes them).
Either runs the ggshield on PATH, and only at the version that ggshield-requirements.lock pins
(CONTRIBUTING.md#secret-scanning).

usage: ggshield_hook.py {pre-commit,pre-push} [the hook's arguments]
"""
import os
import shutil
import sys

from ggshield_pin import require_pinned

MODES = ("pre-commit", "pre-push")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in MODES:
        sys.exit(f"usage: ggshield_hook.py {{{','.join(MODES)}}} [the hook's arguments]")
    binary = shutil.which("ggshield")
    require_pinned(binary, "the ggshield hook", ": put it on PATH (CONTRIBUTING.md#secret-scanning), or skip the "
                                                "hook with SKIP=ggshield,ggshield-push")
    os.execv(binary, [binary, "secret", "scan", mode, *sys.argv[2:]])


if __name__ == "__main__":
    main()
