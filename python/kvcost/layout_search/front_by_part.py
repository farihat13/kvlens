"""A workload's front by part: the lower-left front (wops and bytes) of every mix a set of search rows names, over
Split, Condensed and Chunked at 1-256 KB per type, each mix costed part by part from the captures, and the tables its
figure draws. One code path for every workload: paper/derive/front_by_part_49_layouts_i01.py and
experiments/mix_benefit/front_by_part.py call it.

The candidates: a staircase's limits (search-mix --front --no-walk), a 3-setting front's mixes (optional), and the 49
uniform layouts. Each is costed with front.cost_by_part; a total that differs from its source's by more than TOL
(relative), or parts that do not sum to the total, are refused. The front is front.lower_left_front over all of them,
by bytes; of equal points, the first in that order.

The tables (columns):
  <name>.csv          the front's mixes, one row per mix and part: mix (its place on the front), source (solved /
                      front3 / uniform), label (a solved limit's multiple of Sp-Co's bytes, a uniform layout's cell),
                      wops, bytes, kind (props / adj / index), part, setting, part_wops, part_bytes
  <name>-points.csv   the other points the figure draws: the 49 (uniform49), the nine at 64 KB (uniform9), the
                      3-setting front's mixes (front3); wops, bytes
  <name>-limits.csv   each staircase limit: label, cap_bytes, wops and bytes of its answer (empty where no mix fits),
                      proven, bound (the lowest wops any mix within the limit can have), gap (1 - bound / wops: 0 where
                      proven; empty where no bound is known)
"""
import csv
import os

from kvcost.layout_cost import benchmarks, compose
from kvcost.layout_cost.settings import parse_sizes
from kvevaluator.layouts import DIAGONALS, OTHER_CHUNK_SIZES, SETTINGS
from kvcost.layout_search.front import cost_by_part
from kvevaluator.layouts import cell_id, cell_settings

COLUMNS = ["mix", "source", "label", "wops", "bytes", "kind", "part", "setting", "part_wops", "part_bytes"]
POINT_COLUMNS = ["kind", "name", "wops", "bytes"]
LIMIT_COLUMNS = ["label", "cap_bytes", "wops", "bytes", "proven", "bound", "gap"]
SIZES = OTHER_CHUNK_SIZES
GRID_SETTINGS = SETTINGS                 # Split, Condensed, Chunked at the grid's 64 KB
TOL = 1e-6                               # a costed total against its source's, relative (the paper's 1,765 within 5e-16)


def load(dataset, w, place):
    """(the workload's data costed from the layouts at `place`, a captures/<dataset>/<scale> dir, with the four other
    chunk sizes' ChK-ChK in the same place, chunk view half, as the staircase; its parts)"""
    composer = compose.Composer(criterion=None, sizes=parse_sizes(list(SIZES), place),
                                plan_rule=compose.PlanRule(in_sample=None), families=benchmarks.families_read(dataset, [w]),
                                chunk_view=compose.resolve_chunk_view("half"), param_set=None, bill="")
    data = compose.load(str(place), False, dataset, composer=composer)
    return data, compose.workload_units(w, data)


def assignment_of(a):
    """an assignment as search-mix writes it ({"knows lists": "Ch4k"}) -> {unit: setting id}"""
    from kvcost.layout_search import results as R
    return R.assignment_of(a)


def label_of(entry):
    """a staircase limit's label: its multiple of Sp-Co's bytes"""
    return f"{entry['cap_x_reference']:.4f}"


def staircase_candidates(stair):
    """[(source, label, assignment, wops in the row, bytes in the row)] of a staircase's limits that hold a mix"""
    return [("solved", label_of(s), assignment_of(s["assignment"]), float(s["wops"]), float(s["bytes"]))
            for s in stair if s["assignment"] is not None]


def front3_candidates(points):
    """the same of a search-mix --front row's points (its complete 3-setting front)"""
    return [("front3", "", assignment_of(p["assignment"]), float(p["wops"]), float(p["bytes"])) for p in points]


