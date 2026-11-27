"""kvcost search-mix and kvcost cost-layouts: the mix search (python/kvcost/layout_search/questions.py answers;
python/kvcost/layout_search/results.py keeps the rows) and the per-query costs it reads (python/kvcost/layout_search/export.py).

    kvcost cost-layouts --workload my.json [Sp-Co ...] [--mix ...]          each query's plan and cost on each layout and mix,
                                                                       every model, both chunk views (layout_costs)
    kvcost search-mix   --workload my.json --cost-model wops_bytes4k [--byte-limit 2x] [--trace]
    kvcost search-mix   --question i01-any-size-best --dataset ldbc=3  one mix question the paper reads (mix_questions)

Every form reads the captures/<dataset>/<scale>/ of the tree --from names (default the output dir; each layout by
output_tree.layout_capture: measured itself, else composed) and writes the output dir's summary/<dataset>/<scale>/:
cost-layouts into layout-costs/<workload id>/<count source>/ (kvcost.layout_cost.layout_costs), search-mix one row into mix-search/<workload id, or the question's rows>/<option set>.json (results.home), beside
it the rows that answer with one mix as <option set>.csv and the row's mix as mixes/<option set>-<search name>.json
and mixes/best.json (what check-mix builds). Picking is a rule inside both: each query's plan is picked for every
layout under wops_bytes4k on the held-out params, whatever the model. Each search: load the captures (compose.load under the run's
plan rule), stop there when the result file holds a row searched on the same inputs (reuse_row; --fresh searches
again), build the mix cost cache where the search uses it (cost_cache_use: --cost-cache, or a search that needs it;
saved), answer the question, price the answer again from the captures, upsert one row, and print the verdict and the
row's place.
"""
import argparse
import os
import re
import time

import glob
import hashlib
import json

from kvevaluator import layouts as L
from kvevaluator import weights
from kvevaluator.capture import capture_layout
from kvevaluator.capture import workload_file as WF
from kvevaluator.output_tree import Capture, Tree, capture_in, layout_capture, writes_in, writes_layout_capture
from kvcost.cli import predict as PC
from kvcost.cli import runs
from kvcost.cli import workloads as W
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import compose
from kvcost.layout_cost.settings import parse_sizes
from kvcost.layout_search import bodies as BD
from kvcost.layout_search import export as X
from kvcost.layout_search import questions as Q
from kvcost.layout_search import results as R
from kvcost.layout_search import search as S

# option set -> by direction at load; per-type-any-size: the three settings per type plus Chunked at each --size (the
# chunk size as one more setting per part, priced from that size's measured Chunked/Chunked capture: Composer.sizes)
OPTION_SETS = {"per-type": False, "per-type-and-direction": True, "per-type-any-size": False}


def load_workload(dataset, sf, spec):
    """a workload: a workload file (inputs/workloads/README.md: id, title, items: family, query, frequency, params) or an
    id or name (kvevaluator.capture.workload_file.resolve)"""
    _, w = benchmarks.read_workload(WF.resolve(spec, dataset, sf))
    w.setdefault("title", w["id"])
    return w


def load_composed(a, ad, root, fams, plan_rule, sizes, by_dir, bill, view):
    """the loaded parts of the captures under `root` (a place's captures, or a predicted set's dir), costed under the
    call's op weights and plan rule (--criterion over `plan_rule`): the one Composer a search and a mix's cost build.
    by_dir: the adjacency parts per direction; bill: compose.Composer's (CM.needs_bill); view: the chunk view of a
    write's vector. -> compose.load's data"""
    w = weights.parse(a.weights)
    criterion, plan_rule = compose.pick_rule_under(a.criterion, plan_rule, w)
    composer = compose.Composer(criterion=criterion, families=fams, plan_rule=plan_rule, sizes=sizes,
                                weighting=a.weighting, bill=bill, param_set=getattr(a, "param_set", None),
                                weights=tuple(w[k] for k in "GSN"), chunk_view=view)
    return compose.load(root, by_dir, ad.DATASET, composer=composer)


def _setup(a, ad, plan_rule):
    """(data, workload, units, the result file, the source kinds) of a run"""
    t0 = time.time()
    root = getattr(a, "captures", None) or str((Tree(a.from_dir) if a.from_dir else Tree()).place_dir("captures", ad.DATASET, a.sf))
    model = (getattr(a, "model", None) or "wops")
    fams = ad.FAMILIES if getattr(a, "wrun", None) is None else tuple(
        (f, k) for f, k in ad.FAMILIES if f in a.wrun.families())          # a run prices the families it measured
    source = getattr(a, "source", "measured")
    if source != "measured":
        refuse_sized_bill(a.sub, source, model)
        root, fams, plan_rule, sizes = _predicted_setup(a, ad, plan_rule)
    else:
        sizes = tuple(parse_sizes(a.size, root))     # each size's ChK-ChK in the same place as every layout
    if source == "measured" and R.predicted_capture(root):
        raise SystemExit(f"[kvcost {a.sub}] {R.predicted_capture(root)} is a predicted capture: give --count-source <its set> "
                         f"(e.g. --count-source {PC.default_set()}), so its own plan rule prices it")
    data = load_composed(a, ad, root, fams, plan_rule, sizes, OPTION_SETS[a.option_set], CM.needs_bill(model),
                         compose.default_chunk_view(model))
    w = load_workload(ad.DATASET, a.sf, a.workload)
    units = compose.workload_units(w, data)
    kinds = R.source_kinds(data)
    seen = R.check_sources(kinds)
    path = result_path(a, R.dataset_scale_dir(ad.DATASET, a.sf), w["id"])
    if not getattr(a, "setup_said", False):     # cost-layouts loads once per output: said once
        a.setup_said = True
        print(f"[kvcost {a.sub}] {w['id']} ({w.get('title', '')}), {a.option_set}: {len(units)} parts, "
              f"{len(w['items'])} queries; counts {', '.join(seen)} under {runs.rel(root)}; loaded in "
              f"{time.time() - t0:.1f} s", flush=True)
    return root, data, w, units, path


def result_path(a, base, wid, suffix=""):
    """the search's result file: <base>/mix-search/<the workload's id, or a question's rows dir>/<stem>[<suffix>].json
    (results.stem: mixes-of-<settings>[-by-direction])"""
    return R.home(base, getattr(a, "rows_name", None) or wid, a.option_set, suffix, a.size)


# a Chunked/Chunked cell at one size on both sides (settings.sized_cell), its size captured
SIZED_DIAGONAL = re.compile(r"props_chunked_c(\d+k)_adj_chunked_c\1")

def predicted_sizes(root, dataset, sf, source):
    """the bare sizes (4k, ...) whose Chunked/Chunked cell a predicted set holds (kvcost predict ... Ch4k-Ch4k), by chunk size"""
    from kvcost.cli.predict import predicted_place
    set_dir = predicted_place(root, dataset, sf, source)
    cells = {os.path.basename(p)[:-4] for p in glob.glob(os.path.join(set_dir, "*", "*", "counts", "props_chunked_c*.csv"))}
    return sorted({m.group(1) for c in cells if (m := SIZED_DIAGONAL.fullmatch(c))}, key=lambda k: int(k[:-1]))


def refuse_sized_bill(verb, source, model):
    """bill_exact on a predicted set is refused by name: it bills each request rounded up alone, from the exact unit
    columns only a measured capture has (a predicted row carries requests and bytes: bill_lower and bill_upper)"""
    if CM.needs_bill(model) == "sized":
        raise SystemExit(f"kvcost {verb} --count-source {source}: --cost-model {model} needs a measured capture's exact unit columns; "
                         f"a predicted set gives bill_lower and bill_upper")


def require_grid(a, set_dir, fam, cap):
    """the composer reads each part from the grid's cell that holds its setting: a predicted set's nine grid cells of `fam`
    (its capture dir `cap`) are all there, else refused, naming the kvcost predict call that makes them"""
    missing = [L.name_of(c) for c in compose.CELLS if not os.path.exists(Capture(cap).record(c))]
    if missing:
        raise SystemExit(f"[kvcost {a.sub}] --count-source {a.source}: {set_dir} holds no predicted {fam} capture of "
                         f"{', '.join(missing)}: a mix is composed from the nine grid layouts ({PC.predict_call(a.source)} "
                         f"{' '.join(missing)})")


