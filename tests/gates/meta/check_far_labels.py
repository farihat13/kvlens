#!/usr/bin/env python3
"""The far-end label content check (adjacency far_label): every label a cell stores equals the record stream's.

Where the schema leaves an adjacency entry's far end open (k > 1 labels for (etype, dir) from its endpoints), each
entry stores the far end's label (codec/lists.h label block; a Split record's first byte). Per cell, `kvevaluator
census --far-labels` decodes every stored label; this script derives the expected one from the records (an out entry's
far end is the edge's dst, an in entry's its src) and compares, family by family: the same (owner, neighbour) set and
the same label on each. The positive control (--control) flips one expected label per family: every cell must then
fail on exactly that many entries, so a check that compares nothing cannot pass.

  python3 tests/gates/meta/check_far_labels.py [--sf 0.1] [--cells c1,c2] [--spec-dir DIR] [--control]
Exit 0 = every cell's stored labels equal the records' (and the entry count is printed; 0 compared = FAIL).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from kvevaluator.paths import OUTPUT_DIR, ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.measure import records
from kvevaluator.measure import grid_capture

SCHEMA = schema_path("ldbc")


def open_families():
    """{(etype, dir): reverse kept under direction declared?} for the families whose far end has k > 1 labels"""
    s = json.load(open(SCHEMA))
    fams = {}
    for e in s["edges"]:
        if len({b for _, b in e.get("endpoints", [])}) > 1:
            fams[(e["etype"], "out")] = True
        if len({a for a, _ in e.get("endpoints", [])}) > 1 and e.get("reverse"):
            fams[(e["etype"], "in")] = True
    return fams


def expected(sf, fams):
    """{"etype:dir": {(owner, neighbour): far label}} from the record stream"""
    want = defaultdict(dict)
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            if not line.startswith('["e"'):
                continue
            r = json.loads(line)
            etype, src, dst, src_label, dst_label = r[1], r[2], r[3], r[5], r[6]
            if (etype, "out") in fams:
                want[f"{etype}:out"][(src, dst)] = dst_label
            if (etype, "in") in fams:
                want[f"{etype}:in"][(dst, src)] = src_label
    return want


def stored(store):
    """{"etype:dir": {(owner, neighbour): label}} from census --far-labels"""
    got = defaultdict(dict)
    with tempfile.TemporaryDirectory(dir=str(OUTPUT_DIR)) as tmp:
        tsv = os.path.join(tmp, "far.tsv")
        r = subprocess.run([binary.binary_path(), "census", "--far-labels", tsv, store], capture_output=True, text=True)
        if r.returncode not in (0, 1):   # 1 = totals differ from the manifest: a separate check (check_symmetry)
            raise SystemExit(f"census failed on {store}: {r.stderr[-400:]}")
        with open(tsv) as f:
            for line in f:
                fam, owner, other, label = line.rstrip("\n").split("\t")
                got[fam][(owner, other)] = label
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--cells", default=None, help="comma list (default: every spec in --spec-dir)")
    ap.add_argument("--spec-dir", default=grid_capture.GRID)
    ap.add_argument("--control", action="store_true", help="flip one expected label per family: every cell must fail")
    a = ap.parse_args()
    fams = open_families()
    want = expected(a.sf, fams)
    flipped = 0
    if a.control:   # the positive control: one expected label per family set to a label no entry stores
        for fam in sorted(want):
            k = min(want[fam])
            want[fam][k] = want[fam][k] + "-CONTROL"
            flipped += 1
    cells = a.cells.split(",") if a.cells else sorted(os.path.basename(p)[:-5] for p in
                                                       __import__("glob").glob(os.path.join(a.spec_dir, "*.json")))
    total, bad_cells, flip_cells = 0, [], []   # flip_cells: cells where a flipped label shows as a wrong label (the signature)
    for cell in cells:
        store = grid_capture.meta_store(a.sf, "interactive", os.path.join(a.spec_dir, f"{cell}.json"))
        got = stored(store)
        diffs, n = [], 0
        for fam in sorted(set(want) | set(got)):
            w, g = want.get(fam, {}), got.get(fam, {})
            if set(w) != set(g):
                diffs.append(f"{fam}: {len(w)} expected vs {len(g)} stored entries "
                             f"({len(set(w) - set(g))} missing, {len(set(g) - set(w))} extra)")
                continue
            wrong = [k for k in w if w[k] != g[k]]
            n += len(w)
            if any(w[k].endswith("-CONTROL") for k in wrong) and cell not in flip_cells:
                flip_cells.append(cell)
            if wrong:
                k = wrong[0]
                diffs.append(f"{fam}: {len(wrong)} of {len(w)} labels differ (e.g. {k[0]} -> {k[1]}: stored {g[k]}, "
                             f"records {w[k]})")
        total += n
        fams_seen = sorted(got)
        print(f"  {cell}: {n} entries in {len(fams_seen)} families ({', '.join(fams_seen)})"
              + ("" if not diffs else " -- " + "; ".join(diffs)))
        if diffs or n == 0:
            bad_cells.append(cell)
    if a.control:
        ok = len(flip_cells) == len(cells) and total > 0   # caught = the flip itself, not a missing entry or an empty read
        print(f"[far-labels] CONTROL ({flipped} labels flipped): {len(flip_cells)} of {len(cells)} cells show the flipped label "
              f"({len(bad_cells)} failed in all) -> "
              f"{'OK: the check catches a wrong label' if ok else 'FAIL: a flipped label went unnoticed'}")
    else:
        ok = not bad_cells and total > 0
        print(f"[far-labels] sf{a.sf}: {total} stored labels compared over {len(cells)} cells, {len(fams)} families "
              f"-> {'OK: every stored label equals the records' if ok else f'FAIL ({len(bad_cells)} cells)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
