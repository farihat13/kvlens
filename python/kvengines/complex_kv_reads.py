"""The complex-kv-reads figure (three multi-step reads on the live databases): its cells, the run they are measured
in, and the rule that makes a timed cell. One place for what the chain (run/reproduce.py), the tools that measure
(experiments/engine_fidelity/measure.py, time_queries.py, live_answers.py), the derive
(paper/derive/complex_kv_reads.py) and the compare (experiments/engine_fidelity/compare_complex_kv_reads.py) must agree
on.

The run is one dir of an output tree, <tree>/engines/ldbc/sf<SF>/complex-kv-reads/ (run_dir), all of it measured on
one load of the engine configurations (DBS); a row's `db` is a configuration, its `query` a query or a query-text
variant (QUERY_VARIANTS):
    interactive/live_kvstats.csv         IC1 at its claim params, IC3 at its drawn param, ic1-edge-list at IC1's drawn
                                         param: live counts (measure.py --run)
    interactive/<db>/<query>/p<N>/       each cell's raw counter dump; interactive/dgraph/uids_*.csv: the load's uids
    microbench/live_kvstats.csv          hop1p7_knows at every param
    times/equivalent/<regime>/           live.csv and evaluator.csv, one raw row per run (time_queries.py), per
                                         cache regime the run was timed at: the figure's (REGIME) and, when the
                                         optional chain ran, its options (REGIME_OPTIONS); perf/: perf's raw samples;
                                         starts/ (a cold-start regime): the output of each server start

A timed cell is (db, query, param_source, arm): arm is "live" (time_queries.py live) or "engine" / "cheapest"
(time_queries.py evaluator). The cheapest plan is the format's: it is timed once per engine, on the engine's first
configuration (cheapest_of). A cell is the timed runs of its latest run_id.

Time in the KV store = KV CPU (perf samples the KV store owns x the engine's on-CPU time; storage_profile.py) + disk wait.
A cell whose runs carry under POOL_BELOW perf samples each (Q1, a few ms) takes its KV share pooled over its runs. A
cell timed without perf's call stacks (a CPU that cannot record them) has no KV time: summary's kv_* are None.
"""
import csv
import statistics

SCALE = "ldbc=0.3"          # the figure's dataset and scale (the paper's; kvevaluator.sizes form)
RUN = "complex-kv-reads"    # the run's name below <tree>/engines/ldbc/sf<SF>/
# The cache regime the figure's times are measured at, and the regimes a run may be timed at beside it, by the suffix
# of the figure that draws them next to the figure's. The names and what each sets are the registry's
# (inputs/contract/store_config.json; kvevaluator.measure.store_config.regime): warm-bounded is one engine CPU's
# server after warm-up, its block cache at the steady size and its files in the OS page cache; cold-bounded is that
# server started anew before every run, nothing of its store in memory.
REGIME = "warm-bounded"
REGIME_OPTIONS = {"-cold": "cold-bounded"}
# The engine configurations the chain measures: a database with the settings it is loaded and run with. A
# configuration's name is the `db` of every row, per-cell dir, raw dump and answer of the run; engine(db) is its
# database, for everything keyed by the database (client, counters, query texts, params, parsers, profile). The
# settings' values are not here: engines/*.sh --db <configuration> knows them. In the paper's order.
# TuGraph (v4.5.2, on LMDB) has no format on our evaluator: it is in the live counts and times only (evaluator_dbs).
# Fields beyond engine and what, each only where it is not the default:
#   knobs      server knobs Python pins before the configuration's cells (Dgraph's filter paths: time_queries.py
#              dgraph_point and measure.py read them through knobs()); a load setting stays in engines/<engine>/
#   server     the key engines/*.sh take to load and start it (default: the configuration's own name); configurations
#              of one server are up together (rounds), and are timed after one start
#   queries    the cells it is measured on (default: every cell); a panel whose query it is not measured on shows
#   elsewhere  that configuration's bar instead
CONFIGURATIONS = {
    "dgraph": {"engine": "dgraph", "what": "Dgraph, its equality filters through their index",
               "knobs": {"eqfilterpath": "index"}},
    "dgraph-no-index": {"engine": "dgraph", "server": "dgraph", "queries": ("ic1",), "elsewhere": "dgraph",
                        "knobs": {"eqfilterpath": "per_vertex"},
                        "what": "Dgraph with its equality filters reading the property of every candidate, not its index"},
    "nebula": {"engine": "nebula", "what": "NebulaGraph"},
    "arangodb": {"engine": "arangodb", "what": "ArangoDB"},
    "tugraph": {"engine": "tugraph", "what": "detach_property off: a vertex's properties in its packed record"},
    "tugraph-detached": {"engine": "tugraph",
                         "what": "detach_property on: properties in a table of their own, each read by a GET"},
}
DBS = tuple(CONFIGURATIONS)
# Query-text variants: the same server and settings, another text. Its name is <query>-<variant>; its file is the base
# query's stem plus -<variant> (engines/queries/ldbc/nebulagraph/interactive-complex-1-edge-list.gql). Counted and
# answer-checked at the base query's drawn param, not timed
QUERY_VARIANTS = {"ic1-edge-list": {"query": "ic1", "db": "nebula",
                                    "what": "IC1 binding the walk's edge list, not its whole path"}}
