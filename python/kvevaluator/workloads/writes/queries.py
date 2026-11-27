"""The write workload, as one registry over two altitudes -- kept in sibling sub-packages that
mirror the read side:

  writes/microbench  the write PRIMITIVES (w_*, mb2 MB81-88 lineage) -- what one write costs
  writes/ldbc        the LDBC insert OPERATIONS (iu*) -- what one user action costs

Both live in one registry because they are the same kind of thing measured at two altitudes: an
IU operation is a SUM of primitives. Keeping them together means `--query iu6` and
`--query w_ins_e_facet` are the same gesture and land in the same CSV, so a per-operation number
can be checked against its parts. query = plan; no per-format overrides (a write primitive is the
same LOGICAL operation everywhere -- the divergence is what each layout DOES to carry it out,
which is the operator's business, not the plan's).
"""
from kvevaluator.workloads.writes.microbench.queries import REGISTRY as _PRIM_REGISTRY
from kvevaluator.workloads.writes.ldbc.queries import REGISTRY as _IU_REGISTRY

REGISTRY = {**_PRIM_REGISTRY, **_IU_REGISTRY}

# The suite has TWO ALTITUDES, named so a run can say which it is doing:
#   primitives  one write op each -- what a single write costs (mb2 MB81-88 lineage)
#   ldbc        SNB Interactive's insert operations -- what one user action costs
# `all` (the default) runs both into one CSV on purpose: an IU operation is a SUM of primitives,
# so having them side by side is what lets you check the composition instead of trusting it.
PRIMITIVES = list(_PRIM_REGISTRY)
LDBC_INSERTS = list(_IU_REGISTRY)
QUERIES = list(REGISTRY)
# The suite's GROUPS, the same shape every workload exposes (a plot bands by the workload's
# groups instead of knowing its query names). The primitives split by WRITE CLASS -- an insert is
# an append, an update a read-modify-write of one property, a delete a tombstone or rewrite --
# because the classes have structurally different write amplification and a merged distribution
# hides that (the whole-vertex delete alone skews it). The three classes share one figure
# (`figure` = "primitives": the dense grid draws them as sub-bands); the IU operations are their
# own. DISPLAY only: `suite()` / --family keep the two altitudes.
_class = lambda prefix: [q for q in PRIMITIVES if q.startswith(prefix)]
GROUPS = [
    {"key": "ins", "title": "primitive inserts", "figure": "primitives", "queries": _class("w_ins_")},
    {"key": "upd", "title": "primitive updates", "figure": "primitives", "queries": _class("w_upd_")},
    {"key": "del", "title": "primitive deletes", "figure": "primitives", "queries": _class("w_del_")},
    {"key": "ldbc", "title": "LDBC inserts (IU)", "figure": "ldbc", "queries": LDBC_INSERTS},
]
assert sum(len(g["queries"]) for g in GROUPS[:3]) == len(PRIMITIVES), "a primitive outside the three classes"


def queries_of(family="all"):
    """The query list for one altitude. `family`: all, primitives or ldbc."""
    return {"all": QUERIES, "primitives": PRIMITIVES, "ldbc": LDBC_INSERTS}[family]


def plan_for(name, fmt_name, args):
    """Resolve a write query's plan for one format. A builder returns a parameterized Plan, bound
    to concrete Steps here."""
    from kvevaluator.plans.plan import Plan
    from kvevaluator.plans.ir import bind
    raw = REGISTRY[name](args)
    return bind(raw, args) if isinstance(raw, Plan) else raw
