"""scale_transfer: does a layout chosen at a small scale hold at SF30? Per-query costs of the 49 uniform layouts per
source and their comparison with SF30's measured costs. Analysis only: opens no store.

Run from the artifact's root (its venv: `uv run --locked python`); the driver, run-scale-transfer.sh, beside this
file, calls each subcommand:

    measured   the 49 from a tree (--from; kvcost.layout_cost.uniform_49's load, as kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k), one
               row per (layout, query): the query's weighted cost (frequency x one run); checks that the per-query sums
               equal that command's totals (--check-against its place's layouts.csv), 49 of 49
    predicted  the same rows from a `kvcost cost-layouts --count-source <a predicted set>` place (frequency x one run)
    compare    --large (SF30 measured) against each --source: each side's per-query costs weighted into totals, compared
               layout by layout: the table row per panel and model, the points, the
               per-query ranks and the queries whose best layout moves. --weights FILE (a workload file, e.g. the
               equal-share file of kvcost.layout_cost.query_weighting) re-weights every side's rows by that file's
               frequencies: one set of weights on both axes (BI: its equal-share file, 1 / each query's measured
               combined cost on Sp-Co at SF3, the same file at every scale)
The figures (A and B) are drawn by the artifact's plot_predicted_vs_measured.py --totals from compare's
totals/<panel>.csv (x: SF30 measured; y: the source), the driver's plot stage.

Costs: wops = G + 1.1 S + 0.01 N (reads; a write adds its own reads, PUTs and DELETEs); bytes = key and value bytes read
(missed keys included) + bytes written; wops_bytes4k = wops + bytes / 4096 (kvcost.layout_cost.cost_models).
"""
import argparse
import collections
import csv
import math
import os
import sys

from kvcost.layout_cost import benchmarks, compose
from kvevaluator.layouts import SETTINGS, cell_id
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_cost import uniform_49 as U
from kvcost.layout_search import search as cost_cache
from kvcost.plotting.stats import spearman
from kvcost.predict_accuracy.layouts import short
from kvevaluator.output_tree import Tree
from kvevaluator.paths import paper_results_dir

ROW = ["scale", "source", "workload", "cell", "layout", "query", "frequency", "ops", "bytes"]
MODELS = {"wops_bytes4k": lambda ops, b: CM.wops_bytes4k(ops, b), "wops": lambda ops, b: ops}
WITHIN = 1.05   # "within 5% of the best" (README: hypotheses)


def write_rows(path, rows, fields):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def cmd_measured(a):
    """the 49 per (layout, query), loaded and composed as kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k does (uniform_49.load_49)"""
    cost_cache.set_cache(None)
    src = Tree() if a.from_tree == "output" else Tree(paper_results_dir() if a.from_tree == "paper-results" else a.from_tree)
    grid = str(src.place_dir("captures", a.dataset, a.sf))     # each layout by output_tree.layout_capture
    sizes = U.other_sizes(grid, a.size)
    w, data, _ = U.load_49(grid, sizes, a.dataset, a.sf, a.workload, compose.resolve_chunk_view(a.chunk_view),
                           criterion=a.criterion)
    settings = list(SETTINGS) + [opt for opt, _, _ in sizes]     # the rows' order (adjacency outer), as before
    units = compose.workload_units(w, data)
    rows, totals = [], {}
    for adj in settings:
        for props in settings:
            cell = cell_id(props, adj)
            assign = {u: (props if u[0] == "props" else adj) for u in units}
            whole = compose.cost_mix(w, data, assign)
            if whole is None:
                sys.exit(f"measured: {cell}: a query has no plan valid on it")
            totals[cell] = compose.currencies(whole)
            for item in w["items"]:
                t = compose.cost_mix({**w, "items": [item]}, data, assign)
                cur = compose.currencies(t)
                rows.append({"scale": a.sf, "source": "measured", "workload": w["id"], "cell": cell, "layout": short(cell),
                             "query": item[1], "frequency": item[2], "ops": repr(cur["ops"]), "bytes": repr(cur["bytes"])})
    # the per-query rows must add up to the whole workload's cost, and to the artifact tool's own totals
    bad = []
    for cell, cur in totals.items():
        mine = [r for r in rows if r["cell"] == cell]
        for k in ("ops", "bytes"):
            s = math.fsum(float(r[k]) for r in mine)
            if abs(s - cur[k]) > 1e-9 * max(1.0, abs(cur[k])):
                bad.append(f"{cell} {k}: per-query sum {s} vs whole {cur[k]}")
    checked = 0
    if a.check_against:
        ref = {r["layout"]: r for r in LC.read_layouts(a.check_against, a.chunk_view or LC.VIEWS[0], kind="uniform")}
        for cell, cur in totals.items():
            if cell not in ref:
                bad.append(f"{cell}: not in {a.check_against}")
                continue
            checked += 1
            for k, col, digits in (("ops", "total_ops", 3), ("bytes", "total_bytes", 1)):
                if abs(round(cur[k], digits) - float(ref[cell][col])) > 10 ** -digits * 1.01:
                    bad.append(f"{cell} {k}: {cur[k]} vs the tool's {ref[cell][col]}")
        if len(ref) != len(totals):
            bad.append(f"{a.check_against}: {len(ref)} layouts, here {len(totals)}")
    write_rows(a.out, rows, ROW)
    for x in bad[:10]:
        print("   DIFF", x)
    print(f"scale_transfer measured: sf{a.sf} {w['id']}: {len(totals)} layouts x {len(w['items'])} queries = {len(rows)} rows; "
          f"per-query sums = totals; against the tool's totals {checked} layouts compared, {len(bad)} differ -> {a.out}")
    return 1 if bad or len(totals) != 49 or (a.check_against and checked != 49) else 0


