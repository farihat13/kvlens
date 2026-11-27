#!/usr/bin/env python3
"""The chooser on a named store (python/kvcost/layout_cost/grid_pick_record.py: stores_in + decide). Live, at sf0.1: one
named format's every-body capture of one query (params 1-2, and the held-out 11-12 in the capture's held-out set),
filed as kvcost measure <named format> files it (<captures dir>/interactive/{reported,held-out}/counts/<store dir name>.csv,
beside the cells; this check builds it in a temp tree), then the held-out rows planted
so one body (`cheap`) is 5% cheaper than another (`was`) and every other body is 10x dearer:

  1. stores_in maps the capture's store dir name to the record's key (the same name: pertype_csr-c64k);
  2. a carried entry (v1's selection, rule carried_v1) on `was`: no anchor, `cheap` is picked (the cheapest);
  3. a decided entry on `was`: the anchor, kept (`cheap` is within the 10% margin);
  4. a decided entry on `was` with `cheap` at half its cost: replaced by `cheap` (over the margin).

    python3 tests/gates/meta/check_chooser_named.py [--format pertype_csr] [--query is7]
    python3 tests/gates/meta/check_chooser_named.py --names      # the store dir it reads (for store_copies.py make)
"""
import argparse
import csv
import os
import shutil
import sys
import tempfile

from kvevaluator.paths import OUTPUT_DIR, ROOT
from kvevaluator.measure import grid_bodies as GV
from kvevaluator.measure import grid_capture
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plan_choice import pick_record

import importlib.util
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import capture_in

_spec = importlib.util.spec_from_file_location("grid_pins", os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
GP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GP)

SF, REPORTED, HELD = "0.1", [1, 2], [11, 12]


def plant(path, q, cost):
    """the held-out rows of q priced by `cost` {body: G} (S, N and the byte lanes 0); other queries' rows as they are"""
    rows = list(capture_layout.reader(open(path)))
    for r in rows:
        if r["query"] == q and r["body"] in cost and str(r["ok"]) == "1":
            r.update({"S": "0", "N": "0", "G": str(cost[r["body"]])})
            r.update({k: "0" for k in C.READ_LANES if k in r})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", default="pertype_csr")
    ap.add_argument("--query", default="is7")
    ap.add_argument("--names", action="store_true", help="print the store dir name this check reads, and exit")
    a = ap.parse_args()
    fmt, q = a.format, a.query
    if a.names:
        print(os.path.basename(grid_capture.format_store(SF, "interactive", fmt, build=False)))
        return 0
    binary = GV.binary_path()
    stem = os.path.basename(grid_capture.format_store(SF, "interactive", fmt))   # the store's dir name: the capture's file
    tmp = tempfile.mkdtemp(dir=str(OUTPUT_DIR), prefix="check_chooser_named_")
    rep = capture_in(tmp, "interactive")     # a captures dir of its own
    named = {"reported": rep, "held-out": rep.held_out()}
    ok = True

    def check(what, got, want):
        nonlocal ok
        ok &= got == want
        print(f"{'PASS' if got == want else 'FAIL'} {what}: {got!r}" + ("" if got == want else f" (want {want!r})"))

    try:
        for out, params in ((named["reported"].path, REPORTED), (named["held-out"].path, HELD)):
            GV.measure_cell(stem, "interactive", SF, max(params), 4, 600, binary, out,
                            GV.inputs_fingerprint("interactive", SF, max(params), params), GV.GRID, "serve", ([q], params),
                            fmt=fmt)
        under = GP.stores_in(rep.path)
        check("1 stores_in: (record key, capture stem)", under, [(stem, stem)])
        key, froot = under[0][0], rep.path
        held = named["held-out"].path_of("counts", stem)
        ok_bodies = sorted({r["body"] for r in capture_layout.reader(open(held)) if r["query"] == q and str(r["ok"]) == "1"
                            and r.get("measure_only", "0") != "1"})
        if len(ok_bodies) < 2:
            errs = sorted({r["error"][:160] for r in capture_layout.reader(open(held)) if str(r["ok"]) != "1"})
            raise SystemExit(f"precondition: {q} on {stem} needs two bodies that run ({ok_bodies}); errors: {errs}")
        was, cheap = ok_bodies[0], ok_bodies[1]
        crit = None                              # grid_pins' default: the record's criterion

        def decide(cost, incumbent):
            plant(held, q, {b: cost.get(b, 10_000) for b in ok_bodies})
            return GP.decide(froot, stem, crit, {q: incumbent}, [], workload="interactive")[q]["body"]

        near = {was: 1000, cheap: 950}
        carried = {"body": was, "rule": pick_record.CARRIED}
        decided = {"body": was, "rule": "held_out", "body_sha": None}
        check(f"2 a carried entry on {was!r}: the cheapest", decide(near, carried), cheap)
        check(f"3 a decided entry on {was!r}, {cheap!r} 5% cheaper: kept", decide(near, decided), was)
        check(f"4 a decided entry on {was!r}, {cheap!r} at half: replaced", decide({was: 1000, cheap: 500}, decided), cheap)
        print(f"check_chooser_named: {'OK' if ok else 'FAILED'} ({stem} as {key}, {q}, {len(ok_bodies)} bodies, "
              f"held-out params {HELD})")
    finally:
        for cap in named.values():
            shutil.rmtree(GV.journal_dir(cap.path), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
