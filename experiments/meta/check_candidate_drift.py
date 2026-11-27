#!/usr/bin/env python3
"""Does a grid capture still hold the candidate set HEAD's plan files describe? For every cell of a grid capture
(the 9 uniform cells and the built mixes, each in the family's capture below --grid) and every read family,
grid_capture.candidate_drift names (a) HEAD candidates the capture never ran (the capture predates a plan file: its
pick may miss a cheaper body) and (b) captured bodies HEAD now refuses on the cell (choose() would price a body
HEAD marks invalid). Exit 1 on (b) or no capture at all; exit 3 when (a) is the only drift (the readers pick among the
bodies a capture ran and mark it: candidates_unmeasured); 0 when none. Prints the cells covered. The layout-choice
driver runs it first and goes on, marked, on 3 (`--allow-candidate-drift` goes on past 1 too); check_grid /
check_mix_vs_built call the same function.

  python3 experiments/meta/check_candidate_drift.py --sf 3 [--grid output/captures/ldbc/sf3] [--families interactive,microbench]
  python3 experiments/meta/check_candidate_drift.py --dataset linkbench --sf n1m   # --param-set names the capture
"""
import argparse
import glob
import os
import sys
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Capture, Tree, capture_in
from kvevaluator.plan_choice.candidates import capture_tree

SPEC_DIRS = [grid_capture.GRID, os.path.join(EXP, "inputs", "specs", "mixes")]   # no grouped cells


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", default=None, help="the dataset's captures dir at the scale (default output/captures/<dataset>/<scale>)")
    ap.add_argument("--dataset", default="ldbc", choices=("ldbc", "linkbench"),
                    help="ldbc: the LDBC store's workloads (interactive, microbench); linkbench: LinkBench's own grid")
    ap.add_argument("--param-set", default=None, help="the param set the captures hold (LinkBench: anchors, quick, quick-next, log)")
    ap.add_argument("--families", dest="workloads", default=None, help="default: interactive,microbench (suite ldbc), linkbench (suite linkbench)")
    a = ap.parse_args(argv)
    workloads = (a.workloads or ("linkbench" if a.dataset == "linkbench" else "interactive,microbench")).split(",")
    grid = os.path.abspath(a.grid) if a.grid else str(Tree().place_dir("captures", a.dataset, a.sf))
    cells = drift = refused = 0
    for spec_dir in SPEC_DIRS:
        for spec in sorted(glob.glob(os.path.join(spec_dir, "*.json"))):
            cell = os.path.basename(spec)[:-5]
            for wl in workloads:
                rdir = capture_in(grid, wl, a.param_set).path
                runs_csv = Capture(rdir).path_of("counts", cell)
                if not os.path.exists(runs_csv):
                    continue
                cells += 1
                d = grid_capture.candidate_drift(wl, spec, grid_capture.load_runs(rdir, cell), capture_tree(rdir, cell),
                                              pipeline=grid_capture.capture_pipeline_of(rdir, cell))
                for kind in ("unmeasured", "refused"):
                    for q, names in sorted(d[kind].items()):
                        drift += 1
                        refused += kind == "refused"
                        print(f"  DRIFT {os.path.relpath(rdir, EXP)} {cell} {wl} {q}: {kind}: {names}")
    verdict = ("FAIL" if refused or not cells else
               "DRIFT (candidates never run only: picked among the bodies run, marked)" if drift else "PASS")
    print(f"candidate drift ({os.path.relpath(grid, EXP)}): {cells} (cell, workload) captures checked against HEAD's plan files; "
          f"{drift} (cell, workload, query) with drift, {refused} of them a body HEAD refuses on the cell -- {verdict}")
    return 1 if refused or not cells else 3 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
