"""Steady latency per store and query from a cpplatency run (its steps.csv), with the combined cost of the body timed,
and the weighted ratios over a reference store: the rule latency-steady-ldbc (LDBC) and the JOB study's table share.

The wall of record: per (query, param), the mean over the measured passes from FIRST_PASS (pass
0 follows each store's own number of warm passes) of the run's wall (plan_wall: work_ns + overhead_ns) and of its KV
time (its kind == "kv" steps' wall_ns), read from the cell's raw steady.json; per query, the mean over its params
(OVER_PARAMS). The mean, not the median: a frequent query's runs can be bimodal (a cache hit or miss on its blocks),
and the mean counts every pass. The median (the steps.csv `__plan__` row, then the median over params) is kept beside
it. A weighted ratio's band is the min and max of the per-pass ratio over the same passes. A wall capture whose meta
does not record the measured-index pass order (steady_rule.PASS_ORDER_RULE) is refused.

The combined cost is the mean over params of wops_bytes4k (cost_models.of_channels) of the timed run's own counts (its
`__plan__` row, plan_channels): counts are the same on every run of a plan, so no counts capture is needed. Where the
tree holds the counts capture's row of the body timed (picks.json), every plan row's channels are checked equal to it.

The RocksDB share (timer_query, shares) reads the timer captures beside a wall capture (perf level 2 with the KV
timer; perf level 3 for the breakdown). Per request, the RocksDB time is the sum of its kv steps' kv_call_ns less
calls x timer_pair_ns / 2; the busy time is the plan's work_ns and the wall its work_ns + overhead_ns, each less
calls x timer_pair_ns. Per query the mean over params and runs, so the share is the share of mean busy time. It is
not a share of the wall of record: never multiply it into one or set it against a ratio of them. The perf-3 capture
only splits it (block_read_share: its time per block read times the perf-2 runs' mean block reads); a query whose
perf-3 runs read no block takes the store's pooled time per block. kvtimer_tax (the timer passes' mean wall over the
wall capture's) is indicative only: the passes differ in what the cache holds.
"""
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

from kvcost.layout_cost import cost_models
from kvcost.latency.steps import load_writes
from kvcost.layout_cost import query_weighting
from kvevaluator.capture import capture_layout
from kvevaluator.measure.cpplatency import plan_wall
from kvevaluator.measure.latency_raw_files import raw_path
from kvevaluator.measure.steady_rule import PASS_ORDER_RULE


def capture_costs(picks_path, capture):
    """{store: {(query, param): counts capture row}} of one timing set: the body its picks.json timed, per store and
    query; capture is the workload's capture (output_tree.Capture), each store's counts read from the capture that
    holds it (its of_layout, the rule the pick step reads by). A store the tree holds no counts of is left out, and
    all of them without a picks.json: their cost is the timed run's own (per_query)"""
    if not os.path.exists(picks_path):
        return {}
    picks = json.load(open(picks_path))["cells"]
    out = {}
    for store, bodies in picks.items():
        path = capture.of_layout(store).path_of("counts", store)
        if os.path.exists(path):
            out[store] = {(r["query"], r["param_idx"]): r for r in capture_layout.reader(open(path))
                          if r["body"] == bodies.get(r["query"])}
    return out


def plan_channels(plan):
    """a steps.csv `__plan__` row's channels under the counts capture's names (its byte lanes are <lane>_bytes)"""
    mapped = {k: plan[k] for k in ("S", "G", "N", "P", "D")}
    mapped.update({lane: plan[f"{lane}_bytes"] for lane in (*cost_models.READ_LANES, *cost_models.WRITE_LANES)})
    return mapped


def same_counts(plan, row):
    """a steps.csv `__plan__` row's channels equal the counts capture row's"""
    a, b = cost_models.of_channels(plan_channels(plan)), cost_models.of_channels(row)
    return all(abs(a[k] - b[k]) <= 1e-9 * max(1.0, abs(b[k])) for k in a)


FIRST_PASS = 1   # the first measured pass the wall of record and the band count: pass 0 follows a store's own warm-up
OVER_PARAMS = statistics.fmean   # a query's wall of record and its per-pass walls over its params (statistics.median before)


def counted_runs(measured):
    """the measured runs the rule counts, in pass order: from FIRST_PASS on; a capture of FIRST_PASS runs or fewer (a
    1-pass control re-time) keeps them all"""
    return measured[FIRST_PASS:] if len(measured) > FIRST_PASS else measured


