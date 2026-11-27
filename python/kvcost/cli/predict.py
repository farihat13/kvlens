"""kvcost predict: predict mode's counts for a set of layouts, as captures. kvevaluator predict --plans prices every body
from the graph statistics and each layout's tally; no store is opened.

Terms:
- statistics: the graph's counts and distributions, built once per dataset in one pass over its records.
- anchor statistics: one row per start vertex (its degree per edge type and direction); used when a query's params
  are known.
- tally: one layout's key and byte counts per store part, counted by the ingest's encoder with no store
  (`kvevaluator tally`).
- tally library: the tallies kept per layout setting, so any meta spec's tally is assembled part by part.

    kvcost predict --dataset ldbc=3 --family interactive --queries ic1,ic2 --tally-dir D Sp-Sp Co-Co ...
        [--stats-file F] [--anchor | --anchor-stats-file F] [--nparam 5] [--plans all|picked] [--save-as SET]
        [--work-dir W] [--jobs N] [--no-rules a,b] [--fresh] [--params uniform]
    kvcost predict --workload <workload file> --dataset ldbc=0.1 [<layouts>] [--fresh]
        (a workload file: its queries on the layouts named, default the nine grid layouts, the statistics built when
        missing, each layout's tally assembled from the tally library; the predicted captures in the tree's
        predicted/<dataset>/<scale>/<set>/ (the output dir's: KVEVAL_OUTPUT_DIR), the requests, the tallies and the
        logs in its work/;
        then kvcost cost-layouts --workload <file> --count-source predicted-tier1 costs them: the default tier's set,
        default_set())
    kvcost predict --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 1|2
        (the 49 uniform layouts named: their tallies made once per scale, stats/<dataset>/<scale>/uniform-49/, as
        kvcost build-statistics makes them; --tally-dir D --save-as <another set>: the 49 predicted from the tallies
        in D instead, into that set)

A tier is the statistics a prediction reads: 1 (the default, kvevaluator.predict.tier1.DEFAULT_TIER) a coarser summary of
the dataset's full statistics (tier 1 and the flat rows, derived from the full file), 2 the full statistics. Each tier
writes its own set, predicted-tier<T> (TIER_SETS), so no set is named bare `predicted` and a reader names the tier it
reads; tier 2 is read only when asked (--tier 2, --count-source predicted-tier2). The anchor level (--anchor) costs each body a second time from
the anchor statistics, one row per start vertex (its degree per edge type and direction), into the set's -anchor twin.

A layout is a name at any chunk size (Sp-Co, Ch4k-Ch16k), a cell id, a mix's spec file or a search row's mix
(mix:<question>/<model>[-<limit>], mix:best): its spec is made from its name (kvevaluator.layouts), and a mix's capture
is named by its content, mix_<sha> (as check-mix names it).

One binary call per layout (--jobs at once) writes the capture set --save-as (the population statistics) and, with
--anchor, its -anchor twin (the anchor statistics; off by default). Each set is its own tree <root>/<set>/. The
binary refuses a capture made from other inputs (statistics, rules, tally, spec or request set) unless --fresh; a run
(--workload <file>) replaces its own captures.

A layout already predicted on the same inputs is kept: its record's inputs_key (inputs_keys: the statistics, the
tally, the spec, the schema, each query's plan file and params, the body set, the rules turned off, the binary's
counting versions; no commit, no file time) equals this call's, so no request is written and the binary is not
called for it ("already predicted"). --fresh predicts every layout again. The prediction code itself is not in the
key: a kept capture predicted by another binary commit is named, not predicted again.

kvcost predict-accuracy: one count source against another, on the same layouts and workload: does the first name the
second's cheapest layout, and how far off is it.

    kvcost predict-accuracy --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k
        [--count-source predicted-tier1] [--against measured] [--cost-model wops_bytes4k] [--queries a,b] [--from TREE]

Each side is the place kvcost cost-layouts wrote for it (summary/<dataset>/<scale>/layout-costs/<workload>/<count
source>/: run kvcost cost-layouts --count-source <source> for each side first); any two sources compare (a predicted
set against measured, tier 1 against tier 2). Per model and per query and for the workload: the first side's pick, its
cost on the second side over the second's best, its rank there, the spread (Sp-Co, Co-Co, the median, the worst) and
Spearman, into summary/<dataset>/<scale>/predict-accuracy/<workload>/<count source>-vs-<against>/ (picks.csv,
layouts.csv). The first side's costs are compared at 12 significant digits, the second's exact.

The 49 uniform layouts named with a predicted set against measured also write the paper's report into
summary/<dataset>/<scale>/predict-statistics/<workload>/<set>/: a workload with a write family by its totals
(pick.csv, layouts.csv, against the measured 49's layout-costs place), a read-only one per query and param against its
grid composed in memory (layout_choice/).

--per-step: per KV step, the predicted capture of each read family joined to the measured capture (the tree's), into
<the place above>/per-step/ (steps.csv, ratios.csv, summary.md); --per-step DIR reports a DIR whose steps.csv
kvevaluator predict --accuracy wrote. The join of captures named by path is python -m kvcost.predict_accuracy.join; the
layout report on a grid or an earlier table, with a check workload, is python -m kvcost.predict_accuracy.layouts.
"""
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from kvcost.predict_accuracy import requests as RQ
from kvcost.predict_accuracy import uniform_49_tallies as U49
from kvevaluator import datasets, workloads
from kvevaluator import layouts as L
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure import run_measure as M
from kvevaluator.measure.records import records_file
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT

PREDICT_DOC, _, ACCURACY_DOC = __doc__.partition("\nkvcost predict-accuracy:")   # each command's own --help text
ACCURACY_DOC = "kvcost predict-accuracy:" + ACCURACY_DOC


def predicted_place(root, dataset, sf, name):
    """a predicted set's place in the tree `root` (None: the output dir): predicted/<dataset>/<scale>/<set>/"""
    return Tree(root).predicted_dir(dataset, sf, name)


def capture_dirs(sf, workload, sets, root, param_set=None):
    """[the capture dir of each predicted set]: the workload's capture in the tree's predicted/<dataset>/<scale>/<set>/"""
    return [Tree(root).predicted(workload, sf, s, param_set).path for s in sets]


@functools.lru_cache(maxsize=None)
def binary_version(binary):
    """the binary's --version fields"""
    return json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout)


def binary_line(binary):
    v = binary_version(binary)
    from kvevaluator.capture.binary import binary_state
    state = binary_state(v)
    return v["binary_git_sha"][:10] + ("" if state == "clean" else f" ({state})")


from kvcost.cli.workloads import NPARAM   # noqa: E402  (the params per query when neither --nparam nor the adapter names a count)
TIER_SETS = {t: (U49.tier_set(t), f"{U49.tier_set(t)}-anchor") for t in (1, 2)}   # --tier's sets, each named by its tier


def default_set():
    """the predicted set `kvcost predict` writes with no --tier or --save-as (the default tier's: tier1.DEFAULT_TIER)"""
    from kvevaluator.predict.tier1 import DEFAULT_TIER
    return TIER_SETS[DEFAULT_TIER][0]


