#!/usr/bin/env python3
"""Derive ldbc-random-workloads-triangle's tables: per 5% tile of LDBC Interactive's complex : short : updates split (each
group's share of the cost on Sp-Co), the uniform layouts that are the cheapest for its 100 random workloads, from one
output tree's summary/ldbc/<scale>/random-spread/ (--from; the paper's runs by default:
experiments/random_spread/random_spread.py --dataset ldbc=<S>) and I01's per-query costs beside its 49
(summary/ldbc/<scale>/layout-costs/i01/measured/query_costs.csv, its half view: kvcost cost-layouts --workload i01
Sp-Sp Sp-Co ... Ch256k-Ch256k).

  build/data/ldbc-random-workloads-triangle.csv         per (layout_set, tile, layout): the tile's centre and wins (of
                                                        its 100 workloads; a layout that wins none has no row);
                                                        layout_set = the 25 layouts over Sp, Co, Ch1k-16k, or the 49
  build/data/ldbc-random-workloads-triangle-groups.csv  per group: its queries, and I01's share of the cost on Sp-Co
  build/data/ldbc-random-workloads-triangle-frequency.csv, -frequency-groups.csv   the same two with the split by each
                                                        group's share of the runs
  build/data/ldbc-random-workloads-triangle-queries.csv per I01 query: its group, I01's frequency and its cost per run
                                                        on Sp-Co (wops_bytes4k)
  build/numbers/random_spread.tex   random-spread.tiles, random-spread.largest.tiles (the tiles the layout that wins
                                    the most holds, over the 49)

Checks: 400 tiles per set, every tile's wins add up to 100; a tile's top layout and whether it is clear (at least 50
wins, no tie) equal winners.csv; the shares add up to 1; 12 complex, 7 short and 8 update queries, I01's 27. A cell id
is written in its one spelling (kvevaluator.layouts.cell_id).

    uv run python paper/derive/ldbc_random_workloads_triangle.py [--from output --dataset ldbc=0.1 --tables DIR]
"""
import argparse
import csv
import json

from kvevaluator.layouts import cell_id, cell_settings
from kvcost.layout_cost import layout_costs as LC

from kvcost.layout_cost.workloads.make_workloads import i01
from snapshots import add_tree_args, scale_in, tree_of, write_numbers, write_table

NAME = "ldbc-random-workloads-triangle"
COLUMNS = {"cost_sp_co": NAME, "executions": f"{NAME}-frequency"}   # the snapshot's column -> the tables' name
NUMBERS_COLUMN = "cost_sp_co"                                          # the column the number keys read
GROUPS = {"complex": 12, "short": 7, "update": 8}
SP_CO = "props_split_adj_condensed"                                   # Sp-Co, as query_costs.csv names it
GROUP_OF = {"ic": "complex", "is": "short", "iu": "update"}           # LDBC Interactive's three kinds, by the query's id
SIDES = {"share_left": "share_complex", "share_right": "share_short", "share_top": "share_update"}


SPREAD = None   # the run's random-spread dir (main sets it from --from and --scale)
COSTS = None    # I01's per-query costs on the 49 (main sets it: layout-costs/i01/measured/)


def read(name, where=None):
    with open((where or SPREAD) / name, newline="") as fh:
        return list(csv.DictReader(fh))


def one_id(cell):
    """a cell id in its one spelling (chunked_c64k as chunked)"""
    return cell_id(*cell_settings(cell))


def tables(column, run, sets, n_of):
    """(the tile rows, the group rows, tiles per set, tiles per winning layout per set) of one column, checked"""
    rows, wins = [], {}
    for r in read("tiles.csv"):
        if r["column"] != column:
            continue
        layout = one_id(r["layout"])
        if layout not in sets[r["layout_set"]]:
            raise SystemExit(f"tiles.csv: {r['layout']} is not in the set {r['layout_set']}")
        rows.append({"layout_set": r["layout_set"], "tile": f"{int(r['tile']):03d}", "layout": layout,
                     **{SIDES[k]: r[k] for k in SIDES}, "wins": r["wins"]})
        wins.setdefault((r["layout_set"], int(r["tile"])), {})[layout] = int(r["wins"])
    bad = [k for k, v in wins.items() if sum(v.values()) != 100]
    per_set = {s: sum(1 for k in wins if k[0] == s) for s in sets}
    if bad or set(per_set.values()) != {400}:
        raise SystemExit(f"tiles.csv {column}: tiles per set {per_set}; wins not adding up to 100 in {bad[:5]}")
    shares = {(r["layout_set"], int(r["tile"])): sum(float(r[k]) for k in SIDES.values()) for r in rows}
    if any(abs(v - 1) > 1e-9 for v in shares.values()):
        raise SystemExit(f"tiles.csv {column}: a tile's shares do not add up to 1")
    checked, regions = 0, {}
    for r in read("winners.csv"):
        if r["column"] != column:
            continue
        w = wins[(r["layout_set"], int(r["tile"]))]
        order = sets[r["layout_set"]]
        top = max(sorted(w, key=order.index), key=lambda L: w[L])        # the first of the most wins, in the set's order
        clear = int(w[top] >= 50 and list(w.values()).count(w[top]) == 1)
        if (top, w[top], clear) != (one_id(r["winner"]), int(r["wins"]), int(r["clear"])):
            raise SystemExit(f"winners.csv {column}: {r['layout_set']} tile {r['tile']}: {r['winner']} {r['wins']} "
                             f"{r['clear']}, tiles.csv gives {top} {w[top]} {clear}")
        checked += 1
        regions.setdefault(r["layout_set"], {}).setdefault(top, 0)
        regions[r["layout_set"]][top] += 1
    if checked != 800:
        raise SystemExit(f"winners.csv {column}: {checked} tiles checked, not 800")
    mark = {}
    for r in read("marks.csv"):
        if r["column"] == column and r["mark"] == "i01":
            mark.setdefault(r["group"], set()).add(r["share"])
    if any(len(v) != 1 for v in mark.values()) or abs(sum(float(next(iter(v))) for v in mark.values()) - 1) > 1e-9:
        raise SystemExit(f"marks.csv {column}: I01's shares {mark}")
    print(f"{column}: {len(rows)} (set, tile, layout) rows; 2 sets x 400 tiles, each adding up to 100 draws; "
          f"{checked} winners equal")
    groups = [{"group": g, "queries": n_of[g], "i01_share": next(iter(mark[g]))} for g in GROUPS]
    return rows, groups, per_set, regions


