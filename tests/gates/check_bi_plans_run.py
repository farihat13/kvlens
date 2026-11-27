#!/usr/bin/env python3
"""Every BI plan body runs on every store it is meant for: a C++-only smoke, one param each, that fails on any step
error (bi10 named no label on its by-id read, so ArangoDB refused it; the cpp-diff BI lanes run Dgraph and
NebulaGraph only, so no gate saw it).

The bodies: candidates("bi", q) for every BI query (its bundle's bodies and its twins'; a single plan runs as itself).
The stores, per body:
  - the named formats (FORMATS) where the body is legal (pick_record.legal_bodies over the store's capabilities: what
    the record may name there), on their sf0.1 stores;
  - the nine meta cells, every body the cell may run (grid_capture.invalid_on_cell: what the grid itself runs there).
A refusal at run time ("not runnable", a missing capability) on a store the body is meant for is a failure too.
Prints the runs and wall per store (--jobs stores at once, one process each) and the total wall. Exit 1 on any
failure, or a store with 0 runs.

    python3 tests/gates/check_bi_plans_run.py [--sf 0.1] [--queries bi10,bi1]
    python3 tests/gates/check_bi_plans_run.py --names      # the store dirs it reads (for store_copies.py make)
"""
import argparse
import glob
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import server, stores
from kvevaluator.plan_choice.candidates import candidates
from kvevaluator.workloads.bi.params import query_params
from kvevaluator.measure import grid_capture
from kvevaluator.capture import spec_sets
from kvevaluator.plan_choice import cells

FORMATS = ("dgraph", "nebula", "arangodb", "pertype_csr")   # the sf0.1 stores a `for` can name that this gate runs
PLANS = os.path.join(EXP, "python", "kvevaluator", "workloads", "bi", "plans")


def format_store_name(fmt):
    """the default sf0.1 store of a named format (canonical schema, no codec, its default chunk)"""
    return stores.cpp_store_name(fmt, "canonical", None, stores.store_chunk(fmt))


def cell_specs():
    return sorted(glob.glob(os.path.join(grid_capture.GRID, "*.json")))


def cell_store_name(sf, spec):
    return os.path.basename(grid_capture.meta_store(sf, "interactive", spec, build=False))


def format_legal(fmt):
    """{query: the bodies legal on a named format's store} (pick_record.legal_bodies over its capabilities)"""
    from kvevaluator.plan_choice import pick_record
    caps, of_label = cells.store_caps("bi", spec_sets.format_spec(fmt), stores.store_chunk(fmt))
    out = {}
    for q in grid_capture.workload_queries("bi")[0]:
        ir, _ = candidates("bi", q)
        out[q] = set(pick_record.legal_bodies(ir, caps, of_label)) if "plans" in ir else {None}
    return out


def run_store(job):
    """every (query, body) of one store, one param each: (label, runs, failures, wall s)"""
    sf, (label, name, fmt, spec), work = job
    t0 = time.time()
    n, fails = 0, []
    with server.CppServer(stores.store_dir("ldbc", sf, name, spec), fmt) as srv:
        for q, cname, cir in work:
            for p in query_params(sf, q, 1):
                r = srv.run(cir, p, run={"P": 1})
                n += 1
                if r.get("error"):
                    fails.append(f"{label} {q} {cname}: {r['error']}")
    return label, n, fails, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--queries", default=None, help="comma list (default: every BI query)")
    ap.add_argument("--jobs", type=int, default=6, help="stores run at once (one process each)")
    ap.add_argument("--names", action="store_true", help="print the store dir names this check reads, and exit")
    a = ap.parse_args()
    if a.names:
        print(" ".join([format_store_name(f) for f in FORMATS] + [cell_store_name(a.sf, s) for s in cell_specs()]))
        return 0
    t0 = time.time()
    queries = a.queries.split(",") if a.queries else grid_capture.workload_queries("bi")[0]
    # (store label, store dir, format, spec or None) -> [(query, body name, IR)]
    jobs = defaultdict(list)
    specs = cell_specs()
    invalid = {s: grid_capture.invalid_on_cell("bi", s) for s in specs}
    legal = {f: format_legal(f) for f in FORMATS}
    for q in queries:
        ir, cands = candidates("bi", q)
        runs = [("(plan)", ir, None)] if "plans" not in ir else [(cname, cir, cname) for cname, cir in cands]
        for cname, cir, body in runs:
            for fmt in FORMATS:                              # a named format: every body legal on its store
                if body in legal[fmt][q]:
                    jobs[(fmt, format_store_name(fmt), fmt, None)].append((q, cname, cir))
            for s in specs:
                if cname in invalid[s].get(q, {}):
                    continue
                cell = os.path.basename(s)[:-5]
                jobs[(cell, cell_store_name(a.sf, s), "meta", s)].append((q, cname, cir))
    for (label, name, fmt, spec), work in sorted(jobs.items()):
        if not os.path.exists(os.path.join(stores.store_dir("ldbc", a.sf, name), "manifest.json")):
            raise SystemExit(f"[bi-plans-run] {label}: no store at {stores.store_dir("ldbc", a.sf, name)} "
                             f"(just gate-prep {a.sf})")
    # one process per store, --jobs at once
    with ProcessPoolExecutor(max_workers=a.jobs) as pool:
        done = list(pool.map(run_store, [(a.sf, key, work) for key, work in sorted(jobs.items())]))
    fails, n_runs = [], {}
    for label, n, f, secs in done:
        n_runs[label] = n
        fails += f
        print(f"  {label}: {n} runs, {secs:.1f} s")
    for f in fails[:20]:
        print("  FAIL", f)
    empty = sorted(k[0] for k in jobs if not n_runs.get(k[0]))
    print(f"[bi-plans-run] sf{a.sf}: {len(queries)} queries, {sum(n_runs.values())} runs on {len(n_runs)} stores "
          f"{dict(sorted(n_runs.items()))}; stores with 0 runs: {empty}; "
          f"{time.time() - t0:.0f} s -> {'OK' if not fails and not empty and n_runs else f'FAIL ({len(fails)})'}")
    return 0 if not fails and not empty and n_runs else 1


if __name__ == "__main__":
    sys.exit(main())
