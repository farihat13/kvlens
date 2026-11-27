#!/usr/bin/env python3
"""A control binary can run only while the tree it was built in still exists: the binary resolves a store's schema
from that tree (found from the binary's own location; `kvevaluator schema-path` reports it as "root"). Refuse a control whose tree is gone, or whose LDBC
schema differs from this tree's, before any gate runs it (10-20: a removed build tree made 3 controls crash as VOID).

    python3 tests/gates/check_control_tree.py BINARY [BINARY ...]
Exit 0 when every binary's tree holds this tree's LDBC schema; 3 otherwise, naming the cause per binary.
"""
import hashlib
import json
import os
import subprocess
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

EXP = str(ROOT)
SCHEMA = os.path.relpath(schema_path("ldbc"), EXP)   # repo-relative: the binary resolves it against its root


def build_tree(binary):
    """the repo root the binary resolves schemas against (its `schema-path` answer), or None when it gives none"""
    try:
        out = subprocess.run([binary, "schema-path", "resolve", SCHEMA], capture_output=True, text=True, timeout=60)
        return os.path.normpath(json.loads(out.stdout)["root"]) if out.returncode == 0 else None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def verdict(binary):
    """(ok, message) for one binary"""
    root = build_tree(binary)
    if root is None:
        return False, f"{binary}: it reports no build tree (is it a kvevaluator build?)"
    theirs = os.path.join(root, SCHEMA)
    if not os.path.isdir(root):
        return False, (f"{binary}: its build tree {root} is gone; the binary resolves a store's schema there, so "
                       f"every check it runs fails before testing anything. Rebuild the controls "
                       f"(run/meta-build-controls.sh <dir>: they build in the build cache)")
    if not os.path.isfile(theirs):
        return False, f"{binary}: its build tree {root} has no {SCHEMA}"
    if sha(theirs) != sha(os.path.join(EXP, SCHEMA)):
        return False, f"{binary}: its build tree's {SCHEMA} differs from this tree's (the stores' schema hash would not match)"
    return True, f"{binary}: build tree {root} (LDBC schema = this tree's)"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    bad = 0
    for b in argv:
        ok, msg = verdict(b)
        bad += not ok
        print(("CONTROL TREE OK " if ok else "CONTROL TREE REFUSED ") + msg)
    return 3 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