def cell_runs(capture_dir, arm, query, param):
    """[(wall, kv time)] of one cell's counted runs (counted_runs) in pass order, from its raw steady.json: the wall
    cpplatency's plan_wall, the KV time the sum of its kind == "kv" steps' wall_ns (the steps.csv rule, run by run)"""
    rec = json.load(open(raw_path(capture_dir, arm, query, param, "steady")))
    return [(plan_wall(x), sum(s["wall_ns"] for s in x["steps"] if s.get("kind") == "kv"))
            for x in counted_runs(rec["measured"])]


def wall_of_record(param_runs):
    """(wall, kv time) of one query from {param: [(wall, kv time)] per measured pass}: per param the mean over its
    passes, then OVER_PARAMS over the params, the mean (the rule; see the module docstring)"""
    means = [(statistics.fmean(w for w, _ in runs), statistics.fmean(k for _, k in runs)) for runs in param_runs.values()]
    return OVER_PARAMS([w for w, _ in means]), OVER_PARAMS([k for _, k in means])


def checked(plan, counts, path):
    """a `__plan__` row's channels (plan_channels), checked equal to the counts capture's row of the body timed when
    `counts` ({(query, param): row}) is given"""
    if counts is not None:
        row = counts.get((plan["query"], plan["param_idx"]))
        if row is None or not same_counts(plan, row):
            raise SystemExit(f"{path}: {plan['query']} p{plan['param_idx']}: the counts capture has "
                             f"{'no row' if row is None else 'other counts'} for the body timed")
    return plan_channels(plan)


def per_query(path, counts=None):
    """{query: (latency, kv time, combined cost, median-run latency, median-run kv time)} of one store: the wall of record
    and its KV time (the mean over counted passes per param (counted_runs), then the mean over params), the cost from the
    `__plan__` row's own counts (with `counts`, the counts capture's rows of the body timed, each checked equal to it),
    and the `__plan__` row's median-run values, each the median over params"""
    capture_dir, arm = Path(path).parent, Path(path).parent.parent.name   # <arm>/<regime>/: cpplatency's --out
    plans, kv = defaultdict(list), defaultdict(float)
    for r in capture_layout.reader(open(path)):
        if r["step"] == "__plan__":
            plans[r["query"]].append((float(r["wall_ns"]), cost_models.of_channels(checked(r, counts, path))["wops_bytes4k"],
                                      r["param_idx"]))
        elif r["kind"] == "kv":
            kv[(r["query"], r["param_idx"])] += float(r["wall_ns"])
    out = {}
    for q, runs in plans.items():
        if len({p for _, _, p in runs}) != len(runs):
            raise SystemExit(f"{path}: {q} has two __plan__ rows on one param")
        out[q] = (*wall_of_record({p: cell_runs(capture_dir, arm, q, p) for _, _, p in runs}),
                  statistics.mean(c for _, c, _ in runs),
                  statistics.median(w for w, _, _ in runs), statistics.median(kv[(q, p)] for _, _, p in runs))
    return out


def write_per_query(path):
    """{query: (latency, kv time, combined cost, latency, kv time)} of one store's steady write capture (a cppwlatency
    steps.csv, IU1-8): an applied write runs once, so per (query, param) its one apply wall, its KV time (the kv steps'
    wall_ns) and its combined cost (wops_bytes4k of the run's own counts); per query the mean over params, and the median
    over params in the last two fields (per_query's shape)"""
    w = load_writes(path)
    kv = defaultdict(float)
    for r in capture_layout.reader(open(path)):
        if r["kind"] == "kv" and r["query"].startswith("iu"):
            kv[(r["query"], r["param_idx"])] += float(r["wall_ns"])
    by_q = defaultdict(list)
    for (q, p), v in sorted(w.items()):
        if v["plan"] != v["plan"] or (q, p) not in kv:
            raise SystemExit(f"{path}: {q} p{p}: no apply wall or no kv step")
        by_q[q].append((v["plan"], kv[(q, p)], cost_models.wops_bytes4k(v["ops"], v["bytes"])))
    return {q: (OVER_PARAMS([x[0] for x in xs]), OVER_PARAMS([x[1] for x in xs]), statistics.fmean(x[2] for x in xs),
                statistics.median(x[0] for x in xs), statistics.median(x[1] for x in xs)) for q, xs in by_q.items()}


