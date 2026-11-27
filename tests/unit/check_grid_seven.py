#!/usr/bin/env python3
"""The seven stores the paper's grid runs measure, and the fingerprints of a composed cell. No store, no capture.

  1. layouts.SEVEN: 7 distinct cells, the 3 diagonals of the grid among them, and grid_bodies.DIAGONAL_SF30 is it; no
     named set of layouts is left (a command names its layouts);
  2. layouts.cell_runs (kvcost measure's passes): the seven's ids are the grid's 3 diagonals, then ChK-ChK at 1, 4, 16,
     256 KB, each in its own spec set; ids combine without a duplicate; a ChK-ChK id at another size goes to its size's
     set; any other name stays in the grid's set (the measuring tools refuse it);
  3. grid_pins.fingerprints of a composed cell (a synthetic run record with composed_from) is the pair of its two sources'
     fingerprints, props side first, read at the recorded source paths, else from the copies beside it (a snapshot);
     a measured cell's fingerprints keep their one-store form (the control).

    python3 tests/unit/check_grid_seven.py
"""
import importlib.util
import json
import os
import sys
import tempfile

from kvevaluator import layouts
from kvevaluator.output_tree import Capture
from kvevaluator.measure import grid_bodies
from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location("grid_pins", os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
grid_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid_pins)

SP, CO, CH = "props_split_adj_split", "props_condensed_adj_condensed", "props_chunked_adj_chunked"
SIZES = ["props_chunked_c1k_adj_chunked_c1k", "props_chunked_c4k_adj_chunked_c4k", "props_chunked_c16k_adj_chunked_c16k",
         "props_chunked_c256k_adj_chunked_c256k"]


def record(keys, inputs):
    return {"store_manifest": {"key_codec": "k", "value_codec_hash": "v", "keys": keys}, "inputs": inputs,
            "binary": {"binary_git_sha": "b"}, "nparam": 5}


def write_record(cap_dir, cell, rec):
    p = Capture(cap_dir).record(cell)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(rec, f)


