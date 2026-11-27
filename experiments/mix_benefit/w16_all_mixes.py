#!/usr/bin/env python3
"""Price EVERY per-type mix of I01 at SF3 (3^21 = 10.5 billion) from the per-query costs: a 2-D histogram of (bytes,
wops) on log bins, a uniform random sample of about 1M mixes, and the exhaustive check that no mix beats the complete
front within its own bytes -> all_mixes.npz; exit 1 if the check fails.

    python experiments/mix_benefit/w16_all_mixes.py [--grid DIR] [--out DIR] [--jobs 32] [--fresh] [--chunks N]

About 8.5 min on 32 cores. Reused when all_mixes.npz was made from the same grid and the same complete_front.json
(--fresh forces it). --chunks N: a smoke test on the first N of the 6,561 chunks (written as all_mixes-smoke<N>.npz).
"""
import argparse
import glob
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np

import w16_front_common as F
from w16_front_common import C
from kvcost.layout_search import search as cost_cache

HI = 8                                      # a chunk = the first HI search positions fixed; the rest vectorized
SAMPLE = 1_000_000
NB = 400                                    # histogram bins per axis
G = {}


def extent(pb):
    """log10 bounds of (bytes, wops) over every mix, from each query's costs min and max, padded 2%"""
    lo_o = sum(min(q["mins"]["ops"][-1]) for q in pb.queries)
    hi_o = sum(max(q["mins"]["ops"][-1]) for q in pb.queries)
    lo_b = sum(min(q["mins"]["bytes"][-1]) for q in pb.queries)
    hi_b = sum(max(q["mins"]["bytes"][-1]) for q in pb.queries)
    xl, xh, yl, yh = np.log10([lo_b, hi_b, lo_o, hi_o])
    px, py = 0.02 * (xh - xl), 0.02 * (yh - yl)
    return np.array([xl - px, xh + px, yl - py, yh + py])


