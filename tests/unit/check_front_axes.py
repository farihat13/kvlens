#!/usr/bin/env python3
"""A front over another pair of currencies (kvcost.layout_search: Problem.pair, questions.Axes, front.complete_front)
walks the right front and checks its points on the right pair.

Small synthetic problems (no data, no store): each query's costs in the cost cache hold ops, bytes and two extra lanes, rd and wr (a
bill form's read and write units). The cost a walk checks each point against (front.cost_by_part) is replaced by the
same totals summed by brute force from the raw tables.

  1. pair(name, "bytes") is under(name): every query's one-run and weighted tables equal;
  2. the walk on the axes (rd, wr) gives the brute-force front of rd against wr over every assignment, point for point;
  3. questions.front names the cheapest point at each write price r, equal to the brute-force least rd + r x wr;
  4. a planted swap of the two lanes in that cost (rd read as wr) is caught by the walk's check on every problem;
  5. the default axes (wops against bytes) still walk the ops/bytes front of the same problem;
  6. a read/write front of a workload that writes nothing (B = 1 has 0 write units) is refused up front.

And the shipped wiring, on random cost vectors: each bill form's axes (FRONT_AXES) read its read and write lanes, so
bill_at of the two equals the form's own bill (cost_models.bill_lower / upper / exact); reference_bytes reads the y
lane of B = 1 under a bill pair and currencies' bytes without one; the real front.cost_by_part returns `measure` of the
totals, not the currencies.

    python3 tests/unit/check_front_axes.py
"""
import itertools
import sys
import types

import numpy as np

from kvcost.layout_cost import cost_models as CM
from kvcost.layout_search import front as F
from kvcost.layout_search import questions as Q
from kvcost.layout_search import search as S
from kvevaluator.layouts import SETTINGS

PARTS = [("props", "T0"), ("props", "T1"), ("adj", "e0"), ("adj", "e1"), ("adj", "e2")]
QUERIES = [[0, 1, 2], [1, 3], [2, 4], [0, 4], [3]]
LANES = ("rd", "wr")
AXES = Q.Axes("test:read,write", "read_units", "write_units", LANES)
TABLES = ("ops", "bytes") + LANES


def build(rng):
    """(a Problem built by hand with tables ops, bytes, rd, wr per query, each query's raw tables, its weights)"""
    pb = S.Problem.__new__(S.Problem)
    pb.parts, pb.order = list(PARTS), list(PARTS)
    pb.options = [list(SETTINGS) for _ in PARTS]
    pb.size = [3] * len(PARTS)
    pb.extra = sorted(LANES)
    pb.queries, raw = [], []
    for qparts in QUERIES:
        shape = (3,) * len(qparts)
        tabs = {t: np.round(rng.uniform(1, 100, size=shape), 3).reshape(-1) for t in TABLES}
        raw.append(tabs)
        pb.queries.append({"parts": list(qparts), "one_run": {t: S.prefix_mins(tabs[t], shape) for t in TABLES}})
    often = [float(rng.integers(1, 5)) for _ in QUERIES]
    for q, wt in zip(pb.queries, often):
        q["often"] = wt
    pb.touch = [[(qi, q["parts"].index(p)) for qi, q in enumerate(pb.queries) if p in q["parts"]] for p in range(len(PARTS))]
    pb.reweight(often)
    return pb, raw, often


def totals(raw, often, combo):
    """{table: the weighted total} of one assignment (search order), by brute force from the raw tables"""
    out = {t: 0.0 for t in TABLES}
    for qparts, tabs, wt in zip(QUERIES, raw, often):
        idx = 0
        for p in qparts:
            idx = idx * 3 + combo[p]
        for t in TABLES:
            out[t] += wt * tabs[t][idx]
    return out


