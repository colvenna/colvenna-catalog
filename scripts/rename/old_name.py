#!/usr/bin/env python3
"""The project's old name is gone (#31): the product was Cove Catalog, and every technical identifier
began with cove (cove-catalog, CoveCatalog, COVE_, com.covelake, /cove/admin/v1 ...) until the rename.
This check fails on the old name anywhere in the repository but where exceptions.tsv allows it: the
naming history, the import's provenance, and this check's own definitions. It is ported from
Colvenna Lake's check (its deploy/scripts/rename/), with this repository's paths.

What it looks for, case not mattering:

- "cove" as a word or the start of one, in a file's text or its path: cove-catalog, CoveCatalog,
  covelake, COVE_ENV, and inside an identifier where a capital starts it (isCoveTable,
  CoveCatalogContractTest, coveResnapshot). Not inside a word (recover, discover), nor in the English
  words that start with it, their letters in the same case (cover, Covenant, COVERAGE, covet, covey).
  Only ASCII letters are letters here, so Chinese text right before it does not hide it.
- A hexadecimal literal (0x and 8 to 16 digits) whose bytes, either way round, spell it: a key such
  as 0x636f76656361744c ("covecatL") hides the name from a text search.

exceptions.tsv: one exception a line, <path glob> TAB <text> TAB <reason>. A hit is allowed in a file
the glob matches, on a line holding the text ("*": any line). Lines starting with "#" are comments.

Usage: old_name.py FILE...   (pre-commit passes the staged files)
       old_name.py --all     (every file git tracks)
"""
import fnmatch
import os
import re
import string
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
EXCEPTIONS = os.path.join(HERE, "exceptions.tsv")
CANDIDATE = re.compile(r"cove", re.I)
HEX = re.compile(r"0x([0-9a-f]{8,16})(?![0-9a-f])", re.I)
# The English words that start like the old name: cover, coven(ant), covet, covey.
ENGLISH_NEXT = "rnty"


def old_names(text):
    """The old name's occurrences in <text>, as the four letters found."""
    found = []
    for m in CANDIDATE.finditer(text):
        name, before = m.group(0), text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        # A word starts after anything but an ASCII letter, or at a capital after a small letter.
        starts = not before or before not in string.ascii_letters or (name[0] == "C" and before.islower())
        english = bool(after) and (
            (after in ENGLISH_NEXT and not name.isupper()) or (after in ENGLISH_NEXT.upper() and name.isupper()))
        if starts and not english:
            found.append(name)
    return found


def load_exceptions(path=EXCEPTIONS):
    """The exceptions, as (glob, text, reason) triples; a malformed line stops the check."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 3 or not all(f.strip() for f in fields):
                sys.exit(f"{path}:{number}: want <path glob> TAB <text> TAB <reason>")
            rows.append(tuple(fields))
    return rows


def hits(text):
    """The old name in a line of text: the words, then the hexadecimal literals that spell it."""
    found = old_names(text)
    for m in HEX.finditer(text):
        digits = m.group(1)
        if len(digits) % 2 == 0:
            spelled = bytes.fromhex(digits)
            if any(old_names(b.decode("latin-1")) for b in (spelled, spelled[::-1])):
                found.append(m.group(0))
    return found


def allowed(path, line, rules):
    return any(fnmatch.fnmatchcase(path, glob) and (text == "*" or text in line) for glob, text, _ in rules)


def check(path, rules, root=REPO):
    """The lines of <path> (relative to <root>) where the old name is not allowed, as messages."""
    problems = []
    # A path is judged as a line of its own: an exception for the file's text allows it too.
    if hits(path) and not allowed(path, path, rules):
        problems.append(f"{path}: the old name in the path")
    try:
        with open(os.path.join(root, path), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        return problems
    for number, line in enumerate(lines, 1):
        found = hits(line)
        if found and not allowed(path, line, rules):
            problems.append(f"{path}:{number}: {', '.join(found)}: {line.strip()[:160]}")
    return problems


def main(argv):
    if argv[1:] == ["--all"]:
        paths = subprocess.check_output(["git", "-C", REPO, "ls-files", "-z"], text=True).split("\0")
        paths = [p for p in paths if p]
    elif len(argv) > 1 and "--all" not in argv:
        paths = [os.path.relpath(os.path.abspath(p), REPO) for p in argv[1:]]
    else:
        print(__doc__.strip().rsplit("\n\n", 1)[-1], file=sys.stderr)
        return 2
    rules = load_exceptions()
    problems = [p for path in paths for p in check(path, rules)]
    if problems:
        print(f"The old name is in {len(problems)} places: rename it (#31), or, for history, add an exception"
              " to " + os.path.relpath(EXCEPTIONS, REPO) + " with its reason:")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
