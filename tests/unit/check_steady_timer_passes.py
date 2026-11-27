#!/usr/bin/env python3
"""The steady regime's pass order and timer passes (cpplatency.run_steady) and the RocksDB share rule
(kvcost.latency.steady_table), on a fake server and planted files (no store, no binary):
  1. measured pass k runs steady_rule.measured_order(k) on two stores that warm up 3 and 5 passes: the same orders;
  2. the timer passes follow at the next measured indexes (kvtimer at perf 2, then breakdown at perf 3, the KV timer on
     in both), and a cell's record is its median run of them;
  3. timer_query / shares: hand-worked RocksDB time, busy time, wall and block-read split over every run of a cell,
     the timer's own cost taken out;
  4. cell_latency: the timer dirs' names, the gated dirs, and a timer capture with the wrong pass count refused.

    python3 tests/unit/check_steady_timer_passes.py [--plant warm-index|no-timer-cost]   (a plant must exit 1)
"""
import argparse
import io
import json
import os
import sys
import tempfile

from kvevaluator.measure import cell_latency as CL
from kvevaluator.measure import cpplatency as CP
from kvevaluator.measure import steady_rule
from kvcost.latency import steady_table as ST

FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


class FakeServer:
    """answers each pass with a data-block hit rate from `rates` (the last one repeats); records each pass's order and
    run settings. A run's wall is 100 + its pass number, so the median of a cell's runs is known"""
    def __init__(self, rates):
        self.rates, self.calls = rates, []
        self.proc = type("P", (), {"pid": 0})()

    def run_pass(self, srv, plans, cells, order, run, path_base):
        n = len(self.calls)
        self.calls.append({"order": list(order), "perf_level": run["perf_level"], "kv_timing": run.get("kv_timing", False)})
        rate = self.rates[min(n, len(self.rates) - 1)]
        hit = int(rate * 1000)
        out = []
        for k in order:
            resp = {"rocksdb": {"statistics_delta": {"rocksdb.block.cache.data.hit": hit,
                                                     "rocksdb.block.cache.data.miss": 1000 - hit}},
                    "steps": [], "totals": {"work_ns": 100 + n}, "provenance": {}}
            if run.get("kv_timing"):
                resp["kv_timing"] = {"timer_pair_ns": 20}
            out.append((k, resp))
        return out


