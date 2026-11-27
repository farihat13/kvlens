"""Wall and breakdown captures: the steady walls of record carry no timer of ours, the
per-call timers come from a separate breakdown pass, and no reader mixes the two kinds silently (measure/capture_protocol).

  offline   synthetic run metas, no store: capture_kind; check_same refuses a wall / breakdown mix in one regime and lets
            a reader compare two perf levels as named series (with_perf=False); require_breakdown refuses a wall capture
            (and a timer-off capture when the timer is needed); latency_fresh's per-regime protocol (warm-p0, cold-p3,
            durable headline + breakdown); cppmixed's clean / timed metas; the perf level read from a dir name; the
            --expect CLI; grid_latency's breakdown dir name, wanted protocol and resume refusals; the index + filter
            setting (old metas read whole / outside in steady, mixing refused, resume, --expect +ifcache), the store layout
            refusal, and the counts fingerprint's fields (the layout is not one of them)
  live      sf0.1 (default on): a preflight names any store it needs that is missing, with the command that makes it;
            then cpplatency --steady --steady-breakdown on the dgraph store (wall: perf 2, no timer
            columns, perf-3 timers 0; breakdown: the timer columns, timers > 0; S / G / N and every byte lane equal step
            by step), then grid_latency --steady-breakdown on one meta cell (both captures gated against the counts
            capture: --counts, default output/captures/ldbc/sf0.1/interactive/reported; a second call resumes both;
            a breakdown missing beside a wall: the wall moved aside, both made again, marked), then grid_latency with
            2 kvtimer passes and 1 breakdown pass (the three captures gated, the pass shape, the timers each holds),
            then with --steady-kv-timed (the fold: the timer in the walls, its own dirs, the store config held, and
            refused beside --steady-kvtimer-passes)
Prints the cases each part covered. Exit 1 on any failure.
Run: python3 tests/gates/check_capture_kinds.py [--offline-only] [--keep] [--counts DIR]
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from kvevaluator.paths import OUTPUT_DIR, ROOT, recorded_path
from kvevaluator.datasets import schema_path
from kvevaluator import stale

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import capture_protocol as CP
from kvevaluator.output_tree import Capture, Tree
from kvevaluator.measure import latency_fresh as KF
from kvevaluator.measure import kvstats_layout as L
from kvevaluator.measure import store_config as SC
from kvevaluator.measure import stores
from kvevaluator.capture.step_record import READ_BYTE_LANES, READ_OPS  # (the lanes, one definition)

FAIL = []
N = {"offline": 0, "live": 0}


def check(part, name, ok, detail=""):
    N[part] += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAIL.append(name)


def refuses(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


def capture(root, rel, meta=None, regime="steady"):
    """a capture dir with a one-row steps.csv and (optionally) a run meta"""
    d = os.path.join(root, rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "steps.csv"), "w") as f:
        f.write("arm,query,param_idx,regime,step\nx,is1,1,%s,__plan__\n" % regime)
    if meta is not None:
        json.dump(meta, open(os.path.join(d, "steps.csv.meta.json"), "w"))
    return d


def lat_meta(regime, perf, timer, requests="batch", ifb=None, cfg=False, breakdown=False):
    """a cpplatency run meta of one regime; ifb = (layout, in the cache) writes the index + filter fields, None
    leaves them out (a meta without them); cfg: the store settings inputs/contract/store_config.json decides (a meta
    with direct I/O in cold and steady, the store options); breakdown: the steady breakdown capture's mark"""
    spec = {"perf_level": perf} if regime != "steady" else {"perf_level": perf, "requests": requests}
    m = {"regimes": {regime: spec}, "kv_timing_by_regime": {regime: timer}, "requests_by_regime": {regime: requests}}
    if cfg:   # at sf3, so a steady one has the decided 307 MiB cache (the dirs say steady-307mb)
        spec.update(direct_io=regime != "warm", store_options=dict(SC.decided()["store_options"], direct_io=regime != "warm"))
        m["sf"] = 3
        if regime == "steady":
            spec["block_cache_bytes"] = spec["store_options"]["block_cache_bytes"] = SC.steady_cache_mib(3) << 20
    if breakdown:
        m["wall_capture"] = "the wall capture"
    if ifb:
        m.update({"index_filter_blocks": {"meta-a": ifb[0]}, "index_filter_in_cache_by_regime": {regime: ifb[1]}})
    return m


