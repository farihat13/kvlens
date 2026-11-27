"""kvcost measure-latency: the workload's queries timed on each layout's store under one rule, the paper's: its store
timed in a bounded block cache (inputs/contract/store_config.json's cache, --cache-mb) over --passes passes with the page cache
dropped before it and every server bound to one NUMA node (run/latency-cells.sh), each query at the body the picks file
names for its layout (inputs/latency-picks/<dataset>/<scale>/<family>.json). Nothing is counted for a layout the file
covers; a layout it does not cover (a mix, another chunk size, a scale with no file) is counted first, as its pick needs
(kvcost measure on the diagonals, Sp-Sp, Co-Co and ChK-ChK; kvcost compose for every other uniform layout, from them; a
mix measured itself), and --validate-count counts every layout. Where counts are in the output dir, the counts gate
compares every timed run's counts with the capture's row for the timed plan; a store whose counts differ is kept,
marked (timed_counts_differ) and named in the answer. Then one number per layout.

    kvcost measure-latency --dataset ldbc=3 --workload i01 Sp-Sp Co-Co ... --save-picks      # the counts' pick into the file
    kvcost measure-latency --dataset ldbc=3 --workload i01 Sp-Sp Co-Co ...                   # then time it

--save-picks counts the named uniform layouts (or reads the counts in --from TREE), writes their picks into the file and
times nothing; `git diff inputs/latency-picks/` shows what moved. Timing the counts' pick after a plan change is those
two commands.

    kvcost measure-latency --workload my.json --dataset ldbc=0.1 Co-Ch64k mix:best
    kvcost measure-latency --dataset ldbc=3 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k ... mix:i02-any-size mix:i02-predicted \
        --drift-control Sp-Sp

Without passwordless sudo the page cache is not dropped and the machine not prepared (NUMA balancing, swappiness): the
run goes on, says so in its output and in summary.csv's machine column, and marks its records (stale.jsonl).

A layout is a name at any chunk size, a mix's spec file, mix:<question>/<model>[-<limit>] or mix:best (a search row's
mix), or mix:<question>: the mixes a question hands to the timing by one rule (TIMED): from a measured question, its
fewest wops_bytes4k row (lat_<workload>_measured_mix_any_size) and its fewest bill_exact row, else its local search's
result (lat_<workload>_bill_mix_any_size); from a predicted question (search-mix --count-source), its fewest
wops_bytes4k row (lat_<workload>_predicted_mix_any_size); their specs go to summary/<dataset>/<scale>/latency-mixes/
<query set>/. A workload with a write family also times its writes after each store's steady warm-up. The raw timings
go to the tree's timings/<dataset>/<scale>/<query set>/meta-<layout>/<regime>/ (grid_latency's records to
summary/<dataset>/<scale>/grid-latency/<query set>/); summary/<dataset>/<scale>/workload-latency/<workload id>/summary.csv
holds per (layout, regime, query) the aggregate and its spread, so another aggregation is a re-read, not a re-run.
Everything goes to the output dir (KVEVAL_OUTPUT_DIR).
"""
import csv
import json
import os
import statistics as st
import subprocess
import sys
import time

from kvevaluator import layouts as L
from kvevaluator.measure.latency_raw_files import raw_files
from kvevaluator.output_tree import Tree, timing_in
from kvevaluator.paths import ROOT
from kvcost.cli import runs

REGIMES = ("steady", "cold", "warm")
SUMMARY_COLS = ["layout", "cell", "regime", "regime_dir", "query", "frequency", "mean_ns", "median_ns", "params", "iterations", "min_ns",
                "p90_ns", "max_ns", "machine"]
PAPER_RULE, NO_SUDO = "the paper's rule", "no sudo: page cache not dropped, machine not prepared"


def regime_dir(regime, steady_mb):
    """grid_latency's dir name for a regime at its default perf level (cell_latency.regime_flags): warm-p0, cold-p2, steady-<MB>mb-p2.
    Only that dir's walls are read (raw_walls), so a wall of another perf level (a p3 breakdown pass, an older cold-p3)
    never enters the number; summary.csv names the dir"""
    return {"warm": "warm-p0", "cold": "cold-p2", "steady": f"steady-{steady_mb}mb-p2"}[regime]


def iteration_walls(path):
    """the measured iterations' plan walls (ns) of one (query, param) raw file: warm.json / cold.json totals.wall_ns_all,
    steady.json each measured run's totals.wall_ns"""
    d = json.load(open(path))
    if isinstance(d, dict) and "measured" in d:
        return [float(r["totals"]["wall_ns"]) for r in d["measured"]]
    return [float(x) for x in (d.get("totals") or {}).get("wall_ns_all") or []]


def raw_walls(tree, sf, family, regime_name, cell):
    """{query: {param: [iteration walls]}} from grid_latency's raw files of one layout and regime: the cpplatency --out
    timings/<dataset>/<scale>/<family>/meta-<cell>/<regime dir>/, its arm meta-<cell> (latency_raw_files.py)"""
    walls = {}
    regime = regime_name.split("-")[0]          # warm-p0 -> warm.json, steady-64mb-p2 -> steady.json
    run_out = str(timing_in(tree.timings(family, sf), f"meta-{cell}", regime_name))
    if os.path.isdir(run_out):
        for q, by_idx in raw_files(run_out, f"meta-{cell}", regime).items():
            for idx, path in by_idx.items():
                walls.setdefault(q, {}).setdefault(idx, []).extend(iteration_walls(path))
    return walls


