"""The rule catalog (the search space) and the logical-to-physical expansion.

A logical plan is an ordinary parameterized Plan whose choice points use logical op names; every other step is the
physical step all formats share and passes through unchanged. The logical ops:
  rank           the top-k of a candidate set under sort keys (what, not how). args: vids, keys, k, labels,
                 read_label, rank_props (the cheap first-phase read), body_props (winners only), hi (optional bound)
  entry_eq       an equality lookup of vids by a property value. args: label, prop, value
  window_narrow  narrow a candidate set to a [lo, hi) window on a sorted prop before the per-candidate read (one rule,
                 range_index: the full read is not offered, WINDOW_RULES). args: vids, labels, prop, lo, hi, idx_name
  fetch_props    the winners' remaining props, placed where the query's tail wants them. args: rows, props, label,
                 vids or pluck, read_name, widen. With a rank step it follows the rank rule's fetch field; alone it
                 chooses fetch_deferred vs fetch_inline.

Rules are data: each is a dict of declarative fields the expander interprets, so the catalog can be serialized
(dump_catalog). Legality is a schema predicate, not a capability, where the needed index may be absent by design
(capabilities alone cannot make the date walk illegal on an index-free design). `wins_under` is a selection hint for
the IR bundle's variant `requires`, never a legality gate.
"""
import json

from kvevaluator.plans.plan import Plan, Ref, Step, ref
from kvevaluator.plans.ir.authoring import eq, field, lt
from kvevaluator.plans.ir.lowering import fuse_filters, fusable_filters, nonnull_of

RANK_RULES = [
    # ranking + bound + winner bodies fused into one bounded index walk
    {"name": "index_walk", "logical_op": "rank", "fetch": "fused",
     "requires_sorted_index": True},
    # rank on the cheap props, fetch the heavy props for the winners only
    {"name": "two_phase", "logical_op": "rank", "fetch": "deferred"},
    # rank by reading every candidate's whole row (the body rides the rank read)
    {"name": "one_phase", "logical_op": "rank", "fetch": "inline",
     "wins_under": "whole_row_read"},
]

ENTRY_RULES = [
    # the value index: one point SEEK on (label, prop)
    {"name": "index_eq", "logical_op": "entry_eq", "requires_value_index": True},
    # no value index: enumerate the label, read the prop everywhere, keep the matches. Dominated by index_eq wherever
    # that is legal: the scan reads a superset of the index's entries in every lane (every row of the
    # label against the matching entries), so it is offered only on a design without the value index
    {"name": "label_scan", "logical_op": "entry_eq", "dominated_by": "index_eq"},
]

FETCH_RULES = [                                  # standalone fetch_props markers only
    {"name": "fetch_deferred", "logical_op": "fetch_props"},
    {"name": "fetch_inline", "logical_op": "fetch_props", "wins_under": "whole_row_read"},
]