def setup(grid, front_path):
    data, w = F.load_w16(grid)
    pb = cost_cache.problem(w, data, C.workload_units(w, data))
    n = len(pb.order)
    assert set(pb.size) == {3}, pb.size
    lo = n - HI
    low = np.arange(3 ** lo, dtype=np.int64)
    ldig = [(low // 3 ** (lo - 1 - j)) % 3 for j in range(lo)]          # the digit of search position HI + j
    qs = []
    for q in pb.queries:
        to, tb = np.array(q["mins"]["ops"][-1]), np.array(q["mins"]["bytes"][-1])
        parts, k = q["parts"], len(q["parts"])
        # a cost cache index = sum digit_p * 3^(k-1-j): split into the fixed (high positions) and the vectorized (low) parts
        lidx, hw = np.zeros(len(low), dtype=np.int64), []
        for j, p in enumerate(parts):
            wgt = 3 ** (k - 1 - j)
            if p >= HI:
                lidx += ldig[p - HI] * wgt
            else:
                hw.append((p, wgt))
        qs.append((to, tb, lidx if any(p >= HI for p in parts) else None, hw))
    front = json.load(open(front_path))
    fb, fo = np.array([m["bytes"] for m in front]), np.array([m["ops"] for m in front])
    assert np.all(np.diff(fb) > 0) and np.all(np.diff(fo) < 0)
    G.update(qs=qs, nlow=len(low), fb=fb, fo=fo, n=n, order=[list(u) for u in pb.order], ext=extent(pb))


def chunk(c):
    qs, nlow = G["qs"], G["nlow"]
    xl, xh, yl, yh = G["ext"]
    hd = [(c // 3 ** (HI - 1 - p)) % 3 for p in range(HI)]               # the digits of the fixed positions
    o, b = np.zeros(nlow), np.zeros(nlow)
    for to, tb, lidx, hw in qs:
        off = sum(hd[p] * wgt for p, wgt in hw)
        if lidx is None:
            o += to[off]
            b += tb[off]
        else:
            o += to[lidx + off]
            b += tb[lidx + off]
    xi = np.clip(((np.log10(b) - xl) / (xh - xl) * NB).astype(np.int64), 0, NB - 1)
    yi = np.clip(((np.log10(o) - yl) / (yh - yl) * NB).astype(np.int64), 0, NB - 1)
    hist = np.bincount(xi * NB + yi, minlength=NB * NB)
    j = np.searchsorted(G["fb"], b * (1 + 1e-9), side="right") - 1       # the front's best mix within this mix's bytes
    below = j < 0                                                         # fewer bytes than the fewest-bytes mix
    ok = ~below
    beat = np.zeros(nlow, dtype=bool)
    beat[ok] = o[ok] < G["fo"][j[ok]] * (1 - 1e-9)
    on_front = np.zeros(nlow, dtype=bool)
    on_front[ok] = np.abs(o[ok] - G["fo"][j[ok]]) <= 1e-9 * o[ok]
    rng = np.random.default_rng(c)
    pick = np.nonzero(rng.random(nlow) < SAMPLE / 3 ** G["n"])[0]
    return hist, int(below.sum()), int(beat.sum()), int(on_front.sum()), (c * nlow + pick, o[pick], b[pick]), \
        (float(o.min()), float(b.min()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    F.add_input_args(ap)
    ap.add_argument("--jobs", type=int, default=32)
    ap.add_argument("--chunks", type=int, default=None)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    out = F.abspath(a.out)
    front_path = os.path.join(out, "complete_front.json")
    F.read_json(out, "complete_front.json")
    cap = hashlib.sha256()                       # the grid's capture files by content: an edited row is not reused
    grid_files = sorted(f for f in glob.glob(os.path.join(F.abspath(a.grid), "**", "*props_*"), recursive=True)
                        if os.path.isfile(f) and not f.endswith(".lock"))
    for f in grid_files:
        cap.update(os.path.relpath(f, F.abspath(a.grid)).encode())
        cap.update(hashlib.sha256(open(f, "rb").read()).digest())
    key = (f"grid={os.path.realpath(F.abspath(a.grid))} capture_sha256={cap.hexdigest()} "
           f"front_sha256={hashlib.sha256(open(front_path, 'rb').read()).hexdigest()}")
    dest = os.path.join(out, "all_mixes.npz" if a.chunks is None else f"all_mixes-smoke{a.chunks}.npz")
    if not a.fresh and a.chunks is None and os.path.exists(dest) and str(np.load(dest)["inputs"]) == key:
        print(f"reused {dest} (same grid capture files, same complete front); --fresh re-prices")
        return 0
    t0 = time.time()
    setup(a.grid, front_path)
    nch = a.chunks or 3 ** HI
    print(f"setup {time.time() - t0:.1f}s: {G['n']} parts, {len(G['qs'])} queries, {nch} of {3 ** HI} chunks of "
          f"{G['nlow']:,}", flush=True)
    t1 = time.time()
    hist = np.zeros(NB * NB, dtype=np.int64)
    below = beat = onf = 0
    si, so, sb = [], [], []
    mins = [float("inf"), float("inf")]
    with mp.get_context("fork").Pool(a.jobs) as pool:
        for k, (h, bl, bt, of, (i, o, b), mn) in enumerate(pool.imap_unordered(chunk, range(nch), chunksize=4)):
            hist += h
            below, beat, onf = below + bl, beat + bt, onf + of
            si.append(i)
            so.append(o)
            sb.append(b)
            mins = [min(mins[0], mn[0]), min(mins[1], mn[1])]
            if (k + 1) % 500 == 0:
                el = time.time() - t1
                print(f"  {k + 1}/{nch} chunks, {el:.0f}s, eta {el / (k + 1) * (nch - k - 1):.0f}s", flush=True)
    total = int(hist.sum())
    print(f"priced {total:,} mixes in {time.time() - t1:.1f}s (expected {nch * G['nlow']:,})")
    if total != nch * G["nlow"]:
        raise SystemExit("FAIL: the histogram does not hold every mix")
    print(f"min wops {mins[0] / 1e6:.4f}M (front's fewest {G['fo'][-1] / 1e6:.4f}M), min bytes {mins[1] / 1e9:.4f} GB "
          f"(front's fewest {G['fb'][0] / 1e9:.4f} GB)")
    ok = beat == 0 and below == 0
    print(f"EXHAUSTIVE CHECK: mixes beating the front within their own bytes: {beat:,}; below the fewest-bytes mix: "
          f"{below:,}; at the front's wops (ties included): {onf:,}  -> {'PASS' if ok else 'FAIL'}")
    si, so, sb = np.concatenate(si), np.concatenate(so), np.concatenate(sb)
    order = np.argsort(si, kind="stable")          # the pool returns chunks in any order: sort, so a rerun is identical
    np.savez_compressed(dest, hist=hist.reshape(NB, NB), extent=G["ext"], sample_idx=si[order], sample_ops=so[order],
                        sample_bytes=sb[order], order=np.array(["|".join(u) for u in G["order"]]), inputs=np.array(key))
    print(f"wrote {dest}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
