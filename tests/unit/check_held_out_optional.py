#!/usr/bin/env python3
"""A family without a held-out set (LinkBench, the write families) is priced in-sample, never refused by the held-out
lookup (Capture.held_out() must not raise before grid_capture.load_held_out can return None, or the grid
driver's LinkBench run dies at its check stage). On a tiny synthetic LinkBench capture
(one cell, one query, two params, built in a temporary dir), in order:

  1. picks.choose_cell under the in-sample rule (check_grid's --in-sample) picks the query's body;
  2. grid_capture.load_held_out returns None for it;
  3. Capture.has_held_out is False for LinkBench's rows and a writes capture, True for LDBC's reported rows;
  4. Capture.held_out() still refuses LinkBench (a caller that requires a held-out set fails by name).

    python3 tests/unit/check_held_out_optional.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Capture, Tree
from kvcost.layout_cost import picks

CELL = "props_split_adj_split"
# two getnode runs as a LinkBench grid capture writes them (the n1m capture's first rows, the per-run columns blank)
FIELDS = ["sf", "cell", "query", "param_idx", "body", "measure_only", "ok", "error", "S", "G", "N", "S_miss", "G_miss",
          "N_miss", "get_key", "get_value", "scan_key", "scan_value", "seek_miss_key", "next_miss_key", "get_miss_key",
          "read_bytes", "rows", "result_hash", "answer_key", "body_sha", "run_key", "wall_ns", "measured_as"]
ROWS = [["n1m", CELL, "getnode", "1", "getnode", "0", "1", "", "0", "4", "0", "0", "0", "0", "44", "121", "0", "0", "0",
         "0", "0", "165", "1", "47b50c60e7052f1b", "86a79fd865e9fba1", "437eb02f97cd", "", "", ""],
        ["n1m", CELL, "getnode", "2", "getnode", "0", "1", "", "0", "4", "0", "0", "0", "0", "44", "252", "0", "0", "0",
         "0", "0", "296", "1", "f88d3aa6f06f03bc", "09a1b696a5d907b5", "437eb02f97cd", "", "", ""]]

failed = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failed.append(name)


def fixture(root):
    """one LinkBench cell: its counts CSV and run record, in the tree's capture of param set quick"""
    cap = Tree(root).capture("linkbench", "n1m", "quick")
    out = cap.path
    os.makedirs(os.path.dirname(cap.path_of("counts", CELL)), exist_ok=True)
    with open(cap.path_of("counts", CELL), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(FIELDS)
        w.writerows(ROWS)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    os.makedirs(os.path.dirname(cap.record(CELL)), exist_ok=True)
    with open(cap.record(CELL), "w") as f:
        json.dump({"cell": CELL, "sf": "n1m", "workload": "linkbench", "nparam": 2, "plan_pipeline": 2,
                   "tree": {"git_sha": head, "git_dirty": False}}, f)
    return out


def main():
    with tempfile.TemporaryDirectory() as tmp:
        out = fixture(tmp)
        try:
            chosen = picks.choose_cell(out, CELL, rule=picks.PlanRule(in_sample=True))
            pick = ((chosen or {}).get("getnode") or {}).get("body")
            case("1 choose_cell prices a LinkBench capture in-sample", pick == "getnode", f"getnode -> {pick!r}")
        except (Exception, SystemExit) as e:          # the bug: ValueError from Capture.held_out()
            case("1 choose_cell prices a LinkBench capture in-sample", False, f"{type(e).__name__}: {e}")
        try:
            got = grid_capture.load_held_out(out, CELL)
            case("2 load_held_out is None for LinkBench", got is None, repr(got))
        except Exception as e:
            case("2 load_held_out is None for LinkBench", False, f"{type(e).__name__}: {e}")
        if not hasattr(Capture, "has_held_out"):
            case("3 Capture.has_held_out exists", False, "missing")
        else:
            ldbc = Tree(tmp).capture("interactive", "0.1").path
            writes = Tree(tmp).capture("writes", "0.1").path
            got = (Capture(out).has_held_out(), Capture(writes).has_held_out(), Capture(ldbc).has_held_out())
            case("3 has_held_out: LinkBench False, writes False, LDBC reported True", got == (False, False, True), str(got))
        try:
            Capture(out).held_out()
            case("4 held_out() refuses LinkBench by name", False, "no error")
        except ValueError as e:
            case("4 held_out() refuses LinkBench by name", "no held-out params" in str(e), str(e)[-80:])
    print(f"check_held_out_optional: 4 cases, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
