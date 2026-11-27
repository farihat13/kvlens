#!/usr/bin/env python3
"""The JOB workload's C++-against-Python check: each query's plan run on the Python oracle (the in-memory twin of a
named format, loaded from the sample tag's files) and on the C++ store, rows and per-step S / G / N compared.
The sibling of check_cpp_vs_python.py (which is bound to the LDBC data and stores); it uses that check's own cell
comparison (diff_cell), so the two cannot judge a cell differently.

    python3 tests/gates/check_job_cpp_vs_python.py [--tag sample1pct] [--format nebula] [--queries job1a job2a | --gate]
                                                       [--all-bodies]
Default: each query's first body (113 cells per format). --gate: the round's 16 queries (GATE_QUERIES; `just cpp-diff`,
lane job). --all-bodies: every body of every query (the scans of a large label take minutes each on the Python side).
Exit 0 = every cell equal and, one body per query, one cell per query; prints the cells compared (fewer = FAIL).
"""
import argparse
import json
import os
import sys
import time
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))

from check_cpp_vs_python import diff_cell  # (the one cell comparison)
from kvevaluator.measure import server, stores
from kvevaluator.plan_choice.candidates import forced
from kvevaluator.cost import TwinCountsRefused
from kvevaluator.formats import make_format
from kvevaluator.oracle.interp import run_ir
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.oracle.run import open_store
from kvevaluator.datasets.imdb import loader
from kvevaluator.workloads.job import queries as job_q
from kvevaluator.datasets.imdb import paths
from kvevaluator.datasets.imdb.schema import get_schema

PLANS = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "job", "plans")
# the round's slice: a greedy cover of the 113 first bodies' features (each index read, neighbour read, label property
# read and label scan, each operator, each expression operator: 128 features, 15 queries) + job5a, an empty answer
GATE_QUERIES = ("job3a", "job5a", "job7a", "job7c", "job10a", "job11a", "job12a", "job13b", "job15b", "job15d", "job16a",
                "job18a", "job18c", "job19b", "job20b", "job31b")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="sample1pct", choices=paths.TAGS)
    ap.add_argument("--format", default="nebula", choices=("dgraph", "nebula", "arangodb"))
    ap.add_argument("--queries", nargs="*", default=None)
    ap.add_argument("--gate", action="store_true", help="the round's slice (GATE_QUERIES)")
    ap.add_argument("--all-bodies", action="store_true")
    a = ap.parse_args()
    if a.gate and a.queries:
        sys.exit("--gate names its own queries: not with --queries")
    todo = GATE_QUERIES if a.gate else (a.queries or job_q.QUERIES)
    if a.tag != "sample1pct":
        sys.exit("the Python oracle is a small-scale oracle: run it on sample1pct")
    # the Python twin of Dgraph holds unbounded postings: its counts compare with the UNCHUNKED C++ store (named
    # `dgraph`), an oracle-only store beside the reported dgraph-c512k
    os.environ["KVEVAL_DGRAPH_CHUNK"] = "0"
    t0 = time.time()
    schema = get_schema("canonical")
    fmt = make_format(a.format, schema, True)
    store = open_store(fmt, "abstract", a.tag, lambda: loader.load(paths.tag_dir(a.tag)))
    caps, of_label = caps_for(a.format, schema), caps_resolver(a.format, schema)
    print(f"[ref] the Python {a.format} store of {a.tag} ready in {time.time() - t0:.0f} s", flush=True)
    cpp_store = stores.ensure_store("imdb", a.tag, a.format)
    ok, bad = 0, []
    with server.CppServer(cpp_store, a.format, schema=stores.schema_artifact("imdb")) as srv:
        for q in todo:
            bundle = json.load(open(os.path.join(PLANS, f"{q}.json")))
            for body in list(bundle["plans"])[:None if a.all_bodies else 1]:
                ir = forced(bundle, body)
                try:
                    outs, kv = run_ir(ir, fmt, store, params={}, format_caps=caps, caps_of_label=of_label, scale=a.tag)
                    py_steps = [(s.out, s.op, c) if isinstance(c, TwinCountsRefused) else
                                (s.out, s.op, c.S, c.G, c.N, c.P, c.D, c.S_miss, c.G_miss, c.N_miss, c.get_value_bytes,
                                 c.scan_value_bytes, c.write_key_bytes, c.write_value_bytes) for s, _p, c in kv]
                    reason = diff_cell(py_steps, outs[bundle["plans"][body]["output"]], srv.run(ir, {}, run={"P": 1}),
                                       check_bytes=False, check_counts=True)
                except Exception as e:  # (a cell that cannot run is a failed cell, named)
                    reason = f"error: {type(e).__name__}: {str(e)[:200]}"
                if reason is None:
                    ok += 1
                else:
                    bad.append(f"{q} {body}: {reason}")
    for b in bad[:20]:
        print("  MISMATCH", b[:400])
    expected = None if a.all_bodies else len(todo)        # one body per query: one cell per query
    good = not bad and ok > 0 and (expected is None or ok == expected)
    print(f"[job cpp-vs-python] {a.tag} {a.format}: {ok} ok, {len(bad)} mismatch"
          + (f" of {expected} expected" if expected is not None else "")
          + f" -> {'OK' if good else 'FAIL'} ({time.time() - t0:.0f} s)")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
