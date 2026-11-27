"""Measure the C++ read latency: run every (arm, query, param, cache regime) cell through the C++ evaluator and save the
raw results (one response JSON per cell, the per-step rows in steps.csv). No metric is derived here; kvcost.latency
reads the CSV.

    python3 -m kvevaluator.measure.cpplatency --sf 0.1 --arms pertype_csr-c64k pertype_csr-c64k-zstd \
        --nparam 5 --warm 1,5 --cold 3 --out output/sf0.1/pertype-csr-latency-cpp
    python3 -m kvevaluator.measure.cpplatency --sf 0.1 --family writes --format dgraph --arms dgraph ...

A write plan runs against the executor's shadow overlay: its wall is the read-modify-write reads plus in-memory PUTs,
not durable write latency; the store never changes. Requests go as batches (measure/cppbatch.py).

--steady CACHE_MB,PASSES: a bounded block cache of the same bytes for every format (the fairness rule), every cell run
in a seeded order reshuffled per pass, warmed until the pass hit rate settles, then PASSES measured passes; a cell's
row is its median-wall run. steady-passes.csv holds each pass's hit rate and bytes (the proof steady state was reached).

--kv-timing: the KV-boundary timer runs inside the plan clock, so its cost is in the wall; a wall of record is timed
without it, and --accumulate refuses to mix settings within a regime. --steady-kvtimer / --steady-breakdown: timer
passes after the measured steady passes, in the same server and cache, each written as its own capture.

--numa-node: the server runs bound to one NUMA node (measure/numa_binding.py), since unbound the kernel's NUMA
balancing faults pages back in inside the plan clocks; a bound run whose server the scanner may still scan is refused.
"""
import argparse
import csv
import gzip
import glob
import hashlib
import json
import os
import random
import statistics
import sys
import time

from kvevaluator import stale
from kvevaluator.measure import numa_binding
from kvevaluator.measure.server import CppServer
from kvevaluator.measure.stores import add_spec_arg, cli_spec, store_dir
from kvevaluator.capture.store_identity import allocator as binary_allocator
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure import capture_protocol, steady_rule, workload_plans
from kvevaluator.capture import step_record
from kvevaluator.measure import store_layout
from kvevaluator.measure.latency_raw_files import raw_cells, raw_path
from kvevaluator.measure.cppbatch import run_lines
from kvevaluator.output_tree import Tree
from kvevaluator.paths import recorded_path

QUERIES = workload_plans.queries("interactive")            # the historical default (the LDBC IC/IS suite)

# the per-step columns of steps.csv (raw counts + engine timers, never a derived metric)
KV_FIELDS = ["block_read_count", "block_read_byte", "block_read_time", "block_decompress_time",
             "block_checksum_time", "block_cache_hit_count", "get_from_output_files_time",
             "seek_child_seek_time", "find_next_user_entry_time", "internal_key_skipped_count",
             "iter_read_bytes", "get_read_bytes", "io_bytes_read", "io_read_nanos"]
# the KV-boundary timer's per-step fields (run.kv_timing, store/counting.cc: every Get, NewIterator + Seek /
# SeekForPrev, Next / Prev, iterator delete, Put, Delete; kv_call_ns = their sum). Columns of a capture made with the
# timer on only (columns(True)), so a capture from before the timer, or with it off, keeps its header
KV_TIMER_FIELDS = ["kv_call_ns", "kv_get_ns", "kv_get_calls", "kv_seek_ns", "kv_seek_calls", "kv_next_ns", "kv_next_calls",
                   "kv_iter_close_ns", "kv_iter_close_calls", "kv_put_ns", "kv_put_calls", "kv_delete_ns", "kv_delete_calls"]
# the step record's free fields come from the shared list (capture/step_record.py); the latency file adds its clocks and
# the engine's per-step timers
_TIMING = ["wall_ns_min", "wall_ns_max", "plan_wall_ns", "critical_ns"]
# the remote regime (--remote-delay-us; cpp/kvevaluator/src/store/round_trips.h): its settings on every row, read from the
# response (so --regen keeps them), and what the step waited. Off: mode empty, delay 0; the page is always there (it
# decides the round-trip columns). A capture is ONE setting: a second one takes another --out.
REMOTE = ["remote_mode", "remote_delay_us", "remote_bandwidth_mbps", "remote_page_bytes", "remote_wait_ns"]
COLUMNS = (["arm", "query", "param_idx", "regime", "step_idx", "step", "op"] + step_record.columns() + _TIMING + REMOTE
           + KV_FIELDS)


def columns(kv_timing):
    """steps.csv's columns: the timer's fields only when the capture ran with the timer on"""
    return COLUMNS + (KV_TIMER_FIELDS if kv_timing else [])


def remote_fields(resp, obj):
    """the REMOTE columns of one row: the response's settings + the step's (or the totals') measured wait"""
    r = resp.get("remote") or {}
    return {"remote_mode": r.get("mode", ""), "remote_delay_us": r.get("delay_us", 0 if r else ""),
            "remote_bandwidth_mbps": r.get("bandwidth_mbps", 0 if r else ""), "remote_page_bytes": r.get("page_bytes", ""),
            "remote_wait_ns": obj.get("remote_wait_ns", "")}


def remote_run(a):
    """run.remote of a request from the --remote-* flags (the page always; the rest only when the delay is on)"""
    if not a.remote_delay_us and (a.remote_bandwidth_mbps or a.remote_mode != "batch"):
        raise SystemExit("--remote-mode / --remote-bandwidth-mbps need --remote-delay-us above 0 (the regime is off without it)")
    r = {"page_bytes": a.remote_page_bytes}
    if a.remote_delay_us:
        r.update(delay_us=a.remote_delay_us, mode=a.remote_mode, bandwidth_mbps=a.remote_bandwidth_mbps)
    return r


def whole_key_channels(row):
    """scan_key_bytes / get_key_bytes as every key the op landed on: the hit lanes plus the absence misses' keys (the CSV
    columns are hit bytes only). A row without the miss columns gives its hit lanes. Values may be strings or numbers;
    returns floats.
    """
    f = lambda k: float(row.get(k) or 0)
    return {"scan_key_bytes": f("scan_key_bytes") + f("seek_miss_key_bytes") + f("next_miss_key_bytes"),
            "get_key_bytes": f("get_key_bytes") + f("get_miss_key_bytes")}


def codec_clock(steps, field):
    """a plan's codec clock (decode_ns / decompress_ns) summed over its steps; "" when the run did not time the codec
    (run.decode_timing off: the steps report null), never 0"""
    vals = [s["codec"][field] for s in steps]
    return "" if any(v is None for v in vals) else sum(vals)


def rows_of(arm, query, idx, regime, resp):
    out = []
    timer = "kv_timing" in resp               # the response carries its kv_timing block only when the timer ran
    for i, s in enumerate(resp["steps"]):
        r = {"arm": arm, "query": query, "param_idx": idx, "regime": regime, "step_idx": i, "step": s["name"], "op": s["op"],
             **step_record.fields(s), **remote_fields(resp, s),
             # a step's wall is its own clock (tasks + merge); at P=1 that IS its latency
             "wall_ns_min": s["wall_ns_min"], "wall_ns_max": s["wall_ns_max"],
             "plan_wall_ns": 0, "critical_ns": s.get("critical_ns", s["wall_ns"])}
        for f in KV_FIELDS:
            r[f] = (s["kv"] or {}).get(f, 0)
        if timer:
            for f in KV_TIMER_FIELDS:
                r[f] = (s["kv"] or {}).get(f, 0)
        out.append(r)
    t = resp["totals"]
    steps = resp["steps"]
    out.append({"arm": arm, "query": query, "param_idx": idx, "regime": regime, "step_idx": -1, "step": "__plan__", "op": "plan",
                **step_record.fields(t), **step_record.plan_fields(out), **remote_fields(resp, t), "kind": "plan",
                # the plan's output: the last step's rows. out_bytes is empty here: a latency run does not size the
                # outputs (run.emit.out_bytes = false); the counts run of the same cell has it
                "rows": len(resp.get("rows") or []) if isinstance(resp.get("rows"), list) else 1,
                "out_bytes": sum(s["out_bytes"] for s in steps) if all("out_bytes" in s for s in steps) else "",
                # the plan row's wall is the P=1 latency = work (the sum of the steps' own walls,
                # tasks + dispatch/merge); critical_ns is the critical path (what P=inf could reach);
                # plan_wall_ns is the interpreter's clock including its instrument work
                "wall_ns": t["work_ns"] + t.get("overhead_ns", 0), "wall_ns_min": min(t["wall_ns_all"]), "wall_ns_max": max(t["wall_ns_all"]),
                "plan_wall_ns": t["wall_ns"], "critical_ns": t["span_ns"],
                "decode_ns": codec_clock(steps, "decode_ns"),
                "decompress_ns": codec_clock(steps, "decompress_ns"),
                "chunks_decoded": sum(s["codec"]["chunks_decoded"] for s in steps),
                **{f: sum((s["kv"] or {}).get(f, 0) for s in steps) for f in KV_FIELDS},
                **({f: sum((s["kv"] or {}).get(f, 0) for s in steps) for f in KV_TIMER_FIELDS} if timer else {})})
    return out


# ---------------------------------------------------------------- the STEADY regime
# hit_rate is the DATA blocks' (block.cache.data.hit / miss): the plateau rule reads it. block_cache_hit / miss are every
# block's, index and filter partitions included since they share the bound
PASS_COLUMNS = ["arm", "pass", "phase", "cells", "block_cache_hit", "block_cache_miss", "hit_rate",
                "block_cache_add", "block_cache_bytes_write", "io_bytes_read", "wall_ns_sum", "block_cache_bytes", "direct_io",
                "data_hit", "data_miss", "server_minflt", "faults_per_s", "numa_hint_faults",
                "server_numa_faults_level"]


def _plan_wall(resp):
    """The P=1 latency of one run (the __plan__ row's wall_ns rule)."""
    t = resp["totals"]
    return t["work_ns"] + t.get("overhead_ns", 0)


plan_wall = _plan_wall      # the public name: a reader of raw steady.json runs (kvcost.latency.steady_table) uses the rule


def _pass_stats(resp):
    """The block-cache tickers and device bytes of one run (rocksdb.statistics_delta = that run)."""
    d = resp["rocksdb"]["statistics_delta"]
    return {"block_cache_hit": d.get("rocksdb.block.cache.hit", 0), "block_cache_miss": d.get("rocksdb.block.cache.miss", 0),
            "block_cache_add": d.get("rocksdb.block.cache.add", 0),
            "data_hit": d.get("rocksdb.block.cache.data.hit", 0), "data_miss": d.get("rocksdb.block.cache.data.miss", 0),
            # bytes inserted into the cache: with a cache larger than the working set, one pass's
            # sum is the suite's DISTINCT-block footprint (the sizing measure)
            "block_cache_bytes_write": d.get("rocksdb.block.cache.bytes.write", 0),
            "io_bytes_read": sum((s["kv"] or {}).get("io_bytes_read", 0) for s in resp["steps"])}


