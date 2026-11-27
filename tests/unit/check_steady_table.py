#!/usr/bin/env python3
"""kvcost.latency.steady_table's weighting and ratios on synthetic per-query rows (no data, no store):
  1. equal_share gives each query 1 / its cost on the reference, so the reference's weighted cost is the query count;
  2. ratios: hand-worked wall, KV and cost ratios and the KV share, under equal-share and under equal weights;
  3. a query missing from the store or the reference is refused;
  4. the wall of record: a query whose passes are bimodal (a two-mode toy: is2 p1 on Sp-Co's 10
     measured walls) takes the MEAN over passes, not the median; then the MEAN over params (the
     median over params, reachable as OVER_PARAMS = median, is the control); the median
     run's ratio is kept beside it (ratios' *_median); the band (pass_ratios' min and max) holds every pass's ratio;
     pass_band is None for a capture that does not record its pass order;
  5. on files: the mean and the band count measured passes 1-9 (pass 0 left out), a 1-pass capture
     keeps its single run, and pass_band's min and max; None when the passes do not pair;
  6. the writes: write_per_query reads one apply wall per (query, param), its kv steps' wall and its counts' combined
     cost, the mean over params (the median beside it); pass_band adds a write's one wall to every pass (fixed).
  7. capture_costs reads each timed store's counts from the capture that holds it (output_tree's layout rule): a
     layout measured itself from captures/, a layout only composed from composed/; a store with no counts is left out,
     and every store without a picks.json;
  8. per_query's cost is the plan row's own counts (no capture needed); with the capture's rows, equal counts give the
     same cost and other counts are refused.

    python3 tests/unit/check_steady_table.py [--plant weight]   (the plant must exit 1)
"""
import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

from kvcost.latency import steady_table
from kvevaluator.output_tree import capture_in

FAILED = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


