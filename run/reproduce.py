#!/usr/bin/env python3
"""reproduce.py: one paper item in two commands, `just results` then `just figure` (README.md, "Reproduce the paper"). An item is
every figure, table and number key the paper prints from a derive; it is named by its paper label (fig:uniform-49 ->
uniform-49), never by its number, which moves as the paper changes. FIGURES below is the one table of them. An item with
no measuring chain (`results`: None) is archive-only: `just figure <item>` makes it from paper-results/ alone. An
`optional` item (the field says why: the paper does not print it, or it is a check a reader runs on its own) keeps its
`just results` and `just figure`; the list a reviewer runs (run/reproduce-all.sh, the README's) leaves it out and names
it in one line.

    just results                               list the figures, their flags and the paper's settings
    just results uniform-49                    measure and analyse at the paper's settings (I01 at SF3: hours)
    just results uniform-49 --dataset ldbc=0.1 the same steps with LDBC at sf0.1 (minutes)
    just results uniform-49-workloads --dataset ldbc=0.1,ldbc=0.3,imdb=sample1pct
                                               LDBC at 0.1 and, for the side-by-side rows, 0.3; JOB at sample1pct
    just results uniform-49 --workload j01     the same figure for another workload (its dataset: the workload's)
    just figure uniform-49 --dataset ldbc=0.1  draw it from that run -> output/figures/uniform-49/i01_ldbc-0.1/
    just figure uniform-49 --from paper-results      draw it from paper-results/ (the paper's numbers)
    just results uniform-49 --dry-run          print the chain's commands, run none
    just results uniform-49 --jobs 2           at most 2 stores (or other units) at once: a machine short of memory
    just figure composed-vs-measured           an archive-only item: its derive on paper-results/ alone
    just derive-all                            every derive on paper-results/: ran and failed counts (tables and keys
                                               into output/paper/build/, where gs-paper's check reads them)

Every command is printed before it runs, with its time after: the log is the walkthrough.
"""
import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time

from kvcost.cli.predict import default_set
from kvcost.layout_cost import benchmarks
from kvcost.layout_search import mix_questions
from kvcost.predict_accuracy import uniform_49_tallies as U49
from kvengines import complex_kv_reads as CKR
from kvengines import live_workloads as LW
from kvevaluator import datasets, sizes, workloads
from kvevaluator.measure import run_measure
from kvevaluator.measure import store_config
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT
from kvevaluator.predict.tier1 import DEFAULT_TIER

PY = [sys.executable]


def on(dataset, run, make):
    """the steps on a dataset at its scale in the run (make(scale) -> steps; sizes.scale), or, when the run leaves the
    dataset out, a printed line: the figure leaves it out"""
    sc = sizes.scale(run, dataset)
    if sc:
        return make(sc)
    return [["echo", f"{dataset}: not in this run (--dataset {sizes.text(run)}); the figure leaves it out"]]


KV = PY + ["-m", "kvcost.cli.cli"]
# the layouts the chains name, written out on each command line: the seven measured (the three diagonals and ChK-ChK at
# every other chunk size; every other layout composes from them), the 49 uniform layouts (every pair of the seven's
# settings), and the latency figure's 12 (timed in this order, grouped by spec set: kvcost.cli.latency.TIMED_DIRS)
SEVEN = [f"{s}-{s}" for s in (U49.SHORT[x] for x in U49.SETTINGS)]
UNIFORM_49 = U49.NAMES
LATENCY_12 = ["Sp-Sp", "Co-Co", "Ch64k-Ch64k", "Sp-Co", "Co-Sp", "Ch1k-Ch1k", "Ch16k-Ch16k", "Ch4k-Ch4k", "Ch256k-Ch256k",
              "Ch1k-Ch4k", "Ch1k-Ch16k", "Ch4k-Ch16k"]


JOBS = None   # --jobs: how many stores (or other units) each command runs at once; None: each command decides
VALIDATE = False   # --validate-count: each kvcost measure-latency step counts its layouts first and compares


def own_jobs():
    """--jobs N for a command that runs work side by side (measure and measure-latency: stores; compose, predict,
    search-mix: processes): the chain's --jobs when given (it lowers the command's own number), else nothing: each
    command decides by the one rule (run_measure.at_once: the cores, capped by memory where a peak is known)"""
    return ["--jobs", str(JOBS)] if JOBS else []


def measure(workload, run, at=None):
    """a workload's measured basis on the seven layouts (SEVEN; kvcost measure: every query set it reads, held-out where its
    set picks on held-out params, its writes, the grid check), then the dataset's composed layouts (kvcost compose); at
    its dataset's scale in the run, or at `at`"""
    def make(ds, sc):
        return [KV + ["measure", "--dataset", f"{ds}={sc}", "--workload", workload, *SEVEN, *own_jobs()],
                KV + ["compose", "--dataset", f"{ds}={sc}", *own_jobs()]]
    dataset = benchmarks.dataset_in_run(workload, run if at is None else {"ldbc": [at]})
    if dataset is None:
        return [["echo", f"{workload}: its dataset is not in this run (--dataset {sizes.text(run)}); the figure leaves it out"]]
    return make(dataset, at or sizes.scale(run, dataset))


def cost_49(workload, run, *flags, at=None):
    """the workload's cost on each of the 49 uniform layouts (kvcost cost-layouts, the 49 named: every cost model, both
    chunk views, into layout-costs/<workload>/measured/), at its dataset's scale in the run, or at LDBC scale `at`"""
    dataset = benchmarks.dataset_in_run(workload, run if at is None else {"ldbc": [at]})
    if dataset is None:
        return [["echo", f"{workload}: its dataset is not in this run (--dataset {sizes.text(run)}); left out"]]
    return [KV + ["cost-layouts", "--dataset", f"{dataset}={at or sizes.scale(run, dataset)}", "--workload", workload,
                  *UNIFORM_49, *flags]]


def uniform_49_results(mix, run):
    """measure the workload on the seven (composed into the rest), then cost the 49"""
    return measure(mix, run) + cost_49(mix, run)


def derive(script, *flags):
    """a derive step: paper/derive/<script> on the tree --from names, at the run's scales, its tables into --tables"""
    return lambda source, mix, run, tables: [PY + [f"paper/derive/{script}", "--from", source, "--dataset", sizes.text(run),
                                                   "--tables", str(tables), *[f.format(mix=mix) for f in flags]]]


