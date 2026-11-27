#!/usr/bin/env python3
"""LDBC BI on the nine uniform cells: per (query, cell) the plan the cell is priced at, beside the planner's own body.

Reads the workload's counts capture (run-bi-cells.sh --stage counts -> <root>/captures/ldbc/sf<SF>/bi/reported); measures nothing.
BI is priced IN-SAMPLE: its param files hold 5 rows per family, so the pick is made on the params it is reported on
(picks.IN_SAMPLE_WORKLOADS). To keep an in-sample pick from reading as a finding, every (query, cell) row
shows the planner's body too, and a pick LESS THAN 10% CHEAPER than the planner's body is marked `inside_margin`
(the 10% margin of the plan rule, python/kvevaluator/plan_choice/candidates.py; such a pick is not a finding). The score is the pick rule's: G + 1.1 S + 0.01 N +
read bytes / 4096, summed over the params.

    python3 experiments/meta/bi_cells_table.py --sf 0.1 [--root DIR]
    python3 experiments/meta/bi_cells_table.py --sf 3 --chunked-4k
    python3 experiments/meta/bi_cells_table.py --family job --sf sample1pct      (the JOB workload: -> job_picks.csv)
--chunked-4k: the nine layouts with Chunked at 4 KB (the grid's four cells with no Chunked side + the five specs of
grid_chunked_4k, run-bi-cells.sh --chunked-4k); the file is then bi_picks_chunked_4k.csv and every row names its layout
("Chunked (4 KB)" for a 4 KB side). --formats: the named formats in the same capture, bi_picks_formats.csv.
Writes <root>/summary/<dataset>/<scale>/<workload>-cells/bi_picks.csv (one row per (query, cell): every raw sum beside the
score) and prints the per-cell totals and the picks that differ from the planner's body. Exit 1 when a (query, cell) has
no pick or a cell is missing.
"""
import argparse
import csv
import json
import os
import sys
from kvevaluator import workloads
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Capture, Tree
from kvevaluator.measure.grid_capture import GRID, load_runs, workload_queries
from kvevaluator.capture.spec_sets import format_of, spec_cells
from kvcost.layout_cost import cost_models
from kvcost.layout_cost.picks import IN_SAMPLE_WORKLOADS, PlanRule, choose_cell, weighted

MARGIN = 0.10
CHUNKED_4K = os.path.join(str(ROOT), "inputs", "specs", "grid_chunked_4k")
SETTING = {'{"group": "item", "owners": "one"': "Split", '{"group": "owner", "owners": "one"': "Condensed",
           '{"group": "owner", "owners": {"pack": 4096}': "Chunked (4 KB)", '{"group": "owner", "owners": {"pack": 65536}': "Chunked (64 KB)"}


def layout_name(spec_path):
    """'<props setting> / <adjacency setting>' read from the spec file; a named format: its name"""
    if format_of(spec_path):
        return format_of(spec_path)
    spec = json.load(open(spec_path))
    def one(part):
        text = json.dumps(part, sort_keys=True)
        return next(name for prefix, name in SETTING.items() if text.startswith(prefix))
    return f"{one(spec['vertex_props'])} / {one(spec['adjacency'])}"
RAW = ("S", "G", "N", "read_bytes")


