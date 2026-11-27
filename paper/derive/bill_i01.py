#!/usr/bin/env python3
"""Derive bill-i01's table: I01 at SF3 (64 KB chunks) under DynamoDB's request units, the nine uniform layouts and the
mix with the lowest bill (solved per vertex type and per edge type under the exact form), each under the bill's three
forms: exact (every read and write request rounded up alone), and lower and upper (from per-step totals: bounds on its
reads; on its writes they count a DELETE as 1 unit).

Reads BILL_SF3's bill_optimum.csv (the nine) and MIX_SEARCH_SF3's per-type row (the mix: its lanes hold every form's
units). Writes build/data/bill-i01.csv: 30 rows in bill_optimum.csv's columns (the mix's computed here from its lanes,
rounded as bill_search.py writes them), plus bill_x_sp_co = bill / Sp-Co's exact bill (the figure's bars: the exact
form; the other two are kept, not drawn).

    uv run python paper/derive/bill_i01.py
"""
import csv

from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.compose import CHANNELS, currencies
from snapshots import PAPER, BILL_SF3, assignment_text, mix_search_row, write_table

SOURCE = BILL_SF3 / "chunked-64k/i01-rw-ldbc-mix/bill_optimum.csv"
MIX_OPTION = "per_type_fewest_bill_exact"      # the mix solved under the exact form
REF = "props_split_adj_condensed"              # Sp-Co, the 1x of the figure
FORMS = ("exact", "lower", "upper")


def mix_rows(singles):
    """the mix under each form, as bill_search.py wrote its rows; singles: the nine's rows of bill_optimum.csv"""
    r = mix_search_row(PAPER, "3", "i01", "per-type", question="fewest", model="bill_exact", limit=None, weighting="sum")
    t, out = r["lanes"], []
    for form in ("lower", "upper", "exact"):                          # bill_search.FORMS' order
        reads, writes = CM.BILL_UNITS[f"bill_{form}"]
        bill = getattr(CM, f"bill_{form}")(t)
        best1 = min((x for x in singles if x["form"] == form), key=lambda x: (float(x["bill"]), x["option"]))
        out.append({"workload": "ldbc_mix", "form": form, "option_set": "per type", "option": MIX_OPTION,
                    "bill": round(bill, 3), "read_units": round(t[reads], 3), "write_units": round(t[writes], 3),
                    "bill_x_best_single": round(bill / float(best1["bill"]), 4),
                    **{k: round(x, 3) for k, x in currencies(t).items()}, **{c: round(t[c], 3) for c in CHANNELS},
                    "nodes": r["nodes"], "assignment": assignment_text(r["assignment"])})
    return out


def main():
    with open(SOURCE, newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames) + ["bill_x_sp_co"]
        rows = [r for r in reader if r["option_set"] == "single"]
    rows += mix_rows(rows)
    options = sorted({r["option"] for r in rows})
    if len(options) != 10 or sorted((r["option"], r["form"]) for r in rows) != sorted(
            (o, f) for o in options for f in FORMS):
        raise SystemExit(f"{SOURCE}: not the nine uniform layouts and the mix, each under {', '.join(FORMS)}")
    ref = float(next(r["bill"] for r in rows if r["option"] == REF and r["form"] == "exact"))
    for r in rows:
        r["bill_x_sp_co"] = float(r["bill"]) / ref
    write_table("bill-i01", columns, rows, key=("option_set", "option", "form"))


if __name__ == "__main__":
    main()
