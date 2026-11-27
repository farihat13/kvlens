#!/usr/bin/env python3
"""Per workload family, which uniform layout is the cheapest per query.

Reads existing captures only (no store): I01's breakdown (LDBC IS / IC / IU) with the pick record for its bodies,
bi_picks.csv and job_picks.csv with the captures they were picked on, LinkBench's reads and writes. Per (family, query, cell) one
run's cost (the mean over its params) under three models, at the body the capture picked:
  wops          weighted operations: G + 1.1 S + 0.01 N of the reads (a write's own reads included) + PUTs + DELs
  bytes         key and value bytes read (missed keys included) + bytes written
  wops_bytes4k  wops + bytes / 4096
and beside them the raw channels they are made of (RAW; a write query's read lanes are its own reads), the body and the
source file. Every row is checked: wops and bytes rebuilt from the raw channels must match.
Writes <out>/costs.csv, wins.csv (queries a layout is the cheapest on, 1% tie), spread.csv (the family's best uniform
layout against each query's own best) and tables.md.  Usage: per_query_disagreement.py [--out DIR] [--job CSV] ..."""
import argparse
import csv
import os
import statistics
import collections
from kvevaluator.paths import ROOT, recorded_path
from kvevaluator.layouts import cell_id
from kvevaluator.output_tree import Capture, capture_in, writes_in

EXP = str(ROOT)
from kvcost.layout_cost import query_weighting as QW  # (sum / geomean: the one definition)
from kvcost.layout_cost import cost_models as CM  # (wops, bytes, wops_bytes4k: the one definition)
from kvcost.layout_cost import benchmarks, compose  # (a write's cost and the chunk view: the one definition)
from kvevaluator.measure.grid_capture import PICKS_FILE  # (the pick record's one path)
from kvevaluator.capture import capture_layout
from kvevaluator.plan_choice import pick_record
from kvevaluator.output_tree import Tree
CELLS = [cell_id(p, a) for p in ("condensed", "split", "chunked") for a in ("condensed", "split", "chunked")]
MODELS = CM.MODELS
TIE = 1.01
# the raw channels kept beside the three models, per run: KV ops, read byte lanes (miss keys included), written bytes,
# and a write query's own reads as ops (G + 1.1 S + 0.01 N) and bytes. For a write query S G N and the read lanes ARE its
# own reads (compose.CHANNELS' rule).
LANES = CM.READ_LANES
RAW = ("S", "G", "N", "P", "D") + LANES + ("write_key", "write_value", "write_bytes", "write_rmw_ops", "write_rmw_bytes")
WRITE_BODY = "canonical"   # a write query has one body: this report's label for it
# how a family's best uniform layout is chosen (its headline rule): BI and JOB define no frequencies, so their queries
# count by equal contribution (the geomean); the others by their weighted sum. --best-by once sums every family.
BEST_BY = {"BI": "geomean", "IMDb / JOB": "geomean"}


def name(cell):
    p, a = cell[len("props_"):].split("_adj_")
    return f"{p.capitalize()} / {a.capitalize()}"


def new_cost():
    return dict({"wops": 0.0, "bytes": 0.0, "body": set()}, **{k: 0.0 for k in RAW})


def wops_of(r):
    return CM.op_weight(float(r["S"]), float(r["G"]), float(r["N"]))


def ldbc(path, record):
    """(family, query) -> cell -> cost, and the query's weight in I01 (how often it runs); a read query's body from the
    pick record (picks.csv, the sf3 decision; check_cross_benchmark_numbers.py compares it with the pick margins'
    picks on the same grid, the bodies the breakdown priced)"""
    rec = pick_record.load(record)
    cost, weight = collections.defaultdict(lambda: collections.defaultdict(new_cost)), {}
    for r in capture_layout.reader(open(path)):
        if r["option"] not in CELLS:
            continue
        q = r["query"]
        fam = "LDBC " + ("IS" if q.startswith("is") else "IC" if q.startswith("ic") else "IU")
        c = cost[(fam, q)][r["option"]]
        c["wops"] += float(r["read_ops"]) + float(r["write_ops"]) + float(r["write_rmw_ops"])
        c["bytes"] += float(r["read_bytes"]) + float(r["write_bytes"]) + float(r["write_rmw_bytes"])   # read_bytes holds the miss keys
        for k in RAW:
            c[k] += float(r[k])
        c["body"].add(WRITE_BODY if fam == "LDBC IU" else rec.cell(pick_record.RECORD_WORKLOAD, r["option"])[q]["body"])
        weight[(fam, q)] = float(r["how_often"])
    for (fam, q) in list(cost):                      # the whole mix as one family too: I01's own best uniform layout
        cost[("LDBC, all of I01", q)] = cost[(fam, q)]; weight[("LDBC, all of I01", q)] = weight[(fam, q)]
    return cost, weight


