"""The RocksDB share of steady latency, per store: a timing root's KV-timer and breakdown passes beside its wall passes.

The rule is steady_table's (timer_query, mean_walls, shares): per store, RocksDB time over mean busy time
(rocks_share, at perf level 2), the same over the mean wall (rocks_wall_share), its block-read part
(block_read_share) and the rest (other_rocks_share), each a ratio of weighted sums. rocks_share_pooled weights every
query 1, so the longest queries dominate it. A share is of mean busy time: never multiply it into a wall of record.
The weights (--weights): a workload's own frequencies (frequencies:<dataset>/<scale>/<id>) or its equal-share file
(equal-share:<id>); a weighted query not timed (an update) is left out and named, a timed query with no weight is
refused. kvtimer_tax and perf3_tax (CSV only) compare passes that differ in what the cache holds: neither is the
timer's cost.

A store with no kvtimer capture is refused; one with no breakdown capture gets no split. Writes one CSV row per store
to --out (the ratios, the weighted sums they divide, the passes and the binary), and prints the table.

    python -m kvcost.latency.steady_share --root <timing root> --workload <its dir name> --weights <spec> --out <file.csv>
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

from kvcost.latency import steady_table
from kvcost.layout_cost import benchmarks, query_weighting
from kvevaluator import layouts
from kvevaluator.capture import capture_layout

TIMER_KINDS = {"kvtimer": 2, "breakdown": 3}   # a timer capture's kind and the perf level it must run at
READ_REGIME = re.compile(r"steady-(\d+)mb-p2")                         # the timed read passes, <MB> the block cache
WRITE_REGIME = re.compile(r"after-steady-warmup-(\d+)mb-p1-noflush")   # the writes after the steady read warm-up (run B)
COLUMNS = ["store", "kind", "weights", "queries", "rocks_share", "rocks_share_pooled", "rocks_wall_share",
           "block_read_share", "other_rocks_share", "kvtimer_tax", "perf3_tax", "wall_passes", "kvtimer_passes",
           "breakdown_passes", "binary_git_sha", "rocks_ns", "busy_ns", "timer_wall_ns", "mean_wall_ns", "block_read_ns",
           "perf3_wall_ns", "pooled_rocks_ns", "pooled_busy_ns"]


def weights_of(spec):
    """{query: weight} of a --weights spec: frequencies:<dataset>/<scale>/<id> (the unit's items) or equal-share:<id>"""
    kind, _, name = spec.partition(":")
    if kind == "equal-share":
        return query_weighting.equal_share_frequencies(name)
    if kind == "frequencies" and name.count("/") == 2:
        dataset, scale, wid = name.split("/")
        (unit,) = benchmarks.select(dataset, scale, wid).values()     # select refuses an unknown id
        return {q: n for _, q, n, _ in unit["items"]}
    sys.exit(f"--weights {spec!r}: want frequencies:<dataset>/<scale>/<id> or equal-share:<id>, of one unit")


def timed_weights(weights, timed):
    """the weights of the timed queries: every timed query must have one; the weighted queries not timed are named"""
    missing = sorted(set(timed) - set(weights))
    if missing:
        sys.exit(f"timed queries with no weight: {missing}")
    dropped = sorted(set(weights) - set(timed))
    if dropped:
        print(f"[weights] not timed, left out: {', '.join(dropped)}")
    return {q: weights[q] for q in sorted(timed)}


def one_regime(dirs, pattern, what):
    """the one dir name among `dirs` (paths) that `pattern` matches; SystemExit for 0 or 2 or more names"""
    names = sorted({p.name for p in dirs if p.is_dir() and pattern.fullmatch(p.name)})
    if len(names) != 1:
        sys.exit(f"{what}: {len(names)} regime dirs named {pattern.pattern} ({names}), not one")
    return names[0]


def cache_mb(regime):
    """the block cache in MB a regime's dir name carries (READ_REGIME's or WRITE_REGIME's <MB>)"""
    return int((READ_REGIME.fullmatch(regime) or WRITE_REGIME.fullmatch(regime)).group(1))


def wall_regime(timings_dir):
    """the steady wall regime's dir name (steady-<MB>mb-p2) of one workload's timings (<timings>/meta-<store>/<regime>/);
    one, or SystemExit"""
    return one_regime(list(Path(timings_dir).glob("meta-*/*")), READ_REGIME, str(timings_dir))


def write_regime(writes_timings):
    """the dir name (after-steady-warmup-<MB>mb-p1-noflush) of the write workload's timed run B
    (<timings>/meta-<store>/<regime>/); one, or SystemExit"""
    return one_regime(list(Path(writes_timings).glob("meta-*/*")), WRITE_REGIME, str(writes_timings))


def steady_meta(capture_dir):
    """(the capture's meta, its steady regime block)"""
    with open(Path(capture_dir) / "steps.csv.meta.json") as fh:
        meta = json.load(fh)
    return meta, meta.get("regimes", {}).get("steady", {})


def timer_capture(wall_dir, kind):
    """the timer capture of `kind` beside a wall capture (None when absent), checked by its own meta: its timer kind,
    its perf level and the same binary as the wall capture"""
    d = wall_dir.parent / f"{wall_dir.name[: -len('-p2')]}-{kind}"   # <arm>/<regime>-<kind>/, beside the walls
    if not (d / "steps.csv").exists():
        return None
    meta, steady = steady_meta(d)
    wall_sha = steady_meta(wall_dir)[0]["provenance"]["binary_git_sha"]
    if (meta.get("timer_kind"), steady.get("perf_level")) != (kind, TIMER_KINDS[kind]) or \
            meta["provenance"]["binary_git_sha"] != wall_sha:
        sys.exit(f"{d}: timer kind {meta.get('timer_kind')!r} at perf level {steady.get('perf_level')}, binary "
                 f"{meta['provenance']['binary_git_sha'][:10]}: want {kind!r} at level {TIMER_KINDS[kind]}, binary {wall_sha[:10]}")
    return d


def store_row(spec, store, wall_dir, weights):
    """one store's row: the shares over its wall capture dir's sibling timer captures"""
    kvt_dir, brk_dir = timer_capture(wall_dir, "kvtimer"), timer_capture(wall_dir, "breakdown")
    if kvt_dir is None:
        sys.exit(f"{store}: no kvtimer capture beside {wall_dir}")
    arm = wall_dir.parent.name
    kvtimer = steady_table.timer_query(kvt_dir, arm)
    breakdown = steady_table.timer_query(brk_dir, arm) if brk_dir else None
    walls = steady_table.mean_walls(wall_dir, arm)
    s = steady_table.shares(kvtimer, breakdown, walls, weights)
    pooled = steady_table.shares(kvtimer, None, walls, {q: 1.0 for q in weights})
    wall_meta, wall_steady = steady_meta(wall_dir)
    return {"store": store, "kind": "layout" if layouts.is_cell(store) else "mix", "weights": spec,
            "queries": len(weights), "rocks_share": s["rocks_share"], "rocks_share_pooled": pooled["rocks_share"],
            "rocks_wall_share": s["rocks_wall_share"], "block_read_share": s.get("block_read_share"),
            "other_rocks_share": s.get("other_rocks_share"), "kvtimer_tax": s["kvtimer_tax"],
            "perf3_tax": s.get("perf3_tax"), "wall_passes": wall_steady.get("measured"),
            "kvtimer_passes": steady_meta(kvt_dir)[1].get("measured"),
            "breakdown_passes": steady_meta(brk_dir)[1].get("measured") if brk_dir else 0,
            "binary_git_sha": wall_meta["provenance"]["binary_git_sha"],
            **{k: s.get(k) for k in ("rocks_ns", "busy_ns", "timer_wall_ns", "mean_wall_ns", "block_read_ns", "perf3_wall_ns")},
            "pooled_rocks_ns": pooled["rocks_ns"], "pooled_busy_ns": pooled["busy_ns"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timings", required=True, help="one workload's timings: <timings>/meta-<store>/steady-<MB>mb-*/ "
                                                       "(python -m kvevaluator.output_tree; Tree.timings)")
    ap.add_argument("--weights", required=True, help="frequencies:<dataset>/<scale>/<id> or equal-share:<id>")
    ap.add_argument("--out", required=True, help="the CSV to write, one row per store")
    a = ap.parse_args()
    regime = wall_regime(a.timings)
    stores = steady_table.stores_of(a.timings, regime)
    if not stores:
        sys.exit(f"{a.timings}: no store under meta-*/{regime}/")
    first = sorted(stores.values())[0]    # the queries timed: one store's __plan__ rows (shares() checks every store's)
    timed = {r["query"] for r in capture_layout.reader(open(first)) if r["step"] == "__plan__"}
    weights = timed_weights(weights_of(a.weights), timed)
    rows = []
    for store, path in sorted(stores.items()):
        rows.append(store_row(a.weights, store, path.parent, weights))
        r = rows[-1]
        print(f"[store] {store}: rocks_share {r['rocks_share']:.3f}"
              + ("" if r["breakdown_passes"] else " (no breakdown capture: no block-read split)"), flush=True)
    rows.sort(key=lambda r: (r["kind"], r["rocks_share"], r["store"]))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows({k: (f"{v:.6f}" if isinstance(v, float) else ("" if v is None else v)) for k, v in r.items()}
                    for r in rows)
    fmt = lambda v: "-" if v is None else f"{v:.3f}"
    split = sum(1 for r in rows if r["breakdown_passes"])
    print(f"\n{Path(a.timings).name}: {len(rows)} stores ({split} with the block-read split), {len(weights)} queries, weights "
          f"{a.weights}, regime {regime}, binary {', '.join(sorted({r['binary_git_sha'][:10] for r in rows}))}")
    print("| store | RocksDB / busy time | the same, each query weight 1 | RocksDB / wall | block reads | rest of RocksDB |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| `{r['store']}` | {fmt(r['rocks_share'])} | {fmt(r['rocks_share_pooled'])} | "
              f"{fmt(r['rocks_wall_share'])} | {fmt(r['block_read_share'])} | {fmt(r['other_rocks_share'])} |")
    print(f"\nwrote {out} ({len(rows)} rows)")
    print("STEADY_SHARE_DONE")


if __name__ == "__main__":
    main()
