#!/usr/bin/env python3
"""compose never prices a missing part as 0: the test of _composed's refusal. Analysis only: opens no store.

    python3 tests/gates/check_compose_missing_part.py --grid <captures/ldbc/sf3> \\
        --size 1k [--query ic2]

On one LDBC query, priced with every list on the size option (props Condensed):
  1. the intact data prices (a control: a refusal here would be a false alarm);
  2. one size-option part row dropped (a list unit of one plan and param) must raise;
  3. every part row of one (cell, plan, param) of a uniform cell dropped must raise;
  4. a second compose.load without sizes, then the size option priced, must raise.
Exit 1 when a case goes the wrong way.

    ... --sweep [--by-dir] [--size K ...]
A refusal depends only on whether a source has rows for a (plan, param, unit), and a source is the option of the unit
(a size option) or the cell its layout and one partner layout name. So pricing every query of the capture (every
family) with each unit on each option and every other unit on each of the three layouts reaches every source a front,
a pricing pass or a cost cache fill over these options can read: 0 refusals here means none of those runs priced a
missing part. Prints the (query, unit, option, partner) pricings made and the refusals."""
import argparse
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks, picks, settings


def load(grid, by_dir, sizes=()):
    """a grid's LDBC reads and writes (no microbench), each grid by its own held-out picks (no pick record), the
    default pick rule and the grid's three settings plus `sizes`: the load the scale checks price"""
    return compose.load(grid, by_dir, composer=compose.Composer(criterion=None, sizes=sizes,
                                                              families=benchmarks.families("ldbc", drop=("microbench",)),
                                                              plan_rule=picks.PlanRule(record=None),
                                                              chunk_view="measured"))   # a snapshot made before the last-chunk lane


def raises(fn):
    """(True, the message) if fn raises SystemExit, else (False, its value)"""
    try:
        return False, fn()
    except SystemExit as e:
        return True, str(e)


def sweep(a):
    """every (query, unit, option, partner layout) priced; the refusals listed; exit 1 on any, or on 0 pricings"""
    sizes = settings.parse_sizes(a.size, a.grid)
    data = load(a.grid, a.by_dir, sizes=sizes)
    n, refused = 0, []
    for key in sorted(k for k in data if isinstance(k, tuple) and "cost" in data[k]):
        d = data[key]
        units = sorted({u for per in d["cost"].values() for u in per if u is not None}, key=str)
        pp = compose.params_of(d, None)
        for u in units:
            for opt in compose.options_of(data, u):
                for partner in compose.LAYOUTS:
                    assign = {x: partner for x in units}
                    assign[u] = opt
                    n += 1
                    r, v = raises(lambda: compose.query_cost(d, pp, assign, set(units)))
                    if r:
                        refused.append((key, u, opt, partner, v))
    for x in refused[:10]:
        print("REFUSED", x[0], x[1], x[2], "partner", x[3], ":", x[4][:150])
    print(f"sweep ({'per type and direction' if a.by_dir else 'per type'}; sizes {[s[0] for s in sizes]}): {n:,} pricings, "
          f"{len(refused)} refused -> {'PASS' if n and not refused else 'FAIL'}")
    return 0 if n and not refused else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", required=True)
    ap.add_argument("--size", action="append", default=[], metavar="K",
                    help="another chunk size (1k), its Chunked/Chunked in --grid's place (the cases: one; --sweep: any number)")
    ap.add_argument("--query", default="ic2")
    ap.add_argument("--sweep", action="store_true", help="every query, unit, option and partner layout priced (see above)")
    ap.add_argument("--by-dir", action="store_true", help="--sweep: list units per type and direction")
    a = ap.parse_args()
    if a.sweep:
        return sweep(a)
    if len(a.size) != 1:
        sys.exit("the cases take one --size")
    sizes = settings.parse_sizes(a.size, a.grid)
    oid = sizes[0][0]
    data = load(a.grid, False, sizes=sizes)
    d = data[("interactive", a.query)]
    units = sorted({u for per in d["cost"].values() for u in per if u is not None})
    assign = {u: (oid if u[0] == "adj" else "condensed") for u in units}
    pp = compose.params_of(d, None)
    cost = lambda dd: compose.query_cost(dd, pp, assign, set(units))
    bad = 0
    r, v = raises(lambda: cost(d))
    print(f"1. intact, {a.query} on {oid}: {'RAISED (false alarm)' if r else 'priced, S ' + format(v['S'], ',.1f')}")
    bad += r
    d2 = copy.deepcopy(d)
    key = next(x for x in sorted(d2["cost"], key=str) if x[0] == oid and any(u and u[0] == "adj" for u in d2["cost"][x]))
    u = next(u for u in sorted(d2["cost"][key], key=str) if u and u[0] == "adj")
    del d2["cost"][key][u]
    r, v = raises(lambda: cost(d2))
    print(f"2. size row {key} {u} dropped: {'raised: ' + v[:120] if r else 'PRICED (silent 0)'}")
    bad += not r
    d3 = copy.deepcopy(d)
    cell = "props_condensed_adj_condensed"
    key = next(x for x in sorted(d3["cost"], key=str) if x[0] == cell)
    del d3["cost"][key]
    uni = {u: "condensed" for u in units}
    r, v = raises(lambda: compose.query_cost(d3, pp, uni, set(units)) if key[1] in d3["valid"].get(cell, set()) else None)
    print(f"3. every part row of {key} dropped (plan valid there: {key[1] in d3['valid'].get(cell, set())}): {'raised: ' + v[:120] if r else 'PRICED (silent 0)'}")
    bad += not r
    data2 = load(a.grid, False)
    r, v = raises(lambda: cost(data2[("interactive", a.query)]))
    print(f"4. a second load without sizes, then {oid} priced: {'raised: ' + v[:120] if r else 'PRICED (silent 0)'}")
    bad += not r
    print(f"RESULT: {'PASS' if not bad else 'FAIL'} (4 cases, {bad} wrong)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
