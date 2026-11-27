"""Time one arm's chosen plans per regime (one cpplatency process each) and gate the counts of those runs against a counts capture.

An arm is a meta cell's store (meta-<cell>, the plans the cell's cost uses) or an engine format's store (its registry
pins). Per (arm, regime) one `python -m kvevaluator.measure.cpplatency` process under `/usr/bin/time -v`: the process
ends between regimes (a measure can hold tens of GB) and its wall and peak RSS are recorded. Then:
- the mechanical checks (check_passes): every (query, param) cell present, in every steady pass; a steady store whose
  reads all fit its cache is recorded (read_set_fits), never refused;
- the VARIANT GATE (gate, engine_gate): every (query, param)'s counts in the timed run equal the counts capture's row
  of the plan that was meant to run -- S / G / N, the misses and all seven byte lanes -- or another plan ran (the
  caller marks that timing; a cell missing from the timed run fails it).
Which plan each query runs (the pick) is the caller's: kvcost/latency/grid_latency.py.

Output under Tree(root).timings(workload, sf, arm, regime): cpplatency's own files (steps.csv + .meta.json), run.log.
"""
import json
import os
import re
import subprocess
import sys
import time

from kvevaluator import stale
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice import candidates as C
from kvevaluator.capture import capture_layout, step_record as R
from kvevaluator.output_tree import Capture, Tree, timing_in
from kvevaluator.measure.capture_protocol import (cpplatency_default, dir_perf_level, index_filter_default,
                                                  resume_mismatch)


# the steady wall capture with the KV timer in every pass (cpplatency --kv-timing steady): steady-<MB>mb-p<L>-kvtimed.
# Its own dir, so walls timed with and without the timer never mix; the RocksDB share then comes from the same passes
# as the walls, and no kvtimer passes run beside it
KV_TIMED = "-kvtimed"


def arm_dir(root, sf, workload, regime, arm):
    """one arm's timings in one regime (output_tree.timing_in)"""
    return timing_in(Tree(root).timings(workload, sf), arm, regime)


def regime_flags(regimes, *, warm, cold, steady, steady_perf_level, reuse_passes=2,
                 cold_breakdown=False, cold_breakdown_runs=None, suffix="", steady_kv_timed=False):
    """regime dir name -> cpplatency flags, in the order `regimes` (a comma list) names them. Cold is direct I/O at
    PerfLevel 2 (inputs/contract/store_config.json; cpplatency's defaults). steady_kv_timed: the KV timer in every
    steady pass, in the dir steady-<MB>mb-p<L>-kvtimed"""
    out = {}
    for r in regimes.split(","):
        if r == "warm":
            out["warm-p0" + suffix] = ["--warm", warm, "--cold", "", "--perf-level", "0"]
        elif r == "cold":
            out["cold-p2" + suffix] = ["--warm", "", "--cold", cold, "--perf-level", "2"]
            if cold_breakdown:   # a second cold execution with the per-call timers, never the walls (inputs/contract/store_config.json)
                out["cold-breakdown" + suffix] = ["--warm", "", "--cold", cold_breakdown_runs or cold, "--perf-level", "3",
                                                  "--breakdown-pass", "--kv-timing", "all"]
        elif r == "steady":
            if not steady:
                raise SystemExit("--regimes steady needs --steady CACHE_MB,PASSES")
            mb = steady.split(",")[0]
            out[f"steady-{mb}mb-p{steady_perf_level}" + (KV_TIMED if steady_kv_timed else "") + suffix] = [
                "--warm", "", "--cold", "", "--steady", steady, "--steady-direct-io",
                "--steady-perf-level", str(steady_perf_level), *(["--kv-timing", "steady"] if steady_kv_timed else [])]
        elif r == "reuse":
            # the opt-in reuse-distance pass on the SAME plans and in the steady regime's order: counts, no clock
            out[f"reuse-distance-p{reuse_passes}" + suffix] = ["--warm", "", "--cold", "", "--reuse-distance", str(reuse_passes)]
        else:
            raise SystemExit(f"--regimes: unknown regime {r!r} (warm | cold | steady | reuse)")
    return out