def _predicted_setup(a, ad, plan_rule):
    """(the set dir, the families, the plan rule, the sizes) of a search over a predicted capture set (--count-source <set>: its
    place in the tree --from names, the output dir by default: predicted/<dataset>/<scale>/<set>/): the
    workload's families, a write family's parts from its predicted writes capture (kvcost predict on a run), the grid's
    three settings and Chunked at each --size (that size's Chunked/Chunked cell in the same predicted set: kvcost predict
    ... Ch4k-Ch4k), and the plan rule (candidates.pick_body): in-sample on the capture's own rows (no held-out params are
    predicted), with no pick record and no anchor, so no measured decision enters"""
    w = load_workload(ad.DATASET, a.sf, a.workload)
    used = {fam for fam, *_ in w["items"]}
    from kvcost.cli.predict import predicted_place
    set_dir = str(predicted_place(a.from_dir, ad.DATASET, a.sf, a.source))
    sizes = tuple(parse_sizes(a.size, set_dir))
    for fam in sorted(used):
        cap = capture_in(set_dir, fam, getattr(a, "param_set", None)).path
        if not R.predicted_capture(cap):
            raise SystemExit(f"[kvcost {a.sub}] --count-source {a.source}: {set_dir} holds no predicted capture of {fam} "
                             f"(kvcost predict --save-as {a.source})")
        require_grid(a, set_dir, fam, cap)
        recs = sorted(glob.glob(os.path.join(Capture(cap).dir("counts"), "*.json")))
        old = [os.path.basename(r)[:-5] for r in recs if "workload" not in json.load(open(r))]
        if old:   # read as the family dir's workload (a read family here), their bodies' names looked up at HEAD
            from kvevaluator import stale
            stale.note("capture_shape_moved", f"{cap}: {len(old)} records name no workload or plan tree (an older "
                       f"record shape): the workload from its dir ({fam}), the bodies' names looked up at HEAD; run kvcost "
                       f"predict again to clear it")
    fams = tuple((f, k) for f, k in ad.FAMILIES if f in used)
    for oid, sroot, scell in sizes:      # each size: a predicted capture of its cell, for every family the workload uses
        for fam, kind in fams:
            cap = (writes_in(sroot, ad.DATASET, getattr(a, "param_set", None)) if kind != "read"
                   else capture_in(sroot, fam, getattr(a, "param_set", None)))
            if not os.path.exists(cap.path_of("parts", scell)) or not R.predicted_capture(cap.path):
                raise SystemExit(f"[kvcost {a.sub}] --count-source {a.source} --size {oid[len('chunked_c'):]}: {os.path.relpath(sroot)} "
                                 f"holds no predicted {fam} capture of {scell} ({PC.predict_call(a.source)} "
                                 f"Ch{oid[len('chunked_c'):]}-Ch{oid[len('chunked_c'):]}, beside the nine grid layouts); a "
                                 f"size of a predicted search is predicted too")
    # in-sample by policy for these families (as the microbench is): the composer's in-sample argmin, no record
    return set_dir, fams, plan_rule.but(in_sample=True, in_sample_default=tuple(sorted(used)), record=None, anchor=False,
                                        pin=False, tie_digits=12), sizes


def search_inputs(a, data, w):
    """the key of what one search-mix call reads and is asked: the workload's queries on the loaded captures under the
    composer's settings (S.inputs_key: no code in it), and the question (the model, a front or not, the byte limit or the
    limits with a limits file's content, the node and time limits, the search, the settings a part may take, how the
    queries combine). Not in it: what only changes how the answer is found (--cost-cache, --no-cost-cache, --jobs)"""
    limits = a.limits
    if str(limits).startswith("file:"):
        with open(limits[len("file:"):], "rb") as f:
            limits = ["file", hashlib.sha256(f.read()).hexdigest()]
    asked = {"model": a.model, "front": bool(a.front), "walk": not a.no_walk, "byte_limit": a.byte_limit,
             "limits": limits, "max_nodes": a.max_nodes, "walk_seconds": a.walk_seconds, "search": a.search,
             "option_set": a.option_set, "weighting": a.weighting}
    return hashlib.sha256(json.dumps([S.inputs_key(data, w), asked], sort_keys=True).encode()).hexdigest()[:32]


def reuse_row(a, path, inputs):
    """True when the result file holds a row searched on the same inputs and this call takes it (no --fresh; no
    --trace, which follows a search): the call's answer says so, and the row's run id is kept for the mix written
    from it"""
    row = None if a.fresh or a.trace is not None else R.answered(path, inputs)
    if row is None:
        return False
    a.row_run_id = row["run_id"]
    a.answer = (f"already answered: the row of run {row['run_id']} in {runs.rel(path)} was searched on the same inputs "
                f"({'proven' if row['proven'] else 'not proven'}); nothing searched (--fresh searches again)")
    return True


def _finish(a, row, root, data, pb, w, path, argv, dataset, inputs):
    keys = pb.cost_cache_keys if pb is not None else []    # no cost cache: no keys
    row.update(plan_rule=R.plan_rule_text(data.composer.plan_rule), weighting=data.composer.weighting,
               weights=weights.text(data.composer.w), chunk_view=data.composer.chunk_view,
               criterion=data.composer.criterion, capture=runs.recorded_argv([os.path.abspath(root)])[0],
               option_set=a.option_set, sizes=[[oid, runs.recorded_argv([os.path.abspath(sroot)])[0], cell]
                                               for oid, sroot, cell in data.composer.sizes],
               walk_seconds=getattr(a, "walk_seconds", None),
               search=a.search,
               solver=row.get("solver", "cost-cache"),
               workload=w["id"], frequencies=R.frequencies(w["items"]),
               workload_file=R.workload_file(a.workload),
               benchmark=a.dataset, scale=str(a.sf), run_id=R.run_id(),
               command="kvcost " + " ".join(runs.recorded_argv(argv)),
               identity=R.identity(data, root, keys, dataset, inputs))
    replaced = R.ResultFile(path).upsert(row)
    return replaced


def _needs_cost_cache(a):
    """(why, what to give instead) when this search builds the mix cost cache where it fits, else None. `instead` None:
    it runs without it too, but much slower (the solve without it bounds bytes loosely: the fewest bytes, a byte
    limit, the staircase), so above COST_CACHE_LIMIT it runs without it; else it cannot run without it, and is
    refused there"""
    return _front_needs_cost_cache(a) if a.front else _best_mix_needs_cost_cache(a)


def _front_needs_cost_cache(a):
    """_needs_cost_cache for --front and a --byte-limit list (the staircase)"""
    if not a.no_walk:
        return "--front's walk", "give --byte-limit a list and no --front: the staircase without the cost cache"
    if a.model is not None:
        return (f"a {a.model} staircase (its read units against its write units)",
                "no --cost-model: the staircase without the cost cache is wops against bytes")
    if a.limits == "caps":
        return ("--byte-limit caps (its limits are read off the cost cache: front.caps_of)",
                "give the limits as file:, named, a list of B or bytes:")
    return "a --byte-limit list (the staircase: faster on the cost cache, whose bound on bytes is tight)", None


def _best_mix_needs_cost_cache(a):
    """_needs_cost_cache for the fewest of one model, within a byte limit or not"""
    if a.model not in BD.LINEAR:
        return (f"{a.model} (not linear in the counts: the solve without the cost cache takes {', '.join(BD.LINEAR)})",
                "--search local answers it, not proven")
    if a.trace is not None:
        return "--trace (it follows the search over the cost cache)", "no --trace"
    if a.model == "bytes" or a.byte_limit:
        return (f"{'the fewest bytes' if a.model == 'bytes' else '--byte-limit'} (faster on the cost cache, whose bound "
                f"on bytes is tight)", None)
    return None


def cost_cache_use(a, w, largest):
    """whether this search builds the mix cost cache: the one place that decides. Off by default (the solve on each
    query's bodies: the same answer, faster for the fewest wops or wops_bytes4k with no byte limit); --cost-cache
    turns it on; a search that
    cannot run without it (_needs_cost_cache: a bill model, --front's walk, a bill staircase, --byte-limit caps,
    --trace) or runs much faster on it (the fewest bytes, a byte limit, the staircase) turns it on
    itself and says so; --no-cost-cache keeps it off for a search that can run without it (the others are refused).
    Never above COST_CACHE_LIMIT entries in the largest query's (JOB): there --cost-cache is skipped, said in one
    line, the staircase runs without it, and a search that cannot is refused. When on: saved in its one place
    (S.apply_cache_args; --fresh builds it again)"""
    need = _needs_cost_cache(a)
    if getattr(a, "no_cost_cache", False):
        if a.cost_cache or (need and need[1] is not None):
            raise SystemExit("--no-cost-cache: " + ("--cost-cache asks for it" if a.cost_cache else
                                                    f"{need[0]} needs the mix cost cache: {need[1]}"))
        return False
    if largest > BD.COST_CACHE_LIMIT:
        too_big = (f"the largest query's cost cache would hold {largest:.3g} entries (over {BD.COST_CACHE_LIMIT:.0e}), so "
                   f"none is built")
        if need and need[1] is not None:
            raise SystemExit(f"{w['id']}: {need[0]} needs the mix cost cache, and {too_big}: {need[1]}")
        if a.cost_cache:
            print(f"[kvcost search-mix] --cost-cache: {too_big}", flush=True)
        use = False
    else:
        use = bool(need) or a.cost_cache
        if need and not a.cost_cache:
            print(f"[kvcost search-mix] {need[0]}{' needs the mix cost cache' if need[1] else ''}: building the mix "
                  f"cost cache", flush=True)
    if use:
        S.apply_cache_args(a)
    return use


JOBS_HELP = ("how many it runs at once, a process each: the mix cost cache's queries while it is built, and a "
             "--byte-limit list's limits (the staircase, with the cost cache or without). The answer and the cost "
             "cache's files are the same at any number. Default: the machine's cores, and no more than the available "
             "memory allows (printed with its reason); --jobs 1: one at a time")


def at_once(a, what, building=0):
    """how many of `what` (the cost cache's queries, a staircase's limits) this call runs at once, each in a forked
    process: --jobs, else the one rule (run_measure.at_once): the machine's cores, and no more than the available
    memory over one process's peak, which is what this process holds now (a fork can come to copy all of it) plus,
    building the cost cache, the largest query's `building` entries x S.FILL_BYTES. Printed with its reason"""
    from kvevaluator.measure import run_measure as M
    own = M.own_gb()
    peak = None if own is None else own + building * S.FILL_BYTES / 2**30
    n, why = M.at_once(a.jobs, 1, peak, "a process", "this process's size is not known")
    print(f"[kvcost search-mix] {what}: {why}", flush=True)
    return n


