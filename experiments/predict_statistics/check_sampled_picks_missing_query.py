#!/usr/bin/env python3
"""sampled.py picks (compare_sample) with a query a sample cannot cost: the query is named and left out of both
sides, I02 sums the rest, and no KeyError (a 1% owner sample of LDBC SF3 has no TagClass owner, so IC12 has no
estimate on any layout). Hand-made costs on the 49 layouts; no data, no binary. Under a second.

Run:  python3 experiments/predict_statistics/check_sampled_picks_missing_query.py
"""
import math
import sys

from kvcost.predict_accuracy import uniform_49_tallies as U
from sampled import compare_sample

CELLS = list(U.CELLS)
A, B = CELLS[0], CELLS[1]


def costs(queries, cheap, skip=()):
    """{(cell, query): (wops, wops_bytes4k)}: `cheap` the cheapest layout of every query, the rest by their index"""
    return {(c, q): ((1.0 if c == cheap else 2.0 + i), (1.0 if c == cheap else 2.0 + i) * 1.5)
            for i, c in enumerate(CELLS) for q in queries if (c, q) not in skip}


def main():
    fails, cases = [], 0

    def case(what, ok, got=""):
        nonlocal cases
        cases += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f": {got}"))
        if not ok:
            fails.append(what)

    weights = {"q1": 3, "q2": 1, "q3": 2}
    full = costs(weights, A)

    rows, missing = compare_sample(full, costs(weights, A), weights, CELLS)
    case("every query costed: none missing, 2 currencies x (3 queries + I02) rows", missing == [] and len(rows) == 8,
         (missing, len(rows)))
    case("every query costed: each row the same layout, Spearman 1", all(r["same"] == 1 and r["spearman_49"] == 1.0
                                                                         for r in rows), rows[:1])

    rows, missing = compare_sample(full, costs(["q1", "q2"], A), weights, CELLS)   # q3 on no layout
    case("q3 not costed on any layout: named", missing == ["q3"], missing)
    case("q3 not costed: no q3 row, I02 and q1, q2 rows per currency",
         sorted({r["what"] for r in rows}) == ["I02", "q1", "q2"] and len(rows) == 6, sorted({r["what"] for r in rows}))
    case("q3 not costed: every row names it", all(r["queries_not_costed"] == "q3" for r in rows))

    # q2 missing on one layout of the sample: not costed on every layout, so left out too
    rows, missing = compare_sample(full, costs(weights, A, skip={(B, "q2")}), weights, CELLS)
    case("q2 missing on one layout: named", missing == ["q2"], missing)

    # I02 sums only the costed queries: the sample's q1 names B, q2 names A; I02 = 3 q1 + 1 q2 names B
    samp = costs(["q1"], B)
    samp.update(costs(["q2"], A))
    rows, missing = compare_sample(full, samp, weights, CELLS)
    i02 = next(r for r in rows if r["what"] == "I02" and r["currency"] == "wops")
    want = (3 * 3.0 + 1 * 3.0) / (3 * 1.0 + 1 * 1.0)   # B's full cost over A's, over q1 and q2 only (q3 left out)
    case("I02 over q1 and q2: the sample names B, the full tallies A, B's full cost / A's = 12/4",
         i02["named_sampled"] == U.short(B) and i02["named_full"] == U.short(A) and i02["same"] == 0
         and math.isclose(i02["full_cost_of_sampled_name_over_full_named"], want), i02)

    print(f"sampled picks, a query not costed {'PASS' if not fails else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
