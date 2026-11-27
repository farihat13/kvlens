#!/usr/bin/env python3
"""Derive uniform-i01-chunk-sizes-and-front's tables: a query mix (the paper: I01 at SF3) on the uniform layouts with
Chunked at 1-256 KB, and the points of the fronts the figure draws, from one output tree (--from; the paper's runs by
default).

  build/data/uniform-i01-chunk-sizes-and-front.csv          every uniform row of the tree's layout-costs/<mix>/measured/
                                                            layouts.csv, half view (line: its place among them)
  build/data/uniform-i01-chunk-sizes-and-front-front.csv    the front points: the 3-option complete front, the nine,
                                                            and the "any size" line (the mix search's staircase row,
                                                            128 limits, each solved per type over Sp, Co and Ch1k-256k)
  build/data/uniform-i01-chunk-sizes-and-front-by-part.csv  each of the 49 by part kind (adj, props, index): its
                                                            frequency-weighted wops and bytes, from cost-layouts'
                                                            query_parts.csv beside the rows (each query's cost per run
                                                            on each part kind) times the query's frequency

The line's best uniform layout within each limit is chosen as the chunk-size front chose it: among the layouts whose
Chunked parts share one size, the fewest wops, then bytes. Checks: wops = G + 1.1 S + 0.01 N + P + D on every row; Sp-Co
is the same in the 49 and in the nine, and the limits are multiples of its bytes; each layout's parts sum to its total
(relative 1e-6). Nothing is costed here.

The fronts' table needs the I01 front and mix search runs (run-mix-benefit-w16-front.sh, kvcost search-mix --question); a tree
without them gives the other two tables and says so.

    uv run python paper/derive/uniform_i01_chunk_sizes_and_front.py [--from output --workload i01 --dataset ldbc=0.1]
"""
import argparse
import json

from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_cost.compose import CELLS as GRID_CELLS, CHANNELS, MEASURES  # (compose's order: props outer)
from kvcost.layout_cost.cost_models import currencies
from kvevaluator.layouts import DIAGONALS, cell_id, cell_settings
from kvcost.layout_search.front import uniform_name
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from snapshots import (scale_in, add_tree_args, assignment_text, mix_search_row, staircase_reference, tree_of,
                       write_table)