def cmd_best_mix(a, argv, ad, plan_rule):
    t0 = time.time()
    root, data, w, units, path = _setup(a, ad, plan_rule)
    inputs = search_inputs(a, data, w)
    if reuse_row(a, path, inputs):
        return 0
    if a.trace == "":            # beside the result file: <option set>-<search name>-trace.csv
        a.trace = f"{path[:-len('.json')]}-{search_name(a, a.model)}-trace.csv"
    largest = BD.largest_cost_cache(w, data, units)
    ref = Q.reference_bytes(w, data, units)
    if getattr(a, "byte_limit_bytes", None) is not None:     # --byte-limit 68MB: B = that over the reference's bytes
        a.within = a.byte_limit_bytes / ref
        print(f"[kvcost search-mix] --byte-limit {a.byte_limit}: {a.byte_limit_bytes:,.0f} bytes = B {a.within:.6g} x "
              f"{ref:,.0f} (Sp-Co's bytes)")
    if a.trace and a.weighting != "sum":
        raise SystemExit(f"--trace writes totals in wops and bytes: under the {a.weighting} weighting the search's totals "
                         f"are in that form's units, so the trace takes the sum weighting only")
    trace = [] if a.trace else None
    if not cost_cache_use(a, w, largest):       # branch on each query's bodies
        if largest > BD.COST_CACHE_LIMIT and a.max_nodes is None:
            raise SystemExit(f"{w['id']}: the largest query's cost cache has {largest:.3g} entries (over {BD.COST_CACHE_LIMIT:.0e}), so "
                             f"the solve without the cost cache answers it; it is not expected to finish: give --max-nodes")
        print(f"[kvcost search-mix] the solve without the cost cache (the largest query's cost cache: {largest:.3g} entries)", flush=True)
        pb, row = None, Q.best_mix_no_cost_cache(data, w, units, a.model, a.within, ref, a.max_nodes)
    else:
        t1 = time.time()
        pb = Q.build(w, data, units, at_once(a, "the cost cache's queries", largest), a.model)
        t_cost_cache = time.time() - t1
        row = Q.best_mix(pb, data, w, a.model, a.within, ref, a.max_nodes, trace=trace)
        row["times"] = {"cost_cache_s": round(t_cost_cache, 3), **row["times"]}
    replaced = _finish(a, row, root, data, pb, w, path, argv, ad.DATASET, inputs)
    if trace is not None:
        cols, rows = X.trace_rows(trace, a.model)
        X.write_csv(a.trace, cols, rows)
        print(f"[kvcost search-mix] trace: {len(rows)} decisions in {runs.rel(a.trace)} (the row's {row['nodes']} nodes"
              + (", with an untraced fewest-bytes search: no mix fits the limit)" if not row["fits"] else ")"))
    t = row["totals"]
    what = f"the fewest {a.model}" + (f" within {a.within:g} x {runs.layout_name(row['bytes_reference'])}'s bytes"
                                      if a.within is not None else "")
    if row["score"]:
        sc = row["score"]
        print(f"[kvcost search-mix] the {sc['form']} of {a.model} over {sc['queries']} queries: {sc['value']:.6g}"
              f"{' (each query over its cost on G = ' + sc['additive_reference'] + ')' if 'additive_reference' in sc else ''}")
    a.answer = (f"{'PROVEN' if row['proven'] else 'NOT PROVEN (node limit)'}: {what}: " if row["fits"] else
                f"NO MIX FITS {what.split(' within ', 1)[1]}: the fewest-bytes mix instead, "
                f"{'proven' if row['proven'] else 'not proven (node limit)'}: ")
    a.answer += (
                f"{', '.join(format(t[m], '.6g') + ' ' + m for m in ([a.model] if a.model not in ('wops', 'bytes') else []) + ['wops', 'bytes'])}")
    print(f"[kvcost search-mix] {row['nodes']} nodes; started from {row['start']['used'] or 'no mix (none fits)'}"
          f"{' (from ' + row['start']['best_uniform'] + ')' if row['start']['used'] == 'the two-step mix' else ''}")
    if row.get("bound"):
        bd = row["bound"]
        print(f"  bound {bd['value']:.6g} (the root's {bd['root']:.6g}): " +
              ("proven optimal" if row["proven"] else
               f"the gap to it is {bd['gap']:.1%}, open" if bd["gap"] is not None else "no mix fits"))
    print(f"  times: {', '.join(f'{k} {v}' for k, v in row['times'].items())}; all {time.time() - t0:.1f} s")
    print(f"  row {'replaced' if replaced else 'added'} in {runs.rel(path)}")
    return 0


def cmd_front(a, argv, ad, plan_rule):
    if a.weighting != "sum":
        raise SystemExit(f"front under the {a.weighting} weighting: the front's wops and bytes are sums over queries")
    t0 = time.time()
    root, data, w, units, path = _setup(a, ad, plan_rule)
    inputs = search_inputs(a, data, w)
    if reuse_row(a, path, inputs):
        return 0
    largest = BD.largest_cost_cache(w, data, units)
    if not cost_cache_use(a, w, largest):     # the staircase only, one solve without the cost cache per limit
        return _front_no_cost_cache(a, argv, ad, root, data, w, units, path, largest, t0, inputs)
    axes = Q.axes_of(a.model)                    # wops against bytes, or a bill form's read units against write units
    t1 = time.time()
    pb = Q.build(w, data, units, at_once(a, "the cost cache's queries", largest), a.model)
    t_cost_cache = time.time() - t1
    ref = Q.reference_bytes(w, data, units, None if not axes.lanes else axes)
    row = Q.front(pb, data, w, units, ref, a.max_nodes, a.limits, walk=not a.no_walk, walk_seconds=a.walk_seconds,
                  axes=axes, jobs=at_once(a, "the staircase's limits") if a.limits else 1)
    row["times"] = {"cost_cache_s": round(t_cost_cache, 3), **row["times"]}
    replaced = _finish(a, row, root, data, pb, w, path, argv, ad.DATASET, inputs)
    if a.no_walk:
        unproven = sum(not x["proven"] for x in row["staircase"])
        verdict = "PROVEN" if row["proven"] else f"NOT PROVEN: {unproven} limits at the node limit"
        print(f"[kvcost search-mix --front] {verdict}: staircase at {len(row['staircase'])} limits (no walk), {row['nodes']} nodes")
        a.answer = f"{verdict}: the staircase at {len(row['staircase'])} limits (no walk)"
    else:
        stopped = (f"the walk stopped at --max-seconds {a.walk_seconds:g} after {row['steps']} steps"
                   if row["walk_complete"] is False and a.walk_seconds is not None and row["times"].get("walk_s", 0) >= a.walk_seconds
                   else None)
        verdict = ("PROVEN COMPLETE" if row["proven"] else
                   f"INCOMPLETE: {stopped} ({len(row['points'])} points, each exact)" if stopped and not row["unproven_steps"] else
                   f"NOT PROVEN: {row['unproven_steps']} of {row['steps']} steps at the node limit" + (f"; {stopped}" if stopped else ""))
        n_stair = sum(x["proven"] for x in row["staircase"])
        stair = (f"; staircase at {len(row['staircase'])} limits, {n_stair} proven"
                 + (", equal to the walk there" if row["walk_complete"] else "") if row["staircase"] else "")
        print(f"[kvcost search-mix --front] {verdict}: {axes.x} against {axes.y}, {len(row['points'])} points, "
              f"{row['nodes']} nodes{stair}")
        a.answer = f"{verdict}: the front, {len(row['points'])} points"
    for name, p in row["named"].items():
        print(f"  {name}: " + ("no mix fits" if p is None else f"{p[axes.x]:.6g} {axes.x}, {p[axes.y]:.6g} {axes.y}"))
    print(f"  times: {', '.join(f'{k} {v}' for k, v in row['times'].items())}; all {time.time() - t0:.1f} s")
    print(f"  row {'replaced' if replaced else 'added'} in {runs.rel(path)}")
    return 0


def _front_no_cost_cache(a, argv, ad, root, data, w, units, path, largest, t0, inputs):
    """a --byte-limit list without the cost cache (cost_cache_use: off, or it does not fit): the staircase at --limits,
    each limit one solve without the cost cache (Q.staircase_no_cost_cache)"""
    why = (f"the largest query's cost cache has {largest:.3g} entries" if largest > BD.COST_CACHE_LIMIT
           else "--no-cost-cache")
    if a.max_nodes is None and largest > BD.COST_CACHE_LIMIT:
        raise SystemExit(f"{w['id']}: {why}; the solves without the cost cache are not expected to finish: give --max-nodes")
    ref = Q.reference_bytes(w, data, units)
    print(f"[kvcost search-mix --front] the staircase without the cost cache ({why})", flush=True)
    row = Q.staircase_no_cost_cache(data, w, units, "wops", a.limits, ref, a.max_nodes,
                                    jobs=at_once(a, "the staircase's limits"))
    replaced = _finish(a, row, root, data, None, w, path, argv, ad.DATASET, inputs)
    stair = row["staircase"]
    verdict = "PROVEN" if row["proven"] else f"NOT PROVEN: {sum(not x['proven'] for x in stair)} limits at the node limit"
    print(f"[kvcost search-mix --front] {verdict}: staircase at {len(stair)} limits (no walk, no cost cache), {row['nodes']} nodes")
    for st in stair:
        gap = "" if st["proven"] or not st["wops"] else f", within {1 - st['bound'] / st['wops']:.1%} of the bound"
        print(f"  {st['cap_x_reference']:.4g}x: " + ("no mix fits" if st["wops"] is None else
              f"{st['wops']:.6g} wops, {st['bytes']:.6g} bytes, {'proven' if st['proven'] else 'not proven'}{gap}"))
    a.answer = f"{verdict}: the staircase without the cost cache at {len(stair)} limits"
    print(f"  all {time.time() - t0:.1f} s; row {'replaced' if replaced else 'added'} in {runs.rel(path)}")
    return 0


