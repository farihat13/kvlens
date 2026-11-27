#!/usr/bin/env python3
"""The exact mixed-layout search: branch and bound over a workload's parts, from the mix cost cache (each query's
one-run cost under every assignment of its parts).

A mix's cost is a sum over the workload's queries, and each query's cost depends only on the parts it touches
(compose.query_cost). So each query is priced once for every assignment of its own parts, and a partial assignment
is bounded below by the sum of each query's cheapest completion consistent with it. Objectives (each one exact
solve): fewest total ops, fewest total bytes, fewest total ops within a bytes cap. Ties: the lower other currency,
then the first assignment in the search order. Every reported total should be re-priced by compose.cost_mix.

The mix cost cache (callers import this module as `cost_cache`): for each query, its one-run cost under every
combination of settings of the parts it reads, built once so the search looks costs up. problem() and problems() are
the one builder, and set_cache's dir is where it is saved. problems() builds several queries' costs in one fork pool,
its tasks slices of one query's costs; the result equals Problem(w, data, units) field by field. The saved cache keeps
each query's costs in <dir>/<key>.npz; the key is every input they depend on (the query's data, plan files and run
rows, params, parts and options, pricing settings, COST_CACHE_VERSION and the source of the pricing modules, SOURCES),
never the git sha.
"""
import copy
import dataclasses
import glob
import hashlib
import itertools
import math
import os
import sys
import time

import numpy as np

from kvcost.layout_cost.compose import currencies, options_of, params_of, query_cost
from kvcost.layout_cost.compose import LAYOUTS
from kvcost.layout_cost.compose import CELLS, workload_units  # the joint solve
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_search.fill import vector_cost_cache
from kvcost.layout_cost.settings import B_UNIT, NAMED_BUDGETS, OLD_B_UNIT, settings as cell_settings
from kvevaluator.plan_choice import candidates as C
from kvevaluator.measure.run_measure import at_a_time
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Capture, stats_dir

INF = float("inf")


def query_parts(d):
    """the parts a query's cost rows touch (compose.cost_mix's rule), sorted"""
    return sorted({u for per in d["cost"].values() for u in per if u is not None})


def prefix_mins(tab, shape):
    """a query's costs (flat, mixed radix over its parts in search order) as [mins over parts j.. for j = 0 .. k]: the
    cheapest completion of each j-part prefix; plain lists (a Python search loop indexes them fast)"""
    full = tab.reshape(shape) if shape else tab.reshape(())
    return [(full.min(axis=tuple(range(j, len(shape)))).reshape(-1) if j < len(shape) else full.reshape(-1)).tolist()
            for j in range(len(shape) + 1)]


# a query's costs under this many entries are filled the scalar way: the vectorised fill's fixed cost per query is
# larger there (LinkBench's 32 queries of 3 entries: 24.5 s vectorised, 3.3 s scalar)
SMALL_COST_CACHE = 32


