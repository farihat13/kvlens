"""How a latency capture's requests reached the server, read from its run metas: the one definition every driver that
resumes a capture and every reader that combines captures uses, so walls from two protocols are never mixed silently.

Three timing settings decide what a latency wall means beyond the store and the plan:
  requests     batch: a pass / query / arena as one batch request, the plans back to back;
               single: one request per plan through Python (a control)
  kv_timing    the KV-boundary timer inside the plan clock (cpplatency --kv-timing; its own cost is in the wall)
  perf timers  RocksDB PerfLevel 3: its per-operation timers, also inside the plan clock
Beside them, the index + filter setting (index_filter_of): the stores' layout (whole / partitioned) and whether the
regime's cache held the index + filter blocks; the fair default is partitioned and in the cache. A meta without a field
reads as single requests, timer off (OLD); a capture with no run meta reads the same, per regime of its steps.csv. A
missing perf level is read from the regime dir name (`...-p<L>`), else it stays unknown (None).

Two kinds of capture: a wall capture (perf <= 2, timer off) has no instrument of ours inside its walls; a breakdown
capture (perf 3 and / or the timer) holds the per-call timers, and its walls carry their cost. A reader of walls never
mixes the kinds or settings (check_same); a reader of the timers asks for a breakdown capture (require_breakdown),
since at perf <= 2 the timer columns are 0, not absent.

    python3 -m kvevaluator.measure.capture_protocol <capture dir or steps.csv> ...   prints each capture's protocol; exit 1
                                                                                  if they differ in any shared regime
    python3 -m kvevaluator.measure.capture_protocol --expect batch[+timer][+perf3][+ifcache] <capture> ...   exit 1 unless every
                                                                                  capture was timed so (a driver's resume check)
"""
import csv
import glob
import json
import os
import re
import sys

OLD = {"requests": "single", "kv_timing": False}
PERF_TIMERS = 3        # RocksDB PerfLevel kEnableTimeExceptForMutex: the first level whose per-operation timers run


def metas_of(capture):
    """the run metas of one capture (a dir holding steps.csv, or the steps.csv itself; a str or a Path), sorted"""
    capture = os.fspath(capture)
    csv_path = capture if capture.endswith(".csv") else os.path.join(capture, "steps.csv")
    return sorted(glob.glob(csv_path + ".meta*.json"))


def perf_timers(level):
    """True when a RocksDB perf level runs its per-operation timers; None when the level is unknown"""
    return None if level is None else int(level) >= PERF_TIMERS


def protocol_of(meta, dir_perf=None):
    """{regime: {"requests", "kv_timing", "perf_timers"}} of one run meta (cpplatency, cppwlatency or cppmixed).
    dir_perf: the perf level the capture's dir name says, used where the meta has none"""
    regimes = meta.get("regimes")
    if isinstance(regimes, dict):                      # cpplatency: warm / cold / steady
        reqs = meta.get("requests_by_regime") or {}
        timer = meta.get("kv_timing_by_regime") or {}
        if not timer and meta.get("kv_timing") == "on":   # a timer meta with kv_timing on: every regime
            timer = {r: True for r in regimes}
        out = {}
        for r, spec in regimes.items():
            spec = spec if isinstance(spec, dict) else {}
            how = reqs.get(r) or (spec.get("requests") if r == "steady" else None)
            out[r] = {"requests": how or OLD["requests"], "kv_timing": bool(timer.get(r, OLD["kv_timing"])),
                      "perf_timers": perf_timers(spec.get("perf_level", dir_perf))}
        return out
    if "passes" in meta:                               # cppwlatency: headline / isolate at perf 1, breakdown at perf 3
        return {r: {"requests": meta.get("requests") or OLD["requests"], "kv_timing": False,
                    "perf_timers": r == "breakdown"} for r in meta["passes"]}
    if "regime" in meta and "perf_level" in meta:      # cppmixed: one replay of the request log, as one batch
        return {"replay": {"requests": "batch", "kv_timing": bool(meta.get("kv_timing")),
                           "perf_timers": perf_timers(meta["perf_level"])}}
    return {}