def check_bodies(cands, a):
    """the one-body rule, in every regime: a query names one body, unless --compare-bodies (warm and cold only: a
    steady or reuse run is one plan set over the queries)"""
    many = {q: names for q, names in sorted(cands.items()) if len(names) > 1}
    if not many:
        return
    shown = " ".join(f"{q}={','.join(names)}" for q, names in many.items())
    if a.steady or a.reuse_distance:
        raise SystemExit(f"cpplatency: --candidates {shown}: a steady or reuse run is one plan set over the queries: one "
                         f"body per query, --compare-bodies or not")
    if not a.compare_bodies:
        raise SystemExit(f"cpplatency: --candidates {shown}: one body per query (a latency run times the plan the "
                         f"layout runs). Timing several bodies of one query is exploratory: add --compare-bodies (its "
                         f"cells go under <arm>/<query>/<body>/p<idx>/; steady and reuse take one body per query even "
                         f"then)")


def batch_plans(cells):
    """{plan name: IR} of a batch over cells {(query, idx): (ir, args)}: one body per query (a steady or reuse pass is a
    plan set: --candidates names at most one body per query there)"""
    plans = {}
    for (q, _idx), (ir, _args) in sorted(cells.items()):
        if plans.setdefault(q, ir) is not ir and plans[q] != ir:
            raise SystemExit(f"{q}: two bodies in one plan set")
    return plans


def run_pass(srv, plans, cells, order, run, path_base):
    """[(key, response)] of one batch over `order` (keys of cells), each response in the single-request shape"""
    lines = [{"seq": i, "plan": k[0], "params": cells[k][1]} for i, k in enumerate(order)]
    _br, resps = run_lines(srv, plans, lines, path_base, run, keep_rows=True)
    return list(zip(order, resps))


def _slim(resp):
    """A response without its result rows (identical across a cell's runs; the median run keeps them)."""
    return {k: v for k, v in resp.items() if k != "rows"}


def steady_rows(arm, q, idx, record):
    """The steps rows of one steady cell from its record {median, measured, steady_at_pass}: the MEDIAN-wall measured
    run's rows with wall_ns_min / wall_ns_max over all its measured runs (the measure step and --regen share this)."""
    rs = record["measured"]
    rows = rows_of(arm, q, idx, "steady", record["median"])
    for r in rows:
        walls = [(_plan_wall(x) if r["step"] == "__plan__" else x["steps"][r["step_idx"]]["wall_ns"]) for x in rs]
        r["wall_ns_min"], r["wall_ns_max"] = min(walls), max(walls)
    return rows


def steady_run(perf_level, remote=None, kv_timing=False, decode_timing=False):
    """the run config of every steady pass: one measured run per request, the cache as the passes left it"""
    run = {"P": 1, "cache": "asis", "iterations": {"warmup": 0, "measured": 1}, "perf_level": perf_level,
           "emit": {"iterations": False, "histograms": False, "input_labels": False, "kvpair_stats": False, "out_bytes": False}}   # no RocksDB histogram snapshots: ~2.7 ms per request outside the clock
    if remote:
        run["remote"] = remote
    if kv_timing:
        run["kv_timing"] = True
    if decode_timing:
        run["decode_timing"] = True
    return run


def steady_pass(srv, arm, plans, cells, order, run, out, npass, phase, cache_bytes, direct_io, requests="batch", seen=None):
    """One steady pass over `order` (keys of cells): ([(key, response)], its pass row, its data-block hit rate). requests: "batch" = the pass as
    one batch request (measure/cppbatch.py; a failing request stops the batch and names its seq), "single" = one request
    per cell (the control)."""
    agg = {"block_cache_hit": 0, "block_cache_miss": 0, "block_cache_add": 0, "block_cache_bytes_write": 0, "io_bytes_read": 0,
           "data_hit": 0, "data_miss": 0}
    wall = 0
    f0 = numa_binding.snapshot(srv.proc.pid)
    if requests == "batch":
        got = run_pass(srv, plans, cells, order, run, os.path.join(out, f".batch-{arm}-steady-pass{npass}-p{os.getpid()}"))
    else:
        got = []
        for k in order:
            resp = srv.run(*cells[k], run=run)
            if resp.get("error"):
                raise SystemExit(f"{arm}/{k[0]}/p{k[1]}/steady: {resp['error']}")
            got.append((k, resp))
    f1 = numa_binding.snapshot(srv.proc.pid)
    for _k, resp in got:
        if seen is not None and "store_options" not in seen:      # what the server ran with (run.store_options)
            seen["store_options"] = (resp.get("run") or {}).get("store_options")
        for f, v in _pass_stats(resp).items():
            agg[f] += v
        wall += _plan_wall(resp)
    n = agg["data_hit"] + agg["data_miss"]       # the data blocks' rate: partition hits would dilute it
    rate = agg["data_hit"] / n if n else 0.0
    row = {"arm": arm, "pass": npass, "phase": phase, "cells": len(order), **agg, "hit_rate": round(rate, 5),
           "wall_ns_sum": wall, "block_cache_bytes": cache_bytes, "direct_io": int(direct_io),
           **numa_binding.fault_columns(f0, f1, wall, srv.proc.pid)}
    print(f"    steady pass {npass} ({phase}): hit rate {rate:.3f}, {agg['io_bytes_read'] / 1e6:.1f} MB read, "
          f"{wall / 1e6:.0f} ms, {row['faults_per_s']:.0f} faults/s", flush=True)
    return got, row, rate


def warm_passes(srv, arm, cells, eps, max_warm, seed, run, out, cache_bytes, direct_io, requests="batch", seen=None):
    """The steady warm-up on an already-bounded server: pass n runs steady_rule.pass_order(n) until steady_rule.settled
    (eps) or max_warm passes (then steady_at is None: not converged, recorded). -> (pass rows, steady_at: the passes run
    when it settled). run_steady and cppwlatency --warm-reads (the writes after a steady read warm-up) share it."""
    keys = sorted(cells)
    plans = batch_plans(cells)
    passes, prev = [], None
    while True:
        npass = len(passes)
        _got, row, rate = steady_pass(srv, arm, plans, cells, steady_rule.pass_order(keys, seed, npass), run, out, npass,
                                "warm", cache_bytes, direct_io, requests, seen)
        passes.append(row)
        if steady_rule.settled(prev, rate, eps):
            return passes, len(passes)
        if len(passes) >= max_warm:
            return passes, None
        prev = rate