class Problem:
    """one workload x one option set: each query's costs over its parts (the mix cost cache), ordered for the search"""

    def __init__(self, w, data, parts, extra=None, order=None):
        """extra: {name: function of a query's cost vector} -- further one-run lanes beside "ops" and "bytes", priced
        at the same plan (bill_search.py: the bill's forms from one cost cache build; `under` solves one of them).
        order: the search order of `parts`, given (joint_search.py: one order shared by several workloads' problems)"""
        t0 = time.time()
        self.extra = sorted(extra or {})
        self.parts = list(parts)
        items = w["items"]
        qparts = [query_parts(data[(fam, q)]) for fam, q, _, _ in items]
        degree = {u: sum(u in ps for ps in qparts) for u in self.parts}
        # most-touched first; ties by the parts' own (sorted) order, so the order is deterministic
        self.order = list(order) if order is not None else sorted(self.parts, key=lambda u: (-degree[u], self.parts.index(u)))
        assert sorted(self.order) == sorted(self.parts), "order must hold exactly the parts"
        self.options = [options_of(data, u) for u in self.order]     # per part (search order): its layouts
        self.size = [len(o) for o in self.options]
        self.queries = []                      # per item: its parts (search order), often, its costs
        for item in items:
            ops, byt, more = self.fill_cost_cache(data, item, extra)
            self.queries.append(self.query_entry(data, item, ops, byt, more))
        # for each part (search position): the queries it enters, and at which of their positions
        self.touch = [[(qi, qp.index(p)) for qi, qd in enumerate(self.queries) for qp in [qd["parts"]] if p in qp]
                      for p in range(len(self.order))]
        self.reweight([q["often"] for q in self.queries])
        self.cost_cache_seconds = time.time() - t0
        self.cost_cache_entries = sum(len(q["one_run"]["ops"][-1]) for q in self.queries)

    def query_axes(self, data, item):
        """(the query's data, its params, its parts in search order, their positions): the axes of its costs"""
        fam, q, _, params = item
        d = data[(fam, q)]
        pos = {u: i for i, u in enumerate(self.order)}
        ps = sorted(query_parts(d), key=pos.get)
        return d, params_of(d, params), ps, [pos[u] for u in ps]

    def fill_cost_cache(self, data, item, extra=None, lo=0, hi=None):
        """one query's one-run costs under the assignments lo .. hi - 1 of its own parts (all of them by default):
        (ops, bytes, {extra name: values}), lists of hi - lo, INF where no plan is usable. A query's costs are indexed in mixed
        radix over its parts (search order), each part's digit = its option. All of them are filled at once
        by fill.vector_cost_cache where it can be (equal entry for entry: tests/gates/check_cost_cache_fill.py), else one
        query_cost per entry (scalar_fill); a slice (problems() fills a large query's costs in several processes) is scalar."""
        if lo == 0 and hi is None:
            got = self.vector_fill(data, item, extra)
            if got is not None:
                return got
        return self.scalar_fill(data, item, extra, lo, hi)

    def vector_fill(self, data, item, extra=None):
        """the query's whole costs by fill.vector_cost_cache, or None (that query takes the scalar path)"""
        if os.environ.get("KVEVAL_COST_CACHE_FILL") == "scalar":      # a debugging switch: every query's costs the scalar way
            return None
        d, pp, ps, where = self.query_axes(data, item)
        opts = [self.options[p] for p in where]
        if math.prod(len(o) for o in opts) < SMALL_COST_CACHE:
            return None
        return vector_cost_cache(d, pp, ps, opts, extra)

    def scalar_fill(self, data, item, extra=None, lo=0, hi=None):
        """fill_cost_cache's entries one query_cost at a time (the reference the vectorised fill is checked against)"""
        d, pp, ps, where = self.query_axes(data, item)
        shape = tuple(self.size[p] for p in where)
        n = int(np.prod(shape, dtype=np.int64))
        hi = n if hi is None else hi
        ops, byt = [INF] * (hi - lo), [INF] * (hi - lo)
        more = {name: [INF] * (hi - lo) for name in sorted(extra or {})}
        for idx, combo in enumerate(itertools.islice(itertools.product(*(range(k) for k in shape)), lo, hi)):
            c = query_cost(d, pp, {u: self.options[p][i] for u, p, i in zip(ps, where, combo)}, set(ps))
            if c is not None:
                t = currencies(c)
                ops[idx], byt[idx] = t["ops"], t["bytes"]      # one run; the weight (how often) comes after
                for name in more:
                    more[name][idx] = extra[name](c)
        return ops, byt, more

    def query_entry(self, data, item, ops, byt, more):
        """a query's entry of self.queries from its full costs (fill_cost_cache's lists, lo = 0 and hi = all)"""
        _, _, ps, where = self.query_axes(data, item)
        shape = tuple(self.size[p] for p in where)
        mins = {cur: prefix_mins(np.array(tab, dtype=float), shape)
                for cur, tab in (("ops", ops), ("bytes", byt), *more.items())}
        return {"parts": where, "one_run": {cur: mins[cur] for cur in ("ops", "bytes", *self.extra)}, "often": item[2]}

    def restrict(self, items, w, parts, order=None):
        """the Problem of workload w (its items among `items`, the ones this problem was built from: the same family,
        query and params) over `parts`, without pricing again: each query's full costs are this one's, its axes put in
        the new search order and its prefix mins taken again; weights = w's. Equal, field by field, to Problem(w, data,
        parts, extra, order) (order None: Problem's own rule)."""
        t0 = time.time()
        key = lambda it: (it[0], it[1], None if it[3] is None else tuple(it[3]))
        at = {key(it): i for i, it in enumerate(items)}
        missing = [key(it) for it in w["items"] if key(it) not in at]
        if missing or len(at) != len(self.queries) or not set(parts) <= set(self.parts):
            raise ValueError(f"restrict: items not in this problem {missing[:3]}, or parts outside it")
        sub = copy.copy(self)
        sub.parts = list(parts)
        qunits = [[self.order[p] for p in self.queries[at[key(it)]]["parts"]] for it in w["items"]]
        degree = {u: sum(u in us for us in qunits) for u in sub.parts}
        sub.order = list(order) if order is not None else sorted(sub.parts, key=lambda u: (-degree[u], sub.parts.index(u)))
        assert sorted(sub.order) == sorted(sub.parts), "order must hold exactly the parts"
        old = {u: p for p, u in enumerate(self.order)}
        new = {u: p for p, u in enumerate(sub.order)}
        sub.options = [self.options[old[u]] for u in sub.order]
        sub.size = [len(o) for o in sub.options]
        sub.queries = []
        for it, us in zip(w["items"], qunits):
            q = self.queries[at[key(it)]]
            shape = tuple(self.size[p] for p in q["parts"])
            ps = sorted(us, key=new.get)                       # the query's parts in the new search order
            axes = [us.index(u) for u in ps]
            one_run = {cur: prefix_mins(np.array(tabs[-1], dtype=float).reshape(shape).transpose(axes).reshape(-1),
                                        tuple(shape[i] for i in axes))
                       for cur, tabs in q["one_run"].items()}
            sub.queries.append({"parts": [new[u] for u in ps], "one_run": one_run, "often": it[2]})
        sub.touch = [[(qi, qp.index(p)) for qi, qd in enumerate(sub.queries) for qp in [qd["parts"]] if p in qp]
                     for p in range(len(sub.order))]
        sub.reweight([q["often"] for q in sub.queries])
        sub.cost_cache_seconds = time.time() - t0
        sub.cost_cache_entries = sum(len(q["one_run"]["ops"][-1]) for q in sub.queries)
        return sub

    def reweight(self, weights):
        """set each item's weight (how often it runs); a query's min arrays scale with it (min of w x c = w x min c,
        w > 0), so a sweep over write:read ratios re-solves without re-pricing. A lane named lane_* is not weighted: it is
        a lane of a linear re-weighting, read by under_linear at its full one-run level only (the write weights)"""
        assert all(wt > 0 for wt in weights), "weights must be > 0 (min of w x c = w x min c; 0 x INF is nan)"
        for q, wt in zip(self.queries, weights):
            q["mins"] = {c: [[wt * v for v in arr] for arr in q["one_run"][c]]
                         for c in q["one_run"] if not c.startswith("lane_")}
        self._undominated = None       # undominated() reads the weighted costs: new costs, a new cache

    def under(self, name):
        """the same problem with the extra lane `name` in the place of "ops": solve(pb.under(name), "ops") is the exact
        optimum of that measure (ties: fewer bytes)"""
        return self.pair(name, "bytes")

    def pair(self, x, y):
        """the same problem with lane x in the place of "ops" and lane y in the place of "bytes" (each "ops", "bytes"
        or an extra lane): every solve, bound and walk then reads the pair (a front over two other currencies:
        questions.Axes)"""
        pb = copy.copy(self)
        pb.queries = [{**q, "one_run": {"ops": q["one_run"][x], "bytes": q["one_run"][y]}} for q in self.queries]
        pb.reweight([q["often"] for q in pb.queries])
        return pb

    def weighted(self, form, ref_costs=None):
        """the same problem under a query weighting form (python/kvcost/layout_cost/query_weighting.py): each query's full
        query's costs, every lane, through table_values (geomean: log(c / m) with offset log m, m the query's smallest usable
        entry, so every entry stays >= 0 and no argmin moves), its prefix mins taken again, and each query's weight
        item_weights(form, ...) (additive: ref_costs, each query's cost on the reference layout G, on the solved lane).
        sum: this problem itself. The copy's .form and .offsets[lane] (per query) give query_weighting.score its units.
        The costs (and the saved cache) stay unweighted: the transform reads the built problem only."""
        if form == "sum":
            return self
        from kvcost.layout_cost import query_weighting as QW
        pb = copy.copy(self)
        queries, offsets = [], {}
        for q in self.queries:
            shape = tuple(self.size[p] for p in q["parts"])
            one_run = {}
            for c, tabs in q["one_run"].items():
                vals, off = QW.table_values(form, list(tabs[-1]), INF)
                one_run[c] = prefix_mins(np.array(vals, dtype=float), shape)
                offsets.setdefault(c, []).append(off)
            queries.append({**q, "one_run": one_run})
        weights = QW.item_weights(form, [q["often"] for q in self.queries], ref_costs)
        for q, wt in zip(queries, weights):
            q["often"] = wt                     # every later reweight (under, restrict) keeps the form's weights
        pb.queries, pb.offsets, pb.form = queries, offsets, form
        pb.reweight(weights)
        return pb

    def under_linear(self, name, terms):
        """the same problem with lane `name` + sum(k x lane t for t, k in terms) in the place of "ops" (a re-weighting of
        lanes kept in the cost cache, e.g. the write weights): built from the full costs, since prefix mins of a sum are not the
        sum of prefix mins. An entry with no usable plan stays INF (a negative k on INF would give nan)"""
        if getattr(self, "form", "sum") != "sum":
            raise ValueError("under_linear sums lanes: a weighted problem's costs are logs or ratios (weight after)")
        pb = copy.copy(self)
        queries = []
        for q in self.queries:
            shape = tuple(self.size[p] for p in q["parts"])
            full = np.array(q["one_run"][name][-1], dtype=float)
            ok = full < INF
            for t, k in terms.items():
                if k:
                    full[ok] += k * np.array(q["one_run"][t][-1], dtype=float)[ok]
            queries.append({**q, "one_run": {"ops": prefix_mins(full, shape), "bytes": q["one_run"]["bytes"]}})
        pb.queries = queries
        pb.reweight([q["often"] for q in pb.queries])
        return pb

    def total(self, combo_order):
        """(ops, bytes) of a full assignment given in search order (indexes into each part's options)"""
        o = b = 0.0
        for q in self.queries:
            idx = 0
            for p in q["parts"]:
                idx = idx * self.size[p] + combo_order[p]
            o += q["mins"]["ops"][-1][idx]
            b += q["mins"]["bytes"][-1][idx]
        return o, b

    def to_parts(self, combo_order):
        """search-order combo -> {part: layout name}"""
        return {u: self.options[i][combo_order[i]] for i, u in enumerate(self.order)}

    def from_assign(self, assign):
        return [self.options[i].index(assign[u]) for i, u in enumerate(self.order)]


