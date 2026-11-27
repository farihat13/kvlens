"""Dgraph format: predicate-oriented. Key = (predicate, kind, rest), kind the real key's type byte: "D" data, "R"
reverse, "I" index. A scalar predicate stores a value; an edge predicate stores the whole adjacency as one posting-list
blob of [dst, facets] pairs (edge props are facets inside the blob); an index key stores the matching uids in its value.

  (firstName, D, person_933) -> "Chau"                                   # scalar predicate (GET)
  (knows, D, person_933)     -> [[person_1, {creationDate: ...}], ...]   # forward blob (SEEK)
  (knows, R, person_1)       -> [[person_933, {creationDate: ...}], ...] # reverse blob (SEEK)

Op costs: a scalar prop read is 1 GET per (predicate, vertex); an edge read, an index lookup and a list-valued prop
(dgraph.type, multi-valued props) are 1 SEEK reading the whole posting list. The vid is the uid, so an anchor is a
direct address (the engine twin models the real eq(id, N) index lookup instead).

Schema facts are injected (schema.reverse_etypes / date_cols / multival_cols / indexed_cols): an edge type without
@reverse may only be traversed forward; reverse blobs carry facets too. Indexes and list reads are per predicate
(global across types), so __init__ projects the (label, prop) facts down to predicate sets.

Ingest: scalar props stream; edge blobs and index posting lists buffer until finalize_write (a blob needs all of a
vertex's edges, an index key all its uids).
"""
from dataclasses import dataclass

from kvevaluator.formats.base import Format, counted, neighbor_item
from kvevaluator.formats.dgraph_uidpack import uidpack_bytes, uidpack_encode, uidpack_decode
from kvevaluator.formats.dgraph_writes import DgraphWrites

from kvevaluator.cost import KVCost
from kvevaluator.schema.graph import content_bytes
from kvevaluator.kvstore import valcodec
from kvevaluator.schema.vids import uid_of, key_id

@dataclass(frozen=True)
class DgraphSizing:
    """Tunable byte widths for dgraph. 
    Badger key = [prefix][namespace][attr=predicate name (real len)][typeByte][uid].
    model (int 8, bool 1); tune int_bytes/bool_bytes to model a narrower/wider encoding.

    `pack` is VALUE packing toggle -- OFF by default. 
    ON models real Dgraph's UidPack encoding of UID posting lists. See
    uidpack_bytes and value_bytes;"""
    prefix: int = 0             # real badger prefix 1B; 0 in the single-tenant default
    namespace: int = 0          # real 8B; 0 = single-tenant (set 8 for multi-tenant)
    type_byte: int = 1          # the D/R/I typeByte (0x00/0x04/0x02)
    uid: int = 8                # fixed uint64; index-key TERMS are charged at real len instead
    index_id: int = 4           # a TYPE-SCOPED index's key identity (full_index): nebula's folded 4B
                                # index id instead of the predicate string -- the key band then
                                # prices the same bytes as the nebula-keyed formats (NebulaSizing.index_id)
    int_bytes: int = 8          # value leaf: an int property's stored width
    bool_bytes: int = 1         # value leaf: a bool property's stored width
    pack: bool = False          # kind-1 UidPack value packing (off = neutral)