def brute_front(raw, often, x, y):
    """the non-dominated (x, y) totals over every assignment, fewest y first"""
    pts = {(round(v[x], 6), round(v[y], 6)) for c in itertools.product(range(3), repeat=len(PARTS))
           for v in [totals(raw, often, c)]}
    front = [p for p in pts if not any(q != p and q[0] <= p[0] and q[1] <= p[1] for q in pts)]
    return sorted(front, key=lambda p: (p[1], p[0]))


def cost_stand_in(pb, raw, often, swap=False):
    """front.cost_by_part's stand-in: the brute-force totals of a mix as a cost vector (ops, bytes, rd, wr), read
    through `measure`; swap: rd and wr exchanged (the planted defect)"""
    def cost_by_part(data, w, assign, measure=None):
        t = totals(raw, often, pb.from_assign(assign))
        vec = {"read_ops": t["ops"], "write_rmw_ops": 0.0, "write_ops": 0.0, "read_bytes": t["bytes"],
               "write_rmw_bytes": 0.0, "write_bytes": 0.0, "rd": t["wr" if swap else "rd"], "wr": t["rd" if swap else "wr"]}
        m = measure or CM.currencies
        return m(vec), {}
    return cost_by_part


def walk(pb, raw, often, axes, swap=False):
    F.cost_by_part = cost_stand_in(pb, raw, often, swap)
    mixes, _ = F.complete_front(axes.view(pb) if axes else pb, None, None, [], plain=False,
                                axes=axes, progress_every=10 ** 9)
    nx, ny = axes.names if axes else ("ops", "bytes")
    return [(round(m[nx], 6), round(m[ny], 6)) for m in mixes]


class _Data(dict):
    """loaded data as cost_by_part reads it: {(family, query): its data}, and a composer"""
    composer = None


def wiring(rng):
    """the shipped axes, reference_bytes and cost_by_part on random vectors; returns the cases checked"""
    cases = 0
    keys = sorted({k for lanes in CM.BILL_UNITS.values() for k in lanes} | {"read_ops", "write_rmw_ops",
                   "write_ops", "read_bytes", "write_rmw_bytes", "write_bytes"})
    for _ in range(20):
        t = {k: float(rng.uniform(1, 1e6)) for k in keys}
        for model in CM.BILL_UNITS:
            ax = Q.FRONT_AXES[model]
            m = ax.measure(t)
            if abs(CM.bill_at(m[ax.x], m[ax.y]) - getattr(CM, model)(t)) > 1e-9 * getattr(CM, model)(t):
                sys.exit(f"{model}: bill_at of its axes {m} is not its bill {getattr(CM, model)(t)}")
            if not set(ax.lanes) <= set(Q.extra_for(model)):
                sys.exit(f"{model}: its extra lanes {sorted(Q.extra_for(model))} lack its front's {ax.lanes}")
            cases += 1
        saved = Q.cost_mix, Q.S.b_unit_layout                 # reference_bytes: B = 1's vector is t
        Q.cost_mix, Q.S.b_unit_layout = (lambda w, data, assign: t), (lambda units: {})
        try:
            for model in CM.BILL_UNITS:
                if Q.reference_bytes(None, None, PARTS, Q.FRONT_AXES[model]) != t[CM.BILL_UNITS[model][1]]:
                    sys.exit(f"{model}: reference_bytes is not B = 1's write units")
            if Q.reference_bytes(None, None, PARTS) != CM.currencies(t)["bytes"]:
                sys.exit("reference_bytes without axes is not B = 1's bytes")
        finally:
            Q.cost_mix, Q.S.b_unit_layout = saved
        cases += 1
        # the real cost_by_part, over a stand-in for compose: one query, one part, whose cost vector is t
        unit = ("adj", "e0")
        stub = types.SimpleNamespace(zero=lambda composer: {k: 0.0 for k in keys}, params_of=lambda d, params: params,
                                     add=lambda tot, v, often: tot.update({k: tot[k] + often * v[k] for k in keys}),
                                     query_cost=lambda d, pp, assign, us, by_unit: (t, {unit: t}), currencies=CM.currencies)
        data = _Data({("fam", "q"): {"cost": {"x": {unit: 1}}}})
        w = {"items": [("fam", "q", 1.0, None)]}
        real = F.compose
        F.compose = stub
        try:
            ax = Q.FRONT_AXES["bill_exact"]
            tot, per = F.cost_by_part(data, w, {unit: "split"}, ax.measure)
            if tot != ax.measure(t) or per != {unit: ax.measure(t)}:
                sys.exit(f"cost_by_part with a measure returned {tot}, not {ax.measure(t)}")
            if F.cost_by_part(data, w, {unit: "split"})[0] != CM.currencies(t):
                sys.exit("cost_by_part without a measure is not the currencies")
        finally:
            F.compose = real
        cases += 1
    return cases