def offline(tmp):
    print("offline: synthetic captures")
    old = capture(tmp, "g/ldbc/steady-307mb-p3/meta-a", lat_meta("steady", 3, True))       # the grid before 10-18
    today = CP.index_filter_default("steady")                                               # (the binary's layout, in the cache)
    new = capture(tmp, "g/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 2, False, ifb=today, cfg=True))      # a wall capture
    brk = capture(tmp, "g/ldbc/steady-307mb-breakdown/meta-a", lat_meta("steady", 3, True, ifb=today, cfg=True, breakdown=True))
    p3_notimer = capture(tmp, "g/ldbc/steady-307mb-p3/meta-b", lat_meta("steady", 3, False))
    check("offline", "kind: perf 3 + timer = breakdown", CP.capture_kind(old) == {"steady": "breakdown"}, CP.capture_kind(old))
    check("offline", "kind: perf 2, timer off = wall", CP.capture_kind(new) == {"steady": "wall"}, CP.capture_kind(new))
    check("offline", "check_same refuses a wall + breakdown mix", refuses(lambda: CP.check_same([old, new], "t")))
    labels = CP.check_same([old, new], "t", allow_mixed=True)
    check("offline", "allow_mixed labels the mix", len(labels) == 2 and "perf3" in labels[old], labels)
    brk_old = capture(tmp, "g/ldbc/steady-307mb-breakdown-old/meta-a", lat_meta("steady", 3, True))   # same index + filter as `old`
    check("offline", "check_same keeps one kind", CP.check_same([old, brk_old], "t") == {})
    check("offline", "require_breakdown refuses a wall capture", refuses(lambda: CP.require_breakdown(new, "t")))
    check("offline", "require_breakdown takes a breakdown capture",
          not refuses(lambda: CP.require_breakdown(brk, "t", need=("perf", "timer"))))
    check("offline", "require_breakdown(timer) refuses perf 3 with the timer off",
          refuses(lambda: CP.require_breakdown(p3_notimer, "t", need=("perf", "timer"))))
    w0 = capture(tmp, "k/warm-p0/dgraph", lat_meta("warm", 0, False), "warm")
    w3 = capture(tmp, "k/warm-p3/dgraph", lat_meta("warm", 3, False), "warm")
    check("offline", "two perf levels as named series: with_perf=False passes",
          CP.check_same([w0, w3], "t", with_perf=False) == {})
    check("offline", "... and with the perf element refuses", refuses(lambda: CP.check_same([w0, w3], "t")))
    # the perf level from the dir name when the meta has none; no meta at all: single, timer off, perf by the dir
    nometa = capture(tmp, "k/cold-p3/nebula", None, "cold")
    check("offline", "no meta: single, timer off, perf from the dir", CP.capture_protocol(nometa) == {"cold": {("single", False, True)}},
          CP.capture_protocol(nometa))
    noperf = capture(tmp, "k/x/arango", {"regimes": {"warm": {}}, "requests_by_regime": {"warm": "batch"}}, "warm")
    check("offline", "unknown perf level = kind unknown", CP.capture_kind(noperf) == {"warm": "unknown"}, CP.capture_kind(noperf))
    check("offline", "dir_perf_level", [CP.dir_perf_level(p) for p in (old, new, brk, w0, nometa + "/steps.csv")] == [3, 2, None, 0, 3])
    # cppmixed
    clean = capture(tmp, "m/cell/clean-rep1", {"regime": "clean", "perf_level": 2, "kv_timing": False}, "replay")
    timed = capture(tmp, "m/cell/timed-rep1", {"regime": "timed", "perf_level": 3, "kv_timing": True}, "replay")
    check("offline", "cppmixed clean = wall, timed = breakdown",
          CP.capture_kind(clean) == {"replay": "wall"} and CP.capture_kind(timed) == {"replay": "breakdown"})
    check("offline", "cppmixed: clean + timed refused as one set", refuses(lambda: CP.check_same([clean, timed], "t")))
    # cppwlatency: headline perf 1, breakdown perf 3
    wl = capture(tmp, "k/durable-p1/dgraph", {"passes": ["headline", "breakdown"], "requests": "batch"}, "headline")
    check("offline", "cppwlatency passes", CP.capture_protocol(wl) == {"headline": {("batch", False, False)},
                                                                     "breakdown": {("batch", False, True)}}, CP.capture_protocol(wl))
    # latency_fresh: each stage against its regime's protocol
    paths = {"warm": os.path.join(w0, "steps.csv"), "cold": os.path.join(capture(tmp, "k/cold-p2/dgraph", lat_meta("cold", 2, False), "cold"), "steps.csv"),
             "durable": os.path.join(wl, "steps.csv")}
    saved = dict(KF.STAGES)
    try:
        for st, p in paths.items():
            KF.STAGES[st] = (lambda p: lambda *_: p)(p)
        ok = all(KF.protocol_mismatch(0.1, "interactive", "dgraph", st) is None for st in paths)
        check("offline", "latency_fresh: warm-p0, cold-p2, durable are today's protocol", ok,
              {st: KF.protocol_mismatch(0.1, "interactive", "dgraph", st) for st in paths})
        KF.STAGES["cold"] = lambda *_: os.path.join(capture(tmp, "k/cold-p3/x", lat_meta("cold", 3, False), "cold"), "steps.csv")
        check("offline", "latency_fresh: a cold capture at perf 3 (the timers) is not fresh",
              KF.protocol_mismatch(0.1, "interactive", "x", "cold") is not None)
        KF.STAGES["cold"] = lambda *_: os.path.join(capture(tmp, "k/cold-p2/z", lat_meta("cold", 2, False), "cold"), "steps.csv")
        KF.STAGES["warm"] = lambda *_: os.path.join(capture(tmp, "k/warm-p0/z", lat_meta("warm", 0, False, cfg=True), "warm"), "steps.csv")
        check("offline", "latency_fresh: a cold capture without the decided store settings is stale, a warm one with them is not",
              KF.store_config_mismatch(0.1, "interactive", "z", "cold") is not None and KF.store_config_mismatch(0.1, "interactive", "z", "warm") is None,
              (KF.store_config_mismatch(0.1, "interactive", "z", "cold"), KF.store_config_mismatch(0.1, "interactive", "z", "warm")))
        good = lat_meta("cold", 2, False, cfg=True)
        KF.STAGES["cold"] = lambda *_: os.path.join(capture(tmp, "k/cold-p2/c", good, "cold"), "steps.csv")
        check("offline", "latency_fresh: a cold capture with the decided store settings is fresh",
              KF.store_config_mismatch(0.1, "interactive", "c", "cold") is None, KF.store_config_mismatch(0.1, "interactive", "c", "cold"))
        lied = lat_meta("cold", 2, False, cfg=True)
        lied["regimes"]["cold"]["store_options"]["direct_io"] = False         # the flag says direct, the server read buffered
        n, bad = SC.violations(lied)
        check("offline", "store_config: the server's own direct_io is compared, not only the driver's flag",
              any("the server's direct_io False" in b for b in bad), (n, bad))
        brk_cold = lat_meta("cold", 3, False, cfg=True)                        # run-kvstats --cold-breakdown: buffered, perf 3
        brk_cold["regimes"]["cold"].update(direct_io=False)
        brk_cold["regimes"]["cold"]["store_options"]["direct_io"] = False
        n0, bad0 = SC.violations(brk_cold)
        brk_cold["breakdown_pass"] = True
        n1, bad1 = SC.violations(brk_cold)
        check("offline", "store_config: a buffered perf-3 cold is held to the timed row, the same declared a breakdown to the breakdown row",
              len(bad0) == 3 and bad1 == [] and n1 > 0, (bad0, bad1))   # perf, the flag, the server's direct_io
        # a record holds what it holds: a key it lacks (a run from before the store reported it) is not compared, a key
        # it holds is held to the file, the rocksdb block too when recorded
        def unrep(drop=("block_size", "fill_cache"), set_=None, rocksdb=None, sf=3):
            m = lat_meta("steady", 2, False, cfg=True)
            m["sf"] = sf
            so = m["regimes"]["steady"]["store_options"]
            for k in drop:
                so.pop(k, None)
            so.update(set_ or {})
            if rocksdb is not None:
                so["rocksdb"] = rocksdb
            return SC.violations(m)[1]
        decided_rocksdb = SC.decided()["rocksdb"]
        cases = {"keys missing: not compared": (unrep(), 0),
                 "a recorded wrong value is caught": (unrep(drop=(), set_={"fill_cache": False}), 1),
                 "the recorded rocksdb block, equal": (unrep(rocksdb=dict(decided_rocksdb)), 0),
                 "the recorded rocksdb block, bloom 12": (unrep(rocksdb=dict(decided_rocksdb, bloom_bits_per_key=12)), 1)}
        got = {c: sum("store option" in x for x in b) for c, (b, _) in cases.items()}
        check("offline", "store_config: a key a record lacks is not compared; a recorded one, rocksdb's too, is held to the file",
              all(got[c] == want for c, (_, want) in cases.items()), got)
        lb = unrep(sf="n1m")
        check("offline", "store_config: a steady run on a scale tag (n1m) is a difference, not a crash",
              any("no rule for this sf" in x for x in lb), lb)
        KF.STAGES["durable"] = lambda *_: os.path.join(wl, "steps.csv")
        old_durable = KF.store_config_mismatch(0.1, "interactive", "dgraph", "durable")
        dur = {"passes": ["headline", "breakdown"], "requests": "batch",
               "store_options_by_pass": {r: dict(SC.decided()["store_options"]) for r in ("headline", "breakdown")}}
        KF.STAGES["durable"] = lambda *_: os.path.join(capture(tmp, "k/durable-p1/d", dur, "headline"), "steps.csv")
        check("offline", "latency_fresh: a durable capture with no store options is stale, one with the decided ones is not",
              old_durable is not None and KF.store_config_mismatch(0.1, "interactive", "d", "durable") is None,
              (old_durable, KF.store_config_mismatch(0.1, "interactive", "d", "durable")))
        check("offline", "block_read_count: a capture with readahead off counts the misses, one with readahead on does not",
              SC.block_reads_are_misses(paths["cold"]) and not SC.block_reads_are_misses(os.path.join(tmp, "k/cold-p2/c")),
              (SC.block_reads_are_misses(paths["cold"]), SC.block_reads_are_misses(os.path.join(tmp, "k/cold-p2/c"))))
        check("offline", "block_read_count: a reader of the misses refuses a readahead-on capture",
              refuses(lambda: SC.require_block_reads_are_misses(os.path.join(tmp, "k/cold-p2/c"), "t")))
        KF.STAGES["warm"] = lambda *_: os.path.join(capture(tmp, "k/warm-p0/y", lat_meta("warm", 0, True), "warm"), "steps.csv")
        check("offline", "latency_fresh: a warm capture with the timer is not fresh",
              KF.protocol_mismatch(0.1, "interactive", "y", "warm") is not None)
    finally:
        KF.STAGES.clear()
        KF.STAGES.update(saved)
    # cold_regime: one regime over many roots; the dated cold-p3 when no root has cold-p2
    for rel in ("cr/a/cold-p3/m1", "cr/a/cold-p3/m2", "cr/a/cold-p2/m1", "cr/b/cold-p3/m3"):
        os.makedirs(os.path.join(tmp, rel))
    cr = [os.path.join(tmp, "cr", x) for x in ("a", "b")]
    check("offline", "cold_regime: cold-p2 when any root has it, cold-p3 for a dated root, None for neither",
          (L.cold_regime(*cr), L.cold_regime(cr[1]), L.cold_regime(os.path.join(tmp, "cr", "none"))) == ("cold-p2", "cold-p3", None),
          (L.cold_regime(*cr), L.cold_regime(cr[1])))
    # plot_mix_after_run: warm-p0 beside warm-p3 is two series (fine); another request protocol in one of them is not
    lay = {d: os.path.join(capture(tmp, f"pm/{d}/s", lat_meta(d.split("-")[0], int(d[-1]), False), d.split("-")[0]), "steps.csv")
           for d in ("warm-p0", "warm-p3")}
    saved_csv = KF.L.latency_csv
    try:
        KF.L.latency_csv = lambda sf, wl, d, slug, root=None: lay.get(d, os.path.join(tmp, "absent", d, "steps.csv"))
        ok1 = KF.plot_mix_after_run(0.1, "interactive", ["s"], set()) is None
        json.dump(lat_meta("warm", 3, False, requests="single"), open(lay["warm-p3"] + ".meta.json", "w"))
        ok2 = KF.plot_mix_after_run(0.1, "interactive", ["s"], set()) is not None
    finally:
        KF.L.latency_csv = saved_csv
    check("offline", "plot_mix_after_run: two perf series pass, a request mix across them is caught", ok1 and ok2, (ok1, ok2))
    # the data guard for a concatenated steps file (no meta): blocks read, every timer 0 -> refused
    zero = [{"block_cache_hit_count": "5", "block_read_count": "1", "seek_child_seek_time": "0"}]
    timed = [{"block_cache_hit_count": "5", "block_read_count": "1", "seek_child_seek_time": "120"}]
    check("offline", "refuse_zero_timers: refuses all-zero timers on rows that read blocks, keeps timed rows",
          refuses(lambda: CP.refuse_zero_timers(zero, ("seek_child_seek_time",), "t"))
          and not refuses(lambda: CP.refuse_zero_timers(timed, ("seek_child_seek_time",), "t")))
    # no-meta cppwlatency: its breakdown rows ran at perf 3 whatever the dir name says
    wl0 = capture(tmp, "k2/durable-p1/dgraph", None, "breakdown")
    check("offline", "no-meta durable breakdown rows read perf 3", CP.capture_protocol(wl0) == {"breakdown": {("single", False, True)}},
          CP.capture_protocol(wl0))
    # the CLI
    rc = subprocess.run([sys.executable, os.path.join(str(ROOT), "python", "kvevaluator", "measure", "capture_protocol.py"), "--expect", "batch+timer+perf3", old],
                        capture_output=True).returncode
    rc2 = subprocess.run([sys.executable, os.path.join(str(ROOT), "python", "kvevaluator", "measure", "capture_protocol.py"), "--expect", "batch", old],
                         capture_output=True).returncode
    check("offline", "--expect batch+timer+perf3 passes the old grid capture, --expect batch refuses it", rc == 0 and rc2 == 1, (rc, rc2))
    # the resume rules of a timed cell (cell_latency, grid_latency's timing half)
    from kvevaluator.measure import cell_latency as CL
    check("offline", "breakdown dir name", CL.breakdown_regime("steady-307mb-p2-repeat") == "steady-307mb-breakdown-repeat")
    check("offline", "wanted protocol per dir", [CL.want_protocol(r) for r in ("steady-307mb-p2", "warm-p0", "cold-p2", "steady-307mb-breakdown")]
          == [("batch", False, False), ("batch", False, False), ("batch", False, False), ("batch", True, True)])
    def moved_aside(path, *a):
        """already_measured re-measures (False) and the capture is moved aside under tmp, kept, with a stale note"""
        with stale.scope() as notes:
            got = CL.already_measured(path, "steady-307mb-p2", "a", *a, root=tmp)
        return got is False and not os.path.exists(path) and len(notes) == 1 and any(
            os.path.exists(os.path.join(tmp, d, os.path.relpath(path, tmp))) for d in os.listdir(tmp) if d.startswith("aside-"))
    check("offline", "resume moves aside a p2 wall timed before the decided store settings (inputs/contract/store_config.json), "
          "re-measures it, marked", moved_aside(capture(tmp, "g3/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 2, False, ifb=today))))
    check("offline", "resume moves aside a p3 + timer capture filed as p2, re-measures it, marked",
          moved_aside(capture(tmp, "g2/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 3, True))))
    check("offline", "resume moves aside a wall whose breakdown is missing, re-measures it, marked",
          moved_aside(capture(tmp, "g4/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 2, False, ifb=today, cfg=True)),
                      {"steady-307mb-breakdown": os.path.join(tmp, "nowhere")}))
    kept = []
    check("offline", "resume keeps a wall + breakdown pair",
          not refuses(lambda: kept.append(CL.already_measured(new, "steady-307mb-p2", "a", {"steady-307mb-breakdown": brk}))) and kept == [True])
    # the index + filter setting: layout and whether the cache held the blocks
    print("offline: index + filter setting")
    check("offline", "the binary ingests partitioned stores", today == ("partitioned", True), today)
    oldw = capture(tmp, "i/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 2, False))       # p2 wall, before 10-18
    oldc = capture(tmp, "i/ldbc/cold-p3/meta-a", lat_meta("cold", 3, False))
    check("offline", "an old steady meta reads (whole, outside), an old cold one (whole, in the cache)",
          CP.capture_index_filter(oldw) == {"steady": {("whole", False)}} and CP.capture_index_filter(oldc) == {"cold": {("whole", True)}},
          (CP.capture_index_filter(oldw), CP.capture_index_filter(oldc)))
    check("offline", "a new meta reads its fields", CP.capture_index_filter(new) == {"steady": {today}}, CP.capture_index_filter(new))
    check("offline", "check_same refuses old and new walls of one timing protocol", refuses(lambda: CP.check_same([oldw, new], "t")))
    check("offline", "check_same keeps two new walls", CP.check_same([new, capture(tmp, "i2/ldbc/steady-307mb-p2/meta-a",
                                                                                  lat_meta("steady", 2, False, ifb=today))], "t") == {})
    check("offline", "resume moves aside an old steady wall (whole, outside), re-measures it, marked",
          moved_aside(capture(tmp, "i4/ldbc/steady-307mb-p2/meta-a", lat_meta("steady", 2, False))))
    mixed = capture(tmp, "i3/x", {"regime": "replay", "perf_level": 1, "kv_timing": False,
                                  "identity": {"index_filter_in_cache": False}})            # a cppmixed meta before 10-18
    check("offline", "an old cppmixed meta reads (whole, outside)", CP.capture_index_filter(mixed) == {"replay": {("whole", False)}},
          CP.capture_index_filter(mixed))
    cli = os.path.join(str(ROOT), "python", "kvevaluator", "measure", "capture_protocol.py")
    rc_new = subprocess.run([sys.executable, cli, "--expect", "batch+ifcache", new], capture_output=True).returncode
    rc_old = subprocess.run([sys.executable, cli, "--expect", "batch+ifcache", oldw], capture_output=True).returncode
    check("offline", "--expect batch+ifcache passes a new wall and refuses an old one", (rc_new, rc_old) == (0, 1), (rc_new, rc_old))
    # the store layout check (python/kvevaluator/measure/store_layout.py): a latency run refuses a whole store unless told
    from kvevaluator.measure import store_layout as SL
    check("offline", "a whole store (or one with no field) is refused, a partitioned one passes, the control allows",
          refuses(lambda: SL.refuse_stale({"a": {"index_filter_blocks": "whole"}}))
          and refuses(lambda: SL.refuse_stale({"a": {}}))
          and not refuses(lambda: SL.refuse_stale({"a": {"index_filter_blocks": "partitioned"}}))
          and not refuses(lambda: SL.refuse_stale({"a": {"index_filter_blocks": "whole"}}, allow_whole=True)))
    # the COUNTS fingerprint (latency_fresh.store_fingerprint): every field counts depend on moves it, the index +
    # filter layout does not (counts are taken at our store boundary: the whole-vs-partitioned count control)
    fp_dir = os.path.join(tmp, "fp-store")
    os.makedirs(fp_dir, exist_ok=True)
    base = {"key_codec": "realistic-v4", "value_codec_hash": "abc", "schema": {"name": "canonical"}, "keys": 10,
            "index_filter_blocks": "partitioned"}
    real = KF.stores.store_dir
    KF.stores.store_dir = lambda dataset, sf, slug, spec=None: fp_dir
    try:
        def fp(m):
            json.dump(m, open(os.path.join(fp_dir, "manifest.json"), "w"))
            return KF.store_fingerprint("0.1", "x")
        f0 = fp(base)
        moved = {k: fp({**base, k: v}) != f0 for k, v in (("key_codec", "other"), ("value_codec_hash", "def"),
                                                          ("schema", {"name": "label_only"}), ("keys", 11))}
        same = fp({**base, "index_filter_blocks": "whole"}) == f0 and fp({k: v for k, v in base.items() if k != "index_filter_blocks"}) == f0
    finally:
        KF.stores.store_dir = real
    check("offline", "the counts fingerprint moves with key_codec, value_codec_hash, schema and keys", all(moved.values()), moved)
    check("offline", "... and not with the index + filter layout", same)
    # a round-1 steady meta whose steady block held index + filter in the cache reads as in the cache
    r1 = {"regimes": {"steady": {"cache_index_and_filter_blocks": True}}, "arms": ["a"]}
    check("offline", "an old steady meta whose steady block named the cache reads that setting",
          CP.index_filter_of(r1)["steady"] == {"layout": "whole", "in_cache": True}, CP.index_filter_of(r1))
    # re-ingest keeps a store's layout unless told: a stamp-only --stale must not turn a whole store partitioned
    from kvevaluator.measure import reingest_stores as RS
    sch = os.path.relpath(schema_path("ldbc"), EXP)
    man = {"format": "dgraph", "schema": {"path": sch}, "compression": "none", "index_filter_blocks": "whole"}
    def layout_of(cmd):
        return cmd[cmd.index("--index-filter-blocks") + 1] if cmd and "--index-filter-blocks" in cmd else None
    got = (layout_of(RS.ingest_command(man, "/x")[0]), layout_of(RS.ingest_command({k: v for k, v in man.items() if k != "index_filter_blocks"}, "/x")[0]),
           layout_of(RS.ingest_command(man, "/x", "partitioned")[0]),
           layout_of(RS.ingest_command({**man, "format": "meta", "value_codec": {}}, "/x")[0]))
    check("offline", "a re-ingest keeps the manifest's layout (no field: whole), --to-default-layout's overrides it, "
          "meta's spec path too", got == ("whole", "whole", "partitioned", "whole"), got)
    # the codec and the filter (inputs/contract/store_config.json): a re-ingest takes the codec its NAME says, and a store
    # whose name and manifest disagree, or with another filter, is stale for a size or a time (store_staleness, layout=True)
    from kvevaluator.contract import store_build
    from kvevaluator.measure import store_layout as SLB
    decided_codec, decided_bloom = store_build("compression"), store_build("bloom_bits_per_key")
    def codec_out(cmd):
        return cmd[cmd.index("--compression") + 1], cmd[cmd.index("--out") + 1]
    got = (codec_out(RS.ingest_command({**man, "compression": "none"}, "/x/dgraph")[0]),
           codec_out(RS.ingest_command({**man, "compression": "zstd"}, "/x/dgraph-zstd")[0]),
           codec_out(RS.ingest_command({**man, "compression": "none"}, "/x/dgraph-none")[0]))
    check("offline", "a re-ingest takes the codec the store's name says: none -> the decided one, -zstd and -none kept",
          got == ((decided_codec, "/x/dgraph"), ("zstd", "/x/dgraph"), ("none", "/x/dgraph")) if decided_codec != "none"
          else got[1] == ("zstd", "/x/dgraph"), got)
    old_store = {"compression": "none"}                       # a manifest from before the filter field: 10 bits
    got = (SLB.build_settings_stale("/x/dgraph", {"compression": decided_codec, "bloom_bits_per_key": decided_bloom}),
           SLB.build_settings_stale("/x/dgraph", old_store),
           SLB.build_settings_stale("/x/dgraph-zstd", {"compression": "zstd", "bloom_bits_per_key": decided_bloom}),
           SLB.build_settings_stale("/x/dgraph", {"compression": decided_codec, "bloom_bits_per_key": decided_bloom + 12}))
    check("offline", "a store built as its name says is current; an old manifest (none, no filter field), another "
          "filter are stale", got[0] is None and got[2] is None and got[3] is not None
          and (got[1] is not None) == (decided_codec != "none" or decided_bloom != SLB.BLOOM_BEFORE_THE_FIELD), got)
    # grid_writes.fresh: a record of a store the tool cannot identify (not built, not current) is never fresh
    from kvevaluator.measure import grid_writes as GW
    gw_out = os.path.join(tmp, "gw", "cell.csv")
    os.makedirs(os.path.dirname(gw_out), exist_ok=True)
    open(gw_out, "w").write("x\n")
    saved = (GW.store_identity, GW.kvstats_files, GW.inputs_fingerprint)
    try:
        GW.kvstats_files = lambda *a, **k: []
        GW.inputs_fingerprint = lambda *a, **k: "inputs"
        def gw_fresh(rec_store, ident, write_version=GW.counting.stamp(GW.FORMAT)["write_seam"]):   # a record from before `counting`
            json.dump({"measurement_version": GW.CAPTURE_SHAPE, "write_measurement_version": write_version,
                       "nparam": 2, "inputs": "inputs", "store": rec_store},
                      open(gw_out[:-len(".csv")] + ".json", "w"))
            GW.store_identity = lambda *a, **k: ident
            return GW.fresh("0.1", "cell", gw_out, 2)
        got = (gw_fresh(None, None), gw_fresh(["k", "h", "partitioned"], ["k", "h", "partitioned"]),
               gw_fresh(["k", "h", "whole"], ["k", "h", "partitioned"]),
               gw_fresh(["k", "h", "partitioned"], ["k", "h", "partitioned"], write_version=None))   # before the write version
    finally:
        GW.store_identity, GW.kvstats_files, GW.inputs_fingerprint = saved
    check("offline", "grid_writes: a record of no identifiable store is not fresh; the same store is; another layout is not; "
          "one from before the write counting version is not", got == (False, True, False, False), got)


def rows(path):
    return list(csv.DictReader(open(path)))


# the counts capture grid_latency gates against, and the command that makes it on a tree without one
COUNTS_DEFAULT = Tree().capture("interactive", "0.1").path     # the sf0.1 capture
COUNTS_CELL = "props_condensed_adj_condensed"
COUNTS_HOW = (f"kvcost measure --dataset ldbc=0.1 --family interactive {COUNTS_CELL} --nparam 2 (about 10 s at sf0.1: the reported and the held-out captures), or --counts <a round's "
              f"capture dir> (run/meta-gates.sh's logs dir + /capture/captures/ldbc/sf0.1/interactive/reported)")


# the stores the live part opens, each with the command that makes it
LIVE_STORES = {"dgraph": "run/ingest-cpp-stores.sh --sf 0.1 --formats dgraph"}


def missing_stores(sf="0.1"):
    """[(store name, its dir, the command that makes it)] for each live-part store with no manifest"""
    out = []
    for name, how in sorted(LIVE_STORES.items()):
        d = stores.store_dir("ldbc", sf, name)
        if not os.path.exists(os.path.join(d, "manifest.json")):
            out.append((name, d, how))
    return out


def live(tmp, counts):
    print("live: sf0.1")
    gone = missing_stores()
    check("live", "preflight: the stores the live part opens exist", not gone,
          "; ".join(f"no sf0.1 store '{n}' at {d}; make it: {how}" for n, d, how in gone))
    if gone:
        return
    py = sys.executable
    w, b = os.path.join(tmp, "wall"), os.path.join(tmp, "breakdown")
    cmd = [py, "python/kvevaluator/measure/cpplatency.py", "--sf", "0.1", "--format", "dgraph", "--arms", "dgraph", "--queries",
           "is1", "is3", "ic2", "ic7", "ic9", "--nparam", "2", "--warm", "", "--cold", "", "--steady", "4,2", "--steady-direct-io",
           "--steady-breakdown", b, "--out", w]
    r = subprocess.run(cmd, cwd=EXP, capture_output=True, text=True)
    check("live", "cpplatency --steady-breakdown (expected rc 0)", r.returncode == 0, f"rc {r.returncode}: " + r.stderr[-400:])
    if r.returncode:
        return
    wm, bm = json.load(open(os.path.join(w, "steps.csv.meta.json"))), json.load(open(os.path.join(b, "steps.csv.meta.json")))
    print(f"    binary {wm['provenance'].get('binary_git_sha', '?')[:12]} dirty {wm['provenance'].get('binary_dirty')}")
    check("live", "metas: wall / breakdown, linked", wm["capture_kind"] == "wall" and bm["capture_kind"] == "breakdown"
          and bm["wall_capture"] == recorded_path(os.path.abspath(w), Tree().root)
          and wm["steady_breakdown"] == recorded_path(os.path.abspath(b), Tree().root))
    check("live", "capture_kind agrees with the metas", CP.capture_kind(w) == {"steady": "wall"} and CP.capture_kind(b) == {"steady": "breakdown"})
    wr, br = rows(os.path.join(w, "steps.csv")), rows(os.path.join(b, "steps.csv"))
    check("live", "wall: no timer columns", "kv_call_ns" not in wr[0])
    timers = ("block_read_time", "seek_child_seek_time", "get_from_output_files_time", "find_next_user_entry_time", "io_read_nanos")
    check("live", "wall: the perf-3 timers are 0 (perf 2)", all(float(x[t] or 0) == 0 for x in wr for t in timers))
    check("live", "wall: perf-2 counters present", sum(float(x["block_cache_hit_count"] or 0) for x in wr) > 0)
    check("live", "breakdown: kv_call_ns and the perf-3 timers > 0",
          sum(float(x["kv_call_ns"] or 0) for x in br) > 0 and sum(float(x[t] or 0) for x in br for t in timers) > 0)
    key = lambda x: (x["query"], x["param_idx"], x["step_idx"])
    lanes = (*READ_OPS, *(f"{lane}_bytes" for lane in READ_BYTE_LANES))
    wd, bd = {key(x): x for x in wr}, {key(x): x for x in br}
    diff = [k for k in wd if k not in bd or any(wd[k][c] != bd[k][c] for c in lanes)]
    check("live", "S / G / N, misses and every byte lane equal step by step", not diff and len(wd) == len(bd) > 0,
          f"{len(wd)} step rows compared, {len(diff)} differ")
    passes = rows(os.path.join(b, "steady-passes.csv"))
    # (this store's breakdown pass may be all cache hits, so the device-read check is in the grid part below)
    check("live", "breakdown: one pass, after the measured ones",
          len(passes) == 1 and passes[0]["phase"] == "breakdown"
          and int(passes[0]["pass"]) == len(rows(os.path.join(w, "steady-passes.csv"))), passes)
    check("live", "a reader of walls refuses the pair", refuses(lambda: CP.check_same([w, b], "t")))
    check("live", "a reader of timers refuses the wall, takes the breakdown",
          refuses(lambda: CP.require_breakdown(w, "t")) and not refuses(lambda: CP.require_breakdown(b, "t", need=("perf", "timer"))))
    # grid_latency on one meta cell
    if not os.path.exists(Capture(counts).path_of("counts", COUNTS_CELL)):
        check("live", "grid_latency: the counts capture exists", False,
              f"no {COUNTS_CELL} run in {counts}; make it: {COUNTS_HOW}")
        return
    g = os.path.join(tmp, "grid")
    gcmd = [py, "python/kvcost/latency/grid_latency.py", "--sf", "0.1", "--counts", counts, "--cells", "props_condensed_adj_condensed",
            "--regimes", "steady", "--steady", "10,1", "--steady-breakdown", "--nparam", "2", "--out", g]
    r = subprocess.run(gcmd, cwd=EXP, capture_output=True, text=True)
    check("live", "grid_latency --steady-breakdown (expected rc 0 and STAGE_DONE)", r.returncode == 0 and "STAGE_DONE" in r.stdout,
          f"rc {r.returncode}: " + r.stdout[-600:] + r.stderr[-300:])
    gate = rows(os.path.join(g, "gate.csv")) if os.path.exists(os.path.join(g, "gate.csv")) else []
    per = {}
    for x in gate:
        per.setdefault(x["regime"], [0, 0])[0] += 1
        per[x["regime"]][1] += int(x["equal"])
    gw = rows(os.path.join(g, "interactive", "steady-10mb-p2", "meta-props_condensed_adj_condensed", "steps.csv"))
    gb = rows(os.path.join(g, "interactive", "steady-10mb-breakdown", "meta-props_condensed_adj_condensed", "steps.csv"))
    dev = lambda rs, c: sum(float(x.get(c) or 0) for x in rs if x["step"] != "__plan__")
    check("live", "device reads in both; io_read_nanos and block_read_time 0 in the wall, > 0 in the breakdown",
          dev(gw, "io_bytes_read") > 0 and dev(gb, "io_bytes_read") > 0 and dev(gw, "io_read_nanos") == 0
          and dev(gw, "block_read_time") == 0 and dev(gb, "io_read_nanos") > 0 and dev(gb, "block_read_time") > 0,
          {c: (dev(gw, c), dev(gb, c)) for c in ("io_bytes_read", "io_read_nanos", "block_read_time")})
    check("live", "the gate covers the wall and the breakdown, all equal",
          set(per) == {"steady-10mb-p2", "steady-10mb-breakdown"} and all(n == e and n > 0 for n, e in per.values()), per)
    r = subprocess.run(gcmd, cwd=EXP, capture_output=True, text=True)
    check("live", "a second call resumes both", r.returncode == 0 and r.stdout.count("already measured") == 1, r.stdout[-300:])
    os.rename(os.path.join(g, "interactive", "steady-10mb-breakdown"), os.path.join(g, "interactive", "moved-breakdown"))
    r = subprocess.run(gcmd, cwd=EXP, capture_output=True, text=True)
    # a missing breakdown is not refused (results are marked, not refused): the wall is moved aside and
    # both captures are made again, marked timing_incomplete
    check("live", "a wall without its breakdown is moved aside and re-measured, marked",
          r.returncode == 0 and "timing_incomplete" in (r.stdout + r.stderr)
          and os.path.exists(os.path.join(g, "interactive", "steady-10mb-breakdown", "meta-props_condensed_adj_condensed", "steps.csv"))
          and any(d.startswith("aside-") for d in os.listdir(g)), r.stdout[-300:] + r.stderr[-300:])
    # the timer passes: 2 kvtimer passes (perf 2 + the KV timer), then 1 breakdown pass (perf 3 + the KV timer)
    g2 = os.path.join(tmp, "grid-timers")
    r = subprocess.run([*gcmd[:gcmd.index("--steady")], "--steady", "10,2", "--steady-kvtimer-passes", "2", "--steady-breakdown",
                        "--steady-breakdown-passes", "1", "--nparam", "2", "--out", g2], cwd=EXP, capture_output=True, text=True)
    check("live", "grid_latency --steady-kvtimer-passes 2 --steady-breakdown (expected rc 0 and STAGE_DONE)",
          r.returncode == 0 and "STAGE_DONE" in r.stdout, f"rc {r.returncode}: " + r.stdout[-600:] + r.stderr[-300:])
    per2 = {}
    for x in (rows(os.path.join(g2, "gate.csv")) if os.path.exists(os.path.join(g2, "gate.csv")) else []):
        per2.setdefault(x["regime"], [0, 0])[0] += 1
        per2[x["regime"]][1] += int(x["equal"])
    check("live", "the gate covers the wall, the kvtimer and the breakdown captures, all equal",
          set(per2) == {"steady-10mb-p2", "steady-10mb-kvtimer", "steady-10mb-breakdown"}
          and all(n == e and n > 0 for n, e in per2.values()), per2)
    cap = lambda r: os.path.join(g2, "interactive", r, "meta-props_condensed_adj_condensed")
    gk, gb2 = rows(os.path.join(cap("steady-10mb-kvtimer"), "steps.csv")), rows(os.path.join(cap("steady-10mb-breakdown"), "steps.csv"))
    check("live", "kvtimer: the KV timer on, no PerfContext timer; breakdown: both",
          dev(gk, "kv_call_ns") > 0 and dev(gk, "block_read_time") == 0 and dev(gk, "io_read_nanos") == 0
          and dev(gb2, "kv_call_ns") > 0 and dev(gb2, "block_read_time") > 0,
          {c: (dev(gk, c), dev(gb2, c)) for c in ("kv_call_ns", "block_read_time", "io_read_nanos")})
    phases = lambda r: [x["phase"] for x in rows(os.path.join(cap(r), "steady-passes.csv"))]
    kmeta = json.load(open(os.path.join(cap("steady-10mb-kvtimer"), "steps.csv.meta.json")))
    wmeta = json.load(open(os.path.join(cap("steady-10mb-p2"), "steps.csv.meta.json")))
    check("live", "the pass shape: 2 measured, then 2 kvtimer, then 1 breakdown; the metas name the rule and the kind",
          phases("steady-10mb-p2").count("measured") == 2 and phases("steady-10mb-kvtimer") == ["kvtimer"] * 2
          and phases("steady-10mb-breakdown") == ["breakdown"] and kmeta.get("timer_kind") == "kvtimer"
          and kmeta["regimes"]["steady"]["perf_level"] == 2
          and wmeta["regimes"]["steady"].get("pass_order_rule") == "measured-index",
          (phases("steady-10mb-p2"), phases("steady-10mb-kvtimer"), phases("steady-10mb-breakdown"), kmeta.get("timer_kind")))
    # the fold: the KV timer in every steady pass, the walls in their own -kvtimed dir, the breakdown after
    g3 = os.path.join(tmp, "grid-kv-timed")
    fold = [*gcmd[:gcmd.index("--steady")], "--steady", "10,2", "--steady-kv-timed", "--steady-breakdown",
            "--steady-breakdown-passes", "1", "--nparam", "2", "--out", g3]
    r = subprocess.run(fold, cwd=EXP, capture_output=True, text=True)
    check("live", "grid_latency --steady-kv-timed --steady-breakdown (expected rc 0 and STAGE_DONE)",
          r.returncode == 0 and "STAGE_DONE" in r.stdout, f"rc {r.returncode}: " + r.stdout[-600:] + r.stderr[-300:])
    per3 = {}
    for x in (rows(os.path.join(g3, "gate.csv")) if os.path.exists(os.path.join(g3, "gate.csv")) else []):
        per3.setdefault(x["regime"], [0, 0])[0] += 1
        per3[x["regime"]][1] += int(x["equal"])
    check("live", "the fold's gate covers its wall and breakdown captures (their own dirs), all equal",
          set(per3) == {"steady-10mb-p2-kvtimed", "steady-10mb-breakdown-kvtimed"}
          and all(n == e and n > 0 for n, e in per3.values()), per3)
    cap3 = lambda r: os.path.join(g3, "interactive", r, "meta-props_condensed_adj_condensed")
    gw = rows(os.path.join(cap3("steady-10mb-p2-kvtimed"), "steps.csv"))
    fmeta = json.load(open(os.path.join(cap3("steady-10mb-p2-kvtimed"), "steps.csv.meta.json")))
    calls = sum(dev(gw, c) for c in ("kv_get_calls", "kv_seek_calls", "kv_next_calls"))
    check("live", "the fold's walls: the KV timer on with its calls per row, no PerfContext timer; the meta has the pair",
          dev(gw, "kv_call_ns") > 0 and calls > 0 and dev(gw, "block_read_time") == 0
          and (fmeta.get("kv_timing_block") or {}).get("timer_pair_ns") is not None
          and fmeta.get("kv_timing_by_regime", {}).get("steady") is True,
          {"kv_call_ns": dev(gw, "kv_call_ns"), "calls": calls, "block_read_time": dev(gw, "block_read_time"),
           "kv_timing_block": fmeta.get("kv_timing_block"), "by_regime": fmeta.get("kv_timing_by_regime")})
    ph3 = lambda r: [x["phase"] for x in rows(os.path.join(cap3(r), "steady-passes.csv"))]
    check("live", "the fold's pass shape: warm + 2 measured with the timer, then 1 breakdown; no kvtimer dir",
          ph3("steady-10mb-p2-kvtimed").count("measured") == 2 and ph3("steady-10mb-breakdown-kvtimed") == ["breakdown"]
          and not os.path.exists(os.path.join(g3, "interactive", "steady-10mb-kvtimer-kvtimed")),
          (ph3("steady-10mb-p2-kvtimed"), ph3("steady-10mb-breakdown-kvtimed")))
    from kvevaluator.measure import store_config
    check("live", "the fold's wall meta holds the store config's timed rule", store_config.violations(fmeta)[1] == [],
          store_config.violations(fmeta))
    r = subprocess.run([*fold[:-2], "--steady-kvtimer-passes", "2", "--out", os.path.join(tmp, "grid-kv-both")],
                       cwd=EXP, capture_output=True, text=True)
    check("live", "--steady-kv-timed with --steady-kvtimer-passes refused (expected rc != 0)",
          r.returncode != 0 and "no --steady-kvtimer-passes" in (r.stdout + r.stderr), r.stdout[-300:] + r.stderr[-300:])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the scratch dir (printed)")
    ap.add_argument("--counts", default=COUNTS_DEFAULT,
                    help=f"the meta counts capture the grid part gates against (default "
                         f"output/captures/ldbc/sf0.1/interactive/reported; it needs counts/{COUNTS_CELL}.csv)")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="check-capture-kinds-", dir=os.path.join(str(OUTPUT_DIR)))
    try:
        offline(tmp)
        if not a.offline_only:
            live(tmp, os.path.abspath(a.counts))
    finally:
        if a.keep:
            print(f"scratch kept: {tmp}")
        else:
            shutil.rmtree(tmp)
    print(f"cases: offline {N['offline']}, live {N['live']}; {len(FAIL)} failed" + (f": {FAIL}" if FAIL else ""))
    return 1 if FAIL or N["offline"] == 0 or (not a.offline_only and N["live"] == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