# read_edge_exists answers "is there an edge between the row's candidate c and an anchor a, either way" from an
# ordered list of READS the plan names (formats/base.py read_edge_exists; C++ EdgeRead). Each read is one (etype, dir)
# family, read at the anchors' keys or the candidates':
#   A = out@anchor (a -> c)    B = in@anchor (c -> a)    C = out@cand (c -> a)    D = in@cand (a -> c)
# A complete check covers both halves: (A or D) and (B or C). "X, Y" = Y only for the candidates X did not find.
# A candidate names its reads, so it means the SAME reads on every store: a mixed store's cost can then be put
# together from the uniform stores part by part (python/kvcost/layout_cost/compose.py).
#
# The two probe candidates are the reads the layouts did before the reads were written in the plan (the default
# lowering of a hand plan picks between them by the edge type's caps, planir/lowering.py):
#   edge_probe      A + B, both always  the anchor's out list and in list (dgraph, arangodb, pertype_csr, meta
#                                       lists); needs the in lists (reverse_adjacency, checked on the step's EDGE TYPE)
#   edge_probe_out  A, then C           out keys / out lists only: meta Split, nebula, any store with no in lists
# Seven more orders were measured and are NOT offered (the plan-change rule: under 10% of the query, write it down once):
# over IS7's 35 params at sf0.1, whole query, against edge_probe on dgraph / arangodb / dgneb / pertype_csr:
# "A, then B" and "B, then A" -0.3% to -0.7% ops, -0.1% to -4.4% bytes; the candidate-side orders (B then D, D then B,
# C then A, C then D, D then C) +0.3% to +26% ops and up to +152% bytes; on the pertype_csr-c512k arm at sf3 none
# beats it. On a per-edge-key store the orders differ only in which half is tried first, the same cost in expectation
# (nebula, 5 params: 1.6 against 2.2 GETs by the sample alone). A size from 5 params and one step is a lead, never a
# finding: the size that decides is the whole query over all of the capture's params. The raw vectors:
# output/sf0.1/edge-exists-candidates/ (working).
_A, _B = {"dir": "out", "at": "anchor"}, {"dir": "in", "at": "anchor"}
_C = {"dir": "out", "at": "cand"}
EDGE_PROBE_READS = {
    "edge_probe":     ([dict(_A), dict(_B)], ("reverse_adjacency",)),
    "edge_probe_out": ([dict(_A), dict(_C, unless_found=True)], ()),
}

MEMBERSHIP_RULES = [
    # expand the anchor's adjacency once, flag rows by set membership (cost ~ |adjacency|)
    {"name": "materialize_intersect", "logical_op": "membership_flag"},
    # probe the edge per tested row (read_edge_exists, cost ~ |rows|): one candidate per list of reads
    *({"name": name, "logical_op": "membership_flag", "reads": reads, "validity_caps": list(caps)}
      for name, (reads, caps) in EDGE_PROBE_READS.items()),
]

# filter_by_label is ALSO a cost choice (round #1): the caps-forced lowering becomes two
# measured candidates. filter_index needs the DESIGN to declare the type index for the
# (single) label (or the format's caps to grant one). Every legal rule is a body of the query's generated plan file
# (plan_choice/generate.py); the pick record names the one a store runs.
FILTER_CHOICE_RULES = [
    {"name": "filter_index", "logical_op": "filter_by_label", "requires_type_index": True},
    {"name": "filter_probe", "logical_op": "filter_by_label"},
    # the filter FUSED into the props read it feeds: one read per candidate under the label-scoped key, a not-null test
    # on a non-nullable prop keeps the label's rows (planir/lowering.py fusable_filters / fuse_filters, the same
    # rewrite as a registry's fused twin). Legal only where the filter's single consumer is such a read (plan
    # context); valid only on a format whose props read under another label misses (validity cap).
    {"name": "filter_fused", "logical_op": "filter_by_label", "fuses": "read_vertex_props",
     "validity_caps": ["label_scoped_props"]},
]

WINDOW_RULES = [
    # enumerate the window from the sorted index, intersect with the candidates
    # (wins when the candidate set >> the window -- ic3's shape)
    {"name": "range_index", "logical_op": "window_narrow", "requires_sorted_index": True},
    # NOT offered: full_read, no narrowing (the downstream per-candidate date read + filter does the window). Never the
    # cheapest body on the SF3 captures: ic3's two full_read bodies were removed. A design with
    # no sorted date index has no window_narrow rule, so no body of such a query is generated for it
]

# Ops the search must NEVER emit (the op vocabulary): EXECUTE-ONLY on the C++ side, with no rule in
# predict mode's cardinality layer (cpp/kvevaluator/src/predict/cardinality.cc). A rule whose expansion emits one of
# these is not cost-searchable; the pre-gate asserts the whole catalog stays clear (a rule that needs one must first
# grow the cardinality layer).
EXECUTE_ONLY_OPS = frozenset({"index_range_buckets"})

