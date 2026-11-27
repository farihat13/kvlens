#!/usr/bin/env python3
"""Derive run-time's tables: what choosing among the 49 uniform layouts costs (the paper: at SF3), by measuring and by
predicting, and what finding the best mix costs; one row per run record, from one output tree (--from; the paper's runs
by default) at the run's scales (--scale: each dataset's).

  <tables>/run-time.csv              route (measure, predict, mix), step, workload, item, wall_s, proven and gap
                                     (the mix search's verify only), source (its place in the tree)
  <tables>/run-time-search-size.csv  per workload, the queries and candidate plans the search covers
  <numbers>/run_time.tex             runtime.sf3.<workload>.{measure,predict}.min and .ratio (the routes' totals: build
                                     and run, statistics, tally and predict; sf3 is the key's name at any scale);
                                     runtime.sf3.{ingest,reads,build}.min (one uniform LDBC layout's store built and its
                                     Interactive reads measured, reported and held-out: the longest of Sp-Sp, Co-Co and
                                     Ch64k-Ch64k; ingest the longest store, reads the range)

The 49 are composed from the seven diagonal stores, so: measure = build the seven stores (ingest_wall_s, the store whose
layout.json names the layout) and run the workload on each (every candidate body, 5 params, 8 workers); predict = the
statistics (stats/<dataset>/<scale>/stats.json), one tally per setting, counted on the paper's sample of the owners
(kvcost.predict_accuracy.uniform_49_tallies.PAPER_SAMPLE: the seven diagonal layouts' tallies,
summary/<dataset>/<scale>/predict-statistics/sampled/<sample>/tally-diagonal/) and predict at tier 1 from those tallies
(the run's captures of the sample's set, predicted/<dataset>/<scale>/predicted-tier1-<sample>/), with no store built and
no query run; mix = the two-step's find without the cost cache, then the pinned row's branch and
bound as its verify (the mix search's rows: the two-step's two stages are the row's own times, two_step_a_s and
two_step_b_s, named by the solver that ran them: on the cost cache (a) each part alone, (b) one-part moves; without it (a) at
fixed plans, (b) plans re-picked). The measure route's "compose and price" rows (the composed layouts' records' wall_s,
cost-layouts' runs.jsonl) are shown beside it, not in its total. Totals sum the records one after another (no parallel
lanes). The record stream both routes read is made once and counted in neither (its key file's wall_s). A workload whose dataset the run leaves out, or whose
records the tree does not hold, is skipped and named, as is a larger scale's; their keys are not written. Refused
(exit 1, nothing written): a workload of the runtime chain (CHAIN) whose measured side the tree holds and whose
predicted side (the sampled tallies, the sample's predicted set) it does not; the message names the file and
`just results runtime`.

    uv run python paper/derive/run_time.py [--from output --dataset ldbc=0.1,imdb=sample1pct] [--tables DIR]
"""
import argparse
import csv
import json
from pathlib import Path

from kvcost.layout_search import results as R
from kvcost.predict_accuracy import uniform_49_tallies as U

from kvevaluator.layouts import OTHER_CHUNK_SIZES, cell_id
from kvevaluator.output_tree import capture_in
from kvevaluator.paths import ROOT
from snapshots import add_tree_args, scale_in, store_manifests, tree_of, write_numbers, write_table

SIZES = OTHER_CHUNK_SIZES
DIAGONAL = ("props_split_adj_split", "props_condensed_adj_condensed", "props_chunked_adj_chunked")
SEVEN = DIAGONAL + tuple(cell_id(f"chunked_c{k}", f"chunked_c{k}") for k in SIZES)   # the seven stores' layouts
WORKLOADS = ("Interactive", "BI", "JOB", "LinkBench")
CHAIN = ("Interactive", "BI", "JOB")   # the workloads `just results runtime` measures and predicts (the registry's item)
SHARED = {"Interactive": "LDBC", "BI": "LDBC", "JOB": "IMDb", "LinkBench": "LinkBench"}   # the dataset whose stores, statistics and tallies a workload reads
DATASET = {"LDBC": "ldbc", "IMDb": "imdb", "LinkBench": "linkbench"}
FAMILIES = {"Interactive": ("interactive", "writes"), "BI": ("bi",), "JOB": ("job",),
            "LinkBench": ("linkbench", "linkbench_writes")}   # the captures a workload runs
