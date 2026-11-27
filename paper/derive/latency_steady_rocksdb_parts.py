#!/usr/bin/env python3
"""Derive latency-steady-rocksdb-parts' table: per workload (LDBC Interactive and BI at SF3, JOB on IMDb full) and timed
store, the steady time split into three parts, each summed at the workload's weights over its KV-timer passes:
rocksdb_ns (the time inside RocksDB calls), kv_other_ns (the rest of the steps that read the store) and non_kv_ns (the
other steps), the timer's cost taken out (kvcost.latency.steady_share). rocksdb_ns is checked equal to the run's
grid-latency/<workload>/rocksdb_share.csv.

Per run, from the raw steady.json of the steady-<MB>mb-kvtimer capture beside each store's wall capture: the timer's
cost is the timer pair's ns x the KV calls; RocksDB = the KV steps' kv_call_ns - cost / 2; the KV steps' time = their
wall_ns - cost; the wall = work_ns + overhead_ns - cost. Per query the mean over its params and runs; per store the
weighted sum. The weights: Interactive by I01's frequencies (IU1-8 were not KV-timed and are left out), BI and JOB by
their equal-share files; SF3's at every scale.

Reads one output tree (--from, --scale; the paper's runs by default): its timings/<dataset>/<scale>/<workload>/ and
summary/<dataset>/<scale>/grid-latency/<workload>/rocksdb_share.csv for LDBC's two and JOB's (IMDb full), as kvcost
measure-latency <the 12 layouts> --rocksdb-share writes them; a workload the tree does not hold, or whose stores have no KV-timer capture
(run/latency-cells.sh --kvtimer-passes N), gives no rows and is named. Writes
build/data/latency-steady-rocksdb-parts.csv (or --tables): 45 rows on the paper's runs (15 stores x 3 workloads).

    uv run python paper/derive/latency_steady_rocksdb_parts.py [--from output --dataset ldbc=0.1 --tables DIR]
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict

from kvcost.latency import steady_share as SS, steady_table as T
from kvevaluator.capture import capture_layout
from snapshots import add_tree_args, scale_in, tree_of, write_table

COLUMNS = ["workload", "store", "weights", "queries", "weight_sum", "rocksdb_ns", "kv_other_ns", "non_kv_ns"]
# LDBC's units in its run: (workload, the timing unit's dir, its workload dir name there, the weights)
LDBC_UNITS = (("interactive", "frequencies:ldbc/3/i01"), ("bi", "equal-share:b01"))   # (workload, its weights)
REFERENCE = "props_split_adj_condensed"   # timed on every host of a split run: the first host's is its row
ROCKS_TOL = 1e-9    # rocksdb_ns against the run's rocks_ns (written to 6 decimals), relative


def parts_by_query(kvtimer_dir, arm):
    """{query: (RocksDB, KV steps, wall)} of one KV-timer capture, each the mean over the params and runs, the timer's
    cost out (module docstring)"""
    pair = T.timer_pair_ns(kvtimer_dir)
    per_q = defaultdict(list)
    for (q, _p), path in T._raw_cells(kvtimer_dir, arm, None).items():
        for m in T.raw_runs(json.load(open(path))):
            kv_steps = [s for s in m["steps"] if s.get("kind") == "kv"]
            kv = [s["kv"] or {} for s in kv_steps]
            cost = pair * sum(k.get(c, 0) for k in kv for c in T.TIMER_CALLS)
            t = m["totals"]
            per_q[q].append((sum(k.get("kv_call_ns", 0) for k in kv) - cost / 2,
                             sum(s["wall_ns"] for s in kv_steps) - cost,
                             t["work_ns"] + t.get("overhead_ns", 0) - cost))
    return {q: tuple(statistics.fmean(r[i] for r in runs) for i in range(3)) for q, runs in per_q.items()}


def number(v):
    return str(v) if isinstance(v, int) else f"{v:.1f}"


def root_rows(workload, timings, spec, share_csv):
    """{store: its row} of one run's stores (a workload's timings in its tree), rocksdb_ns checked against its share CSV"""
    regime = SS.wall_regime(timings)
    stores = T.stores_of(timings, regime)
    first = sorted(stores.values())[0]
    timed = {r["query"] for r in capture_layout.reader(open(first)) if r["step"] == "__plan__"}
    weights = SS.timed_weights(SS.weights_of(spec), timed)
    with open(share_csv, newline="") as fh:
        rocks_pinned = {r["store"]: float(r["rocks_ns"]) for r in csv.DictReader(fh)}
    if set(rocks_pinned) != set(stores):
        raise SystemExit(f"{share_csv}: stores {sorted(set(rocks_pinned) ^ set(stores))} not in both it and {timings}")
    rows = {}
    for store, path in sorted(stores.items()):
        wall_dir = path.parent
        kvt = SS.timer_capture(wall_dir, "kvtimer")
        if kvt is None:
            raise SystemExit(f"{store}: no kvtimer capture beside {wall_dir}")
        by_q = parts_by_query(kvt, wall_dir.parent.name)
        if set(by_q) != set(weights):
            raise SystemExit(f"{kvt}: queries {sorted(set(by_q) ^ set(weights))} not in both it and the weights")
        rocks, kv, wall = (sum(w * by_q[q][i] for q, w in weights.items()) for i in range(3))
        if abs(rocks - rocks_pinned[store]) > ROCKS_TOL * rocks_pinned[store]:
            raise SystemExit(f"{workload} {store}: rocksdb_ns {rocks!r}, the run's rocks_ns {rocks_pinned[store]!r}")
        rows[store] = {"workload": workload, "store": store, "weights": spec, "queries": len(weights),
                       "weight_sum": number(sum(weights.values())), "rocksdb_ns": number(rocks),
                       "kv_other_ns": number(kv - rocks), "non_kv_ns": number(wall - kv)}
    print(f"{workload}: {regime}, {len(rows)} stores, {len(weights)} queries at {spec}; rocksdb_ns equal to "
          f"{share_csv.name}'s rocks_ns on every store")
    return rows


def workload_rows(workload, roots, spec, n_stores):
    """the rows of one workload, store order, from its runs [(timings, share CSV)]: one row per store, the reference's
    from the first run (steady_table.row_roots); n_stores of them, when not None"""
    by_root = [root_rows(workload, timings, spec, share_csv) for timings, share_csv in roots]
    owner = T.row_roots([set(r) for r in by_root], REFERENCE)
    if n_stores is not None and len(owner) != n_stores:
        raise SystemExit(f"{workload}: {len(owner)} stores, not {n_stores}")
    return [by_root[i][store] for store, i in sorted(owner.items())]


def main():
    ap = argparse.ArgumentParser(description="latency-steady-rocksdb-parts' table, from one tree's steady latency studies")
    add_tree_args(ap)
    a = ap.parse_args()
    tree, sf, job_sf = tree_of(a.tree), scale_in(a, "ldbc"), scale_in(a, "imdb")
    run = job = tree           # the timings and shares of kvcost measure-latency Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k, in the tree itself
    print(f"[run] {run.root}, {job.root}")
    workloads = []   # (workload, its runs [(timings, share CSV)], the weights)
    for w, spec in LDBC_UNITS:
        workloads.append((w, [(run.timings(w, sf), run.summary("ldbc", sf, "grid-latency", w) / "rocksdb_share.csv")], spec))
    if job_sf is None:
        print("job: IMDb is not in this run (--scale): left out")
    else:
        workloads.append(("job", [(job.timings("job", job_sf),
                                   job.summary("imdb", job_sf, "grid-latency", "job") / "rocksdb_share.csv")], "equal-share:j01"))
    rows = []
    for w, roots, spec in workloads:
        if not roots[0][0].is_dir() or not roots[0][1].is_file():
            print(f"{w}: not in this tree ({roots[0][0] if not roots[0][0].is_dir() else roots[0][1]})")
            continue
        rows += workload_rows(w, roots, spec, None)
    if not rows:
        print("latency-steady-rocksdb-parts: no workload's KV-timer passes in this tree, no table")
        return
    write_table("latency-steady-rocksdb-parts", COLUMNS, rows, key=None, out_dir=a.tables)


if __name__ == "__main__":
    main()
