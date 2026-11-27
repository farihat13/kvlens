"""The mix search without the cost cache, for a workload whose largest query's cost cache does not fit (JOB: a query
reads up to 33 of 54 parts, 3^33 entries): branch and bound on each query's bodies instead of its costs.

A query's cost under a mix is the cost of the body the plan rule picks among the bodies usable under it
(compose.query_pick). At a node of the search (some parts decided), every completion costs, per query, at least

    min over the bodies valid in every pair of the DECIDED parts of
        [ the index's cheapest source + sum over decided parts of its cost at its setting
                                      + sum over free parts of its cheapest setting ]

where a part's cost at a setting is the min over the cells it can be read from. This is a lower bound for any plan
rule: the picked body is usable, usable bodies are valid on the decided pairs, and each part costs at least the min.
A query whose parts are all decided takes its exact cost, so leaves are exact. The search is search.solve's (part
order, bound order, byte cap, node limit, the canonical tie-break), with no dominance cut, so a tie may resolve to an
option the cost cache form leaves out (the totals agree).

The objective is a sum over queries of weight x form(cost) (form: the cost, or its log for geomean). Only models
linear in the cost vector (wops, bytes, wops_bytes4k): the bill rounds per request, so it is refused.
"""
import itertools
import math
import time

from kvcost.layout_cost import query_weighting as QW
from kvevaluator.layouts import cell_id
from kvcost.layout_cost.cost_models import cost_of, currencies
from kvcost.layout_cost.compose import CELLS, CH_CH, LAYOUTS, is_sized, options_of, params_of, query_pick, validity_layout
from kvcost.layout_search.search import INF, query_parts, uniform
from kvevaluator.plan_choice import candidates as C

FLOOR = 1e-12                  # the geomean's log of a lower bound at 0
COST_CACHE_LIMIT = 10 ** 7          # no cost cache above this many entries in the largest query's costs
LINEAR = {"wops": "ops", "bytes": "bytes", "wops_bytes4k": "sum"}     # the models this form solves -> cost_of's currency


# the channels the fast exact path sums: the plan pick's (candidates.pick_body: S, G, N, the read lanes) and the two
# currencies' (cost_models.currencies), the only ones a linear model reads
FAST_KEYS = ("S", "G", "N") + tuple(C.READ_LANES) + ("read_ops", "write_rmw_ops", "write_ops", "read_bytes",
                                                     "write_rmw_bytes", "write_bytes")
_MISSING = object()            # a part row compose._composed refuses (it would be priced 0): the fast path hands over


class _OutOfNodes(Exception):
    """the node limit, raised at a node not searched; .bound is that node's bound"""
    def __init__(self, bound):
        super().__init__(bound)
        self.bound = bound


def largest_cost_cache(w, data, units):
    """the entries of the largest query's costs the mix cost cache would hold for w over `units`"""
    us = set(units)
    return max((math.prod(len(options_of(data, u)) for u in query_parts(data[(f, q)]) if u in us)
                for f, q, _, _ in w["items"]), default=1)


def _lane(model):
    if model not in LINEAR:
        raise SystemExit(f"the form without the cost cache solves a model linear in the cost vector ({', '.join(LINEAR)}), not {model!r}")
    cur = LINEAR[model]
    return lambda vec: cost_of(vec, cur)


def _bytes(vec):
    return currencies(vec)["bytes"]