def index_filter_of(meta):
    """{regime: {"layout", "in_cache"}} of one run meta: the arms' index + filter layout (`index_filter_blocks`; several
    arms: their sorted set, "+"-joined) and whether the regime's cache held the index + filter blocks. A meta from before
    the fields: whole stores; steady kept them outside its bound, warm / cold and the
    others held them in the cache (the server's default)."""
    lay = meta.get("index_filter_blocks")
    layout = ("+".join(sorted(set(lay.values()))) if isinstance(lay, dict) and lay else lay) or "whole"
    in_cache = meta.get("index_filter_in_cache_by_regime") or {}
    regimes = meta.get("regimes")
    if isinstance(regimes, dict):                      # cpplatency
        def held(r):                                   # the per-regime field; else the steady block's own option (the
            spec = regimes[r] if isinstance(regimes[r], dict) else {}    # round-1 in-cache arm); else the old defaults
            return in_cache.get(r, spec.get("cache_index_and_filter_blocks", r != "steady"))
        return {r: {"layout": layout, "in_cache": bool(held(r))} for r in regimes}
    if "passes" in meta:                               # cppwlatency: the server's default cache (index + filter in it)
        return {r: {"layout": layout, "in_cache": bool(in_cache.get(r, True))} for r in meta["passes"]}
    if "regime" in meta and "perf_level" in meta:      # cppmixed: as its meta records it (absent: not in the cache)
        ic = meta.get("index_filter_in_cache", (meta.get("identity") or {}).get("index_filter_in_cache", False))
        return {"replay": {"layout": layout, "in_cache": bool(ic)}}
    return {}


def capture_index_filter(capture):
    """{regime: set of (layout, in_cache)} over every run meta of a capture; a capture with no meta: whole, and the
    cache held index + filter except in steady"""
    out = {}
    metas = metas_of(capture)
    for p in metas:
        with open(p) as f:
            for r, st in index_filter_of(json.load(f)).items():
                out.setdefault(r, set()).add((st["layout"], st["in_cache"]))
    if not metas:
        for r in capture_protocol(capture):
            out[r] = {("whole", r != "steady")}
    return out


def index_filter_default(regime):
    """(layout, in_cache) a driver gives a regime today: the binary's ingest layout, index + filter in the cache"""
    from kvevaluator.measure import store_layout
    return (store_layout.ingest_default(), True)


def dir_perf_level(capture):
    """the perf level a capture's regime dir names (warm-p0, cold-p3, steady-307mb-p3[-repeat], steady-p2), or None"""
    capture = os.fspath(capture)
    d = capture[:-len("steps.csv")] if capture.endswith(".csv") else capture
    for part in reversed(os.path.normpath(d).split(os.sep)):
        m = re.match(r"^(?:warm|cold|steady|durable)(?:-\d+mb)?-p(\d)(?:-|$)", part)
        if m:
            return int(m.group(1))
    return None


def _key(pr, with_perf=True):
    """one protocol as a hashable, comparable tuple"""
    return (pr["requests"], pr["kv_timing"]) + ((pr["perf_timers"],) if with_perf else ())


def capture_protocol(capture, with_perf=True):
    """{regime: set of (requests, kv_timing, perf_timers)} over every run meta of a capture (more than one item = a
    mixed capture); with_perf=False drops the perf element: (requests, kv_timing)"""
    out = {}
    capture = os.fspath(capture)       # a str or a Path (kvstats_layout.latency_csv gives a Path)
    metas = metas_of(capture)
    dperf = dir_perf_level(capture)
    for p in metas:
        with open(p) as f:
            for r, pr in protocol_of(json.load(f), dperf).items():
                out.setdefault(r, set()).add(_key(pr, with_perf))
    if not metas:                                      # no meta: an old capture, by the regimes its rows name
        csv_path = capture if capture.endswith(".csv") else os.path.join(capture, "steps.csv")
        if os.path.exists(csv_path):
            with open(csv_path) as f:
                for r in sorted({row.get("regime") or "" for row in csv.DictReader(f)} - {""}):
                    # cppwlatency's breakdown rows ran at perf 3 whatever the dir (durable-p1) says
                    p = True if r == "breakdown" else perf_timers(dperf)
                    out[r] = {_key({**OLD, "perf_timers": p}, with_perf)}
    return out


