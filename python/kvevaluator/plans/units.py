"""Attribute a plan step's KVCost to the TYPE UNIT it touches.

A mix decides a layout PER TYPE, so we need each costed op's cost tagged by
the type it names. Every costed operator names exactly one unit through a LITERAL arg
(never a Ref), so the tag is a static function of the step:

  EDGE unit   -- ops that traverse an edge type. The unit IS the etype (an edge is its
                 own assignable unit).
  VERTEX unit -- ops on a vertex type. The unit is the type GROUP (group_of(label)), so
                 Post/Comment collapse to one "Message" unit -- the same granularity the
                 spec assigns at.

Free ops (filter/dedup/top_k/join/...) do no KV work (KVCost == 0), so they have no unit;
this returns None for them. A costed op we cannot tag is a BUG, not a zero: `type_unit_of`
raises rather than silently dropping cost. That loud failure is how a new operator or a
step that forgot its label gets caught.
"""
from kvevaluator import contract
from kvevaluator.schema.vids import group_of

# op -> the arg naming its EDGE type (the op table's `unit`, of edge_type: inputs/contract/kv_ops.json). The unit is
# that etype verbatim.
_EDGE_ARG = {op["name"]: op["unit"]["arg"] for op in contract.kv_ops() if op.get("unit", {}).get("of") == "edge_type"}

# op -> the arg naming its VERTEX label (single label OR a candidate/label LIST; the table's `unit`, of vertex_label).
# The unit is group_of(label); a list must collapse to one group (all its labels share it).
_VERTEX_ARG = {op["name"]: op["unit"]["arg"] for op in contract.kv_ops() if op.get("unit", {}).get("of") == "vertex_label"}


def costed_ops():
    """Every operator name this module knows how to attribute (edge + vertex)."""
    return frozenset(_EDGE_ARG) | frozenset(_VERTEX_ARG)


def type_unit_of(step):
    """The unit `step` charges its cost to, or None for a free (0-KV) op.

    Raises on a costed op whose type arg is missing/None/a Ref, or whose label list spans
    two groups -- a costed step that cannot name its type is a bug to fix, not to swallow."""
    op = step.op
    if op in _EDGE_ARG:
        etype = step.args.get(_EDGE_ARG[op])
        if not isinstance(etype, str):
            raise ValueError(f"type_unit_of: {op} has non-literal/absent etype "
                             f"{etype!r} -- a costed edge op must name its etype")
        return etype
    if op in _VERTEX_ARG:
        val = step.args.get(_VERTEX_ARG[op])
        labels = (val,) if isinstance(val, str) else tuple(val) if val else ()
        if not labels or not all(isinstance(l, str) for l in labels):
            raise ValueError(f"type_unit_of: {op} has non-literal/absent label "
                             f"{val!r} -- a costed vertex op must name its label(s)")
        groups = {group_of(l) for l in labels}
        if len(groups) != 1:
            raise ValueError(f"type_unit_of: {op} label list {labels} spans groups "
                             f"{sorted(groups)} -- one op cannot charge two units")
        return groups.pop()
    return None                              # free op: no KV cost, no unit
