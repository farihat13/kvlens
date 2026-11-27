#!/usr/bin/env python3
"""The test of the mixed-layout composition: for each BUILT mixed store (a spec dir's mixes, measured into
output/captures/ldbc/sf<SF>/ by kvcost measure <spec dir>), what the store measured against what compose.py composes
for the same assignment from the uniform captures, through the one comparer, kvcost.layout_cost.composed_vs_measured
(`compare`: every plan pick, every candidate set, every step row in every raw channel, the workload sums, and the one
miss-key rule; its doc). A part Chunked at any chunk size is compared: the sizes come from the spec, each composed from
that size's Chunked/Chunked in --grid. Before comparing, each mix's capture must hold HEAD's candidate set (a capture
that predates a plan file, or ran a body HEAD now refuses, is a finding). A spec with per-direction adjacency
(adjacency_by_etype_dir) is composed on per-direction units; --by-direction composes every mix so.
--swap out|in is the positive control of the direction: the composed side takes that direction's lists at the OTHER
direction's layout; every spec whose directions differ must then differ from its measured store (exit 0 only then).
Which workloads: a spec dir with a <spec dir>-picks.json (mixes_w14, mixes_w16) must be compared on the workload it was
picked for, on the families captured; a dir without one, on every workload, whole (--workload: those named). A required
workload not compared, or 0 cells: exit 1. Exit 2: the inputs cannot be compared (no built mix measured; the message
says how to measure them).

    python3 tests/gates/check_mix_vs_built.py --sf 0.1 [--grid DIR] [--spec-dir inputs/specs/mixes_w16]   (exit 1 on any difference)
"""
import argparse
import json
import os
import sys
from kvevaluator.paths import ROOT

from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import composed_vs_measured as CVM
from kvcost.layout_cost.compose import add_pick_by_arg
from kvcost.layout_cost.picks import PlanRule
from kvevaluator.output_tree import Tree

EXP = str(ROOT)


def picked_for(spec_dir):
    """the workload id the dir's mixes were picked for (its <spec dir>-picks.json, layout_choice's picks), or None"""
    p = spec_dir + "-picks.json"
    return json.load(open(p)).get("workload") if os.path.exists(p) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", default=None, help="the measured <dataset>/<scale> dir of the uniform layouts (default: the "
                                                 "output tree's)")
    ap.add_argument("--spec-dir", default=os.path.join(EXP, "inputs", "specs", "mixes"),
                    help="the built mixes' spec dir (default mixes/)")
    ap.add_argument("--swap", choices=("out", "in"), default=None,
                    help="the control: compose that direction's lists at the other direction's layout; every spec "
                         "whose directions differ must then DIFF")
    ap.add_argument("--by-direction", action="store_true", help="compose every mix on per-direction units, a per type mix too "
                    "(both directions of an edge type at its one layout): the same store, so the same picks and costs")
    benchmarks.add_workload_arg(ap)
    ap.add_argument("--built", default=None, help="the measured <dataset>/<scale> dir of the built mixes (default: --grid's)")
    add_pick_by_arg(ap)
    ap.add_argument("--in-sample", action="store_true", help="pick a capture with no held-out set in-sample, on both sides "
                    "(a new mix: the pick record holds no row for it); every output then says 'in-sample'")
    a = ap.parse_args()
    try:
        return run(a)
    except CVM.Refused as e:
        print(f"measured vs composed: REFUSED: {e}")
        return 2


def run(a):
    selected = benchmarks.select("ldbc", a.sf, a.workload)
    fams = (benchmarks.families_read("ldbc", list(selected.values())) if a.workload
            else tuple(benchmarks.FAMILIES["ldbc"].items()))
    grid = a.grid or str(Tree().place_dir("captures", "ldbc", a.sf))
    spec_dir = os.path.abspath(a.spec_dir)
    found = CVM.mixes_in(spec_dir, a.built or grid, fams, "ldbc")
    # the workloads every measured mix must be compared on: the one the dir's mixes were picked for (W14, I01), on the
    # families its capture has; with no picks file, every workload selected, whole
    for_id = picked_for(spec_dir)
    required = {n for n, w in selected.items() if w["id"] == for_id} if for_id else set(selected)
    print(f"mixes of {os.path.basename(spec_dir)}: " + (f"picked for {for_id} ({os.path.basename(spec_dir)}-picks.json)"
          if for_id else "no picks file: every workload is compared whole") + f"; {len(found)} measured under "
          f"{os.path.relpath(a.built or grid, EXP)}: {' '.join(m.name for m in found) or '-'}")
    if not found:
        raise CVM.Refused(f"no built mix of {os.path.relpath(spec_dir, EXP)} is captured under {a.built or grid}: measure "
                          f"them first (kvcost measure --dataset ldbc={a.sf} --workload i01 {os.path.relpath(spec_dir, EXP)}; "
                          f"run/meta-gates.sh does, before this gate), or name the tree that holds them (--built)")
    drift = [] if a.swap else CVM.drift_findings(found, fams)
    res = CVM.compare(grid, found, sf=a.sf, dataset="ldbc", families=fams, workloads=selected, criterion=a.criterion,
                      rule=PlanRule(in_sample=True if a.in_sample else None), by_direction=a.by_direction, swap=a.swap,
                      required=required, part_ok=bool(for_id))
    for line in res.notes:
        print(line)
    print("\n" + res.counts_line(a.sf))
    print(res.miss_line())
    findings = drift + res.findings
    for b in findings[:40]:
        print("  DIFF:", b)
    if a.swap:   # the control: each per-direction spec must be caught
        missed = sorted(n for n, k in res.caught.items() if not k)
        print(f"control --swap {a.swap}: {len(res.caught) - len(missed)} of {len(res.caught)} per-direction specs caught "
              f"({', '.join(f'{n}: {k} findings' for n, k in sorted(res.caught.items()))})"
              + (f"; NOT caught: {missed}" if missed else ""))
        ok = bool(res.caught) and not missed
        print(f"control: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    ok = res.ok and not drift
    print(f"measured vs composed: {'PASS' if ok else f'FAIL: {len(findings)} findings'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
