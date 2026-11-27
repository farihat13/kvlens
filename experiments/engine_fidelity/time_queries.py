#!/usr/bin/env python3
"""Time the complex-kv-reads figure's three queries (kvengines.complex_kv_reads.CELLS: microbench hop1p7_knows p3,
LDBC IC1 p1, LDBC IC3 p1; the LDBC ones on LDBC's hosted param rows) at a cache regime (--regime: the registry's,
inputs/contract/store_config.json): per cell --warmup runs, then --repeats timed runs, one raw row per run (wall in
microseconds), into the figure's run:
<output>/engines/ldbc/sf<SF>/complex-kv-reads/times/<operating point>/<regime>/{live,evaluator}.csv (rows appended; a
run's rows carry its run id; the derive takes a cell's latest run).

    live       the running engines, one at a time (--db): the client's wall around client.execute only; the engine's
               counters are reset before and read after each run, outside the wall (S / G / N and storage_ns, the time
               in the KV store's read calls, from the same dump). --db names engine configurations
               (kvengines.complex_kv_reads.CONFIGURATIONS), each timed on the cells it is measured on; configurations
               of one server are timed after one start (--db dgraph,dgraph-no-index). --operating-point counting
               (measure.py's pinned Dgraph knobs, with the configuration's own: dgraph's equality filters through their
               index, dgraph-no-index's per vertex; the `path` column says which), defaults (each engine's own settings; Dgraph's knobs
               at "default", LocalCache on, rollup running), or equivalent (counting's knobs on a server started with
               ENGINES_SETTING=equivalent, engines/env.sh: one CPU, engine caches off, equal block caches; checked
               before the run, and this client moves itself off the engine's CPU). At the counting knobs a cell's
               timed S / G / N must equal this run's counted row (measure.py --run complex-kv-reads; exit 1 if not). --regime names the server's
               ENGINES_REGIME (what it was started with). At a cold-start regime (cold-bounded, cold) the tool starts
               the server itself before EVERY timed run, through engines/start-server.sh (stop, restore, the restored
               store put out of the OS page cache, launch on one CPU): nothing reaches the pages a live server has
               mapped, so the server must be new. After the start the store is put out of the cache once more (what
               the start itself read). No warm-up; each row's settings say how much of the store was still in the OS
               page cache just before its run (store_cached_mib=<cached>/<files>), and every run's counts are checked. --storage-profile: also the time in the KV store from outside
               the engine (storage_profile.py); it turns kernel.sched_schedstats on for the run (sudo) and back. On a
               CPU whose perf records no call stacks of the kind an engine needs (Intel LBR for NebulaGraph and
               ArangoDB), that engine's KV CPU time is not measured: its rows say so, the walls and the disk wait
               are measured all the same
    evaluator  the C++ evaluator: each db's engine-faithful plan on its C++ engine store and our cheapest plan on the
               format's own store (the configurations whose engine has a format: CKR.evaluator_dbs(); TuGraph has none;
               the cheapest plan is the format's, timed once per engine, on its first configuration: CKR.cheapest_of);
               a run's wall is the __plan__ row's wall_ns rule (cpplatency.plan_wall), one request
               per run on one open server. Each engine plan's S, G and N must be within validate.py's FIT_REL of this
               run's live counts (measure.py --run complex-kv-reads, the configuration's row). --regime: the store's
               settings from the same registry as the live engines' (store_config.regime: warm, the 8 GiB default
               cache; warm-bounded, the figure's: the steady size, the files in the OS page cache, index and filter
               blocks outside the cache; steady: that size with direct reads; cold-bounded and cold: the store opened
               anew before every run, its cache empty and its files out of the OS page cache, no warm-up)

    python experiments/engine_fidelity/time_queries.py live --sf 0.3 --db dgraph --operating-point equivalent --regime warm-bounded --storage-profile
    python experiments/engine_fidelity/time_queries.py live --sf 0.3 --db tugraph --operating-point equivalent --regime cold-bounded --storage-profile
    python experiments/engine_fidelity/time_queries.py evaluator --sf 0.3 --regime warm-bounded
"""
import argparse
import csv
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import measure as M
from kvengines import complex_kv_reads as CKR
from kvevaluator.measure import store_config
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT

