#!/usr/bin/env python3
"""The local search's first improvement (kvcost.layout_search.local_search.first_improvement), on synthetic scores: it
reaches the optimum of a separable score, keeps the first improving setting in order (not the best one), keeps no
change that only ties, and stops at a local optimum of a non-separable score. No data, no store.

    python3 tests/unit/check_local_search.py [--plant order]   (the plant must exit 1)
"""
import argparse
import sys

from kvcost.layout_search import local_search
from kvcost.layout_search.local_search import first_improvement

SETTINGS = ("a", "b", "c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=("order",), help="a negative control: visit the settings in reverse (must exit 1)")
    a = ap.parse_args()
    settings = SETTINGS[::-1] if a.plant else SETTINGS
    units = [("props", "x"), ("adj", "y"), ("props", "z")]
    bad, cases = [], 0

    # 1. separable: each part's best setting is reached. Parts go in sorted order (adj|y first) and settings in the given
    #    order, a change kept at once: y moves to a; x to a (5) and then, in the same pass, to b (1). Reversed settings
    #    (the plant) move y to b first
    cost = {("props", "x"): {"a": 5, "b": 1, "c": 9}, ("adj", "y"): {"a": 1, "b": 2, "c": 3},
            ("props", "z"): {"a": 4, "b": 4, "c": 4}}
    trail = []
    assign, best, sweeps = first_improvement(units, {u: "c" for u in units}, lambda s: sum(cost[u][s[u]] for u in units),
                                             settings, log=trail.append)
    cases += 1
    if assign != {("props", "x"): "b", ("adj", "y"): "a", ("props", "z"): "c"} or best != 6:
        bad.append(f"separable: {assign}, {best}")
    if not trail or "adj|y -> a" not in trail[0]:
        bad.append(f"separable: the first kept change is not adj|y -> a (sorted parts, settings in order): {trail[:1]}")
    if sweeps != 2:
        bad.append(f"separable: {sweeps} sweeps, want 2 (one that moves, one that finds nothing)")

    # 2. a tie is not a move: z costs 4 on every setting and stays at its start, c
    cases += 1
    if assign[("props", "z")] != "c":
        bad.append("a tie moved part z")

    # 3. non-separable: x and y together at (a, a) cost 0, but each alone from (c, c) costs more: a local optimum
    def joint(s):
        x, y = s[("props", "x")], s[("adj", "y")]
        return 0 if (x, y) == ("a", "a") else 10 if "a" in (x, y) else 5
    assign, best, _ = first_improvement(units, {u: "c" for u in units}, joint, settings)
    cases += 1
    if best != 5 or assign[("props", "x")] == "a" or assign[("adj", "y")] == "a":
        bad.append(f"non-separable: {assign}, {best}: want the local optimum 5 (no single change helps)")

    # 4. the CLI: an equal share is --form sum on the equal-share file; another form, a --ref, or the bare id (it defines
    # no frequencies) exits before any load
    base = ["--dataset", "imdb", "--sf", "full", "--workload", "j01", "--grid", "/nonexistent", "--model", "wops_bytes4k",
            "--chunk-view", "half", "--out", "/nonexistent/x.json"]
    for extra in (["--form", "additive"], ["--ref", "props_split_adj_condensed"], []):
        cases += 1
        try:
            local_search.main(base + extra)
            bad.append(f"{' '.join(extra) or 'bare j01'} was not refused")
        except SystemExit as e:
            if "equal-share" not in str(e.code):
                bad.append(f"{' '.join(extra)}: refused for another reason: {e.code}")

    for b in bad:
        print("FAIL", b)
    print(f"check_local_search: {cases} cases; {'FAIL' if bad else 'PASS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
