#!/usr/bin/env python3
"""Ingest the configurable format's stores of one or more spec sets that are not there yet, several at a time.

A driver's cell loop builds each store when it reaches the cell, one after another, and every ingest reads the whole
record stream again (SF3: 4-5 min per store). This builds the missing ones first, --jobs at a time, each in its own
process (its own `kvevaluator ingest` over the cached record stream); the driver then finds every store present.
A store already there is checked the way every tool checks one (store_staleness: its key-layout stamp, that
it settled, the config this binary writes for its spec): a stale one is named with the reason and NOT rebuilt (never
re-ingest silently; move it aside, or reingest_stores.py), so a run does not find it minutes in. Its index + filter
layout is not checked here: a whole store opens and measures, and the latency drivers and the round refuse it
themselves. Prints, per store, whether it was built, its wall time and the ingest process's peak memory, in the spec
order; exit 1 when an ingest failed or a store is stale.

    python3 -m kvevaluator.measure.ensure_stores --sf 0.1 [--jobs 4] [--dataset ldbc] [--spec-dir DIR]... [--cells a,b] [--names]
--spec-dir defaults to the grid (inputs/specs/grid). The worker count is a memory choice: jobs x the peak this
prints (measure it at the scale of the run before raising --jobs there).
"""
import argparse
import concurrent.futures
import glob
import multiprocessing
import os
import resource
import sys
import time
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import grid_capture
from kvevaluator.measure import records
from kvevaluator.capture import store_identity
from kvevaluator.measure.stores import store_staleness
from kvevaluator import datasets, workloads


def build(sf, workload, spec):
    """one store, in a worker that builds nothing else: (wall seconds, the ingest process's peak RSS in MB)"""
    t0 = time.time()
    grid_capture.meta_store(sf, workload, spec)
    return time.time() - t0, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", required=True, help="the scale (LinkBench: its scale tag)")
    ap.add_argument("--dataset", default="ldbc", choices=datasets.names(), help="whose stores (its scale axis)")
    ap.add_argument("--spec-dir", action="append", default=[], help="a dir of spec files (repeatable; default the grid)")
    ap.add_argument("--cells", default=None, help="comma list of spec names (default: every spec of the dirs)")
    ap.add_argument("--jobs", type=int, default=4, help="ingests at the same time")
    ap.add_argument("--names", action="store_true", help="print the stores' names, one per line, and build nothing")
    ap.add_argument("--fresh-records", action="store_true", help="LDBC: move the record cache aside and dump it again "
                    "(once, here, before any ingest; records.records_file refuses a cache whose freshness key moved)")
    a = ap.parse_args()
    a.workload = workloads.names(kind="read", dataset=a.dataset)[0]    # the dataset's stores, as its first read workload opens them
    dirs = [os.path.abspath(d) for d in a.spec_dir] or [grid_capture.GRID]
    specs = [p for d in dirs for p in sorted(glob.glob(os.path.join(d, "*.json")))]
    if a.cells:
        want = set(a.cells.split(","))
        unknown = sorted(want - {os.path.basename(p)[:-5] for p in specs})
        if unknown:
            raise SystemExit(f"--cells: no spec named {unknown} in {dirs}")
        specs = [p for p in specs if os.path.basename(p)[:-5] in want]
    if not specs:
        raise SystemExit(f"no spec files in {dirs}")
    stores = {p: grid_capture.meta_store(a.sf, a.workload, p, build=False) for p in specs}
    # one store per path: a spec found in two dirs is built once; two different specs for one path are refused
    first, dups = {}, []
    for p in specs:
        q = first.setdefault(stores[p], p)
        if q != p:
            with open(q, "rb") as f1, open(p, "rb") as f2:
                if f1.read() != f2.read():
                    raise SystemExit(f"two different specs give one store {os.path.basename(stores[p])}: {q} and {p}")
            dups.append(p)
    specs = [p for p in specs if p not in dups]
    if a.names:
        print("\n".join(os.path.basename(stores[p]) for p in specs))
        return 0
    state = {p: store_staleness(stores[p], p, a.workload, layout=False)
             for p in specs}
    missing = [p for p in specs if state[p][0] == store_identity.MISSING]
    stale = [p for p in specs if state[p][0] == store_identity.STALE]
    t0 = time.time()
    done, failed = {}, []
    if missing:
        # the dataset's record stream (records.records_file), dumped once here, never by two workers at the same time
        if a.fresh_records:            # here only: a worker that saw the variable would move a fresh cache aside
            from kvevaluator.measure import records_key
            os.environ[records_key.FRESH_ENV] = "1"
            try:
                records.records_file(a.dataset, a.sf)
            finally:
                del os.environ[records_key.FRESH_ENV]
        records.records_file(a.dataset, a.sf)
        from kvevaluator import stale as marks
        stream_notes = [n for n in marks.notes() if n["kind"] == "stream_keyed_by_content"]   # on every store built below
        # a fresh process per store (max_tasks_per_child=1): its children's peak RSS is that one ingest's
        with concurrent.futures.ProcessPoolExecutor(a.jobs, mp_context=multiprocessing.get_context("spawn"),
                                                    max_tasks_per_child=1) as pool:
            jobs = {p: pool.submit(build, a.sf, a.workload, p) for p in missing}
            for p, job in jobs.items():
                try:
                    done[p] = job.result()
                except BaseException as e:   # a worker's SystemExit (a failed ingest) arrives here
                    failed.append((p, e))
    for p in specs:
        name = os.path.basename(stores[p])
        if p in done:
            print(f"  {name}: built in {done[p][0]:.0f} s, peak RSS of its ingest {done[p][1]:,.0f} MB")
            for n in stream_notes:   # the store's own notes (<store>/stale.jsonl): its run records fold them in
                marks.note_into(stores[p], n["kind"], n["detail"], at=n["at"])
        elif p in stale:
            print(f"  {name}: STALE, not rebuilt: {state[p][1]} (move it aside, or "
                  f"python3 -m kvevaluator.measure.reingest_stores --sf {a.sf} {name})")
        elif p not in missing:
            print(f"  {name}: present")
    for p, e in failed:
        print(f"  {os.path.basename(stores[p])}: INGEST FAILED: {e}")
    peak = max((v[1] for v in done.values()), default=0)
    print(f"[ensure_stores] {a.dataset} {a.sf}: {len(specs)} stores, {len(done)} built ({a.jobs} at a time, "
          f"{time.time() - t0:.0f} s wall, {sum(v[0] for v in done.values()):.0f} s summed, largest ingest peak {peak:,.0f} MB), "
          f"{len(specs) - len(missing) - len(stale)} present, {len(stale)} stale, {len(failed)} failed")
    return 1 if failed or stale else 0


if __name__ == "__main__":
    sys.exit(main())