# the timer captures beside a steady wall capture, in the order their passes run (cpplatency --steady-kvtimer, then
# --steady-breakdown): kvtimer = perf level 2 + the KV timer, breakdown = perf level 3 + the KV timer
TIMER_KINDS = ("kvtimer", "breakdown")


def timer_regime(regime, kind):
    """the dir of a steady regime's timer capture: steady-<MB>mb-p<L>[suffix] -> steady-<MB>mb-<kind>[suffix]"""
    return re.sub(r"^(steady-\d+mb)-p\d", rf"\1-{kind}", regime)


def breakdown_regime(regime):
    """the dir of a steady regime's breakdown capture (--steady-breakdown): steady-<MB>mb-p<L>[suffix] ->
    steady-<MB>mb-breakdown[suffix]"""
    return timer_regime(regime, "breakdown")


def timer_regimes(regime, timers):
    """{timer dir name: kind} of a regime's timer captures, for timers = {kind: passes} (none for a non-steady regime)"""
    if not regime.startswith("steady-"):
        return {}
    return {timer_regime(regime, k): k for k in TIMER_KINDS if timers.get(k)}


def want_protocol(regime):
    """the protocol this run gives a regime dir: cpplatency's default at the perf level the dir names (warm-p0, cold-p2,
    steady-<MB>mb-p<L>); a breakdown dir: perf 3 with the KV timer; a kvtimer dir: perf 2 with the KV timer; a kvtimed
    wall dir: its perf level with the KV timer"""
    if "-breakdown" in regime:
        return ("batch", True, True)
    if "-kvtimer" in regime:
        return ("batch", True, False)
    requests, _timer, perf = cpplatency_default(regime.split("-")[0], dir_perf_level(regime))
    return (requests, KV_TIMED in regime, perf)


def move_aside(path, root):
    """move a capture dir out of the way (mv, never delete), to <root>/aside-<YYYYMMDD>/<its path below root>, where no
    reader of the grid looks; the new path"""
    dst = os.path.join(root, f"aside-{time.strftime('%Y%m%d', time.gmtime())}", os.path.relpath(path, root))
    if os.path.exists(dst):
        dst += time.strftime("-%H%M%S", time.gmtime())
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(path, dst)
    return dst


def already_measured(out, regime, label, timer_outs=None, root=None):
    """True when (out, regime) holds a capture to keep. One timed under another protocol than this run would use
    (cpplatency's defaults: batch requests, the KV timer off, the perf level the dir names) is moved aside (under
    `root`) and re-measured, marked protocol_mixed, so a resumed grid never mixes two. timer_outs: {timer dir name: dir}
    of the timer captures this run would make beside it (kvtimer, breakdown); a wall capture without one of them (or the
    reverse) is moved aside with them and re-measured, marked timing_incomplete, as one run makes them all"""
    timer_outs = timer_outs or {}
    have = os.path.exists(os.path.join(out, regime_file(regime)))
    dirs = [out, *timer_outs.values()]
    if any(have != os.path.exists(os.path.join(d, "steps.csv")) for d in timer_outs.values()):
        moved = [move_aside(d, root) for d in dirs if os.path.exists(d)]
        stale.note("timing_incomplete", f"{label} {regime}: {os.path.relpath(out)} and its timer captures "
                   f"{', '.join(os.path.relpath(d) for d in timer_outs.values())} were not all on disk: re-measured (the "
                   f"ones present moved to {', '.join(os.path.relpath(m) for m in moved)})")
        return False
    if not have:
        return False
    mixed = protocol_mismatch([(out, regime)] + [(d, r) for r, d in timer_outs.items()])
    if mixed:
        o, r, why = mixed
        moved = [move_aside(d, root) for d in dirs if d and os.path.exists(d)]
        stale.note("protocol_mixed", f"{label} {r}: {os.path.relpath(o)} was timed under another protocol ({why}): "
                   f"moved to {', '.join(os.path.relpath(m) for m in moved)} and re-measured")
        return False
    print(f"  {label} {regime}: already measured ({os.path.relpath(out)}); --fresh re-measures", flush=True)
    return True


def protocol_mismatch(captures):
    """the first (dir, regime dir name, why) of captures [(dir, regime dir name)] timed under another protocol than this
    run would use, or None (a reuse pass is counts only: no clock to compare)"""
    for o, r in captures:
        if r.startswith("reuse-distance"):
            continue
        why = resume_mismatch(o, want_protocol(r), index_filter_default(r), want_store_config=True)
        if why:
            return o, r, why
    return None