def predict_call(set_name):
    """the `kvcost predict --workload <file>` call that writes the predicted set `set_name` (a hint's text)"""
    tier = next((t for t, (s, _) in TIER_SETS.items() if s == set_name), None)
    flags = "" if set_name == default_set() else f" --tier {tier}" if tier else f" --save-as {set_name}"
    return f"kvcost predict --workload <file>{flags}"


def at_once(a, verb, what, records=None):
    """how many of `what` a call runs at once, said once (run_measure.at_once, the one rule: --jobs, else the cores);
    `records`: the units are the tally library's settings, one pass over that record stream each, so no more than
    the available memory over one pass's peak (tally_library.pass_peak_gb)"""
    from kvevaluator.predict import tally_library as TL
    n, why = (M.at_once(a.jobs, 1, TL.pass_peak_gb(records), "a pass over the record stream") if records else
              M.at_once(a.jobs, 1, None, no_peak="a call's size is not recorded"))
    print(f"[kvcost {verb}] {what}: {why}", flush=True)
    return n


def run_of(a):
    """the workload file --workload names: its queries; the work dir and the tally dir in the
    predicted set's place, predicted/<dataset>/<scale>/<set>/work/ (beside its captures); the nine grid layouts unless
    named; the statistics built when missing; each layout's tally assembled from the library. None for --family"""
    from kvcost.cli import runs
    if a.family:
        if not (a.queries and a.layouts and a.tally_dir):
            raise SystemExit(f"kvcost predict --family {a.family}: give --queries, --tally-dir and the layouts")
        a.spec_files = spec_files(a.layouts)
        a.families = [(a.family, a.queries)]
        a.dataset = workloads.get(a.family).dataset
        a.nparam = NPARAM if a.nparam is None else a.nparam
        return None
    run = runs.load(a)
    fams = run.families()
    # a run predicts every family the registry gives the run's dataset (each on that dataset's stores)
    mine = [w for w in workloads.WORKLOADS if workloads.get(w).dataset == run.dataset]
    if not set(fams) <= set(mine):
        raise SystemExit(f"kvcost predict: {run.path} has queries of {fams} on {run.dataset}, whose families are "
                         f"{', '.join(mine)}: refused")
    if a.queries or a.tally_dir:
        raise SystemExit("kvcost predict --workload <file>: the queries come from the file and the tallies go to "
                         "predicted/<dataset>/<scale>/<set>/work/tally/ (no --queries, no --tally-dir)")
    runs.say_run("predict", run)
    a.sf = run.scale
    a.dataset = run.dataset
    from kvcost.cli import workloads as W
    if a.param_set is None:      # the params the measured side costs (LinkBench: the request log, its set "log")
        W.get(runs.adapter_of(run)).prepare(a)
    if a.nparam is None:         # and as many rows (LinkBench's whole log: 100,000)
        a.nparam = W.nparam_of(runs.adapter_of(run), a.param_set)
    a.families = [(f, ",".join(run.queries(f))) for f in fams]   # one capture per family, its own requests
    fresh_tallies = a.fresh     # --fresh: the tally library's entries are built again too
    # the run's own captures: a call predicts every cell again and replaces one made from other inputs (an edited
    # workload or plan, rebuilt statistics), as a measured run re-measures a cell whose identity moved; the binary
    # names what moved
    a.fresh = True
    a.spec_files = spec_files(a.layouts or sorted(L.CELLS))        # default: the nine grid layouts
    a.work = a.work or str(Tree().predicted_dir(run.dataset, a.sf, a.set) / "work")   # <work>/<family>/: requests
    a.tally_dir = os.path.join(a.work, "tally")
    from kvevaluator.predict import anchor_vids
    from kvevaluator.predict import build_stats as BS
    from kvevaluator.predict import tally_library as TL
    if a.dry_run:               # the inputs named, none built (the record stream not dumped, so no staleness read)
        full = a.stats or BS.stats_file(run.dataset, a.sf)
        print(f"[kvcost predict] statistics {runs.rel(full)}: {'there (built again if stale)' if os.path.exists(full) else 'to build'}"
              + ("; tier 1 derived from them" if a.tier == 1 and not a.stats else ""), flush=True)
        a.stats = a.stats or full
        print(f"[kvcost predict] {len(a.spec_files)} tallies to assemble in {runs.rel(a.tally_dir)} from the tally "
              f"library {runs.rel(TL.library_dir(run.dataset, a.sf))}", flush=True)
        return run
    records = records_file(run.dataset, a.sf)
    schema = a.schema = a.schema or str(ROOT / datasets.get(run.dataset).schema)
    # the dataset's own anchor statistics (LDBC: every Person, beside stats.json), or this workload's (own data: the
    # vids its params bind, in <work>/)
    declared_anchor = "--anchor-labels" in BS.declared_args(run.dataset)
    if not a.stats:             # a --stats file is the caller's: never rebuilt here
        full = BS.stats_file(run.dataset, a.sf)
        stale = BS.stale(full, records, run.dataset)
        if not os.path.exists(full) or (a.anchor and declared_anchor and not os.path.exists(BS.anchor_path(full))) or stale:
            t = time.time()
            BS.build(records, schema, run.dataset, a.sf, full)
            print(f"[kvcost predict] statistics built in {time.time() - t:.0f} s{f' ({stale})' if stale else ''}: "
                  f"{runs.rel(full)}", flush=True)
        from kvevaluator.predict import tier1
        a.stats = tier1.for_tier(full, a.tier)
        if a.tier == 1:
            print(f"[kvcost predict] tier 1 + the flat rows derived from the statistics: {runs.rel(a.stats)}",
                  flush=True)
    if a.anchor and not declared_anchor and not a.anchor_stats:
        t = time.time()
        vids = sorted({v for f, qs in a.families for v in anchor_vids.vids(f, a.sf, qs.split(","), a.nparam, a.param_set)})
        a.anchor_stats, did = anchor_vids.build(vids, records, schema, os.path.join(a.work, "anchor_stats.json"),
                                                run.dataset)
        print(f"[kvcost predict] the anchor statistics {did} in {time.time() - t:.0f} s: "
              f"{runs.rel(a.anchor_stats)}", flush=True)
    t = time.time()
    lib, tallies = TL.library_tallies(run.dataset, a.sf, a.spec_files, records, schema,
                                      at_once(a, "predict", "the tally library's settings", records),
                                      fresh=fresh_tallies, reuse_stale=a.reuse_stale)
    for c, spec in a.spec_files.items():     # each tally named as the dataset's store of the spec
        TL.write_named(tallies[c], spec, a.tally_dir, run.dataset)
    print(f"[kvcost predict] {len(a.spec_files)} tallies in {runs.rel(a.tally_dir)} from the tally library "
          f"{lib.path} ({len(lib.built)} settings built, {len(set(lib.reused))} reused, {time.time() - t:.0f} s)", flush=True)
    return run


