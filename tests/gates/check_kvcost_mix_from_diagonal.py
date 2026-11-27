#!/usr/bin/env python3
"""kvcost's check 6: a mix priced from the three DIAGONAL cells only (the other six composed by the composer, as
kvcost compose runs it) equals the same mix priced from the nine measured cells (kvcost cost-layouts --mix, both
times), on every count, lane and model; so do the nine uniform layouts' rows. The control removes one diagonal cell:
the composer must refuse, naming it. The rerun control plants a mark in the composed capture (output of other code, its
sources unchanged): composing again moves that capture aside and puts today's marks back. Exit 1 on a difference, a
control not caught, or 0 rows compared.

The diagonal cells' files are copied from the grid into a scratch root under the scale's output dir (removed after the
run); the rows go to scratch too.

    python3 tests/gates/check_kvcost_mix_from_diagonal.py --dataset ldbc --sf 0.1 --mix "knows lists=Ch64k" --base Co-Sp [--grid <the nine cells' capture root>]
    python3 tests/gates/check_kvcost_mix_from_diagonal.py --dataset linkbench --sf tiny --params quick --mix "link1 lists=Ch64k" --base Co-Sp --workload l01
"""
import argparse
import csv
import glob
import os
import shutil
import subprocess
import sys

from kvevaluator import layouts as L
from kvcost.cli import views as V
from kvcost.cli import workloads as W
from kvevaluator.output_tree import Tree, capture_in, own_tree, tree_of

TOL = 1e-9
DIAGONAL = ("props_split_adj_split", "props_condensed_adj_condensed", "props_chunked_adj_chunked")
WORKLOAD = {"ldbc": "i01", "linkbench": "l01", "imdb": "j01"}   # each dataset's own mix workload


