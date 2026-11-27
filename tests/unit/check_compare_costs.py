#!/usr/bin/env python3
"""The comparison kvcost predict-accuracy runs (kvcost.cli.compare) on toy cost rows, and the one rounding rule it shares with predict-accuracy --layouts:

  join       the tables join on the layout id (one id per layout, kvevaluator.layouts.cell_id), shown by A's short
             name; a layout on one side only is left out and named; an id in the one-size spelling
             (props_chunked_adj_chunked_c4k), one id given two names in one table, and a single layout (nothing to rank)
             are refused
  rounding   compare.weighted_totals: the naming side at 12 significant digits per query and in total, the judging side
             exact, summed in the order given; compare-costs' workload row equals the rule applied by hand
  summary    with no Sp-Co among the layouts, the printed line says n/a, never nan
  names      a spec's id (kvevaluator.layouts.cell_of_spec) names both Chunked sizes or neither, so the id alone
             names the layout: props_chunked_c64k_adj_chunked_c16k is Ch64k-Ch16k, props_chunked_c16k_adj_chunked_c16k
             Ch16k-Ch16k; uniform_totals.csv names each layout as uniform.csv does (predict_accuracy.uniform.write_totals)
No store, no binary. Under a second.

Run:  python3 tests/unit/check_compare_costs.py
"""
import csv
import os
import sys
import tempfile

from kvcost.cli import compare as CC
from kvcost.layout_cost import compare as CP
from kvcost.layout_cost import cost_table as CT
from kvcost.predict_accuracy import uniform as PU
from kvevaluator import layouts as L
from kvcost.cli.predict import default_set

SET = default_set()   # a predicted set: the default tier's
fails, cases = [], 0


def case(what, ok, got):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}")
    if not ok:
        fails.append(what)


def table(source, costs):
    """cost-table rows from {layout id: [cost of q1, cost of q2]}; frequencies 1 and 3"""
    rows = []
    for lay, (c1, c2) in costs.items():
        for q, f, c in (("q1", 1.0, c1), ("q2", 3.0, c2)):
            r = CT.row(source, lay, "interactive", q, f, "b", 5, None)
            r.update({"wops": c, "bytes": 0.0, "wops_bytes4k": c})
            rows.append({k: ("" if v is None else str(v)) for k, v in r.items()})
    return rows