def picks(path, fam, capture):
    """a picks file of the grid's rule (BI, JOB): one row per (cell, query), the picked body's totals over its params; the
    raw channels from the picked body's rows in `capture` (the capture dir it was picked on), checked against the picks
    file's own totals"""
    runs = collections.defaultdict(lambda: collections.defaultdict(float))
    cells = {r["cell"] for r in capture_layout.reader(open(path))}
    cap = Capture(capture)
    for cell in sorted(cells):
        for r in capture_layout.reader(open(cap.path_of("counts", cell))):
            if r["ok"] == "1":
                a = runs[(cell, r["query"], r["body"])]
                a["rows"] += 1
                for k in ("S", "G", "N", "read_bytes") + LANES:
                    a[k] += float(r[k])
    cost, weight = collections.defaultdict(dict), {}
    for r in capture_layout.reader(open(path)):
        n = float(r["nparam"] or 1)
        a = runs[(r["cell"], r["query"], r["pick"])]
        if a["rows"] != n or any(abs(a[k] - float(r[f"pick_{k}"])) > 1e-6 * max(1.0, a[k]) for k in ("S", "G", "N", "read_bytes")):
            raise SystemExit(f"{path}: {r['cell']} {r['query']} pick {r['pick']}: the runs/ rows ({a['rows']:.0f}, S {a['S']:.0f} "
                             f"G {a['G']:.0f} N {a['N']:.0f} bytes {a['read_bytes']:.0f}) do not match the picks file ({n:.0f}, "
                             f"S {r['pick_S']} G {r['pick_G']} N {r['pick_N']} bytes {r['pick_read_bytes']})")
        c = new_cost()
        c["wops"], c["bytes"] = float(r["pick_wops"]) / n, float(r["pick_read_bytes"]) / n
        for k in ("S", "G", "N") + LANES:
            c[k] = a[k] / n
        c["body"].add(r["pick"])
        cost[(fam, r["query"])][r["cell"]] = c
        weight[(fam, r["query"])] = 1.0
    return cost, weight


def linkbench(root, param_set, view):
    """LinkBench: per request type the mean per request; reads at the planner's body, writes with their own reads, each
    write as compose costs it (compose.write_vector under the chunk view `view`: the one definition of a write's cost);
    `root`: a captures/linkbench/<scale> dir"""
    cost, weight = collections.defaultdict(dict), {}
    for cell in CELLS:
        acc = collections.defaultdict(lambda: [new_cost(), 0])
        for r in capture_layout.reader(open(capture_in(root, "linkbench", param_set).path_of("counts", cell))):
            if r.get("planner_pick", "1") != "1" or r["ok"] != "1":  # one body per request type (a capture with a planner_pick column flags it 1)
                continue
            a = acc[r["query"]]; c = a[0]
            c["wops"] += wops_of(r); c["bytes"] += float(r["read_bytes"]); a[1] += 1
            for k in ("S", "G", "N") + LANES:
                c[k] += float(r[k])
            c["body"].add(r["body"])
        for row in capture_layout.write_rows(writes_in(root, "linkbench", param_set).path_of("counts", cell), cell=cell)[2]:
            r = compose.chunk_view(row, view, cell)
            wv = compose.write_vector(r)
            a = acc[r["query"]]; c = a[0]
            f = lambda k: float(r[k] or 0)
            written = wv["write_bytes"]
            c["wops"] += wops_of(r) + wv["write_ops"]
            c["bytes"] += wv["write_rmw_bytes"] + written; a[1] += 1
            # read_bytes = every read lane, misses included
            for k, v in (("S", f("S")), ("G", f("G")), ("N", f("N")), ("P", f("P")), ("D", f("D")),
                         *((x, f(x)) for x in LANES + CM.WRITE_LANES), ("write_bytes", written),
                         ("write_rmw_ops", wops_of(r)), ("write_rmw_bytes", wv["write_rmw_bytes"])):
                c[k] += v
            c["body"].add(WRITE_BODY)
        for q, (c, n) in acc.items():
            for k in ("wops", "bytes") + RAW:
                c[k] /= n
            cost[("LinkBench", q)][cell] = c
            if weight.setdefault(("LinkBench", q), float(n)) != n:     # the log's count: the same requests on every cell
                raise SystemExit(f"LinkBench {q}: {n} requests on {cell}, {weight[('LinkBench', q)]:.0f} on another cell")
    return cost, weight