def aggregate(walls, frequency, regime):
    """the latency aggregation, applied last. steady: steady_table's rule: per (query, param) the mean
    over the measured passes from steady_table.FIRST_PASS (steady_table.counted_runs; pass 0 follows the store's own
    warm-up), per query steady_table.OVER_PARAMS (the mean) over params. warm and cold: per (query,
    param) the median over every measured iteration (repeats of one run: noise), per query the mean over params (params
    do different work, so a heavy anchor is real cost). The workload's total: the sum of frequency x that per-query value.
    -> (total ns, {query: (that value in ns, spread fields: the median over params beside it, n, min, p90, max)})"""
    from kvcost.latency.steady_table import OVER_PARAMS, counted_runs
    kept = counted_runs if regime == "steady" else (lambda v: v)
    per_param = st.fmean if regime == "steady" else st.median
    over_params = OVER_PARAMS if regime == "steady" else st.mean
    per_query = {}
    for q, by_param in sorted(walls.items()):
        values = [per_param(kept(v)) for _, v in sorted(by_param.items()) if v]
        meds = [st.median(kept(v)) for _, v in sorted(by_param.items()) if v]
        allv = sorted(x for v in by_param.values() for x in kept(v))
        per_query[q] = (over_params(values), {"median_ns": st.median(meds), "params": len(values), "iterations": len(allv),
                                              "min_ns": allv[0], "p90_ns": allv[min(len(allv) - 1, int(0.9 * len(allv)))],
                                              "max_ns": allv[-1]})
    total = sum(frequency[q] * m for q, (m, _) in per_query.items())
    return total, per_query