def require_composed(run, tree=None):
    """where the run's layouts' counts are: the captures/<dataset>/<scale> place of the tree read (--from; default the
    output dir), each layout read from it by output_tree.layout_capture: measured itself, else composed; in the output
    dir, refused when a query of the run has a failed run"""
    from kvevaluator.measure import run_measure as M
    root = str((tree or Tree()).place_dir("captures", run.dataset, run.scale))
    failed = M.failed_runs(run) if tree is None else None
    if failed:      # a query with no costed plan: refused by name, never priced as inf
        raise SystemExit(M.say_failed(run, failed, "REFUSED: a query with failed runs has no costed plan; fix it and "
                                                   "measure and compose again"))
    return root


def run_defaults(a, verb, model):
    """fill a workload's defaults: the benchmark, the scale, the captures read (a.captures: the captures place of the
    tree --from names, default the output dir), and for search-mix the sized Chunked options and its rows' place
    (results.home under summary/<dataset>/<scale>)"""
    run = runs.load(a)          # an id: its one file (kvevaluator.capture.workload_file.resolve), as load_workload reads it
    a.workload, a.sf = run.path, run.scale
    a.dataset = a.dataset or run.dataset
    a.adapter = W.of(a.dataset, run.families())
    runs.say_run(verb, run)
    src = Tree(a.from_dir) if a.from_dir else None          # the tree read; None: the output dir
    if verb == "cost-layouts":       # its place: kvcost.layout_cost.layout_costs.place_of_run
        a.captures = require_composed(run, src) if a.source == "measured" else None
        return run
    if getattr(a, "source", "measured") != "measured":   # predicted captures: the tree's predicted/ place
        a.rows_name = f"{run.name}-{a.source}"    # its rows beside the measured ones, named as a question's
        return run
    a.captures = require_composed(run, src)
    return run


def search_name(a, model):
    """the name of a search's output: its model, its byte limit when it has one (wops_bytes4k-0.5x) and its op weights
    when not the default (wops_bytes4k-w1_30_0.01), so searches under different limits or vectors keep separate files"""
    limit = getattr(a, "byte_limit", None)
    w = weights.parse(getattr(a, "weights", None))
    name = f"{model}-{limit.replace(' ', '')}" if limit else model
    return name if weights.is_default(w) else f"{name}-w{weights.text(w).replace(':', '_')}"


def shared(p, common, verb):
    """the words cost-layouts and search-mix share: the tree read, the workload, how queries combine, the pick rule's
    params, the op weights"""
    common(p)
    p.add_argument("--from", dest="from_dir", default=None, metavar="TREE",
                   help="the output tree read (default the output dir; e.g. paper-results): its captures/<dataset>/"
                        "<scale>/, each layout measured itself or composed, or its predicted/ set (--count-source). Every "
                        "output goes to the output dir (KVEVAL_OUTPUT_DIR)")
    p.add_argument("--workload", default=None, type=runs.workload_arg,
                   help=("(required but with --question) " if verb == "search-mix" else "(required) ")
                   + "a workload file (items: family, query, frequency, params; optional dataset and scale), or the id "
                     "or name of one under inputs/workloads/ (i01)")
    p.add_argument("--combine-queries", dest="weighting", default="sum", choices=("sum", "geomean", "additive"),
                   help="how the queries add up: sum (at their frequencies; default), geomean (equal contribution: "
                        "BI's and JOB's headline), additive (the mean of each query's cost over its cost on G, the "
                        "geomean-best uniform layout)" + ("; the fewest of a model only, without --byte-limit"
                                                          if verb == "search-mix" else "; what is printed (the files "
                                                          "hold each query's cost: a total is made when read)"))
    benchmarks.add_pick_on_arg(p)
    p.add_argument("--op-weights", dest="weights", default=None, metavar="G:S:N",
                   help="the op weights of a GET, a SEEK and a NEXT (default 1:1.1:0.01, python/kvevaluator/weights.py); "
                        "only the ratio matters. They set both the plan pick and the cost, so the best plan and the "
                        "best layout can change with them; another vector picks every plan afresh (no pick record)")
    if verb != "search-mix":
        return
    p.add_argument("--settings", default="Sp,Co,Ch64k", metavar="Sp,Co,Ch1k,...",
                   help="the settings each part may take, short names with their sizes (default Sp,Co,Ch64k); Sp, Co and Ch64k "
                        "always, and Chunked at any other size whose Chunked/Chunked the "
                        "tree holds (Sp,Co,Ch1k,Ch4k,Ch16k,Ch64k,Ch256k: the paper's any size). Names the rows' file: "
                        "mixes-of-<settings>[-by-direction].json")
    p.add_argument("--by-direction", action="store_true", help="each edge type's two directions are parts of their "
                                                               "own (the grid's three settings only)")
    p.add_argument("--search", default="proven", choices=R.SEARCHES,
                   help="proven (default: the two-step mix, then branch and bound proves it), two-step (the two-step "
                        "mix alone, not proven), or local (first-improvement moves from the best uniform layout, not "
                        "proven: for a model or a workload the exact search cannot take; written beside the rows as "
                        "<stem>-local-search.json)")
    p.add_argument("--max-nodes", type=int, default=None, help="each solve's node limit (default none: every solve "
                                                               "finishes; a row says whether it is proven)")
    x = p.add_argument_group("expert", "speed knobs: the answer is the same with any of them")
    x.add_argument("--cost-cache", action="store_true",
                   help="build the mix cost cache (each query's cost under every assignment of the parts it reads) and "
                        "solve on it; it is saved in <stats>/<dataset>/<scale>/mix-cost-cache and reused by any run at "
                        "the scale. Off by default: the solve on each query's bodies (python/kvcost/layout_search/"
                        "bodies.py) gives the same answer, faster for the fewest wops or wops_bytes4k with no byte "
                        "limit. A search that "
                        "needs it turns it on itself and says so: a bill model, --front's walk, a staircase under a "
                        "bill model, --byte-limit caps, --trace; so do the searches much faster on it: --cost-model "
                        "bytes, and --byte-limit (one limit, or a list). Never built where the largest "
                        "query's would pass 10^7 entries (JOB)")
    x.add_argument("--no-cost-cache", action="store_true",
                   help="never build the mix cost cache: a search much faster on it (--cost-model bytes, --byte-limit: "
                        "one limit, or a list, the staircase at wops against bytes) solves on each query's bodies "
                        "instead; a search that needs it is refused")


def settings_option_set(text, by_dir=False):
    """(option set, other chunk sizes ['4k', ...]) of --settings and --by-direction: Sp, Co and Ch64k always, and
    Chunked at other sizes; refused by name otherwise"""
    from kvcost.layout_cost import mixes
    ids = [mixes.setting_of(x) for x in text.split(",") if x.strip()]
    base = {"split", "condensed", "chunked"}
    if not base <= set(ids):
        raise SystemExit(f"--settings {text}: a part's settings here always include Sp, Co and Ch64k (the search's "
                         f"start is the best uniform layout over them); add {', '.join(mixes.setting_short(s) for s in sorted(base - set(ids)))}")
    sizes = sorted({i[len("chunked_c"):] for i in ids if i not in base}, key=lambda k: int(k[:-1]))
    if sizes and by_dir:
        raise SystemExit("--by-direction takes the grid's three settings only (Sp,Co,Ch64k): no search here gives a part "
                         "per direction Chunked at other sizes")
    return ("per-type-and-direction" if by_dir else "per-type-any-size" if sizes else "per-type"), sizes


def is_limit_list(text):
    """--byte-limit's list forms: a comma list, caps, named, bytes:<caps> or file:<path>"""
    t = text.strip()
    return "," in t or t in ("caps", "named") or t.startswith(("bytes:", "file:"))


def search_flags(a):
    """search-mix's flags as the search reads them: --settings into the option set and the sizes, --byte-limit into one
    limit or a staircase's list (without --front: the staircase alone; with it, a single limit too: the walk and the
    list's points on it),
    --search two-step into a one-node solve"""
    a.option_set, a.size = settings_option_set(a.settings, a.by_direction)
    a.limits, a.no_walk = None, False
    if a.byte_limit and (is_limit_list(a.byte_limit) or a.front):    # with --front, one limit is a list of one
        a.limits, a.byte_limit = a.byte_limit, None
        a.no_walk = not a.front
        a.front = True
    if a.walk_seconds is not None and a.no_walk:
        raise SystemExit("--max-seconds limits the walk (--front); a --byte-limit list without --front runs none")
    if a.search == "two-step":
        if a.max_nodes not in (None, 1):
            raise SystemExit("--search two-step stops after the two-step mix: no --max-nodes")
        a.max_nodes = 1


def cmd_local_search(a, argv, ad, plan_rule):
    """search-mix --search local: kvcost.layout_search.local_search on the workload, the tree's captures and the
    settings' sizes, beside the rows as <stem>-local-search.json"""
    from kvcost.layout_cost import cost_models as CM2
    from kvcost.layout_search import local_search as LS
    if a.front or a.byte_limit or a.limits:
        raise SystemExit("--search local answers the fewest of one model: no --front, no --byte-limit")
    if a.cost_cache:
        raise SystemExit("--search local builds no mix cost cache: no --cost-cache")
    if a.model not in ("wops_bytes4k", "bill_exact"):
        raise SystemExit("--search local takes --cost-model wops_bytes4k or bill_exact")
    path = result_path(a, R.dataset_scale_dir(ad.DATASET, a.sf), load_workload(ad.DATASET, a.sf, a.workload)["id"],
                       "-local-search")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    argv2 = ["--dataset", ad.DATASET, "--sf", str(a.sf), "--workload", a.workload, "--grid", a.captures,
             *[x for k in a.size for x in ("--size", k)], "--model", a.model,
             "--chunk-view", "measured" if CM2.needs_bill(a.model) else "half", "--out", path,
             *(["--fresh"] if a.fresh else [])]
    print(f"[kvcost search-mix --search local] {' '.join(argv2)}", flush=True)
    rc = LS.main(argv2)
    if rc == 0:
        runs.answer("search-mix", f"the local search's mix (not proven) in {runs.rel(path)}")
    return rc


