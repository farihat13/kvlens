#!/usr/bin/env python3
"""JOB's steady latency table at the full IMDb tag: per store, the 113 queries' wall, KV time and combined cost
(wops_bytes4k) over Sp-Co's, each query weighted 1 / its combined cost on Sp-Co (equal share: the same frequencies as
JOB's cost results), with the plain sum of the 113 walls (each query weighted equally) beside it.

The rule is kvcost.latency.steady_table's (latency-steady-ldbc's): per query the mean over its measured
passes from the second (1-9 in that snapshot; pass 0 left out: it follows each store's own warm-up; the 1-pass control keeps its run) of the wall (JOB has one param per query), and of its KV steps' wall, from the raw steady.json; the median run's
ratio (the `__plan__` row) beside it, and the band of the per-pass ratio (empty where the
capture does not record its pass order); and the cost of the body timed, from the timed run's own counts, checked
equal to the counts capture's where the tree holds it. The weights are JOB's equal-share frequency file (inputs/workloads/imdb/equal-share/j01.json, the
one every model reads), and Sp-Co's weighted combined total from this run's counts must be the query count (else exit 1).

Reads paper-results' trees of the run (UNITS: the tree itself, every store it holds timings of (kvcost measure-latency
--workload j01 on the latency figure's 12 layouts, and any mix timed beside them: the paper's run timed three), and its
control beside Sp-Co's first timing (<regime>-control); each one's timings, its grid-latency/job/picks.json, and the
counts in the tree's captures when there),
and the weights file. Refused: a store without every query the weights file names (113), no timing of Sp-Co, or no timing of a layout --layouts names. Prints the README's table and writes <output dir>/summary/imdb-rows/full/job-latency-table/job-steady-latency.csv.

    uv run --locked python experiments/job/steady_latency_table.py [--snapshot <id>] [--layouts Sp-Sp Co-Co ...]
"""
import argparse
import csv
import math
import sys

from kvcost.latency import steady_share, steady_table
from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_cost.settings import name_of
from kvevaluator import layouts as L
from kvevaluator.output_tree import Tree, capture_in
from kvevaluator.paths import paper_results_dir

PIN = "job-latency"   # the paper's run: its stores (the 12 layouts and three mixes), and its control beside Sp-Co's first timing
REFERENCE = "props_split_adj_condensed"
# per run: its regime dir's suffix after the steady wall regime (steady-<MB>mb-p2, found in the timings: the cache
# depends on the scale) and its tag in the table; a control re-times a store already timed (its own row)
UNITS = {
    # (the suffix, its tag): the stores of one run (11-08/09), and Sp-Co again at its end (the drift control:
    # the same regime dir with -control, kvcost measure-latency --drift-control)
    "job-latency": (
        ("", "11-08/09"),
        ("-control", "11-09, control")),
}
# a timed mix's label by its store (the paper's run timed these three beside the 12 layouts)
MIXES = {"lat_j01_measured_mix_any_size": "the JOB mix", "lat_j01_bill_mix_any_size": "the bill mix",
         "lat_j01_predicted_mix_any_size": "the predicted mix"}
COLUMNS = ["store", "layout", "timed", "queries", "share_wall_ratio", "share_cost_ratio", "share_kv_ratio",
           "share_kv_share", "equal_wall_s", "equal_wall_ratio", "equal_cost_ratio", "share_wall_ratio_median",
           "share_wall_ratio_pass_min", "share_wall_ratio_pass_max"]


def label(timing_set, store):
    """the store's layout in short names, every Chunked side with its size"""
    if store.startswith("lat_"):
        return MIXES[store]
    return name_of(store)


def load(snapshot_id, root=None, scale="full"):
    """[(tag, store, its tree, per_query, steps.csv)] over one run's trees (UNITS, below `root`: an output tree, default
    paper-results; IMDb at `scale`), the number of plan rows read, and of those compared with a counts capture"""
    out, plan_rows, compared = [], 0, 0
    root = paper_results_dir() if root is None else root
    tree = Tree(root)
    timings = tree.timings("job", scale)
    wall = steady_share.wall_regime(timings)       # steady-<MB>mb-p2: <MB> is the scale's steady cache (store_config)
    for suffix, tag in UNITS[snapshot_id]:
        regime = wall + suffix
        stores = steady_table.stores_of(timings, regime)
        for store, path in sorted(stores.items()):
            counts = steady_table.capture_costs(tree.summary("imdb", scale, "grid-latency", "job") / "picks.json",
                                                capture_in(tree.place_dir("captures", "imdb", scale), "job")).get(store)
            out.append((tag, store, regime, steady_table.per_query(path, counts), path))
            plan_rows += steady_table.plan_rows(path)
            compared += steady_table.plan_rows(path) if counts is not None else 0
    return out, plan_rows, compared


