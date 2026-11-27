#!/usr/bin/env python3
"""Latency of the grid's chosen plans: per cell and query, the plan the cell is priced at, timed by cpplatency.
The plan is the cheapest valid candidate under the named pick rule, read from a counts capture (picks.choose_cell), or
with --picks the body a picks file names (inputs/latency-picks/, written from such a capture by kvcost measure-latency
--save-picks) for every cell the file covers (file_picks). This driver picks the plans and keeps the records; the timing
and the gate are kvevaluator/measure/cell_latency.py.
Per cell and regime one cpplatency run, one cell at a time (a measure can hold tens of GB). Then the variant gate:
every (query, param)'s counts in the latency run against the counts capture's row of the chosen variant (S / G /
N, the misses and all seven byte lanes). A cell's counts are read from the capture that holds it
(output_tree.layout_capture: measured itself, else composed from the measured diagonals). A timed run whose counts
differ is kept and marked (stale kind timed_counts_differ: the cell's record, gate.csv's rows, the queries named) and
the run goes on; --check exits 1 on a difference. A cell timed from --picks is gated only when the capture holds it
(else "not gated" is said); when the capture's pick differs from the file's, the file's body is timed and the cell
marked body_moved.

    python3 -m kvcost.latency.grid_latency --sf 30 --counts output/captures/ldbc/sf30/interactive/reported [--cells a,b] [--nparam 5]
    python3 -m kvcost.latency.grid_latency --sf 30 --counts <capture> --check      # the gate over what is on disk; no store
    python3 -m kvcost.latency.grid_latency --sf 3 --counts output/captures/ldbc/sf3/interactive/reported \
        --spec-dir inputs/specs/mixes_w16 --regimes steady,warm,cold --steady 307,3 --cold 1 --root DIR   # another spec set
    python3 -m kvcost.latency.grid_latency --sf 3 --counts output/captures/ldbc/sf3/interactive/reported --cells "" \
        --engine-arms nebula,dgraph --regimes steady --steady 307,3 --root DIR   # an engine format as an arm: its own store and
        # registry pins, gated against its row in the workload's capture (--engine-counts)

Output, below the output tree --root (default the output dir):
    timings/<dataset>/<scale>/<workload>/meta-<cell>/<regime>/steps.csv (+ .meta.json)   cpplatency's own files
    summary/<dataset>/<scale>/grid-latency/<workload>/
      picks.json     cell -> query -> the variant measured, and the counts capture + pick rule it came from
      cells/<cell>.json   per regime: wall, peak RSS, the command; the gate's verdict; read_set_fits: the steady
                          regimes whose whole read set fits the bounded cache (steady = warm there), with the usage;
                          timed_counts_differ: per regime, the queries whose timed counts differ from the capture's
      gate.csv       one row per (cell, regime, query, param): the fields compared, equal or not
Markers: STAGE_DONE latency <cell> ... [MARKED timed_counts_differ: <queries>] | STAGE_FAIL latency <cell> ...
"""
import argparse
import csv
import json
import os
import sys
from kvevaluator.paths import ROOT

from kvcost.layout_cost.picks import PlanRule, choose_cell   # the pick, as plot_grid.load reads it
from kvevaluator import stale, workloads as W
from kvevaluator.capture import capture_layout
from kvevaluator.measure import cell_latency as T
from kvevaluator.output_tree import Capture, Tree
from kvevaluator.measure.grid_capture import GRID
from kvevaluator.capture.spec_sets import spec_cells
from kvevaluator.plan_choice import candidates as C

EXP = str(ROOT)


