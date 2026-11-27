#!/usr/bin/env python3
"""python/kvcost/layout_cost/settings.py reads a cell id through kvcost.plotting.cell_ids.cell_parts: the unsized ids keep their names, a size on the
properties part is read (no KeyError), a ChK-ChK id names both sizes or neither, and a non-cell is
refused: so are the one-size spellings (props_chunked_adj_chunked_c4k) and a _c64k where cell_id writes none.
Opens no store; under a second.

Run:  python3 tests/unit/check_cell_names.py              (passes)
      python3 tests/unit/check_cell_names.py --control    (the pre-fix parser put back: the sized-properties cases must fail)
"""
import argparse
import os
import re
import sys
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvcost.layout_cost import settings as cells

# the old ids: their names as the pre-fix code gave them
SIDE = {"split": "Sp", "condensed": "Co", "chunked": "Ch64k"}      # the nine: the grid's Chunked is 64 KB, written
OLD = {**{c: f"{SIDE[p]}-{SIDE[a]}" for c in cells.CELLS for p, a in [c[len("props_"):].split("_adj_")]},
       "meta-props_condensed_adj_split": "Co-Sp",
       "meta-props_split_adj_chunked_c256k": "Sp-Ch256k",
       "props_condensed_adj_chunked_c1k": "Co-Ch1k"}
LABELS = {"Chunked/Chunked, 1 KB chunks": "Ch1k-Ch1k", "Condensed/Chunked, 16 KB chunks": "Co-Ch16k",
          "Condensed/Chunked": "Co-Ch64k", "Split/Condensed (Dgraph)": "Sp-Co (Dgraph)",
          "Chunked 1 KB/Chunked 16 KB": "Chunked 1 KB/Chunked 16 KB"}
NEW = {"props_chunked_c4k_adj_split": ("chunked", "split", "Ch4k-Sp"),          # a size per side
       "props_chunked_c4k_adj_chunked_c16k": ("chunked", "chunked", "Ch4k-Ch16k"),
       "props_chunked_c4k_adj_chunked_c4k": ("chunked", "chunked", "Ch4k-Ch4k"),
       "meta-props_chunked_c64k_adj_chunked_c4k": ("chunked", "chunked", "Ch64k-Ch4k")}
BAD = ["props_split_c4k_adj_split", "props_split_adj_condensed_c4k", "props_split_adj", "dgraph-c512k", "props_x_adj_split",
       "props_chunked_adj_chunked_c4k", "props_chunked_c4k_adj_chunked", "props_chunked_c64k_adj_chunked_c64k",
       "meta-props_chunked_c64k_adj_condensed", "props_split_adj_chunked_c64k"]


def old_parts(cell):
    """a parser whose regex takes a size only at the end, shaped like cell_parts: the control"""
    m = re.fullmatch(r"(?:meta-)?props_(\w+?)_adj_(\w+?)(?:_c\d+k)?", cell)
    if not m:
        raise ValueError(f"not a cell: {cell}")
    k = re.search(r"_c(\d+)k$", cell)
    return m.group(1), None, m.group(2), int(k.group(1)) if k else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--control", action="store_true")
    a = ap.parse_args()
    if a.control:
        cells.cell_parts = old_parts
    bad = 0

    def case(ok, what):
        nonlocal bad
        bad += not ok
        if not ok:
            print(f"FAIL {what}")
    for c, want in OLD.items():
        got = cells.name_of(c)
        case(got == want, f"{c}: {got!r}, want {want!r}")
    for c, (p, ad, want) in NEW.items():
        try:
            got = (*cells.settings(c), cells.name_of(c))
        except Exception as e:  # (the pre-fix KeyError is what the control must show)
            got = repr(e)
        case(got == (p, ad, want), f"{c}: {got!r}, want {(p, ad, want)!r}")
    for c in BAD:
        try:
            cells.settings(c)
            case(False, f"{c}: read as a cell, want ValueError")
        except ValueError:
            pass
    for text, want in LABELS.items():       # a label's long words in short: a size the label gives goes into the name
        got = cells.short_names(text)
        case(got == want, f"short_names({text!r}): {got!r}, want {want!r}")
    n = len(OLD) + len(NEW) + len(BAD)
    print(f"[cell-names] {n} ids ({len(OLD)} old, {len(NEW)} sized properties, {len(BAD)} not cells) and {len(LABELS)} "
          f"labels: {bad} failed")
    if a.control:
        print("CONTROL CAUGHT" if bad else "CONTROL MISSED")
        return 0 if bad else 1
    print("CELL NAMES OK" if not bad else "CELL NAMES FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
