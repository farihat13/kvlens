"""Named LDBC Interactive query plans (query = plan), one module per query in this package.

Each module exposes up to these dicts, merged here into the registries:
  SIM          -> REGISTRY               the cheapest plan we can write per format
  LOGICAL      -> LOGICAL                the optimizer's shared logical plan (search space: plan_choice/optimizer/rules.py)
  LABEL_ONLY   -> LABEL_ONLY             the plan when the only index is the vertex-label one (parked)
  ENGINE       -> ENGINE_REGISTRY        the plan the real engine runs, transcribed from its captured plan
  ENGINE_DRAFT -> ENGINE_DRAFT_REGISTRY  an engine plan not yet matched to live /kvstats (engine_fidelity's --drafts)
Bind a picked body with bound_plan (the pick: kvevaluator.plan_choice.resolve.plan_for); engine_plan_for.

A REGISTRY value is a builder (one plan for all formats) or {"default": builder, "<format>": builder}: a per-format
override only where it makes the query cheaper for that format, justified in its docstring. An override, and an
ENGINE plan, keeps the final step's `out` name and row shape, so the cross-format result check still applies.

Conventions:
- The LDBC friends pattern is UNDIRECTED (nGQL `-[:KNOWS]-`, DQL knows + ~knows), so knows traversals use dir="both".
- `label` names the vertex type a step touches; ["Post", "Comment"] means "a message of unknown type" (each format
  pays its own resolution cost).
- Projected ids are FETCHED (a GET for dgraph): real formats pay for projected ids (dgraph uids are opaque).
- Two-phase (rank on cheap props, fetch heavy props for the top k only) is dgraph's default for ranked-message
  queries; the row stores (nebula/arango) override with one-phase, since their first read returns the whole row.
- DATE-ranked queries (ic2/ic8/ic9/is2) have a `rank` choice point; every legal rule is a body of the query's
  generated plan file, and the pick record says which body a store runs (kvevaluator.plan_choice.pick_record).
"""
from kvevaluator.formats.caps import caps_for, edge_caps_of
from kvevaluator.workloads import merge_query_modules

# The submodules are discovered (workloads.merge_query_modules), so a new per-query file can't be silently forgotten.
# A builder is reached through its own module (queries.ic2.ic2_ixwalk), not by a flat name here.
# LABEL_ONLY plans MUST run against a store built from LABEL_ONLY_SCHEMA (value/date index entries dropped).
# A query with a LOGICAL plan and no SIM entry runs it as its hand plan too.
_R = merge_query_modules(__name__, __path__)
REGISTRY, LABEL_ONLY, LOGICAL = _R["REGISTRY"], _R["LABEL_ONLY"], _R["LOGICAL"]
ENGINE_REGISTRY, ENGINE_DRAFT_REGISTRY = _R["ENGINE_REGISTRY"], _R["ENGINE_DRAFT_REGISTRY"]


def _concrete(raw, args):
    """Bind a parameterized Plan to concrete Steps (fill the {"param"}/{"let"} holes); a Step list passes through.
    Every resolver goes through this, so any builder may return either form.
    """
    from kvevaluator.plans.plan import Plan
    from kvevaluator.plans.ir import bind
    from kvevaluator.plans.ir.interp import bind_ir
    if isinstance(raw, dict) and "steps" in raw:      # a committed candidate body (a hand pin): pre-lowered IR
        return bind_ir(raw, args)
    return bind(raw, args) if isinstance(raw, Plan) else raw


def raw_plan_for(name, fmt_name, args):
    """The builder's plan for (query, format) BEFORE binding or via-injection -- a Plan
    (parameterized) or a Step list (baked). For tools/tests that want the reusable template;
    the resolvers below bind + inject."""
    return _pick(REGISTRY[name], fmt_name)(args)


# the logical type filter's lowering: one rule for every registry and the IR dumper
from kvevaluator.plans.ir.lowering import lower_filter


# A format with no key of its own in a {default,nebula,...} entry reuses its BASE format's plan: the chunked
# per-type layout reads whole rows in one go, like nebula's row store, so it takes nebula's one-phase plans
# (falling through to "default" = the DGRAPH two-phase plan would pay a redundant body re-read: a plan
# confound, not a layout cost).
_PLAN_BASE = {"pertype_csr": "nebula"}