def write_summary(path, rows, keys):
    """upsert rows into summary.csv by (layout, regime): a layout timed again replaces its rows, the others are kept"""
    keep = []
    if os.path.exists(path):
        with open(path) as f:
            keep = [r for r in csv.DictReader(f) if (r["layout"], r["regime"]) not in keys]
    out = sorted(keep + rows, key=lambda r: (r["regime"], r["layout"], r["query"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLS)
        w.writeheader()
        w.writerows(out)


GRID_DIR = "inputs/specs/grid"     # the grid's spec set: its layouts need no --spec-dir
# the spec set a named uniform layout is timed from, the first that holds it: the grid's, grid_sized (the ChK-ChK layouts
# with sizes), then its size's set (the order the paper's latency runs group their stores by); a spec equal in each
# (kvevaluator.layouts.check_spec_table), so the set only groups the calls
TIMED_DIRS = ("grid", "grid_sized") + tuple(L.SIZE_DIRS.values())


def read_and_write(dataset, sf, workload):
    """(the workload, its one read family, its write families) of a paper run"""
    from kvcost.cli.search import load_workload
    from kvcost.layout_cost import benchmarks
    w = load_workload(dataset, sf, workload)
    fams = benchmarks.families_read(dataset, [w])
    reads = [f for f, kind in fams if kind == "read"]
    if len(reads) != 1:
        raise SystemExit(f"measure-latency: {w['id']} reads {reads}: one read family per run")
    return w, reads[0], [f for f, kind in fams if kind == "write"]


# the mixes a question hands to the paper's latency runs (the one rule): {question kind: [(spec kind, [(row file, model)], in the order
# tried)]}; a model None takes the file's one result (the local search's). A row is taken whether proven or not: J01's
# measured mix is its two-step row. A predicted question's rows were searched on the default tier's predicted set
# (mix_questions.count_source); no predicted set is named here
ANY = "mixes-of-Sp-Co-Ch1k-Ch4k-Ch16k-Ch64k-Ch256k"      # the any-size rows' file (results.stem("per-type-any-size"))
TIMED = {"measured": (("measured", ((f"{ANY}.json", "wops_bytes4k"),)),
                      ("bill", ((f"{ANY}.json", "bill_exact"), (f"{ANY}-local-search.json", None)))),
         "predicted": (("predicted", ((f"{ANY}.json", "wops_bytes4k"),)),)}


def _fewest(path, model):
    """the one result a timed spec is built from: the file's fewest `model` row with no byte limit, or (model None) the
    file; None when the file or the row is not there. The file may hold one such row per search (results.search_of,
    as the run-time table's mix rows read it): the proven row (branch and bound) is taken, else the two-step row; two
    rows of one search are refused. -> (result, what it is)"""
    from kvcost.layout_search.results import search_of
    if not os.path.isfile(path):
        return None
    d = json.load(open(path))
    if model is None:
        return d, "the local search's result"
    if not any(r.get("question") == "fewest" and r.get("model") == model for r in d["rows"]):
        return None
    row = _proven_else_two_step(path, model, [r for r in d["rows"] if r.get("question") == "fewest"
                                              and r.get("model") == model and r.get("limit") is None])
    return row, (f"its fewest {model} row ({search_of(row)} search, {'proven' if row['proven'] else 'NOT proven'}, "
                 f"{row['nodes']} nodes)")


def _proven_else_two_step(path, model, rows):
    """the row timed of a file's fewest `model` rows: one per search at most (proven, two-step), the proven one first"""
    from kvcost.layout_search.results import search_of
    by_search = {}
    for r in rows:
        by_search.setdefault(search_of(r), []).append(r)
    if not rows or any(len(v) != 1 for v in by_search.values()) or set(by_search) - {"proven", "two-step"}:
        raise SystemExit(f"{path}: fewest {model} rows with no limit by search "
                         f"{ {k: len(v) for k, v in sorted(by_search.items())} }: want one, or one proven and one two-step")
    return (by_search.get("proven") or by_search["two-step"])[0]


def timed_mixes(question, dataset, sf, src):
    """[(spec name, spec)] the mix question hands to the paper's latency runs (TIMED), from its rows in tree `src` at the dataset's
    scale: lat_<the question's workload id>_<kind>_mix_any_size; a mix whose row the tree lacks is skipped and named"""
    from kvcost.layout_search import mix_questions as MQ
    from kvcost.layout_search.export import row_spec
    q = MQ.get(question)
    if q["dataset"] != dataset or q.get("larger"):
        raise SystemExit(f"measure-latency: --question {question} searches {q['dataset']}"
                         f"{' at a larger scale' if q.get('larger') else ''}, not {dataset} at {sf}")
    rows = runs.searches_dir(dataset, sf, q["rows"], str(src.summary(dataset, sf)))
    out = []
    for kind, tries in TIMED["predicted" if "source" in q else "measured"]:
        name = f"lat_{MQ.workload_id(q)}_{kind}_mix_any_size"
        found = next(((os.path.join(rows, f), x) for f, m in tries if (x := _fewest(os.path.join(rows, f), m))), None)
        if found is None:
            print(f"[kvcost measure-latency] {name}: not in {runs.rel(rows)} ({', '.join(f for f, _ in tries)}): "
                  f"skipped", flush=True)
            continue
        path, (result, what) = found
        out.append((name, row_spec(result)[0]))
        print(f"[kvcost measure-latency] {name}: from {runs.rel(path)}, {what}", flush=True)
    return out


def spec_sets(a, family, layouts):
    """[(spec dir, [cell])]: the uniform layouts named, per the spec set each comes from (kvevaluator.layouts.resolve; the
    grid's first), then the mixes named, each spec written to summary/<dataset>/<scale>/latency-mixes/<family>/ (but on
    --dry-run): a mix question's timed mixes (timed_mixes), and mix spec files (files or dirs of them, mix:best,
    mix:<question>/<model>), each named by its content (mix_<sha>, as check-mix names it)"""
    import glob
    from kvcost.layout_search import mix_questions as MQ
    uniform = {}
    timed = []
    for x in layouts:
        if x.startswith("mix:") and x[len("mix:"):] in MQ.questions():
            timed += timed_mixes(x[len("mix:"):], a.dataset, a.sf, Tree(a.from_dir))
            continue
        if x.startswith("mix:"):
            x = runs.mix_paths(a, [x])[0]
        if not x.endswith(".json") and not os.path.isdir(x):
            cell = L.cell_of(x)
            d = next((f"inputs/specs/{s}" for s in TIMED_DIRS if os.path.exists(os.path.join(L.SPECS, s, f"{cell}.json"))),
                     None)
            lay = L.resolve(x) if d is None else None           # a name no spec set holds: its generated spec
            d = d or os.path.relpath(lay.spec_dir, str(ROOT))
            uniform.setdefault(d, [])
            if cell not in uniform[d]:
                uniform[d].append(cell)
            continue
        found = sorted(glob.glob(os.path.join(x, "*.json"))) if os.path.isdir(x) else [x] if os.path.isfile(x) else []
        if not found:
            raise SystemExit(f"measure-latency: {x}: not a layout, mix:<question> ({', '.join(MQ.questions())}), a "
                             f"spec file or a dir of them")
        for f in found:                   # named by its content, as check-mix names it: one name, one spec
            name, full = runs.built_mix(f)
            if name not in {n for n, _ in timed}:
                timed.append((name, full))
    sets = sorted(uniform.items(), key=lambda kv: kv[0] != GRID_DIR)       # the grid's set first, then as named
    if timed:
        d = str(Tree().summary(a.dataset, a.sf, "latency-mixes", family))
        if not a.dry_run:
            os.makedirs(d, exist_ok=True)
            for name, spec in timed:
                with open(os.path.join(d, f"{name}.json"), "w") as f:
                    f.write(json.dumps(spec, indent=2, sort_keys=True) + "\n")
        print(f"[kvcost measure-latency] {len(timed)} timed mixes' specs {'to write' if a.dry_run else 'written'} "
              f"in {runs.rel(d)}", flush=True)
        sets.append((d, sorted(name for name, _ in timed)))      # by name, as a dir of them is read
    elif any(x.startswith("mix:") and x[len("mix:"):] in MQ.questions() for x in layouts):
        print("[kvcost measure-latency] the mix questions' rows hold no timed mix here: the 12 layouts are timed alone",
              flush=True)
    return sets


def steady_setting(a, dataset, sf):
    """(MB,PASSES, the KV-timer passes): --cache-mb, else store_config's cache; --passes, else 5 at the paper's scale or
    SF >= 3, 3 below; the KV-timer passes --kv-timer-passes, else 0, or as the passes with --rocksdb-share (its only
    reader). By default 5 timed passes and no KV-timer passes: 5 passes rank the layouts as 10 do, and only
    --rocksdb-share reads the KV-timer passes"""
    from kvevaluator import sizes
    from kvevaluator.measure.store_config import steady_cache_mib
    paper = sizes.scale(sizes.resolve(sizes.PAPER), dataset)
    passes = a.passes or (5 if sf == paper or (sf.replace(".", "", 1).isdigit() and float(sf) >= 3) else 3)
    mb = a.cache_mb or steady_cache_mib(sf, dataset=dataset)
    kvt = a.kv_timer_passes if a.kv_timer_passes is not None else passes if getattr(a, "rocksdb_share", False) else 0
    return f"{mb},{passes}", str(kvt)


def have_sudo():
    """passwordless sudo, which the paper's rule needs (the page cache dropped, the machine prepared); a test's
    MACHINE_TEST_DIR stands in for it"""
    if os.environ.get("MACHINE_TEST_DIR"):
        return True
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def criterion():
    """the pick rule the timing picks by and --save-picks writes under: the optimizer's default (grid_latency's, with
    no --pick-by)"""
    from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, resolve_criterion
    return resolve_criterion(DEFAULT_CRITERION)[0]


def covered(a, fam, cells, say=True):
    """(the cells timed from the picks file, the cells counted first): every uniform cell the file covers (grid_latency.
    file_picks, the rule the timing applies) is timed from it, unless --validate-count or --save-picks counts them all;
    say: one line per reason a cell is counted"""
    from kvcost.latency.grid_latency import file_picks
    path = str(picks_file(a, fam))
    crit = criterion()
    if a.validate_count or a.save_picks:
        return [], list(cells)
    out, why = [], {}
    for c in cells:
        picks, w = file_picks(path, c, fam, crit)
        (out.append(c) if picks is not None else why.setdefault(w, []).append(L.name_of(c)))
    for w, names in why.items() if say else ():
        print(f"[kvcost measure-latency] counted first, as their pick needs ({w}): {', '.join(names)}", flush=True)
    return out, [c for c in cells if c not in out]


def picks_file(a, fam):
    """the picks file of this run's dataset, scale and read family (kvevaluator.datasets.latency_picks_in)"""
    from kvevaluator.datasets import latency_picks_in
    return latency_picks_in(a.dataset, a.sf, fam)


def paper_steps(a, sudo=True):
    """[(name, argv)] of the latency runs of one workload (one read family, and its write family when it has one) on the
    uniform layouts and the mix spec files named: the counts (kvcost measure on the diagonals the uniform layouts name
    or compose from, into captures/; kvcost compose for the others, into composed/; kvcost measure on a mix's spec),
    only for the layouts the picks file does not cover (covered), all of them under --validate-count, and only these
    under --save-picks), the stores no measure builds that a timing will be made on (kvcost build-store: stores_to_build), then per spec set
    its steady timing at store_config's cache (run/latency-cells.sh: NUMA bound, page cache dropped, the counts gate;
    into timings/<dataset>/<scale>/<family>/ and summary/<dataset>/<scale>/grid-latency/<family>/), --drift-control
    LAYOUT (that store timed again at the end, beside its first timing), --rocksdb-share (KV-timer passes after the
    timed ones, then kvcost.latency.steady_share at --rocksdb-share-weights) and, when the workload writes, its write
    family after each store's steady warm-up (run-w16-mix-latency.sh --stage writes). sudo False: latency-cells.sh --no-sudo.
    -> (steps, sets, the read family, the workload)"""
    from kvevaluator import workloads
    ds, sf = a.dataset, a.sf
    w, fam, write = read_and_write(ds, sf, a.workload)
    sets = spec_sets(a, fam, a.layouts)
    steady, kvt = steady_setting(a, ds, sf)
    from kvcost.cli import workloads as W
    pset = getattr(a, "param_set", None)
    counts_n = W.nparam_of(W.of(ds, [fam] + list(write)), pset)    # the dataset's own (JOB: 1), as measure takes it
    nparam = min(counts_n, len(workloads.params_module(fam).query_params(sf, workloads.query_list(fam)[0], count=counts_n)))
    py, k = sys.executable, [sys.executable, "-m", "kvcost.cli.cli"]
    timed_q = ",".join(dict.fromkeys(q for f, q, *_ in w["items"] if f == fam))   # the workload's reads, in its order
    slice_q = own_queries(fam, timed_q.split(","))
    jobs = ["--jobs", str(a.jobs)] if getattr(a, "jobs", None) else []
    measure = lambda layouts: k + ["measure", "--dataset", f"{ds}={sf}", "--workload", a.workload, *layouts, "--nparam",
                                   str(counts_n), *(["--queries", timed_q] if slice_q else []), *jobs]
    from_file, counted = covered(a, fam, [c for _, cells in sets for c in cells if L.is_cell(c)])
    diagonals, composed = counted_and_composed(counted) if not (a.save_picks and a.from_dir) else ([], [])
    names = lambda cells: [L.name_of(c) for c in cells]
    steps = []
    if diagonals:
        steps.append((f"{fam} counts", measure(names(diagonals))))
    if composed:
        steps.append((f"{fam} compose", k + ["compose", "--dataset", f"{ds}={sf}", *names(composed), *jobs]))
    for d, cells in sets:
        mixes = [c for c in cells if not L.is_cell(c)]
        if mixes and not a.save_picks:
            steps.append((f"{fam} counts {os.path.basename(d)}", measure([os.path.join(d, f"{c}.json") for c in mixes])))
    if a.save_picks:                     # the counts only: the file is written from them, nothing is timed
        return steps, sets, fam, w
    picks = ["--picks", str(picks_file(a, fam))] if picks_file(a, fam).exists() else []
    build = stores_to_build(a, fam, composed + from_file, steady.split(",")[0], bool(write),
                            int(kvt) if a.regime == "steady" else 0)
    if build:
        steps.append((f"{fam} stores", k + ["build-store", "--dataset", f"{ds}={sf}", *names(build)]))
    for d, cells in sets:
        spec = [] if d == GRID_DIR else ["--spec-dir", d]
        steps.append((f"{fam} {a.regime} {os.path.basename(d)}", ["run/latency-cells.sh", "--sf", sf, "--family", fam,
                      "--nparam", str(nparam), *spec, *picks, "--cells", ",".join(cells), "--queries", timed_q, "--regimes", a.regime,
                      "--steady", steady, "--kvtimer-passes", kvt if a.regime == "steady" else "0",
                      *([] if sudo else ["--no-sudo"]), *(["--fresh"] if a.fresh else [])]))
    if a.drift_control:    # timed again after the others, beside its first timing (its regime dir + -control)
        steps.append((f"{fam} {a.regime} drift control", ["run/latency-cells.sh", "--sf", sf, "--family", fam, "--nparam",
                      str(nparam), *picks, "--cells", L.cell_of(a.drift_control), "--queries", timed_q, "--regimes", a.regime,
                      "--steady", steady, "--kvtimer-passes", "0", "--suffix", "-control",
                      *([] if sudo else ["--no-sudo"])]))
    if a.rocksdb_share:
        weights = a.rocksdb_share_weights or f"frequencies:{ds}/{sf}/{w['id']}"
        steps.append((f"{fam} share", [py, "-m", "kvcost.latency.steady_share", "--timings", str(Tree().timings(fam, sf)),
                      "--weights", weights, "--out", str(Tree().summary(ds, sf, "grid-latency", fam) / "rocksdb_share.csv")]))
    if write:              # the workload writes: its write family timed too
        if len(write) != 1:
            raise SystemExit(f"measure-latency: {w['id']} writes {write}: one write family")
        steps.append((write[0], ["experiments/layout_choice/run-w16-mix-latency.sh", "--sf", sf, "--stage", write[0],   # its stage is named for the family
                      "--cells", ",".join(c for _, cells in sets for c in cells), "--no-repeats", "--timings-root",
                      str(Tree().root), *[x for d, _ in sets for x in ("--write-spec-dir", d)], "--steady", steady,
                      "--out", str(Tree().summary(ds, sf, "i01-mix-latency"))]))
    return steps, sets, fam, w


def counted_and_composed(cells):
    """(the diagonals to measure, the layouts to compose) of the uniform layouts named: a diagonal (both parts one
    setting) is measured itself; any other layout is composed from its two parts' diagonals (compose_cells), which are
    measured for it; each in the order first named"""
    diagonals, composed = [], []
    for c in cells:
        p, ad = L.cell_settings(c)
        for x in ([c] if p == ad else [L.cell_id(p, p), L.cell_id(ad, ad)]):
            if x not in diagonals:
                diagonals.append(x)
        if p != ad and c not in composed:
            composed.append(c)
    return diagonals, composed


def stores_to_build(a, fam, composed, steady_mb, writes, kv_timer_passes=0):
    """the layouts no measure of this run builds (composed, or timed from the picks file) whose store this run builds
    (kvcost build-store; a measured layout's store is built by its measure): the ones a timing will be made on, so --fresh or a write family (its timing runs on every store) takes
    them all, else those whose timing of the regime the run would not keep as it is (cell_latency.already_measured's
    rule, read here without moving anything: no capture on disk, a capture under another protocol, or a capture
    without the KV-timer capture this run makes beside it); the drift control's layout: its -control timing too"""
    from kvevaluator.measure import cell_latency as T
    if a.fresh or writes:
        return list(composed)
    name = regime_dir(a.regime, steady_mb)
    timings = Tree().timings(fam, a.sf)
    timers = {"kvtimer": kv_timer_passes} if kv_timer_passes else {}

    def kept(c, suffix="", timers=timers):
        at = lambda regime: str(timing_in(timings, f"meta-{c}", regime))
        regimes = [name + suffix] + [r + suffix for r in T.timer_regimes(name, timers)]
        return (all(os.path.exists(os.path.join(at(r), "steps.csv")) for r in regimes)
                and T.protocol_mismatch([(at(r), r) for r in regimes]) is None)
    control = L.cell_of(a.drift_control) if a.drift_control else None
    return [c for c in composed if not kept(c) or (c == control and not kept(c, "-control", {}))]


def own_queries(fam, queries):
    """the queries of `queries` outside the family's measured list (workloads.query_list: a query someone added, such as
    the example workload's), [] when none. kvcost measure counts a family's own list, so these are counted as a slice
    of the workload's queries (measure --queries), which goes to a tree of its own: refused in the default output dir,
    naming the command to run"""
    from kvevaluator import workloads
    from kvcost.cli.verbs import in_default_tree
    out = [q for q in queries if q not in workloads.query_list(fam)]
    if out and in_default_tree():
        raise SystemExit(f"measure-latency: {', '.join(out)} are not among {fam}'s measured queries "
                         f"(kvevaluator.workloads.query_list), so kvcost measure counts them only as a slice of this "
                         f"workload's queries, which goes to a tree of its own: run it as KVEVAL_OUTPUT_DIR=<another dir> "
                         f"kvcost measure-latency ... (the stores are built there)")
    return out


def counts_picks(fam, sf, cells, tree):
    """{cell: {query: body}}: grid_latency's pick (the pick record's rule, PlanRule()) on the counts capture of tree
    (each layout's own: measured itself, else composed), for the cells it holds counts of"""
    from kvcost.latency.grid_latency import picks_of
    cap = tree.capture(fam, sf)
    return {c: picks_of(cap.of_layout(c).path, c, fam) for c in cells
            if os.path.exists(cap.of_layout(c).path_of("counts", c))}


def paper_picks(a, fam, sets):
    """the body each store times per query: the picks file's for a layout it covers (covered), else grid_latency's pick
    on the counts capture latency-cells.sh reads; one line per (store, query)"""
    from_file, _ = covered(a, fam, [c for _, cells in sets for c in cells if L.is_cell(c)], say=False)
    filed = json.load(open(picks_file(a, fam)))["cells"] if from_file else {}
    rest = [c for _, cells in sets for c in cells if c not in from_file]
    found = counts_picks(fam, a.sf, rest, Tree())
    n = 0
    for _, cells in sets:
        for c in cells:
            if c not in filed and c not in found:
                print(f"  {c}: no counts in {Tree().capture(fam, a.sf).path} yet (the counts steps measure or compose it)")
                continue
            for q, v in sorted((filed.get(c) or found[c]).items()):
                print(f"  {c} {q} {v}")
                n += 1
    print(f"[kvcost measure-latency] {n} (store, query) timed bodies: {len(from_file)} stores from "
          f"{runs.rel(picks_file(a, fam))}, the others from the counts")


def save_picks(a, fam, sets):
    """write the named uniform layouts' picks on the counts (counts_picks: the output dir's, or --from's) into the picks
    file, the other cells it holds kept; a layout with no counts is refused"""
    tree = Tree(a.from_dir) if a.from_dir else Tree()
    cells = [c for _, cs in sets for c in cs if L.is_cell(c)]
    found = counts_picks(fam, a.sf, cells, tree)
    if sorted(found) != sorted(cells):
        raise SystemExit(f"measure-latency --save-picks: no counts of {', '.join(sorted(set(cells) - set(found)))} in "
                         f"{tree.capture(fam, a.sf).path}")
    path = picks_file(a, fam)
    d = json.load(open(path)) if path.exists() else {"cells": {}}
    d.update(criterion=criterion(),
             counts_capture=os.path.relpath(tree.capture(fam, a.sf).path, str(tree.root)))
    d["cells"].update(found)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(d, indent=1, sort_keys=True) + "\n")
    print(f"[kvcost measure-latency] {len(found)} stores' picks ({sum(len(v) for v in found.values())} (store, query)) "
          f"from {runs.rel(tree.capture(fam, a.sf).path)} into {runs.rel(path)}; git diff shows what moved", flush=True)


def timing_marks(tree, dataset, sf, fam, cell, regime_name):
    """what grid_latency's record of a layout's timing says of the regime (summary/<dataset>/<scale>/grid-latency/
    <family>/cells/<cell>.json), for the answer: (the queries whose timed counts differ from the capture's: the counts
    gate, the timing kept and marked; its read_set_fits: the store's whole read set fits the cache, None when not;
    whether the counts' pick differs from the picks file's, body_moved)"""
    path = tree.summary(dataset, sf, "grid-latency", fam) / "cells" / f"{cell}.json"
    if not path.exists():
        return [], None, False
    rec = json.load(open(path))
    return (rec.get("timed_counts_differ", {}).get(regime_name, []), rec.get("read_set_fits", {}).get(regime_name),
            any(n["kind"] == "body_moved" for n in rec.get("stale", [])))


def cmd_measure_latency(a, argv):
    """time the workload's queries on each layout, counting first what the picks file does not cover (paper_steps),
    or with --save-picks count and write the file only; then the workload's latency per layout from
    the raw walls of the regime's dir (aggregate) into workload-latency/<workload id>/summary.csv"""
    runs.one_pair(a, "kvcost measure-latency")
    run = runs.load(a)
    a.dataset, a.sf = run.dataset, run.scale
    a.workload = run.path
    runs.say_run("measure-latency", run)
    from kvcost.cli.verbs import require_plan_files
    require_plan_files(run, "measure-latency")
    if not a.layouts:
        raise SystemExit("measure-latency: name the layouts: names (Co-Ch64k), mix spec files, mix:best, "
                         "mix:<question>/<model> or mix:<question>")
    sudo = a.dry_run or a.save_picks or have_sudo()
    machine = PAPER_RULE if sudo else NO_SUDO
    if not sudo:
        print("[kvcost measure-latency] NO PASSWORDLESS SUDO: the page cache is not dropped between stores and the "
              "machine is not prepared (NUMA balancing, swappiness); the times are not under the paper's rule, and "
              "summary.csv says so", flush=True)
    steps, sets, fam, w = paper_steps(a, sudo)
    t0 = time.time()
    for name, cmd in steps:
        print(f"[kvcost measure-latency] {name}: {' '.join(cmd)}", flush=True)
        if a.dry_run:
            continue
        s = time.time()
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=not a.verbose, text=True)
        if p.returncode != 0:
            if not a.verbose:
                print((p.stdout or "")[-3000:] + (p.stderr or "")[-3000:])
            raise SystemExit(f"measure-latency: {name} FAILED (-v prints every line)")
        print(f"[kvcost measure-latency] {name} done in {time.time() - s:.0f} s", flush=True)
    if a.save_picks and not a.dry_run:
        save_picks(a, fam, sets)
        runs.answer("measure-latency", f"--save-picks: {runs.rel(picks_file(a, fam))} written, nothing timed")
        return 0
    if a.dry_run:                        # and the body each store times per query, from the file or the counts there
        if not a.save_picks:
            paper_picks(a, fam, sets)
        runs.answer("measure-latency", f"the commands, none run (--dry-run): {len(steps)} steps")
        return 0
    frequency = {}
    for f, q, often, _ in w["items"]:
        if f == fam:
            frequency[q] = frequency.get(q, 0.0) + float(often)
    tree = Tree()
    regime_name = regime_dir(a.regime, steady_setting(a, a.dataset, a.sf)[0].split(",")[0])
    totals, rows, marked, fits, moved = {}, [], {}, [], []
    for d, cells in sets:
        for cell in cells:
            shown = L.name_of(cell) if L.is_cell(cell) else f"mix {cell}"
            marks, fit, pick_moved = timing_marks(tree, a.dataset, a.sf, fam, cell, regime_name)
            moved += [shown] if pick_moved else []
            if marks:
                marked[shown] = marks
            fits += [shown] if fit else []
            walls = raw_walls(tree, a.sf, fam, regime_name, cell)
            missing = sorted(set(frequency) - set(walls))
            if missing:
                raise SystemExit(f"measure-latency: {shown}: no raw timing for {', '.join(missing)} under "
                                 f"{runs.rel(timing_in(tree.timings(fam, a.sf), f'meta-{cell}', regime_name))}")
            totals[shown], per_query = aggregate({q: walls[q] for q in frequency}, frequency, a.regime)
            for q, (mean, spread) in per_query.items():
                rows.append({"layout": shown, "cell": cell, "regime": a.regime, "regime_dir": regime_name, "query": q,
                             "frequency": frequency[q], "mean_ns": round(mean),
                             **{k: round(v) if k.endswith("_ns") else v for k, v in spread.items()}, "machine": machine})
    path = os.path.join(str(tree.summary(a.dataset, a.sf, runs.LATENCY, run.name)), "summary.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_summary(path, rows, {(s_, a.regime) for s_ in totals})
    each = ("mean over the measured passes from the second" if a.regime == "steady"
            else "median over the measured runs")
    print(f"[kvcost measure-latency] {a.regime} ({regime_name}), per run of the workload's {fam} queries (per query: "
          f"the mean over params of each param's {each}; summed at the workload's frequencies), {machine}, "
          f"{time.time() - t0:.0f} s:")
    for shown, tot in sorted(totals.items(), key=lambda kv: kv[1]):
        print(f"  {shown:<24} {tot / 1e6:10.3f} ms")
    for shown, queries in marked.items():
        print(f"  MARKED {shown} (timed_counts_differ; its timing is kept): the timed counts differ from the counts "
              f"capture's on {', '.join(queries)}")
    if fits:
        print(f"  read set fits the cache (steady = warm on these stores): {', '.join(fits)}")
    if moved:
        print(f"  MARKED body_moved {', '.join(moved)}: the counts' pick differs from {runs.rel(picks_file(a, fam))}, "
              f"whose bodies were timed; to time the counts' pick: kvcost measure-latency {' '.join(argv)} --save-picks, "
              f"then this command again")
    print(f"  per query: {runs.rel(path)}")
    runs.answer("measure-latency", f"{a.regime}, {a.dataset} {a.sf}" + ("" if sudo else f" ({NO_SUDO})") +
                f": {ratio_text(totals)}" + (f"; MARKED timed_counts_differ: {', '.join(marked)}" if marked else "")
                + (f"; read set fits the cache: {', '.join(fits)}" if fits else "")
                + (f"; MARKED body_moved: {', '.join(moved)}" if moved else ""))
    return 0


def ratio_text(totals):
    """the answer in words, whatever the order of the arguments: each mix over the fastest uniform layout given
    ('the mix wops_bytes4k takes 0.97x Co-Ch64k's time'); with no mix or no uniform layout, each layout over the first's"""
    ms = lambda n: f"{totals[n] / 1e6:.3g} ms"
    mixes = [n for n in totals if n.startswith("mix ")]
    uniform = [n for n in totals if not n.startswith("mix ")]
    parts = [f"{n} {ms(n)}" for n in totals]
    if mixes and uniform:
        ref = min(uniform, key=totals.get)
        said = [f"the {m} takes {totals[m] / totals[ref]:.2f}x {ref}'s time" for m in mixes if totals[ref]]
    else:
        ref = next(iter(totals))
        said = [f"{n} takes {totals[n] / totals[ref]:.2f}x {ref}'s time" for n in totals if n != ref and totals[ref]]
    return ", ".join(parts) + " per run of the workload" + ("; " + "; ".join(said) if said else "")


def add_parsers(sub, common):
    x = sub.add_parser("measure-latency", help="time the workload's queries on each layout's store, under the paper's "
                                               "rule, and the workload's latency per layout",
                       description=__doc__.split("\n\n")[0] + " Regimes: steady (the default and the paper's: a "
                                   "bounded block cache, --cache-mb, over --passes passes; the number is the mean over "
                                   "the passes from the second), cold (direct I/O, nothing cached; the median over the "
                                   "runs) or warm (the store's blocks cached: 1 warm-up run, then 3 measured runs per "
                                   "param; the median over the runs). Per query the mean over params; the workload's "
                                   "total sums them at the workload's frequencies. To time the counts' pick after a "
                                   "plan change: the same command with --save-picks, then without it (git diff "
                                   "inputs/latency-picks/ shows what moved).")
    common(x)
    x.add_argument("--workload", required=True, type=runs.workload_arg, help="a workload file, or the id or name of one "
                   "(a workload with a write family also times its writes)")
    x.add_argument("layouts", nargs="*", metavar="LAYOUT", type=runs.layout_arg(dirs=True),
                   help="layouts at any chunk size (Co-Ch64k, Ch4k-Ch16k), mix spec files or dirs of them, mix:best, "
                        "mix:<question>/<model>[-<limit>], or mix:<question> (its timed mixes: mix:i02-any-size, "
                        "mix:i02-predicted). The paper's figure: Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Co-Sp Ch1k-Ch1k Ch16k-Ch16k "
                        "Ch4k-Ch4k Ch256k-Ch256k Ch1k-Ch4k Ch1k-Ch16k Ch4k-Ch16k, then its questions' mixes")
    x.add_argument("--regime", default="steady", choices=REGIMES, help="steady (default, the paper's rule), cold "
                   "(direct I/O, nothing cached) or warm (the store's blocks cached: 1 warm-up run, 3 measured runs per "
                   "param)")
    x.add_argument("--cache-mb", type=int, default=None, help="steady: the block cache bound in MB (default "
                   "inputs/contract/store_config.json's for the dataset and scale)")
    x.add_argument("--passes", type=int, default=None, help="steady: the passes over the queries (default 5 at the "
                   "paper's scale and SF >= 3, 3 below)")
    x.add_argument("--nparam", type=int, default=5, help="the reported params per query (default 5)")
    x.add_argument("--rocksdb-share", action="store_true", help="the share of each store's time spent in RocksDB calls "
                   "(kvcost.latency.steady_share, from KV-timer passes run after the timed ones, as many as --passes "
                   "unless --kv-timer-passes)")
    x.add_argument("--rocksdb-share-weights", default=None, help="--rocksdb-share's query weights (steady_share "
                   "--weights; default frequencies:<dataset>/<scale>/<workload id>)")
    x.add_argument("--drift-control", default=None, metavar="LAYOUT", help="time this layout's store again after the "
                   "others, beside its first timing (<regime dir>-control)")
    x.add_argument("--fresh", action="store_true", help="time again a (layout, regime) already on disk")
    x.add_argument("--validate-count", action="store_true", help="count every layout first (kvcost measure, compose), "
                   "also those the picks file covers, so the counts gate compares every timed run (default: count only "
                   "what the file does not cover; the gate runs where counts are already in the output dir)")
    x.add_argument("--save-picks", action="store_true", help="count the named uniform layouts (or read the counts in "
                   "--from TREE), write their picks into inputs/latency-picks/<dataset>/<scale>/<family>.json, and time "
                   "nothing")
    x.add_argument("--jobs", type=int, default=None, help="stores counted at once (kvcost measure's --jobs and its "
                                                          "default); the timing is always one store at a time")
    x.add_argument("-v", "--verbose", action="store_true", help="print every step's every line")
    x.add_argument("--dry-run", action="store_true", help="print the steps and the body each store times per query "
                                                          "(the timed mixes' specs are named, not written); run none")
    x.add_argument("--from", dest="from_dir", default=None, metavar="TREE", help="the tree the mix questions' rows are "
                                                                 "read from, and --save-picks's counts (default the "
                                                                 "output dir; paper-results)")
    e = x.add_argument_group("expert")
    e.add_argument("--kv-timer-passes", type=int, default=None, help="steady: the passes with the KV timer after the "
                   "measured ones (default 0, or as the passes with --rocksdb-share, which reads them)")
    x.set_defaults(fn=cmd_measure_latency)
