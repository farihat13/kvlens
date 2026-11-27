"""Capability-based variant selection + compile-time fragment inlining.

A query bundle carries `variants` (each `{"requires": [cap,...], "lacks": [cap,...]?, "plan": name}`)
and `plans` ({name: {steps,...}}). The loader picks the first variant whose requirements are a
subset of the running format's capabilities and whose `lacks` are disjoint from them -- a CODE
check, not a human note.
Fragments are inlined here (hygiene rename + input substitution) so the interpreter
only ever sees a flat step list.
"""
import json

from kvevaluator.plans.ir.capabilities import CAPABILITIES, OP_REQUIRES


class NoVariant(Exception):
    pass


def select_variant(bundle, format_caps, caps_of_label=None, scale=None):
    """A one-body IR (no `variants`: the generated files' bodies are named by the pick record) runs its body. Otherwise (the
    label_only files): the first variant whose `requires` hold, whose `lacks` do not, and whose `scales`, when it carries
    the key, list the store's `scale` (the dataset tag it was built from: "0.1", "3", LinkBench "n1m"). A variant without
    `scales` runs at every scale, so a scale-tagged variant is written before the untagged one for the same formats.
    `scale` None (or "") with a tagged variant whose caps match raises: a pin that differs by scale never guesses.
    A variant may carry `"on": {cap: [label, ...]}`; with a `caps_of_label` resolver (label -> capability set) such a cap
    is checked against the owner of each listed label, so a store with a layout per type selects by each type's layout.
    Un-annotated caps (and every caller without a resolver) check the store-wide set. `lacks` is the negative selector:
    it separates formats whose capability sets are subset-ordered, which no positive set can express.
    The C++ twin is ir/plan.cc (body_to_run, select_variant).
    """
    if "variants" not in bundle:          # a one-body IR (the body named by the caller): that body; several: refused
        plans = bundle.get("plans", {})
        if len(plans) != 1:
            raise ValueError(f"plan {bundle.get('plan')!r} has {len(plans)} bodies and no `variants`: name the body "
                             f"(pick_record.body_for) and send it alone (candidates.forced)")
        return next(iter(plans))
    caps = set(format_caps)
    scale = None if scale in (None, "") else str(scale)
    for v in bundle.get("variants", []):
        req, lacks = set(v.get("requires", [])), set(v.get("lacks", []))
        unknown = (req | lacks) - CAPABILITIES
        if unknown:
            raise ValueError(f"variant {v.get('plan')!r} names unknown "
                             f"capabilities {sorted(unknown)} (not in the vocabulary)")
        on = v.get("on", {})

        def holds(cap):
            if caps_of_label is not None and cap in on:
                return all(cap in caps_of_label(l) for l in on[cap])
            return cap in caps

        def lacking(cap):
            if caps_of_label is not None and cap in on:
                return all(cap not in caps_of_label(l) for l in on[cap])
            return cap not in caps

        if not (all(holds(c) for c in req) and all(lacking(c) for c in lacks)):
            continue
        if "scales" in v:
            # the scale decides only among variants whose caps match (a tagged variant for ANOTHER format never
            # touches this one); a matching tagged variant with no scale known refuses -- never a guess
            tags = v["scales"]
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags):
                raise ValueError(f"variant {v.get('plan')!r}: `scales` must be a non-empty list of strings, got {tags!r}")
            if scale is None:
                raise ValueError(f"variant {v.get('plan')!r} is scale-tagged {tags} but the store's scale is unknown: "
                                 f"pass the store's scale (its manifest's `scale` / the dataset tag)")
            if scale not in tags:
                continue
        return v["plan"]
    raise NoVariant(f"no variant runs here: global capabilities {sorted(caps)}"
                    + (" (per-label `on` checks also failed)" if caps_of_label else ""))


