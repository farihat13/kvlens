"""Entity-oriented schema builder: declare each type ONCE, with its flags local to it, and compile to schema.Schema.

Dataset-agnostic: it holds only the authoring DSL. A dataset declares its types with vertex()/edge()/prop() and an
INDEXES list (kept off the props so the index design reads as one block), and calls compile_schema(); the LDBC
declarations live in datasets/ldbc/schema.py. A prop declares a CAPABILITY (index=VALUE, a value index), never a
format's key layout; each format renders the capability its own way at ingest.
"""
import dataclasses

from kvevaluator.schema.schema import Schema

# --- storage types: what a value IS -------------------------------------------------
STR = "str"
INT = "int"
DATE = "date"                 # epoch-ms int storage; also the type a sorted date index reads
BYTES = "bytes"               # raw bytes (a LinkBench data blob): stored at its length, never text, never indexed
_TYPES = (STR, INT, DATE, BYTES)
_ORDERED = (INT, DATE)        # types a SORTED (range) index is valid on -- NOT date-only

# --- index kinds: how a column is entered -------------------------------------------
VALUE = "value"               # equality (point-lookup) index -> Schema.indexed_cols
SORTED = "sorted"             # sorted/range index (ordered cols) -> Schema.date_cols (the field keeps the date name)


@dataclasses.dataclass(frozen=True)
class Prop:
    name: str
    type: str
    multi: bool = False       # list-valued [type] column (LDBC email/language) -- STRUCTURAL,
                              # so it lives on the prop; index-ness is a separate design knob (Index)
    nullable: bool = False    # column MAY be absent on a vertex of this type (default: required).
                              # Needed for: a covering index enumerates a type only if REQUIRED,
                              # and nebula reserves a fixed row slot for every nullable column.


@dataclasses.dataclass(frozen=True)
class Index:
    label: str
    prop: str = None          # None => a per-TYPE membership index on the label
                              # (type_indexed_labels, e.g. dgraph.type); else a property index
    kind: str = None          # property-index kind (VALUE/SORTED); None => inferred from the type
    type_scoped: bool = False  # scope this property index PER TYPE (Schema.type_scoped_index): it
                              # changes only the index's KEYING (f"{label}.{prop}" vs the bare
                              # predicate), not whether the index exists. Only meaningful on a
                              # property index (needs a prop).
    value_sorted: bool = False  # store this date index's posting VALUE in DATE order, not uid order
                              # (Schema.value_sorted_index). Only sound on a per-type key, so it
                              # implies type_scoped (compile_schema enforces). Needs a prop.
    key_sorted: bool = False  # put ms IN THE KEY, one index entry per value (Schema.key_sorted_index).
                              # Implies type_scoped (compile_schema enforces); never both key_sorted
                              # and value_sorted on one column. Needs a prop.


@dataclasses.dataclass(frozen=True)
class Vertex:
    label: str
    prefix: str               # legacy vid prefix name (person, msg, ...); grouped members share the group's
    props: tuple
    group: str = None         # logical supertype (Post/Comment -> Message)
    type_index: bool = False  # per-type membership index; off in canonical


@dataclasses.dataclass(frozen=True)
class Edge:
    etype: str
    reverse: bool = False     # traversed backward -> reverse_etypes (dgraph @reverse, per-predicate)
    between: tuple = None     # ((src_label, dst_label), ...) -- the edge's endpoints (Schema.edge_endpoints); the
                              # loader has its OWN file<->endpoint map, cross-checked by the schema-vs-loader drift guard.
    props: tuple = None       # (Prop, ...) edge props -- consumed by the LOADER (read by NAME from
                              # the CSV, so order is free) AND compiled into Schema.edge_prop_types
                              # (the codecs' positional facet schema)


@dataclasses.dataclass(frozen=True)
class Group:
    name: str                 # the group's storage predicate (g1, g2) -- becomes ("g1","D",vid)
    cols: tuple               # ((label, prop), ...) the columns co-located into that one key


