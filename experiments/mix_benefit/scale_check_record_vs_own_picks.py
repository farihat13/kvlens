#!/usr/bin/env python3
"""Plan picks across scales: per uniform cell of a larger scale's grid and per query, the plan the pick record holds
(decided at SF3) against the plan that scale's own held-out rows choose, and the recorded plan's held-out cost over the
cheapest candidate's (ops + bytes / 4 KB on the held-out params). Beside them, the plan the pricing takes (the plan rule
on this scale's held-out rows, no anchor, no pin: compose.query_pick in option mode) and whether the record's pick for
this scale is within 10% of it there. Reads the record and the captures; writes neither.

    python3 experiments/mix_benefit/scale_check_record_vs_own_picks.py --grid <nine cells' captures or composed dir> --out DIR
        -> <out>/record_vs_own_picks.csv   one row per (cell, query)
           <out>/record_vs_own_picks.md    per cell: the same plan, differ, the recorded plans over the margin"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import scale_check_common as C
from scale_check_common import grid_capture, picks
from kvcost.layout_cost.cost_models import BYTES_PER_OP, op_weight  # (the op weights and 4,096, one definition)
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import capture_in
from kvevaluator.plan_choice.optimizer.criteria import PICK_BYTE_LANES   # the lanes the pick sums
from kvevaluator.plan_choice import pick_record
from kvevaluator.plan_choice.pick_record import RECORD_WORKLOAD

MARGIN = 1.10          # the plan rule (python/kvevaluator/plan_choice/candidates.py): a recorded plan stands unless it is more than 10% from the cheapest
LAYOUTS = SETTINGS


def held_out_scores(path):
    """{query: {plan: ops + bytes / 4 KB summed over the held-out params}} of the runs that finished"""
    s = defaultdict(lambda: defaultdict(float))
    with open(path) as f:
        for r in capture_layout.reader(f):
            if r["ok"] == "1":
                s[r["query"]][r["body"]] += (op_weight(float(r["S"]), float(r["G"]), float(r["N"]))
                                                + sum(float(r[c] or 0) for c in PICK_BYTE_LANES) / BYTES_PER_OP)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", required=True, help="the larger scale's nine cells, with held-out rows: a "
                    "captures/ldbc/<scale> or composed/ldbc/<scale> dir")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    record = pick_record.load(grid_capture.PICKS_FILE)
    rows = []
    reads = capture_in(a.grid, RECORD_WORKLOAD)
    for cell in [cell_id(p, x) for p in LAYOUTS for x in LAYOUTS]:
        own = picks.choose_cell(reads.path, cell, rule=picks.PlanRule(), pinned=False)
        composer = picks.choose_cell(reads.path, cell, rule=picks.PlanRule(anchor=False, pin=False))   # what the pricing takes
        sf = grid_capture.capture_sf(reads.path, cell)
        at_scale = record.exceptions(RECORD_WORKLOAD, sf, cell)                         # the record's exception there
        sc = held_out_scores(reads.held_out().path_of("counts", cell))
        store = picks.layout_store(record, reads.path, cell)      # the record row of this cell's layout (THE identity)
        if store != cell:
            raise SystemExit(f"{cell} under {a.grid}: its layout is {store or 'no record store'}, not the grid cell of that "
                             f"name: give the grid at the record's own chunk size")
        for q, row in sorted(record.cell(RECORD_WORKLOAD, store).items()):
            if q not in own:
                continue
            measure_only = set(own[q].get("measure_only", {}))
            cand = {v: x for v, x in sc[q].items() if v not in measure_only}
            cheapest = min(sorted(cand), key=cand.get)
            ratio = sc[q][row["body"]] / cand[cheapest] if row["body"] in sc[q] else None
            record_pick = at_scale.get(q, row)["body"]                                     # the record's pick for this scale
            picked = composer[q]["body"]
            vs = sc[q][record_pick] / sc[q][picked] if record_pick in sc[q] and sc[q].get(picked) else None
            rows.append({"cell": cell, "query": q, "recorded_plan": row["body"], "record_decided_at": f"sf{record.decided_at}",
                         "own_held_out_plan": own[q]["body"], "same_plan": int(row["body"] == own[q]["body"]),
                         "cheapest_held_out_plan": cheapest,
                         "recorded_over_cheapest_ops_plus_bytes_per_4kb": "" if ratio is None else round(ratio, 4),
                         "over_the_margin": "" if ratio is None else int(ratio > MARGIN),
                         "picked_plan": picked, "record_plan_at_this_scale": record_pick,
                         "record_over_pricing_ops_plus_bytes_per_4kb": "" if vs is None else round(vs, 4),
                         "record_within_margin_of_pick": "" if vs is None else int(vs <= MARGIN)})
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "record_vs_own_picks.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    L = ["# The pick record's plans against a larger scale's own held-out choice", "",
         "> Generated by "
         "`experiments/mix_benefit/scale_check_record_vs_own_picks.py`.", "",
         f"Grid: `{os.path.relpath(a.grid, C.EXP)}`. The record is read, never written.", "",
         "| cell | queries | the recorded plan = this scale's own choice | differ | recorded plans over 1.10x the cheapest (held-out params) | the record's pick for this scale within 10% of the pricing's |",
         "|---|---|---|---|---|---|"]
    for cell in dict.fromkeys(r["cell"] for r in rows):
        rs = [r for r in rows if r["cell"] == cell]
        p, x = cell[len("props_"):].split("_adj_")
        over = "; ".join(f"{r['query'].upper()} {r['recorded_over_cheapest_ops_plus_bytes_per_4kb']:.2f}x" for r in rs if r["over_the_margin"] == 1)
        within = sum(1 for r in rs if r["record_within_margin_of_pick"] == 1)
        L.append(f"| {p.capitalize()}/{x.capitalize()} | {len(rs)} | {sum(r['same_plan'] for r in rs)} | {sum(1 - r['same_plan'] for r in rs)} | {over or 'none'} | {within} of {len(rs)} |")
    L += ["", f"{sum(r['same_plan'] for r in rows)} of {len(rows)} (cell, query) pairs choose the same plan; "
          f"{sum(1 for r in rows if r['over_the_margin'] == 1)} recorded plans are over the margin; the record's pick for this "
          f"scale is within 10% of the pricing's plan in {sum(1 for r in rows if r['record_within_margin_of_pick'] == 1)} of {len(rows)}.", ""]
    with open(os.path.join(a.out, "record_vs_own_picks.md"), "w") as f:
        f.write("\n".join(L))
    print("\n".join(L[6:]))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
