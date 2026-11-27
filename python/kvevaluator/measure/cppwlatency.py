"""Measure the C++ write latency: per store arm, the whole write suite as one fixed sequence of real writes on a
disposable arena (a hard-link copy of the store, measure/arenas.py), deleted after the run.

Each cell runs four legs on the same accumulated state: a shadow warm-up (discarded), a shadow control and a second
shadow control (the same work minus the engine write; their difference is the noise floor), then the timed apply.
apply_wall - shadow_wall is the engine write's share. The counts, byte channels, rows and final payload must be
identical between the control and apply legs, or the sweep aborts. An applied write runs once per state, so a cell
has one wall; position in the sequence is signal (memtable fill, WAL growth), and each row carries seq_idx.

Regimes (--pass, one per run, never mixed in one arena): headline (perf_level 1, the wall of record), breakdown
(perf_level 3 from a fresh arena), isolate (each cell from its own fresh arena, the A/B control), kvtimer (the KV
timer, with --warm-reads only). --cache steady opens the server under the steady read settings of
inputs/contract/store_config.json; --warm-reads runs a steady read capture's warm passes first, then the apply leg only.

No metric is derived here: raw per-step rows go to steps.csv, the apply leg's full response per cell beside it.

    python3 -m kvevaluator.measure.cppwlatency --sf 0.1 --arms dgraph nebula \
        --nparam 5 --out output/sf0.1/evaluator/writes/latency-cpp
"""
import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import time

from kvevaluator import stale
from kvevaluator.measure.server import CppServer, build_request
from kvevaluator.measure.stores import store_dir
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.plan_runs import PLAN_DIRS
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT, recorded_path
from kvevaluator import workloads as W
from kvevaluator.measure import numa_binding, store_config, store_layout
from kvevaluator.workloads.writes import params as w_params
from kvevaluator.measure import workload_plans as w_plans
from kvevaluator.measure.cppbatch import run_lines
from kvevaluator.measure.cppwrite import (Arena, cross_check, run_cell, same_counts, sequence, _CHECKED, _BYTE_CHECKED,
                                          _DESTRUCTIVE, _SPLIT_GROUPS, _PLANS)  # the write-count oracle

# the writes plan dirs, from the one map (plan_runs.PLAN_DIRS); this tool keeps its own
# SEQUENCED walk over them (conflict groups, family splits, deletes last) -- the accumulate
# semantics measure/workload_plans's per-query interface does not carry

# write-side engine timers + io write fields (perf_level >= 2/3 fills the timers; the io
# COUNTERS move at every level; write_thread_wait/db_mutex need level 5 and so read 0 in the
# breakdown regime). The full PerfContext rides in the saved response JSONs (capture-all);
# these are the columns the write analysis reads flat.
KV_FIELDS = ["write_wal_time", "write_memtable_time", "write_delay_time",
             "write_pre_and_post_process_time", "write_scheduling_flushes_compactions_time",
             "write_thread_wait_nanos",
             "io_bytes_written", "io_write_nanos", "io_prepare_write_nanos", "io_fsync_nanos"]
# the read-before-write byte lanes and absence misses, from the same apply response as the wall (its
# `bytes` / miss counts): column name -> the response's bytes key
READ_BYTES = {"get_key_bytes": "get_key", "get_value_bytes": "get_value", "scan_key_bytes": "scan_key",
              "scan_value_bytes": "scan_value", "seek_miss_key_bytes": "seek_miss_key",
              "next_miss_key_bytes": "next_miss_key", "get_miss_key_bytes": "get_miss_key"}
# the KV-boundary timer's per-step fields (run.kv_timing: every Get, Seek, Next, iterator delete, Put, Delete at the
# RocksDB API boundary, store/counting.cc), one list with the read driver; columns of a --kv-timing capture only
from kvevaluator.measure.cpplatency import KV_TIMER_FIELDS, KVTIMER_PERF_LEVEL, PASS_COLUMNS

_EXP = str(ROOT)
# per-step PerfContext read counters (filled at perf_level >= 2: the breakdown pass; 0 in the headline)
KV_READ_FIELDS = ["block_read_count", "block_read_byte", "block_cache_hit_count"]
# per-cell RocksDB tickers (always on, every level): on the __plan__ row only (a step row carries 0); the
# flush / stall ones are what --require-no-flush checks
TICKERS = {"block_cache_miss": "rocksdb.block.cache.miss", "block_cache_hit": "rocksdb.block.cache.hit",
           "block_cache_data_miss": "rocksdb.block.cache.data.miss", "block_cache_data_hit": "rocksdb.block.cache.data.hit",
           "memtable_hit": "rocksdb.memtable.hit",
           "bytes_read": "rocksdb.bytes.read", "wal_bytes": "rocksdb.wal.bytes",
           "flush_write_bytes": "rocksdb.flush.write.bytes", "stall_micros": "rocksdb.stall.micros"}
COLUMNS = (["arm", "workload", "query", "ran_as", "seq_note", "param_idx", "family_row", "seq_idx", "regime", "step_idx", "step",
            "op", "kind", "rows", "S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss", "write_key_bytes",
            "write_value_bytes"] + list(READ_BYTES) + ["wall_ns", "shadow_wall_ns", "shadow2_wall_ns", "plan_wall_ns"]
           + KV_FIELDS + KV_READ_FIELDS + list(TICKERS))

# Plans that INTERFERE when the writes are real (the shadow suite never sees this -- its
# cells each start pristine; the MB88 lesson from the params docstring). Two conflict kinds:
#   - the vertex deletes remove the very persons every person_incident plan anchors on
#     -> they run LAST, and the two variants SPLIT the family rows between them;
#   - plans that derive the SAME key from the same anchor: the new_pairs trio insert one
#     knows edge, iu2/iu3 one likes edge (both to the hardcoded msg 1000000000000000), and
#     w_ins_v_full/w_ins_v_min ONE new vid (identical new_vid formula -- unsplit, _min
#     would measure an overwrite of the vertex _full just created, not a fresh insert).
#     The second writer throws or degenerates -> each group splits its family rows.
# Deletes-last also keeps arangodb_engine's uid -> collection map honest (cpp/kvevaluator/src/formats/arangodb/engine.h:
# coll_, filled at ingest and by vertex inserts, shadow runs included; deletes never remove): nothing reads a stale
# entry only because no cell runs after the deletes. Reordering the sequence must revisit that. (The write stage of
# experiments/layout_choice times meta stores only, which keep no such map.)
LEGS = ("shadow warm-up", "shadow", "shadow2", "apply")
APPLY_ONLY = ("apply",)                           # --warm-reads: the write once, after the reads' warm-up