def run(cmd, out_tree=None):
    """(rc, output) of a command; out_tree: the output tree it writes into (own_tree: the stores and stats shared)"""
    env = own_tree(out_tree) if out_tree else None
    p = subprocess.run(cmd, cwd=V.EXP, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def rows_of(path, kind):
    """{(family, query, layout, chunk view): row} of one kind's rows (uniform, mix) in a query_costs.csv"""
    return {(r["family"], r["query"], r["layout"], r["chunk_view"]): r for r in csv.DictReader(open(path))
            if r["kind"] == kind}


COST_COLS = ("wops", "bytes", "wops_bytes4k")      # and every raw channel and bill lane: the columns after `body`


def differ(x, y):
    """how many rows differ (a missing row, the plan, or a cost column beyond TOL), and the first few"""
    bad, first = 0, []
    for k in sorted(set(x) | set(y)):
        p, q = x.get(k), y.get(k)
        cols = list(p)[list(p).index("body") + 1:] if p else []
        why = ("in one file only" if not (p and q) else f"plan {p['body']} vs {q['body']}" if p["body"] != q["body"] else
               next((f"{c}: {p[c]} vs {q[c]}" for c in cols if (p[c] == "") != (q[c] == "") or p[c] != "" and
                     abs(float(p[c]) - float(q[c])) > TOL * max(1.0, abs(float(p[c])), abs(float(q[c])))), None))
        if why:
            bad += 1
            if len(first) < 5:
                first.append(f"{' '.join(k)}: {why}")
    return bad, first


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--params", default=None, help="the param set (kvcost --params; default the dataset's)")
    ap.add_argument("--mix", required=True, help="an assignment string (or a spec json that groups no props)")
    ap.add_argument("--base", default=None)
    ap.add_argument("--workload", default=None, help="the workload priced (default: the benchmark's own, i01 for ldbc)")
    ap.add_argument("--grid", default=None, help="the measured <dataset>/<scale> dir (default: the output tree's; a "
                                                 "gate round passes its own)")
    a = ap.parse_args()
    ad = W.get(a.dataset)
    ad.prepare(a)
    grid = os.path.abspath(a.grid) if a.grid else str(Tree().place_dir("captures", ad.DATASET, a.sf))
    fam = next(f for f, k in sorted(dict(ad.FAMILIES).items()) if k == "read")
    # measured as output_tree.layout_capture counts it (counts and run record): else the rule would read a composed one
    have = [c for c in L.CELLS if os.path.exists(capture_in(grid, fam).path_of("counts", c))
            and os.path.exists(capture_in(grid, fam).record(c))]
    if len(have) != 9:
        print(f"FAIL: {grid} has {len(have)} measured cells, not nine: nothing to compare against")
        return 1
    scratch = str(Tree().summary(ad.DATASET, a.sf, "kvcost-diag-check"))
    if os.path.exists(scratch):
        shutil.rmtree(scratch)                    # our own scratch, rebuilt in seconds
    # a tree of the three diagonals measured and the six composed from them: diag/captures/... and diag/composed/...
    diag = Tree(os.path.join(scratch, "diag"))
    diag_root = str(diag.place_dir("captures", ad.DATASET, a.sf))
    copied = 0
    for dp, _, fs in os.walk(grid):               # every file of the three diagonal cells (counts, parts, marks, records)
        for f in fs:
            if any(f.startswith(c + ".") or f.startswith("meta-" + c + ".") for c in DIAGONAL):
                dst = os.path.join(diag_root, os.path.relpath(os.path.join(dp, f), grid))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(dp, f), dst)
                copied += 1
    composer = [sys.executable, "-m", "kvcost.layout_cost.compose_cells", "--dataset", ad.DATASET, "--whole-grid"]
    composed = str(diag.place_dir("composed", ad.DATASET, a.sf))
    rc0, o0 = run(composer + ["--grid", diag_root, "--out", composed, "--replace"])
    if rc0:
        print(f"FAIL: the composer rc {rc0}\n{o0[-800:]}")
        return 1
    cost = [sys.executable, "-m", "kvcost.cli.cli", "cost-layouts", "--dataset", f"{a.dataset}={a.sf}",
            *(["--params", a.params] if a.params else []), "--workload", a.workload or WORKLOAD[a.dataset],
            "--cost-model", "wops", *(L.name_of(c) for c in L.CELLS)]
    cost += [a.mix] if a.mix.endswith(".json") else ["--mix", a.mix, "--mix-name", "diag-check"]
    cost += ["--mix-base", a.base] if a.base else []
    # each read from its tree (--from), written into a scratch output dir of its own
    rc1, o1 = run(cost + ["--from", str(diag.root)], os.path.join(scratch, "from-diagonal"))
    rc2, o2 = run(cost + ["--from", str(tree_of(grid))], os.path.join(scratch, "from-nine"))
    if rc1 or rc2:
        print(f"FAIL: cost-layouts rc {rc1} (from the diagonal) / {rc2} (from the nine)\n{(o1 if rc1 else o2)[-800:]}")
        return 1

    def written(d, name):          # the one file cost-layouts wrote of that name, in its output dir
        found = glob.glob(os.path.join(Tree(os.path.join(scratch, d)).summary(ad.DATASET, a.sf, "layout-costs"), "**", name),
                          recursive=True)
        if len(found) != 1:
            raise SystemExit(f"FAIL: {len(found)} {name} under {os.path.join(scratch, d)}")
        return found[0]
    mx, my = (rows_of(written(d, "query_costs.csv"), "mix") for d in ("from-diagonal", "from-nine"))
    ux, uy = (rows_of(written(d, "query_costs.csv"), "uniform") for d in ("from-diagonal", "from-nine"))
    bad_m, first_m = differ(mx, my)
    bad_u, first_u = differ(ux, uy)
    # a rerun composes again: a mark planted in the composed capture (as older code's output) is replaced
    ex = capture_in(composed, fam).path_of("exact", "props_condensed_adj_chunked")
    good = open(ex).read()
    erows = list(csv.DictReader(open(ex)))
    q0 = erows[0]["query"]
    for r in erows:
        if r["query"] == q0:
            r["exact"] = "unknown"
    with open(ex, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(erows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(erows)
    rc4, o4 = run(composer + ["--grid", diag_root, "--out", composed, "--replace"])
    replaced = (rc4 == 0 and "moved aside" in o4 and open(ex).read() == good
                and any(d.startswith(os.path.basename(composed) + ".aside-") for d in os.listdir(os.path.dirname(composed))))
    # the control: one diagonal cell missing -> refused, naming it
    gone = DIAGONAL[1]
    ctl_root = os.path.join(scratch, "grid-control")
    shutil.copytree(diag_root, ctl_root)
    for dp, _, fs in os.walk(ctl_root):
        for f in fs:
            if gone in f:
                os.remove(os.path.join(dp, f))     # the scratch copy's files only
    rc3, o3 = run(composer + ["--grid", ctl_root, "--out", os.path.join(scratch, "control")])
    caught = rc3 != 0 and gone in o3
    shutil.rmtree(scratch)
    print(f"[check_kvcost_mix_from_diagonal] {a.dataset} sf{a.sf}: {copied} diagonal files copied, six cells composed")
    print(f"  {len(mx)} mix rows, from the diagonal vs from the nine measured cells: {bad_m} differ")
    print(f"  {len(ux)} uniform rows (nine layouts), composed vs measured: {bad_u} differ")
    for f in first_m + first_u:
        print("  DIFF", f)
    print(f"  control (no {L.name_of(gone)}): {'caught: the composer refused, naming it' if caught else 'NOT caught'} (rc {rc3})")
    print(f"  rerun: the composed capture holding 'unknown' planted on {fam} {q0} in Co-Ch64k's marks "
          f"{'moved aside, today' + chr(39) + 's marks back' if replaced else 'NOT replaced'}")
    ok = len(mx) > 0 and len(ux) > 0 and bad_m == 0 and bad_u == 0 and caught and replaced
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