def cmd_search_mix(a, argv, ad, plan_rule):
    """the fewest of one model (optionally within a byte limit), or with --front every mix no other beats in both
    wops and bytes; then the result file's rows as <option set>.csv beside it and the row's mix as a spec"""
    run = a.wrun
    a.answer = None                             # the solve's verdict, set by cmd_best_mix / cmd_front
    if a.search == "local":
        return cmd_local_search(a, argv, ad, plan_rule)
    if a.front:
        rc = cmd_front(a, argv, ad, plan_rule)
    else:
        rc = cmd_best_mix(a, argv, ad, plan_rule)
    path = result_path(a, R.dataset_scale_dir(ad.DATASET, a.sf), load_workload(ad.DATASET, a.sf, a.workload)["id"])
    rows = R.ResultFile(path).read()
    one = [r for r in rows if "assignment" in r]  # the rows that answer with one mix (a front's row holds many)
    if one and run is not None:                  # a workload's view
        cols, out = X.result_rows(one)
        X.write_csv(path[:-len(".json")] + ".csv", cols, out)
        print(f"[kvcost search-mix] rows: {len(out)} in {runs.rel(path[:-len('.json')] + '.csv')}")
    if rows and run is not None and not a.front:
        # the row this call solved: the newest run id (the file keeps its rows in key order, not in the order asked)
        spec, base = X.row_spec(max(rows, key=lambda r: r["run_id"]) if not a.row_run_id
                                else next(r for r in rows if r["run_id"] == a.row_run_id))
        mixes = os.path.join(os.path.dirname(path), "mixes")
        os.makedirs(mixes, exist_ok=True)
        named = os.path.join(mixes, f"{R.stem(a.option_set, a.size)}-{search_name(a, a.model)}.json")
        for spec_path in (os.path.join(mixes, "best.json"), named):
            with open(spec_path, "w") as f:
                json.dump(spec, f, indent=2, sort_keys=True)
                f.write("\n")
        best = os.path.join(mixes, "best.json") == runs.best_mix(run)     # mix:best reads this one
        print(f"[kvcost search-mix] the mix as a spec ({runs.layout_name(base)} with per-type settings): "
              f"{runs.rel(named)}, and as {runs.rel(os.path.join(mixes, 'best.json'))} (the last search's "
              f"mix): kvcost check-mix --workload {runs.shown(run.path)} "
              f"{'mix:best' if best else runs.shown(named)} builds and measures it")
        a.answer = (a.answer or "the mix") + f"; the mix in {runs.rel(named)}"
    from kvevaluator import stale
    if rows and stale.kinds():   # a moved input's notes, for this call's run id (a result file keeps every run's rows)
        stale.mark_dir(os.path.dirname(os.path.abspath(path)), f"run-{max(r['run_id'] for r in rows)}")
    if rc == 0 and getattr(a, "answer", None):
        runs.answer("search-mix", a.answer)
    return rc


TWO_STRINGS = ("--mix: an assignment string needs --mix-name, and one --mix-name names one string. To give several "
               "parts their settings, put them in ONE --mix string joined by ';': --mix \"knows lists=Ch4k; Person "
               "props=Ch64k\" --mix-base Co-Co --mix-name all-chunked. To price a mix's spec file, give it as a layout "
               "(named by its file's stem)")


def mix_assignment(mix, base, name, units, sizes=(), source="measured"):
    """{unit: setting} of one --mix assignment string as list-parts spells the parts, each setting in short (Sp, Co,
    Ch4k: 'knows lists=Ch4k; Person props=Co'), the units it leaves out set by --mix-base. Refused, by name: a unit the
    capture does not have, units left out with no --mix-base, a chunk size the tree (or, from a predicted set `source`, the
    set) holds no Chunked/Chunked of"""
    from kvcost.layout_cost import mixes
    assign = mixes.parse_assignment(mix)
    unknown = sorted(u for u in assign if u not in units)
    if unknown:
        raise SystemExit(f"--mix: REFUSED: units not in this capture: {', '.join(mixes.unit_name(u) for u in unknown)}. "
                         f"Its units: {'; '.join(mixes.unit_name(u) for u in units)} (kvcost list-parts lists them)")
    left = [u for u in units if u not in assign]
    if left and not base:
        raise SystemExit(f"--mix leaves {len(left)} units out (e.g. {', '.join(mixes.unit_name(u) for u in left[:4])}): "
                         f"name them, or give --mix-base <layout>")
    if left:
        bp, ba = L.cell_settings(L.cell_of(base))
        assign.update({u: (bp if u[0] == "props" else ba) for u in left})
    have = set(compose.LAYOUTS) | set(sizes)
    bad = sorted({v for v in assign.values() if v not in have})
    if bad:
        raise SystemExit(f"--mix {name}: {', '.join(mixes.setting_short(b) for b in bad)}: no Chunked/Chunked of that "
                         f"size in the {'tree' if source == 'measured' else 'predicted set'} "
                         f"({'kvcost measure' if source == 'measured' else PC.predict_call(source)} "
                         f"... Ch<K>k-Ch<K>k); it has {', '.join(mixes.setting_short(h) for h in sorted(have))}")
    return assign


def resolve_layouts(a, place, family):
    """[(kind, key, name, spec, assign)] of cost-layouts' layouts: each positional layout (a uniform name or cell id;
    a spec file: a uniform spec is that layout, any other a mix; mix:<question>/<model>[-<limit>] or mix:best, the mix a
    search row holds), then each --mix string (its assignment made once the units are known); default every uniform
    layout the tree holds, measured itself or composed"""
    out, seen = [], set()

    def add(kind, key, name, spec, assign=None):
        if key is None or key not in seen:
            out.append((kind, key, name, spec, assign))
            seen.add(key)
    for t in a.layouts:
        if t.startswith("mix:"):
            t = R.mix_spec(t, a)
        if t.endswith(".json"):
            with open(t) as f:
                spec = L.expand(json.load(f))
            cell = L.cell_of_spec(spec)
            if cell is not None and spec == L.spec_of(cell):
                add("uniform", cell, L.name_of(cell), spec)
            else:
                add("mix", L.mix_name(spec), os.path.basename(t)[:-len(".json")], spec)
            continue
        cell = L.cell_of(t)
        add("uniform", cell, L.name_of(cell), L.spec_of(cell))
    for m in a.mix:
        add("mix", None, a.mix_name, None,
            lambda units, sizes, m=m: mix_assignment(m, a.mix_base, a.mix_name, units, sizes, a.source))
    if not a.layouts and not a.mix:
        for cell in held_layouts(place, family, getattr(a, "param_set", None)):
            add("uniform", cell, L.name_of(cell), L.spec_of(cell))
    return out


def composed_place(captures):
    """the composed/<dataset>/<scale> dir beside a captures/<dataset>/<scale> dir (None when it is not a tree's)"""
    parts = os.path.normpath(captures).split(os.sep)
    if len(parts) < 3 or parts[-3] != "captures":
        return None
    return os.sep.join(parts[:-3] + ["composed"] + parts[-2:])


def held_layouts(place, family, param_set=None):
    """the uniform layouts a captures place holds for a read family, measured itself or composed beside it, in the
    diagonals' order (props outer)"""
    cells = set()
    for root in (place, composed_place(place)):
        if root is None:
            continue
        cap = capture_in(root, family, param_set)
        if os.path.isdir(cap.dir("counts")):
            cells |= {c for c in cap.stores() if L.is_cell(c) and os.path.exists(cap.record(c))}
    rank = lambda c: tuple(L.DIAGONALS.index(s) if s in L.DIAGONALS else len(L.DIAGONALS) for s in L.cell_settings(c))
    return sorted(cells, key=lambda c: (rank(c), c))


def require_on_disk(a, run, place, family, layouts):
    """refuse a uniform layout the tree holds neither measured nor composed, naming the compose (or the measure) to
    run; mark a composed layout stale when a source it was made from was measured again since"""
    from kvcost.layout_cost import compose_cells as CC
    pset = getattr(a, "param_set", None)
    missing = []
    for kind, key, name, _spec, _assign in layouts:
        if kind != "uniform":
            continue
        cap = layout_capture(place, family, key, pset)
        if not os.path.exists(cap.record(key)):
            missing.append(name)
        else:
            CC.note_if_stale(cap.record(key), name)
    if missing:
        raise SystemExit(f"cost-layouts: {len(missing)} layouts are neither measured nor composed under {runs.rel(place)}: "
                         f"{' '.join(missing)}. Compose them first: kvcost compose --dataset {run.dataset}={run.scale} "
                         f"{' '.join(missing)} (it names the measure to run when a side's diagonal is missing)")