def steady_with(rates, kv_timed=False):
    """run_steady over 4 cells: 3 measured passes, 2 kvtimer passes, 1 breakdown pass; kv_timed: the KV timer in every
    steady pass instead (--kv-timing steady) and the breakdown pass only; (server, passes, timed, timer records, kv block)"""
    srv = FakeServer(rates)
    CP.run_pass = srv.run_pass
    CP.numa_binding.snapshot = lambda pid: None
    CP.numa_binding.fault_columns = lambda f0, f1, wall, pid: {"faults_per_s": 0}
    CP.steady_rows = lambda arm, q, idx, record: []
    cells = {(q, 1): ("ir-" + q, {}) for q in ("q1", "q2", "q3", "q4")}
    raws = {"kvtimer": io.StringIO(), "breakdown": io.StringIO()}
    timers = [{"kind": "kvtimer", "dir": "kvt", "level": 2, "passes": 2, "raw": raws["kvtimer"]},
              {"kind": "breakdown", "dir": "brk", "level": 3, "passes": 1, "raw": raws["breakdown"]}]
    if kv_timed:
        timers = timers[1:]
    _rows, passes, _at, _prov, kvb, timed = CP.run_steady(srv, "a", cells, 3, 0.005, 8, 1, 2, "out", 1 << 20, True,
                                                          raw_out=io.StringIO(), timers=timers, kv_timing=kv_timed)
    recs = {k: [json.loads(x) for x in v.getvalue().splitlines()] for k, v in raws.items()}
    return (srv, passes, timed, recs, kvb) if kv_timed else (srv, passes, timed, recs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=("warm-index", "no-timer-cost"),
                    help="a negative control: measured passes ordered by the server's pass index (another "
                         "rule), or the timer's own cost left in (each must exit 1)")
    a = ap.parse_args()
    if a.plant == "warm-index":     # the rule before: every pass ordered by the server's pass count, warm passes included
        CP.steady_rule = type("R", (), {"pass_order": staticmethod(steady_rule.pass_order), "settled": staticmethod(steady_rule.settled),
                                        "measured_order": staticmethod(lambda keys, seed, k: steady_rule.pass_order(
                                            keys, seed, len(CP.run_pass.__self__.calls)))})
    keys = [("q1", 1), ("q2", 1), ("q3", 1), ("q4", 1)]

    # 1-2. two stores, 3 and 5 warm passes
    s3, p3, t3, r3 = steady_with([0.5, 0.9, 0.9])
    s5, p5, t5, r5 = steady_with([0.5, 0.6, 0.7, 0.8, 0.8])
    w3, w5 = sum(p["phase"] == "warm" for p in p3), sum(p["phase"] == "warm" for p in p5)
    check("the two stores warm up 3 and 5 passes", (w3, w5) == (3, 5), (w3, w5))
    m3 = [c["order"] for c in s3.calls[w3:w3 + 3]]
    m5 = [c["order"] for c in s5.calls[w5:w5 + 3]]
    check("measured pass k runs one order on both stores", m3 == m5, (m3, m5))
    want = [[list(x) for x in steady_rule.measured_order(keys, 1, k)] for k in range(3)]
    check("measured pass k runs measured_order(k) on both stores", [[list(x) for x in o] for o in m3] == want
          and [[list(x) for x in o] for o in m5] == want, (m3, m5))
    t_orders = [[list(x) for x in c["order"]] for c in s5.calls[w5 + 3:]]
    check("the timer passes run measured_order(3), (4), (5)",
          t_orders == [[list(x) for x in steady_rule.measured_order(keys, 1, k)] for k in (3, 4, 5)], t_orders)
    check("the warm passes keep pass_order(n)", [c["order"] for c in s5.calls[:w5]] ==
          [steady_rule.pass_order(keys, 1, n) for n in range(w5)])
    lv = [(c["perf_level"], c["kv_timing"]) for c in s5.calls]
    check("measured passes at perf 2 without the timer, kvtimer at perf 2 with it, breakdown at perf 3 with it",
          lv == [(2, False)] * (w5 + 3) + [(2, True)] * 2 + [(3, True)], lv)
    check("each timer capture's pass rows name their kind and number", [(p["phase"], p["pass"]) for t in ("kvtimer", "breakdown")
          for p in t5[t]["pass_rows"]] == [("kvtimer", w5 + 3), ("kvtimer", w5 + 4), ("breakdown", w5 + 5)])
    kv = r5["kvtimer"]
    check("a kvtimer record keeps both runs and the median (upper middle) run",
          len(kv) == 4 and all(len(x["measured"]) == 2 and x["median"]["totals"]["work_ns"] == 100 + w5 + 4
                               and x["perf_level"] == 2 for x in kv), [x["median"]["totals"] for x in kv])

    # 2b. the fold (--kv-timing steady): the KV timer in every steady pass, no kvtimer passes, the breakdown pass after
    sf, pf, tf, _rf, kvb = steady_with([0.5, 0.9, 0.9], kv_timed=True)
    wf = sum(p["phase"] == "warm" for p in pf)
    lv = [(c["perf_level"], c["kv_timing"]) for c in sf.calls]
    check("the fold: warm and measured passes at perf 2 with the timer, then the breakdown at perf 3, no kvtimer pass",
          lv == [(2, True)] * (wf + 3) + [(3, True)] and set(tf) == {"breakdown"}, (lv, sorted(tf)))
    check("the fold: measured passes keep measured_order(k), the breakdown pass measured_order(3)",
          [[list(x) for x in c["order"]] for c in sf.calls[wf:]]
          == [[list(x) for x in steady_rule.measured_order(keys, 1, k)] for k in range(4)])
    check("the fold: the wall capture's kv_timing block (the timer pair's cost) comes back for its meta",
          kvb == {"timer_pair_ns": 20}, kvb)
    fl = CL.regime_flags("steady", warm="", cold="", steady="307,10", steady_perf_level=2, steady_kv_timed=True)
    check("the fold's wall dir and flags: steady-307mb-p2-kvtimed, --kv-timing steady",
          list(fl) == ["steady-307mb-p2-kvtimed"] and fl["steady-307mb-p2-kvtimed"][-2:] == ["--kv-timing", "steady"], fl)
    check("the fold's protocol: batch, the KV timer, no PerfContext timer; the plain wall dir without the timer",
          (CL.want_protocol("steady-307mb-p2-kvtimed"), CL.want_protocol("steady-307mb-p2"))
          == (("batch", True, False), ("batch", False, False)))
    check("the fold's breakdown dir is its own: steady-307mb-breakdown-kvtimed",
          CL.timer_regimes("steady-307mb-p2-kvtimed", {"breakdown": 1}) == {"steady-307mb-breakdown-kvtimed": "breakdown"})

    # 3. the share rule, on planted timer captures: q1 has 2 params, q2 one
    tmp = tempfile.mkdtemp(prefix="steady-timer-")
    from kvevaluator.measure.latency_raw_files import raw_path
    arm = "meta-x"

    def run(kv_call, calls, work, overhead, block=0, reads=0):
        """one run: a kv step (its RocksDB calls) and a flow step, the plan's totals"""
        kv = {"kv_call_ns": kv_call, "kv_get_calls": calls, "block_read_time": block}
        if reads:
            kv["block_read_count"] = reads
        return {"steps": [{"kind": "kv", "kv": kv}, {"kind": "flow", "kv": None}],
                "totals": {"work_ns": work, "overhead_ns": overhead}}

    def plant(name, cells, pair=None, rule=None):
        """a capture: steps.csv's __plan__ rows name the cells, each cell's raw steady.json holds its runs; a timer
        capture's meta has its timer pair, a wall capture's its pass order rule"""
        d = os.path.join(tmp, name)
        os.makedirs(d)
        with open(os.path.join(d, "steps.csv"), "w") as f:
            f.write("query,param_idx,step\n" + "".join(f"{q},{p},__plan__\n" for q, p in cells))
        for (q, p), runs in cells.items():
            path = raw_path(d, arm, q, p, "steady")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump({"measured": runs}, open(path, "w"))
        meta = {"kv_timing_block": {"timer_pair_ns": pair}} if pair is not None else \
            {"regimes": {"steady": {"pass_order_rule": rule}}}
        json.dump(meta, open(os.path.join(d, "steps.csv.meta.json"), "w"))
        return d
    # each run's timer cost 10 calls x 10 ns = 100: half of it out of the RocksDB time, all of it out of busy and wall.
    # q1: p1 (rocks 600-50, busy 1000-100, wall 1100-100, 1 block read), p2 (800-50, 1200-100, 1300-100, 3 reads): means
    # 650, 1000, 1100, 0, 2. q2: one param, two runs (300-50, 600-100, 600-100, 0 reads) and (500-50, 800-100, 800-100, 2
    # reads): means 350, 600, 600, 0, 1
    kvt = plant("kvt", {("q1", "1"): [run(600, 10, 1000, 100, reads=1)], ("q1", "2"): [run(800, 10, 1200, 100, reads=3)],
                        ("q2", "1"): [run(300, 10, 600, 0), run(500, 10, 800, 0, reads=2)]}, 10)
    brk = plant("brk", {("q1", "1"): [run(700, 10, 1000, 0, block=300, reads=2)], ("q2", "1"): [run(400, 10, 600, 0)]}, 10)
    if a.plant == "no-timer-cost":
        ST.timer_pair_ns = lambda d: 0.0
    tq = ST.timer_query(kvt, arm)
    check("timer_query: per query the mean over params and runs, the timer's cost out (half of it from the RocksDB time)",
          tq == {"q1": (650.0, 1000.0, 1100.0, 0.0, 2.0), "q2": (350.0, 600.0, 600.0, 0.0, 1.0)}, tq)
    bq = ST.timer_query(brk, arm)
    per_read = ST.block_ns_per_read({"a": (0, 0, 0, 300, 3), "b": (0, 0, 0, 100, 2), "c": (0, 0, 0, 0, 0)})
    check("block_ns_per_read: per query, and the store's pooled time per read for a query with none",
          per_read == {"a": 100.0, "b": 50.0, "c": 80.0}, per_read)
    walls = {"q1": 950.0, "q2": 480.0}
    sh = ST.shares(tq, bq, walls, {"q1": 1.0, "q2": 2.0})
    # rocks 650 + 2 x 350 = 1350 over busy 1000 + 2 x 600 = 2200, over wall 1100 + 2 x 600 = 2300; the mean walls
    # 950 + 2 x 480 = 1910. Block reads: perf 3's 150 ns per read (q2 none: pooled 150) x the perf-2 reads, 150 x 2 +
    # 2 x 150 x 1 = 600, over the busy 2200; perf 3's wall 900 + 2 x 500 = 1900
    want_sh = {"rocks_share": 1350 / 2200, "rocks_wall_share": 1350 / 2300, "kvtimer_tax": 2300 / 1910,
               "block_read_share": 600 / 2200, "other_rocks_share": 750 / 2200, "perf3_tax": 1900 / 2300,
               "rocks_ns": 1350, "busy_ns": 2200, "timer_wall_ns": 2300, "mean_wall_ns": 1910, "block_read_ns": 600,
               "perf3_wall_ns": 1900}
    check("shares: hand-worked share, split and taxes, and the weighted sums under them",
          set(sh) == set(want_sh) and all(abs(sh[k] - want_sh[k]) < 1e-9 for k in sh), sh)
    refused = False
    try:
        ST.shares(tq, None, {"q1": 1.0}, {"q1": 1.0, "q2": 1.0})
    except SystemExit:
        refused = True
    check("shares: a query missing from the walls refused", refused)

    # the wall capture: mean walls over params and runs, per-pass walls from pass 1, the pass order rule required
    wall_cells = {("q1", "1"): [run(0, 0, 100, 10), run(0, 0, 300, 10), run(0, 0, 200, 10)],
                  ("q2", "1"): [run(0, 0, 50, 0), run(0, 0, 70, 0), run(0, 0, 60, 0)]}
    wcap = plant("wall", wall_cells, rule=steady_rule.PASS_ORDER_RULE)
    mw = ST.mean_walls(wcap, arm)
    check("mean_walls: the mean of plan_wall over params and runs", mw == {"q1": 210.0, "q2": 60.0}, mw)
    pw = ST.per_pass_walls(wcap, arm, sorted(wall_cells))
    pr = ST.pass_ratios(pw, {q: [2 * x for x in v] for q, v in pw.items()}, {"q1": 1.0, "q2": 1.0})
    check("per-pass walls in pass order; pass_ratios from pass 1", pw == {"q1": [110, 310, 210], "q2": [50, 70, 60]}
          and pr == [0.5, 0.5], (pw, pr))
    old = plant("wall-old", wall_cells, rule=None)
    refused = False
    try:
        ST.per_pass_walls(old, arm, sorted(wall_cells))
    except SystemExit:
        refused = True
    check("per_pass_walls: a capture without the measured-index pass order refused", refused)

    # 4. cell_latency's timer dirs, gated dirs and the pass shape
    check("timer dir names", (CL.timer_regime("steady-307mb-p2-repeat", "kvtimer"), CL.breakdown_regime("steady-307mb-p2"))
          == ("steady-307mb-kvtimer-repeat", "steady-307mb-breakdown"))
    check("gated dirs: the wall and each timer capture made",
          CL.gated_regimes(["steady-307mb-p2"], {"kvtimer": 2, "breakdown": 1})
          == ["steady-307mb-p2", "steady-307mb-kvtimer", "steady-307mb-breakdown"]
          and CL.gated_regimes(["steady-307mb-p2", "cold-p2"], {}) == ["steady-307mb-p2", "cold-p2"])
    check("the kvtimer dir's protocol: batch, the KV timer, no PerfContext timer",
          CL.want_protocol("steady-307mb-kvtimer") == ("batch", True, False))
    shape = os.path.join(tmp, "shape")
    for r, phases in (("steady-307mb-p2", ["warm", "measured"]), ("steady-307mb-kvtimer", ["kvtimer"])):
        d = os.path.join(shape, r)
        os.makedirs(d)
        with open(os.path.join(d, "steps.csv"), "w") as f:
            f.write("query,param_idx,step\nq1,1,__plan__\n")
        with open(os.path.join(d, "steady-passes.csv"), "w") as f:
            f.write("pass,phase,cells,hit_rate,direct_io\n" + "".join(f"{i},{p},1,0.5,1\n" for i, p in enumerate(phases)))
    touts = {"steady-307mb-kvtimer": os.path.join(shape, "steady-307mb-kvtimer")}
    ok1 = CL.passes_why(os.path.join(shape, "steady-307mb-p2"), touts, "steady-307mb-p2", 1, 1, timers={"kvtimer": 1})
    bad = CL.passes_why(os.path.join(shape, "steady-307mb-p2"), touts, "steady-307mb-p2", 1, 1, timers={"kvtimer": 2})
    check("the pass shape: 1 kvtimer pass passes, 2 expected refuses", ok1 == "" and "expected 2 x kvtimer" in bad, (ok1, bad))

    print(f"check_steady_timer_passes {'FAIL' if FAILED else 'OK'}: {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