def archive(script):
    """an archive-only item's derive: paper/derive/<script>, which reads paper-results/ alone, its tables and number
    keys into --tables"""
    return lambda source, mix, run, tables: [PY + [f"paper/derive/{script}", "--tables", str(tables)]]


def ldbc(run):
    """the run's LDBC scale: what the LDBC-only drivers take"""
    return sizes.scale(run, "ldbc")


STUDY_SCRIPTS = (   # the figure-specific analyses a chain runs as they are (no kvcost command does them)
    "experiments/random_spread/random_spread.py",              # the random-workload triangle
    "experiments/logical_census/run-logical-census.sh",        # the elements LDBC's plans touch, the same on every layout
    "experiments/layout_choice/run-layout-choice.sh",          # each LDBC workload's options and parts, the budget picks (parts-w11)
    # the live databases (complex-kv-reads): the data, the engines' own setup, start and stop, and the tools that
    # count and time them
    "kvevaluator.fetch_dataset", "engines/once.sh", "engines/start-server.sh", "engines/stop-server.sh",
    "experiments/engine_fidelity/measure.py", "experiments/engine_fidelity/time_queries.py",
    "experiments/engine_fidelity/compare_complex_kv_reads.py",
    "experiments/engine_fidelity/validate.py",                 # engine-fidelity: the evaluator against the paper's live capture
    "experiments/engine_fidelity/write_fidelity.py",           # engine-fidelity: the formats' write counts against the live sweep
    "experiments/predict_statistics/sampled.py",               # predict-49, runtime: the tallies counted on a sample of the owners
    "run/predict-gate.sh",                                     # predict-formulas: the stores (--stores; its sf0.1 run is a gate)
    "run/predict-accuracy.sh",                                 # predict-formulas: the traced pass, every level per KV step
    "kvcost.predict_accuracy.predict_formulas",                # predict-formulas: the formula and statistics errors
)


def questions(run, *names):
    """kvcost search-mix --question per name (experiments/mix_benefit/mix_questions.json): a question on a dataset the
    run leaves out, or a larger-scale one with no larger LDBC scale in the run, is a printed line"""
    out = []
    for n in names:
        q = mix_questions.get(n)
        if q.get("larger") and not sizes.larger(run, "ldbc"):
            out.append(["echo", f"{n}: no larger LDBC scale in this run (--dataset {sizes.text(run)}); left out"])
        elif not sizes.scale(run, q["dataset"]):
            out.append(["echo", f"{n}: {q['dataset']} is not in this run (--dataset {sizes.text(run)}); left out"])
        else:
            out.append(KV + ["search-mix", "--question", n, "--dataset", sizes.text(run), *own_jobs()])
    return out


def ldbc_study(run, make):
    """a study line on LDBC at its scale in the run"""
    return on("ldbc", run, make)


def triangle_results(mix, run):
    """I01 on the seven and its 49 (with the per-query costs), then the random workloads over them"""
    return uniform_49_results("i01", run) + ldbc_study(run, lambda sc: [
        PY + ["experiments/random_spread/random_spread.py", "--dataset", f"ldbc={sc}"]])


def pair(workload, run):
    """the workload's dataset=scale in the run, or None: the run leaves its dataset out"""
    ds = benchmarks.dataset_in_run(workload, run)
    return f"{ds}={sizes.scale(run, ds)}" if ds else None


def sampled_tallies(run, sample, workloads_=("i01", "b01", "j01")):
    """per dataset the workloads read in the run: the seven diagonal layouts tallied on a sample of the owners and the
    49 assembled from them (the predict_statistics study's step, into its place for sampled tallies, U49.sampled_dir;
    the record stream is dumped when missing, a tally already made is kept; seven tallies at once, or the chain's
    --jobs). No store is opened"""
    steps = []
    for ds in dict.fromkeys(d for d in (benchmarks.dataset_in_run(w, run) for w in workloads_) if d):
        sc = sizes.scale(run, ds)
        steps.append(PY + ["experiments/predict_statistics/sampled.py", "tallies", "--dataset", ds, "--sf", str(sc),
                           "--out", str(U49.sampled_dir(Tree(), ds, sc, sample).parent),
                           *[x for k in ("percent", "seed", "floor") for x in (f"--{k}", str(sample[k]))], *own_jobs()])
    return steps


def predict(workload, run, tier, accuracy=True, sample=None):
    """the workload's predicted counts on the 49 (kvcost predict: statistics and tallies when missing), and at the
    default tier (DEFAULT_TIER), with `accuracy`, the predicted-against-measured picks (kvcost predict-accuracy). `sample`: from that sample's
    tallies (sampled_tallies made them) into the sample's own predicted set, U49.sampled_set"""
    p = pair(workload, run)
    if p is None:
        return [["echo", f"{workload}: its dataset is not in this run (--dataset {sizes.text(run)}); left out"]]
    other, source = [], []
    if sample:
        ds, sc = p.split("=")
        if tier != 1:
            raise ValueError("predict: a sample's predicted set is tier 1's (U49.sampled_set)")
        other = ["--tally-dir", str(U49.sampled_dir(Tree(), ds, sc, sample) / "tally"), "--save-as", U49.sampled_set(sample)]
        source = ["--count-source", U49.sampled_set(sample)]
    out = [KV + ["predict", "--dataset", p, "--workload", workload, *UNIFORM_49, "--tier", str(tier), *other, *own_jobs()]]
    if tier == DEFAULT_TIER and accuracy:
        out.append(KV + ["predict-accuracy", "--dataset", p, "--workload", workload, *UNIFORM_49, *source])
    return out


def measured_three(run):
    """I01 on the seven and its 49, B01 and J01 on the seven: what the predictions and the run times start from"""
    return uniform_49_results("i01", run) + measure("b01", run) + measure("j01", run)


def predict_49_results(mix, run):
    """I01, B01 and J01 on the seven; per dataset the tallies counted on the paper's sample of the owners
    (U49.PAPER_SAMPLE); then each workload's predictions on the 49 at the default tier from those tallies, in the sample's own
    predicted set, and their picks against the measured"""
    return (measured_three(run) + sampled_tallies(run, U49.PAPER_SAMPLE)
            + [s for w in ("i01", "b01", "j01") for s in predict(w, run, DEFAULT_TIER, sample=U49.PAPER_SAMPLE)])