def cmd_cost_layouts(a, argv, ad, plan_rule):
    """each layout's per-query plan and cost and its totals, in every model and both chunk views, upserted into the one
    place (kvcost.layout_cost.layout_costs); the totals under --cost-model printed"""
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.layout_cost import uniform_49 as U
    if a.mix and not a.mix_name:
        raise SystemExit(TWO_STRINGS)         # before any pricing
    if len(a.mix) > 1:
        raise SystemExit(TWO_STRINGS)
    t0 = time.time()
    run = a.wrun
    src = Tree(a.from_dir) if a.from_dir else Tree()
    w = load_workload(ad.DATASET, a.sf, a.workload)
    vec = weights.parse(a.weights)
    criterion, rule = compose.pick_rule_under(a.criterion, plan_rule, vec)
    out = LC.place_of_run(Tree(), run, a.source, pick_by=a.criterion,
                          pick_on=W.place_pick_on(run.dataset, run.families(), a.pick_on), op_weights=a.weights,
                          param_set=getattr(a, "param_set", None))
    if a.source != "measured":
        rows_by_view, times, lines, rc = cost_predicted(a, ad, w, vec, plan_rule)
    else:
        family = U.first_read(benchmarks.families_read(ad.DATASET, [w]))
        layouts = resolve_layouts(a, a.captures, family)
        if not layouts:
            raise SystemExit(f"cost-layouts: no uniform layout under {runs.rel(a.captures)} (kvcost measure, then kvcost "
                             f"compose)")
        require_on_disk(a, run, a.captures, family, layouts)
        print(f"[kvcost cost-layouts] {w['id']}: {len(layouts)} layouts from {runs.rel(a.captures)}, both chunk views",
              flush=True)
        by_dir = any("-lists=" in m for m in a.mix) or any(spec and spec.get("adjacency_by_etype_dir")
                                                          for _k, _key, _n, spec, _as in layouts)
        _, rows_by_view, times, lines, rc = U.cost_layouts(src, ad.DATASET, a.sf, a.workload, layouts, plan_rule=rule,
                                                    param_set=getattr(a, "param_set", None), criterion=criterion,
                                                    plant=a.plant_error, weights=tuple(vec[k] for k in "GSN"),
                                                    by_dir=by_dir)
    for line in lines:
        print(f"[kvcost cost-layouts] {line}")
    items = [(f, q) for f, q, *_ in w["items"]]
    lay = [r for v in rows_by_view.values() for r in v[0]]
    have = LC.upsert(out, items, lay, [r for v in rows_by_view.values() for r in v[1]],
                     [r for v in rows_by_view.values() for r in v[2]],
                     {"run_id": R.run_id(), "time": LC.now(), "command": "kvcost " + " ".join(runs.recorded_argv(argv)),
                      "argv": runs.recorded_argv(argv), "layouts": sorted({r["layout"] for r in lay}), "source": a.source,
                      "tree": runs.recorded_argv([str(src.root)])[0],
                      "binary": None, "check": "PASS" if rc == 0 else "FAIL",
                      "wall_s": {**times, "all": round(time.time() - t0, 3)}})        # upsert adds the write
    model = a.model or "wops_bytes4k"
    tot = LC.combined(out, w, model, a.weighting)
    if tot:
        names = {r["layout"]: r["name"] or runs.layout_name(r["layout"]) for r in LC.read_layouts(out)}
        best = min(tot, key=tot.get)
        ref = "props_split_adj_condensed" if "props_split_adj_condensed" in tot else None
        print(f"[kvcost cost-layouts] {model} per layout ({a.weighting} over the queries), {len(tot)} layouts in the "
              f"place" + (", and over Sp-Co (Dgraph's layout):" if ref else ":"))
        width = max(len(names.get(x, x)) for x in tot)
        for x in sorted(tot, key=lambda x: (tot[x], x)):
            print(f"  {names.get(x, x):<{width}}  {tot[x]:>12.6g}"
                  + (f"  {tot[x] / tot[ref]:6.3f}x" if ref and tot[ref] else "") + ("  fewest" if x == best else ""))
        runs.answer("cost-layouts", ("" if rc == 0 else "CHECK FAILED; ") + f"the fewest {model}: "
                    f"{names.get(best, best)}, {tot[best]:.6g}"
                    + (f" ({tot[best] / tot[ref]:.3f}x Sp-Co's {tot[ref]:.6g})" if ref and tot[ref] else "")
                    + f"; rows in {runs.rel(out)} ({len(have)} layouts)")
    else:
        runs.answer("cost-layouts", f"rows in {runs.rel(out)}; no layout has a {model} cost on every query")
    from kvevaluator import stale
    stale.mark_dir(str(out), "cost-layouts")   # the moved inputs these rows were made from
    return rc


def whole_predicted(a, w, vec, view):
    """(layout rows, query rows) of every layout of the predicted set, each costed whole in one chunk view
    (kvcost.predict_accuracy.uniform: the plan rule's body on its rows; the bill's bounds on the measured view)"""
    from kvcost.layout_cost import cost_table as CT
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.predict_accuracy import uniform as PU
    model = "bill_upper" if view == LC.BILL_VIEW else "wops"       # bill_upper: the bounds' lanes carried
    rows, _tot, missing = PU.uniform_cost(a.sf, w, a.source, a.from_dir, model, a.param_set, w=vec, view=view)
    lay_rows, q_rows = {}, []
    for r in rows:
        cell = r["layout"]
        kind = "uniform" if L.is_cell(cell) else "mix"
        p, ad_ = L.cell_settings(cell) if kind == "uniform" else ("", "")
        q_rows.append({**r, "body": r["plan"], "chunk_view": view, "kind": kind, "props": p, "adjacency": ad_})
        t = lay_rows.setdefault(cell, {"workload": w["id"], "layout": cell, "chunk_view": view, "kind": kind,
                                       "name": r["name"], "props": p, "adjacency": ad_, "spec_sha": "",
                                       "measured_as": "predicted", "sums": {}})
        for k in CT.VECTOR + compose.CHANNELS + compose.BILL:
            if r.get(k) not in ("", None):
                t["sums"][k] = t["sums"].get(k, 0.0) + float(r["frequency"]) * float(r[k])
    lrows = []
    for cell, t in sorted(lay_rows.items()):
        if cell in missing:
            continue
        sums = t.pop("sums")
        cur = CM.currencies({k: sums.get(k, 0.0) for k in compose.MEASURES})
        lrows.append({**t, **{k: round(v, 3) for k, v in sums.items()},
                      "total_ops": round(cur["ops"], 3), "total_bytes": round(cur["bytes"], 1)})
    return lrows, q_rows


def cost_predicted(a, ad, w, vec, plan_rule):
    """cost-layouts --count-source <set>: every layout of a predicted capture, each costed whole (whole_predicted), in
    both chunk views (the bill's bounds on the measured view), and each mix asked for (--mix, a mix's spec file)
    composed from the set's part rows (compose_predicted; it replaces the same mix costed whole): ({view: (layout
    rows, query rows, part rows)}, times, lines, 0). A whole predicted row has no parts: query_parts.csv gets none for it"""
    from kvcost.layout_cost import layout_costs as LC
    out, t0 = {}, time.time()
    composed = compose_predicted(a, ad, w, plan_rule)
    for view in LC.VIEWS:
        lrows, q_rows = whole_predicted(a, w, vec, view)
        lay, qs, ps = composed.get(view, ([], [], []))
        mine = {r["layout"] for r in lay}
        out[view] = ([r for r in lrows if r["layout"] not in mine] + lay, [r for r in q_rows if r["layout"] not in mine] + qs, ps)
    lines = [f"{a.source}: {len(out[LC.VIEWS[0]][0])} predicted layouts priced"
             + (f", {len(composed[LC.VIEWS[0]][0])} of them mixes composed from the part rows" if composed else " whole (no part rows)")]
    return out, {"pick_and_price": round(time.time() - t0, 3)}, lines, 0


def compose_predicted(a, ad, w, plan_rule):
    """{view: (layout rows, query rows, part rows)} of the mixes cost-layouts is asked for (--mix strings, a mix's spec
    file as a layout) under --count-source <set>, composed from the set's part rows by the one composer a predicted
    search-mix takes (_predicted_setup's sets and plan rule, load_composed, uniform_49.cost_rows: compose.query_pick
    per query); {} when none is asked. A mix with a part the set does not hold is refused, naming the kvcost predict
    call that makes it"""
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.layout_cost import uniform_49 as U
    asked = [x for x in resolve_layouts(a, None, None) if x[0] == "mix"] if a.layouts or a.mix else []
    if not asked:
        return {}
    a.size = predicted_sizes(a.from_dir, ad.DATASET, a.sf, a.source)
    set_dir, fams, rule, sizes = _predicted_setup(a, ad, plan_rule)
    out = {}
    for view in LC.VIEWS:
        bill = "bounds" if view == LC.BILL_VIEW else ""      # bill_lower and bill_upper: the lanes a predicted row has
        data = load_composed(a, ad, set_dir, fams, rule, sizes, False, bill, view)
        units = compose.workload_units(w, data)
        try:
            entries = U.entries_of(asked, units, data.sized)
        except SystemExit as e:
            raise SystemExit(f"{e}\n(a mix of a predicted set takes each part from one of its Chunked/Chunked cells: "
                             f"the set holds Sp, Co, Ch64k and Chunked at "
                             f"{', '.join(data.sized) or 'no other size'}; {PC.predict_call(a.source)} "
                             f"Ch<K>k-Ch<K>k beside the nine grid layouts makes another)") from None
        _tots, lay, qs, ps = U.cost_rows(w, data, entries, view, lambda layout: "composed")
        out[view] = (lay, qs, ps)
    print(f"[kvcost cost-layouts] {len(asked)} mixes composed from the part rows of {runs.rel(set_dir)} "
          f"(sizes held: {', '.join(a.size) or 'none beyond the grid'})", flush=True)
    return out


def refuse_run_mode(a, verb):
    """a workload's command needs --workload and one --dataset pair"""
    runs.one_pair(a, f"kvcost {verb}")
    if not a.workload:
        raise SystemExit(f"kvcost {verb}: --workload is required (a workload file, or an id: i01)")


