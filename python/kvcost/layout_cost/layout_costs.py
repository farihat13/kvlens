"""cost-layouts' one place, and the one reader of it: a workload's per-query and per-layout costs on every layout costed,
in every cost model and both chunk views. `kvcost cost-layouts` writes it (python/kvcost/cli/search.py); the paper's
derive scripts, the studies, plot-costs and predict-accuracy read it through the functions below.

    summary/<dataset>/<scale>/layout-costs/<workload>/<count source>/[pick-by-<rule>/][pick-on-in-sample/]
        [op-weights-<G>-<S>-<N>/][params-<set>/]

The count source is `measured` (each layout measured itself, else composed: kvcost compose) or a predicted set
(kvcost predict --save-as). A non-default pick rule, op weights or params adds one level each, named by its value. The
cost model, the chunk view and the way queries are combined are not part of the place: every model is written, both
chunk views are rows, and a total under a combine rule is made by the reader.

The files:
    query_costs.csv  a row per (family, query, layout, chunk view): the picked plan (`body`), one run's cost: wops,
                     bytes, wops_bytes4k, every raw channel, and the bill's unit lanes (filled on the measured view's
                     rows, the bill's view, of a measured source; blank elsewhere)
    query_parts.csv  the same plan's cost per run on each part kind (adj, props, index), every measure and channel
    layouts.csv      a row per (layout, chunk view): kind (uniform, mix), its spec's sha, measured_as (measured,
                     composed, predicted), the frequency-weighted totals of every measure, channel and bill lane, and
                     total_ops, total_bytes (cost_models.currencies)
    README.md        what is here, and the commands that made it
    runs.jsonl       one line per run: its argv, the layouts it costed, the source tree, and wall_s per stage
A run upserts by layout: a layout costed again replaces its own rows, the others stay. No column depends on which other
layouts a run held (no rank, no ratio to the best, no additive total): those are made when read (totals, combined).
"""
import csv
import hashlib
import json
import math
import os
import time

COSTS = "layout-costs"
VIEWS = ("half", "measured")      # compose.CHUNK_VIEWS: a write's touch of a family's last chunk at target/2, or as found
BILL_VIEW = "measured"            # the bill's lanes are filled on this view's rows (a measured source's exact units)
KINDS = ("uniform", "mix")
PART_KINDS = ("adj", "props", "index")
FILES = ("query_costs.csv", "query_parts.csv", "layouts.csv", "README.md", "runs.jsonl")


def _channels():
    from kvcost.layout_cost import compose
    return list(compose.MEASURES), list(compose.CHANNELS), list(compose.BILL)


def fields():
    """(layouts.csv's, query_costs.csv's, query_parts.csv's columns)"""
    measures, channels, bill = _channels()
    raw = measures + channels
    key = ["layout", "chunk_view", "kind", "props", "adjacency"]
    return (["workload"] + key + ["name", "spec_sha", "measured_as", "total_ops", "total_bytes"] + raw + bill,
            ["family", "query"] + key + ["body", "wops", "bytes", "wops_bytes4k"] + channels + bill,
            ["family", "query"] + key + ["body", "part"] + raw + bill)


def level_names(pick_by=None, pick_on=None, op_weights=None, params=None):
    """the extra levels of a non-default identity, in a fixed order: pick-by-<rule>, pick-on-in-sample,
    op-weights-<G>-<S>-<N>, params-<set> (each None: the default, no level)"""
    out = []
    if pick_by:
        out.append(f"pick-by-{pick_by}")
    if pick_on:
        out.append(f"pick-on-{pick_on}")
    if op_weights:
        out.append("op-weights-" + str(op_weights).replace(":", "-"))
    if params:
        out.append(f"params-{params}")
    return out


def place(tree, dataset, scale, workload, source="measured", **levels):
    """the dir of one identity (tree: an output_tree.Tree; workload: its id; levels: level_names' keywords, each given
    only when not the default)"""
    return tree.summary(dataset, scale, COSTS, workload, source, *level_names(**levels))


def place_of_run(tree, run, source="measured", pick_by=None, pick_on=None, op_weights=None, param_set=None):
    """the place of a kvcost run (runs.load's WorkloadFile): its dataset, scale and workload id; `pick_by` (a.criterion),
    `pick_on` and `op_weights` (text) as given when not the default, `param_set` when not the families' default set"""
    from kvevaluator import weights
    from kvevaluator.output_tree import default_param_set, params_dir
    if param_set is not None:
        for fam in run.families():      # the first family that lists the set; none does: its refusal is raised
            try:
                params_dir(fam, param_set)
                break
            except SystemExit:
                if fam == run.families()[-1]:
                    raise
    # the families' default set (output_tree.default_param_set, the one owner: LDBC's first set, LinkBench's whole
    # request log) adds no level, named or not; any other set is its own level (LinkBench's anchors too, though its
    # capture dir is reported/)
    default = param_set is None or any(param_set == default_param_set(f) for f in run.families())
    params = None if default else param_set
    w = weights.parse(op_weights) if op_weights else None
    return place(tree, run.dataset, run.scale, run.name, source, pick_by=pick_by, pick_on=pick_on,
                 op_weights=None if w is None or weights.is_default(w) else weights.text(w), params=params)


