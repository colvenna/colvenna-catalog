"""The ggshield version that ggshield-requirements.lock pins, and the version installed (#113)."""
import re
import subprocess
import sys
from pathlib import Path

LOCK = Path(__file__).resolve().parent / "ggshield-requirements.lock"


def pinned_version():
    return re.search(r"^ggshield==(\S+) ", LOCK.read_text(), re.M)[1]


def installed_version(binary):
    """The version of the ggshield at binary (a path, or a name on PATH), or "none"."""
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True) if binary else None
    except OSError:
        return "none"
    return (done.stdout.strip().rpartition(" ")[2] if done and done.returncode == 0 else "") or "none"


def require_pinned(binary, who, advice=""):
    """The pinned version, if binary is that version; otherwise exit, saying that who needs it."""
    want, have = pinned_version(), installed_version(binary)
    if have != want:
        sys.exit(f"{who} needs ggshield {want}, installed from scripts/security/ggshield-requirements.lock "
                 f"(found: {have}){advice}")
    return have