def prop(name, type, multi=False, nullable=False):
    if type not in _TYPES:
        raise ValueError(f"prop {name!r}: unknown type {type!r} (expected STR/INT/DATE/BYTES)")
    if type == BYTES and multi:
        raise ValueError(f"prop {name!r}: a bytes property is single-valued")
    return Prop(name, type, multi, nullable)


def group(name, cols):
    """A property group: the listed scalar columns in ONE storage key named `name` (a retired Dgraph variant)."""
    return Group(name, tuple(cols))


def index(label, prop=None, kind=None, type_scoped=False, value_sorted=False, key_sorted=False):
    """Declare an index.

    `index(label, prop)` = a PROPERTY index on the column. The KIND (VALUE = equality / SORTED = range) is inferred
    from the column type: strings VALUE, numbers/dates SORTED. Pass kind only to override (equality on a number).
    `index(label)` = a per-TYPE membership index ("all vids of this type"); no prop, no kind.
    `type_scoped=True` keys a property index PER TYPE (Schema.type_scoped_index).
    `value_sorted=True` stores the date index's posting VALUE in date order; `key_sorted=True` puts ms IN THE KEY,
    one entry per value. Each needs a prop and type_scoped (compile_schema enforces); never both on one column.
    """
    if prop is None and kind is not None:
        raise ValueError(f"index {label}: a type index takes no kind")
    if prop is None and type_scoped:
        raise ValueError(f"index {label}: a per-type MEMBERSHIP index cannot be type_scoped "
                         f"(type_scoped scopes a PROPERTY index -- it needs a prop)")
    if prop is None and value_sorted:
        raise ValueError(f"index {label}: a per-type MEMBERSHIP index cannot be value_sorted "
                         f"(value_sorted orders a PROPERTY index's value -- it needs a prop)")
    if prop is None and key_sorted:
        raise ValueError(f"index {label}: a per-type MEMBERSHIP index cannot be key_sorted "
                         f"(key_sorted keys a PROPERTY index's value -- it needs a prop)")
    if value_sorted and key_sorted:
        raise ValueError(f"index {label}.{prop}: value_sorted and key_sorted are distinct dim-2 "
                         f"layouts (day-bucket date-value vs ms-in-key) -- declare at most one")
    if kind not in (None, VALUE, SORTED):
        raise ValueError(f"index {label}.{prop}: unknown kind {kind!r} (expected VALUE/SORTED)")
    return Index(label, prop, kind, type_scoped, value_sorted, key_sorted)


def _infer_kind(type):
    """Default index kind for a column type: strings -> VALUE, numbers/dates -> SORTED (order-preserving encoding)."""
    return VALUE if type == STR else SORTED


def vertex(label, prefix, props, group=None, type_index=False):
    return Vertex(label, prefix, tuple(props), group, type_index)


def edge(etype, reverse=False, between=None, props=None):
    return Edge(etype, reverse,
                tuple(between) if between is not None else None,
                tuple(props) if props is not None else None)


