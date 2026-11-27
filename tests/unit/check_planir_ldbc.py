"""Validate the plan IR at REAL LDBC scale with REAL params -- the PARAMETERIZED template
(query builder -> Plan) serialized to neutral IR, JSON round-tripped, then bound with real
params must give byte-identical S/G/N + bytes and the same result as what production runs
(resolve_for -> bind -> via-inject), on non-empty data at scale.

Complements tests/unit/check_planir.py (demo graph): this exercises the message queries and
pertype_csr, which the demo can't. Runs on the ABSTRACT oracle
(the in-process independent leg -- no store, no build; the C++ store's coverage of the same
plans lives in check_cpp_vs_python.py). Reference and round-trip each run on a FRESH format
(so pertype_csr's chunk-reader cache can't undercount the 2nd run).

The materialized store BACKEND is cached per (format, sf) and shared across runs --
reads never mutate it (asserted: no write ops enter these plans); each run still gets a
FRESH format + counting wrapper + warm() (the part the fresh-format guarantee is about).

Run:   python3 tests/unit/check_planir_ldbc.py [--formats dgraph nebula pertype_csr] [--sf 0.1]
"""
import os
import sys

import argparse
import json

from kvevaluator.oracle.runner import run_plan
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.measure.report import normalize
from kvevaluator.oracle.run import open_store
from kvevaluator.formats import make_format, QUERY_FORMATS
from kvevaluator.plan_choice import resolve
from kvevaluator.datasets import demo as data
from kvevaluator.cost import TwinCountsRefused
from kvevaluator.workloads.interactive import queries
from kvevaluator.formats.caps import caps_for
from kvevaluator.datasets.ldbc.params import param_rows, args_from_row
from kvevaluator.plans.ir import plan_to_ir, NeedsExpr, bind_ir
from declared_skips import declared_skip, demo_query, failure


def cost_rows(kvcosts):
    # a step whose counts the Python format disowns (cost.TwinCountsRefused: Per-type CSR's twin) compares as
    # "refused" on both runs: the round trip is still checked on its rows and on every other step's counts
    return [(s.op, "counts refused" if isinstance(a, TwinCountsRefused) else
             (a.S, a.G, a.N, a.value_bytes, a.scan_key_bytes, a.get_key_bytes)) for s, _p, a in kvcosts]


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


def validate(fmt_name, sf, query, args):
    """Reference = what production runs (resolve_for -> bind -> lower the type filter). Round-trip
    = the PARAMETERIZED template serialized to neutral IR, JSON'd, then bound with the same params
    and lowered. If they match, the language-neutral JSON preserved the plan exactly (params,
    lets, and pred ASTs included) -- the real test of the migration."""
    schema = data.schema(sf)
    fmtschema = make_format(fmt_name, schema, False).schema
    ref_plan = resolve.resolve_for("interactive", schema)(query, fmt_name, fmtschema, args, scale=sf)
    # the template leg A resolved, pre-bind (Plan or list): the optimizer's pick where the picks file covers the cell
    # (per-SF pins included) and a twin's transform (builder_for), never the hand registry entry alone -- the two
    # legs must be the SAME plan, or the check compares a pick with its hand baseline
    raw = resolve.builder_for("interactive", query, fmt_name, "canonical", fmtschema.variant, sf)(args)
    try:
        # a cell pinned to a committed candidate body gets that body as IR already (queries._concrete reads the same
        # form): its round trip is the JSON one alone
        ir = json.loads(json.dumps(raw if isinstance(raw, dict) and "steps" in raw else plan_to_ir(raw, name=query)))
    except NeedsExpr as e:
        return "skip", f"lambda ({e})"
    ir_plan = queries.lower_filter(bind_ir(ir, args), caps_for(fmt_name, fmtschema),
                                   queries.edge_caps_of(fmt_name, fmtschema))
    ref_rows, ref_final = _run(fmt_name, schema, ref_plan, sf)
    ir_rows, ir_final = _run(fmt_name, schema, ir_plan, sf)
    if ref_rows == ir_rows and repr(normalize(ref_final)) == repr(normalize(ir_final)):
        return "ok", sum(r[1][0] + r[1][1] for r in ref_rows)   # a S+G total, just to show it's non-trivial
    diff = [(a, b) for a, b in zip(ref_rows, ir_rows) if a != b]
    return "mismatch", diff


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--formats", nargs="+", default=["dgraph", "nebula"],
                    choices=list(QUERY_FORMATS))
    ap.add_argument("--sf", default="0.1")
    a = ap.parse_args(argv)

    npass = nmis = ncells = 0
    skip, failed = {}, {}                               # (query, format or "*") -> why
    for q in sorted(queries.REGISTRY):
        if q in queries.ALTERNATIVES:                   # a twin is a body of its base's plan file, not a query
            continue
        if demo_query(q):
            skip[q, "*"] = demo_query(q); continue
        try:
            rows = param_rows(a.sf, q)
        except Exception as e:  # (a registry query whose params cannot be read)
            failed[q, "*"] = "no params: " + failure(e); continue
        if not rows:
            failed[q, "*"] = "its param file is empty"; continue
        args = args_from_row(q, rows[0])
        qbad = False
        ran = []
        for fname in a.formats:
            try:
                status, info = validate(fname, a.sf, q, args)
            except Exception as e:
                # only a declared skip is a skip (declared_skips.py); anything else is a failure of the part
                if declared_skip(e):
                    skip[q, fname] = declared_skip(e)
                else:
                    failed[q, fname] = failure(e)
                continue
            ncells += 1
            if status == "skip":
                skip[q, fname] = info
            elif status == "mismatch":
                nmis += 1; qbad = True
                print(f"  MISMATCH {q} [{fname}]")
                for x, y in info:
                    print(f"      {x}  vs  {y}")
            else:
                ran.append(f"{fname}:S+G={info}")
        if qbad:
            continue
        if ran:
            npass += 1
            print(f"  OK       {q:8s} ({', '.join(ran)})")
    print(f"\n  abstract sf{a.sf}: {npass} queries OK, {ncells} (query, format) cells run, {nmis} mismatch, "
          f"{len(skip)} declared skips, {len(failed)} failed | formats={a.formats}")
    for (q, f), why in sorted(skip.items()):
        print(f"    skip {q:8s} {f:8s} {why}")
    for (q, f), why in sorted(failed.items()):
        print(f"    FAILED {q:8s} {f:8s} {why}")
    ok = nmis == 0 and npass > 0 and not failed
    print("\n" + (f"PLAN IR LDBC OK: {ncells} cells, {len(skip)} declared skips" if ok else "PLAN IR LDBC FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
