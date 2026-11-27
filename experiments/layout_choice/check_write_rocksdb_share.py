#!/usr/bin/env python3
"""write_rocksdb_share.py on planted captures (no store, no binary): a B run and its KV-timer passes run-0, run-1 for one
store, IU1-8 and one primitive, and the steady read capture that warmed B (a wall capture and its kvtimer capture).
Checks the hand-worked shares (the timer's cost out: half of it from the RocksDB time, all of it from the busy time),
I01's weights against each query weight 1, the standard error over the passes, the I01 share with the reads' row
computed from B's own warm-up capture (steady_share.store_row), an incomplete pass left out, an unsettled warm-up
counted; and refused: a pass whose counts differed, a pass of another binary, warm-up capture or compared run, and
--reads when B's warm-up capture is not there or its kvtimer capture is of another binary.

    python3 experiments/layout_choice/check_write_rocksdb_share.py [--plant no-timer-cost|no-pass-identity|reads-elsewhere]
    (a plant must exit 1: no-pass-identity drops the passes' identity check, reads-elsewhere takes the reads' row from
    another capture than B's warm-up one)
"""
import argparse
import csv
import io
import json
import math
import os
import sys
import tempfile
from contextlib import redirect_stdout

import write_rocksdb_share as WS   # this script's dir comes first on the path
from kvcost.latency import steady_table as ST
from kvcost.latency.i01 import weights as w16_weights
from kvevaluator.measure.cppwlatency import raw_file
from kvevaluator.measure.latency_raw_files import raw_path

FAILED = []
ARM, WL = "meta-s", "writes/ldbc/plans"
PAIR = 10                                   # the timer's own cost per call, ns


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


def resp(kv_call, calls, work):
    """one applied write's response: a kv step with its RocksDB calls (Puts) and the plan's totals"""
    return {"steps": [{"kind": "kv", "kv": {"kv_call_ns": kv_call, "kv_put_calls": calls}}],
            "totals": {"work_ns": work, "overhead_ns": 100}}


def plant(d, regime, cells, meta):
    """a cppwlatency --out: steps.csv's __plan__ rows, each cell's raw <regime>.json, the meta"""
    os.makedirs(d)
    with open(os.path.join(d, "steps.csv"), "w") as f:
        f.write("arm,workload,query,param_idx,regime,step\n" + "".join(f"{ARM},{WL},{q},{p},{regime},__plan__\n"
                                                                      for q, p in cells))
    for (q, p), r in cells.items():
        path = raw_file(d, ARM, WL, q, p, regime)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(r, open(path, "w"))
    json.dump(meta, open(os.path.join(d, "steps.csv.meta.json"), "w"))


def plant_reads(root, sha, kvt_sha, rocks=600):
    """a steady read capture of ic1 and is1 (one param, one run each: RocksDB 600 -> 550, busy 1100 -> 1000 after the
    timer's cost) and its kvtimer capture beside it -> the wall capture dir"""
    dirs = {}
    for regime, meta in (("steady-10mb-p2", {"provenance": {"binary_git_sha": sha}, "regimes": {"steady": {"perf_level": 2,
                                                                                                         "measured": 1}}}),
                         ("steady-10mb-kvtimer", {"provenance": {"binary_git_sha": kvt_sha}, "timer_kind": "kvtimer",
                                                  "kv_timing_block": {"timer_pair_ns": PAIR},
                                                  "regimes": {"steady": {"perf_level": 2, "measured": 1}}})):
        d = os.path.join(root, "wl", ARM, regime)   # <a workload's timings>/<arm>/<regime>/ (the output tree's layout)
        os.makedirs(d)
        with open(os.path.join(d, "steps.csv"), "w") as f:
            f.write("query,param_idx,step\nic1,1,__plan__\nis1,1,__plan__\n")
        for q in ("ic1", "is1"):
            path = raw_path(d, ARM, q, "1", "steady")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            run = {"steps": [{"kind": "kv", "kv": {"kv_call_ns": rocks, "kv_get_calls": 10}}],
                   "totals": {"work_ns": 1100, "overhead_ns": 0}}
            json.dump({"measured": [run]}, open(path, "w"))
        json.dump(meta, open(os.path.join(d, "steps.csv.meta.json"), "w"))
        dirs[regime] = d
    return dirs["steady-10mb-p2"]


