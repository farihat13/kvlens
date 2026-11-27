#!/usr/bin/env python3
"""grid_pins.carry_scales: a rewrite of the record at the deciding scale carries a smaller scale's entry when the deciding
pick it was measured against is unchanged, and drops it (named) when that pick, the mix's spec or the cell changed.
Synthetic records, no captures.    python3 tests/gates/meta/check_record_carry_over.py"""
import argparse
import copy
import importlib.util
import inspect
import os
import sys

from kvevaluator.paths import ROOT


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--module-dir", default=str(ROOT / "python" / "kvcost" / "layout_cost"),
                    help="where grid_pick_record.py is (a dry run on a copy)")
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("grid_pins", os.path.join(a.module_dir, "grid_pick_record.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    if "carry_scales(" not in inspect.getsource(P.write):
        print("FAIL: grid_pins.write() does not call carry_scales")
        return 1
    e_is7 = {"body": "default", "body_sha": "aaa", "recorded_body_ratio": 1.198, "why": "the sf3 plan is 1.198x ..."}
    e_ic1 = {"body": "whole_row_read", "body_sha": "bbb", "recorded_body_ratio": 1.2, "why": "..."}
    fp = {"binary": "e97", "inputs": "102e", "params": [6, 7, 8, 9, 10]}
    prev = {"decided_at": "3", "spec_sha": {"w16_b1": "s1"},
            "cells": {"props_split_adj_split": {"is7": {"body": "whole_row_read_not_type_index_single_posting", "body_sha": "be37"},
                                                "ic1": {"body": "default", "body_sha": "ba99"}},
                      "w16_b1": {"ic1": {"body": "default", "body_sha": "ba99"}}},
            "scales": {"0.3": {"props_split_adj_split": {"is7": e_is7}, "w16_b1": {"ic1": e_ic1}}},
            "scale_fingerprints": {"0.3": {"props_split_adj_split": fp, "w16_b1": fp}}}
    fresh = {"cells": copy.deepcopy(prev["cells"]), "spec_sha": dict(prev["spec_sha"]), "scales": {}, "scale_fingerprints": {}}
    bad = 0

    def case(name, rec, want_carried, want_dropped):
        nonlocal bad
        carried, dropped = P.carry_scales(prev, rec)
        got_c = sorted((sf, c, q) for sf, c, q, _ in carried)
        got_d = sorted((sf, c, q) for sf, c, q, _ in dropped)
        ok = got_c == sorted(want_carried) and got_d == sorted(want_dropped) and all(w for _, _, _, w in dropped)
        ok = ok and all(rec["scales"].get(sf, {}).get(c, {}).get(q) == prev["scales"][sf][c][q] for sf, c, q in want_carried if q != "(fingerprint)")
        ok = ok and all(q != "(fingerprint)" or rec["scale_fingerprints"].get(sf, {}).get(c) == fp for sf, c, q in want_carried)
        ok = ok and all(q not in rec["scales"].get(sf, {}).get(c, {}) for sf, c, q in want_dropped if q != "(fingerprint)")
        print(f"{'ok  ' if ok else 'FAIL'} {name}: carried {len(carried)}, dropped {len(dropped)}" + "".join(f"\n        dropped sf{sf} {c} {q}: {w}" for sf, c, q, w in dropped))
        bad += 0 if ok else 1

    A, M = "props_split_adj_split", "w16_b1"
    all_c = [("0.3", A, "is7"), ("0.3", M, "ic1"), ("0.3", A, "(fingerprint)"), ("0.3", M, "(fingerprint)")]
    case("every deciding pick unchanged: all carried", copy.deepcopy(fresh), all_c, [])
    r = copy.deepcopy(fresh); r["cells"][A]["is7"] = {"body": "adjacency_lists_reverse_adjacency", "body_sha": "9bc2"}
    case("the cell's deciding pick for is7 changed: that entry dropped, the rest carried", r,
         [x for x in all_c if x != ("0.3", A, "is7")], [("0.3", A, "is7")])
    r = copy.deepcopy(fresh); r["cells"][A]["is7"] = {"body": "whole_row_read_not_type_index_single_posting", "body_sha": "NEW"}
    case("the same variant with a new body sha: dropped", r, [x for x in all_c if x != ("0.3", A, "is7")], [("0.3", A, "is7")])
    r = copy.deepcopy(fresh); r["spec_sha"][M] = "s2"
    case("the mix re-solved under its name: its entry and fingerprint dropped", r,
         [x for x in all_c if x[1] != M], [("0.3", M, "ic1"), ("0.3", M, "(fingerprint)")])
    r = copy.deepcopy(fresh); del r["cells"][M]; del r["spec_sha"][M]
    case("the cell gone from the record: dropped", r, [x for x in all_c if x[1] != M], [("0.3", M, "ic1"), ("0.3", M, "(fingerprint)")])
    r = copy.deepcopy(fresh)
    c, d = P.carry_scales({}, r)
    ok = (c, d) == ([], []) and r["scales"] == {} and r["scale_fingerprints"] == {}
    print(f"{'ok  ' if ok else 'FAIL'} no previous record: nothing carried or dropped"); bad += 0 if ok else 1
    print(f"RESULT: {'PASS' if not bad else 'FAIL'} (6 cases, {bad} failed)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
