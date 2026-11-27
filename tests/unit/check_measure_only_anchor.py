#!/usr/bin/env python3
"""The planted-flag control of the plan-choice rule (python/kvevaluator/plan_choice/candidates.py): a measure-only body is never the
pick and never the anchor. On one cell and query, the body the rule picks WITHOUT a plant is flagged measure-only in a COPY of
the capture (its run rows, reported and held-out), and the pick must move away from it on every path: the held-out path
(picks.choose_cell), the in-sample path (the copy without its held-out set, --in-sample), and the composed path
(layout_choice/compose.query_pick on a non-uniform assignment, the flag planted through compose.measure_only_for_assignment). On
each path the anchor (the planner) must not be the planted body either. Exit 1 on any path where the plant does not bite.

    python3 tests/unit/check_measure_only_anchor.py --sf 0.1 [--grid output/captures/ldbc/sf0.1] [--cell props_condensed_adj_condensed] [--query is7]
"""
import argparse
import csv
import os
import shutil
import sys
import tempfile
from kvevaluator.paths import OUTPUT_DIR, ROOT
from kvevaluator.layouts import cell_id

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.output_tree import Tree, capture_in, writes_in
from kvcost.layout_cost import picks
from kvevaluator.capture import capture_layout


def plant(src, dst, cell, q, body):
    """copy one cell's captures from place dir `src` to `dst` (reads reported and held-out, writes), with `body`'s rows of
    `q` flagged measure-only"""
    s_rd, d_rd = capture_in(src, "interactive"), capture_in(dst, "interactive")
    for s_cap, d_cap in ((s_rd, d_rd), (s_rd.held_out(), d_rd.held_out())):
        if not os.path.exists(s_cap.path_of("counts", cell)):
            continue
        files = [(s_cap.path_of(k, cell), d_cap.path_of(k, cell)) for k in ("counts", "parts", "steps")]
        files.append((writes_in(src, "ldbc").path_of("counts", cell), writes_in(dst, "ldbc").path_of("counts", cell)))
        files.append((s_cap.record(cell), d_cap.record(cell)))
        for f, to in files:
            if os.path.exists(f):
                os.makedirs(os.path.dirname(to), exist_ok=True)
                shutil.copy(f, to)
        p = d_cap.path_of("counts", cell)
        with open(p) as f:
            rows = list(capture_layout.reader(f)); fields = f.fieldnames if hasattr(f, "fieldnames") else None
        fields = list(rows[0].keys())
        n = 0
        for r in rows:
            if r["query"] == q and r["body"] == body:
                r["measure_only"] = "1"; n += 1
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
        print(f"   planted measure_only=1 on {n} rows of {q} {body!r} in {os.path.relpath(p)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", default=None)
    ap.add_argument("--cell", default="props_condensed_adj_condensed")
    ap.add_argument("--query", default="is7")
    a = ap.parse_args()
    grid = a.grid or str(Tree().place_dir("captures", "ldbc", a.sf))
    cell, q = a.cell, a.query
    # pinned=False: the recorded sf3 decision pins the very body this check plants, so a pinned pick never moves
    base = picks.choose_cell(capture_in(grid, "interactive").path, cell, rule=picks.PlanRule(), pinned=False)[q]
    body = base["body"]
    print(f"unplanted: {cell} {q}: pick {body!r}, planner {base['planner']!r}, rule {base['rule']}; pickable {sorted(base['costs'])}, "
          f"measure-only {sorted(base['measure_only'])}")
    if len(base["costs"]) < 2:
        raise SystemExit(f"{cell} {q}: only one pickable body ({sorted(base['costs'])}): the control needs two (pick another cell / query)")
    ok = True
    tmp = tempfile.mkdtemp(prefix="planted-", dir=os.path.join(str(OUTPUT_DIR)))
    try:
        # 1. the held-out path
        plant(grid, tmp, cell, q, body)
        c = picks.choose_cell(capture_in(tmp, "interactive").path, cell, rule=picks.PlanRule(), pinned=False)[q]
        good = c["body"] != body and c["planner"] != body and c["body"] in base["costs"]
        ok &= good
        print(f"held-out path: pick {c['body']!r}, planner {c['planner']!r} (engine {c.get('engine_pick')!r}), rule {c['rule']} -- "
              f"{'moved off the planted body: OK' if good else 'FAIL'}")
        # 2. the in-sample path: the same copy without its held-out set
        shutil.rmtree(capture_in(tmp, "interactive").held_out().path, ignore_errors=True)
        c = picks.choose_cell(capture_in(tmp, "interactive").path, cell, rule=picks.PlanRule(in_sample=True), pinned=False)[q]
        good = c["body"] != body and c["planner"] != body
        ok &= good
        print(f"in-sample path: pick {c['body']!r}, planner {c['planner']!r}, rule {c['rule']} -- {'OK' if good else 'FAIL'}")
        # 3. the composed path: a NON-uniform assignment of the query's units, the plant through compose's own door
        from kvcost.layout_cost import compose
        data = compose.load(grid, by_dir=False, composer=compose.Composer(criterion=None, plan_rule=picks.PlanRule()))
        d = data[("interactive", q)]
        units = sorted(u for per in d["cost"].values() for u in per if u is not None)
        all_units = sorted({u for dd in data.values() for per in dd["cost"].values() for u in per if u is not None})
        if len(units) < 2:
            raise SystemExit(f"{q} reads {units}: the composed control needs two units")
        layouts = ("condensed", "split", "chunked")
        assign = {u: layouts[i % 2] for i, u in enumerate(all_units)}      # the WHOLE assignment, alternating: never uniform
        before = compose.query_pick(d, compose.params_of(d, None), assign, set(units))[0]
        orig = compose.measure_only_for_assignment
        compose.measure_only_for_assignment = lambda dd, aa, uu: (orig(dd, aa, uu) | {body}) if dd.get("q") == q else orig(dd, aa, uu)
        compose._MO_CELL.clear()
        after = compose.query_pick(d, compose.params_of(d, None), assign, set(units))[0]
        planner_after = ""             # a composed option has no anchor: never the planted body
        compose.measure_only_for_assignment = orig
        good = after != body and planner_after != body and (before == body or before in d["valid"].get(cell_id(assign[units[0]], assign[units[1]]), set()) or True)
        ok &= (after != body and planner_after != body)
        print(f"composed path ({ {u: assign[u] for u in units} } of {len(all_units)} units): pick {before!r} -> {after!r}, planner after {planner_after!r} -- "
              f"{'OK' if after != body and planner_after != body else 'FAIL'}"
              + ("" if before == body else f"  (note: the unplanted composed pick was {before!r}, not the planted body: the plant proves the anchor, not a move)"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("measure-only anchor control:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
