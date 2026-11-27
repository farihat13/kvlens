"""A predicted capture (`kvevaluator predict --plans --out-predicted DIR`, python/kvevaluator/measure/README.md): the
one loader kvcost reads it through, a row's cost under a model (cost_models.cost, the one pricer), and the plan rule on
it (candidates.pick_body, the measured captures' own). Opens no store."""
import glob
import json
import os
from collections import defaultdict

from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import benchmarks, compare, compose, picks
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture
from kvevaluator.plan_choice import candidates as C

PREDICTED = "predicted"           # a predicted cell's record says so in measured_as (kvevaluator predict --plans)
TIE_DIGITS = compare.TIE_DIGITS   # predicted costs are floats: two bodies equal at 12 significant digits tie


def record(cap_dir, cell):
    """a predicted cell's record (counts/<cell>.json); refused when the cell is not predicted"""
    path = Capture(cap_dir).record(cell)
    rec = json.load(open(path))
    if rec.get("measured_as") != PREDICTED:
        raise SystemExit(f"{path}: not a predicted capture (measured_as {rec.get('measured_as')!r})")
    return rec


def cells(cap_dir):
    """the cells a predicted capture holds, sorted"""
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(Capture(cap_dir).dir("counts"), "*.json")))


def rows(cap_dir, kind, cell):
    """a cell's rows of one kind (counts, steps, parts)"""
    record(cap_dir, cell)
    with open(Capture(cap_dir).path_of(kind, cell)) as f:
        return list(capture_layout.reader(f))


def vector(r, write=False, weights=None, view="measured", cell=""):
    """a run or step row as a priced vector: a read's (compose.read_cost: read_ops, 7-lane read_bytes, miss_key_bytes;
    the write channels 0), or a write's (compose.write_vector under the chunk view `view`, compose.chunk_view: its PUTs,
    DELs and bytes written, its own reads as write_rmw_*; the read channels 0); with the raw channels beside
    (compose.CHANNELS: a lane the row has no column of, a read's PUTs, is 0, as captured; a write's scan_value and
    write_value under the view, as the composer carries them), which no cost reads.
    `weights`: the op-weight vector of the weighted ops (None: the default). The accuracy reports compare an estimate
    with the bytes its run found, so they read "measured"; a cost beside a measured search passes its view"""
    if write:
        r = compose.chunk_view(r, view, cell)       # the raw lanes too
    raw = {c: float(r.get(c) or 0) for c in compose.CHANNELS}
    if write:
        return {"read_ops": 0.0, "read_bytes": 0.0, "miss_key_bytes": 0.0, **compose.write_vector(r, weights), **raw}
    v = compose.read_cost(r, weights)
    v.update(write_rmw_ops=0.0, write_ops=0.0, write_rmw_bytes=0.0, write_bytes=0.0, write_rmw_miss_key_bytes=0.0)
    v.update(raw)
    return v


def is_writes(cap_dir, cell):
    """a predicted cell of the writes family (its record's workload, as kvcost predict writes it): its rows are writes"""
    workload = record(cap_dir, cell).get("workload")   # a record made before records named their workload: reads
    return workload is not None and benchmarks.kind_of(workload) == "write"


def pick_vector(r):
    """a run row as the plan rule reads it (candidates.pick_body): S, G, N and the bytes of candidates.READ_LANES"""
    return {"S": float(r["S"]), "G": float(r["G"]), "N": float(r["N"]),
            "total_bytes": sum(float(r[l] or 0) for l in C.READ_LANES)}


def cost(v, model):
    """a vector's cost under a model (cost_models.cost)"""
    return CM.cost(v, model)


def comparable(x):
    """a predicted cost as it is compared: at 12 significant digits (compare.comparable). The summation order (per step
    or per run, the weights scaled or not) moves the last bits, and two bodies or layouts the model prices the same
    must tie (then the first name in sort order wins), not be ordered by rounding noise"""
    return compare.comparable(x)


def hit_bytes(v):
    """the hit keys and values only (read_bytes less the miss keys): a raw column of the accuracy report, beside its
    7-lane bytes"""
    return v["read_bytes"] - v["miss_key_bytes"]


def mean(vs):
    out = defaultdict(float)
    for v in vs:
        for k, x in v.items():
            out[k] += x
    return {k: x / len(vs) for k, x in out.items()}


