"""Microbench workload: one canonical query per KV access-path shape (distilled from mb2's primitive catalog).

The shapes are the primitives the LDBC queries are made of, so a per-shape cost can be quoted, and mixed, one shape
at a time. Everything runs against the canonical LDBC store. The "unindexed" twins (entry_eq_scan, topk10_scan) are
plans that do not read the value/date indexes, so no label-only store is needed.

Same conventions as the Interactive registry (workloads/interactive/queries): query = plan; a REGISTRY value is a
builder or a per-format override dict; an override keeps the final step's `out` name and row shape. Also:
- Finals are cheap and comparable: a count row (`as_row`) or a person/tag vid list, never a projected-id list for
  big sets (dgraph pays 1 GET per projected id, which would bury the entry primitive). Where ids ARE part of the
  shape (point/topk/hop1p/chain), dgraph pays them.
- Message vids differ across formats, so message-set finals are counts; person/tag/forum vids agree raw.
- nhop1..nhop6 is BFS with a GLOBAL visited set. The final is the NEW frontier at exactly depth K, so hop K's cost
  is the row-total difference nhopK - nhop(K-1).
- DATE_CUTOFF is a fixed mid-dataset date for the two 1-hop filter shapes; the run reports its selectivity.
"""
from kvevaluator.plans.plan import Step
from kvevaluator.workloads.microbench.queries.entry import (_entry_label, entry_eq, entry_eq_scan, entry_range,
                                                             entry_range_dgraph,
                                                             entry_range_scan)
from kvevaluator.workloads.microbench.queries.hop import (_hop1, _hop1p, edge_probe, hop1_edgefilter, hop1_label,
                                                           hop1_msgs_content, hop1_msgs_date, hop1_posts_props,
                                                           hop1_propfilter, hop1p_knows, hop2_post_likers)
from kvevaluator.workloads.microbench.queries.nhop import _chain, _nhop, closure, nhop2_hetero
from kvevaluator.workloads.microbench.queries.point import point1, point4, point7, point_post
from kvevaluator.workloads.microbench.queries.topk import (_topk_dgraph, _topk_rowwalk, topk10_scan,
                                                            topk10_scan_onephase)
from kvevaluator.workloads.microbench.queries.split import (fof_posters_profile, fof_posters_profile_from_person_run,
                                                             fof_posters_profile_from_person_run_scan,
                                                             fof_posters_profile_onephase,
                                                             fof_posters_profile_scan_onephase)


REGISTRY = {
    "point1": point1, "point4": point4, "point7": point7, "point_post": point_post,
    "entrylabel_person": _entry_label("Person"),
    "entrylabel_forum":  _entry_label("Forum"),
    "entrylabel_post":   _entry_label("Post"),
    "entry_eq": entry_eq,
    "entry_eq_scan": entry_eq_scan,
    # Per-format plans where the parents' access paths genuinely differ (dgraph's date index
    # is per-PREDICATE and DAY-granular; its message rows are columnar). default = the row-walk
    # / non-dgraph shape; dgraph overrides (a dgraph-derived name would otherwise silently fall to the row-walk,
    # which dgraph's read_top_k_by_index refuses: vids=None).
    "entry_range": {"default": entry_range, "dgraph": entry_range_dgraph},
    "entry_range_scan": entry_range_scan,
    "topk1":   {"default": _topk_rowwalk(1),   "dgraph": _topk_dgraph(1)},
    "topk10":  {"default": _topk_rowwalk(10),  "dgraph": _topk_dgraph(10)},
    "topk100": {"default": _topk_rowwalk(100), "dgraph": _topk_dgraph(100)},
    "topk10_scan": {"default": topk10_scan,
                    "nebula": topk10_scan_onephase, "arangodb": topk10_scan_onephase},
    "hop1_knows":      _hop1("knows", "both", "Person"),
    "hop1_knows_out":  _hop1("knows", "out", "Person"),
    "hop1p_knows":     hop1p_knows,
    # width x fan-out family: read n props off each friend (PERSON_PROPS[:n]); n=1/3/7.
    "hop1p1_knows":    _hop1p(1),
    "hop1p3_knows":    _hop1p(3),
    "hop1p7_knows":    _hop1p(7),
    "hop1_hascreator": _hop1("hasCreator", "in", "Person"),
    "hop1_hasmember":  _hop1("hasMember", "out", "Forum", "forum"),
    "hop1_hastag":     _hop1("hasTag", "in", "Tag", "tag"),
    "hop1_label": hop1_label,
    # the per-fragment WIN demo (split.py): three shapes, one per parent family
    # nebula pins the whole-type-scan path: the pin check measured the per-candidate
    # one-phase probe at 51x the scan at sf3 (38x at sf0.1); arangodb's index path is the same either way
    "fof_posters_profile": {"default": fof_posters_profile,
                            "nebula": fof_posters_profile_scan_onephase,
                            "arangodb": fof_posters_profile_onephase,
                            # nebula's former body stays in the bundle as a measured candidate (no format selects
                            # it): the meta grid picks it on 4 cells (dump_planir KEEP_KEY)
                            "_keep": {"whole_row_read": ("nebula", fof_posters_profile_onephase)}},
    # the PERSON-RUN twin: the authors' rows by one scan of Person kept to the authors (TWIN_REQUIRES); the formats
    # on nebula's plans keep the base's whole-type-scan filter, so the twin differs from their base in that read only
    "fof_posters_profile_from_person_run": {"default": fof_posters_profile_from_person_run,
                                            "nebula": fof_posters_profile_from_person_run_scan},
    "hop1_propfilter": hop1_propfilter,
    # a person's messages: narrow vs fat column, a person's posts + props, who liked them
    "hop1_msgs_date": hop1_msgs_date,
    "hop1_msgs_content": hop1_msgs_content,
    # nebula pins the fused read (the _fused twin's body): 1.20x / 1.10x cheaper than probe + re-read at sf0.1 / sf3
    # (pertype_csr, which inherits nebula's entry by _PLAN_BASE, keeps the probe path, the cheapest for it at sf3)
    "hop1_posts_props": {"default": hop1_posts_props,
                         "nebula": lambda args: _fused(hop1_posts_props(args)),
                         "pertype_csr": hop1_posts_props},
    "hop1_posts_props_fused": lambda args: _fused(hop1_posts_props(args)),
    "hop2_post_likers": hop2_post_likers,
    "hop1_edgefilter": hop1_edgefilter,
    "edge_probe": edge_probe,
    **{f"nhop{k}": _nhop(k) for k in range(1, 7)},
    "nhop2p": _nhop(2, props=True),
    "nhop4p": _nhop(4, props=True),
    # the PERSON-RUN twin of nhop2p: the frontier's props by one scan of Person kept to the frontier (TWIN_REQUIRES)
    "nhop2p_from_person_run": _nhop(2, props=True, from_person_run=True),
    "nhop2_hetero": nhop2_hetero,
    "chain": _chain(None),
    "chain3": _chain(3),
    "closure": closure,
}