def predict_49_full_tallies_results(mix, run):
    """predict-49's chain with the 49's own tallies, counted on every owner (kvcost predict builds them when missing),
    in the default tier's own predicted set"""
    return measured_three(run) + [s for w in ("i01", "b01", "j01") for s in predict(w, run, DEFAULT_TIER)]


def front_mix_results(mix, run):
    """I01 on the seven and its 49, its complete 3-setting front and the any-size staircase"""
    return uniform_49_results("i01", run) + questions(run, "i01-front", "i01-any-size")


def two_fronts_results(mix, run):
    """I01 on the seven and its 49: their wops and bytes, and the bill's unit lanes (cost-layouts writes them on the
    measured chunk view's rows); the figure shows the uniform layouts only, so no mix search"""
    return uniform_49_results("i01", run)


def workloads_49_results(mix, run):
    """every workload on the seven and its 49 (every model: the combined model's half view, the bill's measured view), I01
    and B01 also at each larger LDBC scale; no mix search (the figure shows the uniform layouts only)"""
    def both(w, at=None):
        return measure(w, run, at=at) + cost_49(w, run, at=at)
    big = [s for sc in sizes.larger(run, "ldbc") for w in ("i01", "b01") for s in both(w, at=sc)]
    return both("i01") + both("b01") + both("j01") + both("l01") + big


def runtime_results(mix, run):
    """The run-time table reads the runs' own records: I01, B01 and J01 on the seven (the stores' builds, the counts),
    the tier-1 predictions as predict-49 makes them (the statistics, the sampled tallies, the 49 predicted), the 49
    costed at B01's and J01's equal share (the table's compose-and-cost rows read cost-layouts' run record there; I01's
    is uniform-49's), and the mix searches it times (I01's best any-size mix, B01's and J01's at equal share: each row's
    times hold the two-step's)"""
    return (measured_three(run) + sampled_tallies(run, U49.PAPER_SAMPLE)
            + [s for w in ("i01", "b01", "j01") for s in predict(w, run, DEFAULT_TIER, accuracy=False, sample=U49.PAPER_SAMPLE)]
            + cost_49("b01-equal-share", run) + cost_49("j01-equal-share", run)
            + questions(run, "i01-any-size-best", "b01-spco-share", "j01-spco-share"))


def census_results(mix, run):
    """every workload on the seven and its 49 (the 49-workloads chain: the captures of Sp-Sp, Co-Co and Ch64k-Ch64k and
    I01's cost by part), then LDBC's logical census (analysis only: the step rows of its captures)"""
    return workloads_49_results(mix, run) + ldbc_study(run, lambda sc: [
        ["experiments/logical_census/run-logical-census.sh", "--sf", sc]])


def datasets_results(mix, run):
    """every workload on the seven: the stores (their manifests: the counts, the types, the sizes) and the run records"""
    return [s for w in ("i01", "b01", "j01", "l01") for s in measure(w, run)]


def microbench_steps_results(mix, run):
    """the microbenchmark (one access path per query) measured on the seven, three of which (Sp-Sp, Co-Co, Ch64k-Ch64k)
    the figure costs each step in, and on Co-Ch64k itself: the built layout its derive checks the costed one against.
    All seven, not the three: kvcost compose reads every query set of the dataset from each measured diagonal and
    refuses a set that only some of them hold, so a set is measured on all of them or on none"""
    return ldbc_study(run, lambda sc: [KV + ["measure", "--dataset", f"ldbc={sc}", "--family", "microbench",
                                            *SEVEN, "Co-Ch64k", *own_jobs()]])


def layout_choice_results(mix, run):
    """the layout-choice study W11's parts figure reads (the lc.w11.* keys): I01 on the seven and its 49 (its reads and
    writes), the microbenchmark on the seven (Figure 6's chain), the grid composed again with both, then the study over
    it (run-layout-choice.sh: every LDBC workload's options and breakdowns, and the budget picks)"""
    return (uniform_49_results("i01", run) + microbench_steps_results(mix, run) + ldbc_study(run, lambda sc: [
        KV + ["compose", "--dataset", f"ldbc={sc}", *own_jobs()],
        ["experiments/layout_choice/run-layout-choice.sh", "--sf", sc, *own_jobs()]]))


def complex_kv_reads_timing(sc, regimes, dbs):
    """the configurations `dbs` of the complex-kv-reads run timed at each cache regime of `regimes` (the registry's
    names, store_config.regimes): each server alone on one CPU at the regime and its configurations timed, the engines
    stopped, and, when one of `dbs` has a format on our evaluator, the same queries timed there. At a cold-start regime
    time_queries.py starts the server itself, anew before every run"""
    ef, steps = "experiments/engine_fidelity/", []
    for regime in regimes:
        at = ["env", "ENGINES_SETTING=equivalent", f"ENGINES_REGIME={regime}"]
        timed = PY + [ef + "time_queries.py", "live", "--sf", sc, "--operating-point", "equivalent", "--regime", regime,
                      "--storage-profile"]
        starts = not store_config.regime(regime, sc)["cold_start"]
        for srv, its in CKR.servers(dbs).items():          # one engine at a time: the others' threads off the machine
            steps += ([at + ["bash", "engines/start-server.sh", "--db", srv, "--sf", sc]] if starts else []) + [
                timed + ["--db", ",".join(its)], ["bash", "engines/stop-server.sh", "--db", srv]]
        if set(dbs) & set(CKR.evaluator_dbs()):
            steps.append(PY + [ef + "time_queries.py", "evaluator", "--sf", sc, "--regime", regime])
    return steps


