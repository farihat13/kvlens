#!/usr/bin/env python3
"""Measure the LIVE engines' /kvstats per (query, db, param) -> live_kvstats.csv, the baseline `validate.py` compares
against.

The param files are STRATIFIED: 6 messages per reply depth, depths 0-5, so indices 1,7,13,19,25,31 (the default) give
one anchor per depth. Param 1 alone is the worst choice: its message is a depth-0 Post, so IS4-IS7's reply chase never
runs. Depths 4-5 exceed NebulaGraph's REPLY_OF*0..3 cap and Dgraph's 3-level unroll, so the ENGINES return no row there
and a faithful plan must reproduce that emptiness.

Usage:
  python3 measure.py --sf 0.1                        # every cell, one param per depth
  python3 measure.py --sf 0.1 --param 1,13,31        # explicit params
  python3 measure.py --sf 0.1 --query is6 --db all   # one query
  python3 measure.py --sf 0.1 --append               # merge into the existing CSV
  python3 measure.py --sf 0.3 --run complex-kv-reads --query ic1   # into a figure's own run, not the engine-fidelity check's

Needs all three servers up. `--db all` is the three engines of the engine-fidelity check; the complex-kv-reads figure's
other engine configurations (kvengines.complex_kv_reads.CONFIGURATIONS: tugraph, tugraph-detached; no evaluator
format) are measured when named (`--db tugraph-detached`, or the figure's chain's list). A configuration's rows, raw
dumps and per-cell dirs carry its name; its engine (CKR.engine) picks the client, counters, query text and params.

PINS THE SAME OPERATING POINT AS the engine plan capture, and records it in every row: Dgraph's KV cost depends on its
config (an eq-filter path of index vs per_vertex moves IC2's seeks 17x), so a baseline at one operating point is not
evidence about a plan captured at another.

The write side, --family writes: the live write sweep, the engines' read and write counters per (write primitive, db,
param) -> <output>/engines/ldbc/sf<SF>/write-sweep/write_sweep.csv, the file write_fidelity.py --sweep reads.
  python3 measure.py --family writes --sf 0.3        # the three servers on a clean restore (the writes change the store)
- Texts: engines/queries/writes/<db>/<primitive>.<ext>, one per primitive write_fidelity.PRIMITIVES maps to an mb2 MB
  number (the file is the registry). Params: the evaluator's writes params, the first rows as many as `kvcost measure`
  binds, so a live cell and an evaluator cell are the same anchor; each of the plan's lets (an insert's synthetic id)
  enters as the plain LDBC id inside its vid.
- Binding: a Dgraph upsert takes no variable and a NebulaGraph vid no parameter, so their write texts are `$name`
  templates (the `#` header dropped, then string.Template); ArangoDB's take bind variables (`@name`), as its read texts.
- Order: per engine, the primitives by MB number, except that the edge delete runs before the vertex delete; params in
  order. Every cell starts from the loaded store, so the order changes no count: each insert (a fresh synthetic id, or
  a pair that is not adjacent) is undone right after it is measured, by the delete primitive's text, uncounted (the
  vertex inserts share an id and the edge inserts their pairs; a Dgraph vertex insert needs no undo, as each makes a
  new node). The updates are not undone: they write constants into persons the vertex delete later removes, and that
  delete removes the record and its index entries whatever their values (mb2 ran them on one person too). The edge
  delete runs first because a vertex-delete anchor is an endpoint of an edge-delete pair (sf0.3: person
  2199023259200), whose edges Dgraph's vertex delete would remove; it changes only Dgraph's vertex delete there (one
  edge fewer), a count that grows with degree and is compared by its range.
- Dgraph's writes are measured through KVProfiler.dgraph_measure_write (the rollup drain that turns its delta into
  counted SETs). Exit 1 when a cell errs, when an engine's dump lacks its write counters, or when no cell ran.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import string
import sys
import time
from pathlib import Path
from kvevaluator.paths import ROOT
from kvengines import complex_kv_reads as CKR

_HERE = ROOT / "experiments" / "engine_fidelity" / "measure.py"
_QUERIES = ROOT / "engines" / "queries" / "ldbc"          # the engines' LDBC query texts, one dir per engine
_ENGINE_DIR = {"dgraph": "dgraph", "nebula": "nebulagraph", "arangodb": "arangodb", "tugraph": "tugraph"}   # db -> its dir
# --db all: the engine-fidelity check's engines (each has an evaluator format); TuGraph is measured when named
FIDELITY_DBS = ("arangodb", "dgraph", "nebula")
LIVE_DBS = FIDELITY_DBS + tuple(db for db in CKR.DBS if db not in FIDELITY_DBS)   # every name --db takes

from kvevaluator.measure.report import output_dir
from kvevaluator.workloads.interactive import engine_plans
from kvevaluator.datasets.ldbc.params import param_rows

# Every workload keeps what the counter call returns for free: the counts AND the bytes read.
# data_created: the engine backup's `created` stamp -- which load the counts came from (a reload can
# change what depends on the load, e.g. Dgraph's uids).
COLUMNS = ["sf", "query", "db", "param_idx", "S", "G", "N", "SG",
           "get_bytes", "seek_bytes", "next_bytes", "cache_mode", "ineqpath", "data_created",
           "secs", "status"]
# Non-LDBC workloads also keep the answer size (rows the engine returned), which the evaluator
# comparison checks; LDBC's answers are compared row by row (live_answers.py).
MB_COLUMNS = COLUMNS[:11] + ["rows"] + COLUMNS[11:]

_EXT = {"dgraph": "graphql", "nebula": "gql", "arangodb": "aql", "tugraph": "cypher"}
_KIND = {"ic": "interactive-complex", "is": "interactive-short"}

# The write sweep's columns: the archived sweep's ten (db as write_fidelity.ENGINE names it, mb from
# write_fidelity.PRIMITIVES, param 1-based), then the free ones the read side keeps.
WRITE_COLUMNS = ["db", "mb", "param", "S", "G", "N", "P", "D", "COMMIT", "write_bytes",
                 "query", "get_bytes", "seek_bytes", "next_bytes", "data_created", "secs", "status"]
WRITE_DBS = ("dgraph", "nebula", "arangodb")                   # the sweep's engine order
# an insert is undone, uncounted, by the delete primitive's text; a vertex insert's anchor is its new id. Not on
# Dgraph's vertex inserts: a Dgraph insert makes a new node (a fresh uid) every time, so the two never meet
UNDO = {"w_ins_v_min": "w_del_v_record", "w_ins_v_full": "w_del_v_record",
        "w_ins_e_plain": "w_del_e", "w_ins_e_facet": "w_del_e"}
NO_UNDO = {("dgraph", "w_del_v_record")}
WRITE_COUNTER = {"dgraph": "set_count", "nebula": "put_count", "arangodb": "put_count"}   # a dump without it counted no write


# The operating point the captures were taken at (see any plan.txt header). Overridable for
# a deliberate sweep, but the DEFAULT must match the captures, not the ambient environment.
CACHE_MODE = os.environ.get("COSTEVAL_LOCALCACHE", "off")
INEQ_PATH = os.environ.get("COSTEVAL_INEQFILTERPATH", "per_vertex")


def _clients(dbs):
    """One client per DB this run touches, plus one KVProfiler. Imported lazily: the offline
    consumers of this CSV must not need kvengines or a running server just to read it."""
    from kvengines import connect
    from kvengines.kv import KVProfiler

    make = {"dgraph": lambda: connect("dgraph"),
            "nebula": lambda: connect("nebula"),
            "arangodb": lambda: connect("arangodb", host="127.0.0.1", request_timeout=600),
            "tugraph": lambda: connect("tugraph")}
    return {db: make[CKR.engine(db)]() for db in dbs}, KVProfiler()


def counters(profiler, db):
    """(reset, snapshot): the counter calls of db's engine"""
    return {"dgraph": (profiler.dgraph_kvstats_reset, profiler.dgraph_kvstats_snapshot),
            "nebula": (profiler.nebula_kvstats_reset, profiler.nebula_kvstats_snapshot),
            "arangodb": (profiler.arango_kvstats_reset, profiler.arango_kvstats_snapshot),
            "tugraph": (profiler.tugraph_kvstats_reset, profiler.tugraph_kvstats_snapshot)}[CKR.engine(db)]


