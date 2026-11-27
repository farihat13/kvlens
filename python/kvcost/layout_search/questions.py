"""The questions a mix answers, each as one result row (kvcost search-mix (and --front) call these).

Q1 the fewest under one model, no limit; Q2 the fewest wops within B x the reference layout's bytes (B_UNIT, Sp-Co);
Q3 the front, every mix no other beats in both wops and bytes, with the named points read off it. Each solve starts
from the two-step mix (search.two_step: each part alone, then one-part repair; under a limit, the best uniform layout
that fits when the two-step mix does not; the solve without the cost cache: Bodies.fit_start), then branch and bound proves it (search.solve: the dominance cut, the
canonical tie-break, a node limit). Every answer is priced again from the captures (front.cost_by_part) and must
equal the cost cache's totals. The mix cost cache comes from the one builder (search.problem), saved by set_cache.
"""
import json
import math
import re
import time
from typing import NamedTuple

from kvcost.layout_cost import cost_models as CM
from kvevaluator.layouts import cell_id
from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost.compose import LAYOUTS, currencies, cost_mix
from kvcost.layout_cost.settings import B_UNIT
from kvcost.layout_search import front as F
from kvcost.layout_search import results as R
from kvcost.layout_search import search as S

# a model: its name in a result row -> the lane it is solved on ("ops" and "bytes" are the lanes every problem holds;
# another name is an extra lane, built by EXTRA's function of a query's cost vector)
MODELS = {"wops": "ops", "bytes": "bytes", "wops_bytes4k": "sum",
          "bill_lower": "bill_lower", "bill_upper": "bill_upper", "bill_exact": "bill_exact"}
EXTRA = {"sum": lambda t: cost_of(t, "sum"),          # wops + bytes / 4,096 (cost_models.cost_of)
         "bill_lower": CM.bill_lower, "bill_upper": CM.bill_upper, "bill_exact": CM.bill_exact}   # the bill's forms


class Axes(NamedTuple):
    """a front's two currencies: x (the fewest of it is solved) against y (capped). Today's front is wops against bytes,
    the problem's own "ops" and "bytes" lanes; a bill form's is its read units against its write units
    (cost_models.BILL_UNITS: two lanes of a cost vector, kept as extra lanes and put in their place by Problem.pair)"""
    model: str              # the row's model field
    x: str                  # x's name in the row (points, staircase, named points)
    y: str                  # y's name
    lanes: tuple = ()       # (x's lane, y's lane) of a cost vector; () = currencies()' ops and bytes

    @property
    def names(self):
        """the two keys of a mix record (front.mix_record): ops and bytes, or the lanes' names"""
        return ("ops", "bytes") if not self.lanes else (self.x, self.y)

    def measure(self, t):
        """a cost vector's two values, keyed by names"""
        if not self.lanes:
            return currencies(t)
        return {self.x: t[self.lanes[0]], self.y: t[self.lanes[1]]}

    def view(self, pb):
        """the problem whose "ops" and "bytes" hold x and y"""
        return pb if not self.lanes else pb.pair(*self.lanes)


# the fronts search-mix --front walks: no --cost-model, wops against bytes; a bill model, its read units against its write units.
# A result file can hold both: a reader of the wops-vs-bytes front selects its rows by this model, never by question alone
FRONT_WOPS_BYTES = "wops,bytes"
FRONT_AXES = {None: Axes(FRONT_WOPS_BYTES, "wops", "bytes"),
              **{m: Axes(f"{m}:read,write", "read_units", "write_units", lanes) for m, lanes in CM.BILL_UNITS.items()}}
# the write prices a complete read/write front names its cheapest point at (bill = read + price x write units)
BILL_R = (0.1, 0.3, 1, 3, 5, 10, 30, 100, 300)


def axes_of(model):
    """the front's axes under --cost-model (None: wops against bytes); another model is refused by name"""
    if model not in FRONT_AXES:
        raise SystemExit(f"--front --cost-model {model}: a front is wops against bytes (no --cost-model) or a bill form's read units "
                         f"against its write units ({', '.join(m for m in FRONT_AXES if m)})")
    return FRONT_AXES[model]


def _lane(key):
    """a cost vector's lane as an extra lane's function"""
    return lambda t: t[key]