def complex_kv_reads_steps(sc, dbs, starts_run):
    """the configurations `dbs` of the three multi-step reads on the live databases, from fresh databases: the data;
    each configuration's server (complex_kv_reads.servers) built (when its binary is missing), loaded and backed up;
    then, round by round
    (complex_kv_reads.rounds: at most one server per engine, as two servers of one engine share its ports), the
    round's servers started at the counting point, their configurations counted (IC1 at its claim params, IC3, the
    microbenchmark read, each on the configurations measured on it; NebulaGraph's IC1 edge-list text at IC1's drawn
    param), and stopped; then the timing at the figure's cache regime (complex_kv_reads_timing at
    complex_kv_reads.REGIME: one CPU's server with a bounded block cache, warmed up). starts_run: the first round's
    counts start the run's count files (else every round adds to them). Needs `just setup-engines` first (the engines'
    build tools; sudo)"""
    ef = "experiments/engine_fidelity/"
    servers = CKR.servers(dbs)                              # {server: its configurations}
    # named: the engines' `--db all` is the first three only
    steps = [PY + ["-m", "kvevaluator.fetch_dataset", f"ldbc={sc}"],
             ["bash", "engines/once.sh", "--db", ",".join(servers), "--sf", sc]]
    drawn = {q: p for _, q, p in CKR.CELLS}
    for i, rnd in enumerate(CKR.rounds(dbs)):
        count = PY + [ef + "measure.py", "--run", CKR.RUN, "--sf", sc, "--db",
                      ",".join(db for srv in rnd for db in servers[srv])]
        fresh = [] if i == 0 and starts_run else ["--append"]   # the run's first round starts its counts
        steps += [["bash", "engines/start-server.sh", "--db", ",".join(rnd), "--sf", sc],
                  count + ["--query", "ic1", "--param", ",".join(map(str, CKR.claim_params()))] + fresh,
                  count + ["--query", "ic3", "--param", str(drawn["ic3"]), "--append"],
                  count + ["--family", "microbench", "--query", "hop1p7_knows"] + fresh]
        for variant, v in CKR.QUERY_VARIANTS.items():  # a query-text variant: counted at its base query's drawn param
            if CKR.server(v["db"]) in rnd:
                steps.append(PY + [ef + "measure.py", "--run", CKR.RUN, "--sf", sc, "--db", v["db"], "--query", variant,
                                   "--param", str(drawn[v["query"]]), "--append"])
        steps.append(["bash", "engines/stop-server.sh", "--db", ",".join(rnd)])
    return steps + complex_kv_reads_timing(sc, (CKR.REGIME,), dbs)


def complex_kv_reads_results(mix, run):
    """the configurations the paper's figure reads (complex_kv_reads.figure_configurations), counted and timed into a
    fresh run (complex_kv_reads_steps)"""
    return ldbc_study(run, lambda sc: complex_kv_reads_steps(sc, CKR.figure_configurations(), starts_run=True))


def complex_kv_reads_other_results(mix, run):
    """the configurations only the figure's options draw (complex_kv_reads.other_configurations), counted and timed into
    the run `just results complex-kv-reads` made, beside its own (complex_kv_reads_steps)"""
    return ldbc_study(run, lambda sc: complex_kv_reads_steps(sc, CKR.other_configurations(), starts_run=False))


def complex_kv_reads_cold_results(mix, run):
    """the complex-kv-reads run timed at its other cache regimes (complex_kv_reads.REGIME_OPTIONS: a cold start before
    every run), nothing else: it adds times to the run `just results complex-kv-reads` made (its loads and its counts,
    which each timed run is checked against; time_queries.py refuses a run without them) and leaves that run's own
    times and counts as they are"""
    return ldbc_study(run, lambda sc: complex_kv_reads_timing(sc, tuple(CKR.REGIME_OPTIONS.values()),
                                                              CKR.figure_configurations()))


def engine_fidelity_results(mix, run):
    """each engine's own plan on the evaluator against the paper's live capture (paper-results: the engines' counters,
    their answers, Dgraph's uids), with no engine running: LDBC Interactive at the capture's scales that the run
    names, the microbench at the first. The engine stores are ingested on first use"""
    have = [sc for sc in LW.FIDELITY_SCALES if sc in run.get("ldbc", [])]
    if not have:
        sys.exit(f"engine-fidelity: the paper's live capture is at ldbc {' and '.join(LW.FIDELITY_SCALES)}; "
                 f"--dataset {sizes.text(run)} names neither (its own: --dataset {LW.FIDELITY_DATASET})")
    validate = PY + ["experiments/engine_fidelity/validate.py", "--live", "--from", "paper-results"]
    steps = []
    for sc in have:
        steps += [PY + ["-m", "kvevaluator.fetch_dataset", f"ldbc={sc}"], validate + ["--sf", sc]]
        if sc == LW.FIDELITY_SCALES[0]:
            steps.append(validate + ["--sf", sc, "--family", "microbench"])
    # the write side: the formats' write counts against the paper's live write sweep; a write's counts do not depend
    # on the scale (write_fidelity.py), so they are measured at the run's smallest fidelity scale
    sc, sweep = have[-1], LW.write_sweep_path(LW.FIDELITY_SCALES[0], Tree(ROOT / "paper-results"))
    return steps + [KV + ["measure", "--dataset", f"ldbc={sc}", "--family", "writes", *LW.WRITE_STORES.values(), *own_jobs()],
                    PY + ["experiments/engine_fidelity/write_fidelity.py", "--sweep", str(sweep),
                          "--capture", Tree().capture("writes", sc).dir("counts")]]


def complex_kv_reads_compare(source, run):
    """the fresh run against the paper's (experiments/engine_fidelity/compare_complex_kv_reads.py), after the draw: at
    the paper's scale, with paper-results/ in place; else a line"""
    if source == "paper-results":
        return []
    if ldbc(run) != ldbc(sizes.resolve(CKR.SCALE)):
        return [["echo", f"compare: the paper's run is at {CKR.SCALE}; nothing to compare at --dataset {sizes.text(run)}"]]
    if not (ROOT / "paper-results").is_dir():
        return [["echo", "compare: paper-results/ is not here (just fetch-results); the fresh run is not compared"]]
    return [PY + ["experiments/engine_fidelity/compare_complex_kv_reads.py", "--dataset", sizes.text(run)]]


def latency_results(mix, run):
    """the 12 layouts' steady timing for LDBC Interactive and BI and JOB, and Interactive's writes, each query at the
    body the picks file names (inputs/latency-picks/); nothing counted unless --validate-count"""
    return [s for w in ("interactive", "bi", "job") for s in latency(w, run)]


LATENCY = (  # (query set timed, workload, its own flags)
    ("interactive", "i01", []),      # I01 writes: its writes timed too
    ("bi", "b01", []),
    ("job", "j01", []),              # a drift check (Sp-Co timed again at the end): measure-latency --drift-control Sp-Co
)