def spec_sha(spec):
    """the sha256 of a spec's full form (kvevaluator.layouts.expand), the key a layout's rows carry"""
    from kvevaluator import layouts as L
    if spec is None:                     # a mix no spec builds (another dataset's parts): no sha
        return ""
    return hashlib.sha256(json.dumps(L.expand(spec), sort_keys=True).encode()).hexdigest()


# -- write ------------------------------------------------------------------------------------------------------------
def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path, cols, rows):
    tmp = path + ".writing"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def uniform_rank(layout):
    """a uniform layout's place in layouts.csv: adjacency outer, properties inner, each in the settings' order (Sp, Co,
    then Chunked by size); () for a mix"""
    from kvevaluator import layouts as L
    if not L.is_cell(layout):
        return ()
    p, a = L.cell_settings(layout)
    rank = lambda s: (L.DIAGONALS.index(s) if s in L.DIAGONALS else len(L.DIAGONALS), s)
    return (rank(a), rank(p))


def upsert(out, items, layout_rows, query_rows, part_rows, run):
    """write one run's rows into the place `out`: every row of a layout this run costed replaces that layout's rows (both
    views), the other layouts' rows stay; `items`: the workload's (family, query) pairs in order (the rows' order);
    `run`: its runs.jsonl line (its wall_s gains the write's own time). -> the layouts now in the place"""
    t = time.time()
    os.makedirs(out, exist_ok=True)
    lf, qf, pf = fields()
    mine = {r["layout"] for r in layout_rows}
    order = {fq: i for i, fq in enumerate(items)}
    lay_key = lambda r: (KINDS.index(r["kind"]) if r["kind"] in KINDS else len(KINDS), uniform_rank(r["layout"]),
                         r["layout"], VIEWS.index(r["chunk_view"]) if r["chunk_view"] in VIEWS else len(VIEWS))
    q_key = lambda r: (order.get((r["family"], r["query"]), len(order)), r["family"], r["query"], r["layout"],
                       VIEWS.index(r["chunk_view"]) if r["chunk_view"] in VIEWS else len(VIEWS))
    p_key = lambda r: q_key(r) + (PART_KINDS.index(r["part"]) if r["part"] in PART_KINDS else len(PART_KINDS),)
    for name, cols, new, key in (("layouts.csv", lf, layout_rows, lay_key), ("query_costs.csv", qf, query_rows, q_key),
                                 ("query_parts.csv", pf, part_rows, p_key)):
        path = os.path.join(out, name)
        old = [r for r in _read_csv(path) if r["layout"] not in mine]
        _write_csv(path, cols, sorted(old + list(new), key=key))
    if isinstance(run.get("wall_s"), dict):        # the write: the three files rewritten, before the run's line
        w = round(time.time() - t, 3)
        run["wall_s"] = {**run["wall_s"], "write": w, "all": round(run["wall_s"].get("all", 0.0) + w, 3)}
    with open(os.path.join(out, "runs.jsonl"), "a") as f:
        f.write(json.dumps(run, sort_keys=True) + "\n")
    have = sorted({r["layout"] for r in _read_csv(os.path.join(out, "layouts.csv"))})
    write_readme(out)
    return have


def write_readme(out):
    """README.md of a place: what each file holds, the layouts in it, and the commands that wrote it (runs.jsonl)"""
    lays = _read_csv(os.path.join(out, "layouts.csv"))
    runs_ = [json.loads(x) for x in open(os.path.join(out, "runs.jsonl"))] if os.path.exists(
        os.path.join(out, "runs.jsonl")) else []
    kinds = {}
    for r in lays:
        if r["chunk_view"] == VIEWS[0]:
            kinds.setdefault((r["kind"], r["measured_as"]), []).append(r["name"] or r["layout"])
    text = ["> Generated by `kvcost cost-layouts` "
            "(python/kvcost/layout_cost/layout_costs.py, which also reads them).", "",
            f"# Layout costs: {lays[0]['workload'] if lays else '(none yet)'}", "",
            f"{len({r['layout'] for r in lays})} layouts: " + "; ".join(
                f"{len(v)} {k} ({m})" for (k, m), v in sorted(kinds.items())) + ".", "",
            "- `query_costs.csv`: per (query, layout, chunk view), the picked plan (`body`) and one run's cost (the mean "
            "over the reported params): wops (G + 1.1 S + 0.01 N), bytes, wops_bytes4k (wops + bytes / 4096), every raw "
            "channel, and the bill's unit lanes. Two rows per query and layout: the half and the measured chunk view "
            "(`chunk_view`, below); a read query's two rows are equal, and the bill is on the measured rows (of a "
            "measured source).",
            "- `query_parts.csv`: the same plan's cost per run on each part kind (adj, props, index).",
            "- `layouts.csv`: per (layout, chunk view), the totals at the workload's frequencies: total ops = read wops "
            "+ the writes' own reads (same weights) + PUTs + DELs; total bytes = bytes read + the bytes the writes read "
            "first + bytes written; every raw lane. `measured_as`: measured (its own capture), composed (from the "
            "measured diagonals' parts: kvcost compose, or a mix costed from its parts), predicted.",
            "- Chunk view: half charges a write's touch of a family's last chunk at the chunk target / 2; measured "
            "charges the bytes the capture found (the bill's view).",
            "- `runs.jsonl`: one line per run (argv, layouts, wall_s per stage).", "", "Commands:", ""]
    text += [f"    {r['command']}" for r in runs_[-10:]] + [""]
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write("\n".join(text))


