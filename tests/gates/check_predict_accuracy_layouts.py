#!/usr/bin/env python3
"""The layout report's own entry point, `python -m kvcost.predict_accuracy.layouts` (kvcost predict-accuracy's --layouts
mode before it moved there), on the scale's predicted 49 and measured captures (no store, no plan run):

  1. --grid with a check workload: the check workload's composed totals equal the reference place's (a kvcost
     cost-layouts place), then the report: every file written, one pick row per query, the 49 layouts;
  2. --measured-table: the measured side read from run 1's tables gives the picks and totals --grid gives (the same
     layouts; every number, the rank correlations too, within 1e-9: the tables hold full precision);
  3. controls: a reference with one layout's total moved is refused, and so is --expect-layouts the capture does not hold.

    python3 tests/gates/check_predict_accuracy_layouts.py [--sf 0.1] [--cost-workload i02] [--check-workload i01]

Its inputs, in the output dir: the predicted 49 (kvcost predict --dataset ldbc=<SF> --workload i02 <the 49>), the
grid captures (kvcost measure ... on the seven, then kvcost compose) and the check workload's cost-layouts place
(kvcost cost-layouts --dataset ldbc=<SF> --workload i01 <the 49>). Seconds at sf0.1.
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile

from kvcost.cli.predict import default_set
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT

FILES = ("by_query.csv", "by_param.csv", "picks.csv", "picks_by_param.csv", "constant_layouts.csv", "totals.csv",
         "tie_rule.csv", "summary.md")
results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def same_table(x, y, rel=1e-9):
    """two CSVs with the same rows: text cells equal, numbers within `rel` (a table read back holds full precision, so the
    rank correlations compare too)"""
    def num(v):
        try:
            return float(v)
        except ValueError:
            return None
    rx, ry = list(csv.reader(open(x))), list(csv.reader(open(y)))
    if len(rx) != len(ry):
        return False
    if rx[0] != ry[0]:
        return False
    keep = range(len(rx[0]))
    for a, b in zip(rx, ry):
        if len(a) != len(b):
            return False
        for u, v in ((a[i], b[i]) for i in keep):
            nu, nv = num(u), num(v)
            if nu is None or nv is None or nu != nu or nv != nv:      # text, or nan
                if u != v:
                    return False
            elif abs(nu - nv) > rel * max(1.0, abs(nu)):
                return False
    return True


def layouts(*args):
    r = subprocess.run([sys.executable, "-m", "kvcost.predict_accuracy.layouts", *args], cwd=ROOT, capture_output=True,
                       text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--cost-workload", default="i02")
    ap.add_argument("--check-workload", default="i01")
    a = ap.parse_args()
    tree = Tree()
    predicted = tree.predicted("interactive", a.sf, default_set()).path
    grid = tree.place_dir("captures", "ldbc", a.sf)
    reference = tree.summary("ldbc", a.sf, "layout-costs", a.check_workload, "measured")
    need = [p for p in (predicted, grid, reference / "layouts.csv") if not os.path.exists(p)]
    if need:
        sys.exit(f"check_predict_accuracy_layouts: no {need[0]} (the docstring names the command that makes it)")
    tmp = tempfile.mkdtemp(prefix="pa-layouts-")
    common = ["--dataset", f"ldbc={a.sf}", "--predicted", str(predicted), "--cost-workload", a.cost_workload]
    try:
        one = os.path.join(tmp, "one")
        rc, out = layouts(*common, "--grid", str(grid), "--check-workload", a.check_workload, "--check-reference",
                          str(reference), "--expect-layouts", "49", "--out", one)
        got = os.path.join(one, "layout_choice")
        picks = list(csv.DictReader(open(os.path.join(got, "picks.csv")))) if rc == 0 else []
        cells = {r["cell"] for r in csv.DictReader(open(os.path.join(got, "by_query.csv")))} if rc == 0 else set()
        case(f"--grid, checked against {a.check_workload}'s cost-layouts place (\"49 layouts, 0 differ\"): rc 0, the "
             f"{len(FILES)} files, pick rows, the 49 layouts per query",
             rc == 0 and "49 layouts, 0 differ" in out and all(os.path.isfile(os.path.join(got, f)) for f in FILES)
             and len(picks) > 0 and len(cells) == 49, (rc, out[-600:], len(picks), len(cells)))
        plain = os.path.join(tmp, "plain")          # --grid with no check workload: what --measured-table must give
        rc1b, out1b = layouts(*common, "--grid", str(grid), "--out", plain)
        two = os.path.join(tmp, "two")
        rc2, out2 = layouts(*common, "--measured-table", got, "--out", two)
        same = [f for f in ("picks.csv", "totals.csv", "by_query.csv") if rc1b == 0 and rc2 == 0 and same_table(
            os.path.join(plain, "layout_choice", f), os.path.join(two, "layout_choice", f))]
        case("--measured-table (run 1's tables): the picks, totals and per-query rows of --grid with no check workload "
             "(the same layouts; every number within 1e-9, the rank correlations too)", rc2 == 0 and len(same) == 3,
             (rc1b, rc2, out2[-600:], same))
        planted = os.path.join(tmp, "reference")
        shutil.copytree(reference, planted)
        rows = list(csv.DictReader(open(os.path.join(planted, "layouts.csv"))))
        victim = next(r for r in rows if r["kind"] == "uniform")
        for r in rows:            # the layout's row in every chunk view: the check reads the composition's view
            if r["layout"] == victim["layout"]:
                r["total_ops"] = str(float(r["total_ops"]) * 1.01 + 1)
        with open(os.path.join(planted, "layouts.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        rc3, out3 = layouts(*common, "--grid", str(grid), "--check-workload", a.check_workload, "--check-reference",
                            planted, "--out", os.path.join(tmp, "three"))
        case(f"control: a reference with {victim['layout']}'s total_ops moved is refused", rc3 != 0, (rc3, out3[-400:]))
        rc4, out4 = layouts(*common, "--grid", str(grid), "--expect-layouts", "48", "--out", os.path.join(tmp, "four"))
        case("control: --expect-layouts 48 on the 49 is refused", rc4 != 0, (rc4, out4[-400:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n_ok = sum(results)
    print(f"check_predict_accuracy_layouts {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases "
          f"(sf{a.sf}, {a.cost_workload} on the 49, checked by {a.check_workload})")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
