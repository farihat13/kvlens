#!/usr/bin/env python3
"""kvcost measure-latency's own rules, on planted files (no store, no binary): the aggregation (per (query, param) the
mean over the measured iterations from the second, per query the mean over params, the total at the workload's frequencies),
the raw files it reads in the output tree's timings/ (warm.json's wall_ns_all, steady.json's measured runs), the
summary's accumulation per (layout, regime, query), the answer's ratio in words whatever the argument order, the one
timing rule's steps (each layout counted, then timed steady by run/latency-cells.sh; --no-sudo without sudo;
--drift-control, --cache-mb, --passes; --regime warm, timed warm by latency-cells.sh), the output dir (KVEVAL_OUTPUT_DIR) taking every file the command writes and
summary.csv's machine column, and grid_latency's default plan rule unchanged by --in-sample.

Run:  python3 tests/unit/check_measure_latency.py
"""
import csv
import json
import os
import sys
import tempfile
import types

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def plant(out, regime_dir, cell, q, body, idx, payload, name):
    """a raw file where grid_latency's cpplatency run writes it in the output tree `out`: timings/ldbc/sf0.1/interactive/
    meta-<cell>/<regime dir>/ is cpplatency's --out (cell_latency.arm_dir), and the file's path below it comes from
    cpplatency's own raw_path (body None: a one-body run's, the shape every regime writes; a body name: the
    --compare-bodies shape, planted where it does not belong)"""
    from kvevaluator.measure.latency_raw_files import raw_path
    from kvevaluator.output_tree import Tree, timing_in
    arm = timing_in(Tree(out).timings("interactive", "0.1"), f"meta-{cell}", regime_dir)
    path = raw_path(str(arm), f"meta-{cell}", q, idx, name[:-len(".json")], body=body)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(payload, open(path, "w"))