def run_steady(srv, arm, cells, measured, eps, max_warm, seed, perf_level, out, cache_bytes, direct_io, raw_out=None,
               remote=None, kv_timing=False, requests="batch", timers=(), decode_timing=False, seen=None):
    """The steady regime for one arm on an already-bounded server: (steps rows, pass rows, ...).
    cells = {(query, idx): (ir, args)}. Every pass runs every cell once, in steady_rule's order (the same for every arm);
    no run evicts anything. Warm passes run until the pass hit rate moves by less than `eps` (or max_warm is hit: recorded,
    not hidden); then `measured` passes. A cell's rows come from its median-wall measured run, with wall_ns_min /
    wall_ns_max over all its measured runs. requests: "batch" (one request per pass, the protocol) or "single" (one per
    cell, a control). timers: the timer passes after the measured ones, each {kind, dir, level, passes, raw}; their results
    come back as the sixth item, {kind: {rows, pass_rows, kv_block, prov}} ({} without them).
    """
    run = steady_run(perf_level, remote, kv_timing, decode_timing)
    keys = sorted(cells)
    plans = batch_plans(cells)
    per_cell = {k: [] for k in keys}
    passes, steady_at = warm_passes(srv, arm, cells, eps, max_warm, seed, run, out, cache_bytes, direct_io,
                                    requests=requests, seen=seen)
    npass = len(passes)
    nmeasured = 0
    while True:                                  # the measured passes (at least one)
        order = steady_rule.measured_order(keys, seed, nmeasured)
        got, row, _rate = steady_pass(srv, arm, plans, cells, order, run, out, npass, "measured", cache_bytes,
                                      direct_io, requests, seen)
        for k, resp in got:
            per_cell[k].append(resp)
        passes.append(row)
        npass += 1
        nmeasured += 1
        if nmeasured >= measured:
            break
    timed = {}
    for t in timers:          # each timer capture's passes continue the measured passes' index
        timed[t["kind"]] = run_timer_passes(srv, arm, cells, plans, keys, seed, npass, nmeasured, t, run, requests,
                                            steady_at, cache_bytes, direct_io)
        npass += t["passes"]
        nmeasured += t["passes"]
    rows, prov, kv_block = [], None, None
    for k in keys:
        q, idx = k
        rs = per_cell[k]
        order = sorted(range(len(rs)), key=lambda i: _plan_wall(rs[i]))
        mid = rs[order[len(order) // 2]]             # the upper middle for an even count, as the executor
        prov = prov or mid.get("provenance")       # the run's binary / store identity, for the meta
        kv_block = kv_block or mid.get("kv_timing")
        record = {"median": mid, "measured": [_slim(x) for x in rs], "steady_at_pass": steady_at}
        rows += steady_rows(arm, q, idx, record)
        if raw_out is not None:                      # --raw jsonl: one line per cell, no file per cell (78,327 files for the sample quick)
            raw_out.write(json.dumps({"arm": arm, "query": q, "row": idx, "regime": "steady", **record}) + "\n")
        else:
            path = raw_path(out, arm, q, idx, "steady")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fo:   # json.dumps: the C encoder (json.dump is pure Python, ~5x slower)
                fo.write(json.dumps(record))
    return rows, passes, steady_at, prov, kv_block, timed


def read_cache_usage(srv, cells, perf_level, passes, timed):
    """always, after every steady pass (the timer passes too), ONE more request (the first cell, its blocks already cached) with
    run.emit.pinned_usage on: the block cache's capacity as the opened store holds it (the size its LRU cache was built
    with) and the cache's usage now. A read-only store's cache drops a block only to evict it, so when the usage now is at
    least every byte the passes inserted, nothing was evicted and the usage now is the run's peak. Outside every clock:
    no measured or timer wall moves."""
    k = sorted(cells)[0]
    run = {"P": 1, "cache": "asis", "iterations": {"warmup": 0, "measured": 1}, "perf_level": perf_level,
           "emit": {"iterations": False, "histograms": False, "input_labels": False, "kvpair_stats": False, "out_bytes": False,
                    "pinned_usage": True}}
    resp = srv.run(*cells[k], run=run)
    if resp.get("error"):
        raise SystemExit(f"{k[0]}/p{k[1]}/steady cache usage: {resp['error']}")
    kv = [s for s in resp["steps"] if "cache_usage_at_end" in s]
    if not kv:
        raise SystemExit(f"{k[0]}: the cache usage request's steps carry no cache_usage_at_end (the binary predates run.emit.pinned_usage)")
    inserted = sum(p["block_cache_bytes_write"] for p in passes) + sum(p["block_cache_bytes_write"]
                                                                       for t in timed.values() for p in t["pass_rows"])
    usage = max(s["cache_usage_at_end"] for s in kv)
    return {"cell": f"{k[0]}/p{k[1]}", "capacity_bytes": kv[0]["block_cache_bytes"], "usage_bytes": usage,
            "pinned_bytes": max(s["pinned_bytes_at_end"] for s in kv), "inserted_bytes": inserted,
            "nothing_evicted": usage >= inserted}


BREAKDOWN_PERF_LEVEL = 3          # RocksDB's per-operation timers (the PerfContext *_time columns, io_read_nanos)
KVTIMER_PERF_LEVEL = 2            # the kvtimer passes: the KV timer alone, RocksDB's counters as the walls of record


def run_timer_passes(srv, arm, cells, plans, keys, seed, npass, k0, timer, run, requests, steady_at, cache_bytes, direct_io):
    """TIMER passes after the measured ones (the same server and warmed cache): timer["passes"] passes at perf level
    timer["level"] with the KV timer on, pass i in the order steady_rule.measured_order(k0 + i). Each cell's rows come
    from its median-wall run of these passes (as the measured passes'), its raw record keeps every run. npass: the
    server's pass count so far (the pass rows' numbers). Returns {rows, pass_rows, kv_block, prov}."""
    brun = dict(run, perf_level=timer["level"], kv_timing=True)
    out, raw_out, kind = timer["dir"], timer["raw"], timer["kind"]
    per_cell = {k: [] for k in keys}
    pass_rows, kv_block, prov = [], None, None
    for i in range(timer["passes"]):
        order = steady_rule.measured_order(keys, seed, k0 + i)
        f0 = numa_binding.snapshot(srv.proc.pid)
        if requests == "batch":
            got = run_pass(srv, plans, cells, order, brun, os.path.join(out, f".batch-{arm}-steady-{kind}-p{os.getpid()}"))
        else:
            got = []
            for k in order:
                resp = srv.run(*cells[k], run=brun)
                if resp.get("error"):
                    raise SystemExit(f"{arm}/{k[0]}/p{k[1]}/steady {kind}: {resp['error']}")
                got.append((k, resp))
        f1 = numa_binding.snapshot(srv.proc.pid)
        agg = {"block_cache_hit": 0, "block_cache_miss": 0, "block_cache_add": 0, "block_cache_bytes_write": 0, "io_bytes_read": 0,
               "data_hit": 0, "data_miss": 0}
        wall = 0
        for k, resp in got:
            for f, v in _pass_stats(resp).items():
                agg[f] += v
            wall += _plan_wall(resp)
            if "kv_timing" not in resp:
                raise SystemExit(f"{arm}/{k[0]}: the {kind} pass's response has no kv_timing block (the binary predates it)")
            kv_block = kv_block or resp.get("kv_timing")
            prov = prov or resp.get("provenance")
            per_cell[k].append(resp)
        n = agg["data_hit"] + agg["data_miss"]           # the data blocks' rate, as the measured passes'
        rate = agg["data_hit"] / n if n else 0.0
        pass_rows.append({"arm": arm, "pass": npass + i, "phase": kind, "cells": len(order), **agg, "hit_rate": round(rate, 5),
                          "wall_ns_sum": wall, "block_cache_bytes": cache_bytes, "direct_io": int(direct_io),
                          **numa_binding.fault_columns(f0, f1, wall, srv.proc.pid)})
        print(f"    steady pass {npass + i} ({kind}, perf {timer['level']} + KV timer): hit rate {rate:.3f}, "
              f"{wall / 1e6:.0f} ms", flush=True)
    rows = []
    for k in keys:
        q, idx = k
        rs = per_cell[k]
        mid = rs[sorted(range(len(rs)), key=lambda i: _plan_wall(rs[i]))[len(rs) // 2]]   # the upper middle, as run_steady
        record = {"median": mid, "measured": [_slim(x) for x in rs], "steady_at_pass": steady_at,
                  "timer_passes": list(range(npass, npass + timer["passes"])), "perf_level": timer["level"]}
        rows += steady_rows(arm, q, idx, record)
        if raw_out is not None:
            raw_out.write(json.dumps({"arm": arm, "query": q, "row": idx, "regime": "steady", **record}) + "\n")
        else:
            path = raw_path(out, arm, q, idx, "steady")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fo:
                fo.write(json.dumps(record))
    return {"rows": rows, "pass_rows": pass_rows, "kv_block": kv_block, "prov": prov}


# ---------------------------------------------------------------- the REUSE-DISTANCE pass (opt-in; counts, no clock)
REUSE_COLUMNS = ["arm", "pass", "order", "query", "param_idx", "step_idx", "step", "op", "kind", "S", "G", "N",
                 "fetched_kvbytes", "reuse_distance_kvbytes", "reuse_distance_kvpair_blocks",
                 "reuse_distance_request_blocks"]
# the named cache sizes, as power-of-two bucket edges: bucket i holds distances in [2^(i-1), 2^i) bytes
REUSE_EDGES = (("under_1mb", 20), ("under_16mb", 24), ("under_128mb", 27), ("under_1gb", 30))


def reuse_text(h):
    """a response step's `reuse` ({bucket: bytes, "new": bytes}) as "bucket:bytes" pairs in bucket order, "new" last"""
    return " ".join(f"{k}:{h[k]}" for k in sorted(h, key=lambda k: (k == "new", int(k) if k != "new" else 0)))


def run_reuse(srv, arm, cells, passes, seed, out):
    """The reuse-distance pass for one arm: every cell once per pass in run_steady's order (random.Random(seed + pass)),
    P=1, the tracker reset before the first request, so pass 0 is a cold start and a later pass the steady stream.
    Per step: its fetched kvbytes by the reuse distance of each item (cpp/kvevaluator/src/store/reuse_distance.h), and by
    the same buckets the sum of each item's own 4 KB blocks, ceil(item bytes / 4096): the bytes a cache misses divided
    by 4096 are the fewest device reads the missed items cost, that sum the most; and between them each REQUEST's bytes
    per bucket rounded up once (the keys of one range read are contiguous; a GET is its own request).
    Returns REUSE_COLUMNS rows; deterministic (no clock is read)."""
    keys = sorted(cells)
    plans = batch_plans(cells)
    rows = []
    for npass in range(passes):
        order = steady_rule.pass_order(keys, seed, npass)
        got = []
        if not order:
            continue                                 # no cell: the caller's "0 rows" check fails the run
        # one batch per pass (measure/cppbatch.py); the very first request alone, as it resets the tracker
        for part, reset in (([order[0]], True), (order[1:], False)) if npass == 0 else ((order, False),):
            run = {"P": 1, "cache": "asis", "iterations": {"warmup": 0, "measured": 1}, "perf_level": 1,
                   "reuse_distance": True, "reuse_reset": reset,
                   "emit": {"iterations": False, "histograms": False, "input_labels": False, "kvpair_stats": False}}
            if part:
                got += run_pass(srv, plans, cells, part, run,
                                os.path.join(out, f".batch-{arm}-reuse-pass{npass}{'-reset' if reset else ''}-p{os.getpid()}"))
        for n, (k, resp) in enumerate(got):
            for i, st in enumerate(resp["steps"]):
                if "reuse" not in st:
                    raise SystemExit(f"{arm}/{k[0]}: the binary returned no reuse distances (it predates run.reuse_distance)")
                # every fetched byte is in exactly one bucket: the GET hits, every cursor landing's key (hit or miss) and
                # the values read (P=1: the physical fetches are the logical counts)
                b = st["bytes"]
                lanes = sum(b[x] for x in ("get_key", "get_value", "scan_key", "scan_value", "seek_miss_key", "next_miss_key"))
                if sum(st["reuse"].values()) != lanes:
                    raise SystemExit(f"{arm}/{k[0]}/p{k[1]} step {st['name']}: reuse-distance bytes {sum(st['reuse'].values())} "
                                     f"!= the fetched lanes {lanes}")
                if "reuse_blocks" not in st or "reuse_request_blocks" not in st:
                    raise SystemExit(f"{arm}/{k[0]}: the binary returned no reuse_blocks / reuse_request_blocks (it predates them)")
                # per bucket: bytes / 4096 <= the requests' blocks <= the items' blocks (a cursor that outlived its step
                # would break the first)
                order_bad = [x for x in st["reuse"] if not (st["reuse"][x] <= st["reuse_request_blocks"].get(x, 0) * 4096
                                                            and st["reuse_request_blocks"].get(x, 0) <= st["reuse_blocks"].get(x, 0))]
                if order_bad or set(st["reuse_request_blocks"]) != set(st["reuse"]):
                    raise SystemExit(f"{arm}/{k[0]}/p{k[1]} step {st['name']}: not bytes / 4096 <= request blocks <= item blocks "
                                     f"at {sorted(order_bad)}")
                # per bucket: items fill at least their bytes / 4096 blocks, and a bucket has blocks iff it has bytes
                bad = [x for x in set(st["reuse"]) | set(st["reuse_blocks"])
                       if st["reuse_blocks"].get(x, 0) * 4096 < st["reuse"].get(x, 0) or (st["reuse_blocks"].get(x, 0) > 0) != (st["reuse"].get(x, 0) > 0)]
                if bad:
                    raise SystemExit(f"{arm}/{k[0]}/p{k[1]} step {st['name']}: reuse_blocks below bytes / 4096, or on an empty "
                                     f"bucket, at {sorted(bad)}")
                rows.append({"arm": arm, "pass": npass, "order": n, "query": k[0], "param_idx": k[1], "step_idx": i,
                             "step": st["name"], "op": st["op"], "kind": st["kind"], "S": st["S"], "G": st["G"], "N": st["N"],
                             "fetched_kvbytes": sum(st["reuse"].values()), "reuse_distance_kvbytes": reuse_text(st["reuse"]),
                             "reuse_distance_kvpair_blocks": reuse_text(st["reuse_blocks"]),
                             "reuse_distance_request_blocks": reuse_text(st["reuse_request_blocks"])})
    return rows


def reuse_summary(rows):
    """per (arm, pass): the fetched kvbytes and their split at the named cache sizes (a cache of that size misses the
    bytes at or beyond it, plus the never-seen ones)"""
    out = {}
    for r in rows:
        d = out.setdefault((r["arm"], r["pass"]), {"arm": r["arm"], "pass": r["pass"], "fetched_kvbytes": 0, "never_seen": 0,
                                                    **{n: 0 for n, _ in REUSE_EDGES}, "at_or_over_1gb": 0})
        for x in r["reuse_distance_kvbytes"].split():
            b, n = x.split(":")
            n = int(n)
            d["fetched_kvbytes"] += n
            if b == "new":
                d["never_seen"] += n
            else:
                d[next((name for name, edge in REUSE_EDGES if int(b) <= edge), "at_or_over_1gb")] += n
    return [out[k] for k in sorted(out)]


def candidate_bodies(workload, q, arm, sf, names, bundle_ir, cand_set="grid", spec=None):
    """[(name, IR)] for --candidates: '(pin)' -> the committed bundle; any other name -> one of the plan files' own
    bodies and twins (kvevaluator.plan_choice.candidates.candidates: a bundle body, or "<twin>:<body>"; the names a meta-grid
    capture's `variant` column carries), forced as a single-body IR on this store's format + chunk (its capabilities
    decide which candidates are valid). `cand_set`: "grid", the one set. `spec`: a meta store's spec file."""
    from kvevaluator.capture.store_identity import store_chunk_target
    from kvevaluator.plan_choice import candidates as C
    from kvevaluator.formats.caps import caps_for, caps_resolver
    from kvevaluator.plan_choice.cells import workload_schema
    if cand_set != "grid":
        raise ValueError(f"candidate set {cand_set!r}: only 'grid' exists")
    schema = workload_schema(workload)                  # the workload's dataset's schema, as the counts use it
    where = arm_store_dir(workload, sf, arm, spec)      # the store on the workload's dataset
    fmt = json.load(open(os.path.join(where, "manifest.json")))["format"]
    chunk = store_chunk_target(where)
    invalid = C.invalid_candidates(workload, q, caps_for(fmt, schema, chunk=chunk, spec=spec),
                                   caps_resolver(fmt, schema, chunk=chunk, spec=spec))
    cands = [(n, ir) for n, ir in C.candidates(workload, q)[1] if n not in invalid]
    by_name = dict(cands)
    out = []
    for name in names:
        if name == "(pin)":
            out.append((name, bundle_ir))
        elif name in by_name:
            out.append((name, by_name[name]))
        else:
            raise SystemExit(f"{arm}/{q}: no candidate {name!r} on this store (valid: {sorted(by_name)}; "
                             f"invalid: {invalid})")
    return out


def steady_cells(workload, sf, arm, fmt, queries, nparam, params_source, lo_family, cands, cand_set="grid", spec=None,
                 window=None):
    """The cells of a steady (or reuse-distance) run on one arm, one PLAN SET: {(query, idx): (ir, args)}, {query: the
    candidate forced}, {arm/query/candidate: sha256 of its IR}. A query `cands` names runs that candidate, forced from the
    whole file; any other query the body the pick record names for this store. cppwlatency --warm-reads rebuilds a
    steady capture's cells with it."""
    from kvevaluator.measure.plan_runs import ir_for, ir_to_run
    from kvevaluator.plans.ir.bundle import file_sha
    cells, forced, shas = {}, {}, {}
    for q in queries:
        ir = (ir_for(workload, q, lo_family) if q in cands else
              ir_to_run(workload, q, fmt, arm_store_dir(workload, sf, arm, spec), lo_family, spec=spec))
        if q in cands:
            (forced[q], ir), = candidate_bodies(workload, q, arm, sf, cands[q], ir, cand_set, spec)
            shas[f"{arm}/{q}/{forced[q]}"] = file_sha(ir, 64)
        plist = list(enumerate(workload_plans.query_params(workload, sf, q, nparam if not window else 0, params_source), 1))
        if window:
            plist = [(i, args) for i, args in plist if window[0] <= i <= window[1]]
        for idx, args in plist:
            cells[(q, idx)] = (ir, args)
    return cells, forced, shas


def regen(a):
    """steps.csv from the saved responses: the measure step's other half, re-runnable."""
    csv_path = os.path.join(a.out, "steps.csv")
    n = 0
    jsonl = os.path.join(a.out, "responses.jsonl.gz")          # --raw jsonl: every cell is a line here, no per-cell files
    if not any(os.path.isdir(os.path.join(a.out, arm, q)) for arm in a.arms for q in a.queries) and not os.path.exists(jsonl):
        raise SystemExit(f"--regen: no saved cells under {a.out} for arms {a.arms} -- refusing to "
                         f"overwrite {csv_path} with an empty table")

    def cell_rows(arm, q, idx, regime, rec):
        # a warm / cold file holds the response; a steady file holds the record {median, measured, steady_at_pass}
        return steady_rows(arm, q, idx, rec) if regime == "steady" and "median" in rec else rows_of(arm, q, idx, regime, rec)
    # the kvstats layout's files carry `run`; a rebuilt file keeps the column (blank: the
    # per-cell JSONs do not record the run) so a later --accumulate can merge into it
    # the cells and their shape come from measure/latency_raw_files.py: a one-body run's under <arm>/<query>/p<idx>/,
    # its body's name from the meta's `candidates`; a --compare-bodies run's under <arm>/<query>/<body>/p<idx>/, the
    # name from the folder. The `candidate` column appears when a body was named
    meta = os.path.join(a.out, "steps.csv.meta.json")
    named = (json.load(open(meta)).get("candidates") or {}) if os.path.exists(meta) else {}
    cells = {(arm, q): raw_cells(a.out, arm, q) for arm in a.arms for q in a.queries
             if os.path.isdir(os.path.join(a.out, arm, q))}
    has_cands = any(q in named for _arm, q in cells) or any(b for c in cells.values() for b, *_ in c)
    with open(csv_path, "w", newline="") as f:
        # the timer's columns when the capture's (latest) meta says the timer ran (a meta from before the flag: off)
        timer = os.path.exists(meta) and any(timer_by_regime(json.load(open(meta))).values())
        w = csv.DictWriter(f, fieldnames=columns(timer) + ["run"] + (["candidate"] if has_cands else []))
        w.writeheader()
        for arm in a.arms:
            for q in a.queries:
                for body, idx, regime, path in cells.get((arm, q), []):
                    cand = body or (named[q][0] if q in named else None)
                    for r in cell_rows(arm, q, idx, regime, json.load(open(path))):
                        if cand:
                            r["candidate"] = cand
                        w.writerow(r)
                    n += 1
        if os.path.exists(jsonl):
            with gzip.open(jsonl, "rt") as f:
                for line in f:
                    rec = json.loads(line)
                    if rec["arm"] not in a.arms or rec["query"] not in a.queries:
                        continue
                    if rec.get("candidate") and not has_cands:
                        raise SystemExit(f"--regen: a candidate cell in {jsonl} ({rec['arm']}/{rec['query']}/{rec['candidate']}); "
                                         "candidate runs keep per-cell files (no --raw jsonl)")
                    for r in cell_rows(rec["arm"], rec["query"], int(rec["row"]), rec["regime"], rec.get("response", rec)):
                        if rec.get("candidate"):
                            r["candidate"] = rec["candidate"]
                        w.writerow(r)
                    n += 1
    print(f"regenerated {csv_path} from {n} cells")


def arm_store_dir(workload, sf, arm, spec=None):
    """An arm's store on the workload's dataset: output/stores/<dataset>/<scale>/<arm>,
    imdb-<mapping>/<tag>/<arm> (stores.store_dir). `spec`: a meta arm's spec file (store_path finds its store)."""
    from kvevaluator import workloads
    return store_dir(workloads.get(workload).dataset, sf, arm, spec)


def request_params(a, q):
    """[(row, args)]: the params a query runs, row = its 1-based row in the params source (a stable key across arms).
    LinkBench: --hubs K = the K highest-degree anchors (getlinklist / countlink: the hub stratum); --subsample N
    --seed S = a seeded N-row subset of the source (a query with <= N rows runs in full), the same on every arm."""
    if a.nodes:
        return [(i, {"node": int(n)}) for i, n in enumerate(a.nodes.split(","), 1)]
    if a.hubs:
        from kvevaluator.datasets.linkbench.params import anchors_path
        with open(anchors_path(a.sf)) as f:
            next(f)
            nodes = sorted(((int(d), int(n)) for n, d in (ln.rstrip("\n").split("|") for ln in f)), reverse=True)
        return [(i, {"node": n}) for i, (_d, n) in enumerate(nodes[:a.hubs], 1)]
    rows = list(enumerate(workload_plans.query_params(a.workload, a.sf, q, 0 if a.subsample else a.nparam,
                                                      a.params_source), 1))
    if a.subsample and len(rows) > a.subsample:
        import random
        keep = sorted(random.Random(f"{a.seed}:{q}").sample(range(len(rows)), a.subsample))
        rows = [rows[i] for i in keep]
    return rows


def index_filter_blocks(sf, arm, workload="interactive", spec=None):
    """The store's index + filter block setting (manifest; stores from before the field are whole)."""
    with open(os.path.join(arm_store_dir(workload, sf, arm, spec), "manifest.json")) as f:
        return store_layout.of_manifest(json.load(f))


def earlier_index_filter_blocks(csv_path):
    """{arm: setting} the capture's earlier run metas recorded (a meta from before the field: whole)."""
    out = {}
    for p in sorted(glob.glob(csv_path + ".meta*.json")):
        m = json.load(open(p))
        rec = m.get("index_filter_blocks") or {}
        for arm in m.get("arms") or []:
            out.setdefault(arm, rec.get(arm, "whole"))
    return out


def timer_by_regime(meta):
    """{regime: timer on} of one run meta (a meta from before the timer: off in every regime it ran)"""
    return meta.get("kv_timing_by_regime") or {r: False for r in meta.get("regimes") or {}}


def earlier_kv_timing(csv_path):
    """{regime: {timer settings}} over the capture's earlier run metas"""
    out = {}
    for p in glob.glob(csv_path + ".meta*.json"):
        for r, on in timer_by_regime(json.load(open(p))).items():
            out.setdefault(r, set()).add(on)
    return out


def earlier_index_filter_in_cache(csv_path):
    """{regime: {in the cache?}} over the capture's earlier run metas (capture_protocol.index_filter_of: a meta from
    before the field had steady outside, warm / cold inside)"""
    out = {}
    for p in glob.glob(csv_path + ".meta*.json"):
        for r, st in capture_protocol.index_filter_of(json.load(open(p))).items():
            out.setdefault(r, set()).add(st["in_cache"])
    return out


def earlier_requests(csv_path):
    """{regime: {request protocols}} over the capture's earlier run metas: batch or single (a meta without the field:
    single)"""
    out = {}
    for p in glob.glob(csv_path + ".meta*.json"):
        m = json.load(open(p))
        by = m.get("requests_by_regime") or {}
        for r, st in (m.get("regimes") or {}).items():
            got = by.get(r) or (st.get("requests") if isinstance(st, dict) and r == "steady" else None) or "single"
            out.setdefault(r, set()).add(got)
    return out


def earlier_allocators(csv_path):
    """The allocators the capture's earlier run metas name (a meta from before the field: glibc)."""
    return sorted({((json.load(open(p)).get("provenance") or {}).get("allocator") or {}).get("name", "glibc")
                   for p in glob.glob(csv_path + ".meta*.json")})


def resolve_direct_io(a):
    """the read path of each server (inputs/contract/store_config.json: direct I/O in the timed regimes, steady and cold):
    cold and steady read direct unless --buffered-reads (a control); a warm-only run stays buffered (everything it times
    is in its block cache); a run of warm and cold shares one server, so it must say which"""
    if a.direct_io and a.buffered_reads:
        raise SystemExit("--direct-io and --buffered-reads: pick one")
    if a.warm and a.cold and not (a.direct_io or a.buffered_reads):
        raise SystemExit("warm and cold in one call open one server: say --direct-io (the cold default) or --buffered-reads")
    a.direct_io = a.direct_io or (bool(a.cold) and not a.warm and not a.buffered_reads)
    a.steady_direct_io = not a.buffered_reads
    timers = ((a.warm or a.cold) and a.perf_level >= BREAKDOWN_PERF_LEVEL) or (a.steady and a.steady_perf_level >= BREAKDOWN_PERF_LEVEL)
    if timers and not a.breakdown_pass:
        raise SystemExit(f"PerfLevel >= {BREAKDOWN_PERF_LEVEL} runs RocksDB's per-call timers inside the walls: a timed pass "
                         "runs at 0-2 (inputs/contract/store_config.json); say --breakdown-pass for a pass that keeps them on purpose")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--family", dest="workload", choices=list(workload_plans.WORKLOADS), default="interactive",
                    help="the suite / plan files / params (measure/workload_plans.py)")
    ap.add_argument("--format", default="pertype_csr", help="the format every arm's store holds")
    ap.add_argument("--arms", nargs="+", default=["pertype_csr-c64k", "pertype_csr-c64k-zstd"])
    ap.add_argument("--queries", nargs="+", default=None,
                    help="default: the workload's whole suite")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--warm", default="1,5", help="warmup,measured iterations for the WARM regime ('' = skip)")
    ap.add_argument("--cold", default="3", help="measured iterations for the COLD regime ('' = skip)")
    ap.add_argument("--block-cache-gb", type=float, default=8)
    ap.add_argument("--direct-io", action="store_true",
                    help="open the warm/cold server with use_direct_reads: a block-cache miss reads the device, not the "
                         "OS page cache. The default for a cold run (inputs/contract/store_config.json); a run of warm AND cold "
                         "in one call opens one server and must say --direct-io or --buffered-reads")
    ap.add_argument("--breakdown-pass", action="store_true",
                    help="this run keeps RocksDB's per-call timers on purpose (PerfLevel 3 in warm, cold or steady): a "
                         "breakdown, or an arm that measures the timers' cost; never walls of record. Without it a level "
                         "of 3 or more is refused (inputs/contract/store_config.json: no per-call timers in a timed pass)")
    ap.add_argument("--buffered-reads", action="store_true",
                    help="a CONTROL: cold and steady read through the OS page cache (no use_direct_reads), the protocol "
                         "the readahead-off cold setting; never for walls of record")
    ap.add_argument("--steady", default=None, metavar="CACHE_MB,PASSES",
                    help="the STEADY regime: block cache of CACHE_MB (the same absolute size for every "
                         "format), the suite interleaved, PASSES measured passes after the hit rate "
                         "plateaus (run_steady); '' / absent = skip")
    ap.add_argument("--reuse-distance", type=int, default=0, metavar="PASSES",
                    help="the opt-in reuse-distance pass INSTEAD of timing: every cell of the suite once per pass in the "
                         "steady regime's order (--steady-seed), per step its fetched kvbytes by reuse distance -> "
                         "<out>/reuse-distance.csv and reuse-distance-summary.csv; no clock, no cache bound")
    ap.add_argument("--steady-requests", choices=["batch", "single"], default="batch",
                    help="how a steady pass reaches the server: batch = one batch request per pass (the protocol; the "
                         "mixed replay uses it too); single = one request per cell through Python, "
                         "for controls only (its idle time between plans is ~290 us against ~161 us, and "
                         "some queries' walls move with it: experiments/steady_cache/README.md)")
    ap.add_argument("--steady-eps", type=float, default=0.005, help="steady when the pass hit rate moves less than this")
    ap.add_argument("--steady-max-warm", type=int, default=8, help="warm passes at most (not converged is recorded)")
    ap.add_argument("--steady-seed", type=int, default=1, help="the per-pass shuffle seed base (same for every arm)")
    ap.add_argument("--steady-perf-level", type=int, default=2,
                    help="RocksDB PerfLevel for steady (2 = counters without timers: block reads and cache hits per step, no timer tax)")
    ap.add_argument("--steady-direct-io", action="store_true",
                    help="open the steady store with use_direct_reads (the default: inputs/contract/store_config.json; the flag is kept for the "
                         "drivers that pass it; --buffered-reads turns it off)")
    ap.add_argument("--steady-index-filter-outside", action="store_true",
                    help="a CONTROL: cache_index_and_filter_blocks off in steady. The setting needs "
                         "it on a WHOLE store (+ --allow-whole-index-filter): on a partitioned store RocksDB still caches "
                         "the partitions, only the top levels stay outside. Default: inside the bound, so each format pays "
                         "for the metadata it touches (partitioned stores: 4 KB partitions under a pinned top level)")
    ap.add_argument("--allow-whole-index-filter", action="store_true",
                    help="a CONTROL: stores whose index + filter layout is not this binary's ingest default "
                         "(python/kvevaluator/measure/store_layout.py) are timed and marked protocol_mixed either way; "
                         "the flag names the run a control")
    ap.add_argument("--sample-every", type=int, default=0,
                    help="Stage B: 1-in-K per-op rdtsc sampling (0 = off; run.sample_every)")
    ap.add_argument("--kv-timing", default="off", choices=["off", "steady", "all"],
                    help="the KV-boundary timer (run.kv_timing): off (default: the walls of record), in the steady "
                         "regime only, or in warm / cold / steady (all): per step the time in "
                         "every RocksDB call (kv_call_ns and per kind), reverse reads included. It runs inside the plan "
                         "clock, so it adds its own cost to the wall (the response's kv_timing block has the timer "
                         "pair's cost). For the per-call breakdown beside untaxed steady walls: --steady-breakdown. "
                         "Not used by --reuse-distance")
    ap.add_argument("--decode-timing", action="store_true",
                    help="time our codec (run.decode_timing): decode_ns / decompress_ns per step, in every regime. Off "
                         "(default: the walls of record), the codec takes no clock and those columns are empty; the KV "
                         "timer (--kv-timing, the breakdown pass) turns it on for its own passes")
    ap.add_argument("--steady-breakdown", default=None, metavar="DIR",
                    help="after the measured steady passes (and the kvtimer passes), --steady-breakdown-passes passes at "
                         "perf level 3 with the KV timer, written as its own capture in DIR (capture_kind breakdown): the "
                         "per-call timers, beside the walls of record in --out. Needs --steady; not with --accumulate")
    ap.add_argument("--steady-breakdown-passes", type=int, default=1, metavar="N", help="the breakdown capture's passes")
    ap.add_argument("--steady-kvtimer", default=None, metavar="DIR",
                    help="after the measured steady passes, --steady-kvtimer-passes passes at perf level 2 with the KV "
                         "timer, its own capture in DIR (capture_kind breakdown): the time inside RocksDB calls with no "
                         "PerfContext timer in it. Needs --steady; not with --accumulate")
    ap.add_argument("--steady-kvtimer-passes", type=int, default=1, metavar="N", help="the kvtimer capture's passes")
    ap.add_argument("--perf-level", type=int, default=2,
                    help="RocksDB PerfLevel for warm / cold (default 2: counters, no per-call timer, inputs/contract/store_config.json; 3 = "
                         "per-op timers ON, ~55 ns inside every forward Next: a breakdown pass only, named as one)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--require-clean", action="store_true",
                    help="refuse to run from a driver whose files differ from HEAD (the meta's driver SHA must be the code)")
    ap.add_argument("--params-source", default=None, choices=["anchors", "quick", "quick-next", "log"],
                    help="LinkBench: its param set (datasets/linkbench/params.py PARAM_SETS): degree-stratified anchors, "
                         "a sample of LinkBench's own request log (quick, quick-next), or the whole log (log, the default)")
    ap.add_argument("--subsample", type=int, default=0, metavar="N",
                    help="a seeded N-row subset per query (all rows when fewer), the same on every arm; overrides --nparam")
    ap.add_argument("--seed", default="1", help="the --subsample seed (per query: '<seed>:<query>')")
    ap.add_argument("--hubs", type=int, default=0, metavar="K",
                    help="LinkBench: the K highest-degree anchors as the params (getlinklist / countlink: the hub stratum)")
    ap.add_argument("--nodes", default="", metavar="ID[,ID]",
                    help="LinkBench: these node ids as every query's params (the request log's own hub requests)")
    ap.add_argument("--raw", default="files", choices=["files", "jsonl"],
                    help="raw responses: one JSON per cell (files) or one gzip'd JSONL per run (jsonl: responses.jsonl.gz, "
                         "one line per response with its arm / query / row / regime)")
    ap.add_argument("--schema", default="canonical",
                    help="the physical design the arms were ingested with: a non-canonical schema "
                         "runs its own ldbc plan family (plan_runs.label_only_family) and "
                         "opens the store with its schema artifact")
    ap.add_argument("--allow-mixed-index-filter-blocks", action="store_true",
                    help="let --accumulate add rows of another index/filter layout than the capture's earlier runs")
    ap.add_argument("--accumulate", default=None, metavar="RUN_ID",
                    help="UPSERT this run's rows into an existing steps.csv (keyed by regime, query, "
                         "param, step; stamped with RUN_ID) instead of overwriting it -- the kvstats "
                         "layout's rule (measure/kvstats_layout); the per-cell JSONs are written either way")
    ap.add_argument("--regen", action="store_true",
                    help="rebuild steps.csv from the per-cell JSONs already under --out (no measurement)")
    ap.add_argument("--candidates", nargs="+", default=None, metavar="QUERY=BODY",
                    help="run a NAMED body per query instead of the committed bundle: '(pin)' = the bundle as C++ "
                         "selects it; any other name = that candidate of the plan files (a bundle body or "
                         "<twin>:<body>, as a meta-grid capture names it), forced as a single-body IR. One body per "
                         "query (a latency run times the plan the layout runs); rows gain a `candidate` column, the "
                         "meta records the names, and the cell JSON stays at <arm>/<query>/p<idx>/. Several bodies: "
                         "--compare-bodies")
    ap.add_argument("--compare-bodies", action="store_true",
                    help="exploratory: let --candidates name several bodies per query (QUERY=BODY,BODY), timed "
                         "against each other: within a (param, regime) they run interleaved, their order alternated "
                         "per regime; each cell JSON lives under <arm>/<query>/<body>/p<idx>/ and the meta records "
                         "compare_bodies. Warm and cold only: a steady or reuse run is one plan set")
    ap.add_argument("--candidate-set", default="grid", choices=["grid"],
                    help="with --candidates: which names they are: grid = the plan files' bodies and twins "
                         "(python/kvevaluator/plan_choice/candidates.py: a bundle body or <twin>:<body>), the `variant` names "
                         "of a meta-grid capture (the one set; the pin check's set is retired)")
    ap.add_argument("--params", default=None, metavar="LO-HI",
                    help="with --candidates: the 1-based param window to run instead of the first --nparam (the held-out "
                         "params, so every measured body's counts can be checked against its rows)")
    ap.add_argument("--alternate", type=int, default=0,
                    help="with --compare-bodies: flips the bodies' starting order (one regime per invocation: pass 0 / 1 "
                         "alternately so the order is not tied to the param parity alone)")
    ap.add_argument("--remote-delay-us", type=int, default=0,
                    help="the REMOTE regime: a real wait (a sleep) of this many microseconds per round trip, so the plan "
                         "is timed as against a store one network hop away (0 = off, today's regime; at least 100). "
                         "Counts do not change. One setting per --out (no --accumulate)")
    ap.add_argument("--remote-mode", default="batch", choices=["operation", "batch", "step"],
                    help="where the network boundary sits: operation = every GET, range read and page waits; batch = a "
                         "step waits once per level of requests that can be sent together (round_trips_critical); "
                         "step = a step that reads or writes waits once")
    ap.add_argument("--remote-page-bytes", type=int, default=1 << 20,
                    help="the page of a range read: every further page is one more request (also sets the "
                         "round_trips columns with the delay off)")
    ap.add_argument("--remote-bandwidth-mbps", type=float, default=0,
                    help="with --remote-delay-us: the fetched keys + values also wait bytes / this bandwidth (0 = off)")
    numa_binding.add_argument(ap)
    add_spec_arg(ap)
    a = ap.parse_args(argv)
    a.meta_spec = cli_spec(a.spec) if a.format == "meta" else None
    if not a.regen:              # --regen rebuilds steps.csv from saved cells: it opens no server
        resolve_direct_io(a)
    if a.alternate and not a.compare_bodies:
        raise SystemExit("cpplatency: --alternate orders several bodies of one query: it goes with --compare-bodies")
    if a.compare_bodies and not a.candidates:
        raise SystemExit("cpplatency: --compare-bodies times the bodies --candidates names: name them")
    if a.remote_delay_us and a.accumulate:
        raise SystemExit("--remote-delay-us with --accumulate: a capture holds one remote setting; use one --out per setting")
    cands = {}
    if a.candidates:
        if a.accumulate:
            raise SystemExit("--candidates with --accumulate: the kvstats upsert has no candidate key; use one --out "
                             "per (regime, run) instead")
        for item in a.candidates:
            q, bodies = item.split("=", 1)
            cands[q] = bodies.split(",")             # ',' : an ldbc label itself joins its rules with '+'
        check_bodies(cands, a)
        if a.queries is None:
            # the per-query path runs the named cells alone; a STEADY run is a plan set over the WHOLE suite (the
            # forced queries inside it), else the bounded cache sees 15 cells instead of 95 and reads like warm
            a.queries = workload_plans.queries(a.workload) if a.steady or a.reuse_distance else sorted(cands)
        unknown = sorted(set(cands) - set(a.queries))
        if unknown:
            raise SystemExit(f"--candidates names queries not in --queries: {unknown}")
    if a.regen:
        if a.queries is None:
            # rebuild from what is ON DISK, whatever workload/queries the run used (the suite
            # default would drop every non-ldbc cell and leave a header-only csv)
            a.queries = sorted({q for arm in a.arms if os.path.isdir(os.path.join(a.out, arm))
                                for q in os.listdir(os.path.join(a.out, arm))})
        return regen(a)
    if a.queries is None:
        a.queries = workload_plans.queries(a.workload)

    regimes = []
    if a.warm:
        k, m = (int(x) for x in a.warm.split(","))
        regimes.append(("warm", {"P": 1, "cache": "warm", "iterations": {"warmup": k, "measured": m},
                                 "sample_every": a.sample_every, "perf_level": a.perf_level, "emit": {"histograms": False, "input_labels": False, "kvpair_stats": False, "out_bytes": False},
                                 "remote": remote_run(a), **({"kv_timing": True} if a.kv_timing == "all" else {}),
                                 **({"decode_timing": True} if a.decode_timing else {})}))
    if a.cold:
        regimes.append(("cold", {"P": 1, "cache": "cold", "iterations": {"warmup": 0, "measured": int(a.cold)},
                                 "sample_every": a.sample_every, "perf_level": a.perf_level, "emit": {"histograms": False, "input_labels": False, "kvpair_stats": False, "out_bytes": False},
                                 "remote": remote_run(a), **({"kv_timing": True} if a.kv_timing == "all" else {}),
                                 **({"decode_timing": True} if a.decode_timing else {})}))
    # the timer per regime this run measures (recorded in the meta; the columns exist when any regime has it)
    kv_by_regime = {r: a.kv_timing == "all" for r, _run in regimes}
    if a.steady:
        kv_by_regime["steady"] = a.kv_timing in ("steady", "all")
    timer_any = any(kv_by_regime.values())
    for flag, d, n in (("--steady-breakdown", a.steady_breakdown, a.steady_breakdown_passes),
                       ("--steady-kvtimer", a.steady_kvtimer, a.steady_kvtimer_passes)):
        if d and n < 1:
            raise SystemExit(f"{flag}-passes {n}: at least 1")
    if a.steady_breakdown and a.steady_kvtimer and os.path.abspath(a.steady_breakdown) == os.path.abspath(a.steady_kvtimer):
        raise SystemExit("--steady-breakdown and --steady-kvtimer must name two dirs (two captures)")
    if a.steady_kvtimer:
        if not a.steady:
            raise SystemExit("--steady-kvtimer needs --steady (its passes follow the steady regime's)")
        if a.accumulate:
            raise SystemExit("--steady-kvtimer with --accumulate: the capture is written whole; use one --out per run")
        if os.path.abspath(a.steady_kvtimer) == os.path.abspath(a.out):
            raise SystemExit("--steady-kvtimer must name another dir than --out (two captures, two kinds)")
    if a.steady_breakdown:
        if not a.steady:
            raise SystemExit("--steady-breakdown needs --steady (it is one more pass of the steady regime)")
        if a.accumulate:
            raise SystemExit("--steady-breakdown with --accumulate: the breakdown capture is written whole; use one --out per run")
        if os.path.abspath(a.steady_breakdown) == os.path.abspath(a.out):
            raise SystemExit("--steady-breakdown must name another dir than --out (two captures, two kinds)")
    if a.reuse_distance and (regimes or a.steady or a.accumulate):
        raise SystemExit("--reuse-distance runs alone: pass --warm '' --cold '' and no --steady / --accumulate "
                         "(it reads no clock and bounds no cache; its files are its own)")
    os.makedirs(a.out, exist_ok=True)
    csv_path = os.path.join(a.out, "steps.csv")
    if a.accumulate:        # before anything is measured: an accumulating file with another header is moved aside (the
        from kvevaluator.measure import kvstats_layout as L      # upsert would refuse it only after the whole run)
        L.aside_other_header(csv_path, columns(timer_any))
    # each arm's index + filter blocks (whole / partitioned, the store manifest's): cold latency depends on it, so it
    # is recorded per arm, and an accumulated capture never mixes two settings for one arm unless told
    from kvevaluator.measure.cppwlatency import driver_state
    driver = driver_state(["python/kvevaluator/measure/cpplatency.py", "python/kvevaluator/measure/cppbatch.py", "python/kvevaluator/measure/workload_plans.py",
                           "python/kvevaluator/measure/numa_binding.py",
                           "python/kvevaluator/capture/binary.py", "python/kvevaluator/capture/store_identity.py", "python/kvevaluator/measure/records.py",
                           "python/kvevaluator/measure/server.py", "python/kvevaluator/measure/stores.py", "python/kvevaluator/measure/plan_runs.py",
                           "python/kvevaluator/datasets/linkbench/params.py"])
    if a.require_clean and driver["dirty_files"]:
        raise SystemExit(f"--require-clean: driver files differ from HEAD {driver['git_sha'][:9]}: {driver['dirty_files']}")
    if a.hubs and a.workload != "linkbench":
        raise SystemExit("--hubs is for --family linkbench")
    ifb = {arm: index_filter_blocks(a.sf, arm, a.workload, a.meta_spec) for arm in a.arms}
    # a store of another index + filter layout than this binary ingests is timed and marked (its layout is in the
    # meta per arm, and capture_protocol tells the readers; --allow-whole-index-filter names a control on purpose)
    whole = store_layout.refuse_stale({arm: {"index_filter_blocks": v} for arm, v in ifb.items()}, allow_whole=True,
                                      what="cpplatency")
    if whole:
        stale.note("protocol_mixed", f"cpplatency: stores with index + filter blocks other than this binary's: {whole}")
    if len(set(ifb.values())) > 1 and not a.allow_mixed_index_filter_blocks:   # one run: arms of two layouts
        raise SystemExit(f"arms of index_filter_blocks {sorted(set(ifb.values()))} ({ifb}) in one capture -- "
                         f"--allow-mixed-index-filter-blocks (a control)")
    if a.accumulate and not a.allow_mixed_index_filter_blocks:
        for arm, earlier in earlier_index_filter_blocks(csv_path).items():
            if arm in ifb and earlier != ifb[arm]:
                raise SystemExit(f"{csv_path}: arm {arm} was captured with index_filter_blocks={earlier}, its store now "
                                 f"says {ifb[arm]} -- another --out, or --allow-mixed-index-filter-blocks")
        # ... and across arms: a new arm of one layout beside earlier arms of another is a cross-layout comparison
        layouts = set(earlier_index_filter_blocks(csv_path).values()) | set(ifb.values())
        if len(layouts) > 1:
            raise SystemExit(f"{csv_path}: the capture would hold arms of index_filter_blocks {sorted(layouts)} -- "
                             f"another --out, or --allow-mixed-index-filter-blocks")
    if a.accumulate:   # walls with and without the KV timer are not comparable (the timer runs inside the plan clock)
        earlier = earlier_kv_timing(csv_path)
        clash = sorted(r for r, on in kv_by_regime.items() if earlier.get(r, {on}) != {on})
        if clash:
            raise SystemExit(f"{csv_path}: regime(s) {clash} were measured with the KV timer "
                             f"{ {r: sorted(earlier[r]) for r in clash} } in earlier runs, this run has {kv_by_regime} -- "
                             f"walls with and without the timer are not comparable: use another --out")
    # how each regime's requests reach the server: warm / cold always as batches; steady by --steady-requests
    requests_by_regime = dict({r: "batch" for r, _run in regimes}, **({"steady": a.steady_requests} if a.steady else {}))
    # whether each regime's cache holds the index + filter blocks: warm / cold always (the server's default); steady
    # unless --steady-index-filter-outside. Walls of the two settings are not comparable (capture_protocol)
    ifcache_by_regime = dict({r: True for r, _run in regimes},
                             **({"steady": not a.steady_index_filter_outside} if a.steady else {}))
    if a.accumulate:
        earlier = earlier_index_filter_in_cache(csv_path)
        clash = sorted(r for r, on in ifcache_by_regime.items() if earlier.get(r, {on}) != {on})
        if clash:
            raise SystemExit(f"{csv_path}: regime(s) {clash} were measured with index + filter in the cache "
                             f"{ {r: sorted(earlier[r]) for r in clash} } in earlier runs, this run has {ifcache_by_regime} -- "
                             f"use another --out")
    if a.accumulate:   # walls from the two request protocols are not comparable (the idle time between plans moves some)
        earlier = earlier_requests(csv_path)
        clash = sorted(r for r, how in requests_by_regime.items() if earlier.get(r, {how}) != {how})
        if clash:
            raise SystemExit(f"{csv_path}: regime(s) {clash} were measured with requests "
                             f"{ {r: sorted(earlier[r]) for r in clash} } in earlier runs, this run {requests_by_regime} "
                             f"-- the idle time between plans moves some walls: use another --out")
    if a.accumulate:   # latencies from two allocators are not comparable (cpp/kvevaluator/README.md "The allocator")
        mine, earlier = binary_allocator(), earlier_allocators(csv_path)
        if earlier and earlier != [mine]:
            raise SystemExit(f"{csv_path}: earlier runs were measured on {earlier}, this binary links {mine} -- "
                             f"latencies are not comparable across allocators: use another --out")
    numa = numa_binding.choose(a.numa_node)     # once per run: every server of the run on one node
    where = f" node {numa['node']}" if numa["policy"] == "bind" else ""
    print(f"  numa: {numa['policy']}{where} ({numa['reason']})", flush=True)
    t0 = time.time()
    provenance = None
    kv_block = None                              # the first response's kv_timing block (the timer pair's own cost)
    plan_bodies = {}       # {arm: {query[/candidate]: {"body", "body_sha"}}}: what each cell RAN (the response's
                           # provenance; one entry per (arm, query, body), the params share it) -- the first response's
                           # `provenance` below is the run's binary / store identity, not the per-cell body
    # the IR family of this design: label_only's index-free plans;
    # canonical = the canonical plan dirs
    from kvevaluator.measure.plan_runs import ir_for as _ir_for, ir_to_run as _ir_to_run, label_only_family
    if a.workload == "linkbench":
        from kvevaluator.datasets.linkbench.schema import get_schema
    else:
        from kvevaluator.datasets.ldbc.schema import get_schema
    lo_family = label_only_family(get_schema(a.schema))
    kw = {}
    from kvevaluator import workloads
    if workloads.get(a.workload).own_data:        # a dataset with its own schema (LinkBench, IMDb), as its counts open it
        from kvevaluator.measure.stores import schema_artifact
        kw["schema"] = schema_artifact(workloads.get(a.workload).dataset)
    elif a.schema != "canonical":
        from kvevaluator.datasets import schema_path
        kw["schema"] = schema_path("ldbc", a.schema)
    rows = []
    request_list = []                            # the exact params run (the first arm's; every arm runs the same list)
    raw_out = None
    if a.raw == "jsonl":
        if a.accumulate:
            raise SystemExit("--raw jsonl writes one responses.jsonl.gz per run: use a fresh --out, not --accumulate")
        raw_out = gzip.open(os.path.join(a.out, "responses.jsonl.gz"), "wt")
    ir_shas = {}                                 # (arm, query, candidate) -> sha256 of the IR run (the body's identity)
    window = None
    if a.params:
        if not cands:
            raise SystemExit("--params is for --candidates runs (the default path keeps the first --nparam params)")
        lo, hi = (int(x) for x in a.params.split("-"))
        window = (lo, hi)
    steady = None
    if a.steady:
        mb, passes = a.steady.split(",")
        steady = {"block_cache_bytes": int(float(mb) * (1 << 20)), "measured": int(passes), "eps": a.steady_eps,
                  "max_warm": a.steady_max_warm, "seed": a.steady_seed, "pass_order_rule": steady_rule.PASS_ORDER_RULE,
                  "perf_level": a.steady_perf_level,
                  "direct_io": a.steady_direct_io, "cache_index_and_filter_blocks": not a.steady_index_filter_outside,
                  "cache": ("bounded (data blocks; index + filter outside the budget: --steady-index-filter-outside)"
                            if a.steady_index_filter_outside else "bounded (data, index and filter blocks)"),
                  "order": "seeded random per pass", "requests": a.steady_requests, "steady_at_pass": {}}
    pass_rows = []
    steady_seen = {}                             # the steady server's own store options (its first response)
    store_opts = {}                              # warm / cold: the same, per regime
    batch_faults = []                            # warm / cold: the server's faults per (arm, query) batch
    policies = {}                                # {arm/regime: the server's memory policies at its end}
    reuse_rows = []
    # the timer captures, in the order their passes run: {kind, dir, level, passes, raw} and what they gather
    timers = [{"kind": kind, "dir": d, "level": level, "passes": n, "raw": None}
              for kind, d, level, n in (("kvtimer", a.steady_kvtimer, KVTIMER_PERF_LEVEL, a.steady_kvtimer_passes),
                                        ("breakdown", a.steady_breakdown, BREAKDOWN_PERF_LEVEL, a.steady_breakdown_passes)) if d]
    gathered = {t["kind"]: {"rows": [], "pass_rows": [], "kv_block": None, "prov": None} for t in timers}
    for t in timers:
        os.makedirs(t["dir"], exist_ok=True)
        if a.raw == "jsonl":
            t["raw"] = gzip.open(os.path.join(t["dir"], "responses.jsonl.gz"), "wt")
    for arm in a.arms:
        if steady or a.reuse_distance:
            # a steady run is one PLAN SET over the whole suite: --candidates names at most one body per query (the
            # queries it names run that body, every other query its bundle), so two runs -- the ops-only set and the
            # bytes-aware set -- share the cache the same way the suite does (cache pollution shows in the suite total)
            cells, forced, shas = steady_cells(a.workload, a.sf, arm, a.format, a.queries, a.nparam, a.params_source,
                                               lo_family, cands, a.candidate_set, a.meta_spec, window)
            ir_shas.update(shas)
            if a.reuse_distance:
                with CppServer(arm_store_dir(a.workload, a.sf, arm, a.meta_spec), a.format, numa=numa, **kw) as srv:
                    reuse_rows += run_reuse(srv, arm, cells, a.reuse_distance, a.steady_seed, a.out)
                    policies[f"{arm}/reuse"] = numa_binding.server_policies(srv.proc.pid)
                print(f"  {arm}/reuse-distance: {len(cells)} cells x {a.reuse_distance} passes ({time.time() - t0:.0f}s)", flush=True)
            if steady:
                print(f"  {arm}/steady: {len(cells)} cells, cache {steady['block_cache_bytes'] >> 20} MB"
                      f"{', direct io' if steady['direct_io'] else ''}", flush=True)
                with CppServer(arm_store_dir(a.workload, a.sf, arm, a.meta_spec), a.format, block_cache_bytes=steady["block_cache_bytes"],
                               direct_io=steady["direct_io"],
                               cache_index_and_filter_blocks=steady["cache_index_and_filter_blocks"], numa=numa, **kw) as srv:
                    srows, prow, at, prov, kvb, b = run_steady(srv, arm, cells, steady["measured"], steady["eps"],
                                                 steady["max_warm"], steady["seed"], steady["perf_level"], a.out,
                                                 steady["block_cache_bytes"], steady["direct_io"], raw_out=raw_out,
                                                 remote=remote_run(a), kv_timing=kv_by_regime["steady"],
                                                 requests=steady["requests"], timers=timers,
                                                 seen=steady_seen, decode_timing=a.decode_timing)
                    for r in srows + [x for t in b.values() for x in t["rows"]]:
                        if r["query"] in forced:
                            r["candidate"] = forced[r["query"]]
                    for kind, t in b.items():
                        g = gathered[kind]
                        g["rows"] += t["rows"]
                        g["pass_rows"] += t["pass_rows"]
                        g["kv_block"] = g["kv_block"] or t["kv_block"]
                        g["prov"] = g["prov"] or t["prov"]
                    rows += srows
                    pass_rows += prow
                    steady["steady_at_pass"][arm] = at
                    # outside every clock: the cache's capacity and usage, for the fit (cell_latency.read_set_fits)
                    steady.setdefault("cache_usage", {})[arm] = cu = read_cache_usage(srv, cells, steady["perf_level"], prow, b)
                    print(f"  {arm}/steady: cache usage {cu['usage_bytes'] / (1 << 20):.1f} MiB of {cu['capacity_bytes'] / (1 << 20):.1f} MiB "
                          f"(inserted {cu['inserted_bytes'] / (1 << 20):.1f} MiB; nothing evicted: {cu['nothing_evicted']})", flush=True)
                    policies[f"{arm}/steady"] = numa_binding.server_policies(srv.proc.pid)
                    provenance = provenance or prov
                    kv_block = kv_block or kvb
                print(f"  {arm}/steady: done ({time.time() - t0:.0f}s), steady at pass {at}", flush=True)
        if not regimes:
            continue
        with CppServer(arm_store_dir(a.workload, a.sf, arm, a.meta_spec), a.format,
                       block_cache_bytes=int(a.block_cache_gb * (1 << 30)),
                       direct_io=a.direct_io, numa=numa, **kw) as srv:
            for q in a.queries:
                ir = (_ir_for(a.workload, q, lo_family) if q in cands else
                      _ir_to_run(a.workload, q, a.format, srv.store_dir, lo_family, spec=a.meta_spec))
                # the bodies of this query: the bundle alone (the default path), or the named candidates
                bodies = [(None, ir)] if q not in cands else candidate_bodies(a.workload, q, arm, a.sf, cands[q], ir, a.candidate_set, a.meta_spec)
                for name, body_ir in bodies:
                    if name:
                        ir_shas[f"{arm}/{q}/{name}"] = hashlib.sha256(
                            json.dumps(body_ir, sort_keys=True).encode()).hexdigest()
                idx = 0
                plist = (list(enumerate(workload_plans.query_params(a.workload, a.sf, q, 0, a.params_source), 1)) if window
                         else request_params(a, q))
                if window:
                    plist = [(i, args) for i, args in plist if window[0] <= i <= window[1]]
                request_list.extend({"arm": arm, "query": q, "row": i, "args": ar} for i, ar in plist
                                    if arm == a.arms[0])
                # the query's requests as ONE batch (measure/cppbatch.py), in the order single requests had: per param,
                # per regime, the bodies interleaved and alternated; each line carries its regime's run
                plans = {q + (f"/{name}" if name else ""): body_ir for name, body_ir in bodies}
                lines, keys = [], []
                for idx, args in plist:
                    for ri, (regime, run) in enumerate(regimes):
                        order = bodies if (idx + ri + a.alternate) % 2 == 0 else bodies[::-1]   # interleaved, alternated
                        for name, _body in order:
                            lines.append({"seq": len(lines), "plan": q + (f"/{name}" if name else ""), "params": args,
                                          "run": run})
                            keys.append((idx, regime, name))
                f0 = numa_binding.snapshot(srv.proc.pid)
                resps = run_lines(srv, plans, lines, os.path.join(a.out, f".batch-{arm}-{q}-p{os.getpid()}"), {},
                                  keep_rows=True, labels=[f"{arm}/{q}/{n or ''}/p{i}/{r}" for i, r, n in keys]
                                  )[1] if lines else []
                # the query's batch (every param and regime, warm-ups included): recorded, not checked (a warm-up's
                # first touch and a cold reopen's new cache fault legitimately)
                batch_faults.append({"arm": arm, "query": q, "requests": len(lines),
                                     **numa_binding.fault_columns(f0, numa_binding.snapshot(srv.proc.pid),
                                                                  sum(_plan_wall(r) for r in resps), srv.proc.pid)})
                for (idx, regime, name), resp in zip(keys, resps):
                    store_opts.setdefault(regime, (resp.get("run") or {}).get("store_options"))
                    if raw_out is not None:
                        raw_out.write(json.dumps({"arm": arm, "query": q, "row": idx, "candidate": name,
                                                  "regime": regime, "response": resp}) + "\n")
                    else:
                        path = raw_path(a.out, arm, q, idx, regime, body=name if a.compare_bodies else None)
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "w") as fo:   # json.dumps: the C encoder
                            fo.write(json.dumps(resp))
                    cell_rows = rows_of(arm, q, idx, regime, resp)
                    if name:
                        for r in cell_rows:
                            r["candidate"] = name
                    rows += cell_rows
                    provenance = provenance or resp["provenance"]
                    kv_block = kv_block or resp.get("kv_timing")
                    plan_bodies.setdefault(arm, {})[q + (f"/{name}" if name else "")] = {
                        "body": resp["provenance"].get("body"), "body_sha": resp["provenance"].get("body_sha")}
                print(f"  {arm}/{q}: {len(plist)} params{f' ({window[0]}-{window[1]})' if window else ''} x {len(regimes)} regimes"
                      f"{f' x {len(bodies)} bodies' if len(bodies) > 1 else ''}  ({time.time() - t0:.0f}s)", flush=True)
            policies[f"{arm}/{'+'.join(r for r, _run in regimes)}"] = numa_binding.server_policies(srv.proc.pid)
    if raw_out is not None:
        raw_out.close()
    for t in timers:
        if t["raw"] is not None:
            t["raw"].close()
    if a.reuse_distance:
        if not reuse_rows:
            raise SystemExit("--reuse-distance: 0 rows (no cell ran): a failure, not an empty result")
        for name, cols, rr in (("reuse-distance.csv", REUSE_COLUMNS, reuse_rows),
                               ("reuse-distance-summary.csv", None, reuse_summary(reuse_rows))):
            with open(os.path.join(a.out, name), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols or list(rr[0]))
                w.writeheader()
                w.writerows(rr)
        with open(os.path.join(a.out, "reuse-distance.meta.json"), "w") as f:
            json.dump({"sf": a.sf, "workload": a.workload, "arms": a.arms, "queries": a.queries, "nparam": a.nparam,
                       "passes": a.reuse_distance, "seed": a.steady_seed, "order": "seeded random per pass, as the steady regime",
                       "binary": recorded_path(binary_path()), "driver": driver, "ir_sha": ir_shas, "wall_s": round(time.time() - t0, 1),
                       "what": "per step: fetched kvbytes by reuse distance (distinct kvbytes fetched since the item was last "
                               "fetched); bucket i = [2^(i-1), 2^i) bytes, 'new' = never fetched before in the pass stream"}, f, indent=1)
        cells_n = len({(r["arm"], r["query"], r["param_idx"]) for r in reuse_rows})
        print(f"reuse distance -> {os.path.join(a.out, 'reuse-distance.csv')}: {len(reuse_rows)} step rows, {cells_n} (arm, query, param) "
              f"cells x {a.reuse_distance} passes ({time.time() - t0:.0f}s)")
        return
    if a.accumulate:
        from kvevaluator.measure import kvstats_layout as L
        rep, add, kept = L.upsert(csv_path, rows, L.KEYS["latency"], a.accumulate, columns=columns(timer_any),
                                  scope=L.SCOPE["latency"])
        print(f"[kvstats] {csv_path}: {rep} replaced, {add} added, {kept} kept (run {a.accumulate})")
    else:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns(timer_any) + (["candidate"] if cands else []))
            w.writeheader()
            w.writerows(rows)
    if pass_rows:
        with open(os.path.join(a.out, "steady-passes.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PASS_COLUMNS)
            w.writeheader()
            w.writerows(pass_rows)
    # a bound run with a server the NUMA scanner may scan: its walls may carry hint faults; the meta says which, marked
    scannable = numa_binding.scannable_servers(policies, numa)
    if scannable:
        stale.note("numa_scannable", f"{a.out}: bound ({numa['policy']}, node {numa['node']}), yet the NUMA scanner may "
                   f"scan these servers' mappings: {scannable}")
    # time inside plans: the interpreter's own clock of every measured run kept in the rows (plan_wall_ns on the
    # __plan__ rows); the rest of wall_s is process starts, store opens, warm-ups, unkept iterations and this driver
    plan_wall_s = round(sum(float(r.get("plan_wall_ns") or 0) for r in rows if r.get("step") == "__plan__") / 1e9, 3)
    meta = {"sf": a.sf, "workload": a.workload, "arms": a.arms, "queries": a.queries, "nparam": a.nparam,
            "regimes": dict({r: dict(run, direct_io=a.direct_io, store_options=store_opts.get(r)) for r, run in regimes},
                            **({"steady": dict(steady, store_options=steady_seen.get("store_options"))} if steady else {})),
            "block_cache_bytes": int(a.block_cache_gb * (1 << 30)), "direct_io": a.direct_io, "binary": recorded_path(binary_path()),
            "index_filter_blocks": ifb, "index_filter_in_cache_by_regime": ifcache_by_regime,
            "params": {"source": a.params_source, "subsample": a.subsample,
                       "seed": a.seed, "hubs": a.hubs, "nodes": a.nodes, "requests": request_list},
            "raw": a.raw, "driver": driver, "remote": remote_run(a), "breakdown_pass": a.breakdown_pass,
            "kv_timing": a.kv_timing, "kv_timing_by_regime": kv_by_regime, "kv_timing_block": kv_block,
            "decode_timing": a.decode_timing,
            # the server's NUMA binding (python/kvevaluator/measure/numa_binding.py) and the faults of each warm / cold batch; the
            # steady passes' faults are columns of steady-passes.csv (a bound run whose server the scanner faulted is
            # refused)
            "numa": numa, "batch_faults": batch_faults, "server_policies": policies,
            "requests_by_regime": requests_by_regime,
            # wall: every regime at perf <= 2 with the KV timer off, so its walls carry no instrument of ours;
            # breakdown: a timer runs inside the walls (measure/capture_protocol.capture_kind reads the same fields)
            "capture_kind": "breakdown" if timer_any or any(
                int(r.get("perf_level") or 0) >= BREAKDOWN_PERF_LEVEL for r in dict(regimes, **({"steady": steady} if steady else {})).values())
                else "wall",
            "free_rows": False,   # a latency run never sets run.free_rows (the executor refuses it on one)
            "provenance": provenance, "wall_s": round(time.time() - t0, 1), "plan_wall_s": plan_wall_s,
            # the body each (arm, query) ran: the variant C++ selected and its sha (ir/plan.cc body_sha_of ==
            # planir/bundle.body_sha), so "every cell holds at code state X" is a diff against the plan body's sha
            "plan_bodies": plan_bodies}
    meta["compare_bodies"] = bool(a.compare_bodies)        # the raw cells' shape (measure/latency_raw_files.py)
    if cands:
        meta["candidates"] = cands
        meta["candidate_set"] = a.candidate_set
        meta["candidate_ir_sha256"] = ir_shas
        meta["params_window"] = window
        meta["alternate"] = a.alternate
        meta["candidates_note"] = ("named bodies per query, each forced as a single-body IR (the plan files' candidate "
                                   "set); '(pin)' is the committed bundle as C++ selects it" +
                                   ("; within a (param, regime) the bodies run interleaved with the order alternated "
                                    "per regime" if a.compare_bodies else "; one body per query"))
    if a.workload == "writes":
        meta["write_latency_note"] = ("a write plan runs against the executor's shadow overlay: wall = "
                                      "its RMW reads + in-memory PUTs/DELs, NOT durable WAL/flush/"
                                      "compaction latency (a separate future measurement); the store "
                                      "never mutates")
    if scannable:
        meta["numa_scannable"] = scannable
    meta = stale.fold(meta)           # a run's moved inputs (kvevaluator.stale): one cpplatency run per process
    meta_path = csv_path + ".meta.json"
    if a.accumulate:
        # steps.csv holds rows of several runs (each row carries `run`): one meta per run, never overwritten;
        # steps.csv.meta.json stays the latest run's
        meta["run"] = a.accumulate
        if os.path.exists(meta_path) and "run" not in json.load(open(meta_path)):
            # a meta from before the per-run files names no run: kept once, as the earlier runs' provenance
            os.replace(meta_path, csv_path + f".meta.before-{a.accumulate}.json")
        json.dump(meta, open(csv_path + f".meta.{a.accumulate}.json", "w"), indent=1)
    for t in timers:
        meta[f"steady_{t['kind']}"] = recorded_path(os.path.abspath(t["dir"]), Tree().root)
    json.dump(meta, open(meta_path, "w"), indent=1)
    print(f"raw -> {csv_path}  ({meta['wall_s']}s wall, {plan_wall_s}s inside the plans of the rows kept)")
    for t in timers:
        write_timer_capture(a, t, gathered[t["kind"]], meta, cands)


def write_timer_capture(a, timer, got, wall_meta, cands):
    """a timer capture (--steady-kvtimer, --steady-breakdown): steps.csv (the timer columns on), steady-passes.csv and a
    meta naming it a breakdown capture of the wall capture in --out"""
    flag = f"--steady-{timer['kind']}"
    if not got["rows"]:
        raise SystemExit(f"{flag}: 0 rows (no cell ran): a failure, not an empty result")
    out = timer["dir"]
    path = os.path.join(out, "steps.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns(True) + (["candidate"] if cands else []))
        w.writeheader()
        w.writerows(got["rows"])
    with open(os.path.join(out, "steady-passes.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PASS_COLUMNS)
        w.writeheader()
        w.writerows(got["pass_rows"])
    steady = dict(wall_meta["regimes"]["steady"], perf_level=timer["level"], measured=timer["passes"],
                  passes_note=f"{timer['passes']} pass(es) after the measured passes of the wall capture, in the same "
                              f"server and cache; a cell's row is its median run")
    meta = dict(wall_meta, regimes={"steady": steady}, kv_timing="steady", kv_timing_by_regime={"steady": True}, decode_timing=True,
                kv_timing_block=got["kv_block"], requests_by_regime={"steady": steady["requests"]},
                capture_kind="breakdown", timer_kind=timer["kind"], wall_capture=recorded_path(os.path.abspath(a.out), Tree().root),
                provenance=got["prov"] or wall_meta["provenance"],
                plan_wall_s=round(sum(float(r.get("plan_wall_ns") or 0) for r in got["rows"] if r.get("step") == "__plan__") / 1e9, 3))
    meta.pop("steady_breakdown", None)
    meta.pop("steady_kvtimer", None)
    json.dump(meta, open(path + ".meta.json", "w"), indent=1)
    cells_n = len({(r["arm"], r["query"], r["param_idx"]) for r in got["rows"]})
    print(f"{timer['kind']} -> {path}  ({cells_n} (arm, query, param) cells, {timer['passes']} pass(es) at perf "
          f"{timer['level']} + the KV timer)")

if __name__ == "__main__":
    main()
