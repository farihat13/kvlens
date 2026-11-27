"""The workload adapter for the C++ capture tools -- neutral infra, owned by no workload.

Three facts every capture over the C++ evaluator needs per workload, in ONE place:
which queries make up the suite, where a query's committed plan IR is, and how its params
are read:

    suite(workload)                       -> [query names] (the workload's suite)
    all_plans(workload)                   -> every query with a committed plan file (twins too)
    ir_for(workload, qname)               -> the committed plan IR dict (plan_runs.ir_for)
    query_params(workload, sf, qname, n)  -> [arg dicts] for the first n params (all if n=0)
    request_log(workload, sf)             -> the path of the workload's ordered request file (a mixed run)

Canonical schema only (the committed plan family the C++ capture runs; label_only
families are cpplatency's per-design concern). The writes workload's suite is the WHOLE suite
(primitives + LDBC inserts), writes.queries.queries_of("all"). measure/cppwlatency.py keeps its own
sequenced walk over the writes plans (a different pipeline, not a copy of this one).
"""
import os
import sys


from kvevaluator.measure import plan_runs
from kvevaluator import workloads as W
from kvevaluator.paths import ROOT

WORKLOADS = tuple(W.names())     # the workloads table (kvevaluator/workloads/*/manifest.json), in its order


def queries(workload):
    """the workload's query list: its queries module's QUERIES, else its GROUPS flattened (LDBC Interactive)"""
    mod = W.queries_module(workload)
    if hasattr(mod, "QUERIES"):
        return list(mod.QUERIES)
    return [q for g in groups(workload) for q in g["queries"]]


def groups(workload):
    """The workload's named query groups, in suite order: [{key, title, figure, queries}].
    `figure` ("" = the workload's one figure) says which figure a group's band belongs to
    (ldbc: IC and IS are separate figures by the figure canon). A workload that declares no
    GROUPS is one group named after itself, so every consumer can band uniformly."""
    GROUPS = W.queries_module(workload).GROUPS
    return [{"key": g["key"], "title": g["title"], "figure": g.get("figure", ""),
             "queries": list(g["queries"])} for g in GROUPS]


def all_plans(workload):
    """Every query with a committed canonical plan file (the kv_amp study's scope: the whole
    committed family, twins included) -- a superset of suite(workload)."""
    import glob
    base = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")
    names = []
    for d in plan_runs.PLAN_DIRS[workload]:
        names += [os.path.basename(f)[:-5]
                  for f in sorted(glob.glob(os.path.join(base, *d.split("/"), "*.json")))]
    return names


def ir_for(workload, qname):
    """The committed IR for one query of the canonical plan family (raises, via plan_runs,
    when the file is missing -- never a silent fallback)."""
    W.get(workload)
    return plan_runs.ir_for(workload, qname)


def query_params(workload, sf, qname, n=0, param_set=None):
    """Arg dicts for the first n params (n=0: all). Each workload's params module has the
    same query_params(sf, qname, count) contract (`sf` is the dataset's scale: the LDBC scale
    factor, LinkBench's scale tag, IMDb's data tag). A query that borrows another's rows declares it (params_like).
    `param_set`: one of the sets the params module lists (PARAM_SETS; None: its default), passed only when given"""
    mod = W.params_module(workload)
    if param_set is None:
        return mod.query_params(sf, qname, n or None)
    if param_set not in getattr(mod, "PARAM_SETS", ()):
        raise SystemExit(f"{workload}: no param set {param_set!r} (its params module lists "
                         f"{', '.join(getattr(mod, 'PARAM_SETS', ())) or 'one set'}) -- REFUSED")
    return mod.query_params(sf, qname, n or None, param_set=param_set)


def request_log(workload, sf):
    """The path of a workload's ORDERED request file, the input of a mixed read + write run: one
    JSON line per request in run order, {seq, query, row, args, ran_as, note}; ran_as is the plan that runs once the
    writes before it are applied. A log workload writes it from its log (LinkBench: python/kvevaluator/datasets/linkbench/gen_linkbench_params.py); a
    weighted-mix workload would get a seeded order generated once into a tracked file. Fails for a workload that has
    none yet."""
    if workload in WORKLOADS:
        mod = W.params_module(workload)
        if hasattr(mod, "request_log"):     # a log workload's params module names its file (LinkBench's)
            return mod.request_log(sf)
        raise SystemExit(f"{workload}: no ordered request file (its params module has no request_log; a weighted-mix "
                         f"workload needs a seeded order generated into a tracked file first)")
    raise ValueError(f"unknown workload {workload!r} (one of {WORKLOADS})")