def _pin(profiler, cache, ineqpath, db="dgraph"):
    """Re-pin dgraph's knobs via the shared cost_eval helper, so the plan capture and this tool never drift; with the
    knobs of configuration db (complex_kv_reads.CONFIGURATIONS: dgraph's equality filter through its index,
    dgraph-no-index's per vertex)."""
    from kvengines.cost_eval.measured.measure import DGRAPH_KNOBS, pin_dgraph_config
    DGRAPH_KNOBS.update(CKR.knobs(db))
    return pin_dgraph_config(profiler, cache, ineqpath)


def live_rows(db, raw, qtext):
    """The answer rows a live result holds. Dgraph nests; its query file declares where the
    answer list sits with a `# answer-path: result.knows` line (dot path, lists flattened at every
    level); without one the `result` block's items are the answer. TuGraph: the rows of its `result`. db: a
    configuration or a database (its engine decides)."""
    db = CKR.engine(db)
    if db == "nebula":
        return [None] * raw.row_size()
    if db == "arangodb":
        return raw
    if db == "tugraph":
        return raw["result"]
    path = next((ln.split(":", 1)[1].strip() for ln in qtext.splitlines()
                 if ln.strip().startswith("# answer-path:")), "result")
    items = [raw]
    for key in path.split("."):
        nxt = []
        for it in items:
            v = it.get(key, []) if isinstance(it, dict) else []
            nxt.extend(v if isinstance(v, list) else [v])
        items = nxt
    return items