def regime_file(regime):
    """the file that says a (cell, regime) was measured"""
    return "reuse-distance.csv" if regime.startswith("reuse-distance") else "steps.csv"


def read_set_fits(out, cache_mb=None):
    """a steady capture whose whole read set fits its bounded cache: steady = warm on that store, a property of the
    layout at that cache size, recorded and never refused. From the cache usage cpplatency reads after the last timed
    pass (the meta's regimes.steady.cache_usage): every arm's capacity the regime's cache (`cache_mb`, when given), its
    usage below the capacity and nothing evicted. A capture with no usage recorded: every measured pass at a hit rate
    of 1.0 (as steady-passes.csv rounds it). -> (the fit: {"usage_bytes", "capacity_bytes", "inserted_bytes"}, or
    {"hit_rate": 1.0} without a usage, else None; a line to say when every measured pass is at 1.0 and it is no fit,
    or when the store's capacity is not the regime's)"""
    path = os.path.join(out, "steps.csv.meta.json")
    cu = json.load(open(path)).get("regimes", {}).get("steady", {}).get("cache_usage") if os.path.exists(path) else None
    all_hits = _every_measured_pass_hits(out)
    if not cu:
        return ({"hit_rate": 1.0} if all_hits else None), ""
    wrong, full = _usage_misfits(cu, int(float(cache_mb) * (1 << 20)) if cache_mb else None)
    if wrong or full:
        return None, ("; ".join(wrong + full) if wrong or all_hits else "")
    return {k: max(u[k] for u in cu.values()) for k in ("usage_bytes", "capacity_bytes", "inserted_bytes")}, ""


def _every_measured_pass_hits(out):
    """True when a steady capture's every measured pass has a hit rate of 1.0 (steady-passes.csv)"""
    path = os.path.join(out, "steady-passes.csv")
    if not os.path.exists(path):
        return False
    rates = [float(r["hit_rate"]) for r in capture_layout.reader(open(path)) if r["phase"] == "measured"]
    return bool(rates) and all(r >= 1.0 for r in rates)


def _usage_misfits(cu, want):
    """(the arms whose store's cache capacity is not `want` bytes, the arms whose usage is no fit: not below the
    capacity, or something evicted) of a recorded cache usage, each as a line"""
    wrong = [f"{arm}: the store's cache capacity {u['capacity_bytes']} is not the {want} asked" for arm, u in sorted(cu.items())
             if want is not None and u["capacity_bytes"] != want]
    full = [f"{arm}: usage {u['usage_bytes']} of {u['capacity_bytes']}, nothing evicted {u['nothing_evicted']}"
            for arm, u in sorted(cu.items()) if not (u["usage_bytes"] < u["capacity_bytes"] and u["nothing_evicted"])]
    return wrong, full


def fits_of(root, sf, workload, arm, regimes):
    """{regime dir: read_set_fits} of an arm's steady captures on disk that fit their cache (the cache's size from the
    regime dir's name, steady-<MB>mb-...), each said once; a capture at a hit rate of 1.0 that is no fit is said too"""
    out = {}
    for regime in regimes:
        d = str(arm_dir(root, sf, workload, regime, arm))
        if not (regime.startswith("steady") and os.path.exists(os.path.join(d, "steps.csv"))):
            continue
        mb = re.match(r"steady-(\d+(?:\.\d+)?)mb", regime)
        fit, why_not = read_set_fits(d, mb.group(1) if mb else None)
        if fit:
            out[regime] = fit
            print(f"    {arm} {regime}: read set fits the cache ("
                  + (f"peak {fit['usage_bytes'] / (1 << 20):.1f} MiB of {fit['capacity_bytes'] / (1 << 20):.0f} MiB, "
                     f"nothing evicted" if "usage_bytes" in fit else "every measured pass at a hit rate of 1.0; no cache "
                     "usage recorded") + "): steady = warm on this store", flush=True)
        elif why_not:
            print(f"    {arm} {regime}: not recorded as a fit: {why_not}", flush=True)
    return out