# the configurations the paper's figure, its number keys and its claims are over (the paper's choice: Dgraph without
# its first-name index on IC1, and Dgraph elsewhere; TuGraph's detach_property off)
FIGURE_DBS = ("dgraph-no-index", "nebula", "arangodb", "tugraph")
# the draw's options beside the default (FIGURE_DBS), each drawn when the run holds its configurations: the name's
# suffix -> the configurations, in order. -with-cheapest is the evaluator's figure (the shown configurations with an
# engine format), not a set here
SHOW_OPTIONS = {"-tugraph-detached": ("dgraph-no-index", "nebula", "arangodb", "tugraph-detached"),
                "-without-tugraph": ("dgraph-no-index", "nebula", "arangodb")}
# the derive's IC1 claim with each query-text variant in place of its base text (READS_MOST["ic1"] still reads the most)
CLAIM_VARIANTS = ("ic1-edge-list",)
# the configurations a run may lack: the paper's run (paper-results/) predates TuGraph. The derive draws the run's own
# and says which it lacks; the compare names one the paper's run lacks as new
MAY_LACK = ("tugraph", "tugraph-detached")


def figure_configurations():
    """the configurations the paper's figure reads, in DBS order: FIGURE_DBS and the configuration each shows on the
    panels it is not measured on (its `elsewhere`). `just results complex-kv-reads` measures these; the others only the
    draw's options show (SHOW_OPTIONS), and `just results complex-kv-reads-other-configurations` adds them to the run"""
    want = set(FIGURE_DBS) | {CONFIGURATIONS[db]["elsewhere"] for db in FIGURE_DBS if "elsewhere" in CONFIGURATIONS[db]}
    return tuple(db for db in DBS if db in want)


def other_configurations():
    """the configurations the paper's figure does not read (DBS without figure_configurations), in DBS order"""
    return tuple(db for db in DBS if db not in figure_configurations())


def engine(db):
    """the database of a configuration (a name that is no configuration is a database: the engine-fidelity check's)"""
    return CONFIGURATIONS[db]["engine"] if db in CONFIGURATIONS else db


def server(db):
    """the key engines/*.sh take to load and start a configuration"""
    return CONFIGURATIONS.get(db, {}).get("server", db)


def knobs(db):
    """the server knobs Python pins for a configuration ({} when it has none)"""
    return dict(CONFIGURATIONS.get(db, {}).get("knobs", {}))


def servers(dbs=DBS):
    """{server: its configurations}, in DBS order"""
    out = {}
    for db in dbs:
        out.setdefault(server(db), []).append(db)
    return {k: tuple(v) for k, v in out.items()}


def rounds(dbs=DBS):
    """the servers in rounds that can be up together: at most one server per engine (two servers of one engine share
    its ports), each round in DBS order: (dgraph, nebula, arangodb, tugraph), then (tugraph-detached,)"""
    out = []
    for srv in servers(dbs):
        free = next((r for r in out if engine(srv) not in {engine(x) for x in r}), None)
        if free is None:
            out.append([srv])
        else:
            free.append(srv)
    return tuple(tuple(r) for r in out)


def base_query(q):
    """the query a query-text variant is written for (a query is its own)"""
    return QUERY_VARIANTS[q]["query"] if q in QUERY_VARIANTS else q


def measured_on(db, q):
    """whether configuration db is measured on query (or query-text variant) q"""
    if q in QUERY_VARIANTS:
        return QUERY_VARIANTS[q]["db"] == db
    qs = CONFIGURATIONS.get(db, {}).get("queries")
    return qs is None or q in qs


def shown_as(db, q):
    """the configuration whose cell stands for db's bar in q's panel: db where it is measured on q, else its
    `elsewhere`"""
    return db if measured_on(db, q) else CONFIGURATIONS[db]["elsewhere"]


def cheapest_of(db):
    """the configuration whose evaluator rows hold db's cheapest plan: its engine's first configuration (the cheapest
    plan is the format's, timed once per engine)"""
    return next(x for x in DBS if engine(x) == engine(db))
# the figure's cells, in panel order (Dgraph, ArangoDB, NebulaGraph read the most): (query set, query, the param drawn)
CELLS = (("microbench", "hop1p7_knows", 3), ("interactive", "ic1", 1), ("interactive", "ic3", 1))
# per query: the panel's title and the line that names its parameters, filled from the cell's own capture (the rows
# it returns, the params it asked with)
TITLES = {"hop1p7_knows": "A person's {rows} friends,\n7 properties each",
          "ic1": "Friends within 3 hops\nwith first name {firstName}",
          "ic3": "Friends within 2 hops who\nposted in two countries"}
PARAMS_TEXT = {"hop1p7_knows": "microbench p{param} (a person with {rows} friends)",
               "ic1": "LDBC IC1 p{param}: personId {personId}, firstName {firstName}",
               "ic3": "LDBC IC3, LDBC's hosted parameter row {param}"}
