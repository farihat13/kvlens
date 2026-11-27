#!/usr/bin/env python3
"""The mix search without the cost cache (python/kvcost/layout_search/bodies.py) finds the exact optimum: on planted problems
(synthetic queries with bodies, validity per cell and part rows per cell, priced by the real rule, compose.query_pick),
its answer equals a brute force over every assignment, for the fewest wops, the fewest wops within a byte cap, and the
geomean and additive forms; ties resolve to the brute force's canonical (smallest) assignment.

  1. random problems: 3 to 6 parts, 2 to 5 queries, 1 to 4 bodies, 1 to 3 params, validity on random cells, small
     integer costs (many ties); a fourth setting, Chunked at 4 KB (a size option, its own rows); some unit rows
     missing (a plan that reads no row of that unit there: priced 0)
  2. a tight problem: two queries' cheapest settings of a shared part conflict, and one query's cheapest body is
     valid on one pair only, so the root bound is not reached: the search must branch (nodes > the parts + 1), and its
     answer still equals the brute force
  3. a node limit: a solve stopped early says proven false (one that ends within the limit, true) and its answer is
     never below the optimum
  4. the fast exact path (in-sample queries) gives the same floats as compose.query_pick on every cost of 1-3 (an
     assertion in Bodies(verify=True))
  5. holes: a cell with no row for one unit, where the Condensed cell has one, is that unit at 0 on both paths; a cell
     with no rows at all for a plan and param is refused by query_pick, and the fast path hands it over (before
     compose._composed priced that part 0 when the Condensed cell had rows)
  6. the start under a byte cap (Bodies.fit_start, when the two-step mix is over the cap): it fits the cap, it is
     never worse than the best uniform layout within it, it is None only when no uniform layout fits and the rule
     found nothing, and a solve started from it gives the brute force's answer

    python3 tests/unit/check_no_cost_cache_solve.py [--cases 300] [--seed 1]
Prints each kind's count of cases; exit 1 on a difference, or a kind that ran 0 cases.
"""
import argparse
import itertools
import random
import sys

from kvcost.layout_cost import compose
from kvcost.layout_cost.picks import PlanRule
from kvcost.layout_search import bodies as BD

FAM = "planted"
SIZE = "chunked_c4k"           # Chunked at another chunk size: a fourth setting with rows of its own
COMPOSER = compose.Composer(criterion=None, plan_rule=PlanRule(in_sample_default=(FAM,)),
                        sizes=((SIZE, "/planted", "props_chunked_c4k_adj_chunked_c4k"),))


class Data(dict):
    """the slice of compose.load's data the form without the cost cache reads: queries by (family, query), the pricing, no grouped
    options, no other chunk sizes"""
    def __init__(self, queries, sized=()):
        super().__init__(queries)
        self.composer, self.options, self.sized = COMPOSER, {}, sized


def row(G, S=0, N=0, get_value=0):
    """a read part's cost vector, as compose.cost_vec makes it from a step row"""
    return compose.cost_vec({"G": G, "S": S, "N": N, "get_value": get_value}, False, COMPOSER)


def vec(rng, lo, hi):
    return row(rng.randint(lo, hi), rng.randint(0, 2), rng.randint(0, 3), 4096 * rng.randint(0, 4))


def query(rng, name, parts, n_bodies, valid=None, cost=None, n_params=1, holes=0.0):
    """one planted query: per body, the cells it is valid on, and per cell (and the size option) and param its part
    rows; `holes`: the chance a cell has no row for a unit (the size option keeps every row: _composed refuses a size
    without a row where Chunked/Chunked has one)"""
    d = {"fam": FAM, "q": name, "composer": COMPOSER, "sized_missing": {}, "valid": {c: set() for c in compose.CELLS}, "cost": {}}
    for b in range(n_bodies):
        v = f"b{b}"
        cells = valid(b) if valid else [c for c in compose.CELLS if rng.random() < 0.7]
        for c in cells:
            d["valid"][c].add(v)
        for c in compose.CELLS + ([SIZE] if cost is None else []):
            for i in range(1, n_params + 1):
                rows = {u: (cost(b, c, u) if cost else vec(rng, 1, 6)) for u in parts
                        if c == SIZE or rng.random() >= holes}
                if c != SIZE:
                    rows[None] = cost(b, c, None) if cost else vec(rng, 0, 2)
                d["cost"][(c, v, str(i))] = rows
    for (c, v, i), rows in list(d["cost"].items()):   # the 64 KB size's rows: its Chunked/Chunked's, as compose.load reads them
        if c == compose.CH_CH:
            d["cost"][(compose.GRID_CHUNKED, v, i)] = {u: r for u, r in rows.items() if u is not None}
    return d