def lagrange_bounds(pb, multipliers):
    """[(m, per query: its min arrays of ops + m x bytes)] for solve(lagrange=): under a bytes cap, every completion that
    fits has ops >= (ops + m x bytes) - m x cap, so the cheapest completion in ops + m x bytes bounds the ops from below
    with the cap inside the bound (the plain ops bound ignores the cap). Built from the problem's weighted costs."""
    out = []
    for m in multipliers:
        per_q = []
        for q in pb.queries:
            shape = tuple(pb.size[p] for p in q["parts"])
            full = np.array(q["mins"]["ops"][-1], dtype=float) + m * np.array(q["mins"]["bytes"][-1], dtype=float)
            full = full.reshape(shape) if shape else full.reshape(())
            per_q.append([(full.min(axis=tuple(range(j, len(shape)))).reshape(-1) if j < len(shape) else full.reshape(-1)).tolist()
                          for j in range(len(shape) + 1)])
        out.append((m, per_q))
    return out


def undominated(pb, choices):
    """per part (search order): the options of choices[t] that no other of them dominates. Option i dominates j at part
    t when, in every query that reads t and every assignment of that query's other parts, i costs no more ops and no
    more bytes than j (the weighted costs solve reads). A mix with j is then never better than the same mix with i, for
    any objective and under any bytes cap, so solve may leave j out. Of two equal options the first stays. Cached per
    problem until reweight; the cache belongs to the problem's own queries list, so a copy with other costs (under,
    few_options_pareto's lambda views) gets its own and never reads or writes the original's."""
    cache = pb.__dict__.get("_undominated")
    if cache is None or cache["queries"] is not pb.queries:
        cache = pb._undominated = {"queries": pb.queries, "by_choices": {}}
    key = tuple(tuple(c) for c in choices)
    if key in cache["by_choices"]:
        return cache["by_choices"][key]
    opts = [list(c) for c in choices]
    le = [{(i, j): True for i in o for j in o if i != j} for o in opts]   # per part: i costs no more than j, so far
    for q in pb.queries:               # one query's full costs at a time (memory: never every query's at once)
        shape = tuple(pb.size[p] for p in q["parts"])
        tabs = [np.asarray(q["mins"][c][-1], dtype=float).reshape(shape) for c in ("ops", "bytes")]
        for pos, t in enumerate(q["parts"]):
            for (i, j), ok in le[t].items():
                if ok:
                    le[t][(i, j)] = all(np.all(np.take(tab, i, axis=pos) <= np.take(tab, j, axis=pos)) for tab in tabs)
    allowed = []
    for t, o in enumerate(opts):       # j is left out when some i is cheaper, or equal and earlier (no cycle)
        out = {j for j in o for i in o if i != j and le[t][(i, j)] and (i < j or not le[t][(j, i)])}
        allowed.append([v for v in o if v not in out])
    cache["by_choices"][key] = allowed
    return allowed


class _OutOfNodes(Exception):
    pass


