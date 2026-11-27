#!/usr/bin/env python3
"""cpplatency's one-body rule, from its command line, with no store: --candidates names one body per query in every
regime; two bodies need --compare-bodies (warm and cold only: steady and reuse refuse them even then); --alternate
and a bare --compare-bodies are refused. An accepted command is stopped by --regen on an empty --out (it refuses only
after the arguments' checks, before any server). --control runs the cases with the check replaced by a no-op: the
two-body cases must then pass the checks, so the control ends in FAIL.

    python3 tests/unit/check_cpplatency_bodies.py              # OK: 9/9
    python3 tests/unit/check_cpplatency_bodies.py --control    # FAIL (the two-body refusals are gone)
"""
import argparse
import sys
import tempfile

from kvevaluator.measure import cpplatency

ONE_BODY = "one body per query"
CHECKED = "--regen: no saved cells"        # past the arguments' checks: an accepted command


def run(out, *args):
    """cpplatency's SystemExit text for these arguments (with --regen on an empty --out)"""
    try:
        cpplatency.main(["--sf", "0.1", "--family", "interactive", "--format", "nebula", "--arms", "nebula",
                         "--queries", "is1", "is2", "--out", out, "--regen", *args])
    except SystemExit as e:
        return str(e)
    return "(returned)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--control", action="store_true", help="the cases with the one-body check removed")
    a = ap.parse_args()
    if a.control:
        cpplatency.check_bodies = lambda cands, args: None
    results = []
    two = ["--candidates", "is2=index_walk,two_phase"]
    cases = [
        ("one body, warm and cold: accepted", ["--candidates", "is2=index_walk"], CHECKED),
        ("two bodies, warm and cold, no flag: refused", two, "add --compare-bodies"),
        ("two bodies, steady, no flag: refused", two + ["--steady", "64,2"], ONE_BODY),
        ("two bodies, reuse, no flag: refused", two + ["--reuse-distance", "2"], ONE_BODY),
        ("two bodies with --compare-bodies, warm and cold: accepted", two + ["--compare-bodies"], CHECKED),
        ("two bodies with --compare-bodies, steady: still refused", two + ["--compare-bodies", "--steady", "64,2"], ONE_BODY),
        ("two bodies with --compare-bodies, reuse: still refused", two + ["--compare-bodies", "--reuse-distance", "2"], ONE_BODY),
        ("--alternate without --compare-bodies: refused", ["--candidates", "is2=index_walk", "--alternate", "1"],
         "goes with --compare-bodies"),
        ("--compare-bodies with no --candidates: refused", ["--compare-bodies"], "name them"),
    ]
    with tempfile.TemporaryDirectory(prefix="cpplatency-bodies-") as out:
        for name, args, want in cases:
            got = run(out, *args)
            ok = want in got
            results.append(ok)
            print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    print(f"check_cpplatency_bodies{' --control' if a.control else ''} {'OK' if all(results) else 'FAIL'}: "
          f"{sum(results)}/{len(results)} cases")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
