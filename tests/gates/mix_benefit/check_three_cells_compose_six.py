#!/usr/bin/env python3
"""Are three layouts enough? Price each of the six off-diagonal uniform cells from the three diagonal ones
(props parts from the diagonal cell with that props layout, adjacency parts from the one with that adjacency layout,
index parts from the first), per (query, body, param), and compare with what was measured on that cell.
Inputs: the per-part rows of each capture (<workload>/<params>/parts/<cell>.csv) below --grid, a captures/ldbc/<scale>
dir (default the output tree's captures/ldbc/sf3); BI's from --bi DIR (default the grid; a grid without BI
skips it); LinkBench's from --linkbench DIR, a captures/linkbench/<scale> dir holding the whole request log (param set
all; skipped without it).
Output: output/summary/ldbc/sf3/mix-benefit/three-cells/{rows.csv, differ.csv, missing.csv, summary.csv}. Opens no store.
Exit 1 when a priced row differs from the measured one in any channel, or when 0 rows were compared. A body that a
source cell did not run is listed in missing.csv and not priced (BI's label-scan bodies need Chunked props).
--grid DIR: another nine-cell capture (a gate round's, at sf0.1). --plant adds one SEEK to one row of the Split/Split
source: the control (it must exit 1 and name that row).
  python3 tests/gates/mix_benefit/check_three_cells_compose_six.py [--grid DIR] [--bi DIR] [--linkbench DIR] [--out DIR] [--plant]"""
import argparse, csv, collections, os, sys
from kvevaluator.paths import ROOT
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import HELD_OUT, Tree, capture_in, writes_in
from kvevaluator.capture.step_record import HIT_BYTE_LANES  # (the lanes, one definition)
csv.field_size_limit(1 << 30)
EXP = str(ROOT)
ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--linkbench", default="", help="LinkBench's captures/linkbench/<scale> dir (its whole request log: param set all)")
ap.add_argument("--bi", default="", help="the captures dir of BI's nine cells, when the grid holds none (default: the grid)")
ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "mix-benefit", "three-cells")))
ap.add_argument("--grid", default=str(Tree().place_dir("captures", "ldbc", "3")),
                help="the nine cells: a captures/ldbc/<scale> dir")
ap.add_argument("--plant", action="store_true", help="the control: one SEEK added to one source row; must exit 1")
a = ap.parse_args()
M = a.grid.rstrip("/") + "/"; PLANT = a.plant                                # `a` is a loop variable below
LB = (a.linkbench.rstrip("/") + "/") if a.linkbench else "/no-linkbench-dir-given/"
OUT = a.out.rstrip("/") + "/"; os.makedirs(OUT, exist_ok=True)
LAY = SETTINGS
cell = cell_id
def _src(label, cap, write=False):   # (label, parts file per cell, steps file per cell or None, is a write capture)
    return (label, cap.path_of("parts", "{c}"), None if write else cap.path_of("steps", "{c}"), write)
_lb = LB.rstrip("/")
SOURCES = [_src("interactive", capture_in(M, "interactive")),
           _src("interactive held-out", capture_in(M, "interactive", HELD_OUT)),
           _src("microbench", capture_in(M, "microbench")),
           _src("bi", capture_in(a.bi.rstrip("/") or M, "bi")),
           _src("ldbc writes", writes_in(M, "ldbc"), write=True),
           _src("linkbench", capture_in(_lb, "linkbench", "log")),
           _src("linkbench writes", writes_in(_lb, "linkbench", "log"), write=True)]
COUNTS = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")
def lanes_of(header):
    return [h for h in header if h.endswith("_key") or h.endswith("_value")]
def kind_of(part):
    k = part.split(":")[0]
    return {"props": "props", "adj": "adj", "eprops": "adj", "index": "index"}.get(k) or sys.exit(f"unknown part {part}")