def raw_file(out, arm, wl, q, idx, regime):
    """the raw response file of one (arm, workload, query, param, regime) below a run's --out (--raw files): the one place
    that knows the layout (the writer, --same-counts-as and kvcost.latency's readers)"""
    return os.path.join(out, arm, wl, q, f"p{idx}", f"{regime}.json")


def kvtimer_dir(b_dir):
    """the KV-timer passes' regime dir name beside a writes-after-warm-up (B) one, the one place of the rule (the writes
    stage, write_rocksdb_share.py): after-steady-warmup-<MB>mb-p1-noflush -> after-steady-warmup-<MB>mb-kvtimer-noflush,
    as the reads' steady-<MB>mb-kvtimer beside steady-<MB>mb-p2"""
    name = os.path.basename(os.path.normpath(b_dir))
    if name.count("-p1-") != 1:
        raise SystemExit(f"{b_dir}: not a B regime dir (after-steady-warmup-<MB>mb-p1-noflush)")
    return os.path.join(os.path.dirname(os.path.normpath(b_dir)), name.replace("-p1-", "-kvtimer-"))


def raw_files(out, regime):
    """{(query, param): raw file} of one run's regime, its cells from steps.csv's __plan__ rows (one arm per run; a
    (query, param) twice is refused)"""
    found = {}
    with open(os.path.join(out, "steps.csv")) as fh:
        for r in csv.DictReader(fh):
            if r["step"] != "__plan__" or r["regime"] != regime:
                continue
            k = (r["query"], r["param_idx"])
            if k in found:
                raise SystemExit(f"{out}: {k} has two __plan__ rows in regime {regime}")
            found[k] = raw_file(out, r["arm"], r["workload"], r["query"], r["param_idx"], regime)
    return found


def run_cells_paired(srv, cells, run, path_base, legs=LEGS):
    """Per cell, in order: warm shadow (discarded) -> control shadow -> second control shadow -> apply (mutates), all
    cells as ONE batch (measure/cppbatch.py; a line's apply false = shadow mode). Both controls are cross-checked against
    apply. Returns [(apply, shadow, shadow2)] in cell order. legs=APPLY_ONLY: the apply line alone (shadow, shadow2
    None). measure/cppwrite.run_cell is the one-control form the write-count oracle uses."""
    plans, names, lines = {}, {}, []
    for wl, q, idx, args, ir in cells:
        name = names.setdefault(id(ir), f"{q}#{len(names)}")   # one plan per distinct IR (a sequence reuses its objects)
        plans[name] = ir
        for leg in legs:
            lines.append({"seq": len(lines), "plan": name, "params": args, "apply": leg == "apply"})
    labels = [f"{wl}/{q}/p{idx}/{leg}" for wl, q, idx, _a, _i in cells for leg in legs]
    print(f"    batch of {len(lines)} lines -> {path_base}.responses.jsonl (grows as it runs)", flush=True)
    _br, resps = run_lines(srv, plans, lines, path_base, run, keep_rows=True, labels=labels)   # cross_check compares the rows
    out = []
    k = len(legs)
    for n, (wl, q, idx, _args, _ir) in enumerate(cells):
        got = dict(zip(legs, resps[k * n:k * n + k]))
        if not got["apply"]["run"].get("apply_writes"):
            raise SystemExit(f"{wl}/{q}/p{idx}: the apply leg ran in shadow mode")
        if legs == APPLY_ONLY:
            out.append((got["apply"], None, None))
            continue
        cross_check(got["shadow"], got["apply"], f"{wl}/{q}/p{idx}")
        cross_check(got["shadow2"], got["apply"], f"{wl}/{q}/p{idx} (shadow2)")
        out.append((got["apply"], got["shadow"], got["shadow2"]))
    return out


def run_cells_single(srv, cells, run):
    """The control (--requests single): the same four legs per cell, one request each, a Python round trip between
    them. Returns what run_cells_paired returns."""
    out = []
    for wl, q, idx, args, ir in cells:
        legs = []
        for leg in LEGS:
            r = srv.run(ir, args, run=dict(run, apply_writes=True) if leg == "apply" else run)
            if r.get("error"):
                raise SystemExit(f"{wl}/{q}/p{idx} {leg}: {r['error']}")
            legs.append(r)
        cross_check(legs[1], legs[3], f"{wl}/{q}/p{idx}")
        cross_check(legs[2], legs[3], f"{wl}/{q}/p{idx} (shadow2)")
        out.append((legs[3], legs[1], legs[2]))
    return out


def family_row(sf, q, args):
    """The 1-based row of the query's param family these args came from (a split family's param_idx
    indexes its slice, not the family -- this is the key the counts capture uses)."""
    for i, r in enumerate(w_params.param_rows(sf, q), 1):
        if w_params.args_from_row(q, r) == args:
            return i
    raise SystemExit(f"{q}: args not in its param family: {args}")


def flush_check(resp, where, regime):
    """--require-no-flush: no flush, no stall, no write delay inside the window."""
    sd = (resp.get("rocksdb") or {}).get("statistics_delta") or {}
    bad = {k: sd.get(TICKERS[k], 0) for k in ("flush_write_bytes", "stall_micros") if sd.get(TICKERS[k], 0)}
    delay = sum((s.get("kv") or {}).get("write_delay_time", 0) for s in resp["steps"])
    if delay:
        bad["write_delay_time"] = delay
    if bad:
        raise SystemExit(f"{where} ({regime}): a flush or stall landed in the window: {bad} -- raise --write-buffer-mb")


