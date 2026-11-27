#!/usr/bin/env python3
"""A grid capture is refused when its bodies split on the answer (grid_bodies.answer_splits): every legal body of a
query must give one answer per param (candidates.result_key), so a body that answers otherwise is a plan bug.

  1. synthetic rows: a split is found, a failed run does not vote, one answer is no split;
  2. live, sf0.1, one cell, ic8 and is7 on param 1: the capture is written when the bodies agree; with one body's rows
     cut by one (the plant), the capture is refused, names the query and the planted body, and writes no CSV.

    python3 tests/gates/meta/check_answer_vote.py [--sf 0.1] [--cell props_split_adj_split]
"""
import argparse
import os
import shutil
import sys
import tempfile

from kvevaluator.measure import grid_bodies as GV
from kvevaluator.paths import OUTPUT_DIR
from kvevaluator.output_tree import Capture, capture_in
from kvevaluator.plan_choice.candidates import candidates


def synthetic():
    rows = [{"query": "q", "param_idx": 1, "body": "a", "ok": "1", "answer_key": "x"},
            {"query": "q", "param_idx": 1, "body": "b", "ok": "1", "answer_key": "y"},
            {"query": "q", "param_idx": 1, "body": "c", "ok": "0", "answer_key": ""},
            {"query": "q", "param_idx": 2, "body": "a", "ok": "1", "answer_key": "x"},
            {"query": "q", "param_idx": 2, "body": "b", "ok": "1", "answer_key": "x"}]
    got = GV.answer_splits(rows)
    ok = got == [("q", 1, {"x": ["a"], "y": ["b"]})]
    print(f"{'PASS' if ok else 'FAIL'} 1 synthetic: one split on param 1, a failed run does not vote, param 2 agrees: {got}")
    return ok


class PlantRunner(GV.ServeRunner):
    """the real runner, with one body's rows cut by one (the plant)"""
    target = None

    def call(self, req):
        resp, err = super().call(req)
        if resp and PlantRunner.target is not None and GV.body_key(req["plan"]) == PlantRunner.target and resp.get("rows"):
            resp["rows"] = resp["rows"][:-1]
        return resp, err


def live(sf, cell):
    binary = GV.binary_path()
    inputs = GV.inputs_fingerprint("interactive", sf, 1, [1])
    slice_ = (["ic8", "is7"], [1])
    ok = True
    with tempfile.TemporaryDirectory(dir=str(OUTPUT_DIR)) as tmp:
        cap = lambda sub: capture_in(os.path.join(tmp, sub), "interactive").path  # (a capture of its own)
        GV.RUNNERS["plant"] = PlantRunner
        try:
            PlantRunner.target = None
            GV.measure_cell(cell, "interactive", sf, 1, 4, 600, binary, cap("agree"), inputs, GV.GRID, "plant",
                            slice_, fresh_start=True)
            wrote = os.path.exists(Capture(cap("agree")).path_of("counts", cell))
            print(f"{'PASS' if wrote else 'FAIL'} 2a the bodies agree: the capture is written")
            ok &= wrote
            _ir, cands = candidates("interactive", "ic8")
            bad = GV.invalid_on_cell("interactive", os.path.join(GV.GRID, f"{cell}.json")).get("ic8", {})
            planted = next(n for n, _ in cands if n not in bad)          # a body the cell runs
            PlantRunner.target = GV.body_key(dict(cands)[planted])
            try:
                GV.measure_cell(cell, "interactive", sf, 1, 4, 600, binary, cap("plant"), inputs, GV.GRID,
                                "plant", slice_, fresh_start=True)
                refused, msg = False, ""
            except SystemExit as e:
                refused, msg = True, str(e)
            none = not os.path.exists(Capture(cap("plant")).path_of("counts", cell))
            named = "ic8" in msg
            good = refused and none and named
            print(f"{'PASS' if good else 'FAIL'} 2b the plant ({planted}'s rows cut by one): refused {refused}, no CSV "
                  f"{none}, names ic8 {named}: {msg[:160]}")
            ok &= good
        finally:
            GV.RUNNERS.pop("plant", None)
            PlantRunner.target = None
            for sub in ("agree", "plant"):                 # the journals this check made (outside its temp dir)
                shutil.rmtree(GV.journal_dir(cap(sub)), ignore_errors=True)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--cell", default="props_split_adj_split")
    a = ap.parse_args()
    ok = synthetic() & live(a.sf, a.cell)
    print(f"check_answer_vote: {'OK' if ok else 'FAILED'} (1 synthetic case, 2 live captures on {a.cell} sf{a.sf})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