def plan_rows(path):
    """the number of `__plan__` rows in one steps.csv (a check's count)"""
    return sum(1 for r in capture_layout.reader(open(path)) if r["step"] == "__plan__")


def stores_of(timings_dir, regime):
    """{store: steps.csv} of one workload's timings in one regime (output_tree: <workload's timings>/meta-<store>/<regime>/)"""
    return {p.parents[1].name[len("meta-"):]: p for p in sorted(Path(timings_dir).glob(f"meta-*/{regime}/steps.csv"))}


def equal_share(ref):
    """{query: 1 / its combined cost on the reference store} (query_weighting.item_weights' additive rule), ref:
    per_query's output for that store"""
    queries = list(ref)
    return dict(zip(queries, query_weighting.item_weights("additive", [1.0] * len(queries),
                                                          [ref[q][2] for q in queries])))


def ratios(d, ref, weights):
    """{wall_ratio, kv_ratio, kv_share, cost_ratio, wall_ratio_median, kv_ratio_median} of one store's per_query output
    `d` over the reference's, each the weighted sum sum_q w_q x(q) (kv_share: the store's weighted KV time over its
    weighted wall); wall_ratio and kv_ratio are the wall of record's (the mean over passes 1-9), the *_median ones the
    median run's; every query of `weights` must be in both"""
    for name, x in (("the store", d), ("the reference", ref)):
        if set(x) != set(weights):
            raise SystemExit(f"{name}: queries {sorted(set(x) ^ set(weights))} not in both it and the weights")
    total = lambda x, i: sum(w * x[q][i] for q, w in weights.items())
    return {"wall_ratio": total(d, 0) / total(ref, 0), "kv_ratio": total(d, 1) / total(ref, 1),
            "kv_share": total(d, 1) / total(d, 0), "cost_ratio": total(d, 2) / total(ref, 2),
            "wall_ratio_median": total(d, 3) / total(ref, 3), "kv_ratio_median": total(d, 4) / total(ref, 4)}


def row_roots(stores_by_root, reference):
    """{store: the index of the timing root whose timing is its row}, stores_by_root the stores of each root in order
    (one root per host, each with its own reference). Every root must time the reference; a store timed in two roots
    is refused, except the reference: the first root's is its row, the others' only the reference of their own stores"""
    owner = {}
    for i, stores in enumerate(stores_by_root):
        if reference not in stores:
            raise SystemExit(f"timing root {i}: the reference {reference} is not among its stores ({sorted(stores)})")
        for store in sorted(stores):
            if store == reference and i > 0:
                continue
            if store in owner:
                raise SystemExit(f"{store}: timed in timing roots {owner[store]} and {i}, one row per store")
            owner[store] = i
    return owner


def ratios_by_root(data_by_root, reference, weights):
    """{store: (its root's index, ratios)}: each store's per_query output over its own root's reference (ratios), the
    rows chosen by row_roots; data_by_root [{store: per_query output}] per root in order"""
    owner = row_roots([set(d) for d in data_by_root], reference)
    return {store: (i, ratios(data_by_root[i][store], data_by_root[i][reference], weights)) for store, i in owner.items()}


# the KV timer's call counts per kv step (cpplatency.KV_TIMER_FIELDS): calls x timer_pair_ns is the timer's own cost
TIMER_CALLS = ("kv_get_calls", "kv_seek_calls", "kv_next_calls", "kv_iter_close_calls", "kv_put_calls", "kv_delete_calls")


def timer_pair_ns(capture_dir):
    """the KV timer's own cost per call, from a timer capture's meta (its kv_timing block); refused when absent"""
    meta = json.load(open(Path(capture_dir) / "steps.csv.meta.json"))
    pair = (meta.get("kv_timing_block") or {}).get("timer_pair_ns")
    if pair is None:
        raise SystemExit(f"{capture_dir}: no kv_timing_block.timer_pair_ns in its meta: not a timer capture")
    return float(pair)


def raw_runs(rec):
    """the runs of one raw file: a steady record's measured runs (cpplatency), or one applied write's response
    (cppwlatency: an applied write runs once per state, so its file is the run itself)"""
    return rec["measured"] if "measured" in rec else [rec]


def _raw_cells(capture_dir, arm, raw):
    """{(query, param): raw file} of one capture: raw when given (a write capture's, cppwlatency.raw_files), else the
    cells of its steps.csv's __plan__ rows at the steady raw path"""
    if raw is not None:
        return dict(sorted(raw.items()))
    cells = sorted({(r["query"], r["param_idx"]) for r in capture_layout.reader(open(Path(capture_dir) / "steps.csv"))
                    if r["step"] == "__plan__"})
    return {(q, p): raw_path(capture_dir, arm, q, p, "steady") for q, p in cells}