def spec_files(layouts):
    """{cell: spec file} of the layouts named (kvevaluator.layouts, the one grammar): a name's or a cell id's spec,
    made from its name; a spec file as it is, named by its cell id when it holds a uniform layout, else (a mix) by its
    content, mix_<sha>"""
    out = {}
    for x in layouts:
        if x.endswith(".json"):
            lay = L.from_spec(x)
            with open(lay.spec) as f:
                spec = json.load(f)
            cell = L.cell_of_spec(L.expand(spec))
            out[cell if cell and L.spec_of(cell) == L.expand(spec) else L.mix_name(spec)] = lay.spec
        else:
            lay = L.resolve(x)
            out[lay.cell] = lay.spec
    return out


def check_49_args(a):
    """predict on the 49 takes one (dataset, scale) and the workload; the rest comes from the 49"""
    if not (a.dataset and a.sf):
        raise SystemExit("kvcost predict <the 49>: give --dataset DATASET=SCALE and --workload <id>")
    if a.queries:
        raise SystemExit("kvcost predict <the 49>: the queries come from the workload (no --queries)")
    if a.tally_dir and a.set == TIER_SETS[a.tier][0]:
        raise SystemExit(f"kvcost predict <the 49>: --tally-dir {a.tally_dir} (tallies from another place, e.g. a sample "
                         f"of the owners) needs --save-as with another name than the tier's own set ({a.set}): a "
                         f"prediction from other tallies never replaces the one made from the 49's own")


def workload_families(dataset, sf, workload):
    """{family: [query]} of a workload, in its file's order"""
    from kvcost.cli.search import load_workload
    fams = {}
    for fam, q, *_ in load_workload(dataset, sf, workload)["items"]:
        fams.setdefault(fam, [])
        if q not in fams[fam]:
            fams[fam].append(q)
    return fams


def fresh_stats(dataset, sf, records):
    """the dataset's statistics file at the scale, built when missing or stale against the records stream"""
    from kvevaluator.predict import build_stats as BS
    full = BS.stats_file(dataset, sf)
    stale = BS.stale(full, records, dataset)
    if not os.path.exists(full) or stale:
        BS.build(records, str(ROOT / datasets.get(dataset).schema), dataset, sf, full)
        print(f"[kvcost predict] statistics {full}: built{f' ({stale})' if stale else ''}", flush=True)
    return full


def tallies_49(a, verb, fresh=False, tallies=True):
    """the statistics (built when missing or stale) and the 49's specs and tallies (kvcost.predict_accuracy.
    uniform_49_tallies), assembled from the dataset's tally library (stats/<dataset>/<scale>/tallies/, tally_library.
    library_dir: what its record stream gives every setting, its settings built at_once) into
    stats/<dataset>/<scale>/uniform-49/{specs,tally}/: inputs of every predicted set and tier at a's dataset and
    scale, beside the statistics they come from. -> that dir. `tallies` False (the tallies come from another place,
    --tally-dir): the statistics and the specs only; the library is neither built nor read"""
    from kvcost.predict_accuracy import uniform_49_tallies as U
    from kvevaluator.predict import tally_library as TL
    dataset, sf = a.dataset, a.sf
    records = records_file(dataset, sf)
    fresh_stats(dataset, sf, records)
    from kvevaluator.output_tree import stats_dir
    out = str(stats_dir(dataset, sf) / "uniform-49")
    if not tallies:
        for c in U.CELLS:
            U.write_spec(out, c)
        return out
    U.assemble_all(out, records, dataset, lib_dir=TL.library_dir(dataset, sf), fresh=fresh,
                   jobs=at_once(a, verb, "the tally library's settings", records))
    return out


def run_49(a):
    """predict --dataset D=S --workload W <the 49 named>: the workload's families and queries (its file's order), the
    statistics and the 49's tallies (tallies_49), the params per query (5, or fewer where the params module has fewer:
    JOB one); the captures go to predicted/<dataset>/<scale>/<set>/, its requests and logs to <that>/work/"""
    from kvcost.cli import runs
    from kvcost.predict_accuracy import uniform_49_tallies as U
    runs.one_pair(a, "kvcost predict")
    check_49_args(a)
    fams = workload_families(a.dataset, a.sf, a.workload)
    a.families = [(fam, ",".join(qs)) for fam, qs in fams.items()]
    if a.nparam is None:
        a.nparam = min(min(NPARAM, len(workloads.params_module(fam).query_params(a.sf, qs[0], count=NPARAM)))
                       for fam, qs in fams.items())
    if a.dry_run:
        from kvevaluator.output_tree import stats_dir
        out = str(stats_dir(a.dataset, a.sf) / "uniform-49")
        print(f"[kvcost predict] the statistics and the 49's {'specs' if a.tally_dir else 'tallies'}: {runs.rel(out)} "
              f"(made where missing)", flush=True)
    else:
        out = tallies_49(a, "predict", tallies=not a.tally_dir)       # other tallies: the 49's own are not made
    a.spec_files = {c: os.path.join(out, "specs", f"{c}.json") for c in U.CELLS}
    own = os.path.join(out, "tally")
    if a.tally_dir and not a.dry_run:      # other tallies (check_49_args: into another set), every one of the 49: no fallback
        from kvevaluator.measure import stores
        gone = [c for c, spec in a.spec_files.items()
                if not os.path.exists(os.path.join(a.tally_dir, f"{stores.meta_store_name(spec)}.json"))]
        if gone:
            raise SystemExit(f"kvcost predict <the 49>: --tally-dir {a.tally_dir} holds no tally for {len(gone)} of the "
                             f"49 ({', '.join(gone[:3])}{' ...' if len(gone) > 3 else ''}); none is taken from {runs.rel(own)}")
        print(f"[kvcost predict] the 49's tallies from --tally-dir {runs.rel(a.tally_dir)}, into the set {a.set}", flush=True)
    a.tally_dir, a.fresh = a.tally_dir or own, True
    print(f"[kvcost predict] {a.workload} on the 49 at {a.dataset} {a.sf}, tier {a.tier}: "
          + "; ".join(f"{fam} {len(qs.split(','))} queries" for fam, qs in a.families) + f", {a.nparam} params each",
          flush=True)


def record_fields(workload):
    """what a predicted record carries beside the binary's own fields, as a measured record does: the workload, and the
    commit and pipeline of the plan files the requests were made from (the composer's plan rule reads them)"""
    from kvevaluator.plan_choice.candidates import PLAN_PIPELINE, plan_tree
    return {"workload": workload, "tree": plan_tree(), "plan_pipeline": PLAN_PIPELINE}


def param_set_fields(param_set):
    """the record's param set: named only when it is not the default (a default record has no param_set field)"""
    return {"param_set": param_set} if param_set else {}


