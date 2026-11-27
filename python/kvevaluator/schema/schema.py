"""Dataset schema: the dataset's PHYSICAL DESIGN, a core input next to format and workload.

A Format is a KV-layout mechanism; which columns a dataset indexes, which edges its workload walks backward, which
columns are list-valued and how its types group into supertypes are dataset facts. They are declared here and
injected at construction (`DgraphFormat(schema)`), so format code never hardcodes a dataset. Instances:
datasets/ldbc/schema.py, datasets/demo.py.

- The declared indexes are the dataset's CANONICAL design, by cross-format symmetry: a structure goes in when any
  format's real engine can build it or a plan needs it, and every format then materializes its own rendering.
- The schema DECLARES; each format CONSUMES what it needs.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Schema:
    # The physical-design name this schema compiled from ("canonical", "label_only", ...).
    # A LABEL, not a layout fact: everything the layout needs is in the fields below, so it is
    # NOT fingerprinted (two schemas differing only in name are the same design).
    variant: str = field(default="canonical", metadata={"fingerprint": False})
    indexed_cols: frozenset = frozenset()    # {(label, prop)} with a materialized value index
    reverse_etypes: frozenset = frozenset()  # {etype} the workload traverses backward
    # {etype} EVERY declared edge type (superset of reverse_etypes). Formats don't need it to
    # STORE the pairs (nebula/arango get direction free, dgraph needs only reverse); it lets a
    # post-hoc reader tell a dgraph edge posting (etype,"D",..) from a vertex-prop (prop,"D",..)
    # -- both share kind "D" (kvevaluator.measure.kvsize). FINGERPRINTED: the dgraph value CODEC
    # dispatches on it (_is_posting_key picks UidPack vs prim leaf by `attr in etypes`), so it
    # decides physical stored bytes.
    etypes: frozenset = frozenset()
    date_cols: frozenset = frozenset()       # {(label, prop)} date cols with a sorted index
    multival_cols: frozenset = frozenset()   # {(label, prop)} list-valued cols (LDBC [string])
    nullable_cols: frozenset = frozenset()   # {(label, prop)} cols that MAY be absent (default:
                                             # required). A covering index enumerates a type only
                                             # if the prop is required; nebula reserves a row slot
                                             # for every nullable column.
    type_groups: dict = field(default_factory=dict)  # {group: (member_labels,)}, e.g.
                                                     # {"Message": ("Post", "Comment")}
    adj_sort: dict = field(default_factory=dict)     # {etype: prop} -- the reverse adjacency of
                                                     # etype is stored SORTED by the neighbor's
                                                     # `prop` (desc), so a bounded time-ordered
                                                     # walk (IC2) can stop early. A retired Dgraph variant
                                                     # only; empty otherwise. Content-affecting -> hashed.
    adj_inline: dict = field(default_factory=dict)   # {etype: prop} -- the neighbor's `prop` is
                                                     # stored INLINE in each adjacency entry, so the
                                                     # walk reads it for 0 GET at a byte cost. A retired
                                                     # Dgraph variant only. Content-affecting.
    adj_sort_facet: dict = field(default_factory=dict)  # {etype: facet} -- reverse adjacency of
                                                     # etype stored SORTED by the EDGE FACET `facet`
                                                     # (desc), e.g. likes.creationDate: the facet
                                                     # already lives IN the posting entry. A retired
                                                     # Dgraph variant only. Content-affecting -> hashed.
    adj_dir: dict = field(default_factory=dict)      # {etype: "out"} -- the DECLARED direction a
                                                     # dated adjacency (adj_sort / adj_inline /
                                                     # adj_sort_facet) is walked, like an index's
                                                     # direction: the walked side carries the sort
                                                     # order and the inline copy. Only "out" is
                                                     # recorded; absent = "in" (the reverse walk)
                                                     # -- read through adj_direction().
                                                     # Content-affecting -> hashed.
    prop_groups: dict = field(default_factory=dict)  # {group_name: frozenset(predicate_names)} --
                                                     # scalar predicates co-located into ONE storage
                                                     # key (group_name IS that key's predicate), e.g.
                                                     # {"g1": {"firstName", "lastName"}}. Consumed
                                                     # only by a retired Dgraph variant; empty in
                                                     # canonical.
    # {etype: ((src_label, dst_label), ...)} -- the declared endpoints of every edge type. Like
    # `etypes` this is DERIVED (straight off the edge declarations) and never consulted by
    # add_vertex/add_edge/finalize_write, so it changes nothing a store CONTAINS ->
    # fingerprint=False, or every store would go stale for a field that cannot move a byte.
    # It exists for the WRITE side: deleting a vertex means finding its incident edges, which
    # means knowing which edge types can touch a label at all (see incident_etypes).
    edge_endpoints: dict = field(default_factory=dict, metadata={"fingerprint": False})
    # {label: (prop, ...)} -- the columns each vertex label declares. Derived from the vertex
    # declarations, consulted by nothing on the load path -> fingerprint=False, same reasoning as
    # `etypes` and `edge_endpoints`. The WRITE side needs it: deleting a vertex in a
    # predicate-oriented layout means tombstoning one posting per property, and you cannot count
    # those from the props a caller happened to pass -- the row on disk has all of them.
    vertex_props: dict = field(default_factory=dict, metadata={"fingerprint": False})
    # {label: ((prop, type), ...)} / {etype: ((prop, type), ...)} -- each column's declared
    # storage type (schemadef STR/INT/DATE), in declaration order (multi-ness stays in
    # `multival_cols`). Consumed by the value CODECS (formats/*.py encode_value/decode_value):
    # nebula's RowV2 is positional, so decoding a row needs the label's ordered columns + types.
    # Unlike `vertex_props` these are NOT tagged fingerprint=False -- the codec lays out stored
    # bytes from them, so a type change changes what a store physically CONTAINS and must stale it.
    vertex_prop_types: dict = field(default_factory=dict)
    edge_prop_types: dict = field(default_factory=dict)
    type_indexed_labels: frozenset = frozenset()  # {label} with a per-type membership index --
                                                  # a structure returning all vids of a type.
                                                  # OPTIONAL and layout-dependent: dgraph mandates
                                                  # it (dgraph.type), nebula makes it opt-in,
                                                  # arango folds type into the id. Consulted by
                                                  # read_filter_by_label_probe.
                                                  # Empty by default: NOT part of the base KV format.
    predicates_per_label: bool = False            # the DSL's `predicates per_label`: a store that names its data by
                                                  # predicate (Dgraph) names a vertex property's predicate per label
                                                  # (`Label.prop`), data keys and index keys alike
    type_scoped_index: frozenset = frozenset()    # {(label, prop)} whose value/date index is
                                                  # scoped PER TYPE, not predicate-global. OPT-IN and
                                                  # layout-dependent: dgraph keys that posting per type
                                                  # (f"{label}.{prop}") instead of predicate-global, so the
                                                  # index IS the type restriction and a top-k walk can be
                                                  # candidate-free. A SUBSET of date_cols/indexed_cols (it
                                                  # changes only the keying). Nebula/arango already index
                                                  # per type, so they ignore it. Content-affecting -> hashed.
    value_sorted_index: frozenset = frozenset()   # {(label, prop)} whose date index posting VALUE is
                                                  # stored in DATE order, not uid order. OPT-IN: dgraph
                                                  # stores that posting as date-descending [vid, ms] pairs,
                                                  # so a top-k ranks with NO per-candidate ms GET. A SUBSET
                                                  # of type_scoped_index: value-sorting is only sound on a
                                                  # per-type key. Nebula/arango ignore it (ms is in their
                                                  # key). Content-affecting -> hashed.
    key_sorted_index: frozenset = frozenset()     # {(label, prop)} whose date index puts ms IN THE KEY
                                                  # one index entry PER value. OPT-IN: dgraph emits one entry
                                                  # per message keyed by an order-preserving ms+vid token,
                                                  # empty value, so a KEY range scan reads exactly K key-only
                                                  # entries and stops (like nebula's ms-in-key index). A
                                                  # SUBSET of type_scoped_index; never on the same column as
                                                  # value_sorted_index. Nebula/arango ignore it.
                                                  # Content-affecting -> hashed.

    def __post_init__(self):
        # `etypes` is the set of EVERY declared edge type and, by contract, a superset of the
        # reverse / dated-adjacency / endpoint declarations. The entity DSL (schemadef) fills it
        # off the edge declarations; a schema built with this constructor directly (a non-LDBC
        # toy, a test) may declare an edge only through reverse_etypes -- the formats' unknown-
        # etype guard (C++ parity) would then refuse every edge of it. Close the invariant here,
        # where the fields live: a DSL schema is unchanged (its etypes already hold every edge).
        declared = (set(self.reverse_etypes) | set(self.adj_sort) | set(self.adj_inline)
                    | set(self.adj_sort_facet) | set(self.edge_endpoints))
        if not declared <= self.etypes:
            object.__setattr__(self, "etypes", frozenset(self.etypes | declared))

    def is_indexed(self, label, prop):
        return (label, prop) in self.indexed_cols

    def has_type_index(self, label):
        """Is a per-type membership index declared for this label?"""
        return label in self.type_indexed_labels

    def is_type_scoped(self, label, prop):
        """Is this column's index scoped PER TYPE (keyed by f"{label}.{prop}") instead of predicate-global?"""
        return (label, prop) in self.type_scoped_index

    def is_value_sorted(self, label, prop):
        """Is this column's date-index posting stored in DATE order ([vid, ms] pairs) instead of uid order?"""
        return (label, prop) in self.value_sorted_index

    def is_key_sorted(self, label, prop):
        """Is this column's date index keyed with ms IN THE KEY, one entry per value?"""
        return (label, prop) in self.key_sorted_index

    def props_of(self, label):
        """The columns `label` declares, in declaration order ((), for an unknown label)."""
        return self.vertex_props.get(label, ())

    def index_cols_of(self, label):
        """This label's indexed columns (value and date indexes), in a stable order. A row store maintains an index entry
        per row per index, so an insert's index cost follows the label's index count, not the props passed.
        """
        return sorted(p for (lbl, p) in (self.indexed_cols | self.date_cols) if lbl == label)

    def adj_direction(self, etype):
        """The declared walk direction of a dated adjacency (the side whose posting is sorted and carries any inline copy);
        default "in", the reverse walk.
        """
        return self.adj_dir.get(etype, "in")

    def incident_etypes(self, label):
        """[(etype, "out"|"in")]: every edge type that can touch `label`, and from which side (a vertex delete reaches these)."""
        out = []
        for etype, pairs in sorted(self.edge_endpoints.items()):
            if any(s == label for s, _ in pairs):
                out.append((etype, "out"))
            if any(d == label for _, d in pairs):
                out.append((etype, "in"))
        return out

    def indexed_predicates(self):
        """dgraph's view: its indexes are per-PREDICATE, global across types."""
        return {p for _, p in self.indexed_cols}

    def multival_predicates(self):
        """dgraph's view of multival_cols (same per-predicate projection)."""
        return {p for _, p in self.multival_cols}

    def date_predicates(self):
        """dgraph's view of date_cols. It over-covers by design: dgraph's index is predicate-global, so declaring
        (Post, creationDate) also indexes Forum/Person dates, as the real @index(day) does.
        """
        return {p for _, p in self.date_cols}

    def group_of(self, label):
        """The logical supertype of a label (Post -> Message); itself if ungrouped."""
        for g, members in self.type_groups.items():
            if label in members:
                return g
        return label

    def summary(self, name=None):
        """One line naming this physical design and its index counts -- the header a run
        prints to say WHICH schema is live (`--schema`)."""
        head = f"[schema] {name or 'active'}:"
        parts = [f"{len(self.indexed_cols)} value idx", f"{len(self.date_cols)} sorted idx",
                 f"{len(self.multival_cols)} multi", f"{len(self.reverse_etypes)} reverse edges"]
        if self.type_indexed_labels:
            parts.append(f"type-index on {len(self.type_indexed_labels)} labels")
        return f"{head} {', '.join(parts)}"

    def describe(self, name=None):
        """A readable multi-line dump of this physical design: the flat frozensets grouped back
        the way they were authored, so `--schema X` can be inspected at a glance."""
        def cols(s):
            return ", ".join(f"{lbl}.{p}" for lbl, p in sorted(s)) or "(none)"
        groups = "; ".join(f"{g} = {' + '.join(m)}"
                           for g, m in sorted(self.type_groups.items())) or "(none)"
        return "\n".join([
            self.summary(name),
            f"  value indexes  ({len(self.indexed_cols)}): {cols(self.indexed_cols)}",
            f"  sorted indexes ({len(self.date_cols)}): {cols(self.date_cols)}",
            f"  multi-valued   ({len(self.multival_cols)}): {cols(self.multival_cols)}",
            f"  reverse edges  ({len(self.reverse_etypes)}): "
            f"{', '.join(sorted(self.reverse_etypes)) or '(none)'}",
            f"  type groups: {groups}",
            f"  type index on  ({len(self.type_indexed_labels)}): "
            f"{', '.join(sorted(self.type_indexed_labels)) or '(none)'}",
            f"  type-scoped idx ({len(self.type_scoped_index)}): {cols(self.type_scoped_index)}",
            f"  value-sorted idx ({len(self.value_sorted_index)}): {cols(self.value_sorted_index)}",
            f"  key-sorted idx ({len(self.key_sorted_index)}): {cols(self.key_sorted_index)}",
        ])