def _measure_raw(profiler, client, db, qtext, params):
    """Reset the engine's counters, run the query once, snapshot: (the RAW counter dump, the normalized
    {get,seek,next}_{count,bytes}). cost_eval's measure_kv_normalized does the same but keeps only the
    normalized view; the raw dump (Nebula's per-kind seek counters, ArangoDB's RocksDB tickers, found /
    miss counts) is free, so every cell keeps it (kvstats.json)."""
    from kvengines.kv import KVProfiler
    reset, snap = counters(profiler, db)
    reset()
    client.execute(qtext, params)
    raw = snap()
    return raw, KVProfiler._normalize_kvstats(raw)


def _keep_raw(workload, sf, qname, db, pidx, qtext, params, raw, run, config=None):
    """Store one cell's raw counter dump with what decided it (query text, params, engine load; Dgraph's pinned
    knobs as `config`)."""
    import hashlib
    from kvengines import live_workloads as W
    path = W.kvstats_path(workload, sf, qname, db, pidx, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"query_sha": hashlib.sha256(qtext.encode()).hexdigest()[:16],
                                "params": params, "data_created": _data_created(db, sf),
                                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "raw": raw,
                                **({"config": config} if config else {})},
                               indent=1, default=str) + "\n")


def _data_created(db, sf):
    """The engine backup's `created` stamp (its load record, live_answers._data_provenance), or ''."""
    from live_answers import _data_provenance
    return (_data_provenance(db, sf) or {}).get("created", "")


def export_dgraph_uids(client, workload, sf, run):
    """Write the live Dgraph's uid of every vertex of each DGRAPH_UID_TYPES type (LDBC id, uid) --
    the order its `first: k` over a uid variable follows. Part of the live capture: a reload can
    change it."""
    from kvengines import live_workloads as W
    for label, dtype in W.DGRAPH_UID_TYPES.items():
        raw = client.execute(f"{{ q(func: type({dtype})) {{ uid id }} }}").raw
        rows = sorted(((int(r["uid"], 16), r["id"]) for r in raw["q"]), key=lambda t: t[0])
        path = W.dgraph_uids_path(workload, sf, label, run)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "uid"])
            w.writerows((i, hex(u)) for u, i in rows)
        print(f"  dgraph uids: {len(rows)} {label} -> {path}")
    meta = W.dgraph_uids_path(workload, sf, "x", run).parent / "uids.meta.json"
    meta.write_text(json.dumps({"data_created": _data_created("dgraph", sf),
                                "types": sorted(W.DGRAPH_UID_TYPES)}, indent=1) + "\n")