def _text_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def inputs_keys(a, binary, stats, anchor, schema, fam, cells, queries):
    """{layout: the key of everything its prediction is made from}: the statistics (and the anchor statistics when
    their level is written), the layout's tally and spec, the schema, each query's plan file and the params it binds
    (their count and set), the body set (and the pick record when it names the bodies), the rules turned off, and the
    binary's counting versions (a version it states, never its commit). A layout's record carries its key
    (inputs_key); kept() compares it"""
    from kvevaluator.measure import stores, workload_plans
    from kvevaluator.measure.records_key import file_sha256
    from kvevaluator.plan_choice.pick_record import PICKS_FILE
    v = binary_version(binary)
    bodies = {"picked": "picks"}.get(a.bodies, a.bodies)
    ps = a.param_set
    if fam in workloads.names(kind="write") and not workloads.get(fam).own_data:
        ps = None                # LDBC's writes have one param set, whatever set the reads bind (predict_requests)
    asked = {"stats": file_sha256(stats), "anchor_stats": file_sha256(anchor) if anchor else None,
             "schema": file_sha256(schema), "rules_off": a.rules_off or "", "bodies": bodies,
             "picks": file_sha256(PICKS_FILE) if bodies == "picks" else None,
             "nparam": a.nparam, "param_set": a.param_set, "family": fam, "scale": str(a.sf),
             "queries": [[q, _text_key(workload_plans.ir_for(fam, q)),
                          _text_key(workload_plans.query_params(fam, a.sf, q, a.nparam, ps))] for q in queries],
             "binary": {"counting": [v.get("counting", {}).get("formats", {}).get("meta"), v.get("counting", {}).get("seam")],
                        "key_layout": v.get("key_layouts", {}).get("meta"), "tally_version": v.get("tally_version")}}
    common, out = _text_key(asked), {}
    for c in cells:
        with open(a.spec_files[c]) as f:
            spec = json.load(f)
        tally = os.path.join(a.tally_dir, f"{stores.meta_store_name(a.spec_files[c], a.dataset)}.json")
        out[c] = _text_key([common, spec, file_sha256(tally) if os.path.exists(tally) else None])[:32]
    return out


def kept(dirs, cell, key, binary):
    """(is the layout's capture in every set of `dirs` one predicted on the inputs `key` names: its record carries the
    key and its three tables are there; the commit of the binary that predicted it when it is not this binary's)"""
    other = None
    for d in dirs:
        rec = os.path.join(d, "counts", f"{cell}.json")
        if not all(os.path.exists(os.path.join(d, kind, f"{cell}.csv")) for kind in ("counts", "steps", "parts")):
            return False, None
        try:
            with open(rec) as f:
                old = json.load(f)
        except (OSError, ValueError):
            return False, None
        if old.get("inputs_key") != key:
            return False, None
        sha = (old.get("binary") or {}).get("binary_git_sha")
        if sha != binary_version(binary).get("binary_git_sha"):
            other = (sha or "?")[:10]
    return True, other


def predict_family(a, binary, stats, anchor, schema, fam, cells, queries, work):
    """one family's requests and its captures (--set, and --anchor-set when `anchor` names the anchor statistics) for
    every layout not already predicted on the same inputs (inputs_keys; --fresh: every layout):
    ([layouts that failed], [the capture dirs], KV steps not estimated, layouts kept)"""
    print(f"[kvcost predict] {fam}: binary {binary} {binary_line(binary)}; {len(cells)} layouts, {len(queries)} queries, "
          f"bodies {a.bodies}; requests in {work}", flush=True)
    if a.dry_run:
        dirs = capture_dirs(a.sf, fam, (a.set, a.anchor_set) if anchor else (a.set,), None, a.param_set)
        for c in cells:
            print(f"  {c}: {binary} predict --plans --stats {stats} --tally-dir {a.tally_dir} --schema {schema} "
                  f"--out-predicted {dirs[0]}"
                  + (f" --out-predicted-anchor {dirs[1]}" if anchor else "")
                  + f" --requests-from {os.path.join(work, 'logs', c + '.requests.txt')}", flush=True)
        return [], dirs, 0, 0
    dirs = capture_dirs(a.sf, fam, (a.set, a.anchor_set) if anchor else (a.set,), None, a.param_set)
    keys = inputs_keys(a, binary, stats, anchor, schema, fam, cells, queries)
    have = {} if a.again else {c: other for c in cells for ok, other in [kept(dirs, c, keys[c], binary)] if ok}
    if have:
        others = sorted({o for o in have.values() if o})
        print(f"[kvcost predict] {fam}: {len(have)} of {len(cells)} layouts already predicted on the same inputs: kept "
              f"(--fresh predicts again)"
              + (f"; predicted by another binary commit ({', '.join(others)}), which the key does not hold" if others else ""),
              flush=True)
    done, cells = len(have), [c for c in cells if c not in have]
    if not cells:
        return [], dirs, 0, done
    reqs = RQ.write(a.sf, fam, cells, queries, a.nparam, a.tally_dir, work, {"picked": "picks"}.get(a.bodies, a.bodies),
                    specs=a.spec_files, schema_json=schema,
                    param_set=a.param_set, dataset=a.dataset, jobs=a.layouts_at_once)
    base = [binary, "predict", "--plans", "--stats", stats, "--tally-dir", a.tally_dir, "--schema", schema,
            "--out-predicted", dirs[0]]
    if anchor:      # the anchor level: only when asked (--anchor, --anchor-stats)
        base += ["--anchor-stats", anchor, "--out-predicted-anchor", dirs[1]]
    else:           # an anchor capture an earlier call wrote is not updated by this one: say so
        old = capture_dirs(a.sf, fam, (TIER_SETS[a.tier][1],), None, a.param_set)[0]
        if os.path.isdir(os.path.join(old, "counts")):
            print(f"[kvcost predict] {fam}: no anchor level (no --anchor); the anchor capture {old} is left as it was, "
                  f"from an earlier call", flush=True)
    if a.rules_off:
        base += ["--rules-off", a.rules_off]          # the binary's own flag
    if a.fresh:
        base += ["--fresh"]
    os.makedirs(os.path.join(work, "logs"), exist_ok=True)

    fields = {}     # per layout: what its record carries beside the binary's own fields
    for c in cells:
        fields[c] = {**record_fields(fam), "bodies": {"picked": "picks"}.get(a.bodies, a.bodies),   # the records' word
                     **param_set_fields(a.param_set), "inputs_key": keys[c]}
        if a.bodies == "all":       # what this layout's requests leave out, and why (a measured capture runs them all)
            fields[c]["bodies_left_out"] = RQ.left_out(fam, os.path.join(work, "specs", f"{c}.json"), queries)

    def one(c):
        # the requests go by a list file, one path per line: a set can exceed the argument list (LinkBench's log)
        listed = os.path.join(work, "logs", f"{c}.requests.txt")
        names = sorted(os.listdir(reqs[c]))
        with open(listed, "w") as f:
            f.writelines(os.path.join(reqs[c], name) + "\n" for name in names)
        s = time.time()
        with open(os.path.join(work, "logs", f"{c}.err"), "w") as err:
            r = subprocess.run(base + ["--record-fields", json.dumps(fields[c]), "--requests-from", listed],
                               stdout=subprocess.DEVNULL, stderr=err)
        return c, r.returncode, time.time() - s, len(names)

    bad, not_estimated = [], 0
    with ThreadPoolExecutor(max_workers=a.layouts_at_once) as ex:
        for c, rc, wall, sent in ex.map(one, cells):
            log = os.path.join(work, "logs", f"{c}.err")
            lines = open(log).readlines()
            summary = next((l.strip() for l in lines if l.startswith("predict --plans: binary")), "(no summary line)")
            read = next((l.split(": ")[-1].split()[0] for l in lines if l.startswith("predict: --requests-from")), None)
            print(f"[kvcost predict] {fam} {c} rc {rc} ({wall:.0f} s): {summary}", flush=True)
            m = re.search(r" (\d+) not estimated;", summary)   # the binary's count (rc 0: a step not estimated is no refusal)
            not_estimated += int(m.group(1)) if m else 0
            if rc == 0 and read != str(sent):    # every request listed was read: none dropped on the way
                rc = 1
                print(f"[kvcost predict] {fam} {c}: the binary read {read} request paths of the {sent} listed: refused",
                      flush=True)
            if rc != 0:
                bad.append(c)
                print("".join(open(log).readlines()[-5:]), file=sys.stderr)
    return bad, dirs, not_estimated, done