# the figure's claim, per query: the configuration that issues the most KV operations, over FIGURE_DBS
READS_MOST = {"hop1p7_knows": "dgraph", "ic1": "arangodb", "ic3": "nebula"}
# a cell's param_source in the times CSVs: an LDBC query reads LDBC's hosted param row, the microbenchmark its own
PARAM_SOURCE = {"interactive": "hosted", "microbench": "microbench"}
QUERIES = tuple((q, PARAM_SOURCE[w]) for w, q, _ in CELLS)
POOL_BELOW = 200


def evaluator_dbs():
    """the figure's configurations whose engine has a format on our evaluator (time_queries.py evaluator's arms:
    engine, cheapest), in DBS order"""
    from kvevaluator.capture.store_identity import CPP_ENGINE_FORMATS
    return tuple(db for db in DBS if engine(db) in CPP_ENGINE_FORMATS)


def claim_params():
    """IC1's params the figure's claim is checked on (the same database reads the most on every one): measure.py's
    one-per-reply-depth default and the holdout params, the archived captures' 12"""
    from kvengines import live_workloads as W
    return sorted({*W.DEPTH_PARAMS, *W.ldbc_holdout()["params"]})


def run_dir(tree, sf):
    """the figure's run in an output tree (kvevaluator.output_tree.Tree)"""
    return tree.engines("ldbc", sf, RUN)


def check_shape(run):
    """refuse a run dir that stores its IC1 variants in the earlier shape (dgraph-eq-filter-path/, nebula-path-binding/):
    it reads nothing of it, it only says why and how to convert"""
    import sys
    old = [d for d in ("dgraph-eq-filter-path", "nebula-path-binding") if (run / d).is_dir()]
    if old:
        sys.exit(f"complex-kv-reads: {run} stores its IC1 variants in the earlier shape ({', '.join(d + '/' for d in old)}): convert a "
                 f"copy with run/convert-paper-results.py --tree <a copy> --steps 18,19 (paper-results/ itself is converted "
                 f"at its next re-pack)")
    if (run / "times" / "equivalent" / "small-cache").is_dir():
        sys.exit(f"complex-kv-reads: {run} names its times' regime small-cache, the earlier name of {REGIME}: convert a "
                 f"copy with run/convert-paper-results.py --tree <a copy> --steps 19 (paper-results/ itself is converted "
                 f"at its next re-pack)")


def counts_file(tree, sf, workload):
    return run_dir(tree, sf) / workload / "live_kvstats.csv"


def times_dir(tree, sf, point="equivalent", regime=REGIME):
    """time_queries.py's raw rows at an operating point and cache regime (the figure's: equivalent, REGIME)"""
    return run_dir(tree, sf) / "times" / point / regime


def cells(path, mode):
    """{(db, query, param_source, arm): [timed rows of the latest run]} from a live.csv (mode "live") or an
    evaluator.csv (mode "evaluator")"""
    runs = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if r["phase"] != "timed":
                continue
            arm = "live" if mode == "live" else r["path"]
            runs.setdefault((r["db"], r["query"], r["param_source"], arm), {}).setdefault(r["run_id"], []).append(r)
    return {k: v[max(v)] for k, v in runs.items()}


def summary(rows):
    """one cell: runs, median / min / max wall ms; on live rows also the disk wait (io_wait_ms), and with perf's call
    stacks the KV-store ms (kv_ms = KV CPU + disk wait, and its KV CPU part) and whether the KV share is pooled over
    the runs"""
    wall = sorted(float(r["wall_us"]) / 1000 for r in rows)
    out = {"runs": len(rows), "median_ms": statistics.median(wall), "min_ms": wall[0], "max_ms": wall[-1],
           "kv_ms": None, "kv_cpu_ms": None, "io_wait_ms": None, "kv_pooled": None, "run_id": rows[0]["run_id"]}
    if rows[0]["mode"] != "live":
        return out              # the evaluator's rows
    if all(r.get("io_wait_ns") for r in rows):      # the disk wait is measured with or without perf's call stacks
        out["io_wait_ms"] = statistics.median(float(r["io_wait_ns"]) / 1e6 for r in rows)
    if not all(r.get("perf_samples") for r in rows):
        return out              # live rows timed without perf's call stacks: no KV time
    io = out["io_wait_ms"]
    n = [int(r["perf_samples"]) for r in rows]
    pooled = statistics.median(n) < POOL_BELOW
    if pooled:
        share = sum(int(r["perf_kv_samples"]) for r in rows) / max(1, sum(n))
        kv_cpu = share * statistics.median(float(r["engine_cpu_ns"]) / 1e6 for r in rows)
    else:
        kv_cpu = statistics.median(float(r["kv_cpu_ns"]) / 1e6 for r in rows)
    out.update(kv_ms=min(kv_cpu + io, out["median_ms"]), kv_cpu_ms=kv_cpu, io_wait_ms=io, kv_pooled=int(pooled))
    return out
