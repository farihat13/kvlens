#!/usr/bin/env python3
"""Derive bill-49's table: the 49 uniform layouts of I01 (the paper: at SF3) under DynamoDB's request units, from one
output tree (--from; the paper's runs by default), each under the bill's three forms: exact (every read and write
request rounded up alone), and lower and upper (from per-step totals: bounds on its reads; on its writes they count a
DELETE as 1 unit). The bill is R + r x W for a write price r; the table keeps R and W apart.

Nothing is costed here: the 49 are read from kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's layouts.csv, its
measured chunk view's rows (every count and byte lane and the bill's unit lanes), in the tree's
summary/ldbc/<scale>/layout-costs/i01/measured/ (kvcost.layout_cost.layout_costs).

Writes build/data/bill-49-i01.csv (49 x 3 rows): per layout and form, read_units and write_units and the raw lanes
they were computed from.

    uv run python paper/derive/bill_49.py [--from output --dataset ldbc=0.1]
"""
import argparse

from kvcost.layout_cost import compose
from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_cost import cost_models as CM
from snapshots import add_tree_args, scale_in, tree_of, write_table


FORMS = ("exact", "lower", "upper")
COLUMNS = ["workload", "option_set", "cell", "props", "adjacency", "form", "read_units", "write_units",
           *compose.CHANNELS, "assignment"]
TABLE, DATASET, WORKLOAD = "bill-49-i01", "ldbc", "i01"     # the table, and the workload whose layout-costs place it reads
TEXT = ("workload", "layout", "chunk_view", "kind", "props", "adjacency", "name", "spec_sha", "measured_as")


def read_49(place):
    """{cell: (props, adjacency, {lane: value})} of the 49 in layouts.csv's order, its measured view's rows (the bill's)"""
    rows = LC.read_layouts(place, LC.BILL_VIEW, kind="uniform")
    if len(rows) != 49 or any(r["bill_read_exact"] == "" for r in rows):
        raise SystemExit(f"{place}: {len(rows)} uniform layouts, not 49, or no bill lanes (kvcost cost-layouts ... "
                         f"Sp-Sp Sp-Co ... Ch256k-Ch256k on a measured capture with the exact unit columns)")
    return {r["layout"]: (r["props"], r["adjacency"], {k: float(v) for k, v in r.items() if k not in TEXT}) for r in rows}


def form_rows(wid, option_set, cell, props, adj, t, assignment=""):
    """one row per form: its read and write units, and the raw lanes"""
    out = []
    for form in FORMS:
        reads, writes = CM.BILL_UNITS[f"bill_{form}"]
        out.append({"workload": wid, "option_set": option_set, "cell": cell, "props": props, "adjacency": adj,
                    "form": form, "read_units": round(t[reads], 3), "write_units": round(t[writes], 3),
                    **{c: round(t[c], 3) for c in compose.CHANNELS}, "assignment": assignment})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    a = ap.parse_args()
    tree = tree_of(a.tree)
    sf = scale_in(a, DATASET)
    if sf is None:
        print(f"{TABLE}: {DATASET} is not in this run (--scale)")
        return
    path = LC.place(tree, DATASET, sf, WORKLOAD)
    if not (path / "layouts.csv").is_file():
        raise SystemExit(f"{TABLE}: no {path}/layouts.csv (kvcost cost-layouts --workload {WORKLOAD} Sp-Sp Sp-Co ... writes it)")
    rows = [x for cell, (props, adj, t) in read_49(path).items()
            for x in form_rows(WORKLOAD, "uniform", cell, props, adj, t)]
    write_table(TABLE, COLUMNS, rows, key=("option_set", "cell", "form"), out_dir=a.tables)


if __name__ == "__main__":
    main()