def rows_of(arm, wl, q, idx, seq_idx, regime, resp, shadow, shadow2=None, frow=None):
    """One CSV row per step + a __plan__ total row. `wall_ns` is the APPLY run's step wall
    (RMW CPU + engine write); `shadow_wall_ns` is the paired warm control (same work, no
    engine write) -- their difference is the engine write's share. The __plan__ row's wall
    is the sum of step walls (the P=1 latency); plan_wall_ns is the interpreter's clock. shadow None (the apply-only
    leg of --warm-reads): the shadow columns are empty."""
    out = []
    sh_steps = shadow["steps"] if shadow else None
    sh2 = shadow2["steps"] if shadow2 else None
    for i, s in enumerate(resp["steps"]):
        r = {"arm": arm, "workload": wl, "query": q, "param_idx": idx, "family_row": frow, "seq_idx": seq_idx,
             "regime": regime, "step_idx": i, "step": s["name"], "op": s["op"], "kind": s["kind"],
             "rows": s.get("rows", 0),
             "S": s["S"], "G": s["G"], "N": s["N"], "P": s["P"], "D": s["D"],
             "S_miss": s.get("S_miss", 0), "G_miss": s.get("G_miss", 0), "N_miss": s.get("N_miss", 0),
             "write_key_bytes": s["bytes"]["write_key"], "write_value_bytes": s["bytes"]["write_value"],
             **{c: s["bytes"].get(k, 0) for c, k in READ_BYTES.items()},
             "wall_ns": s["wall_ns"], "shadow_wall_ns": sh_steps[i]["wall_ns"] if sh_steps else "",
             "shadow2_wall_ns": sh2[i]["wall_ns"] if sh2 else "", "plan_wall_ns": 0}
        for f in KV_FIELDS + KV_READ_FIELDS:
            r[f] = (s["kv"] or {}).get(f, 0)
        if "kv_timing" in resp:              # the response carries its kv_timing block only when the timer ran
            for f in KV_TIMER_FIELDS:
                r[f] = (s["kv"] or {}).get(f, 0)
        r.update(dict.fromkeys(TICKERS, 0))
        out.append(r)
    t = resp["totals"]
    ts = shadow["totals"] if shadow else None
    sd = (resp.get("rocksdb") or {}).get("statistics_delta") or {}
    out.append({"arm": arm, "workload": wl, "query": q, "param_idx": idx, "family_row": frow, "seq_idx": seq_idx,
                "regime": regime, "step_idx": -1, "step": "__plan__", "op": "plan", "kind": "plan",
                "rows": len(resp.get("rows") or []),
                "S": t["S"], "G": t["G"], "N": t["N"], "P": t["P"], "D": t["D"],
                "S_miss": t.get("S_miss", 0), "G_miss": t.get("G_miss", 0), "N_miss": t.get("N_miss", 0),
                "write_key_bytes": t["bytes"]["write_key"], "write_value_bytes": t["bytes"]["write_value"],
                **{c: t["bytes"].get(k, 0) for c, k in READ_BYTES.items()},
                "wall_ns": t["work_ns"] + t.get("overhead_ns", 0),
                "shadow_wall_ns": (ts["work_ns"] + ts.get("overhead_ns", 0)) if ts else "",
                "shadow2_wall_ns": (shadow2["totals"]["work_ns"] + shadow2["totals"].get("overhead_ns", 0)) if shadow2 else "",
                "plan_wall_ns": t["wall_ns"],
                **{f: sum((s["kv"] or {}).get(f, 0) for s in resp["steps"]) for f in KV_FIELDS + KV_READ_FIELDS},
                **({f: sum((s["kv"] or {}).get(f, 0) for s in resp["steps"]) for f in KV_TIMER_FIELDS} if "kv_timing" in resp else {}),
                **{c: sd.get(k, 0) for c, k in TICKERS.items()}})
    return out


LB_WRITES = ("addlink_link1", "addlink_link2", "addnode", "deletelink_link1", "deletelink_link2", "deletenode",
             "updatelink_link1", "updatelink_link2", "updatenode")


def linkbench_sequence(tag, seed, queries=None, nparam=0, param_set=None):
    """LinkBench's write requests (the sample `param_set`, quick or quick-next) as ONE fixed order: a seeded interleave that keeps
    each query's rows in their sample order and draws the next query with weight = its remaining rows (the closest to
    a request stream; the same on every arm). Cells are (workload, query, row, args, ir); row = the 1-based row in the
    query's sample file (the family_row). A sample applied IN SEQUENCE changes some requests' meaning, decided from
    the sequence alone by the LinkBench workload's sequence_meaning (the same on every arm; LINKBENCH_NOTES[seq_idx] =
    (ran_as, note)). The query column keeps the sample file's op; ran_as names the plan that ran."""
    import random
    from kvevaluator.measure.plan_runs import ir_for
    from kvevaluator.datasets.linkbench.params import sequence_meaning
    rows = {}
    for q in LB_WRITES:
        if queries and q not in queries:
            continue
        rs = w_plans.query_params("linkbench", tag, q, nparam, param_set)
        if not rs:
            raise SystemExit(f"linkbench {q}: no params")
        rows[q] = list(enumerate(rs, 1))
    irs = {q: ir_for("linkbench", q) for q in rows}
    rng, pos, order = random.Random(str(seed)), {q: 0 for q in rows}, []
    while any(pos[q] < len(rows[q]) for q in rows):
        left = [q for q in sorted(rows) if pos[q] < len(rows[q])]
        q = rng.choices(left, weights=[len(rows[x]) - pos[x] for x in left])[0]
        idx, args = rows[q][pos[q]]
        pos[q] += 1
        order.append((q, idx, args))
    LINKBENCH_NOTES.clear()
    seq = []
    for i, ((q, idx, args), (ran, note)) in enumerate(zip(order, sequence_meaning([(q, a) for q, _i, a in order]))):
        if ran != q and ran not in irs:
            irs[ran] = ir_for("linkbench", ran)
        if ran != q or note:
            LINKBENCH_NOTES[i] = (ran, note)
        seq.append(("linkbench", q, idx, args, irs[ran]))
    return seq


LINKBENCH_NOTES = {}                              # seq_idx -> (ran_as, seq_note): linkbench_sequence's conflict marks


def warm_reads_capture(path, sf, arm, cache_bytes):
    """--warm-reads: a steady read capture's meta, refused unless it is this store's at this SF and its steady block
    ran at the settings this server runs (the steady rule's cache, direct I/O, index + filter blocks in the cache)"""
    mp = os.path.join(path, "steps.csv.meta.json")
    if not os.path.isfile(mp):
        raise SystemExit(f"--warm-reads {path}: no steps.csv.meta.json (a cpplatency steady capture dir)")
    m = json.load(open(mp))
    st = (m.get("regimes") or {}).get("steady")
    if not st:
        raise SystemExit(f"--warm-reads {path}: not a steady capture (its meta has no steady regime)")
    if str(m.get("sf")) != str(sf) or m.get("arms") != [arm]:
        raise SystemExit(f"--warm-reads {path}: a capture of sf {m.get('sf')} arms {m.get('arms')}, this run sf {sf} arm {arm}")
    want = {"block_cache_bytes": cache_bytes, "direct_io": True, "cache_index_and_filter_blocks": True}
    bad = {k: (st.get(k), v) for k, v in want.items() if st.get(k) != v}
    if bad:
        raise SystemExit(f"--warm-reads: its steady settings differ from this server's (capture, server): {bad} ({path})")
    return m


