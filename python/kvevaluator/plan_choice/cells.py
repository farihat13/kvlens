"""What plan choice knows of a store: the schema a workload's plans run on, the store's capabilities (a named
format's at its chunk, a meta cell's from its spec), and the body v1's selector ran on a cell."""
import json

from kvevaluator import workloads as W
from kvevaluator.capture.spec_sets import format_of


def workload_schema(workload):
    """the schema a workload's plans run on: its dataset's (the microbench and BI run on LDBC's)"""
    from kvevaluator import datasets
    return datasets.schema(W.get(workload).dataset)


def store_caps(workload, spec_path, chunk=None):
    """(caps, caps per label) of a store: a named format's own at its chunk (`chunk`, else the arm's, else the format's
    default store chunk); a meta cell's from its spec file, resolved now"""
    from kvevaluator.formats.caps import caps_for, caps_resolver
    schema = workload_schema(workload)
    if format_of(spec_path):
        from kvevaluator.plan_choice.optimizer.arms import split_arm, store_chunk_of
        fmt, arm_chunk = split_arm(format_of(spec_path))
        chunk = chunk if chunk is not None else (arm_chunk or store_chunk_of(fmt))
        return caps_for(fmt, schema, chunk=chunk), caps_resolver(fmt, schema, chunk=chunk)
    res = caps_resolver("meta", schema, spec=spec_path)
    labels = sorted(set(schema.vertex_props) | set(schema.etypes))
    of = {l: frozenset(res(l)) for l in labels}
    return frozenset(caps_for("meta", schema, spec=spec_path)), (lambda label: of.get(label, frozenset()))


def cell_selection(workload, spec_path, scale, queries, chunk=None):
    """{query: the body v1's selector ran on this cell (a bundle's body name, a bare plan's name), or None when no
    variant runs}: kvevaluator.plans.ir.bundle.select_variant at `scale` (the store's) over the cell's capabilities, from the same
    source as invalid_on_cell (a named format: its own caps, at its store's `chunk`; a meta cell: its spec). A
    generated file has no selector (its store's body is the pick record's): its selector is read from the plan file at
    pick_record.SELECTOR_TREE. For a capture with a planner_pick column only."""
    from kvevaluator.plan_choice.candidates import plan_files
    from kvevaluator.plans.ir.bundle import NoVariant, require_bare, select_variant
    from kvevaluator.plan_choice import pick_record

    def pick(q, caps, of_label):
        ir = json.load(open(plan_files(workload, q)[0]))
        if "plans" in ir and "variants" not in ir:
            ir = pick_record._plan_file_at(workload, q, pick_record.SELECTOR_TREE)
            if ir is None or "variants" not in ir:
                raise ValueError(f"{workload}/{q}: no selector at {pick_record.SELECTOR_TREE}")
        try:
            if "variants" not in ir:
                require_bare(ir, caps)                        # a bare plan's own `requires` (property_index)
                return ir.get("plan", q)
            return select_variant(ir, caps, of_label, str(scale))
        except NoVariant:
            return None

    caps, of_label = store_caps(workload, spec_path, chunk)
    return {q: pick(q, caps, of_label) for q in queries}
