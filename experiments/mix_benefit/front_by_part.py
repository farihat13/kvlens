#!/usr/bin/env python3
"""Figure 10(a)'s tables for LDBC BI (b01) or JOB (j01): the front (wops against bytes, both summed at the
equal-share weights, inputs/workloads/<dataset>/equal-share/<workload>.json) of every mix the search rows name, each costed part
by part, with Split, Condensed and Chunked at 1-256 KB per type. The same code as I01's
(paper/derive/front_by_part_49_layouts_i01.py): python/kvcost/layout_search/front_by_part.py. Analysis only: opens no
store.

The candidates, read from the mix search's rows in the tree --from names (kvcost search-mix --question bi-any-size,
job-any-size; summary/<its dataset>/<scale>/mix-search/<workload>/):
- the staircase (mix-search/<workload>/mixes-of-Sp-Co-Ch1k-Ch4k-Ch16k-Ch64k-Ch256k.json, question staircase): BI's proven at every limit; JOB's
  without the cost cache, each limit the best mix found within 30,000 nodes (NOT proven: its bound and gap in the limits table);
- the 3-setting complete front (mix-search/<workload>/mixes-of-Sp-Co-Ch64k.json, question front), when the row exists (BI);
- the 49 uniform layouts, costed here.

Writes to the output dir's summary/<its dataset>/<scale>/mix-benefit/front-by-part/<workload>/: front-by-part-<workload>.csv,
-points.csv, -limits.csv (the columns: front_by_part.py's docstring).

    python experiments/mix_benefit/front_by_part.py --workload b01|j01 --dataset ldbc=3|imdb=full [--from TREE]
"""
import argparse
import os
import sys
import time

from kvevaluator import sizes
from kvevaluator.output_tree import Tree

from kvcost.layout_cost import benchmarks, query_weighting as QW
from kvcost.layout_search import front_by_part as FB
from kvcost.layout_search.front import lower_left_front
from kvcost.layout_search import results as R
from kvcost.layout_search.results import ResultFile  # (one reader: refuses a file mixing frequencies)

def row_of(path, question):
    """the one row of a search-mix result file whose question is `question`, or None when the file is missing"""
    if not os.path.exists(path):
        return None
    rows = [r for r in ResultFile(path).read() if r.get("question") == question]
    if len(rows) != 1:
        raise SystemExit(f"{path}: {len(rows)} rows with question {question}, not one")
    return rows[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", required=True, help="the workload id (b01, j01): its equal-share file")
    ap.add_argument("--dataset", required=True, help="the run's datasets at their scales (ldbc=3,imdb=full): the "
                                                     "workload's own holds its layouts and its rows")
    ap.add_argument("--from", dest="tree", default=None, help="the output tree read (default the output dir)")
    a = ap.parse_args()
    t0 = time.time()
    wl = a.workload
    run = sizes.resolve(a.dataset)
    ds = benchmarks.dataset_in_run(wl, run)
    sc = sizes.scale(run, ds) if ds else None
    if not sc:
        raise SystemExit(f"--dataset {a.dataset}: give {wl}'s dataset's scale")
    src = Tree(a.tree)
    _, w = benchmarks.read_workload(str(QW.equal_share_path(wl)))
    measured = str(src.place_dir("captures", ds, sc))
    data, units = FB.load(ds, w, measured)        # each layout by output_tree.layout_capture
    here = src.summary(ds, sc, "mix-search", w["id"])
    a.out = str(Tree().summary(ds, sc, "mix-benefit", "front-by-part", wl))
    stair_row = row_of(os.path.join(here, R.stem("per-type-any-size") + ".json"), "staircase")
    if stair_row is None:
        raise SystemExit(f"{here}/{R.stem('per-type-any-size')}.json: no staircase row (kvcost search-mix --question "
                         f"{wl}-any-size)")
    stair = stair_row["staircase"]
    cands = FB.staircase_candidates(stair)
    front3 = row_of(os.path.join(here, R.stem("per-type") + ".json"), "front")
    if front3 is not None:
        cands += FB.front3_candidates(front3["points"])
    cands += FB.uniform_candidates(units)
    costed, worst = FB.cost_all(w, data, cands)
    front = lower_left_front(costed)
    rows, pts, lims = FB.front_rows(front), FB.point_rows(costed), FB.limit_rows(stair)
    name = f"front-by-part-{wl}"
    for suffix, cols, rs in (("", FB.COLUMNS, rows), ("-points", FB.POINT_COLUMNS, pts), ("-limits", FB.LIMIT_COLUMNS, lims)):
        FB.write(os.path.join(a.out, f"{name}{suffix}.csv"), cols, rs)
    n = {k: sum(c[0] == k for c in cands) for k in ("solved", "front3", "uniform")}
    on = {k: sum(c[0] == k for _, _, c, _ in front) for k in n}
    gaps = [r["gap"] for r in lims if r["gap"] != ""]
    unproven = sum(not s["proven"] for s in stair)
    if n["uniform"] != 49 or not n["solved"]:
        raise SystemExit(f"candidates {n}: want 49 uniform layouts and a staircase")
    print(f"{name}: {len(stair)} limits ({len(stair) - unproven} proven, {n['solved']} with a mix; the largest gap "
          f"{max(gaps or [0]):.1%}), {n['front3']} front3 mixes{'' if front3 else ' (no 3-setting front row)'}, 49 "
          f"uniform; the largest total difference {worst:.1e}; the front: {len(front)} mixes {on}; {len(units)} parts; "
          f"{time.time() - t0:.0f} s -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