def check_raw(k, cell, c):
    """wops and bytes rebuilt from the raw channels must match the loader's own sums (a lane or an op lost or counted twice)"""
    w = CM.op_weight(c["S"], c["G"], c["N"]) + c["P"] + c["D"]
    b = sum(c[x] for x in LANES) + c["write_bytes"]
    if abs(w - c["wops"]) > 1e-6 * max(1.0, w) or abs(b - c["bytes"]) > 1e-6 * max(1.0, b) or \
            abs(c["write_key"] + c["write_value"] - c["write_bytes"]) > 1e-6 * max(1.0, c["write_bytes"]):
        raise SystemExit(f"{k} {cell}: the raw channels give wops {w:.4f} bytes {b:.1f}, the loader {c['wops']:.4f} {c['bytes']:.1f}")


def shown(path):
    """a source path as written in the outputs (paths.recorded_path): relative to the output tree or the repo, naming
    no machine"""
    return recorded_path(os.path.abspath(path), Tree().root)


def value(c, model):
    return c["wops"] if model == "wops" else c["bytes"] if model == "bytes" else CM.wops_bytes4k(c["wops"], c["bytes"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "mix-benefit", "per-query-disagreement")))
    ap.add_argument("--ldbc", default=os.path.join(benchmarks.out_root("ldbc", "3", "wops_bytes4k"), "i01-rw-ldbc-mix", "breakdown.csv"))
    ap.add_argument("--ldbc-record", default=PICKS_FILE,
                    help="the pick record the breakdown's grid was priced with (LDBC read bodies)")
    ap.add_argument("--bi", required=True, help="the BI cells' bi_picks.csv")
    ap.add_argument("--bi-capture", required=True, help="the BI capture dir --bi was picked on")
    ap.add_argument("--job", default="", help="job_picks.csv of the IMDb / JOB grid")
    ap.add_argument("--job-capture", default="", help="the JOB capture dir --job was picked on")
    ap.add_argument("--linkbench", default="", help="LinkBench's captures: a captures/linkbench/<scale> dir, e.g. output/captures/linkbench/n1m")
    ap.add_argument("--linkbench-params", default=None, help="the LinkBench param set read (default: LinkBench's one "
                    "default, output_tree.default_param_set: log, the whole request log)")
    ap.add_argument("--best-by", default="headline", choices=("headline", "once"),
                    help="a family's best uniform layout: by its headline rule (BEST_BY), or by the weighted sum for every family")
    benchmarks.add_chunk_view_arg(ap)
    a = ap.parse_args()
    view = compose.resolve_chunk_view(a.chunk_view)
    cost, weight, source = {}, {}, {}
    for fam, loader, path in (("LDBC", lambda p: ldbc(p, a.ldbc_record), a.ldbc), ("BI", lambda p: picks(p, "BI", a.bi_capture), a.bi),
                              ("IMDb / JOB", lambda p: picks(p, "IMDb / JOB", a.job_capture), a.job),
                              ("LinkBench", lambda p: linkbench(p, a.linkbench_params, view), a.linkbench)):
        if not path:
            continue
        c, w = loader(path)
        cost.update(c); weight.update(w)
        for k in c:
            source[k[0]] = path
    os.makedirs(a.out, exist_ok=True)
    fams = sorted({f for f, _ in cost})
    with open(os.path.join(a.out, "costs.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["family", "query", "weight", "cell", "wops", "bytes", "wops_bytes4k"] + list(RAW) + ["body", "source"])
        for k in sorted(cost):
            missing = [c for c in CELLS if c not in cost[k]]
            if missing:
                raise SystemExit(f"{k}: no cost on {missing}")
            for c in CELLS:
                v = cost[k][c]
                check_raw(k, c, v)
                # 6 decimals: a LinkBench request costs ~2 wops, and its benchmark's near ties sit at 4e-5 relative
                w.writerow([k[0], k[1], weight[k], c, round(v["wops"], 6), round(v["bytes"], 6), round(CM.wops_bytes4k(v["wops"], v["bytes"]), 6)]
                           + [round(v[x], 6) for x in RAW] + ["+".join(sorted(v["body"])), shown(source[k[0]])])
    wins, spread, md = [], [], []
    for fam in fams:
        qs = sorted(q for f, q in cost if f == fam)
        md.append(f"\n### {fam}: {len(qs)} queries x {len(CELLS)} uniform layouts\n")
        md.append("| layout (props / adjacency) | cheapest on, wops | bytes | wops_bytes4k |")
        md.append("|---|---|---|---|")
        per_model = {}
        for m in MODELS:
            n = collections.Counter(); total = collections.Counter()
            for q in qs:
                best = min(value(cost[(fam, q)][c], m) for c in CELLS)
                for c in CELLS:
                    v = value(cost[(fam, q)][c], m)
                    if v <= best * TIE:
                        n[c] += 1
                    total[c] += weight[(fam, q)] * v
            rule = BEST_BY.get(fam, "sum") if a.best_by == "headline" else "sum"
            if rule == "sum":
                top = min(CELLS, key=lambda c: (total[c], c))
            else:
                top = QW.best(QW.aggregate({q: {c: value(cost[(fam, q)][c], m) for c in CELLS} for q in qs}, rule))
            ratios = sorted((value(cost[(fam, q)][top], m) / min(value(cost[(fam, q)][c], m) for c in CELLS), q) for q in qs
                            if min(value(cost[(fam, q)][c], m) for c in CELLS) > 0)
            rs = [r for r, _ in ratios]
            p90 = rs[min(len(rs) - 1, int(0.9 * (len(rs) - 1) + 0.5))]
            over = sum(1 for r in rs if r > 1.5)
            per_model[m] = (n, top, statistics.median(rs), p90, ratios[-1], over, len(rs), max(n.values()))
            for c in CELLS:
                wins.append([fam, m, c, n[c], len(qs)])
            spread.append([fam, m, top, len(qs), n[top], round(statistics.median(rs), 3), round(p90, 3), round(ratios[-1][0], 3), ratios[-1][1], over, rule])
        for c in CELLS:
            md.append(f"| {name(c)} | " + " | ".join(str(per_model[m][0][c]) for m in MODELS) + " |")
        md.append("")
        md.append("| model | the family's best uniform layout | cheapest on | its cost over a query's own best: median | p90 | max (query) | queries over 1.5x |")
        md.append("|---|---|---|---|---|---|---|")
        for m in MODELS:
            n, top, med, p90, mx, over, nr, _ = per_model[m]
            md.append(f"| {m} | {name(top)} | {n[top]} of {len(qs)} | {med:.2f}x | {p90:.2f}x | {mx[0]:.2f}x ({mx[1]}) | {over} of {nr} |")
    with open(os.path.join(a.out, "wins.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["family", "model", "cell", "queries_cheapest_on", "queries"]); w.writerows(wins)
    with open(os.path.join(a.out, "spread.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["family", "model", "best_uniform_cell", "queries", "cheapest_on", "ratio_median", "ratio_p90", "ratio_max", "ratio_max_query", "queries_over_1.5x", "best_by"])
        w.writerows(spread)
    head = ["# Per-query disagreement (way 6): which uniform layout is the cheapest per query", "",
            "Generated by `experiments/mix_benefit/per_query_disagreement.py`. A layout is \"cheapest on\" a query when its cost is",
            "within 1% of the lowest of the nine (ties count for each). The family's best uniform layout has the lowest score",
            ("(LDBC: the total weighted by how often I01 runs the query; LinkBench: by the request log; BI and JOB, which define no"
             " frequencies: the geomean over queries, equal contribution; `python/kvcost/layout_cost/query_weighting.py`)." if a.best_by == "headline" else
             "(LDBC: weighted by how often I01 runs the query; LinkBench: by the request log; BI and JOB: every query once)."),
            "Costs are those of the body each capture picked (its plan rule), read under three models.", "", "Sources:"]
    head += [f"- {f}: `{shown(source[f])}`" for f in sorted(source)]
    open(os.path.join(a.out, "tables.md"), "w").write("\n".join(head + md) + "\n")
    print(f"[per_query_disagreement] {len(cost)} queries in {len(fams)} families, {len(cost) * len(CELLS)} cells -> {a.out}")


if __name__ == "__main__":
    main()