def load(path, steps_path, write):
    """{(query, body, param): {kind: Counter}}, {(query, body, param): {part: Counter}}, the channel names"""
    by_kind = collections.defaultdict(lambda: collections.defaultdict(collections.Counter)); by_part = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    if write:                                    # a writes file in either shape, read in the grid one
        _, fields, rd = capture_layout.write_rows(path)
    else:
        rd = capture_layout.read_rows(path)
        fields = list(rd[0]) if rd else []
    lanes = lanes_of(fields); ch = [c for c in COUNTS if c in fields] + lanes
    for r in rd:
        k = (r["query"], "write" if write else r["body"], r["param_idx"])
        for c in ch:
            v = int(float(r[c] or 0))
            if v: by_kind[k][kind_of(r["part"])][c] += v; by_part[k][r["part"]][c] += v
        by_kind[k]                                                     # a body with no nonzero part still exists
    if steps_path:
        for r in capture_layout.reader(open(steps_path)): by_kind[(r["query"], r["body"], r["param_idx"])]
    return by_kind, by_part, ch
def metrics(vec, ch, write):
    lanes = [c for c in ch if c not in COUNTS]
    read = [c for c in lanes if c in HIT_BYTE_LANES]
    wr = [c for c in lanes if c.startswith("write_")]
    m = {c: vec[c] for c in ch}
    m["wops"] = vec["G"] + 1.1 * vec["S"] + 0.01 * vec["N"] + vec["P"] + vec["D"]
    m["bytes"] = sum(vec[c] for c in read) + sum(vec[c] for c in wr)
    m["wops_bytes4k"] = m["wops"] + m["bytes"] / 4096
    return m
rows, differ, missing, summary = [], [], [], []
planted = None                                                             # --plant: (workload, query, body, param) of the changed row
for wl, ptmpl, stmpl, write in SOURCES:
    if not all(os.path.exists(ptmpl.format(c=cell(p, a))) for p in LAY for a in LAY):
        print(f"{wl}: no nine-cell per-part capture at {ptmpl}: skipped"); continue
    D = {(p, a): load(ptmpl.format(c=cell(p, a)), stmpl and stmpl.format(c=cell(p, a)), write) for p in LAY for a in LAY}
    ch = D[("split", "split")][2]
    if PLANT and planted is None:                                        # one props SEEK more in the Split/Split source
        k = next(k for k in sorted(D[("split", "split")][0]) if D[("split", "split")][0][k]["props"])
        part = next(pt for pt in sorted(D[("split", "split")][1][k]) if kind_of(pt) == "props")
        D[("split", "split")][0][k]["props"]["S"] += 1; D[("split", "split")][1][k][part]["S"] += 1
        planted = (wl,) + k
    idx_differs = 0
    for p in LAY:
        for a in LAY:
            if p == a: continue
            meas, meas_parts, _ = D[(p, a)]; dp, dp_parts, _ = D[(p, p)]; da, da_parts, _ = D[(a, a)]
            agg = collections.defaultdict(lambda: [collections.Counter(), collections.Counter(), 0, 0])   # (query, body) -> composed, measured, params, equal params
            for k in sorted(meas):
                if k not in dp or k not in da:
                    missing.append([wl, cell(p, a), k[0], k[1], k[2], "" if k in dp else cell(p, p), "" if k in da else cell(a, a)]); continue
                composed = collections.Counter(); composed.update(dp[k]["props"]); composed.update(da[k]["adj"]); composed.update(dp[k]["index"])
                if dp[k]["index"] != da[k]["index"]: idx_differs += 1
                measured = collections.Counter()
                for v in meas[k].values(): measured.update(v)
                pm, mm = metrics(composed, ch, write), metrics(measured, ch, write)
                eq = all(pm[c] == mm[c] for c in ch)
                rows.append([wl, cell(p, a), k[0], k[1], k[2], int(eq)] + [x for c in ("S", "G", "N", "wops", "bytes", "wops_bytes4k") for x in (round(pm[c], 2), round(mm[c], 2))])
                g = agg[(k[0], k[1])]; g[0].update(composed); g[1].update(measured); g[2] += 1; g[3] += eq
                if not eq:
                    bad = [c for c in ch if pm[c] != mm[c]]
                    kinds = [kd for kd, src in (("props", dp), ("adj", da), ("index", dp)) if src[k][kd] != meas[k][kd]]
                    src_parts = {}
                    for pt, v in dp_parts[k].items():
                        if kind_of(pt) != "adj": src_parts[pt] = v
                    for pt, v in da_parts[k].items():
                        if kind_of(pt) == "adj": src_parts[pt] = v
                    parts_bad = sorted(pt for pt in set(src_parts) | set(meas_parts[k]) if src_parts.get(pt, {}) != meas_parts[k].get(pt, {}))
                    differ.append([wl, cell(p, a), k[0], k[1], k[2], "+".join(kinds), ";".join(parts_bad)[:300], ";".join(f"{c}:{pm[c]}/{mm[c]}" for c in bad)[:400],
                                   round(pm["wops"] / mm["wops"], 5) if mm["wops"] else "", round(pm["bytes"] / mm["bytes"], 5) if mm["bytes"] else "", round(pm["wops_bytes4k"] / mm["wops_bytes4k"], 5) if mm["wops_bytes4k"] else ""])
            n = len(agg); eqb = sum(g[2] == g[3] for g in agg.values())
            tp = collections.Counter(); tm = collections.Counter()
            for g in agg.values(): tp.update(g[0]); tm.update(g[1])
            pm, mm = metrics(tp, ch, write), metrics(tm, ch, write)
            nr = sum(g[2] for g in agg.values()); er = sum(g[3] for g in agg.values())
            summary.append([wl, cell(p, a), n, eqb, nr, er, round(pm["wops"] / mm["wops"], 6) if mm["wops"] else "", round(pm["bytes"] / mm["bytes"], 6) if mm["bytes"] else "",
                            round(pm["wops_bytes4k"] / mm["wops_bytes4k"], 6) if mm["wops_bytes4k"] else ""])
    s = [x for x in summary if x[0] == wl]
    print(f"{wl:17s}: 6 cells, (query, body) {sum(x[2] for x in s):5d}, all params exactly equal {sum(x[3] for x in s):5d}; (query, body, param) rows {sum(x[4] for x in s):6d}, exactly equal {sum(x[5] for x in s):6d}; "
          f"index parts differ between the two diagonal sources: {idx_differs}; channels compared: {len(ch)}")