def picks_of(counts, cell, workload, criterion=None, rule=None):
    """query -> the variant to measure, by HEAD's name: the capture's pick under the rule; `(pin)` for the planner's own
    body; a pick HEAD names otherwise resolves through its rows' body_sha to the one HEAD candidate with that body (said
    on stderr). Anything else refuses: a pick timed as another body would still pass the gate, which reads the planner's
    row for `(pin)`. criterion: the pick rule (criteria.NAMED; None = the optimizer's default); rule: a PlanRule."""
    ran, planner = {}, set()
    with open(Capture(counts).path_of("counts", cell)) as f:
        for r in capture_layout.reader(f):
            if r["ok"] == "1":
                ran.setdefault((r["query"], r["body"]), set()).add(r.get("body_sha") or "")
                # the planner's row (a capture with a planner_pick column); a capture without the column: a single-plan
                # query's one body (its rows carry the plan's name, no candidate's), found as `(pin)`
                if T.is_plan_row(r, workload):
                    planner.add((r["query"], r["body"]))
    out = {}
    for q, d in choose_cell(counts, cell, criterion, rule=rule or PlanRule()).items():   # plot_grid.load's pick (it skips a query with no variant)
        if d["body"] is None:
            continue
        names = {n for n, _ in C.candidates(workload, q)[1]}
        v = d["body"]
        if v in names:
            out[q] = v
        elif (q, v) in planner:
            out[q] = "(pin)"
        else:
            # a counts row of a body rewritten count-equal since: its successor (the counts are the same)
            shas = {C.successor_sha(workload, q, s) for s in ran.get((q, v), set()) - {""}}
            head = C.candidate_bodies(workload, q)
            same = sorted(n for n in names if len(shas) == 1 and head.get(n) in shas)
            if len(same) != 1:
                raise SystemExit(f"{cell} {q}: the pick {v!r} is not a HEAD candidate nor the planner's body, and its rows' "
                                 f"body {sorted(shas)} matches {len(same)} HEAD candidates {same}: re-measure the query")
            print(f"[picks_of] {cell} {q}: the pick {v!r} is HEAD's {same[0]!r} (the same body {next(iter(shas))})",
                  file=sys.stderr)
            out[q] = same[0]
    return out


def file_picks(path, cell, workload, criterion):
    """(query -> the body the picks file names for this cell, "") when the file covers the cell: written under this pick
    rule, a body for every query of the workload, each a HEAD candidate or `(pin)`; else (None, why)"""
    if not path or not os.path.exists(path):
        return None, f"no picks file {path}" if path else "no --picks"
    d = json.load(open(path))
    if d.get("criterion") != criterion:
        return None, f"{path} was written under {d.get('criterion')}, not {criterion}"
    picks = d.get("cells", {}).get(cell)
    if picks is None:
        return None, f"{path} holds no {cell}"
    queries = C.workload_queries(workload)[0]
    gone = sorted(q for q in queries if q not in picks
                  or (picks[q] != "(pin)" and picks[q] not in {n for n, _ in C.candidates(workload, q)[1]}))
    if gone:
        return None, f"{path}: {cell} has no HEAD body for {', '.join(gone)}"
    return {q: picks[q] for q in queries}, ""


def regimes_of(a):
    """regime dir name -> cpplatency flags for this driver's flags (cell_latency.regime_flags), in the order --regimes
    names them (the defaults: warm-p0 then cold-p2)"""
    return T.regime_flags(a.regimes, warm=a.warm, cold=a.cold, steady=a.steady, steady_perf_level=a.steady_perf_level,
                          reuse_passes=a.reuse_passes, cold_breakdown=a.cold_breakdown,
                          cold_breakdown_runs=a.cold_breakdown_runs, suffix=a.suffix, steady_kv_timed=a.steady_kv_timed)


def timers_of(a):
    """{kind: passes} of the steady regime's timer captures this run makes (cell_latency.TIMER_KINDS)"""
    out = {}
    if a.steady_kvtimer_passes:
        out["kvtimer"] = a.steady_kvtimer_passes
    if a.steady_breakdown:
        out["breakdown"] = a.steady_breakdown_passes
    return out


