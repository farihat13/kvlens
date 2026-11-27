"""Named BI query plans (query = plan) -- one module per query in this package, each exposing SIM
({name: builder}); this __init__ merges them into REGISTRY. A builder returns a parameterized Plan that
every format runs (one shared logical plan; a per-format override dict {"default": b, "<format>": b} is
allowed only when it makes the query genuinely cheaper for that format, as in the LDBC registry).
The logical `filter_by_label` is lowered per format at bind time (kvevaluator.plans.ir.lowering), as in the siblings.

GROUPS: the figure bands, in suite order (the whole-graph scans, the tag-anchored fan-outs, the
class / country anchored ones); QUERIES is its flattening. Only built queries are listed."""
from kvevaluator.formats.caps import caps_for
from kvevaluator.plans.ir.lowering import lower_filter
from kvevaluator.workloads import merge_query_modules

REGISTRY = merge_query_modules(__name__, __path__)["REGISTRY"]   # every query file's SIM (BI has no LOGICAL yet)

GROUPS = [
    {"key": "scan", "title": "Whole-graph scans", "figure": "", "queries": ["bi1", "bi12", "bi9"]},
    {"key": "tag", "title": "Tag-anchored", "figure": "", "queries": ["bi2", "bi5", "bi7", "bi8"]},
    {"key": "class", "title": "Class and country anchored", "figure": "", "queries": ["bi3"]},
    {"key": "country", "title": "Country anchored", "figure": "", "queries": ["bi14", "bi11"]},
    {"key": "batch2", "title": "Second batch", "figure": "", "queries": ["bi6", "bi4", "bi13", "bi18", "bi16", "bi17", "bi10"]},
]
QUERIES = [q for g in GROUPS for q in g["queries"]]

# twin -> base: the same question by another access path (the type-pass scan vs the date index), measured beside
# the base and priced as the cheaper per currency, per format (the candidate set the pick rule chooses from)
ALTERNATIVES = {"bi1_scan": "bi1", "bi12_scan": "bi12", "bi16_index": "bi16",
                "bi12_source": "bi12", "bi9_source": "bi9", "bi3_source": "bi3"}    # the per-source chase, the record of that op
# the capability a query's non-fallback variant needs (the IR dumper's `requires`): the label-scoped bodies
VARIANT_REQUIRES = {"bi1": "label_scoped_props", "bi12": "label_scoped_props"}
TWIN_REQUIRES = {}
# The fused-scan body of these three reads a WHOLE label with its props (Post and Comment; Forum), so it is valid
# wherever the label's rows are one key run, not only where the scan is the layout's enumeration (label_scan_carries_props:
# Chunked props only). Its variant names whole_label_scan_by_default (every props layout of meta, pertype_csr), as
# microbench's whole-label scans do (FUSED_SCAN_REQUIRES there); the dumper derives the selector as before and emits
# this cap in its place. ArangoDB holds label_rows_one_run, so the body is a valid candidate there, not its default.
FUSED_SCAN_REQUIRES = {q: "whole_label_scan_by_default" for q in ("bi1_scan", "bi4", "bi12_scan", "bi12_scan_run")}
# The LABEL-RUN twins: a step that reads the properties of many vertices
# by one point read each (bi9's window, bi13's and bi2's messages, bi12's roots: 0.3-2.0M per param at sf3) also runs
# as one scan of the label's key run kept to those vertices (_shared.with_label_run, as ic10_fof_from_person_run). One
# twin per body some properties layout picks; valid where a label's records are one key run; picked by cost.
for _twin, _base in (("bi9_run", "bi9"), ("bi9_source_run", "bi9"), ("bi2_run", "bi2"), ("bi12_scan_run", "bi12"),
                     ("bi13_run", "bi13")):
    ALTERNATIVES[_twin] = _base
    TWIN_REQUIRES[_twin] = ("label_rows_one_run",)
# bi9's twins enter through the per-label date index with no label filter (the typed-index builder): the index must be
# the type filter, as in bi9's own per_type_index body
for _twin in ("bi9_run", "bi9_source_run"):
    TWIN_REQUIRES[_twin] = ("label_rows_one_run", "per_type_index")
LOGICAL = {}
# the layouts whose type scan carries the rows: a scan twin's enumerate + props pair fuses into one read there
_FUSED = {"pertype_csr"}


def _pick(entry, fmt_name):
    """The builder for (entry, format): a per-format override, else the shared builder; a _FUSED format's builder
    fuses its label scans at the plan level, so the dumped IR carries the fused body (microbench's rule)."""
    builder = entry if not isinstance(entry, dict) else (entry.get(fmt_name) or entry["default"])
    if fmt_name in _FUSED:
        from kvevaluator.plans.ir.lowering import with_fused_label_scan
        return lambda args: with_fused_label_scan(builder(args))
    return builder


def raw_plan_for(name, fmt_name):
    """The builder's Plan for (query, format) BEFORE binding -- for the IR dumper / drift guard."""
    return _pick(REGISTRY[name], fmt_name)({})


def plan_for(name, fmt_name, args, schema):
    """The bound, lowered Step list one cell runs on the Python oracle."""
    from kvevaluator.plans.ir import bind
    steps = bind(_pick(REGISTRY[name], fmt_name)(args), args)
    return lower_filter(steps, caps_for(fmt_name, schema))
