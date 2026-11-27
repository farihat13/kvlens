#!/usr/bin/env python3
"""Generate node-id anchors for the LinkBench wide-read queries, from the datagen output.

Counts each node's out-degree (links.jsonl) and writes a rank-SPACED sample across the degree distribution (a few
super-hubs, a spread of mid/low-degree nodes): LinkBench's graph is hub-dominated, so a head-only or random sample
would misrepresent it. Writes `node|degree` (degree desc) to the tag's anchors.txt (workloads/linkbench/params.py).

    python3 python/kvevaluator/datasets/linkbench/gen_linkbench_anchors.py --data <jsonl-dir> --tag <scale> [--count 60]
"""
import argparse
import json
import os
from kvevaluator.paths import ROOT
from kvevaluator.datasets import data_in

REPO_EXP = str(ROOT)


def degrees(data_dir):
    deg = {}
    with open(os.path.join(data_dir, "nodes.jsonl")) as f:
        for line in f:
            deg[json.loads(line)["id"]] = 0
    with open(os.path.join(data_dir, "links.jsonl")) as f:
        for line in f:
            i1 = json.loads(line)["id1"]
            if i1 in deg:
                deg[i1] += 1
    return deg


def rank_spaced(items, count):
    if count >= len(items):
        return items
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dir holding nodes.jsonl / links.jsonl")
    ap.add_argument("--tag", required=True, help="scale tag, e.g. 1m (the LDBC-sf analog)")
    ap.add_argument("--count", type=int, default=60)
    a = ap.parse_args()

    deg = degrees(a.data)
    ranked = sorted(deg.items(), key=lambda kv: (-kv[1], kv[0]))
    sample = rank_spaced(ranked, a.count)

    out = str(data_in("linkbench", a.tag))
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "anchors.txt")
    with open(path, "w") as f:
        f.write("node|degree\n")
        for nid, d in sample:
            f.write(f"{nid}|{d}\n")
    print(f"wrote {len(sample)} anchors -> {path} (degree {sample[0][1]}..{sample[-1][1]}, "
          f"{len(ranked)} nodes total)")


if __name__ == "__main__":
    main()
