"""How a benchmark's per-query costs add up to one score per layout: the one definition every study uses.

  sum       sum_q w_q * c(q, L): w_q = how often the benchmark runs q (LDBC, LinkBench), or 1 for "each query once"
  geomean   (prod_q c(q, L))^(1/n): equal contribution, for a benchmark that defines no frequencies (BI, JOB). A 2x win
            and a 2x loss on any query count the same, so no query picks the winner by its size. It needs no reference
            layout. LDBC BI's power score aggregates query times the same way (spec v2.2.4, sec. 9.5.4.2).
  additive  (1/n) sum_q c(q, L) / c(q, ref): equal contribution as a weighted sum (each query costs 1 on ref). With ref =
            Sp-Co it is the robustness check beside geomean. With ref = the benchmark's geomean-best layout it picks the
            same best layout as geomean (a mean of ratios is >= their geomean, and ref scores 1 on both), so a mix search
            that needs per-query weights uses w_q = 1 / c(q, that layout).
The mix search takes each form as a query's costs in the mix cost cache and a weight (table_values, item_weights, score, below).
A benchmark with no frequencies of its own (BI, JOB) has an equal-share frequency file instead (EQUAL_SHARE_DIR, below):
its readers use the sum form over that file, as for any workload. A workload with frequencies of its own may have one
beside them, as a second view (EQUAL_SHARE_BESIDE).
"""
import math
from kvevaluator.layouts import SETTINGS, cell_id
import statistics

FORMS = ("sum", "geomean", "additive")


def aggregate(per_query, form, weight=None, ref=None):
    """{cell: score} from per_query {query: {cell: cost}}; weight {query: w} for sum (default 1), ref a cell for additive"""
    queries = sorted(per_query)
    if not queries:
        raise ValueError("no queries")
    cells = sorted(per_query[queries[0]])
    if any(sorted(per_query[q]) != cells for q in queries):
        raise ValueError("not every query is priced on the same layouts")
    if form == "sum":
        w = weight or {}
        return {c: math.fsum(w.get(q, 1.0) * per_query[q][c] for q in queries) for c in cells}
    if weight:
        raise ValueError(f"{form} takes no frequencies: equal contribution")
    bad = [q for q in queries for c in cells if per_query[q][c] <= 0]
    if bad:                                     # a zero cost has no ratio or log; refuse rather than drop it silently
        raise ValueError(f"{form}: a cost <= 0 on {sorted(set(bad))[:5]}")
    if form == "geomean":
        return {c: math.exp(math.fsum(math.log(per_query[q][c]) for q in queries) / len(queries)) for c in cells}
    if form == "additive":
        if ref not in cells:
            raise ValueError(f"additive needs a reference layout, got {ref!r}")
        return {c: math.fsum(per_query[q][c] / per_query[q][ref] for q in queries) / len(queries) for c in cells}
    raise ValueError(f"unknown form {form!r}; one of {FORMS}")


def shares(per_query, weight):
    """({query: run share}, {query: cost share}) from per_query {query: {cell: cost}} and weight {query: w}: a query's
    weight over all the weights, and its w x cost over the layout's sum (the "sum" form), the median over the layouts"""
    total_w = math.fsum(weight[q] for q in per_query)
    totals = aggregate(per_query, "sum", weight)
    runs = {q: weight[q] / total_w for q in per_query}
    cost = {q: statistics.median(weight[q] * per_query[q][c] / totals[c] for c in sorted(totals)) for q in per_query}
    return runs, cost


def best(scores):
    """the lowest-scoring cell, ties by name"""
    return min(scores, key=lambda c: (scores[c], c))


# The same three forms for the mix search, whose objective is a sum over queries of each query's costs in the mix cost
# cache (one entry per assignment of the query's parts). sum and additive are such sums already (weights `often` or 1 / c(q, G)). The
# geomean is one through the log: n log(geomean) = sum_q log c(q, L), and log is increasing, so each query's prefix mins
# and the search's bound stay exact. The search's totals must stay >= 0 (its move and tie tests are relative), so the
# geomean's table is log(c / m_q), m_q = the query's smallest usable entry: >= 0, the same argmin, offset log m_q.