def capture_kind(capture):
    """{regime: "wall" | "breakdown" | "unknown"}: wall = perf <= 2 and the timer off in every run meta of the regime"""
    out = {}
    for r, ps in capture_protocol(capture).items():
        if any(t or p for _q, t, p in ps):
            out[r] = "breakdown"
        elif any(p is None for _q, _t, p in ps):
            out[r] = "unknown"
        else:
            out[r] = "wall"
    return out


def require_breakdown(capture, what, need=("perf",)):
    """Refuse (SystemExit) a capture a timer reader cannot use: need "perf" = every run at perf 3 (the PerfContext
    *_time columns and io_read_nanos; 0 below it), "timer" = every run with the KV-boundary timer (the kv_* columns).
    Returns the capture's protocol for the reader to print."""
    got = capture_protocol(capture)
    if not got:
        raise SystemExit(f"[capture_protocol] {what}: {capture} has neither a run meta nor latency rows")
    bad = []
    for r, ps in sorted(got.items()):
        for q, t, p in sorted(ps, key=str):
            if "perf" in need and p is not True:
                bad.append(f"{r}: perf timers {'off' if p is False else 'unknown'}")
            if "timer" in need and not t:
                bad.append(f"{r}: KV timer off")
    if bad:
        raise SystemExit(f"[capture_protocol] {what}: {os.path.relpath(capture)} is not a breakdown capture ("
                         + "; ".join(sorted(set(bad))) + "): its timer columns are 0 or absent. Read the breakdown "
                         "capture beside it (cpplatency --steady-breakdown, grid_latency --steady-breakdown: "
                         "<regime dir>-breakdown), or measure one")
    return got


def refuse_zero_timers(rows, timer_cols, what):
    """A data guard where no run meta is at hand (a concatenated steps file): refuse rows whose KV steps read blocks
    (block_cache_hit_count + block_read_count > 0) while every one of timer_cols is 0 -- a wall capture at perf <= 2"""
    work = sum(float(r.get("block_cache_hit_count") or 0) + float(r.get("block_read_count") or 0) for r in rows)
    timed = sum(float(r.get(c) or 0) for r in rows for c in timer_cols)
    if work > 0 and timed == 0:
        raise SystemExit(f"[capture_protocol] {what}: the rows read blocks but every timer column ({', '.join(timer_cols)}) "
                         "is 0: a wall capture (perf <= 2). Read the breakdown capture beside it")


def label(capture):
    """a short label for a figure or a table: e.g. "steady: batch+timer+perf3; warm: batch" """
    parts = []
    ifl = capture_index_filter(capture)
    for r, s in sorted(capture_protocol(capture).items()):
        parts.append(f"{r}: " + "/".join(sorted(f"{q}{'+timer' if t else ''}{'+perf3' if p else ('+perf?' if p is None else '')}"
                                                for q, t, p in s))
                     + "".join(f" [{lay}, index+filter {'in' if inc else 'outside'} the cache]"
                               for lay, inc in sorted(ifl.get(r, set()), key=str)))
    return "; ".join(parts) or "no latency rows"


def cpplatency_default(regime, perf_level):
    """the protocol cpplatency gives a regime at a perf level when no flag says otherwise: batch requests, the KV timer
    off"""
    return ("batch", False, perf_timers(perf_level))