# The type-filter pair (today's queries.lower_filter), recorded as catalog data.
# NOT wired as a choice point: which op is legal is caps-FORCED per (format, design),
# so it stays a lowering (queries.lower_filter / dump_planir.lower_ir_steps), not a
# cost decision.
FILTER_RULES = [
    {"name": "index_probe", "logical_op": "filter_by_label",
     "physical_op": "read_filter_by_label_index", "single_label_only": True,
     "requires_caps": ["type_index_single_posting"]},
    {"name": "probe", "logical_op": "filter_by_label",
     "physical_op": "read_filter_by_label_probe"},
]


def choice_points(plan):
    """The steps of a logical plan that carry >1 physical alternative: rank and
    entry_eq steps, plus fetch_props markers NOT governed by a rank step (with a
    rank in the plan, the marker follows the rank rule's fetch field)."""
    has_rank = any(s.op == "rank" for s in plan.steps)
    pts = []
    for s in plan.steps:
        if (s.op in ("rank", "entry_eq", "window_narrow", "membership_flag",
                     "filter_by_label")
                or (s.op == "fetch_props" and not has_rank)):
            pts.append(s)
    return pts


def legal_rules(step, schema, plan=None):
    """The legal rules for one choice point on one physical design. `plan` (the logical plan holding the step)
    decides the rules that need context: filter_fused is offered only where the filter feeds a fusable props read
    (without the plan it is never offered). A rule whose `dominated_by` rule is legal here is dropped (no cost is
    read: the dominance holds by construction, the catalog's comment on the rule says why)."""
    legal = _legal_rules(step, schema, plan)
    names = {r["name"] for r in legal}
    return [r for r in legal if r.get("dominated_by") not in names]


def _legal_rules(step, schema, plan=None):
    if step.op == "rank":
        keyprop = step.args["keys"][0][0]
        have = all((lbl, keyprop) in schema.date_cols for lbl in step.args["labels"])
        return [r for r in RANK_RULES
                if not (r.get("requires_sorted_index") and not have)]
    if step.op == "entry_eq":
        have = (step.args["label"], step.args["prop"]) in schema.indexed_cols
        return [r for r in ENTRY_RULES
                if not (r.get("requires_value_index") and not have)]
    if step.op == "window_narrow":
        have = all((lbl, step.args["prop"]) in schema.date_cols
                   for lbl in step.args["labels"])
        return [r for r in WINDOW_RULES
                if not (r.get("requires_sorted_index") and not have)]
    if step.op == "fetch_props":
        return list(FETCH_RULES)
    if step.op == "membership_flag":
        return list(MEMBERSHIP_RULES)
    if step.op == "filter_by_label":
        single = isinstance(step.args.get("label"), str)
        have = single and step.args["label"] in getattr(schema, "type_indexed_labels", ())
        fusable = plan is not None and step.out in fusable_filters(plan.steps, nonnull_of(schema), plan.output)
        return [r for r in FILTER_CHOICE_RULES
                if not (r.get("requires_type_index") and not have)
                and not (r.get("fuses") and not fusable)]
    raise ValueError(f"not a choice point: {step.op}")


def validity_ok(logical, choice, caps_of_label):
    """Does a format hold every validity cap the candidate's rules need, on the LABEL each rule acts on
    (`caps_of_label` = kvevaluator.formats.caps.caps_resolver(fmt, schema): a format's caps can differ per label)?"""
    by_name = {r["name"]: r for table in (RANK_RULES, ENTRY_RULES, FETCH_RULES, MEMBERSHIP_RULES,
                                          FILTER_CHOICE_RULES, WINDOW_RULES) for r in table}
    for s in choice_points(logical):
        need = by_name.get(choice.get(s.out), {}).get("validity_caps", ())
        if not need:
            continue
        # the name the rule's caps are checked on: the step's label(s); a membership rule's, the EDGE TYPE it reads
        lab = s.args["etype"] if s.op == "membership_flag" else s.args.get("label", s.args.get("labels"))
        for label in ([lab] if isinstance(lab, str) else list(lab or ())):
            if not set(need) <= caps_of_label(label):
                return False
    return True