def check_passes(out, regime, nq, nparam):
    """the mechanical gates on one (cell, regime) just measured: the latency file holds nq x nparam cells, and a steady
    run's every pass ran them all. Each pass's hit rate is printed; a hit rate of 1.0 is not refused (read_set_fits
    records a store whose reads fit the cache). Returns "" or the failure."""
    path = os.path.join(out, regime_file(regime))
    if not os.path.exists(path):
        return f"no {regime_file(regime)}"
    if regime.startswith("reuse-distance"):     # a row per step and pass: the (query, param) cells of its last pass
        rows = list(capture_layout.reader(open(path)))
        last = max((r["pass"] for r in rows), default="")
        cells = {(r["query"], r["param_idx"]) for r in rows if r["pass"] == last}
    else:
        cells = {(r["query"], r["param_idx"]) for r in capture_layout.reader(open(path)) if r["step"] == "__plan__"}
    if len(cells) != nq * nparam:
        return f"{len(cells)} (query, param) cells in steps.csv, expected {nq} x {nparam}"
    print(f"    cells: {len(cells)} = {nq} queries x {nparam} params", flush=True)
    if regime.startswith("steady"):
        ppath = os.path.join(out, "steady-passes.csv")
        if not os.path.exists(ppath):
            return "no steady-passes.csv"
        passes = list(capture_layout.reader(open(ppath)))
        if not passes:
            return "steady-passes.csv is empty"
        for r in passes:
            print(f"    pass {r['pass']} {r['phase']:8s} cells {r['cells']} hit rate {float(r['hit_rate']):.4f} direct_io {r['direct_io']}", flush=True)
        bad = [r for r in passes if int(r["cells"]) != nq * nparam]
        if bad:
            return "a steady pass with cells != expected: " + "; ".join(f"pass {r['pass']} cells {r['cells']}" for r in bad)
    return ""


def passes_why(out, timer_outs, regime, nq, nparam, timers=None):
    """check_passes on a capture and its timer captures ({timer dir name: dir}): "" when all hold, else why. With
    timers ({kind: passes}), each timer capture must hold exactly that many passes of its kind (the pass shape). Run
    after a measurement AND on a capture a resume finds on disk (a failed check leaves its capture there: a resume must
    not pass it)"""
    why = check_passes(out, regime, nq, nparam)
    for r, d in (timer_outs or {}).items():
        why = why or check_passes(d, r, nq, nparam)
        kind = timer_regimes(regime, {k: 1 for k in TIMER_KINDS}).get(r)
        if not why and timers and kind:
            got = [x["phase"] for x in capture_layout.reader(open(os.path.join(d, "steady-passes.csv")))]
            if got != [kind] * timers[kind]:
                why = f"{r}: passes {got}, expected {timers[kind]} x {kind}"
    return why


def gated_regimes(regimes, timers):
    """the regime dirs the variant gate reads: every regime, and each steady regime's timer captures when made
    (timers = {kind: passes})"""
    return list(regimes) + [t for r in regimes for t in timer_regimes(r, timers or {})]