def solve(pb, primary, cap=None, start=None, plain=False, allowed=None, lagrange=None, max_nodes=None, info=None,
          trace=None):
    """the exact optimum of `primary` ('ops' | 'bytes'), optionally with total bytes <= cap. Returns
    (assign or None, ops, bytes, nodes, seconds); None when no assignment fits the cap. plain=True: only the grid's
    three settings per part (options_of lists them first), no other option -- the same costs, whose mins over all
    the options are still valid lower bounds (a min over a superset). allowed: per part (search order) the option
    indexes it may take (a subset of its options, e.g. the grid's Split and Condensed and Chunked at one size); the
    same costs and bounds again. lagrange (lagrange_bounds; fewest ops within a cap only): more lower bounds on the
    ops, with the cap inside them; they cut nodes, never the optimum (the same answer, fewer nodes). max_nodes: the
    search stops after that many nodes and returns the best assignment found, a mix that fits but is NOT proven the
    optimum; info (a dict) then gets "proven": False (True when the search finished). A node limit, never a wall clock,
    so an unproven row is the same on every machine. Without max_nodes nothing changes.
    Every option another option dominates is left out first (undominated): the totals are the exact optimum's. Ties
    (the canonical tie-break): among assignments whose (primary, other) totals are equal within solve's 1e-9 tolerance,
    the one returned is the lexicographically smallest in search order (option indexes, part by part) among the options
    left after the dominance cut, whatever the start. info also gets "dominated": how many options were left out.
    trace (a list): one dict per decision is appended (trace_row): the start, each option the dominance cut leaves out,
    each child the search tries (explored, or cut and why) and each full mix it reaches; it only reads the search's
    state, so the answer is the same with or without it."""
    t0 = time.time()
    other = "bytes" if primary == "ops" else "ops"
    nq, n = len(pb.queries), len(pb.order)
    tol = lambda x: 0.0 if x == INF else 1e-9 * max(1.0, abs(x))  # (no incumbent yet: inf - inf is nan)
    best = {"key": (INF, INF), "combo": None}
    if start is not None:                       # an incumbent to prune with from the start (e.g. the best uniform)
        o, b = pb.total(start)
        if cap is None or b <= cap + tol(cap):
            best = {"key": ((o, b) if primary == "ops" else (b, o)), "combo": list(start)}
    idx = [0] * nq                              # per query: the mixed-radix index of its decided prefix
    dep = [0] * nq                              # per query: how many of its parts are decided
    cur = {c: [q["mins"][c][0][0] for q in pb.queries] for c in ("ops", "bytes")}
    lb = {c: sum(cur[c]) for c in ("ops", "bytes")}
    combo = [0] * n
    nodes = 0
    choices = [range(min(k, len(LAYOUTS)) if plain else k) for k in pb.size] if allowed is None else [list(x) for x in allowed]
    assert start is None or all(v in ch for v, ch in zip(start, choices)), "the starting assignment is outside the allowed options"
    # leave out every dominated option (undominated): exact, the same totals with far fewer nodes (I02 per type and
    # direction at sf0.1, 4 KB: 1.7 G nodes -> 20 M over its fronts); a start is only the first incumbent, kept as given
    n_choices = sum(len(c) for c in choices)
    full_choices = choices
    choices = undominated(pb, choices)
    if best["combo"] is not None and any(v not in ch for v, ch in zip(best["combo"], choices)):
        best["outside"] = True       # a start with a dominated option loses every tie: the answer must not depend on it
    if info is not None:
        info["dominated"] = n_choices - sum(len(c) for c in choices)
    if trace is not None:
        trace.append(trace_row(pb, "start", 0, None, None, best["combo"] or [], best["key"], best["key"]))
        for t, (before, after) in enumerate(zip(full_choices, choices)):
            for v in before:
                if v not in after:
                    trace.append(trace_row(pb, "dominated", 0, t, v, [], None, best["key"]))
    lag = lagrange if (lagrange and cap is not None and primary == "ops") else []
    lcur = [[tab[qi][0][0] for qi in range(nq)] for _, tab in lag]      # per multiplier, per query: its current min
    llb = [sum(c) for c in lcur]

    def cut():
        """a lagrange bound already above the incumbent's ops: no completion that fits the cap can beat it"""
        bp = best["key"][0]
        return any(llb[k] - m * (cap + tol(cap)) > bp + tol(bp) for k, (m, _) in enumerate(lag))   # a fit within tol(cap) is kept

    def enter(t, v):
        """decide part t = v; returns what leave() restores. Bounds are restored from saved values, never by
        subtracting: a cost cache entry can be INF (no plan valid under that pairing), and INF - INF is nan"""
        undo = (lb["ops"], lb["bytes"], [(qi, idx[qi], dep[qi], cur["ops"][qi], cur["bytes"][qi]) for qi, _ in pb.touch[t]],
                list(llb), [[c[qi] for qi, _ in pb.touch[t]] for c in lcur])
        for qi, _ in pb.touch[t]:
            idx[qi] = idx[qi] * pb.size[t] + v
            dep[qi] += 1
            for c in ("ops", "bytes"):
                new = pb.queries[qi]["mins"][c][dep[qi]][idx[qi]]
                lb[c] += new - cur[c][qi]           # cur is finite here: a node is entered only from a finite bound
                cur[c][qi] = new
            if lb["ops"] < INF and lb["bytes"] < INF:     # (an INF node is left at once: its lagrange sums are not read)
                for k, (_, tab) in enumerate(lag):
                    new = tab[qi][dep[qi]][idx[qi]]
                    llb[k] += new - lcur[k][qi]
                    lcur[k][qi] = new
        assert lb["ops"] == lb["ops"] and lb["bytes"] == lb["bytes"], "the bound became nan"
        return undo

    def leave(undo):
        lb["ops"], lb["bytes"], qs, l0, lq = undo
        for qi, i0, d0, co, cb in qs:
            idx[qi], dep[qi], cur["ops"][qi], cur["bytes"][qi] = i0, d0, co, cb
        llb[:] = l0
        for k, vals in enumerate(lq):
            for (qi, _, _, _, _), v0 in zip(qs, vals):
                lcur[k][qi] = v0

    def dfs(t):
        nonlocal nodes
        nodes += 1
        me = nodes                              # this node's number (the trace's rows of its children name it)
        if max_nodes is not None and nodes > max_nodes:
            raise _OutOfNodes
        if t == n:
            key = (lb[primary], lb[other])
            bp, bo = best["key"]
            tied = abs(key[0] - bp) <= tol(bp) and abs(key[1] - bo) <= tol(bo)
            better = key[0] < bp - tol(bp) or (abs(key[0] - bp) <= tol(bp) and key[1] < bo - tol(bo)) \
                or (tied and (best.get("outside") or combo < best["combo"]))   # the canonical tie-break
            if better:
                best["key"], best["combo"], best["outside"] = key, list(combo), False
            if trace is not None:
                trace.append(trace_row(pb, "leaf: new best" if better else "leaf: not better", me, None, None, combo,
                                       key, best["key"]))
            return
        children = []
        for v in choices[t]:                    # the child bounds, to try the most promising first
            undo = enter(t, v)
            finite = lb["ops"] < INF and lb["bytes"] < INF
            if finite and not cut():            # an INF bound: no plan under this pairing, no completion
                children.append((lb[primary], lb[other], v))
            elif trace is not None:
                trace.append(trace_row(pb, "cut: lagrange bound" if finite else "cut: no plan", me, t, v, combo[:t],
                                       (lb[primary], lb[other]), best["key"]))
            leave(undo)
        for lp, lo, v in sorted(children):
            bp, bo = best["key"]
            # `continue`, not `break`: a child cut by the tie arm (primary within the tolerance, other currency
            # worse) can be followed by one whose primary is also within the tolerance and whose other is better
            if lp > bp + tol(bp) or (abs(lp - bp) <= tol(bp) and lo > bo + tol(bo)):
                if trace is not None:
                    trace.append(trace_row(pb, "cut: floor not below the best", me, t, v, combo[:t], (lp, lo), best["key"]))
                continue
            undo = enter(t, v)
            fits = cap is None or lb["bytes"] <= cap + tol(cap)
            if fits and not cut():
                if trace is not None:
                    trace.append(trace_row(pb, "explored", me, t, v, combo[:t], (lb[primary], lb[other]), best["key"],
                                           child=nodes + 1))
                combo[t] = v
                dfs(t + 1)
            elif trace is not None:
                trace.append(trace_row(pb, "cut: lagrange bound" if fits else "cut: over the byte limit", me, t, v,
                                       combo[:t], (lb[primary], lb[other]), best["key"]))
            leave(undo)

    proven = True
    if lb["ops"] < INF and lb["bytes"] < INF:  # else some query has no usable plan under any assignment
        try:
            dfs(0)
        except _OutOfNodes:                    # the best found so far stands; its totals are re-priced from the combo below
            proven = False
            if trace is not None:                # the last explored child named this node; it was not entered
                trace.append(trace_row(pb, "node limit: not entered", nodes, None, None, [], None, best["key"]))
    elif trace is not None:
        trace.append(trace_row(pb, "stop: a query has no plan under any mix", 0, None, None, [],
                               (lb[primary], lb[other]), best["key"]))
    if info is not None:
        info["proven"] = proven
    if best["combo"] is None:
        return None, INF, INF, nodes, time.time() - t0
    o, b = pb.total(best["combo"])
    return pb.to_parts(best["combo"]), o, b, nodes, time.time() - t0


def trace_row(pb, verdict, node, part, option, prefix, floor, best, child=None):
    """one decision of solve's trace: the node it was taken at (solve's node count), the part decided (search position)
    and the option tried, the mix decided so far ({part: layout}, before this option), the floor after the option (on a
    leaf: the mix's total; on the start row: the start's) and the best so far, both (primary, other) totals as the
    search sees them (weighted by how often each query runs, under the sum weighting), and why"""
    return {"verdict": verdict, "node": node, "child": child,
            "part": None if part is None else pb.order[part], "option": None if part is None else pb.options[part][option],
            "mix": {pb.order[i]: pb.options[i][v] for i, v in enumerate(prefix)},
            "floor": None if floor is None else tuple(floor), "best": None if best is None else tuple(best)}