def workload(qs):
    return {"id": "planted", "items": [(FAM, q, 1 + i % 3, None) for i, q in enumerate(sorted(qs))]}


def random_problem(rng):
    n = rng.randint(3, 6)
    parts = [("props", f"L{i}") if rng.random() < 0.5 else ("adj", f"E{i}") for i in range(n)]
    qs = {}
    for i in range(rng.randint(2, 5)):
        qs[(FAM, f"q{i}")] = query(rng, f"q{i}", sorted(rng.sample(parts, rng.randint(1, n))), rng.randint(1, 4),
                                   n_params=rng.randint(1, 3), holes=0.1)
    return Data(qs, sized=(SIZE,) if rng.random() < 0.5 else ()), parts


def tight_problem():
    """q0 reads parts A (props) and X (adj), q1 reads A and Y (adj). q0 is cheapest with A Split, q1 with A Chunked, and
    q0's cheapest body (0 per part) is valid on Split-Chunked only, so the bound at the root (each query at its own
    cheapest) is below every mix's cost: only branching on A, then X, shows the optimum"""
    rng = random.Random(7)
    A, X, Y = ("props", "A"), ("adj", "X"), ("adj", "Y")
    one = {"split": 1, "condensed": 5, "chunked": 9}
    def side(c, u):
        p, a = c[len("props_"):].split("_adj_")
        return p if u[0] == "props" else a
    q0 = query(rng, "q0", [A, X], 2, valid=lambda b: compose.CELLS if b == 0 else ["props_split_adj_chunked"],
               cost=lambda b, c, u: row(0) if u is None or b == 1 else row(one[side(c, u)]))
    q1 = query(rng, "q1", [A, Y], 1, valid=lambda b: compose.CELLS,
               cost=lambda b, c, u: row(0) if u is None else row(10 - one[side(c, u)] if u == A else one[side(c, u)]))
    return Data({(FAM, "q0"): q0, (FAM, "q1"): q1}), [A, X, Y]


def holes():
    """(kind, ok, what) of the two holes: one props unit A at Split, two plans, every cell holding every row except"""
    A = ("props", "A")
    out = []

    def problem():
        d = {"fam": FAM, "q": "q0", "composer": COMPOSER, "sized_missing": {}, "valid": {c: {"b0", "b1"} for c in compose.CELLS},
             "cost": {(c, v, "1"): {A: row(5), None: row(1)} for c in compose.CELLS for v in ("b0", "b1")}}
        return d, Data({(FAM, "q0"): d})
    w = {"id": "holes", "items": [(FAM, "q0", 1, None)]}
    # a unit row missing in props_split_adj_chunked (Condensed has it): A is 0 there on both paths, so b0 is the pick
    d, data = problem()
    d["cost"][("props_split_adj_chunked", "b0", "1")] = {None: row(1)}
    bd = BD.Bodies(w, data, [A], verify=True)
    combo = bd.from_assign({A: "split"})
    try:
        got = bd.pick(0, combo)                   # verify: the fast path and query_pick, asserted equal
        out.append(("holes", got[0] == "b0" and got[1]["b0"][0] == 1.0, f"a missing unit row: {got}"))
    except (AssertionError, SystemExit) as e:
        out.append(("holes", False, f"a missing unit row: {e}"))
    # no rows at all for b0 at param 1 in props_split_adj_chunked (Condensed has them): refused, never priced 0
    d, data = problem()
    del d["cost"][("props_split_adj_chunked", "b0", "1")]
    bd = BD.Bodies(w, data, [A])
    q = bd.queries[0]
    try:
        compose.query_pick(d, ["1"], {A: "split"}, {A})
        refused = False
    except SystemExit:
        refused = True
    fast = bd._fast(q, {A: "split"})
    out.append(("holes", refused and fast is None, f"a plan with no rows in its cell: query_pick refused {refused}, "
                                                    f"the fast path {fast}"))
    return out


