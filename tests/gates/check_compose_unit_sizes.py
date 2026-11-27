#!/usr/bin/env python3
"""compose.set_unit_sizes, the per-unit chunk-size filter of options_of (for the pruned per type
and direction run). Analysis only: opens no store.

    python3 tests/gates/check_compose_unit_sizes.py --grid <captures/ldbc/sf3> \\
        --size 1k --size 4k

Per type and direction units, two sizes. Cases:
  1. nothing set: every unit's options = the three layouts + every size (the default path, unchanged);
  2. one direction unit restricted to the first size: that unit loses the others, every other unit keeps all
     (exact_search.Problem takes its options from options_of, python/kvcost/layout_search/search.py's Problem.__init__);
  3. a unit no query has must raise; 4. a size that is not an option must raise.
Exit 1 when a case goes the wrong way, or when a size loads no rows (case 0: the cases would test nothing)."""
import argparse
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
    try:
        fn()
        return False, ""
    except SystemExit as e:
        return True, str(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", required=True)
    ap.add_argument("--size", action="append", default=[], metavar="K", help="another chunk size (1k): its Chunked/Chunked in --grid's place; repeatable")
    a = ap.parse_args()
    if len(a.size) < 2:
        sys.exit("give two --size")
    data = load(a.grid, True,
                  sizes=settings.parse_sizes(a.size, a.grid))
    units = sorted({u for d in data.values() if isinstance(d, dict) for per in d.get("cost", {}).values() for u in per if u is not None}, key=str)
    sized = tuple(data.sized)
    rows = {s: sum(1 for d in data.values() if isinstance(d, dict) for x in d.get("cost", {}) if x[0] == s) for s in sized}
    print(f"0. sized rows loaded: {', '.join(f'{s} {n:,}' for s, n in rows.items())}; {data.sized_checked:,} runs checked against the grid")
    if not all(rows.values()) or not data.sized_checked:
        print("RESULT: FAIL (a size loaded no rows: the cases below would test nothing)")
        return 1
    bad = 0
    full = {u: compose.options_of(data, u) for u in units}
    ok = all(o[:len(compose.LAYOUTS) + len(sized)] == compose.LAYOUTS + sized for o in full.values())
    print(f"1. nothing set, {len(units)} units, sizes {sized}: {'every unit has every size' if ok else 'WRONG'}")
    bad += not ok
    u0 = next(u for u in units if u[0] == "adj" and len(u) == 3)
    compose.set_unit_sizes(data, {u0: {sized[0]}})
    after = {u: compose.options_of(data, u) for u in units}
    want = compose.LAYOUTS + (sized[0],) + full[u0][len(compose.LAYOUTS) + len(sized):]
    others = [u for u in units if u != u0 and after[u] != full[u]]
    ok = after[u0] == want and not others
    print(f"2. {u0} restricted to {sized[0]}: its options {after[u0]}; other units changed: {len(others)} -> {'ok' if ok else 'WRONG'}")
    bad += not ok
    r, msg = raises(lambda: compose.set_unit_sizes(data, {("adj", "no_such_type", "out"): {sized[0]}}))
    print(f"3. a unit no query has: {'raised: ' + msg[:100] if r else 'ACCEPTED'}")
    bad += not r
    r, msg = raises(lambda: compose.set_unit_sizes(data, {u0: {"chunked_c999k"}}))
    print(f"4. a size that is not an option: {'raised: ' + msg[:100] if r else 'ACCEPTED'}")
    bad += not r
    print(f"RESULT: {'PASS' if not bad else 'FAIL'} (4 cases, {bad} wrong)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