def cmd_predicted(a):
    """kvcost cost-layouts --count-source <a predicted set>'s query costs (its place: --costs) as the same rows
    (frequency x one run), the half view, each query's frequency from the workload"""
    (w,) = benchmarks.select("ldbc", a.sf, a.workload).values()
    freq = LC.frequencies(w)
    order, costs = LC.read_query_costs(a.costs)
    rows = []
    for fq in order:
        for cell, r in sorted(costs[fq].items()):
            n = float(freq[fq])
            rows.append({"scale": a.sf, "source": a.label, "workload": a.workload, "cell": cell, "layout": short(cell),
                         "query": fq[1], "frequency": repr(n) if not n.is_integer() else str(int(n)),
                         "ops": repr(n * float(r["wops"])), "bytes": repr(n * float(r["bytes"]))})
    cells = {r["cell"] for r in rows}
    queries = collections.Counter(r["cell"] for r in rows)
    write_rows(a.out, rows, ROW)
    print(f"scale_transfer predicted: sf{a.sf} {a.label}: {len(cells)} layouts, {sorted(set(queries.values()))} queries "
          f"each, {len(rows)} rows -> {a.out}")
    return 0 if len(cells) == 49 and len(set(queries.values())) == 1 else 1


def load_costs(path, weights=None):
    """{(cell, query): (ops, bytes)} and the rows' scale, source, workload; weights {query: w}: each row re-weighted
    from its own frequency to w (its ops and bytes x w / frequency)"""
    out, meta = {}, set()
    with open(path) as f:
        for r in csv.DictReader(f):
            k = 1.0
            if weights is not None:
                if r["query"] not in weights:
                    sys.exit(f"{path}: query {r['query']} has no weight in --weights")
                k = weights[r["query"]] / float(r["frequency"])
            out[(r["cell"], r["query"])] = (k * float(r["ops"]), k * float(r["bytes"]))
            meta.add((r["scale"], r["source"], r["workload"]))
    if len(meta) != 1:
        sys.exit(f"{path}: {len(meta)} (scale, source, workload) sets, want 1")
    return out, meta.pop()


def read_weights(path):
    """{query: frequency} of a workload file (one item per query), and its id"""
    _, w = benchmarks.read_workload(path)
    if w.get("equal_share"):                            # an equal-share file: the tracked file's checks (model, chunk
        QW.equal_share_given(path, QW.equal_share_base(w["id"]))   # view, scale), and --weights names that file
    qs = [q for _, q, _, _ in w["items"]]
    if len(set(qs)) != len(qs):
        sys.exit(f"--weights {path}: a query appears twice")
    return {q: f for _, q, f, _ in w["items"]}, w["id"]


def totals_of(costs, model):
    """{cell: the workload's cost under model}: the model of the summed lanes (a sum of ops and bytes, then the model)"""
    ops, by = collections.defaultdict(list), collections.defaultdict(list)
    for (cell, _), (o, b) in costs.items():
        ops[cell].append(o)
        by[cell].append(b)
    return {c: MODELS[model](math.fsum(ops[c]), math.fsum(by[c])) for c in ops}


def ranks(costs):
    """{cell: rank, 1 = cheapest}, ties broken by the layout's name"""
    return {c: i + 1 for i, c in enumerate(sorted(costs, key=lambda c: (costs[c], short(c))))}