def same(got, want):
    """the solve's (assign, primary, other) against the brute force's (combo, (primary, other)) on one Bodies"""
    assign, o, b = got
    if want is None:
        return assign is None
    tol = 1e-9 * max(1.0, abs(want[1][0]))
    return assign is not None and abs(o - want[1][0]) <= tol and abs(b - want[1][1]) <= 1e-9 * max(1.0, abs(want[1][1]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    ran, bad, fast = {}, [], {"fast": 0, "query_pick": 0}

    def check(kind, ok, what):
        ran[kind] = ran.get(kind, 0) + 1
        if not ok:
            bad.append(f"{kind}: {what}")

    for case in range(a.cases):
        data, parts = random_problem(rng)
        w = workload([q for _, q in data])
        units = compose.workload_units(w, data)
        flat = BD.Bodies(w, data, units, model="wops", verify=True)
        want = BD.brute(flat)
        if want is None:
            continue
        for start in (None, flat.two_step()[0]):
            got = flat.solve(start=start)
            check("fewest wops", same(got[:3], want) and flat.from_assign(got[0]) == want[0],
                  f"case {case} start {start is not None}: {got[1:3]} {got[0]} vs {want}")
        mid = sorted(flat.total(list(c))[1] for c in itertools.product(*(range(k) for k in flat.size)))
        cap = mid[len(mid) // 3]
        wc = BD.brute(flat, cap)
        got = flat.solve(cap=cap)
        check("within a cap", same(got[:3], wc) and (wc is None or flat.from_assign(got[0]) == wc[0]),
              f"case {case} cap {cap}: {got[1:3]} vs {wc}")
        for form in ("geomean", "additive"):
            try:
                ref = BD.additive_reference(w, data, units)[1] if form == "additive" else None
                bd = BD.Bodies(w, data, units, model="wops", form=form, ref_costs=ref, verify=True)
            except ValueError:                     # a uniform layout with no usable body or a cost 0: no reference
                continue
            try:
                wf = BD.brute(bd)
                got = bd.solve()
            except SystemExit:                     # a query at cost 0 (its rows missing): no log, refused as the cost cache do
                continue
            check(form, same(got[:3], wf), f"case {case}: {got[1:3]} vs {wf}")
        for k in flat.calls:
            fast[k] += flat.calls[k]
        for cap in (mid[0], mid[len(mid) // 10], cap):        # the fewest bytes, tight, a third in
            two = flat.two_step()[0]
            if flat.total(two)[1] <= cap * (1 + 1e-9):
                continue                                         # fit_start runs only over the cap
            start, used, steps = flat.fit_start(cap, two)
            wc = BD.brute(flat, cap)
            fit = lambda c: flat.total(c)[0] < BD.INF and flat.total(c)[1] <= cap * (1 + 1e-9)
            unis = [flat.from_assign(BD.uniform(flat.order, p, q)) for p in compose.LAYOUTS for q in compose.LAYOUTS]
            unis = [c for c in unis if fit(c)]
            if start is None:
                ok = not unis and used is None
            else:
                got = flat.solve(cap=cap, start=start)
                ok = (fit(start) and (not unis or flat.total(start)[0] <= min(flat.total(c)[0] for c in unis))
                      and wc is not None and flat.total(start)[0] >= wc[1][0] - 1e-9
                      and same(got[:3], wc) and flat.from_assign(got[0]) == wc[0])
            check("fit start", ok, f"case {case} cap {cap}: {used} {steps} start {start} vs {wc}")
        info = {}
        got = flat.solve(max_nodes=2, info=info)
        check("node limit", (not info["proven"] or got[3] <= 2) and (got[0] is None or got[1] >= want[1][0] - 1e-9),
              f"case {case}: proven {info['proven']}, {got[1]} vs {want[1][0]}")

    data, parts = tight_problem()
    w = workload([q for _, q in data])
    bd = BD.Bodies(w, data, compose.workload_units(w, data), model="wops", verify=True)
    want, info = BD.brute(bd), {}
    got = bd.solve(info=info)
    print(f"  tight: optimum {want[1][0]:g} at {bd.to_parts(want[0])}, root bound {info['ceiling']:g}, {got[3]} nodes")
    check("tight", same(got[:3], want) and info["ceiling"] < want[1][0] and got[3] > len(parts) + 1,
          f"{got[1:4]} vs {want}, bound {info['ceiling']}")

    for kind, ok, what in holes():
        check(kind, ok, what)
    # every exact cost went through both paths (verify): the fast path equals query_pick wherever it priced
    print(f"  fast exact path: {fast['fast']} costs, each equal to query_pick's ({fast['query_pick']} calls)")
    check("fast path", fast["fast"] > 0, "the fast path priced nothing")
    for line in bad[:20]:
        print("  DIFFER", line)
    kinds = ("fewest wops", "within a cap", "geomean", "additive", "node limit", "tight", "fast path", "holes", "fit start")
    for k in kinds:
        print(f"  {k}: {ran.get(k, 0)} cases, {sum(b.startswith(k + ':') for b in bad)} differ")
    ok = not bad and all(ran.get(k, 0) > 0 for k in kinds)
    print(f"check_no_cost_cache_solve {'OK' if ok else 'FAIL'}: {sum(ran.values())} cases, {len(bad)} differ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