def expand(plan, choice, schema=None):
    """Lower a logical Plan to a physical Plan under `choice` = {choice-point out
    name -> rule name}, one entry per choice point (may be empty for a plan with
    none -- the expansion is then a passthrough copy). Emits fresh Step objects
    (arg VALUES -- lists, Refs, Exprs -- are shared with the logical plan, safe
    because bind()'s IR round-trip rebuilds every container before a candidate
    runs; a rule that mutated an arg in place would corrupt later candidates --
    don't). Legality is the CALLER's job via legal_rules; the caller applies
    queries.lower_filter afterwards, exactly like the hand path. `schema` (the compiled design) is needed only by a
    filter_fused choice: its not-null test prop comes from the schema's non-nullable props."""
    rank_rules = {r["name"]: r for r in RANK_RULES}
    ranks = [s for s in plan.steps if s.op == "rank"]
    if len(ranks) > 1:
        raise ValueError(f"one rank step per plan, got {[s.out for s in ranks]}")
    rank_fetch = rank_rules[choice[ranks[0].out]]["fetch"] if ranks else None

    # pre-scan the fetch markers: which are kept, which upstream reads widen
    renames, widen = {}, {}
    for s in plan.steps:
        if s.op != "fetch_props":
            continue
        mode = rank_fetch or ("deferred" if choice[s.out] == "fetch_deferred" else "inline")
        if mode != "deferred":
            renames[s.out] = s.args["rows"].name
            if rank_fetch is None:               # standalone inline: fold into the named read
                widen.setdefault(s.args["widen"], []).extend(s.args["props"])

    steps = []
    for s in plan.steps:
        if s.op == "rank":
            steps += _expand_rank(s, rank_rules[choice[s.out]])
        elif s.op == "entry_eq":
            steps += _expand_entry(s, choice[s.out])
        elif s.op == "window_narrow":           # range_index, the one rule
            steps += [
                Step(s.args["idx_name"], "read_index_range", labels=s.args["labels"],
                     prop=s.args["prop"], lo=s.args["lo"], hi=s.args["hi"]),
                Step(s.out, "intersect", items=s.args["vids"],
                     other=ref(s.args["idx_name"])),
            ]
        elif s.op == "membership_flag":
            steps += _expand_membership(s, choice[s.out])
        elif s.op == "filter_by_label":
            c = choice[s.out]
            if c in ("caps_default", "filter_fused"):   # caps_default: keep LOGICAL, lower per caps (emit path);
                steps.append(Step(s.out, s.op, **s.args))   # filter_fused: kept until the fuse pass below
            else:
                op = ("read_filter_by_label_index" if c == "filter_index"
                      else "read_filter_by_label_probe")
                steps.append(Step(s.out, op, **s.args))
        elif s.op == "fetch_props":
            if s.out not in renames:
                steps += _expand_fetch(s)
        elif s.out in widen:
            args = dict(s.args)
            args["props"] = list(args["props"]) + widen[s.out]
            steps.append(Step(s.out, s.op, **args))
        else:
            steps.append(Step(s.out, s.op, **s.args))
    fused = [s.out for s in plan.steps if s.op == "filter_by_label" and choice.get(s.out) == "filter_fused"]
    if fused:
        if schema is None:
            raise ValueError(f"filter_fused on {fused} needs the schema (its non-nullable props): expand(..., schema=)")
        pairs = fusable_filters(steps, nonnull_of(schema), plan.output)
        missing = [f for f in fused if f not in pairs]
        if missing:                              # legal_rules offers the rule only where this holds
            raise ValueError(f"filter_fused on {missing}: the filter does not feed a fusable props read")
        steps = fuse_filters(steps, {f: pairs[f] for f in fused})
    names = [s.out for s in steps]
    dups = {n for n in names if names.count(n) > 1}
    if dups:                                     # run_plan overwrites duplicates SILENTLY
        raise ValueError(f"expansion produced duplicate step names {sorted(dups)} -- "
                         f"a logical step collides with a rule template's name")
    missing_widen = set(widen) - set(names)
    if missing_widen:                            # a typo'd widen target would silently
        raise ValueError(f"widen targets not in the expansion: {sorted(missing_widen)}")
    # renames may CHAIN (a marker whose rows ref another dropped step): fixed point
    for k in list(renames):
        seen = {k}
        while renames[k] in renames:
            if renames[renames[k]] in seen:
                raise ValueError(f"rename cycle through {k!r}")
            seen.add(renames[k])
            renames[k] = renames[renames[k]]
    out_name = renames.get(plan.output, plan.output)
    steps = [_rewire(s, renames) for s in steps]
    dangling = {r.name for s in steps for r in _refs_of(s.args)} - set(names)
    if dangling:
        raise ValueError(f"expansion left dangling refs: {sorted(dangling)}")
    return Plan(steps, params=plan.params, let=plan.let, output=out_name)