def extra_for(model):
    """the extra lanes a problem for `model` holds: the sum always (the named points read it); for a bill form, its
    cost and its two unit lanes (cost_models.BILL_UNITS: its front's axes), which exist only under Composer(bill="bounds"
    or "sized"): cost_models.needs_bill. One set per model, whatever is asked of it (the fewest, a front, a staircase):
    the lanes are part of the saved cost cache's key, so one build answers each of them"""
    out = {"sum": EXTRA["sum"]}
    if model in CM.BILL_UNITS:
        out[MODELS[model]] = EXTRA[MODELS[model]]
        out.update({k: _lane(k) for k in CM.BILL_UNITS[model]})
    return out


TOL = 1e-6                                            # the re-price check: relative, as front.complete_front's


def build(w, data, units, jobs=None, model="wops"):
    """the workload's Problem with the lanes `model` needs (extra_for; the one builder and its cache)"""
    return S.problems([(w, data, units, extra_for(model))], S.jobs() if jobs is None else jobs)[0]


def reference_bytes(w, data, units, axes=None):
    """B = 1: B_UNIT's bytes, priced from the captures (as workload_story prices it); with axes, B_UNIT's y"""
    t = cost_mix(w, data, S.b_unit_layout(units))
    return currencies(t)["bytes"] if axes is None else axes.measure(t)[axes.names[1]]


def uniforms(pb):
    """the uniform layouts of the problem's three settings, as search-order combos"""
    return {(p, a): pb.from_assign(S.uniform(pb.parts, p, a)) for p in LAYOUTS for a in LAYOUTS}


def _view(pb, model):
    """(the problem the model is solved on, the solve's primary)"""
    lane = MODELS[model]
    if lane == "bytes":
        return pb, "bytes"
    return (pb if lane == "ops" else pb.under(lane)), "ops"


def _key(pb, primary, combo):
    o, b = pb.total(combo)
    return (o, b) if primary == "ops" else (b, o)


def _per_query_at(pb, lane, combo):
    """each query's one-run cost on `lane` at a full assignment (search order), from the unweighted cost cache"""
    out = []
    for q in pb.queries:
        idx = 0
        for p in q["parts"]:
            idx = idx * pb.size[p] + combo[p]
        out.append(q["one_run"][lane][-1][idx])
    return out


def _per_query_uniform(pb, lane):
    """{query index: {uniform cell: its one-run cost on `lane`}} (query_weighting.additive_reference reads it)"""
    out = {i: {} for i in range(len(pb.queries))}
    for (p, a), combo in uniforms(pb).items():
        for i, c in enumerate(_per_query_at(pb, lane, combo)):
            out[i][cell_id(p, a)] = c
    return out


def lanes(data, w, assign):
    """the mix's whole cost vector, every channel and measure the pricing keeps (compose.Composer.keys: S, G, N, P, D,
    each byte lane, the read and write measures, the bill's units when priced for the bill), as cost_mix sums it"""
    t = cost_mix(w, data, assign)
    return None if t is None else {k: t[k] for k in data.composer.keys}


def mix_cost(data, w, assign, model):
    """(totals: wops, bytes, the model's value; totals per part; lanes) of one mix, priced from the captures"""
    c, pc = F.cost_by_part(data, w, assign)
    tot = {"wops": c["ops"], "bytes": c["bytes"]}
    vec = lanes(data, w, assign)
    if MODELS[model] not in ("ops", "bytes"):
        tot[model] = EXTRA[MODELS[model]](vec)
    return tot, {str(u): v for u, v in sorted(pc.items(), key=lambda kv: str(kv[0]))}, vec


def _check(name, got, want):
    if abs(got - want) > TOL * max(1.0, abs(want)):
        raise SystemExit(f"{name}: priced again {got!r}, the cost cache says {want!r}: the cost cache and the captures disagree")