class Bodies:
    """one workload x one option set, as per-query bodies: per body and part, a lower-bound cost per setting"""

    def __init__(self, w, data, units, model="wops", form="sum", ref_costs=None, verify=False):
        t0 = time.time()
        self.w, self.data, self.model, self.form = w, data, model, form
        self.verify = verify                   # every fast exact cost also through query_pick, asserted equal (the checks)
        self.calls = {"fast": 0, "query_pick": 0}
        self.primary = _lane(model)
        self.other = _bytes if model != "bytes" else _lane("wops")
        self.parts = list(units)
        items = w["items"]
        qparts = [query_parts(data[(f, q)]) for f, q, _, _ in items]
        degree = {u: sum(u in ps for ps in qparts) for u in self.parts}
        self.order = sorted(self.parts, key=lambda u: (-degree[u], self.parts.index(u)))     # search.Problem's order
        pos = {u: i for i, u in enumerate(self.order)}
        self.options = [options_of(data, u) for u in self.order]
        self.size = [len(o) for o in self.options]
        self.weights = QW.item_weights(form, [it[2] for it in items], ref_costs)
        if any(not wt > 0 for wt in self.weights):
            raise ValueError("a query weight <= 0: the bound arithmetic needs positive weights")
        self.queries = []
        for (fam, q, often, params), ps in zip(items, qparts):
            d = data[(fam, q)]
            outside = sorted(u for u in ps if u not in pos)
            if outside:
                raise ValueError(f"{fam} {q} reads parts outside the problem's: {outside}")
            where = sorted(pos[u] for u in ps)
            self.queries.append(self._query(d, params_of(d, params), where))
        self.touch = [[qi for qi, qd in enumerate(self.queries) if t in qd["parts"]] for t in range(len(self.order))]
        # the geomean's totals are sums of logs: its tie tolerance is taken on the total less the root bound (each
        # query's cheapest), so it acts on a sum near 0, as the cost cache form's log(c / m) does
        self.offset = (self.ceiling(0), self.ceiling(1)) if form == "geomean" else (0.0, 0.0)
        self.build_s = time.time() - t0

    def _query(self, d, pp, where):
        """a query's entry: its parts (search order), params, bodies with per-part lower bounds, its exact cache"""
        units = [self.order[t] for t in where]
        variants = sorted(set().union(*(d["valid"].get(c, set()) for c in CELLS)))
        n = len(pp)

        def mean(src, v, u, fn):
            """a part's mean cost over the params, read from cell or option `src` (0 where a param has no row)"""
            tot = 0.0
            for i in pp:
                vec = (d["cost"].get((src, v, i)) or {}).get(u)
                if vec:
                    tot += fn(vec)
            return tot / n

        def sources(u, o):
            """the cells or options a unit at setting o can be read from (compose._composed's rule over every pair)"""
            if is_sized(o, self.data.composer):
                return [o]
            if u[0] == "props":
                return [cell_id(o, a) for a in LAYOUTS]
            return [cell_id(p, o) for p in LAYOUTS]

        bodies = []
        for v in variants:
            lb = []                              # per part (query order), per option: (primary, other) lower bounds
            for t, u in zip(where, units):
                lb.append([(min(mean(s, v, u, self.primary) for s in sources(u, o)),
                            min(mean(s, v, u, self.other) for s in sources(u, o))) for o in self.options[t]])
            base = (min(mean(c, v, None, self.primary) for c in CELLS), min(mean(c, v, None, self.other) for c in CELLS))
            mins = [(min(x[0] for x in opts), min(x[1] for x in opts)) for opts in lb]
            valid = frozenset(c for c in CELLS if v in d["valid"].get(c, set()))
            bodies.append({"v": v, "lb": lb, "mins": mins, "valid": valid,
                           "root": (base[0] + sum(m[0] for m in mins), base[1] + sum(m[1] for m in mins))})
        composer = self.data.composer
        fast = (not d.get("cost_h") and not d.get("measure_only_where") and not d.get("measure_only_registry")
                and not getattr(self.data, "options", None) and d.get("fam") in composer.plan_rule.in_sample_default)
        return {"d": d, "pp": pp, "parts": where, "units": units, "bodies": bodies, "cache": {}, "fast": fast, "rows": {},
                "has_props": any(u[0] == "props" for u in units), "has_adj": any(u[0] == "adj" for u in units)}

    # --- exact costs: the one rule, cached per query on its own parts' settings -------------------------------------
    def pick(self, qi, combo):
        """(picked body, {usable body: (primary, other)}) of query qi under combo (search order; only its parts are read)"""
        q = self.queries[qi]
        key = tuple(combo[t] for t in q["parts"])
        hit = q["cache"].get(key)
        if hit is None:
            assign = {u: self.options[t][o] for u, t, o in zip(q["units"], q["parts"], key)}
            got = self._fast(q, assign) if q["fast"] else None
            if got is None or self.verify:
                best, tots, _ = query_pick(q["d"], q["pp"], assign, set(q["units"]))
                slow = (best, {v: (self.primary(t), self.other(t)) for v, t in (tots or {}).items()})
                self.calls["query_pick"] += 1
                if got is not None and got != slow:
                    raise AssertionError(f"{q['d'].get('q')} under {assign}: the fast path gives {got}, query_pick {slow}")
                got = slow
            hit = q["cache"][key] = got
        return hit

    def _fast(self, q, assign):
        """query_pick under the in-sample rule (no held-out set, no measure-only body), from the part
        rows directly: the same sources, the same additions in the same order (compose._composed: per param, per unit
        sorted, then the index, each += row / n) and the same pick (candidates.pick_body on S, G, N, the read bytes), so
        the same floats; None where query_pick would refuse (it then says why)"""
        d, composer = q["d"], self.data.composer
        units = sorted(q["units"])
        playouts = {validity_layout(assign[u], composer) for u in units if u[0] == "props"} or set(LAYOUTS)
        alayouts = {validity_layout(assign[u], composer) for u in units if u[0] == "adj"} or set(LAYOUTS)
        if any(is_sized(assign[u], composer) and d.get("fam") in d["sized_missing"].get(assign[u], ()) for u in units):
            return None
        pairs = [cell_id(p, a) for p in sorted(playouts) for a in sorted(alayouts)]
        variants = set.intersection(*(d["valid"].get(c, set()) for c in pairs))
        if not variants:
            return (None, {})
        pmin, amin, k, K = min(playouts), min(alayouts), 1.0 / len(q["pp"]), len(FAST_KEYS)
        srcs = []
        for u in units:
            a = assign[u]
            if is_sized(a, composer):
                srcs.append((u, a))
            elif u[0] == "props":
                srcs.append((u, cell_id(a, amin)))
            else:
                srcs.append((u, cell_id(pmin, a)))
        srcs.append((None, pairs[0]))
        tots = {}
        for v in sorted(variants):
            tot = [0.0] * K
            for i in q["pp"]:
                for u, src in srcs:
                    vals = self._row(q, src, v, i, u)
                    if vals is _MISSING:
                        return None
                    if vals is not None:                 # compose.add's a[m] += k * b[m], key by key
                        tot = [x + k * y for x, y in zip(tot, vals)]
            tots[v] = dict(zip(FAST_KEYS, tot))
        self.calls["fast"] += 1
        if set(tots) == {"write"}:
            best = "write"
        elif len(tots) == 1:
            best = next(iter(tots))
        else:
            best, _ = C.pick_body({v: {"S": t["S"], "G": t["G"], "N": t["N"], "total_bytes": sum(t[l] for l in C.READ_LANES)}
                                   for v, t in tots.items()}, composer.criterion)
        return (best, {v: (self.primary(t), self.other(t)) for v, t in tots.items()})

    def _row(self, q, src, v, i, u):
        """one part row's FAST_KEYS values (None: no row, priced 0 as _composed does), memoized; _MISSING where
        _composed would stop"""
        key = (src, v, i, u)
        got = q["rows"].get(key, False)
        if got is not False:
            return got
        d = q["d"]
        rows = d["cost"].get((src, v, i))
        if rows is None:
            got = _MISSING
        else:
            vec = rows.get(u)
            if vec:
                got = tuple(vec[c] for c in FAST_KEYS)
            elif vec is None and u is not None and is_sized(src, self.data.composer) and \
                    d["cost"].get((CH_CH, v, i), {}).get(u):
                got = _MISSING
            else:
                got = None
        q["rows"][key] = got
        return got

    def query_cost(self, qi, combo):
        """(primary, other) of one run of query qi under combo, or None if no body is usable"""
        best, costs = self.pick(qi, combo)
        if best is None:
            return None
        if self.form == "geomean" and min(costs[best]) <= 0:      # as query_weighting.table_values refuses it
            q = self.queries[qi]
            raise SystemExit(f"geomean: {q['d'].get('q')} costs {costs[best]} under "
                             f"{ {u: self.options[t][combo[t]] for u, t in zip(q['units'], q['parts'])} }: a cost <= 0 "
                             f"has no log")
        return costs[best]

    def g(self, x):
        """the form of one query's cost in the objective: the cost, or its log (geomean)"""
        if self.form != "geomean" or x == INF:
            return x
        return math.log(max(x, FLOOR))         # a bound of 0 stays a bound (a usable cost is >= 1 op; table_values refuses <= 0)

    def total(self, combo):
        """(primary, other) objective of a full assignment (search order); (INF, INF) if a query has no usable body"""
        o = b = 0.0
        for qi in range(len(self.queries)):
            c = self.query_cost(qi, combo)
            if c is None:
                return INF, INF
            o += self.weights[qi] * self.g(c[0])
            b += self.weights[qi] * self.g(c[1])
        return o, b

    def per_query(self, combo):
        """each query's one-run primary cost under combo (for a score); INF where no body is usable"""
        return [(self.query_cost(qi, combo) or (INF,))[0] for qi in range(len(self.queries))]

    def from_assign(self, assign):
        return [self.options[t].index(assign[u]) for t, u in enumerate(self.order)]

    def to_parts(self, combo):
        return {u: self.options[t][combo[t]] for t, u in enumerate(self.order)}

    def ceiling(self, lane=0):
        """the root bound: each query at its cheapest body and each part at its cheapest setting and source, validity
        ignored; a lower bound on every mix's objective (lane 0 the primary, 1 the other)"""
        return sum(self.weights[qi] * self.g(min(b["root"][lane] for b in q["bodies"]))
                   for qi, q in enumerate(self.queries) if q["bodies"])

    def tol(self, x, lane=0):
        """the tie tolerance on a total (search.solve's: 1e-9 relative), the geomean's on the total less its offset"""
        return 0.0 if abs(x) == INF else 1e-9 * max(1.0, abs(x - self.offset[lane]))

    # --- the search ----------------------------------------------------------------------------------------------------
    def solve(self, cap=None, start=None, max_nodes=None, info=None):
        """the exact optimum of the primary model, optionally with the other lane (bytes) <= cap: (assign or None,
        primary, other, nodes, seconds), as search.solve returns. info gets "proven", "ceiling" (the root bound) and
        "bound": the best lower bound known when the search ends (proven: the answer; stopped by the node limit: the
        lowest of the answer, the stopped node's bound and every child still waiting on the search's stack; never
        below the ceiling)"""
        if cap is not None and self.form != "sum":
            raise ValueError(f"a byte cap is a sum over queries: not under the {self.form} form")
        t0 = time.time()
        nq, n = len(self.queries), len(self.order)
        tol, tol1 = self.tol, (lambda x: self.tol(x, 1))
        best = {"key": (INF, INF), "combo": None}
        if start is not None:
            o, b = self.total(start)
            if o < INF and (cap is None or b <= cap + tol1(cap)):
                best = {"key": (o, b), "combo": list(start)}
        combo = [None] * n
        # per query: per body its running (primary, other) bound; the decided layouts per side (counts, for undo)
        s = [[list(b["root"]) for b in q["bodies"]] for q in self.queries]
        sides = [{"props": {}, "adj": {}} for _ in self.queries]
        dep = [0] * nq
        qb = [self._query_bound(qi, s, sides, dep, combo) for qi in range(nq)]
        lb = [sum(self.weights[qi] * qb[qi][k] for qi in range(nq)) for k in (0, 1)]
        nodes = 0
        stack = []                               # per depth: the bounds of the children not entered yet

        def enter(t, v):
            undo = (list(lb), [(qi, list(map(list, s[qi])), qb[qi]) for qi in self.touch[t]])
            combo[t] = v
            u = self.order[t]
            lay = validity_layout(self.options[t][v], self.data.composer)
            for qi in self.touch[t]:
                q = self.queries[qi]
                j = q["parts"].index(t)
                for bi, b in enumerate(q["bodies"]):
                    s[qi][bi][0] += b["lb"][j][v][0] - b["mins"][j][0]
                    s[qi][bi][1] += b["lb"][j][v][1] - b["mins"][j][1]
                side = sides[qi][u[0]]
                side[lay] = side.get(lay, 0) + 1
                dep[qi] += 1
                new = self._query_bound(qi, s, sides, dep, combo)
                for k in (0, 1):
                    lb[k] += self.weights[qi] * (new[k] - qb[qi][k])
                qb[qi] = new
            return undo

        def leave(t, undo):
            l0, qs = undo
            u = self.order[t]
            lay = validity_layout(self.options[t][combo[t]], self.data.composer)
            for qi, s0, b0 in qs:
                s[qi], qb[qi] = s0, b0
                side = sides[qi][u[0]]
                side[lay] -= 1
                if not side[lay]:
                    del side[lay]
                dep[qi] -= 1
            lb[:] = l0
            combo[t] = None

        def dfs(t):
            nonlocal nodes
            nodes += 1
            if max_nodes is not None and nodes > max_nodes:
                raise _OutOfNodes(lb[0])
            if t == n:                           # every query is decided: lb is the exact total
                if cap is not None and lb[1] > cap + tol1(cap):
                    return
                key = (lb[0], lb[1])
                bp, bo = best["key"]
                tied = abs(key[0] - bp) <= tol(bp) and abs(key[1] - bo) <= tol1(bo)
                if key[0] < bp - tol(bp) or (abs(key[0] - bp) <= tol(bp) and key[1] < bo - tol1(bo)) \
                        or (tied and combo < best["combo"]):
                    best["key"], best["combo"] = key, list(combo)
                return
            children = []
            for v in range(self.size[t]):
                undo = enter(t, v)
                if lb[0] < INF and lb[1] < INF:
                    children.append((lb[0], lb[1], v))
                leave(t, undo)
            children.sort()
            waiting = [c[0] for c in reversed(children)]          # popped from the end as each child is entered
            stack.append(waiting)
            for lp, lo, v in children:
                waiting.pop()
                bp, bo = best["key"]
                if lp > bp + tol(bp) or (abs(lp - bp) <= tol(bp) and lo > bo + tol1(bo)):
                    continue
                undo = enter(t, v)
                if cap is None or lb[1] <= cap + tol1(cap):
                    dfs(t + 1)
                leave(t, undo)
            stack.pop()

        proven, ceiling, bound = True, self.ceiling(), None
        if lb[0] < INF:
            try:
                dfs(0)
            except _OutOfNodes as stop:
                proven = False
                bound = max(ceiling, min([best["key"][0], stop.bound] + [x for w in stack for x in w]))
        if bound is None:                        # finished: the answer is the bound (none found: nothing fits)
            bound = best["key"][0] if best["combo"] is not None else ceiling
        if info is not None:
            info.update(proven=proven, bound=bound, ceiling=ceiling)
        if best["combo"] is None:
            return None, INF, INF, nodes, time.time() - t0
        o, b = self.total(best["combo"])
        return self.to_parts(best["combo"]), o, b, nodes, time.time() - t0

    def _query_bound(self, qi, s, sides, dep, combo):
        """(primary, other) of query qi's bound in the objective's form: exact once all its parts are decided"""
        q = self.queries[qi]
        if dep[qi] == len(q["parts"]):
            c = self.query_cost(qi, combo)
            return (INF, INF) if c is None else (self.g(c[0]), self.g(c[1]))
        ps = set(sides[qi]["props"]) if q["has_props"] else set(LAYOUTS)
        as_ = set(sides[qi]["adj"]) if q["has_adj"] else set(LAYOUTS)
        need = {cell_id(p, a) for p in ps for a in as_}
        lo = [INF, INF]
        for bi, b in enumerate(q["bodies"]):
            if need <= b["valid"]:
                lo[0] = min(lo[0], s[qi][bi][0])
                lo[1] = min(lo[1], s[qi][bi][1])
        return self.g(lo[0]), self.g(lo[1])

    # --- the start: the two-step search (the combined panel's JOB search) ---------------------------------------------
    def two_step(self):
        """the incumbent: B = the best uniform layout; (a) each part alone at its cheapest setting, the others at B and
        each query's plan fixed at its pick under B; (b) from (a) (or B, whichever is lower), one part at a time while
        the exact total drops. Returns (combo, {"B", "B_total", "a_total", "b_total", "tried", "a_s", "b_s"}): a_s the wall
        time of B and (a), b_s of (b)"""
        t0 = time.time()
        unis = {(p, a): self.from_assign(uniform(self.order, p, a)) for p in LAYOUTS for a in LAYOUTS}
        tots = {k: self.total(c) for k, c in unis.items()}
        B = min(unis, key=lambda k: (tots[k], k))
        base = unis[B]
        plans = [self.pick(qi, base)[0] for qi in range(len(self.queries))]

        def fixed(c):
            o = 0.0
            for qi, p in enumerate(plans):
                _, costs = self.pick(qi, c)
                if p not in costs:
                    return INF
                o += self.weights[qi] * self.g(costs[p][0])
            return o

        base_val = fixed(base)
        a = list(base)
        for t in sorted(range(len(self.order)), key=lambda t: self.order[t]):    # the units' sorted order, as before
            best_v = base_val
            for v in range(self.size[t]):
                if v == base[t]:
                    continue
                val = fixed(base[:t] + [v] + base[t + 1:])
                if val < best_v - 1e-12 * abs(best_v):
                    a[t], best_v = v, val
        a_total = self.total(a)
        a_s, t1 = round(time.time() - t0, 3), time.time()
        cur, val = (list(a), a_total) if a_total[0] <= tots[B][0] else (list(base), tots[B])
        tried, improved = 0, True
        while improved:
            improved = False
            for t in sorted(range(len(self.order)), key=lambda t: self.order[t]):
                for v in range(self.size[t]):
                    if v == cur[t]:
                        continue
                    nxt = cur[:t] + [v] + cur[t + 1:]
                    got = self.total(nxt)
                    tried += 1
                    if got[0] < val[0] - 1e-12 * abs(val[0]):
                        cur, val, improved = nxt, got, True
        return cur, {"B": "/".join(B), "B_total": tots[B][0], "a_total": a_total[0], "b_total": val[0], "tried": tried,
                     "a_s": a_s, "b_s": round(time.time() - t1, 3)}

    def _fits(self, combo, cap):
        tot = self.total(combo)
        return tot[0] < INF and tot[1] <= cap * (1 + 1e-9)

    def _moves(self, combo):
        """every mix one part away from combo"""
        for t in range(len(self.order)):
            for v in range(self.size[t]):
                if v != combo[t]:
                    yield combo[:t] + [v] + combo[t + 1:]

    def _shed(self, cap, cur):
        """fit_start's step (1): (the mix, its moves) moved one part at a time to the setting that sheds bytes at the
        fewest wops per byte shed, until it fits; None if no move sheds a byte first"""
        tot, n = self.total(cur), 0
        while not self._fits(cur, cap):
            best = None
            for nxt in self._moves(cur):
                got = self.total(nxt)
                drop = tot[1] - got[1]
                if got[0] == INF or drop <= 0:
                    continue
                key = ((got[0] - tot[0]) / drop, got[1], nxt)     # wops per byte shed; ties: fewer bytes, then the combo
                if best is None or key < best[0]:
                    best = (key, got)
            if best is None:                                    # no move sheds a byte: the rule gives up
                return None, n
            cur, tot, n = best[0][2], best[1], n + 1
        return cur, n

    def _improve(self, cap, cur):
        """fit_start's step (2): (the mix, its moves) by the one-part move that lowers wops most and still fits, until
        none does"""
        tot, n = self.total(cur), 0
        while True:
            best = None
            for nxt in self._moves(cur):
                got = self.total(nxt)
                if self._fits(nxt, cap) and got[0] < tot[0] - self.tol(tot[0]) and (best is None or (got[0], nxt) < (best[1][0], best[0])):
                    best = (nxt, got)
            if best is None:
                return cur, n
            cur, tot, n = best[0], best[1], n + 1

    def fit_start(self, cap, mix):
        """the start of a capped solve when `mix` (the two-step mix) is over the byte cap: (combo or None, what it is,
        {"shed", "improve"}). (1) from `mix`, one part at a time to the setting that sheds bytes at the fewest wops per
        byte shed, until it fits; (2) then the one-part move that lowers wops most and still fits, until none does;
        (3) the better of that and the best uniform layout within the cap. Only a start: a finished solve's answer
        does not depend on it, but under a node limit a start near the answer leaves a smaller gap (JOB at 30,000
        nodes: from the best uniform layout the search ended where it began)"""
        cur, shed = self._shed(cap, list(mix))
        cur, improve = self._improve(cap, cur) if cur is not None else (None, 0)
        unis = [self.from_assign(uniform(self.order, p, a)) for p in LAYOUTS for a in LAYOUTS]
        unis = [c for c in unis if self._fits(c, cap)]
        uni = min(unis, key=lambda c: (self.total(c), c)) if unis else None
        steps = {"shed": shed, "improve": improve}
        if cur is not None and (uni is None or self.total(cur) <= self.total(uni)):
            return cur, "the two-step mix moved within the limit", steps
        return uni, ("the best uniform layout" if uni is not None else None), steps