def over_tree(a, argv, verb, parser, run):
    """search-mix --question: its calls, read from the tree --from names, written to the output dir"""
    if a.workload:
        raise SystemExit("--question names its workloads and flags: give --dataset and --from only")
    return run_question(a, argv, parser)


def run_question(a, argv, parser):
    """--question: the question's search-mix calls (each parsed by `parser` as its own command line), in the table's
    order"""
    calls = question_calls(a)
    t0 = time.time()
    for cmd, rows in calls:
        print(f"[kvcost search-mix --question {a.question}] search-mix {' '.join(cmd)}", flush=True)
        c = parser.parse_args(cmd)
        if a.dry_run:
            continue
        c.rows_name, c.question = rows, None
        rc = c.fn(c, ["search-mix", *cmd])
        if rc:
            return rc
    print(f"[kvcost search-mix --question {a.question}] {len(calls)} calls "
          + ("printed, none run (--dry-run)" if a.dry_run else f"in {time.time() - t0:.0f} s"))
    return 0


def question_calls(a):
    """[(argv for search-mix, rows dir name)] of --question: each call of the question (layout_search.mix_questions) on
    the tree --from names, its rows, their CSV and their mixes into the output dir at the question's own dataset and
    scale: summary/<dataset>/<scale>/mix-search/<rows>/ (a larger question: the run's second scale)"""
    from kvcost.layout_search import mix_questions as MQ
    q = MQ.get(a.question)
    run = a.run or {}                      # the --dataset flags: {dataset: [scales]} (cli.DatasetScale)
    ds = q["dataset"]
    want = 2 if q.get("larger") else 1
    if len(run.get(ds, [])) < want:
        raise SystemExit(f"--question {a.question}: give --dataset {ds}=<scale>"
                         + (f" and --dataset {ds}=<the larger scale>" if want == 2 else ""))
    sc = run[ds][want - 1]
    src = Tree(a.from_dir) if a.from_dir else Tree()
    calls = []
    for call in q["calls"]:
        flags = call["flags"]
        if MQ.count_source(q):       # a predicted set: the tree's predicted/<ds>/<sc>/<set>/
            flags = ["--count-source", MQ.count_source(q), *flags]
        cmd = ["--dataset", f"{ds}={sc}", "--workload", call["workload"], "--from", str(src.root),
               *MQ.flags_at(flags, sc), *(["--fresh"] if a.fresh else []),
               # after the call's own flags: the last --jobs counts
               *(["--jobs", str(a.jobs)] if getattr(a, "jobs", None) else [])]
        calls.append((cmd, q["rows"]))
    return calls


def _capture_of(a, verb):
    """the capture root a list-parts call reads (None for plot-costs, which reads cost-layouts' rows), and the
    workload: the captures place of the tree --from names (default the output dir)"""
    run = runs.load(a)
    a.workload, a.sf = run.path, run.scale
    a.dataset = a.dataset or run.dataset
    a.adapter = W.of(a.dataset, run.families())
    runs.say_run(verb, run)
    src = Tree(a.from_dir) if getattr(a, "from_dir", None) else None
    return (None if verb == "plot-costs" else require_composed(run, src)), run   # plot-costs reads rows, no capture


def cmd_list_parts(a, argv, ad, plan_rule):
    """the layout units a mix can assign (as --mix spells them), read from the capture's part files: seconds, no store"""
    from kvcost.layout_cost import mixes
    root = a.root
    units = set()
    for fam, kind in sorted(dict(ad.FAMILIES).items()):
        for cell in L.CELLS:
            pset = getattr(a, "param_set", None)       # each layout's capture: measured itself, else composed
            path = (layout_capture(root, fam, cell, pset).path_of("parts", cell) if kind == "read"
                    else writes_layout_capture(root, ad.DATASET, cell, pset).path_of("parts", cell))
            if os.path.exists(path):
                rows = capture_layout.read_rows(path) if kind == "read" else capture_layout.write_rows(path, cell=cell)[2]
                units |= {compose.unit_of(r["part"], a.by_direction) for r in rows} - {None}
    if not units:
        raise SystemExit(f"no part files of a uniform layout under {root}: measure and compose the run first "
                         f"(kvcost measure, kvcost compose)")
    units = sorted(units)
    print(f"[kvcost list-parts] {a.dataset} {a.sf}: read from {runs.rel(root)}")
    for kind, title in (("props", "props parts (a vertex group's properties)"), ("adj", "lists parts (an edge type's adjacency)")):
        print(f"  {title}:")
        print("    " + "; ".join(mixes.unit_name(u) for u in units if u[0] == kind))
    one = [mixes.unit_name(u) for u in units if u[0] == "adj"][:1] + [mixes.unit_name(u) for u in units if u[0] == "props"][:1]
    print(f"  e.g. kvcost cost-layouts --workload {runs.shown(a.workload) if os.path.isfile(a.workload) else a.workload} "
          f"--mix \"{'; '.join(p + '=Ch64k' for p in one)}\" --mix-base Sp-Co --mix-name <name>")
    print("       (several parts go in one --mix string, joined by ';', each setting in short: Sp, Co, Ch64k, Ch4k; the "
          "parts it leaves out take --mix-base"
          + ("" if a.by_direction else "; --by-direction lists the per-direction parts, '<etype> out-lists'") + ")")
    runs.answer("list-parts", f"{len(units)} parts: {sum(1 for u in units if u[0] == 'props')} props parts, "
                              f"{sum(1 for u in units if u[0] == 'adj')} lists parts")
    return 0


def cmd_plot_costs(a, argv, ad, plan_rule):
    """per query, each layout's cost over a baseline layout, and the totals: from cost-layouts' rows (its place in the
    tree --from names), the figures into the output dir's place"""
    from kvcost.cli import plot as P
    from kvcost.layout_cost import layout_costs as LC
    where = dict(pick_by=a.criterion, pick_on=W.place_pick_on(a.wrun.dataset, a.wrun.families(), a.pick_on),
                 op_weights=a.weights,
                 param_set=a.param_set)
    rows_dir = LC.place_of_run(Tree(a.from_dir) if a.from_dir else Tree(), a.wrun, a.source, **where)
    if not (rows_dir / "query_costs.csv").exists():
        raise SystemExit(f"plot-costs: no {rows_dir}/query_costs.csv: run kvcost cost-layouts --workload "
                         f"{a.wrun.name} first (with the same --count-source, --pick-by, --op-weights, --params)")
    views = P.read_views(rows_dir)
    key = lambda x: L.cell_of(x) if L.NAME.fullmatch(x) else next((k for k, v in views.items() if v.label == x), x)
    ref_key = key(a.baseline)
    if ref_key not in views:
        raise SystemExit(f"plot-costs: no rows of the baseline {a.baseline} in {rows_dir} (have: "
                         f"{', '.join(sorted(v.label for v in views.values()))})")
    if a.layouts:
        keys = [key(x) for x in a.layouts]
        lack = [x for x, k in zip(a.layouts, keys) if k not in views]
        if lack:
            raise SystemExit(f"plot-costs: no rows of {', '.join(lack)} in {rows_dir} (have: "
                             f"{', '.join(sorted(v.label for v in views.values()))})")
        chosen = [views[k] for k in keys]
    else:
        chosen = list(views.values())
    out = str(LC.place_of_run(Tree(), a.wrun, a.source, **where) / "plots")
    paths = P.draw(chosen, views[ref_key], out, f"Data: kvcost cost-layouts rows in {rows_dir}.")
    for p in paths:
        print(f"    {runs.rel(p)}")
    runs.answer("plot-costs", f"{len(paths)} figures in {runs.rel(out)}: {len(chosen)} layouts over {a.baseline} (the "
                              f"1x line)")
    return 0