def additive_reference(per_query):
    """the additive form's reference layout in the mix search: G, the uniform layout with the lowest geomean (ties by
    name, as the combined panel weights each query 1 / its cost on G). per_query {query: {uniform
    cell: cost}}; a result names G beside its additive number, never only "additive" """
    return best(aggregate(per_query, "geomean"))


def table_values(form, values, inf=float("inf")):
    """(the values the search sums, offset) of one query's full table: sum and additive -> (values, 0.0); geomean ->
    (log(c / m), log m), m the smallest entry below `inf` (an entry at `inf` has no usable plan and stays `inf`).
    Refuses a usable entry <= 0 (no log), and a geomean table with no usable entry"""
    if form in ("sum", "additive"):
        return values, 0.0
    if form != "geomean":
        raise ValueError(f"unknown form {form!r}; one of {FORMS}")
    usable = [v for v in values if v < inf]
    if not usable:
        raise ValueError("geomean: a query with no usable entry")
    if min(usable) <= 0:
        raise ValueError(f"geomean: a cost <= 0 ({min(usable)!r}) has no log")
    m = min(usable)
    return [math.log(v / m) if v < inf else inf for v in values], math.log(m)


def item_weights(form, often, ref_costs=None):
    """each query's weight in the search's sum: sum -> how often it runs; geomean -> 1 (equal contribution, on the
    log table); additive -> 1 / c(q, G) (ref_costs: each query's cost on additive_reference's G, the same model)"""
    if form == "sum":
        return list(often)
    if form == "geomean":
        return [1.0] * len(often)
    if form == "additive":
        if ref_costs is None or len(ref_costs) != len(often):
            raise ValueError("additive needs each query's cost on the reference layout")
        if any(not 0 < c < float("inf") for c in ref_costs):
            raise ValueError("additive: a reference cost <= 0 or unusable")
        return [1.0 / c for c in ref_costs]
    raise ValueError(f"unknown form {form!r}; one of {FORMS}")


def score(form, total, offsets=(), n=None):
    """a search total back in the form's own units (aggregate's): sum -> total; additive -> total / n (the mean of
    c / c_ref); geomean -> exp((total + the sum of table_values' offsets) / n)"""
    if form == "sum":
        return total
    if not n:
        raise ValueError(f"{form} needs n, the number of queries")
    if form == "additive":
        return total / n
    if form == "geomean":
        return math.exp((total + math.fsum(offsets)) / n)
    raise ValueError(f"unknown form {form!r}; one of {FORMS}")


# The equal-share frequency file of a benchmark that defines no frequencies (BI, JOB): a copy of the workload whose query
# q runs 1 / c(q, Sp-Co) times, c its MEASURED combined cost (wops_bytes4k, chunk view half) on Sp-Co at the deciding
# scale (LDBC: SF3; IMDb: full, its only scale). Each query then costs 1 on Sp-Co. ONE file per workload, tracked, read
# by every model (combined, bill, latency) and every scale (SF30 too): a reader sums frequency x
# its per-query cost, as for any workload; changing the weighting = pointing at another file. main() writes only this.

REF = "props_split_adj_condensed"   # Sp-Co
EQUAL_SHARE_MODEL = "wops_bytes4k"
EQUAL_SHARE_CHUNK_VIEW = "half"
EQUAL_SHARE_DIR = "inputs/workloads/{dataset}/equal-share"   # <workload id>.json; each file's "equal_share" says how it was made
# The workloads with an equal-share file, by what it is to them. A reader names the weighting it uses; it never switches
# to the equal share because a file exists.
EQUAL_SHARE_ONLY = ("b01", "j01")       # no frequencies of their own (BI, JOB): the file is their weighting
EQUAL_SHARE_BESIDE = ("i01",)           # frequencies of their own (LDBC's); the file is a second view, never the default
CHECK_TOL = 1e-12        # a frequency against 1 / its cost composed again, relative