# The access-path GROUPS, in suite order:
# the one place the grouping is defined -- figures band by it, QUERIES is its flattening.
# key = the group's name in code and filenames; title = what a reader sees; figure = the TIER a
# dense per-query figure splits on (one 40-query grid is unreadable): the small read families
# share one, single-hop and multi-hop (+ composed) each get their own. Sparse per-group figures
# band all seven groups into one figure regardless. A twin (ALTERNATIVES, below) is a body of its base, not a
# query: it is in no group.
GROUPS = [
    {"key": "point", "title": "Point reads", "figure": "reads",
     "queries": ["point1", "point4", "point7", "point_post"]},
    {"key": "enumerate", "title": "Type enumeration", "figure": "reads",
     "queries": ["entrylabel_person", "entrylabel_forum", "entrylabel_post"]},
    {"key": "property", "title": "Property lookup", "figure": "reads",
     "queries": ["entry_eq", "entry_eq_scan", "entry_range", "entry_range_scan"]},
    {"key": "topk", "title": "Top-K", "figure": "reads",
     "queries": ["topk1", "topk10", "topk100", "topk10_scan"]},
    {"key": "hop1", "title": "Single-hop", "figure": "single-hop",
     "queries": ["hop1_knows", "hop1_knows_out", "hop1p_knows",
                 "hop1p1_knows", "hop1p3_knows", "hop1p7_knows",
                 "hop1_hascreator", "hop1_hasmember", "hop1_hastag",
                 "hop1_label", "hop1_propfilter", "hop1_edgefilter",
                 "edge_probe", "hop1_msgs_date", "hop1_msgs_content", "hop1_posts_props"]},
    {"key": "nhop", "title": "Multi-hop", "figure": "multi-hop",
     "queries": ["nhop1", "nhop2", "nhop3", "nhop4", "nhop5", "nhop6",
                 "nhop2p", "nhop4p", "nhop2_hetero",
                 "chain", "chain3", "hop2_post_likers"]},
    {"key": "composed", "title": "Composed", "figure": "multi-hop",
     "queries": ["fof_posters_profile"]},
]
QUERIES = [q for g in GROUPS for q in g["queries"]]


# Formats that take another format's per-format plan when they have none of their own: the
# chunked per-type layout returns whole rows from one read, like nebula's row store, so it
# takes nebula's one-phase plans (a two-phase re-read would be a plan confound, not a layout
# cost -- the same rule the LDBC registry applies). On top of that its scans
# FUSE enumerate + project (read_vertices_by_label(props=...)): see _fuse_scan.
_PLAN_BASE = {"pertype_csr": "nebula"}

