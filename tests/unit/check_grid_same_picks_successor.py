#!/usr/bin/env python3
"""tests/gates/meta/check_grid_same_picks.py matches runs by body: a capture before a count-equal rewrite (the old body's
sha) and one after it (the successor's) hold the same runs (the reuse ledger's body rows, inputs/adoptions.json, bi3's renamed
body); the control, a sha outside the table, is a different run. Store-free: two synthetic counts files.

    python3 tests/unit/check_grid_same_picks_successor.py
"""
import csv
import importlib.util
import os
import sys
import tempfile

from kvevaluator.paths import ROOT

spec = importlib.util.spec_from_file_location("gsp", ROOT / "tests" / "gates" / "meta" / "check_grid_same_picks.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

CELL = "props_split_adj_split"


def capture(root, body, sha):
    """<root>/bi/reported/counts/<CELL>.csv: one ok run of bi3 under `body` with `sha`"""
    d = os.path.join(root, "bi", "reported", "counts")
    os.makedirs(d, exist_ok=True)
    cols = ["query", "param_idx", "body", "body_sha", *G.COLS]
    with open(os.path.join(d, f"{CELL}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({**{c: 0 for c in cols}, "query": "bi3", "param_idx": 1, "body": body, "body_sha": sha, "ok": 1,
                    "result_hash": "h"})
    return root


def main():
    fails = 0
    with tempfile.TemporaryDirectory() as t:
        before = capture(os.path.join(t, "before", "captures", "grid"), "hand~2bb1f0", "2bb1f0ebfdd8")
        after = capture(os.path.join(t, "after", "captures", "grid"), "hand~a4eca1", "a4eca18cff92")
        stray = capture(os.path.join(t, "stray", "captures", "grid"), "hand~000000", "000000000000")
        (ra, _), (rb, _), (rs, _) = G.runs(before), G.runs(after), G.runs(stray)
        probs, n = G.compare("runs", ra, rb)
        ok = n == 1 and not probs
        print(f"{'PASS' if ok else 'FAIL'} 1 the old body's run and its successor's are one run: {n} compared, {len(probs)} problems")
        fails += not ok
        probs, n = G.compare("runs", rs, rb)
        ok = n == 0 and len(probs) == 2
        print(f"{'PASS' if ok else 'FAIL'} 2 the control: a sha outside the table is another run ({len(probs)} problems)")
        fails += not ok
    print(f"check_grid_same_picks_successor: 2 cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