def additive_reference(w, data, units, model="wops"):
    """(G, each query's one-run cost on G): G the geomean-best uniform layout (query_weighting.additive_reference, as the
    cost cache form's best_mix takes it)"""
    flat = Bodies(w, data, units, model=model, form="sum")
    per_query = {i: {} for i in range(len(flat.queries))}
    for p in LAYOUTS:
        for a in LAYOUTS:
            for i, c in enumerate(flat.per_query(flat.from_assign(uniform(flat.order, p, a)))):
                per_query[i][cell_id(p, a)] = c
    G = QW.additive_reference(per_query)
    return G, [per_query[i][G] for i in range(len(flat.queries))]


def brute(bodies, cap=None):
    """every assignment's exact total, the canonical optimum: (combo, key), ties within the solve's tolerance (planted
    problems in the tests only)"""
    best = None
    for combo in itertools.product(*(range(k) for k in bodies.size)):
        o, b = bodies.total(list(combo))
        if o == INF or (cap is not None and b > cap + bodies.tol(cap, 1)):
            continue
        if best is None:
            best = (list(combo), (o, b))
            continue
        (bo, bb), to, tb = best[1], bodies.tol(best[1][0]), bodies.tol(best[1][1], 1)
        if o < bo - to or (abs(o - bo) <= to and (b < bb - tb or (abs(b - bb) <= tb and list(combo) < best[0]))):
            best = (list(combo), (o, b))     # the solve's order: primary, then the other lane, then the smallest combo
    return best
