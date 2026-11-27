#!/usr/bin/env python3
"""Measure the live engines' /kvstats per (db, query, param, config) -> engine_kvstats.csv. This tool is its sole writer.

The config is part of the key: an engine's KV cost depends on it, so every row carries the dgraph operating point
(cache_mode, ineqpath, eqpath, typepath) and the key includes (cache_mode, ineqpath); nebula and arango have no such
knobs ("n/a"). The point is the one the server was started with (the dgraph knobs in .env, set at server start by
engines/run-engine.sh); this tool only reads it (DGRAPH_KNOBS) and never re-pins. To measure another point, change
.env and restart the server.

Each (db, query, param) writes its own dir <db>/<query>/p<param>/ below the scale's engine output dir, holding
kvstats.csv (its one row) and answer.json (the engine's answer rows). These per-cell files are the source of truth;
engine_kvstats.csv is rebuilt from them after every run. A stopped sweep resumes by default: a cell whose kvstats.csv
is ok and was measured at the current operating point is skipped (--overwrite re-measures). A failed cell writes no
file, so it is retried on the next run.

Usage:
  python -m kvengines.measure --sf 0.1                         # all 19 queries x 3 DBs, 35 params (resume if partial)
  python -m kvengines.measure --sf 0.1 --overwrite             # re-measure every cell
  python -m kvengines.measure --sf 0.1 --query ic2 --db dgraph # one cell

Needs the target servers up (engines/run-engine.sh brings them up first).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from kvevaluator.paths import ROOT
_QDIR = ROOT / "engines" / "queries" / "ldbc"
_ENGINE_DIR = {"dgraph": "dgraph", "nebula": "nebulagraph", "arangodb": "arangodb"}

from kvevaluator.measure.report import output_dir
from kvevaluator.datasets.ldbc.params import param_rows
from kvengines.cost_eval.measured import measure as measure_mod  # (DGRAPH_KNOBS = the .env operating point)

COLUMNS = ["sf", "db", "query", "param_idx", "cache_mode", "ineqpath", "eqpath", "typepath", "detsort",
           "S", "G", "N", "get_bytes", "seek_bytes", "next_bytes", "bytes_read",
           "latency_ms", "secs", "status"]
# The KEY that identifies a measured cell (used by --fill and the merge). Config is part of it.
KEY_COLS = ("sf", "db", "query", "param_idx", "cache_mode", "ineqpath")
# The dgraph operating point a cell was measured at. Resume compares these against the current point so it
# never reuses a cell measured under different knobs (the cell path does not encode them -- see _cell_dir).
OP_POINT_COLS = ("cache_mode", "ineqpath", "eqpath", "typepath", "detsort")

# The full LDBC set this experiment covers: IC1-12 + IS1-7 (IC13/14 are out of scope). The plots need
# every query, so unlike engine_fidelity/measure.py we do NOT restrict to queries that have an engine plan.
ALL_QUERIES = [f"ic{i}" for i in range(1, 13)] + [f"is{i}" for i in range(1, 8)]
ALL_DBS = ("dgraph", "nebula", "arangodb")

_EXT = {"dgraph": "graphql", "nebula": "gql", "arangodb": "aql"}
_KIND = {"ic": "interactive-complex", "is": "interactive-short"}


def _clients(dbs):
    """One client per DB in `dbs` (a DB not measured needs no running server) + one KVProfiler. Imported lazily so a
    caller that only reads the CSV needs neither kvengines nor a running server."""
    from kvengines import connect
    from kvengines.kv import KVProfiler
    kwargs = {"arangodb": {"host": "127.0.0.1", "request_timeout": 600}}
    return {db: connect(db, **kwargs.get(db, {})) for db in dbs}, KVProfiler()


def _cell_config(db):
    """The config columns for a cell, from the .env-declared dgraph operating point (DGRAPH_KNOBS); nebula and arango: 'n/a'."""
    if db != "dgraph":
        return {"cache_mode": "n/a", "ineqpath": "n/a", "eqpath": "n/a", "typepath": "n/a",
                "detsort": "n/a"}
    k = measure_mod.DGRAPH_KNOBS
    # All 5 dgraph knobs are recorded so a row carries the full operating point. Only cache_mode + ineqpath
    # are part of the KEY (they are the ones a case study actually sweeps); eq/type/detsort are held at the
    # canonical point (index/index/enable) but recorded anyway, so provenance is complete, not partial.
    return {"cache_mode": k["localcache"], "ineqpath": k["ineqfilterpath"],
            "eqpath": k["eqfilterpath"], "typepath": k["typefilterpath"], "detsort": k["detsort"]}


def _cell_dir(sf, db, qname, pidx):
    """The per-cell output dir <engine dir>/<db>/<query>/p<param>/, holding kvstats.csv and answer.json.
    The config is not in this path, so one operating point is stored per (db, query, param); to keep two side by side,
    put the config in the path here (the aggregate's _key already tells them apart)."""
    return Path(output_dir(sf, "engine", db, qname, f"p{pidx}"))


def _save_answer(cell, db, raw):
    """Write one engine's answer rows to <cell>/answer.json; default=str so a non-serializable value never fails a cell."""
    payload = raw.as_data_frame().to_dict(orient="records") if db == "nebula" else raw
    with open(cell / "answer.json", "w") as f:
        json.dump(payload, f, default=str, ensure_ascii=False)


def measure_cell(sf, db, qname, pidx, client, profiler, cfg):
    """One (db, query, param): reset counters, run on the real engine, snapshot. cfg = the config
    columns dict from _cell_config. Returns a full CSV row."""
    from kvengines.cost_eval.loader import build_db_params, parse_query_spec
    from kvengines.cost_eval.measured.measure import measure_kv_normalized

    base = {"sf": sf, "db": db, "query": qname, "param_idx": pidx, **cfg}
    qt, num = parse_query_spec(qname)
    qtext = (_QDIR / _ENGINE_DIR[db] /
             f"{_KIND[qt]}-{num}.{_EXT[db]}").read_text()
    rows = param_rows(sf, qname)
    if pidx > len(rows):
        return {**base, "S": "", "G": "", "N": "", "secs": "", "status": f"no-param-{pidx}"}
    params = build_db_params(rows[pidx - 1], db, qt, num, qtext)
    t0 = time.time()
    try:
        # measure_kv_normalized returns the full {get,seek,next}_{count,bytes} view so we record
        # bytes-read alongside S/G/N. seek_bytes is 0 for nebula/arango (no per-seek byte ticker),
        # so bytes_read = get_bytes + seek_bytes + next_bytes is effectively get+next there.
        n = measure_kv_normalized(profiler, client, db, qtext, params)
    except Exception as exc:                      # a dead server or a bad query, not a result
        return {**base, "S": "", "G": "", "N": "", "secs": "",
                "status": f"ERROR {type(exc).__name__}: {exc}"}
    # Second, UNMEASURED execute (read-only, deterministic): capture the answer rows and a clean
    # single-run latency. The kvstats run above does reset+snapshot, so its wall is not a faithful
    # query latency -- qr.latency_ms is. Guarded so an answer-serialization hiccup never voids a cell
    # whose S/G/N + bytes are already good.
    latency_ms = ""
    try:
        cell = _cell_dir(sf, db, qname, pidx)
        cell.mkdir(parents=True, exist_ok=True)
        qr = client.execute(qtext, params)
        latency_ms = round(qr.latency_ms, 3)
        _save_answer(cell, db, qr.raw)
    except Exception as exc:
        print(f"    WARN {db} {qname} p{pidx}: answer/latency capture failed ({type(exc).__name__}: {exc})")
    gb, sb, nb = n["get_bytes"], n["seek_bytes"], n["next_bytes"]
    return {**base, "S": n["seek_count"], "G": n["get_count"], "N": n["next_count"],
            "get_bytes": gb, "seek_bytes": sb, "next_bytes": nb, "bytes_read": gb + sb + nb,
            "latency_ms": latency_ms, "secs": round(time.time() - t0, 2), "status": "ok"}


def _key(row):
    return tuple(str(row[c]) for c in KEY_COLS)


def _load_cell(path):
    """Read one per-cell kvstats.csv (header + 1 row) -> the row dict, or None if absent/empty."""
    if not path.exists():
        return None
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[0] if rows else None


def _same_operating_point(prev, cfg):
    """True if the stored cell `prev` was measured at the operating point `cfg` (from _cell_config).
    The cell path does not encode the knobs, so resume uses this to avoid reusing a stale-config cell."""
    return all(str(prev.get(c)) == str(cfg[c]) for c in OP_POINT_COLS)


def _write_atomic(path, rows):
    """Sort by (db, query, param, config) and write via temp+replace so a crash never leaves a torn CSV."""
    rows = sorted(rows, key=lambda r: (r["db"], r["query"], int(r["param_idx"]),
                                       r["cache_mode"], r["ineqpath"]))
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _write_cell_kvstats(cell, row):
    """Write one measured cell as a standalone 1-row kvstats.csv (header = COLUMNS) inside <cell>/."""
    _write_atomic(cell / "kvstats.csv", [row])


def _rebuild_aggregate(sf, out):
    """Rebuild the flat engine_kvstats.csv from every per-cell kvstats.csv below the scale's engine dir; the row count."""
    root = Path(output_dir(sf, "engine"))
    rows = {}
    for kv in sorted(root.glob("*/*/p*/kvstats.csv")):    # <db>/<query>/p<param>/kvstats.csv
        r = _load_cell(kv)
        if r:
            rows[_key(r)] = r
    if rows:
        _write_atomic(out, list(rows.values()))
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("--query", default="all", help="all | ic2 | ic2,is1")
    ap.add_argument("--db", default="all", help="all | dgraph | dgraph,nebula")
    ap.add_argument("--nparam", type=int, default=int(os.environ.get("KVBENCH_NPARAM", "35")),
                    help="measure params 1..N (capped at each query's available params). Default 35 = "
                         "KVBENCH_NPARAM, matching the sim sweep. Ignored if --param is given.")
    ap.add_argument("--param", default=None,
                    help="explicit comma-separated 1-based param indices; overrides --nparam")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-measure every requested cell even if its per-cell kvstats.csv is already ok "
                         "(default: RESUME -- skip cells already done, so a stopped sweep finishes on re-run)")
    ap.add_argument("--fill", action="store_true", help=argparse.SUPPRESS)  # a no-op: resume is the default
    ap.add_argument("--quiet", action="store_true",
                    help="drop the per-cell S/G/N counts but keep a one-line progress marker per cell "
                         "(which db/query/param ran); read the counts from the CSV. Non-ok cells still "
                         "print their status.")
    a = ap.parse_args(argv)

    queries = ALL_QUERIES if a.query == "all" else [x.strip() for x in a.query.split(",")]
    dbs = ALL_DBS if a.db == "all" else tuple(x.strip() for x in a.db.split(","))

    unknown = [d for d in dbs if d not in ALL_DBS]
    if unknown:
        ap.error(f"unknown --db {', '.join(unknown)} (all | {' | '.join(ALL_DBS)} | a comma list)")
    clients, profiler = _clients(dbs)
    # No pinning here: the 5 dgraph knobs are set once at server start from .env (run-engine.sh brings the
    # servers up), and measure_kv does not re-pin. We just record the declared operating point per row.
    print(f"dgraph operating point (from .env / DGRAPH_KNOBS): {dict(measure_mod.DGRAPH_KNOBS)}")

    out = Path(output_dir(a.sf, "engine")) / "engine_kvstats.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    resume = not a.overwrite      # default: skip cells already done; --overwrite forces a full re-measure

    # Build the cell list (query, param, db). Params are capped per query at what the param file has, so
    # a query with fewer than --nparam params does not spawn a wall of no-param rows.
    cells = []
    for q in queries:
        avail = len(param_rows(a.sf, q))
        pidxs = [int(x) for x in a.param.split(",")] if a.param else range(1, min(a.nparam, avail) + 1)
        for pidx in pidxs:
            for db in dbs:
                cells.append((q, pidx, db))

    print(f"{'resuming' if resume else 'measuring (overwrite)'}: {len(cells)} requested cells, sf{a.sf}")
    t0 = time.time()
    bad, done, skipped = [], 0, 0
    try:
        for q, pidx, db in cells:
            cfg = _cell_config(db)
            # RESUME: skip a cell only if its per-cell kvstats.csv is already ok AND was measured at the SAME
            # operating point we are about to use. The cell path does not encode the knobs, so without the
            # operating-point check a re-run after changing the dgraph knobs in .env would silently resume
            # stale-config numbers as "done" -- the operating-point confound this project exists to avoid. A
            # mismatch re-measures the cell (overwriting it). --overwrite skips the whole check.
            kvcsv = _cell_dir(a.sf, db, q, pidx) / "kvstats.csv"
            if resume:
                prev = _load_cell(kvcsv)
                if prev and prev.get("status") == "ok":
                    if _same_operating_point(prev, cfg):
                        skipped += 1
                        continue
                    print(f"  {q:5s} p{pidx:<3d} {db:9s}   <-- re-measuring: operating point changed "
                          f"({prev.get('cache_mode')}/{prev.get('ineqpath')} -> "
                          f"{cfg['cache_mode']}/{cfg['ineqpath']})")

            r = measure_cell(a.sf, db, q, pidx, clients[db], profiler, cfg)
            # Only an ok cell gets a durable per-cell file; a failed/no-param cell writes nothing, so it is
            # retried on the next run instead of being resumed as "done".
            if r["status"] == "ok":
                _write_cell_kvstats(kvcsv.parent, r)
                done += 1
            else:
                bad.append(r)
            # --quiet keeps a one-line-per-cell PROGRESS marker (which db/query/param ran) but drops the
            # S/G/N counts, which are in the CSV. A non-ok cell prints its status either way.
            flag = "" if r["status"] == "ok" else f"   <-- {r['status']}"
            if a.quiet:
                print(f"  {q:5s} p{pidx:<3d} {db:9s}{flag}")
            else:
                print(f"  {q:5s} p{pidx:<3d} {db:9s} S={r['S']:<7} G={r['G']:<8} N={r['N']:<8} "
                      f"B={r.get('bytes_read', ''):<10}{flag}")
    finally:
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass
        # Rebuild the flat engine-of-record from EVERY per-cell file on disk (this run's + prior runs'),
        # even on an interrupt -- so a partial sweep still yields a complete, up-to-date aggregate.
        total = _rebuild_aggregate(a.sf, out)

    print(f"\nmeasured {done}, skipped {skipped} already-done, {len(bad)} failed this run "
          f"({time.time() - t0:.1f}s) -> {total} rows in {out}")
    if bad:
        print(f"  NOT ok: {[(r['query'], r['db'], r['param_idx']) for r in bad]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
