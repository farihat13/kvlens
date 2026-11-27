#!/usr/bin/env python3
"""The example workload (README "Add your own workload") runs as written: `kvcost predict` prices its two queries on
the nine grid layouts from the scale's statistics (no store opened), and `kvcost cost-layouts --count-source <the default
set, predicted-tier1>` ranks the layouts. Passes when both exit 0, every query has a run row with ok = 1 on every layout (the predicted
level; the anchor level is off unless asked, kvcost predict --anchor), and the ranking holds the nine layouts. The
predicted capture is shared (other runs' layouts sit beside the nine), so the nine grid cells' files are read by name. Needs the scale's record stream (the LDBC data); seconds once the statistics exist.

    python3 tests/gates/check_example_workload.py [--sf 0.1] [--workload inputs/workloads/ldbc/examples/example.json]
"""
import argparse
import csv
import json
import os
import subprocess
import sys

from kvcost.cli.predict import default_set
from kvevaluator.layouts import CELLS
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT

LAYOUTS = len(CELLS)   # the grid's nine uniform layouts, what kvcost predict costs by default


def run(cmd):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("".join((r.stdout + r.stderr).splitlines(keepends=True)[-8:]), end="")
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--workload", default="inputs/workloads/ldbc/examples/example.json")
    a = ap.parse_args()
    kvcost = os.path.join(os.path.dirname(sys.executable), "kvcost")
    wl = json.load(open(os.path.join(ROOT, a.workload)))
    queries = sorted({i["query"] for i in wl["items"]})
    fails = []
    if run([kvcost, "predict", "--workload", a.workload, "--dataset", f"ldbc={a.sf}"]) != 0:
        fails.append("kvcost predict failed")
    rows = 0
    for level in (default_set(),):
        caps = [Tree().predicted(fam, a.sf, level) for fam in sorted({i["family"] for i in wl["items"]})]
        files = sorted(f for cap in caps for c in CELLS if os.path.exists(f := cap.path_of("counts", c)))
        if len(files) != LAYOUTS:
            fails.append(f"{level}: {len(files)} layouts, want {LAYOUTS}")
        for f in files:
            got = {r["query"]: r["ok"] for r in csv.DictReader(open(f))}
            rows += len(got)
            for q in queries:
                if got.get(q) != "1":
                    fails.append(f"{level} {os.path.basename(f)[:-4]}: {q} {'missing' if q not in got else 'not ok'}")
    if run([kvcost, "cost-layouts", "--workload", a.workload, "--dataset", f"ldbc={a.sf}", "--cost-model", "wops_bytes4k",
            "--count-source", default_set()]) != 0:
        fails.append("kvcost cost-layouts failed")
    from kvcost.cli import runs
    from kvcost.layout_cost import layout_costs as LC
    wf = runs.load(argparse.Namespace(workload=a.workload, sf=a.sf))     # its costs: summary/.../layout-costs/<id>/predicted/
    place = LC.place_of_run(Tree(), wf, default_set())
    layouts = {r["layout"] for r in LC.read_layouts(place)} if (place / "layouts.csv").exists() else set()
    if len(layouts) != LAYOUTS:
        fails.append(f"cost-layouts ranked {len(layouts)} layouts, want {LAYOUTS}")
    for f in fails[:10]:
        print("  FAIL", f)
    print(f"example workload {'FAIL' if fails else 'PASS'}: {len(queries)} queries, {rows} run rows on "
          f"{LAYOUTS} layouts x 1 level, {len(layouts)} layouts ranked ({a.workload}, sf{a.sf})")
    return 1 if fails or not rows else 0


if __name__ == "__main__":
    sys.exit(main())
