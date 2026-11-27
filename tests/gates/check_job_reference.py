#!/usr/bin/env python3
"""The JOB workload's correctness check: every body of every query, run on the C++ evaluator's stores of a data tag,
against the reference (DuckDB on the same merged tables: workloads/job/dataset/reference.py).

Per (store, query, body): the MIN columns equal the reference's answer; the entry's row count equals the
reference's count of that alias's rows; and EVERY alias's final row count equals the reference's
COUNT(DISTINCT alias.id) over the join (an alias seen only through a MIN, or through no output at all, is checked
too; on the 5 queries whose answer is empty every count must be 0). A body the store's capabilities do not allow
(a label-run body where a label is not one key run) is not run and counted as such.
    python3 tests/gates/check_job_reference.py --tag sample1pct [--stores cells,dgraph,nebula,arangodb]
                                                   [--queries job1a,job2a] [--default-only]
--stores: `cells` = the nine uniform cells of the configurable format; a cell name; or a named format.
--from-capture: run nothing; read the counts capture of the tag (python/kvevaluator/measure/grid_bodies.py: the cells, and the
named formats under captures/formats/job/reported/) and compare EVERY captured run's result hash with the hash of the reference's row: the MIN columns and every alias's
final row count (a plan returns both; the entry's own row count is a free step the capture does not keep: the
direct mode checks it). The check of the full tag: the
capture's own executions are the ones checked.
Exit 0 = every body equal on every store; prints the (store, query, body) cells covered (0 = FAIL).
"""
import argparse
import glob
import json
import os
import sys
import time
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)

from kvevaluator.measure import server, stores
from kvevaluator.plan_choice.candidates import forced, invalid_candidates
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.datasets.imdb.schema import get_schema
from kvevaluator.workloads.job import queries as job_q
from kvevaluator.datasets.imdb import paths
from kvevaluator.capture import capture_layout

GRID = os.path.join(EXP, "inputs", "specs", "grid")
PLANS = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "job", "plans")


def stores_of(names):
    out = []
    for n in names:
        if n == "cells":
            out += [("meta", os.path.basename(p)[:-5]) for p in sorted(glob.glob(os.path.join(GRID, "*.json")))]
        elif os.path.exists(os.path.join(GRID, f"{n}.json")):
            out.append(("meta", n))
        else:
            out.append((n, None))
    return out


