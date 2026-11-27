#!/usr/bin/env python3
"""The mixed-layout search returns one assignment among tied optima, whatever its start (search.solve's canonical
tie-break): among assignments whose (primary, other) totals are equal within the solver's 1e-9 tolerance, the
lexicographically smallest in search order.

Small synthetic problems (no data, no store) with planted ties that no per-part dominance removes (two queries over
two parts each, where (Split, Condensed) and (Condensed, Split) tie at the minimum and neither option is cheaper in
every context):

  1. fewest ops, fewest bytes and fewest ops within a bytes cap, each from six starts (no start, each of the four
     tied optima, an all-Chunked one): one assignment, the smallest tied one, equal to brute force's
     (min totals, then the smallest assignment);
  2. a tie within the tolerance (a relative 1e-12 apart) counts as a tie;
  3. a start that uses an option the dominance cut removes loses the tie;
  4. a node limit: proven False when the limit stops the search, True when it does not.

    python3 tests/unit/check_solve_tiebreak.py
"""
import itertools
import sys

import numpy as np

from kvcost.layout_search import search as S
from kvevaluator.layouts import SETTINGS

HI = 50.0


def build(nparts, queries, tables):
    """a Problem built by hand: `queries` lists each query's parts, `tables` each query's (ops, bytes) arrays, one axis
    per part, three options each (as check_solve_dominance.py builds them)"""
    pb = S.Problem.__new__(S.Problem)
    pb.parts = [f"p{i}" for i in range(nparts)]
    pb.order = list(pb.parts)
    pb.options = [list(SETTINGS) for _ in pb.parts]
    pb.size = [3] * nparts
    pb.extra = []
    pb.queries = []
    for qparts, (ops, byt) in zip(queries, tables):
        shape = (3,) * len(qparts)
        where = [pb.parts.index(p) for p in qparts]
        pb.queries.append({"parts": where, "often": 1.0,
                           "one_run": {"ops": S.prefix_mins(np.asarray(ops, float).reshape(-1), shape),
                                       "bytes": S.prefix_mins(np.asarray(byt, float).reshape(-1), shape)}})
    pb.touch = [[(qi, q["parts"].index(p)) for qi, q in enumerate(pb.queries) if p in q["parts"]]
                for p in range(nparts)]
    pb.reweight([1.0] * len(pb.queries))
    return pb


def tied_pair(eps=0.0, dominated_third=False):
    """one query's (ops, bytes) over two parts: (0, 1) and (1, 0) cost 10 (the second times 1 + eps), every other
    entry more; option 0 is cheaper than option 1 in some context and dearer in another, so neither dominates.
    dominated_third: option 2 costs more than option 0 in every context (the cut removes it)"""
    ops = np.full((3, 3), HI)
    ops[0, 1], ops[1, 0] = 10.0, 10.0 * (1 + eps)
    ops[2, :] = ops[:, 2] = 30.0
    ops[2, 2] = 20.0
    if dominated_third:
        ops[2, :] = ops[0, :] + 1.0
        ops[:, 2] = ops[:, 0] + 1.0
        ops[2, 2] = ops[0, 0] + 2.0
    return ops, ops.copy()


def problem(eps=0.0, dominated_third=False):
    """p0..p3; q0 over (p0, p1) and q1 over (p2, p3), each a tied pair; q2 over (p1, p2) costs the same everywhere"""
    queries = [["p0", "p1"], ["p2", "p3"], ["p1", "p2"]]
    flat = np.ones((3, 3))
    return build(4, queries, [tied_pair(eps, dominated_third), tied_pair(eps, dominated_third), (flat, flat)])


def brute(pb, primary, cap=None):
    """the minimum totals over every assignment, then the smallest assignment among those within the tolerance"""
    other = "bytes" if primary == "ops" else "ops"
    rows = []
    for combo in itertools.product(*(range(k) for k in pb.size)):
        o, b = pb.total(list(combo))
        if cap is None or b <= cap * (1 + 1e-9):
            rows.append(({"ops": o, "bytes": b}[primary], {"ops": o, "bytes": b}[other], list(combo)))
    p0 = min(r[0] for r in rows)
    near = [r for r in rows if abs(r[0] - p0) <= 1e-9 * max(1.0, p0)]
    o0 = min(r[1] for r in near)
    return min(r[2] for r in near if abs(r[1] - o0) <= 1e-9 * max(1.0, o0))


def main():
    failed = 0
    tied = [[0, 1, 0, 1], [0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 1, 0]]
    starts = [None] + tied + [[2, 2, 2, 2]]
    cases = 0
    for eps in (0.0, 1e-12):
        for primary, cap in (("ops", None), ("bytes", None), ("ops", 45.0)):
            pb = problem(eps)
            want = brute(pb, primary, cap)
            got = set()
            for st in starts:
                if st is not None and cap is not None and pb.total(st)[1] > cap:
                    st = None                     # a start over the cap is no incumbent (solve ignores it the same way)
                assign, *_ = S.solve(pb, primary, cap, st)
                got.add(tuple(pb.from_assign(assign)))
                cases += 1
            ok = got == {tuple(want)}
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'} 1/2 {primary}{'' if cap is None else f' within {cap:g}'}, eps {eps:g}: "
                  f"{len(starts)} starts -> {sorted(got)}; brute force {want}")
    # 3: a start using a dominated option (option 2 at p1, p3 ties nothing here, so plant one: a start equal in totals)
    pb = problem(dominated_third=True)
    info = {}
    assign, *_ = S.solve(pb, "ops", None, [0, 1, 0, 1], info=info)
    base = tuple(pb.from_assign(assign))
    ok3 = info.get("dominated", 0) > 0 and base == tuple(brute(pb, "ops"))
    for st in ([1, 0, 1, 0], [0, 1, 1, 0]):
        a2, *_ = S.solve(pb, "ops", None, st)
        ok3 &= tuple(pb.from_assign(a2)) == base
        cases += 1
    failed += not ok3
    print(f"{'PASS' if ok3 else 'FAIL'} 3 with a dominated option cut ({info.get('dominated')} options): every start -> {base}")
    # 4: the node limit
    pb = problem()
    i1, i2 = {}, {}
    S.solve(pb, "ops", None, None, max_nodes=2, info=i1)
    S.solve(pb, "ops", None, None, max_nodes=10**6, info=i2)
    ok4 = i1.get("proven") is False and i2.get("proven") is True
    failed += not ok4
    print(f"{'PASS' if ok4 else 'FAIL'} 4 max_nodes: proven {i1.get('proven')} at 2 nodes, {i2.get('proven')} at 10^6")
    print(f"check_solve_tiebreak: {cases} solves, {failed} checks failed")
    return 1 if failed or cases == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
