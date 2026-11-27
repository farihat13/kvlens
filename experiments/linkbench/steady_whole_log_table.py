#!/usr/bin/env python3
"""LinkBench steady on the WHOLE request log, the table: per cell its steady timings (steps.csv + steady-passes.csv):
per cell the median wall per request type (the steady __plan__ rows), the total per measured pass (steady-passes.csv wall_ns_sum,
median of the measured passes), the measured passes' hit rate and MB read, the warm pass count; beside them the counts' totals on
the SAME requests (wops = G + 1.1 S + 0.01 N, bytes = the read lanes, wops_bytes4k = wops + bytes / 4096), as ratios to Split/Split.
Reads captures only.

    python3 experiments/linkbench/steady_whole_log_table.py --scale n1m --regime steady-<MB>mb-p2 [--root DIR] [--counts CAPTURE]
        the timings: <root>/timings/linkbench/<scale>/linkbench/meta-<cell>/<regime>/ (kvevaluator.output_tree);
        --counts: LinkBench's capture of the whole log (default the output dir's, param set all), every cell in it
        prints the table (markdown); with --csv FILE also one row per cell with every number of the table"""
import argparse
import csv, os, statistics as st, sys
from kvevaluator.output_tree import Capture, Tree, timing_in
from kvcost.layout_cost import cost_models as CM
ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
ap.add_argument("--scale", required=True)
ap.add_argument("--regime", required=True)
ap.add_argument("--root", default=None)
ap.add_argument("--counts", default=None)
ap.add_argument("--csv", default=None)
A = ap.parse_args()
CSV = A.csv
from kvevaluator.capture.step_record import READ_BYTE_LANES  # (the lanes, one definition)
LANES = READ_BYTE_LANES
ORDER = ["props_split_adj_split", "props_split_adj_condensed", "props_split_adj_chunked", "props_condensed_adj_split", "props_condensed_adj_condensed",
         "props_condensed_adj_chunked", "props_chunked_adj_split", "props_chunked_adj_condensed", "props_chunked_adj_chunked"]
ADDED = ["props_split_adj_chunked_c4k", "props_condensed_adj_chunked_c4k", "props_chunked_c4k_adj_split", "props_chunked_c4k_adj_condensed",
         "props_chunked_c4k_adj_chunked_c4k"]      # the Chunked side at 4 KB chunks
ORDER += ADDED
CAP = Capture(A.counts) if A.counts else Tree().capture("linkbench", A.scale, "log")
out = str(Tree(A.root).timings("linkbench", A.scale))
TYPES = ["getnode", "getlinklist_link1", "getlinklist_link2", "countlink_link1", "countlink_link2", "getlink_link1", "getlink_link2"]
res = {}
for c in ORDER:
    d = str(timing_in(out, f"meta-{c}", A.regime))
    if not os.path.exists(os.path.join(d, "steps.csv")):
        continue
    walls = {}
    for r in csv.DictReader(open(os.path.join(d, "steps.csv"))):
        if r["step"] == "__plan__" and r["regime"].startswith("steady"):
            walls[(r["query"], r["param_idx"])] = float(r["wall_ns"])
    passes = list(csv.DictReader(open(os.path.join(d, "steady-passes.csv"))))
    meas = [p for p in passes if p["phase"] == "measured"]
    ops = by = 0.0; seen = set()
    for r in csv.DictReader(open(CAP.path_of("steps", c))):
        k = (r["query"], r["param_idx"])
        if k in walls and r.get("step") != "__plan__":
            seen.add(k); ops += CM.op_weight(float(r["S"]), float(r["G"]), float(r["N"])); by += sum(float(r[x] or 0) for x in LANES)
    res[c] = {"med": {t: st.median(w for (q, _), w in walls.items() if q == t) / 1e3 for t in TYPES if any(q == t for q, _ in walls)},
              "tot": st.median(float(p["wall_ns_sum"]) for p in meas) / 1e9, "hit": " ".join(f"{float(p['hit_rate']):.3f}" for p in meas),
              "read": " ".join(f"{float(p['io_bytes_read']) / 1e6:.0f}" for p in meas), "warm": len(passes) - len(meas), "n": len(walls),
              "ncounts": len(seen), "wops": ops, "bytes": by, "wb4k": CM.wops_bytes4k(ops, by)}
cells = list(res)
if not cells:
    sys.exit("0 cells found: FAIL")
ref = res[cells[0]]
print(f"steady {out}: {len(cells)} cells; requests per cell {sorted({r['n'] for r in res.values()})}, matched in the counts {sorted({r['ncounts'] for r in res.values()})}; ratios to {cells[0]}")
print("| cell | warm passes | pass total s | /ref | hit rates | MB read | " + " | ".join(f"{t} us" for t in TYPES) + " | wops | /ref | wops_bytes4k | /ref | bytes GB |")
print("|---|" + "---|" * (len(TYPES) + 10))
for c in cells:
    r = res[c]
    print(f"| {c} | {r['warm']} | {r['tot']:.2f} | {r['tot'] / ref['tot']:.3f} | {r['hit']} | {r['read']} | " + " | ".join(f"{r['med'].get(t, float('nan')):.0f}" for t in TYPES)
          + f" | {r['wops']:,.0f} | {r['wops'] / ref['wops']:.3f} | {r['wb4k']:,.0f} | {r['wb4k'] / ref['wb4k']:.3f} | {r['bytes'] / 1e9:.2f} |")
for name, key in (("steady pass total", "tot"), ("wops", "wops"), ("wops_bytes4k", "wb4k")):
    print(f"order by {name}:", " < ".join(sorted(cells, key=lambda c: res[c][key])))
if CSV:
    with open(CSV, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["cell", "warm_passes", "pass_total_s", "pass_total_over_split_split", "measured_hit_rates", "measured_mb_read"]
                   + [f"{t}_median_us" for t in TYPES] + ["requests", "requests_matched_in_counts", "wops", "bytes", "wops_bytes4k"])
        for c in cells:
            r = res[c]
            w.writerow([c, r["warm"], round(r["tot"], 4), round(r["tot"] / ref["tot"], 4), r["hit"], r["read"]]
                       + [round(r["med"].get(t, float("nan")), 1) for t in TYPES] + [r["n"], r["ncounts"], round(r["wops"], 2), round(r["bytes"]), round(r["wb4k"], 2)])