def best_mix(pb, data, w, model, within=None, ref_bytes=None, max_nodes=None, trace=None):
    """Q1 (within None) or Q2 (fewest of the model within B x ref_bytes): one row's fields (the caller adds the
    identity). A limit no mix fits answers with the fewest-bytes mix, fits False (criteria.budget_pick's fallback).
    trace (a list): the main solve's decisions (search.solve's trace), read by `kvcost search-mix --trace`; when no mix
    fits the limit, one more row says the answer came from the fewest-bytes solve, which is not traced."""
    if model not in MODELS:
        raise SystemExit(f"model {model!r}: one of {', '.join(MODELS)}")
    if within is not None and MODELS[model] == "bytes":
        raise SystemExit("a byte limit on the fewest bytes asks nothing: --byte-limit takes wops or wops_bytes4k")
    form = data.composer.weighting
    if form != "sum" and within is not None:
        raise SystemExit(f"--byte-limit under the {form} weighting: a byte limit is a sum over queries; the {form} form is a "
                         f"question without a limit")
    view, primary = _view(pb, model)
    flat, lane, reference = view, ("ops" if primary == "ops" else "bytes"), None
    if form != "sum":                                  # the same cost cache, each query's through the form (Problem.weighted)
        per_query = _per_query_uniform(flat, lane)
        ref_costs = None
        if form == "additive":
            reference = QW.additive_reference(per_query)
            ref_costs = [per_query[i][reference] for i in range(len(flat.queries))]
        view = flat.weighted(form, ref_costs)
    cap = None if within is None else within * ref_bytes
    times, t0 = {}, time.time()
    singles = uniforms(view)
    fits = {c: s for c, s in singles.items() if cap is None or view.total(s)[1] <= cap * (1 + 1e-9)}
    best_uniform = min(fits, key=lambda c: _key(view, primary, fits[c])) if fits else None
    start, two, used = None, None, None
    if best_uniform is not None:
        start, two = S.two_step(view, fits[best_uniform], primary)
        times["two_step_a_s"], times["two_step_b_s"] = two["a_s"], two["b_s"]    # (a) each part alone, (b) one-part moves
        used = "the two-step mix"
        if cap is not None and view.total(start)[1] > cap * (1 + 1e-9):
            start, used = fits[best_uniform], "the best uniform layout"   # the two-step mix does not fit
    times["start_s"] = round(time.time() - t0, 3)
    t1, info = time.time(), {}
    assign, o, b, nodes, _ = S.solve(view, primary, cap, start, max_nodes=max_nodes, info=info, trace=trace)
    times["solve_s"] = round(time.time() - t1, 3)
    fits_limit, proven = assign is not None, bool(info.get("proven", True))
    if assign is None:                                 # no mix fits the limit: the fewest-bytes mix (budget_pick's rule)
        more_info = {}                                 # its own proof: "nothing fits" is proven only if both solves are
        assign, _, _, more, _ = S.solve(pb, "bytes", None, None, max_nodes=max_nodes, info=more_info)
        if trace is not None:                          # that solve is not traced: say so where the reader looks
            trace.append(S.trace_row(pb, "no mix fits the limit: the answer is the fewest-bytes mix (a second search, "
                                     f"{more} nodes, not traced)", nodes, None, None, [], None, None))
        nodes += more
        proven = proven and bool(more_info.get("proven", True))
    t2 = time.time()
    tot, per_part, vec = mix_cost(data, w, assign, model)
    combo = pb.from_assign(assign)
    _check("wops", tot["wops"], pb.total(combo)[0])
    _check("bytes", tot["bytes"], pb.total(combo)[1])
    if MODELS[model] not in ("ops", "bytes") and form == "sum":
        _check(model, tot[model], view.total(view.from_assign(assign))[0])     # the model's lane: view's "ops" lane
    score = None
    if form != "sum":                                  # the search total in the form's units, checked per query
        # the solved lane's total: view.total gives (ops, bytes), and the fewest bytes solves the bytes lane
        value = QW.score(form, view.total(view.from_assign(assign))[0 if primary == "ops" else 1], view.offsets[lane],
                         len(flat.queries))
        costs = _per_query_at(flat, lane, flat.from_assign(assign))
        want = (math.exp(math.fsum(math.log(c) for c in costs) / len(costs)) if form == "geomean" else
                math.fsum(c / r for c, r in zip(costs, ref_costs)) / len(costs))
        _check(f"{form} score", value, want)
        score = {"form": form, "value": value, "queries": len(costs),
                 **({"additive_reference": reference} if reference else {})}
    times["cost_s"] = round(time.time() - t2, 3)
    return {"question": "fewest" if within is None else "within", "model": model, "limit": within,
            "score": score, "fits": fits_limit, "proven": proven, "max_nodes": max_nodes, "nodes": nodes,
            "dominated": info.get("dominated"), "totals": tot, "per_part": per_part, "lanes": vec,
            "assignment": R.row_assignment(assign),
            "bytes_reference": B_UNIT, "reference_bytes": ref_bytes,
            "start": {"best_uniform": "/".join(best_uniform) if best_uniform else None, "used": used,
                      "two_step": None if two is None else {"total": list(two["total"]), "moves": two["moves"],
                                                            "start_total": list(two["start_total"])}},
            "times": times}


