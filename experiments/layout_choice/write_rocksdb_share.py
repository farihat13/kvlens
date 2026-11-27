#!/usr/bin/env python3
"""The RocksDB share of the steady writes, per store: the KV-timer passes beside the writes after the steady read
warm-up (run-w16-mix-latency.sh --stage writes --write-kvtimer N), and with --reads the share of I01 (Interactive's
reads and updates) together. Opens no store.

    python3 experiments/layout_choice/write_rocksdb_share.py --b <writes>/after-steady-warmup-<MB>mb-p1-noflush --out <dir> \
        [--weights frequencies:ldbc/3/i01] [--reads]

The rule is the reads' (kvcost.latency.steady_table: timer_query, mean_walls, shares; kvcost.latency.steady_share): per
request, the time inside RocksDB calls (the KV timer's kv_call_ns, half the timer's own cost per call taken out) over
the busy time (the plan's work_ns, the workers' task time, the timer's whole cost per call taken out); per query the
mean over its params and every pass's run; per store the ratio of the weighted sums. A write's busy time is its whole
apply: the reads it makes first, the decode, merge and re-encode, and the engine's Put and Delete (both timed by the KV
timer, inside RocksDB). The weights (--weights, the reads' share's spec): I01's counts per 100,000 updates, over the
timed queries that carry one (IU1-8). The primitives (w_*) carry none: their pooled share (each query weight 1, the
summed RocksDB time over the summed busy time) is named by the query that holds most of their busy time, and each primitive's own share is in the per-query CSV.

--reads: the I01 share = (the reads' rocks_ns + the updates' rocks_ns) / (the reads' busy_ns + the updates' busy_ns),
both weighted sums at I01's counts. The reads' row is computed here, by steady_share.store_row (the reads' share, one
rule), from the steady read capture that warmed B's cache (B's meta, warm_reads.capture) and its kvtimer capture beside
it: the same store, scale, cache and read bodies as the writes by construction. A capture with no kvtimer beside it is
refused. A share is of mean busy time: never multiply it into a wall of record.

Passes: every run-<k>/ below <the kvtimer dir (cppwlatency.kvtimer_dir)>/meta-<store>/ with its meta (a pass that
stopped has none and is left out, said on stdout). Each pass must be a kvtimer capture (perf level 2) whose counts
equalled B's on every cell (same_counts against B's dir), and must share B's binary, store manifest, warm-up capture and
read bodies; anything else is refused. A pass whose read warm-up did not settle (max_warm reached) is counted and named
(unsettled_passes), as B's. The pass-to-pass spread: the share of each pass alone, and its standard error over the
passes (sample standard deviation / sqrt(passes); none with one pass); the I01 share's spread moves only the updates'
passes (the reads' sums held). Per store the timer passes' mean wall (the timer's cost out) over B's mean apply wall,
per query and as kvtimer_tax (B runs at perf level 1: the rest of the gap is perf level 2 and what the timer costs
beyond its clock reads). Output: <out>/write_rocksdb_share.csv (per store), <out>/write_rocksdb_share_by_query.csv (per
store and query, every pass pooled), each ratio beside the sums it divides; the table as Markdown on stdout.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
from pathlib import Path

from kvcost.latency import steady_share, steady_table
from kvcost.latency.i01 import qclass
from kvevaluator.capture import capture_layout
from kvevaluator.measure.cpplatency import KVTIMER_PERF_LEVEL
from kvevaluator.measure.cppwlatency import kvtimer_dir, raw_files

STORE_COLUMNS = ["store", "weights", "iu_queries", "primitive_queries", "kvtimer_passes", "unsettled_passes",
                 "iu_rocks_share", "iu_rocks_share_se", "iu_rocks_share_min", "iu_rocks_share_max", "iu_rocks_share_pooled",
                 "iu_rocks_wall_share", "primitives_rocks_share_pooled", "primitives_top_query", "primitives_top_busy_fraction",
                 "kvtimer_tax", "w16_rocks_share", "w16_rocks_share_se", "reads_rocks_share", "iu_rocks_share_by_pass",
                 "binary_git_sha", "reads_binary_git_sha", "reads_capture", "iu_rocks_ns", "iu_busy_ns", "iu_timer_wall_ns",
                 "iu_b_wall_ns", "iu_pooled_rocks_ns", "iu_pooled_busy_ns", "primitives_rocks_ns", "primitives_busy_ns",
                 "reads_rocks_ns", "reads_busy_ns", "w16_rocks_ns", "w16_busy_ns"]
QUERY_COLUMNS = ["store", "query", "class", "weight", "params", "kvtimer_passes", "rocks_share", "rocks_ns", "busy_ns",
                 "timer_wall_ns", "b_wall_ns", "timer_over_b_wall", "block_reads"]


def identity(meta, arm):
    """what a pass must share with B: the binary, the store, the warm-up capture and its read bodies, the sequence"""
    prov, warm = meta["provenance"][arm], (meta.get("warm_reads") or {}).get(arm) or {}
    return {"binary_git_sha": prov.get("binary_git_sha"), "store_manifest": prov.get("store_manifest"),
            "warm_reads_capture": warm.get("capture"), "body_shas": warm.get("body_shas"), "sequence": meta["sequence"]}


def timer_runs(kvt_store_dir, b_dir, arm):
    """the complete kvtimer passes of one store: ([(k, run dir, settled)] sorted by k, B's meta), each pass checked
    against its meta (timer kind, perf level, B's counts on every cell, B's identity); a run-<k> without its meta is
    named and left out"""
    b_meta = json.load(open(Path(b_dir) / "steps.csv.meta.json"))
    want = identity(b_meta, arm)
    found = []
    for d in sorted(Path(kvt_store_dir).glob("run-*")):
        tail = d.name[len("run-"):]
        if not tail.isdigit():
            print(f"[passes] {d}: not a pass dir (an incomplete pass moved aside), left out")
            continue
        if not (d / "steps.csv.meta.json").exists():
            print(f"[passes] {d}: no meta (the pass stopped), left out")
            continue
        m = json.load(open(d / "steps.csv.meta.json"))
        same = m.get("same_counts") or {}
        if (m.get("timer_kind"), m.get("perf_level")) != ("kvtimer", KVTIMER_PERF_LEVEL):
            sys.exit(f"{d}: timer kind {m.get('timer_kind')!r} at perf level {m.get('perf_level')}: want 'kvtimer' at "
                     f"{KVTIMER_PERF_LEVEL}")
        if same.get("differ") != 0 or same.get("cells_compared") != len(m["sequence"]) or not m["sequence"]:
            sys.exit(f"{d}: its counts were not held equal to a run's on every cell (same_counts {same})")
        # B's dir by its last two parts (<regime>/meta-<store>): a run tree copied elsewhere keeps them
        if Path(same.get("run", "")).parts[-2:] != Path(b_dir).resolve().parts[-2:]:
            sys.exit(f"{d}: its counts were compared with {same.get('run')}, not B ({b_dir})")
        got = identity(m, arm)
        diff = sorted(k for k in want if got[k] != want[k])
        if diff:
            sys.exit(f"{d}: its {', '.join(diff)} differ from B's ({b_dir}): one store, binary and warm-up for every pass")
        steady_at = m["warm_reads"][arm].get("steady_at")
        if steady_at is None:
            print(f"[passes] {d}: its read warm-up did not settle (max_warm passes): counted in unsettled_passes")
        found.append((int(tail), d, steady_at is not None))
    return sorted(found, key=lambda x: x[0]), b_meta


def sub(d, queries):
    return {q: d[q] for q in queries}


def pooled(per_pass):
    """{query: the mean over the passes of each field} of timer_query outputs, one per pass: every pass holds the same
    params, one run each, so this is the mean over every run and param (timer_query's rule over all passes)"""
    return {q: tuple(statistics.fmean(p[q][i] for p in per_pass) for i in range(5)) for q in per_pass[0]}


def se(xs):
    return statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None


def reads_row(spec, store, b_meta, arm):
    """the reads' share row of this store (steady_share.store_row) from the steady read capture that warmed B's cache,
    over its timed queries at the same weights"""
    cap = Path((b_meta.get("warm_reads") or {}).get(arm, {}).get("capture") or "")
    if not (cap / "steps.csv.meta.json").exists():
        sys.exit(f"{store}: --reads: B's warm-up capture {cap} is not here (its meta names it; run where B ran)")
    timed = {r["query"] for r in capture_layout.reader(open(cap / "steps.csv")) if r["step"] == "__plan__"}
    return steady_share.store_row(spec, store, cap, steady_share.timed_weights(steady_share.weights_of(spec), timed)), cap


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--b", required=True, help="the writes after the steady read warm-up (apply leg only): meta-<store>/")
    ap.add_argument("--weights", default="frequencies:ldbc/3/i01", help="steady_share's spec (default I01's counts)")
    ap.add_argument("--reads", action="store_true",
                    help="also the I01 share: the reads' row from B's warm-up capture and its kvtimer capture beside it")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bdir = Path(a.b)
    kdir = Path(kvtimer_dir(a.b))
    if not kdir.is_dir():
        sys.exit(f"no KV-timer dir {kdir} beside {bdir}")
    weights = steady_share.weights_of(a.weights)
    stores = sorted(p.name[len("meta-"):] for p in kdir.glob("meta-*") if (bdir / p.name / "steps.csv.meta.json").exists())
    rows, qrows = [], []
    for s in stores:
        arm = f"meta-{s}"
        runs, b_meta = timer_runs(kdir / arm, bdir / arm, arm)
        if not runs:
            print(f"[store] {s}: no complete kvtimer pass, left out")
            continue
        per_pass = [steady_table.timer_query(d, arm, raw=raw_files(d, "kvtimer")) for _k, d, _ok in runs]
        if any(set(p) != set(per_pass[0]) for p in per_pass):
            sys.exit(f"{s}: the passes time different queries")
        tq = pooled(per_pass)
        walls = steady_table.mean_walls(bdir / arm, arm, raw=raw_files(bdir / arm, "headline"))
        if set(walls) != set(tq):
            sys.exit(f"{s}: B and the kvtimer passes time different queries: {sorted(set(walls) ^ set(tq))}")
        missing = sorted(q for q in weights if qclass(q) == "IU" and q not in tq)
        if missing:
            sys.exit(f"{s}: weighted updates not timed: {missing}")
        iu = {q: weights[q] for q in sorted(tq) if q in weights}
        prim = {q: 1.0 for q in sorted(tq) if q not in weights}
        sh = steady_table.shares(sub(tq, iu), None, sub(walls, iu), iu)
        ones = steady_table.shares(sub(tq, iu), None, sub(walls, iu), {q: 1.0 for q in iu})
        pr = steady_table.shares(sub(tq, prim), None, sub(walls, prim), prim) if prim else {}
        top = max(prim, key=lambda q: tq[q][1]) if prim else None
        by_pass = [steady_table.shares(sub(p, iu), None, sub(walls, iu), iu) for p in per_pass]
        sha = b_meta["provenance"][arm]["binary_git_sha"]
        r = {"store": s, "weights": a.weights, "iu_queries": len(iu), "primitive_queries": len(prim),
             "kvtimer_passes": len(runs), "unsettled_passes": sum(1 for *_x, ok in runs if not ok),
             "iu_rocks_share": sh["rocks_share"], "iu_rocks_share_se": se([p["rocks_share"] for p in by_pass]),
             "iu_rocks_share_min": min(p["rocks_share"] for p in by_pass),
             "iu_rocks_share_max": max(p["rocks_share"] for p in by_pass), "iu_rocks_share_pooled": ones["rocks_share"],
             "iu_rocks_wall_share": sh["rocks_wall_share"], "primitives_rocks_share_pooled": pr.get("rocks_share"),
             "primitives_top_query": top, "primitives_top_busy_fraction": tq[top][1] / pr["busy_ns"] if prim else None,
             "kvtimer_tax": sh["kvtimer_tax"],
             "iu_rocks_share_by_pass": " ".join(f"{p['rocks_share']:.6f}" for p in by_pass),
             "binary_git_sha": sha, "iu_rocks_ns": sh["rocks_ns"],
             "iu_busy_ns": sh["busy_ns"], "iu_timer_wall_ns": sh["timer_wall_ns"], "iu_b_wall_ns": sh["mean_wall_ns"],
             "iu_pooled_rocks_ns": ones["rocks_ns"], "iu_pooled_busy_ns": ones["busy_ns"],
             "primitives_rocks_ns": pr.get("rocks_ns"), "primitives_busy_ns": pr.get("busy_ns")}
        if a.reads:
            rd, cap = reads_row(a.weights, s, b_meta, arm)
            rr, rb = rd["rocks_ns"], rd["busy_ns"]
            i01 = [(rr + p["rocks_ns"]) / (rb + p["busy_ns"]) for p in by_pass]
            if rd["binary_git_sha"] != sha:
                print(f"[binary] {s}: the reads by {rd['binary_git_sha'][:10]}, the updates by {sha[:10]}")
            r.update(w16_rocks_share=(rr + sh["rocks_ns"]) / (rb + sh["busy_ns"]), w16_rocks_share_se=se(i01),
                     reads_rocks_share=rd["rocks_share"], reads_binary_git_sha=rd["binary_git_sha"], reads_capture=str(cap),
                     reads_rocks_ns=rr, reads_busy_ns=rb, w16_rocks_ns=rr + sh["rocks_ns"], w16_busy_ns=rb + sh["busy_ns"])
        rows.append(r)
        params = {}
        for q, _p in raw_files(runs[0][1], "kvtimer"):
            params[q] = params.get(q, 0) + 1
        for q in sorted(tq):
            rocks, busy, wall, _brt, blocks = tq[q]
            qrows.append({"store": s, "query": q, "class": "IU" if q in iu else "primitive", "weight": iu.get(q, ""),
                          "params": params[q], "kvtimer_passes": len(runs), "rocks_share": rocks / busy,
                          "rocks_ns": rocks, "busy_ns": busy, "timer_wall_ns": wall, "b_wall_ns": walls[q],
                          "timer_over_b_wall": wall / walls[q], "block_reads": blocks})
        print(f"[store] {s}: {len(runs)} passes, IU rocks_share {sh['rocks_share']:.3f}; the timer passes' wall (its "
              f"cost out) over B's, per query: " + " ".join(f"{q} {tq[q][2] / walls[q]:.3f}" for q in sorted(tq)), flush=True)
    if not rows:
        sys.exit(f"{kdir}: no store with a complete kvtimer pass beside {bdir}")
    os.makedirs(a.out, exist_ok=True)
    fmt6 = lambda v: f"{v:.6f}" if isinstance(v, float) else ("" if v is None else v)
    for name, cols, data in (("write_rocksdb_share.csv", STORE_COLUMNS, rows),
                             ("write_rocksdb_share_by_query.csv", QUERY_COLUMNS, qrows)):
        with open(os.path.join(a.out, name), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows({k: fmt6(r.get(k)) for k in cols} for r in data)
        print(f"wrote {os.path.join(a.out, name)} ({len(data)} rows)")
    f3 = lambda v: "-" if v is None else f"{v:.3f}"
    rows.sort(key=lambda r: (r["iu_rocks_share"], r["store"]))
    print(f"\nThe RocksDB share of mean busy time, weights {a.weights} (IU1-8; the primitives pooled), from {kdir}:\n")
    print("| store | passes (unsettled) | IU1-8: RocksDB / busy | its SE over passes | each query weight 1 | primitives, "
          "weight 1 (the top query's share of their busy time) | timer wall / B wall (IU) | I01 reads + updates | its SE "
          "(the updates' passes) | reads alone |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        prim = (f"{f3(r['primitives_rocks_share_pooled'])} ({r['primitives_top_query']} "
                f"{r['primitives_top_busy_fraction']:.3f})" if r["primitives_top_query"] else "-")
        print(f"| `{r['store']}` | {r['kvtimer_passes']} ({r['unsettled_passes']}) | {f3(r['iu_rocks_share'])} | "
              f"{f3(r['iu_rocks_share_se'])} | {f3(r['iu_rocks_share_pooled'])} | {prim} | {f3(r['kvtimer_tax'])} | "
              f"{f3(r.get('w16_rocks_share'))} | {f3(r.get('w16_rocks_share_se'))} | {f3(r.get('reads_rocks_share'))} |")
    print(f"\n{len(rows)} stores, {len(qrows)} (store, query) rows, {sum(r['kvtimer_passes'] for r in rows)} passes "
          f"({sum(r['unsettled_passes'] for r in rows)} unsettled), binary "
          f"{', '.join(sorted({r['binary_git_sha'][:10] for r in rows}))}")
    print("WRITE_SHARE_DONE")


if __name__ == "__main__":
    main()