def two_step(pb, start, primary="ops", allowed=None):
    """the greedy incumbent from `start` (a search-order combo, e.g. the best uniform layout), over the problem's costs:
    (a) each part alone takes its cheapest option with every other part at the start, the choices combined; (b) from the
    cheaper of (a) and the start, one part at a time (search order) takes the option that lowers the total while one
    does. Totals compare as (primary, the other currency), a move only on a relative drop over 1e-12. The costs hold
    each query's plan pick, so (a) here re-picks plans (the per-part rule at fixed plans needs the plans themselves:
    experiments/mix_benefit/combined_other_benchmarks.py). allowed: per part the option indexes it may take (default
    all). -> (combo, {"a": combo, "a_total": (ops, bytes), "start_total", "total", "moves", "tried", "a_s", "b_s": each
    stage's wall time}). Not a proof: solve proves, starting here."""
    other = "bytes" if primary == "ops" else "ops"
    choices = [list(range(k)) for k in pb.size] if allowed is None else [list(x) for x in allowed]

    def key(c):
        o, b = pb.total(c)
        return ({"ops": o, "bytes": b}[primary], {"ops": o, "bytes": b}[other])

    def better(k, than):
        return k[0] < than[0] * (1 - 1e-12) or (k[0] <= than[0] * (1 + 1e-12) and k[1] < than[1] * (1 - 1e-12))

    import time as _time
    t_a = _time.time()
    start = list(start)
    k0 = key(start)
    a = list(start)
    tried = 0
    for t in range(len(pb.order)):              # (a): each part alone, the others at the start
        best_v, best_k = start[t], k0
        for v in choices[t]:
            if v != start[t]:
                k = key(start[:t] + [v] + start[t + 1:])
                tried += 1
                if better(k, best_k):
                    best_v, best_k = v, k
        a[t] = best_v
    ka = key(a)
    a_s, t_b = round(_time.time() - t_a, 3), _time.time()
    cur, kc = (list(a), ka) if not better(k0, ka) else (list(start), k0)
    moves, improved = 0, True
    while improved:                             # (b): one part at a time while the total drops
        improved = False
        for t in range(len(pb.order)):
            for v in choices[t]:
                if v != cur[t]:
                    trial = cur[:t] + [v] + cur[t + 1:]
                    k = key(trial)
                    tried += 1
                    if better(k, kc):
                        cur, kc, improved, moves = trial, k, True, moves + 1
    tot = lambda k: k if primary == "ops" else (k[1], k[0])  # back to (ops, bytes)
    return cur, {"a": a, "a_total": tot(ka), "start_total": tot(k0), "total": tot(kc), "moves": moves, "tried": tried,
                 "a_s": a_s, "b_s": round(_time.time() - t_b, 3)}


BUDGETS = NAMED_BUDGETS              # B: total bytes <= B x B_UNIT's (Sp-Co's; options.csv's B=<b> questions)
QUESTIONS = ("fewest ops", "fewest bytes") + tuple(f"B={b:g}" for b in BUDGETS)


def options_b_unit(questions):
    """the cell an options.csv's B=<b> questions are B x the bytes of: B_UNIT, or Co-Sp (OLD_B_UNIT) in a file before
    told by its question set (B = 0.5, 1, 2, 3 then; B = 1, 2, 3, 5 since)"""
    named = sorted(float(q[2:]) for q in set(questions) if q.startswith("B="))
    if named == [0.5, 1.0, 2.0, 3.0]:
        return OLD_B_UNIT
    if named == sorted(float(b) for b in BUDGETS):
        return B_UNIT
    raise SystemExit(f"an options.csv whose named limits are B = {named}: neither the Co-Sp set (0.5, 1, 2, 3; "
                     f"B x Co-Sp's bytes) nor the Sp-Co set ({', '.join(f'{b:g}' for b in BUDGETS)}; B x Sp-Co's): its unit is unknown")


def uniform(parts, props, adj):
    """every part on one single layout"""
    return {u: (props if u[0] == "props" else adj) for u in parts}


def b_unit_layout(parts):
    """every part on B_UNIT's layout: the uniform layout whose bytes are B = 1"""
    return uniform(parts, *cell_settings(B_UNIT))


def named_questions(pb, ref_bytes, budgets=BUDGETS, plain=False):
    """[(question, primary, cap, start)]: named_solves' solves in its order, each with its start (the best single layout
    that qualifies). Each is one solve(pb, primary, cap, start, plain=plain), independent of the others: a caller may run
    them side by side, then named_answers puts the results together as named_solves does."""
    singles = [pb.from_assign(uniform(pb.parts, p, a)) for p in LAYOUTS for a in LAYOUTS]
    out = []
    for question, primary, cap in ([("fewest ops", "ops", None), ("fewest bytes", "bytes", None)] +
                                   [(f"B={b:g}", "ops", b * ref_bytes) for b in budgets]):
        fits = [s for s in singles if cap is None or pb.total(s)[1] <= cap * (1 + 1e-9)]
        order = (lambda s: pb.total(s)) if primary == "ops" else (lambda s: pb.total(s)[::-1])
        out.append((question, primary, cap, min(fits, key=order) if fits else None))
    return out


def named_answers(questions, results):
    """named_solves' list from named_questions' and each one's solve() result, in the same order. A budget no mix fits
    gets the fewest-bytes mix, fits=False (criteria.budget_pick's fallback)."""
    out, fewest_bytes = [], None
    for (question, _, _, _), (assign, _, _, nodes, _) in zip(questions, results):
        if question == "fewest bytes":
            fewest_bytes = assign
        if assign is None:
            out.append((question, fewest_bytes, False, nodes))
        else:
            out.append((question, assign, True, nodes))
    return out


def named_solves(pb, ref_bytes, budgets=BUDGETS, plain=False):
    """[(question, assign, fits, nodes)]: fewest ops, fewest bytes, then fewest ops within B x ref_bytes for each B.
    A budget no mix fits gets the fewest-bytes mix, fits=False (criteria.budget_pick's fallback). Each solve starts
    from the best single layout that qualifies. plain=True: the grid's three settings only (no other option)."""
    questions = named_questions(pb, ref_bytes, budgets, plain)
    return named_answers(questions, [solve(pb, primary, cap, start, plain=plain) for _, primary, cap, start in questions])


# --- two more exact solves -------------------------------------------------------------------------------------------
# The joint solve (joint_search.py's CLI): ONE per type mix for several workloads at once, by max / geomean / mean of
# each workload's ratio to its best uniform layout.
TOL = 1e-9


def stats(ratios):
    """(max, geomean, mean) of the workloads' ratios"""
    return max(ratios), math.exp(math.fsum(math.log(r) for r in ratios) / len(ratios)), math.fsum(ratios) / len(ratios)


def key_of(reading, ratios):
    """the objective, smaller is better: A (max, then geomean); B (geomean,); C (mean,)"""
    mx, geo, mean = stats(ratios)
    return {"max": (mx, geo), "geomean": (geo,), "mean": (mean,)}[reading]


def worse(a, b):
    """is key a no better than b can ever get (lexicographic, with the tolerance): a lower bound a prunes against b"""
    for x, y in zip(a, b):
        if x > y + TOL * max(1.0, abs(y)):
            return True
        if x < y - TOL * max(1.0, abs(y)):
            return False
    return True                                  # equal within the tolerance: no improvement