def best_mix_no_cost_cache(data, w, units, model, within=None, ref_bytes=None, max_nodes=None, fewest_if_none=True):
    """best_mix without the cost cache (layout_search/bodies.py): for a workload whose per-query costs do not fit (JOB). The
    same row, started from the two-step mix of the combined panel, plus solver "no-cost-cache" and the bound: the root
    bound in the score's units (Bodies.solve's info: the lowest any mix could be, from what the search left undone) and the
    gap 1 - bound / found. proven only if the search finished; a node limit leaves the gap open. A query the fast path
    cannot price goes through query_pick; a capture query_pick refuses stops the run only where the search meets it.
    With a limit the two-step mix is over, the start is Bodies.fit_start's (the row's start says which and its steps).
    No mix within the limit: the fewest-bytes mix instead, or (fewest_if_none=False, the staircase) a row with fits
    false, proven, nodes and times only"""
    from kvcost.layout_search import bodies as BD
    if model not in BD.LINEAR:
        raise SystemExit(f"the solve without the cost cache takes a model linear in the cost vector ({', '.join(BD.LINEAR)}), not {model!r}")
    if within is not None and MODELS[model] == "bytes":
        raise SystemExit("a byte limit on the fewest bytes asks nothing: --byte-limit takes wops or wops_bytes4k")
    form = data.composer.weighting
    if form != "sum" and within is not None:
        raise SystemExit(f"--byte-limit under the {form} weighting: a byte limit is a sum over queries; the {form} form is a "
                         f"question without a limit")
    times, t0 = {}, time.time()
    G, ref_costs = BD.additive_reference(w, data, units, model) if form == "additive" else (None, None)
    bd = BD.Bodies(w, data, units, model=model, form=form, ref_costs=ref_costs)
    # a query outside the fast path's case (held-out rows, a measure-only body) is priced by
    # compose.query_pick itself: slower, the same bound (the picked body is one of the valid bodies, whatever the rule)
    slow = sum(not q["fast"] for q in bd.queries)
    times["bodies_s"] = round(time.time() - t0, 3)
    cap = None if within is None else within * ref_bytes
    t1 = time.time()
    start, two = bd.two_step()
    times["two_step_a_s"], times["two_step_b_s"] = two["a_s"], two["b_s"]    # (a) at fixed plans, (b) plans re-picked
    used, fit_steps = "the two-step mix", None
    if cap is not None and bd.total(start)[1] > cap * (1 + 1e-9):     # the two-step mix does not fit: moved within it
        start, used, fit_steps = bd.fit_start(cap, start)
    times["start_s"] = round(time.time() - t1, 3)
    t2, info = time.time(), {}
    assign, o, b, nodes, _ = bd.solve(cap, start, max_nodes=max_nodes, info=info)
    times["solve_s"] = round(time.time() - t2, 3)
    fits_limit, proven = assign is not None, bool(info["proven"])
    if assign is None and not fewest_if_none:      # the staircase: no mix within the limit (proven only if finished)
        return {"fits": False, "proven": proven, "nodes": nodes, "times": times}
    if assign is None:                                 # no mix fits the limit: the fewest-bytes mix (budget_pick's rule)
        fewest, more_info = BD.Bodies(w, data, units, model="bytes"), {}
        assign, fb, fo, more, _ = fewest.solve(start=fewest.two_step()[0], max_nodes=max_nodes, info=more_info)
        nodes += more
        proven = proven and bool(more_info["proven"])
    t3 = time.time()
    tot, per_part, vec = mix_cost(data, w, assign, model)
    n = len(bd.queries)
    other = "bytes" if MODELS[model] != "bytes" else "wops"
    if form == "sum":
        if fits_limit:                                 # both lanes, as best_mix checks them against the cost cache
            _check(model, tot[model], o)
            _check(other, tot[other], b)
        else:
            _check("bytes", tot["bytes"], fb)
            _check("wops", tot["wops"], fo)
        found, bound, score = o, info["bound"], None
        ceiling = info["ceiling"]
    else:                                              # the search total in the form's units, checked per query
        costs = bd.per_query(bd.from_assign(assign))
        want = (math.exp(math.fsum(math.log(c) for c in costs) / n) if form == "geomean" else
                math.fsum(c / r for c, r in zip(costs, ref_costs)) / n)
        found = math.exp(o / n) if form == "geomean" else o / n
        _check(f"{form} score", found, want)
        bound = math.exp(info["bound"] / n) if form == "geomean" else info["bound"] / n
        ceiling = math.exp(info["ceiling"] / n) if form == "geomean" else info["ceiling"] / n
        score = {"form": form, "value": found, "queries": n, **({"additive_reference": G} if G else {})}
    times["cost_s"] = round(time.time() - t3, 3)
    return {"question": "fewest" if within is None else "within", "model": model, "limit": within,
            "score": score, "fits": fits_limit, "proven": proven, "max_nodes": max_nodes, "nodes": nodes,
            "dominated": None, "totals": tot, "per_part": per_part, "lanes": vec,
            "assignment": R.row_assignment(assign),
            "bytes_reference": B_UNIT, "reference_bytes": ref_bytes, "solver": "no-cost-cache", "queries_by_query_pick": slow,
            "bound": {"value": bound, "gap": (1 - bound / found) if found and fits_limit else None, "root": ceiling,
                      "what": "the lowest any mix could be: proven, the answer; else the lowest of the answer and the bounds "
                              "of the parts of the search not done (each query at its cheapest plan valid there, each "
                              "free part at its cheapest setting); root: the same with nothing decided"},
            "start": {"best_uniform": two["B"], "used": used,
                      "two_step": {"total": two["b_total"], "after_each_part_alone": two["a_total"],
                                   "start_total": two["B_total"], "moves_tried": two["tried"],
                                   "fixed_plan_s": two["a_s"], "repick_s": two["b_s"]},
                      **({} if fit_steps is None else {"fit_steps": fit_steps})},
            "times": times}