def timer_query(capture_dir, arm, raw=None):
    """{query: (RocksDB time, busy time, wall, block-read time, block reads)} of one timer capture, from every run in
    each cell's raw file (the cells: its steps.csv's __plan__ rows at the steady raw path; arm: meta-<store>; raw: the
    {(query, param): file} of a write capture instead), each the mean over the params and runs, the timer's own cost
    taken out of the first three (_timer_run)"""
    pair = timer_pair_ns(capture_dir)
    per_q = defaultdict(list)
    for (q, p), path in _raw_cells(capture_dir, arm, raw).items():
        for m in raw_runs(json.load(open(path))):
            per_q[q].append(_timer_run(m, pair, f"{capture_dir}: {q} p{p}"))
    return {q: tuple(statistics.fmean(v[i] for v in xs) for i in range(5)) for q, xs in per_q.items()}


def _timer_run(m, pair, where):
    """(RocksDB time, busy time, wall, block-read time, block reads) of one timer run: half the timer's cost out of the
    RocksDB time (one clock read per call falls inside the timed interval), all of it out of the busy time and wall"""
    kv = [s["kv"] or {} for s in m["steps"] if s.get("kind") == "kv"]
    rocks = sum(k.get("kv_call_ns", 0) for k in kv)
    if rocks <= 0:
        raise SystemExit(f"{where}: no kv_call_ns: the KV timer did not run")
    cost = pair * sum(k.get(c, 0) for k in kv for c in TIMER_CALLS)
    t = m["totals"]
    return (rocks - cost / 2, t["work_ns"] - cost, t["work_ns"] + t.get("overhead_ns", 0) - cost,
            sum(k.get("block_read_time", 0) for k in kv), sum(k.get("block_read_count", 0) for k in kv))


def shares(kvtimer, breakdown, walls, weights):
    """the RocksDB share of one store: kvtimer and breakdown are timer_query's output for its perf-2 and perf-3 timer
    captures (breakdown may be None), walls mean_walls' output for its wall capture. Returns rocks_share (RocksDB time
    over busy time, perf 2: the share of mean busy time) and rocks_wall_share (over the mean wall), block_read_share
    (block_ns_per_read x the perf-2 block reads, over the perf-2 busy time) and other_rocks_share (the rest),
    kvtimer_tax (the perf-2 timer passes' mean wall, the timer's cost out, over the wall capture's mean wall) and
    perf3_tax (the perf-3 mean wall over the perf-2 timer passes'), each a ratio of weighted sums; and those sums
    (rocks_ns, busy_ns, timer_wall_ns, mean_wall_ns; with a breakdown, block_read_ns and perf3_wall_ns)"""
    for name, x in (("kvtimer", kvtimer), ("walls", walls)) + ((("breakdown", breakdown),) if breakdown else ()):
        if set(x) != set(weights):
            raise SystemExit(f"{name}: queries {sorted(set(x) ^ set(weights))} not in both it and the weights")
    total = lambda x, i: sum(w * x[q][i] for q, w in weights.items())
    sums = {"rocks_ns": total(kvtimer, 0), "busy_ns": total(kvtimer, 1), "timer_wall_ns": total(kvtimer, 2),
            "mean_wall_ns": sum(w * walls[q] for q, w in weights.items())}
    out = {"rocks_share": sums["rocks_ns"] / sums["busy_ns"], "rocks_wall_share": sums["rocks_ns"] / sums["timer_wall_ns"],
           "kvtimer_tax": sums["timer_wall_ns"] / sums["mean_wall_ns"]}
    if breakdown:
        per_read = block_ns_per_read(breakdown)
        sums["block_read_ns"] = sum(w * per_read[q] * kvtimer[q][4] for q, w in weights.items())
        sums["perf3_wall_ns"] = total(breakdown, 2)
        block = sums["block_read_ns"] / sums["busy_ns"]
        out.update(block_read_share=block, other_rocks_share=out["rocks_share"] - block,
                   perf3_tax=sums["perf3_wall_ns"] / sums["timer_wall_ns"])
    return {**out, **sums}


