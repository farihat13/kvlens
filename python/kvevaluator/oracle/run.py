"""The in-RAM Python store, the small-scale test oracle (tests/): open_store for one format, build_stores / open_stores
for a set, and the write-overlay helpers. Result measurement is the C++ evaluator's.
"""
import sys
import time

from kvevaluator.kvstore.kvstore import AbstractKV, CountingKV
from kvevaluator.formats import ENGINE_FORMATS, make_format
from kvevaluator.datasets import demo as data
from kvevaluator.datasets.ldbc.schema import get_schema


def open_store(fmt, backend, sf, get_graph, schema_name="canonical"):
    """Build/open the store for one format: abstract = in-memory + materialize (the one
    python backend since step 5; the persistent stores live with the C++ evaluator).

    schema_name is UNUSED on the abstract backend (the design comes from fmt's own
    schema); it is kept for call-site parity with drivers that key stores by it."""
    if backend == "abstract":
        raw = AbstractKV()
        fmt.materialize(get_graph(), raw)
        store = CountingKV(raw, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes)
        fmt.warm(store)                # rebuild derived state (no-op after in-process ingest)
        return store
    raise SystemExit(f"unknown backend '{backend}' -- python runs the abstract oracle; "
                     "disk-store measurement lives with the C++ evaluator")


def open_stores(sf, dbs, backend, schema_name, rebuild=False, pack=False, writes=False,
                codec="none"):
    """{db: (fmt, store)} for the one physical design `schema_name` names: stores are keyed by schema, not by plan variant
    (resolved per cell). rebuild=True rebuilds the canonical design's stores too.
    """
    if rebuild:
        for design in dict.fromkeys(("canonical", schema_name)):   # dedupe, keep order
            build_stores(sf, dbs, backend, rebuild=True, schema_name=design, pack=pack,
                         codec=codec)
    return build_stores(sf, dbs, backend, rebuild=False, schema_name=schema_name, pack=pack,
                        writes=writes, codec=codec)


def _install_decode_timer(fmt):
    """Times fmt.decode_value into fmt.decode_us. On the abstract backend the baselines
    (RowV2 / UidPack / VelocyPack) never decode -- values are live objects -- so their
    decode_us is legitimately 0 here; the pertype_csr family decodes above the store and
    accumulates into the same counter from its operators. Idempotent per instance."""
    if getattr(fmt, "_decode_timed", False):
        return
    inner = fmt.decode_value
    fmt.decode_us = 0.0

    def timed(key, raw):
        t = time.perf_counter()
        try:
            return inner(key, raw)
        finally:
            fmt.decode_us += (time.perf_counter() - t) * 1e6
    fmt.decode_value = timed
    fmt._decode_timed = True


def _wrap_store(fmt, raw, writes):
    """CountingKV over `raw`, plus the copy-on-write overlay when this is a WRITE run.

    The overlay is what makes a write sweep safe to run against the shared, cached RocksDB store:
    writes land in RAM and are dropped per cell, so the store on disk is exactly as the loader left
    it. See kvstore/shadow.py for what that costs (the `rocks` check goes n/a)."""
    from kvevaluator.kvstore.shadow import WriteShadowKV
    inner = WriteShadowKV(raw) if writes else raw
    _install_decode_timer(fmt)
    return CountingKV(inner, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes, write_mode=writes)


# (sf, the schema's prop filter) -> the neutral Graph, loaded ONCE per process. The load is
# the whole LDBC record stream (~5-7 min at sf3); the graph depends on the schema only through
# the props it declares (loader._keep_filters), and materialize() only READS it, so every
# format / schema / chunk arm of one run (gen_space's ~8 build_stores calls) shares one parse.
_GRAPH_CACHE = {}


def _graph_key(sf, schema):
    return (str(sf), tuple(sorted((l, tuple(c)) for l, c in schema.vertex_prop_types.items())),
            tuple(sorted((e, tuple(c)) for e, c in schema.edge_prop_types.items())))


def load_graph(sf, schema):
    key = _graph_key(sf, schema)
    if key not in _GRAPH_CACHE:
        _GRAPH_CACHE[key] = data.load(sf, schema=schema)    # filter props to the design (nocontent etc.)
    return _GRAPH_CACHE[key]


def build_stores(sf, dbs, backend, rebuild=False, schema_name="canonical", pack=False,
                 engine=False, writes=False, codec="none", dir_suffix=""):
    """One in-RAM store per format (backend "abstract", the only one), built once and shared by every param of the sweep.

    engine=True materializes with the ENGINE_FORMATS registry: the dgraph engine twin keeps the id index it anchors
    through, so its store differs from the dgraph format's; nebula's and arango's twins materialize identically.
    CountingKV wraps each store and tallies the KVCost vector.
    """
    # canonical -> data.schema(sf) so sf=None still picks the tiny DEMO_SCHEMA; a named LDBC
    # schema (label_only / type_index / custom) is taken verbatim from the registry.
    schema = data.schema(sf) if schema_name == "canonical" else get_schema(schema_name)
    t0 = time.time()
    stores = {}
    if backend == "abstract":
        g = load_graph(sf, schema)          # parsed once per (sf, prop filter) per process
        for name in dbs:
            fmt = ENGINE_FORMATS[name](schema) if engine else make_format(name, schema, pack)
            raw = AbstractKV()
            fmt.materialize(g, raw)
            store = _wrap_store(fmt, raw, writes)
            fmt.warm(store)      # rebuild derived state a cached store leaves empty (no-op here)
            stores[name] = (fmt, store)
    else:
        raise SystemExit(f"unknown backend {backend!r} -- python runs the abstract oracle; "
                         "persistent stores live with the C++ evaluator (--backend cpp)")
    print(f"[setup] {backend}: {len(stores)} store(s) in {time.time() - t0:.1f}s",
          file=sys.stderr)
    return stores


def _is_shadowed(store):
    """Is this store a write cell's copy-on-write overlay? Duck-typed on `reset`, so a caller does
    not have to import the store module to ask."""
    return hasattr(getattr(store, "_backend", None), "reset")


def _reset_shadow(store, fmt=None):
    """Start a write cell from the pristine base store. Without this a sweep's cells would see
    each other's writes -- an insert would become an update on the second param, and the numbers
    would drift in a way no check would catch. The format's own write state (dgraph's accumulated
    deltas and base snapshots) is reset with it, for the same reason."""
    if _is_shadowed(store):
        store._backend.reset()
        if fmt is not None and hasattr(fmt, "reset_write_state"):
            fmt.reset_write_state()
