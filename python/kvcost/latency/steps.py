"""The latency layer's step-record readers: a capture's steps.csv per request (the walls, the counts, the RocksDB
timers), a store's class totals, the KV timer's own cost per call, the raw per-run medians, the files of a regime.
Shared by the latency studies."""
import csv
import glob
import io
import json
import math
import os
import re
import statistics as st
import tarfile

from kvcost.layout_cost import cost_models
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.measure.latency_raw_files import raw_file, raw_globs
from kvcost.latency.features import BLOCK, BYTE_LANES, GET_LANES, SCAN_LANES, f

STEADY_REGIME = "steady-307mb-p3"     # the default regime of step_files (latency_levels_tables' REGIME)

CELLS = [cell_id(p, d) for p in SETTINGS for d in SETTINGS]


PC_TIMERS = ("seek_child_seek_time", "get_from_output_files_time", "find_next_user_entry_time")


CALLS = ("kv_get_calls", "kv_seek_calls", "kv_next_calls", "kv_iter_close_calls", "kv_put_calls", "kv_delete_calls")


WRITE_TIMERS = ("write_wal_time", "write_memtable_time", "write_delay_time", "write_pre_and_post_process_time",
                "write_scheduling_flushes_compactions_time")


ENGINE_ARMS = ("dgraph", "dgraph-c512k", "nebula", "arangodb", "pertype_csr-c64k")   # a format's store dir is its own name


SUBS = ("grid", "mixes", "added")     # the sub-roots run-w16-mix-latency.sh writes (not its control or aside dirs)


NAN = float("nan")


def isnan(x):
    return x != x


def breakdown_of(regime):
    """the breakdown capture beside a steady regime (cell_latency.breakdown_regime) or cold-p2 (grid_latency
    --cold-breakdown: off by default, so usually absent), or None"""
    if regime == "cold-p2":
        return "cold-breakdown"
    return re.sub(r"^(steady-\d+mb)-p\d", r"\1-breakdown", regime) if regime.startswith("steady-") else None


def find_steps(root, workload, regime, arm, subs=SUBS):
    """the one steps.csv of (regime, arm) under the root or one of its sub-roots, or None; two is an error"""
    hits = sorted(p for d in ("",) + tuple(subs) for p in glob.glob(os.path.join(root, d, workload, regime, arm, "steps.csv")))
    if len(hits) > 1:
        raise SystemExit(f"{arm} {regime}: {len(hits)} captures ({hits}): one per store")
    return hits[0] if hits else None


def raw_pattern(raw):
    """the globs of a capture's raw responses (raw: steady.json, cold.json) below its dir, in a one-body run's layout
    (measure/latency_raw_files.py). A list: the patterns to try, in order"""
    return raw_globs(raw[:-len(".json")])


def timer_pair_ns(capture_dir, pattern):
    """the KV timer's own cost per call, from one raw response under the capture (kv_timing.timer_pair_ns); None if absent.
    pattern: a glob, or a list of globs tried in order (raw_pattern)"""
    for pat in ([pattern] if isinstance(pattern, str) else pattern):
        for p in sorted(glob.glob(os.path.join(capture_dir, pat)))[:1]:
            d = json.load(open(p))
            kt = (d.get("median") or d).get("kv_timing") or {}
            return kt.get("timer_pair_ns")
    return None


def all_nan_out(out, keys):
    """a timer that is 0 on every request of a file is NaN (no timer in that capture), never a zero time"""
    for key in keys:
        if not any(v[key] for v in out.values()):
            for v in out.values():
                v[key] = NAN


