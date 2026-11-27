"""meta -- the config-driven C++ format (cpp/kvevaluator/src/formats/meta/): a KV layout is a POINT
in the co-location design space, chosen by a codec_config spec. This class is SIZE-ONLY:
meta is measured by the C++ engine, never run in Python: its read and write operators here only
refuse, by name; it is a named, --db-selectable C++ store. Its layout is a spec file (there are no presets):
MetaFormat.of_spec(path) is the class bound to one spec, which its capabilities read; the bare class refuses.
"""
import json
import os

from kvevaluator.formats.base import Format
from kvevaluator.paths import ROOT


class MetaFormat(Format):
    name = "meta"
    SPEC = None          # (spec path, its content) on a class of_spec made; None on the bare class

    def __init__(self, schema):
        super().__init__(schema)

    _BOUND = {}

    @classmethod
    def of_spec(cls, path):
        """the meta format bound to the spec file at `path` (one class per path per process)"""
        tag, spec = meta_spec(path)
        key = (str(path), json.dumps(spec, sort_keys=True))
        if key not in cls._BOUND:
            cls._BOUND[key] = type(f"MetaFormat_{tag}", (cls,), {"SPEC": (str(path), spec)})
        return cls._BOUND[key]

    @classmethod
    def _spec(cls):
        if cls.SPEC is None:
            raise SystemExit("meta's capabilities need its spec: MetaFormat.of_spec(path) (a caps function's spec=, a "
                             "CLI's --spec)")
        return cls.SPEC[1]

    # the C++ Config defaults a spec's absent fields fall back to (config.h; only the fields the caps read)
    _DEFAULT = {"vertex_props": {"group": "item"}, "index": {"group": "item", "term": "exact"}}

    @classmethod
    def _layout(cls, spec, labels=()):
        """(index group, granularity, packed vertex, grouped props) of a spec: the C++ defaults (item vertex
        props, item/exact index), then the spec's own fields -- the same merge Config::from_json does. With
        per-label props overrides (vertex_props_by_label), packed / grouped hold only if they hold for EVERY
        label in `labels` (capabilities_for passes one label; labels=() is used for the label-free index
        granularity only)."""
        base = cls._DEFAULT
        vp = spec.get("vertex_props", base["vertex_props"])
        ix = dict(base["index"])
        if "index" in spec:
            if "group" in spec["index"]:   # a component override replaces it; term is its own field
                ix["group"] = spec["index"]["group"]
            ix["term"] = spec["index"].get("term", ix["term"])
        by_label = spec.get("vertex_props_by_label", {})
        comps = [by_label.get(label, vp) for label in labels] if labels else [vp, *by_label.values()]
        packed = all(isinstance(c.get("owners"), dict) and c["owners"].get("pack", 0) > 0 for c in comps)
        grouped = all(c.get("group") == "owner" for c in comps)
        return ix["group"], ("full" if ix["term"] == "exact" else "day"), packed, grouped

    @classmethod
    def pack_target(cls, schema, chunk=None):
        """The SMALLEST vertex-props pack over the labels (owners.pack, per label with vertex_props_by_label), None
        if any label's props are not packed -- the every-label intersection the other caps use. The store's
        chunk target plays no part: meta's packing is in its spec. Mirrors C++ Meta::pack_target."""
        spec = cls._spec()
        base = cls._DEFAULT
        vp = spec.get("vertex_props", base["vertex_props"])
        by_label = spec.get("vertex_props_by_label", {})
        packs = [((by_label.get(lb, vp).get("owners") or {}).get("pack", 0)
                  if isinstance(by_label.get(lb, vp).get("owners"), dict) else 0) for lb in schema.vertex_props]
        return min(packs) if packs and min(packs) > 0 else None

    @classmethod
    def _label_free_caps(cls, spec):
        """index + key caps, the same on every label: full => sorted (stop-early) range, day => unsorted (coarse)
        range; meta's index is per-type (dgraph's GLOBAL index is out of scope); every props layout keys by vtype."""
        _, gran, _, _ = cls._layout(spec, labels=())
        return {"per_type_index", "sorted_range_index" if gran == "full" else "unsorted_range_index",
                "label_scoped_props"}

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """per edge type, from the spec: the in lists where the store keeps them (direction both, or the schema's
        @reverse); lists when every stored direction of it is Condensed or Chunked (one Split direction: not
        advertised). The C++ defaults: direction declared, adjacency item. Mirrors C++ Meta::edge_capabilities_for."""
        spec = cls._spec()
        rev = spec.get("direction", "declared") == "both" or etype in schema.reverse_etypes
        default = spec.get("adjacency_by_etype", {}).get(etype, spec.get("adjacency", {"group": "item"}))
        per_dir = spec.get("adjacency_by_etype_dir", {}).get(etype, {})
        comps = [per_dir.get(d, default) for d in (("out", "in") if rev else ("out",))]
        caps = {"reverse_adjacency"} if rev else set()
        if all(c.get("group") == "owner" for c in comps):
            caps.add("adjacency_lists")
        return caps

    @classmethod
    def capabilities_for(cls, schema, label):
        """The caps on ONE label: its own props layout (vertex_props_by_label) -- the one definition the selectors
        (caps_resolver, a variant's `on`) and the grid's validity gate read. Mirrors C++ Meta::capabilities_for."""
        spec = cls._spec()
        index_group, _, packed, grouped = cls._layout(spec, labels=(label,))
        caps = cls._label_free_caps(spec)
        if label in schema.type_indexed_labels:
            caps.add("type_index")
            # a posting (plain or packed: the packed index keeps type indexes as plain postings) holds a type in one
            # unit -- an index-wide property that needs EVERY label typed, as C++ PostingIndex::single_posting_type_index
            if index_group == "owner" and schema.type_indexed_labels >= set(schema.vertex_props):
                caps.add("type_index_single_posting")
        if grouped:                 # grouped row (nebula/pertype_csr) reads a whole row in one GET
            caps.add("whole_row_read")
        if packed:                  # a chunked layout scans the label's props while enumerating (pertype_csr);
            caps.add("label_scan_carries_props")   # read_vertices_by_label(props) checks exactly this label
        caps.add("label_rows_one_run")   # every props layout keys a label's records together: one scan may read it
        caps.add("whole_label_scan_by_default")   # and a whole-label read with props takes that scan by default
        return caps

    @classmethod
    def capabilities(cls, schema):
        """The store-wide set: the INTERSECTION of the per-label sets (capabilities_for), so an un-annotated pick runs
        on every label it may touch. Mirrors C++ Meta::capabilities."""
        labels = tuple(schema.vertex_props)
        if not labels:
            return cls._label_free_caps(cls._spec())
        return set.intersection(*(cls.capabilities_for(schema, label) for label in labels))

    # --- no Python execution twin: every abstract op is a loud, named refusal -------------
    _NO_TWIN = ("meta has no Python execution twin (C++-only format; cpp/kvevaluator/src/"
               "formats/meta/) -- it is a SIZE-ONLY format measured by a real C++ run, never "
               "scored by a Python formula. Never open_store(fmt, 'abstract', ...) for "
               "'meta'; score a real C++ run instead (--measured cpp).")

    def add_vertex(self, store, vid, label, props):
        raise NotImplementedError(self._NO_TWIN)

    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        raise NotImplementedError(self._NO_TWIN)

    def read_vertex_by_id(self, store, vid, label=None):
        raise NotImplementedError(self._NO_TWIN)

    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False):
        raise NotImplementedError(self._NO_TWIN)

    def read_vertex_props(self, store, vids, props, label=None):
        raise NotImplementedError(self._NO_TWIN)

    def read_anchor_props(self, store, cands, props, labels=None):
        raise NotImplementedError(self._NO_TWIN)

    def read_index_eq(self, store, label, prop, value):
        raise NotImplementedError(self._NO_TWIN)

    def read_index_range(self, store, labels, prop, lo, hi):
        raise NotImplementedError(self._NO_TWIN)

    def read_vertices_by_label(self, store, label):
        raise NotImplementedError(self._NO_TWIN)

    def read_filter_by_label_probe(self, store, vids, label):
        raise NotImplementedError(self._NO_TWIN)

    def read_filter_by_label_index(self, store, vids, label):
        raise NotImplementedError(self._NO_TWIN)