# The capability a query's NON-dgraph plan variant genuinely needs (the IR-bundle dumper emits it
# as that variant's `requires`; named here because it can't be auto-derived -- see the LDBC note).
# entry_range: the row stores read a value-sorted index (dgraph has only the day-granular token
# index, so it takes a different plan). topk1/10/100: the candidate-less walk needs the index to
# BE the type restriction (per_type_index) -- exactness is every format's read_top_k_by_index
# contract, granularity included (dgraph needs candidates only because its index mixes types).
# topk10_scan: the row stores' rank read returns the whole row, so one-phase (whole_row_read).
# entry_eq_scan / entry_range_scan: the fused scan+project is the chunked layout's plan
# (label_scan_carries_props); topk10_scan has BOTH a one-phase (whole_row_read) and, on top,
# a fused-scan body -- the dumper fingerprints the third group with the hitting set.
VARIANT_REQUIRES = {"entry_range": "sorted_range_index",
                    "topk1": "per_type_index", "topk10": "per_type_index",
                    "topk100": "per_type_index", "topk10_scan": "whole_row_read",
                    "entry_eq_scan": "label_scan_carries_props",
                    "entry_range_scan": "label_scan_carries_props",
                    # the WIN demo: the one-phase profile is the row stores' story (whole_row_read);
                    # arango's combined body (index filter + one-phase) is specialized on top of it
                    "fof_posters_profile": "whole_row_read"}
# The fused-scan body of these three reads a WHOLE label, so it is the default plan where a whole-label read takes
# one scan of the label's rows (whole_label_scan_by_default: every props layout of meta, pertype_csr), not only where
# the scan is the layout's enumeration (label_scan_carries_props: the Chunked layouts). The dumper derives the
# selector as before and emits this cap in its place (dump_planir.bundle_for fused_scan_requires).
# ArangoDB's scan of a collection's documents is valid (label_rows_one_run) and much cheaper here, but it does not hold
# whole_label_scan_by_default until the other formats' whole-label scans are sized. To lift the hold: give arangodb
# the capability in both languages and make its registry entry the fused body (lowering.with_fused_label_scan).
FUSED_SCAN_REQUIRES = {q: "whole_label_scan_by_default" for q in ("entry_eq_scan", "entry_range_scan", "topk10_scan")}
_FUSED = {"pertype_csr"}

# Twin -> base: the same query on another plan, measured as extra candidates of the base query (the meta grid).
ALTERNATIVES = {# the FUSED twin: the Post props read straight from the candidates (a Comment reads nulls) instead
                # of a probe per candidate and a re-read of the Posts' rows (plans/ir/lowering.py with_fused_label_props)
                "hop1_posts_props_fused": "hop1_posts_props",
                # the PERSON-RUN twins: a point read per vertex replaced by one sequential scan of the Person label's
                # key run kept to the wanted vids (read_vertices_by_label with keep); candidates, picked by cost
                "nhop2p_from_person_run": "nhop2p",
                "fof_posters_profile_from_person_run": "fof_posters_profile"}


# The caps a twin needs on top of its base's (the dumper stamps them as the twin bundle's requires; plan_for
# refuses the twin elsewhere): the fused read tests membership by a props read under the label, valid only where a
# read under another label misses.
TWIN_REQUIRES = {"hop1_posts_props_fused": ("label_scoped_props",),
                 # the row scan with keep runs where the label's records are one key run (every props layout of meta,
                 # pertype_csr, arangodb); the other formats refuse the op
                 "nhop2p_from_person_run": ("label_rows_one_run",),
                 "fof_posters_profile_from_person_run": ("label_rows_one_run",)}


def _fused(plan):
    from kvevaluator.datasets import schema_path
    from kvevaluator.plans.ir.lowering import nonnull_props, with_fused_label_props
    return with_fused_label_props(plan, nonnull_props(schema_path("ldbc")))


# the fused read is valid only where a props read under another label misses: its selector must carry the cap
REGISTRY["hop1_posts_props"]["nebula"].validity_caps = frozenset(TWIN_REQUIRES["hop1_posts_props_fused"])


def _fuse_scan(steps):
    """The scan fusion (kvevaluator.plans.ir.lowering.fuse_label_scan, shared with the BI workload's scan twins)."""
    from kvevaluator.plans.ir.lowering import fuse_label_scan
    return fuse_label_scan(steps)


def _fuse_plan(raw):
    """with_fused_label_scan over a builder's product (kvevaluator.plans.ir.lowering), Plan or Step list."""
    from kvevaluator.plans.ir.lowering import with_fused_label_scan
    return with_fused_label_scan(raw)