def _expand_rank(s, rule):
    a = s.args
    keyprop = a["keys"][0][0]
    id_order = a["keys"][1][1]
    if rule["fetch"] == "fused":             # index_walk
        if a["keys"][1][0] != "id":          # the walk's tie-break prop is hardcoded to id
            raise ValueError(f"index_walk ties on id; rank keys say {a['keys'][1][0]!r}")
        kw = {"vids": a["vids"], "prop": keyprop, "k": a["k"]}
        if "hi" in a:
            kw["hi"] = a["hi"]
        kw["labels"] = a["labels"]
        kw["props"] = a["body_props"]
        if id_order != "asc":
            kw["id_order"] = id_order
        return [Step(s.out, "read_top_k_by_index", **kw)]
    props = list(a["rank_props"])
    if rule["fetch"] == "inline":            # one_phase: the rank read carries the body
        props += a["body_props"]
    steps = [Step("ranked", "read_vertex_props", vids=a["vids"], props=props,
                  label=a["read_label"])]
    src = "ranked"
    if "hi" in a:
        steps.append(Step("before_max", "filter", rows=ref(src),
                          pred=lt(field(keyprop), a["hi"])))
        src = "before_max"
    steps.append(Step(s.out, "top_k", rows=ref(src), keys=a["keys"], k=a["k"]))
    return steps


def _expand_entry(s, rule_name):
    a = s.args
    if rule_name == "index_eq":
        return [Step(s.out, "read_index_eq", label=a["label"], prop=a["prop"],
                     value=a["value"])]
    # label_scan: enumerate the label, read the prop off every vertex, keep the
    # equality matches (the per-vertex read IS the price of the missing index)
    return [
        Step(s.out + "_scan", "read_vertices_by_label", label=a["label"]),
        Step(s.out + "_pr",   "read_vertex_props", vids=ref(s.out + "_scan"),
             props=[a["prop"]], label=a["label"]),
        Step(s.out + "_fx",   "filter", rows=ref(s.out + "_pr"),
             pred=eq(field(a["prop"]), a["value"])),
        Step(s.out,           "pluck", rows=ref(s.out + "_fx"), field="_vid"),
    ]