# the retired meta presets: a spec file named like one is warned and marked, since a store named
# meta-<preset> may still exist from before (a v3 store under the same name is refused by the spec check, never confused)
META_RETIRED_PRESETS = ("dgraph", "nebula", "dgneb", "pertype_csr")


def meta_spec(path):
    """(tag, spec) of a meta store's spec file `path` (an interior design-space point, e.g.
    inputs/specs/grid/*.json, or a hand-written one; a caller passes it, a CLI takes it as --spec): tag = the
    file's stem, spec = its content -- short or in the full canonical form, both work: the store checks
    compare the ingested manifest against the file's canonical form (canonical_meta_spec), so a spec
    edited under the same name can never reuse the old store. There is no default: meta has no presets (the engine
    layouts are the separate formats, their full meta specs inputs/specs/corners/)."""
    if not path:
        raise SystemExit("meta needs its spec file (inputs/specs/grid/<cell>.json, inputs/specs/corners/"
                         "corner_<engine>.json, ...): pass spec= (a CLI: --spec); it has no default layout")
    path = str(path)
    if not os.path.isabs(path):
        path = os.path.join(str(ROOT), path)
    with open(path) as f:
        spec = json.load(f)
    tag = os.path.splitext(os.path.basename(path))[0]
    if tag in META_RETIRED_PRESETS:
        from kvevaluator import stale
        stale.note("shape_sibling", f"meta spec {path}: the stem {tag!r} is a retired preset's store name (meta-{tag}); "
                   f"an old store of that name is refused by the spec check, never used; rename the spec file to clear it")
    return tag, spec