def main():
    original = F.cost_by_part
    problems = cases = 0
    try:
        for seed in range(12):
            rng = np.random.default_rng(seed)
            pb, raw, often = build(rng)
            problems += 1
            # 1. pair(name, "bytes") is under(name)
            a, b = pb.under("rd"), pb.pair("rd", "bytes")
            for qa, qb in zip(a.queries, b.queries):
                for c in ("ops", "bytes"):
                    if any(not np.array_equal(x, y) for x, y in zip(qa["one_run"][c], qb["one_run"][c])) or \
                            qa["mins"][c] != qb["mins"][c]:
                        sys.exit(f"seed {seed}: pair('rd', 'bytes') differs from under('rd') on {c}")
            cases += 1
            # 2. the walk on (rd, wr) is the brute-force front
            got, want = walk(pb, raw, often, AXES), brute_front(raw, often, "rd", "wr")
            if got != want:
                sys.exit(f"seed {seed}: the (rd, wr) walk has {len(got)} points, the brute force {len(want)}: {got} vs {want}")
            cases += 1
            # 3. the cheapest point at each write price
            row = Q.front(pb, None, None, PARTS, ref_bytes=1.0, axes=AXES)
            for r in Q.BILL_R:
                p = row["named"][f"cheapest at r={r:g}"]
                best = min(CM.bill_at(x, y, r) for x, y in want)
                if abs(CM.bill_at(p["read_units"], p["write_units"], r) - best) > 1e-6 * best:
                    sys.exit(f"seed {seed}: at r={r:g} the named point bills "
                             f"{CM.bill_at(p['read_units'], p['write_units'], r)}, the brute force {best}")
            if row["model"] != AXES.model or not row["proven"] or "reference_write_units" not in row:
                sys.exit(f"seed {seed}: the row's model {row['model']!r}, proven {row['proven']}, fields {sorted(row)}")
            cases += 1
            # 4. a planted swap of the lanes is caught
            try:
                walk(pb, raw, often, AXES, swap=True)
            except AssertionError:
                cases += 1
            else:
                sys.exit(f"seed {seed}: the walk read rd as wr and its check did not stop it")
            # 5. the default axes: the ops/bytes front
            if walk(pb, raw, often, None) != brute_front(raw, often, "ops", "bytes"):
                sys.exit(f"seed {seed}: the default walk is not the ops/bytes front")
            cases += 1
            # 6. no writes: refused up front
            try:
                Q.front(pb, None, None, PARTS, ref_bytes=0.0, axes=AXES)
            except SystemExit as e:
                if "writes nothing" not in str(e):
                    raise
                cases += 1
            else:
                sys.exit(f"seed {seed}: a read/write front with 0 write units at B = 1 was not refused")
    finally:
        F.cost_by_part = original
    wired = wiring(np.random.default_rng(99))
    print(f"check_front_axes OK: {problems} problems, {cases} cases (pair = under, the (rd, wr) front = brute force, "
          f"the cheapest point at {len(Q.BILL_R)} write prices, a planted lane swap caught, the default front, no writes "
          f"refused); the shipped wiring: {wired} cases (each bill form's axes give its bill, reference_bytes reads y, "
          f"cost_by_part returns the measure)")


if __name__ == "__main__":
    main()