def run_cell(a, root, cell, picks, spec_dir, regimes, fresh=False):
    """time one meta cell's picks in each regime (a regime on disk kept unless `fresh` or --fresh): (record, ok)"""
    queries = [q for q in a.queries.split(",") if q]
    cand = [f"{q}={v}" for q, v in sorted(picks.items()) if not queries or q in queries]
    recs, ok = T.time_arm(a.sf, a.workload, f"meta-{cell}", root, regimes,
                          ["--format", "meta", "--spec", os.path.join(spec_dir, f"{cell}.json"), "--arms", f"meta-{cell}"],
                          len(queries) or len(picks), a.nparam, label=cell,
                          mid_args=(["--queries", *queries] if queries else []) + (["--require-clean"] if a.require_clean else []),
                          tail_args=["--candidate-set", "grid", "--candidates", *cand], fresh=a.fresh or fresh,
                          timers=timers_of(a))
    fits = T.fits_of(root, a.sf, a.workload, f"meta-{cell}", regimes) if ok else {}   # also of a capture kept from disk
    return {"cell": cell, "sf": a.sf, "regimes": recs, "read_set_fits": fits}, ok


def run_engine_arm(a, root, fmt, regimes):
    """an engine format timed as an arm: its own store and its registry pins; the same regime flags as a cell; its
    timings beside the cells' (cell_latency.arm_dir). (record, ok)"""
    # a chunk arm's slug (dgraph-c512k: the format's store at the 512 KB posting split) names the store; the format is its base
    recs, ok = T.time_arm(a.sf, a.workload, fmt, root, regimes, ["--format", fmt.split("-")[0], "--arms", fmt],
                          len(C.workload_queries(a.workload)[0]), a.nparam,
                          tail_args=["--require-clean"] if a.require_clean else [], fresh=a.fresh,
                          timers=timers_of(a))
    return {"cell": fmt, "sf": a.sf, "engine_format": True, "regimes": recs}, ok