SAMPLE = U.PAPER_SAMPLE           # the sample of the owners the paper's tallies are counted on
PREDICTED = U.sampled_set(SAMPLE)  # kvcost predict Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 1 from those tallies: its set
MIX = {"Interactive": "i01", "BI": "b01-spco-share", "JOB": "j01-spco-share", "LinkBench": "l01"}
MIX_MODEL = "wops_bytes4k"
MIX_ROWS = R.stem("per-type-any-size") + ".json"      # the any-size rows (each type its own setting, any chunk size)
WORKLOAD_FILES = {"Interactive": "inputs/workloads/ldbc/i01.json", "BI": "inputs/workloads/ldbc/equal-share/b01.json",
                  "JOB": "inputs/workloads/imdb/equal-share/j01.json",
                  "LinkBench": "inputs/workloads/linkbench/n1m/l01.json"}
# the two-step's stages by the solver that ran them (questions.best_mix, best_mix_no_cost_cache)
TWO_STEP = {"cost-cache": (("find, each part alone", "two-step (a)"), ("find, one-part moves", "two-step (b)")),
            "no-cost-cache": (("find, fixed plan", "two-step (a)"), ("find, plans re-picked", "two-step (b)"))}
COLUMNS = ("route", "step", "workload", "item", "wall_s", "proven", "gap", "source")
SIZE_COLUMNS = ("workload", "queries", "plans", "source")


def wall(path, key="wall_s"):
    v = json.load(open(path))[key]
    if not isinstance(v, (int, float)):
        raise SystemExit(f"{path}: no {key}")
    return float(v)


def setting_of(cell):
    """Sp, Co or Ch<k> of a diagonal cell (Ch64k: the grid's size)"""
    s = cell.split("_adj_")[0].removeprefix("props_")
    return {"split": "Sp", "condensed": "Co", "chunked": "Ch64k"}.get(s) or f"Ch{s.removeprefix('chunked_c')}"


def diagonal_records(captures, family):
    """the seven diagonal stores' run records of one family (`family`/reported/counts): {setting: path}"""
    out = {setting_of(c): Path(capture_in(captures, family).record(c)) for c in SEVEN}
    missing = [str(p) for p in out.values() if not p.exists()]
    if missing:
        raise SystemExit(f"missing run records: {missing}")
    return out


def tallies(where):
    """[(setting, path)] of the seven diagonal layouts' tallies in a sample's tally dir (`where`: its tally-diagonal/,
    one file per diagonal cell, each with its wall_s); the setting is the layout's (Sp, Co, Ch1k, ...)"""
    out = sorted((setting_of(p.stem), p) for p in where.glob("*.json"))
    if sorted(s for s, _ in out) != sorted(setting_of(c) for c in SEVEN):
        raise SystemExit(f"{where}: settings {[s for s, _ in out]}, not the seven")
    return out


def predicted(d):
    """the 49 predicted run records of one set: [path]"""
    out = sorted(p for p in d.glob("props_*.json") if not p.name.endswith(".meta.json"))
    if len(out) != 49:
        raise SystemExit(f"{d}: {len(out)} predicted records, not the 49")
    return out


def search_size(workload, records):
    """(queries, plans): the workload's queries and their candidate plans (bodies a plan choice picks among: measure-only
    bodies left out, and bodies not offered on the layout, whose row is written and never run), from the Sp-Sp
    store's counts"""
    from kvevaluator.plan_choice.candidates import INVALID_HERE
    want = {i["query"] for i in json.load(open(ROOT / WORKLOAD_FILES[workload]))["items"]}
    plans, srcs = set(), []
    for p in records:
        srcs.append(p.with_suffix(".csv"))
        for r in csv.DictReader(open(p.with_suffix(".csv"))):
            if r["query"] in want and r["measure_only"] != "1" and not (r.get("error") or "").startswith(INVALID_HERE):
                plans.add((r["query"], r["body"]))
    got = {q for q, _ in plans}
    if got != want:
        raise SystemExit(f"{workload}: queries {sorted(want - got)} have no plan in {srcs}")
    return len(want), len(plans), srcs