def measure_cell_workload(workload, sf, qname, db, pidx, client, profiler, cfg, run):
    """One (query, db, param) of a non-LDBC workload: the counters (+ bytes) of ONE run, then the
    answer size from a second, uncounted run."""
    from kvengines import live_workloads as W

    base = {"sf": sf, "query": qname, "db": db, "param_idx": pidx}
    qtext = W.query_path(workload, CKR.engine(db), qname).read_text()
    params = W.db_params(workload, sf, qname, CKR.engine(db), pidx, qtext)
    if params is None:
        return {**base, "status": f"no-param-{pidx}"}
    t0 = time.time()
    try:
        raw, n = _measure_raw(profiler, client, db, qtext, params)
        _keep_raw(workload, sf, qname, db, pidx, qtext, params, raw, run, cfg.get("pinned"))
        rows = live_rows(db, client.execute(qtext, params).raw, qtext)
    except Exception as exc:                      # a dead server or a bad query, not a result
        return {**base, "status": f"ERROR {type(exc).__name__}: {exc}"}
    return {**base, "S": n["seek_count"], "G": n["get_count"], "N": n["next_count"],
            "SG": n["seek_count"] + n["get_count"], "get_bytes": n["get_bytes"],
            "seek_bytes": n["seek_bytes"], "next_bytes": n["next_bytes"], "rows": len(rows),
            "cache_mode": cfg["cache_mode"], "ineqpath": cfg["ineqfilterpath"],
            "data_created": _data_created(db, sf),
            "secs": round(time.time() - t0, 2), "status": "ok"}


def write_binds(sf, qname, pidx):
    """The template / bind values of write param `pidx` (1-based): the evaluator's args, plus each of the plan's lets as
    the plain LDBC id inside its vid (the insert's synthetic id comes from the plan's own let, never a second formula).
    None when the scale has fewer params."""
    from kvengines import live_workloads as W
    from kvevaluator import workloads
    from kvevaluator.plans.ir.interp import _coerce, _eval_lets
    from kvevaluator.plans.ir.serialize import plan_to_ir
    from kvevaluator.schema.vids import key_id
    rows = workloads.params_module(W.write_family()).query_params(sf, qname)
    if pidx > len(rows):
        return None
    args = rows[pidx - 1]
    ir = plan_to_ir(workloads.queries_module(W.write_family()).REGISTRY[qname](args))
    lets = _eval_lets(ir.get("let"), _coerce(args, ir.get("params") or {}))
    return {**{k: str(v) for k, v in args.items()}, **{k: str(key_id(v)) for k, v in lets.items()}}


def _write_once(profiler, client, db, text, binds):
    """Run one write text once with the engine's counters around it: the raw dump (Dgraph's through the rollup drain,
    dgraph_measure_write)."""
    from kvengines.cost_eval.loader import filter_params
    if db == "arangodb":
        profiler.arango_kvstats_reset()
        client.execute(text, filter_params(binds, text, "@"))
        return profiler.arango_kvstats_snapshot()
    body = string.Template("".join(ln for ln in text.splitlines(keepends=True)
                                   if not ln.lstrip().startswith("#"))).substitute(binds)
    if db == "dgraph":
        return profiler.dgraph_measure_write(lambda: client.mutate(body))
    profiler.nebula_kvstats_reset()
    client.execute(body)
    return profiler.nebula_kvstats_snapshot()