_TABLE_FREE = {}                 # the staircase's inputs, for its forked workers (set before the pool forks)


def _no_cost_cache_limit(cap):
    t = _TABLE_FREE
    try:
        return best_mix_no_cost_cache(t["data"], t["w"], t["units"], t["model"], cap / t["ref"], t["ref"], t["max_nodes"],
                                   fewest_if_none=False)
    except SystemExit as e:          # which limit refused (at_a_time carries a worker's refusal to the caller)
        raise SystemExit(f"the staircase without the cost cache: the limit {cap:.6g} bytes: {e}") from None


def _no_cost_cache_entry(cap, kind, ref_bytes, r):
    """one staircase entry from a limit's best_mix_no_cost_cache row (front()'s entry, plus the solve's start and times)"""
    entry = {"cap_bytes": cap, "cap_kind": kind, "cap_x_reference": cap / ref_bytes, "wops": None, "bytes": None,
             "proven": r["proven"], "nodes": r["nodes"], "assignment": None, "lanes": None, "bound": None,
             "start": None, "times": r["times"]}
    if r["fits"]:
        entry.update(wops=r["totals"]["wops"], bytes=r["totals"]["bytes"], assignment=r["assignment"],
                     lanes=r["lanes"], bound=r["bound"]["value"],
                     start={k: r["start"][k] for k in ("used", "fit_steps") if k in r["start"]})
    return entry


