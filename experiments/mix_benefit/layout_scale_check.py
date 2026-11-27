#!/usr/bin/env python3
"""Does a mixed layout chosen at one scale hold at a larger one? For I01 and I02 (LDBC Interactive, LDBC frequencies):
the layout the small scale's captures choose, priced at the large scale, against the layout the large scale's own
captures choose. Analysis only: every cost is composed from uniform captures; no store is opened, nothing is solved.

The layouts are read from each scale's mix-search result files (kvcost search-mix (and --front), run on that scale's
grid's tree, written into an output tree whose summary/ldbc/sf<scale>/ is the results dir; the grid's three settings per
part), selected by their fields:
    fewest ops + bytes / 4 KB    search-mix --cost-model wops_bytes4k (question "fewest", no limit, weighting sum)
    fewest ops within B          search-mix --byte-limit named: its named point B=<b>, total bytes <= B x Sp-Co's bytes
                                 (settings.B_UNIT) AT THAT SCALE
Per question and option set: a_small = the layout chosen at the small scale, a_large = at the large scale. Printed:
are they the same layout (and where they differ), cost at the large scale of a_small over a_large (1.00 = the small
scale's choice is as good as the large scale's own), and whether a_small still fits the byte limit at the large scale.

Plans at the large scale are picked two ways, each its own labelled output (--picks): `own` = the large scale's own
held-out rows, no anchor (the plan rule for anything priced from parts:
the reported column); `record` = SF3's pinned picks, as a measured store runs them (store mode: a query uniform on its
parts takes the record's anchored, pinned row; never a reported number).

    python3 experiments/mix_benefit/layout_scale_check.py --small <grid> --small-results <dir> --large <grid>
        --large-results <dir> --picks own|record --out DIR
        -> <out>/scale_check_layouts-picks-<record|own>.csv   one row per (workload, option set, question, solved at,
                                                              priced at): the cost with every raw channel, the assignment
           <out>/scale_check-picks-<record|own>.md             the table
The large grid can be a composed one (compose_cells.py: the nine cells from three measured diagonal cells)."""
import argparse
import csv
import os
import sys
from kvevaluator.output_tree import same_place
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import compose
from kvcost.layout_cost import picks
from kvcost.layout_cost import benchmarks
from kvcost.layout_search.search import b_unit_layout
from kvcost.layout_cost.settings import B_UNIT, NAMED_BUDGETS, name_of
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from kvcost.layout_search import results as R
from kvcost.layout_search.results import ResultFile  # (one reader: refuses a file mixing frequencies)

WORKLOADS = ("i01", "i02")
# (option set, by direction at load, its rows' option set: results.home names the file, mixes-of-Sp-Co-Ch64k[-by-direction])
OPTION_SETS = (("per type and direction", True, "per-type-and-direction"), ("per type", False, "per-type"))
BUDGETS = NAMED_BUDGETS
RAW = list(compose.MEASURES) + list(compose.CHANNELS)


def load(grid, by_dir, record, sizes=(), est_blocks=False):
    """a grid's data for the LDBC reads and writes (no microbench: I01 and I02 do not use it); record False = the
    capture's own held-out picks, as when no pick record exists. Priced under the default pick rule, the grid's three
    settings, the LDBC reads and writes, plus `sizes` (compose.Composer.sizes) and the est_blocks channel when asked"""
    rule = picks.PlanRule() if record else picks.PlanRule(record=None)
    return compose.load(grid, by_dir, composer=compose.Composer(criterion=None, sizes=sizes, est_blocks=est_blocks,
                                                              families=benchmarks.families("ldbc", drop=("microbench",)),
                                                              plan_rule=rule))


def result_rows(results, wid, set_file, grid):
    """the rows of one workload and option set in a scale's result file that were priced from `grid`"""
    path = R.home(results, wid, set_file)
    if not os.path.exists(path):
        sys.exit(f"{path}: no result file (kvcost search-mix / front on {grid}'s tree, into the tree of {results})")
    rows = ResultFile(path).read()
    return path, [r for r in rows if same_place(r["capture"], grid) and r["benchmark"] == "ldbc"
                  and r["weighting"] == "sum" and r["max_nodes"] is None]