def latency(query_set, run):
    """one query set's latency (kvcost measure-latency): the 12 uniform layouts timed at the paper's steady settings
    (with --validate-count, counted first and their timed counts compared)"""
    _, workload, flags = next(t for t in LATENCY if t[0] == query_set)
    ds = workloads.get(query_set).dataset
    sc = sizes.scale(run, ds)
    if sc is None:
        return [["echo", f"{query_set}: {ds} is not in this run (--dataset {sizes.text(run)}); left out"]]
    return [KV + ["measure-latency", "--dataset", f"{ds}={sc}", "--workload", workload, *LATENCY_12, *flags,
                  *(["--validate-count"] if VALIDATE else []), *own_jobs()]]


def predict_formulas_results(mix, run):
    """predict mode's accuracy on the workload `mix` (a workload file, as predict-49: i01, b01, j01) at its dataset's scale
    in the run: the data when missing; per read family of the workload, its stores on the seven (run/predict-gate.sh
    --stores, built when missing); per family (its reads, then its writes: a write runs in shadow on the same stores, at
    the rows kvcost predict binds, --write-params capture) run/predict-accuracy.sh's traced pass on the seven, the
    family's queries of the workload, the items' params (else the script's default, 1-5), into
    summary/<dataset>/<scale>/predict-formulas/<workload>/<family>/; then the workload's prediction as predict-49 makes
    it (the sampled tallies, kvcost predict on the 49), the last levels of the chain"""
    from kvcost.predict_accuracy import layouts as PL
    from kvcost.predict_accuracy import predict_formulas as PF
    from kvevaluator import layouts
    ds = benchmarks.dataset_in_run(mix, run)
    if ds is None:
        return [["echo", f"{mix}: its dataset is not in this run (--dataset {sizes.text(run)}); left out"]]

    def steps(sc):
        w = PL.one_workload(ds, sc, mix)
        out = [PY + ["-m", "kvevaluator.fetch_dataset", f"{ds}={sc}"]]
        fams = PF.families(w)
        # the stores first (a read family's; the writes run on the same ones), then each family's pass
        out += [["run/predict-gate.sh", "--sf", sc, "--workload", f, "--stores", "--no-fixtures", "--no-writes",
                 *own_jobs()] for f in fams if benchmarks.kind_of(f) == "read"]   # the seven only
        for f in fams:
            items = [it for it in w["items"] if it[0] == f]
            own = [max(p) for *_, p in items if p]   # the items' own params, else the script's default (1-5)
            write = benchmarks.kind_of(f) == "write"
            out.append(["run/predict-accuracy.sh", "--sf", sc, "--family", f, "--cells", ",".join(layouts.SEVEN),
                        "--queries", ",".join(dict.fromkeys(q for _, q, *_ in items)),
                        *(["--nparam", str(max(own))] if own else []), *(["--write-params", "capture"] if write else []),
                        "--out", str(PF.item_dir("output", ds, sc, mix, f)), *own_jobs()])
        return out
    # the sampled levels: predict-49's own prediction of the workload (the sampled tallies, kvcost predict at the default
    # tier into the sample's predicted set, U49.sampled_set), which the tables read in place
    return (on(ds, run, steps) + sampled_tallies(run, U49.PAPER_SAMPLE, (mix,))
            + predict(mix, run, DEFAULT_TIER, accuracy=False, sample=U49.PAPER_SAMPLE))


def predict_formulas_tables(source, mix, run, tables):
    """the workload totals' error per level and layout, the parts, the spread, and per operator
    (kvcost.predict_accuracy.predict_formulas)"""
    return [PY + ["-m", "kvcost.predict_accuracy.predict_formulas", "--from", source, "--dataset", sizes.text(run),
                  "--workload", mix, "--tables", str(tables)]]


