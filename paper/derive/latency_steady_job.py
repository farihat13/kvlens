#!/usr/bin/env python3
"""Derive latency-steady-job's table: JOB's steady latency per store (the 113 queries on IMDb full), each query with an
equal share of Sp-Co's combined cost, over Sp-Co's, as experiments/job/steady_latency_table.py computes it (its `table`,
imported: one rule; its docstring has the columns and the weights). Reads the unit STEADY_LATENCY_JOB names, below one
output tree (--from; the paper's runs by default; JOB is at IMDb full whatever --scale says): a tree without it is
skipped and named. One row per store the run timed; --layouts names the layouts it must hold (the figure's chain names
its 12), else refused. Writes build/data/latency-steady-job.csv (or --tables).

    uv run python paper/derive/latency_steady_job.py [--from output --tables DIR] [--layouts Sp-Sp Co-Co ...]
"""
import argparse
import importlib.util

from kvevaluator.paths import ROOT
from snapshots import STEADY_LATENCY_JOB, add_tree_args, scale_in, tree_of, write_table

_spec = importlib.util.spec_from_file_location("steady_latency_table", ROOT / "experiments/job/steady_latency_table.py")
J = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(J)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    add_tree_args(ap)
    ap.add_argument("--layouts", nargs="*", default=[], metavar="LAYOUT",
                    help="layouts the run must hold timings of, else refused (default: Sp-Co alone)")
    a = ap.parse_args()
    tree, sf = tree_of(a.tree), scale_in(a, "imdb")
    if sf is None:
        print("JOB: IMDb is not in this run (--scale): no table")
        return
    timings = tree.timings("job", sf)
    if not timings.is_dir():
        print(f"JOB: not in this tree ({timings})")
        return
    print(f"[snapshot] {STEADY_LATENCY_JOB} in {tree.root}")
    rows, info = J.table(STEADY_LATENCY_JOB, tree.root, sf, layouts=a.layouts)
    print(f"{info['stores']} stores + {info['controls']} control runs, {info['weights']} queries each; the cost from each plan "
          f"row's own counts, {info['compared']} of {info['plan_rows']} plan rows compared with the counts capture"
          f"{', all equal' if info['compared'] else ''}; Sp-Co's equal-share total {info['sp_co_share']:.6f}")
    write_table("latency-steady-job", J.COLUMNS, rows, key=None, out_dir=a.tables)


if __name__ == "__main__":
    main()