def main():
    tmp = tempfile.mkdtemp(prefix="measure-latency-")
    os.environ["KVEVAL_OUTPUT_DIR"] = os.path.join(tmp, "output")
    from kvcost.cli import latency as LT

    # the aggregation, by hand (steady_table's rule: the first iteration dropped, the mean over the rest, then the mean
    # over params): q1's per-param means 55 (20, 90), 45 (40, 50), 120 (110, 130) -> 73.33 (the median of the per-param
    # medians, 55, beside it); q2's 200 (300, 100); total 2 x 73.33 + 1 x 200
    walls = {"q1": {"1": [10, 20, 90], "2": [40, 40, 50], "3": [120, 110, 130]}, "q2": {"1": [100, 300, 100]}}
    total, per = LT.aggregate(walls, {"q1": 2.0, "q2": 1.0}, "steady")
    case("steady: per (query, param) the mean over the iterations from the second, then the MEAN over params",
         abs(per["q1"][0] - 220 / 3) < 1e-9 and per["q2"][0] == 200, per)
    case("the workload's total at its frequencies", abs(total - (2 * 220 / 3 + 200)) < 1e-9, total)
    case("the median over params and the spread kept beside it",
         per["q1"][1] == {"median_ns": 55, "params": 3, "iterations": 6, "min_ns": 20, "p90_ns": 130, "max_ns": 130},
         per["q1"][1])
    # warm and cold: every iteration kept, the median per param, then the mean over params: q1's
    # medians 20, 40, 120 -> 60; q2's 100; total 2 x 60 + 100
    total, per = LT.aggregate(walls, {"q1": 2.0, "q2": 1.0}, "warm")
    case("warm: per (query, param) the median over every iteration, then the MEAN over params",
         per["q1"][0] == 60 and per["q2"][0] == 100 and total == 220 and per["q1"][1]["iterations"] == 9, (total, per))

    from kvevaluator.output_tree import Tree
    out = os.path.join(tmp, "lat")
    plant(out, "warm-p0", "c", "q1", None, 1, {"totals": {"wall_ns_all": [5, 7, 6]}}, "warm.json")
    plant(out, "warm-p0", "c", "q1", None, 2, {"totals": {"wall_ns_all": [9, 8, 9]}}, "warm.json")
    plant(out, "steady-64mb-p2", "c", "q1", None, 1, {"measured": [{"totals": {"wall_ns": 3}}, {"totals": {"wall_ns": 4}}]},
          "steady.json")      # steady writes no candidate level (cpplatency run_steady)
    got = LT.raw_walls(Tree(out), "0.1", "interactive", "warm-p0", "c")
    case("warm.json: wall_ns_all per param", got == {"q1": {"1": [5.0, 7.0, 6.0], "2": [9.0, 8.0, 9.0]}}, got)
    got = LT.raw_walls(Tree(out), "0.1", "interactive", "steady-64mb-p2", "c")
    case("steady.json: each measured run's wall", got == {"q1": {"1": [3.0, 4.0]}}, got)
    plant(out, "cold-p3", "c", "q1", None, 1, {"totals": {"wall_ns_all": [99]}}, "cold.json")
    plant(out, "steady-64mb-breakdown", "c", "q1", None, 1, {"measured": [{"totals": {"wall_ns": 99}}]}, "steady.json")
    case("only the default perf level's dir is read: cold-p2, never cold-p3 or a breakdown pass",
         LT.raw_walls(Tree(out), "0.1", "interactive", LT.regime_dir("cold", None), "c") == {} and LT.regime_dir("cold", None) == "cold-p2"
         and LT.raw_walls(Tree(out), "0.1", "interactive", LT.regime_dir("steady", 64), "c") == {"q1": {"1": [3.0, 4.0]}})
    plant(out, "warm-p0", "c", "q2", None, 1, {"totals": {"wall_ns_all": [1]}}, "warm.json")
    plant(out, "warm-p0", "c", "q2", "other", 1, {"totals": {"wall_ns_all": [2]}}, "warm.json")
    try:
        LT.raw_walls(Tree(out), "0.1", "interactive", "warm-p0", "c")
        refused = ""
    except SystemExit as e:
        refused = str(e)
    case("a body folder in a one-body run (a second body) is refused, not mixed", "other shape" in refused,
         refused or "no refusal")

    path = os.path.join(tmp, "summary.csv")
    row = lambda lay, q, m: {"layout": lay, "cell": "c", "regime": "warm", "regime_dir": "warm-p0", "query": q, "frequency": 1, "mean_ns": m,
                             "median_ns": m, "params": 1, "iterations": 1, "min_ns": m, "p90_ns": m, "max_ns": m}
    LT.write_summary(path, [row("A", "q1", 1), row("B", "q1", 2)], {("A", "warm"), ("B", "warm")})
    LT.write_summary(path, [row("A", "q1", 5)], {("A", "warm")})
    got = sorted((r["layout"], r["mean_ns"]) for r in csv.DictReader(open(path)))
    case("summary.csv: a layout timed again replaces its rows, the others kept", got == [("A", "5"), ("B", "2")], got)
    steady = dict(row("A", "q1", 7), regime="steady", regime_dir="steady-64mb-p2")
    LT.write_summary(path, [steady], {("A", "steady")})
    got = sorted((r["layout"], r["regime"], r["mean_ns"]) for r in csv.DictReader(open(path)))
    case("summary.csv: another regime accumulates beside the first",
         got == [("A", "steady", "7"), ("A", "warm", "5"), ("B", "warm", "2")], got)

    t1 = LT.ratio_text({"mix m": 1.62e6, "Co-Ch64k": 1.67e6})
    t2 = LT.ratio_text({"Co-Ch64k": 1.67e6, "mix m": 1.62e6})
    case("the ratio in words: the mix over the uniform layout, in either argument order",
         "the mix m takes 0.97x Co-Ch64k's time" in t1 and "the mix m takes 0.97x Co-Ch64k's time" in t2, (t1, t2))
    t3 = LT.ratio_text({"mix m": 1.0e6, "Co-Ch64k": 2.0e6, "Co-Co": 1.6e6})
    case("... over the fastest uniform layout named", "the mix m takes 0.62x Co-Co's time" in t3, t3)
    t4 = LT.ratio_text({"Co-Ch64k": 2.0e6, "Co-Co": 1.0e6})
    case("no mix: each layout named over the first", "Co-Co takes 0.50x Co-Ch64k's time" in t4, t4)

    # the one rule (paper_steps): each layout's counts first (kvcost measure on the diagonals, kvcost compose for a
    # layout off the diagonal, its store built by kvcost build-store), then timed steady at store_config's cache by
    # run/latency-cells.sh; without sudo the same steps with --no-sudo
    import argparse
    w = {"id": "w", "items": [("interactive", "is1", 2.0, None), ("interactive", "is2", 1.0, None)]}
    LT.read_and_write = lambda ds, sf, wl: (w, "interactive", [])

    def ns(**k):
        base = dict(workload="w.json", dataset="ldbc", sf="0.1", layouts=["Co-Ch64k"], regime="steady", cache_mb=None,
                    passes=None, kv_timer_passes=None, nparam=None, fresh=False, verbose=False, dry_run=False,
                    drift_control=None, rocksdb_share=False, rocksdb_share_weights=None, param_set=None,
                    validate_count=False, save_picks=False, from_dir=None)
        return argparse.Namespace(**{**base, **k})
    picks_path = __import__("pathlib").Path(tmp) / "latency-picks" / "interactive.json"   # no file: every layout counted
    LT.picks_file = lambda a, fam: picks_path
    steps, sets, fam, _ = LT.paper_steps(ns())
    names = [n for n, _ in steps]
    by_name = dict(steps)
    measure, compose, build = (by_name.get(f"interactive {s}", []) for s in ("counts", "compose", "stores"))
    cells = next((c for n, c in steps if c[0] == "run/latency-cells.sh"), [])
    mb = LT.steady_setting(ns(), "ldbc", "0.1")[0]
    case("paper_steps: a layout off the diagonal is composed (kvcost measure on its two diagonals, kvcost compose on it), "
         "its store built, then timed steady by latency-cells.sh",
         sets == [(LT.GRID_DIR, ["props_condensed_adj_chunked"])] and fam == "interactive"
         and names == ["interactive counts", "interactive compose", "interactive stores", "interactive steady grid"]
         and "measure" in measure and "Co-Ch64k" not in measure and measure[measure.index("w.json") + 1:][:2] == ["Co-Co", "Ch64k-Ch64k"]
         and compose[3:] == ["compose", "--dataset", "ldbc=0.1", "Co-Ch64k"]
         and build[3:] == ["build-store", "--dataset", "ldbc=0.1", "Co-Ch64k"]
         and cells[cells.index("--steady") + 1] == mb and cells[cells.index("--queries") + 1] == "is1,is2"
         and cells[cells.index("--regimes") + 1] == "steady" and "--no-sudo" not in cells, steps)
    case("steady_setting: 5 timed passes and no KV-timer passes at SF3, 3 below; --rocksdb-share adds as many KV-timer "
         "passes as the passes, --kv-timer-passes sets them",
         LT.steady_setting(ns(), "ldbc", "3")[0].endswith(",5") and LT.steady_setting(ns(), "ldbc", "3")[1] == "0"
         and mb.endswith(",3") and LT.steady_setting(ns(), "ldbc", "0.1")[1] == "0"
         and LT.steady_setting(ns(rocksdb_share=True), "ldbc", "3")[1] == "5"
         and LT.steady_setting(ns(rocksdb_share=True, kv_timer_passes=2), "ldbc", "3")[1] == "2"
         and cells[cells.index("--kvtimer-passes") + 1] == "0",
         [LT.steady_setting(ns(), "ldbc", "3"), LT.steady_setting(ns(rocksdb_share=True), "ldbc", "3"), cells])
    # a steady store whose reads all fit the cache is recorded, never refused (cell_latency.check_passes, read_set_fits):
    # no flag on any timing step, and no manifest key
    from kvevaluator.measure import cell_latency as CL
    from kvevaluator import workloads as WL
    case("no --may-fit on any step, no steady_may_fit in a workload's manifest",
         not any("--may-fit" in c for _, c in LT.paper_steps(ns(drift_control="Co-Ch64k"))[0])
         and not any(hasattr(WL.get(n), "steady_may_fit") for n in WL.names()))

    def capture(name, rates, cells=2, usage=None):
        d = os.path.join(tmp, "fit", name)
        os.makedirs(d)
        with open(os.path.join(d, "steps.csv"), "w") as f:
            f.write("query,param_idx,step\nq1,1,__plan__\nq1,2,__plan__\n")
        with open(os.path.join(d, "steady-passes.csv"), "w") as f:
            f.write("pass,phase,cells,hit_rate,direct_io\n" + "".join(
                f"{i},{'warm' if i == 0 else 'measured'},{cells},{r},1\n" for i, r in enumerate(rates)))
        steady = {"cache_usage": {"arm": usage}} if usage else {}
        json.dump({"regimes": {"steady": steady}}, open(os.path.join(d, "steps.csv.meta.json"), "w"))
        return d
    MB = 1 << 20
    fit_u = {"capacity_bytes": 307 * MB, "usage_bytes": 240 * MB, "inserted_bytes": 235 * MB, "nothing_evicted": True}
    full_u = {"capacity_bytes": 307 * MB, "usage_bytes": 306 * MB, "inserted_bytes": 900 * MB, "nothing_evicted": False}
    fits = capture("fits", [0.87, 1.0, 1.0], usage=fit_u)
    below = capture("below", [0.5, 0.8, 0.8], usage=full_u)
    old = capture("no-usage", [0.9, 1.0, 1.0])
    old_below = capture("no-usage-below", [0.5, 0.8, 0.8])
    short = capture("short", [0.87, 1.0, 1.0], cells=1, usage=fit_u)
    why = [CL.check_passes(d, "steady-307mb-p2", 1, 2) for d in (fits, below, old, old_below, short)]
    case("check_passes: a pass at a hit rate of 1.0 passes (with or without a recorded usage), as one below 1.0; a "
         "pass with fewer cells than expected refuses",
         why[:4] == [""] * 4 and "cells != expected" in why[4], why)
    got = [CL.read_set_fits(d, "307") for d in (fits, below, old, old_below)]
    fit0 = {"usage_bytes": 240 * MB, "capacity_bytes": 307 * MB, "inserted_bytes": 235 * MB}
    case("read_set_fits: the usage below the capacity with nothing evicted is a fit (the usage kept); a full cache is "
         "none; with no usage recorded, every measured pass at 1.0 is a fit",
         got == [(fit0, ""), (None, ""), ({"hit_rate": 1.0}, ""), (None, "")], got)
    full_hits = capture("full-at-1", [0.9, 1.0, 1.0], usage=full_u)
    other = [CL.read_set_fits(fits, "614"), CL.read_set_fits(full_hits, "307"), CL.read_set_fits(fits)]
    case("... the store's cache capacity must be the regime's: another capacity is no fit, said; every measured pass "
         "at 1.0 with a full cache is no fit, said; no size given: the usage alone",
         other[0][0] is None and "is not the" in other[0][1] and other[1][0] is None and "nothing evicted False" in other[1][1]
         and other[2] == (fit0, ""), other)
    json.dump({"regimes": {"steady-307mb-p2": {}}, "read_set_fits": {"steady-307mb-p2": fit0},
               "timed_counts_differ": {"steady-307mb-p2": ["q1"], "steady-307mb-p2-control": []},
               "stale": [{"kind": "timed_counts_differ", "detail": "x: differ on q1"}, {"kind": "foreign_load", "detail": "y"}]},
              open(os.path.join(tmp, "rec.json"), "w"))
    tree = types.SimpleNamespace(summary=lambda *a: __import__("pathlib").Path(tmp))
    os.makedirs(os.path.join(tmp, "cells"))
    os.replace(os.path.join(tmp, "rec.json"), os.path.join(tmp, "cells", "c.json"))
    case("timing_marks: the regime's queries whose timed counts differ and its fit, from grid_latency's record; none "
         "for another regime or without a record",
         LT.timing_marks(tree, "ldbc", "3", "interactive", "c", "steady-307mb-p2") == (["q1"], fit0, False)
         and LT.timing_marks(tree, "ldbc", "3", "interactive", "c", "steady-307mb-p2-control") == ([], None, False)
         and LT.timing_marks(tree, "ldbc", "3", "interactive", "c", "warm-p0") == ([], None, False)
         and LT.timing_marks(tree, "ldbc", "3", "interactive", "other", "steady-307mb-p2") == ([], None, False))
    json.dump({"regimes": {}, "stale": [{"kind": "body_moved", "detail": "c: the counts' pick differs"}]},
              open(os.path.join(tmp, "cells", "m.json"), "w"))
    case("... and a cell whose counts' pick differs from the picks file's (body_moved)",
         LT.timing_marks(tree, "ldbc", "3", "interactive", "m", "steady-307mb-p2")[2] is True)
    got = LT.counted_and_composed([LT.L.cell_of(x) for x in ("Sp-Sp", "Sp-Co", "Ch1k-Ch4k", "Co-Co", "Ch4k-Ch16k")])
    case("counted_and_composed: a diagonal is measured itself, any other layout composed from its two diagonals",
         [[LT.L.name_of(c) for c in x] for x in got] == [["Sp-Sp", "Co-Co", "Ch1k-Ch1k", "Ch4k-Ch4k", "Ch16k-Ch16k"],
                                                         ["Sp-Co", "Ch1k-Ch4k", "Ch4k-Ch16k"]], got)
    steps, *_ = LT.paper_steps(ns(layouts=["Sp-Sp", "Co-Co"]))
    case("... diagonals only: one counts step, nothing composed, no store step (measure builds a measured layout's store)",
         [n for n, _ in steps] == ["interactive counts", "interactive steady grid"], [n for n, _ in steps])
    # a composed layout's store is built only when a timing will be made on it: none on disk, or one the run would not
    # keep as it is (another protocol; a KV-timer capture this run makes that is not beside it)
    real_mismatch, mixed = CL.protocol_mismatch, []
    CL.protocol_mismatch = lambda captures: (captures[0][0], captures[0][1], "another protocol") if mixed else None
    co_ch = LT.L.cell_of("Co-Ch64k")
    timed_dir = LT.timing_in(Tree().timings("interactive", "0.1"), f"meta-{co_ch}", LT.regime_dir("steady", mb.split(",")[0]))
    os.makedirs(str(timed_dir) + "-control")
    os.makedirs(timed_dir)
    open(os.path.join(timed_dir, "steps.csv"), "w").close()
    on_disk = [[n for n, _ in LT.paper_steps(ns(**k))[0]] for k in ({}, {"fresh": True}, {"drift_control": "Co-Ch64k"})]
    open(os.path.join(str(timed_dir) + "-control", "steps.csv"), "w").close()
    on_disk.append([n for n, _ in LT.paper_steps(ns(drift_control="Co-Ch64k"))[0]])
    on_disk.append([n for n, _ in LT.paper_steps(ns(kv_timer_passes=2))[0]])        # no kvtimer capture beside the walls
    mixed.append(1)
    on_disk.append([n for n, _ in LT.paper_steps(ns())[0]])                         # timed under another protocol
    CL.protocol_mismatch = real_mismatch
    os.remove(os.path.join(timed_dir, "steps.csv"))
    os.remove(os.path.join(str(timed_dir) + "-control", "steps.csv"))
    case("stores_to_build: no store step when the layout's timing is on disk and kept; --fresh, a drift control not yet "
         "timed, a KV-timer capture to make, or a timing under another protocol builds it",
         ["interactive stores" in x for x in on_disk] == [False, True, True, False, True, True], on_disk)
    steps, *_ = LT.paper_steps(ns(), sudo=False)
    case("... without sudo: the same steps, latency-cells.sh with --no-sudo",
         [n for n, _ in steps] == names and all("--no-sudo" in c for _, c in steps if c[0] == "run/latency-cells.sh"), steps)
    steps, *_ = LT.paper_steps(ns(drift_control="Sp-Co", cache_mb=64, passes=4))
    last = steps[-1][1]
    case("--drift-control LAYOUT: that store timed again at the end, -control; --cache-mb and --passes set the steady run",
         steps[-1][0] == "interactive steady drift control" and last[last.index("--cells") + 1] == "props_split_adj_condensed"
         and "-control" in last and all(c[c.index("--steady") + 1] == "64,4" for _, c in steps
                                        if c[0] == "run/latency-cells.sh"), steps)

    # the picks file: a layout it covers is timed from it (--picks), with no counts step and its store built; under
    # --validate-count it is counted too; --save-picks counts and times nothing
    from kvcost.latency import grid_latency as GLM
    from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, resolve_criterion
    picks_path.parent.mkdir(parents=True, exist_ok=True)
    every = GLM.C.workload_queries("interactive")[0]
    json.dump({"criterion": resolve_criterion(DEFAULT_CRITERION)[0], "counts_capture": "x",
               "cells": {"props_condensed_adj_chunked": {q: "(pin)" for q in every}}}, open(picks_path, "w"))
    got = {tuple(k): LT.paper_steps(ns(**k))[0] for k in ({}, {"validate_count": True}, {"save_picks": True})}
    got = {k: ([n for n, _ in v], [c for _, c in v if c[0] == "run/latency-cells.sh"]) for k, v in got.items()}
    default, validate, save = got[()], got[("validate_count",)], got[("save_picks",)]
    case("the picks file: a layout it covers is not counted, its store is built and timed with --picks; "
         "--validate-count counts it too; --save-picks counts and times nothing",
         default[0] == ["interactive stores", "interactive steady grid"] and "--picks" in default[1][0]
         and default[1][0][default[1][0].index("--picks") + 1] == str(picks_path)
         and validate[0] == names and "--picks" in validate[1][0]
         and save[0] == ["interactive counts", "interactive compose"] and save[1] == [], got)
    json.dump({"criterion": resolve_criterion(DEFAULT_CRITERION)[0], "cells": {"props_condensed_adj_chunked":
              {**{q: "(pin)" for q in every}, every[0]: "no-such-body"}}}, open(picks_path, "w"))
    gone = GLM.file_picks(str(picks_path), "props_condensed_adj_chunked", "interactive", resolve_criterion(DEFAULT_CRITERION)[0])
    other = GLM.file_picks(str(picks_path), "props_condensed_adj_chunked", "interactive", "bill_exact")
    case("file_picks: a body HEAD lacks, or another pick rule, does not cover the cell (it is counted), said",
         gone[0] is None and every[0] in gone[1] and other[0] is None and "bill_exact" in other[1]
         and [n for n, _ in LT.paper_steps(ns())[0]] == names, (gone, other))
    os.remove(picks_path)

    # --regime warm, an option beside steady and cold: latency-cells.sh times the warm regime (no KV-timer passes), its
    # grid_latency call names warm (the dry run), and measure-latency reads warm-p0's raw files
    steps, *_ = LT.paper_steps(ns(regime="warm"))
    cells = next((c for n, c in steps if c[0] == "run/latency-cells.sh"), [])
    case("--regime warm: latency-cells.sh times warm, no KV-timer passes; the walls read from warm-p0",
         "interactive warm grid" in [n for n, _ in steps] and cells[cells.index("--regimes") + 1] == "warm"
         and cells[cells.index("--kvtimer-passes") + 1] == "0" and LT.regime_dir("warm", "64") == "warm-p0", steps)
    import subprocess as SP
    from kvevaluator.paths import ROOT
    dry = SP.run(["run/latency-cells.sh", "--sf", "0.1", "--family", "interactive", "--cells", "props_split_adj_split",
                  "--regimes", "warm", "--dry-run"], cwd=str(ROOT), capture_output=True, text=True)
    call = next((ln for ln in dry.stdout.splitlines() if "kvcost.latency.grid_latency" in ln), "")
    case("latency-cells.sh --regimes warm: its grid_latency call times warm (not cold)",
         dry.returncode == 0 and " --regimes warm" in call and "--cold" not in call, (dry.returncode, call, dry.stderr[-300:]))

    # the output dir takes every file (the steps stubbed: the timing plants one raw file per (query, param) in the
    # output dir, as latency-cells.sh's run writes there); without sudo, summary.csv's machine column says so
    run2 = types.SimpleNamespace(path="w.json", name="w", dataset="ldbc", scale="0.1")
    run2.families = lambda: ["interactive"]
    LT.runs.load = lambda a: run2
    LT.runs.say_run = lambda cmd, r: None
    import kvcost.cli.verbs as VB
    VB.require_plan_files = lambda r, verb: None
    LT.have_sudo = lambda: False
    steady_dir = LT.regime_dir("steady", mb.split(",")[0])
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd[0] if cmd[0] != sys.executable else cmd[3])
        if cmd[0] == "run/latency-cells.sh":
            for q in ("is1", "is2"):
                plant(str(Tree().root), steady_dir, "props_condensed_adj_chunked", q, None, 1,
                      {"measured": [{"totals": {"wall_ns": 1000}}, {"totals": {"wall_ns": 2000}}]}, "steady.json")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    LT.subprocess.run = fake_run
    mine = str(Tree().root)                            # main sets KVEVAL_OUTPUT_DIR to tmp/output
    rc = LT.cmd_measure_latency(ns(dataset=None, sf=None), [])
    summary = Tree().summary("ldbc", "0.1", "workload-latency", "w", "summary.csv")
    got = list(csv.DictReader(open(summary))) if summary.exists() else []
    case("the output dir: the raw files (timings/) and summary.csv (summary/.../workload-latency/<id>/) under it",
         rc == 0 and ran == ["measure", "compose", "build-store", "run/latency-cells.sh"] and os.path.isdir(os.path.join(mine, "timings")),
         (rc, ran, sorted(os.listdir(mine)) if os.path.isdir(mine) else None))
    case("no sudo: the run goes on, and summary.csv's machine column says so",
         len(got) == 2 and {r["machine"] for r in got} == {LT.NO_SUDO} and {r["regime_dir"] for r in got} == {steady_dir}
         and {r["mean_ns"] for r in got} == {"2000"}, got)

    from kvcost.layout_cost.picks import PlanRule
    case("grid_latency's default rule unchanged (no --in-sample)",
         PlanRule(historical=False, in_sample=None) == PlanRule(historical=False))

    n_ok = sum(results)
    print(f"check_measure_latency {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
