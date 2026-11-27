"""The capability vocabulary a plan variant may `require`: format knobs stated as access-path properties, never format
names (a variant that needs a value + date range walk requires `sorted_range_index`; any format that has it qualifies).

Each format owns its capability list (Format.capabilities(schema)), which may depend on the design (dgraph holds
type_index_single_posting only when the schema declares the type index). The vocabulary is data
(inputs/contract/capabilities.json, read here and compiled into the C++ evaluator); each format's capability rules are
code on both sides, held equal by tests/unit/check_caps_parity.py.

Known limit: caps can select a per-format pick only when they tell apart the formats that need different picks. Two
formats with identical caps but different cheapest bodies would need an explicit format selector in the IR (not built).
"""
from kvevaluator import contract

# The vocabulary, each capability's class and the coarse-chunk threshold are the data file
# inputs/contract/capabilities.json (its entries hold what each capability means and the evidence for its class); the
# C++ evaluator compiles the same file in. Extend it deliberately.
_CAPS = contract.capabilities()
CAPABILITIES = frozenset(_CAPS)
# The pack target (bytes, Format.pack_target) from which a store holds `coarse_chunks` (the file's chunk_coarse_doc).
# NOT in the kvstats freshness key (measure/latency_fresh): changing it changes which body a chunked store selects, so bump
# the C++ counting versions with it (formats/registry.cc kFormatCounting).
CHUNK_COARSE = contract.chunk_coarse()

# VALIDITY vs SELECTOR (the file's class_doc): a capability is VALIDITY when a body that relies on it returns
# WRONG rows, or fails, on a format without it -- so a measured choice among a bundle's bodies may never pick a body whose
# validity caps the format lacks. A SELECTOR only picks the cheaper body.
VALIDITY_CAPS = frozenset(c for c, e in _CAPS.items() if e["class"] == "validity")
SELECTOR_CAPS = frozenset(c for c, e in _CAPS.items() if e["class"] == "selector")
# Validity caps the plan dumper stamps from a body's OPS at emit (planir/bundle.py with_property_index_requires), not from a
# pick's rules: a pin reading a committed twin's file never inherits them as its guard (candidates.candidate_builder),
# or each dump would feed the last one's stamps back into the fallback body's guard.
STAMPED_AT_EMIT = frozenset(c for c, e in _CAPS.items() if e.get("stamped_at_emit"))
# the STORE caps: derived from the pack target / the schema (store_caps), never declared by a format
STORE_CAPS = frozenset(c for c, e in _CAPS.items() if e.get("store_derived"))
# a selector whose bodies still need a validity cap: the validity caps of a body = (requires & VALIDITY_CAPS) | these
VALIDITY_IMPLIED = {c: frozenset(e["implies"]) for c, e in _CAPS.items() if e.get("implies")}


def validity_caps(requires):
    """The validity caps a body with these `requires` relies on (a fallback body's unnamed needs are not here: a
    candidate that fails when run is invalid too)."""
    out = set(requires) & VALIDITY_CAPS
    for c in requires:
        out |= VALIDITY_IMPLIED.get(c, frozenset())
    return frozenset(out)


# ---- the KV-op contract ------------------------------------------------------------------------------------------------
# Every KV op a plan step may name: (kind, served by every format, {arg: REQUIRED | OPTIONAL | its default}), from the
# data file inputs/contract/kv_ops.json (the C++ evaluator compiles the same file in). A plan step's missing optional arg
# takes the default given there.
REQUIRED, OPTIONAL = "<required>", "<optional>"
KV_OPS = {op["name"]: (op["kind"], op["every_format"],
                       {a["name"]: REQUIRED if a["required"] else a.get("default", OPTIONAL) for a in op["args"]})
          for op in contract.kv_ops()}
DEFAULT_DIR = KV_OPS["read_neighbors"][2]["dir"]
KV_READ_OPS = frozenset(op for op, (kind, _, _) in KV_OPS.items() if kind == "read")
KV_WRITE_OPS = frozenset(op for op, (kind, _, _) in KV_OPS.items() if kind == "write")
# per read op, the validity caps a body that runs it needs (the file's `requires`): an index read over labels needs a
# per-type index, a range walk its range index, ...; property_index is stamped at emit (STAMPED_AT_EMIT)
OP_REQUIRES = {op["name"]: frozenset(op["requires"]) for op in contract.kv_ops() if op["kind"] == "read"}