CELLS = list(CKR.CELLS)             # the figure's cells: (query set, query, param)
LDBC = "interactive"                # the LDBC cells' query set: they read LDBC's hosted param rows
DBS = CKR.DBS
# evaluator mode's databases: those with an engine format on our evaluator (TuGraph has none: no evaluator arm)
EVALUATOR_DBS = CKR.evaluator_dbs()
COLUMNS = ["run_id", "utc", "host", "mode", "db", "workload", "query", "param", "param_source", "path", "operating_point", "store",
           "run_idx", "phase", "wall_us", "rows", "S", "G", "N", "storage_ns", "cpu", "engine_cpu_ns", "kv_cpu_ns", "io_wait_ns", "kv_total_ns",
           "perf_samples", "perf_kv_samples", "perf_engine_samples", "perf_kv_any_samples", "settings", "build", "code"]


def code_state():
    """this checkout's commit and whether its tracked files are edited"""
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True, check=True).stdout.strip()
    return f"{sha[:10]}{' dirty' if dirty else ''}"


def engine_build(db):
    """the engine binary's provenance file (engines/<db>/bin/*.provenance or build.json), as one string"""
    d = ROOT / "engines" / M._ENGINE_DIR[CKR.engine(db)] / "bin"
    found = sorted(p for p in d.glob("*") if p.suffix in (".json", ".txt", ".provenance")) if d.is_dir() else []
    return ";".join(f"{p.name}={p.read_text().strip()[:200]}" for p in found) or "no provenance file"


def query_of(workload, sf, qname, db, pidx):
    """(query text, bind params, param source): measure.py's own for the cell"""
    from kvengines import live_workloads as W
    qtext = W.query_path(workload, CKR.engine(db), qname).read_text()
    return qtext, W.db_params(workload, sf, qname, CKR.engine(db), pidx, qtext), CKR.PARAM_SOURCE[workload]


def dgraph_point(prof, point, db="dgraph"):
    """pin Dgraph's operating point: counting = measure.py's (LocalCache off, per_vertex inequality path, deterministic
    sort, rollup paused) with configuration db's knobs (CKR.knobs: its equality filter path); defaults = the server's
    own (filter paths default, LocalCache on, deterministic sort off, rollup running)"""
    import requests
    from kvengines.cost_eval.measured import measure as CE
    if point in ("counting", "equivalent"):
        CE.DGRAPH_KNOBS.update(typefilterpath="index", detsort="enable", **CKR.knobs(db))
        return CE.pin_dgraph_config(prof, "off", "per_vertex")
    for knob in ("eqfilterpath", "typefilterpath", "ineqfilterpath"):
        prof.dgraph_knob_set(knob, "default")
    prof.dgraph_localcache_set("enable")
    requests.put(f"{prof.dgraph_url}/detsort/disable", timeout=10).raise_for_status()
    prof.dgraph_rollup_resume()
    return {"filter_paths": "default", "localcache": "enable", "detsort": "disable", "rollup": "running"}


def path_of(db, point):
    """the `path` column of a configuration's live rows: Dgraph's equality-filter path (CKR.knobs: index for dgraph,
    per_vertex for dgraph-no-index; default at the defaults point), "-" for an engine with no such knob"""
    if CKR.engine(db) != "dgraph":
        return "-"
    return "default" if point == "defaults" else CKR.knobs(db)["eqfilterpath"]


def own_engine_plan(qname, db, path, args):
    """the figure's engine plans outside the engine-fidelity check's plan set: Q1 on all three engines, Dgraph IC1
    per_vertex; None for any other cell (its plan is the engine-fidelity check's: live_workloads.engine_plan)"""
    from kvevaluator.plans.ir import bind
    from kvevaluator.plans.plan import Plan
    from kvevaluator.workloads.interactive.queries.ic1 import DGRAPH_ENGINE_BY_EQ_PATH
    from kvevaluator.workloads.microbench.queries.hop import COMPLEX_KV_READS_ENGINE
    if (qname, db, path) == ("ic1", "dgraph", "per_vertex"):
        return DGRAPH_ENGINE_BY_EQ_PATH["per_vertex"](args)
    builder = COMPLEX_KV_READS_ENGINE.get(qname, {}).get(db)
    if builder is None:
        return None
    raw = builder(args)
    return bind(raw, args) if isinstance(raw, Plan) else raw


