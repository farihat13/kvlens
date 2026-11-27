"""Validate the microbench workload's parameterized plans on the REAL canonical sf0.1 store:
the template (builder -> Plan) serialized to neutral IR, JSON round-tripped, then bound with
real params must give byte-identical S/G/N + bytes + result to what production runs
(queries.plan_for -> bind [-> fuse_scan]). Mirrors tests/unit/check_planir_ldbc.py for the LDBC
suite. A still-baked builder (entry_range_dgraph -- its day-token strings don't fit the closed
grammar) is skipped, like an engine plan.

The materialized store BACKEND is cached per (format, sf) across runs -- reads never
mutate it (asserted: no write ops enter these plans); each run still gets a FRESH format
+ counting wrapper, which is what the fresh-format guarantee is about.

Run:   python3 tests/unit/check_microbench_planir.py [--formats dgraph nebula] [--sf 0.1]
"""
import os
import sys

import argparse
import json

from kvevaluator.plans.plan import Plan
from kvevaluator.oracle.runner import run_plan
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.measure.report import normalize
from kvevaluator.oracle.run import open_store
from kvevaluator.formats import make_format
from kvevaluator.datasets import demo as data
from kvevaluator.cost import TwinCountsRefused
from kvevaluator.workloads.microbench import queries
from kvevaluator.workloads.microbench.queries import _pick, _FUSED, _fuse_scan
from kvevaluator.workloads.microbench.params import query_params
from kvevaluator.plans.ir import plan_to_ir, NeedsExpr, bind_ir
from kvevaluator.formats.caps import caps_for, edge_caps_of
from kvevaluator.plans.ir.lowering import lower_ir_steps
from declared_skips import declared_skip, failure


def cost_rows(kv):
    # a step whose counts the Python format disowns (cost.TwinCountsRefused: Per-type CSR's twin) compares as
    # "refused" on both runs: the round trip is still checked on its rows and on every other step's counts
    return [(s.op, "counts refused" if isinstance(a, TwinCountsRefused) else
             (a.S, a.G, a.N, a.value_bytes, a.scan_key_bytes, a.get_key_bytes)) for s, _p, a in kv]


_BACKENDS = {}    # (fmt_name, sf) -> materialized AbstractKV, built once, read-only after
from kvevaluator.plans.ir.capabilities import KV_WRITE_OPS as _WRITE_OPS  # (the op table's writes)


def _run(fmt_name, schema, steps, sf):
    """FRESH format + counting wrapper per run (fresh reader/cache); the materialized
    backend is cached (canonical schema only -- key it by schema too before adding a
    --schema axis) instead of paying a full LDBC load + materialize per run."""
    bad = [s.op for s in steps if s.op in _WRITE_OPS]
    assert not bad, f"write ops {bad} would mutate the SHARED cached backend"
    fmt = make_format(fmt_name, schema, False)
    key = (fmt_name, sf)
    if key not in _BACKENDS:
        store = open_store(fmt, "abstract", sf, lambda: data.load(sf), "canonical")
        _BACKENDS[key] = store.backend
    else:
        store = CountingKV(_BACKENDS[key], sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes)
        fmt.warm(store)
    outs, kv = run_plan(steps, fmt, store)
    return cost_rows(kv), outs[steps[-1].out]


def validate(fmt_name, sf, q, args, schema):
    ref = queries.plan_for(q, fmt_name, args, schema)             # production: bind [+ lower, fuse]
    try:
        raw = _pick(queries.REGISTRY[q], fmt_name)({})            # the template, pre-bind
    except Exception:  # -- baked, reads args
        return "skip", "baked builder (reads args -- e.g. entry_range_dgraph day tokens)"
    if not isinstance(raw, Plan):
        return "skip", "baked builder"
    try:
        ir = json.loads(json.dumps(plan_to_ir(raw, name=q)))
    except NeedsExpr as e:
        return "skip", f"lambda ({e})"
    # the dumper's lowering of the logical type filter (filter_by_label) to the format's path
    ir["steps"] = lower_ir_steps(ir["steps"], caps_for(fmt_name, schema), edge_caps_of(fmt_name, schema))
    rt = bind_ir(ir, args)
    if fmt_name in _FUSED:
        rt = _fuse_scan(rt)
    ref_rows, ref_final = _run(fmt_name, schema, ref, sf)
    rt_rows, rt_final = _run(fmt_name, schema, rt, sf)
    if ref_rows == rt_rows and repr(normalize(ref_final)) == repr(normalize(rt_final)):
        return "ok", sum(r[1][0] + r[1][1] for r in ref_rows)
    return "mismatch", [(a, b) for a, b in zip(ref_rows, rt_rows) if a != b]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--formats", nargs="+", default=["dgraph", "nebula"])
    ap.add_argument("--sf", default="0.1")
    a = ap.parse_args(argv)
    schema = data.schema(a.sf)

    npass = nmis = ncells = 0
    skip, failed = {}, {}                                       # (query, format) -> why
    for q in sorted(queries.REGISTRY):
        ps = query_params(a.sf, q, 1)
        args = ps[0] if ps else {}
        ran = []
        for fmt in a.formats:
            try:
                status, info = validate(fmt, a.sf, q, args, schema)
            except Exception as e:
                # only a declared skip is a skip (declared_skips.py); anything else fails the check by name
                if declared_skip(e):
                    skip[q, fmt] = declared_skip(e)
                else:
                    failed[q, fmt] = failure(e)
                continue
            if status == "skip":
                skip[q, fmt] = info
                continue
            ncells += 1
            if status == "mismatch":
                nmis += 1
                print(f"  MISMATCH {q} [{fmt}]")
                for x, y in info:
                    print(f"      {x}  vs  {y}")
            else:
                ran.append(f"{fmt}:S+G={info}")
        if ran:
            npass += 1
            print(f"  OK       {q:18s} ({', '.join(ran)})")
    print(f"\n  microbench sf{a.sf}: {npass} queries OK, {ncells} (query, format) cells run, {nmis} mismatch, "
          f"{len(skip)} declared skips, {len(failed)} failed | formats={a.formats}")
    for (q, f), why in sorted(skip.items()):
        print(f"    skip {q:18s} {f:8s} {why}")
    for (q, f), why in sorted(failed.items()):
        print(f"    FAILED {q:18s} {f:8s} {why}")
    ok = nmis == 0 and npass > 0 and not failed
    print("\n" + (f"MICROBENCH PLAN IR OK: {ncells} cells, {len(skip)} declared skips" if ok else "MICROBENCH PLAN IR FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