class Joint:
    """several workloads over one unit set: one exact_search.Problem per workload, all in ONE search order"""

    def __init__(self, ws, data):
        self.names = list(ws)
        own = {n: workload_units(w, data) for n, w in ws.items()}
        units = sorted({u for us in own.values() for u in us})
        touched = {u: sum(u in us for us in own.values()) for u in units}
        self.order = sorted(units, key=lambda u: (-touched[u], units.index(u)))    # shared by the most workloads first
        self.own = own
        extra = {"sum": lambda t: cost_of(t, "sum")}
        built = problems([(w, data, own[n], extra, [u for u in self.order if u in own[n]]) for n, w in ws.items()], jobs())
        self.pbs = dict(zip(ws, built))      # the one builder: the saved mix cost cache when set (set_cache)
        # the search indexes a part's options in base 3: the grid's three settings only
        assert all(k == len(LAYOUTS) for pb in self.pbs.values() for k in pb.size), "the grid's three settings only"

    def cost(self, name, cur, assign):
        """a workload's cost under a full assignment, from its costs"""
        pb = self.pbs[name]
        combo = pb.from_assign({u: assign[u] for u in pb.order})
        tot = 0.0
        for q in pb.queries:
            idx = 0
            for p in q["parts"]:
                idx = idx * pb.size[p] + combo[p]
            tot += q["mins"][cur][-1][idx]
        return tot

    def refs(self, cur):
        """{workload: (the best uniform layout, its cost)} in a currency"""
        out = {}
        for n in self.names:
            costs = {c: self.cost(n, cur, uniform(self.order, *c[len("props_"):].split("_adj_"))) for c in CELLS}
            best = min(CELLS, key=lambda c: (costs[c], c))
            out[n] = (best, costs[best])
        return out

    def solve(self, cur, reading, max_nodes=None):
        """the exact optimum of `reading` in `cur`: (assign, key, nodes, seconds). Starts from the best uniform layout."""
        t0 = time.time()
        refs = self.refs(cur)
        n_parts, names = len(self.order), self.names
        # flat state per (workload, query): the decided prefix's index, its depth, its current bound
        qs = []                                   # (workload index, min arrays per depth, sizes of its parts)
        touch = [[] for _ in range(n_parts)]      # global part -> the flat queries it enters
        for wi, n in enumerate(names):
            pb = self.pbs[n]
            for q in pb.queries:
                qi = len(qs)
                qs.append((wi, q["mins"][cur]))
                for p in q["parts"]:
                    touch[self.order.index(pb.order[p])].append(qi)
        idx, dep = [0] * len(qs), [0] * len(qs)
        curv = [tabs[0][0] for _, tabs in qs]
        lb = [0.0] * len(names)
        for (wi, _), v in zip(qs, curv):
            lb[wi] += v
        ref = [refs[n][1] for n in names]
        # every workload needs a usable uniform layout (its reference) and a finite root bound: else no ratio exists
        assert all(0 < r < INF for r in ref), "a workload has no uniform layout with a plan (or a zero cost): no reference"
        assert all(x < INF for x in lb), "a workload has a query with no usable plan under any assignment"
        best = {"key": None, "combo": None}
        for c in CELLS:                           # the incumbent: the best uniform layout under this reading
            a = uniform(self.order, *c[len("props_"):].split("_adj_"))
            costs = [self.cost(n, cur, a) for n in names]
            if all(x < INF for x in costs):
                k = key_of(reading, [x / r for x, r in zip(costs, ref)])
                if best["key"] is None or not worse(k, best["key"]):
                    best = {"key": k, "combo": [LAYOUTS.index(a[u]) for u in self.order]}
        combo, nodes = [0] * n_parts, 0

        def enter(t, v):
            undo = (list(lb), [(qi, idx[qi], dep[qi], curv[qi]) for qi in touch[t]])
            for qi in touch[t]:
                wi, tabs = qs[qi]
                idx[qi] = idx[qi] * len(LAYOUTS) + v
                dep[qi] += 1
                new = tabs[dep[qi]][idx[qi]]
                lb[wi] += new - curv[qi]
                curv[qi] = new
            return undo

        def leave(undo):
            lb[:], rest = undo
            for qi, i0, d0, c0 in rest:
                idx[qi], dep[qi], curv[qi] = i0, d0, c0

        def dfs(t):
            nonlocal nodes
            nodes += 1
            if max_nodes and nodes > max_nodes:
                raise TimeoutError
            if t == n_parts:
                k = key_of(reading, [x / r for x, r in zip(lb, ref)])
                if best["key"] is None or not worse(k, best["key"]):
                    best["key"], best["combo"] = k, list(combo)
                return
            children = []
            for v in range(len(LAYOUTS)):
                undo = enter(t, v)
                if all(x < INF for x in lb):
                    children.append((key_of(reading, [x / r for x, r in zip(lb, ref)]), v))
                leave(undo)
            for k, v in sorted(children):
                if best["key"] is not None and worse(k, best["key"]):
                    continue
                undo = enter(t, v)
                combo[t] = v
                dfs(t + 1)
                leave(undo)

        dfs(0)
        assign = {u: LAYOUTS[v] for u, v in zip(self.order, best["combo"])}
        return assign, best["key"], nodes, time.time() - t0


COST_CACHE_VERSION = 1      # bump when the cost cache's meaning changes outside SOURCES (e.g. a new input the key does not cover)
HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
# the code the cost cache's entries come from (Problem.fill_cost_cache -> compose.query_pick and what it calls); an edit to any
# of them rebuilds every query's costs. check_cost_cache.py fails when a fill imports a repo module that is not listed here.
SOURCES = sorted(glob.glob(os.path.join(EXP, "python", "kvcost", "**", "*.py"), recursive=True)            # kvcost, whole
                 + glob.glob(os.path.join(EXP, "python", "kvevaluator", "**", "*.py"), recursive=True))   # the evaluator, whole
_CACHE = {"dir": None, "fresh": False}
_FILE_SHA = {}         # path -> sha256 of its content, once per process
TMP_MARK = ".tmp"   # a query's costs being written: <key>.tmp<pid>.npz, renamed onto <key>.npz when complete


def set_cache(path, fresh=False):
    """save and reuse each query's costs in `path` (None: build them in memory, save none); fresh: build every query's
    again and overwrite what is there"""
    _CACHE.update(dir=path, fresh=bool(fresh))
    if path:
        os.makedirs(path, exist_ok=True)


def _sha_files(paths):
    """sha256 over the files' names and contents (a missing file counts as missing)"""
    h = hashlib.sha256()
    for p in sorted(paths):
        if p not in _FILE_SHA:
            try:
                with open(p, "rb") as f:
                    _FILE_SHA[p] = hashlib.sha256(f.read()).hexdigest()
            except FileNotFoundError:
                _FILE_SHA[p] = "missing"
        h.update(f"{os.path.relpath(p, EXP)}\0{_FILE_SHA[p]}\n".encode())
    return h.hexdigest()


def _canon(x):
    """a stable text form of a query's data (dicts and sets sorted; floats by repr)"""
    if isinstance(x, dict):
        return "{" + ",".join(f"{_canon(k)}:{_canon(v)}" for k, v in sorted(x.items(), key=lambda kv: repr(kv[0]))) + "}"
    if isinstance(x, (set, frozenset)):
        return "s[" + ",".join(sorted(_canon(v) for v in x)) + "]"
    if isinstance(x, (list, tuple)):
        return "[" + ",".join(_canon(v) for v in x) + "]"
    text = repr(x)
    if " object at 0x" in text:     # an address differs in every process: a key built from it never hits again
        raise TypeError(f"the mix cost cache key: {text} has no repr of its content (give its class a __repr__)")
    return text


def _context():
    """what every query's costs of this process share: the code and the version. The pricing settings (compose.Composer, with
    its plan rule) and the load's missing sizes come in with each query's data (d["composer"], d["sized_missing"])"""
    return repr((COST_CACHE_VERSION, _sha_files(SOURCES)))


