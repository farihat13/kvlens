"""Lowering a LOGICAL plan op to the PHYSICAL access path a format's capabilities allow --
the one rule every registry and the IR dumper share, so the python run and the emitted IR
can never lower differently.

filter_by_label (the logical type filter) becomes `read_filter_by_label_index` (enter by the
type index once, intersect) when the format holds `type_index_single_posting` and the label is
one label, else `read_filter_by_label_probe` (probe each candidate). The access path is the op NAME,
so an IR file carries it and a format only executes what it is handed; a builder that wants a
fixed path names the physical op itself. Why the split falls this way: (dgraph's type posting makes index-once 1 SEEK; nebula's
per-vertex index makes it a whole-type scan; arango is free either way).

`via="index"` on the logical step widens the rule to `type_index` (a type index of ANY kind):
the index op also on a format whose type index is per-vertex (nebula, the chunked layout),
where it is the whole-type SCAN (1 SEEK + |type| NEXT). Which of scan and probe is cheaper there depends on the candidate
count vs |type| and on the currency, which no static rule knows -- so a query that wants nebula
priced at the cheaper of its two paths is measured twice (the plain twin and the via="index"
twin) and the analysis takes the cheaper per currency. The hint is consumed here: the physical
step carries only the op name.
"""

TYPE_CAP = "type_index_single_posting"
ANY_TYPE_CAP = "type_index"

# read_edge_exists (is there an edge between a row's candidate and an anchor, either way): the PLAN names the reads
# (formats/base.py read_edge_exists; C++ EdgeRead). A builder that leaves `reads` out gets the DEFAULT order for the
# edge type's adjacency caps (formats.caps.edge_caps_of: an `on`-style lookup by edge-type name) -- the reads each
# layout did before the reads were written in the plan:
#   lists in every stored direction, in lists kept (adjacency_lists + reverse_adjacency):
#       the anchor's out list and in list, both always             (dgraph, arangodb, pertype_csr, meta lists)
#   otherwise (a per-edge key in some direction, or no in lists):
#       out@anchor, then out@cand for the candidates not found     (meta Split; nebula; a list store with no in lists)
# A measured pick (optimizer/rules.py MEMBERSHIP_RULES) names its reads itself.
LIST_CAP, REVERSE_CAP = "adjacency_lists", "reverse_adjacency"
READS_BOTH_LISTS = [{"dir": "out", "at": "anchor"}, {"dir": "in", "at": "anchor"}]
READS_OUT_KEYS = [{"dir": "out", "at": "anchor"}, {"dir": "out", "at": "cand", "unless_found": True}]


def default_edge_reads(etype, edge_caps):
    """the default reads of a read_edge_exists over `etype` (see above); `edge_caps`: etype -> its adjacency caps"""
    if edge_caps is None:
        raise ValueError(f"read_edge_exists over {etype!r} names no reads and the lowering was given no edge caps "
                         "(pass edge_caps=caps.edge_caps_of(format, schema))")
    caps = edge_caps(etype)
    return [dict(r) for r in (READS_BOTH_LISTS if LIST_CAP in caps and REVERSE_CAP in caps else READS_OUT_KEYS)]


def _index_op(label, via, caps):
    single = isinstance(label, str)
    index = TYPE_CAP in caps or (via == "index" and ANY_TYPE_CAP in caps)
    return "read_filter_by_label_index" if single and index else "read_filter_by_label_probe"


def with_index_filter(plan):
    """The same plan with via="index" on every logical type filter: a query's SCAN TWIN, measured
    beside the plain query so a per-vertex-index format is priced at the cheaper of its two
    filter paths per currency (a registry's ALTERNATIVES map names the twins). A Plan or a
    baked Step list; fresh Step objects, arg values shared."""
    from kvevaluator.plans.plan import Plan, Step

    def twin(steps):
        return [Step(s.out, s.op, **{**s.args, "via": "index"}) if s.op == "filter_by_label" else s
                for s in steps]
    if isinstance(plan, Plan):
        return Plan(twin(plan.steps), params=plan.params, let=plan.let, output=plan.output)
    return twin(plan)


def _refs_to(value, name):
    """does an arg value (nested lists / dicts / Exprs) reference the step output `name`?"""
    from kvevaluator.plans.plan import Ref
    if isinstance(value, Ref):
        return value.name == name
    if isinstance(value, (list, tuple)):
        return any(_refs_to(v, name) for v in value)
    if isinstance(value, dict):
        return any(_refs_to(v, name) for v in value.values())
    return False


FILTER_OPS = ("filter_by_label", "read_filter_by_label_index", "read_filter_by_label_probe")   # logical + both paths


def fusable_filters(steps, nonnull, output=None):
    """{filter step out: (its consumer's out, the non-nullable prop that tests membership)} for every single-label
    type filter (logical, or either physical path) whose ONLY consumer is `read_vertex_props(vids=it, label=L,
    props=P)` with a non-nullable prop of L in P -- the pattern a FUSED read replaces (the optimizer's filter_fused
    rule and a registry's fused twin both ask this one function). `nonnull`: {label: its non-nullable single-valued
    props}. `output` (the plan's output step): a filter that IS the output has another consumer, so it never fuses."""
    out = {}
    for i, s in enumerate(steps):
        if s.op not in FILTER_OPS or not isinstance(s.args.get("label"), str) or s.out == output:
            continue
        label = s.args["label"]
        users = [j for j, t in enumerate(steps) if j != i and any(_refs_to(v, s.out) for v in t.args.values())]
        if len(users) != 1:
            continue
        t = steps[users[0]]
        keep = [p for p in t.args.get("props") or [] if p in nonnull.get(label, ())]
        from kvevaluator.plans.plan import Ref
        if (t.op == "read_vertex_props" and isinstance(t.args.get("vids"), Ref) and t.args["vids"].name == s.out
                and t.args.get("label") == label and keep):
            out[s.out] = (t.out, keep[0])
    return out