def add_parsers(sub, common, run):
    """search-mix and cost-layouts; run(fn) wraps a command with the adapter and the plan rule (cli.py's), after the
    run's defaults"""
    def with_run(fn, verb):
        def go(a, argv):
            if verb == "search-mix" and a.question:
                return over_tree(a, argv, verb, b, run)      # a question: no workload file
            if getattr(a, "dry_run", False) and verb == "search-mix":
                raise SystemExit("--dry-run: with --question (its calls printed); one search-mix call runs in this process")
            refuse_run_mode(a, verb)
            if verb == "search-mix":
                search_flags(a)
            if a.byte_limit if verb == "search-mix" else False:
                a.within, a.byte_limit_bytes = Q.parse_byte_limit(a.byte_limit)
            else:
                a.within, a.byte_limit_bytes = None, None
            if verb == "search-mix" and getattr(a, "front", False):
                Q.axes_of(a.model)                  # no --cost-model: wops against bytes; a bill form: read against write units
            elif not a.model and verb == "search-mix":
                raise SystemExit(f"kvcost {verb}: --cost-model is required (wops, bytes, wops_bytes4k, bill_exact, ...)")
            a.wrun = run_defaults(a, verb, (f"front-{a.model}" if a.model else "front") if getattr(a, "front", False)
                                 else a.model)
            a.sub = verb
            return run(fn)(a, argv)
        return go
    b = sub.add_parser("search-mix", help="the best mix: the fewest of one model, within a byte limit or not; or the front",
                       description="The fewest of one model (wops, bytes, wops_bytes4k, the bill's three forms) for a "
                                   "workload, each part taking one of --settings, or the fewest within a byte limit: the "
                                   "two-step mix first, then branch and bound proves it (--search). With --front: every "
                                   "mix no other beats in both wops and bytes (with a bill model: in both its read and "
                                   "its write units). One row in the output dir's summary/<dataset>/<scale>/mix-search/"
                                   "<workload id>/mixes-of-<settings>[-by-direction].json (its times: the two-step's two "
                                   "stages, the cost cache, the solve, the cost); beside it the rows that answer with one mix "
                                   "as <stem>.csv and the row's mix as mixes/<stem>-<model>[-<limit>].json and "
                                   "mixes/best.json (the last search's: check-mix mix:best builds it). A layout argument "
                                   "elsewhere names a question's mix as mix:<question>/<model>[-<limit>].")
    shared(b, common, "search-mix")
    b.add_argument("--cost-model", dest="model", default=None, choices=sorted(Q.MODELS), help="wops, bytes, wops_bytes4k, or the bill's "
                                                                          "three forms (bill_lower, bill_upper, bill_exact); "
                                                                          "required but with --front (none: wops against bytes; "
                                                                          "a bill form: its read against its write units)")
    b.add_argument("--byte-limit", default=None,
                   help="total bytes at most this: a multiple of the reference Sp-Co's bytes (2x) or a size (68MB, "
                        "1.5GiB): the fewest within it. Or a list: 1x,2x,3x, caps (the sweep's 32 limits, the singles, "
                        "the midpoint, the named B), named (B = 1, 2, 3, 5), bytes:<cap>,<cap>, or file:<path> (a JSON "
                        "list of {cap_bytes, cap_kind} or {B, cap_kind}): the fewest at each (the staircase); with "
                        "--front, the walk and those points named on it")
    b.add_argument("--front", action="store_true", help="every mix no other beats in both wops and bytes (the walk); "
                                                        "with a bill --cost-model, in both its read units and its write "
                                                        "units; --byte-limit's list names points on it")
    b.add_argument("--max-seconds", dest="walk_seconds", type=float, default=None,
                   help="--front: stop the walk after this wall time: the row keeps the exact points found and says the "
                        "front is incomplete")
    b.add_argument("--trace", nargs="?", const="", default=None, metavar="CSV",
                   help="write the search's decisions, one row per decision (default <out>/trace.csv; the answer is "
                        "the same with it). Columns: step, verdict, node (the search node deciding), child (the node an "
                        "explored row enters), part and option (the setting tried), mix_so_far (the parts set on the "
                        "way there), floor_<model> / floor_<other> (the least any mix on that branch can cost) and "
                        "best_<model> / best_<other> (the best full mix found so far). Verdicts: start (the first mix "
                        "to beat: the two-step mix, each part alone at its cheapest from the best uniform layout, then "
                        "one-part repairs; a greedy mix, not proven), dominated (a setting another setting of the same "
                        "part beats or ties in every query in both wops and bytes: left out), explored, cut: floor not "
                        "below the best, cut: over the byte limit, cut: lagrange bound (a second lower bound under a "
                        "byte limit), leaf: new best / not better (a full mix). The search is PROVEN when it ends "
                        "with no branch left whose floor is below the best")
    b.add_argument("--jobs", type=int, default=None, help=JOBS_HELP)
    b.add_argument("--count-source", dest="source", default="measured",
                   help="measured (default: the composed capture), or a predicted capture set (kvcost predict --save-as, e.g. "
                        f"{PC.default_set()}, the default tier's): the mix composed from its part rows under the plan rule (in-sample, no "
                        "pick record); reads-only workloads, the grid's three settings. Its row goes to "
                        "mix-search/<workload id>-<set>/")
    b.add_argument("--question", default=None, metavar="NAME",
                   help="one mix question the paper reads, its calls written once (python/kvcost/layout_search/"
                        "mix_questions.py: i01-front, i01-any-size-best, b01-spco-share, j01-spco-share, ...): each call "
                        "on the tree --from names (default the output dir), its rows into the output dir's summary/"
                        "<its dataset>/<its scale>/mix-search/<rows>/. Takes --dataset and --from only: --dataset "
                        "<its dataset>=<scale> (a run of several pairs: ldbc=3,imdb=full), and for a -larger question "
                        "the dataset's larger scale second (--dataset ldbc=3,ldbc=30)")
    b.add_argument("--fresh", action="store_true", help="search again: reuse nothing an earlier call saved, neither a "
                   "row searched on the same inputs (the captures read, the composer's settings, the question: such a "
                   "row is kept and nothing searched) nor the mix cost cache")
    b.add_argument("--dry-run", action="store_true", help="--question: print each call, run none")
    b.set_defaults(fn=with_run(cmd_search_mix, "search-mix"), row_run_id=None)

    c = sub.add_parser("cost-layouts", help="each query's plan and cost on each layout and mix, every model",
                       description="Picks, then costs: for each layout (and mix) each query's plan is the one the plan "
                                   "rule picks there, its cost one run of the query, in every cost model and both chunk "
                                   "views, upserted by layout into summary/<dataset>/<scale>/layout-costs/<workload>/"
                                   "<count source>/ (query_costs.csv, query_parts.csv, layouts.csv, README.md, "
                                   "runs.jsonl: python/kvcost/layout_cost/layout_costs.py). A uniform layout is read "
                                   "measured itself, else composed (kvcost compose); one neither is refused, with the "
                                   "compose to run. Each measured layout is checked against its composition. Prints the "
                                   "workload's total under --cost-model per layout in the place.")
    shared(c, common, "cost-layouts")
    c.add_argument("layouts", nargs="*", metavar="LAYOUT", type=runs.layout_arg(),
                   help="layouts: Sp-Co, Co-Ch4k, Ch1k-Ch16k (each Chunked side with its size; the grid's 64 KB is Ch64k), a "
                        "cell id, a mix's spec file, or mix:<question>/<model>[-<limit>] (the mix a search row holds; "
                        "mix:best, the last search's); default every uniform layout the tree holds, measured or "
                        "composed. The paper's 49: Sp-Sp Sp-Co ... Ch256k-Ch256k")
    c.add_argument("--cost-model", dest="model", default=None, choices=sorted(Q.MODELS),
                   help="the model the totals are printed in (default wops_bytes4k); the files hold every model")
    c.add_argument("--mix", action="append", default=[], metavar="STRING",
                   help="also price this mix from the parts, no store: an assignment string, every part it sets in ONE "
                        "string joined by ';', each setting a short name with its size ('knows lists=Ch4k; Person "
                        "props=Co'; kvcost list-parts lists the parts), named by --mix-name. A mix's spec file is a "
                        "layout argument (with --count-source <set>, composed from its part rows)")
    c.add_argument("--mix-base", default=None, help="--mix: the layout of every part the string leaves out (e.g. Co-Sp)")
    c.add_argument("--mix-name", default=None, help="--mix: the name of the string's rows")
    c.add_argument("--count-source", dest="source", default="measured",
                   help="measured (default: each layout measured itself, else composed), or a predicted capture set "
                        f"(kvcost predict --save-as, e.g. {PC.default_set()}, the default tier's): each of its layouts priced whole; a --mix or a mix's spec file composed from its part rows")
    c.add_argument("--plant-error", action="store_true", help=argparse.SUPPRESS)   # the check's control: must exit 1
    c.set_defaults(fn=with_run(cmd_cost_layouts, "cost-layouts"))

    def with_capture(fn, verb):
        def go(a, argv):
            a.root, a.wrun = _capture_of(a, verb)
            return run(fn)(a, argv)
        return go
    u = sub.add_parser("list-parts", help="the parts a mix can assign, as --mix spells them (no store)",
                       description="List the layout parts of the run's capture as a mix string spells them ('Person "
                                   "props', 'knows lists'): read from the part files, in seconds. cost-layouts --mix "
                                   "refuses a string with any other part.")
    common(u, picks=False)
    u.add_argument("--workload", required=True, type=runs.workload_arg, help="a workload file, or the id or name of one (i01)")
    u.add_argument("--from", dest="from_dir", default=None, metavar="TREE", help="the output tree read (default the "
                                                                              "output dir): its captures/<dataset>/<scale>/")
    u.add_argument("--by-direction", action="store_true", help="the per-direction parts ('knows out-lists')")
    u.set_defaults(fn=with_capture(cmd_list_parts, "list-parts"))
    p = sub.add_parser("plot-costs", help="draw cost-layouts' rows: per query over a baseline layout, and the totals",
                       description="Per query, each layout's cost (the layouts and mixes cost-layouts priced) over the "
                                   "baseline layout, one row per model (wops, bytes, wops_bytes4k), log scale; and the "
                                   "totals, every query once. Reads only cost-layouts' rows (query_costs.csv, its half "
                                   "view) in the tree --from names; the figures go to the output dir's place, plots/.")
    common(p)
    p.add_argument("--workload", required=True, type=runs.workload_arg, help="a workload file, or the id or name of one (i01)")
    p.add_argument("--from", dest="from_dir", default=None, metavar="TREE", help="the output tree read (default the "
                                                                              "output dir; e.g. paper-results)")
    p.add_argument("--count-source", dest="source", default="measured",
                   help="whose rows: measured (default) or a predicted set cost-layouts priced")
    benchmarks.add_pick_on_arg(p)
    p.add_argument("--op-weights", dest="weights", default=None, metavar="G:S:N",
                   help="the op weights cost-layouts ran under (default 1:1.1:0.01): which of its places to draw")
    p.add_argument("layouts", nargs="*", metavar="LAYOUT", help="the layouts drawn: Sp-Co, Co-Ch4k, a mix's name (its "
                                                                 "rows' name); default every layout")
    p.add_argument("--baseline", default="Sp-Co", help="the layout the others are shown against (default Sp-Co, "
                                                       "Dgraph's layout)")
    p.set_defaults(fn=with_capture(cmd_plot_costs, "plot-costs"))
