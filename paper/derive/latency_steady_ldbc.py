#!/usr/bin/env python3
"""Derive latency-steady-ldbc's table: per LDBC workload (I01: its 19 reads and IU1-8; BI's 17 queries) and store, the
steady latency, its KV time and its combined cost (wops_bytes4k), each summed over the queries by frequency and divided
by the same sum on Sp-Co.

The wall of record is kvcost.latency.steady_table's: per (query, param) the mean over the measured
passes from the second (pass 0 follows the store's own warm-up), per query the mean over the params. An update runs once, in the
write run (on each store after the read run's warm-up, under the same cache settings), and joins the reads in one sum
weighted by I01's frequencies (kvcost.latency.i01.weights). BI is weighted by its equal-share file (SF3's at every
scale), checked at SF3: Sp-Co's weighted combined total is the query count. The combined cost is the timed run's own
counts (each plan row's), checked equal to the counts capture's row for the body timed (picks.json) where the tree holds
it. The median run's ratios and the min and max per-pass wall ratio are kept beside the mean.

Reads one output tree (--from, --scale; the paper's SF3 run by default): its timings/ldbc/sf<scale>/{interactive,bi}/
and the writes beside them in timings/ldbc/sf<scale>/writes/meta-<store>/after-steady-warmup-<MB>mb-p1-noflush/ (kvcost
measure-latency --dataset ldbc=<scale> --workload i01|b01 <the 12 layouts>; I01's writes timed too), and grid-latency's picks.json and the
counts captures in the same tree when there; a workload the tree does not hold gives no rows. Writes
build/data/latency-steady-ldbc.csv (or --tables), one row per (workload, store).

    uv run python paper/derive/latency_steady_ldbc.py [--from output --dataset ldbc=0.1 --tables DIR]
"""
import argparse

from kvcost.latency import steady_share, steady_table, i01
from kvcost.layout_cost import query_weighting as QW
from kvevaluator.output_tree import Tree, capture_in, place
from snapshots import add_tree_args, scale_in, tree_of, write_table

REFERENCE = "props_split_adj_condensed"
UNITS = (("interactive", "ldbc_frequency"), ("bi", "one_over_sp_co_combined"))   # (workload, its weighting)
COLUMNS = ["workload", "store", "kind", "weights", "reference", "wall_ratio", "kv_ratio", "kv_share", "cost_ratio",
           "wall_ratio_median", "kv_ratio_median", "wall_ratio_pass_min", "wall_ratio_pass_max"]
def root_timing(run, write_run, workload, scale, weights):
    """({store: steps.csv}, {store: per_query output, IU1-8's walls added for Interactive}, {store: pass_band's fixed
    walls}) of one run's tree (its timings of `workload`, the body each store timed (grid-latency/<workload>/picks.json)
    and that body's counts in its captures; the writes in the write run's tree), each store over the run's reference"""
    tree, sf = Tree(run), scale[len("sf"):]
    dataset = "ldbc"
    timings = tree.timings(workload, sf)
    regime = steady_share.wall_regime(timings)
    stores = steady_table.stores_of(timings, regime)
    if REFERENCE not in stores:
        raise SystemExit(f"{timings}: the reference {REFERENCE} is not among the stores timed ({sorted(stores)})")
    counts = steady_table.capture_costs(tree.summary(dataset, sf, "grid-latency", workload) / "picks.json",
                                        capture_in(tree.place_dir("captures", dataset, sf), workload))
    data = {store: steady_table.per_query(p, counts.get(store)) for store, p in stores.items()}
    fixed = {store: {} for store in data}
    if workload == "interactive":
        write_timings = Tree(write_run).timings("writes", sf)
        write_name = steady_share.write_regime(write_timings)
        if steady_share.cache_mb(write_name) != steady_share.cache_mb(regime):
            raise SystemExit(f"{write_timings}: the write run's cache is not the read run's ({regime})")
        written = sorted(p.parents[1].name[len("meta-"):] for p in write_timings.glob(f"meta-*/{write_name}/steps.csv"))
        if written != sorted(data):
            raise SystemExit(f"{write_timings}: the write run's stores {written} are not the read run's {sorted(data)}")
        for store in data:
            writes = steady_table.write_per_query(write_timings / f"meta-{store}" / write_name / "steps.csv")
            if sorted(writes) != sorted(q for q in weights if q.startswith("iu")):
                raise SystemExit(f"{store}: writes {sorted(writes)}, not IU1-8")
            data[store].update(writes)
        for store in data:
            fixed[store] = {q: (data[store][q][0], data[REFERENCE][q][0]) for q in weights if q.startswith("iu")}
        print(f"[writes] {write_timings}: {len(written)} stores")
    n = sum(steady_table.plan_rows(p) for s, p in stores.items() if s in counts)
    print(f"{workload}: {regime}, {len(data)} stores, {len(weights)} queries; the cost from each plan row's own counts; "
          f"{n} of {sum(steady_table.plan_rows(p) for p in stores.values())} plan rows compared with the counts capture"
          f"{', all equal' if n else ''} ({len(counts)} of {len(stores)} stores have counts in this tree)")
    return stores, data, fixed

def main():
    ap = argparse.ArgumentParser(description="latency-steady-ldbc's table, from one tree's steady latency study")
    add_tree_args(ap)
    a = ap.parse_args()
    scale = place("ldbc", scale_in(a, "ldbc")).name                  # sf3, sf0.1
    run = write_run = tree_of(a.tree).root   # the reads and the writes: the tree's timings (kvcost measure-latency Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k)
    print(f"[run] {run}, scale {scale}")
    rows = []
    for workload, weighting in UNITS:
        if not Tree(run).timings(workload, scale[len("sf"):]).is_dir():
            print(f"{workload}: no timings in this run's tree, no rows")
            continue
        if workload == "interactive":
            weights = i01.weights()     # its reads and IU1-8: each store must hold every one (steady_table.ratios)
        else:
            weights = QW.equal_share_frequencies("b01")
        timed = [root_timing(run, write_run, workload, scale, weights)]
        if workload == "bi" and scale == "sf3":   # the file is 1 / Sp-Co's SF3 costs: elsewhere the total is not the count
            for _, data, _ in timed:
                QW.check_reference_total(weights, {q: v[2] for q, v in data[REFERENCE].items()}, "latency_steady_ldbc bi")
        for store, (i, r) in sorted(steady_table.ratios_by_root([data for _, data, _ in timed], REFERENCE, weights).items()):
            stores, _, fixed = timed[i]
            band = steady_table.pass_band(stores[store], stores[REFERENCE], weights, fixed[store])
            rows.append({"workload": workload, "store": store, "kind": "mix" if store.startswith("lat_") else "layout",
                         "weights": weighting, "reference": REFERENCE, **{k: f"{v:.6f}" for k, v in r.items()},
                         "wall_ratio_pass_min": f"{band[0]:.6f}" if band else "",
                         "wall_ratio_pass_max": f"{band[1]:.6f}" if band else ""})
    if not rows:
        raise SystemExit(f"{run}: no timings of {[w for w, _ in UNITS]}, no rows")
    write_table("latency-steady-ldbc", COLUMNS, rows, key=None, out_dir=a.tables)


if __name__ == "__main__":
    main()
