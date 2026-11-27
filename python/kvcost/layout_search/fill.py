"""The vectorised cost cache fill: one query's cost under EVERY assignment of its parts at once, equal entry for entry to the
scalar fill (search.Problem.fill_cost_cache, one compose.query_cost call per entry).

A query's cost under an assignment is its picked body's vector, composed from per-part rows (compose._composed): per
param, per part (sorted, then the index), the row of the part's source under its layout (a cell, or a Chunked part's
chunk size). So each body's vector over all of a query's costs is a sum of per-part arrays broadcast over their axes,
added in _composed's order, so every entry gets the same float operations as the scalar path (byte-identical). The pick
is then taken over arrays: an entry uniform on the query's parts at Split and Condensed takes the cell's pick (one
scalar query_pick per cell, so it follows whatever rule the cell has);
any other entry the held-out cheapest with no anchor (compose.query_pick's decide with no planner: candidates.held_out_pick,
ties in candidates.tie_order), or, with no held-out rows, the in-sample cheapest (candidates.pick_body: ties by sorted
name), one usable body, or the write plan.

Anything else takes the scalar path, query by query (vector_cost_cache returns None): a measure_only_where body, a missing part row (the scalar path raises there), store mode, a criterion that is not a
linear score, an entry whose pick the arrays cannot decide. The check: tests/gates/check_cost_cache_fill.py.
"""
import numpy as np

from kvcost.layout_cost import compose as P
from kvevaluator.layouts import cell_id
from kvcost.layout_cost.cost_models import currencies
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plan_choice.optimizer.criteria import parse_criterion, resolve_criterion

INF = float("inf")
NAMES = sorted(P.LAYOUTS)            # "chunked" < "condensed" < "split": the order a set's min() follows in _composed
BIT = {l: 1 << i for i, l in enumerate(NAMES)}
SCORE_KEYS = ("S", "G", "N") + tuple(C.READ_LANES)
COST_KEYS = ("read_ops", "write_rmw_ops", "write_ops", "read_bytes", "write_rmw_bytes", "write_bytes")


def _low(mask):
    """the name of the lowest set bit (min() over the set of layouts)"""
    return NAMES[(mask & -mask).bit_length() - 1]


def _layouts(mask):
    return [l for l in NAMES if mask & BIT[l]]


def _score_fn(criterion):
    """the pick's scalar of a {S, G, N, total_bytes} vector, the same code pick_body runs (scalar, lane and bytes specs:
    plain arithmetic, so arrays flow through it); None for a criterion that is not one (the scalar path then)"""
    _, spec = resolve_criterion(criterion or C.DEFAULT_CRITERION)
    if not (spec.startswith("scalar:") or spec.startswith("lane:") or spec == "bytes"):
        return None
    return parse_criterion(spec).score


