#!/usr/bin/env python3
"""compose.query_candidates hands out exactly what compose.query_pick picks from. On a grid capture, for every read query
under the nine uniform assignments and two non-uniform ones (adjacency units alternating between two layouts): the
reported vectors equal query_pick's own (every plan, every channel); query_pick's pick is among them and never
measure-only; and on a non-uniform option with held-out parts, the pick is the held-out rule's (candidates.decide, no
anchor) over the held-out vectors query_candidates returns. --control inflates the picked plan's held-out vector 10x on
every option: the check must then find a pick that disagrees (exit 0 only if it does).

    python3 tests/unit/check_query_candidates.py --sf 0.1 [--grid DIR] [--control]
"""
import argparse
import sys

from kvcost.layout_cost import compose, picks
from kvevaluator.output_tree import Tree
from kvevaluator.plan_choice import candidates as C


def assignments(units):
    """the nine uniform assignments, then two non-uniform ones (props one layout, adjacency units alternating)"""
    out = [(f"{p}/{a}", {u: (p if u[0] == "props" else a) for u in units}) for p in compose.LAYOUTS for a in compose.LAYOUTS]
    adj = [u for u in units if u[0] == "adj"]
    for p, (a1, a2) in (("split", ("split", "chunked")), ("condensed", ("condensed", "split"))):
        out.append((f"{p}/{a1}+{a2}", {u: (p if u[0] == "props" else (a1, a2)[adj.index(u) % 2]) for u in units}))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", default=None)
    ap.add_argument("--control", action="store_true")
    a = ap.parse_args()
    grid = a.grid or str(Tree().place_dir("captures", "ldbc", a.sf))
    data = compose.load(grid, by_dir=False, composer=compose.Composer(criterion=None, plan_rule=picks.PlanRule()))
    all_units = compose.grid_units(data)
    cases = bad = decided = 0
    for (fam, q), d in sorted(data.items()):
        if fam == "writes" or not d.get("cost"):
            continue
        units = {u for per in d["cost"].values() for u in per if u is not None}
        params = compose.params_of(d, None)
        for name, assign in assignments(sorted(all_units)):
            best, tots, _pu = compose.query_pick(d, params, assign, units)
            got, _gpu, tots_h, mo = compose.query_candidates(d, params, assign, units)
            if best is None:
                ok = not got
            else:
                ok = sorted(got) == sorted(tots) and all(got[v] == tots[v] for v in tots) and best in got and best not in mo
                uniform = len({assign[u] for u in units if u[0] == "props"}) == 1 and len({assign[u] for u in units if u[0] == "adj"}) == 1
                if ok and tots_h and not uniform:
                    if a.control:
                        tots_h = {v: ({k: x * 10 for k, x in t.items()} if v == best else t) for v, t in tots_h.items()}
                    pool = {v: t for v, t in got.items() if v not in mo}
                    scores = C.pick_body({v: compose._scalar(t) for v, t in pool.items()}, None)[1]
                    scores_h = C.pick_body({v: compose._scalar(t) for v, t in tots_h.items()}, None)[1]
                    ok = C.decide(q, scores, scores_h, "")[0] == best
                    decided += 1
            cases += 1
            if not ok:
                bad += 1
                if bad <= 5:
                    print(f"  DIFFER {fam} {q} {name}: query_pick {best!r}; query_candidates {sorted(got)}")
    print(f"{cases} cases ({decided} decided on held-out vectors), {bad} differ")
    if cases == 0 or (decided == 0 and not a.control):
        print("FAIL: no case compared" if cases == 0 else "FAIL: no non-uniform option was decided on held-out vectors")
        return 1
    if a.control:
        print("control caught: PASS" if bad else "control caught: FAIL (an inflated held-out vector changed no pick)")
        return 0 if bad else 1
    print("PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