def build_parser():
    """the driver's flags and their defaults (check_store_config.py reads the regimes' flags through it)"""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--counts", required=True, help="the counts capture the picks and the gate read (<...>/<workload>/<params>)")
    ap.add_argument("--cells", default="", help="comma list (default: every grid cell)")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--pick-by", dest="criterion", default=None, help="the pick rule (criteria.NAMED; default the optimizer's)")
    ap.add_argument("--root", default=None, help="the output tree the timings and the records go below (default: the output dir)")
    ap.add_argument("--fresh", action="store_true", help="re-measure a (cell, regime) already on disk")
    ap.add_argument("--historical", action="store_true", help="--check against a capture made before the measure_only column / the held-out rule: read as captured, said")
    ap.add_argument("--check", action="store_true", help="only the variant gate over what is on disk (opens no store)")
    ap.add_argument("--spec-dir", default=GRID, help="the cells' spec set (default: the grid); a cell's store is meta-<stem>")
    ap.add_argument("--regimes", default="warm,cold", help="comma list of warm | cold | steady | reuse (the reuse-distance pass: "
                    "counts on the same plans, no clock; its dir is reuse-distance-p<passes>), run in this order")
    ap.add_argument("--warm", default="1,3", help="the warm regime's warmup,measured iterations")
    ap.add_argument("--cold", default="3", help="the cold regime's measured iterations")
    ap.add_argument("--steady", default=None, metavar="CACHE_MB,PASSES", help="the steady regime's bounded cache and measured passes")
    ap.add_argument("--reuse-passes", type=int, default=2, help="the reuse regime's passes (the last one is the steady stream)")
    ap.add_argument("--steady-perf-level", type=int, default=2,
                    help="the steady regime's RocksDB perf level (2: counts without per-operation timers, the walls of "
                         "record; 3: the captures in steady-<MB>mb-p3 dirs)")
    ap.add_argument("--steady-breakdown", action="store_true",
                    help="--steady-breakdown-passes more steady passes at perf 3 with the KV timer, after the measured "
                         "ones (and the kvtimer passes), in their own capture steady-<MB>mb-breakdown[suffix] (the "
                         "per-call timers: wall_lines, the latency-levels and time-split figures); gated like the wall "
                         "capture")
    ap.add_argument("--steady-breakdown-passes", type=int, default=1, metavar="N",
                    help="the breakdown capture's passes (default 1); a cell's row is its median run")
    ap.add_argument("--steady-kvtimer-passes", type=int, default=0, metavar="N",
                    help="N steady passes at perf 2 with the KV timer after the measured ones (default 0: none), in their "
                         "own capture steady-<MB>mb-kvtimer[suffix]: the time inside RocksDB calls with no PerfContext "
                         "timer inside it; gated like the wall capture")
    ap.add_argument("--steady-kv-timed", action="store_true",
                    help="the KV timer in every steady pass (cpplatency --kv-timing steady), in the wall capture's own "
                         "dir steady-<MB>mb-p<L>-kvtimed[suffix]: the RocksDB share from the measured passes themselves. "
                         "Each row carries its timer calls and the meta the timer pair's cost (calls x timer_pair_ns, to "
                         "subtract). Refused with --steady-kvtimer-passes (it replaces them); --steady-breakdown stays")
    ap.add_argument("--cold-breakdown", action="store_true",
                    help="off by default (for investigating): after cold-p2, one more cold run at perf 3 with the KV timer "
                         "and the same direct I/O, declared a breakdown pass, in its own capture cold-breakdown[suffix] "
                         "(the RocksDB time inside a cold wall: w16_pass_through's pc and kv columns)")
    ap.add_argument("--cold-breakdown-runs", default=None, metavar="N",
                    help="the cold breakdown pass's runs per param (default: --cold's). A breakdown splits a wall, it is "
                         "not a wall of record, so one run may do (the sf3 re-time: 1 for BI, whose walls take 3)")
    ap.add_argument("--suffix", default="", help="appended to every regime dir (e.g. -repeat: a second copy beside the first)")
    ap.add_argument("--require-clean", action="store_true", help="passed to cpplatency: refuse a dirty driver tree")
    ap.add_argument("--family", dest="workload", default="interactive", choices=W.names(kind="read"),
                    help="a read workload of any dataset (the workload manifests): its counts capture (--counts: for bi / "
                         "microbench the grid capture's bi/ or microbench/ dir; job: an IMDb capture, --sf the tag), its "
                         "candidates and its own <out>/<workload>/ dir")
    ap.add_argument("--queries", default="", help="comma list: time only these queries (the ic10-bodies stage, a kvcost run's "
                    "queries); the counts capture needs a pick for these only, and the gate covers them only")
    ap.add_argument("--in-sample", action="store_true", help="the plan rule's in-sample setting (picks.PlanRule(in_sample=True)): "
                    "a cell with no pick-record row, such as a run's new mix, is picked by its own capture (kvcost "
                    "measure-latency, as check-mix)")
    ap.add_argument("--picks", default=None, metavar="FILE", help="the body each cell times per query from this picks file "
                    "(inputs/latency-picks/<dataset>/<scale>/<family>.json) for every cell it covers; the others are "
                    "picked from --counts. The gate runs where --counts holds the cell")
    ap.add_argument("--pick", action="append", default=[], metavar="Q=BODY", help="time this body instead of the pick (the ic10-bodies stage)")
    ap.add_argument("--engine-arms", default="", help="comma list of engine FORMATS timed as arms on their own stores with their "
                    "registry pins (nebula, dgraph); with --cells '' no cell runs")
    ap.add_argument("--engine-counts", default=None, help="the capture the engine arms' gate reads (default this scale's "
                    "capture of the workload, which holds the named formats)")
    return ap


def as_timed(picks, timed):
    """(the picks to gate, the queries whose pick moved): a query whose pick moved since its timing (`timed`, the cell's
    picks.json entry; None: never timed) is gated against the body that was timed, and listed"""
    if timed is None:
        return picks, []
    moved = sorted(q for q in picks if timed.get(q, picks[q]) != picks[q])
    return {q: timed.get(q, v) for q, v in picks.items()}, moved


def gate_verdict(ok, compared, differ, check, missing=0, gated=True):
    """a cell's verdict after its gate: "fail" when a regime failed to run, a gated cell compared nothing or a (query,
    param) the counts hold is `missing` from the latency file (a short timing), and under --check (a check's verdict is
    its exit code) when counts differ; "marked" when a measuring run's counts differ (its timing kept, noted
    timed_counts_differ, the run goes on); else "done". gated False: no counts capture holds the cell (timed from
    --picks), so nothing is compared"""
    if not ok or (gated and compared == 0) or missing or (differ and check):
        return "fail"
    return "marked" if differ else "done"


def main():
    a = build_parser().parse_args()
    wl = a.workload
    if wl != "interactive" and a.engine_arms:
        raise SystemExit("--engine-arms time an engine format on its LDBC registry pins: not built for another workload")
    if a.steady_kv_timed and a.steady_kvtimer_passes:
        raise SystemExit("--steady-kv-timed times the measured passes with the KV timer: no --steady-kvtimer-passes beside it")
    rule = PlanRule(historical=bool(a.historical), in_sample=True if a.in_sample else None)
    spec_dir = os.path.abspath(a.spec_dir)
    regimes = regimes_of(a)
    counts = os.path.abspath(a.counts)
    root = os.path.abspath(a.root) if a.root else None
    out = str(Tree(root).summary(W.get(wl).dataset, a.sf, "grid-latency", wl))
    from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, resolve_criterion
    crit = resolve_criterion(a.criterion or DEFAULT_CRITERION)[0]
    engine_arms = [f for f in a.engine_arms.split(",") if f]
    engine_counts = os.path.abspath(a.engine_counts or Tree().capture(wl, a.sf).path) if engine_arms else None
    cells = [c for c in a.cells.split(",") if c] or ([] if engine_arms else spec_cells(spec_dir))
    os.makedirs(os.path.join(out, "cells"), exist_ok=True)
    ppath = os.path.join(out, "picks.json")
    allp = json.load(open(ppath)) if os.path.exists(ppath) else {"counts_capture": os.path.relpath(counts, EXP), "criterion": crit, "cells": {}}
    # a measuring run keeps ONE counts capture per output root; --check may read the same rows from another copy
    # of that capture (the working dir on the measuring machine, a copy elsewhere)
    if allp["criterion"] != crit or (not a.check and allp["counts_capture"] != os.path.relpath(counts, EXP)):
        raise SystemExit(f"{ppath} was written for {allp['counts_capture']} / {allp['criterion']}: another identity needs another --root")
    failed, marked, gate_rows = [], [], []
    for cell in cells:
        with stale.scope():        # this cell's notes go to its own record
            held = Capture(counts).of_layout(cell).path      # the capture that holds the cell: measured itself, else composed
            has_counts = os.path.exists(Capture(held).path_of("counts", cell))
            mine, why = (None, "--check") if a.check else file_picks(a.picks, cell, wl, crit)
            if mine is None and not has_counts:
                raise SystemExit(f"{cell}: no pick: {why}, and no counts in {held} (measure or compose it first)")
            picks = picks_of(held, cell, wl, a.criterion, rule) if mine is None else mine
            moved = []                                       # the queries whose gate rows are marked body_moved
            if mine is not None and has_counts:          # both: the file's body is timed; a counts' pick that differs is said
                try:
                    theirs = picks_of(held, cell, wl, a.criterion, rule)
                except SystemExit as e:              # a capture the pick rule cannot read: nothing to compare, said
                    theirs = {}
                    print(f"[grid_latency] {cell}: the counts' pick not compared with the file's: {e}", flush=True)
                moved = sorted(q for q in mine if q in theirs and theirs[q] != mine[q])
                if moved:
                    stale.note("body_moved", f"{cell}: the counts' pick ({os.path.relpath(held)}) differs from "
                               f"{os.path.relpath(a.picks)} on {', '.join(moved)}: the file's bodies timed; to "
                               f"time the counts' pick, rewrite the file (kvcost measure-latency ... --save-picks)")
            for pk in a.pick:                                   # the ic10-bodies stage: a named body instead of the pick
                q, body = pk.split("=", 1)
                picks[q] = body
            only = [q for q in a.queries.split(",") if q]
            if only:                                         # a query subset (a kvcost run's queries): their picks only
                missing = sorted(set(only) - set(picks))
                if missing:
                    raise SystemExit(f"{cell}: the counts capture gives no pick for {', '.join(missing)}")
                picks = {q: v for q, v in picks.items() if q in only}
            else:
                nq = len(C.workload_queries(wl)[0])          # the workload's queries (LDBC: 19)
                if len(picks) != nq:
                    raise SystemExit(f"{cell}: the counts capture gives a pick for {len(picks)} of {nq} queries")
            if a.check:
                picks, moved = as_timed(picks, allp["cells"].get(cell))
                if moved:
                    stale.note("body_moved", f"{cell}: the picks from {counts} moved since the timing ({ppath}) for "
                               f"{', '.join(moved)}: gated as timed, those rows marked")
            else:                # a body other than the one timed on disk: its timing is made again (as_timed's rule)
                retime = as_timed(picks, allp["cells"].get(cell))[1]
                if retime:
                    print(f"[grid_latency] {cell}: timed again, its body moved since its timing on {', '.join(retime)}",
                          flush=True)
                allp["cells"][cell] = picks
                json.dump(allp, open(ppath, "w"), indent=1, sort_keys=True)
            ok = True
            if not a.check:
                forced = {q: v for q, v in picks.items() if v != "(pin)"}
                print(f"##### latency {cell} sf{a.sf}: {len(picks)} queries, {len(forced)} with a named variant; pick rule {crit}", flush=True)
                rec, ok = run_cell(a, root, cell, picks, spec_dir, regimes, fresh=bool(retime))
            rows, n, bad = (T.gate(root, a.sf, held, cell, picks, a.nparam, T.gated_regimes(regimes, timers_of(a)), wl,
                                   set(q for q in a.queries.split(",") if q) or None) if has_counts else ([], 0, 0))
            differ = sorted({r["query"] for r in rows if not r["equal"]})
            missing = sum(r["differing"].startswith(T.MISSING) for r in rows)
            end = gate_verdict(ok, n, bad, a.check, missing, has_counts)
            by_regime = {}                              # regime -> the queries whose counts differ, for the record
            for r in rows:
                if not r["equal"] and r["query"] not in by_regime.setdefault(r["regime"], []):
                    by_regime[r["regime"]].append(r["query"])
            if end == "marked":      # kept and marked: the record written below folds the note in
                stale.note("timed_counts_differ", f"{cell}: the timed run's counts differ from the counts capture's row "
                           f"of the picked plan ({os.path.relpath(held)}) on {bad} of {n} (query, param, regime) cells: "
                           f"{', '.join(differ)}")
            for r in rows:
                r["stale"] = stale.column((["body_moved"] if r["query"] in moved else [])
                                          + ([] if r["equal"] else ["timed_counts_differ"]))
            gate_rows += rows
            verdict = (f"gate {n - bad}/{n} (query, param, regime) cells equal on {len(T.GATE)} fields ({len(T.REUSE_GATE)} for a reuse pass)"
                       if has_counts else f"not gated: no counts of {cell} in {os.path.relpath(held)} (picks from {os.path.relpath(a.picks)})")
            if not a.check:
                rec["gate"] = {"compared": n, "differ": bad}
                cpath = os.path.join(out, "cells", f"{cell}.json")
                prev = json.load(open(cpath)) if os.path.exists(cpath) else {"regimes": {}}
                rec["regimes"] = {**prev.get("regimes", {}), **rec["regimes"]}       # a regime measured earlier keeps its record
                gated = T.gated_regimes(regimes, timers_of(a))               # this call's regimes read again, the others kept
                rec["read_set_fits"] = {**{r: f for r, f in prev.get("read_set_fits", {}).items() if r not in regimes},
                                        **rec["read_set_fits"]}
                rec["timed_counts_differ"] = {**{r: q for r, q in prev.get("timed_counts_differ", {}).items() if r not in gated},
                                              **by_regime}
                json.dump(stale.fold(rec, out, unit=cell), open(cpath, "w"), indent=1)   # + a driver's notes: the dir's, this cell's
            if end == "fail":
                failed.append(cell)
                print(f"STAGE_FAIL latency {cell} sf{a.sf}: {verdict}" + ("" if ok else "; a regime failed")
                      + (f"; {missing} (query, param, regime) cells missing in the latency file" if missing else ""), flush=True)
            else:
                marked += [cell] if end == "marked" else []
                print(f"STAGE_DONE latency {cell} sf{a.sf}: {verdict}"
                      + (f"; MARKED timed_counts_differ: {', '.join(differ)}" if end == "marked" else ""), flush=True)
            for r in [r for r in rows if not r["equal"]][:5]:
                print(f"     {r['regime']} {r['query']} p{r['param_idx']} ({r['body']}): {r['differing']}", flush=True)
            if cell in failed and not a.check:
                break                                      # a failed regime (or nothing compared) stops the lane
    for fmt in engine_arms:
        if failed:
            break
        with stale.scope():        # this arm's notes go to its own record
            if not a.check:
                allp.setdefault("engine_arms", {})[fmt] = {"plans": "pins", "counts_capture": os.path.relpath(engine_counts, EXP)}
                json.dump(allp, open(ppath, "w"), indent=1, sort_keys=True)
                print(f"##### latency {fmt} sf{a.sf}: an engine format on its own store, its registry pins", flush=True)
                rec, ok = run_engine_arm(a, root, fmt, regimes)
            else:
                ok = True
            rows, n, bad = T.engine_gate(root, engine_counts, fmt, a.nparam, T.gated_regimes(regimes, timers_of(a)), a.sf, wl)
            for r in rows:
                r["stale"] = ""
            gate_rows += rows
            verdict = f"gate {n - bad}/{n} (query, param, regime) cells equal on {len(T.GATE)} fields ({len(T.REUSE_GATE)} for a reuse pass) (step sums vs the formats capture)"
            if not a.check:
                rec["gate"] = {"compared": n, "differ": bad}
                cpath = os.path.join(out, "cells", f"{fmt}.json")
                prev = json.load(open(cpath)) if os.path.exists(cpath) else {"regimes": {}}
                rec["regimes"] = {**prev.get("regimes", {}), **rec["regimes"]}
                json.dump(stale.fold(rec, out, unit=fmt), open(cpath, "w"), indent=1)
            if not ok or bad or n == 0:
                failed.append(fmt)
                print(f"STAGE_FAIL latency {fmt} sf{a.sf}: {verdict}" + ("" if ok else "; a regime failed"), flush=True)
                for r in [r for r in rows if not r["equal"]][:5]:
                    print(f"     {r['regime']} {r['query']} p{r['param_idx']}: {r['differing']}", flush=True)
            else:
                print(f"STAGE_DONE latency {fmt} sf{a.sf}: {verdict}", flush=True)
    cells = cells + engine_arms
    # the gate file holds every cell on disk, not only this invocation's
    gpath = os.path.join(out, "gate.csv")
    old = [r for r in capture_layout.reader(open(gpath))] if os.path.exists(gpath) else []
    mine = {(r["cell"], r["regime"]) for r in gate_rows}      # replace per (cell, regime): a later regime's run keeps the earlier's rows
    out_rows = [r for r in old if (r["cell"], r["regime"]) not in mine] + gate_rows
    out_rows.sort(key=lambda r: (r["cell"], r["regime"], r["query"], int(r["param_idx"])))
    if out_rows:
        cols = list(out_rows[0]) + [k for k in ("stale",) if k not in out_rows[0]]   # a gate.csv from before the column
        with open(gpath, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, restval="")
            w.writeheader()
            w.writerows(out_rows)
    print(f"cells: {len(cells)} asked, {len(cells) - len(failed)} passed" + (f", FAILED {failed}" if failed else "")
          + (f", MARKED timed_counts_differ {marked}" if marked else "") + f"; gate.csv holds {len(out_rows)} rows", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