def cmd_predict(a, argv):
    t0 = time.time()
    a.again = a.fresh                                      # --fresh as given: a run sets a.fresh for the binary's own rule
    a.anchor = a.anchor or bool(a.anchor_stats)            # --anchor-stats-file F asks for the anchor level too
    if a.stats and a.tier not in (None, 2):
        raise SystemExit("kvcost predict: --tier picks the dataset's own statistics file; --stats-file names another "
                         "(give one)")
    from kvevaluator.predict.tier1 import DEFAULT_TIER
    a.tier = 2 if a.stats else (a.tier or DEFAULT_TIER)   # a --stats-file run is named as the full file's set
    a.layouts = __import__("kvcost.cli.runs", fromlist=["mix_paths"]).mix_paths(a, a.layouts)
    a.set = a.set or TIER_SETS[a.tier][0]                  # tier 1's captures never replace tier 2's
    a.anchor_set = (f"{a.set}-anchor" if a.set != TIER_SETS[a.tier][0] else TIER_SETS[a.tier][1]) if a.anchor else None
    if bool(a.family) == bool(a.workload):
        raise SystemExit("kvcost predict: give --workload (a workload: an id or a file) or --family (with --queries, "
                         "--tally-dir and the layouts), one of the two")
    if not a.layouts and not a.sf and a.family:      # a family call names its scale
        raise SystemExit("kvcost predict: give the scale, --dataset D=S")
    from kvcost.predict_accuracy import uniform_49_tallies as U
    family = bool(a.family)
    the_49 = not family and bool(a.layouts) and U.is_the_49(a.layouts)
    run = run_49(a) if the_49 else run_of(a)
    from kvevaluator.predict.build_stats import anchor_path, stats_file
    stats = a.stats
    if not stats:              # a family call: the dataset's statistics, built beforehand (build_stats); tier 1 from them
        stats = stats_file(a.dataset, a.sf)
        if os.path.exists(stats) and not a.dry_run:
            from kvevaluator.predict import tier1
            stats = tier1.for_tier(stats, a.tier)
    anchor = (a.anchor_stats or anchor_path(stats)) if a.anchor else None   # None: no anchor level
    for p in (stats, anchor) if anchor else (stats,):
        if not os.path.exists(p) and not (a.dry_run and run is not None):     # a run's dry run named it: to build
            how = (f"--family {workloads.names(kind='read', dataset=a.dataset)[0]} --tag {a.sf}"
                   if datasets.get(a.dataset).own_axis else f"--sf {a.sf}")
            raise SystemExit(f"kvcost predict: no statistics {p} (python -m kvevaluator.predict.build_stats {how})")
    binary = binary_path()
    cells = list(a.spec_files)
    schema = str(ROOT / datasets.get(a.dataset).schema) if a.schema is None else a.schema
    bad, dirs, nq, not_estimated, done = [], [], 0, 0, 0
    a.layouts_at_once = at_once(a, "predict", "layouts (their requests written, then their binary calls)")
    for fam, fam_queries in a.families:
        queries = [q for q in fam_queries.split(",") if q]
        nq += len(queries)
        # a workload file: each family's requests in <work>/<family>/; a family call: --work or the default for its set
        work = os.path.abspath(os.path.join(a.work, fam) if run is not None else
                               a.work or Tree().predicted_dir(a.dataset, a.sf, a.set) / "work" / fam)
        bad_f, dirs_f, ne_f, done_f = predict_family(a, binary, stats, anchor, schema, fam, cells, queries, work)
        not_estimated += ne_f
        done += done_f
        bad += [f"{c} ({fam})" for c in bad_f]
        dirs += dirs_f
    n = len(cells) * len(a.families)
    if a.dry_run:
        print(f"[kvcost predict] the calls, none run (--dry-run): {n} (layout, family) pairs ({nq} queries) -> "
              f"{', '.join(dirs)}")
        return 0
    gaps = f", {not_estimated} KV steps not estimated (named in the logs)" if not_estimated else ""
    said = (f"{n - len(bad) - done} of {n} (layout, family) pairs predicted"
            + (f", {done} already predicted on the same inputs (kept; --fresh predicts again)" if done else ""))
    msg = (f"{said}{gaps} in {time.time() - t0:.0f} s -> "
           f"{', '.join(dirs)}" + (f"; FAILED: {','.join(bad)}" if bad else ""))
    if run is None:
        print(f"[kvcost predict] {msg}")
    else:
        from kvcost.cli import runs
        runs.answer("predict", f"{said} ({nq} queries){gaps}, "
                    f"captures in {predicted_place(None, a.dataset, a.sf, a.set)}"
                    + (f"; FAILED: {','.join(bad)}" if bad else ""))
    return 1 if bad else 0


def cmd_build_statistics(a, argv):
    """the statistics and the 49 uniform layouts' tallies at one dataset and scale (tallies_49): what every prediction
    at that scale reads, made once per dataset, as build-store is for measure; each records its own wall_s"""
    from kvcost.cli import runs
    from kvcost.predict_accuracy import uniform_49_tallies as U
    from kvevaluator.output_tree import stats_dir
    runs.one_pair(a, "kvcost build-statistics")
    if not (a.dataset and a.sf):
        raise SystemExit("kvcost build-statistics: give --dataset DATASET=SCALE")
    if a.layouts and not U.is_the_49([L.cell_of(x) for x in a.layouts]):
        raise SystemExit("kvcost build-statistics: the 49 uniform layouts' tallies (the default, or the 49 named); a "
                         "workload's own layouts' tallies are made by kvcost predict --workload <file> <layouts>")
    out = stats_dir(a.dataset, a.sf) / "uniform-49"
    if a.dry_run:
        print(f"[kvcost build-statistics] the statistics in {runs.rel(stats_dir(a.dataset, a.sf))} and the 49's tallies "
              f"in {runs.rel(out)} (made where missing{', all again: --fresh' if a.fresh else ''}); none made (--dry-run)")
        return 0
    t0 = time.time()
    if a.fresh:
        from kvevaluator.predict import build_stats as BS
        BS.build(records_file(a.dataset, a.sf), str(ROOT / datasets.get(a.dataset).schema), a.dataset, a.sf,
                 BS.stats_file(a.dataset, a.sf))
    got = tallies_49(a, "build-statistics", fresh=a.fresh)
    runs.answer("build-statistics", f"the statistics and the 49's tallies at {a.dataset} {a.sf} in "
                                    f"{time.time() - t0:.0f} s: {runs.rel(got)}")
    return 0


