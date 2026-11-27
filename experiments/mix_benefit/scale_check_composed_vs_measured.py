#!/usr/bin/env python3
"""Composed against measured for built mixes outside the pick record (the scale check's at SF30; the one-for-all
stores at SF3): composed_vs_measured.py as it is, run on nine uniform cells of one chunk size and on the built mixes
whose specs use that chunk size. In this process the LDBC store's families are the ones --families names (default the
LDBC reads and the writes; microbench and bi where the captures hold them: BI is registered as a read family of the
store, as one_mix_per_dataset.py does, priced in-sample), and both sides pick by
THIS scale's own held-out rows (a file with no recorded pick stands in for the pick record, which is read and never
written).

    python3 experiments/mix_benefit/scale_check_composed_vs_measured.py --sf 30 --cells <composed/ldbc/sf30 dir> \\
        --mixes <captures/ldbc/sf30 dir> --spec-dir inputs/specs/mixes_scale_check_20261013 --chunk-bytes 65536|4096 --out DIR
        [--families ldbc,writes,microbench,bi] [--plant ...]
        -> the files of composed_vs_measured.py under <out>; its exit code"""
import argparse
import json
import os
import re
import sys
import tempfile

from scale_check_common import grid_capture
from kvevaluator.plan_choice import pick_record
from kvevaluator.capture.spec_sets import spec_cells
from kvevaluator.layouts import is_cell
from kvevaluator.output_tree import capture_in
from kvcost.layout_cost.picks import PlanRule


def link_layouts(src, dst, keep):
    """symlink every file of a layout `keep` accepts below the captures dir `src` into `dst`, at the same relative path"""
    for root, _, files in os.walk(src):
        for f in sorted(files):
            if not f.startswith(".") and keep(f.split(".")[0]):
                to = os.path.join(dst, os.path.relpath(root, src))
                os.makedirs(to, exist_ok=True)
                os.symlink(os.path.join(root, f), os.path.join(to, f))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--cells", required=True, help="the nine uniform cells (compose_cells.py), Chunked at --chunk-bytes: "
                    "a composed/<dataset>/<scale> dir")
    ap.add_argument("--mixes", required=True, help="the captures/<dataset>/<scale> dir the built mixes were measured in")
    ap.add_argument("--spec-dir", required=True, help="the built mixes' spec set (inputs/specs/<set>)")
    ap.add_argument("--chunk-bytes", required=True, choices=("65536", "4096"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--families", default="interactive,writes", help="comma list of interactive, writes, microbench, bi")
    a, rest = ap.parse_known_args()
    kinds = {"interactive": "read", "writes": "write", "microbench": "read", "bi": "read"}
    families = {f: kinds[f] for f in a.families.split(",")}
    mixes = os.path.abspath(a.mixes)
    names = []
    reads = capture_in(mixes, "interactive")
    for name in spec_cells(a.spec_dir):
        if not os.path.exists(reads.record(name)):
            continue
        packs = set(re.findall(r'"pack": (\d+)', json.dumps(json.load(open(reads.record(name)))["spec"])))
        if packs <= {a.chunk_bytes}:                        # a mix with no Chunked part belongs to both chunk sizes
            names.append(name)
    if not names:
        sys.exit(f"no built mix under {mixes} with chunks of {a.chunk_bytes} bytes")
    with tempfile.TemporaryDirectory() as tmp:
        # the captures dir the tool expects: the nine cells of --cells and this chunk size's mixes of --mixes, linked
        grid = os.path.join(tmp, "captures")
        link_layouts(os.path.abspath(a.cells), grid, is_cell)
        link_layouts(mixes, grid, lambda n: n in names)
        # a record decided at this scale with no rows: no recorded pick or anchor applies here; written as any record is
        record = pick_record.load(grid_capture.PICKS_FILE)
        picks = os.path.join(tmp, "no_recorded_picks", "picks.csv")
        pick_record.dump({"criterion": record.criterion, "margin": record.margin, "decided_at": a.sf, "cells": {},
                          "what": "not a pick record: no recorded pick applies at this scale"}, picks)
        from kvcost.layout_cost.mixes import grid_settings
        import composed_vs_measured
        settings = grid_settings()                    # a spec's {"pack": N} is the cells' Chunked setting
        for kind in settings:
            for k, v in list(settings[kind].items()):
                settings[kind][k.replace("65536", a.chunk_bytes)] = v
        sys.argv = [sys.argv[0], "--sf", a.sf, "--grid", grid, "--out", os.path.abspath(a.out)] + rest
        print(f"[scale_check_composed_vs_measured] sf{a.sf}, chunks of {a.chunk_bytes} bytes: {len(names)} built mixes: {' '.join(names)}")
        return composed_vs_measured.main(families=families, settings=settings,
                                         rule=PlanRule(record=picks))


if __name__ == "__main__":
    sys.exit(main())