def per_request(path, pair_ns=None):
    """a read capture's steps.csv -> {(query, param): {wall, ops, bytes, est_blocks, rocks_kv, rocks_pc, lines}}"""
    out, dup, decode_timed = {}, [], False
    for r in csv.DictReader(open(path)):
        k = (r["query"], r["param_idx"])
        x = out.setdefault(k, {"wall": NAN, "clock": NAN, "ops": 0.0, "bytes": 0.0, "est_blocks": 0.0, "rocks_kv": 0.0,
                               "rocks_pc": 0.0, "calls": 0.0, "kv_wall": 0.0, "flow_wall": 0.0, "decode": 0.0, "_plan": 0})
        if r["step"] == "__plan__":
            x["_plan"] += 1
            if x["_plan"] > 1:
                dup.append(k)
            w = f(r, "wall_ns")
            x["wall"] = w if w > 0 else NAN                # a 0 wall is missing, never a value
            x["clock"] = f(r, "plan_wall_ns")              # the interpreter's clock, its time between steps included
            continue
        if r.get("kind") != "kv":
            x["flow_wall"] += f(r, "wall_ns")
            continue
        S, G = f(r, "S"), f(r, "G")
        gb, sb = sum(f(r, c) for c in GET_LANES), sum(f(r, c) for c in SCAN_LANES)
        x["kv_wall"] += f(r, "wall_ns")
        x["decode"] += f(r, "decode_ns") + f(r, "decompress_ns")
        decode_timed |= (r.get("decode_ns") or "") != ""      # empty = the codec timer was off (null), not a zero
        x["ops"] += cost_models.op_weight(S, G, f(r, "N"))
        x["bytes"] += sum(f(r, c) for c in BYTE_LANES)
        x["est_blocks"] += (G * math.ceil(gb / G / BLOCK) if G else 0) + (S * math.ceil(sb / S / BLOCK) if S else 0)
        x["rocks_kv"] += f(r, "kv_call_ns")
        x["calls"] += sum(f(r, c) for c in CALLS)
        x["rocks_pc"] += sum(f(r, t) for t in PC_TIMERS)
    if dup:
        raise SystemExit(f"{path}: {len(dup)} (query, param) requests appear twice (e.g. {dup[0]}): one regime per file")
    all_nan_out(out, ("rocks_kv", "rocks_pc"))
    if not decode_timed:
        for v in out.values():
            v["decode"] = NAN          # not timed: kv_op is NaN too, never kv_op with the decode time in it
    for v in out.values():
        v["rocks_kv_raw"] = v["rocks_kv"]
        if pair_ns is not None:
            v["rocks_kv"] = v["rocks_kv"] - v["calls"] * pair_ns     # the timer's own cost out
        # the timer table's lines; the wall of record is the
        # steps' own work (cpplatency: work_ns + overhead_ns), so "between" comes from the clock
        v["kv_op"] = v["kv_wall"] - v["rocks_kv_raw"] - v["decode"]
        v["between"] = v["clock"] - v["kv_wall"] - v["flow_wall"]
    return out


def writes_of(root, arm):
    """the updates of one store: {(query, param): {wall, ops, bytes, est_blocks, rocks_kv, rocks_pc, write_timers}} from
    the headline apply wall and counts, and the breakdown pass's KV timer (made with --kv-timing; NaN without it)"""
    p = os.path.join(root, "writes", "durable-p1-noflush", arm, "steps.csv")
    if not os.path.exists(p):
        return {}
    out = {k: {"wall": v["plan"], "ops": v["ops"], "bytes": v["bytes"], "est_blocks": 0.0, "rocks_kv": NAN,
               "rocks_pc": NAN, "write_timers": NAN} for k, v in load_writes(p).items()}
    bdir = os.path.join(root, "writes", "durable-p3-breakdown", arm)
    b = os.path.join(bdir, "steps.csv")
    if not os.path.exists(b):
        return out
    kv, calls, wt = {}, {}, {}
    for r in csv.DictReader(open(b)):
        if r["query"].startswith("iu") and r["step"] != "__plan__":
            k = (r["query"], r["param_idx"])
            kv[k] = kv.get(k, 0.0) + f(r, "kv_call_ns")
            calls[k] = calls.get(k, 0.0) + sum(f(r, c) for c in CALLS)
            wt[k] = wt.get(k, 0.0) + sum(f(r, c) for c in WRITE_TIMERS)
    timed = any(kv.values())
    # the write breakdown's own layout (<arm>/writes/ldbc/plans/<query>/p<idx>/breakdown.json), not cpplatency's: no
    # writer here, read from the archived captures that hold it
    pair = timer_pair_ns(bdir, os.path.join("*", "*", "*", "plans", "*", "p*", "breakdown.json")) if timed else None
    if timed and pair is None:
        raise SystemExit(f"{bdir}: KV timer columns but no kv_timing.timer_pair_ns in its responses")
    for k, v in out.items():
        v["write_timers"] = wt.get(k, NAN)
        if timed and k in kv:
            v["rocks_kv"] = kv[k] - calls[k] * pair
    return out


COUNTS = ("ops", "bytes", "est_blocks")


