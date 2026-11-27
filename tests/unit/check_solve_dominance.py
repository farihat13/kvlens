#!/usr/bin/env python3
"""The mixed-layout search leaves out dominated options (kvcost.layout_search.search.undominated) and stays exact.

Small synthetic problems (no data, no store), each solved by search.solve and by brute force over every assignment:

  1. planted dominance: in some parts one option costs no more ops and no more bytes than another in every context of
     every query that reads the part; solve leaves those out (info["dominated"] > 0) and its totals equal the brute
     force optimum's, for fewest ops, fewest bytes and fewest ops within several bytes caps;
  2. no dominance (the options cross in every part): nothing is left out, the same totals;
  3. a start at a dominated option: kept as the first incumbent, the same totals;
  4. two equal options: the first stays;
  5. a part no query reads: one option is left, the first;
  6. random problems, nothing planted: the same totals (a rule that compares a prefix minimum instead of the full
     table cuts options it must keep, and fails here);
  7. an option with no plan in one context (INF there) never dominates, however cheap elsewhere;
  8. an allowed subset solved after a full solve on the same problem: the cut is per subset (a cache keyed without
     the subset would hand back options outside it);
  9. a copy of a problem with other tables (few_options_pareto's lambda views) gets its own cut and leaves the
     original's cache as it was.

    python3 tests/unit/check_solve_dominance.py
"""
import copy
import itertools
import sys

import numpy as np

from kvcost.layout_search import search as S
from kvevaluator.layouts import SETTINGS

QUERIES = [["p0", "p1", "p2"], ["p1", "p3"], ["p2", "p4", "p5"], ["p0", "p5"], ["p3"]]


def build(nparts, queries, tables):
    """a Problem built by hand: `queries` lists each query's parts, `tables` each query's (ops, bytes) arrays, one axis
    per part, three options each"""
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


def problem(nparts, queries, rng, plant=(), equal=(), crossing=False):
    """random costs, with option 2 dominated by option 0 in the parts of `plant`, options 0 and 1 equal in the parts of
    `equal`, and in `crossing` mode every part's options trading ops against bytes (none dominates)"""
    tables = []
    for qparts in queries:
        shape = (3,) * len(qparts)
        ops = rng.uniform(1, 100, size=shape)
        byt = rng.uniform(1, 100, size=shape)
        for ax, p in enumerate(qparts):
            if crossing:     # option k: ops falls, bytes rises with k, by more than the noise: no option dominates
                ramp = np.arange(3).reshape([3 if a == ax else 1 for a in range(len(qparts))]) * 200.0
                ops, byt = ops + (400.0 - ramp), byt + ramp
            if p in plant:
                o0, b0 = np.take(ops, 0, axis=ax), np.take(byt, 0, axis=ax)
                idx = [slice(None)] * len(qparts)
                idx[ax] = 2
                ops[tuple(idx)] = o0 + rng.uniform(0, 5, size=o0.shape)
                byt[tuple(idx)] = b0 + rng.uniform(0, 5, size=b0.shape)
            if p in equal:
                idx0, idx1 = [slice(None)] * len(qparts), [slice(None)] * len(qparts)
                idx0[ax], idx1[ax] = 0, 1
                ops[tuple(idx1)] = ops[tuple(idx0)]
                byt[tuple(idx1)] = byt[tuple(idx0)]
        tables.append((np.round(ops, 3), np.round(byt, 3)))
    return build(nparts, queries, tables)


def brute(pb, primary, cap=None, allowed=None):
    """the optimum's (primary, other) over every assignment (within allowed), ties on the other currency (solve's rule)"""
    best = None
    for combo in itertools.product(*(allowed or [range(k) for k in pb.size])):
        o, b = pb.total(list(combo))
        if cap is not None and b > cap * (1 + 1e-9):
            continue
        key = (o, b) if primary == "ops" else (b, o)
        if best is None or key < best:
            best = key
    return best


def same(a, b):
    return a is not None and b is not None and all(abs(x - y) <= 1e-9 * max(1.0, abs(y)) for x, y in zip(a, b))


def check_exact(pb, label, want_cut=None, allowed=None):
    """solve against brute force for every objective; want_cut True / False: options are / are not left out (None: no
    check); returns (cases, failed)"""
    lo, hi = brute(pb, "bytes", allowed=allowed)[0], brute(pb, "ops", allowed=allowed)[1]
    asks = [("ops", None), ("bytes", None)] + [("ops", lo + f * (hi - lo)) for f in (0.0, 0.2, 0.5, 0.8)]
    cases = bad = 0
    for primary, cap in asks:
        info = {}
        assign, o, b, _, _ = S.solve(pb, primary, cap, allowed=allowed, info=info)
        got = (o, b) if primary == "ops" else (b, o)
        want = brute(pb, primary, cap, allowed)
        cut = info.get("dominated", 0)   # absent on a search with no cut: then nothing was left out
        cut_ok = want_cut is None or (cut > 0 if want_cut else cut == 0)
        inside = assign is not None and (allowed is None or all(
            pb.options[t].index(assign[pb.order[t]]) in allowed[t] for t in range(len(pb.order))))
        ok = same(got, want) and cut_ok and inside
        cases += 1
        if not ok:
            bad += 1
            print(f"  FAIL {label} {primary} cap {cap}: solve {got} {assign} (left out {info.get('dominated')}), brute {want}")
    return cases, bad