def close(a, b):
    return abs(a - b) <= 1e-12 * max(1.0, abs(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=("weight",), help="a negative control: double one weight (must exit 1)")
    a = ap.parse_args()
    # per query: (wall, kv time, combined cost, median-run wall, median-run kv time); the last two = the first two here
    ref = {"q1": (10.0, 8.0, 2.0, 10.0, 8.0), "q2": (100.0, 90.0, 50.0, 100.0, 90.0), "q3": (1.0, 1.0, 0.5, 1.0, 1.0)}
    store = {"q1": (5.0, 4.0, 4.0, 5.0, 4.0), "q2": (200.0, 150.0, 25.0, 200.0, 150.0), "q3": (2.0, 1.0, 1.0, 2.0, 1.0)}

    share = steady_table.equal_share(ref)
    if a.plant:
        share["q2"] *= 2
    check("1 weights are 1 / the reference's cost", all(close(share[q], 1 / ref[q][2]) for q in ref), share)
    check("1 the reference's weighted cost is the query count", close(sum(share[q] * ref[q][2] for q in ref), 3.0))

    # equal share: weights 0.5, 0.02, 2. wall: (2.5 + 4 + 4) / (5 + 2 + 2) = 10.5 / 9; kv: (2 + 3 + 2) / (4 + 1.8 + 2)
    # = 7 / 7.8; cost: (2 + 0.5 + 2) / 3 = 1.5; kv share: 7 / 10.5
    r = steady_table.ratios(store, ref, share)
    want = {"wall_ratio": 10.5 / 9, "kv_ratio": 7 / 7.8, "cost_ratio": 1.5, "kv_share": 7 / 10.5}
    check("2 equal-share ratios", all(close(r[k], v) for k, v in want.items()), (r, want))
    # equal weights: wall 207 / 111; kv 155 / 99; cost 30 / 52.5; kv share 155 / 207
    e = steady_table.ratios(store, ref, {q: 1.0 for q in ref})
    want = {"wall_ratio": 207 / 111, "kv_ratio": 155 / 99, "cost_ratio": 30 / 52.5, "kv_share": 155 / 207}
    check("2 equal-weight ratios", all(close(e[k], v) for k, v in want.items()), (e, want))
    check("2 the reference over itself is 1", all(close(v, 1.0) for k, v in steady_table.ratios(ref, ref, share).items()
                                                  if k != "kv_share"))

    for name, s, rf in (("store", {q: v for q, v in store.items() if q != "q3"}, ref),
                        ("reference", store, {q: v for q, v in ref.items() if q != "q3"})):
        try:
            steady_table.ratios(s, rf, share)
            check(f"3 a query missing from the {name} is refused", False, "not refused")
        except SystemExit as err:
            check(f"3 a query missing from the {name} is refused", "q3" in str(err), str(err))
    # 4. the wall of record: a two-mode query (is2 p1 on Sp-Co, ms) and a steady one
    is2 = [28.7, 28.6, 2.4, 28.5, 1.7, 28.8, 17.4, 1.9, 28.7, 20.1]
    rec = getattr(steady_table, "wall_of_record", None)     # absent on the code before the rule: the check fails
    got = rec({"1": [(w, w / 2) for w in is2]}) if rec else None
    check("4 a bimodal query's wall of record is the mean over passes, not the median",
          got is not None and close(got[0], statistics.fmean(is2)) and not close(got[0], statistics.median(is2))
          and close(got[1], statistics.fmean(is2) / 2), (got, statistics.fmean(is2), statistics.median(is2)))
    three = {"1": [(1.0, 0.0), (3.0, 0.0)], "2": [(10.0, 0.0)] * 3, "3": [(2.0, 0.0), (4.0, 0.0)]}   # means 2, 10, 3
    got = rec(three) if rec else None
    check("4 then the mean over params of the per-param means (5; the median would be 3)",
          got is not None and close(got[0], 5.0), got)
    rule = steady_table.OVER_PARAMS
    steady_table.OVER_PARAMS = statistics.median
    got = rec(three)
    steady_table.OVER_PARAMS = rule
    check("4 the control: the rule before (the median over params) gives 3", close(got[0], 3.0), got)
    two = {"q1": (6.0, 3.0, 1.0, 9.0, 4.0)}
    r = steady_table.ratios(two, {"q1": (3.0, 1.0, 1.0, 3.0, 2.0)}, {"q1": 1.0})
    check("4 ratios: the wall of record's ratio, and the median run's kept beside it",
          close(r["wall_ratio"], 2.0) and close(r.get("wall_ratio_median", float("nan")), 3.0)
          and close(r.get("kv_ratio_median", float("nan")), 2.0), r)
    per_pass_store, per_pass_ref = {"q1": [5.0, 6.0, 9.0, 4.0]}, {"q1": [3.0, 3.0, 3.0, 2.0]}
    pr = steady_table.pass_ratios(per_pass_store, per_pass_ref, {"q1": 1.0})
    check("4 the band (pass 1 on) holds every pass's ratio", pr == [2.0, 3.0, 2.0] and min(pr) == 2.0 and max(pr) == 3.0, pr)
    band = getattr(steady_table, "pass_band", None)
    with tempfile.TemporaryDirectory() as tmp:
        for d in ("a", "b"):
            (Path(tmp) / d).mkdir()
            json.dump({"regimes": {"steady": {}}}, open(Path(tmp) / d / "steps.csv.meta.json", "w"))
        got = band(Path(tmp) / "a" / "steps.csv", Path(tmp) / "b" / "steps.csv", {"q1": 1.0}) if band else "absent"
    check("4 pass_band is None for a capture that does not record its pass order", got is None, got)
    # 5. the readers on files: a synthetic wall capture (meta, steps.csv, raw steady.json per cell) for two stores
    with tempfile.TemporaryDirectory() as tmp:
        def capture(name, walls, rule="measured-index"):
            """walls: {(query, param): [plan wall per measured pass]}; each run has one kv step of half its wall"""
            d = Path(tmp) / name / f"meta-{name}" / "steady-10mb-p2"   # <timings>/<arm>/<regime>/ (the output tree's layout)
            d.mkdir(parents=True)
            json.dump({"regimes": {"steady": {"pass_order_rule": rule}}}, open(d / "steps.csv.meta.json", "w"))
            with open(d / "steps.csv", "w") as f:
                f.write("query,param_idx,step,kind,wall_ns\n")
                for (q, p) in walls:
                    f.write(f"{q},{p},__plan__,plan,0\n")
            for (q, p), ws in walls.items():
                raw = d / f"meta-{name}" / q / f"p{p}" / "steady.json"
                raw.parent.mkdir(parents=True)
                runs = [{"totals": {"work_ns": w - 1, "overhead_ns": 1}, "steps": [{"kind": "kv", "wall_ns": w / 2},
                                                                                   {"kind": "flow", "wall_ns": 7}]} for w in ws]
                json.dump({"median": runs[0], "measured": runs}, open(raw, "w"))
            return d / "steps.csv"
        a = capture("a", {("q1", "1"): [1.0, 9.0, 2.0, 4.0], ("q1", "2"): [3.0, 3.0, 3.0, 3.0]})
        b = capture("b", {("q1", "1"): [2.0, 2.0, 2.0, 2.0], ("q1", "2"): [1.0, 1.0, 1.0, 1.0]})
        runs = steady_table.cell_runs(a.parent, a.parent.parent.name, "q1", "1") if hasattr(steady_table, "cell_runs") else None
        check("5 cell_runs reads each counted run's wall and its kv steps' wall, in pass order, pass 0 left out",
              runs == [(9.0, 4.5), (2.0, 1.0), (4.0, 2.0)], runs)
        first = getattr(steady_table, "FIRST_PASS", None)
        check("5 the mean and the band start at the same pass (pass_ratios' default is FIRST_PASS)",
              first == 1 and steady_table.pass_ratios.__defaults__ == (first,), (first, steady_table.pass_ratios.__defaults__))
        got = steady_table.wall_of_record({p: steady_table.cell_runs(a.parent, a.parent.parent.name, "q1", p) for p in ("1", "2")})
        check("5 the wall of record from files: the mean over params of the means over passes 1-3 (5 and 3)",
              close(got[0], 4.0) and close(got[1], 2.0), got)
        # per pass, the mean over params of a: [2, 6, 2.5, 3.5]; of b: [1.5] * 4; the band from pass 1: 4, 5/3, 7/3
        got = band(a, b, {"q1": 1.0}) if band else None
        check("5 pass_band on files: the min and max of passes 1-3's ratio", got is not None and close(got[0], 2.5 / 1.5)
              and close(got[1], 6 / 1.5), got)
        c = capture("c", {("q1", "1"): [2.0, 2.0], ("q1", "2"): [1.0, 1.0]})
        check("5 pass_band is None when the two stores hold other pass counts", band(a, c, {"q1": 1.0}) is None if band else False)
        d1, d2 = capture("d1", {("q1", "1"): [2.0]}), capture("d2", {("q1", "1"): [3.0]})
        try:
            got = band(d1, d2, {"q1": 1.0}) if band else "absent"
        except ValueError as err:
            got = f"ValueError: {err}"
        check("5 pass_band is None when both hold one pass (no pass after pass 0)", got is None, got)
        runs = steady_table.cell_runs(d1.parent, d1.parent.parent.name, "q1", "1")
        check("5 a 1-pass capture keeps its single run (the control re-time)", runs == [(2.0, 1.0)], runs)
        # 6. a write capture: iu1 on params 1-3, one apply each (walls 4, 8, 3), one kv step of half the wall, and
        # S = 1, P = 1, 4096 bytes written per request: wops 1.1 + 1 = 2.1, wops_bytes4k 3.1
        w = Path(tmp) / "w.csv"
        with open(w, "w") as fh:
            fh.write("query,param_idx,step,kind,wall_ns,S,G,N,P,D,write_key_bytes,write_value_bytes\n")
            for p, wall in (("1", 4.0), ("2", 8.0), ("3", 3.0)):
                fh.write(f"iu1,{p},__plan__,plan,{wall},1,0,0,1,0,96,4000\n")
                fh.write(f"iu1,{p},s0,kv,{wall / 2},1,0,0,1,0,96,4000\n")
        got = steady_table.write_per_query(w)["iu1"]
        check("6 write_per_query: the mean over params (5), its kv steps' (2.5), the cost (3.1), the medians (4, 2)",
              all(close(x, y) for x, y in zip(got, (5.0, 2.5, 3.1, 4.0, 2.0))), got)
        got = band(a, b, {"q1": 1.0, "iu1": 1.0}, {"iu1": (3.0, 1.5)})
        check("6 pass_band adds a write's one wall to every pass: (6 + 3) / 3 the max, (2.5 + 3) / 3 the min",
              got is not None and close(got[0], 5.5 / 3) and close(got[1], 9 / 3), got)
    # 7. a tree's BI place: `own` measured itself (counts and run record in captures/), `mix` only composed. Each holds
    # the body timed (b1) and another body (b0), which is left out
    with tempfile.TemporaryDirectory() as tmp:
        place = Path(tmp) / "captures" / "ldbc" / "sf0.1"
        for kind, layout in (("captures", "own"), ("composed", "mix")):
            counts = Path(tmp) / kind / "ldbc" / "sf0.1" / "bi" / "reported" / "counts"
            counts.mkdir(parents=True)
            (counts / f"{layout}.csv").write_text(f"query,param_idx,body,S\nbi1,1,b0,{len(kind)}\nbi1,1,b1,{len(layout)}\n")
            if kind == "captures":
                (counts / f"{layout}.json").write_text("{}")
        picks = Path(tmp) / "picks.json"
        json.dump({"cells": {"own": {"bi1": "b1"}, "mix": {"bi1": "b1"}}}, open(picks, "w"))
        try:
            got = steady_table.capture_costs(picks, capture_in(place, "bi"))
            got = {store: {k: (r["body"], r["S"]) for k, r in rows.items()} for store, rows in got.items()}
        except (OSError, AttributeError, TypeError) as err:
            got = f"{type(err).__name__}: {err}"
        want = {store: {("bi1", "1"): ("b1", "3")} for store in ("own", "mix")}
        check("7 capture_costs: a measured layout's counts from captures/, an only-composed layout's from composed/",
              got == want, got)
        json.dump({"cells": {"own": {"bi1": "b1"}, "none": {"bi1": "b1"}}}, open(picks, "w"))
        got = steady_table.capture_costs(picks, capture_in(place, "bi"))
        check("7 capture_costs leaves out a store with no counts in the tree, and returns {} without picks.json",
              sorted(got) == ["own"] and steady_table.capture_costs(Path(tmp) / "no.json", capture_in(place, "bi")) == {},
              sorted(got))
    # 8. the cost from the plan row: S = 2, G = 3, N = 100, 8192 bytes of get values: wops 3 + 2.2 + 1 = 6.2, + 2
    from kvcost.layout_cost import cost_models
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "meta-s" / "steady-10mb-p2"
        d.mkdir(parents=True)
        lanes = [f"{x}_bytes" for x in (*cost_models.READ_LANES, *cost_models.WRITE_LANES)]
        vals = {x: 0 for x in lanes}
        vals["get_value_bytes"] = 8192
        with open(d / "steps.csv", "w") as f:
            f.write("query,param_idx,step,kind,wall_ns,S,G,N,P,D," + ",".join(lanes) + "\n")
            f.write("q1,1,__plan__,plan,10,2,3,100,0,0," + ",".join(str(vals[x]) for x in lanes) + "\n")
        raw = d / "meta-s" / "q1" / "p1" / "steady.json"
        raw.parent.mkdir(parents=True)
        run = {"totals": {"work_ns": 9, "overhead_ns": 1}, "steps": [{"kind": "kv", "wall_ns": 4}]}
        json.dump({"median": run, "measured": [run, run]}, open(raw, "w"))
        own = steady_table.per_query(d / "steps.csv")["q1"][2]
        row = {"S": "2", "G": "3", "N": "100", "P": "0", "D": "0", "get_value": "8192"}
        same = steady_table.per_query(d / "steps.csv", {("q1", "1"): row})["q1"][2]
        try:
            steady_table.per_query(d / "steps.csv", {("q1", "1"): {**row, "G": "4"}})
            other = "not refused"
        except SystemExit as err:
            other = str(err)
        check("8 per_query: the cost from the plan row's own counts (8.2), the same with the capture's equal row; other "
              "counts refused", close(own, 8.2) and close(same, 8.2) and "other counts" in other, (own, same, other))
    print(f"check_steady_table: {CHECKS[0]} checks, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