def accuracy_49(a, run, w):
    """the paper's report of the 49 named with a predicted set against measured, into summary/<dataset>/<scale>/
    predict-statistics/<workload>/<set>/: uniform.csv and uniform_totals.csv (the predicted costs of the 49); a workload
    with a write family by its totals (workload_pick: pick.csv and layouts.csv, against the measured 49's layout-costs
    place), a read-only one per query against its grid composed in memory (layouts.run: layout_choice/). Both read the
    tree --from names"""
    from argparse import Namespace
    from kvcost.layout_cost import benchmarks
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.predict_accuracy import layouts, workload_pick
    from kvcost.predict_accuracy import uniform as PU
    from kvevaluator import weights
    from kvcost.layout_cost import compose
    src = Tree(a.tree)
    fams = benchmarks.families_read(a.dataset, [w])
    out = str(Tree().summary(a.dataset, a.sf, "predict-statistics", w["id"], a.set))
    rows, tot, missing = PU.uniform_cost(a.sf, w, a.set, a.tree, "wops_bytes4k", None, w=weights.parse(None),
                                         view=compose.resolve_chunk_view(None, "wops_bytes4k"))
    if missing:
        raise SystemExit(f"predict-accuracy: {a.set}: no body costed for {sorted(missing.items())[:3]}")
    os.makedirs(out, exist_ok=True)
    PU.write(os.path.join(out, "uniform.csv"), rows)       # the predicted costs of the 49, the layouts within 5%
    PU.write_totals(os.path.join(out, "uniform_totals.csv"), tot, "wops_bytes4k", PU.names_of(rows))
    if any(k != "read" for _, k in fams):
        measured = LC.place_of_run(src, run, "measured")
        if not os.path.isfile(os.path.join(measured, "layouts.csv")):
            raise SystemExit(f"predict-accuracy: no measured layout costs at {measured} (kvcost cost-layouts --dataset "
                             f"{a.dataset}={a.sf} --workload {w['id']} Sp-Sp Sp-Co ... Ch256k-Ch256k)")
        workload_pick.run(os.path.join(out, "uniform.csv"), str(measured), out)
        return out
    first = fams[0][0]
    nparam = min(NPARAM, len(workloads.params_module(first).query_params(a.sf, w["items"][0][1], count=NPARAM)))
    layouts.run(Namespace(sf=a.sf, predicted=capture_dirs(a.sf, first, (a.set,), a.tree)[0],
                          grid=str(src.place_dir("captures", a.dataset, a.sf)), dataset=a.dataset, cost_workload=a.workload,
                          check_workload=None, check_reference=None, queries=a.queries, param_rows=f"1-{nparam}",
                          out=out, expect_layouts=49, measured_table=None))
    return out


def side_rows(place, w, model, keep):
    """compare.compare_tables' rows of one side: query_costs.csv of a layout-costs place in the model's chunk view,
    each with its layout's short name and its query's frequency (the workload's); a layout not in keep (None: all) left out"""
    from kvcost.layout_cost import layout_costs as LC
    view = LC.view_of(model)
    names = {r["layout"]: r["name"] or r["layout"] for r in LC.read_layouts(place, view)}
    freq = LC.frequencies(w)
    order, by = LC.read_query_costs(place, view)
    out = []
    for fq in order:
        if fq not in freq:
            continue
        for lay, r in by[fq].items():
            if keep is not None and lay not in keep:
                continue
            v = LC.query_cost(r, model)
            out.append({"layout": lay, "name": names.get(lay, lay), "family": fq[0], "query": fq[1],
                        "frequency": freq[fq], model: "" if v is None else v})
    return out


def compare_sources(a, run, w):
    """--count-source against --against over the layouts both layout-costs places hold (the named ones, if any): picks.csv
    and layouts.csv into summary/<dataset>/<scale>/predict-accuracy/<workload>/<source>-vs-<against>/ -> that dir, or
    None (a side has no place, when the 49's report ran)"""
    from kvcost.cli import compare as CC
    from kvcost.layout_cost import layout_costs as LC
    src = Tree(a.tree)
    from kvcost.cli import workloads as W
    pick_on = W.place_pick_on(run.dataset, run.families(), a.pick_on)
    sides = {s: LC.place_of_run(src, run, s, pick_on=pick_on, param_set=getattr(a, "param_set", None))
             for s in (a.set, a.against)}
    lacking = [s for s, p in sides.items() if not os.path.isfile(os.path.join(p, "query_costs.csv"))]
    if lacking:
        return lacking
    keep = None
    if a.layouts_set:
        from kvcost.cli import runs
        keep = set()
        for x in runs.mix_paths(a, a.layouts_set):
            keep.add(L.cell_of(x) if not str(x).endswith(".json") else
                     L.mix_name(json.load(open(x))) if L.cell_of_spec(L.expand(json.load(open(x)))) is None
                     else L.cell_of_spec(L.expand(json.load(open(x)))))
    keep_q = set(a.queries.split(",")) if a.queries else None
    models = [a.model] + [m for m in ("wops_bytes4k", "wops", "bytes") if m != a.model]
    picks, per_layout, one_side, left = [], [], {"A": set(), "B": set()}, set()
    for m in models:
        p, l, o, lo = CC.compare_tables(side_rows(sides[a.set], w, m, keep), side_rows(sides[a.against], w, m, keep),
                                        [m], keep_q)
        picks += p
        per_layout += l
        for k in o:
            one_side[k] |= set(o[k])
        left |= set(lo)
    out = str(Tree().summary(a.dataset, a.sf, "predict-accuracy", w["id"], f"{a.set}-vs-{a.against}"))
    os.makedirs(out, exist_ok=True)
    CC.write(os.path.join(out, "picks.csv"), picks)
    CC.write(os.path.join(out, "layouts.csv"), per_layout)
    for side, names in (("--count-source", one_side["A"]), ("--against", one_side["B"])):
        if names:
            print(f"[kvcost predict-accuracy] left out, costed on {side} only: {', '.join(sorted(names))}")
    if left:
        print(f"[kvcost predict-accuracy] left out (a query missing on one side): {', '.join(sorted(left))}")
    for line in CC.summary_lines(picks, a.model, a.set, a.against):
        print(f"[kvcost predict-accuracy] {line}")
    return out