def _expand_membership(s, rule_name):
    a = s.args
    if rule_name in EDGE_PROBE_READS:
        if a.get("dir", "both") != "both":
            # every candidate's reads cover BOTH halves; a directed membership would silently diverge from materialize
            raise ValueError(f"{rule_name} supports dir='both' only, got {a.get('dir')!r}")
        kw = {"rows": a["rows"], "field": a["field"], "etype": a["etype"],
              "into": a["into"]}
        if "anchors" in a:
            kw["others"] = a["anchors"]
        else:
            kw["via_srcs"] = a["via_srcs"]
            kw["via_etype"] = a["via_etype"]
        kw["reads"] = [dict(r) for r in EDGE_PROBE_READS[rule_name][0]]   # last: where the default lowering puts them
        return [Step(s.out, "read_edge_exists", **kw)]
    # materialize_intersect: expand the anchor set's adjacency, dedup, flag_in
    steps = []
    if "anchors" in a:
        srcs = a["anchors"]
    else:
        # the VIA anchor is read only when there are rows to flag (`gate`: a data dependency, free): a message
        # with no replies then costs the replies SEEK alone, as edge_probe already does (read_edge_exists resolves
        # its via_srcs only when rows exist). An unconditional read would be a cost the RULE adds, not one the layout
        # forces.
        steps.append(Step(a["via_name"] + "_src", "gate", items=a["via_srcs"], when=a["rows"]))
        steps.append(Step(a["via_name"], "read_neighbors", srcs=ref(a["via_name"] + "_src"),
                          etype=a["via_etype"], dir="out"))
        srcs = ref(a["via_name"])
    steps += [
        Step(a["set_read_name"], "read_neighbors", srcs=srcs, etype=a["etype"],
             dir=a.get("dir", "both")),
        Step(a["set_name"], "dedup", items=ref(a["set_read_name"])),
        Step(s.out, "flag_in", rows=a["rows"], field=a["field"],
             allowed=ref(a["set_name"]), into=a["into"]),
    ]
    return steps


def _expand_fetch(s):
    a = s.args
    read_name = a.get("read_name", "bodies")
    steps = []
    if "pluck" in a:
        steps.append(Step(a["pluck"], "pluck", rows=a["rows"], field="_vid"))
        vids = ref(a["pluck"])
    else:
        vids = a["vids"]
    steps += [
        Step(read_name, "read_vertex_props", vids=vids, props=a["props"], label=a["label"]),
        Step(s.out, "zip_rows", left=a["rows"], right=ref(read_name)),
    ]
    return steps


def _refs_of(v):
    """Every Ref inside an args value (lists/dicts included)."""
    if isinstance(v, Ref):
        return [v]
    if isinstance(v, list):
        return [r for x in v for r in _refs_of(x)]
    if isinstance(v, dict):
        return [r for x in v.values() for r in _refs_of(x)]
    return []


def _rewire(s, renames):
    """A fresh Step with every Ref to a dropped step's output pointing at its
    forwarded source instead (refs may sit inside lists/dicts of args)."""
    if not renames:
        return s

    def fix(v):
        if isinstance(v, Ref):
            return ref(renames.get(v.name, v.name))
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v
    return Step(s.out, s.op, **{k: fix(v) for k, v in s.args.items()})


def dump_catalog():
    """The catalog as JSON -- the language-neutral artifact a C++ driver would read."""
    tables = {"rank": RANK_RULES, "entry_eq": ENTRY_RULES,
              "window_narrow": WINDOW_RULES, "fetch_props": FETCH_RULES,
              "membership_flag": MEMBERSHIP_RULES,
              "filter_choice": FILTER_CHOICE_RULES,
              "filter_by_label": FILTER_RULES}
    return json.dumps(tables, indent=2)


def legal_choices(logical, schema):
    """[(label, choice)] -- the cartesian product of every choice point's legal
    rules on this design. label = '+'-joined rule names in point order (a plain
    rule name for a one-point plan); choice = {point out name -> rule name}.
    A plan with no choice point has the one candidate ('', {})."""
    import itertools
    points = choice_points(logical)
    per_point = [[r["name"] for r in legal_rules(s, schema, logical)] for s in points]
    return [("+".join(combo), dict(zip([s.out for s in points], combo))) for combo in itertools.product(*per_point)]