def equal_share_id(wid):
    """the id of workload `wid`'s equal-share file (EQUAL_SHARE_DIR/<wid>.json): an id of its own, so an id names one
    file (kvevaluator.capture.workload_file.resolve) and a run on it writes its own places"""
    return f"{wid}-equal-share"


def equal_share_base(eid):
    """the workload an equal-share file's id (equal_share_id's) weights"""
    if not eid.endswith("-equal-share"):
        raise ValueError(f"{eid!r} is not an equal-share file's id (<workload id>-equal-share)")
    return eid[:-len("-equal-share")]


def equal_share_ids():
    """the workloads that have an equal-share file; refuses a file not listed in EQUAL_SHARE_ONLY or EQUAL_SHARE_BESIDE"""
    from kvevaluator.paths import ROOT
    have = sorted(p.stem for p in ROOT.glob(EQUAL_SHARE_DIR.format(dataset="*") + "/*.json"))
    unlisted = [w for w in have if w not in EQUAL_SHARE_ONLY + EQUAL_SHARE_BESIDE]
    if unlisted:
        raise ValueError(f"{EQUAL_SHARE_DIR.format(dataset='*')}: {unlisted} not listed in query_weighting.EQUAL_SHARE_ONLY or EQUAL_SHARE_BESIDE")
    return have


def equal_share_path(wid):
    """the tracked equal-share file of workload `wid`"""
    from kvevaluator.paths import ROOT
    hits = sorted(ROOT.glob(EQUAL_SHARE_DIR.format(dataset="*") + f"/{wid}.json"))
    if len(hits) != 1:
        raise ValueError(f"{wid!r} has {len(hits)} equal-share files, not one (have {', '.join(equal_share_ids())}): "
                         "it has frequencies of its own, or add one with python -m kvcost.layout_cost.query_weighting")
    return hits[0]


def equal_share(wid):
    """the workload `wid` with its equal-share frequencies (benchmarks.read_workload's form: items as (family, query,
    frequency, params) tuples; "equal_share": the dataset and scale it was measured at), read from its tracked file.
    Refuses a file made under another model, chunk view or reference"""
    from kvcost.layout_cost import benchmarks
    path = equal_share_path(wid)
    w = benchmarks.read_workload(path)[1]
    how = w.get("equal_share") or {}
    want = {"model": EQUAL_SHARE_MODEL, "chunk_view": EQUAL_SHARE_CHUNK_VIEW, "reference": REF}
    if w["id"] != equal_share_id(wid) or {k: how.get(k) for k in want} != want or not how.get("dataset") or not how.get("scale"):
        raise ValueError(f"{path}: not an equal-share file of {wid} under {want} (its id {w['id']!r}, equal_share {how})")
    deciding = _deciding_scale(how["dataset"])
    if deciding is not None and how["scale"] != deciding:
        raise ValueError(f"{path}: made at {how['dataset']} {how['scale']}, not its deciding scale {deciding}")
    return w


def equal_share_given(path, wid):
    """equal_share(wid)'s checks for a file a CLI was given: refused unless its bytes are the tracked file's (a copy
    edited or made elsewhere would be read while the tracked file was checked)"""
    tracked = equal_share_path(wid)
    w = equal_share(wid)
    with open(path, "rb") as f:
        if f.read() != tracked.read_bytes():
            raise ValueError(f"{path}: an equal-share file of {wid} that differs from the tracked {tracked}: give the "
                             "tracked file, or remake it with python -m kvcost.layout_cost.query_weighting")
    return w


def _deciding_scale(dataset):
    """the dataset's deciding scale (LDBC: "3"), or None where it has none (IMDb: the file's own scale stands)"""
    from kvevaluator import datasets
    return datasets.get(dataset).deciding_scale