def time_arm(sf, workload, arm, root, regimes, arm_args, nq, nparam, *, label=None, mid_args=(), tail_args=(),
             fresh=False, timers=None):
    """time one arm in each regime: one cpplatency process per regime, then passes_why. arm_args name the store and
    its plans (--format, --spec, --arms); mid_args follow the regime flags, tail_args the breakdown flag. A regime
    already on disk is kept (already_measured) unless `fresh`. timers = {kind: passes}: the steady regime's timer
    captures (kvtimer, breakdown). Returns ({regime: wall, peak RSS, rc, command}, ok)."""
    timers = timers or {}
    label = label or arm
    recs = {}
    for regime, flags in regimes.items():
        out = str(arm_dir(root, sf, workload, regime, arm))
        touts = {r: str(arm_dir(root, sf, workload, r, arm)) for r in timer_regimes(regime, timers)}
        if not fresh and already_measured(out, regime, label, touts, root):
            why = passes_why(out, touts, regime, nq, nparam, timers)
            if why:
                print(f"STAGE_FAIL latency {label} {regime} (on disk, not re-measured; --fresh re-measures it): {why}", flush=True)
                return recs, False
            continue
        os.makedirs(out, exist_ok=True)
        cmd = [sys.executable, "-m", "kvevaluator.measure.cpplatency", "--sf", sf, "--family", workload,
               *arm_args, "--nparam", str(nparam), *flags, *mid_args,
               *[x for r, k in timer_regimes(regime, timers).items()
                 for x in (f"--steady-{k}", touts[r], f"--steady-{k}-passes", str(timers[k]))],
               *tail_args, "--out", out]
        t0 = time.time()
        # its own process: the store closes between regimes; /usr/bin/time gives the peak RSS for free
        r = subprocess.run(["/usr/bin/time", "-v", *cmd], cwd=str(ROOT), capture_output=True, text=True)
        log = os.path.join(out, "run.log")
        with open(log, "w") as f:
            f.write(r.stdout + r.stderr)
        m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", r.stderr)
        recs[regime] = {"wall_s": round(time.time() - t0, 1), "peak_rss_gb": round(int(m.group(1)) / 1048576, 1) if m else None,
                        "rc": r.returncode, "command": " ".join(cmd[1:])}
        print(f"  {label} {regime}: rc={r.returncode} wall={recs[regime]['wall_s']}s "
              f"peak_rss={recs[regime]['peak_rss_gb']}GB", flush=True)
        if r.returncode != 0:
            print(f"STAGE_FAIL latency {label} {regime} rc={r.returncode} (see {os.path.relpath(log)})", flush=True)
            return recs, False
        why = passes_why(out, touts, regime, nq, nparam, timers)
        if why:
            print(f"STAGE_FAIL latency {label} {regime}: {why}", flush=True)
            return recs, False
    return recs, True


# -- The variant gate: the timed run's counts against the counts capture's row of the plan meant to run.

# the counts capture's column -> the latency file's `__plan__` column (the same execution's counters): the read counts
# and every read byte lane (S / G / N, the misses, the seven lanes)
GATE = {**{c: c for c in R.READ_OPS}, **{c: f"{c}_bytes" for c in R.READ_BYTE_LANES}}
REUSE_GATE = (*R.BASE_OPS, "fetched_kvbytes")
FETCHED = R.READ_BYTE_LANES[:-1]   # what the reuse pass tracks: every read lane but the last, the missed GETs' keys


def is_plan_row(r, workload):
    """a row of the committed IR's own body: the selector's row of an older capture (planner_pick), else
    a single-plan query's (no bundle: one body, run under the plan's name)"""
    if "planner_pick" in r:
        return r["planner_pick"] == "1"
    return "plans" not in C.candidates(workload, r["query"])[0]


def counts_rows(counts, cell, picks, workload):
    """(query, param) -> the counts capture's row of the chosen variant (the planner's own row for `(pin)`; for a pick
    the capture ran under an older name, the row of the same body by body_sha, a row of the same name first)"""
    head = {q: C.candidate_bodies(workload, q).get(v) for q, v in picks.items() if v != "(pin)"}
    out, by_body = {}, {}
    with open(Capture(counts).path_of("counts", cell)) as f:
        for r in capture_layout.reader(f):
            v = picks.get(r["query"])
            if r["ok"] != "1" or v is None:
                continue
            if r["body"] == v or (v == "(pin)" and is_plan_row(r, workload)):
                out[(r["query"], r["param_idx"])] = r
            # (a body rewritten count-equal since the capture: its successor, candidates.successor_sha)
            elif v != "(pin)" and r.get("body_sha") and C.successor_sha(workload, r["query"], r["body_sha"]) == head.get(r["query"]):
                by_body.setdefault((r["query"], r["param_idx"]), r)
    for k, r in by_body.items():
        out.setdefault(k, r)
    return out


def reuse_sums(path):
    """(query, param) -> S, G, N and the fetched kvbytes summed over the steps of a reuse-distance.csv's last pass"""
    rows = list(capture_layout.reader(open(path)))
    last = max((r["pass"] for r in rows), default="")
    out = {}
    for r in rows:
        if r["pass"] == last:
            x = out.setdefault((r["query"], r["param_idx"]), {c: 0 for c in REUSE_GATE})
            for c in REUSE_GATE:
                x[c] += int(r[c])
    return out