def per_step(a, run):
    """--per-step: a DIR's steps.csv reported, or each read family's predicted capture of --count-source joined to the
    measured capture of the tree --from names, into the place's per-step/ -> that dir"""
    from kvcost.predict_accuracy import join as J
    from kvcost.predict_accuracy import report
    from kvevaluator.capture.binary import binary_path
    binary = binary_line(binary_path())
    if isinstance(a.per_step, str):
        out = a.per_step
    else:
        if a.against != "measured":
            raise SystemExit("predict-accuracy --per-step: joins a predicted set to measured (--against measured)")
        src = Tree(a.tree)
        fams = [(f, qs) for f, qs in (a.families or [])]
        out = str(Tree().summary(a.dataset, a.sf, "predict-accuracy", run.name if run else a.family,
                                 f"{a.set}-vs-{a.against}") / "per-step")
        cells = [L.cell_of(x) for x in a.layouts_set] if a.layouts_set else None
        rows, lines = [], []
        for fam, qs in fams:
            place = src.place_dir("captures", a.dataset, a.sf)
            meas = J.measured_capture(str(place), fam, None)
            lacking = [c for c in (cells or []) if not os.path.isfile(meas.path_of("counts", c))]
            if lacking:
                raise SystemExit(f"predict-accuracy --per-step: {', '.join(L.name_of(c) for c in lacking)} not measured in "
                                 f"{meas.path} (the join reads measured step rows; kvcost measure --dataset "
                                 f"{a.dataset}={a.sf} --workload ... {' '.join(L.name_of(c) for c in lacking)})")
            pred = capture_dirs(a.sf, fam, (a.set,), a.tree, getattr(a, "param_set", None))[0]
            anchor = capture_dirs(a.sf, fam, (f"{a.set}-anchor",), a.tree, getattr(a, "param_set", None))[0]
            r, line = J.joined(a.sf, fam, pred, anchor if os.path.isdir(anchor) else None,
                               str(src.place_dir("captures", a.dataset, a.sf)), cells, qs, a.param_rows)
            rows += r
            lines.append(line)
        J.write_steps(out, rows)
        for line in lines:
            print(line)
    with open(os.devnull, "w") as quiet:
        import contextlib
        with contextlib.redirect_stdout(quiet):
            report.main([out, binary])
    print(f"kvcost predict-accuracy: {out}/steps.csv, ratios.csv, summary.md")
    return out


def cmd_predict_accuracy(a, argv):
    from kvcost.cli import runs
    from kvcost.cli.search import load_workload
    from kvcost.predict_accuracy import uniform_49_tallies as U
    runs.one_pair(a, "kvcost predict-accuracy")
    if isinstance(a.per_step, str) and not os.path.isfile(os.path.join(a.per_step, "steps.csv")):
        a.layouts_set = [runs.layout_arg()(a.per_step)] + list(a.layouts_set)   # a layout after --per-step, not its DIR
        a.per_step = True
    if isinstance(a.per_step, str):
        return 0 if per_step(a, None) else 1
    if bool(a.workload) == bool(a.family):
        raise SystemExit("kvcost predict-accuracy: give --workload (an id or a file), or --family F --queries a,b with "
                         "--per-step")
    if a.family:
        if not (a.per_step and a.queries):
            raise SystemExit("kvcost predict-accuracy --family: with --per-step and --queries (the join per KV step)")
        if not a.sf:
            raise SystemExit("kvcost predict-accuracy --family: give --dataset DATASET=SCALE")
        a.families = [(a.family, a.queries.split(","))]
        per_step(a, None)
        return 0
    run = runs.load(a)
    a.dataset, a.sf = run.dataset, run.scale
    w = load_workload(a.dataset, a.sf, a.workload)
    if a.per_step:
        keep = set(a.queries.split(",")) if a.queries else None
        reads = set(workloads.names(kind="read"))
        a.families = [(f, [q for q in run.queries(f) if keep is None or q in keep]) for f in run.families() if f in reads]
        per_step(a, run)
        return 0
    paper = None
    named = [str(x) for x in a.layouts_set]
    if (named and not any(x.endswith(".json") or x.startswith("mix:") for x in named) and U.is_the_49([L.cell_of(x) for x in named])
            and a.set.startswith("predicted") and a.against == "measured"):
        paper = accuracy_49(a, run, w)
    got = compare_sources(a, run, w)
    if isinstance(got, list):
        need = " and ".join(f"kvcost cost-layouts --dataset {a.dataset}={a.sf} --workload {w['id']} --count-source {s}"
                            for s in got)
        if paper is None:
            raise SystemExit(f"kvcost predict-accuracy: no layout costs for {', '.join(got)} under the tree: run {need}")
        runs.answer("predict-accuracy", f"the 49's report in {runs.rel(paper)}; no per-query comparison ({need} "
                                        f"makes the missing side)")
        return 0
    runs.answer("predict-accuracy", f"{runs.rel(got)}" + (f"; the 49's report in {runs.rel(paper)}" if paper else ""))
    return 0


