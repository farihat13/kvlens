#!/usr/bin/env python3
"""A predicted capture's plan is picked by the measured captures' own rule (candidates.pick_body: S, G, N and the bytes
of candidates.READ_LANES, the 7 read lanes), not a second formula: a toy capture where the miss-key bytes (in the rule's
7 lanes, not in a hit-only formula's 4) flip the pick, and a tie at 12 significant digits that goes to the first body name.
Under a second.

Run:  python3 tests/unit/check_predicted_plan_rule.py
"""
import csv
import json
import os
import sys
import tempfile

from kvcost.predict_accuracy import predicted as PD
from kvcost.predict_accuracy.predicted import PREDICTED
from kvevaluator.output_tree import Capture
from kvevaluator.plan_choice import candidates as C

CELL = "props_split_adj_split"
from kvevaluator.capture.step_record import READ_BYTE_LANES  # (the lanes, one definition)
LANES = READ_BYTE_LANES
COLS = ("query", "body", "param_idx", "ok", "S", "G", "N") + LANES

# (query, body, G, get_value bytes, get_miss_key bytes), params 1 and 2 the same
ROWS = [
    # q1: by the rule (7 lanes), "a" costs 10 GETs + 40,960 miss-key bytes (10 GETs at 4,096) = 20 and "b" 15, so the
    # rule picks "b"; a hit-only formula (the 4 hit lanes) would price "a" at 10 and pick it
    ("q1", "a", 10, 0, 40960),
    ("q1", "b", 15, 0, 0),
    # q2: "y" and "z" tie: 3 GETs + 4,096 bytes vs 4 GETs, apart only in the 16th digit; the first name wins
    ("q2", "z", 4, 0, 0),
    ("q2", "y", 3, 4096 * (1 + 1e-15), 0),
]


def write_capture(d):
    cap = Capture(d)
    os.makedirs(cap.dir("counts"), exist_ok=True)
    with open(cap.path_of("counts", CELL), "w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        for q, b, g, gv, miss in ROWS:
            for p in ("1", "2"):
                row = {c: 0 for c in COLS}
                row.update(query=q, body=b, param_idx=p, ok="1", G=g, get_value=gv, get_miss_key=miss)
                w.writerow(row)
    json.dump({"measured_as": PREDICTED, "bodies": "all"}, open(cap.record(CELL), "w"))


def main():
    fails, cases = [], 0

    def case(what, ok, got):
        nonlocal cases
        cases += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}")
        if not ok:
            fails.append(what)

    with tempfile.TemporaryDirectory() as t:
        write_capture(t)
        best, bad = PD.pick(t, CELL, view="measured")
    got = {q: b for q, (b, *_) in best.items()}
    case("the rule's 7 lanes, not the 4 hit lanes: q1 picks b", got.get("q1") == "b", got.get("q1"))
    case("a tie at 12 significant digits goes to the first name: q2 picks y", got.get("q2") == "y", got.get("q2"))
    case("no body left out", not bad, sorted(bad))
    vec = {"y": {"S": 0, "G": 3, "N": 0, "total_bytes": 4096 * (1 + 1e-15)}, "z": {"S": 0, "G": 4, "N": 0, "total_bytes": 0}}
    exact, _ = C.pick_body(vec)
    tied, _ = C.pick_body({"z": vec["z"], "y": vec["y"]}, tie_digits=12)
    case("pick_body: exact compare orders by the noise, tie_digits=12 ties to the first name", (exact, tied) == ("z", "y"),
         (exact, tied))
    ok = not fails
    print(f"predicted plan rule {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