def main():
    bad = []

    def check(ok, what):
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            bad.append(what)

    # 1. the set
    check(len(set(layouts.SEVEN)) == 7 and {SP, CO, CH} <= set(layouts.SEVEN) and set(SIZES) <= set(layouts.SEVEN),
          "SEVEN: 7 distinct cells, the 3 grid diagonals and ChK-ChK at 1, 4, 16, 256 KB")
    check(grid_bodies.DIAGONAL_SF30 == layouts.SEVEN, "grid_bodies.DIAGONAL_SF30 is layouts.SEVEN")
    check(not hasattr(layouts, "CELL_SETS") and not hasattr(layouts, "NINE"), "no named layout set (CELL_SETS, NINE)")

    # 2. the passes of --cells
    runs = layouts.cell_runs(",".join(layouts.SEVEN))
    check(runs == [("grid", [SP, CO, CH])] + [(d, [c]) for d, c in zip(("grid_chunked_1k", "grid_chunked_4k",
                                                                        "grid_chunked_16k", "grid_chunked_256k"), SIZES)],
          f"the seven: the 3 diagonals in grid, then one ChK-ChK per size set in size order ({runs})")
    check(layouts.cell_runs("seven") == [("grid", ["seven"])], "seven is no set: an unknown name (the measuring tools refuse it)")
    runs = layouts.cell_runs(",".join(layouts.SEVEN) + ",props_condensed_adj_chunked,props_split_adj_split")
    check(runs[0] == ("grid", [SP, CO, CH, "props_condensed_adj_chunked"]) and len(runs) == 5,
          "the seven + Co-Ch64k + a repeated diagonal: Co-Ch64k added to the grid's pass, no duplicate")
    check(layouts.cell_runs("props_chunked_c4k_adj_chunked_c4k,props_split_adj_condensed")
          == [("grid", ["props_split_adj_condensed"]), ("grid_chunked_4k", ["props_chunked_c4k_adj_chunked_c4k"])],
          "a sized ChK-ChK id goes to its size's set, a grid id to the grid's")
    check(layouts.cell_runs("not_a_cell") == [("grid", ["not_a_cell"])], "an unknown name stays in the grid's set")

    # 3. a composed cell's fingerprints
    with tempfile.TemporaryDirectory() as tmp:
        measured = os.path.join(tmp, "measured", "interactive", "reported")
        composed = os.path.join(tmp, "composed", "interactive", "reported")
        write_record(measured, CO, record(100, "in-co"))
        write_record(measured, SP, record(200, "in-sp"))
        rel = lambda cap, cell: os.path.relpath(os.path.join(cap, "parts", cell + ".csv"), str(ROOT))
        write_record(composed, "props_condensed_adj_split", {
            "store_manifest": None, "inputs": "in-co", "binary": {"binary_git_sha": "b"}, "nparam": 5,
            "composed_from": {"props and index parts": rel(measured, CO), "adjacency parts": rel(measured, SP)}})
        fp = grid_pins.fingerprints(composed, "props_condensed_adj_split", in_sample=True)
        want = [grid_pins.fingerprints(measured, c, in_sample=True) for c in (CO, SP)]
        check(fp["store"] == [w["store"] for w in want] and fp["reported"] == [w["reported"] for w in want]
              and fp["held_out"] is None,
              "composed Co-Sp: the pair (Co-Co's, Sp-Sp's) at the recorded source paths")
        # a snapshot: the recorded paths are gone, the sources' copies sit beside the composed cell
        gone = os.path.join(tmp, "gone", "interactive", "reported")
        write_record(composed, CO, record(100, "in-co"))
        write_record(composed, SP, record(200, "in-sp"))
        write_record(composed, "props_split_adj_condensed", {
            "store_manifest": None, "inputs": "in-sp", "binary": {"binary_git_sha": "b"}, "nparam": 5,
            "composed_from": {"props and index parts": rel(gone, SP), "adjacency parts": rel(gone, CO)}})
        fp = grid_pins.fingerprints(composed, "props_split_adj_condensed", in_sample=True)
        check(fp["store"] == [want[1]["store"], want[0]["store"]],
              "composed Sp-Co with its source paths gone: the pair from the copies beside it, props side first")
        fp = grid_pins.fingerprints(measured, CO, in_sample=True)
        check(fp["store"] == {"key_codec": "k", "value_codec_hash": "v", "keys": 100},
              "control: a measured cell keeps its one-store form")
    # 4. a composed cell in an output tree names its sources relative to the tree (paths.recorded_path), so a copy of
    # the tree read anywhere finds them; its stale check reads them there too
    from kvcost.layout_cost.compose_cells import sources_moved
    from kvevaluator.paths import recorded_path
    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, "a-tree")
        measured = os.path.join(tree, "captures", "ldbc", "sf3", "interactive", "reported")
        composed = os.path.join(tree, "composed", "ldbc", "sf3", "interactive", "reported")
        write_record(measured, CO, record(100, "in-co"))
        write_record(measured, SP, record(200, "in-sp"))
        parts = {c: os.path.join(measured, "parts", c + ".csv") for c in (CO, SP)}
        for f in parts.values():
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").write("x\n")
        rel = {c: recorded_path(f, tree) for c, f in parts.items()}
        check(rel[CO] == f"captures/ldbc/sf3/interactive/reported/parts/{CO}.csv", f"recorded_path: {rel[CO]}")
        write_record(composed, "props_condensed_adj_split", {
            "store_manifest": None, "inputs": "in-co", "binary": {"binary_git_sha": "b"}, "nparam": 5,
            "composed_from": {"props and index parts": rel[CO], "adjacency parts": rel[SP]}})
        moved = os.path.join(tmp, "moved")
        os.rename(tree, moved)            # the tree read at another path: its records name no path outside it
        composed = composed.replace(tree, moved)
        fp = grid_pins.fingerprints(composed, "props_condensed_adj_split", in_sample=True)
        want = [grid_pins.fingerprints(measured.replace(tree, moved), c, in_sample=True) for c in (CO, SP)]
        check(fp["store"] == [w["store"] for w in want], "a tree-relative composed_from read at another path")
        sha = __import__("hashlib").sha256(b"x\n").hexdigest()
        check(sources_moved({"composed_from_sha": {rel[CO]: sha}}, moved) == []
              and sources_moved({"composed_from_sha": {rel[CO]: "0"}}, moved) == [rel[CO]],
              "sources_moved reads a tree-relative source under the record's tree")
    print(f"check_grid_seven: {'FAIL ' + str(len(bad)) if bad else 'PASS'} ({14 - len(bad)} of 14 checks)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
