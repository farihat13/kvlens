#!/usr/bin/env python3
"""The mix search solves each way of adding up queries (kvcost.layout_cost.query_weighting: sum, geomean, additive)
exactly, through per-query tables and weights (table_values, item_weights, score).

Small synthetic problems (no data, no store; seeded): 4 parts of three options, 5 queries over 1-3 parts each, costs
from 0.01 to 10^6, so a plain log would go negative. Per form, the search's optimum (search.solve on the transformed
tables) must equal brute force over every assignment of query_weighting.aggregate on the raw costs, and score() of the
search's total must equal aggregate() of the assignment it returns. Every geomean table entry must be >= 0 (the search's
move and tie tests are relative). additive's reference is G, the uniform assignment with the lowest geomean
(additive_reference). Also: a cost <= 0 refuses, additive without reference costs refuses.

    python3 tests/unit/check_query_weighting_tables.py [--plant weights|noshift]   (a plant must exit 1)
"""
import argparse
import itertools
import math
import random
import sys

import numpy as np

from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_search import search as S
from kvevaluator.layouts import SETTINGS

NPARTS, NQUERIES, SEEDS = 4, 5, 40
TOL = 1e-9


def instance(seed):
    """[(the query's parts, its raw cost array, one axis per part), ...] and each query's `often`"""
    rng = random.Random(seed)
    qs = []
    for _ in range(NQUERIES):
        parts = sorted(rng.sample(range(NPARTS), rng.randint(1, 3)))
        costs = np.array([10 ** rng.uniform(-2, 6) for _ in range(3 ** len(parts))]).reshape((3,) * len(parts))
        qs.append((parts, costs))
    return qs, [rng.choice((1.0, 2.0, 7.0, 0.5)) for _ in qs]


def build(qs, form, often, plant):
    """a Problem by hand (as check_solve_tiebreak.py builds one) on the form's tables and weights; and the offsets"""
    pb = S.Problem.__new__(S.Problem)
    pb.parts = [f"p{i}" for i in range(NPARTS)]
    pb.order = list(pb.parts)
    pb.options = [list(SETTINGS) for _ in pb.parts]
    pb.size = [3] * NPARTS
    pb.extra = []
    pb.queries, offsets = [], []
    for parts, costs in qs:
        vals, off = QW.table_values(form, costs.reshape(-1).tolist())
        if plant == "noshift" and form == "geomean":
            vals, off = [math.log(v) for v in costs.reshape(-1)], 0.0
        offsets.append(off)
        shape = costs.shape
        tab = S.prefix_mins(np.asarray(vals, float), shape)
        pb.queries.append({"parts": parts, "often": 1.0, "one_run": {"ops": tab, "bytes": tab}})
    pb.touch = [[(qi, q["parts"].index(p)) for qi, q in enumerate(pb.queries) if p in q["parts"]] for p in range(NPARTS)]
    ref = [float(costs[(g,) * costs.ndim]) for _, costs in qs for g in [reference(qs)]]
    weights = QW.item_weights(form, often, ref)
    if plant == "weights" and form == "geomean":
        weights = list(often)                     # the frequencies on the log table: not the geomean
    pb.reweight(weights)
    return pb, offsets, ref


def reference(qs):
    """additive's reference: the uniform assignment (one option on every part) with the lowest geomean"""
    per_query = {f"q{i}": {o: float(costs[(o,) * costs.ndim]) for o in range(3)} for i, (_, costs) in enumerate(qs)}
    return QW.additive_reference(per_query)


def raw(qs, combo):
    return [float(costs[tuple(combo[p] for p in parts)]) for parts, costs in qs]


def aggregate(form, costs, often, ref):
    """aggregate() of one assignment's raw per-query costs (the one definition)"""
    per_query = {f"q{i}": {"L": c, "ref": r} for i, (c, r) in enumerate(zip(costs, ref))}
    weight = {f"q{i}": w for i, w in enumerate(often)} if form == "sum" else None
    return QW.aggregate(per_query, form, weight=weight, ref="ref")["L"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plant", choices=("weights", "noshift"), default=None)
    a = ap.parse_args()
    bad, checked, negative = [], 0, 0
    for seed in range(SEEDS):
        qs, often = instance(seed)
        for form in QW.FORMS:
            pb, offsets, ref = build(qs, form, often, a.plant)
            negative += sum(v < 0 for q in pb.queries for v in q["one_run"]["ops"][-1])
            assign, total, _, _, _ = S.solve(pb, "ops")
            combo = [pb.options[p].index(assign[pb.order[p]]) for p in range(NPARTS)]
            got = QW.score(form, total, offsets, n=len(qs))
            mine = aggregate(form, raw(qs, combo), often, ref)
            best = min(aggregate(form, raw(qs, c), often, ref) for c in itertools.product(range(3), repeat=NPARTS))
            checked += 1
            if abs(got - mine) > TOL * max(1.0, abs(mine)):
                bad.append((seed, form, "score != aggregate of the returned assignment", got, mine))
            if mine > best * (1 + TOL):
                bad.append((seed, form, "not the optimum", mine, best))
    for form, vals in (("geomean", [1.0, 0.0]), ("additive", None)):
        try:
            QW.table_values(form, vals) if vals else QW.item_weights(form, [1.0])
            bad.append((None, form, "did not refuse", None, None))
        except ValueError:
            pass
    if negative:
        bad.append((None, "geomean", f"{negative} table entries < 0", None, None))
    for b in bad[:10]:
        print("FAIL", *b)
    print(f"check_query_weighting_tables {'FAIL' if bad else 'OK'}: {checked} problems (forms {', '.join(QW.FORMS)}, "
          f"{SEEDS} seeds), each against brute force over {3 ** NPARTS} assignments; {len(bad)} failures"
          + (f" (plant {a.plant})" if a.plant else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