def by_body(cap_dir, cell, queries=None, params=None, view="measured"):
    """({(query, body): {param: vector}} over the bodies priced on every param asked, {(query, body)} left out: a run
    not ok (a step not estimated, a refusal) or a param missing). params: the param_idx strings every body needs
    (default: the params the query's rows hold). view: the chunk view of a write's vector (compose.chunk_view)"""
    bodies, bad = _run_rows(cap_dir, cell, queries, params)
    w = is_writes(cap_dir, cell)
    return {k: {p: vector(r, w, view=view, cell=cell) for p, r in ps.items()} for k, ps in bodies.items()}, bad


def _run_rows(cap_dir, cell, queries, params):
    """by_body's selection, the run rows themselves: ({(query, body): {param: row}}, the bodies left out). A capture of
    the picked bodies only is refused (picks.every_body): its rows are no layout's candidates"""
    picks.every_body(record(cap_dir, cell), cell)
    per = defaultdict(dict)
    bad = set()
    for r in rows(cap_dir, "counts", cell):
        if queries is not None and r["query"] not in queries:
            continue
        k = (r["query"], r["body"])
        if r["ok"] != "1":
            bad.add(k)
        per[k][r["param_idx"]] = r
    seen = defaultdict(set)
    for (q, _), ps in per.items():
        seen[q] |= set(ps)
    out = {}
    for k, ps in per.items():
        want = set(params) if params is not None else seen[k[0]]
        if k in bad or not want <= set(ps):
            bad.add(k)
            continue
        out[k] = {p: ps[p] for p in sorted(want, key=int)}
    return out, bad


def bill_bounds(cap_dir, cell, queries=None):
    """{(query, body, param_idx): the bill's bounds (compose.BILL_BOUNDS)} of a predicted cell: compose.bill_units on
    each part row (a step and a part of one param), summed per run, as the measured composer sums them. A run total
    would understate the lower form: it takes a max per step row"""
    out = defaultdict(lambda: dict.fromkeys(compose.BILL_BOUNDS, 0.0))
    for r in rows(cap_dir, "parts", cell):
        if queries is not None and r["query"] not in queries:
            continue
        units = compose.bill_units(r, {c: float(r.get(c) or 0) for c in compose.CHANNELS}, sized=False)
        acc = out[(r["query"], r["body"], r["param_idx"])]
        for k, x in units.items():
            acc[k] += x
    return dict(out)


def pick(cap_dir, cell, queries=None, params=None, bill=False, criterion=None, weights=None, *, view):
    """the plan rule on a predicted capture: per query, candidates.pick_body (the measured captures' rule, its default
    criterion) over each body's mean pick vector over the params; a tie (equal at TIE_DIGITS significant digits) goes
    to the first body name in sort order. -> ({query: (body, mean vector, {param: vector})}, the bodies left out).
    In-sample: no held-out params are predicted. bill: each vector also carries the bill's bounds (bill_bounds; the
    exact form needs a measured capture's exact unit columns, so a predicted vector never carries it). criterion and
    weights: another pick rule and the op-weight vector of the costs (kvcost cost-layouts --op-weights: both from the vector,
    criteria.with_weights); None: the default. view: the chunk view of a write's vector (compose.chunk_view)"""
    bodies, bad = _run_rows(cap_dir, cell, queries, params)
    w = is_writes(cap_dir, cell)
    units = bill_bounds(cap_dir, cell, queries) if bill else None
    per_query = defaultdict(dict)
    for (q, b), ps in sorted(bodies.items()):
        per_query[q][b] = ps
    best = {}
    for q, bs in per_query.items():
        b, _ = C.pick_body({b: mean([pick_vector(r) for r in ps.values()]) for b, ps in bs.items()}, criterion,
                           tie_digits=TIE_DIGITS)
        vs = {p: vector(r, w, weights, view, cell) for p, r in bs[b].items()}
        if units is not None:
            for p, v in vs.items():
                if (q, b, p) not in units:
                    raise SystemExit(f"{cap_dir} {cell}: {q} {b} param {p} has no part rows: its bill cannot be computed")
                v.update(units[(q, b, p)])
        best[q] = (b, mean(list(vs.values())), vs)
    return best, bad