def cache_view(pb):
    """what the problem's dominance cache holds, as text (the queries list it belongs to left out)"""
    cache = pb.__dict__.get("_undominated")
    return repr(sorted((k, v) for k, v in cache.items() if k != "queries")) if isinstance(cache, dict) else repr(cache)


def main():
    results = []                         # (case, cases, failed)

    def report(name, cases, failed, note=""):
        results.append((cases, failed))
        print(f"{'PASS' if failed == 0 else 'FAIL'} {name}: {cases} cases{note}")

    # 1. planted dominance
    c = f = 0
    for seed in range(5):
        dc, df = check_exact(problem(6, QUERIES, np.random.default_rng(seed), plant=("p1", "p3", "p5")),
                             f"planted seed {seed}", want_cut=True)
        c += dc; f += df
    report("1 planted dominance", c, f, ", solve equals brute force, dominated options left out")
    # 2. no dominance
    c = f = 0
    for seed in range(3):
        dc, df = check_exact(problem(6, QUERIES, np.random.default_rng(100 + seed), crossing=True),
                             f"crossing seed {seed}", want_cut=False)
        c += dc; f += df
    report("2 no dominance", c, f, ", nothing left out")
    # 3. a start at a dominated option
    pb = problem(6, QUERIES, np.random.default_rng(11), plant=("p1", "p3", "p5"))
    start = [2] * 6                                   # option 2 is dominated in p1, p3, p5
    lo, hi = brute(pb, "bytes")[0], brute(pb, "ops")[1]
    cap = max(pb.total(start)[1], lo + 0.5 * (hi - lo))
    _, o, b, _, _ = S.solve(pb, "ops", cap, start=start)
    report("3 a start at a dominated option", 1, 0 if same((o, b), brute(pb, "ops", cap)) else 1,
           f", the same totals ({o:.3f}, {b:.3f})")
    # 4. two equal options: the first stays
    pb = problem(6, QUERIES, np.random.default_rng(12), equal=("p2",))
    allowed = S.undominated(pb, [range(3)] * 6)
    ok4 = 0 in allowed[2] and 1 not in allowed[2]
    dc, df = check_exact(pb, "equal options", want_cut=True)
    report("4 two equal options", dc + 1, df + (0 if ok4 else 1), f", part p2 keeps {allowed[2]}")
    # 5. a part no query reads
    pb = problem(7, QUERIES, np.random.default_rng(13))
    allowed = S.undominated(pb, [range(3)] * 7)
    ok5 = allowed[6] == [0]
    dc, df = check_exact(pb, "an unread part", want_cut=True)
    report("5 a part no query reads", dc + 1, df + (0 if ok5 else 1), f", it keeps {allowed[6]}")
    # 6. random problems, nothing planted
    c = f = 0
    for seed in range(60):
        dc, df = check_exact(problem(6, QUERIES, np.random.default_rng(1000 + seed)), f"random seed {seed}")
        c += dc; f += df
    report("6 random problems", c, f, " (60 problems), solve equals brute force")
    # 7. INF: p0's option 2 has no plan when p1 = 0 and is the cheapest everywhere else; p1 = 0 is far the cheapest
    inf = float("inf")
    q01 = np.array([[5, 50, 50], [6, 50, 50], [inf, 1, 1]])     # axes (p0, p1)
    pb = build(2, [["p0", "p1"], ["p0"], ["p1"]],
               [(q01, q01), (np.array([10, 10, 1]),) * 2, (np.array([1, 100, 100]),) * 2])
    allowed = S.undominated(pb, [range(3)] * 2)
    ok7 = 0 in allowed[0]                             # option 2 must not cut option 0
    dc, df = check_exact(pb, "INF entry")
    report("7 an option with no plan in one context", dc + 1, df + (0 if ok7 else 1), f", p0 keeps {allowed[0]}")
    # 8. an allowed subset after a full solve: in p1, p3, p5 option 2 is dominated only by option 0, which the subset
    # leaves out, so option 2 must stay there
    pb = problem(6, QUERIES, np.random.default_rng(14), plant=("p1", "p3", "p5"))
    dc, df = check_exact(pb, "full, before a subset", want_cut=True)
    sub = [[0, 1, 2] if t in (0, 2, 4) else [1, 2] for t in range(6)]
    allowed = S.undominated(pb, sub)
    ok8 = all(set(a) <= set(s) for a, s in zip(allowed, sub))
    dc2, df2 = check_exact(pb, "allowed subset", allowed=sub)
    report("8 an allowed subset after a full solve", dc + dc2 + 1, df + df2 + (0 if ok8 else 1))
    # 9. a copy with other tables (a lambda view: ops + lambda x bytes in the place of ops, no reweight)
    pb = problem(6, QUERIES, np.random.default_rng(15), plant=("p1", "p3", "p5"))
    S.solve(pb, "ops")
    before = cache_view(pb)
    view = copy.copy(pb)
    view.queries = [{**q, "mins": {"ops": [[o + 1000.0 * b for o, b in zip(lo_, lb)]
                                           for lo_, lb in zip(q["mins"]["ops"], q["mins"]["bytes"])],
                                   "bytes": q["mins"]["bytes"]}} for q in pb.queries]
    dc, df = check_exact(view, "lambda view")
    ok9 = cache_view(pb) == before                    # a view that writes into the original's cache changes it
    report("9 a copy with other tables", dc + 1, df + (0 if ok9 else 1), ", the original's cache unchanged")
    total, failed = sum(c for c, _ in results), sum(f for _, f in results)
    print(f"check_solve_dominance: {total} cases, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