def cmd_compare(a):
    weights, a.weights_id = read_weights(a.weights) if a.weights else (None, None)
    large, (lsc, _, lwl) = load_costs(a.large, weights)
    panels, points, per_query, query_ranks = [], [], [], []
    totals = collections.defaultdict(list)   # panel -> the totals.csv rows (the source's cost as "predicted")
    for spec in a.source:
        panel, _, path = spec.partition("=")
        src, (ssc, ssrc, swl) = load_costs(path, weights)
        if swl != lwl:
            sys.exit(f"compare: {panel} costs workload {swl}, the large side {lwl}")
        cells = sorted({c for c, _ in large})
        if sorted({c for c, _ in src}) != cells or len(cells) != 49:
            sys.exit(f"compare: {panel}: {len({c for c, _ in src})} layouts, the large side {len(cells)}; want the same 49")
        queries = sorted({q for _, q in large})
        if sorted({q for _, q in src}) != queries:
            sys.exit(f"compare: {panel}: the queries differ from the large side's")
        if weights is not None and sorted(weights) != queries:
            sys.exit(f"compare: --weights names {len(weights)} queries, the rows {len(queries)}")
        for model in a.model:
            L, S = totals_of(large, model), totals_of(src, model)
            lr, sr = ranks(L), ranks(S)
            lbest, pick = min(L, key=lambda c: (L[c], short(c))), min(S, key=lambda c: (S[c], short(c)))
            near_src = [c for c in cells if S[c] <= WITHIN * S[pick]]
            near_both = [c for c in near_src if L[c] <= WITHIN * L[lbest]]
            rho = spearman([L[c] for c in cells], [S[c] for c in cells])
            panels.append({"panel": panel, "source": ssrc, "scale": ssc, "workload": lwl, "model": model,
                           "large_best": short(lbest), "pick": short(pick), "pick_over_large_best": f"{L[pick] / L[lbest]:.4f}",
                           "pick_rank_at_large": lr[pick], "spearman": f"{rho:.3f}", "within5_source": len(near_src),
                           "within5_also_at_large": len(near_both), "layouts": len(cells), "queries": len(queries)})
            for c in cells:
                totals[panel].append({"currency": model, "cell": c, "layout": short(c), "predicted": f"{S[c]:.6g}",
                                      "measured": f"{L[c]:.6g}"})
                points.append({"panel": panel, "model": model, "cell": c, "layout": short(c), "x": f"{L[c] / L[lbest]:.6g}",
                               "y": f"{S[c] / S[pick]:.6g}", "rank_large": lr[c], "rank_source": sr[c]})
            # per query: each layout's rank at both scales; the best layout of each side; the query's share of the
            # large side's cost at the large side's best layout
            ltot = L[lbest]
            for q in queries:
                lq = {c: MODELS[model](*large[(c, q)]) for c in cells}
                sq = {c: MODELS[model](*src[(c, q)]) for c in cells}
                lqr, sqr = ranks(lq), ranks(sq)
                lb, sb = min(lq, key=lambda c: (lq[c], short(c))), min(sq, key=lambda c: (sq[c], short(c)))
                per_query.append({"panel": panel, "model": model, "query": q, "large_best": short(lb), "source_best": short(sb),
                                  "moves": int(lb != sb), "source_best_over_large_best": f"{lq[sb] / lq[lb]:.4f}" if lq[lb] else "",
                                  "source_best_rank_at_large": lqr[sb],
                                  "share_of_large_cost": f"{lq[lbest] / ltot:.4f}",
                                  "spearman": f"{spearman([lq[c] for c in cells], [sq[c] for c in cells]) or float('nan'):.3f}"})
                for c in cells:
                    query_ranks.append({"panel": panel, "model": model, "query": q, "layout": short(c),
                                        "rank_large": lqr[c], "rank_source": sqr[c]})
    os.makedirs(a.out, exist_ok=True)
    write_rows(os.path.join(a.out, "panels.csv"), panels, list(panels[0]))
    write_rows(os.path.join(a.out, "points.csv"), points, list(points[0]))
    write_rows(os.path.join(a.out, "per_query.csv"), per_query, list(per_query[0]))
    write_rows(os.path.join(a.out, "query_ranks.csv"), query_ranks, list(query_ranks[0]))
    write_table(os.path.join(a.out, "panels.md"), panels, per_query, a)
    # one totals.csv per panel in the layout_choice report's columns, so the artifact's
    # plot_predicted_vs_measured.py --totals draws it (x: SF30 measured; y: the source)
    for panel in dict.fromkeys(p["panel"] for p in panels):
        write_rows(os.path.join(a.out, "totals", f"{panel}.csv"), totals[panel], ["currency", "cell", "layout", "predicted", "measured"])
    print(f"scale_transfer compare: {len(a.source)} panels x {len(a.model)} models = {len(panels)} rows; "
          f"{len(points)} points; {len(per_query)} (panel, model, query) rows -> {a.out}")
    for p in panels:
        print(f"   {p['panel']:<22} {p['model']:<13} pick {p['pick']:<12} {p['pick_over_large_best']}x of SF{lsc}'s best "
              f"({p['large_best']}), rank {p['pick_rank_at_large']}; Spearman {p['spearman']}; "
              f"within 5%: {p['within5_also_at_large']} of {p['within5_source']}")
    return 0 if panels and all(p["layouts"] == 49 for p in panels) else 1