def require_bare(plan, format_caps):
    """A bare plan's own `requires` (store caps it needs, stamped by dump_planir: property_index) must all be held;
    raises NoVariant otherwise, as a bundle with no fitting variant does. Mirrors C++ ir/plan.cc flatten_ir."""
    req = set(plan.get("requires", []))
    unknown = req - CAPABILITIES
    if unknown:
        raise ValueError(f"plan {plan.get('plan')!r} requires unknown capabilities {sorted(unknown)}")
    if not req <= set(format_caps):
        raise NoVariant(f"no variant runs here: the plan requires {sorted(req - set(format_caps))}")


# the ops that read a PROPERTY index (their `requires` names property_index: inputs/contract/kv_ops.json): a body with one
# is valid only on a store that declares property indexes
PROPERTY_INDEX_OPS = frozenset(op for op, req in OP_REQUIRES.items() if "property_index" in req)


def with_property_index_requires(ir):
    """Every body that reads a property index requires `property_index` (a store cap: the schema declares property
    indexes), so a design without them (label_only, ...) never selects it: there the read throws or,
    on dgraph, returns nothing. A bundle's variant gets it in its `requires`; a bare plan gets a top-level `requires`
    (require_bare; C++ flatten_ir), so its name and every capture's name stay as they were. On every
    store that holds the cap the selection is unchanged. tests/unit/check_plan_ops_supported.py holds the rule
    per (label, prop). Every workload's plan dumper calls it (python/kvevaluator/plan_choice/dump_planir.py, workloads/job/dump_plans.py)."""
    def reads_index(plan):
        return any(s.get("op") in PROPERTY_INDEX_OPS
                   for s in expand_fragments(plan, ir.get("fragments", [])).get("steps", []))

    if "variants" not in ir:
        if reads_index(ir) and "property_index" not in ir.get("requires", []):
            ir["requires"] = sorted(set(ir.get("requires", [])) | {"property_index"})
        return ir
    for v in ir["variants"]:
        if reads_index(ir["plans"][v["plan"]]) and "property_index" not in v.get("requires", []):
            v["requires"] = sorted(set(v.get("requires", [])) | {"property_index"})
    return ir


def covers(bundle, format_caps, caps_of_label=None, scale=None):
    """Does any variant of `bundle` run for a format with these caps? The ONE coverage rule
    (built on select_variant, so the unknown-capability raise and the `on` annotations are
    honored) -- every resolver deciding "dated family or fallback?" must use this, never a
    local requires-subset re-implementation. A bare plan covers every format that holds its own `requires`."""
    if "variants" not in bundle:
        try:
            require_bare(bundle, format_caps)
            return True
        except NoVariant:
            return False
    try:
        select_variant(bundle, format_caps, caps_of_label, scale)
        return True
    except NoVariant:
        return False


# the IR's plan-level fields the C++ reads besides a body (ir/plan.cc prepare_plan: bind, let, params); body_sha covers
# only {steps, output}, so a `let` edit changes what runs under the same body sha
PLAN_LEVEL_KEYS = ("bind", "let", "params")
# what a plan file says about its query, not what runs: the English question and the query whose params it borrows
# (kvevaluator.workloads.with_query_fields). No freshness key or provenance sha covers them.
QUERY_FIELDS = ("question", "params_like", "estimate_hints")   # the query's own fields: no body or whole-file key hashes them


def file_sha(ir, n=12):
    """sha256 of a plan file's content, key-sorted, without its QUERY_FIELDS: what a whole-file key hashes"""
    import hashlib
    body = {k: v for k, v in ir.items() if k not in QUERY_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:n]


def plan_level(ir):
    """the IR's plan-level fields, each dict as an ordered (name, value) list: C++ binds `let` in file order, each
    against the earlier ones, and a key-sorted dump would lose that order"""
    return {k: list(ir[k].items()) if isinstance(ir[k], dict) else ir[k] for k in PLAN_LEVEL_KEYS if k in ir}


