#!/usr/bin/env python3
"""The COMPLETE per-type Pareto front of I01 at SF3 (every mix no other mix beats in both wops and bytes), each mix and the
nine uniform layouts priced per part -> complete_front.json, uniform_layouts.json.

    python experiments/mix_benefit/w16_complete_front.py [--grid DIR] [--front CSV] [--out DIR]

How: python/kvcost/layout_search/front.py's walk (from the fewest-wops mix down in bytes, the three settings only), started from the
nine uniform layouts and the snapshot front's mixes. Check: every per-type mix of the snapshot's front is on it.
About 2 min at SF3 (1,648 solves on the 10-12 grid).
"""
import argparse
import json
import os
import time

import w16_front_common as F
from w16_front_common import C, SETTINGS, UNITS
import sys
from kvcost.layout_search import search as cost_cache
from kvcost.layout_search.search import uniform
from kvcost.layout_search import front
from kvcost.layout_cost.compose import LAYOUTS
from kvcost.layout_cost.mixes import parse_assignment


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    F.add_input_args(ap)
    a = ap.parse_args()
    out = F.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    data, w = F.load_w16(a.grid)
    units = C.workload_units(w, data)
    pb = cost_cache.problem(w, data, units)
    print(f"{len(units)} parts, cost cache {time.time() - t0:.1f}s", flush=True)

    # starting points (they only speed up the search): the nine uniform layouts and the snapshot front's mixes
    snap = F.snapshot_front(a.front)
    pool = [pb.from_assign(uniform(pb.parts, p, q)) for p in LAYOUTS for q in LAYOUTS]
    pool += [pb.from_assign(parse_assignment(r["assignment"])) for r in snap]

    t1 = time.time()
    mixes, nodes_all = front.complete_front(pb, data, w, pool)
    print(f"{len(mixes)} mixes on the complete front, {time.time() - t1:.1f}s of solves, {nodes_all:,} nodes")

    # check: every mix of the snapshot's front is on the complete front
    want = {(round(float(r["total_ops"]), 3), round(float(r["total_bytes"]), 0)) for r in snap}
    have = {(round(m["ops"], 3), round(m["bytes"], 0)) for m in mixes}
    print(f"snapshot front mixes: {len(want)}, of them on the complete front: {len(want & have)}")
    if not want or want - have:
        raise SystemExit("FAIL: a snapshot front mix is not on the complete front")

    uni = []
    for p in SETTINGS:
        for q in SETTINGS:
            assign = {u: (p if u[0] == "props" else q) for u in UNITS}
            c, pc = front.cost_by_part(data, w, assign)
            rec = front.mix_record(assign, c, pc)
            uni.append({"name": front.uniform_name(rec["assign"]), **rec})
    ref, unit = F.ref_bytes(a.front)                 # B = 1: Sp-Co's bytes (Co-Sp's in a front in Co-Sp units)
    mine = next(u["bytes"] for u in uni if u["name"] == unit)
    if abs(mine - ref) > 1e-6 * ref:
        raise SystemExit(f"FAIL: {unit}'s bytes {mine:.0f} here, {ref:.0f} in {a.front} (its B = 1): the front is from another grid")
    json.dump(mixes, open(os.path.join(out, "complete_front.json"), "w"), indent=1)
    json.dump(uni, open(os.path.join(out, "uniform_layouts.json"), "w"), indent=1)
    print(f"PASS; wrote {out}/complete_front.json ({len(mixes)} mixes), uniform_layouts.json (9), {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