def staircase_no_cost_cache(data, w, units, model, limits, ref_bytes, max_nodes, jobs=1):
    """front(..., walk=False) without the cost cache (JOB): the staircase, one best_mix_no_cost_cache per limit (its start
    Bodies.fit_start's, never the previous limit's: the limits run apart), `jobs` limits at a time in forked processes.
    The row is front()'s staircase row; an entry's bound is the solve's (the lowest wops any mix within the limit can
    have), and its start says what the solve started from. Limits from parse_limits, but not caps (it reads the cost cache)"""
    if model != "wops":
        raise SystemExit(f"the staircase without the cost cache is wops against bytes (no --cost-model), not {model}")
    if limits == "caps":
        raise SystemExit("--limits caps reads the cost cache (front.caps_of); the staircase without the cost cache takes file:, named, "
                         "a list of B or bytes:")
    caps = parse_limits(limits, None, units, ref_bytes)
    t0 = time.time()
    _TABLE_FREE.update(data=data, w=w, units=units, model=model, ref=ref_bytes, max_nodes=max_nodes)
    order = sorted(caps)
    try:
        rows = S.at_a_time(_no_cost_cache_limit, order, jobs)
    finally:
        _TABLE_FREE.clear()
    stair = [_no_cost_cache_entry(cap, caps[cap], ref_bytes, r) for cap, r in zip(order, rows)]
    named = {_named(st["cap_kind"]): {"wops": st["wops"], "bytes": st["bytes"], "assignment": st["assignment"]}
             for st in stair if _named(st["cap_kind"]) and st["wops"] is not None}
    return {"question": "staircase", "model": FRONT_AXES[None].model, "limit": limits,     # front()'s: one key
            "fits": any(st["wops"] is not None for st in stair),
            "proven": all(st["proven"] for st in stair), "max_nodes": max_nodes,
            "nodes": sum(st["nodes"] for st in stair), "steps": None, "unproven_steps": None,
            "walk_complete": None, "walk_seconds": None, "points": [], "named": named, "staircase": stair,
            "bytes_reference": B_UNIT, "reference_bytes": ref_bytes, "solver": "no-cost-cache", "jobs": jobs,
            "times": {"staircase_s": round(time.time() - t0, 3)}}


def lagrange(pb, max_nodes=None):
    """the Lagrange bounds of a capped fewest-wops solve (search.lagrange_bounds): nine multipliers around the front's
    mean slope (wops per byte between the fewest-bytes and the fewest-wops mix), as the chunk-size front set them. They
    only add lower bounds, so every answer stays the same and fewer nodes are searched (any multiplier >= 0 gives a
    bound, so a node-limited end solve still gives valid ones). Returns (the bounds or None, the nodes its solves took)"""
    _, o_lo, b_lo, n_lo, _ = S.solve(pb, "bytes", max_nodes=max_nodes)
    _, o_hi, b_hi, n_hi, _ = S.solve(pb, "ops", max_nodes=max_nodes)
    m0 = (o_lo - o_hi) / (b_hi - b_lo) if b_hi > b_lo and o_lo > o_hi else 0.0
    return (S.lagrange_bounds(pb, [m0 * 4.0 ** k for k in range(-4, 5)]) if m0 > 0 else None), n_lo + n_hi


def _limit_points(points, cap, axes=FRONT_AXES[None]):
    """the walk's answer at a limit on y (bytes): the fewest-x point within it (points: fewest y first), else None"""
    x, y = axes.x, axes.y
    inside = [p for p in points if p[y] <= cap * (1 + 1e-9)]
    return min(inside, key=lambda p: (p[x], p[y])) if inside else None


def _named(kind):
    """a limit's name as a named point (B=<b>), or None: parse_limits' B=<b> and caps_of's reference_x<b> (F.B_KIND)"""
    if kind.startswith("B="):
        return kind
    if kind.startswith(F.B_KIND):
        return f"B={float(kind[len(F.B_KIND):]):g}"
    return None


BYTE_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30}


def parse_byte_limit(text):
    """--byte-limit: '2x' (a multiple of the reference Sp-Co's bytes) -> (2.0, None); '68MB' -> (None, 68e6)"""
    t = text.strip()
    m = re.fullmatch(r"([0-9.]+)x", t)
    if m:
        return float(m.group(1)), None
    m = re.fullmatch(r"([0-9.]+)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB)", t)
    if m:
        return None, float(m.group(1)) * BYTE_UNITS[m.group(2)]
    raise SystemExit(f"--byte-limit {text}: a multiple of Sp-Co's bytes (2x) or a size (68MB, 1.5GiB)")


