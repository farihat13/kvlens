#!/usr/bin/env python3
"""Per (layout, LDBC read query) the plan the pick rule keeps on a grid capture and how close the next candidate is (owner: experiments/layout_choice).

The pick is picks.choose_cell's (the plan rule of python/kvevaluator/plan_choice/candidates.py, on the capture's held-out
params). Columns: option (the cell), query, rule, pick, planner (the planner's own plan), other and ratio (the
challenger, rows_of), flips_at, headroom, ties (candidates scoring exactly the pick's), costs (every candidate's
held-out score, by name). Opens no store.
check_cross_benchmark_numbers.py reads the pick column.

    python3 experiments/layout_choice/pick_margins.py --grid <sf3 grid capture root> --out <dir>    # -> <dir>/pick_margins.csv
"""
import argparse
import csv
import os
import sys

from kvcost.layout_cost import picks
from kvcost.layout_cost.compose import CELLS
from kvevaluator.plan_choice.candidates import MARGIN

FIELDS = ["option", "query", "rule", "pick", "planner", "other", "ratio", "flips_at", "headroom", "ties", "costs"]


def rows_of(grid):
    """one row per (cell, query) of the nine cells, sorted. The pick's challenger: the planner's plan when the pick moved
    off it (ratio = the pick's score / the planner's), else the cheapest candidate not tied with the pick (ratio = its
    score / the pick's). flips_at = 1 - the margin; headroom = how far ratio is from flipping, relative"""
    flips = 1 - MARGIN
    out = []
    for cell in sorted(CELLS):
        for q, d in sorted(picks.choose_cell(grid, cell, rule=picks.PlanRule()).items()):
            scores = d.get("held_out_scores") or {}
            pick, planner = d["body"], d.get("planner", "")
            mine = scores.get(pick)
            ties = sorted(v for v, s in scores.items() if v != pick and s == mine)
            other, ratio, headroom = "", None, ""
            if mine and pick != planner and scores.get(planner):
                other, ratio = planner, mine / scores[planner]
                headroom = f"{(flips - ratio) / ratio:.4f}"
            elif mine:
                rest = sorted((s, v) for v, s in scores.items() if s != mine)
                if rest:
                    other, ratio = rest[0][1], rest[0][0] / mine
                    headroom = f"{(ratio - flips) / flips:.4f}"
            out.append({"option": cell, "query": q, "rule": d.get("rule", ""), "pick": pick, "planner": planner, "other": other,
                        "ratio": f"{ratio:.4f}" if ratio is not None else "", "flips_at": f"{flips:.2f}", "headroom": headroom,
                        "ties": " ".join(ties), "costs": " ".join(f"{v}={scores[v]:g}" for v in sorted(scores))})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grid", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = rows_of(a.grid)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "pick_margins.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    moved = sum(1 for r in rows if r["pick"] != r["planner"])
    print(f"[pick_margins] {len(rows)} (cell, query) picks, {moved} off the planner's plan -> {path}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