for name, header, data in (("rows", "workload cell query body param exact S_priced S_measured G_priced G_measured N_priced N_measured wops_priced wops_measured bytes_priced bytes_measured wops_bytes4k_priced wops_bytes4k_measured", rows),
                           ("differ", "workload cell query body param kinds_that_differ parts_that_differ channels_priced/measured ratio_wops ratio_bytes ratio_wops_bytes4k", differ),
                           ("missing", "workload cell query body param not_run_on_props_source not_run_on_adjacency_source", missing),
                           ("summary", "workload cell query_bodies query_bodies_exact rows rows_exact priced_over_measured_wops priced_over_measured_bytes priced_over_measured_wops_bytes4k", summary)):
    with open(OUT + name + ".csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(header.split()); w.writerows(data)
print(f"rows {len(rows)}, differ {len(differ)}, missing {len(missing)}")
print("workload cell | (query, body): all, exact | rows: all, exact | priced / measured: wops, bytes, wops_bytes4k")
for x in summary: print("  " + " | ".join(str(v) for v in x))
g = collections.Counter((d[0], d[1], d[2], d[5], d[6][:80]) for d in differ)
print("DIFFER by (workload, cell, query, kinds, parts): params")
for k, v in sorted(g.items()): print("  ", k, v)
g = collections.Counter((m[0], m[1], m[2], m[3], m[5], m[6]) for m in missing)
print("MISSING by (workload, cell, query, body, not on props source, not on adjacency source): params")
for k, v in sorted(g.items()): print("  ", k, v)
if PLANT:
    caught = planted is not None and any((d[0], d[2], d[3], d[4]) == planted for d in differ)
    print(f"CONTROL: one SEEK planted in props_split_adj_split at {planted}: {'caught' if caught else 'NOT CAUGHT'} ({len(differ)} rows differ)")
print(f"RESULT: {'FAIL' if differ or not rows else 'PASS'} ({len(rows)} rows compared over {len({(r[0], r[1]) for r in rows})} (workload, cell), {len(differ)} differ, {len(missing)} not priced)")
sys.exit(1 if differ or not rows else 0)