# -- read --------------------------------------------------------------------------------------------------------------
def _require(out, name):
    path = os.path.join(str(out), name)
    if not os.path.exists(path):
        raise SystemExit(f"no {path} (kvcost cost-layouts writes it)")
    return path


def read_layouts(out, view=VIEWS[0], kind=None):
    """layouts.csv's rows of one chunk view (and kind, if given), as written (strings)"""
    return [r for r in _read_csv(_require(out, "layouts.csv"))
            if r["chunk_view"] == view and (kind is None or r["kind"] == kind)]


def read_query_costs(out, view=VIEWS[0]):
    """(the (family, query) pairs in the file's order, {(family, query): {layout: row}}) of query_costs.csv's rows of one
    chunk view"""
    order, rows = [], {}
    for r in _read_csv(_require(out, "query_costs.csv")):
        if r["chunk_view"] != view:
            continue
        fq = (r["family"], r["query"])
        if fq not in rows:
            order.append(fq)
        rows.setdefault(fq, {})[r["layout"]] = r
    return order, rows


def read_query_parts(out, view=VIEWS[0]):
    """query_parts.csv's rows of one chunk view"""
    return [r for r in _read_csv(_require(out, "query_parts.csv")) if r["chunk_view"] == view]


def runs_of(out):
    """runs.jsonl's lines"""
    path = os.path.join(str(out), "runs.jsonl")
    return [json.loads(x) for x in open(path)] if os.path.exists(path) else []


def query_cost(r, model):
    """one query_costs row's cost of one run under `model`: wops, bytes, wops_bytes4k, or a bill form from its lanes
    (None where the row has no bill lanes)"""
    from kvcost.layout_cost import cost_models as CM
    if model in ("wops", "bytes", "wops_bytes4k"):
        return float(r[model])
    if CM.needs_bill(model):
        lanes = {k: r.get(k) for k in _channels()[2]}
        if any(v in ("", None) for k, v in lanes.items() if "exact" not in k or model == "bill_exact"):
            return None
        return getattr(CM, model)({k: float(v or 0) for k, v in lanes.items()})
    raise SystemExit(f"--cost-model {model}: wops, bytes, wops_bytes4k or a bill form")


def view_of(model):
    """the chunk view a model is read in: the bill's lanes in the measured view, every other model in half"""
    from kvcost.layout_cost import cost_models as CM
    return BILL_VIEW if CM.needs_bill(model) else VIEWS[0]


def frequencies(workload):
    """{(family, query): its frequency} of a workload (its items; one query listed twice adds up)"""
    out = {}
    for family, q, often, _params in workload["items"]:
        out[(family, q)] = out.get((family, q), 0) + often
    return out


def combined(out, workload, model, how="sum"):
    """{layout: the workload's total under `model`} from query_costs.csv in the model's view, the queries combined by
    `how`: sum (at their frequencies), geomean (each query once: the geometric mean of its cost), additive (the mean of
    each query's cost over its cost on G, the geomean-best uniform layout). A layout missing a query's cost is left out"""
    freq = frequencies(workload)
    _, rows = read_query_costs(out, view_of(model))
    lays = sorted({lay for per in rows.values() for lay in per})
    cost = {lay: {fq: query_cost(rows[fq][lay], model) for fq in freq if lay in rows.get(fq, {})} for lay in lays}
    cost = {lay: c for lay, c in cost.items() if len(c) == len(freq) and all(v is not None for v in c.values())}
    if how == "sum":
        return {lay: sum(freq[fq] * v for fq, v in c.items()) for lay, c in cost.items()}
    geo = {lay: math.exp(sum(math.log(max(v, 1e-300)) for v in c.values()) / len(c)) for lay, c in cost.items()}
    if how == "geomean":
        return geo
    uni = {lay for lay in geo if lay.startswith("props_")}
    g = min(uni or geo, key=geo.get)
    return {lay: sum(v / cost[g][fq] if cost[g][fq] else 1.0 for fq, v in c.items()) / len(c) for lay, c in cost.items()}


def stage_times(**stages):
    """{stage: seconds} rounded, in the order given"""
    return {k: round(v, 3) for k, v in stages.items()}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