def vector_cost_cache(d, params, ps, opts, extra=None, mode="option"):
    """(ops, bytes, {extra name: values}) over every assignment of the query's parts `ps` (search order), each part's
    options `opts[j]`, flat in mixed radix (the scalar fill's order), INF where no plan is usable; None when this query
    must take the scalar path"""
    composer = d["composer"]
    score = _score_fn(composer.criterion)
    if mode != "option" or score is None or d.get("measure_only_where"):
        return None
    shape = tuple(len(o) for o in opts)
    n = int(np.prod(shape, dtype=np.int64))
    k = len(ps)
    digit = [np.arange(s).reshape([s if a == j else 1 for a in range(k)]) for j, s in enumerate(shape)]

    def per_entry(values, j):
        """an array over the query's costs: part j's option-indexed values broadcast along its axis"""
        return np.asarray(values)[digit[j]]

    # which validity layouts the entry puts together, per side (compose.query_pick's playouts / alayouts as bit masks)
    props = [j for j, u in enumerate(ps) if u[0] == "props"]
    adjs = [j for j, u in enumerate(ps) if u[0] == "adj"]
    full = sum(BIT.values())
    pmask = np.full(shape, 0 if props else full, dtype=np.int64)
    amask = np.full(shape, 0 if adjs else full, dtype=np.int64)
    for j in props:
        pmask = pmask | per_entry([BIT[P.validity_layout(o, composer)] for o in opts[j]], j)
    for j in adjs:
        amask = amask | per_entry([BIT[P.validity_layout(o, composer)] for o in opts[j]], j)
    sized = np.zeros(shape, dtype=bool)
    for j in range(k):
        sized = sized | per_entry([P.is_sized(o, composer) for o in opts[j]], j)
    pmask, amask = np.broadcast_to(pmask, shape), np.broadcast_to(amask, shape)
    combos = sorted({(int(p), int(a)) for p, a in zip(pmask.reshape(-1), amask.reshape(-1))})
    for oid in sorted({o for oo in opts for o in oo if P.is_sized(o, composer)}):
        if d.get("fam") in d["sized_missing"].get(oid, ()):
            return None                                   # the scalar path refuses it
    bodies = sorted(set().union(*(d["valid"].get(c, set()) for c in P.CELLS)) | {v for (_, v, _) in d.get("cost", {})})

    def pairs(pm, am):
        return [cell_id(p, a) for p in _layouts(pm) for a in _layouts(am)]

    combo_of = {c: i for i, c in enumerate(combos)}
    lookup = np.full(64, -1, dtype=np.int64)          # (pmask, amask) -> its combo's index, by array lookup
    for (pm, am), ci in combo_of.items():
        lookup[pm * 8 + am] = ci
    cidx = lookup[pmask * 8 + amask]
    single = np.array([bin(m).count("1") == 1 for m in range(8)])
    low = np.array([NAMES.index(_low(m)) if m else 0 for m in range(8)])
    lp_idx, la_idx = low[pmask], low[amask]            # per entry: min() of its props layouts, of its adj layouts

    def use_masks(valid):
        """{body: the entries where it is valid in every pair the entry puts together}, by a lookup per combo"""
        out = {}
        for v in bodies:
            ok = np.zeros(len(combos), dtype=bool)
            for (pm, am), ci in combo_of.items():
                ok[ci] = all(v in valid.get(c, set()) for c in pairs(pm, am))
            out[v] = ok[cidx]
        return out

    use = use_masks(d["valid"])

    def compose(cost_key, prm, keys, want):
        """{body: {key: array over the query's costs}} composed from `cost_key` rows over params `prm`, for the bodies in `want`
        ({body: bool array of the entries it is needed at}); None on a missing row an entry needs (the scalar path)"""
        rows = d.get(cost_key, {})
        w = 1.0 / len(prm)
        out = {}
        units = sorted(ps)
        for v, need in want.items():
            if not need.any():
                continue
            acc = {m: np.zeros(shape) for m in keys}
            for i in prm:
                for u in units + [None]:
                    # the part's source cell per (its option, the entry's min layout on the other side): a small table,
                    # gathered over the entries (the index: per (min props, min adj))
                    if u is None:
                        src = [[cell_id(lp, la) for la in NAMES] for lp in NAMES]
                        ix = (lp_idx, la_idx)
                    else:
                        j = ps.index(u)
                        if u[0] == "props":
                            src = [[o if P.is_sized(o, composer) else cell_id(o, la) for la in NAMES] for o in opts[j]]
                            ix = (np.broadcast_to(digit[j], shape), la_idx)
                        else:
                            src = [[o if P.is_sized(o, composer) else cell_id(lp, o) for lp in NAMES] for o in opts[j]]
                            ix = (np.broadcast_to(digit[j], shape), lp_idx)
                    tab = {m: np.zeros((len(src), len(NAMES))) for m in keys}
                    hole = np.zeros((len(src), len(NAMES)), dtype=bool)
                    for r0, line in enumerate(src):
                        for c0, sc in enumerate(line):
                            r = rows.get((sc, v, i))
                            vec = (r or {}).get(u)
                            if r is None or (vec is None and u is not None and P.is_sized(sc, composer)
                                             and rows.get((P.CH_CH, v, i), {}).get(u)):
                                hole[r0, c0] = True   # the scalar path raises if an entry that needs it is priced
                            elif vec:
                                for m in keys:
                                    tab[m][r0, c0] = vec[m]
                    if hole.any() and (need & hole[ix]).any():
                        return None
                    for m in keys:
                        acc[m] += w * tab[m][ix]          # add(tot, vec, 1 / len(params)): the same float operations
            out[v] = acc
        return out

    def scores_of(vecs):
        out = {}
        for v, t in vecs.items():
            out[v] = score(P._scalar(t))
        return out

    pp = params
    anyuse = {v: use[v] for v in bodies}
    keys = sorted(composer.keys) if extra else list(COST_KEYS)
    need_keys = sorted(set(keys) | set(SCORE_KEYS))
    vec = compose("cost", pp, need_keys, anyuse)
    if vec is None:
        return None
    mo = set(d.get("measure_only_registry", ()))
    nuse = sum(use[v].astype(np.int64) for v in bodies) if bodies else np.zeros(shape, dtype=np.int64)
    pool = {v: use[v] for v in bodies if v not in mo and v in vec}
    npool = sum(m.astype(np.int64) for m in pool.values()) if pool else np.zeros(shape, dtype=np.int64)
    if ((nuse > 0) & (npool == 0)).any():
        return None                                       # the scalar path refuses: every usable plan is measure-only
    pick = np.full(shape, -1, dtype=np.int64)            # index into `bodies`
    bidx = {v: i for i, v in enumerate(bodies)}
    has_h = bool(d.get("cost_h"))
    if not has_h:
        in_sample = d.get("fam") in composer.plan_rule.in_sample_default   # as compose.query_pick: the rule's default families
        one = nuse == 1
        for v in bodies:                                  # one usable plan (or the write plan): nothing to choose
            pick = np.where(one & use[v], bidx[v], pick)
        rest = (nuse > 1)
        if rest.any():
            if not in_sample:
                return None                               # the scalar path refuses: no held-out rows
            sc = scores_of({v: vec[v] for v in pool})
            best = np.full(shape, INF)
            for v in sorted(pool):                         # pick_body: the first in sorted order on an exact tie
                better = rest & pool[v] & (sc[v] < best)
                best = np.where(better, sc[v], best)
                pick = np.where(better, bidx[v], pick)
    else:
        if any(c not in d.get("valid_h", {}) for (pm, am) in combos for c in pairs(pm, am)):
            return None
        uni = single[pmask] & single[amask] & ~sized     # compose.query_pick's `uniform`
        # uniform entries: the cell's own pick, from one scalar call per cell (whatever rule a cell is priced by)
        for (pm, am), ci in combo_of.items():
            if bin(pm).count("1") != 1 or bin(am).count("1") != 1:
                continue
            here = uni & (cidx == ci)
            if not here.any():
                continue
            first = tuple(int(x[0]) for x in np.nonzero(here))
            assign = {u: opts[j][first[j]] for j, u in enumerate(ps)}
            best, tots, _ = P.query_pick(d, pp, assign, set(ps), mode)
            if best is None or best not in bidx:
                return None
            pick = np.where(here, bidx[best], pick)
        other = ~uni & (nuse > 0)
        if other.any():
            params_h = sorted({kk[2] for kk in d["cost_h"]}, key=int)
            use_h = use_masks(d["valid_h"])
            cand = {v: other & pool[v] & use_h[v] for v in pool}
            covered = np.zeros(shape, dtype=bool)
            for c in cand.values():
                covered = covered | c
            if (other & ~covered).any():
                return None                               # the scalar path refuses: no plan usable on both sets
            vh = compose("cost_h", params_h, list(SCORE_KEYS), cand)
            if vh is None:
                return None
            sh = scores_of(vh)
            best = np.full(shape, INF)
            for v in C.tie_order(sorted(vh), d.get("q")):   # held_out_pick, no anchor: the first in tie order on a tie
                better = cand[v] & (sh[v] < best)
                best = np.where(better, sh[v], best)
                pick = np.where(better, bidx[v], pick)
    if ((nuse > 0) & (pick < 0)).any():
        return None
    ops = np.full(shape, INF)
    byt = np.full(shape, INF)
    more = {name: np.full(shape, INF) for name in sorted(extra or {})}
    for v in bodies:
        at = pick == bidx[v]
        if not at.any():
            continue
        cur = currencies(vec[v])
        ops = np.where(at, cur["ops"], ops)
        byt = np.where(at, cur["bytes"], byt)
    if extra:
        flat_pick = pick.reshape(-1)
        cols = {v: {m: vec[v][m].reshape(-1) for m in keys} for v in vec}
        for name, f in sorted(extra.items()):
            vals = more[name].reshape(-1)
            for e in np.nonzero(flat_pick >= 0)[0]:
                v = bodies[flat_pick[e]]
                vals[e] = f({m: float(cols[v][m][e]) for m in keys})
            more[name] = vals
    return ops.reshape(-1).tolist(), byt.reshape(-1).tolist(), {name: np.asarray(more[name]).reshape(-1).tolist()
                                                                for name in sorted(extra or {})}