def resume_mismatch(capture, want, want_index_filter=None, want_store_config=False):
    """For a driver about to SKIP a capture already on disk: why it may not (a str), or None. `want` = {regime:
    (requests, kv_timing, perf_timers)} the driver would measure today, or one tuple for every regime. A capture without a run meta reads as
    the old protocol (capture_protocol); one with neither a meta nor latency rows is refused. want_index_filter
    (optional): {regime: (layout, in_cache)} or one pair for every regime, checked the same way. want_store_config: every
    cpplatency run meta must also hold the decided settings of inputs/contract/store_config.json (measure/store_config.violations): a
    capture timed before them (readahead off, cold buffered at perf 3, no store options recorded) is not reused."""
    got = capture_protocol(capture)
    if not got:
        return "it has neither a run meta nor latency rows, so its request protocol is unknown"
    bad = {r: sorted(s) for r, s in sorted(got.items()) if s != {want if isinstance(want, tuple) else want.get(r)}}
    if bad:
        return "it was timed under another request protocol than this run would use: " + "; ".join(
            f"{r} {v} (now {want if isinstance(want, tuple) else want.get(r)})" for r, v in bad.items())
    if want_index_filter is not None:
        wi = lambda r: want_index_filter if isinstance(want_index_filter, tuple) else want_index_filter.get(r)
        gi = capture_index_filter(capture)
        bad = {r: sorted(s) for r, s in sorted(gi.items()) if s != {wi(r)}}
        if bad:
            return "its index + filter setting (layout, in the cache) differs from this run's: " + "; ".join(
                f"{r} {v} (now {wi(r)})" for r, v in bad.items())
    if want_store_config:
        from kvevaluator.measure import store_config          # here: a reader of protocols that never asks needs no doc on disk
        for p in metas_of(capture):
            with open(p) as f:
                m = json.load(f)
            if isinstance(m.get("regimes"), dict):
                _, diff = store_config.violations(m)
                if diff:
                    return "it was timed under other store settings than inputs/contract/store_config.json's decided settings: " + "; ".join(diff)
    return None


def check_same(captures, what, allow_mixed=False, with_perf=True):
    """Refuse (SystemExit) a set of captures whose walls came from different protocols in a regime they share; with
    allow_mixed, return {capture: label} for a mixed set instead (empty when the set is one protocol), for the reader to
    write beside its numbers. with_perf=False compares requests and the timer only: for a reader that draws two perf
    levels of one regime as two named series (warm-p0 beside warm-p3), each series then checked on its own."""
    per = {c: capture_protocol(c, with_perf) for c in captures}
    ifl = {c: capture_index_filter(c) for c in captures}
    per = {c: {r: {k + (i,) for k in ks for i in sorted(ifl[c].get(r, {None}), key=str)} for r, ks in p.items()}
           for c, p in per.items()}   # each protocol element + the regime's (layout, in_cache)
    clash = {}
    for r in sorted({r for p in per.values() for r in p}):
        seen = set().union(*(p.get(r, set()) for p in per.values()))
        if len(seen) > 1:
            clash[r] = {c: sorted(p[r], key=str) for c, p in per.items() if r in p}
    if clash and not allow_mixed:
        lines = [f"  {r}: " + "; ".join(f"{os.path.relpath(c)} {v}" for c, v in sorted(cs.items())) for r, cs in clash.items()]
        raise SystemExit(f"[capture_protocol] {what}: these captures timed their requests differently, their walls are "
                         f"not comparable (requests batch/single, kv_timing on/off, perf timers on/off, "
                         f"(index + filter layout, in the cache)):\n" + "\n".join(lines) +
                         "\nre-measure the old ones, or pass the reader's --allow-mixed-protocol to label them instead")
    return {c: label(c) for c in captures} if (allow_mixed and clash) else {}


def main(argv):
    expect = None
    if argv[:1] == ["--expect"]:
        expect, argv = argv[1], argv[2:]
    caps = [a for a in argv if not a.startswith("-")]
    if not caps:
        raise SystemExit(__doc__)
    if expect:
        parts = expect.split("+")
        want = (parts[0], "timer" in parts[1:], "perf3" in parts[1:])
        # +ifcache: also today's index + filter setting (the binary's ingest layout, in the cache) in every regime
        want_if = index_filter_default(None) if "ifcache" in parts[1:] else None
        bad = [(c, resume_mismatch(c, want, want_if)) for c in caps]
        for c, why in bad:
            if why:
                print(f"{c}: {why}", file=sys.stderr)
        return 1 if any(why for _, why in bad) else 0
    for c in caps:
        print(f"{c}: {label(c)}")
    try:
        check_same(caps, "the captures given")
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{len(caps)} capture(s): one protocol per regime")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
