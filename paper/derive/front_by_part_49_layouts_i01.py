#!/usr/bin/env python3
"""Derive front-by-part-49-layouts-i01's tables: I01 (the paper: at SF3), the lower-left front (wops and bytes) of every
mix the paper names over Split, Condensed and Chunked at 1, 4, 16, 64 or 256 KB per type, each costed part by part, from
one output tree (--from; the paper's runs by default) at --scale.

The candidates: the proven limits of the mix search's any-size staircase (kvcost search-mix --question i01-any-size;
the paper's 128), the mixes of the 3-setting complete front (search-mix --question i01-front's walk; the paper's 1,588),
and the 49 uniform layouts (kvcost cost-layouts: layout-costs/i01/measured/layouts.csv). Each is costed by part from the captures
(the nine, and each chunk size's ChK-ChK; chunk view half, as the staircase) and the front taken by
kvcost.layout_search.front_by_part, the code every workload's front uses. A candidate whose total differs from its file's by more than TOL, or whose parts do not sum to its
total, is refused. Of equal points, the first in the order above. Opens no store.

  build/data/front-by-part-49-layouts-i01.csv           the front's mixes, one row per mix and part
  build/data/front-by-part-49-layouts-i01-points.csv    the other points panel (a) draws: uniform49, uniform9, front3
  build/data/front-by-part-49-layouts-i01-limits.csv    each staircase limit: its answer, proven, bound, gap

    uv run python paper/derive/front_by_part_49_layouts_i01.py [--from output --dataset ldbc=0.1] [--compare <gs-paper's data dir>]
"""
import argparse
import csv
import time


from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_search import front_by_part as FB
from kvcost.layout_search.front import lower_left_front
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from snapshots import add_tree_args, mix_search_row, scale_in, tree_of, write_table

NAME = "front-by-part-49-layouts-i01"
KINDS = ("solved", "front3", "uniform")     # the candidates' sources, in order


def load(tree, scale):
    """(I01, its data costed from the tree's nine with each chunk size's ChK-ChK in the run's captures, its parts)"""
    (w,) = benchmarks.select("ldbc", scale, "i01").values()
    measured = tree.place_dir("captures", "ldbc", scale)
    return (w, *FB.load("ldbc", w, measured))


def candidates(tree, scale, units):
    """([(source, label, assignment, wops in its file, bytes in its file)]: the staircase's limits, the 3-setting front,
    the 49; each source's count checked against its file, the staircase, the counts {source: n})"""
    stair = mix_search_row(tree, scale, "i01", "per-type-any-size", question="staircase")["staircase"]
    if not all(s["proven"] for s in stair):
        raise SystemExit(f"the staircase: {sum(not s['proven'] for s in stair)} of {len(stair)} limits not proven")
    out = FB.staircase_candidates(stair)
    front3 = mix_search_row(tree, scale, "i01", "per-type", question="front", model=FRONT_WOPS_BYTES)["points"]
    out += FB.front3_candidates(front3)
    uni = {r["layout"]: r for r in LC.read_layouts(LC.place(tree, "ldbc", scale, "i01"), LC.VIEWS[0], kind="uniform")}
    out += FB.uniform_candidates(units, {c: (float(r["total_ops"]), float(r["total_bytes"])) for c, r in uni.items()})
    want = {"solved": sum(s["assignment"] is not None for s in stair), "front3": len(front3), "uniform": 49}
    got = {k: sum(c[0] == k for c in out) for k in KINDS}
    if got != want or len(uni) != 49:
        raise SystemExit(f"candidates {got}, want {want} (the staircase's limits with a mix, the 3-setting front's "
                         f"mixes, the 49; {uni_path.name}: {len(uni)} rows)")
    return out, stair, got


def point_rows(costed, counts):
    """the 49 (and the nine of them at 64 KB) and the front3 mixes (FB.point_rows), their counts checked"""
    pts = FB.point_rows(costed)
    got = [sum(d["kind"] == k for d in pts) for k in ("front3", "uniform49", "uniform9")]
    if got != [counts["front3"], 49, 9]:
        raise SystemExit(f"points: {got}, not {counts['front3']:,} + 49 + 9")
    return pts


def compare(theirs_path, rows, columns, key):
    """every row of gs-paper's CSV against ours by `key`, field by field (as write_table writes a value); prints the
    differences (a report, not a gate)"""
    text = lambda v: repr(v) if isinstance(v, float) else str(v)  # (write_table's)
    with open(theirs_path, newline="") as fh:
        theirs = [{c: r[c] for c in columns} for r in csv.DictReader(fh)]
    ours = [{c: text(r[c]) for c in columns} for r in rows]
    by = lambda rs: {tuple(r[k] for k in key): r for r in rs}
    t, o = by(theirs), by(ours)
    differ = [(k, c, t[k][c], o[k][c]) for k in sorted(set(t) & set(o)) for c in columns if t[k][c] != o[k][c]]
    for k, c, a, b in differ[:20]:
        print(f"  differs: {'/'.join(k)} {c}: {a} there, {b} here")
    print(f"compare {theirs_path.split('/')[-1]}: {len(set(t) & set(o))} rows in both ({len(theirs)} there, {len(ours)} "
          f"here; {len(set(t) ^ set(o))} keys in one only), {len({k for k, *_ in differ})} differ in some field; the "
          f"row order {'is' if [tuple(r[k] for k in key) for r in theirs] == [tuple(r[k] for k in key) for r in ours] else 'is NOT'} the same")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    ap.add_argument("--compare", help="gs-paper's evidence/front-by-part-49-layouts-i01/data/: print each field that differs")
    a = ap.parse_args()
    tree, scale = tree_of(a.tree), scale_in(a, "ldbc")
    t0 = time.time()
    w, data, units = load(tree, scale)
    t_load = time.time() - t0
    cands, stair, counts = candidates(tree, scale, units)
    costed, worst = FB.cost_all(w, data, cands)
    front = lower_left_front(costed)
    rows, pts = FB.front_rows(front), point_rows(costed, counts)
    write_table(NAME, FB.COLUMNS, rows, key=None, out_dir=a.tables)
    write_table(f"{NAME}-points", FB.POINT_COLUMNS, pts, key=None, out_dir=a.tables)
    write_table(f"{NAME}-limits", FB.LIMIT_COLUMNS, FB.limit_rows(stair), key=None, out_dir=a.tables)
    on = {k: sum(c[0] == k for _, _, c, _ in front) for k in KINDS}
    print(f"{NAME}: {len(cands)} candidates costed by part ({', '.join(f'{v} {k}' for k, v in counts.items())}), the largest "
          f"total difference from its file {worst:.1e}; the front: {len(front)} mixes ({on}), {len(rows)} rows; "
          f"{len(pts)} points (load {t_load:.0f} s, all {time.time() - t0:.0f} s)")
    if a.compare:
        compare(f"{a.compare.rstrip('/')}/{NAME}.csv", rows, FB.COLUMNS, ("mix", "kind", "part"))
        compare(f"{a.compare.rstrip('/')}/{NAME}-points.csv", pts, FB.POINT_COLUMNS, ("kind", "name", "wops", "bytes"))


if __name__ == "__main__":
    main()