ROWS = "layouts.csv"                                    # cost-layouts' totals (its half view's uniform rows)
LINE = "mix-search i01/mixes-of-Sp-Co-Ch1k-Ch4k-Ch16k-Ch64k-Ch256k.json"   # where the line comes from, in messages
LINE_SETTINGS = {"split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked_c64k", "chunked_c256k"}
CELLS = [cell_id(p, a) for a in DIAGONALS for p in DIAGONALS]
REF = "props_split_adj_condensed"
RAW = MEASURES + CHANNELS                               # the raw lanes, in the old front file's column order
AS_DRAWN = {"total_ops": "wops", "total_bytes": "bytes"}   # the names the draw reads
TEXT = ("workload", "layout", "props", "adjacency", "measured_as")   # the text columns kept, before the totals
WORKLOAD = "ldbc_mix"                                   # I01's name in the CSVs
GRID_SIZE = {"chunked": "chunked_c64k"}                # kvcost's name of the grid's size -> the old front file's
LINES = ("chunked_c64k", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked_c256k")   # the grid's size first
LINE_COLUMNS = (["workload", "cap_bytes", "cap_kind", "cap_x_reference", "option_set", "option", "wops", "bytes"]
                + list(RAW) + ["best_single_within_cap", "best_single_ops", "ops_single_over_mix", "nodes", "assignment"])


def wops_of(r):
    return float(r["G"]) + 1.1 * float(r["S"]) + 0.01 * float(r["N"]) + float(r["P"]) + float(r["D"])


def read(place):
    """(columns, rows) of the uniform layouts in a cost-layouts place's layouts.csv (the half view), each row with its
    line among them and the columns renamed as the draw reads them"""
    rows = LC.read_layouts(place, LC.VIEWS[0], kind="uniform")
    keep = [c for c in TEXT] + ["total_ops", "total_bytes"] + list(RAW)
    cols = [AS_DRAWN.get(c, c) for c in keep]
    return cols, [{"line": f"{i:05d}", **{AS_DRAWN.get(k, k): r[k] for k in keep}} for i, r in enumerate(rows, 2)]


def check_wops(path, rows, name):
    for r in rows:
        w = float(r["wops"])
        if abs(wops_of(r) - w) > 1e-6 * w + 1:
            raise SystemExit(f"{path}: {r[name]} total ops {w} is not G + 1.1 S + 0.01 N + P + D = {wops_of(r)}")


def one_size_uniforms(cells):
    """[(line, nine-cell name, wops, bytes)] of each size's nine uniform layouts, in the chunk-size front's order"""
    out = []
    for line in LINES:
        size = "chunked" if line == LINES[0] else line
        for cell in GRID_CELLS:
            p, a = cell[len("props_"):].split("_adj_")
            name = cell_id(size if p == 'chunked' else p, size if a == 'chunked' else a)
            out.append((line, cell, *cells[name]))
    return out


def line_rows(stair, ref_bytes, uniforms):
    """the "any size" staircase as the old front file's rows: values rounded as chunk_size_front.py wrote them"""
    rows = []
    for i, x in enumerate(stair):
        if x["wops"] is None:
            continue
        fits = [u for u in uniforms if u[3] <= x["cap_bytes"] * (1 + 1e-9)]
        bu = min(fits, key=lambda u: (u[2], u[3])) if fits else None
        rows.append({"line": f"{i:05d}", "workload": WORKLOAD, "cap_bytes": round(x["cap_bytes"], 3),
                     "cap_kind": x["cap_kind"], "cap_x_reference": round(x["cap_bytes"] / ref_bytes, 4),
                     "option_set": "per type", "option": "fewest_ops_within_cap", "wops": round(x["wops"], 3),
                     "bytes": round(x["bytes"], 3), **{k: round(x["lanes"][k], 3) for k in RAW},
                     "best_single_within_cap": f"{bu[1]} at {bu[0]}" if bu else "",
                     "best_single_ops": round(bu[2], 3) if bu else "",
                     "ops_single_over_mix": round(bu[2] / x["wops"], 4) if bu else "", "nodes": x["nodes"],
                     "assignment": assignment_text(x["assignment"], GRID_SIZE)})
    return rows


BY_PART_COLUMNS = ["layout", "props", "adjacency", "kind", "wops", "bytes"]
PARTS = "query_parts.csv"                               # cost-layouts' per-part costs, beside ROWS
KINDS = ("adj", "props", "index")                       # its part kinds (uniform_49.PART_KINDS)


def by_part_rows(cells, w, path):
    """each of the 49 by part kind (KINDS): its frequency-weighted wops and bytes, summed over the workload's queries
    from `path` (query_parts.csv: a query's picked plan's cost per run on each part kind) as a layout's total is (each
    measure times the query's frequency, then the two currencies); each layout's kinds checked to sum to `cells`
    ({cell: (wops, bytes)} of layouts.csv)"""
    often = {(f, q): o for f, q, o, _ in w["items"]}
    acc = {}
    for r in LC.read_query_parts(path.parent):
        v = acc.setdefault((r["layout"], r["part"]), dict.fromkeys(MEASURES, 0.0))
        for m in MEASURES:
            v[m] += often[(r["family"], r["query"])] * float(r[m])
    rows = []
    for layout in CELLS:                                # adjacency outer, properties inner
        p, a = cell_settings(layout)
        kinds = {k: currencies(acc[(layout, k)]) for k in KINDS}
        for cur, want in zip(("ops", "bytes"), cells[layout]):
            if abs(sum(c[cur] for c in kinds.values()) - want) > 1e-6 * max(1.0, want):
                raise SystemExit(f"{path}: {layout}'s parts do not sum to its total {cur} in {ROWS}")
        rows += [{"layout": layout, "props": p, "adjacency": a, "kind": k, "wops": kinds[k]["ops"],
                  "bytes": kinds[k]["bytes"]} for k in KINDS]
    if len(rows) != 49 * len(KINDS):
        raise SystemExit(f"{path}: {len(rows)} rows by part, not 49 x {len(KINDS)}")
    print(f"by part: the 49 from {PARTS}, each layout's parts summing to its total in {ROWS}")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    add_tree_args(ap)
    ap.add_argument("--workload", default="i01", help="the workload (default i01: the paper's)")
    a = ap.parse_args()
    tree, mix = tree_of(a.tree), a.workload
    dataset = benchmarks.dataset_in_run(mix, a.run)
    if dataset is None:
        raise SystemExit(f"{mix}: its dataset is not in this run (--scale)")
    sf = scale_in(a, dataset)
    rows_file = LC.place(tree, dataset, sf, mix) / ROWS
    cols, uniform = read(rows_file.parent)
    check_wops(rows_file, uniform, "layout")
    cells = {r["layout"]: (float(r["wops"]), float(r["bytes"])) for r in uniform}
    missing = [c for c in CELLS if c not in cells]
    if missing:
        raise SystemExit(f"{rows_file}: no row for {missing}")
    write_table("uniform-i01-chunk-sizes-and-front", ["line"] + cols, uniform, key=("line",), out_dir=a.tables)
    (w,) = benchmarks.select(dataset, sf, mix).values()
    write_table("uniform-i01-chunk-sizes-and-front-by-part", BY_PART_COLUMNS,
                by_part_rows(cells, w, rows_file.parent / PARTS), key=None, out_dir=a.tables)
    front_dir = tree.summary(dataset, sf, "mix-benefit", "i01-front")
    if not (front_dir / "uniform_layouts.json").is_file():
        print(f"the fronts' table: not in this tree (no {front_dir}/uniform_layouts.json: run-mix-benefit-w16-front.sh)")
        return
    rw, rb = cells[REF]
    uni = [{**u, "name": uniform_name(u["assign"])} for u in json.load(open(front_dir / "uniform_layouts.json"))]
    sp_co = next(u for u in uni if u["name"] == "Sp-Co")
    if abs(sp_co["ops"] / rw - 1) > 1e-9 or abs(sp_co["bytes"] / rb - 1) > 1e-9:
        raise SystemExit("Sp-Co differs between the 49 and the i01-front's nine: the two scales are not one")
    front = mix_search_row(tree, sf, mix, "per-type", question="front", model=FRONT_WOPS_BYTES)
    if not front["walk_complete"]:
        raise SystemExit("I01's per-type front row: the walk did not finish")
    mixes = [{"ops": p["wops"], "bytes": p["bytes"]} for p in front["points"]]
    srow = mix_search_row(tree, sf, mix, "per-type-any-size", question="staircase", model=FRONT_WOPS_BYTES)
    lcols = list(LINE_COLUMNS)
    line = line_rows(srow["staircase"], staircase_reference(srow["staircase"]), one_size_uniforms(cells))
    check_wops(LINE, line, "line")
    bad = [r["line"] for r in line if r["option_set"] != "per type" or r["option"] != "fewest_ops_within_cap"
           or not r["assignment"] or not {p.split("=", 1)[1].strip() for p in r["assignment"].split(";")} <= LINE_SETTINGS]
    solved = sum(x["wops"] is not None for x in srow["staircase"])   # the paper's: 128 limits; another scale's: the rule's
    if len(line) != solved or bad:
        raise SystemExit(f"{LINE}: {len(line)} rows, not {solved}; not solved per type over Sp/Co/Ch1k-256k: lines {bad[:5]}")
    off = [r["line"] for r in line if abs(float(r["cap_bytes"]) / float(r["cap_x_reference"]) / rb - 1) > 1e-3]
    if off:
        raise SystemExit(f"{LINE}: limits not a multiple of Sp-Co's bytes {rb:.0f}: lines {off[:5]}")
    rows = ([{"file": "complete_front.json", "line": f"{i:05d}", "ops": m["ops"], "bytes": m["bytes"]}
             for i, m in enumerate(mixes)]
            + [{"file": "uniform_layouts.json", "line": f"{i:05d}", "name": u["name"], "ops": u["ops"], "bytes": u["bytes"]}
               for i, u in enumerate(uni)]
            + [{"file": "equal_bytes_front.csv", **r} for r in line])
    columns = ["file", "line", "name", "ops", "bytes"] + [c for c in lcols if c not in ("bytes",)]
    write_table("uniform-i01-chunk-sizes-and-front-front", columns, [{c: r.get(c, "") for c in columns} for r in rows],
                key=("file", "line"), out_dir=a.tables)


if __name__ == "__main__":
    main()