def warm_reads_cells(m, sf, arm, fmt, spec):
    """the read cells of a steady capture (cpplatency.steady_cells: its queries, params and candidates), refused unless
    their bodies' shas equal the capture's candidate_ir_sha256 for this arm. Only a forced candidate's sha is recorded,
    so on a meta store every query must be forced (grid_latency forces each cell's picks); on a named format a query may
    run its registry pin, unchecked (-> the queries not checked)"""
    from kvevaluator.measure import cpplatency
    from kvevaluator.measure.plan_runs import label_only_family
    from kvevaluator.datasets.ldbc.schema import get_schema
    cands = m.get("candidates") or {}
    unforced = sorted(q for q in m["queries"] if q not in cands)
    if fmt == "meta" and unforced:
        raise SystemExit(f"--warm-reads: the capture forced no body for {unforced}: its bodies cannot be checked")
    window = tuple(m["params_window"]) if m.get("params_window") else None
    cells, _forced, shas = cpplatency.steady_cells(
        m["workload"], sf, arm, fmt, m["queries"], m["nparam"], (m.get("params") or {}).get("source"),
        label_only_family(get_schema("canonical")), cands, m.get("candidate_set") or "grid", spec, window)
    want = {k: v for k, v in (m.get("candidate_ir_sha256") or {}).items() if k.startswith(arm + "/")}
    if shas != want:
        diff = sorted(k for k in set(shas) | set(want) if shas.get(k) != want.get(k))
        raise SystemExit(f"--warm-reads: {len(diff)} read bodies differ from the capture's candidate_ir_sha256: {diff[:5]}")
    return cells, unforced


def same_run_shape(other, arms, seq_record, provenance, legs=(LEGS,)):
    """--same-counts-as: the other run is a 4-leg run (legs: the leg lists allowed; a kvtimer run also takes the
    apply-only headline run after the warm-up) of the same sequence on the same stores (manifest and scale)"""
    om = json.load(open(os.path.join(other, "steps.csv.meta.json")))
    if om.get("legs") not in [list(x) for x in legs]:
        raise SystemExit(f"--same-counts-as {other}: not a {'4-leg or apply-only' if len(legs) > 1 else '4-leg'} run "
                         f"(legs {om.get('legs')})")
    for arm in arms:
        mine, theirs = provenance.get(arm) or {}, (om.get("provenance") or {}).get(arm) or {}
        if any(mine.get(k) != theirs.get(k) for k in ("store_manifest", "scale")) or not mine.get("store_manifest"):
            raise SystemExit(f"--same-counts-as {other}: {arm} ran on another store (its manifest or scale differs)")
    if om.get("sequence") != seq_record or om.get("arms") != arms:
        raise SystemExit(f"--same-counts-as {other}: its sequence ({len(om.get('sequence') or [])} cells, arms "
                         f"{om.get('arms')}) is not this run's ({len(seq_record)} cells, arms {arms})")


def same_counts_as(other, arms, seq_record, applied, provenance, timer=False):
    """--same-counts-as: every apply of this run against the other run's saved apply (<other>/<arm>/<wl>/<q>/p<idx>/
    headline.json), the same sequence on the same store (cppwrite.same_counts); refuses on any difference, a missing
    cell or a sequence that differs. timer (a kvtimer pass): the other run may also be the apply-only headline run after
    the warm-up. -> the meta's record."""
    same_run_shape(other, arms, seq_record, provenance, legs=(LEGS, APPLY_ONLY) if timer else (LEGS,))
    names = ("the compared run's apply", "the kvtimer pass's apply") if timer else ("4-leg apply", "apply after warm-up")
    n = 0
    for arm in arms:
        for i, c in enumerate(seq_record):
            where = f"{arm}/{c['workload']}/{c['query']}/p{c['param_idx']}"
            path = raw_file(other, arm, c["workload"], c["query"], c["param_idx"], "headline")
            if (arm, i) not in applied or not os.path.isfile(path):
                raise SystemExit(f"--same-counts-as: {where} missing ({'this run' if (arm, i) not in applied else path})")
            same_counts(json.load(open(path)), applied[(arm, i)], where, names)
            n += 1
    if n != len(arms) * len(seq_record) or n == 0:
        raise SystemExit(f"--same-counts-as: {n} cells compared, the sequence has {len(arms) * len(seq_record)}")
    print(f"  same counts as {other}: {n} cells, every step, 0 differ", flush=True)
    return {"run": os.path.abspath(other), "cells_compared": n, "differ": 0}


