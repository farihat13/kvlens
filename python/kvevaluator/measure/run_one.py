"""Run one query once on one store and print what the C++ evaluator answers: each step's counts, then the answer rows.

    python -m kvevaluator.measure.run_one --sf 0.1 --family <a read workload> --store nebula --query is2
        [--param 1] [--body two_phase] [--answer-rows 10]

--store is a named format (dgraph, nebula, arangodb, pertype_csr, a chunk arm such as dgraph-c512k) or a layout
(Sp-Co, Co-Ch4k, props_condensed_adj_chunked, a spec file: layouts.resolve). A missing store is built first (seconds
at sf0.1). The body is the one the pick record names for this store, the query's only body when it has one, or
--body. The request is grid_bodies.request(), the one the grid's captures send. It writes nothing but a missing store,
and checks the query, the store name, the body and the param row before it builds one.
It opens the store, so it cannot run beside a job that holds that store's LOCK (an ingest, a capture, a gate).
"""
import argparse
import json
import os
import sys

from kvevaluator.measure import grid_bodies
from kvevaluator.capture.binary import binary_path, binary_provenance
from kvevaluator.measure.grid_capture import WORKLOADS, format_store, meta_store, query_params, request_format, schema_file
from kvevaluator.capture.spec_sets import format_spec
from kvevaluator.measure.plan_runs import record_store_key
from kvevaluator.plan_choice import pick_record
from kvevaluator.plan_choice.candidates import candidates, plan_files, plans_dir
from kvevaluator.plan_choice.optimizer.arms import base_format


def is_layout(text):
    """a layout name (resolved by layouts.resolve), not a named format's"""
    return text.endswith(".json") or text.startswith("props_") or text[:3] in ("Sp-", "Co-", "Ch-")


def checked_spec(workload, query, store):
    """the store's spec path, after the query and the store name are checked (each refused by name)"""
    if not os.path.exists(plan_files(workload, query)[0]):
        known = sorted(f[:-len(".json")] for f in os.listdir(plans_dir(workload)) if f.endswith(".json"))
        sys.exit(f"unknown query {query!r} for workload {workload} (known: {', '.join(known)})")
    if is_layout(store):
        from kvevaluator.layouts import resolve
        return resolve(store).spec
    from kvevaluator.capture.store_identity import key_layouts
    named = sorted(f for f in key_layouts() if f != "meta")   # the binary's named formats
    if base_format(store) not in named:
        sys.exit(f"unknown store {store!r}: a named format ({', '.join(named)}, or one with a chunk tag such as "
                 f"dgraph-c512k) or a layout: <props>-<adjacency>, each Sp (Split), Co (Condensed) or Ch<K>k (Chunked at K KB), "
                 f"e.g. Sp-Co or Co-Ch4k, or a spec file")
    return format_spec(store)


def refuse_bad_rows(a, cands):
    """refuse a --param below 1 (it would index from the end) and a --body the query does not have"""
    if a.param < 1:
        sys.exit(f"--param is a 1-based row of the query's parameter file; asked for row {a.param}")
    if a.body is not None and a.body not in {c for c, _ in cands}:
        sys.exit(f"--body {a.body!r} is not a body of {a.query} (its bodies: {', '.join(c for c, _ in cands)})")


def recorded_body(a, cands, fmt, store, spec_path):
    """the body to run with no --body: the query's only one, else the pick record's for this store"""
    if len(cands) == 1:
        return cands[0][0]
    e = pick_record.entry(a.workload, record_store_key(fmt, store, spec_path), a.query, a.sf)
    if e is None:
        sys.exit(f"no pick recorded for {os.path.basename(store)} / {a.workload} {a.query}: name a body with "
                 f"--body (one of: {', '.join(c for c, _ in cands)})")
    return e["body"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--family", dest="workload", required=True, choices=sorted(WORKLOADS))
    ap.add_argument("--store", required=True, help="a named format or a layout (Sp-Co, props_..., a spec file)")
    ap.add_argument("--query", required=True)
    ap.add_argument("--param", type=int, default=1, help="the 1-based row of the query's parameter file")
    ap.add_argument("--body", default=None, help="the body to run (default: the pick record's for this store)")
    ap.add_argument("--answer-rows", type=int, default=10, help="how many answer rows to print (0: all)")
    a = ap.parse_args(argv)

    prov = binary_provenance()
    print(f"binary {binary_path()} sha {str(prov.get('binary_git_sha', '?'))[:12]} dirty {prov.get('binary_dirty')}",
          flush=True)
    spec_path = checked_spec(a.workload, a.query, a.store)   # before a missing store is built (seconds to minutes)
    fmt = request_format(spec_path)

    ir, cands = candidates(a.workload, a.query)
    refuse_bad_rows(a, cands)
    store = (meta_store(a.sf, a.workload, spec_path) if is_layout(a.store)
             else format_store(a.sf, a.workload, a.store))
    if not cands:
        cands = [(ir.get("plan", a.query), ir)]
    body = a.body if a.body is not None else recorded_body(a, cands, fmt, store, spec_path)
    [(body, body_ir)] = grid_bodies.pick_runs(a.query, body, ir, cands)
    params = query_params(a.workload, a.sf, a.query, a.param)
    if len(params) < a.param:
        sys.exit(f"{a.query} has {len(params)} params at sf{a.sf}; asked for row {a.param}")
    p = params[a.param - 1]

    req = grid_bodies.request(store, schema_file(a.workload), body_ir, p, 1, fmt)
    runner = grid_bodies.SpawnRunner(binary_path(), store, schema_file(a.workload), timeout=600, fmt=fmt)
    resp, err = runner.call(req)
    if resp is None:
        sys.exit(f"run failed: {err}")

    print(f"{a.workload} {a.query} param {a.param} {json.dumps(p, sort_keys=True)}")
    print(f"store {store} ({fmt}), body {body}")
    print(f"{'step':<16} {'op':<22} {'S':>6} {'G':>6} {'N':>7} {'get_value':>10} {'scan_value':>11} {'rows':>6}")
    tot = [0, 0, 0, 0, 0]
    for s in resp["steps"]:
        b = s.get("bytes", {})
        vals = [s.get("S", 0), s.get("G", 0), s.get("N", 0), b.get("get_value", 0), b.get("scan_value", 0)]
        tot = [x + y for x, y in zip(tot, vals)]
        print(f"{s['name']:<16} {s['op']:<22} {vals[0]:>6} {vals[1]:>6} {vals[2]:>7} {vals[3]:>10} {vals[4]:>11} "
              f"{s.get('rows', ''):>6}")
    print(f"{'total':<16} {'':<22} {tot[0]:>6} {tot[1]:>6} {tot[2]:>7} {tot[3]:>10} {tot[4]:>11}")
    rows = resp["rows"]
    shown = rows if a.answer_rows == 0 else rows[:a.answer_rows]
    print(f"answer: {len(rows)} rows" + (f" (first {len(shown)})" if len(shown) < len(rows) else ""))
    for r in shown:
        print("  " + json.dumps(r, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