# The capability that a query's NON-dgraph plan variant genuinely NEEDS -- the IR-bundle dumper
# emits it as that variant's `requires` (the code selector). It cannot be auto-derived: the row
# stores and dgraph split the SAME format set, but the REASON differs per query, so it is named
# here by the query author. Every ranked/two-phase query here wins on the row store because its
# read returns the whole row in one go (whole_row_read). (plan_choice/dump_planir.py reads this.)
# ic2/ic9 (+merge twins) differ only in the label_only design, where their one-phase body's
# sole distinguishing capability IS whole_row_read -- without naming it here the requires
# derivation over-specifies (the full discriminator cap set), which a store with a different layout
# per type can never satisfy globally (its intersection caps are empty), making the variant unreachable.
VARIANT_REQUIRES = {q: "whole_row_read" for q in
                    ("ic1", "ic2", "ic2_merge", "ic8", "ic8_merge", "ic9", "ic9_merge",
                     "ic10", "is2", "is2_capped")}
# is7's edge probe: the two-list body (out@anchor + in@anchor) is for the stores whose knows adjacency is lists
VARIANT_REQUIRES["is7"] = "adjacency_lists"


def _pick(entry, fmt_name):
    """Choose a builder from a REGISTRY/LABEL_ONLY entry for fmt_name (format -> base -> default)."""
    if not isinstance(entry, dict):
        return entry
    if fmt_name in entry:
        return entry[fmt_name]
    base = _PLAN_BASE.get(fmt_name)
    if base and base in entry:
        return entry[base]
    return entry["default"]


# Twins, twin -> base: a transform of the base's plan (TWIN_TRANSFORM), measured as extra candidates of the base query
# (the meta grid): plan_choice/generate.py builds its bodies from each base body; a twin is never run as a query.
ALTERNATIVES = {"ic5_listjoin": "ic5"}           # the plan-change rule; each a transform of its base, below


def _ic5_list_join(plan):
    from kvevaluator.workloads.interactive.queries.ic5 import ic5_list_join
    return ic5_list_join(plan)


TWIN_TRANSFORM = {"ic5_listjoin": _ic5_list_join}
# A twin that is MEASURED and reported on every cell but never picked or pinned: its gain depends on the parameters,
# not on the layout (the plan-change rule's realism test; IC12's small-tag-class body). candidates.measure_only_bodies reads
# this; grid_bodies writes it into each run row; candidates.choose prices such a body aside.
TWIN_MEASURE_ONLY = set()
# The caps a twin needs on top of its base's: the dumper stamps them as the twin bundle's requires, so the twin is
# not run elsewhere (a fused read is valid only where a props read under another label misses).
TWIN_REQUIRES = {}

# The list-join twins of IC1 / IC3 / IC10 (the plan-change rule): each a
# transform of its base's plan, written beside the base's builder (queries/<q>.py); one per-item adjacency read
# becomes one list per owner on the other side plus an in-memory join.
def _list_join_twin(module, fn):
    def transform(plan):
        import importlib
        return getattr(importlib.import_module(f"{__name__}.{module}"), fn)(plan)
    return transform


ALTERNATIVES["ic3_country_lists"] = "ic3"
TWIN_TRANSFORM["ic3_country_lists"] = _list_join_twin("ic3", "ic3_country_lists")
ALTERNATIVES["ic3_exact_window"] = "ic3"
TWIN_TRANSFORM["ic3_exact_window"] = _list_join_twin("ic3", "ic3_exact_window")
TWIN_REQUIRES["ic3_exact_window"] = ("sorted_range_index",)
ALTERNATIVES["ic3_exact_window_country_lists"] = "ic3"
TWIN_TRANSFORM["ic3_exact_window_country_lists"] = _list_join_twin("ic3", "ic3_exact_window_country_lists")
TWIN_REQUIRES["ic3_exact_window_country_lists"] = ("sorted_range_index",)
ALTERNATIVES["ic10_interest_lists"] = "ic10"
TWIN_TRANSFORM["ic10_interest_lists"] = _list_join_twin("ic10", "ic10_interest_lists")
ALTERNATIVES["ic1_expand_name_matches"] = "ic1"
TWIN_TRANSFORM["ic1_expand_name_matches"] = _list_join_twin("ic1", "ic1_expand_name_matches")

# The bodies the list-join twins REPLACED, kept measurable (the plan-change rule): the base bundles of ic1 / ic3 / ic5 /
# ic10 hold the cheaper bodies, so these are in no bundle. Each is a twin whose transform changes nothing
# (plan_choice/generate.py applies it to each base body). measure_only: measured and reported on every cell beside
# the body that replaced it, never picked or pinned.
def _searched_body(plan):
    return plan


for _earlier, _base in (("ic1_level2_expansion", "ic1"),        # both knows lists of every level-2 person
                        ("ic3_per_message_location", "ic3"),    # the date re-read and one isLocatedIn OUT read per window message
                        ("ic5_per_post_creator", "ic5"),        # one hasCreator OUT read per forum post
                        ("ic10_per_post_tags", "ic10")):        # one hasTag OUT read per candidate post
    ALTERNATIVES[_earlier] = _base
    TWIN_TRANSFORM[_earlier] = _searched_body
    TWIN_MEASURE_ONLY.add(_earlier)

