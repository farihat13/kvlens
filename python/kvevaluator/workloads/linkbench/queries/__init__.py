"""LinkBench query registry -- one module per query file, auto-discovered, as in workloads/interactive/queries. Each
module exposes LOGICAL (one shared shape per query, a single-candidate passthrough), which is also its hand plan
(workloads.merge_query_modules). No LABEL_ONLY / ENGINE families (LinkBench has one design)."""
from kvevaluator.workloads import merge_query_modules

_R = merge_query_modules(__name__, __path__)   # every query file; a LOGICAL plan with no SIM entry is its hand plan
REGISTRY, LOGICAL = _R["REGISTRY"], _R["LOGICAL"]

# no per-format overrides: every query is one shared shape (bare builder), so the pick is a passthrough
VARIANT_REQUIRES = {}


def _pick(entry, fmt_name):
    """Passthrough: a bare builder serves every format (no per-format overrides in LinkBench)."""
    return entry


# The suite's groups (the figure canon bands by group): LinkBench's reads (+ our composed_getnode, not a LinkBench
# op) and its writes. The writes need request-log params (the sets quick, quick-next or log, see params.py).
GROUPS = [
    {"key": "wide", "title": "LinkBench reads", "figure": "",
     "queries": ["getnode", "composed_getnode", "getlinklist_link1", "getlinklist_link2",
                 "countlink_link1", "countlink_link2", "getlink_link1", "getlink_link2"]},
    {"key": "writes", "title": "LinkBench writes", "figure": "",
     "queries": ["addlink_link1", "addlink_link2", "updatelink_link1", "updatelink_link2",
                 "deletelink_link1", "deletelink_link2", "addnode", "updatenode", "deletenode"]},
]
QUERIES = [q for g in GROUPS for q in g["queries"]]


def plan_for(name, fmt_name, schema, args):
    """Resolve a query's plan for one format. Single-candidate passthrough (no lowering needed --
    LinkBench queries carry no logical type filter), bound to concrete steps."""
    from kvevaluator.plans.ir import bind
    return bind(REGISTRY[name](args), args)