# The once weighting of a benchmark with no frequencies of its own (BI, JOB): every query run once. It is the workload's
# own tracked file (inputs/workloads/ldbc/b01.json, imdb/j01.json: every frequency 1), so no second file; a reader that
# shows it names it (a figure's `once` form beside `spco-share`), as it names the equal share.
ONCE = "once"


def once_path(wid):
    """the tracked workload file of `wid` (one of EQUAL_SHARE_ONLY), at its equal-share file's dataset and scale"""
    if wid not in EQUAL_SHARE_ONLY:
        raise ValueError(f"{wid!r}: the once weighting is for a workload with no frequencies of its own "
                         f"({', '.join(EQUAL_SHARE_ONLY)}); {wid} has its own")
    from pathlib import Path
    from kvevaluator.capture import workload_file as WF
    how = equal_share(wid)["equal_share"]
    return Path(WF.resolve(wid, how["dataset"], how["scale"]))


def once(wid):
    """the workload `wid` with every query once (benchmarks.read_workload's form), read from its own tracked file.
    Refuses a file with a frequency other than 1"""
    from kvcost.layout_cost import benchmarks
    path = once_path(wid)
    w = benchmarks.read_workload(path)[1]
    bad = [q for _, q, f, _ in w["items"] if f != 1]
    if bad:
        raise ValueError(f"{path}: not every query once (frequency != 1 on {bad[:5]})")
    return w


def equal_share_frequencies(wid):
    """{query: its equal-share frequency} of workload `wid`"""
    items = equal_share(wid)["items"]
    freq = {q: f for _, q, f, _ in items}
    if len(freq) != len(items):
        raise ValueError(f"{wid}: two items share a query name")
    return freq


def check_reference_total(freq, ref_cost, what):
    """refuse unless Sp-Co's weighted combined total is the query count (each query costs 1 on Sp-Co): freq and
    ref_cost {query: value}, ref_cost each query's combined cost on Sp-Co"""
    if set(freq) != set(ref_cost):
        raise SystemExit(f"{what}: the equal-share file's queries differ from the costed ones "
                         f"({sorted(set(freq) ^ set(ref_cost))[:5]})")
    total = math.fsum(freq[q] * ref_cost[q] for q in freq)
    if abs(total - len(freq)) > CHECK_TOL * len(freq):
        raise SystemExit(f"{what}: Sp-Co's weighted combined total is {total!r}, not {len(freq)} (the equal-share file is "
                         "not 1 / these costs: python -m kvcost.layout_cost.query_weighting --check)")
    return total


def equal_share_workload(w, ref_costs, wid, title):
    """the workload file (a dict, inputs/workloads/README.md) of w with each item's frequency 1 / ref_costs[i]
    (item_weights' additive weights); w's items are (family, query, frequency, params) tuples"""
    freq = item_weights("additive", [often for _, _, often, _ in w["items"]], ref_costs)
    return {"id": wid, "name": wid.replace("-", "_"), "title": title,
            "items": [{"family": fam, "query": q, "frequency": f, "params": params}
                      for (fam, q, _, params), f in zip(w["items"], freq)]}


def query_rows(w, data, cell, model):
    """[(ops, bytes, cost) of each item on the uniform layout `cell` (props_<P>_adj_<A>), cost in `model`
    (wops_bytes4k or bill_exact)], one run per item, costed as compose costs a mix (data: compose.load's, its Composer
    set up for the model)"""
    from kvcost.layout_cost import compose
    from kvevaluator.layouts import cell_settings
    p, a = cell_settings(cell)      # ValueError when not a cell id; 64 KB read as 'chunked' on both spellings
    return assign_rows(w, data, {u: (p if u[0] == "props" else a) for u in compose.workload_units(w, data)},
                       model, cell)