# IC10's PERSON-RUN twin: the fof props read as ONE sequential scan of the Person label's key run kept to the fof
# set (read_vertices_by_label with keep; queries/ic10.py ic10_fof_from_person_run), valid where the label's records are one key
# run (label_rows_one_run: every props layout of meta, pertype_csr, arangodb); a candidate beside today's bodies, picked by cost.
def _ic10_fof_from_person_run(plan):
    from kvevaluator.workloads.interactive.queries.ic10 import ic10_fof_from_person_run
    return ic10_fof_from_person_run(plan)


ALTERNATIVES["ic10_fof_from_person_run"] = "ic10"
TWIN_TRANSFORM["ic10_fof_from_person_run"] = _ic10_fof_from_person_run
TWIN_REQUIRES["ic10_fof_from_person_run"] = ("label_rows_one_run",)
# A twin of TODAY's body derives from another twin's body, named here (plan_choice/generate.py composes that twin's transform
# first, then this one's): on ic10's replaced per-post-tags body a scan twin would measure the old tag step, not the scan.
TWIN_DERIVES_FROM = {"ic10_fof_from_person_run": "ic10_interest_lists"}
# ic3_exact_window: the exact-window read on ic3's REPLACED per-message-location body (today's bodies are the country-lists
# ones, and ic3_exact_window_country_lists is the exact window on them): kept measurable, never picked (the guard caught it)
TWIN_MEASURE_ONLY.add("ic3_exact_window")

# The generated bodies (plan_choice/generate.py) that are measured and reported but never picked, named by how they are
# made (a rule label, or "<twin>(<rule label>)"), with the reason: the bodies the list-join twins replaced, and the
# exact window on ic3's replaced body. The exact window on any other ic3 body is a candidate.
MEASURE_ONLY = {
    "ic1": {"index_eq+fetch_deferred": "replaced by the list-join body (2026-10-10); kept measurable",
            "index_eq+fetch_inline": "replaced by the list-join body (2026-10-10); kept measurable"},
    "ic3": {"index_eq+index_eq+range_index": "replaced by the country-lists body (2026-10-10); kept measurable",
            "ic3_exact_window(index_eq+index_eq+range_index)": "the exact window on ic3's replaced body"},
    "ic5": {"ic5": "replaced by the list-join body (2026-10-10); kept measurable"},
    "ic10": {"filter_index+fetch_inline": "replaced by the interest-lists body (2026-10-10); kept measurable",
             "filter_index+fetch_deferred": "replaced by the interest-lists body (2026-10-10); kept measurable",
             "filter_probe+fetch_deferred": "replaced by the interest-lists body (2026-10-10); kept measurable"},
}

# The suite's GROUPS (the figure canon keeps IC and IS in separate figures: each group names
# its own `figure`); the same shape every workload exposes, so a plot bands by the workload's
# groups instead of knowing its query names.
GROUPS = [
    {"key": "ic", "title": "Interactive complex (IC)", "figure": "ic",
     "queries": [f"ic{i}" for i in range(1, 13)]},
    {"key": "is", "title": "Interactive short (IS)", "figure": "is",
     "queries": [f"is{i}" for i in range(1, 8)]},
]
for _twin, _base in ALTERNATIVES.items():
    REGISTRY[_twin] = REGISTRY[_base]              # the same hand entry; resolve.builder_for applies the twin
    if _base in VARIANT_REQUIRES:
        VARIANT_REQUIRES[_twin] = VARIANT_REQUIRES[_base]


def _parked(family):
    """the label_only family is parked: no plan file or pick record entry is generated for it"""
    raise ValueError(f"the {family} plan family is parked: it has no generated plan files or recorded picks")


def label_only_plan_for(name, fmt_name, schema, args, scale=None):
    """The label-index-only plan for a query: parked (refuses)."""
    _parked("label_only")


def engine_plan_for(name, fmt_name, args):
    """The engine plan for (query, format), or None when it has not been written yet. Falls back
    to a DRAFT plan: which cells run is chosen by the caller's cell list, not here."""
    for reg in (ENGINE_REGISTRY, ENGINE_DRAFT_REGISTRY):
        entry = reg.get(name)
        if entry and fmt_name in entry:
            return entry[fmt_name](args)
    return None


def bound_plan(name, fmt_name, schema, args, builder):
    """A query's plan for one format from the body `builder` builds (the pick record's: kvevaluator.plan_choice.resolve),
    with each logical type filter lowered by the format's capabilities on `schema` (lower_filter). A builder may return a
    parameterized Plan; _concrete binds it to concrete Steps here so every caller downstream sees a plain step list."""
    return lower_filter(_concrete(builder(args), args), caps_for(fmt_name, schema), edge_caps_of(fmt_name, schema))