def fuse_filters(steps, pairs):
    """The steps with each filter in `pairs` (fusable_filters' output, or a subset) FUSED into its props read:
    `read_vertex_props(vids=<the filter's input>)` named `<read>_cand` + a not-null filter on the tested prop, named
    as the read was (a vertex of another label reads nulls). Fresh Step objects for the rewritten pair."""
    from kvevaluator.plans.plan import Ref, Step
    from kvevaluator.plans.ir.authoring import field, notnull
    by_read = {read: (flt, prop) for flt, (read, prop) in pairs.items()}
    filters = {s.out: s for s in steps if s.out in pairs}
    out = []
    for t in steps:
        if t.out in pairs:
            continue
        if t.out in by_read:
            flt, prop = by_read[t.out]
            cand = f"{t.out}_cand"
            out.append(Step(cand, "read_vertex_props", **{**t.args, "vids": filters[flt].args["vids"]}))
            out.append(Step(t.out, "filter", rows=Ref(cand), pred=notnull(field(prop))))
        else:
            out.append(t)
    return out


def with_fused_label_props(plan, nonnull):
    """The same plan with each type filter (logical, or either physical path) FUSED into the props read it feeds:
    `filter_by_label(V, L)`
    whose only consumer is `read_vertex_props(vids=it, label=L, props=P)` becomes `read_vertex_props(vids=V,
    label=L, props=P)` + a filter on a non-nullable prop of L in P (a vertex of another label reads nulls). One read
    per candidate instead of a probe and a re-read of the same rows: a query's FUSED TWIN, measured beside the plain
    query (a registry's ALTERNATIVES + TWIN_TRANSFORM). `nonnull`: {label: its non-nullable props}. A filter that
    feeds anything else, or a read without such a prop, is left alone. A Plan or a baked Step list."""
    from kvevaluator.plans.plan import Plan

    def fuse(steps, output=None):
        return fuse_filters(steps, fusable_filters(steps, nonnull, output))
    if isinstance(plan, Plan):
        return Plan(fuse(plan.steps, plan.output), params=plan.params, let=plan.let, output=plan.output)
    return fuse(plan)


def fuse_label_scan(steps):
    """Rewrite `read_vertices_by_label(L)` immediately followed by `read_vertex_props(vids=that, label=L)` into one
    fused `read_vertices_by_label(L, props=...)` -- legal only when nothing else references the enumeration's
    output. A layout whose type scan carries the rows (label_scan_carries_props: the chunked per-type layouts) has
    every row in hand while enumerating, so a second pass over the same chunks is pure re-read. (From the
    microbench registry; shared by every workload with a scan twin.)"""
    from kvevaluator.plans.plan import Ref, Step
    out, i = [], 0
    while i < len(steps):
        a = steps[i]
        b = steps[i + 1] if i + 1 < len(steps) else None
        if (b is not None and a.op == "read_vertices_by_label" and b.op == "read_vertex_props"
                and isinstance(b.args.get("vids"), Ref) and b.args["vids"].name == a.out
                and b.args.get("label") == a.args.get("label")
                and not any(isinstance(v, Ref) and v.name == a.out
                            for s in steps[i + 2:] for v in s.args.values())):
            out.append(Step(b.out, "read_vertices_by_label", label=a.args["label"], props=b.args["props"]))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def with_fused_label_scan(plan):
    """fuse_label_scan over a builder's product, Plan (parameterized) or Step list (baked)."""
    from kvevaluator.plans.plan import Plan
    if isinstance(plan, Plan):
        return Plan(fuse_label_scan(plan.steps), params=plan.params, let=plan.let, output=plan.output)
    return fuse_label_scan(plan)


def nonnull_of(schema):
    """{label: its non-nullable single-valued props} from a compiled Schema (nonnull_props reads the same from a
    schema file). A multi-valued prop is left out: an empty list reads like a miss, so it cannot test the label."""
    return {label: {p for p in props
                    if (label, p) not in schema.nullable_cols and (label, p) not in schema.multival_cols}
            for label, props in schema.vertex_props.items()}


def nonnull_props(schema_path):
    """{label: its non-nullable single-valued props} from a schema file -- the fused twin's existence test"""
    import json
    s = json.load(open(schema_path))
    return {v["label"]: {p["name"] for p in v["props"] if p.get("nullable") is False and not p.get("multi")}
            for v in s["vertices"]}


def lower_filter(plan, caps, edge_caps=None):
    """In place, on bound Steps (the python runtime's plan). `edge_caps` (etype -> its adjacency caps) is needed only
    by a read_edge_exists step that names no reads."""
    for s in plan:
        if s.op == "filter_by_label":
            s.op = _index_op(s.args.get("label"), s.args.pop("via", None), caps)
        elif s.op == "read_edge_exists" and s.args.get("reads") is None:
            s.args["reads"] = default_edge_reads(s.args["etype"], edge_caps)
    return plan


def lower_ir_steps(steps, caps, edge_caps=None):
    """The same rules on IR step DICTS (the dumper's / the optimizer's form). Returns new dicts."""
    out = []
    for s in steps:
        if s.get("op") == "filter_by_label":
            s = dict(s)
            s["op"] = _index_op(s.get("label"), s.pop("via", None), caps)
        elif s.get("op") == "read_edge_exists" and s.get("reads") is None:
            s = dict(s)
            s["reads"] = default_edge_reads(s["etype"], edge_caps)
        out.append(s)
    return out