FIGURES = {   # in the order a full run takes them (the README's; run/reproduce-all.sh): the chain that times stores last
    "uniform-49": {"shows": ['figures/evaluation/uniform-49-layouts-w16-by-part.pdf'], "workloads": ["{mix}"], "paper": "fig:uniform-49",
             "what": "the 49 uniform layouts of one workload: wops and bytes, each bar split by part",
             "flags": {"workload": "i01", "dataset": sizes.PAPER},
             "results": uniform_49_results, "derive": derive("uniform_i01_chunk_sizes_and_front.py", "--workload", "{mix}"),
             "draw": ["fig_uniform_49_layouts_w16_by_part.py"]},
    "ldbc-triangle": {"shows": ['figures/evaluation/ldbc-random-workloads-triangle-right.pdf'], "workloads": ["i01"], "paper": "fig:ldbc-triangle",
             "what": "random LDBC Interactive workloads on a triangle: the cheapest uniform layout of each",
             "flags": {"dataset": sizes.PAPER},
             "results": triangle_results, "derive": derive("ldbc_random_workloads_triangle.py"),
             "draw": ["fig_ldbc_random_workloads_triangle.py"]},
    "two-fronts": {"shows": ['figures/evaluation/two-fronts-w16.pdf'], "workloads": ["i01"], "paper": "fig:two-fronts",
             "what": "I01's two fronts over the 49 uniform layouts: wops against bytes, and the DynamoDB bill's units",
             "flags": {"dataset": sizes.PAPER},
             "results": two_fronts_results,
             "derive": lambda *a: (derive("bill_49.py")(*a)
                                   + derive("uniform_i01_chunk_sizes_and_front.py", "--workload", "i01")(*a)),
             "draw": ["fig_two_fronts_w16.py"]},
    "front-mix": {"shows": ['figures/evaluation/front-mix-vs-uniform-w16.pdf'], "workloads": ["i01"], "paper": "fig:front-mix",
             "what": "I01: the best mix with Chunked at any size against the uniform layouts",
             "flags": {"dataset": sizes.PAPER},
             "results": front_mix_results, "derive": derive("front_by_part_49_layouts_i01.py"),
             "draw": ["fig_front_mix_vs_uniform_w16.py"]},
    "uniform-49-workloads": {"shows": ['figures/evaluation/uniform-49-layouts-workloads-cc-wops-bytes-no-mix-no-once.pdf'], "workloads": ["i01", "b01", "j01", "l01"], "paper": "fig:uniform-49-workloads",
             "what": "the 49 uniform layouts' combined cost on each workload (LDBC Interactive and BI, JOB, LinkBench)",
             "flags": {"dataset": sizes.PAPER},
             "results": workloads_49_results, "derive": derive("uniform_49_layouts_workloads.py"),
             "draw": ["fig_uniform_49_layouts_workloads_wops_bytes.py"]},
    "predict-49": {"shows": ['figures/evaluation/predict-49-w16-bi-job.pdf'], "workloads": ["i01", "b01", "j01"], "paper": "fig:predict-49",
             "what": f"predicted against measured cost of the 49 layouts: I01, BI B01, JOB J01 (predictions from "
                     f"{U49.sample_text(U49.PAPER_SAMPLE)})",
             "flags": {"dataset": sizes.PAPER},
             "results": predict_49_results, "derive": derive("predict_49.py"),
             "draw": ["fig_predict_49_w16_bi_job.py"]},
    # the same figure from the 49's own tallies (every owner): the same derive on the tier's own set, the same draw
    "predict-49-full-tallies": {"workloads": ["i01", "b01", "j01"], "paper": "fig:predict-49",
             "what": "predictions from the full tallies (every owner), against measured cost of the 49 layouts (I01, BI "
                     "B01, JOB J01); the paper's figure uses sampled tallies",
             "flags": {"dataset": sizes.PAPER},
             "results": predict_49_full_tallies_results,
             "derive": derive("predict_49.py", "--set", default_set(), "--note", "the full tallies"),
             "draw": ["fig_predict_49_w16_bi_job.py"], "optional": "the paper's figure from the full tallies, not in the paper"},
    "runtime": {"shows": ['tables/run-time.tex'], "workloads": ["i01", "b01", "j01"], "paper": "tab:runtime",
             "what": "the run time of measuring against predicting, and of finding the best mix",
             "flags": {"dataset": sizes.PAPER},
             "results": runtime_results, "derive": derive("run_time.py"), "draw": ["tab_run_time.py"]},
    "census-touched": {"shows": ['tables/census-workloads-touched.tex'], "workloads": ["i01", "b01", "j01", "l01"], "paper": "tab:census-touched",
             "what": "the elements each workload's plans touch, by part (the census.* keys)",
             "flags": {"dataset": sizes.PAPER},
             "results": census_results, "derive": derive("census_workloads.py"),
             "draw": ["tab_census_workloads_touched.py"]},
    "datasets": {"shows": ['tables/datasets.tex'], "workloads": ["i01", "b01", "j01", "l01"], "paper": "tab:datasets",
             "what": "each dataset's size and workloads (the workloads.* keys)",
             "flags": {"dataset": sizes.PAPER},
             "results": datasets_results, "derive": derive("datasets.py"), "draw": ["tab_datasets.py"]},
    "plan-bytes": {"workloads": ["i01"], "paper": "sec:kvformat-evaluator",
             "what": "why plans are picked by operations plus bytes: the largest bytes gap between the two rules on the "
                     "measured uniform layouts (the plans.sf3.grid.* keys)",
             "flags": {"dataset": sizes.PAPER},
             "results": lambda mix, run: measure("i01", run), "derive": derive("plan_bytes.py"), "draw": []},
    "price-steps": {"workloads": [], "paper": "fig:price-steps",
             "what": "one microbenchmark read's steps costed in the three built layouts (the keys the "
                     "table cites)",
             "flags": {"dataset": sizes.PAPER},
             "results": microbench_steps_results, "derive": derive("price_steps.py"), "draw": []},
    "complex-kv-reads": {"shows": ['figures/background/complex_kv_reads_stacked_times.pdf'], "workloads": [], "paper": "fig:complex-kv-reads",
             "what": "the KV reads and time of three multi-step reads on the live databases (the complex-kv-reads.* keys)",
             "flags": {"dataset": CKR.SCALE},
             "results": complex_kv_reads_results, "derive": derive("complex_kv_reads.py"),
             "draw": ["fig_complex_kv_reads.py"], "compare": complex_kv_reads_compare,
             # `just reproduce-all` runs it at the paper's settings only, at this item's own scale: a small-set run
             # stays a quick check with no docker, no sudo and no database builds (alone, `just results
             # complex-kv-reads` runs at any --dataset)
             "paper_settings_only": "the three live databases"},
    "complex-kv-reads-cold": {"workloads": [], "paper": "fig:complex-kv-reads",
             "what": "the same three reads timed from a cold start, after `just results complex-kv-reads`: every run on "
                     "a server started anew, nothing of its store in memory",
             "flags": {"dataset": CKR.SCALE},
             "results": complex_kv_reads_cold_results,
             # its times are rows of complex-kv-reads' run: that item's derive and draw read them, and `just figure`
             # of this item is that item's (drawn_by), so no table or figure is made twice
             "derive": lambda *a: [], "draw": [], "drawn_by": "complex-kv-reads",
             "optional": "the paper prints the warm times only"},
    "complex-kv-reads-other-configurations": {"workloads": [], "paper": "fig:complex-kv-reads",
             "what": "the engine configurations only the figure's options draw (complex_kv_reads.other_configurations), "
                     "counted and timed into the run `just results complex-kv-reads` made",
             "flags": {"dataset": CKR.SCALE},
             "results": complex_kv_reads_other_results,
             # its rows join complex-kv-reads' run: that item's derive and draw read them (`just figure` of this item
             # is that item's), and draw each option whose configurations the run holds
             "derive": lambda *a: [], "draw": [], "drawn_by": "complex-kv-reads",
             "optional": "the paper's figure shows its own configurations only"},
    "engine-fidelity": {"workloads": [], "paper": "sec:kvformat-evaluator",
             "what": "each engine's own plan on the evaluator against the live engines' saved counters and answers, "
                     "with no engine running (the fidelity.* keys)",
             "flags": {"dataset": LW.FIDELITY_DATASET},
             "results": engine_fidelity_results, "derive": derive("engine_fidelity.py"), "draw": [],
             # at its own scales, the live capture's; a check of the evaluator, run on its own
             "optional": "the paper's fidelity paragraph, re-checked on request"},
    "latency-vs-cost": {"shows": ['tables/latency-selection.md'], "workloads": ["i01", "b01", "j01"], "paper": "sec:results:latency",
             "what": "steady latency against the op-byte cost, the 12 uniform layouts: LDBC Interactive and BI, JOB",
             "flags": {"dataset": sizes.PAPER},
             "results": latency_results,
             "derive": lambda *a: (derive("latency_steady_ldbc.py")(*a)
                                   + derive("latency_steady_job.py", "--layouts", *LATENCY_12)(*a)),
             # "draw": ["fig_latency_vs_cost_linear.py"]},   # the figure, not in the paper: the table takes its place
             "draw": ["tab_latency_selection.py"]},
    "parts-w11": {"workloads": [], "paper": "fig:parts-w11",
             "what": "why a mix can pay, part by part, on W11 (the lc.w11.* keys the figure cites)",
             "flags": {"dataset": sizes.PAPER},
             "results": layout_choice_results, "derive": derive("layout_choice.py"), "draw": []},
    "predict-formulas": {"workloads": ["{mix}"], "paper": "no paper section",
             "what": "predict mode's accuracy on a workload's total (its reads and its writes, and each alone), on the "
                     "seven, at the default statistics tier: each level's error, from the truth card down to predict-49's "
                     "own prediction (the chunks touched, the sizes, the counts, the owner sample, the plan choice), the "
                     "parts it adds up from, the spread over the layouts, and per operator",
             "flags": {"workload": "i01", "dataset": sizes.PAPER},
             "results": predict_formulas_results, "derive": predict_formulas_tables, "draw": ["fig_predict_formulas.py"],
             "optional": "a check of predict mode the paper does not print"},
    # archive-only (made from paper-results/ alone: no `just results` chain re-measures it) and not printed by the paper
    # (it cites none of the composed.* keys): its study's check, kept with its `just figure`
    "composed-vs-measured": {"workloads": [], "paper": "sec:layout-choice",
             "what": "composed against measured KV cost of the built mixes (the composed.* keys)",
             "flags": {}, "results": None, "derive": archive("composed_vs_measured.py"), "draw": [], "optional": "not in the paper"},
}