def live_counts(sf, workload, qname, db, pidx):
    """the live engine's S / G / N for a cell, from this run's counts: measure.py --run complex-kv-reads' row of the
    configuration"""
    f = CKR.counts_file(Tree(), sf, workload)
    if not f.exists():
        raise SystemExit(f"{f}: not there; run measure.py --run {CKR.RUN} --sf {sf} first (the live counts of this load: "
                         f"`just results {CKR.RUN}` makes them)")
    with open(f) as fh:
        r = [x for x in csv.DictReader(fh) if (x["db"], x["query"], x["param_idx"], x["status"]) == (db, qname, str(pidx), "ok")]
    if len(r) != 1:
        raise SystemExit(f"{f}: {len(r)} ok rows for {db} {qname} p{pidx}, want 1")
    return {k: int(float(r[0][k])) for k in "SGN"}


def settings_of(db, point, regime, sf):
    """the engine settings engines/env.sh declares for db at an operating point, regime and SF (what a server started
    with ENGINES_SETTING, ENGINES_REGIME and SF gets; counting and defaults: the file's own point). A configuration
    that is not its engine's first (CKR.CONFIGURATIONS) is named: engines/*.sh --db <it> holds its settings"""
    named = [] if CKR.engine(db) == db else [f"configuration={db}"]
    keys = {"dgraph": ("DGRAPH_LOCALCACHE", "DGRAPH_CACHE_MB", "DGRAPH_CACHE_PERCENTAGE"),
            "nebula": ("NEBULA_CACHE_MB",), "arangodb": ("ARANGODB_CACHE_MB",),
            "tugraph": ()}[CKR.engine(db)] + ("ENGINES_SETTING", "ENGINES_REGIME")       # TuGraph: LMDB has no block cache
    env = dict(os.environ, ENGINES_SETTING="equivalent" if point == "equivalent" else "counting",
               ENGINES_REGIME=regime if point == "equivalent" else "warm", SF=str(sf))
    out = subprocess.run(["bash", "-c", f"source engines/env.sh > /dev/null && for k in {' '.join(keys)}; do echo \"$k=${{!k}}\"; done"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    return ";".join(named + out.split())


def write_rows(out, name, rows):
    path = out / f"{name}.csv"
    new = not path.exists()
    if not new:
        with open(path) as f:
            head = f.readline().strip().split(",")
        if head != COLUMNS:
            sys.exit(f"{path}: its columns are an older set; move it aside first")
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def timed(fn, warmup, repeats, before=None, after=None):
    """[(phase, run index, wall_us, result, after())]: warm-up runs then timed runs, each wall around fn() alone;
    before() and after() (a counter reset and snapshot) run outside the wall"""
    got = []
    for phase, n in (("warmup", warmup), ("timed", repeats)):
        for i in range(n):
            if before:
                before()
            t0 = time.perf_counter_ns()
            res = fn()
            wall = (time.perf_counter_ns() - t0) / 1000.0
            got.append((phase, i, wall, res, after() if after else None))
    return got


def check_equivalent(db):
    """the equivalent setting is live: every process of db is pinned to one CPU (ENGINES_CPU); returns that CPU, after
    moving this client (and what it starts) off it. Refuses when the engine is not on one CPU."""
    names = {"dgraph": ("dgraph",), "nebula": ("nebula-metad", "nebula-graphd", "nebula-storaged"),
             "arangodb": ("arangod",), "tugraph": ("lgraph_server",)}[CKR.engine(db)]
    allowed = {}
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
                if comm.startswith(names):
                    allowed[f"{comm}:{pid}"] = sorted(os.sched_getaffinity(int(pid)))
            except (OSError, ProcessLookupError):
                continue
    cpus = {tuple(v) for v in allowed.values()}
    if not allowed or len(cpus) != 1 or len(next(iter(cpus))) != 1:
        sys.exit(f"equivalent: {db}'s processes are not all on one CPU: {allowed} (start it with ENGINES_SETTING=equivalent)")
    cpu = next(iter(cpus))[0]
    mine = os.sched_getaffinity(0)
    if cpu in mine:
        if mine == {cpu}:
            sys.exit(f"equivalent: this client may run only on the engine's CPU {cpu}")
        os.sched_setaffinity(0, mine - {cpu})
        print(f"  this client moved off the engine's CPU {cpu} ({len(mine) - 1} CPUs left)", flush=True)
    return cpu


def cmd_live(a, out, run_id, base):
    import storage_profile as SP
    if a.storage_profile and a.operating_point != "equivalent":
        sys.exit("--storage-profile needs --operating-point equivalent (one engine CPU)")
    rows, path = [], out / "live.csv"

    def keep(cell_rows):
        """a cell's rows go to the file when the cell is done: a start that fails later loses its own cell alone"""
        rows.extend(cell_rows)
        write_rows(out, "live", cell_rows)

    with SP.schedstats(a.storage_profile):       # block-I/O wait per thread needs kernel.sched_schedstats=1
        differ = live_rows_of(a, out, run_id, base, SP, keep)
    for d in differ:
        print(f"  COUNTS DIFFER from the counted row {d}")
    print(f"live: {len(rows)} rows ({sum(r['phase'] == 'timed' for r in rows)} timed), {len(differ)} cells whose counts "
          f"differ from this run's counted rows -> {path}: {'PASS' if rows and not differ else 'FAIL'}")
    return 0 if rows and not differ else 1


def engines_env(regime):
    """the environment of an engines/*.sh call at the equivalent setting and a regime"""
    return dict(os.environ, ENGINES_SETTING="equivalent", ENGINES_REGIME=regime)


def start_cold(db, a, out, run_id):
    """one cold start of db's server at the run's regime, through the engines' own start: it stops the server that
    runs, restores the backup, drops the OS page cache and launches (engines/start-server.sh; at a cold-start regime
    its checks read no rows). Its output goes to the run's starts/ log"""
    srv = CKR.server(db)
    log = out / "starts" / f"{run_id}-{srv}.log"
    log.parent.mkdir(exist_ok=True)
    with open(log, "a") as f:
        rc = subprocess.run(["bash", "engines/start-server.sh", "--db", srv, "--sf", str(a.sf)], cwd=ROOT,
                            env=engines_env(a.regime), stdout=f, stderr=subprocess.STDOUT).returncode
    if rc:
        sys.exit(f"cold start: engines/start-server.sh --db {srv} failed (rc {rc}); its output: {log}")


def store_dir(db, sf):
    """the dir of db's running KV store: the engines' own (env.sh engine_store_dir)"""
    return subprocess.run(["bash", "-c", f"source engines/env.sh > /dev/null && engine_store_dir {CKR.server(db)}"], cwd=ROOT,
                          env=dict(os.environ, SF=str(sf)), capture_output=True, text=True, check=True).stdout.strip()


def evict_store(db, sf):
    """put the running server's store out of the OS page cache once more, after its start: what the start itself read
    (ArangoDB reads about 40% of its tables, NebulaGraph its logs) leaves the cache, by the engines' own step
    (lifecycle-common.sh lc_evict_files). The pages a server holds mapped stay (Badger's table indexes, LMDB's meta
    pages): store_cached then says what is left"""
    subprocess.run(["bash", "-c", "source engines/env.sh > /dev/null && source engines/lifecycle-common.sh && "
                    f"lc_evict_files \"$(engine_store_dir {CKR.server(db)})\""], cwd=ROOT, env=dict(os.environ, SF=str(sf)), check=True)


def store_cached(db, sf):
    """(MiB of db's running store in the OS page cache, MiB its files hold on disk): asked through a mapping that
    touches no page (mincore), so the asking reads nothing. Before a cold run, after evict_store, it is what the
    server holds mapped and what it wrote since (Badger's memtable file, a log)"""
    import ctypes
    import mmap
    d = store_dir(db, sf)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    cached = total = 0
    for f in sorted(x for x in Path(d).rglob("*") if x.is_file()):
        try:                                   # a file the server removed meanwhile (a log it rolled) is no store file
            st = f.stat()
            fd = os.open(f, os.O_RDONLY)
        except FileNotFoundError:
            continue
        size = st.st_size
        if not size:
            os.close(fd)
            continue
        pages = -(-size // mmap.PAGESIZE)
        try:
            addr = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
            if addr in (None, ctypes.c_void_p(-1).value):
                raise OSError(ctypes.get_errno(), f"mmap {f}")
            vec = ctypes.create_string_buffer(pages)
            if libc.mincore(addr, size, vec):
                raise OSError(ctypes.get_errno(), f"mincore {f}")
            libc.munmap(addr, size)
        finally:
            os.close(fd)
        cached += sum(x & 1 for x in vec.raw) * mmap.PAGESIZE
        total += min(size, st.st_blocks * 512)         # what the file holds on disk: a sparse file's holes are not store
    if not total:
        sys.exit(f"store_cached: no store files under {d}")
    return cached / 2**20, total / 2**20


def time_cell(a, db, cell, client, prof, cpu, stacks, out, run_id, base, SP, warmup, repeats, run0=0):
    """one (db, cell) on the server that runs: warmup warm-up runs then repeats timed runs, numbered from run0; its raw
    rows. At a cold-start regime the row's settings also say how much of the store was in the OS page cache just
    before the run"""
    from kvengines.kv import KVProfiler
    workload, qname, pidx = cell
    dbe = CKR.engine(db)
    reset, snap = M.counters(prof, db)
    path = path_of(db, a.operating_point)
    cfg = dgraph_point(prof, a.operating_point, db) if dbe == "dgraph" else {}
    qtext, params, src = query_of(workload, a.sf, qname, db, pidx)
    probe = []                     # per run: (t0, t1, sched before, sched after), monotonic ns
    clock = SP.SchedClock(SP.engine_pids(dbe)) if a.storage_profile else None
    cached = None
    if a.cold_start:               # the start's own reads leave the page cache; what stays cached is in the row
        evict_store(db, a.sf)
        cached = "store_cached_mib={:.1f}/{:.0f}".format(*store_cached(db, a.sf))

    def before(r=reset):
        r()
        if a.storage_profile:
            probe.append([time.clock_gettime_ns(time.CLOCK_MONOTONIC), None, clock.read(), None])

    def after(s=snap):
        if a.storage_profile:
            probe[-1][1] = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            probe[-1][3] = clock.read()
        return s()

    perf = None
    if stacks:
        (out / "perf").mkdir(exist_ok=True)
        tag = f"-r{run0}" if a.cold_start else ""       # a cold-start regime: one server, so one perf file, per run
        perf = SP.PerfSession(dbe, cpu, a.perf_freq, out / "perf" / f"{run_id}-{db}-{qname}-{src}-{path}{tag}.data")
    try:
        runs = timed(lambda c=client, q=qtext, p=params: c.execute(q, p).raw, warmup, repeats, before=before, after=after)
    finally:
        if perf:
            perf.stop()
        if clock:
            clock.stop()
    extra_cols = [{} for _ in runs]
    if clock:                      # the on-CPU time and the disk wait; with call stacks, the KV share
        counts = (SP.attribute(perf.samples(dbe), [(t0, t1) for t0, t1, _, _ in probe]) if perf
                  else [None] * len(probe))
        for i, ((t0, t1, b, e), got) in enumerate(zip(probe, counts)):
            on_cpu = e["se.sum_exec_runtime"] - b["se.sum_exec_runtime"]
            extra_cols[i] = {"engine_cpu_ns": on_cpu, "io_wait_ns": e["iowait_sum"] - b["iowait_sum"]}
            if got is not None:
                n, k, eng, any_kv = got
                kv_cpu = round(on_cpu * k / n) if n else ""
                extra_cols[i].update({"kv_cpu_ns": kv_cpu, "kv_total_ns": kv_cpu + extra_cols[i]["io_wait_ns"] if n else "",
                                      "perf_samples": n, "perf_kv_samples": k, "perf_engine_samples": eng,
                                      "perf_kv_any_samples": any_kv})
    n_rows = len(M.live_rows(db, runs[-1][3], qtext))
    settings = ";".join([settings_of(db, a.operating_point, a.regime, a.sf)] + [f"{k}={v}" for k, v in sorted(cfg.items())]
                        + ([] if stacks or not a.storage_profile else ["kv_cpu=not measured (no call stacks on this CPU)"])
                        + ([cached] if cached else []))
    rows = []
    for (phase, i, us, _, stats), extra in zip(runs, extra_cols):
        norm = KVProfiler._normalize_kvstats(stats)
        rows.append({**base, "mode": "live", "db": db, "workload": workload, "query": qname, "param": pidx,
                     "param_source": src, "path": path, "operating_point": f"{a.operating_point}/{a.regime}" if a.operating_point == "equivalent" else a.operating_point, "store": "live",
                     "run_idx": i + (run0 if phase == "timed" else 0),
                     "phase": phase, "wall_us": us, "rows": n_rows, "settings": settings,
                     "S": norm["seek_count"], "G": norm["get_count"], "N": norm["next_count"],
                     "storage_ns": KVProfiler.storage_read_ns(dbe, stats), "cpu": cpu, **extra,
                     "build": engine_build(db)})
    return rows


def live_rows_of(a, out, run_id, base, SP, keep):
    """keep(the raw rows of a (db, cell, path): warm-up and timed runs) as each cell is done; returns the cells whose
    timed counts are not this run's counted row: at the counting knobs the same execution must be timed that was
    counted. At a cold-start regime every timed run is on a server started anew (start_cold), with no warm-up"""
    differ = []
    for db in a.db:
        dbe = CKR.engine(db)                     # the configuration's database: its processes, profile and counters
        stacks = a.storage_profile and SP.call_stacks_work(dbe)
        if a.storage_profile and not stacks:
            print(f"  {db}: KV CPU time not measured on this CPU: perf records no {SP.CALL_GRAPH[dbe]} call stacks here "
                  f"({SP.call_stacks_why(dbe)}); the walls and the disk wait are measured", flush=True)

        def serve():
            """(this client's CPU check done, the client of the server that runs now, the profiler)"""
            cpu = check_equivalent(db) if a.operating_point == "equivalent" else ""
            clients, prof = M._clients([db])
            return cpu, clients[db], prof

        def leave(client, prof):
            if dbe == "dgraph" and a.operating_point == "defaults":
                dgraph_point(prof, "counting")
            try:
                client.close()
            except Exception:
                pass

        if not a.cold_start:                   # one server for every cell: one client
            cpu, client, prof = serve()
        try:
            for cell in a.cells:
                workload, qname, pidx = cell
                if not CKR.measured_on(db, qname):
                    continue                       # a configuration is timed on the cells it is measured on
                if a.cold_start:
                    got = []
                    for i in range(a.repeats):
                        start_cold(db, a, out, run_id)
                        cpu, client, prof = serve()
                        try:
                            got += time_cell(a, db, cell, client, prof, cpu, stacks, out, run_id, base, SP, 0, 1, run0=i)
                        finally:
                            leave(client, prof)
                else:
                    got = time_cell(a, db, cell, client, prof, cpu, stacks, out, run_id, base, SP, a.warmup, a.repeats)
                keep(got)
                path, src = got[-1]["path"], got[-1]["param_source"]
                if a.operating_point != "defaults":      # the counting knobs: the counts are the counted row's
                    want = live_counts(a.sf, workload, qname, db, pidx)
                    for r in got if a.cold_start else got[-1:]:
                        if {k: r[k] for k in "SGN"} != want:
                            differ.append(f"{db} {qname} p{pidx} {path} run {r['run_idx']}: timed {({k: r[k] for k in 'SGN'})}, counted {want}")
                t = [r["wall_us"] for r in got if r["phase"] == "timed"]
                st = [r["storage_ns"] for r in got if r["phase"] == "timed" and r["storage_ns"] is not None]
                io = [r["io_wait_ns"] for r in got if r["phase"] == "timed" and r.get("io_wait_ns") is not None]
                cold = "" if not a.cold_start else ("; cold starts: " + ", ".join(sorted({x for r in got for x in r["settings"].split(";") if x.startswith("store_cached_mib=")}))
                                                    + (f"; disk wait median {statistics.median(io) / 1e6:.2f} ms" if io else ""))
                print(f"  {db:8s} {qname:13s} {src:13s} {path:10s} {a.operating_point:9s} median {statistics.median(t) / 1000:9.2f} ms "
                      f"(min {min(t) / 1000:.2f}, max {max(t) / 1000:.2f}; {len(t)} runs, {got[-1]['rows']} rows); storage median "
                      f"{f'{statistics.median(st) / 1e6:.2f} ms' if st else 'n/a (no ns timers)'}{cold}", flush=True)
        finally:
            if not a.cold_start:
                leave(client, prof)
    return differ


def cmd_evaluator(a, out, run_id, base):
    from validate import _fits                  # the engine-fidelity count rule (FIT_REL), one definition
    from kvengines import live_workloads as W
    from kvevaluator.capture import store_identity
    from kvevaluator.datasets import demo as data
    from kvevaluator.formats import FORMATS
    from kvevaluator.measure import server, stores
    from kvevaluator.measure.cpplatency import plan_wall, steady_run
    from kvevaluator.plans.ir.serialize import plan_to_ir
    # this load's Dgraph uids (measure.py --run complex-kv-reads writes them with its counts)
    uid_of = W.dgraph_uid_map(LDBC, a.sf, CKR.RUN)
    if uid_of is None and "dgraph" in {CKR.engine(db) for db in a.db}:
        raise SystemExit(f"no Dgraph uid map in {W.out_dir(LDBC, a.sf, CKR.RUN)}/dgraph: run measure.py --run {CKR.RUN} "
                         f"--sf {a.sf} first")
    # one measured run per request: the cache as the earlier runs left it; at a cold-start regime the store is opened
    # anew before it, its cache empty and its files out of the OS page cache (the evaluator's cold: executor.cc make_cold)
    run = dict(steady_run(a.perf_level), **({"cache": "cold"} if a.cold_start else {}))
    regime = store_config.regime(a.regime, a.sf)
    rows, bad = [], []
    for db in a.db:
        dbe = CKR.engine(db)                     # the configuration's database: its formats and plans
        efmt = store_identity.CPP_ENGINE_FORMATS[dbe]
        # the store's settings at the regime, as the live engines' ENGINES_REGIME: the registry's (store_config.regime)
        opts = {"block_cache_bytes": regime["block_cache_mib"] << 20, "direct_io": regime["direct_io"],
                **({} if regime["index_filter_in_cache"] is None else {"cache_index_and_filter_blocks": regime["index_filter_in_cache"]})}
        # the engine's plan on its engine store; the cheapest plan (the format's) on the engine's first configuration only
        arms = [("engine", efmt, server.CppServer(stores.ensure_store("ldbc", a.sf, efmt, chunk=None), efmt,
                                                  **opts).__enter__())]
        if CKR.cheapest_of(db) == db:
            arms.append(("cheapest", dbe, server.CppServer(stores.ensure_store("ldbc", a.sf, dbe, chunk=None), dbe,
                                                           **opts).__enter__()))
        try:
            for workload, qname, pidx in a.cells:
                if not CKR.measured_on(db, qname):
                    continue                        # a configuration is timed on the cells it is measured on
                args = W.plan_args(workload, a.sf, qname, pidx)
                src = CKR.PARAM_SOURCE[workload]
                for arm, fmt, srv in arms:
                    live = None                     # an engine plan's counts are checked against this run's live counts
                    if arm == "engine":
                        plan = own_engine_plan(qname, dbe, path_of(db, "counting"), args)
                        steps = W.with_uid_map(plan if plan is not None else W.engine_plan(workload, qname, dbe, args),
                                               uid_of if dbe == "dgraph" else None)
                        live = live_counts(a.sf, workload, qname, db, pidx)
                    else:
                        steps = W.optimal_plan(workload, qname, dbe, FORMATS[dbe](data.schema(a.sf)).schema, args, a.sf)
                    ir = plan_to_ir(steps, name=f"{qname}-{db}-{arm}")
                    got = []
                    for phase, n in (("warmup", a.warmup), ("timed", a.repeats)):
                        for i in range(n):
                            resp = srv.run(ir, {}, run=run)
                            if resp.get("error"):
                                raise SystemExit(f"{qname} {db} {arm}: {resp['error']}")
                            got.append((phase, i, plan_wall(resp) / 1000.0, resp))
                    last = got[-1][3]
                    cnt = {k: sum(s[k] for s in last["steps"]) for k in "SGN"}
                    fit = None
                    if live is not None:            # validate.py's rule: each of S, G, N within FIT_REL of live
                        fit = "exact" if cnt == live else "fit" if all(_fits(cnt[k], live[k]) for k in "SGN") else "NO FIT"
                        if fit == "NO FIT":
                            bad.append((db, qname, arm, cnt, live))
                    opts = json.dumps((last.get("run") or {}).get("store_options"), sort_keys=True)
                    n_rows = len(last.get("rows") or [])
                    for phase, i, us, _ in got:
                        rows.append({**base, "mode": "evaluator", "db": db, "workload": workload, "query": qname,
                                     "param": pidx, "param_source": src, "path": arm, "operating_point": a.regime, "store": fmt, "run_idx": i,
                                     "phase": phase, "wall_us": us, "rows": n_rows, **cnt,
                                     "settings": f"store_options={opts};perf_level={a.perf_level}", "build": base["build"]})
                    t = [us for ph, _, us, _ in got if ph == "timed"]
                    print(f"  {db:8s} {qname:13s} {arm:8s} on {fmt:16s} median {statistics.median(t) / 1000:9.3f} ms "
                          f"(min {min(t) / 1000:.3f}, max {max(t) / 1000:.3f}); S {cnt['S']:,} G {cnt['G']:,} N {cnt['N']:,}"
                          f"{'' if live is None else f'; live {live}: {fit}'}", flush=True)
        finally:
            for _, _, srv in arms:
                srv.close()
    path = write_rows(out, "evaluator", rows)
    for b in bad:
        print(f"  COUNT MISMATCH {b}")
    print(f"evaluator: {len(rows)} rows ({sum(r['phase'] == 'timed' for r in rows)} timed), {len(bad)} count "
          f"mismatches -> {path}: {'PASS' if rows and not bad else 'FAIL'}")
    return 1 if bad or not rows else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=["live", "evaluator"])
    ap.add_argument("--sf", default=CKR.SCALE.split("=")[1])
    ap.add_argument("--db", default="all", help=f"all | a comma list of {', '.join(DBS)} (evaluator: all is "
                                                f"{', '.join(EVALUATOR_DBS)}, the databases with an engine format)")
    ap.add_argument("--operating-point", choices=["counting", "defaults", "equivalent"], default="counting",
                    help="live: the servers' operating point (the evaluator's rows are equivalent's)")
    ap.add_argument("--storage-profile", action="store_true",
                    help="live, equivalent: also the KV-store time from outside the engine (storage_profile.py: perf "
                         "call stacks on the engine's CPU + block-I/O wait per thread)")
    ap.add_argument("--regime", choices=store_config.regimes(), default="warm",
                    help="the cache regime, one of the registry's (inputs/contract/store_config.json): the evaluator's "
                         "store settings; live, what the servers were started with (ENGINES_REGIME, engines/env.sh). "
                         "At a cold-start regime (cold, cold-bounded) there is no warm-up, and live starts the server "
                         "anew before every timed run")
    ap.add_argument("--perf-freq", type=int, default=10000, help="--storage-profile: perf samples per second")
    ap.add_argument("--warmup", type=int, default=3, help="warm-up runs per cell (a cold-start regime: none)")
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--perf-level", type=int, default=2, help="evaluator: the RocksDB perf level (warm: 0-2)")
    ap.add_argument("--queries", default="all", help="all | a comma list of hop1p7_knows, ic1, ic3")
    a = ap.parse_args(argv)
    a.db = list(DBS if a.mode == "live" else EVALUATOR_DBS) if a.db == "all" else a.db.split(",")
    if set(a.db) - set(DBS):
        sys.exit(f"--db {', '.join(sorted(set(a.db) - set(DBS)))}: not a configuration of {CKR.RUN} ({', '.join(DBS)})")
    if a.mode == "evaluator" and set(a.db) - set(EVALUATOR_DBS):
        sys.exit(f"evaluator: {', '.join(sorted(set(a.db) - set(EVALUATOR_DBS)))} has no engine format on our evaluator")
    a.cells = [c for c in CELLS if a.queries == "all" or c[1] in a.queries.split(",")]
    point = "equivalent" if a.mode == "evaluator" else a.operating_point
    if point != "equivalent" and a.regime != "warm":
        sys.exit(f"--regime {a.regime} needs --operating-point equivalent (engines/env.sh: a regime is a setting of it)")
    a.cold_start = store_config.regime(a.regime, a.sf)["cold_start"]
    if a.cold_start:
        a.warmup = 0
    out = CKR.times_dir(Tree(), a.sf, point, a.regime)
    out.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    build = ""
    if a.mode == "evaluator":
        from kvevaluator.capture.binary import binary_provenance
        v = binary_provenance()
        build = f"{v['binary_git_sha'][:10]}{' dirty' if v['binary_dirty'] else ''}"
    base = {"run_id": run_id, "utc": run_id, "host": socket.gethostname().split(".")[0], "code": code_state(),
            "build": build}
    print(f"time_queries {a.mode}: code {base['code']}{f', evaluator binary {build}' if build else ''}; dbs {a.db}; "
          f"{a.warmup} warm-up + {a.repeats} timed runs per cell; run {run_id}; into {out}", flush=True)
    return (cmd_live if a.mode == "live" else cmd_evaluator)(a, out, run_id, base)


if __name__ == "__main__":
    sys.exit(main())