def uniform_candidates(units, totals=None):
    """the 49 uniform layouts, adjacency outer, properties inner; totals: {cell: (wops, bytes)} from a source file to
    check against, or None (costed here only)"""
    out = []
    for a in DIAGONALS:
        for p in DIAGONALS:
            cell = cell_id(p, a)
            want = totals[cell] if totals is not None else (None, None)
            out.append(("uniform", cell, {u: (p if u[0] == "props" else a) for u in units}, *want))
    return out


def cost_all(w, data, cands):
    """[(bytes, wops, candidate, {part: totals})], each candidate costed by part; refuses a total that differs from its
    source's, or parts that do not sum to it. Also returns the largest relative difference seen"""
    costed, worst = [], 0.0
    for c in cands:
        tot, per = cost_by_part(data, w, c[2])
        for cur, want in (("ops", c[3]), ("bytes", c[4])):
            if want is None:
                continue
            err = abs(tot[cur] - want) / max(1.0, want)
            worst = max(worst, err)
            if err > TOL:
                raise SystemExit(f"{c[0]} {c[1]}: {cur} costed {tot[cur]!r}, its source says {want!r}")
        for cur in ("ops", "bytes"):
            if abs(sum(v[cur] for v in per.values()) - tot[cur]) > TOL * tot[cur]:
                raise SystemExit(f"{c[0]} {c[1]}: the parts do not sum to the total {cur}")
        costed.append((tot["bytes"], tot["ops"], c, per))
    return costed, worst


def front_rows(front):
    """one row per front mix and part, in (mix, kind, part) order"""
    rows = []
    for i, (b, o, c, per) in enumerate(front):
        for u, v in per.items():
            rows.append({"mix": i, "source": c[0], "label": c[1], "wops": o, "bytes": b,
                         "kind": "index" if u is None else u[0], "part": "index" if u is None else "|".join(u[1:]),
                         "setting": "" if u is None else c[2][u], "part_wops": v["ops"], "part_bytes": v["bytes"]})
    return sorted(rows, key=lambda d: (d["mix"], d["kind"], d["part"]))


def point_rows(costed):
    """the 49 (and the nine of them at 64 KB) and the front3 mixes, in (kind, bytes, wops, name) order"""
    pts = []
    for b, o, c, _ in costed:
        if c[0] == "uniform":
            pts.append({"kind": "uniform49", "name": c[1], "wops": o, "bytes": b})
            p, a = cell_settings(c[1])
            if p in GRID_SETTINGS and a in GRID_SETTINGS:
                pts.append({"kind": "uniform9", "name": c[1], "wops": o, "bytes": b})
        elif c[0] == "front3":
            pts.append({"kind": "front3", "name": "", "wops": o, "bytes": b})
    return sorted(pts, key=lambda d: (d["kind"], d["bytes"], d["wops"], d["name"]))


def limit_rows(stair):
    """one row per staircase limit, in the staircase's order (by cap)"""
    rows = []
    for s in stair:
        wops, bound = s.get("wops"), s.get("bound")
        if s["proven"] and wops is not None:
            bound = wops if bound is None else bound
        gap = "" if wops is None or bound is None else (0.0 if s["proven"] else 1 - float(bound) / float(wops))
        rows.append({"label": label_of(s), "cap_bytes": s["cap_bytes"], "wops": "" if wops is None else wops,
                     "bytes": "" if s.get("bytes") is None else s["bytes"], "proven": int(bool(s["proven"])),
                     "bound": "" if bound is None else bound, "gap": gap})
    return rows


def write(path, columns, rows):
    """one table as CSV: floats as repr (the paper's write_table)"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    text = lambda v: repr(v) if isinstance(v, float) else str(v)
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(columns)
        for r in rows:
            wr.writerow([text(r[c]) for c in columns])