def run_identity(ir):
    """what running this IR file can execute, by content, for a freshness key: each body's sha (body_sha), the
    plan-level fields (plan_level) and the selectors (a bundle's variants without their doc fields; a bare plan's own
    `requires`). A doc-field or formatting edit leaves it equal."""
    if "plans" in ir:
        bodies = {b: body_sha(ir, b) for b in ir["plans"]}
        selectors = [{k: v[k] for k in ("plan", "requires", "lacks", "on", "scales") if k in v} for v in ir.get("variants", [])]
    else:
        bodies = {ir.get("plan", ""): body_sha(ir, None)}
        selectors = sorted(ir.get("requires", []))
    return {"bodies": bodies, "plan_level": plan_level(ir), "selectors": selectors}


def body_sha(ir, variant=None):
    """The identity of the body a store runs: sha256[:12] of the compact, key-sorted JSON of {steps, output} of the
    selected variant after fragment expansion (a bare plan: its own steps) -- the C++ twin is ir/plan.cc body_sha_of,
    which the response records as provenance.body_sha; the two serializers agree byte for byte because bodies carry
    no floats or non-ASCII text (checked by tests/gates/check_cpp_vs_python.py on every cell)."""
    import hashlib
    if variant is None and "steps" not in ir and "variants" not in ir and len(ir.get("plans", {})) == 1:
        variant = next(iter(ir["plans"]))         # a one-body IR: its body (ir/plan.cc body_to_run)
    plan = ir if "steps" in ir else ir["plans"][variant]
    flat = expand_fragments(plan, ir.get("fragments", []))
    body = {"steps": flat.get("steps", [])}
    if "output" in flat:
        body["output"] = flat["output"]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:12]


def expand_fragments(plan, fragments):
    """Inline every `{"use": frag, "args": {...}}` step into a flat step list.
    Loops to a fixpoint so a fragment that itself `use`s another is expanded too."""
    by_name = {f["fragment"]: f for f in fragments}
    steps = plan.get("steps", [])
    depth = 0
    while any("use" in s for s in steps):
        depth += 1
        if depth > 100:
            raise ValueError("fragment expansion did not terminate (a cycle?)")
        out = []
        for s in steps:
            if "use" in s:
                if s["use"] not in by_name:
                    raise KeyError(f"unknown fragment {s['use']!r}")
                out.extend(_inline(s, by_name[s["use"]]))
            else:
                out.append(s)
        steps = out
    return {**plan, "steps": steps}


def _inline(use_step, fragment):
    host = use_step["name"]
    args = use_step.get("args", {})
    internal = {st["name"] for st in fragment["steps"]}
    export = fragment["output"]

    def sub(v):
        # {"in": x} -> the host-supplied arg; {"ref": internal} -> prefixed name
        if isinstance(v, dict) and len(v) == 1:
            (k, a), = v.items()
            if k == "in":
                if a not in args:
                    raise KeyError(f"fragment {fragment['fragment']!r} input {a!r} not supplied")
                return args[a]
            if k == "ref" and a in internal:
                return {"ref": f"{host}.{a}"}
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    steps = [{k: (f"{host}.{st['name']}" if k == "name" else sub(v)) for k, v in st.items()}
             for st in fragment["steps"]]
    # the fragment's exported output takes the HOST name (so a host ref to `host` binds it);
    # rewrite the export step's name AND every ref to it -- recursively, refs can be nested.
    old = f"{host}.{export}"
    renamed = []
    for st in steps:
        if st.get("name") == old:
            st = {**st, "name": host}
        renamed.append(_rewrite_ref(st, old, host))
    return renamed


def _rewrite_ref(v, old, new):
    """Replace every {"ref": old} with {"ref": new}, at any depth."""
    if isinstance(v, dict):
        if len(v) == 1 and v.get("ref") == old:
            return {"ref": new}
        return {k: _rewrite_ref(x, old, new) for k, x in v.items()}
    if isinstance(v, list):
        return [_rewrite_ref(x, old, new) for x in v]
    return v