def _pick(entry, fmt_name):
    """The builder for (entry, format): per-format override -> plan-base -> default. A _FUSED format's builder fuses
    its scans here, at the plan level, so the IR dumper emits the fused body.
    """
    if not isinstance(entry, dict):
        builder = entry
    else:
        builder = entry.get(fmt_name) or entry.get(_PLAN_BASE.get(fmt_name, ""), None) or entry["default"]
    if fmt_name in _FUSED:
        return lambda args: _fuse_plan(builder(args))
    return builder


def raw_plan_for(name, fmt_name):
    """The builder's plan for (query, format) BEFORE binding -- a Plan (parameterized) or a
    Step list (baked). For the IR dumper / drift guard."""
    return _pick(REGISTRY[name], fmt_name)({})


# Engine plans: what each real engine runs for a shape, transcribed from its captured plan -- the same rule as the
# LDBC engine plans. Each family module keeps a shape's engine plans beside its optimal plan, one
# section per engine: `ENGINE_DGRAPH = {shape: builder}`, `ENGINE_NEBULA`, `ENGINE_ARANGODB`
# (`ENGINE_DRAFT_<DB>` for a plan that does not fit live yet). Only the fidelity subset has them
# (the engine-fidelity method). The final step's name and row shape match the optimal
# plan's, so validate.py's row check applies.
from kvevaluator.workloads.microbench.queries import (entry as _entry, hop as _hop, nhop as _nhop_mod, point as _point,
                                                       split as _split, topk as _topk)
_FAMILY_MODULES = (_point, _entry, _topk, _hop, _nhop_mod, _split)


def _engine_registry(prefix):
    reg = {}
    for m in _FAMILY_MODULES:
        for db in ("dgraph", "nebula", "arangodb"):
            for q, builder in getattr(m, f"{prefix}_{db.upper()}", {}).items():
                reg.setdefault(q, {})[db] = builder
    return reg


# LOGICAL: the optimizer's logical plans, for the shapes whose question has more than one legal
# plan in the rule catalog (plan_choice/optimizer/rules.py). The hand REGISTRY keeps each shape's primitive
# measurement; the optimizer's pick is the shape's CHEAPEST plan (cheapest_plan_for).
LOGICAL = {q: b for m in _FAMILY_MODULES for q, b in getattr(m, "LOGICAL", {}).items()}

ENGINE_REGISTRY = _engine_registry("ENGINE")
ENGINE_DRAFT_REGISTRY = _engine_registry("ENGINE_DRAFT")


def engine_plan_for(name, fmt_name, args):
    """The engine plan for (shape, engine) bound to `args` as concrete Steps, or None when it
    has not been written. Suite first, then draft (the caller's cell list picks which run)."""
    from kvevaluator.plans.plan import Plan
    from kvevaluator.plans.ir import bind
    for reg in (ENGINE_REGISTRY, ENGINE_DRAFT_REGISTRY):
        builder = reg.get(name, {}).get(fmt_name)
        if builder:
            raw = builder(args)
            return bind(raw, args) if isinstance(raw, Plan) else raw
    return None


def plan_for(name, fmt_name, args, schema=None):
    """A microbench query's plan for one format, bound to concrete Steps (overrides, plan base and scan fusion as _pick).
    With `schema`, a logical `filter_by_label` is lowered to the format's access path; refuses a twin the format lacks
    the caps for.
    """
    from kvevaluator.plans.plan import Plan
    from kvevaluator.plans.ir import bind
    from kvevaluator.formats.caps import caps_for, edge_caps_of
    from kvevaluator.plans.ir.lowering import lower_filter
    if name in TWIN_REQUIRES:
        missing = set(TWIN_REQUIRES[name]) - (caps_for(fmt_name, schema) if schema is not None else set())
        if missing:
            raise ValueError(f"{name}: {fmt_name} lacks {sorted(missing)} (or no schema given to check)")
    raw = _pick(REGISTRY[name], fmt_name)(args)
    steps = bind(raw, args) if isinstance(raw, Plan) else raw
    if schema is not None:
        lower_filter(steps, caps_for(fmt_name, schema), edge_caps_of(fmt_name, schema))
    return steps


def bound_body(fmt_name, args, builder, schema=None):
    """a body's steps from `builder` (a recorded body: kvevaluator.plan_choice.resolve.cheapest_plan_for), bound to
    `args`, the chunked layout's scans fused as _pick does, the type filters lowered when `schema` is given"""
    from kvevaluator.plans.ir.interp import bind_ir
    from kvevaluator.formats.caps import caps_for, edge_caps_of
    from kvevaluator.plans.ir.lowering import lower_filter
    steps = bind_ir(builder(args), args)
    if fmt_name in _FUSED:
        steps = _fuse_plan(steps)
    if schema is not None:
        lower_filter(steps, caps_for(fmt_name, schema), edge_caps_of(fmt_name, schema))
    return steps