def assign_rows(w, data, assign, model, name="the mix"):
    """[(ops, bytes, cost) of each item under the assignment `assign` ({part: setting}, a mix or a uniform layout), cost
    in `model` (wops_bytes4k or bill_exact)], one run per item, costed as compose costs a mix"""
    from kvcost.layout_cost import compose
    from kvcost.layout_cost import cost_models as CM
    if model not in ("wops_bytes4k", "bill_exact"):
        raise ValueError(f"model {model!r}: wops_bytes4k or bill_exact")
    out = []
    for fam, q, _, params in w["items"]:
        t = compose.cost_mix({**w, "items": [(fam, q, 1.0, params)]}, data, assign)
        if t is None:
            raise SystemExit(f"{fam} {q}: no plan valid on {name}")
        cur = compose.currencies(t)
        cost = CM.wops_bytes4k(cur["ops"], cur["bytes"]) if model == "wops_bytes4k" else CM.bill_exact(t)
        out.append((cur["ops"], cur["bytes"], cost))
    return out


def query_costs(w, data, cell, model):
    """[each item's cost on the uniform layout `cell` in `model`], one run per item (query_rows' third field)"""
    return [c for _, _, c in query_rows(w, data, cell, model)]


def main(argv=None):
    """write (or --check) a workload's equal-share frequency file from measured captures (the note above EQUAL_SHARE_DIR)"""
    import argparse
    import json
    import os

    from kvcost.layout_cost import benchmarks, compose
    from kvcost.layout_cost.settings import parse_sizes
    ap = argparse.ArgumentParser(description="the equal-share frequency file: each query's frequency 1 / its measured "
                                             "combined cost on Sp-Co at the deciding scale")
    ap.add_argument("--workload", required=True, help="the workload id: one of EQUAL_SHARE_ONLY or EQUAL_SHARE_BESIDE "
                    f"({', '.join(EQUAL_SHARE_ONLY + EQUAL_SHARE_BESIDE)})")
    ap.add_argument("--dataset", help="a NEW file's dataset (an existing file's comes from it)")
    ap.add_argument("--grid", required=True, help="the dataset's captures/<dataset>/<scale> place at the workload's "
                    "scale (each layout read there by output_tree.layout_capture)")
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="Chunked at another chunk size (4k), its "
                    "Chunked/Chunked in --grid's place; repeat per size")
    ap.add_argument("--sf", help="a NEW file's scale: the dataset's deciding scale where it has one; for an existing "
                    "file, refused unless the file's own (one file, one scale)")
    ap.add_argument("--model", help=f"refused unless {EQUAL_SHARE_MODEL} (every model reads the one file)")
    ap.add_argument("--chunk-view", help=f"refused unless {EQUAL_SHARE_CHUNK_VIEW}")
    ap.add_argument("--ref", help=f"refused unless {REF} (Sp-Co)")
    ap.add_argument("--out", help="where to write (default: the tracked file)")
    ap.add_argument("--check", action="store_true", help="compare the tracked file with the captures; write nothing")
    ap.add_argument("--scores-out", metavar="DIR", help="also every uniform layout over Split, Condensed and Chunked at "
                    "the grid's and each --size's chunk size: per_query.csv (each query's ops, bytes and cost, its weight)"
                    " and scores.csv (each layout's equal-share score, the mean over queries of cost / its cost on Sp-Co)")
    a = ap.parse_args(argv)
    dataset, sf, path = _target(a)
    a.model, a.ref = EQUAL_SHARE_MODEL, REF                     # write_scores reads both
    (w,) = benchmarks.select(dataset, sf, a.workload).values()
    composer = compose.Composer(criterion=None, sizes=parse_sizes(a.size, a.grid), plan_rule=compose.PlanRule(),
                                families=benchmarks.families_read(dataset, [w]),
                                chunk_view=compose.resolve_chunk_view(EQUAL_SHARE_CHUNK_VIEW), bill="")
    data = compose.load(os.path.abspath(a.grid), False, dataset, composer=composer)
    if str(data.sf) != str(sf):
        raise SystemExit(f"query_weighting: --grid {a.grid} is {dataset} {data.sf}; the {a.workload} file is made at "
                         f"{sf} only (one file, one scale)")
    ref_costs = query_costs(w, data, REF, EQUAL_SHARE_MODEL)
    if a.check:
        return _check(a.workload, w, ref_costs, path)
    title = (f"Equal-share frequencies of {w['id']} ({w.get('title', w['id'])}): each query weighted 1 / its measured "
             f"{EQUAL_SHARE_MODEL} on Sp-Co at {dataset} {sf} (chunk view {EQUAL_SHARE_CHUNK_VIEW}), so each costs 1 on Sp-Co")
    out = equal_share_workload(w, ref_costs, equal_share_id(a.workload), title)
    out = {"id": out["id"], "name": f"{a.workload}_equal_share", "title": out["title"],
           "equal_share": {"dataset": dataset, "scale": sf, "model": EQUAL_SHARE_MODEL,
                           "chunk_view": EQUAL_SHARE_CHUNK_VIEW, "reference": REF}, "items": out["items"]}
    from kvevaluator.paths import ROOT
    dest = a.out or str(path or ROOT / EQUAL_SHARE_DIR.format(dataset=dataset) / f"{a.workload}.json")
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    total = math.fsum(fq * c for fq, c in zip((it["frequency"] for it in out["items"]), ref_costs))
    print(f"query_weighting: {len(out['items'])} queries of {w['id']} weighted 1 / {EQUAL_SHARE_MODEL} on {REF} "
          f"({dataset} {sf}, chunk view {EQUAL_SHARE_CHUNK_VIEW}); the weighted total on {REF} = {total!r} -> {dest}")
    if a.scores_out:
        write_scores(a, w, data, [it["frequency"] for it in out["items"]])
    return 0