TIMES = ("wall", "rocks_kv", "rocks_pc")


AGGS = {"mean": st.mean, "median": st.median}


def total(reqs, weights, key, agg=st.median):
    """sum_q weight_q x agg over q's params; NaN when a query has a NaN or no request"""
    per_q = {}
    for (q, _), v in reqs.items():
        if q in weights:
            per_q.setdefault(q, []).append(v.get(key, NAN))
    if set(per_q) != set(weights):
        return NAN
    return sum(weights[q] * (NAN if any(isnan(x) for x in vs) else agg(vs)) for q, vs in per_q.items())


LINES = ("rocks_kv_raw", "decode", "kv_op", "flow_wall", "between")


MEASURES = ("wall", "ops", "bytes", "est_blocks", "rocks_kv", "rocks_pc")


def store_totals(a, arm, weights, reads_only, regime=None):
    """{(class, measure): total} for one store (regime: default a.regime), or None when its capture is missing"""
    regime = regime or a.regime
    p = find_steps(a.latency_root, a.suite, regime, arm, a.subs)
    if p is None:
        return None
    rec = per_request(p)
    base, rep = (regime[:-len("-repeat")], "-repeat") if regime.endswith("-repeat") else (regime, "")
    bd = breakdown_of(base)
    bdp = find_steps(a.latency_root, a.suite, bd + rep, arm, a.subs) if bd else None
    raw = "cold.json" if bd == "cold-breakdown" else "steady.json"     # the raw response of the breakdown's regime
    pair = timer_pair_ns(os.path.dirname(bdp), raw_pattern(raw)) if bdp else None
    if bdp and pair is None:
        raise SystemExit(f"{bdp}: no kv_timing.timer_pair_ns in its raw responses: the timer's own cost cannot be taken out")
    tim = per_request(bdp, pair) if bdp else rec    # cold: the record's own PerfContext
    if bdp and set(tim) != set(rec):
        raise SystemExit(f"{arm}: the breakdown capture covers {len(tim)} requests, the walls {len(rec)}: "
                         f"{len(set(tim) ^ set(rec))} differ (e.g. {sorted(set(tim) ^ set(rec))[0]})")
    tagg = st.mean if a.time_agg == "mean" else st.median
    if not bdp:
        for v in tim.values():
            v["rocks_kv"] = NAN                       # no KV timer outside the steady breakdown capture
    w_reads = {q: n for q, n in weights.items() if q in reads_only}
    wr = writes_of(a.writes_root or a.latency_root, arm) if weights != w_reads else {}
    t = {"_capture": os.path.relpath(p, a.latency_root), "_breakdown": os.path.relpath(bdp, a.latency_root) if bdp else "",
         "_requests": sum(1 for (q, _) in rec if q in w_reads),
         "_keys_reads": frozenset(k for k in rec if k[0] in w_reads),
         "_keys_writes": frozenset(k for k in wr if k[0] in weights and k[0] not in w_reads)}
    for cls, w in (("reads", w_reads), ("all", weights)):
        if cls == "all" and w == w_reads:
            continue
        for m in MEASURES:
            src_reads = tim if m.startswith("rocks_") else rec
            t[(cls, m)] = total({**src_reads, **wr}, w, m, st.mean if m in COUNTS else tagg)
            if m in TIMES:                            # both statistics, the primary also under its plain name
                for an, ag in AGGS.items():
                    t[(cls, f"{m}_{an}")] = total({**src_reads, **wr}, w, m, ag)
        t[(cls, "sum4k")] = cost_models.wops_bytes4k(t[(cls, "ops")], t[(cls, "bytes")])
        t[(cls, "ops_blocks")] = t[(cls, "ops")] + t[(cls, "est_blocks")]
    if wr:
        t["write_timers"] = total(wr, {q: n for q, n in weights.items() if q not in reads_only}, "write_timers", tagg)
    if bdp:
        t["wall_breakdown"] = total(tim, w_reads, "wall", tagg)
        t["clock_breakdown"] = total(tim, w_reads, "clock", tagg)
        for x in LINES:
            t[f"line_{x}"] = total(tim, w_reads, x, tagg)
        t["_negative"] = sum(1 for v in tim.values() if v["kv_op"] < 0 or v["between"] < 0)
    return t