def refused(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


SP, CO, CH4, OLD = ("props_split_adj_split", "props_condensed_adj_condensed",
                    "props_chunked_c4k_adj_chunked_c4k", "props_chunked_adj_chunked_c4k")


def check_join():
    a = table(SET, {SP: [3.0, 3.0], CO: [2.0, 2.0], CH4: [1.0, 1.0], "props_split_adj_condensed": [9.0, 9.0]})
    b = table("measured", {SP: [3.0, 3.0], CO: [2.5, 2.0], CH4: [1.0, 1.5], "props_condensed_adj_split": [9.0, 9.0]})
    picks, per_layout, one_side, left = CC.compare_tables(a, b, ["wops"])
    w = [p for p in picks if p["kind"] == "workload"][0]
    case("the tables join on the id: 3 layouts, A's pick Ch4k-Ch4k",
         w["layouts"] == 3 and w["named_short"] == "Ch4k-Ch4k" and w["named"] == CH4, (w["layouts"], w["named_short"]))
    case("a layout on one side only is named, per side", one_side == {"A": ["Sp-Co"], "B": ["Co-Sp"]}, one_side)
    case("layouts.csv carries A's id", sorted(r["layout"] for r in per_layout) == sorted([SP, CO, CH4]),
         [r["layout"] for r in per_layout])
    old = table("measured", {CH4: [1.0, 1.5], SP: [3.0, 3.0], CO: [2.5, 2.0]})
    for r in old:                                  # a table with one-size ids (cost_table.row refuses the id)
        r["layout"] = OLD if r["layout"] == CH4 else r["layout"]
    case("a table holding an id in the old spelling is refused", refused(lambda: CC.compare_tables(a, old, ["wops"])), "")
    two = table("measured", {CH4: [1.0, 1.0], SP: [2.0, 2.0], CO: [2.0, 2.0]})
    two[0]["name"] = "Ch64k-Ch64k"                     # its name is Ch4k-Ch4k
    case("one id given two names in one table is refused", refused(lambda: CC.compare_tables(a, two, ["wops"])), "")
    one = table("measured", {SP: [3.0, 3.0]})
    try:
        CC.compare_tables(one, one, ["wops"])
        why = ""
    except SystemExit as e:
        why = str(e)
    case("one layout in both tables is refused, saying a comparison needs 2", "at least 2" in why, why)


def check_rounding():
    # costs that differ past the 12th digit on the naming side tie; the judging side keeps them apart
    x, y = 1.0000000000001, 1.0000000000002
    a = table(SET, {SP: [x, x], CO: [y, y]})
    b = table("measured", {SP: [y, y], CO: [x, x]})
    picks, per_layout, _, _ = CC.compare_tables(a, b, ["wops"])
    w = [p for p in picks if p["kind"] == "workload"][0]
    by = {r["layout"]: r for r in per_layout}
    pq = CP.naming_side({(c, k): v for c, vs in ((SP, (x, x)), (CO, (y, y))) for k, v in zip(("q1", "q2"), vs)})
    want_a = CP.weighted_totals(pq, [SP, CO], ["q1", "q2"], {"q1": 1.0, "q2": 3.0}, naming=True)
    case("the naming side ties at 12 digits: the first id in sort order is named",
         w["named"] == CO and by[SP]["a"] == by[CO]["a"] == want_a[SP] == want_a[CO], (w["named"], by[SP]["a"]))
    case("the judging side is exact: its best is the cheaper one", w["measured_best"] == CO and by[CO]["b"] < by[SP]["b"],
         (by[CO]["b"], by[SP]["b"]))


def check_summary():
    a = table(SET, {SP: [3.0, 3.0], CO: [2.0, 2.0]})
    b = table("measured", {SP: [3.0, 3.0], CO: [2.0, 2.0]})
    picks, _, _, _ = CC.compare_tables(a, b, ["wops"])
    line = [x for x in CC.summary_lines(picks, "wops") if "A's cheapest layout is" in x]
    case("no Sp-Co: the summary says n/a, never nan", line and "Sp-Co costs n/a," in line[0]
         and "removes n/a of Sp-Co's extra cost" in line[0] and "nan" not in line[0],
         line[:1])


def check_names():
    def part(kb):
        return {"group": "owner", "owners": {"pack": kb * 1024}, "split": "never"}
    got = {(p, a): L.name_of(L.cell_of_spec({"vertex_props": part(p), "adjacency": part(a)}))
           for p, a in ((64, 16), (16, 16), (64, 64), (16, 64))}
    want = {(64, 16): "Ch64k-Ch16k", (16, 16): "Ch16k-Ch16k", (64, 64): "Ch64k-Ch64k", (16, 64): "Ch16k-Ch64k"}
    case("a Chunked layout named from its spec, sizes per side", got == want, got)
    case("a spec with no single props and adjacency part (a mix) has no uniform name",
         L.cell_of_spec({"vertex_props_by_label": {}}) is None, "")
    cell = "props_chunked_c64k_adj_chunked_c16k"   # uniform49's Ch64k-Ch16k
    rows = [CT.row(SET, cell, "interactive", "ic2", 1.0, "b", 5, None, "Ch64k-Ch16k")]
    with tempfile.TemporaryDirectory() as t:
        path = PU.write_totals(os.path.join(t, "uniform_totals.csv"), {cell: 1.0}, "wops", PU.names_of(rows))
        with open(path) as f:
            got = [r["layout"] for r in csv.DictReader(f)]
    case("uniform_totals.csv names a layout as uniform.csv does, and the id read alone gives the same name",
         got == ["Ch64k-Ch16k"] and L.name_of(cell) == "Ch64k-Ch16k", f"{got}, the id read alone: {L.name_of(cell)}")


def main():
    check_names()
    check_join()
    check_rounding()
    check_summary()
    ok = not fails
    print(f"compare-costs {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