def measure_write_cell(sf, qname, mb, db, pidx, client, profiler, run):
    """One (write primitive, db, param): the write once with its counters, the raw dump kept; an insert is then undone
    (UNDO, uncounted) so the next cell sees the loaded store."""
    from kvengines import live_workloads as W
    from kvengines.kv import KVProfiler
    from write_fidelity import ENGINE

    base = {"db": ENGINE[db], "mb": mb, "param": pidx, "query": qname}
    binds = write_binds(sf, qname, pidx)
    if binds is None:
        return {**base, "status": f"no-param-{pidx}"}
    text = W.query_path(W.write_family(), db, qname).read_text()
    t0 = time.time()
    try:
        raw = _write_once(profiler, client, db, text, binds)
        _keep_raw(W.write_family(), sf, qname, db, pidx, text, binds, raw, run)
        undo = UNDO.get(qname)
        if undo and (db, undo) not in NO_UNDO:
            ubinds = {"person": binds["new_vid"]} if "new_vid" in binds else binds
            uw = KVProfiler._normalize_writes(
                _write_once(profiler, client, db, W.query_path(W.write_family(), db, undo).read_text(), ubinds))
            if uw["put"] + uw["delete"] == 0:
                raise RuntimeError(f"the undo ({undo}) wrote nothing")
    except Exception as exc:                      # a dead server or a bad text, not a result
        return {**base, "status": f"ERROR {type(exc).__name__}: {exc}"}
    if WRITE_COUNTER[db] not in raw:
        return {**base, "status": f"ERROR the dump has no {WRITE_COUNTER[db]}"}
    n, w = KVProfiler._normalize_kvstats(raw), KVProfiler._normalize_writes(raw)
    return {**base, "S": n["seek_count"], "G": n["get_count"], "N": n["next_count"], "P": w["put"],
            "D": w["delete"], "COMMIT": w["commit"], "write_bytes": w["write_bytes"], "get_bytes": n["get_bytes"],
            "seek_bytes": n["seek_bytes"], "next_bytes": n["next_bytes"], "data_created": _data_created(db, sf),
            "secs": round(time.time() - t0, 2), "status": "ok"}


def measure_writes(a):
    """--family writes: every (primitive with a text, db, param) in the sweep's order -> write_sweep.csv."""
    from kvcost.cli.workloads import nparam_of
    from kvengines import live_workloads as W
    from kvevaluator import workloads
    from write_fidelity import PRIMITIVES

    mb_of = {q: mb for q, mb in PRIMITIVES if mb is not None}
    want_q = None if a.query == "all" else {x.strip() for x in a.query.split(",")}
    dbs = [db for db in WRITE_DBS if a.db == "all" or db in {x.strip() for x in a.db.split(",")}]
    # the order (the module docstring): by MB number, the edge delete before the vertex delete
    order = {db: sorted((q for q in W.queries_with_text(W.write_family(), db) if q in mb_of
                         and (want_q is None or q in want_q)), key=lambda q: (q == "w_del_v_record", mb_of[q]))
             for db in dbs}
    nparam = nparam_of(workloads.get(W.write_family()).dataset)
    params = [int(x) for x in a.param.split(",")] if a.param else list(range(1, nparam + 1))
    if not any(order.values()):
        raise SystemExit(f"no write cells match --query {a.query} --db {a.db}")
    clients, profiler = _clients(dbs)
    if "dgraph" in clients:
        print(f"dgraph operating point: {_pin(profiler, a.cache, a.ineqpath)}")
    print(f"write sweep, sf{a.sf}: " + "; ".join(f"{db} {len(order[db])} primitives" for db in dbs)
          + f" x params {params}")
    rows, t0 = [], time.time()
    try:
        for db in dbs:
            for q in order[db]:
                for pidx in params:
                    r = measure_write_cell(a.sf, q, mb_of[q], db, pidx, clients[db], profiler, a.run)
                    if str(r["status"]).startswith("no-param"):
                        continue
                    rows.append(r)
                    flag = "" if r["status"] == "ok" else f"   <-- {r['status']}"
                    print(f"  {db:9s} MB{r['mb']} {q:15s} p{pidx:<2d} S={r.get('S', ''):<4} G={r.get('G', ''):<4} "
                          f"N={r.get('N', ''):<4} P={r.get('P', ''):<4} D={r.get('D', ''):<4} "
                          f"COMMIT={r.get('COMMIT', '')}{flag}")
    finally:
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass

    out = W.write_sweep_path(a.sf)
    out.parent.mkdir(parents=True, exist_ok=True)
    if a.append and out.exists():
        keep = {(r["db"], r["mb"], r["param"]): r for r in csv.DictReader(open(out))}
        keep.update({(r["db"], str(r["mb"]), str(r["param"])): r for r in rows})
        rows = list(keep.values())
    rows = sorted(rows, key=lambda r: (r["db"], int(r["mb"]), int(r["param"])))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WRITE_COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    from write_fidelity import ENGINE
    per = {db: sum(1 for r in rows if r["db"] == ENGINE[db] and r["status"] == "ok") for db in WRITE_DBS}
    bad = [r for r in rows if r["status"] != "ok"]
    print(f"\n{len(rows)} rows -> {out}  ({time.time() - t0:.1f}s)")
    print("cells ok per engine: " + ", ".join(f"{db} {n}" for db, n in per.items()))
    if bad:
        print(f"  {len(bad)} NOT ok: {[(r['db'], r['query'], r['param'], r['status']) for r in bad]}")
    if not rows or bad:
        print(f"measure --family writes: FAILED: {len(rows)} cells, {len(bad)} not ok", file=sys.stderr)
        return 1
    return 0