def one(rows, path, what, **fields):
    """the one proven row with these fields"""
    got = [r for r in rows if all(r.get(k) == v for k, v in fields.items())]
    if len(got) != 1:
        sys.exit(f"{path}: {len(got)} rows for {what} ({fields}); want 1")
    if not got[0]["proven"]:
        sys.exit(f"{path}: the row for {what} is not proven (a node limit stopped it)")
    return got[0]


def as_assign(label, units, path):
    """a result row's assignment {"adj|knows|in": "split"} as {unit: layout}, over exactly the workload's units"""
    assign = R.assignment_of(label)
    if sorted(assign) != sorted(units):
        sys.exit(f"{path}: an assignment over other parts than the workload's ({len(assign)} vs {len(units)})")
    return assign


def chosen(w, data, results, set_file, grid):
    """{question: assign} one scale chose for one workload, read from its result file, plus the units and the B reference's
    bytes (priced here, the same as the row's reference_bytes)"""
    units = compose.workload_units(w, data)
    path, rows = result_rows(results, w["id"], set_file, grid)
    out = {"fewest ops + bytes / 4 KB": as_assign(
        one(rows, path, "the fewest wops_bytes4k", question="fewest", model="wops_bytes4k", limit=None)["assignment"], units, path)}
    front = one(rows, path, "the named limits", question="staircase", limit="named", model=FRONT_WOPS_BYTES)
    for b in BUDGETS:
        p = front["named"].get(f"B={b:g}")
        if p is None:
            sys.exit(f"{path}: no mix within B={b:g} x {front['bytes_reference']}'s bytes")
        out[f"fewest ops within B={b:g}"] = as_assign(p["assignment"], units, path)
    ref = compose.currencies(compose.cost_mix(w, data, b_unit_layout(units)))["bytes"]
    if abs(ref - front["reference_bytes"]) > 1e-9 * ref:
        sys.exit(f"{path}: the row's reference bytes {front['reference_bytes']} are not this grid's {ref}")
    return out, units, ref


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--small", required=True, help="the smaller scale's grid capture (nine cells, held-out rows, writes)")
    ap.add_argument("--small-results", required=True, help="the --out of search-mix (and --front) on --small")
    ap.add_argument("--large", required=True, help="the larger scale's grid capture (measured or composed)")
    ap.add_argument("--large-results", required=True, help="the --out of search-mix (and --front) on --large")
    ap.add_argument("--picks", required=True, choices=("record", "own"), help="how plans are picked at the LARGE scale")
    ap.add_argument("--out", required=True)
    ap.add_argument("--small-own-picks", action="store_true", help="price the SMALL scale by its own held-out rows too, no "
                    "record: for a grid whose cells are not the record's (Chunked at 4 KB under the grid's cell names)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rows, table = [], []
    for set_name, by_dir, set_file in OPTION_SETS:
        small = load(a.small, by_dir, record=not a.small_own_picks)     # the small scale as the tools price it
        sf_small = small.sf
        ws = {n: w for n, w in benchmarks.select("ldbc", sf_small, None).items() if w["id"] in WORKLOADS}
        chosen_small = {n: chosen(w, small, a.small_results, set_file, a.small) for n, w in ws.items()}
        cost_small = {n: {q: compose.cost_mix(w, small, s) for q, s in chosen_small[n][0].items()} for n, w in ws.items()}
        large = load(a.large, by_dir, record=a.picks == "record")
        sf_large = large.sf
        mode = "store" if a.picks == "record" else "option"         # record: SF3's pinned picks, as a store runs them
        for n, w in ws.items():
            small_q, units_s, _ = chosen_small[n]
            large_q, units_l, ref_l = chosen(w, large, a.large_results, set_file, a.large)
            if sorted(units_s) != sorted(units_l):
                sys.exit(f"{n}: the two scales' captures name different units")
            for q in small_q:
                a_s, a_l = small_q[q], large_q[q]
                t_ss = cost_small[n][q]
                t_sl, t_ll = compose.cost_mix(w, large, a_s, mode=mode), compose.cost_mix(w, large, a_l, mode=mode)
                for solved, costed, sf_p, t, assign in ((sf_small, sf_small, sf_small, t_ss, a_s), (sf_small, sf_large, sf_large, t_sl, a_s),
                                                        (sf_large, sf_large, sf_large, t_ll, a_l)):
                    cc = compose.currencies(t)
                    rows.append({"workload_id": w["id"], "workload": n, "option_set": set_name, "question": q, "picks_at_the_large_scale": a.picks,
                                 "layout_solved_at": f"sf{solved}", "cost_at": f"sf{costed}",
                                 "ops_plus_bytes_per_4kb": round(cost_of(t, "sum"), 3), "total_ops": round(cc["ops"], 3),
                                 "total_bytes": round(cc["bytes"], 3), **{x: round(t[x], 3) for x in RAW},
                                 "assignment": assignment_label(assign)})
                differ = sorted(u for u in a_s if a_s[u] != a_l[u])
                B = float(q.split("B=")[1]) if "B=" in q else None
                cs, cl = compose.currencies(t_sl), compose.currencies(t_ll)
                key = "ops" if B is not None else None
                ratio = (cs["ops"] / cl["ops"]) if key else cost_of(t_sl, "sum") / cost_of(t_ll, "sum")
                table.append((w["id"].upper(), set_name, q, len(differ), len(a_s), ratio, cost_of(t_sl, "sum") / cost_of(t_ll, "sum"),
                              cs["bytes"] / cl["bytes"], "" if B is None else ("yes" if cs["bytes"] <= B * ref_l * (1 + 1e-9) else
                                                                           f"no ({cs['bytes'] / ref_l:.2f}x)"),
                              "; ".join(f"{compose_unit(u)}: {a_s[u]} -> {a_l[u]}" for u in differ[:6]) + (" ..." if len(differ) > 6 else "")))
    name = f"picks-{a.picks}"
    with open(os.path.join(a.out, f"scale_check_layouts-{name}.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    L = [f"# A layout chosen at SF{sf_small}, priced at SF{sf_large} (plans at SF{sf_large}: "
         + ("SF3's pinned picks, as a measured store runs them; never a reported number" if a.picks == "record"
            else f"SF{sf_large}'s own held-out picks") + ")", "",
         "> Generated by "
         "`experiments/mix_benefit/layout_scale_check.py`; working output.", "",
         f"Small scale: `{os.path.relpath(a.small, EXP)}`. Large scale: `{os.path.relpath(a.large, EXP)}`. Every cost is composed. "
         "Ratio = cost at the large scale of the layout chosen at the small scale / of the layout the large scale chooses itself "
         "(1.00 = the small scale's choice is as good). For a byte-limit question the ratio is in weighted ops, and the limit is B x "
         f"{name_of(B_UNIT)}'s bytes at each scale (B's unit).", "",
         "| workload | option set | question | parts that differ | ratio (the question's own measure) | ratio in ops + bytes / 4 KB | "
         "bytes ratio | still within B at the large scale | where they differ (small -> large) |", "|---|---|---|---|---|---|---|---|---|"]
    for wid, s, q, nd, nu, r, rs, rb, fits, where in table:
        L.append(f"| {wid} | {s} | {q} | {nd} of {nu} | {r:.3f} | {rs:.3f} | {rb:.3f} | {fits} | {where} |")
    same = sum(1 for t in table if t[3] == 0)
    L += ["", f"{same} of {len(table)} (workload, option set, question) rows choose the same layout at both scales; "
          f"{sum(1 for t in table if t[5] <= 1.005 and not t[8].startswith('no'))} of {len(table)} hold: within 0.5% in the "
          "question's own measure and, for a byte-limit question, still within the limit at the large scale.", ""]
    with open(os.path.join(a.out, f"scale_check-{name}.md"), "w") as f:
        f.write("\n".join(L))
    print("\n".join(L[8:]))
    return 0


def compose_unit(u):
    return " ".join(str(x) for x in u[1:]) + (" props" if u[0] == "props" else " lists")



if __name__ == "__main__":
    sys.exit(main())
