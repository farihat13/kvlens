"""The KV-formats: dgraph (predicate-oriented), nebula (vertex-oriented),
arangodb (document)."""
from kvevaluator.formats.base import Format
from kvevaluator.formats.dgraph import DgraphFormat, DgraphSizing
from kvevaluator.formats.nebula import NebulaFormat, NebulaSizing
from kvevaluator.formats.arangodb import ArangodbFormat, ArangoSizing
from kvevaluator.formats.pertype_csr import PerTypeCsrFormat
from kvevaluator.formats.meta import MetaFormat
from kvevaluator.formats.dgraph_engine import DgraphEngineFormat
from kvevaluator.formats.nebula_engine import NebulaEngineFormat
from kvevaluator.formats.arangodb_engine import ArangodbEngineFormat
from kvevaluator.plans.ir import capabilities as _CAPS   # the op table (inputs/contract/kv_ops.json)

# name -> class, the registry every driver selects formats from (--db / --format flags).
FORMATS = {"dgraph": DgraphFormat, "nebula": NebulaFormat, "arangodb": ArangodbFormat,
           # pertype_csr: the per-type chunked layout
           "pertype_csr": PerTypeCsrFormat,
           # meta: the config-driven C++ format (formats/meta.py) -- SIZE-ONLY, no
           # Python execution twin. Registered so --db meta validates and
           # make_format("meta", ...) works for capabilities(); QUERY_FORMATS excludes it below.
           "meta": MetaFormat}

# The VALIDATION whitelist for an explicit --db/--format: every registered format is valid to NAME.
# It is NOT the default sweep set (DEFAULT_DBS below is), so a newly registered format never joins every
# default sweep by itself. meta has NO read operators in Python, so QUERY_FORMATS excludes it: a driver that
# iterates QUERY_FORMATS would crash on its first read.
SIZE_ONLY_FORMATS = {"meta"}
QUERY_FORMATS = {n: c for n, c in FORMATS.items() if n not in SIZE_ONLY_FORMATS}

# The DEFAULT --db/--format set when none is passed -- the canonical cross-format comparison the README
# and the --db help promise. SEPARATE from the whitelist above by design: adding a specialty format to
# FORMATS makes it valid to name explicitly, but it NEVER leaks into a default sweep.
DEFAULT_DBS = ("dgraph", "nebula", "arangodb")
DEFAULT_WRITE_DBS = DEFAULT_DBS

# The Sizing dataclass per packable format family -- the ONE place make_format maps a format
# to the config it constructs with kind-1 value packing on. issubclass() (not identity) so a
# DERIVED format inherits its parent's sizing (dgraph_engine -> DgraphSizing) -- future-proofing:
# no engine format actually reaches make_format yet (ENGINE_FORMATS is a separate registry).
_PACK_SIZING = {DgraphFormat: DgraphSizing, NebulaFormat: NebulaSizing, ArangodbFormat: ArangoSizing}

def make_format(name, schema, pack=False):
    """Construct a format against `schema`, optionally with kind-1 (format-level) VALUE
    packing on: dgraph UidPack, nebula RowWriterV2 framing, arango VelocyPack. Packing is a
    pure ACCOUNTING toggle -- it changes value_bytes only, never the stored data or the op
    counts -- so a packed and an unpacked run share the same store."""
    cls = FORMATS[name]
    if pack:
        for base, sizing in _PACK_SIZING.items():
            if issubclass(cls, base):
                return cls(schema, sizing(pack=True))
        if issubclass(cls, PerTypeCsrFormat):
            # a composite: packing reaches its parts (the posting-index values it prices through dgraph)
            return cls(schema, pack=True)
    return cls(schema)

# The ENGINE-faithful counterparts: same store and schema, but the plan the real engine runs
# (workloads/ldbc/engine_plans.py). Kept in a SEPARATE registry so a sweep over FORMATS never
# silently picks one up -- they are a validation baseline, not a candidate design.
ENGINE_FORMATS = {"dgraph": DgraphEngineFormat, "nebula": NebulaEngineFormat,
                  "arangodb": ArangodbEngineFormat}

# --- KV operator surface: the ONE definition of "which methods perform storage ops" ---------
# The engine-format checks import from here, so the surface can't drift between two hand-lists --
# it did once, when the concrete names were mistyped in coverage_matrix (read_chase came out as
# `chase`), silently dropping three ops from every coverage count.
#
# KV_CONCRETE_OPS: concrete BASE methods that perform reads but aren't abstract -- they compose
# read_neighbors, so `__isabstractmethod__` doesn't flag them. MUST be the real Step op names.
KV_CONCRETE_OPS = frozenset({"read_top_k_by_index", "read_chase", "read_closure", "read_edge_exists"})

# KV_FORMAT_OPS: KV Step ops defined on SOME formats only, not the shared ABC -- so they are NOT in the shared surface
# every engine manifest must partition, but they ARE real operators the store-scan gate must recognize: the op table's
# ops not served by every format (inputs/contract/kv_ops.json; today `index_range_buckets`, dgraph's day-token range
# walk, which the microbench entry_range_dgraph plan uses to price dgraph's per-predicate date index precisely).
KV_FORMAT_OPS = frozenset(op for op, (_, every, _) in _CAPS.KV_OPS.items() if not every)

# KV_WRITE_OPS: the WRITE surface -- the op table's write operators plus the bulk-load ingest methods. Deliberately NOT
# folded into KV_BASE_OPS: the engine OVERRIDES/INHERITS manifests exist to declare how each engine's READ mechanics
# differ from the optimal plan, and no engine plan writes. Folding writes in would force every manifest to carry entries
# that say nothing. They are still REGISTERED, so the store-scan gate can recognise them and a new write operator
# cannot slip in unnoticed.
KV_WRITE_OPS = _CAPS.KV_WRITE_OPS | frozenset({"add_vertex", "add_edge", "finalize_write", "materialize"})

# The SHARED surface every format implements (abstract read/write ops + the concrete-KV composites).
# This is what the engine OVERRIDES/INHERITS manifests must partition exactly.
KV_ABSTRACT_OPS = frozenset(n for n, v in vars(Format).items()
                            if getattr(v, "__isabstractmethod__", False))
KV_BASE_OPS = KV_ABSTRACT_OPS | KV_CONCRETE_OPS

