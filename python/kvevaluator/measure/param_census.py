#!/usr/bin/env python3
"""The param census: per LDBC Interactive query, which HOSTED rows 1-10 return an empty answer, and which of those make
the plan stop early. A report only: every query reads LDBC's hosted params, and the census decides nothing
(inputs/params/ldbc/README.md).

Runs the C++ binary (counts, `kvevaluator serve`) on ONE store per SF, the reference cell props_condensed_adj_split
(built on first use), each query's bundle as the planner selects it. Per row it keeps the answer's row count, its hash,
the body the planner picked, every op and byte lane the
response carries (as the grid's runs CSV) and weighted ops (kvevaluator.weights.wops). wall_ns is kept because it is free; the
census is not a timing run. A row stops early when its answer is empty and its weighted ops are
under 10% of the median row's: the median over the non-empty rows, or over all rows when none is non-empty (scale-free, so an all-empty query that does its full work has no row that stops early). The answer is the same on every store (the gates check it); "stops early" is read on this one cell.

Writes inputs/params/ldbc/sf<SF>/interactive/param_census.csv and its .meta.json (binary, store, command, CENSUS_VERSION), then
prints each query's stop-early rows beside param_sources.json's source. A run with a failed row writes to
output/logs/param-census/ instead: the tracked file only ever holds a complete census.

    python3 python/kvevaluator/measure/param_census.py --sf 0.1 [--queries ic1,...,ic12] [--rows 1-10]
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import statistics
import sys
from kvevaluator import weights
from kvevaluator.paths import ROOT, recorded_path, store_dir
from kvevaluator.output_tree import Tree

EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.plan_choice.candidates import candidates
from kvevaluator.datasets.ldbc import params as P
from kvevaluator.measure import grid_capture
from kvevaluator.measure.grid_bodies import ServeRunner, request
from kvevaluator.capture.step_record import READ_BYTE_LANES, READ_OPS  # (a read row's ops and lanes)

CENSUS_VERSION = 3           # bump when what a row records or how "stops early" is decided changes; the gate refuses another
CELL = "props_condensed_adj_split"
STOP_SHARE = 0.10            # a stop-early row's weighted ops are under this share of the median row's
QUERIES = [f"ic{i}" for i in range(1, 13)]
COLS = ["sf", "query", "row", "source", "param_file_sha", "ok", "error", "body", "body_sha", "answer_rows", "result_hash",
        *READ_OPS, "P", "D", *READ_BYTE_LANES, "read_bytes", "wall_ns", "wops", "empty", "stops_early"]


def census_path(sf):
    return os.path.join(P._interactive(sf), "param_census.csv")


def stop_flags(rows):
    """mark each row's `empty` and `stops_early` within one query's rows"""
    full = [r["wops"] for r in rows if r["ok"] and r["answer_rows"] > 0]
    ran = [r["wops"] for r in rows if r["ok"]]
    ref = statistics.median(full or ran) if (full or ran) else 0.0   # the non-empty rows, else all rows
    for r in rows:
        r["empty"] = int(r["ok"] and r["answer_rows"] == 0)
        r["stops_early"] = int(bool(r["empty"]) and r["wops"] < STOP_SHARE * ref)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--queries", default=",".join(QUERIES))
    ap.add_argument("--rows", default="1-10", metavar="LO-HI")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--P", type=int, default=8)
    ap.add_argument("--out", default=None, help="write here instead of the tracked sf<SF>/param_census.csv (the gate's re-run)")
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.rows.split("-"))
    if a.rows != "1-10" and not a.out:
        raise SystemExit("the tracked census holds rows 1-10: write any other range with --out, never to the tracked file")
    queries = a.queries.split(",")
    spec = os.path.join(grid_capture.GRID, f"{CELL}.json")
    store = grid_capture.meta_store(a.sf, "interactive", spec, build=True)
    schema = grid_capture.schema_file("interactive")
    prov = binary.binary_provenance()
    print(f"[census] binary {binary.binary_path()} commit {prov.get('binary_git_sha')} dirty {prov.get('binary_dirty')}; "
          f"store {store}", flush=True)
    runner = ServeRunner(binary.binary_path(), store, schema, a.timeout, fmt="meta")
    out = []
    try:
        for q in queries:
            path = P.param_file(a.sf, q, source="hosted")
            fsha = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
            allp = P.query_params(a.sf, q, hi, source="hosted")
            if len(allp) < hi:
                raise SystemExit(f"{q}: the hosted file has {len(allp)} rows, --rows {a.rows} needs {hi}")
            ir = candidates("interactive", q)[0]
            rows = []
            for i in range(lo, hi + 1):
                resp, err = runner.call(request(store, schema, ir, allp[i - 1], a.P))
                r = {"sf": a.sf, "query": q, "row": i, "source": "hosted", "param_file_sha": fsha, "ok": int(resp is not None),
                     "error": err[:200], "body": "", "body_sha": "", "answer_rows": 0, "result_hash": "",
                     **{k: 0 for k in (*READ_OPS, "P", "D", *READ_BYTE_LANES, "read_bytes")}, "wall_ns": "", "wops": 0.0}
                if resp is not None:
                    prov = resp.get("provenance", {})
                    r["body"], r["body_sha"] = prov.get("body", ""), prov.get("body_sha", "")
                    r["answer_rows"] = len(resp.get("rows") or [])
                    r["result_hash"] = hashlib.sha1(json.dumps(resp.get("rows"), sort_keys=True).encode()).hexdigest()[:16]
                    for s in resp.get("steps", []):
                        if s.get("kind") == "kv":
                            for k in (*READ_OPS, "P", "D"):
                                r[k] += int(s.get(k, 0))
                            for b in READ_BYTE_LANES:
                                r[b] += int(s["bytes"].get(b, 0))
                    r["read_bytes"] = sum(r[b] for b in READ_BYTE_LANES)
                    r["wall_ns"] = (resp.get("totals") or {}).get("wall_ns", "")
                    r["wops"] = round(weights.wops(r["S"], r["G"], r["N"]), 2)
                rows.append(r)
            stop_flags(rows)
            out += rows
            print(f"[census] sf{a.sf} {q}: {sum(r['ok'] for r in rows)}/{len(rows)} ran, {sum(r['empty'] for r in rows)} empty, "
                  f"{sum(r['stops_early'] for r in rows)} stop early", flush=True)
    finally:
        runner.close()
    failed = sum(1 for r in out if not r["ok"])
    dest = a.out or census_path(a.sf)
    if failed and not a.out:                 # the tracked file only ever holds a complete census
        dest = Tree().logs("param-census", f"sf{a.sf}-failed.csv")
        os.makedirs(dest.parent, exist_ok=True)
        print(f"[census] {failed} rows failed: NOT writing the tracked file; see {os.path.relpath(dest, EXP)}")
    with open(dest, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    meta = {"what": "the param census (python/kvevaluator/measure/param_census.py): hosted rows, answer row counts and weighted ops per row on one cell",
            "census_version": CENSUS_VERSION, "cell": CELL, "store": recorded_path(os.path.abspath(store), store_dir()), "rows": a.rows,
            "queries": queries, "binary": {k: prov.get(k) for k in ("binary_git_sha", "binary_dirty")},
            "command": f"python3 python/kvevaluator/measure/param_census.py --sf {a.sf} --queries {a.queries} --rows {a.rows}"
                       + (f" --out {recorded_path(os.path.abspath(a.out))}" if a.out else ""),
            "date": datetime.date.today().isoformat(),
            "stops_early": f"an empty answer with weighted ops under {STOP_SHARE:g} x the median row's (over the non-empty rows, or over all rows when none is non-empty)"}
    with open(dest[:-4] + ".meta.json", "w") as f:
        json.dump(meta, f, indent=1)
        f.write("\n")
    print(f"[census] wrote {os.path.relpath(dest, EXP)} ({len(out)} rows)")
    for q in queries:
        rq = [r for r in out if r["query"] == q]
        stops = [str(r["row"]) for r in rq if r["stops_early"]]
        print(f"  sf{a.sf} {q:5s} stops early {len(stops)}/{sum(1 for r in rq if r['ok'])}"
              + (f" (rows {', '.join(stops)})" if stops else "") + f"; param_sources.json: {P.param_source(a.sf, q)}")
    return 1 if any(not r["ok"] for r in out) else 0


if __name__ == "__main__":
    sys.exit(main())