def reuse_diff(g, w):
    """the REUSE_GATE fields on which a reuse pass's sums differ from a counts row (keyed by GATE's counts-side names)"""
    want = {"S": w["S"], "G": w["G"], "N": w["N"], "fetched_kvbytes": sum(int(float(w[c])) for c in FETCHED)}
    return sorted(c for c in REUSE_GATE if g is None or g[c] != int(float(want[c])))


MISSING = "MISSING in the latency file"     # a gate row's `differing` when the timed run has no row of a (query, param)


def gate(root, sf, counts, cell, picks, nparam, regimes, workload, queries=None):
    """the variant gate's rows for one cell over the regimes on disk; (rows, compared, bad); `queries` = only these"""
    want = counts_rows(counts, cell, picks, workload)
    if queries:
        want = {k: v for k, v in want.items() if k[0] in queries}
    rows, n, bad = [], 0, 0
    for regime in regimes:
        reuse = regime.startswith("reuse-distance")     # the pass has no __plan__ row: its step sums of S, G, N and bytes
        path = os.path.join(arm_dir(root, sf, workload, regime, f"meta-{cell}"), regime_file(regime))
        if not os.path.exists(path):
            continue
        got = reuse_sums(path) if reuse else {(r["query"], r["param_idx"]): r for r in capture_layout.reader(open(path)) if r["step"] == "__plan__"}
        for key in sorted(want, key=lambda k: (k[0], int(k[1]))):
            if int(key[1]) > nparam:
                continue
            g, w = got.get(key), want[key]
            diff = reuse_diff(g, w) if reuse else sorted(c for c, lc in GATE.items() if g is None or int(float(g[lc])) != int(float(w[c])))
            n += 1
            bad += bool(diff)
            rows.append({"cell": cell, "regime": regime, "query": key[0], "param_idx": key[1], "body": picks[key[0]],
                         "fields_compared": len(REUSE_GATE if reuse else GATE), "equal": int(not diff),
                         "differing": " ".join(diff) if g is not None else MISSING})
    return rows, n, bad


def _lane_sums(path, nparam, skip_plan_row):
    """(query, param) -> the GATE lanes summed over a latency run's steps.csv rows (its __plan__ rows skipped:
    `skip_plan_row`)"""
    out = {}
    for r in capture_layout.reader(open(path)):
        if (skip_plan_row and r["step"] == "__plan__") or int(r["param_idx"]) > nparam:
            continue
        x = out.setdefault((r["query"], r["param_idx"]), {c: 0 for c in GATE})
        for c, lc in GATE.items():
            x[c] += int(float(r[lc]))
    return out


def engine_gate(root, formats_capture, fmt, nparam, regimes, sf, workload):
    """the variant gate for an engine-format arm: the latency run's lane sums per (query, param) == the format's row in
    the formats capture (grid_bodies --format), of the body the pick record names for the store (as the latency run
    sends it: kvstats_layout.store_counts); (rows, compared, bad) as gate()"""
    from kvevaluator.measure import kvstats_layout as L
    cpath = Capture(formats_capture).path_of("counts", fmt)
    if not os.path.exists(cpath):
        raise SystemExit(f"{fmt}: no counts capture at {os.path.relpath(cpath)} "
                         f"(python -m kvevaluator.measure.grid_bodies --sf <SF> --format {fmt} first)")
    want = {(r["query"], r["param_idx"]): {c: r[c] for c in GATE}
            for r in L.store_counts(sf, workload, fmt, cpath) if int(r["param_idx"]) <= nparam}
    rows, n, bad = [], 0, 0
    for regime in regimes:
        reuse = regime.startswith("reuse-distance")
        path = os.path.join(arm_dir(root, sf, workload, regime, fmt), regime_file(regime))
        if not os.path.exists(path):
            continue
        got = reuse_sums(path) if reuse else _lane_sums(path, nparam, skip_plan_row=True)
        for key in sorted(want, key=lambda k: (k[0], int(k[1]))):
            g, w = got.get(key), want[key]
            diff = reuse_diff(g, w) if reuse else sorted(c for c in GATE if g is None or g[c] != w[c])
            n += 1
            bad += bool(diff)
            rows.append({"cell": fmt, "regime": regime, "query": key[0], "param_idx": key[1], "body": "(pin)",
                         "fields_compared": len(GATE), "equal": int(not diff),
                         "differing": " ".join(diff) if g is not None else MISSING})
    return rows, n, bad