def sums(rows):
    out = {k: sum(int(float(r[k] or 0)) for r in rows) for k in RAW}
    out["wops"] = sum(weighted(r) for r in rows)
    out["score"] = cost_models.wops_bytes4k(out["wops"], out["read_bytes"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", default=None, help="the output tree (default: the output dir)")
    ap.add_argument("--family", dest="workload", default="bi", choices=IN_SAMPLE_WORKLOADS,
                    help="an in-sample workload of the grid (default bi; job: --sf is its data tag, e.g. sample1pct)")
    ap.add_argument("--chunked-4k", action="store_true", help="the nine layouts with Chunked at 4 KB (see the docstring)")
    ap.add_argument("--formats", action="store_true", help="the NAMED formats captured beside the cells "
                    "(grid_bodies --format), instead of the cells")
    a = ap.parse_args()
    W = a.workload
    tree = Tree(a.root)
    cap = tree.capture(W, a.sf).path
    queries, _ = workload_queries(W)
    # cell -> its spec file
    home = {c: os.path.join(GRID, f"{c}.json") for c in spec_cells(GRID)}
    if a.chunked_4k:
        home = {c: h for c, h in home.items() if "chunked" not in c}
        home.update({c: os.path.join(CHUNKED_4K, f"{c}.json") for c in spec_cells(CHUNKED_4K)})
    if a.formats:      # every named format in the capture, by its store's name (its run record's spec_path: format:<name>)
        specs = {s_: json.load(open(Capture(cap).record(s_)))["spec_path"] for s_ in Capture(cap).stores()}
        home = {s_: p for s_, p in specs.items() if format_of(p)}
    cells = list(home)
    rows, missing = [], []
    for cell in cells:
        spec = home[cell]
        if not os.path.exists(Capture(cap).path_of("counts", cell)):
            missing.append(f"{cell}: no capture")
            continue
        chosen = choose_cell(cap, cell, rule=PlanRule(), spec_path=spec)
        runs = load_runs(cap, cell)
        for q in queries:
            d = chosen.get(q)
            if d is None or d["body"] is None:
                missing.append(f"{cell} {q}: no pick")
                continue
            pick = sums(list(d["rows"].values()))
            # the selector's rows: a capture with a planner_pick column only (else the planner columns stay empty)
            prow = [r for r in runs if r["query"] == q and r.get("planner_pick") == "1" and r["ok"] == "1"]
            planner = sums(prow) if prow else None
            pv = prow[0]["body"] if prow else ""
            ratio = (pick["score"] / planner["score"]) if planner and planner["score"] else None
            # the cheapest body that uses NO property index and returns the planner body's rows (bodies named
            # ..._by_index enter by one): the no-index reading (JOB's own index setting). Empty where bodies carry
            # no such names (BI)
            free = [r for r in runs if r["query"] == q and r["ok"] == "1" and "_by_" in r["body"] and "_by_index" not in r["body"]
                    and r["result_hash"] == (prow or list(d["rows"].values()))[0]["result_hash"]]
            by_body = {}
            for r in free:
                by_body.setdefault(r["body"], []).append(r)
            best_free = min(((sums(rs)["score"], b) for b, rs in by_body.items()), default=None)
            rows.append({"sf": a.sf, "cell": cell, "layout": layout_name(spec), "query": q, "nparam": len(d["rows"]), "pick": d["body"],
                         "planner_body": pv, "pick_is_planner": int(d["body"] == pv),
                         **{f"pick_{k}": round(v, 3) if isinstance(v, float) else v for k, v in pick.items()},
                         **{f"planner_{k}": (round(v, 3) if isinstance(v, float) else v) for k, v in (planner or {}).items()},
                         "pick_over_planner": round(ratio, 4) if ratio is not None else "",
                         "inside_margin": int(ratio is not None and d["body"] != pv and ratio > 1 - MARGIN),
                         "index_free_body": best_free[1] if best_free else "",
                         "index_free_score": round(best_free[0], 3) if best_free else "",
                         "rows_out": sum(int(float(r["rows"] or 0)) for r in d["rows"].values()),
                         "pick_rule": "in-sample (no held-out rows exist: BI has 5 params per family, a JOB query has none)"})
    name = f"{W}_picks_formats.csv" if a.formats else f"{W}_picks_chunked_4k.csv" if a.chunked_4k else f"{W}_picks.csv"
    out = tree.summary(workloads.get(W).dataset, a.sf, f"{W}-cells")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    if rows:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), restval="")
            w.writeheader(); w.writerows(rows)
    print(f"{'LDBC BI' if W == 'bi' else W} on the nine cells, {a.sf}: {len(rows)} (query, cell) rows of {len(queries)} x {len(cells)}; "
          f"picks IN-SAMPLE on the reported params (no held-out rows exist)")
    print(f"score = the pick rule's wops_bytes4k, summed over the queries. 'first body' = each query's default plan (the planner's); "
          f"'no index' = the cheapest body that uses no property index (it reads a label's rows in one run where the "
          f"store's layout keeps them together, per vertex elsewhere)")
    print(f"{'cell':32} {'pick: score (K)':>16} {'first body (K)':>15} {'no index (K)':>13} {'weighted ops (K)':>17} {'read MB':>9} "
          f"{'picks != first body':>20} {'of them inside the margin':>26}")
    for cell in cells:
        cr = [r for r in rows if r["cell"] == cell]
        if not cr:
            continue
        diff = [r for r in cr if not r["pick_is_planner"]]
        nofree = [r for r in cr if r["index_free_score"] == ""]
        free_total = "" if nofree else f"{sum(r['index_free_score'] for r in cr) / 1e3:,.1f}"
        print(f"{cell:32} {sum(r['pick_score'] for r in cr) / 1e3:16,.1f} {sum(r.get('planner_score') or 0 for r in cr) / 1e3:15,.1f} "
              f"{free_total:>13} {sum(r['pick_wops'] for r in cr) / 1e3:17,.1f} "
              f"{sum(r['pick_read_bytes'] for r in cr) / 1e6:9,.1f} {len(diff):20d} {sum(r['inside_margin'] for r in diff):26d}")
    diff = [r for r in rows if not r["pick_is_planner"]]
    print(f"\npicks that are not the planner's body: {len(diff)} of {len(rows)}; "
          f"{sum(r['inside_margin'] for r in diff)} of them less than {MARGIN:.0%} cheaper (inside the margin, in-sample: not a finding)")
    for r in sorted(diff, key=lambda r: (r["query"], r["cell"])):
        print(f"  {r['query']:6} {r['cell']:30} {r['planner_body'] or '(none)':28} -> {r['pick']:40} "
              f"{r['pick_over_planner']}x{'  INSIDE THE MARGIN' if r['inside_margin'] else ''}")
    print(f"wrote {os.path.relpath(path)} ({len(rows)} rows)")
    if missing:
        print(f"FAIL: {len(missing)} missing: {missing[:8]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