class DgraphFormat(DgraphWrites, Format):
    name = "dgraph"

    # The vid IS the uid: anchor is a direct address (0 SEEK), `id` is derived from the vid,
    # and no fqid/external-id index is built. The engine-faithful subclass sets this False to
    # model the real engine's `eq(id,N)` index lookup (fqid index: build + SEEK anchor + id GET).
    _id_as_uid = True

    def __init__(self, schema, sizing=None):
        super().__init__(schema)
        if schema.predicates_per_label:
            # one predicate per label (data keys and index keys): built for this format's reads only. The subclasses
            # (the engine twin) build bare keys in their own paths; the C++ side refuses the same cases
            if type(self) is not DgraphFormat:
                raise ValueError(f"{type(self).__name__}: per-label predicates (`predicates per_label`) are built for "
                                 "the dgraph format only")
            if schema.multival_cols:
                raise ValueError("dgraph: per-label predicates with a multi-valued property are not built")
            bare = (schema.indexed_cols | schema.date_cols) - schema.type_scoped_index
            if bare:
                raise ValueError(f"dgraph: `predicates per_label` needs every property index declared type_scoped "
                                 f"({sorted(bare)} are not)")
        self.sz = sizing or DgraphSizing()                  # tunable key + value byte widths
        # Predicate projections of the (label, prop) schema facts, computed once
        # (add_vertex/read_vertex_props are per-vertex hot loops). dgraph.type is a
        # format-internal list predicate (every store has it, whatever the dataset).
        self._indexed = schema.indexed_predicates()
        self._list_preds = {"dgraph.type"} | schema.multival_predicates()
        self._date_preds = schema.date_predicates()
        # OPT-IN per-type date/value index (schema.type_scoped_index). Empty on canonical -> the
        # predicate-global keying below is unchanged. When (label, prop) is here, add_vertex keys
        # that posting f"{label}.{prop}" instead of the bare predicate, so the index is scoped per
        # type (the index IS the type restriction) and read_top_k_by_index can walk it candidate-free.
        self._type_scoped = set(schema.type_scoped_index)
        # OPT-IN date-ordered posting VALUE (schema.value_sorted_index, dim-2). Empty on canonical ->
        # the uid-ordered posting below is unchanged. When (label, prop) is here, add_vertex records
        # (ms, vid) instead of the bare vid, finalize_write sorts that bucket by ms (date order), and
        # read_top_k_by_index reads it in date order -- ranking a top-k with NO per-candidate ms GET.
        # value_sorted implies type_scoped (schemadef enforces), so a value-sorted posting is always
        # keyed per type f"{label}.{prop}"; _value_sorted_preds is the set of those pred names, so
        # finalize_write can tell a date-ordered bucket from a uid-ordered one by its key alone.
        self._value_sorted = set(schema.value_sorted_index)
        self._value_sorted_preds = {self._index_pred(lbl, p)
                                    for (lbl, p) in self._value_sorted}
        # OPT-IN ms-in-key date index (schema.key_sorted_index, dim-2 Option B). Empty on canonical ->
        # the day-bucket keying below is unchanged. When (label, prop) is here, add_vertex emits ONE
        # index entry per message keyed by an order-preserving ms+vid composite token (empty value)
        # instead of the day-bucket posting, so a top-k KEY range scan reads exactly K key-only entries
        # and STOPS -- like nebula. key_sorted implies type_scoped (schemadef enforces), so every such
        # entry is keyed per type f"{label}.{prop}". A distinct layout from value_sorted (never both on
        # one column). These entries are keyed UNIQUELY per message, so add_vertex puts them DIRECT (no
        # self._idx accumulation, no finalize_write pass -- unlike the day-bucket / value_sorted branch).
        self._key_sorted = set(schema.key_sorted_index)
        self._key_sorted_preds = {self._index_pred(lbl, p) for (lbl, p) in self._key_sorted}
        self._key_sorted_props = {p for (_lbl, p) in self._key_sorted}   # the no-labels guard
        # the BYTE fold of a type-scoped index key (full_index): its attr is priced at the 4B index
        # id, a key-sorted term at nebula's own pricing (the ms digits + the 8B vid), so the key band
        # of the control arm is like-for-like with the nebula-keyed formats in this model
        self._scoped_preds = {self._index_pred(lbl, p) for (lbl, p) in self._type_scoped}
        self._scoped_props = {p for (_lbl, p) in self._type_scoped}
        self._adj = {}      # (etype, "D"/"R", vertex) -> [[other, facets], ...]
        self._idx = {}      # (pred, token) -> [vid, ...] (or [(ms, vid), ...] for a value_sorted bucket)

    def key_bytes(self, key):
        """Badger key = [prefix][namespace][attr][typeByte][last]. Every dgraph key is a
        3-tuple (attr, kind, last): attr is the predicate name (real content length),
        kind is the D/R/I marker (a 1B typeByte, NOT its string length), and `last` is a
        uid for data/reverse keys (fixed uid width) or an index TERM for I keys (real
        length -- the indexed value/label/day-token)."""
        s = self.sz
        attr, kind, last = key                              # all dgraph keys are 3-tuples
        framing = s.prefix + s.namespace + s.type_byte
        if kind in ("D", "R"):
            last_bytes = s.uid
        elif attr in self._key_sorted_preds:                # ms digits + the 8B vid (nebula's index-key pricing)
            last_bytes = self._KEY_SORTED_MS_WIDTH + s.uid
        else:
            last_bytes = len(str(last))
        attr_bytes = s.index_id if attr in self._scoped_preds else len(str(attr))
        return framing + attr_bytes + last_bytes

    # --- symbolic key widths (the byte-term coefficients; same model key_bytes prices) ---
    def _kw_data(self, pred):
        """Modeled width of a D/R posting key: framing + predicate name + fixed uid (9+len(pred)
        at the defaults)."""
        self._no_per_label("_kw_data")
        s = self.sz
        return s.prefix + s.namespace + s.type_byte + len(str(pred)) + s.uid

    def _adj_prop_sorted(self, key):
        """True if `key` names a reverse adjacency this schema stores in NEIGHBOR-PROP order (a
        date-sorted adjacency: schema.adj_sort / adj_sort_facet). Such a posting is NOT
        uid-ascending, so its UidPack compression is forfeited (uidpack_bytes ordered=False).
        Empty on base dgraph -> always False, so base pricing is unchanged."""
        if not key or not isinstance(key, (tuple, list)):
            return False
        attr = key[0]                                    # dgraph key = (attr=predicate, kind, last)
        return attr in self.schema.adj_sort or attr in self.schema.adj_sort_facet

    def value_bytes(self, v, key=None):
        """Value bytes under dgraph's model. Neutral unless sz.pack, which models UidPack
        over UID posting lists.
        Edge FACETS ride at full content size (they don't pack); the dst UIDs pack whether or not
        facets are present (real dgraph keeps all uids in the Pack, facets in a parallel Postings
        array). `key` (optional) lets a date-sorted adjacency be priced as UidPack-incompressible;
        without it, uid-ascending order is assumed.
        A value is treated as a UID list only when its elements PARSE to int UIDs (key_id), so
        dgraph.type's [label] and multival props (email) correctly stay neutral, and a value
        we can't UID-parse falls back to neutral rather than crashing."""
        def neutral(x):
            return content_bytes(x, self.sz.int_bytes, self.sz.bool_bytes)
        if not self.sz.pack or not isinstance(v, list) or not v:
            return neutral(v)
        # edge adjacency blob: list of [dst, facets] pairs -> pack dst UIDs + facet bytes. A
        # date-sorted adjacency (adj_sort/adj_sort_facet) is not uid-ordered -> ordered=False.
        if all(isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[1], dict)
               for e in v):
            uids = [uid_of(dst) for dst, _ in v]
            if all(u is not None for u in uids):
                ordered = not self._adj_prop_sorted(key)
                return uidpack_bytes(uids, ordered=ordered) + sum(neutral(f) for _, f in v)
            return neutral(v)
        # dim-2 date-ordered index posting: [[vid, ms], ...] (value_sorted_index). The vids are in
        # DATE order, not uid-ascending, so UidPack cannot delta-compress -> ordered=False (same
        # rule as a date-sorted adjacency); the per-entry ms rides at int_bytes each (the analogue
        # of the adjacency's facet bytes). Guarded on the [vid, int] pair shape, so a canonical
        # flat-vid posting (below) is never mistaken for one.
        if all(isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[1], int)
               and not isinstance(e[1], bool) for e in v):
            uids = [uid_of(vid) for vid, _ms in v]
            if all(u is not None for u in uids):
                return uidpack_bytes(uids, ordered=False) + len(v) * self.sz.int_bytes
            return neutral(v)
        # flat UID posting list (an index value): every element parses to a UID (uid-ascending)
        if all(uid_of(e) is not None for e in v):
            return uidpack_bytes([uid_of(e) for e in v])
        return neutral(v)

    # --- physical value codec (it lives on the format) ---------------------------------
    # Head byte on posting values only (leaf keys go straight to the prim codec):
    #   U  flat uid posting (index): UidPack blocks to end of buffer
    #   A  adjacency, uid-ascending: uvarint n + UidPack + n self-describing facet dicts
    #   a  adjacency in stored (date) order: same layout, full-width codes
    #   P  fallback: prim-encoded as-is (demo graphs / non-uid vids / empty)
    # This physical codec stays NAMED (every facet name per entry): the C++ stores default to the schema
    # codec (cpp/kvevaluator/src/codec/props_codec.h), but no gate compares these bytes to C++ (--skip-bytes).
    # Known divergences from value_bytes' model (deliberate, documented): +1 head byte
    # per posting; the adjacency's uvarint count; facet dicts carry their KEY strings +
    # prim tags (real dgraph stores facet keys too -- the MODEL's neutral(f) prices only
    # the values, so val_phybytes > model_value on faceted adjacency, honestly).
    #   d  dim-2 date-ordered index posting (value_sorted): uvarint n + UidPack (date order,
    #      full-width codes) + n ms as prim ints. Round-trips to [[vid, ms], ...].
    _VC_UIDS, _VC_ADJ, _VC_ADJ_UNORD, _VC_PRIM, _VC_DSORT = b"U", b"A", b"a", b"P", b"d"

    def _is_posting_key(self, key):
        attr, kind, _last = key
        return kind == "I" or (kind in ("D", "R") and attr in self.schema.etypes)

    def encode_value(self, key, value):
        if value is None:
            return b""
        if not self._is_posting_key(key):
            return valcodec.encode_prim(value)   # scalar / [label] / multival / fqid leaf
        v = value
        # adjacency blob: [[dst, facets], ...]
        if (isinstance(v, list) and v
                and all(isinstance(e, (list, tuple)) and len(e) == 2
                        and isinstance(e[1], dict) for e in v)):
            uids = [uid_of(dst) for dst, _f in v]
            if all(u is not None for u in uids):
                ordered = not self._adj_prop_sorted(key)
                head = self._VC_ADJ if ordered else self._VC_ADJ_UNORD
                if ordered and any(uids[j] <= uids[j - 1] for j in range(1, len(uids))):
                    # the store MUST hold uid-ascending postings (finalize_write sorts);
                    # an unsorted one here means a write path skipped canonicalization.
                    raise ValueError(f"dgraph codec: unsorted/duplicate adjacency at {key}")
                out = head + valcodec.uvarint(len(v)) + uidpack_encode(uids, ordered)
                for _dst, f in v:
                    out += valcodec.encode_prim(f)
                return out
        # dim-2 date-ordered index posting: [[vid, ms], ...] (value_sorted). Date order, not
        # uid-ascending, so UidPack keeps the STORED order (ordered=False); the n ms follow as
        # prim ints. Guarded on the [vid, int] pair shape (e[1] is int, not the dict of an adjacency).
        elif (isinstance(v, list) and v
              and all(isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[1], int)
                      and not isinstance(e[1], bool) for e in v)):
            uids = [uid_of(vid) for vid, _ms in v]
            if all(u is not None for u in uids):
                out = self._VC_DSORT + valcodec.uvarint(len(v)) + uidpack_encode(uids, ordered=False)
                for _vid, ms in v:
                    out += valcodec.encode_prim(ms)
                return out
        # flat uid posting list (index value)
        elif isinstance(v, list) and v:
            uids = [uid_of(e) for e in v]
            if all(u is not None for u in uids):
                if any(uids[j] <= uids[j - 1] for j in range(1, len(uids))):
                    raise ValueError(f"dgraph codec: unsorted/duplicate index posting at {key}")
                return self._VC_UIDS + uidpack_encode(uids, ordered=True)
        return self._VC_PRIM + valcodec.encode_prim(v)

    def decode_value(self, key, raw):
        if not raw:
            return None
        if not self._is_posting_key(key):
            return valcodec.decode_prim(raw)
        head = raw[:1]
        if head == self._VC_UIDS:
            uids, i = uidpack_decode(raw, 1, None, ordered=True)
            return [format(u, "016x") for u in uids]
        if head in (self._VC_ADJ, self._VC_ADJ_UNORD):
            n, i = valcodec.read_uvarint(raw, 1)
            uids, i = uidpack_decode(raw, i, n, ordered=(head == self._VC_ADJ))
            out = []
            for u in uids:
                f, i = valcodec.decode_prim_stream(raw, i)
                out.append([format(u, "016x"), f])
            if i != len(raw):
                raise ValueError(f"dgraph codec: {len(raw) - i} trailing bytes at {key}")
            return out
        if head == self._VC_DSORT:                            # dim-2 date-ordered posting
            n, i = valcodec.read_uvarint(raw, 1)
            uids, i = uidpack_decode(raw, i, n, ordered=False)
            out = []
            for u in uids:
                ms, i = valcodec.decode_prim_stream(raw, i)
                out.append([format(u, "016x"), ms])
            if i != len(raw):
                raise ValueError(f"dgraph codec: {len(raw) - i} trailing bytes at {key}")
            return out
        if head == self._VC_PRIM:
            return valcodec.decode_prim(raw[1:])
        raise ValueError(f"dgraph codec: unknown head {head!r} at {key}")

    # ----------------------
    # --- STORAGE WRITES ---
    # ----------------------

    def _index_entry(self, label, prop):
        """Does (label, prop) get an index entry? Canonical: the PREDICATE view -- any label carrying
        an indexed / dated predicate lands in its global index (Forum.creationDate, Organisation.name:
        undeclared, yet indexed, faithful to real dgraph's per-predicate @index). A type-scoped design
        indexes only the DECLARED (label, prop) pairs: no other format holds an index the schema does
        not declare, and the arm must not carry one either. The policy is PER DESIGN (any type-scoped
        index flips the whole store to declared-only), not per column. Mirrored by C++
        Dgraph::index_entry."""
        if not self._type_scoped:
            return prop in self._indexed or prop in self._date_preds
        return (label, prop) in self.schema.indexed_cols or (label, prop) in self.schema.date_cols

    def _no_per_label(self, what):
        """refuse a path that builds bare predicate keys or prices them (writes; the model layer's byte and oracle
        functions) under per-label predicates: not built there, never silently wrong"""
        if self.schema.predicates_per_label:
            raise ValueError(f"dgraph: {what} is not built under per-label predicates (`predicates per_label`)")

    def _data_pred(self, label, prop):
        """The predicate a vertex property's data key is under: the bare `prop`, or f"{label}.{prop}" when the
        schema names predicates per label (schema.predicates_per_label) -- then a read needs the label, never
        guessed. Mirrored by C++ Dgraph::data_pred."""
        if not self.schema.predicates_per_label:
            return prop
        if not label or not isinstance(label, str):
            raise ValueError(f"dgraph: a property read under per-label predicates needs the step's label ({prop})")
        return f"{label}.{prop}"

    def _index_pred(self, label, prop):
        """The predicate name a property index (value or date) is keyed under. Predicate-global
        (the bare `prop`) by default -- faithful to dgraph's per-predicate @index (Tag / TagClass /
        Place share one `name` index; every dated type shares one `creationDate` index). When
        (label, prop) is type-scoped (schema.type_scoped_index), it is f"{label}.{prop}", so the
        index is scoped per type -- the index IS the type restriction. STILL a 3-tuple key
        ((pred,"I",token)) either way.
        Mirrored by C++ Dgraph::index_pred."""
        return f"{label}.{prop}" if (label, prop) in self._type_scoped else prop

    # dim-2 Option B (key_sorted) ms zero-pad width. LDBC creationDate is milliseconds since epoch
    # (~1.3e12 for 2010-2013 = 13 digits); 13 zero-padded digits keep LEXICAL == NUMERIC order
    # through year 2286. Matches nebula's f"{ms:013d}" index token, so the two walks compare
    # like-for-like. The vid is appended at its fixed 16-hex width (vids.VID_WIDTH).
    _KEY_SORTED_MS_WIDTH = 13

    def _key_sorted_token(self, ms, vid):
        """dim-2 Option B composite index token: the ms (zero-padded to a fixed width) then the vid,
        joined by ':' so a LEXICAL key range scan yields exact DATE order. ms is FIRST and fixed-width
        so the token sorts by date; the vid (a fixed 16-hex uid string) is the tie-break AND keeps the
        key UNIQUE per message (two messages sharing an ms must not collide into one key).

        NON-inverted ms + seek_for_prev/prev walks newest-first -- the same direction as nebula's
        ms-in-key index and dgraph's own day-bucket walk (so it follows the established pattern, needs
        no MAXMS constant, and gives the exclusive maxDate `hi` filter for FREE: a bare f"{hi:013d}"
        prefix sorts before every same-ms token). See _read_top_k_key_sorted. Inverted ms with a forward scan costs
        the same; this keeps one walk direction across the file. Still a 3-tuple key ((pred, "I", token))."""
        if not 0 <= ms < 10 ** self._KEY_SORTED_MS_WIDTH:   # decimal ASCII is order-preserving only here
            raise ValueError(f"key_sorted token: ms {ms} outside [0, 10^{self._KEY_SORTED_MS_WIDTH}) "
                             f"(the C++ term is a sign-flipped 8-byte int and would sort differently)")
        return f"{ms:0{self._KEY_SORTED_MS_WIDTH}d}:{vid}"


    # ---------------------
    # --- STORAGE READS ---
    # ---------------------

    def _read_list(self, store, key):
        """The posting-list read path: 1 SEEK, whole value at one key, 0 NEXT. A miss (the SEEK
        landed on a foreign key) is an absence miss: its key goes to the miss lane, not read."""
        return store.seek_exact(key)

    @counted
    def read_vertex_by_id(self, store, vid, label=None):
        cands = (vid,) if isinstance(vid, str) else tuple(vid)
        # SIM: direct uid anchor -- the vid IS the uid, 0 SEEK, no fqid/id lookup. (A unified
        # message anchor is one candidate; the concrete Post-vs-Comment type is recovered
        # downstream from dgraph.type, not the anchor.)
        if self._id_as_uid:
            return list(cands), None
        # Engine: `eq(id,N)` -> ONE fqid index point-lookup SEEK per candidate (misses paid).
        for cand in cands:
            uids = self._read_list(store, ("fqid", "I", cand))
            if uids:
                return list(uids), None
        return [], None

    @counted
    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False):
        kinds = {"out": ("D",), "in": ("R",), "both": ("D", "R")}[dir]
        if etype not in self.schema.etypes:
            raise ValueError(f"dgraph read_neighbors: unknown etype {etype!r}")
        if "R" in kinds and etype not in self.schema.reverse_etypes:
            raise ValueError(f"dgraph: predicate '{etype}' has no @reverse; "
                             f"a plan may only traverse it forward")
        out, seen = [], set()                       # srcs read as a SET (base docstring)
        for s in srcs:
            if s in seen:
                continue
            seen.add(s)
            for kind in kinds:
                postings = self._read_list(store, (etype, kind, s)) or []  # 1 SEEK each
                for other, facets in postings:
                    out.append(neighbor_item(s, other, facets, edge_props, pairs))
        return out, None

    @counted
    def read_vertex_props(self, store, vids, props, label=None):
        # 1 GET per requested scalar predicate; 1 SEEK per list-valued predicate
        # (each (predicate, vertex) is its own key). `label` is ignored: predicates
        # are shared across types, so an unknown Post-vs-Comment costs nothing extra
        # here -- a genuine predicate-oriented advantage. Distinct vids are read
        # once; output stays one row per input vid.
        cache = {}
        for v in dict.fromkeys(vids):
            row = {"_vid": v}
            for p in props:
                if p == "id" and self._id_as_uid:
                    row[p] = key_id(v)          # SIM: id is the vid -- derived, 0 op. (Engine GETs it.)
                elif p in self._list_preds:
                    row[p] = self._read_list(store, (self._data_pred(label, p), "D", v))
                else:
                    row[p] = store.get((self._data_pred(label, p), "D", v))
            cache[v] = row
        return ([dict(cache[v]) for v in vids],
                None)

    def read_anchor_props(self, store, cands, props, labels=None):
        # SIM: direct anchor -- the vid is the uid, read the (unified message) candidate's props
        # straight (no fqid resolve, 0 extra SEEK). Props that don't apply to the concrete
        # Post/Comment type read as None (predicates are per-key, nothing overlaps).
        if self._id_as_uid:
            return self.read_vertex_props(store, list(cands), props)
        # Engine: resolve which candidate exists (fqid SEEK per try, misses paid) then read it.
        S = 0
        for cand in cands:
            S += 1
            if self._read_list(store, ("fqid", "I", cand)):
                rows, c = self.read_vertex_props(store, [cand], props)
                return rows, KVCost(S=S + c.S, G=c.G)
        return [], KVCost(S=S)

    @counted
    def read_index_eq(self, store, label, prop, value):
        # Index point lookup: 1 SEEK; the matching uids are in the VALUE (one
        # posting list per term -- hash index, limit-hostile but 1 op regardless
        # of t). `label` picks the per-type posting when the index is type-scoped; on
        # canonical it is unused: dgraph indexes are per-PREDICATE, so the list can mix
        # types (callers filter if it matters; fine for firstName/name).
        if not label and self._type_scoped:   # a bare predicate has no posting under a type-scoped design: refuse, never []
            raise ValueError(f"dgraph read_index_eq: a type-scoped index needs the step's label ({prop})")
        if (label, prop) in self._key_sorted:
            raise ValueError(f"dgraph read_index_eq: {label}.{prop} is a key-sorted date index; "
                             f"use read_index_range [v, v+1)")
        vids = self._read_list(store, (self._index_pred(label, prop), "I", str(value))) or []
        return list(vids), None

    def _key_sorted_walk(self, labels, prop, op):
        """Do `labels` key-sort `prop`? All of them -> True; none -> False; a mix raises (one walk
        per op). Mirrored by C++ Dgraph::key_sorted_walk."""
        labels_t = ((labels,) if isinstance(labels, str) else tuple(labels)) if labels else ()
        if not labels_t and prop in self._key_sorted_props:
            # no labels would fall through to the day-token walk of the bare predicate -- an EMPTY
            # band under this design, so a silent [] both languages would agree on
            raise ValueError(f"{op}: {prop} is key-sorted (per type); the step must name its labels")
        n = sum(1 for lbl in labels_t if (lbl, prop) in self._key_sorted)
        if n and n != len(labels_t):
            raise ValueError(f"{op}: {prop} is key-sorted on some of the labels but not all -- one walk per op")
        return n > 0

    def _day_walk_preds(self, labels, prop, op):
        """The index bands a DAY-token walk of `prop` crosses: each label's own f"{label}.{prop}" band when every
        label type-scopes it (type_scoped / value_sorted designs), else the one predicate-global band. A mix, or no
        labels on a type-scoped prop, raises: the bare band is EMPTY under such a design, so the walk would return a
        silent []. Mirrored by C++ Dgraph::day_walk_preds."""
        labels_t = ((labels,) if isinstance(labels, str) else tuple(labels)) if labels else ()
        if not labels_t and prop in self._scoped_props:
            raise ValueError(f"{op}: {prop} is type-scoped; the step must name its labels")
        n = sum(1 for lbl in labels_t if (lbl, prop) in self._type_scoped)
        if n and n != len(labels_t):
            raise ValueError(f"{op}: {prop} is type-scoped on some of the labels but not all -- one walk per op")
        return [self._index_pred(lbl, prop) for lbl in labels_t] if n else [prop]

    def _day_range(self, store, labels, prop, lo, hi, buckets, op):
        """read_index_range / index_range_buckets over a DAY-token date index: per band (_day_walk_preds), seek the lo
        day and step to the hi day, taking each token's whole posting -- DAY-GRANULAR, a superset at the two
        boundary days (the caller's exact-ms filter trims them). 1 SEEK per band + 1 NEXT per day token crossed; the
        landing past each band's window is an absence miss. A value-sorted posting holds [vid, ms] pairs: its vids."""
        # hi is exclusive: its last day is (hi - 1)'s, not hi's (a midnight hi would add a whole day)
        lo_tok, hi_tok = f"{lo // 86400000:07d}", f"{(hi - 1) // 86400000:07d}"
        out, n = [], 0
        for pred in self._day_walk_preds(labels, prop, op):
            sorted_value = pred in self._value_sorted_preds
            cur = store.seek((pred, "I", lo_tok))
            while cur.valid() and cur.key()[:2] == (pred, "I") and cur.key()[2] <= hi_tok:
                vids = [e[0] for e in cur.value()] if sorted_value else cur.value()   # this day's posting
                if buckets:
                    day = int(cur.key()[2])
                    out.extend({"_vid": v, "_bucket": day} for v in vids)
                else:
                    out.extend(vids)
                cur.next()
                n += 1
            cur.mark_miss()                        # the landing past the window: an absence miss
        return out, n

    def _key_sorted_range(self, store, labels, prop, lo, hi, buckets):
        """read_index_range / index_range_buckets over a KEY-SORTED date index (key_sorted_index,
        dim-2 Option B; C++ Dgraph::read_index_range's key-sorted walk): per label, seek the lo ms
        under its per-type f"{label}.{prop}" index (the bare 13-digit token sorts before every
        same-ms entry) and step while the key's ms < hi -- every entry crossed is in the window
        (exact, no boundary superset), key-only (no value read), 1 NEXT each; the landing past the
        window is an absence miss. `_bucket` keeps the op's contract: the DAY number."""
        labels_t = ((labels,) if isinstance(labels, str) else tuple(labels))
        lo_tok = f"{lo:0{self._KEY_SORTED_MS_WIDTH}d}"
        out, n = [], 0
        for lbl in labels_t:
            pred = self._index_pred(lbl, prop)
            cur = store.seek((pred, "I", lo_tok))                     # 1 SEEK per label
            while cur.valid() and cur.key()[:2] == (pred, "I"):
                ms_s, vid = cur.key()[2].split(":", 1)               # composite token -> ms, vid (key-only)
                ms = int(ms_s)
                if ms >= hi:
                    break
                out.append({"_vid": vid, "_bucket": ms // 86400000} if buckets else vid)
                cur.next()                                            # 1 NEXT per entry crossed
                n += 1
            cur.mark_miss()                                           # the landing past the window
        return out, KVCost(S=len(labels_t), N=n)

    def read_index_range(self, store, labels, prop, lo, hi):
        if self._key_sorted_walk(labels, prop, "read_index_range"):
            return self._key_sorted_range(store, labels, prop, lo, hi, False)
        # Day-token index forward walk (_day_range). Predicate-global by default: `labels` ignored (every dated
        # type shares the index -- so this also picks up Forum dates, faithful); a type-scoped design walks each
        # label's own band.
        out, n = self._day_range(store, labels, prop, lo, hi, False, "read_index_range")
        nbands = len(self._day_walk_preds(labels, prop, "read_index_range"))
        return out, KVCost(S=nbands, N=n)

    @counted
    def read_vertices_by_label(self, store, label, props=None):
        if props is not None:
            return self._read_label_run(store, label, props)
        # Type-enumeration entry: read the dgraph.type index posting for this label
        # -- the uids are in the VALUE at one key ("dgraph.type","I",label), so it's
        # 1 SEEK regardless of t (same posting-list read path as read_index_eq/read_vertex_by_id).
        # This is the "ONE type-index SEEK reading every uid of that type" that
        # read_filter_by_label_probe names as its alternative; the label-only plans enter here.
        # A design that does NOT declare a per-type index for this label has no such posting, and
        # reading the absent key would return [] -- a silently WRONG answer (an empty enumeration
        # looks exactly like a type with no members). dgraph has no other single-type covering scan:
        # its value indexes are per-PREDICATE and mix types. So refuse, loudly, rather than hand
        # back an empty result the cross-format `agree` check would happily bless (all dgraph-family
        # formats would break identically). Same reasoning as resolve_for's index-free guard.
        if not self.schema.has_type_index(label):
            raise ValueError(
                f"dgraph read_vertices_by_label({label!r}): this design declares no per-type index "
                f"for {label!r}, and dgraph has no other way to enumerate one type (its value "
                f"indexes are per-predicate and mix types). Reading the absent posting would "
                f"return an empty list, not an error -- so this is refused. Declare `index "
                f"{label}` in the schema, or use a plan that does not enumerate by type.")
        vids = self._read_list(store, ("dgraph.type", "I", label)) or []
        return list(vids), None

    def _read_label_run(self, store, label, props):
        """The label with its properties in one run per property (per-label predicates only; C++
        Dgraph::read_label_run): the label's type posting (every vertex of the label, so one that lacks a property
        still gets its row), then per wanted property ONE scan of its predicate: 1 SEEK + 1 NEXT per stored value."""
        if not self.schema.predicates_per_label:
            raise ValueError("dgraph read_vertices_by_label takes no props unless the schema names predicates per label")
        vids, cost = self.read_vertices_by_label(store, label)
        cols, S, N = {}, 0, 0
        for p in props:
            if p == "id" and self._id_as_uid:
                continue
            if p in self._list_preds:
                raise ValueError(f"dgraph read_vertices_by_label(props): list predicate {p}")
            pred, col = self._data_pred(label, p), {}
            cur = store.seek((pred, "D", ""))                          # 1 SEEK per property
            S += 1
            while cur.valid() and cur.key()[:2] == (pred, "D"):
                col[cur.key()[2]] = cur.value()
                cur.next()                                             # 1 NEXT per stored value
                N += 1
            cur.mark_miss()                                            # the landing past the predicate
            cols[p] = col
        rows = [{"_vid": v, **{p: (key_id(v) if p == "id" and self._id_as_uid else cols[p].get(v)) for p in props}} for v in vids]
        cost.S += S
        cost.N += N
        return rows, cost

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """a posting list per (predicate, uid); the reverse list only where the schema declares @reverse"""
        return {"adjacency_lists"} | ({"reverse_adjacency"} if etype in schema.reverse_etypes else set())

    @classmethod
    def capabilities(cls, schema):
        # The dgraph.type posting makes index-once 1 SEEK whatever the type size -- when the
        # design declares the type index (all labels or none; partial sets are not a design).
        # A day-token date index answers a range with the boundary-day SUPERSET (read then
        # re-filter). With EVERY date index key-sorted the walk is exact and stops early, and the
        # unsorted story goes -- one range-walk story per design (a design
        # holding both would take the bucket bodies meant for day-granular indexes). Mirrored in C++.
        caps = set()
        if not (schema.date_cols and schema.date_cols <= schema.key_sorted_index):
            caps.add("unsorted_range_index")
        else:
            caps.add("sorted_range_index")      # every date index exact (ms in the key): the walk stops early
        if schema.type_indexed_labels and schema.type_indexed_labels >= set(schema.vertex_props):
            caps |= {"type_index", "type_index_single_posting"}
            if schema.predicates_per_label:      # a label's values of one property are one whole predicate: the label
                caps.add("label_rows_one_run")   # can be read with its properties in one run per property (C++ too)
        # OPT-IN per-type keying on EVERY property index (value and date; the full_index design):
        # the index IS the type restriction everywhere, so per_type_index is literally true and the
        # candidate-free top-k bodies the per-type formats take select here too. A design that
        # scopes only some indexes keeps dgraph's mixed-type story (no cap).
        prop_cols = schema.indexed_cols | schema.date_cols
        if prop_cols and prop_cols <= schema.type_scoped_index:
            caps.add("per_type_index")
        # NOT sorted_range_index for a type-scoped index alone (a type-scoped index alone): a day-token index stays
        # day-granular, so read_index_range returns the boundary days' superset -- entry_range counted 4/42/582
        # vs 2/41/580 on type_scoped_date. Exact labels get it per label (capabilities_for).
        # OPT-IN date-ordered posting VALUE (schema.value_sorted_index, dim-2): the top-k reads each
        # touched day's value in date order and ranks with NO per-candidate ms GET -- the ops win the
        # day-granular type_scoped walk lacks. The topk bundle's `sorted_range_index` variant (which
        # value_sorted also advertises, via its implied type_scoped) already emits the candidate-free
        # walk this needs, so NO new plan variant is required: select_variant picks sorted_range_index,
        # and read_top_k_by_index routes to the date-ordered path from the schema. This capability is
        # advertised so a future plan CAN require the ops-optimal walk explicitly, and so the caps
        # fingerprint distinguishes a value-sorted design. Empty on canonical/type_scoped_date.
        if schema.value_sorted_index:
            caps |= {"value_sorted_index"}
        # OPT-IN ms-in-key date index (schema.key_sorted_index, dim-2 Option B): the top-k KEY range
        # scan reads exactly K key-only entries and STOPS -- the byte floor value_sorted (day-bucket,
        # reads the whole boundary day's VALUE) leaves. key_sorted implies type_scoped, so
        # sorted_range_index is already advertised above and select_variant picks that variant;
        # read_top_k_by_index then routes to the ms-in-key path from the schema, so NO new plan variant
        # is required. Advertised so a future plan CAN demand the byte-optimal walk, and so the caps
        # fingerprint distinguishes a key-sorted design. Empty on canonical/type_scoped/value_sorted.
        if schema.key_sorted_index:
            caps |= {"key_sorted_index"}
        return caps

    @classmethod
    def capabilities_for(cls, schema, label):
        # per_type_index PER LABEL: a label whose every declared property index (value and date) is type-scoped
        # has an index that IS its type restriction, even when other labels' indexes stay shared (the date-index
        # designs scope only Post/Comment creationDate). Per label, not per (label, prop): Post and Comment have
        # exactly one index in every scoped design today; a per-property rule waits until a design needs it.
        # Mirrored in C++ (Dgraph::capabilities_for).
        caps = cls.capabilities(schema)
        cols = {(lbl, p) for (lbl, p) in schema.indexed_cols | schema.date_cols if lbl == label}
        if cols and cols <= schema.type_scoped_index:
            caps = caps | {"per_type_index"}
        # sorted_range_index PER LABEL: every date index the label declares is key-sorted (ms in the key, so
        # read_index_range walks it exactly: _key_sorted_walk); a day-token index is a superset at the edges
        dates = {(lbl, p) for (lbl, p) in schema.date_cols if lbl == label}
        if dates and dates <= schema.key_sorted_index:
            caps = caps | {"sorted_range_index"}
        return caps

    def read_filter_by_label_index(self, store, vids, label):
        # ONE type-index enumeration (read_vertices_by_label) + a free in-memory intersect:
        # 1 SEEK regardless of |vids|. The plan picks this op when the format holds
        # type_index_single_posting (the resolver's lowering of `filter_by_label`).
        if not vids:
            return [], KVCost()
        members, cost = self.read_vertices_by_label(store, label)      # 1 SEEK, whole posting
        keep = set(members)
        return [v for v in vids if v in keep], cost

    @counted
    def read_filter_by_label_probe(self, store, vids, label):
        # The per-candidate probe: a dgraph.type read per DISTINCT vid, a list predicate -> 1 SEEK each; a repeated
        # vid is kept at every occurrence.
        if not isinstance(label, str):
            raise ValueError(f"dgraph read_filter_by_label_probe needs one label, got {label!r} "
                             f"(list-label filtering is unimplemented)")
        if not vids:
            return [], KVCost()
        keep = {v: label in (self._read_list(store, ("dgraph.type", "D", v)) or []) for v in dict.fromkeys(vids)}
        return [v for v in vids if keep[v]], None

    def _read_top_k_type_scoped(self, store, vids, prop, k, hi, labels, props, id_order):
        """Top-k over a PER-TYPE (type_scoped_index) day-token date index: one cursor per label's
        own f"{label}.{prop}" index, merged newest-first by day token, mirroring nebula's per-tag
        multi-cursor merge. Because the index IS the type restriction, `vids=None` is legal (every
        entry is a hit); a candidate set, if given, is intersected. It STAYS day-granular, so once
        the newest day-bucket(s) are crossed the exact ms is still read per candidate to rank -- the
        "condensed dim-2" residual vs nebula (whose ms lives in the key). The stop is at the k-th
        DAY boundary: after a whole day is ranked and >= k survivors are in hand, lower days are
        strictly older, so the walk stops (it does not read every candidate the way the
        candidate-set default path must)."""
        DAY = 86400000
        cand = None if vids is None else set(vids)
        # hi is exclusive: its last day is (hi - 1)'s, not hi's (a midnight hi would add a whole day)
        hi_day = None if hi is None else (hi - 1) // DAY
        start = "~" if hi is None else f"{hi_day:07d}"          # "~" sorts after digits
        cursors = []                                            # one per label's per-type index
        for lbl in labels:
            pred = f"{lbl}.{prop}"
            cursors.append((pred, store.seek_for_prev((pred, "I", start))))  # 1 SEEK each

        def cur_day(pc):                                        # the day token under a cursor, or None
            pred, cur = pc
            if cur.valid() and cur.key()[:2] == (pred, "I"):
                return int(cur.key()[2])
            return None

        ranked = []                                            # rows {_vid, prop} with exact ms
        walked = S = G = 0
        while True:
            best_day = None                                    # newest day still under any cursor
            for pc in cursors:
                d = cur_day(pc)
                if d is not None and (best_day is None or d > best_day):
                    best_day = d
            if best_day is None:
                break
            day_vids = []                                      # all labels' entries at best_day merge
            for pc in cursors:
                if cur_day(pc) == best_day:
                    pred, cur = pc
                    posting = cur.value()                      # whole day-bucket (scan_value_bytes)
                    day_vids.extend(posting)
                    cur.prev()                                 # 1 NEXT per token stepped
                    walked += 1
            hits = day_vids if cand is None else [u for u in day_vids if u in cand]
            if hits:
                drows, c = self.read_vertex_props(store, hits, [prop])   # exact-ms GET per hit
                S += c.S; G += c.G
                if hi is not None and best_day == hi_day:      # within the hi-day: keep < hi only
                    drows = [r for r in drows if r[prop] < hi]
                ranked.extend(drows)
            if len(ranked) >= k:                               # k-th DAY boundary crossed: done
                break
        for _pred, cur in cursors:                             # each rests on an unconsumed entry
            cur.mark_miss()                                    # (or off its index): an absence miss
        # rank by date desc; id is the tie-break (key-derived under id_as_uid -> 0 GET, else GET
        # only for the boundary tie-group), matching the default path.
        ranked.sort(key=lambda r: r[prop], reverse=True)
        cut = ranked[k - 1][prop] if len(ranked) >= k else None
        need = ranked if cut is None else [r for r in ranked if r[prop] >= cut]
        irows, c = self.read_vertex_props(store, [r["_vid"] for r in need], ["id"])
        S += c.S; G += c.G
        for r, ir in zip(need, irows):
            r["id"] = ir["id"]
        need.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        need.sort(key=lambda r: r[prop], reverse=True)
        rows = need[:k]
        if props:
            xrows, c = self.read_vertex_props(store, [r["_vid"] for r in rows], props)
            S += c.S; G += c.G
            for r, xr in zip(rows, xrows):
                for p in props:
                    r[p] = xr[p]
        return rows, KVCost(S=len(cursors) + S, G=G, N=walked)

    def _read_top_k_value_sorted(self, store, vids, prop, k, hi, labels, props, id_order):
        """Top-k over a DATE-ORDERED (value_sorted_index, dim-2) day-token index: one cursor per
        label's per-type f"{label}.{prop}" index (value_sorted implies type_scoped), merged
        newest-first by day token. The DIFFERENCE from _read_top_k_type_scoped: each day bucket's
        VALUE is stored date-descending [vid, ms] pairs, so the ms is read straight from the value --
        NO per-candidate ms GET. That is dim-2's ops win: ranking, the exact intra-day maxDate filter,
        and the result's creationDate all come from the value for free; only the K winner rows pay a
        props GET (like nebula). `vids=None` (candidate-free) is legal because the per-type index IS
        the type restriction; a candidate set, if given, is intersected. Stop is at the k-th DAY
        boundary: once a whole day (all labels' buckets at that day) is read and >= k survivors are in
        hand, every lower day is strictly older, so the walk stops."""
        DAY = 86400000
        cand = None if vids is None else set(vids)
        # hi is exclusive: its last day is (hi - 1)'s, not hi's (a midnight hi would add a whole day)
        hi_day = None if hi is None else (hi - 1) // DAY
        start = "~" if hi is None else f"{hi_day:07d}"          # "~" sorts after digits
        cursors = []                                            # one per label's per-type index
        for lbl in labels:
            pred = self._index_pred(lbl, prop)             # per-type (value_sorted => type_scoped)
            cursors.append((pred, store.seek_for_prev((pred, "I", start))))  # 1 SEEK each

        def cur_day(pc):                                        # the day token under a cursor, or None
            pred, cur = pc
            if cur.valid() and cur.key()[:2] == (pred, "I"):
                return int(cur.key()[2])
            return None

        entries = []                                            # accumulated (ms, vid), date order in value
        walked = 0
        while True:
            best_day = None                                     # newest day still under any cursor
            for pc in cursors:
                d = cur_day(pc)
                if d is not None and (best_day is None or d > best_day):
                    best_day = d
            if best_day is None:
                break
            for pc in cursors:
                if cur_day(pc) == best_day:
                    pred, cur = pc
                    posting = cur.value()                       # [[vid, ms], ...] date-desc; bytes charged
                    for vid, ms in posting:
                        # exact intra-day maxDate filter: an entry AT/above hi is excluded (ms in
                        # the value, so free -- no ms GET). Lower days are wholly < hi.
                        if hi is not None and ms >= hi:
                            continue
                        if cand is None or vid in cand:
                            entries.append((ms, vid))
                    cur.prev()                                  # 1 NEXT per token stepped
                    walked += 1
            if len(entries) >= k:                               # k-th DAY boundary crossed: done
                break
        for _pred, cur in cursors:                              # each rests on an unconsumed entry
            cur.mark_miss()                                     # (or off its index): an absence miss
        # rank by (date desc, id asc/desc); id = key_id(vid) is derived under id_as_uid -> 0 GET.
        rows = [{"_vid": vid, prop: ms, "id": key_id(vid)} for ms, vid in entries]
        rows.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        rows.sort(key=lambda r: r[prop], reverse=True)          # stable: date desc, id tie-break kept
        rows = rows[:k]
        S = G = 0
        if props:                                               # winner rows only: 1 GET each (like nebula)
            xrows, c = self.read_vertex_props(store, [r["_vid"] for r in rows], props)
            S += c.S; G += c.G
            for r, xr in zip(rows, xrows):
                for p in props:
                    r[p] = xr[p]
        return rows, KVCost(S=len(cursors) + S, G=G, N=walked)

    def _read_top_k_key_sorted(self, store, vids, prop, k, hi, labels, props, id_order):
        """Top-k over a KEY-SORTED (key_sorted_index, dim-2 Option B) date index: one index ENTRY per
        message, ms+vid folded into an order-preserving composite token f"{ms:013d}:{vid}" (empty
        value), so a KEY range scan yields exact date order. One cursor per label's per-type
        f"{label}.{prop}" index (key_sorted implies type_scoped), merged newest-first.

        The DIFFERENCE from _read_top_k_value_sorted: NO whole-day bucket VALUE is read.
        The ms and vid are BOTH in the key, so the walk reads exactly the entries it crosses (one key
        per NEXT) and STOPS at the k-th date -- scan_key proportional to K, scan_value ~ 0, matching
        nebula (whose ms is also in the index key). Ranking, the exact maxDate filter, and the result
        creationDate are all key-derived (GET-free); only the K winner rows pay a props GET. `vids=None`
        (candidate-free) is legal because the per-type index IS the type restriction; a candidate set,
        if given, is intersected. The exclusive `hi` (maxDate) is FREE: seek_for_prev on the bare
        f"{hi:013d}" lands on the last entry strictly below hi (an entry AT hi extends the token with
        ':vid' and sorts after) -- nebula's mechanism. The drain past the k-th date makes the (date
        desc, id) tie-break cut complete before the stop, exactly like nebula's walk."""
        cand = None if vids is None else set(vids)
        bound = "~" if hi is None else f"{hi:0{self._KEY_SORTED_MS_WIDTH}d}"   # "~" sorts after digits;
                                                    # a bare ms prefix sorts before the same-ms ':vid' tokens
        cursors = []                                            # one per label's per-type index
        for lbl in labels:
            pred = self._index_pred(lbl, prop)             # per-type (key_sorted => type_scoped)
            cursors.append((pred, store.seek_for_prev((pred, "I", bound))))   # 1 SEEK each

        def cur_entry(pc):                                      # (ms, vid) under a cursor, or None
            pred, cur = pc
            if cur.valid() and cur.key()[:2] == (pred, "I"):
                ms_s, vid = cur.key()[2].split(":", 1)          # composite token -> ms, vid (key-only)
                return int(ms_s), vid
            return None

        hits, walked = [], 0
        while True:
            best = None                                         # cursor holding the newest (largest ms) entry
            for pc in cursors:
                e = cur_entry(pc)
                if e is not None and (best is None or e[0] > best[1][0]):
                    best = (pc, e)
            if best is None:
                break
            if len(hits) >= k and best[1][0] != hits[-1][prop]:
                break                                           # past the k-th date: the tie group is complete
            pc, (ms, vid) = best
            if cand is None or vid in cand:
                hits.append({"_vid": vid, prop: ms, "id": key_id(vid)})
            pc[1].prev()                                        # 1 NEXT per entry crossed (no value() read)
            walked += 1
        for _pred, cur in cursors:                              # each rests on an unconsumed entry
            cur.mark_miss()                                     # (or off its index): an absence miss
        # rank by (date desc, id asc/desc); id = key_id(vid) is key-derived -> 0 GET.
        hits.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        hits.sort(key=lambda r: r[prop], reverse=True)          # stable: date desc, id tie-break kept
        rows = hits[:k]
        S = G = 0
        if props:                                               # winner rows only: 1 GET each (like nebula)
            xrows, c = self.read_vertex_props(store, [r["_vid"] for r in rows], props)
            S += c.S; G += c.G
            for r, xr in zip(rows, xrows):
                for p in props:
                    r[p] = xr[p]
        return rows, KVCost(S=len(cursors) + S, G=G, N=walked)

    def read_top_k_by_index(self, store, vids, prop, k, order="desc", hi=None,
                       labels=None, props=None, id_order="asc"):
        """The ranking access path real dgraph uses for `orderdesc: creationDate, first: k`: walk the prop's day-token index
        newest-first (1 SEEK + 1 NEXT per token, each token's whole uid posting list read), intersect each token with the
        candidate set until k candidates are collected (day granularity makes that a complete top-k boundary), then GET the
        exact ms of the crossed candidates to rank them, and the id only for the date-tie group at the k-th boundary.
        Returns rows [{_vid, prop, id}] sorted (prop desc, id asc), top k. It never walks a lower day once the boundary day
        holds k survivors, and never fetches id for every candidate.

        `hi` (exclusive ms) fuses the maxDate filter into the walk: it starts at hi's day token, whose candidates pay an
        exact-ms GET each; every lower token is entirely below hi. `labels` is ignored (the index is per predicate). `props`
        are fetched for the final top k only, kept in the op so the contract matches nebula / arango. `id_order` sets the
        tie-break direction (LDBC: asc everywhere but IS2).
        """
        assert order == "desc", "the LDBC shape; asc would walk forward"
        labels_t = ((labels,) if isinstance(labels, str) else tuple(labels)) if labels else ()
        # OPT-IN ms-in-key value (schema.key_sorted_index, dim-2 Option B): CHECKED FIRST -- key_sorted
        # implies type_scoped and is a distinct layout from value_sorted, so it must win over both
        # branches below. When every queried label key-sorts this prop, walk the per-type indexes and
        # rank straight from the ms+vid composite KEY, reading exactly K key-only entries (no whole-day
        # value read) -- the byte-optimal walk, matching nebula at every K.
        if labels_t and all((lbl, prop) in self._key_sorted for lbl in labels_t):
            return self._read_top_k_key_sorted(store, vids, prop, k, hi, labels_t,
                                               props, id_order)
        # OPT-IN date-ordered value (schema.value_sorted_index, dim-2): CHECKED FIRST because
        # value_sorted implies type_scoped -- a value-sorted column is also type-scoped, so this must
        # win over the type_scoped branch below. When every queried label value-sorts this prop, walk
        # the per-type indexes and rank from the date-ordered value with no per-candidate ms GET.
        if labels_t and all((lbl, prop) in self._value_sorted for lbl in labels_t):
            return self._read_top_k_value_sorted(store, vids, prop, k, hi, labels_t,
                                                 props, id_order)
        # OPT-IN per-type date index (schema.type_scoped_index): when every queried label keys this
        # prop's index per type, walk each label's own f"{label}.{prop}" index and merge newest-first
        # (nebula-style), so the index IS the type restriction and vids=None is legal. Off by default
        # (canonical -> _type_scoped empty -> this is never taken, the default path below is unchanged).
        if labels_t and all((lbl, prop) in self._type_scoped for lbl in labels_t):
            return self._read_top_k_type_scoped(store, vids, prop, k, hi, labels_t,
                                                props, id_order)
        # the default walk reads the predicate-global band: refuse no labels / a label mix on a type-scoped prop (that
        # band is empty for the scoped types -- a silent []), the range walk's rule (_day_walk_preds)
        self._day_walk_preds(labels_t, prop, "read_top_k_by_index")
        # vids=None (global top-k, no candidate set) is deliberately NOT supported
        # here: dgraph's date index is per-PREDICATE -- Person/Post/Comment/Forum
        # dates share one token space -- so an unrestricted walk would collect
        # non-message uids into a "top-k messages" answer. A global-top-k plan on
        # dgraph must pass the type posting(s) as candidates (read_vertices_by_label is
        # 1 SEEK each); nebula/arango accept vids=None because their per-tag /
        # per-collection index IS the type restriction.
        if vids is None:
            raise ValueError("dgraph read_top_k_by_index needs a candidate set: its date "
                             "index mixes all types (pass read_vertices_by_label output)")
        cand = set(vids)
        prefix = (prop, "I")
        rows, pending = [], []          # rows: ms-checked boundary survivors
        S = G = walked = 0
        # hi is exclusive: its last day is (hi - 1)'s, not hi's (a midnight hi would add a whole day)
        start = "~" if hi is None else f"{(hi - 1) // 86400000:07d}"   # "~" sorts after digits
        cur = store.seek_for_prev((prop, "I", start))
        if hi is not None and cur.valid() and cur.key() == (prop, "I", start):
            bhits = [u for u in cur.value() if u in cand]
            if bhits:
                brows, c = self.read_vertex_props(store, bhits, [prop])   # exact-ms check
                S += c.S; G += c.G
                rows = [r for r in brows if r[prop] < hi]
            cur.prev()
            walked += 1
        # Walk lower day-tokens ONLY if the boundary day did not already supply k survivors.
        # When it did, the top-k is entirely within the boundary day (every boundary survivor
        # is newer than any lower-day message), so reading a lower bucket is pure waste
        # (an unconditional first step here inflates a k=1 query by a whole day-bucket of GETs).
        consumed = False                                      # the landing the loop ends on: read, or not
        while len(rows) + len(pending) < k and cur.valid() and cur.key()[:2] == prefix:
            hits_here = [u for u in cur.value() if u in cand]  # tokens < hi-day: all valid
            pending.extend(hits_here)
            if len(rows) + len(pending) >= k:
                consumed = True
                break
            cur.prev()                                        # 1 NEXT per token step
            walked += 1
        if not consumed:
            cur.mark_miss()                                   # an unconsumed landing: an absence miss
        # exact ms for the pending candidates (the boundary `rows` already carry ms). id is
        # DEFERRED -- it is a pure tie-break, so it is fetched below only for the candidates
        # whose date could affect the cut, NOT [prop, id] for every pending candidate.
        if pending:
            prows, c = self.read_vertex_props(store, pending, [prop])
            S += c.S; G += c.G
        else:
            prows = []
        ranked = rows + prows
        ranked.sort(key=lambda r: r[prop], reverse=True)      # by date desc; ties unresolved
        # id ONLY for candidates whose date is >= the k-th date -- the only ones whose order
        # can change the top-k. Rows below the cut never enter the result, so their id is
        # never read (reading id for ALL pending would ~2x the pending cost even at
        # near-unique creationDates). Degenerates to "id for all" only when every date ties.
        cut = ranked[k - 1][prop] if len(ranked) >= k else None
        need = ranked if cut is None else [r for r in ranked if r[prop] >= cut]
        irows, c = self.read_vertex_props(store, [r["_vid"] for r in need], ["id"])
        S += c.S; G += c.G
        for r, ir in zip(need, irows):                        # same vid order: aligned
            r["id"] = ir["id"]
        need.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        need.sort(key=lambda r: r[prop], reverse=True)
        rows = need[:k]
        if props:
            xrows, c = self.read_vertex_props(store, [r["_vid"] for r in rows], props)
            S += c.S; G += c.G
        if props:
            for r, xr in zip(rows, xrows):                    # same vid order: aligned
                for p in props:
                    r[p] = xr[p]
        return rows, KVCost(S=1 + S, G=G, N=walked)
    
    # dgraph only
    def index_range_buckets(self, store, labels, prop, lo, hi):
        # read_index_range's tagged twin: same one-cursor day-token walk, but each vid is
        # returned as a row {"_vid", "_bucket"} carrying the day NUMBER it came from (ms // DAY).
        # The day index is already DAY-granular, so a caller can split boundary days
        # (need the exact-ms re-check) from interior days (wholly inside the window)
        # with two FREE filters on _bucket -- no extra SEEK. Same cost as read_index_range:
        # 1 SEEK + 1 NEXT per day token crossed. Only entry_range_dgraph uses this;
        # nebula/arango run the exact-ms `default` plan and never call it (which is why
        # it lives here, not on the base class). _bucket is the day NUMBER (int), not the
        # 7-wide key string, so a language-neutral plan can compute the boundaries with plain
        # floordiv instead of format()-ing the key spelling.
        if self._key_sorted_walk(labels, prop, "index_range_buckets"):
            return self._key_sorted_range(store, labels, prop, lo, hi, True)
        out, n = self._day_range(store, labels, prop, lo, hi, True, "index_range_buckets")
        return out, KVCost(S=len(self._day_walk_preds(labels, prop, "index_range_buckets")), N=n)

    def _edge_read(self, store, run, anchors, todo, etype):
        # One read = the named posting list (forward for out, reverse for in) of each owner, once (1 SEEK);
        # membership checks against the blobs are free. No eprobes (no per-pair GETs), but #noedges IS paid in
        # BYTES: the blobs read carry one entry per edge.
        if any(r["dir"] == "in" for r in run) and etype not in self.schema.reverse_etypes:
            raise ValueError(f"dgraph read_edge_exists needs @reverse on '{etype}' for a read with dir in")
        kinds = ["D" if r["dir"] == "out" else "R" for r in run]
        at_anchor = run[0]["at"] == "anchor"
        owners = anchors if at_anchor else todo
        S, found, aset = 0, set(), set(anchors)
        nbrs = set()
        for o in owners:
            mine = set()
            for kind in kinds:
                postings = self._read_list(store, (etype, kind, o)) or []
                mine.update(other for other, _f in postings)
                S += 1
            nbrs |= mine
            if not at_anchor and mine & aset:
                found.add(o)
        if at_anchor:
            found = {c for c in todo if c in nbrs}
        return found, KVCost(S=S)

    # xxx STORAGE READS END xxx