def queries(group_of):
    """per I01 query: its group, I01's runs of it and its cost per run on Sp-Co (wops_bytes4k), checked"""
    cost = {}
    for r in read("query_costs.csv", COSTS):
        if r["layout"] == SP_CO and r["chunk_view"] == "half":
            if r["query"] in cost or GROUP_OF[r["query"][:2]] != group_of.get(r["query"]):
                raise SystemExit(f"query_costs.csv: {r['query']} on {SP_CO}: a second row, or group "
                                 f"{GROUP_OF[r['query'][:2]]} against groups.csv's {group_of.get(r['query'])}")
            cost[r["query"]] = float(r["wops_bytes4k"])
    freq = {q: float(n) for _, q, n, _ in i01()["items"]}
    if set(cost) != set(group_of) or set(freq) != set(group_of):
        raise SystemExit(f"queries: groups.csv {len(group_of)}, query_costs.csv on {SP_CO} {len(cost)}, I01 {len(freq)}")
    print(f"queries: {len(cost)} I01 queries, each one Sp-Co row of query_costs.csv, in its groups.csv group")
    return [{"query": q, "group": group_of[q], "i01_frequency": freq[q], "cost_sp_co": cost[q]} for q in group_of]


def main():
    global SPREAD, COSTS
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    add_tree_args(ap)
    a = ap.parse_args()
    SPREAD = tree_of(a.tree).summary("ldbc", scale_in(a, "ldbc"), "random-spread")
    COSTS = LC.place(tree_of(a.tree), "ldbc", scale_in(a, "ldbc"), "i01")
    run = json.load(open(SPREAD / "random_spread_run.json"))
    if run["groups"] != list(GROUPS) or run["queries_per_group"] != GROUPS or run["draws"] != 100:
        raise SystemExit(f"random_spread_run.json: groups {run['groups']} {run['queries_per_group']}, draws {run['draws']}")
    sets = {k: [cell_id(p, a) for p in v["settings"] for a in v["settings"]] for k, v in run["sets"].items()}
    if sorted(len(v) for v in sets.values()) != [25, 49]:
        raise SystemExit(f"random_spread_run.json: layout sets {sorted(len(v) for v in sets.values())}, not 25 and 49")
    n_of, group_of = {}, {}
    for r in read("groups.csv"):
        n_of[r["group"]] = n_of.get(r["group"], 0) + 1
        group_of[r["query"]] = r["group"]
    if n_of != GROUPS:
        raise SystemExit(f"groups.csv: {n_of}")
    write_table(f"{NAME}-queries", ["query", "group", "i01_frequency", "cost_sp_co"], queries(group_of), key=("query",),
                out_dir=a.tables)
    for column, name in COLUMNS.items():
        rows, groups, per_set_c, regions_c = tables(column, run, sets, n_of)
        write_table(name, ["layout_set", "tile", "layout", "share_complex", "share_short", "share_update", "wins"], rows,
                    key=("layout_set", "tile", "layout"), out_dir=a.tables)
        write_table(f"{name}-groups", ["group", "queries", "i01_share"], groups, key=("group",), out_dir=a.tables)
        if column == NUMBERS_COLUMN:
            per_set, regions = per_set_c, regions_c
    figure_set = next(k for k, v in sets.items() if len(v) == 49)
    write_numbers("random_spread", {"random-spread.tiles": str(per_set[figure_set]),
                                    "random-spread.largest.tiles": str(max(regions[figure_set].values()))},
                  f"random-spread (sf{scale_in(a, 'ldbc')}), winners.csv, the {figure_set} set, column {NUMBERS_COLUMN}",
                  out_dir=a.tables)


if __name__ == "__main__":
    main()