def _target(a):
    """(dataset, scale, the tracked path or None for a new file) of main's workload; refuses any setting but the rule's"""
    from kvevaluator import datasets
    _listed(a.workload)
    if (equal_share_path_or_none := _existing(a.workload)) is not None:
        how = equal_share(a.workload)["equal_share"]
        dataset, sf, path = how["dataset"], how["scale"], equal_share_path_or_none
        if a.dataset is not None and a.dataset != dataset:
            raise SystemExit(f"query_weighting: --dataset {a.dataset}: {path} is {dataset}'s")
    else:
        if a.check:
            raise SystemExit(f"query_weighting --check: {a.workload} has no equal-share file")
        if not a.dataset or not (a.sf or datasets.get(a.dataset).deciding_scale):
            raise SystemExit("query_weighting: a new equal-share file needs --dataset, and --sf where the dataset has no "
                             "deciding scale")
        dataset, path = a.dataset, None
        sf = datasets.get(dataset).deciding_scale or a.sf
        from kvcost.layout_cost import benchmarks
        (base,) = benchmarks.select(dataset, sf, a.workload).values()
        _listed(a.workload, own=any(f != 1.0 for _, _, f, _ in base["items"]))
    for flag, given, rule in (("--sf", a.sf, sf), ("--model", a.model, EQUAL_SHARE_MODEL),
                              ("--chunk-view", a.chunk_view, EQUAL_SHARE_CHUNK_VIEW), ("--ref", a.ref, REF)):
        if given is not None and given != rule:
            raise SystemExit(f"query_weighting: {flag} {given}: the equal-share file is one per workload, 1 / the measured "
                             f"{EQUAL_SHARE_MODEL} on {REF} at {dataset} {sf} (chunk view {EQUAL_SHARE_CHUNK_VIEW}), read "
                             "by every model and every scale (one file); no other file is written")
    return dataset, sf, path