def refused(argv, what):
    rc, text = run_main(argv)
    return rc not in (0, None) and what in str(rc), (rc, text[-300:])


def run_main(argv):
    out = io.StringIO()
    old, sys.argv = sys.argv, ["write_rocksdb_share.py", *argv]
    try:
        with redirect_stdout(out):
            WS.main()
        return 0, out.getvalue()
    except SystemExit as e:
        return e.code, out.getvalue()
    finally:
        sys.argv = old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=["no-timer-cost", "no-pass-identity", "reads-elsewhere"], default=None)
    a = ap.parse_args()
    if a.plant == "no-timer-cost":
        ST.timer_pair_ns = lambda d: 0.0
    if a.plant == "no-pass-identity":
        WS.identity = lambda meta, arm: {}
    n = w16_weights()
    iu = sorted(q for q in n if q.startswith("iu"))
    seq = [{"query": q, "param_idx": "1"} for q in iu + ["w_x"]]
    prov = {ARM: {"binary_git_sha": "a" * 40, "store_manifest": "m1"}}
    tmp = tempfile.mkdtemp(prefix="check-write-share-")
    reads_cap = plant_reads(os.path.join(tmp, "reads"), "a" * 40, "a" * 40)
    warm = {ARM: {"capture": reads_cap, "body_shas": {"meta-s/ic1": "x"}, "steady_at": 3}}
    if a.plant == "reads-elsewhere":       # a capture of the same store elsewhere, other numbers (RocksDB 800)
        elsewhere = plant_reads(os.path.join(tmp, "reads-elsewhere"), "a" * 40, "a" * 40, rocks=800)
        orig = WS.reads_row
        WS.reads_row = lambda spec, store, b_meta, arm: orig(spec, store, {"warm_reads": {arm: {"capture": elsewhere}}}, arm)
    b, k = os.path.join(tmp, "b-p1-noflush", ARM), os.path.join(tmp, "b-kvtimer-noflush", ARM)
    # every request: 10 Puts (timer cost 100 ns: 50 out of the RocksDB time, 100 out of the busy time). Pass 0: RocksDB
    # 600 -> 550, busy 1100 -> 1000; pass 1: RocksDB 800 -> 750. iu1 works 2100 (busy 2000). w_x: RocksDB 300 -> 250.
    def cells(rocks):
        return {(q, "1"): resp(300 if q == "w_x" else rocks, 10, 2100 if q == "iu1" else 1100) for q in iu + ["w_x"]}
    bmeta = {"sequence": seq, "provenance": prov, "warm_reads": warm}
    plant(b, "headline", {c: resp(0, 0, 900) for c in cells(0)}, bmeta)
    tmeta = {"timer_kind": "kvtimer", "perf_level": 2, "kv_timing_block": {"timer_pair_ns": PAIR}, "sequence": seq,
             "provenance": prov, "warm_reads": warm, "same_counts": {"run": os.path.abspath(b),
                                                                     "cells_compared": len(seq), "differ": 0}}
    plant(os.path.join(k, "run-0"), "kvtimer", cells(600), tmeta)
    unsettled = dict(tmeta, warm_reads={ARM: dict(warm[ARM], steady_at=None)})
    plant(os.path.join(k, "run-1"), "kvtimer", cells(800), unsettled)
    os.makedirs(os.path.join(k, "run-2"))                     # a pass that stopped: no meta
    out = os.path.join(tmp, "out")
    rc, text = run_main(["--b", os.path.dirname(b), "--reads", "--out", out])
    check("runs, and names the incomplete pass and the unsettled warm-up", rc in (0, None) and "run-2: no meta" in text
          and "did not settle" in text, (rc, text[-400:]))
    row = next(csv.DictReader(open(os.path.join(out, "write_rocksdb_share.csv"))))
    total = sum(n[q] for q in iu)                             # 100,000: I01's updates per 100,000 updates
    rocks = total * 650                                       # the mean of 550 and 750, at I01's counts
    busy = (total - n["iu1"]) * 1000 + n["iu1"] * 2000
    by_pass = [total * 550 / busy, total * 750 / busy]
    wr = n["ic1"] + n["is1"]                                  # the reads' weights (their row: steady_share.store_row)
    want = {"kvtimer_passes": 2, "iu_queries": 8, "primitive_queries": 1, "iu_rocks_share": rocks / busy,
            "iu_rocks_share_se": abs(by_pass[1] - by_pass[0]) / 2,          # stdev of two / sqrt(2) = |a - b| / 2
            "iu_rocks_share_pooled": 8 * 650 / (7 * 1000 + 2000), "primitives_rocks_share_pooled": 250 / 1000,
            "w16_rocks_share": (wr * 550 + rocks) / (wr * 1000 + busy), "reads_rocks_share": 0.55,
            "iu_rocks_ns": rocks, "iu_busy_ns": busy, "unsettled_passes": 1, "primitives_top_busy_fraction": 1.0,
            "kvtimer_tax": ((total - n["iu1"]) * 1100 + n["iu1"] * 2100) / (total * 1000)}
    got = {key: float(row[key]) for key in want}
    check("the hand-worked shares, the SE over 2 passes, the I01 share and the sums",
          all(math.isclose(got[key], v, rel_tol=1e-5) for key, v in want.items()), {kk: (got[kk], v) for kk, v in want.items()})
    q = {r["query"]: r for r in csv.DictReader(open(os.path.join(out, "write_rocksdb_share_by_query.csv")))}
    check("by query: iu1's share and its class, w_x a primitive", math.isclose(float(q["iu1"]["rocks_share"]), 650 / 2000,
          rel_tol=1e-5) and q["iu1"]["class"] == "IU" and q["w_x"]["class"] == "primitive", (q["iu1"], q["w_x"]))
    argv = ["--b", os.path.dirname(b), "--out", out]
    for name, meta, what in (
            ("counts that differed from B's", dict(tmeta, same_counts=dict(tmeta["same_counts"], differ=1)), "same_counts"),
            ("counts compared with another run", dict(tmeta, same_counts=dict(tmeta["same_counts"], run="/x/r/meta-s")),
             "not B"),
            ("another binary", dict(tmeta, provenance={ARM: dict(prov[ARM], binary_git_sha="c" * 40)}), "binary_git_sha"),
            ("another store", dict(tmeta, provenance={ARM: dict(prov[ARM], store_manifest="m2")}), "store_manifest"),
            ("another warm-up capture", dict(tmeta, warm_reads={ARM: dict(warm[ARM], capture="/elsewhere")}),
             "warm_reads_capture"),
            ("other read bodies", dict(tmeta, warm_reads={ARM: dict(warm[ARM], body_shas={})}), "body_shas")):
        d = os.path.join(k, "run-3")
        plant(d, "kvtimer", cells(600), meta)
        check(f"a pass with {name} refused", *refused(argv, what))
        os.rename(d, os.path.join(tmp, f"aside-{len(os.listdir(tmp))}"))
    # --reads: B's warm-up capture gone, or its kvtimer capture of another binary
    other_cap = plant_reads(os.path.join(tmp, "reads2"), "a" * 40, "d" * 40)
    for i, (cap, what, name) in enumerate(((os.path.join(tmp, "gone"), "is not here", "B's warm-up capture not here"),
                                           (other_cap, "binary", "its kvtimer capture of another binary"))):
        bb, kk = os.path.join(tmp, f"b{i}", "x-p1-noflush", ARM), os.path.join(tmp, f"b{i}", "x-kvtimer-noflush", ARM)
        wm = {ARM: dict(warm[ARM], capture=cap)}
        plant(bb, "headline", {c: resp(0, 0, 900) for c in cells(0)}, dict(bmeta, warm_reads=wm))
        plant(os.path.join(kk, "run-0"), "kvtimer", cells(600),
              dict(tmeta, warm_reads=wm, same_counts=dict(tmeta["same_counts"], run=os.path.abspath(bb))))
        check(f"--reads refused: {name}", *refused(["--b", os.path.dirname(bb), "--reads", "--out", out], what))
    print(f"check_write_rocksdb_share {'FAIL' if FAILED else 'OK'}: {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