def table(snapshot_id, root=None, scale="full", layouts=()):
    """(rows, info): the table of one snapshot in UNITS (below `root`, default paper-results), each row a dict over COLUMNS with its floats as "%.6f" text, in the
    table's order: one row per store the run timed; info holds what main prints (the counts read and Sp-Co's
    equal-share total). Refused: a store without the 113 queries, no timing of Sp-Co (the reference), or no timing of
    one of `layouts` (names as kvcost takes them: the figure's chain names its 12). The paper's derive step
    (paper/derive/latency_steady_job.py) calls it too."""
    control_order = [tag for _, tag in UNITS[snapshot_id] if "control" in tag]
    rows, plan_rows, compared = load(snapshot_id, root, scale)
    main_rows = [r for r in rows if "control" not in r[0]]
    share = QW.equal_share_frequencies("j01")
    short = [(r[1], len(r[3])) for r in rows if set(r[3]) != set(share)]
    if short:
        sys.exit(f"want the weights file's {len(share)} queries on every store: {short}")
    timed = {r[1] for r in main_rows}
    missing = [x for x in dict.fromkeys(["Sp-Co", *layouts]) if L.cell_of(x) not in timed]
    if missing:
        sys.exit(f"no timing of {', '.join(missing)} among the {len(timed)} stores timed: {sorted(timed)}")
    ((ref, ref_path),) = [(d, p) for _, s, _, d, p in main_rows if s == REFERENCE]
    equal = {q: 1.0 for q in ref}

    # each query costs 1 on Sp-Co in this run's counts too: the file is 1 / these costs
    sp_co_costs = {q: ref[q][2] for q in ref}
    if scale == QW.equal_share("j01")["equal_share"]["scale"]:     # 1 / Sp-Co's costs holds at the file's own scale only
        sp_co_share = QW.check_reference_total(share, sp_co_costs, "steady_latency_table")
    else:
        sp_co_share = math.fsum(share[q] * sp_co_costs[q] for q in share)

    out = []
    for tag, store, timing_set, d, path in rows:
        s, e = steady_table.ratios(d, ref, share), steady_table.ratios(d, ref, equal)
        band = steady_table.pass_band(path, ref_path, share)
        out.append({"store": store, "layout": label(timing_set, store), "timed": tag, "queries": len(d),
                    "share_wall_ratio": s["wall_ratio"], "share_cost_ratio": s["cost_ratio"],
                    "share_kv_ratio": s["kv_ratio"], "share_kv_share": s["kv_share"],
                    "equal_wall_s": sum(v[0] for v in d.values()) / 1e9, "equal_wall_ratio": e["wall_ratio"],
                    "equal_cost_ratio": e["cost_ratio"], "share_wall_ratio_median": s["wall_ratio_median"],
                    "share_wall_ratio_pass_min": band[0] if band else "", "share_wall_ratio_pass_max": band[1] if band else ""})
    out.sort(key=lambda r: (control_order.index(r["timed"]) if "control" in r["timed"] else -1, r["share_wall_ratio"], r["store"]))
    text = [{k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in r.items()} for r in out]
    return text, {"stores": len(main_rows), "controls": len(rows) - len(main_rows), "plan_rows": plan_rows, "compared": compared,
                  "weights": len(share), "sp_co_share": sp_co_share}


def main():
    ap = argparse.ArgumentParser(description="JOB's steady latency table, from the pinned snapshot or a named one")
    ap.add_argument("--snapshot", default=PIN, choices=sorted(UNITS), help=f"the snapshot id (default: the pin, {PIN})")
    ap.add_argument("--layouts", nargs="*", default=[], metavar="LAYOUT",
                    help="layouts the run must hold timings of, else refused (default: Sp-Co alone; every store timed is a row)")
    a = ap.parse_args()
    print(f"[snapshot] {a.snapshot}" + ("" if a.snapshot == PIN else " (named, not the pin)"))
    table_rows, info = table(a.snapshot, layouts=a.layouts)

    out = Tree().summary("imdb", "full", "job-latency-table")
    out.mkdir(parents=True, exist_ok=True)
    name = "job-steady-latency.csv" if a.snapshot == PIN else f"job-steady-latency--{a.snapshot.rsplit('/', 1)[-1]}.csv"
    with open(out / name, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(table_rows)

    print(f"[read] {info['stores']} stores + {info['controls']} control runs, {info['weights']} queries each, {info['plan_rows']} plan rows; "
          f"{info['compared']} compared with the counts capture" + (", all equal" if info['compared'] else ""))
    print(f"[weights] {info['weights']} queries, the equal-share file; Sp-Co's equal-share cost total "
          f"{info['sp_co_share']:.6f} ({info['weights']} expected)")
    print("\n| store | layout | timed | wall, equal share / Sp-Co | combined, equal share / Sp-Co | KV time, equal share "
          "/ Sp-Co | KV share of the wall | sum of 113 walls (each query weighted equally) | / Sp-Co |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in table_rows:
        f = {k: float(r[k]) for k in ("share_wall_ratio", "share_cost_ratio", "share_kv_ratio", "share_kv_share",
                                       "equal_wall_s", "equal_wall_ratio")}
        print(f"| `{r['store']}` | {r['layout']} | {r['timed']} | {f['share_wall_ratio']:.2f} | {f['share_cost_ratio']:.2f} "
              f"| {f['share_kv_ratio']:.2f} | {f['share_kv_share']:.3f} | {f['equal_wall_s']:.2f} s | "
              f"{f['equal_wall_ratio']:.3f} |")
    print(f"\nwrote {out / name} ({len(table_rows)} rows)")


if __name__ == "__main__":
    main()