def mix_rows(tree, d, workload):
    """the find and verify rows of a workload's fewest-op-byte mix (each type its own setting, any chunk size), from its
    rows (d: its rows' dir, places' "mix"): find in the two-step's two stages (a row's times two_step_a_s and
    two_step_b_s, named by the solver that ran them), verify from solve_s, and cost_cache_s for a solve on the cost cache (the cost cache
    only the exact search needs). The file may hold one fewest row per search (results.search_of): the proven row
    (branch and bound) gives verify, and the two-step row (search-mix --search two-step), when there is one, gives
    find, as the find step's own run; with one row, that row gives both. A row with no two-step times gives the verify
    row alone, the gap named"""

    path = d / MIX_ROWS
    rs = [r for r in json.load(open(path))["rows"] if r["question"] == "fewest" and r["model"] == MIX_MODEL
          and r.get("limit") is None]
    by_search = {}
    for x in rs:
        by_search.setdefault(R.search_of(x), []).append(x)
    if not rs or any(len(v) != 1 for v in by_search.values()) or set(by_search) - {"proven", "two-step"}:
        raise SystemExit(f"{path}: fewest {MIX_MODEL} rows by search "
                         f"{ {k: len(v) for k, v in sorted(by_search.items())} }: want one row, or one proven and one two-step")
    r = (by_search.get("proven") or by_search["two-step"])[0]          # verify
    find = (by_search.get("two-step") or [r])[0]
    t, tf = r["times"], find["times"]
    no_cost_cache = r.get("solver") == "no-cost-cache"
    gap = (r.get("bound") or {}).get("gap")
    base = {"route": "mix", "workload": workload, "proven": "", "gap": "", "source": path.relative_to(tree.root)}
    out = []
    if "two_step_a_s" in tf and "two_step_b_s" in tf:
        solver = tf.get("two_step_solver") or ("no-cost-cache" if find.get("solver") == "no-cost-cache" else "cost-cache")
        (step_a, item_a), (step_b, item_b) = TWO_STEP[solver]
        out += [{**base, "step": step_a, "item": item_a, "wall_s": tf["two_step_a_s"]},
                {**base, "step": step_b, "item": item_b, "wall_s": tf["two_step_b_s"]}]
    else:
        print(f"{workload}: {path} holds no two-step times (two_step_a_s, two_step_b_s): its find rows left out")
    out.append({**base, "step": "verify", "item": "branch and bound" + ("" if no_cost_cache else " (cost cache)"),
                "proven": "yes" if r["proven"] else "no", "gap": "" if r["proven"] or gap is None else round(gap, 4),
                "wall_s": round(t["solve_s"] + (0.0 if no_cost_cache else t["cost_cache_s"]), 3)})
    return out


def compose_cost_rows(tree, p, workload):
    """the measure route's "compose and price" rows of a workload: each composed layout's record (composed/, its own
    wall_s) per family, and cost-layouts' last run on its layout-costs place (runs.jsonl, wall_s["all"]); [] and the gap
    named when the tree holds no such record"""
    out, base = [], {"route": "measure", "workload": workload, "proven": "", "gap": ""}
    for family in FAMILIES[workload]:
        counts = Path(capture_in(p["composed"], family).dir("counts"))
        recs = sorted(x for x in counts.glob("props_*.json")) if counts.is_dir() else []
        timed = [(x, json.load(open(x)).get("wall_s")) for x in recs]
        if not timed or any(not isinstance(w, (int, float)) for _, w in timed):
            print(f"{workload}: no composed records with wall_s in {counts} (kvcost compose writes them): compose left out")
            continue
        out += [{**base, "step": "compose", "item": f"{x.stem} {family}", "wall_s": float(w),
                 "source": x.relative_to(tree.root)} for x, w in timed]
    runs_file = p["layout_costs"] / "runs.jsonl"
    lines = [json.loads(x) for x in open(runs_file)] if runs_file.is_file() else []
    if lines and isinstance((lines[-1].get("wall_s") or {}).get("all"), (int, float)):
        out.append({**base, "step": "price", "item": f"{len(lines[-1].get('layouts') or [])} layouts",
                    "wall_s": float(lines[-1]["wall_s"]["all"]), "source": runs_file.relative_to(tree.root)})
    else:
        print(f"{workload}: no cost-layouts run with wall_s in {runs_file}: its cost row left out")
    return out


