#!/usr/bin/env python3
"""Every reader that picks or prices a layout from a predicted capture (predicted.by_body and .pick, uniform.uniform_cost,
layouts.load_predicted) refuses a capture of the picked bodies only (kvcost predict --bodies picks) by name: its rows
are no layout's candidates. The same toy capture recorded with every body is read (the control). Under a second.

Run:  python3 tests/unit/check_predicted_picks_only.py
"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile

from kvcost.predict_accuracy import layouts, uniform
from kvcost.predict_accuracy import predicted as PD
from kvcost.cli.predict import capture_dirs, default_set
from kvcost.predict_accuracy.predicted import PREDICTED
from kvevaluator.output_tree import Capture

from kvevaluator.capture.step_record import BASE_OPS, READ_BYTE_LANES  # (the lanes, one definition)
COLS = ("query", "body", "param_idx", "ok", *BASE_OPS, *READ_BYTE_LANES)
WHY = "not every body"
CELLS = ("props_split_adj_split", "props_condensed_adj_condensed")
PARAMS = [str(i) for i in range(1, 6)]


def write_set(root, bodies):
    """the default tier's predicted set (default_set()) under `root`: two cells, one query q1 with one body, params 1-5"""
    cap = Capture(capture_dirs(3, "interactive", (default_set(),), root)[0])
    os.makedirs(cap.dir("counts"), exist_ok=True)
    for cell in CELLS:
        with open(cap.path_of("counts", cell), "w", newline="") as f:
            w = csv.DictWriter(f, COLS)
            w.writeheader()
            for p in PARAMS:
                w.writerow({**{c: 0 for c in COLS}, "query": "q1", "body": "a", "param_idx": p, "ok": "1", "G": 1})
        json.dump({"measured_as": PREDICTED, "bodies": bodies}, open(cap.record(cell), "w"))
    return cap.path


def outcome(fn):
    """'read' when fn returns, the refusal's text when it exits"""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
        return "read"
    except SystemExit as e:
        return str(e)


def main():
    fails, cases = [], 0
    cell = CELLS[0]
    workload = {"id": "toy", "items": [("interactive", "q1", 1.0, None)]}
    for bodies in ("picks", "all"):
        with tempfile.TemporaryDirectory() as t:
            cap = write_set(t, bodies)
            readers = {
                "predicted.by_body": lambda: PD.by_body(cap, cell),
                "predicted.pick": lambda: PD.pick(cap, cell, view="measured"),
                "layouts.load_predicted": lambda: layouts.load_predicted(cap, CELLS, ["q1"], PARAMS),
                "uniform.uniform_cost": lambda: uniform.uniform_cost(3, workload, default_set(), t, "wops", view="measured"),
            }
            for name, fn in readers.items():
                got = outcome(fn)
                ok = (WHY in got) if bodies == "picks" else (WHY not in got)
                cases += 1
                print(f"  {'ok  ' if ok else 'FAIL'} {name} on a capture of bodies={bodies}: "
                      f"{'refused by name' if WHY in got else got[:160]}")
                if not ok:
                    fails.append((name, bodies))
    ok = not fails
    print(f"predicted picks-only refusal {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