def from_capture(tag, ref, entry, finals, root=None):
    """every run of the tag's counts capture against the reference; (cells covered, failures)"""
    import hashlib
    from kvevaluator.output_tree import Tree
    grid = Tree(root).capture("job", tag)
    # the row every body of a query returns: its MIN columns and each alias's final row count
    want_hash = {q: hashlib.sha1(json.dumps([{**dict(zip(job_q.PATTERNS[q].output_names, ans)),
                                              **{f"rows_{a_}": n for a_, n in finals[q].items()}}], sort_keys=True).encode()).hexdigest()[:16]
                 for q, ans in ref.items()}
    fails, covered = [], 0
    for name in grid.stores():                       # the layouts and the named formats, side by side
        runs = grid.path_of("counts", name)
        n = 0
        for r in capture_layout.reader(open(runs)):
            q, body = r["query"], r["body"]
            if r["ok"] != "1":
                if r["error"].startswith("invalid on this cell"):     # a body the store's capabilities do not allow:
                    continue                                          # recorded by the capture, never run
                fails.append(f"{name} {q} {body}: the run failed: {r['error'][:120]}")
                continue
            n += 1
            if r["result_hash"] != want_hash[q]:
                fails.append(f"{name} {q} {body}: result hash {r['result_hash']} != the reference row's {want_hash[q]} "
                             f"(the MIN columns and every alias's final row count)")
        covered += n
        print(f"  {name}: {n} captured runs checked", flush=True)
    return covered, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, choices=paths.TAGS)
    ap.add_argument("--stores", default="cells,dgraph,nebula,arangodb")
    ap.add_argument("--queries", default="")
    ap.add_argument("--default-only", action="store_true", help="only each query's first body")
    ap.add_argument("--from-capture", action="store_true", help="check the tag's counts capture instead of running")
    ap.add_argument("--capture", default=None, help="with --from-capture: the capture's own root (default: the tag's "
                    "working capture); a snapshot's unpacked meta-grid")
    ap.add_argument("--reference", default=None, help="the dir of reference_answers.csv, entry_counts.csv and "
                    "alias_counts.csv (default: the tag's data dir)")
    a = ap.parse_args()
    stores.dataset_defaults("imdb")      # the Dgraph format at its reported 512 KB split
    tdir = a.reference or paths.tag_dir(a.tag)
    ref = {r["query"]: json.loads(r["answer"]) for r in capture_layout.reader(open(os.path.join(tdir, "reference_answers.csv")))}
    entry = {(r["query"], r["alias"]): int(r["rows"]) for r in capture_layout.reader(open(os.path.join(tdir, "entry_counts.csv"))) if r["test"] == "all"}
    finals = {}
    for r in capture_layout.reader(open(os.path.join(tdir, "alias_counts.csv"))):
        finals.setdefault(r["query"], {})[r["alias"]] = int(r["rows"])
    if a.from_capture:
        t0 = time.time()
        covered, fails = from_capture(a.tag, ref, entry, finals, a.capture)
        for f in fails[:20]:
            print("  FAIL", f)
        ok = not fails and covered > 0
        print(f"[job-reference] {a.tag}, from the capture: {covered} (store, query, body) runs, {len(fails)} failed -> "
              f"{'OK' if ok else 'FAIL'} ({time.time() - t0:.0f} s)")
        sys.exit(0 if ok else 1)
    todo = [q for q in a.queries.split(",") if q] or job_q.QUERIES
    pyschema = get_schema("canonical")
    schema = stores.schema_artifact("imdb")
    fails, covered, skipped, t0 = [], 0, 0, time.time()
    for fmt, cell in stores_of(a.stores.split(",")):
        spec = os.path.join(GRID, f"{cell}.json") if cell else None
        store = stores.ensure_store("imdb", a.tag, fmt, spec=spec)
        name, n, t1 = cell or fmt, 0, time.time()
        caps, of_label = caps_for(fmt, pyschema, spec=spec), caps_resolver(fmt, pyschema, spec=spec)   # meta: its spec
        with server.CppServer(store, fmt, schema=schema) as srv:
            for q in todo:
                ir = json.load(open(os.path.join(PLANS, f"{q}.json")))
                outs = job_q.PATTERNS[q].output_names
                invalid = invalid_candidates("job", q, caps, of_label)
                for body in list(ir["plans"])[:1 if a.default_only else None]:
                    if body in invalid:
                        skipped += 1
                        continue
                    alias = body[len("enter_"):].split("_by_")[0]
                    try:
                        resp = srv.run(forced(ir, body), {})
                        got = [resp["rows"][0][o] for o in outs]
                        got_entry = next(s["rows_in"] for s in resp["steps"] if s["name"].endswith("_entry_n"))
                        got_final = {k[len("rows_"):]: v for k, v in resp["rows"][0].items() if k.startswith("rows_")}
                    except Exception as ex:        # a body that fails to run is a failed cell, named
                        fails.append(f"{name} {q} {body}: ERROR {str(ex)[-200:]}")
                        continue
                    n += 1
                    if got != ref[q]:
                        fails.append(f"{name} {q} {body}: answer {got} != reference {ref[q]}")
                    elif got_entry != entry[(q, alias)]:
                        fails.append(f"{name} {q} {body}: entry rows {got_entry} != reference {entry[(q, alias)]}")
                    elif got_final != finals[q]:
                        diff = {a_: (got_final.get(a_), finals[q].get(a_)) for a_ in sorted(set(got_final) | set(finals[q]))
                                if got_final.get(a_) != finals[q].get(a_)}
                        fails.append(f"{name} {q} {body}: final rows per alias (plan, reference) differ: {diff}")
        covered += n
        print(f"  {name}: {n} bodies of {len(todo)} queries run ({time.time() - t1:.0f} s)", flush=True)
    for f in fails[:20]:
        print("  FAIL", f)
    ok = not fails and covered > 0
    print(f"[job-reference] {a.tag}: {covered} (store, query, body) cells, {len(fails)} failed, {skipped} bodies not valid on "
          f"their store (not run) -> {'OK' if ok else 'FAIL'} ({time.time() - t0:.0f} s)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