def query_path(db, qt, num):
    """The engine's LDBC query text for (db, ic|is, number); db: a configuration or a database."""
    db = CKR.engine(db)
    return _QUERIES / _ENGINE_DIR[db] / f"{_KIND[qt]}-{num}.{_EXT[db]}"


def measure_cell(sf, qname, db, pidx, client, profiler, cfg, run, workload):
    """One (query, db, param) of LDBC's workload (`workload`): reset counters, run on the real engine, snapshot. qname
    may be a query-text variant (complex_kv_reads.QUERY_VARIANTS): its own text, its base query's params."""
    from kvengines import live_workloads as W
    from kvengines.cost_eval.loader import build_db_params, parse_query_spec

    qt, num = parse_query_spec(CKR.base_query(qname))
    qtext = W.query_path(workload, CKR.engine(db), qname).read_text()
    rows = param_rows(sf, CKR.base_query(qname))
    if pidx > len(rows):
        return {"sf": sf, "query": qname, "db": db, "param_idx": pidx, "S": "", "G": "",
                "N": "", "SG": "", "cache_mode": "", "ineqpath": "",
                "secs": "", "status": f"no-param-{pidx}"}
    params = build_db_params(rows[pidx - 1], CKR.engine(db), qt, num, qtext)
    t0 = time.time()
    try:
        raw, n = _measure_raw(profiler, client, db, qtext, params)
        _keep_raw(workload, sf, qname, db, pidx, qtext, params, raw, run, cfg.get("pinned"))
    except Exception as exc:                      # a dead server or a bad query, not a result
        return {"sf": sf, "query": qname, "db": db, "param_idx": pidx, "S": "", "G": "",
                "N": "", "SG": "", "cache_mode": "", "ineqpath": "", "secs": "",
                "status": f"ERROR {type(exc).__name__}: {exc}"}
    return {"sf": sf, "query": qname, "db": db, "param_idx": pidx,
            "S": n["seek_count"], "G": n["get_count"], "N": n["next_count"],
            "SG": n["seek_count"] + n["get_count"], "get_bytes": n["get_bytes"],
            "seek_bytes": n["seek_bytes"], "next_bytes": n["next_bytes"],
            "cache_mode": cfg["cache_mode"], "ineqpath": cfg["ineqfilterpath"],
            "data_created": _data_created(db, sf),
            "secs": round(time.time() - t0, 2), "status": "ok"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("--family", dest="workload", default="interactive", choices=["interactive", "microbench", "writes"],
                    help="ldbc (default), microbench (engines/queries/microbench/<db>/) or writes (the live write "
                         "sweep: engines/queries/writes/<db>/, the docstring's section)")
    ap.add_argument("--query", default="all", help="all | is6 | is6,is7")
    ap.add_argument("--db", default="all", help="all (the engine-fidelity check's three: arangodb, dgraph, nebula) | "
                                                "dgraph | dgraph,nebula | tugraph (named only)")
    ap.add_argument("--param", default=None,
                    help="comma-separated 1-based param indices. The default is ONE PER REPLY "
                         "DEPTH: the message-anchored param files are stratified 6-per-depth "
                         "over depths 0-5, so 1,7,13,19,25,31 spans a depth-0 Post through a "
                         "depth-5 Comment. Param 1 alone is what hid four fitted plans. "
                         "[default: 1,7,13,19,25,31 for ldbc; every param for microbench]")
    ap.add_argument("--cache", default=CACHE_MODE, choices=["off", "on"],
                    help="dgraph LocalCache [default off -- what the captures used]")
    ap.add_argument("--ineqpath", default=INEQ_PATH, choices=["per_vertex", "index"],
                    help="dgraph inequality access path [default per_vertex -- what the "
                         "captures used; the ambient .env says index, which is NOT the same "
                         "execution]")
    ap.add_argument("--append", action="store_true",
                    help="merge into the existing CSV instead of replacing it "
                         "((query,db,param) is the key; a re-measured cell wins)")
    ap.add_argument("--run", default=None,
                    help="the run the counts go into, <output>/engines/<dataset>/<scale>/<run>/<family>/ [default "
                         "fidelity: the engine-fidelity check's, which validate.py reads; a figure's own run "
                         "(complex-kv-reads) keeps its cells apart from it]")
    a = ap.parse_args(argv)
    from kvengines import live_workloads as W
    if a.workload == W.write_family():
        if a.run not in (None, W.WRITE_SWEEP):
            raise SystemExit(f"--family writes measures into its own run, {W.WRITE_SWEEP}; --run does not apply")
        a.run = W.WRITE_SWEEP
        return measure_writes(a)
    a.run = a.run or W.FIDELITY

    want_q = None if a.query == "all" else {x.strip() for x in a.query.split(",")}
    want_db = set(FIDELITY_DBS) if a.db == "all" else {x.strip() for x in a.db.split(",")}
    if want_db - set(LIVE_DBS):
        raise SystemExit(f"unknown --db {', '.join(sorted(want_db - set(LIVE_DBS)))} (all | {' | '.join(LIVE_DBS)})")
    all_dbs = LIVE_DBS
    if a.workload != "interactive":
        # the query FILES are the registry: every shape with a live text for the db
        cells = [(q, db) for db in all_dbs if db in want_db
                 for q in W.queries_with_text(a.workload, CKR.engine(db)) if (want_q is None or q in want_q)
                 and CKR.measured_on(db, q)]
        maxp = max((len(W.raw_rows(a.workload, a.sf, q)) for q, _ in cells), default=1)
        params = [int(x) for x in a.param.split(",")] if a.param else list(range(1, maxp + 1))
    else:
        params = [int(x) for x in a.param.split(",")] if a.param else list(W.DEPTH_PARAMS)
        # Default: every cell that HAS an engine plan. But a query named explicitly is measured
        # whether or not a plan exists yet -- measuring the target is how you write the plan, so
        # requiring the plan to exist first would be backwards.
        if want_q is None:
            cells = [(q, db) for q, entry in sorted(engine_plans.ENGINE_REGISTRY.items())
                     for db in sorted(entry) if db in want_db]
        else:
            # a configuration only on the queries it is measured on (CKR.measured_on: dgraph-no-index on ic1; a
            # query-text variant on its one database)
            cells = [(q, db) for q in sorted(want_q) for db in all_dbs
                     if db in want_db and CKR.measured_on(db, q)]
    if not cells:
        raise SystemExit(f"no cells match --query {a.query} --db {a.db}")

    clients, profiler = _clients(sorted({db for _, db in cells}))
    # Dgraph's knobs are pinned per configuration (CKR.knobs), before its first cell and whenever the configuration
    # changes; at the end dgraph's (the counting point, through its index) is pinned again, as measure.py left it
    dgraph_dbs = [d for d in dict.fromkeys(d for _, d in cells) if CKR.engine(d) == "dgraph"]
    pinned = {"db": None}

    def dgraph_cfg(db):
        if pinned["db"] != db:
            pinned.update(db=db, cfg=_pin(profiler, a.cache, a.ineqpath, db))
            print(f"dgraph operating point ({db}): {pinned['cfg']}")
        c = pinned["cfg"]
        return {"cache_mode": c["cache_mode"], "ineqfilterpath": c["ineqfilterpath"], "pinned": c}

    rows, t0 = [], time.time()
    print(f"measuring {len(cells)} cells x {len(params)} params "
          f"= {len(cells) * len(params)} runs, sf{a.sf}")
    try:
        for pidx in params:
            for qname, db in cells:
                cellcfg = dgraph_cfg(db) if CKR.engine(db) == "dgraph" else {"cache_mode": "n/a",
                                                                              "ineqfilterpath": "n/a"}
                r = (measure_cell(a.sf, qname, db, pidx, clients[db], profiler, cellcfg, a.run, a.workload)
                     if a.workload == "interactive" else
                     measure_cell_workload(a.workload, a.sf, qname, db, pidx, clients[db],
                                           profiler, cellcfg, a.run))
                if str(r["status"]).startswith("no-param"):
                    continue                        # this shape has fewer params than the widest
                rows.append(r)
                flag = "" if r["status"] == "ok" else f"   <-- {r['status']}"
                print(f"  p{pidx:<3d} {qname:5s} {db:9s} S={r.get('S', ''):<7} "
                      f"G={r.get('G', ''):<8} N={r.get('N', ''):<8}{flag}")
        if "dgraph" in clients and a.workload == "interactive":
            export_dgraph_uids(clients["dgraph"], a.workload, a.sf, a.run)
    finally:
        home = next(x for x in CKR.DBS if CKR.engine(x) == "dgraph")      # dgraph: the counting point, its index
        if dgraph_dbs and pinned["db"] not in (None, home):
            dgraph_cfg(home)

        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass

    out = W.out_dir(a.workload, a.sf, a.run) / "live_kvstats.csv"
    cols = COLUMNS if a.workload == "interactive" else MB_COLUMNS
    out.parent.mkdir(parents=True, exist_ok=True)   # output_dir returns a path; the caller makes dirs
    if a.append and out.exists():
        keep = {(r["query"], r["db"], r["param_idx"]): r
                for r in csv.DictReader(open(out)) if "param_idx" in r}
        keep.update({(r["query"], r["db"], str(r["param_idx"])): r for r in rows})
        rows = sorted(keep.values(), key=lambda r: (r["query"], r["db"], int(r["param_idx"])))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    # A "no-param-N" row is NOT a failure: this SF's param file simply has fewer than N entries
    # (IC queries have 15 params at sf0.1 vs 35 at sf0.3), so index N is not a cell to measure.
    # Only a real error (a dead server, a bad query) should make this run report failure -- else
    # every sub-sf0.3 run exits non-zero just for asking for params that scale never had.
    skipped = [r for r in rows if str(r["status"]).startswith("no-param")]
    bad = [r for r in rows if r["status"] != "ok" and not str(r["status"]).startswith("no-param")]
    print(f"\n{len(rows)} rows -> {out}  ({time.time() - t0:.1f}s)")
    if skipped:
        print(f"  {len(skipped)} skipped (param index beyond sf{a.sf}): "
              f"{[(r['query'], r['db'], r['param_idx']) for r in skipped]}")
    if bad:
        print(f"  {len(bad)} NOT ok: {[(r['query'], r['db'], r['param_idx']) for r in bad]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