def _listed(wid, own=None):
    """refuse a workload in neither EQUAL_SHARE_ONLY nor EQUAL_SHARE_BESIDE, and (own: whether it has frequencies of its
    own) one listed in the wrong list"""
    if wid not in EQUAL_SHARE_ONLY + EQUAL_SHARE_BESIDE:
        raise SystemExit(f"query_weighting: {wid} is in neither EQUAL_SHARE_ONLY {EQUAL_SHARE_ONLY} nor "
                         f"EQUAL_SHARE_BESIDE {EQUAL_SHARE_BESIDE}: list it there first, with what the file is to it")
    if own and wid not in EQUAL_SHARE_BESIDE:
        raise SystemExit(f"query_weighting: {wid} has frequencies of its own; an equal-share file is for a workload "
                         "that runs each query once (BI, JOB), or one listed in EQUAL_SHARE_BESIDE")
    if own is False and wid in EQUAL_SHARE_BESIDE:
        raise SystemExit(f"query_weighting: {wid} runs each query once; list it in EQUAL_SHARE_ONLY")


def _check(wid, w, ref_costs, path):
    """main --check: 0 when each tracked frequency x the composed Sp-Co cost is 1 within CHECK_TOL, else 1"""
    have = equal_share_frequencies(wid)
    queries = [q for _, q, _, _ in w["items"]]
    if sorted(have) != sorted(queries):
        raise SystemExit(f"query_weighting --check FAIL: {path} names other queries than {w['id']}")
    bad = [q for q, c in zip(queries, ref_costs) if abs(have[q] * c - 1) > CHECK_TOL]
    worst = max(abs(have[q] * c - 1) for q, c in zip(queries, ref_costs))
    print(f"query_weighting --check {'FAIL' if bad else 'OK'}: {len(queries)} queries of {wid}, frequency x "
          f"Sp-Co's measured {EQUAL_SHARE_MODEL} = 1 within {worst:.1e} ({len(bad)} off by more than {CHECK_TOL}): "
          f"{path}")
    return 1 if bad else 0


def _existing(wid):
    """the tracked equal-share file of `wid`, or None"""
    try:
        return equal_share_path(wid)
    except ValueError:
        return None


def write_scores(a, w, data, weight):
    """every uniform layout's per-query rows and its equal-share score (main's --scores-out)"""
    import csv
    import os

    from kvcost.layout_cost.settings import parse_sizes
    settings = list(SETTINGS) + [opt for opt, _, _ in parse_sizes(a.size, a.grid)]
    rows, scores = [], []
    for props in settings:
        for adj in settings:
            cell = cell_id(props, adj)
            qr = query_rows(w, data, cell, a.model)
            for (_, q, _, _), wq, (ops, nbytes, cost) in zip(w["items"], weight, qr):
                rows.append({"model": a.model, "cell": cell, "props": props, "adjacency": adj, "query": q,
                             "ops": repr(ops), "bytes": repr(nbytes), "cost": repr(cost), "weight": repr(wq)})
            total = math.fsum(wq * c for wq, (_, _, c) in zip(weight, qr))
            scores.append({"model": a.model, "cell": cell, "props": props, "adjacency": adj,
                           "score": repr(total / len(weight)), "weighted_total": repr(total)})
    os.makedirs(a.scores_out, exist_ok=True)
    for name, data_rows in (("per_query.csv", rows), ("scores.csv", scores)):
        with open(os.path.join(a.scores_out, name), "w", newline="") as f:
            wr = csv.DictWriter(f, list(data_rows[0]), lineterminator="\n")
            wr.writeheader()
            wr.writerows(data_rows)
    ref = next(s["score"] for s in scores if s["cell"] == a.ref)
    best = min(scores, key=lambda s: (float(s["score"]), s["cell"]))
    print(f"query_weighting: {len(scores)} uniform layouts x {len(weight)} queries ({len(rows)} rows); {a.ref} scores "
          f"{float(ref):.6f}; the best {best['cell']} {float(best['score']):.6f} -> {a.scores_out}")


if __name__ == "__main__":
    raise SystemExit(main())