def places(tree, scales):
    """per workload, the places its rows read: {"captures": ..., "stores", "stats", "tallies", "predicted": [dirs],
    "mix"}; `scales`: {dataset: scale}"""
    out = {}
    for w in WORKLOADS:
        ds = DATASET[SHARED[w]]
        sc = scales[ds]
        if sc is None:
            out[w] = None
            continue
        out[w] = {"captures": tree.place_dir("captures", ds, sc), "stores": tree.place_dir("stores", ds, sc),
                  "stats": tree.stats(ds, sc) / "stats.json",
                  "tallies": U.sampled_dir(tree, ds, sc, SAMPLE) / "tally-diagonal",
                  "predicted": [Path(tree.predicted(f, sc, PREDICTED).dir("counts")) for f in FAMILIES[w]],
                  "mix": tree.summary(ds, sc, "mix-search", MIX[w]),
                  "composed": tree.place_dir("composed", ds, sc),
                  "layout_costs": tree.summary(ds, sc, "layout-costs",
                                               json.load(open(ROOT / WORKLOAD_FILES[w]))["id"], "measured")}
    return out


def missing(w, p):
    """the first place workload `w` reads (places' entry `p`) that the tree does not hold, or None"""
    need = ([p["captures"] / f for f in FAMILIES[w]] + [p["stores"], p["stats"], p["tallies"], *p["predicted"],
            p["mix"] / MIX_ROWS])
    return next((x for x in need if not x.exists()), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    a = ap.parse_args()
    tree = tree_of(a.tree)
    scales = {ds: scale_in(a, ds) for ds in DATASET.values()}   # a dataset the run leaves out: None
    where = places(tree, scales)
    kept, absent = [], []
    for w in WORKLOADS:
        if where[w] is None:
            print(f"{w}: its dataset is not in this run (--scale)")
            continue
        gone = missing(w, where[w])
        if gone is None:
            kept.append(w)
        elif w in CHAIN and gone in (where[w]["tallies"], *where[w]["predicted"]):
            absent.append(f"{w}: {gone}")       # measured here, its predicted side not: never a table without it
        else:
            print(f"{w}: not in this tree ({gone})")
    if absent:
        raise SystemExit("run_time: the tree holds the measured side but not the predicted one (the sampled tallies, "
                         f"the set {PREDICTED}), nothing written:\n  " + "\n  ".join(absent)
                         + "\n  make it: just results runtime (with the run's --dataset)")
    rows = []

    def add(route, step, workload, item, path, key="wall_s"):
        rows.append({"route": route, "step": step, "workload": workload, "item": item, "wall_s": wall(path, key),
                     "proven": "", "gap": "", "source": path.relative_to(tree.root)})

    # measure: the stores (LDBC's serve Interactive and BI), then each workload on each
    datasets = sorted({SHARED[w] for w in kept}, key=list(DATASET).index)
    first = {d: next(w for w in kept if SHARED[w] == d) for d in datasets}    # a workload of the dataset, for its places
    for d in datasets:
        for cell, p in sorted(store_manifests(where[first[d]]["stores"], SEVEN).items()):
            add("measure", "build", d, cell, p, "ingest_wall_s")
    for w in kept:
        for family in FAMILIES[w]:
            for setting, p in diagonal_records(where[w]["captures"], family).items():
                add("measure", "run", w, f"{setting} {family}", p)
    # predict: statistics and tallies per dataset, then the 49 per workload
    for d in datasets:
        add("predict", "statistics", d, "stats.json", where[first[d]]["stats"])
        for setting, p in tallies(where[first[d]]["tallies"]):
            add("predict", "tally", d, setting, p)
    for w in kept:
        for family, counts in zip(FAMILIES[w], where[w]["predicted"]):
            for p in predicted(counts):
                add("predict", "predict", w, f"{p.stem} {family}", p)
    # the search's size: queries and their candidate plans
    search_sizes = []
    for w in kept:
        recs = [Path(capture_in(where[w]["captures"], f).record("props_split_adj_split")) for f in FAMILIES[w]]
        q, n, srcs = search_size(w, recs)
        search_sizes.append({"workload": w, "queries": q, "plans": n,
                      "source": " ".join(str(s.relative_to(tree.root)) for s in srcs)})
        print(f"{w}: {q} queries, {n} plans")
    write_table("run-time-search-size", SIZE_COLUMNS, search_sizes, key=("workload",), out_dir=a.tables)
    # compose and price, beside the measure route (not in its total)
    for w in kept:
        rows += compose_cost_rows(tree, where[w], w)
    # the best mix, both stages
    for w in kept:
        rows += mix_rows(tree, where[w]["mix"], w)
    write_table("run-time", COLUMNS, rows, key=("route", "step", "workload", "item"), out_dir=a.tables)

    def total(route, workload, steps):
        return sum(r["wall_s"] for r in rows if r["route"] == route and r["step"] in steps
                   and r["workload"] in (workload, SHARED[workload]))
    nums = {}
    for w in kept:
        m, p = total("measure", w, ("build", "run")), total("predict", w, ("statistics", "tally", "predict"))
        nums |= {f"runtime.sf3.{w.lower()}.measure.min": f"{m / 60:.0f}", f"runtime.sf3.{w.lower()}.predict.min": f"{p / 60:.0f}",
                 f"runtime.sf3.{w.lower()}.ratio": f"{m / p:.1f}"}
        print(f"{w}: measure {m / 60:.1f} min, predict {p / 60:.1f} min, {m / p:.2f}x")
    for (route, step), n in sorted({(r["route"], r["step"]): 0 for r in rows}.items()):
        rs = [r for r in rows if (r["route"], r["step"]) == (route, step)]
        print(f"{route} {step}: {len(rs)} records")
    # one layout built and measured at SF3 (section 6's sentence): per diagonal layout (Sp-Sp, Co-Co, Ch64k-Ch64k at the grid's
    # 64 KB), its store's ingest and its Interactive reads, reported and held-out (kvcost measure makes both); the longest.
    # It needs the LDBC stores and the Interactive captures alone, not the rest of the Interactive rows
    one = where["Interactive"]
    if one is None:
        print("one layout at SF3: LDBC is not in this run (--scale)")
    else:
        recs = {c: [Path(capture_in(one["captures"], "interactive", ps).record(c)) for ps in (None, "held-out")]
                for c in DIAGONAL}
        gone = [p for ps in recs.values() for p in ps if not p.exists()]
        if not one["stores"].is_dir() or gone:
            print(f"one layout at SF3: not in this tree ({one['stores'] if not one['stores'].is_dir() else gone[0]})")
        else:
            ingest = {c: wall(p, "ingest_wall_s") for c, p in store_manifests(one["stores"], DIAGONAL).items()}
            reads = {c: sum(wall(p) for p in recs[c]) for c in DIAGONAL}
            build = {c: ingest[c] + reads[c] for c in DIAGONAL}
            nums |= {"runtime.sf3.ingest.min": f"{max(ingest.values()) / 60:.0f}",
                     "runtime.sf3.reads.min": f"{min(reads.values()) / 60:.0f}--{max(reads.values()) / 60:.0f}",
                     "runtime.sf3.build.min": f"{max(build.values()) / 60:.0f}"}
            for c in DIAGONAL:
                print(f"one layout at SF3, {setting_of(c)}: ingest {ingest[c] / 60:.1f} min + reads {reads[c] / 60:.1f} "
                      f"min (reported and held-out) = {build[c] / 60:.1f} min")
    write_numbers("run_time", nums, "sf3: the stores' ingest_wall_s, the run records', stats.json's, the tallies' and "
                  "the predicted records' wall_s, search-mix's times; one layout: its store's ingest and its Interactive "
                  "run records (reported and held-out)", out_dir=a.tables)


if __name__ == "__main__":
    main()
