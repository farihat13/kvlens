#!/usr/bin/env python3
"""The latency drivers' notes_into (run/measure-preflight.sh, which run/latency-cells.sh sources;
experiments/layout_choice/run-w16-mix-latency.sh)
put each note where the record's writer folds it (kvevaluator.stale.fold): a run's note on every record of the dir, a
store's note on that store's record only, and parallel calls on one capture neither mark nor move each other's notes.
Store-free: the function is cut from each driver and run in bash on a temp dir.

    python3 tests/unit/check_driver_notes.py
"""
import os
import re
import subprocess
import sys
import tempfile

from kvevaluator import stale
from kvevaluator.paths import ROOT


def function_of(driver):
    """the driver's notes_into, as its text"""
    m = re.search(r"^notes_into\(\) \{.*?^\}$", open(ROOT / driver).read(), re.S | re.M)
    if not m:
        raise SystemExit(f"{driver}: no notes_into function")
    return m.group(0)


def bash(driver, body):
    """run `body` after the driver's notes_into and its state, in bash -euo pipefail"""
    script = "\n".join(["set -euo pipefail", f"PYTHON={sys.executable}; DRY=0",
                        "NOTES=(); STORE_NOTES=(); declare -A STARTED=() WRITTEN=()", function_of(driver), body])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "python"), os.environ.get("PYTHONPATH", "")])})
    if r.returncode:
        raise SystemExit(f"{driver}: notes_into failed rc {r.returncode}: {r.stderr[-400:]}")


def kinds(directory, unit=None):
    return [n["kind"] for n in stale.fold({}, directory, unit=unit).get("stale", [])]


def main():
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        fails += not ok

    with tempfile.TemporaryDirectory() as t:
        i01, root, writes = "experiments/layout_choice/run-w16-mix-latency.sh", os.path.join(t, "i01"), os.path.join(t, "wr")
        bash(i01, "\n".join([
            "NOTES+=(binary_unclean$'\\t'dirty)",
            "STORE_NOTES=(foreign_load$'\\t'before-A); notes_into " + root + " cellA",
            "STORE_NOTES=(); notes_into " + root + " cellB",
            "STORE_NOTES=(foreign_load$'\\t'before-W); notes_into " + writes]))
        check(kinds(root, "cellA") == ["binary_unclean", "foreign_load"], f"1 i01: cell A, the run's and its store's notes {kinds(root, 'cellA')}")
        check(kinds(root, "cellB") == ["binary_unclean"], f"2 i01: cell B, after A, the run's note only {kinds(root, 'cellB')}")
        check(sorted(kinds(writes)) == ["binary_unclean", "foreign_load"], f"3 i01: a store's own dir (writes) holds both {kinds(writes)}")

        bic, cap = "run/measure-preflight.sh", os.path.join(t, "cap")
        bash(bic, "NOTES+=(binary_unclean$'\\t'dirty); notes_into " + cap + " c1")
        bash(bic, "notes_into " + cap + " c2")    # a second, clean, parallel call on the same capture
        check(kinds(cap, "c1") == ["binary_unclean"] and kinds(cap, "c2") == [],
              f"4 bi-cells: two calls on one capture, c1 {kinds(cap, 'c1')}, c2 {kinds(cap, 'c2')}")
    print(f"check_driver_notes: 4 cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
