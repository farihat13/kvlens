"""The capabilities a format's STORE has: the format class's own set (Format.capabilities), validated against the
vocabulary (plans/ir/capabilities.py), plus the store caps derived from the pack target and the schema. Mirrored in
C++ Format::store_capabilities; tests/unit/check_caps_parity.py holds the two equal."""
from kvevaluator.plans.ir.capabilities import CAPABILITIES, CHUNK_COARSE, STORE_CAPS


def format_class(format_name, spec=None):
    """the Format class whose caps a store has: FORMATS[format_name]; for meta, the class bound to the store's spec file
    `spec` (MetaFormat.of_spec; the bare class refuses when asked for caps). None for a name with no Python class."""
    from kvevaluator.formats import FORMATS
    cls = FORMATS.get(format_name)
    if cls is not None and format_name == "meta" and spec is not None:
        return cls.of_spec(spec)
    return cls


def store_caps(format_name, schema, chunk=None, spec=None):
    """The capabilities a STORE adds to its format's: coarse_chunks iff the format's pack target (the bytes one
    landing on a row decodes; Format.pack_target, given the store's chunk target `chunk`) is >= CHUNK_COARSE; and
    property_index iff the schema declares a property (value or date) index. The ONE derivation, mirrored in C++
    Format::store_capabilities. A format that does not pack rows (dgraph, whose chunk target is a split
    threshold; nebula; arangodb) never gets coarse_chunks."""
    cls = format_class(format_name, spec)
    pack = cls.pack_target(schema, chunk) if cls is not None else None
    caps = {"coarse_chunks"} if pack is not None and int(pack) >= CHUNK_COARSE else set()
    if schema.indexed_cols or schema.date_cols:
        caps.add("property_index")
    return caps


def caps_resolver(format_name, schema, chunk=None, spec=None):
    """label -> capability set, for per-label variant selection (bundle.select_variant's `caps_of_label`): the
    format's own per-label set (Format.capabilities_for: the store-wide set, or dgraph's per-label per_type_index),
    so a variant annotated `"on": {cap: [label]}` runs exactly when the format has the cap on that label. `chunk` =
    the store's chunk target (bytes), as caps_for: its caps hold on every label. Mirrored in C++
    Format::store_capabilities_for. `spec`: a meta store's spec file (format_class)."""
    cls = format_class(format_name, spec)
    memo = {}

    def label_caps(fmt, label):                       # a format's own per-label set, validated once
        if (fmt, label) not in memo and label in schema.etypes and label not in schema.vertex_props:
            # an `on` that names an EDGE TYPE: that edge type's adjacency caps (C++ Format::edge_capabilities_for)
            caps = set(cls.edge_capabilities_for(schema, label)) if cls is not None else set()
            unknown = caps - CAPABILITIES
            if unknown:
                raise ValueError(f"{fmt} declares unknown capabilities on edge type {label!r}: {sorted(unknown)}")
            memo[(fmt, label)] = caps
        if (fmt, label) not in memo:
            caps = set(cls.capabilities_for(schema, label)) if cls is not None else caps_for(fmt, schema)
            unknown = caps - CAPABILITIES
            if unknown:
                raise ValueError(f"{fmt} declares unknown capabilities on {label!r}: {sorted(unknown)}")
            if caps & STORE_CAPS:
                raise ValueError(f"{fmt} declares a store property on {label!r}: {sorted(caps & STORE_CAPS)} (store_caps)")
            memo[(fmt, label)] = caps | store_caps(fmt, schema, chunk, spec)
        return memo[(fmt, label)]

    caps_for(format_name, schema, spec=spec)          # the store-wide checks
    return lambda label: label_caps(format_name, label)


def edge_caps_of(format_name, schema, chunk=None, spec=None):
    """etype -> the adjacency caps of that edge type on this format (reverse_adjacency, adjacency_lists): caps_resolver
    asked by edge-type name. What the lowering of a read_edge_exists with no reads consults (planir/lowering.py)."""
    resolve = caps_resolver(format_name, schema, chunk=chunk, spec=spec)
    return lambda etype: resolve(etype) & {"reverse_adjacency", "adjacency_lists"}


# CAPABILITY PROFILES: a fallback for a format that exists only in the C++ evaluator with NO
# Python Format class at all (so it can't be in FORMATS and can't define its own
# capabilities()) -- the planner still needs its capability set to select and emit plans for
# it. A profile is the declared contract, mirrored from the format's C++ `capabilities()`;
# tests/unit/check_caps_parity.py (via `kvevaluator caps`) holds it equal to the binary's.
#
# Empty: caps_for's guard below (`format_name not in FORMATS`) means a PROFILES entry for a name that IS in FORMATS
# is dead code, never consulted, so a hand-mirrored copy could drift from the format's capabilities() with nothing
# catching it (check_caps_parity.py tests caps_for's OUTPUT). A future format with no Python class goes here.
PROFILES = {}


def caps_for(format_name, schema, chunk=None, spec=None):
    """The capabilities `format_name` provides on `schema` (empty set = only a bundle's
    universal fallback variant runs). Validated against the vocabulary. `chunk` = the STORE's chunk target in bytes (the manifest's
    value_codec.chunk_target: store_identity.store_chunk_target / store_chunk / chunk_of_slug), None for an
    unchunked store or a chunk-blind caller: it adds the store caps (store_caps). `spec`: a meta store's spec file."""
    cls = format_class(format_name, spec)
    if cls is None and format_name in PROFILES:
        caps = set(PROFILES[format_name](schema))
    elif cls is None:
        raise KeyError(format_name)
    else:
        caps = set(cls.capabilities(schema))
    unknown = caps - CAPABILITIES
    if unknown:
        raise ValueError(f"{format_name} declares unknown capabilities: {sorted(unknown)}")
    if caps & STORE_CAPS:
        raise ValueError(f"{format_name} declares a store property {sorted(caps & STORE_CAPS)}: "
                         f"derived from the pack target / the schema (store_caps)")
    return caps | store_caps(format_name, schema, chunk, spec)
