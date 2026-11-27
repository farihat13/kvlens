#!/usr/bin/env python3
"""kvcost predict-accuracy --layouts with a query costed on no layout (layouts.costed_queries): the query is named and
left out of both sides, a query costed on some layouts stays (its uncosted layouts leave the comparison, as before),
and the workload's order is kept. A 1% owner sample of LDBC SF3 has no TagClass owner, so IC12 has no estimate on any
layout; the report crashed on it (IndexError). Hand-made dicts; no data, no binary. Under a second.

Run:  python3 tests/unit/check_layouts_query_not_costed.py
"""
import sys

from kvcost.predict_accuracy.layouts import costed_queries


def main():
    fails, cases = [], 0

    def case(what, got, want):
        nonlocal cases
        cases += 1
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}" + ("" if ok else f" (want {want})"))
        if not ok:
            fails.append(what)

    cells, queries = ["a", "b", "c"], ["ic1", "ic12", "is1"]
    every = {(c, q): (q, 1.0, 0) for c in cells for q in queries}
    case("every query costed", costed_queries(every, cells, queries), (queries, []))
    no12 = {k: v for k, v in every.items() if k[1] != "ic12"}
    case("ic12 on no layout: left out and named", costed_queries(no12, cells, queries), (["ic1", "is1"], ["ic12"]))
    one = {k: v for k, v in every.items() if k != ("b", "ic12")}
    case("ic12 missing on one layout only: kept", costed_queries(one, cells, queries), (queries, []))
    case("nothing costed: every query named", costed_queries({}, cells, queries), ([], queries))

    print(f"layouts, a query not costed {'PASS' if not fails else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
