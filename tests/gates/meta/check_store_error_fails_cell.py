#!/usr/bin/env python3
"""A store grid_bodies cannot open fails its cell (grid_bodies.infra_error, the cell abort): live, at sf0.1, one cell,
one query and param per case, each into its own scratch capture root:

  1. the control: the cell with its store free: rc 0, a counts file, every row ok;
  2. the store held open by a second process (a server, as another driver's would): rc 1, a FAIL line naming the cell
     and the store error, and no counts file (no rows written, so nothing for a later run to reuse);
  3. the store released, the same command again without --fresh: rc 0, the cell measured (not "reused"), rows ok.
The failure it catches: case 2 giving rc 0 and a counts file of failed rows, which case 3 would then reuse.

    python3 tests/gates/meta/check_store_error_fails_cell.py [--sf 0.1] [--cell props_split_adj_split]
Needs the built binary; builds the cell's store if missing (seconds at sf0.1). Exit 0 = every assertion holds.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile

from kvevaluator.output_tree import Tree
from kvevaluator.measure import grid_bodies as GV
from kvevaluator.measure import server
from kvevaluator.paths import OUTPUT_DIR, ROOT

SF = "0.1"   # --sf


def run_cell(cell, query, out):
    """grid_bodies on one (query, param 1) of the cell into the output tree `out`: (rc, its output)"""
    r = subprocess.run([sys.executable, "-m", "kvevaluator.measure.grid_bodies", "--sf", SF, "--family", "interactive",
                        "--cells", cell, "--queries", query, "--params", "1", "--nparam", "1", "--root", out],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    return r.returncode, r.stdout + r.stderr


def rows_of(out, cell):
    """the cell's counts rows in the output tree `out`, or None when it has no counts file"""
    p = Tree(out).capture("interactive", SF).path_of("counts", cell)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else None


def main():
    global SF
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", default=SF)
    ap.add_argument("--cell", default="props_split_adj_split")
    a = ap.parse_args()
    SF, cell = a.sf, a.cell
    tmp = tempfile.mkdtemp(dir=str(OUTPUT_DIR), prefix="check_store_error_fails_cell_")
    fails, checked = [], 0

    def check(what, good, detail=""):
        nonlocal checked
        checked += 1
        print(f"{'PASS' if good else 'FAIL'} {what}{': ' + detail if detail else ''}", flush=True)
        if not good:
            fails.append(what)

    try:
        # 1. the control: the store free
        rc, log = run_cell(cell, "is1", os.path.join(tmp, "free"))
        rows = rows_of(os.path.join(tmp, "free"), cell)
        check("1 a free store: rc 0 and every row ok", rc == 0 and rows and all(r["ok"] == "1" for r in rows),
              f"rc {rc}, {len(rows or [])} rows")
        store = GV.meta_store(SF, "interactive", os.path.join(GV.GRID, f"{cell}.json"), build=False)
        m = json.load(open(os.path.join(store, "manifest.json")))
        held = os.path.join(tmp, "held")
        # 2. the store held open by a server
        schema = GV.schema_file("interactive")
        with server.CppServer(store, m["format"], schema=schema) as srv:
            r = srv.request({"store": {"dir": os.path.abspath(store), "format": m["format"],
                                       "schema": os.path.abspath(schema)}})
            # the hold must hold: the server opened the store (else case 2 tests nothing); a request with no plan opens
            # the store, keeps it open, then answers with a 'plan' error
            err = (r or {}).get("error") or {}
            check("2 the second process holds the store open", err.get("where") == "plan", str(err)[:160])
            rc, log = run_cell(cell, "is2", held)
        rows = rows_of(held, cell)
        fail_line = [ln for ln in log.splitlines() if ln.startswith(f"[grid_bodies] FAIL {cell}")]
        check("2 a held store: rc 1", rc == 1, f"rc {rc}")
        check("2 a FAIL line names the cell and the store error", fail_line and "error (store)" in fail_line[0],
              (fail_line or ["no FAIL line"])[0][:160])
        check("2 no counts file (nothing to reuse)", rows is None,
              "absent" if rows is None else f"{len(rows)} rows, ok {sum(r['ok'] == '1' for r in rows)}, "
              f"first error {rows[0].get('error', '')[:100]!r}")
        # 3. released: the same command, no --fresh
        rc, log = run_cell(cell, "is2", held)
        rows = rows_of(held, cell)
        check("3 released: rc 0, measured, not reused",
              rc == 0 and "-- reused" not in log and rows and all(r["ok"] == "1" for r in rows),
              f"rc {rc}, reused {'-- reused' in log}, {len(rows or [])} rows, ok {sum(r['ok'] == '1' for r in rows or [])}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"check_store_error_fails_cell: FAILED {len(fails)} of {checked}: {fails}")
        return 1
    print(f"check_store_error_fails_cell: OK ({checked} assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