def driver_state(files=None):
    """git HEAD of this tree and which of the driver's files differ from it (recorded in every meta)."""
    files = files or ["python/kvevaluator/measure/cppwlatency.py", "python/kvevaluator/measure/cppwrite.py", "python/kvevaluator/measure/arenas.py",
                      "python/kvevaluator/measure/cppbatch.py",
                      "python/kvevaluator/measure/numa_binding.py", "python/kvevaluator/measure/cpplatency.py",
                      "python/kvevaluator/measure/steady_rule.py",
                      "python/kvevaluator/capture/binary.py", "python/kvevaluator/capture/store_identity.py", "python/kvevaluator/measure/records.py",
                      "python/kvevaluator/measure/server.py", "python/kvevaluator/measure/stores.py", "python/kvevaluator/measure/plan_runs.py",
                      "python/kvevaluator/measure/workload_plans.py", "python/kvevaluator/datasets/linkbench/params.py"]
    sha = subprocess.run(["git", "-C", _EXP, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    st = subprocess.run(["git", "-C", _EXP, "status", "--porcelain", "--", *files], capture_output=True, text=True).stdout
    return {"git_sha": sha, "dirty_files": sorted(ln[3:] for ln in st.splitlines() if ln.strip())}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--arms", nargs="+", default=["dgraph", "nebula"],
                    help="store dir names under output/stores/<dataset>/<scale>/ (format read from the manifest)")
    ap.add_argument("--queries", nargs="*", default=None)
    ap.add_argument("--nparam", type=int, default=5,
                    help="params per plan (the stratified families hold 5-6 rows; "
                         "conflict-group plans get their family split, so fewer)")
    ap.add_argument("--breakdown", action="store_true",
                    help="(without --pass) add the perf_level 3 pass to the headline run, in one steps.csv")
    ap.add_argument("--pass", dest="passes", choices=["headline", "breakdown", "isolate", "kvtimer"], default=None,
                    help="run ONE regime (one steps.csv per (arm, regime)): headline (perf 1), breakdown (perf 3), "
                         "isolate (each cell from its own fresh arena, perf 1), or kvtimer (perf 2 + the KV timer, "
                         "--warm-reads only: one pass, the headline's protocol with the timer on)")
    ap.add_argument("--kv-timing", action="store_true",
                    help="with --pass breakdown: the KV-boundary timer on (kv_call_ns and per kind, per step, the "
                         "read-before-write included); its columns join steps.csv")
    ap.add_argument("--isolate", type=int, default=0, metavar="N",
                    help="A/B control: the first N cells each from their own fresh arena (--pass isolate: all)")
    ap.add_argument("--cache", choices=["warm", "steady"], default="warm",
                    help="the server's read settings: warm = a buffered cache of --block-cache-gb (default 8); steady = "
                         "the steady read rule of inputs/contract/store_config.json (its cache at this SF, direct I/O, index + "
                         "filter blocks inside the cache), as cpplatency's steady server")
    ap.add_argument("--block-cache-gb", type=float, default=None, help="--cache warm: the block cache (default 8)")
    ap.add_argument("--warm-reads", default=None, metavar="CAPTURE_DIR",
                    help="the writes after a steady read warm-up: the steady read capture whose cells warm the cache "
                         "(its queries, params, bodies and warm-up rule) before the write sequence runs once, apply leg "
                         "only; with --cache steady, --pass headline or kvtimer and one arm")
    ap.add_argument("--spec", default=None,
                    help="--warm-reads on a meta store: the store's spec file (its read bodies' capabilities); refused "
                         "unless it names this store")
    ap.add_argument("--same-counts-as", default=None, metavar="RUN_DIR",
                    help="refuse unless every apply's counts equal that run's (the same store and sequence, its raw "
                         "files per cell: the 4-leg run beside a --warm-reads run; for --pass kvtimer, also the headline "
                         "run after the warm-up), every step, misses included")
    ap.add_argument("--write-buffer-mb", type=int, default=64,
                    help="the memtable size (RocksDB's default 64 MB; a larger one keeps every flush out of the "
                         "timed writes). Recorded in the meta and every response's store_options")
    ap.add_argument("--require-no-flush", action="store_true",
                    help="fail on any flush, write stall or write delay inside the window, or a new SST in the arena")
    ap.add_argument("--require-clean", action="store_true",
                    help="refuse to run from a driver whose files differ from HEAD (the SHA in the meta must be the code)")
    ap.add_argument("--family", dest="workload", default="writes", choices=W.names(kind="write"),
                    help="a write workload (the workload manifests). writes: the write suite (its plan dirs, w_params); "
                         "linkbench_writes: LinkBench's write requests "
                         "(--sf = the scale tag, e.g. n1m; --params-source picks the sample)")
    ap.add_argument("--params-source", default=None, choices=["quick", "quick-next"], help="LinkBench: the request-log sample")
    ap.add_argument("--seed", default="1", help="LinkBench: the interleave seed")
    ap.add_argument("--warmup-write", action="store_true",
                    help="one untimed write (Put + Delete of a key above every format's key space) before the first "
                         "timed cell of each arena: the memtable's first allocation and the WAL's first record")
    ap.add_argument("--raw", default="files", choices=["files", "jsonl"],
                    help="raw responses: JSON files per cell, or one gzip'd JSONL per run (responses.jsonl.gz)")
    ap.add_argument("--requests", choices=["batch", "single"], default="batch",
                    help="how the legs reach the server: batch = one batch per arena (the protocol); single = one request "
                         "per leg through Python, for controls only (it adds a few us to "
                         "each short write's legs, more to apply than to shadow)")
    ap.add_argument("--keep-arena", action="store_true")
    ap.add_argument("--allow-whole-index-filter", action="store_true",
                    help="a CONTROL: stores whose index + filter layout is not this binary's ingest default are "
                         "timed and marked protocol_mixed either way; the flag names the run a control")
    ap.add_argument("--out", required=True)
    numa_binding.add_argument(ap)
    a = ap.parse_args(argv)
    if a.kv_timing and a.passes != "breakdown":
        raise SystemExit("--kv-timing goes with --pass breakdown (the headline and isolate walls carry no timer; "
                         "--pass kvtimer turns it on by itself)")
    kvt = a.passes == "kvtimer"                  # a KV-timer pass (perf 2 + the timer): the RocksDB share's
    if kvt and (not a.warm_reads or a.raw != "files" or a.breakdown):
        raise SystemExit("--pass kvtimer goes with --warm-reads (the writes after the steady read warm-up), --raw files "
                         "and no --breakdown")
    if a.warm_reads and (a.cache != "steady" or a.passes not in ("headline", "kvtimer") or len(a.arms) != 1
                         or a.requests != "batch" or W.get(a.workload).own_data or a.isolate):
        raise SystemExit("--warm-reads goes with --cache steady, --pass headline or kvtimer, one arm, --requests batch, an "
                         "LDBC write workload and no --isolate")
    if a.same_counts_as and (a.passes not in ("headline", "kvtimer") or a.raw != "files"):
        raise SystemExit("--same-counts-as compares the headline pass's raw files: --pass headline or kvtimer, --raw files")
    legs = APPLY_ONLY if a.warm_reads else LEGS

    if a.cache == "steady":
        if a.block_cache_gb is not None:
            raise SystemExit("--cache steady takes its cache from inputs/contract/store_config.json's steady rule: no --block-cache-gb")
        try:                                   # the reads' rule, one copy (store_config.steady_cache_mib)
            cache_bytes = store_config.steady_cache_mib(a.sf, dataset=W.get(a.workload).dataset) << 20
        except ValueError as e:
            raise SystemExit(f"--cache steady: {e}")
        server_read = {"direct_io": True, "cache_index_and_filter_blocks": True}   # cpplatency's steady server
    else:
        cache_bytes = int((8 if a.block_cache_gb is None else a.block_cache_gb) * (1 << 30))
        server_read = {}
    driver = driver_state()
    if a.require_clean and driver["dirty_files"]:
        raise SystemExit(f"--require-clean: driver files differ from HEAD {driver['git_sha'][:9]}: {driver['dirty_files']}")
    if a.passes == "isolate":
        seq_passes, n_isolate = [], a.isolate or 10**9
    elif kvt:
        seq_passes, n_isolate = [("kvtimer", KVTIMER_PERF_LEVEL)], 0
    elif a.passes:
        seq_passes, n_isolate = [(a.passes, 1 if a.passes == "headline" else 3)], 0
    else:
        seq_passes, n_isolate = [("headline", 1)] + ([("breakdown", 3)] if a.breakdown else []), a.isolate
    os.makedirs(a.out, exist_ok=True)
    lb = a.workload == "linkbench_writes"
    if lb:
        if not a.params_source:
            raise SystemExit("--family linkbench_writes needs --params-source quick|quick-next")
        from kvevaluator.measure.stores import scale_root, schema_artifact
        ds = W.get(a.workload).dataset
        root, srv_kw = scale_root(ds, a.sf), {"schema": schema_artifact(ds)}
    else:
        root, srv_kw = None, {}
    numa = numa_binding.choose(a.numa_node)     # once per run: every server (arena) of the run on one node
    srv_kw["numa"] = numa
    server_faults = []                           # per server: its faults over its life (recorded, not checked)
    # each arm's index + filter layout (recorded; a store of another layout than this binary ingests is refused)
    layouts = {}
    for arm in a.arms:
        with open(os.path.join(os.path.join(root, arm) if root else store_dir("ldbc", a.sf, arm), "manifest.json")) as f:
            layouts[arm] = json.load(f)
    whole = store_layout.refuse_stale(layouts, allow_whole=True, what="cppwlatency")   # timed and marked (B4 compares it)
    if whole:
        stale.note("protocol_mixed", f"cppwlatency: stores with index + filter blocks other than this binary's: {whole}")
    layouts = {arm: store_layout.of_manifest(m) for arm, m in layouts.items()}
    raw_out = None
    if a.raw == "jsonl":
        import gzip
        raw_out = gzip.open(os.path.join(a.out, "responses.jsonl.gz"), "wt")
    t0 = time.time()
    provenance = {}                                        # per arm (a multi-arm run has several stores)
    store_options = {}                                     # per pass: the first response's (the store-config check)
    csv_path = os.path.join(a.out, "steps.csv")
    seq_record = []
    applied = {}                                           # (arm, seq_idx) -> the apply response (--same-counts-as)
    warm_record = {}                                       # --warm-reads: per arm, what warmed the cache
    warm_rows = []                                         # --warm-reads: the warm passes' rows (warmup-passes.csv)
    same = None
    wb = a.write_buffer_mb << 20

    def save(arm, wl, q, idx, regime, resp, shadow, shadow2):
        if raw_out is not None:
            raw_out.write(json.dumps({"arm": arm, "workload": wl, "query": q, "row": idx, "regime": regime,
                                      "apply": resp, "shadow": shadow, "shadow2": shadow2}) + "\n")
            return
        d = os.path.dirname(raw_file(a.out, arm, wl, q, idx, regime))
        os.makedirs(d, exist_ok=True)
        # json.dumps: the C encoder (json.dump is pure Python, ~5x slower); the same bytes
        for name, obj in ((f"{regime}.json", resp), (f"{regime}-shadow.json", shadow), (f"{regime}-shadow2.json", shadow2)):
            if obj is None:                      # the apply-only leg (--warm-reads): no shadow runs
                continue
            with open(os.path.join(d, name), "w") as fo:
                fo.write(json.dumps(obj))

    def run_seq(srv, arm, items, regime, level, tag):
        """items = [(seq_idx, cell)], run as one batch (run_cells_paired) -> [(apply, shadow, shadow2)] in item order,
        each apply held to --require-no-flush"""
        run = {"P": 1, "perf_level": level, "iterations": {"warmup": 0, "measured": 1},
               "emit": {"histograms": False, "input_labels": False, "kvpair_stats": False}}   # no RocksDB histogram snapshots: ~2.7 ms per request outside the clock
        # the timer on the breakdown pass (--kv-timing) and the kvtimer pass only: the wall of record carries none
        if (a.kv_timing and regime == "breakdown") or regime == "kvtimer":
            run["kv_timing"] = True
        if a.requests == "batch":
            got = run_cells_paired(srv, [cell for _i, cell in items], run,
                                   os.path.join(a.out, f".batch-{arm}-{tag}-p{os.getpid()}"), legs=legs)
        else:
            got = run_cells_single(srv, [cell for _i, cell in items], run)
        for (_seq_idx, cell), (resp, _shadow, _shadow2) in zip(items, got):
            if a.require_no_flush:
                flush_check(resp, f"{arm}/{cell[1]}/p{cell[2]}", regime)
            if regime == "kvtimer" and "kv_timing" not in resp:
                raise SystemExit(f"{arm}/{cell[1]}/p{cell[2]}: the kvtimer pass's response has no kv_timing block (the "
                                 "binary predates it)")
        return got

    def keep(arm, seq_idx, cell, regime, resp, shadow, shadow2, w):
        """one cell's apply: its raw files, its steps.csv rows, the run's provenance"""
        wl, q, idx, args = cell[0], cell[1], cell[2], cell[3]
        save(arm, wl, q, idx, regime, resp, shadow, shadow2)
        frow = idx if lb else family_row(a.sf, q, args)
        ran, note = LINKBENCH_NOTES.get(seq_idx, (q, "")) if lb else (q, "")
        for r in rows_of(arm, wl, q, idx, seq_idx, regime, resp, shadow, shadow2, frow):
            r["ran_as"], r["seq_note"] = ran, note
            w.writerow(r)
        provenance.setdefault(arm, resp["provenance"])
        store_options.setdefault(regime, (resp.get("run") or {}).get("store_options"))
        applied[(arm, seq_idx)] = resp

    def cells(srv, arm, items, regime, level, w, tag):
        """items = [(seq_idx, cell)], run as one batch (run_cells_paired), then saved and written per cell"""
        for (seq_idx, cell), (resp, shadow, shadow2) in zip(items, run_seq(srv, arm, items, regime, level, tag)):
            keep(arm, seq_idx, cell, regime, resp, shadow, shadow2, w)

    def warmup(srv):
        """the untimed warm-up write (the session's warmup_write: Put + Delete above every format's key space)"""
        req = build_request(None, None, srv.store_dir, srv.fmt, srv.schema, None, srv.block_cache_bytes,
                            srv.direct_io, srv.cache_index_and_filter_blocks, write_buffer_bytes=srv.write_buffer_bytes)
        req["warmup_write"] = True
        r = srv.request(req)
        if r.get("error"):
            raise SystemExit(f"warmup_write: {r['error']}")

    def warm_reads(srv, arm, src, fmt):
        """--warm-reads: the capture's read cells in steady warm passes on this server until the hit rate settles
        (cpplatency.warm_passes), untimed; the pass rows are kept. -> what warmed the cache (the meta's warm_reads record)"""
        from kvevaluator.measure import cpplatency
        from kvevaluator.measure.stores import cpp_store_name
        m = warm_reads_capture(a.warm_reads, a.sf, arm, cache_bytes)
        if fmt == "meta":
            if not a.spec:
                raise SystemExit("--warm-reads on a meta store needs --spec (the store's spec file)")
            if cpp_store_name("meta", "canonical", None, None, spec=a.spec) != os.path.basename(src):
                raise SystemExit(f"--spec {a.spec} is not the spec of {arm}'s store {os.path.basename(src)}")
        rcells, unforced = warm_reads_cells(m, a.sf, arm, fmt, a.spec)
        st = m["regimes"]["steady"]
        # the reads' own run config: their perf level, timers and remote block (cpplatency.remote_run: the page always)
        run = cpplatency.steady_run(st["perf_level"], remote=m.get("remote"),
                                    kv_timing=bool((m.get("kv_timing_by_regime") or {}).get("steady")),
                                    decode_timing=bool(m.get("decode_timing")))
        t = time.time()
        seen = {}
        passes, at = cpplatency.warm_passes(srv, arm, rcells, st["eps"], st["max_warm"], st["seed"], run, a.out,
                                            cache_bytes, True, requests=st.get("requests") or "batch", seen=seen)
        warm_rows.extend(passes)
        rec = {"capture": recorded_path(os.path.abspath(a.warm_reads), Tree().root), "cells": len(rcells), "passes": len(passes),
               "steady_at": at, "capture_steady_at": (st.get("steady_at_pass") or {}).get(arm),
               "rule": {k: st[k] for k in ("eps", "max_warm", "seed", "perf_level")},
               "pass_order_rule": st.get("pass_order_rule"),
               "body_shas": {k: v for k, v in (m.get("candidate_ir_sha256") or {}).items()
                             if k.startswith(arm + "/")},
               "bodies_not_checked": unforced,     # a named format's registry pins (none on meta)
               # the server's store options in the warm passes: the write batch must report the same (a
               # store block that differed would reopen the store and drop the warmed cache)
               "store_options": seen.get("store_options"),
               "wall_s": round(time.time() - t, 1)}
        print(f"  {arm}: warmed by {len(rcells)} read cells in {len(passes)} passes"
              f"{'' if at else ' (not settled: max_warm)'} ({time.time() - t:.0f}s)", flush=True)
        return rec

    def no_new_sst(ar, where):
        new = sorted(set(os.listdir(os.path.join(ar.dir, "db"))) - set(os.listdir(os.path.join(ar.src, "db"))))
        new = [f for f in new if f.endswith(".sst")]
        if a.require_no_flush and new:
            raise SystemExit(f"{where}: the arena gained SST files (a flush): {new[:5]}")

    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS + (KV_TIMER_FIELDS if a.kv_timing or kvt else []))
        w.writeheader()
        for arm in a.arms:
            src = os.path.join(root, arm) if root else store_dir("ldbc", a.sf, arm)
            fmt = json.load(open(os.path.join(src, "manifest.json")))["format"]
            seq = linkbench_sequence(a.sf, a.seed, a.queries, 0, a.params_source) if lb else sequence(a.sf, a.queries, a.nparam)
            # args recorded verbatim: param_idx indexes the SLICED family (family_row is the family's own row)
            seq_record = [{"workload": wl, "query": q, "param_idx": idx,
                           "family_row": idx if lb else family_row(a.sf, q, ar), "args": ar} for wl, q, idx, ar, _i in seq]
            if lb:
                import hashlib
                lines = ["seq_idx,query,family_row,ran_as,seq_note"] + [
                    f"{i},{q},{idx},{LINKBENCH_NOTES.get(i, (q, ''))[0]},{LINKBENCH_NOTES.get(i, (q, ''))[1]}"
                    for i, (_wl, q, idx, _ar, _ir) in enumerate(seq)]
                text = "\n".join(lines) + "\n"
                open(os.path.join(a.out, "sequence.csv"), "w").write(text)
                seq_sha = hashlib.sha256(text.encode()).hexdigest()
            for regime, level in seq_passes:
                with Arena(a.sf, arm, regime, root=root) as ar:
                    ar.keep = a.keep_arena
                    with CppServer(ar.dir, fmt, block_cache_bytes=cache_bytes,
                                   write_buffer_bytes=wb, **server_read, **srv_kw) as srv:
                        if a.warm_reads:
                            warm_record[arm] = warm_reads(srv, arm, src, fmt)
                        if a.warmup_write:
                            warmup(srv)
                        f0, t_srv = numa_binding.snapshot(srv.proc.pid), time.time()
                        cells(srv, arm, list(enumerate(seq)), regime, level, w, regime)
                        el = time.time() - t_srv         # the rate here is per second of the sequence's elapsed time
                        server_faults.append({"arm": arm, "regime": regime, "elapsed_s": round(el, 3),
                                              "server_policies": numa_binding.server_policies(srv.proc.pid),
                                              **numa_binding.fault_columns(f0, numa_binding.snapshot(srv.proc.pid), el * 1e9,
                                                                       srv.proc.pid)})
                    no_new_sst(ar, f"{arm}/{regime}")
                    if a.warm_reads and warm_record[arm]["store_options"] != store_options.get(regime):
                        raise SystemExit(f"{arm}: the write batch ran under other store options than the warm passes "
                                         f"({warm_record[arm]['store_options']} vs {store_options.get(regime)}): the "
                                         "warmed cache may be gone")
                print(f"  {arm}/{regime}: {len(seq)} cells  ({time.time() - t0:.0f}s)", flush=True)
            for seq_idx, cell in enumerate(seq[:n_isolate]):
                # the A/B control: same cell, pristine state, its own arena (one per
                # cell). Its counts may LEGITIMATELY differ from the sequence rows (pristine
                # vs accumulated postings) -- compare walls only where analysis checks the
                # counts match; the warm-up shadow leg absorbs most of the fresh-server tax.
                with Arena(a.sf, arm, f"iso{seq_idx}", root=root) as ar:
                    with CppServer(ar.dir, fmt, block_cache_bytes=cache_bytes,
                                   write_buffer_bytes=wb, **server_read, **srv_kw) as srv:
                        if a.warmup_write:
                            warmup(srv)
                        cells(srv, arm, [(seq_idx, cell)], "isolate", 1, w, f"iso{seq_idx}")
                    no_new_sst(ar, f"{arm}/isolate/{seq_idx}")
            if n_isolate:
                print(f"  {arm}/isolate: {min(n_isolate, len(seq))} cells  ({time.time() - t0:.0f}s)", flush=True)

    if warm_rows:
        with open(os.path.join(a.out, "warmup-passes.csv"), "w", newline="") as f:
            pw = csv.DictWriter(f, fieldnames=PASS_COLUMNS)
            pw.writeheader()
            pw.writerows(warm_rows)
    if a.same_counts_as:
        same = same_counts_as(a.same_counts_as, a.arms, seq_record, applied, provenance, timer=kvt)
    meta = {"sf": a.sf, "arms": a.arms, "nparam": a.nparam, "queries": a.queries, "index_filter_blocks": layouts,
            "passes": [r for r, _ in seq_passes] + (["isolate"] if n_isolate else []),
            "sequence": seq_record, "breakdown": a.breakdown, "isolate": min(n_isolate, len(seq_record)),
            "kv_timing": bool(a.kv_timing) or kvt,   # the KV timer on the breakdown or kvtimer pass (its kv_call_ns columns)
            "block_cache_bytes": cache_bytes, "binary": recorded_path(binary_path()),
            # the read settings the servers ran under (--cache): store_config.write_violations holds a steady run to the
            # steady rule (not a `regimes` block: capture_protocol reads that as a cpplatency meta)
            "read_settings": {"cache": a.cache, "block_cache_bytes": cache_bytes,
                              "direct_io": bool(server_read.get("direct_io")),
                              "cache_index_and_filter_blocks": server_read.get("cache_index_and_filter_blocks")},
            # the engine settings that decide a write's wall (also in every response's run.store_options)
            "write_settings": {"write_buffer_bytes": wb, "max_write_buffer_number": 2,
                               "wal": True, "wal_sync": False, "write_batch": 1, "auto_compactions": False},
            "require_no_flush": a.require_no_flush,
            "workload": a.workload, "params_source": a.params_source, "seed": a.seed if lb else None,
            "warmup_write": a.warmup_write, "raw": a.raw,
            "requests": a.requests,                        # batch: the 4 legs of every cell back to back
            "legs": list(legs),                            # apply alone: --warm-reads (no shadow columns)
            # --warm-reads: per arm the read capture that warmed the cache, its passes and where it settled
            "warm_reads": warm_record or None,
            "same_counts": same,                           # --same-counts-as: the run compared, cells compared, 0 differ
            "sequence_sha256": seq_sha if lb else None,
            "sequence_notes": ({n: sum(1 for _r, x in LINKBENCH_NOTES.values() if n in x.split("+"))
                                for n in ("add_of_existing", "delete_of_sequence_added", "update_of_deleted_node",
                                          "link_of_deleted_node")} if lb else None),
            "driver": driver,                              # the Python that drove the run (git HEAD + dirty files)
            # the servers' NUMA binding (python/kvevaluator/measure/numa_binding.py) and each sequence server's faults over its cells
            "numa": numa, "server_faults": server_faults,
            "provenance": provenance, "wall_s": round(time.time() - t0, 1),
            "store_options_by_pass": store_options,        # what each pass's server reported (inputs/contract/store_config.json)
            "notes": ["WAL on / sync off (default WriteOptions; matches the read-side store config); one engine "
                      "write per logical write (never batched: a normalization the same for every format)",
                      "a memtable switch + background flush can land inside the sequence when the suite writes "
                      "more than write_buffer_bytes; a write that waits on it shows in write_delay_time (breakdown) "
                      "and the stall_micros ticker; --require-no-flush refuses both",
                      "wall_ns is the apply wall: RMW CPU + engine write; the engine write's share is "
                      "wall_ns - shadow_wall_ns (the paired warm control), per step; shadow2_wall_ns - "
                      "shadow_wall_ns is that difference's noise floor",
                      "measured=1 by construction: no within-cell dispersion exists",
                      "headline rows are perf_level 1 (walls + tickers + io counters; all "
                      "PerfContext TIMERS and read counters 0); the breakdown regime (level 3) fills them",
                      "ticker columns are per cell, on the __plan__ row (0 on step rows)",
                      "cross-check: per-step ops, all byte channels, rows, and the final "
                      "payload shadow==apply and shadow2==apply on every cell",
                      "isolate rows: counts may differ from the sequence rows (pristine vs "
                      "accumulated state); filter on regime before regressing on seq_idx",
                      "rocksdb histograms_since_open include the shadow legs' reads"]}
    if raw_out is not None:
        raw_out.close()
    if kvt:   # a timer capture: its kind and level (as cpplatency's timer captures) and the timer's own cost per call
        kv_block = next((r["kv_timing"] for r in applied.values() if "kv_timing" in r), None)
        meta.update(timer_kind="kvtimer", perf_level=KVTIMER_PERF_LEVEL, kv_timing_block=kv_block)
        meta["notes"].append("kvtimer rows are perf_level 2 + the KV timer (kv_call_ns: every RocksDB call at the API "
                             "boundary, the timer's cost inside the walls): the RocksDB share, never a wall of record")
    meta = stale.fold(meta, a.out)    # a run's moved inputs (kvevaluator.stale), and a shell step's notes in --out
    json.dump(meta, open(csv_path + ".meta.json", "w"), indent=1)
    print(f"raw -> {csv_path}  ({meta['wall_s']}s)")


if __name__ == "__main__":
    main()
