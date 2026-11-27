#!/usr/bin/env python3
"""LinkBench's whole request log per cell and request type, as two CSVs the summary figure reads. Reads captures only.
  whole_log_counts.csv   the counts (LinkBench's captures of the whole log, param set all: the reads' and the writes'): requests and the
                         raw lanes summed (S, G, N, P, D; get / scan / miss / written bytes; the reads a write does first)
  whole_log_time.csv     the steady time of the READS (the steady whole-log capture's responses.jsonl.gz: one line per
                         request, its median-wall run over the measured passes): requests, and the sums over them of the
                         whole time, the storage steps' time and the time inside RocksDB (its seek, get and next timers)
Raw sums, no weights.

    python3 experiments/linkbench/whole_log_counts_and_time.py --scale n1m --regime steady-<MB>mb-p2 [--root DIR] --out DIR
        reads <root>/captures/linkbench/<scale>/ and <root>/timings/linkbench/<scale>/linkbench/meta-<cell>/<regime>/"""
import argparse
import csv
import gzip
import json
import os
import sys
from collections import defaultdict

from kvevaluator.measure.capture_protocol import require_breakdown
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.output_tree import Tree, timing_in, writes_in
from kvevaluator.capture import capture_layout
from kvevaluator.capture.step_record import READ_BYTE_LANES  # (the lanes, one definition)

LAYOUTS = SETTINGS
CELLS = [cell_id(p, a) for p in LAYOUTS for a in LAYOUTS]
LANES = ("S", "G", "N", "P", "D", "get", "scan", "miss", "written", "wS", "wG", "wN", "wread")
ROCKSDB_TIMERS = ("seek_child_seek_time", "get_from_output_files_time", "find_next_user_entry_time")


PARAMS = "log"   # the whole request log


def counts(tree, scale, cell):
    """{request type: lanes + requests}: the picked plan's run per read request, every write request"""
    t = defaultdict(lambda: dict.fromkeys(LANES + ("requests",), 0.0))
    with open(tree.capture("linkbench", scale, PARAMS).path_of("counts", cell)) as f:
        for r in csv.DictReader(f):
            if r.get("planner_pick", "1") != "1" or r["ok"] != "1":  # one body per request type (a capture with a planner_pick column flags it 1)
                continue
            x, v = t[r["query"]], lambda k: float(r[k] or 0)
            x["requests"] += 1
            for o in "SGN":
                x[o] += v(o)
            x["get"] += v("get_key") + v("get_value")
            x["scan"] += v("scan_key") + v("scan_value")
            x["miss"] += v("seek_miss_key") + v("next_miss_key") + v("get_miss_key")
    writes = writes_in(tree.place_dir("captures", "linkbench", scale), "linkbench", PARAMS)
    for r in capture_layout.write_rows(writes.path_of("counts", cell), cell=cell)[2]:
        x, v = t[r["query"]], lambda k: float(r[k] or 0)
        x["requests"] += 1
        x["P"] += v("P")
        x["D"] += v("D")
        x["written"] += v("write_key") + v("write_value")
        x["wS"] += v("S")
        x["wG"] += v("G")
        x["wN"] += v("N")
        x["wread"] += sum(v(k) for k in READ_BYTE_LANES)
    return t


def times(root, scale, regime, cell):
    """{request type: [requests, wall ns, storage steps' ns, inside-RocksDB ns, kv steps with no timer block]}"""
    t = defaultdict(lambda: [0, 0, 0, 0, 0])
    steady = str(timing_in(Tree(root).timings("linkbench", scale), f"meta-{cell}", regime))
    require_breakdown(steady, f"{cell} (the time inside RocksDB)")
    with gzip.open(os.path.join(steady, "responses.jsonl.gz"), "rt") as f:
        for line in f:
            o = json.loads(line)
            m, x = o["median"], t[o["query"]]
            x[0] += 1
            x[1] += m["totals"]["wall_ns"]
            for s in m["steps"]:
                if s.get("kind") != "kv":
                    continue
                x[2] += s["wall_ns"]
                k = s.get("kv")
                if k is None:
                    x[4] += 1
                else:                                   # PerfContext per step: a timer that stayed 0 is not written
                    x[3] += sum(k.get(n, 0) for n in ROCKSDB_TIMERS)
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scale", required=True, help="LinkBench's scale tag (n1m)")
    ap.add_argument("--regime", required=True, help="the steady whole-log timings' regime dir (responses.jsonl.gz per cell)")
    ap.add_argument("--root", default=None, help="the output tree (default: the output dir)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tree = Tree(a.root)
    os.makedirs(a.out, exist_ok=True)
    crow, trow = [], []
    for cell in CELLS:
        for q, x in sorted(counts(tree, a.scale, cell).items()):
            crow.append({"cell": cell, "request": q, **{k: round(v, 3) for k, v in x.items()}})
        for q, x in sorted(times(a.root, a.scale, a.regime, cell).items()):
            trow.append({"cell": cell, "request": q, "requests": x[0], "wall_ns": x[1], "kv_ns": x[2], "rocksdb_ns": x[3], "kv_steps_without_timers": x[4]})
    for name, rows in (("whole_log_counts.csv", crow), ("whole_log_time.csv", trow)):
        with open(os.path.join(a.out, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    print(f"whole_log_counts.csv: {len(crow)} rows ({sum(r['requests'] for r in crow if r['cell'] == CELLS[0]):,.0f} requests per cell); "
          f"whole_log_time.csv: {len(trow)} rows ({sum(r['requests'] for r in trow if r['cell'] == CELLS[0]):,} read requests per cell)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
