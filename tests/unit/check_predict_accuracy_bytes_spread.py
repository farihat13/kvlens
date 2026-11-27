#!/usr/bin/env python3
"""Two rules of the predict-accuracy reports, on toy rows:
  bytes    report.priced: a step's bytes are cost_models' "bytes" (the 7 read lanes, the key bytes of lookups that missed
           included), hit_bytes (the hit keys and values) is kept beside it; a row with an empty byte lane (a hit or a
           miss lane) has neither. Then report.main on a toy steps.csv: ratios.csv's bytes_ratio and summary.md's
           ratio table are on the 7 lanes
  spread   compare.spread: each layout x over the measured best (Sp-Co, Co-Co, the median, the worst) and the gap share
           (Sp-Co - pick) / (Sp-Co - best), empty when Sp-Co is the best or absent
No store, no binary. Under a second.

Run:  python3 tests/unit/check_predict_accuracy_bytes_spread.py
"""
import contextlib
import csv
import json
import io
import math
import os
import sys
import tempfile

from kvcost.layout_cost import compare as CP
from kvcost.layout_cost import cost_models as CM
from kvcost.predict_accuracy import layouts as LY
from kvcost.predict_accuracy import predicted as PD
from kvcost.predict_accuracy import report as RP

fails, cases = [], 0


def case(what, ok, got):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}")
    if not ok:
        fails.append(what)


def step_row(**lanes):
    r = {"S": "1", "G": "2", "N": "3", "S_miss": "0", "G_miss": "1", "N_miss": "0", "get_key": "10", "get_value": "100",
         "scan_key": "20", "scan_value": "200", "seek_miss_key": "7", "next_miss_key": "5", "get_miss_key": "3",
         "used_key": "0", "used_value": "0", "rows": "1"}
    r.update(lanes)
    return r


def check_bytes():
    r = step_row()
    got = RP.costs_of(r)
    want = CM.cost(PD.vector(r), "bytes")
    case("bytes = cost_models' 7-lane bytes (hit 330 + miss keys 15)", got.get("bytes") == want == 345.0, got.get("bytes"))
    case("hit_bytes kept beside it: the hit keys and values only", got.get("hit_bytes") == 330.0, got.get("hit_bytes"))
    empty = RP.costs_of(step_row(scan_value=""))
    case("an empty byte lane: no bytes and no hit_bytes, not 0",
         math.isnan(empty["bytes"]) and math.isnan(empty["hit_bytes"]), (empty["bytes"], empty["hit_bytes"]))
    miss = RP.costs_of(step_row(get_miss_key=""))
    case("an empty miss lane: no bytes, not the misses priced 0", math.isnan(miss["bytes"]), miss["bytes"])


def check_report():
    """two cells, one step; B predicts the hit lanes exactly and doubles the miss keys: the 7-lane ratio is
    (330 + 30) / 345, the hit-only ratio would be 1"""
    keys = {"query": "q1", "param": "1", "step": "s1", "op": "read_neighbors"}
    with tempfile.TemporaryDirectory() as t:
        rows = []
        for cell in ("props_split_adj_split", "props_condensed_adj_condensed"):
            rows.append({"cell": cell, **keys, "source": "measured", **step_row()})
            rows.append({"cell": cell, **keys, "source": "predicted",
                         **step_row(seek_miss_key="14", next_miss_key="10", get_miss_key="6")})
        with open(os.path.join(t, "steps.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        try:   # no levels.json beside steps.csv: refused, naming what writes it
            RP.main([t, "toy"])
            refused = ""
        except SystemExit as e:
            refused = str(e)
        case("no levels.json: refused, naming kvevaluator predict --levels-out", "--levels-out" in refused, refused)
        with open(os.path.join(t, "levels.json"), "w") as f:   # the toy's one level (the pass writes kLevels)
            json.dump([{"name": "predicted", "card": False, "anchor": False, "sampled": False, "meaning": "the toy's prediction"}], f)
        with contextlib.redirect_stdout(io.StringIO()):
            RP.main([t, "toy"])
        got = [float(r["bytes_ratio_predicted"]) for r in csv.DictReader(open(os.path.join(t, "ratios.csv")))]
        summary = open(os.path.join(t, "summary.md")).read()
    want = 360 / 345
    case("ratios.csv: bytes_ratio_predicted = 7-lane bytes predicted / measured, on both cells",
         len(got) == 2 and all(abs(x - want) < 1e-5 for x in got), got)
    case("summary.md's ratio table: bytes median 1.04 (hit-only would be 1.00)", "| predicted | bytes | 1.04 |" in summary,
         [l for l in summary.splitlines() if "| B | bytes |" in l][:1])


def check_spread():
    best, pick, other = "props_chunked_c1k_adj_chunked_c4k", "props_chunked_c1k_adj_chunked_c16k", "props_split_adj_split"
    meas = {best: 10.0, pick: 11.0, CP.SP_CO: 30.0, CP.CO_CO: 20.0, other: 50.0}
    s = CP.spread(pick, meas, LY.short)
    want = {"sp_co_over_best": 3.0, "co_co_over_best": 2.0, "median_over_best": 2.0, "worst": "Sp-Sp",
            "worst_over_best": 5.0, "gap_share": 0.95}
    case("x over the best, the median of five layouts, the worst, gap share (30 - 11) / (30 - 10)",
         all(s[k] == v if isinstance(v, str) else abs(s[k] - v) < 1e-12 for k, v in want.items()), s)
    s = CP.spread(pick, {best: 10.0, pick: 11.0, CP.SP_CO: 30.0, CP.CO_CO: 20.0}, LY.short)
    case("an even count: the median is the mean of the middle two, (11 + 20) / 2 / 10",
         abs(s["median_over_best"] - 1.55) < 1e-12, s["median_over_best"])
    s = CP.spread(CP.SP_CO, {CP.SP_CO: 10.0, best: 12.0}, LY.short)
    case("Sp-Co is the best: no gap share", math.isnan(s["gap_share"]) and math.isnan(s["co_co_over_best"]), s)
    s = CP.spread(pick, {best: 10.0, pick: 11.0, CP.CO_CO: 15.0}, LY.short)
    case("Sp-Co not among the layouts: no Sp-Co, no gap share",
         math.isnan(s["sp_co_over_best"]) and math.isnan(s["gap_share"]), s)
    row = LY.pick_row("I02", "wops", {best: 1.0, pick: 0.5, CP.SP_CO: 3.0, CP.CO_CO: 2.0, other: 5.0}, meas, "workload")
    case("picks rows carry the spread: the named layout is the pick", row["named"] == pick and row["gap_share"] == 0.95,
         (row["named_short"], row["gap_share"]))


def main():
    check_bytes()
    check_report()
    check_spread()
    ok = not fails
    print(f"predict-accuracy bytes and spread {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