def _family_inputs(d):
    """the files a query's pick reads besides its data: its family's plan files, the capture's run rows (reported and
    held-out) and their run records"""
    from kvevaluator.plan_choice.candidates import PLANS_ROOT
    paths = glob.glob(os.path.join(PLANS_ROOT, str(d.get("fam")), "**", "*"), recursive=True)
    for rd in sorted(set((d.get("read_dirs") or {}).values())):    # each cell's capture (output_tree.layout_capture)
        cap = Capture(rd)
        paths += glob.glob(os.path.join(cap.dir("counts"), "*"))
        if cap.has_held_out():
            paths += glob.glob(os.path.join(cap.held_out().dir("counts"), "*"))
    return _sha_files([p for p in paths if os.path.isfile(p)])


def _code_key(code, globs):
    """what a function's code computes: its bytecode, its constants (a nested function's code by the same rule), and
    each global name it reads with what that name is bound to (`cost_of` and `value` differ only there)"""
    consts = tuple(_code_key(c, globs) if hasattr(c, "co_code") else repr(c) for c in code.co_consts)

    def bound(name):
        v = globs.get(name)
        return getattr(v, "__module__", None), getattr(v, "__qualname__", None) or getattr(v, "__name__", None)
    return (code.co_code.hex(), consts, tuple((n, bound(n)) for n in code.co_names))


def extra_key(extra):
    """the extra lanes' part of a cost cache key: each one's name, function and code (_code_key) and its closure's values"""
    return [(name, f.__module__, f.__qualname__, _code_key(f.__code__, f.__globals__),
             tuple(repr(c.cell_contents) for c in (f.__closure__ or ())))
            for name, f in sorted((extra or {}).items())]


def query_key(context, data, item, extra):
    """the cost cache key of one query's costs: its data, plan files, run rows and records, params, parts and their
    options, the extra lanes, and `context` (_context: the code)"""
    fam, q, _, params = item
    d = data[(fam, q)]
    parts = query_parts(d)
    opts = {u: options_of(data, u) for u in parts}
    more = extra_key(extra)
    if "composer" in d:                         # the weighting reads built costs, never changes them: one cached entry
        d = {**d, "composer": dataclasses.replace(d["composer"], weighting="sum")}
    return hashlib.sha256(repr((context, fam, q, None if params is None else list(params), _family_inputs(d), parts,
                                sorted(opts.items()), more, hashlib.sha256(_canon(d).encode()).hexdigest())).encode()).hexdigest()[:32]


def inputs_key(data, w):
    """what a search of workload `w` reads, as one key: each query's cost cache key without the code and the lanes
    (query_key: its data with the composer's settings, its plan files, its run rows and their records, its params, parts
    and options), with how often it runs. A change to our own code does not move it; a result searched on the same
    inputs is reused by it (results.identity's `inputs`)"""
    text = repr(sorted((query_key(None, data, item, None), repr(item[2])) for item in w["items"]))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _axes(pb, data, item):
    """(the query's parts in search order, their shape, the permutation search order -> canonical (sorted))"""
    _, _, ps, where = pb.query_axes(data, item)
    shape = tuple(pb.size[p] for p in where)
    canon = sorted(range(len(ps)), key=lambda j: ps[j])
    return ps, shape, canon


def _save(path, pb, data, item, ops, byt, more):
    """one query's full costs, their axes in canonical order"""
    _, shape, canon = _axes(pb, data, item)
    arrs = {f"t_{name}": np.asarray(tab, dtype=float).reshape(shape).transpose(canon).copy()
            for name, tab in (("ops", ops), ("bytes", byt), *sorted(more.items()))}
    tmp = path + f"{TMP_MARK}{os.getpid()}.npz"
    np.savez(tmp, **arrs)
    os.replace(tmp, path)


def _load(path, pb, data, item, extra):
    """one query's full costs from the saved cache, their axes put back in this problem's search order: (ops, bytes, more);
    None for an entry of another shape (the caller builds it again and overwrites it: a cache entry, not a result)"""
    _, shape, canon = _axes(pb, data, item)
    back = np.argsort(canon)
    with np.load(path) as z:
        got = {k[2:]: z[k].transpose(back).reshape(-1).tolist() for k in z.files}
    want = {"ops", "bytes", *sorted(extra or {})}
    if set(got) != want or any(len(v) != int(np.prod(shape, dtype=np.int64)) for v in got.values()):
        print(f"[mix cost cache] WARNING {path} holds {sorted(got)} of another shape than {shape}: built again, overwritten",
              file=sys.stderr, flush=True)
        return None
    return got.pop("ops"), got.pop("bytes"), {name: got[name] for name in sorted(extra or {})}


def add_cache_args(ap):
    """--fresh, for a command that builds the mix cost cache (the fronts' drivers always; search-mix when it uses it)"""
    ap.add_argument("--fresh", action="store_true", help="build every query's mix cost cache entry again and overwrite the "
                    "saved copy (the mix cost cache: each query's cost under every assignment of the parts it reads, its "
                    "picked plan's ops and bytes, one .npz file per query in <stats>/<dataset>/<scale>/mix-cost-cache, "
                    "KVEVAL_STATS_DIR; keyed by everything it is made from, so any run at the scale reuses it and a change "
                    "builds it again)")


def apply_cache_args(a, default=None):
    """set_cache for a command that builds the mix cost cache, with its --fresh: the dir is `default`, else its one place,
    the shared stats' <dataset>/<scale>/mix-cost-cache (the run's --dataset; a driver that names none runs LDBC)"""
    path = default or os.path.join(stats_dir(getattr(a, "dataset", None) or "ldbc", a.sf), "mix-cost-cache")
    set_cache(path, a.fresh)
    if path not in _CACHE_SAID:                 # once per process: a command may load its data more than once
        _CACHE_SAID.add(path)
        print(f"[mix cost cache] {path}{' (--fresh: every query\'s costs built again)' if a.fresh else ''}",
              file=sys.stderr)


_CACHE_SAID = set()


def cache_size():
    """(files, bytes) of the complete entries in the saved cache dir. An entry being written is not counted, and a file gone
    between the listing and its size (another process's rename) is skipped: parallel chains share one cache"""
    files, size = 0, 0
    for f in (glob.glob(os.path.join(_CACHE["dir"], "*.npz")) if _CACHE["dir"] else []):
        if TMP_MARK in os.path.basename(f):
            continue
        try:
            size += os.path.getsize(f)
        except FileNotFoundError:
            continue
        files += 1
    return files, size


def jobs():
    """how many cost cache slices to fill at a time: LAYOUT_CHOICE_JOBS (run-layout-choice.sh --jobs), else 1 (one process)"""
    return max(1, int(os.environ.get("LAYOUT_CHOICE_JOBS") or 1))


def problem(w, data, units, extra=None, order=None):
    """exact_search.Problem(w, data, units, extra, order) by problems(): from the saved mix cost cache when set, the costs that
    must be built filled LAYOUT_CHOICE_JOBS slices at a time"""
    return problems([(w, data, units, extra, order)], jobs())[0]


_SPECS = []      # what a forked worker reads: [(w, data, the Problem without its queries, extra)]
SLICE = 1500     # cost cache entries per task: a few seconds of pricing
# a process's peak while it fills one query's costs whole, in bytes per entry: the most seen (LDBC's interactive
# queries at seven settings a part, sf0.1: ic10, 823,543 entries, 2.6 GB = 3.1 KB an entry; the largest,
# ic12, 5,764,801 entries, 4.4 GB = 0.8 KB an entry: it grows with the query's plans)
FILL_BYTES = 3200


