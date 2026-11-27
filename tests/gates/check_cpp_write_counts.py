"""The WRITE-COUNT ORACLE: every write plan x every format with a C++ store, each cell on a
FRESH disposable arena, run shadow-mode then apply-mode (real RocksDB writes). Two independent
checks per cell, no Python execution twin:
  1. inside the executor (apply mode): RocksDB's own tickers == the workers' counts --
     keys.read == G + raw_gets (raw_gets: the few audited UNCOUNTED reads -- a date the op already
     holds; every read-modify-write read is in G), seek/next as for reads,
     keys.written == P + D, bytes.written == the single-op batch formula (12 + record);
     a mismatch errors the cell;
  2. measure/cppwrite.cross_check: the shadow run (what grid_writes measures) ==
     the apply run on every step count, every byte channel and the rows.
So the measured write counts == the apply counts == what RocksDB booked. Prints one line per cell
and a CSV (--csv) of the audited counts per cell (S G N P D, write bytes, raw_gets, RocksDB's
keys/bytes written, and every byte lane the steps return as bytes_<lane>, the used lanes included). Exit 1 on any failure.
meta joins with three cells (--meta-cells; each a spec in inputs/specs/grid/), reported as meta-<cell>.
Run: python3 tests/gates/check_cpp_write_counts.py [--sf 0.1] [--formats a,b] [--meta-cells c,..] [--queries q,..] [--nparam 5] [--csv out]
"""
import argparse
import csv
import os
import sys
import time
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.measure.cppwrite import Arena, run_cell, sequence
from kvevaluator.measure.server import CppServer
from kvevaluator.measure.stores import cpp_store_name, store_chunk

FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr", "meta"]
META_CELLS = ["props_split_adj_split", "props_condensed_adj_condensed", "props_chunked_adj_chunked"]
META_SPECS = os.path.join(str(ROOT), "inputs", "specs", "grid")
COLS = ["sf", "format", "workload", "query", "param_idx", "S", "G", "N", "P", "D", "write_key_bytes",
        "write_value_bytes", "raw_gets", "rocks_keys_written", "rocks_bytes_written", "rocks_bytes_expected",
        "steps_checked", "rows", "result"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--formats", default=",".join(FORMATS))
    ap.add_argument("--queries", default=None, help="comma list (default: every write plan)")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--csv", default=None, help="write the audited per-cell counts here")
    ap.add_argument("--schema", default="canonical", help="the store's physical design (full_index, ...)")
    ap.add_argument("--meta-cells", default=",".join(META_CELLS), help="meta's cells (spec stems in inputs/specs/grid)")
    a = ap.parse_args(argv)
    queries = [q for q in a.queries.split(",") if q] if a.queries else None
    seq = sequence(a.sf, queries, a.nparam)
    run_apply = {"P": 1, "apply_writes": True, "perf_level": 1, "iterations": {"warmup": 0, "measured": 1}}
    rows, bad, n = [], [], 0
    t0 = time.time()
    # (format, meta cell): meta runs once per cell, its spec passed by name (the store's name carries it)
    units = []
    for fmt in [f for f in a.formats.split(",") if f]:
        units += [(fmt, c) for c in a.meta_cells.split(",") if c] if fmt == "meta" else [(fmt, None)]
    for fmt, cell in units:
        spec = os.path.join(META_SPECS, f"{cell}.json") if cell else None
        arm = cpp_store_name(fmt, a.schema, None, store_chunk(fmt), spec=spec)
        label = f"meta-{cell}" if cell else fmt
        srv_kw = {}
        if a.schema != "canonical":
            srv_kw["schema"] = schema_path("ldbc", a.schema)
        for wl, q, idx, args, ir in seq:
            n += 1
            where = f"{label} {wl.split('/')[1]}/{q} p{idx}"
            try:
                with Arena(a.sf, arm, "wcount") as ar:
                    with CppServer(ar.dir, fmt, **srv_kw) as srv:
                        applied, _shadow = run_cell(srv, (wl, q, idx, args, ir), run_apply)
            except SystemExit as e:                      # the executor's ticker mismatch / cross_check
                bad.append((where, str(e)))
                print(f"  FAIL {where}: {str(e)[:200]}")
                continue
            tot = {k: 0 for k in ("S", "G", "N", "P", "D", "write_key", "write_value", "raw_gets",
                                  "rocks_keys_written", "rocks_bytes_written", "rocks_bytes_expected")}
            checked = 0
            lanes = {}                                   # every byte lane the steps return (free): bytes_<lane>
            for s in applied["steps"]:
                for k in ("S", "G", "N", "P", "D"):
                    tot[k] += s[k]
                for k, v in sorted((s.get("bytes") or {}).items()):
                    if isinstance(v, (int, float)):
                        lanes[f"bytes_{k}"] = lanes.get(f"bytes_{k}", 0) + v
                tot["write_key"] += s["bytes"]["write_key"]
                tot["write_value"] += s["bytes"]["write_value"]
                au = s.get("audit") or {}
                checked += bool(au.get("checked"))
                for k in ("raw_gets", "rocks_keys_written", "rocks_bytes_written", "rocks_bytes_expected"):
                    tot[k] += au.get(k, 0)
            ok = checked == len(applied["steps"]) and tot["rocks_keys_written"] == tot["P"] + tot["D"] \
                and tot["rocks_bytes_written"] == tot["rocks_bytes_expected"]
            if not ok:
                bad.append((where, f"audit totals: keys {tot['rocks_keys_written']} vs P+D {tot['P'] + tot['D']}, "
                                   f"bytes {tot['rocks_bytes_written']} vs {tot['rocks_bytes_expected']}, "
                                   f"checked {checked}/{len(applied['steps'])}"))
            print(f"  {'OK  ' if ok else 'FAIL'} {where}: P {tot['P']} D {tot['D']} = rocks keys {tot['rocks_keys_written']}; "
                  f"bytes {tot['write_key'] + tot['write_value']} (batched {tot['rocks_bytes_written']}); "
                  f"reads G {tot['G']} + raw_gets {tot['raw_gets']}; rows {len(applied['rows'])}")
            rows.append({"sf": a.sf, "format": label, "workload": wl.split("/")[1], "query": q, "param_idx": idx,
                         "S": tot["S"], "G": tot["G"], "N": tot["N"], "P": tot["P"], "D": tot["D"],
                         "write_key_bytes": tot["write_key"], "write_value_bytes": tot["write_value"],
                         "raw_gets": tot["raw_gets"], "rocks_keys_written": tot["rocks_keys_written"],
                         "rocks_bytes_written": tot["rocks_bytes_written"],
                         "rocks_bytes_expected": tot["rocks_bytes_expected"],
                         "steps_checked": f"{checked}/{len(applied['steps'])}", "rows": len(applied["rows"]),
                         "result": "ok" if ok else "FAIL", **lanes})
    if a.csv:
        os.makedirs(os.path.dirname(os.path.abspath(a.csv)), exist_ok=True)
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS + sorted({k for r in rows for k in r} - set(COLS)), restval=0)
            w.writeheader()
            w.writerows(rows)
    if bad:
        print(f"WRITE-COUNT ORACLE FAIL: {len(bad)} of {n} cells ({time.time() - t0:.0f}s)")
        return 1
    if n == 0:                                       # a filter that matched nothing is not a pass
        print(f"WRITE-COUNT ORACLE FAIL: 0 cells ran (formats {a.formats!r}, queries {a.queries!r})")
        return 1
    print(f"WRITE-COUNT ORACLE OK: {n} cells, RocksDB tickers == counts and shadow == apply on every one "
          f"({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
