"""Costs are the freeze's: Sp-Co composed from the fixed 2-cell capture (tests/fixtures/golden/kvcost_capture.tar.gz)
by the composer (kvcost.layout_cost.compose_cells, as kvcost compose runs it), then priced by the views library
(kvcost.cli.views.cell_view: the per-query pick and cost, the totals), writes costs.csv and totals.csv with the same
bytes, the same I01 total and the same LDBC picks as tests/fixtures/golden/costs.json. No store, no binary: seconds.

    python tests/gates/check_golden_costs.py
Work dir: <output>/golden-costs/ (the capture unpacked, the composed view).
"""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tarfile

from kvevaluator.paths import OUTPUT_DIR, ROOT

GOLDEN = ROOT / "tests" / "fixtures" / "golden"
CELL = "props_split_adj_condensed"                  # Sp-Co, composed from the fixture's Sp-Sp and Co-Co


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    want = json.load(open(GOLDEN / "costs.json"))
    tar = GOLDEN / want["input"]["file"]
    if sha256(tar) != want["input"]["sha256"]:
        sys.exit(f"check_golden_costs FAIL: {tar.name} is not the fixture costs.json names")
    work = OUTPUT_DIR / "golden-costs"
    # a tree of its own: the fixture in its captures/ place, compose's output in the composed/ place beside it
    cap, out = work / "tree" / "captures" / "ldbc" / "sf0.1", work / "out"
    n = 0
    with tarfile.open(tar) as t:
        for m in t.getmembers():
            if m.isfile():
                t.extract(m, cap, filter="data")
                n += 1
    if n != want["input"]["files"]:
        sys.exit(f"check_golden_costs FAIL: {n} files in the capture, costs.json says {want['input']['files']}")
    view = out / want["view"]
    for name in ("costs.csv", "totals.csv"):          # a file from an earlier run must not pass as this run's
        if (view / name).exists():
            os.replace(view / name, str(view / name) + ".previous")
    composed = work / "tree" / "composed" / "ldbc" / "sf0.1"
    cmd = [sys.executable, "-m", "kvcost.layout_cost.compose_cells", "--grid", str(cap), "--dataset", "ldbc",
           "--cells", CELL, "--out", str(composed), "--replace"]
    log = work / "compose.log"
    with open(log, "w") as fh:
        rc = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
    if rc:
        sys.exit(f"check_golden_costs FAIL: the composer rc {rc} (log: {log})")
    from kvcost.cli import views as V
    from kvcost.cli import workloads as W
    from kvcost.layout_cost.picks import PlanRule
    from kvevaluator import layouts as L
    V.cell_view(W.get("ldbc"), str(cap), CELL, "0.1", str(view), rule=PlanRule(), spec_path=L.resolve("Sp-Co").spec)
    results = []
    for name, key in (("costs.csv", "costs_csv_sha256"), ("totals.csv", "totals_csv_sha256")):
        results.append((f"{name} bytes", sha256(view / name) == want[key]))
    totals = list(csv.DictReader(open(view / "totals.csv")))
    i01 = [t for t in totals if t["scope"].startswith("i01 ")]
    results.append(("one I01 total, equal", len(i01) == 1 and i01[0] == want["total"]))
    costs = list(csv.DictReader(open(view / "costs.csv")))
    picks = {c["query"]: c["body"] for c in costs if c["family"] == "interactive"}
    results.append((f"{len(want['picks'])} LDBC picks, equal", picks == want["picks"]))
    for what, ok in results:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    bad = sum(not ok for _, ok in results)
    print(f"check_golden_costs {'FAIL' if bad else 'OK'}: {len(results) - bad}/{len(results)} checks; a {n}-file capture, "
          f"{len(costs)} cost rows, {len(totals)} totals, I01 wops_bytes4k {i01[0].get('wops_bytes4k') if i01 else '?'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