def parse_limits(limits, pb, units, ref_bytes):
    """{cap in bytes: its kind} of --limits: caps (front.caps_of: the sweep's limits, the singles, the midpoint, the
    named B), named (B = 1, 2, 3, 5), a list of byte limits (1x,2x,3x or 1,2,3: multiples of the reference, named
    B=<b>; 68MB: a size, kind bytes), bytes:<cap>,<cap>,... (limits in bytes), or file:<path> (a
    JSON file whose "limits" list holds {cap_bytes, cap_kind}, or {B, cap_kind}: B x ref_bytes, so a limit named for
    a multiple of the reference is that multiple exactly; a fixed set of limits kept as an input)"""
    if str(limits).startswith("file:"):
        with open(limits[len("file:"):]) as f:
            caps = {float(x["cap_bytes"]) if "cap_bytes" in x else float(x["B"]) * ref_bytes: x["cap_kind"]
                    for x in json.load(f)["limits"]}
        if not caps:
            raise SystemExit(f"--limits {limits}: no limits in the file")
        return caps
    if limits == "caps":
        return F.caps_of(pb, units, plain=False)[1]      # every option: with other chunk sizes the ends reach further
    if limits == "named":
        return {b * ref_bytes: f"B={b:g}" for b in S.BUDGETS}
    if str(limits).startswith("bytes:"):
        return {float(x): "bytes" for x in limits[len("bytes:"):].split(",") if x}
    caps = {}
    for x in str(limits).split(","):
        b, size = (float(x), None) if re.fullmatch(r"\s*[0-9.]+\s*", x) else parse_byte_limit(x)
        caps[b * ref_bytes if size is None else size] = f"B={b:g}" if size is None else "bytes"
    return caps


_STAIR = {}                      # the cost cache staircase's inputs, for its forked workers (set before the pool forks)


def _stair_limit(limit):
    """(one staircase entry on the cost cache, its answer as a search-order combo or None): the fewest x within the
    limit `cap` on y, started from the best uniform layout that fits it"""
    cap, kind = limit
    t = _STAIR
    pb, x, y = t["pb"], t["axes"].x, t["axes"].y
    fits = [s for s in t["starts"] if pb.total(s)[1] <= cap * (1 + 1e-9)]
    start = min(fits, key=lambda s: pb.total(s)) if fits else None
    info = {}
    try:        # plain=False: every option the problem holds (at three settings per part it is the plain solve)
        assign, o, b, n, _ = S.solve(pb, "ops", cap, start, plain=False, lagrange=t["lag"], max_nodes=t["max_nodes"],
                                     info=info)
    except SystemExit as e:          # which limit refused (at_a_time carries a worker's refusal to the caller)
        raise SystemExit(f"the staircase: the limit {cap:.6g} {y}: {e}") from None
    return ({f"cap_{y}": cap, "cap_kind": kind, "cap_x_reference": cap / t["ref"],
             x: None if assign is None else o, y: None if assign is None else b,
             "proven": bool(info.get("proven", True)), "nodes": n,
             # the lowest x any mix within the limit can have: the answer where proven (the cost cache solve
             # keeps no bound when a node limit stops it: None)
             "bound": (o if assign is not None else None) if info.get("proven", True) else info.get("bound"),
             "assignment": None if assign is None else R.row_assignment(assign),
             "lanes": None if assign is None else lanes(t["data"], t["w"], assign)},
            None if assign is None else pb.from_assign(assign))