def workload_home(mix):
    """(dataset, the workload file's title) of a query mix, at whichever of its dataset's scales holds its file"""
    for ds in datasets.names():
        for sc in datasets.get(ds).scales:
            for d in benchmarks.workloads_dirs(ds, sc):
                if (d / f"{mix}.json").is_file():
                    return ds, json.loads((d / f"{mix}.json").read_text()).get("title", "")
    return None, ""


def git_commit():
    """the checkout's commit and whether it has changes, or "unknown" (a release archive has no .git)"""
    r = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


SIBLINGS = (".pdf", ".png", ".tex", ".md")      # a shown file's other forms, copied beside it when the draw wrote them


def show_paper_files(name, f, out):
    """copy the file(s) the paper prints (the item's "shows", relative to out) into <out>/paper/, each with its other
    forms (SIBLINGS); the other files under figures/ and tables/ are extra views. A named file the draw did not write
    stops the step: the registry and the draw script disagree"""
    if not f.get("shows"):
        return []
    dest = out / "paper"
    shutil.rmtree(dest, ignore_errors=True)            # a copy this step makes: rebuilt on every draw
    dest.mkdir(parents=True)
    shown = []
    for rel in f["shows"]:
        src = out / rel
        if not src.is_file():
            sys.exit(f"{name}: the paper's file {rel} was not drawn (run/reproduce.py \"shows\" and the draw script disagree)")
        for sib in sorted({src, *(src.with_suffix(x) for x in SIBLINGS)}):
            if sib.is_file():
                shutil.copy2(sib, dest / sib.name)
                shown.append(sib.name)
    return shown


def write_about(name, f, mix, scales, source, out, shown=()):
    """<out>/about.md: what the drawn figure shows, its workloads at the run's scales, the tree and the commit it came
    from (a drawn PDF has no caption: the paper's caption says this)"""
    rows = []
    for w in (x.format(mix=mix) for x in f["workloads"]):
        ds, title = workload_home(w)
        sc = sizes.scale(scales, ds) if ds else None
        rows.append(f"| {w} | {title} | {ds or '?'} | {datasets.get(ds).scale_name(sc) if sc else 'not in this run'} |")
    text = "\n".join([
        f"# {name} ({f['paper']})", "",
        *([f"What the paper prints: {', '.join(f'`paper/{x}`' for x in shown)}. The other files under figures/ and "
           f"tables/ are extra views of the same data.", ""] if shown else []),
        f"{f['what'][0].upper()}{f['what'][1:]}.", "",
        (f"Made from paper-results/ alone (an archive-only item); artifact {git_commit()}." if f["results"] is None else
         f"Drawn from {'paper-results/ (the paper\'s runs)' if source == 'paper-results' else 'output/'} at --dataset "
         f"{sizes.text(scales)}; artifact {git_commit()}."), "",
        *(["| query mix | what | dataset | scale |", "|---|---|---|---|", *rows] if rows else []), ""])
    (out / "about.md").write_text(text)


def derive_all():
    """every paper/derive/*.py (snapshots.py is their shared code) once on paper-results/, as gs-paper's check runs
    them: no argument, tables and number keys into output/paper/build/; prints each one's rc and time, the derives no
    item names, and the ran and failed counts; exits 1 when one failed"""
    scripts = sorted(p for p in (ROOT / "paper" / "derive").glob("*.py") if p.name != "snapshots.py")
    named = set()
    for f in FIGURES.values():
        for step in f["derive"]("paper-results", f["flags"].get("workload"), sizes.resolve(sizes.PAPER), ROOT):
            named |= {x.rsplit("/", 1)[1] for x in step if x.startswith("paper/derive/")}
    failed = []
    for script in scripts:
        t = time.monotonic()
        rc = subprocess.run(PY + [str(script.relative_to(ROOT))], cwd=ROOT, capture_output=True, text=True)
        tail = (rc.stderr or rc.stdout).strip().splitlines()[-1:] if rc.returncode else []
        print(f"derive {script.stem}: rc={rc.returncode} ({time.monotonic() - t:.0f} s)"
              + (f" {tail[0][:200]}" if tail else ""), flush=True)
        if rc.returncode:
            failed.append(script.stem)
    loose = sorted(s.name for s in scripts if s.name not in named)
    print(f"derives no item names ({len(loose)}): {', '.join(loose)}")
    print(f"derive-all {'OK' if not failed else 'FAIL'}: {len(scripts)} ran, {len(failed)} failed"
          + (f" ({', '.join(failed)})" if failed else "") + f"; tables and keys in {Tree().paper('build')}")
    return 1 if failed else 0


