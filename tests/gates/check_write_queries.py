"""The write suite on the Python store, as an ORACLE check (sf0.1): each write step's predicted reads (S/G/N) and
writes (P/D) must equal what the store counted (a read step reports its counted cost), and
every format must return the same answer (normalized) per (query, param). It replaces the justfile write-gate's old
kvbench `--backend abstract` sweeps (result measurement is C++ only).

It runs the committed write IR (canonical design) on the 4 formats with a Python twin (meta has none).

  python3 tests/gates/check_write_queries.py [--sf 0.1] [--queries q1,q2] [--inject <db>]

--inject <db> adds 1 to every predicted SEEK of that format's ops: the check must FAIL (its
own proof that a prediction error cannot pass). Exit 1 on any failure, or when no cell ran.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

from kvevaluator.measure.plan_runs import ir_for
from kvevaluator.oracle.interp import run_ir
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.measure.report import normalize
from kvevaluator.oracle.run import build_stores, _reset_shadow
from kvevaluator.workloads.writes import queries as w_queries
from kvevaluator.workloads.writes import params as w_params

FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr"]
NPARAM = 5



def _twin_counts(fmt):
    """the named bypass of runner.run_plan, for the formats whose twin disowns some counts (Per-type CSR): this suite
    checks each Python format against ITSELF (predicted == actual), so the twin's own counts are what it needs. Every
    other format gets none: a twin that starts to disown a count must fail here, not pass in silence"""
    return "old-label-rule" if fmt.name.startswith("pertype_csr") else None

def _inject(fmt):
    """Wrap every op method of this format instance (an op returns (result, predicted KVCost), which
    run_plan / run_ir compare with the store's count) so its prediction reads one SEEK too many."""
    from kvevaluator.cost import KVCost
    for name in dir(fmt):
        if name.startswith("_"):
            continue
        fn = getattr(fmt, name)
        if not callable(fn):
            continue

        def wrapped(*a, _fn=fn, **k):
            out = _fn(*a, **k)
            if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], KVCost):
                out[1].S += 1
            return out
        setattr(fmt, name, wrapped)


def _cell(workload, qname, args, db, fmt, store):
    """Run one write cell on a fresh overlay; return (final answer, predicted-vs-counted errors)."""
    _reset_shadow(store, fmt)                  # each cell starts from the pristine base store
    outputs, kvcosts = run_ir(ir_for(workload, qname), fmt, store, params=args,
                              format_caps=caps_for(db, fmt.schema),
                              caps_of_label=caps_resolver(db, fmt.schema), twin_counts=_twin_counts(fmt))
    plan = [st for st, _p, _a in kvcosts]
    errs = []
    for step, pred, act in kvcosts:
        if pred.triple() != act.triple():
            errs.append(f"{step.op}({step.out}) read pred {pred.triple()} != act {act.triple()}")
        if pred.wpair() != act.wpair():
            errs.append(f"{step.op}({step.out}) write pred {pred.wpair()} != act {act.wpair()}")
    return outputs[plan[-1].out], errs


def run_block(label, sf, dbs, qnames, inject):
    """The formats over the write suite. Returns (cells, prediction failures, disagreements)."""
    stores = build_stores(sf, dbs, "abstract", schema_name="canonical", writes=True)
    if inject in stores:
        _inject(stores[inject][0])
    cells, pred_fail, disagree = 0, [], []
    for qname in qnames:
        for pidx, args in enumerate(w_params.query_params(sf, qname, NPARAM), 1):   # 1-based, the shared convention
            finals = {}
            for db in dbs:
                fmt, store = stores[db]
                final, errs = _cell("writes", qname, args, db, fmt, store)
                cells += 1
                finals[db] = normalize(final)
                if errs:
                    pred_fail.append((qname, pidx, db, errs[0]))
            if len(finals) >= 2 and len({repr(f) for f in finals.values()}) != 1:
                disagree.append((qname, pidx))
    print(f"  {label}: {cells} cells ({len(qnames)} queries x <= {NPARAM} params x {len(dbs)} formats); "
          f"prediction failures {len(pred_fail)}, disagreements {len(disagree)}")
    for q, p, db, e in pred_fail[:5]:
        print(f"    PRED FAIL {q} p{p} {db}: {e}")
    for q, p in disagree[:5]:
        print(f"    DISAGREE {q} p{p}")
    return cells, pred_fail, disagree


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")     # a small-scale oracle: never the env's SF
    ap.add_argument("--queries", default=None, help="comma list (default: the whole write suite)")
    ap.add_argument("--inject", default=None, help="a format whose predictions get +1 SEEK (must FAIL)")
    a = ap.parse_args(argv)
    qnames = a.queries.split(",") if a.queries else list(w_queries.queries_of("all"))
    total, pf, dis = run_block("committed IR, canonical", a.sf, FORMATS, qnames, a.inject)
    bad = len(pf) + len(dis)
    if total == 0:
        print("WRITE QUERIES FAILED: 0 cells ran")
        return 1
    if bad:
        print(f"WRITE QUERIES FAILED: {bad} failures over {total} cells")
        return 1
    print(f"WRITE QUERIES OK: predicted == actual and all formats agree on {total} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