def front(pb, data, w, units, ref_bytes, max_nodes=None, limits=None, walk=True, progress_every=200, walk_seconds=None,
          axes=FRONT_AXES[None], jobs=1):
    """Q3: the complete front by the walk (front.complete_front: one capped solve per point), the named points read
    off it (fewest wops, fewest bytes, within B x ref_bytes for the named B), and, with `limits` (parse_limits), the
    staircase: one capped solve per limit, each equal to the walk's answer at that limit (refused if not). walk=False:
    the staircase only (its named points read off the staircase where it holds them). The staircase runs first: each
    limit with the Lagrange bounds (lagrange()) and the best uniform layout that fits it as a start (_stair_limit; never
    the previous limit's answer: the limits run apart, `jobs` at a time in forked processes, and the row is the same at
    any `jobs`), and its answers are starts
    for the walk (an answer at a smaller limit fits a step's cap). walk_seconds: the walk stops after that wall time,
    and the row says the front is incomplete (walk_complete false, the points found so far).
    axes (Axes): the two currencies, x against y; wops against bytes by default, a bill form's read units against its
    write units (pb holds their lanes: extra_for); ref_bytes is then B_UNIT's y. A complete
    read/write front also names its cheapest point at each of BILL_R"""
    x, y = axes.x, axes.y
    if axes.lanes and not ref_bytes > 0:              # no writes: every mix has 0 write units, the front is one point
        raise SystemExit(f"a {x} against {y} front: {B_UNIT} has {ref_bytes!r} {y}, so the workload writes nothing and "
                         f"the front is one point; ask for the fewest of the bill instead (search-mix --cost-model, no --front)")
    pb = axes.view(pb)
    nx, ny = axes.names                                # the walk's record keys (front.mix_record)
    t0, stats = time.time(), {}
    starts = list(uniforms(pb).values())
    stair, found = [], []
    times, lag_nodes = {}, 0
    if limits:
        lag, lag_nodes = lagrange(pb, max_nodes)
        caps = parse_limits(limits, pb, units, ref_bytes)
        _STAIR.update(pb=pb, axes=axes, starts=starts, lag=lag, max_nodes=max_nodes, ref=ref_bytes, data=data, w=w)
        try:
            solved = S.at_a_time(_stair_limit, sorted(caps.items()), jobs)
        finally:
            _STAIR.clear()
        stair = [entry for entry, _ in solved]
        found = [combo for _, combo in solved if combo is not None]
        times["staircase_s"] = round(time.time() - t0, 3)
    t1 = time.time()
    mixes, nodes = (F.complete_front(pb, data, w, starts + found, progress_every=progress_every, max_nodes=max_nodes,
                                     stats=stats, plain=False, seconds=walk_seconds,
                                     axes=None if not axes.lanes else axes)
                    if walk else ([], 0))
    points = [{x: m[nx], y: m[ny], "assignment": R.row_assignment({tuple(k.split("|")): v for k, v in m["assign"].items()}),
               "per_part": m["units"]} for m in mixes]
    complete = walk and not stats.get("stopped") and stats.get("unproven", 0) == 0
    if walk:
        times["walk_s"] = round(time.time() - t1, 3)
    named = {}
    if complete:
        named[f"fewest {x}"] = min(points, key=lambda p: (p[x], p[y]))
        named[f"fewest {y}"] = min(points, key=lambda p: (p[y], p[x]))
        for b in S.BUDGETS:
            named[f"B={b:g}"] = _limit_points(points, b * ref_bytes, axes)
        if axes.lanes:                                 # a bill form: its cheapest point at each write price
            for r in BILL_R:
                named[f"cheapest at r={r:g}"] = min(points, key=lambda p: (CM.bill_at(p[x], p[y], r), p[y]))
        for st in stair:                               # every proven staircase answer equals the walk's at its limit
            if not st["proven"]:
                continue
            at = _limit_points(points, st[f"cap_{y}"], axes)
            if (st[x] is None) != (at is None):
                raise SystemExit(f"at {st[f'cap_{y}']:.6g} {y} ({st['cap_kind']}) the staircase "
                                 f"{'finds no mix' if st[x] is None else 'finds a mix'}, the walk {'none' if at is None else 'one'}")
            if st[x] is not None and abs(st[x] - at[x]) > TOL * max(1.0, at[x]):
                raise SystemExit(f"the staircase at {st[f'cap_{y}']:.6g} {y} ({st['cap_kind']}) gives {st[x]!r} {x}, "
                                 f"the walk {at[x]!r}")
    else:                                         # the named points the staircase holds (its B limits)
        if walk and points and not stats.get("unproven"):     # a walk stopped by time: its first point is still exact
            named[f"fewest {x}"] = min(points, key=lambda p: (p[x], p[y]))
        named.update({_named(st["cap_kind"]): {x: st[x], y: st[y], "assignment": st["assignment"]}
                      for st in stair if _named(st["cap_kind"]) and st[x] is not None})
    nodes += sum(st["nodes"] for st in stair) + lag_nodes
    proven = (complete or not walk) and all(st["proven"] for st in stair)
    return {"question": "front" if walk else "staircase", "model": axes.model, "limit": limits,
            "fits": bool(points) or any(st[x] is not None for st in stair),
            "proven": proven, "max_nodes": max_nodes, "nodes": nodes,
            "steps": stats.get("steps"), "unproven_steps": stats.get("unproven"),
            "walk_complete": complete if walk else None, "walk_seconds": walk_seconds,
            "points": points, "named": named, "staircase": stair,
            f"{y}_reference": B_UNIT, f"reference_{y}": ref_bytes, "times": times}
