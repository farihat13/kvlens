"""The body a query runs, from the pick record: for a workload whose plans are generated (Interactive), the record's
body of the query's plan file; for the microbench, a shape's cheapest plan (the record's body, else its hand plan). The
workloads bind a body they are handed (their bound_plan / bound_body) and never read the record."""
import json
import os

from kvevaluator import datasets
from kvevaluator import workloads as W
from kvevaluator.plan_choice import pick_record
from kvevaluator.plan_choice.candidates import candidate_builder, plans_dir
from kvevaluator.plan_choice.optimizer.arms import caps_of, resolver_of


def recorded_builder(workload, name, fmt_name, family, design=None, scale=None):
    """The body the pick record names for (query, store) at `scale` (None: the deciding scale's), built from the query's
    generated plan file; a query with one legal body on the store resolves to it (pick_record.body_for). None for a
    query the generator does not write (its file comes from dump_planir)."""
    if family != "canonical":
        raise ValueError(f"{name}: the {family} design is parked (its plans are not generated or recorded)")
    path = os.path.join(plans_dir(workload), f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        ir = json.load(f)
    if "generated_by" not in ir:
        return None
    schema = datasets.schema(W.get(workload).dataset, design or "canonical")
    body = pick_record.body_for(workload, fmt_name, name, ir, caps_of(fmt_name, schema), resolver_of(fmt_name, schema),
                                scale)
    b = candidate_builder(workload, name, body if body is not None else name)
    b.scales = []
    return b


def builder_for(workload, name, fmt_name, family="canonical", design=None, scale=None):
    """The producer for (query, format): the pick record's body of the query's generated plan file. A twin is a body
    of its base's file, not a query: refused. `design` = the physical design the plan runs under (None: the family's
    own design); `scale` = the store's dataset tag (None: the deciding scale's pick)."""
    base = W.queries_module(workload).ALTERNATIVES.get(name)
    if base is not None:
        raise ValueError(f"{name}: a twin is a body of {base}'s plan file, not a query: run {base} and name the body")
    b = recorded_builder(workload, name, fmt_name, family, design, scale)
    if b is None:
        raise ValueError(f"{name}: no generated plan file (python -m kvevaluator.plan_choice.generate --write)")
    return b


def plan_for(workload, name, fmt_name, schema, args, scale=None):
    """A query's plan for one format: the pick record's body (builder_for), bound and lowered by the workload
    (its bound_plan). `scale` = the store's dataset tag ("0.1" / "3" / "30"): the record's entry at that scale where one
    differs, else the deciding scale's."""
    builder = builder_for(workload, name, fmt_name, "canonical", schema.variant, scale)
    return W.queries_module(workload).bound_plan(name, fmt_name, schema, args, builder)


def resolve_for(workload, schema):
    """The plan resolver matching a physical DESIGN (not the schema NAME): a date-sorted-adjacency design is refused;
    an index-free design uses the label-only plans; else the pick record's (plan_for). Not by name: a plan that reads
    an index finds nothing on an index-free store and silently returns empty results. Partial index sets are not a
    supported design."""
    if schema.adj_sort or schema.adj_sort_facet:
        raise ValueError("a date-sorted adjacency design: no format or plan supports it "
                         "(tried and not adopted)")
    if not (schema.indexed_cols or schema.date_cols):
        return W.queries_module(workload).label_only_plan_for
    return lambda name, fmt_name, schema, args, scale=None: plan_for(workload, name, fmt_name, schema, args, scale)


def recorded_pick(workload, name, fmt_name):
    """the body the pick record names for (shape, format), or None when it names none (then the cheapest plan is the
    hand plan, as v1's search left it)"""
    e = pick_record.entry(workload, fmt_name, name)
    return e["body"] if e else None


def cheapest_plan_for(workload, name, fmt_name, args, schema=None):
    """A microbench shape's CHEAPEST plan -- what the <=-engine bound compares a live engine with: the body the pick
    record names for (shape, format) (recorded_pick; a format v1 let inherit another's pick is recorded so), else the
    hand plan. The workload's plan_for stays the shape's primitive measurement: a shape that pins a physical path to
    isolate it (hop1_label's per-candidate probe) keeps it there."""
    Q = W.queries_module(workload)
    body = recorded_pick(workload, name, fmt_name)
    if body is None:
        return Q.plan_for(name, fmt_name, args, schema=schema)
    return Q.bound_body(fmt_name, args, candidate_builder(workload, name, body), schema)