def _fill(task):
    """one slice of a query's costs, and whether the slice priced an option that is no recorded store (compose says
    that once, in the parent: a worker prints nothing)"""
    from kvcost.layout_cost import compose
    k, i, lo, hi = task
    w, data, pb, extra = _SPECS[k]
    n0 = len(compose._UNRECORDED)
    return k, i, lo, pb.fill_cost_cache(data, w["items"][i], extra, lo, hi), len(compose._UNRECORDED) > n0


def _fill_whole(task):
    """a query's whole costs by the vectorised fill (None: it takes the scalar path, in slices), and _fill's flag"""
    from kvcost.layout_cost import compose
    k, i = task
    w, data, pb, extra = _SPECS[k]
    n0 = len(compose._UNRECORDED)
    return k, i, pb.vector_fill(data, w["items"][i], extra), len(compose._UNRECORDED) > n0


def problems(specs, jobs):
    """[exact_search.Problem(w, data, units, extra, order) for (w, data, units[, extra[, order]]) in specs], the costs
    filled `jobs` at a time (at_a_time; jobs <= 1: one after another, as problem() does): first the queries the
    vectorised fill takes whole, a query a process, the largest first; then the others' slices. The costs, and the
    files saved, are the same at any `jobs`. extra: {name: function of a query's
    cost vector}, further lanes beside ops and bytes; order: the parts' search order, given (one order shared by several
    workloads' problems). Each query's costs come from the saved cache when set_cache set one."""
    specs = [tuple(sp) + (None,) * (5 - len(sp)) for sp in specs]
    t0 = time.time()
    out, tasks = [None] * len(specs), []
    # the persistent cache (set_cache): each query's key, the costs in hand, a key's first query in this call, hits/misses
    context = _context() if _CACHE["dir"] else None
    keys, costs, first, same, n_hit, n_miss = {}, {}, {}, {}, 0, 0
    vector_filled = {}         # (spec, item) -> a query's costs fill.vector_cost_cache filled whole
    whole = []                 # (entries, spec, item) of the queries to build
    del _SPECS[:]
    for k, (w, data, units, extra, order) in enumerate(specs):
        parts = list(units)
        if order is None:      # the search order, exact_search.Problem's own rule: most-touched part first, ties by the parts' order
            qparts = [query_parts(data[(fam, q)]) for fam, q, _, _ in w["items"]]
            degree = {u: sum(u in ps for ps in qparts) for u in parts}
            order = sorted(parts, key=lambda u: (-degree[u], parts.index(u)))
        pb = Problem({"items": []}, data, parts, extra=extra, order=order)   # the parts, their order and options; no query yet
        _SPECS.append((w, data, pb, extra))
        for i, item in enumerate(w["items"]):
            if _CACHE["dir"]:
                key = query_key(context, data, item, extra)
                keys[(k, i)] = key
                path = os.path.join(_CACHE["dir"], f"{key}.npz")
                hit = _load(path, pb, data, item, extra) if not _CACHE["fresh"] and os.path.exists(path) else None
                if hit is not None:
                    costs[(k, i)] = hit
                    n_hit += 1
                    continue
                if key in first:                   # the same costs twice in this call: priced once
                    same[(k, i)] = first[key]
                    continue
                first[key] = (k, i)
                n_miss += 1
            whole.append((math.prod(pb.size[p] for p in pb.query_axes(data, item)[3]), k, i))
    entries = {(k, i): n for n, k, i in whole}
    # all of a query's costs at once where they can be; the largest first, so the pool ends on the short ones
    tried = at_a_time(_fill_whole, [(k, i) for n, k, i in sorted(whole, key=lambda t: (-t[0], t[1], t[2]))], jobs)
    unrecorded = jobs > 1 and any(t[3] for t in tried)
    for k, i, got, _ in sorted(tried, key=lambda t: t[:2]):
        if got is not None:
            vector_filled[(k, i)] = got
        else:
            tasks += [(k, i, lo, min(lo + SLICE, entries[(k, i)])) for lo in range(0, entries[(k, i)], SLICE)]
    for (k, i), tab in sorted(vector_filled.items()):
        costs[(k, i)] = tab
        if (k, i) in keys:
            w, data, pb, _ = _SPECS[k]
            _save(os.path.join(_CACHE["dir"], f"{keys[(k, i)]}.npz"), pb, data, w["items"][i], *tab)
    filled = at_a_time(_fill, tasks, jobs)
    if unrecorded or (jobs > 1 and any(f[4] for f in filled)):
        from kvcost.layout_cost import compose
        if not compose._UNRECORDED and not _CACHE.get("said"):     # once per process, if the parent has not said it
            _CACHE["said"] = True
            print("[compose] options that are no recorded store (in the cost cache fill's workers): the held-out rule on "
                  "their own capabilities (said once)", file=sys.stderr)
    if tasks:
        built = {}
        for k, i, lo, (ops, byt, more), _ in sorted(filled, key=lambda t: t[:3]):     # a query's slices in order
            o, b, m = built.setdefault((k, i), ([], [], {name: [] for name in more}))
            o += ops
            b += byt
            for name in more:
                m[name] += more[name]
        for (k, i), tab in sorted(built.items()):
            costs[(k, i)] = tab
            if (k, i) in keys:
                w, data, pb, _ = _SPECS[k]
                _save(os.path.join(_CACHE["dir"], f"{keys[(k, i)]}.npz"), pb, data, w["items"][i], *tab)
    for (k, i), (k0, i0) in sorted(same.items()):      # a duplicate within the call: its first copy, its own axes order
        w, data, pb, extra = _SPECS[k]
        costs[(k, i)] = _load(os.path.join(_CACHE["dir"], f"{keys[(k0, i0)]}.npz"), pb, data, w["items"][i], extra)
    for k in range(len(specs)):                         # every spec, one with no items too
        w, data, pb, _ = _SPECS[k]
        pb.queries = [pb.query_entry(data, item, *costs[(k, i)]) for i, item in enumerate(w["items"])]
        pb.touch = [[(qi, qp.index(p)) for qi, qd in enumerate(pb.queries) for qp in [qd["parts"]] if p in qp]
                    for p in range(len(pb.order))]
        pb.reweight([q["often"] for q in pb.queries])
        pb.cost_cache_seconds = time.time() - t0
        pb.cost_cache_entries = sum(len(q["one_run"]["ops"][-1]) for q in pb.queries)
        # each query's key in the saved cache, as this build read or wrote it (a row's identity keeps them)
        pb.cost_cache_keys = [keys[(k, i)] for i in range(len(w["items"]))] if _CACHE["dir"] else []
        out[k] = pb
    del _SPECS[:]
    if _CACHE["dir"]:
        nf, nb = cache_size()
        print(f"[mix cost cache] {_CACHE['dir']}: {n_hit} queries' costs loaded, {n_miss} built"
              f"{' (--fresh)' if _CACHE['fresh'] else ''}, {len(same)} shared within this call; {time.time() - t0:.0f} s; "
              f"the cache now {nf} files, {nb / 2**30:.2f} GiB", file=sys.stderr, flush=True)
    return out