def block_ns_per_read(breakdown):
    """{query: block-read time per block read} of a perf-3 timer_query output; a query with no block read in its perf-3
    runs takes the store's pooled time per read (0 when the store read none)"""
    reads = sum(x[4] for x in breakdown.values())
    pooled = sum(x[3] for x in breakdown.values()) / reads if reads else 0.0
    return {q: x[3] / x[4] if x[4] else pooled for q, x in breakdown.items()}


def mean_walls(capture_dir, arm, raw=None):
    """{query: the mean wall over its params and measured runs} of one wall capture (each run's plan_wall, from each
    cell's raw file; the cells as timer_query's, raw likewise): kvtimer_tax's denominator, a mean like its numerator"""
    per_q = defaultdict(list)
    for (q, _p), path in _raw_cells(capture_dir, arm, raw).items():
        per_q[q] += [plan_wall(m) for m in raw_runs(json.load(open(path)))]
    return {q: statistics.fmean(v) for q, v in per_q.items()}


def per_pass_walls(capture_dir, arm, cells):
    """{query: [OVER_PARAMS (the mean) over params of the wall, per measured pass]} of one wall capture, from each cell's raw
    steady.json (its measured runs in pass order); cells: the (query, param) of its steps.csv. Every cell must hold the
    same number of runs, and the capture's meta must record the measured-index pass order"""
    rule = json.load(open(Path(capture_dir) / "steps.csv.meta.json")).get("regimes", {}).get("steady", {}).get("pass_order_rule")
    if rule != PASS_ORDER_RULE:
        raise SystemExit(f"{capture_dir}: pass order rule {rule!r}, not {PASS_ORDER_RULE!r}: its passes do not pair "
                         f"with another store's")
    runs = {}
    for q, p in cells:
        rec = json.load(open(raw_path(capture_dir, arm, q, p, "steady")))
        runs[(q, p)] = [plan_wall(x) for x in rec["measured"]]
    n = {len(v) for v in runs.values()}
    if len(n) != 1:
        raise SystemExit(f"{capture_dir}: the cells hold {sorted(n)} measured runs: one count for all")
    k = n.pop()
    by_q = defaultdict(list)
    for (q, _p), walls in runs.items():
        by_q[q].append(walls)
    return {q: [OVER_PARAMS([w[i] for w in ws]) for i in range(k)] for q, ws in by_q.items()}


def pass_ratios(store, ref, weights, first=FIRST_PASS):
    """[pass k's weighted wall over the reference's pass k], k from `first` (default FIRST_PASS: pass 0 follows each
    store's own number of warm passes), for two per_pass_walls outputs with the same pass count"""
    k = {len(v) for x in (store, ref) for v in x.values()}
    if len(k) != 1:
        raise SystemExit(f"per-pass ratios: pass counts {sorted(k)}: the two stores must hold the same")
    return [sum(w * store[q][i] for q, w in weights.items()) / sum(w * ref[q][i] for q, w in weights.items())
            for i in range(first, k.pop())]


def pass_order_recorded(capture_dir):
    """True when a wall capture's meta records the measured-index pass order (per_pass_walls reads only such a one)"""
    meta = json.load(open(Path(capture_dir) / "steps.csv.meta.json"))
    return meta.get("regimes", {}).get("steady", {}).get("pass_order_rule") == PASS_ORDER_RULE


def pass_band(path, ref_path, weights, fixed=None):
    """(min, max) over the measured passes from FIRST_PASS of the per-pass weighted wall ratio of one store's steps.csv over
    the reference's (per_pass_walls, pass_ratios). None when the passes do not pair: either capture does not record the
    pass order, the two hold other numbers of measured passes, or fewer than two. fixed: {query: (the store's wall, the
    reference's)} of queries timed once, not per pass (the writes), the same in every pass's sum"""
    dirs = [Path(p).parent for p in (path, ref_path)]
    if not all(pass_order_recorded(d) for d in dirs):
        return None
    walls = [per_pass_walls(d, d.parent.name, sorted({(r["query"], r["param_idx"]) for r in capture_layout.reader(open(p))
                                               if r["step"] == "__plan__"}))
             for d, p in zip(dirs, (path, ref_path))]
    counts = {len(v) for w in walls for v in w.values()}
    if len(counts) != 1 or (k := counts.pop()) < 2:
        return None
    for q, (mine, ref) in (fixed or {}).items():
        walls[0][q], walls[1][q] = [mine] * k, [ref] * k
    r = pass_ratios(walls[0], walls[1], weights)
    return min(r), max(r)