def add_parsers(sub):
    from kvcost.cli import runs
    x = sub.add_parser("predict", help="predict mode's counts for layouts, as captures (no store opened)",
                       description=PREDICT_DOC, formatter_class=__import__("argparse").RawDescriptionHelpFormatter)
    from kvcost.cli.cli import DatasetScale
    x.add_argument("--dataset", default=None, action=DatasetScale, metavar="DATASET=SCALE",
                   help="the dataset and its scale (ldbc=3, imdb=full); a family call needs it, a workload file "
                        "defaults to its own")
    x.set_defaults(sf=None)
    x.add_argument("layouts", nargs="*", metavar="LAYOUT", type=runs.layout_arg(),
                   help="the layouts predicted: a name at any chunk size (Sp-Co, Ch4k-Ch16k; the grid's 64 KB is Ch64k), a "
                        "cell id, a mix's spec file (its capture named by its content, mix_<sha>), or a search row's mix "
                        "(mix:<question>/<model>[-<limit>], mix:best); a workload file's default: the nine grid "
                        "layouts. The 49 uniform layouts named (Sp-Sp Sp-Co ... Ch256k-Ch256k) with a workload id: "
                        "their tallies made once per scale (kvcost build-statistics), the captures into "
                        f"predicted/<dataset>/<scale>/<set>/ ({', '.join(f'{s} with --tier {t}' for t, (s, _) in TIER_SETS.items())})")
    x.add_argument("--workload", type=runs.workload_arg, default=None,
                   help="a workload file (or its id): its captures in predicted/<dataset>/<scale>/<set>/, its "
                   "requests, tallies and logs in that set's work/; or --family")
    x.add_argument("--family", choices=workloads.names(), default=None,
                   help="one family (a query set) in place of --workload: the plans of --queries, with --tally-dir "
                   "and the layouts")
    x.add_argument("--queries", help="comma list of plan names (with --family)")
    x.add_argument("--tier", type=int, choices=sorted(TIER_SETS), default=None,
                   help="the statistics read: 1 (the default, kvevaluator.predict.tier1.DEFAULT_TIER) a coarser "
                        "summary of the dataset's full statistics (tier 1 and the flat rows, stats-tier1.json beside "
                        f"them, derived again on every call), into the set {TIER_SETS[1][0]}; 2 the full statistics, "
                        f"into the set {TIER_SETS[2][0]}")
    x.add_argument("--anchor", action="store_true", help="also the anchor level (off by default): each body costed a "
                   "second time from the anchor statistics (one row per start vertex), into the set's -anchor twin")
    x.add_argument("--nparam", type=int, default=None, help="params per query (default 5; a run: as many as the "
                   "measured side costs, LinkBench's whole request log)")
    x.add_argument("--save-as", dest="set", default=None, metavar="SET",
                   help=f"the name of the predicted set written (default by --tier: "
                        f"{', '.join(s for s, _ in TIER_SETS.values())})")
    x.add_argument("--jobs", type=int, default=None,
                   help="units at once, a process each: the layouts (their requests written, then their binary calls), "
                        "and the tally library's settings built (one pass over the record stream each). Default: the "
                        "machine's cores, and for the tally settings no more than the available memory over one "
                        "pass's peak (0.9 x the record stream's file). The captures do not depend on it")
    x.add_argument("--fresh", action="store_true", help="predict again: keep no capture an earlier call predicted on the "
                   "same inputs, and replace one made from other inputs (statistics, rules, tally, spec or request "
                   "set; a run, --workload, always replaces those); a run also builds the tally library's entries again")
    e = x.add_argument_group("expert", "other inputs and the plans predicted")
    e.add_argument("--stats-file", dest="stats", help="another statistics file (default the dataset's, "
                   "build_stats.stats_file: LDBC's output/stats/ldbc/sf<SF>/stats.json, an own-data dataset's "
                   "output/stats/<its root>/<tag>/stats.json); never rebuilt here")
    e.add_argument("--anchor-stats-file", dest="anchor_stats", help="the anchor statistics; implies --anchor (default "
                   "anchor_stats.json beside the statistics; a workload file on own data (LinkBench, IMDb): "
                   "<work>/anchor_stats.json, of the vids its params bind, built here)")
    e.add_argument("--schema-file", dest="schema", help="the schema.json (default the dataset's)")
    e.add_argument("--tally-dir", help="the layouts' tallies, meta-<sha12>.json: a built store's name "
                   "(python -m kvevaluator.predict.tally_library --out-dir, or kvevaluator tally); with --family. With "
                   "the 49 uniform layouts named: their tallies from this dir instead of the 49's own (e.g. tallies "
                   "counted on a sample of the owners), taken only with --save-as naming another set than the tier's "
                   "own, so the set made from the 49's own tallies is never replaced; every one of the 49 must be "
                   "there, and the 49's own tallies are then neither built nor read")
    e.add_argument("--work-dir", dest="work", help="where the requests go (default predicted/<dataset>/<scale>/<set>/"
                   "work/<workload>; a workload file: <work>/<family>/, its tallies in <work>/tally/)")
    e.add_argument("--plans", dest="bodies", choices=("all", "picked"), default="all",
                   help="every legal plan (body) of each query (default), or the one the pick record names per "
                        "(layout, query)")
    e.add_argument("--no-rules", dest="rules_off", metavar="RULES",
                   help="cardinality rules turned off, a comma list (the binary's --rules-off)")
    e.add_argument("--reuse-stale", action="store_true", help="a run: keep a tally library entry made from another "
                   "record stream or binary (never another tally version), marked reused_stale (kvevaluator.stale)")
    x.add_argument("--dry-run", action="store_true", help="print the inputs and each binary call, build and run none")
    from kvcost.layout_cost import benchmarks
    benchmarks.add_params_arg(x, help="the params the requests bind: a set the workload's params module lists (LDBC "
                                      "Interactive: ldbc, the default, or uniform; LinkBench: log, the default, quick, "
                                      "quick-next, anchors); a non-default set's captures go to its own rows dir")
    x.set_defaults(fn=cmd_predict)
    bs = sub.add_parser("build-statistics", help="the statistics and the 49's tallies, once per dataset (no store)",
                        description="The dataset's statistics (built from its record stream when missing or stale) and "
                                    "the tallies of the 49 uniform layouts, assembled from the tally library, in "
                                    "stats/<dataset>/<scale>/ (stats.json, tallies/, uniform-49/): what every kvcost "
                                    "predict at that scale reads, as build-store is for measure. Each records its own "
                                    "wall_s (the run-time table's statistics and tally rows)")
    bs.add_argument("--dataset", default=None, action=DatasetScale, metavar="DATASET=SCALE",
                    help="the dataset and its scale (ldbc=3, imdb=full, linkbench=n1m)")
    bs.set_defaults(sf=None)
    bs.add_argument("layouts", nargs="*", metavar="LAYOUT", type=L.arg_type(files=False),
                    help="the 49 uniform layouts (the default; named: Sp-Sp Sp-Co ... Ch256k-Ch256k)")
    bs.add_argument("--jobs", type=int, default=None,
                    help="the tally library's settings built at once (one pass over the record stream each; default: "
                         "the machine's cores, and no more than the available memory over one pass's peak, 0.9 x the "
                         "record stream's file)")
    bs.add_argument("--fresh", action="store_true", help="build the statistics and the tally library's entries again")
    bs.add_argument("--dry-run", action="store_true", help="name what would be made, make none")
    bs.set_defaults(fn=cmd_build_statistics)
    y = sub.add_parser("predict-accuracy", help="one count source against another: the pick, x over the best, Spearman",
                       description=ACCURACY_DOC, formatter_class=__import__("argparse").RawDescriptionHelpFormatter)
    y.set_defaults(sf=None)
    y.add_argument("--dataset", action=DatasetScale, metavar="DATASET=SCALE",
                   help="the dataset and its scale (ldbc=3); a workload file defaults to its own")
    y.add_argument("--workload", type=runs.workload_arg, default=None, help="the workload (an id or a file)")
    y.add_argument("layouts_set", nargs="*", metavar="LAYOUT", type=runs.layout_arg(),
                   help="the layouts compared (default every layout both sides costed): names at any chunk size, cell "
                        "ids, spec files or mix:<question>/<model>. The 49 uniform layouts named (Sp-Sp Sp-Co ... "
                        "Ch256k-Ch256k) with a predicted set against measured also write the paper's report "
                        "(predict-statistics/)")
    y.add_argument("--count-source", dest="set", default=default_set(),
                   help=f"the side that names a layout: a predicted set (kvcost predict --save-as; default "
                        f"{default_set()}, the default tier's) or measured")
    y.add_argument("--against", default="measured", help="the side that judges it (default measured), or another "
                   "predicted set")
    y.add_argument("--cost-model", dest="model", default="wops_bytes4k",
                   help="the model printed (default wops_bytes4k); picks.csv holds wops_bytes4k, wops and bytes, and "
                        "a bill model when named")
    y.add_argument("--queries", help="keep only these queries (comma list)")
    y.add_argument("--from", dest="tree", default=None, metavar="TREE", help="the tree read (default the output dir)")
    benchmarks = __import__("kvcost.layout_cost.benchmarks", fromlist=["add_pick_on_arg"])
    benchmarks.add_pick_on_arg(y, help="which layout-costs place each side is read from: the plans picked on held-out "
                                       "params (the default where the dataset has a held-out set) or in-sample")
    e = y.add_argument_group("expert", "per KV step")
    e.add_argument("--per-step", nargs="?", const=True, default=None, metavar="DIR",
                   help="per KV step: each read family's predicted capture joined to the measured capture, reported "
                        "into the place's per-step/ (steps.csv, ratios.csv, summary.md); DIR: report a dir whose "
                        "steps.csv kvevaluator predict --accuracy wrote")
    e.add_argument("--family", choices=workloads.names(), default=None,
                   help="with --per-step, in place of --workload: one family's --queries")
    e.add_argument("--param-rows", dest="param_rows", default="1-5", help="with --per-step: the param rows joined, lo-hi "
                   "(default 1-5)")
    y.set_defaults(fn=cmd_predict_accuracy)
