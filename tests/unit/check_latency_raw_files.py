#!/usr/bin/env python3
"""cpplatency's raw-file layout (measure/latency_raw_files.py) and its readers in kvcost/latency/steps.py, on files
planted with the writer's own raw_path: a one-body run (the default, every regime) reads <query>/p<n>/ only, a
--compare-bodies run <query>/<body>/p<n>/ only, each refusing a file of the other shape; a meta from before the flag
naming two bodies is refused; steps.py's raw_pattern / timer_pair_ns / raw_medians read a one-body run.

Run:  python3 tests/unit/check_latency_raw_files.py
"""
import glob
import json
import os
import sys
import tempfile

from kvevaluator.measure.latency_raw_files import compare_bodies, raw_cells, raw_file, raw_files, raw_globs, raw_path
from kvcost.latency import steps as ST

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def plant(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def meta(out, **m):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "steps.csv.meta.json"), "w") as f:
        json.dump(m, f)


def refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return ""


def kv_step(wall, io, block):
    return {"kind": "kv", "wall_ns": wall, "kv": {"io_read_nanos": io, "block_read_time": block}}


def main():
    root = tempfile.mkdtemp(prefix="latency-raw-files-")
    arm = "meta-c"
    # a one-body run: steady (cpplatency run_steady) and cold under --candidates (grid_latency), both at <query>/p<n>/
    one = os.path.join(root, "one")
    meta(one, compare_bodies=False, candidates={"ic2": ["ic2_scan"]})
    steady = {"median": {"kv_timing": {"timer_pair_ns": 21}},
              "measured": [{"steps": [kv_step(10, 1, 2), kv_step(5, 1, 0)]}, {"steps": [kv_step(30, 3, 4)]},
                           {"steps": [kv_step(20, 2, 3), {"kind": "op", "wall_ns": 99}]}]}
    ps = plant(raw_path(one, arm, "ic1", 1, "steady"), steady)
    cold = {"kv_timing": {"timer_pair_ns": 33},
            "steps": [{"kind": "kv", "per_iteration": [{"wall_ns": 7, "kv": {"io_read_nanos": 1}},
                                                       {"wall_ns": 9, "kv": {"io_read_nanos": 2}}]}]}
    pc = plant(raw_path(one, arm, "ic2", 1, "cold"), cold)

    case("raw_path: no body level by default, one under a body name",
         ps.endswith(os.path.join(arm, "ic1", "p1", "steady.json"))
         and raw_path(one, arm, "ic2", 1, "cold", body="b").endswith(os.path.join(arm, "ic2", "b", "p1", "cold.json")))
    case("a one-body run: raw_file finds each regime's cell, None when absent",
         not compare_bodies(one) and raw_file(one, arm, "ic1", 1, "steady") == ps
         and raw_file(one, arm, "ic2", 1, "cold") == pc and raw_file(one, arm, "ic1", 2, "steady") is None)
    case("a one-body run: raw_files keyed by query and param; raw_cells for --regen, no body",
         raw_files(one, arm, "steady") == {"ic1": {"1": ps}} and raw_files(one, arm, "cold") == {"ic2": {"1": pc}}
         and raw_cells(one, arm, "ic2") == [(None, 1, "cold", pc)])
    case("raw_globs below a dir of outs: the one-body shape",
         sorted(p for g in raw_globs("*") for p in glob.glob(os.path.join(root, "*", g))) == sorted([ps, pc]))

    case("steps.raw_pattern + timer_pair_ns: steady's pair", ST.timer_pair_ns(one, ST.raw_pattern("steady.json")) == 21)
    case("steps.raw_pattern + timer_pair_ns: cold's pair", ST.timer_pair_ns(one, ST.raw_pattern("cold.json")) == 33)
    got = ST.raw_medians(one, arm, "ic1", 1)
    case("steps.raw_medians: steady, per measured run the kv steps' sum, then the median", got == (20.0, 2.0, 3.0), got)
    got = ST.raw_medians(one, arm, "ic2", 1, regime="cold")
    case("steps.raw_medians: cold, per iteration", got == (8.0, 1.5, 0.0), got)
    case("steps.raw_medians: None with no raw file", ST.raw_medians(one, arm, "ic9", 1) is None)

    # a body folder where a one-body run keeps none: every reader refuses it
    plant(raw_path(one, arm, "ic2", 1, "cold", body="ic2_point"), cold)
    why = [refused(lambda: raw_file(one, arm, "ic2", 1, "cold")), refused(lambda: raw_files(one, arm, "cold")),
           refused(lambda: raw_cells(one, arm, "ic2")), refused(lambda: ST.raw_medians(one, arm, "ic2", 1, regime="cold"))]
    case("a body folder in a one-body run: refused by raw_file, raw_files, raw_cells and raw_medians",
         all("other shape" in w for w in why), why)

    # a --compare-bodies run: the body shape only, never read as one plan's walls
    many = os.path.join(root, "many")
    meta(many, compare_bodies=True, candidates={"ic2": ["a", "b"]})
    pa = plant(raw_path(many, arm, "ic2", 1, "warm", body="a"), cold)
    pb = plant(raw_path(many, arm, "ic2", 1, "warm", body="b"), cold)
    pu = plant(raw_path(many, arm, "ic1", 1, "warm"), cold)          # a query --candidates does not name: its bundle
    case("a --compare-bodies run: raw_cells gives each named body's cells, and an unnamed query's at <query>/p<n>/",
         compare_bodies(many) and raw_cells(many, arm, "ic2") == [("a", 1, "warm", pa), ("b", 1, "warm", pb)]
         and raw_cells(many, arm, "ic1") == [(None, 1, "warm", pu)])
    why = [refused(lambda: raw_file(many, arm, "ic2", 1, "warm")), refused(lambda: raw_files(many, arm, "warm"))]
    case("a --compare-bodies run: raw_file and raw_files refuse it (its walls mix plans)",
         all("mix plans" in w for w in why), why)
    plant(raw_path(many, arm, "ic2", 2, "warm"), cold)
    case("a --compare-bodies run holding a one-body cell: raw_cells refuses it",
         "other shape" in refused(lambda: raw_cells(many, arm, "ic2")))

    # a meta from before the flag: two bodies of one query cannot be told apart from one
    old = os.path.join(root, "old")
    meta(old, candidates={"ic2": ["a", "b"]})
    case("a meta from before --compare-bodies naming two bodies is refused",
         "before --compare-bodies" in refused(lambda: compare_bodies(old)))
    meta(old, candidates={"ic2": ["a"]})
    case("a meta from before the flag naming one body per query is a one-body run", compare_bodies(old) is False)

    print(f"check_latency_raw_files {'OK' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} cases")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