def write_table(path, panels, per_query, a):
    """the table, one row per panel and model, and the queries whose best layout moves"""
    lines = [f"# scale_transfer: {panels[0]['workload']}, each source against SF30 measured", "",
             f"> Made by `scale_transfer.py compare` ({a.provenance}). Costs: wops = G + 1.1 S + 0.01 N; wops_bytes4k = "
             "wops + bytes / 4096. Each side's pick is its own cheapest layout of the 49."
             + (f" Every side's queries weighted by `{a.weights_id}` (--weights): one set of weights on both axes."
                if a.weights else ""), "",
             ("| panel | model | pick | its cost at SF30 over SF30's best | its rank at SF30 | SF30's best | Spearman (49) "
              "| within 5% at the source: also within 5% at SF30 |"), "|---|---|---|---|---|---|---|---|"]
    for p in panels:
        lines.append(f"| {p['panel']} | {p['model']} | {p['pick']} | {p['pick_over_large_best']}x | {p['pick_rank_at_large']} "
                     f"| {p['large_best']} | {p['spearman']} | {p['within5_also_at_large']} of {p['within5_source']} |")
    lines += ["", "Queries whose best layout differs between the source and SF30 (wops_bytes4k):", "",
              "| panel | query | SF30's best | the source's best | that one at SF30 over SF30's best | share of SF30's cost |",
              "|---|---|---|---|---|---|"]
    for q in per_query:
        if q["model"] == "wops_bytes4k" and q["moves"] == 1:
            lines.append(f"| {q['panel']} | {q['query']} | {q['large_best']} | {q['source_best']} | "
                         f"{q['source_best_over_large_best']}x | {float(q['share_of_large_cost']):.1%} |")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measured")
    m.add_argument("--dataset", required=True, choices=benchmarks.DATASETS)
    m.add_argument("--sf", required=True)
    m.add_argument("--from", dest="from_tree", default="output", help="the tree read: output (default), paper-results "
                   "or a path; each layout by output_tree.layout_capture, Chunked at the other sizes from its captures")
    m.add_argument("--size", action="append", default=None, metavar="NAME", help="the other chunk sizes (default every one)")
    m.add_argument("--workload", required=True)
    m.add_argument("--chunk-view", default=None)
    m.add_argument("--pick-by", dest="criterion", default="wops_bytes4k")
    m.add_argument("--check-against", help="kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's place (its layouts.csv) "
                   "for the same inputs")
    m.add_argument("--out", required=True)
    p = sub.add_parser("predicted")
    p.add_argument("--sf", required=True)
    p.add_argument("--costs", required=True, help="cost-layouts --count-source <set>'s place (its query_costs.csv)")
    p.add_argument("--label", required=True, help="the source's name in the rows: predicted-tier1, predicted-tier2")
    p.add_argument("--workload", required=True, help="the workload id the rows cost (its frequencies)")
    p.add_argument("--out", required=True)
    j = sub.add_parser("compare")
    j.add_argument("--large", required=True, help="SF30's measured rows (scale_transfer.py measured)")
    j.add_argument("--source", action="append", required=True, metavar="PANEL=ROWS_CSV")
    j.add_argument("--model", action="append", default=None, choices=list(MODELS))
    j.add_argument("--weights", help="a workload file whose frequencies re-weight every side's rows (both axes)")
    j.add_argument("--provenance", default="", help="the binary and commits, for the table's header")
    j.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "compare" and not a.model:
        a.model = list(MODELS)
    return {"measured": cmd_measured, "predicted": cmd_predicted, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