def compile_schema(vertices, edges, indexes=(), col_groups=(), adj=(), variant="canonical", predicates_per_label=False):
    """Fold the entity + index declarations into the flat Schema every format consumes.

    Each index is validated against the declared columns: an index on an undeclared column, or a SORTED index on a
    non-ordered column, is a loud error. `col_groups` are property groups (a retired Dgraph variant). Edge between=
    gives edge_endpoints and edge props= give edge_prop_types. Raises on a duplicate (label, prop).
    """
    proptype, multival, nullable, type_indexed = {}, set(), set(), set()
    groups, vlabels = {}, set()
    for v in vertices:
        vlabels.add(v.label)
        for p in v.props:
            key = (v.label, p.name)
            if key in proptype:
                raise ValueError(f"duplicate property {key}")
            proptype[key] = p.type
            if p.multi:
                multival.add(key)
            if p.nullable:
                nullable.add(key)
        if v.type_index:
            type_indexed.add(v.label)
        if v.group:
            groups.setdefault(v.group, []).append(v.label)   # member order = declaration order

    indexed, sorted_cols, type_scoped, value_sorted, key_sorted = set(), set(), set(), set(), set()
    for ix in indexes:
        if ix.prop is None:                                  # per-TYPE membership index
            if ix.label not in vlabels:
                raise ValueError(f"type index on undeclared vertex {ix.label}")
            type_indexed.add(ix.label)
            continue
        key = (ix.label, ix.prop)
        if key not in proptype:
            raise ValueError(f"index on undeclared column {ix.label}.{ix.prop}")
        if proptype[key] == BYTES:                           # a blob: no format keys an index term by it (C++ schema.cc)
            raise ValueError(f"index on {ix.label}.{ix.prop}: a bytes property cannot be indexed")
        kind = ix.kind or _infer_kind(proptype[key])         # kind follows the column type by default
        if kind == VALUE:
            indexed.add(key)
        else:                                                # SORTED
            if proptype[key] not in _ORDERED:
                raise ValueError(f"SORTED index on {ix.label}.{ix.prop}: strings aren't "
                                 f"range-indexed in this model (no order-preserving encoding); "
                                 f"got type {proptype[key]!r}")
            sorted_cols.add(key)                             # SORTED -> date_cols (field-name note up top)
        if ix.type_scoped:                                   # per-type KEYING of this index (opt-in)
            type_scoped.add(key)
        if ix.value_sorted:                                  # date-ordered posting VALUE (opt-in)
            value_sorted.add(key)
        if ix.key_sorted:                                    # ms-in-key, one entry per value (opt-in)
            key_sorted.add(key)
    # Invariant: a value-sorted posting must be keyed per type. A predicate-global bucket is
    # shared by every dated type, so value-sorting it would mix one type's [vid, ms] pairs with
    # another's plain vids in the same posting. Require type_scoped so each value-sorted (label, prop)
    # owns its key f"{label}.{prop}". (The value_sorted_date schema declares both, via type_scoped_date.)
    if not value_sorted <= type_scoped:
        stray = ", ".join(f"{l}.{p}" for l, p in sorted(value_sorted - type_scoped))
        raise ValueError(f"value_sorted index needs type_scoped (else a predicate-global bucket "
                         f"mixes types' postings); missing type_scoped on: {stray}")
    # same invariant for key_sorted: the ms+vid composite token is a PER-TYPE token
    # space -- a predicate-global one would mix Post/Comment/Person/Forum message entries. Require
    # type_scoped so each key_sorted (label, prop) owns its key f"{label}.{prop}". (The key_sorted_date
    # schema declares both, via type_scoped_date.)
    if not key_sorted <= type_scoped:
        stray = ", ".join(f"{l}.{p}" for l, p in sorted(key_sorted - type_scoped))
        raise ValueError(f"key_sorted index needs type_scoped (else a predicate-global token space "
                         f"mixes types' entries); missing type_scoped on: {stray}")

    # --- property groups (a retired Dgraph variant): co-locate scalar predicates into ONE storage key ---
    # The group NAME becomes a stored predicate ("g1","D",vid), so it must not collide with a real
    # predicate or edge type. A predicate lives in at most one group (one key). List predicates
    # keep their own key (a group value is a positional scalar row).
    #
    # SCOPE IS PER-PREDICATE, NOT PER-LABEL: dgraph predicates are global (one `content` predicate,
    # shared by Post and Comment), so the group keys on the bare predicate NAME and the Label. in
    # `group g1 Post.content ...` is only for readability + an existence check. Grouping a SHARED
    # predicate therefore groups it for EVERY type that has it -- usually intended (messages share a
    # layout), but be aware `Post.content` is not scoped to Post. Op counts stay correct either way.
    pred_names = {p.name for v in vertices for p in v.props}
    etype_names = {e.etype for e in edges}
    prop_groups, grouped_pred = {}, {}
    for grp in col_groups:
        if grp.name in prop_groups:
            raise ValueError(f"duplicate group name {grp.name!r}")
        if grp.name in pred_names or grp.name in etype_names:
            raise ValueError(f"group name {grp.name!r} collides with a predicate/edge type "
                             f"(it becomes a stored predicate)")
        if len(grp.cols) < 2:
            # Guards against a single COLUMN (`group g1 Post.content`). Two columns naming the SAME
            # shared predicate (`Post.content Comment.content`) pass and dedup to a 1-predicate group
            # -- intended: a merge no-op for dgraph, a real "peel one prop" split for nebula.
            raise ValueError(f"group {grp.name!r} needs >= 2 columns")
        preds = []
        for lbl, p in grp.cols:
            key = (lbl, p)
            if key not in proptype:
                raise ValueError(f"group {grp.name!r} on undeclared column {lbl}.{p}")
            if key in multival:
                raise ValueError(f"group {grp.name!r}: {lbl}.{p} is multi-valued; list predicates "
                                 f"keep their own key")
            # A predicate is GLOBAL (dgraph shares `content` across Post/Comment), so listing it per
            # label -- `Post.content Comment.content` -- is ONE predicate in this group, not two.
            # Only a DIFFERENT group is a conflict; a repeat within this group dedups.
            prev = grouped_pred.get(p)
            if prev is not None and prev != grp.name:
                raise ValueError(f"predicate {p!r} in two groups ({prev!r}, {grp.name!r})")
            grouped_pred[p] = grp.name
            if p not in preds:
                preds.append(p)
        prop_groups[grp.name] = frozenset(preds)

    # --- adjacency directives (of a retired Dgraph variant): sort/inline an etype's reverse adjacency ---
    # Two flavours, by WHERE the sort key lives:
    #   sort/inline (adj_sort/adj_inline): a NEIGHBOR-VERTEX prop (e.g. hasCreator by the message's
    #     creationDate). Validated against declared scalar columns.
    #   sort_facet  (adj_sort_facet):      an EDGE FACET (e.g. likes by the like edge's creationDate).
    #     Validated against declared EDGE props -- the facet already lives in the posting entry, so
    #     the format sorts by it storage-free and reads it for 0 GET.
    scalar_by_label = {}                             # {label: {scalar prop names}} for owner validation
    for v in vertices:
        for p in v.props:
            if not p.multi:
                scalar_by_label.setdefault(v.label, set()).add(p.name)
    edge_facets = {(e.etype, p.name) for e in edges if e.props for p in e.props}
    adj_sort, adj_inline, adj_sort_facet, adj_dir = {}, {}, {}, {}
    for entry in adj:
        # 4-tuples (no direction) remain legal for programmatic callers; the text DSL emits 5.
        mode, etype, prop, owner = entry[:4]
        d = entry[4] if len(entry) > 4 else "in"
        if etype not in etype_names:
            raise ValueError(f"adj_{mode} on undeclared edge type {etype!r}")
        if d not in ("in", "out"):
            raise ValueError(f"adj_{mode} {etype}: direction must be `in` or `out`, got {d!r}")
        # ONE direction per etype (its lines must agree).
        prev = adj_dir.get(etype)
        if prev is not None and prev != d:
            raise ValueError(f"adj_{mode} {etype}: direction {d!r} conflicts with an earlier "
                             f"declaration ({prev!r}) -- an adjacency has one walked direction")
        adj_dir[etype] = d
        if mode == "sort_facet":
            if (etype, prop) not in edge_facets:
                raise ValueError(f"adj_sort_facet {etype}: {prop!r} is not a declared edge facet of "
                                 f"{etype!r} (declare it as an edge prop)")
            adj_sort_facet[etype] = prop
            continue
        # sort / inline: `owner` is the NEIGHBOR vertex type (a label or a type group). Validate the
        # prop is a scalar of that type -- of the label, or of EVERY member of the group (so the walk
        # can read it off whichever concrete neighbor it lands on).
        member_labels = groups.get(owner) or ([owner] if owner in vlabels else None)
        if member_labels is None:
            raise ValueError(f"adj_{mode} {etype}: {owner!r} is not a declared vertex label or type "
                             f"group (in {owner}.{prop})")
        missing = [l for l in member_labels if prop not in scalar_by_label.get(l, ())]
        if missing:
            raise ValueError(f"adj_{mode} {etype} {owner}.{prop}: {prop!r} is not a scalar property "
                             f"of {sorted(missing)}")
        (adj_sort if mode == "sort" else adj_inline)[etype] = prop
    # inline implies sort (you sort by the prop you inline); a stray inline without sort is a typo.
    for etype in adj_inline:
        if etype not in adj_sort:
            raise ValueError(f"adj_inline {etype}: needs a matching `adj_sort {etype} ...` "
                             f"(inline stores the prop the adjacency is sorted by)")
    # a facet-sorted etype and a vertex-prop-sorted etype are mutually exclusive (one order per
    # posting); catch a schema that declares both for the same etype.
    both = set(adj_sort) & set(adj_sort_facet)
    if both:
        raise ValueError(f"etype(s) {sorted(both)} have BOTH adj_sort and adj_sort_facet "
                         f"(an adjacency can be sorted by one key only)")

    # AUTHORING CONSTRAINT: edge endpoints are declared
    # by CONCRETE vertex label, never by a type group/supertype ("Message"). The inline-copy
    # fan-out's label rule matches endpoint labels and their groups against the op's label --
    # a by-group endpoint would match no concrete label and the maintenance would be SILENTLY
    # skipped (copies go stale). Refuse at compile, the one gate every consumer passes.
    labels = {v.label for v in vertices}
    bad_eps = sorted({lab for e in edges for pair in (e.between or ())
                      for lab in pair if lab not in labels})
    if bad_eps:
        raise ValueError(f"edge endpoint label(s) {bad_eps} are not declared vertex labels "
                         f"-- endpoints must be CONCRETE labels, not type groups "
                         f"(a by-group endpoint silently skips the copy-maintenance fan-out)")

    return Schema(
        variant=variant,
        indexed_cols=frozenset(indexed),
        reverse_etypes=frozenset(e.etype for e in edges if e.reverse),
        etypes=frozenset(e.etype for e in edges),
        # Endpoints per edge type, for the write side's incident-edge walk (Schema.incident_etypes).
        # `between` is optional in a declaration; an edge without it contributes no endpoints, so a
        # dataset that omits it simply has no incident-edge model -- loud enough (a delete finds no
        # edges) without breaking the read path, which never consults this.
        edge_endpoints={e.etype: tuple(e.between or ()) for e in edges},
        vertex_props={v.label: tuple(p.name for p in v.props) for v in vertices},
        # Ordered (prop, type) per label/etype -- the codecs' decode schema (nebula RowV2 is
        # positional). This is the one place edge props DO become a Schema fact; the loader
        # keeps reading them by name independently.
        vertex_prop_types={v.label: tuple((p.name, p.type) for p in v.props) for v in vertices},
        edge_prop_types={e.etype: tuple((p.name, p.type) for p in e.props)
                         for e in edges if e.props},
        date_cols=frozenset(sorted_cols),
        multival_cols=frozenset(multival),
        nullable_cols=frozenset(nullable),
        type_groups={g: tuple(members) for g, members in groups.items()},
        type_indexed_labels=frozenset(type_indexed),
        predicates_per_label=predicates_per_label,
        type_scoped_index=frozenset(type_scoped),
        value_sorted_index=frozenset(value_sorted),
        key_sorted_index=frozenset(key_sorted),
        prop_groups=prop_groups,
        adj_sort=adj_sort,
        adj_inline=adj_inline,
        adj_sort_facet=adj_sort_facet,
        # only the non-default `out` is stored, so an explicit `in` and an omitted direction
        # compile identically
        adj_dir={e: d for e, d in adj_dir.items() if d == "out"},
    )