def run_label(scales):
    """a run's name in output/figures/<figure>/: its scales, ldbc-0.1_imdb-sample1pct"""
    return sizes.text(scales).replace("=", "-").replace(",", "_")


DRY = False   # --dry-run: print each command, run none


def run(cmd):
    """print the command, run it from the repo root, print its time; exit with its code when it fails"""
    print(f"\n$ {shlex.join(cmd)}", flush=True)
    if DRY:
        return
    t = time.monotonic()
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    print(f"[reproduce] rc={rc} in {time.monotonic() - t:.0f} s", flush=True)
    if rc != 0:
        sys.exit(rc)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("action", choices=("results", "figure", "derive-all"))
    ap.add_argument("figure", nargs="?", help="the figure's name, e.g. uniform-49 (none: list them)")
    ap.add_argument("--workload", help="the workload, e.g. i01 or j01 (default: the paper's)")
    ap.add_argument("--dataset", help="each dataset's scale, e.g. ldbc=0.1,imdb=sample1pct; a dataset left out is not "
                    f"run (default the paper's: {sizes.PAPER}; python/kvevaluator/sizes.py)")
    ap.add_argument("--from", dest="source", choices=("output", "paper-results"), default=None,
                    help="figure: draw from a `just results` run (default) or from paper-results/ (the paper's runs; "
                         "an archive-only item's only source)")
    ap.add_argument("--dry-run", action="store_true", help="print the chain's commands, run none")
    ap.add_argument("--validate-count", action="store_true",
                    help="results, for an item whose chain times stores (kvcost measure-latency): count its layouts "
                         "too and compare every timed run's counts with them (default: time only)")
    ap.add_argument("--jobs", type=int, default=None,
                    help="results: how many stores (or other units: the processes of a compose, a predict or a mix "
                         "search) each command runs at once, passed to every command that runs work side by side "
                         "(default: each command's own, the cores capped by memory where a peak is known; a "
                         "measure's is printed first). Lower it on a machine short of memory or disk")
    a = ap.parse_args()
    global DRY, JOBS, VALIDATE
    DRY, JOBS, VALIDATE = a.dry_run, a.jobs, a.validate_count
    if a.action == "derive-all":
        if a.figure or a.workload or a.dataset or a.source:
            sys.exit("derive-all runs every derive on paper-results/: it takes no item, --workload, --dataset or --from")
        sys.exit(derive_all())
    if a.figure is None:
        for name, f in FIGURES.items():
            flags = " ".join(f"--{k} {v}" for k, v in f["flags"].items())
            how = (f"optional ({f['optional']}): just {'results' if f['results'] else 'figure'} {name}" if f.get("optional") else
                   f"paper's settings only ({f['paper_settings_only']}): {flags}" if f.get("paper_settings_only") else
                   f"paper's settings: {flags}" if f["results"] else "archive-only: just figure " + name)
            print(f"{name:20} {f['paper']:24} {f['what']}\n{'':20} {how}")
        return
    if a.figure not in FIGURES:
        sys.exit(f"unknown figure {a.figure}; known: {', '.join(FIGURES)}")
    if a.action == "figure" and "drawn_by" in FIGURES[a.figure]:   # an item that adds rows to another item's run
        print(f"[reproduce] {a.figure} is drawn by {FIGURES[a.figure]['drawn_by']}: its figure", flush=True)
        a.figure = FIGURES[a.figure]["drawn_by"]
    f = FIGURES[a.figure]
    if f["results"] is None:
        if a.action == "results":
            sys.exit(f"{a.figure} is archive-only: no chain re-measures it; make it from paper-results/ with "
                     f"`just figure {a.figure}`")
        if a.source == "output" or a.workload or a.dataset:
            sys.exit(f"{a.figure} is archive-only: its derive reads paper-results/ alone (no --from output, --workload "
                     f"or --dataset)")
        a.source = "paper-results"
    a.source = a.source or "output"
    if a.workload and "workload" not in f["flags"]:
        sys.exit(f"{a.figure} draws the paper's own workloads: it takes no --workload")
    mix, scale = a.workload or f["flags"].get("workload"), a.dataset or f["flags"].get("dataset", sizes.PAPER)
    scales = sizes.resolve(scale)
    t0 = time.monotonic()
    if VALIDATE and (a.action != "results" or not f["results"] or not any("measure-latency" in step for step in f["results"](mix, scales))):
        sys.exit(f"--validate-count: {a.figure}'s {a.action} times no store (no kvcost measure-latency step)")
    if a.action == "results":
        for ds in datasets.names():                # the seven's stores at once, and why, per dataset of the run
            for sc in filter(None, [sizes.scale(scales, ds), *sizes.larger(scales, ds)]):
                why = run_measure.measure_at_once(JOBS, None, ds, sc, run_measure.passes(SEVEN)[1])[2]
                print(f"[reproduce] {ds} {sc}: its seven stores measured {why}", flush=True)
        for step in f["results"](mix, scales):
            run(step)
        print(f"\nresults OK: {a.figure} {mix or ''} at --dataset {scale} ({sizes.text(scales)}) in {time.monotonic() - t0:.0f} s; draw it: "
              f"just figure {a.figure}{' --workload ' + mix if a.workload else ''} --dataset {scale}")
        return
    if a.source == "paper-results" and (a.workload or a.dataset):
        sys.exit("--from paper-results draws the paper's own data: no --workload or --dataset")
    run_name = "paper-results" if a.source == "paper-results" else "_".join(filter(None, (mix, run_label(scales))))
    out = Tree().figure(a.figure, run_name)
    for step in f["derive"](a.source, mix, scales, out / "tables"):
        run(step)
    for script in f["draw"]:
        run(PY + [f"paper/draw/{script}", "--data", str(out / "tables"), "--out", str(out), "--scale", ldbc(scales)])
    if not DRY:
        shown = show_paper_files(a.figure, f, out)
        write_about(a.figure, f, mix, scales, a.source, out, shown)
    print(f"\nfigure OK: {a.figure} ({f['paper']}) from {a.source} -> {out}", flush=True)
    for step in f.get("compare", lambda *x: [])(a.source, scales):     # a fresh run against the paper's, its verdict last
        run(step)


if __name__ == "__main__":
    main()