ROCKSDB_TIMERS = PC_TIMERS           # latency_levels_tables' name for the same three PerfContext timers


def step_files(archive, regime=STEADY_REGIME):
    """{store: steps.csv} of every store timed in the regime (the repeat and set-aside units have other dir names)"""
    files = {}
    paths = glob.glob(os.path.join(archive, "*", "*", regime, "*", "steps.csv"))   # <unit>/<workload>/...: interactive (or ldbc)
    for p in sorted(p for p in paths if os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(p)))) in ("interactive", "ldbc")):
        store = os.path.basename(os.path.dirname(p))
        if store in files:
            raise SystemExit(f"{store}: two units in {regime} ({files[store]} and {p})")
        files[store] = p
    return files


def load_writes(path):
    """cppwlatency steps.csv -> {(query, param): {"plan": apply wall_ns, ops: P + D + the RMW reads}} for the IU queries"""
    out = {}
    for r in csv.DictReader(open(path)):
        if not r["query"].startswith("iu") or r["step"] != "__plan__":
            continue
        k = (r["query"], r["param_idx"])
        if k in out:
            raise SystemExit(f"{path}: {k} appears twice")
        wall = f(r, "wall_ns")
        out[k] = {"plan": wall if wall > 0 else float("nan"), "kv": float("nan"), "device": float("nan"),   # a 0 wall = missing, never a value
                  "ops": cost_models.op_weight(f(r, "S"), f(r, "G"), f(r, "N")) + f(r, "P") + f(r, "D"),
                  "bytes": sum(f(r, c) for c in BYTE_LANES) + f(r, "write_key_bytes") + f(r, "write_value_bytes"),
                  "est_blocks": 0.0}
    return out


def pair_ns(capture_dir, raw="steady.json"):
    """the KV timer's own cost per call from the first raw response of the capture (w16_pass_through.timer_pair_ns):
    raw = steady.json (a steady breakdown pass) or cold.json (cold-breakdown); read from the loose files or from the
    capture's raw-requests.tar.xz"""
    v = timer_pair_ns(capture_dir, raw_pattern(raw))
    if v is not None:
        return v
    arc = os.path.join(capture_dir, "raw-requests.tar.xz")
    with tarfile.open(arc) as t:
        depths = [len(p.split(os.sep)) for p in raw_pattern(raw)]   # the same layouts as the loose files, in order
        names = []
        for depth in depths:
            names = names or sorted(n for n in t.getnames()
                                    if len(n.split("/")) == depth and n.split("/")[-2].startswith("p") and n.endswith("/" + raw))
        d = json.load(io.TextIOWrapper(t.extractfile(names[0])))
    return ((d.get("median") or d).get("kv_timing") or {}).get("timer_pair_ns")


def raw_medians(out, arm, query, param, regime="steady"):
    """-> (kv_wall, io_read_nanos, block_read_time), each the median over the measured runs of the kv steps' sum, from
    the raw file below a cpplatency --out (a one-body run: latency_raw_files.raw_file); None if no raw file"""
    path = raw_file(out, arm, query, param, regime)
    if path is None:
        return None
    d = json.load(open(path))
    runs = d.get("measured") if isinstance(d, dict) and "measured" in d else None     # steady.json: one entry per measured run
    per_run = []
    if runs:
        for r in runs:
            kvs = [st_ for st_ in r.get("steps", []) if st_.get("kind") == "kv"]
            per_run.append((sum(f(st_, "wall_ns") for st_ in kvs),
                            sum(float((st_.get("kv") or {}).get("io_read_nanos") or 0) for st_ in kvs),
                            sum(float((st_.get("kv") or {}).get("block_read_time") or 0) for st_ in kvs)))
    else:                                                                             # cold.json / warm.json: per_iteration per step
        kvs = [st_ for st_ in d.get("steps", []) if st_.get("kind") == "kv"]
        n = min((len(st_.get("per_iteration") or []) for st_ in kvs), default=0)
        for j in range(n):
            its = [st_["per_iteration"][j] for st_ in kvs]
            per_run.append((sum(f(it, "wall_ns") for it in its),
                            sum(float((it.get("kv") or {}).get("io_read_nanos") or 0) for it in its),
                            sum(float((it.get("kv") or {}).get("block_read_time") or 0) for it in its)))
    if not per_run:
        return None
    return tuple(st.median(v[i] for v in per_run) for i in range(3))
