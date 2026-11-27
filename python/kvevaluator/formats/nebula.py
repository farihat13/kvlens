"""Nebula format: vertex-oriented. A vertex's props live in one value keyed by (vid, tag); each directed edge is its own
key stored in both directions (forward <src, +et, rank, dst>, reverse <dst, -et, rank, src>, each with a full prop
copy), so the neighbor vid is read from the key. A whole-tag read is 1 GET; reading one prop still pays the whole row.

  (person_933, Person)                     -> {firstName: ..., email: [...], ...}
  (person_933, +knows, 0, person_1)        -> {creationDate: ...}
  (person_1, -knows, 0, person_933)        -> {creationDate: ...}   # same edge, reverse key
  (i, Person, firstName, Chau, person_933) -> None                  # index: value in the key

Edge props are in the edge value, so read_neighbors covers them (0 extra ops, but their bytes on every edge, projected
or not). The indexes are the shared design's (schema.indexed_cols + date_cols); a label with no property index gets an
empty tag index (the type-enumeration structure); index values are empty (the value is in the key, so range and limit
friendly). An undirected pattern scans both directions: 2 SEEKs per source. A message vid does not reveal Post vs
Comment and props are tag-qualified, so a read of a message of unknown type tries the candidate tags in order (a
format property). Partitions are not modelled (a deployment knob). Ingest streams with no buffering.
"""
from dataclasses import dataclass

import struct

from kvevaluator.formats.base import Format, _acc, counted, neighbor_item
from kvevaluator.cost import KVCost
from kvevaluator.schema.graph import content_bytes
from kvevaluator.kvstore import valcodec
from kvevaluator.schema.vids import key_id, without_key_id

@dataclass(frozen=True)
class NebulaSizing:
    """Tunable byte widths for nebula. 
    Data key = [type+partId][...]. 
    Real widths: vertex = prefix+vid+tagId (16B); 
    edge adds edgeType+rank+dst+placeholder; 
    index = prefix+indexId+colval(real)+vid. 
    Value leaf widths default to the neutral model (int 8, bool 1).

    `pack` -- OFF by default (neutral bytes). ON models RowWriterV2 row FRAMING (nebula compresses nothing; it frames): 
    header + null bitmap + fixed-width slots (8B per numeric, an 8B offset+length slot per string) + the string bytes + an 8B timestamp. See rowv2_bytes."""
    part_prefix: int = 1        # 1B key-type marker; real is 4 (+3B partId), set 4 if partitioned
    vid: int = 8                # INT64 space; set 30 to model FIXED_STRING(30)
    tag_id: int = 4
    edge_type: int = 4
    rank: int = 0               # real 8B edge rank/version; 0 = unversioned default
    placeholder: int = 0        # real width 1B; modeled 0 (a key-model gap)
    index_id: int = 4           # (space, tag/edge, index) folded into one 4B id
    int_bytes: int = 8          # value leaf: an int property's stored width
    bool_bytes: int = 1         # value leaf: a bool property's stored width
    pack: bool = False          # kind-1 RowWriterV2 row framing (off = neutral)
    row_timestamp: int = 0      # per-row trailing timestamp; validated on a VERTEX dump
                                # (Bob=31), NOT on an edge value -- set 0 to bracket its effect

def rowv2_bytes(props, timestamp=8):
    """Modeled RowWriterV2 size of one property row -- header(1) + null bitmap + per-column
    fixed slots + variable string bytes + a trailing `timestamp`. A numeric column is an
    8-byte fixed slot; a bool is 1 byte; a string is an 8-byte (offset+length) slot PLUS its
    bytes in the variable area. Worked example {age:35, lastname:'Smith'} -> 31 bytes
    (a VERTEX dump).

    TWO caveats, both because the evaluator lacks a per-tag column schema and this is modeled
    from the doc, not a fresh dump (the doc itself says the nebula model needs dump
    validation before it is trusted):
    - RESERVED SLOTS: absent nullable columns are not charged their reserved slot (real
      RowWriterV2 is fixed-schema -- the doc's Alice=26 case), so an under-count on sparse rows.
      Exact for fully-present rows, which LDBC's dense entities mostly are.
    - EDGE TIMESTAMP: the trailing `timestamp` is validated only against a VERTEX ValueSize.
      It is applied to EDGE rows too here, which is the dominant driver of the packed nebula
      byte total (a facet-free edge value is `{}`, so ~all of its cost is this field, doubled
      because every edge is stored both directions). It may instead be the edge version/rank,
      which lives in the KEY (NebulaSizing.rank), not the value -- unconfirmed. Pass timestamp=0
      (NebulaSizing.row_timestamp=0) to bracket how much of the packed total it accounts for."""
    n = len(props)
    total = 1 + (n + 7) // 8 + timestamp              # header + null bitmap + trailing timestamp
    for val in props.values():
        if isinstance(val, bool):
            total += 1
        elif isinstance(val, (int, float)):
            total += 8                                # int64 / double / timestamp fixed slot
        elif isinstance(val, str):
            total += 8 + len(val)                     # offset+length slot + string bytes
        elif isinstance(val, (bytes, bytearray)):
            total += 8 + len(val)                     # a bytes cell: the same slot + its raw bytes (C++ rowv2)
        elif val is None:
            total += 0
        else:
            total += content_bytes(val)               # fallback for anything unexpected
    return total

class _Tagged(str):
    """a vid with the tag a read named for it (NebulaFormat "labels on rows"); a plain str everywhere else"""
    def __new__(cls, vid, label):
        self = super().__new__(cls, vid)
        self.label = label
        return self

    def __getnewargs__(self):              # copy / pickle keep the tag
        return (str(self), self.label)

class NebulaFormat(Format):
    name = "nebula"
    CHASE_FIRST_EDGE_ONLY = True   # a chase hop: 1 SEEK landing on the first edge, no NEXT (C++ nebula chase_reads)

    def __init__(self, schema, sizing=None):
        super().__init__(schema)
        self.sz = sizing or NebulaSizing()                  # tunable key + value byte widths
        # Labels with a REQUIRED (non-nullable) property index (value or date). Such a label
        # needs NO separate membership index: real nebula's `LOOKUP ON Tag` enters through ANY
        # index on the tag, and a REQUIRED prop's index has one entry per vertex (the vid in the
        # key), so scanning it enumerates the whole type at the same 1 SEEK + M NEXT a dedicated
        # empty-tag index would cost. So the canonical `index <Label>` (membership) declaration is
        # SATISFIED by that property index -- nebula reuses it and builds no redundant structure.
        # Only labels with no required indexed prop (Forum, Organisation) build the empty-tag index.
        # REQUIRED matters: a nullable prop's index skips vertices that lack it, so it can't
        # enumerate the type -- such a label falls through to the empty-tag index. (dgraph does NOT
        # do this reuse: its per-predicate index can't isolate one type and its dgraph.type posting
        # is both mandatory and cheaper at O(1); arango folds the type into the id.)
        self._prop_indexed = {l for (l, p) in schema.indexed_cols | schema.date_cols
                              if (l, p) not in schema.nullable_cols}

    def key_bytes(self, key):
        """Nebula data key = [type+partId][...]; no spaceId in a data key. Three shapes,
        dispatched by arity + marker:
          vertex (vid, tag)                 -> prefix + vid + tagId
          edge   (src, ±et, rank, dst)      -> prefix + vid + edgeType + rank + vid + placeholder
          index  ("i", label, prop, val, vid) -> prefix + indexId + val(real) + vid
        label/prop identify the index (folded into a 4B indexId, NOT charged as strings);
        only the indexed column VALUE is content. Known limitation: a numeric index token
        (f"{val:013d}") is charged at its 13-char string length, a small over-count vs a
        real 8-byte sortable int -- a later type-aware refinement, noted in the plan."""
        s = self.sz
        n = len(key)
        if n == 2:                                          # (vid, tag)
            return s.part_prefix + s.vid + s.tag_id
        if n == 4 and str(key[1])[:1] in "+-":              # (src, ±etype, rank, dst)
            # Rank width by USE: "0" is the unversioned default (s.rank, 0B per the model's
            # convention); a non-"0" rank holds a real DATE (a date-keyed reverse
            # edges) -- that edge is VERSIONED, so its rank is a real int64 (s.int_bytes) and must
            # be charged. Pricing a dated rank at 0 would make date-keying look free.
            rank_bytes = s.int_bytes if key[2] != "0" else s.rank
            return s.part_prefix + s.vid + s.edge_type + rank_bytes + s.vid + s.placeholder
        if n == 5 and key[0] == "i":                        # (i, label, prop, val, vid)
            return s.part_prefix + s.index_id + len(str(key[3])) + s.vid
        raise ValueError(f"nebula key_bytes: unrecognized key shape {key!r}")

    def value_bytes(self, v, key=None):
        """Value bytes under nebula's model. Neutral unless sz.pack, which models RowWriterV2
        FRAMING for property ROWS -- the dict values (a vertex tag row, an edge prop row). The
        empty index values (None) stay neutral. `dict` is the row discriminator: no other
        nebula value is a dict."""
        if self.sz.pack and isinstance(v, dict):
            return rowv2_bytes(v, self.sz.row_timestamp)
        return content_bytes(v, self.sz.int_bytes, self.sz.bool_bytes)

    # --- physical value codec: RowV2, positional (on the format) ------------------------
    # Layout mirrors rowv2_bytes: header(1) + null bitmap + fixed slots in DECLARED column
    # order (int/date 8B, bool 1B, str/list an 8B offset+length slot) + the variable string
    # area + sz.row_timestamp trailing zeros. Positional -> decode pulls the column list +
    # types from the schema (§0), so there are no names in the bytes. Documented divergences
    # from the model (deliberate): the bitmap covers the DECLARED columns (the model counts
    # only PRESENT ones -- ±1 byte on rows with absent columns at a byte boundary), and a
    # multi-valued column adds count/length varints the model's bare content sum lacks.
    # Bit set in the bitmap = column ABSENT; decode rebuilds only the present keys, so an
    # explicit None value cannot round-trip -- refused at encode.

    def _row_columns(self, key):
        """The DECLARED (name, type, multi) list for the row at `key`, in schema order.
        Vertex rows hold the declared props only: `id` is key-derived, never stored."""
        if len(key) == 2:                                   # (vid, tag) vertex row
            label = key[1]
            if label not in self.schema.vertex_prop_types:
                raise ValueError(f"nebula codec: no declared columns for tag {label!r}")
            return [(p, t, (label, p) in self.schema.multival_cols)
                    for p, t in self.schema.vertex_prop_types[label]]
        if len(key) == 4 and str(key[1])[:1] in "+-":       # (src, ±etype, rank, dst)
            etype = key[1][1:]
            return [(p, t, False) for p, t in self.schema.edge_prop_types.get(etype, ())]
        raise ValueError(f"nebula codec: unrecognized row key {key!r}")

    def _rowv2_encode(self, cols, props):
        bitmap = bytearray((len(cols) + 7) // 8)
        fixed, var = bytearray(), bytearray()
        seen = 0
        for idx, (name, typ, multi) in enumerate(cols):
            if name not in props:
                bitmap[idx // 8] |= 1 << (idx % 8)          # bit set = absent
                continue
            val = props[name]
            seen += 1
            if val is None:
                raise ValueError(f"nebula codec: explicit None for column {name!r} "
                                 f"(absent-vs-None is not representable in RowV2)")
            if multi:
                if not isinstance(val, list) or not all(isinstance(e, str) for e in val):
                    raise ValueError(f"nebula codec: {name!r} declared multi (list of str), "
                                     f"got {val!r}")
                blob = valcodec.uvarint(len(val))
                for e in val:
                    u = e.encode("utf-8")
                    blob += valcodec.uvarint(len(u)) + u
                fixed += struct.pack(">II", len(var), len(blob))
                var += blob
            elif typ in ("str", "bytes"):                   # bytes: stored at its raw length, like the C++ rowv2
                want = str if typ == "str" else bytes
                if not isinstance(val, want):
                    raise ValueError(f"nebula codec: {name!r} declared {typ}, got {type(val).__name__}")
                u = val.encode("utf-8") if typ == "str" else val
                fixed += struct.pack(">II", len(var), len(u))
                var += u
            else:                                           # int / date
                if (isinstance(val, bool) or not isinstance(val, int)
                        or not -(1 << 63) <= val < (1 << 63)):
                    raise ValueError(f"nebula codec: {name!r} declared {typ}, got {val!r}")
                fixed += struct.pack(">q", val)
        if seen != len(props):
            extra = set(props) - {c[0] for c in cols}
            raise ValueError(f"nebula codec: undeclared columns {sorted(extra)}")
        return b"\x00" + bytes(bitmap) + bytes(fixed) + bytes(var) + b"\x00" * self.sz.row_timestamp

    def _rowv2_decode(self, cols, raw):
        nb = (len(cols) + 7) // 8
        bitmap = raw[1:1 + nb]
        i = 1 + nb
        slots, out = [], {}
        for idx, (name, typ, multi) in enumerate(cols):
            if bitmap[idx // 8] >> (idx % 8) & 1:
                continue                                    # absent
            if multi or typ in ("str", "bytes"):
                off, ln = struct.unpack_from(">II", raw, i)
                slots.append((name, multi, typ, off, ln))
                out[name] = None                            # placeholder, filled from var area
                i += 8
            else:
                out[name] = struct.unpack_from(">q", raw, i)[0]
                i += 8
        var0 = i
        for name, multi, typ, off, ln in slots:
            blob = raw[var0 + off:var0 + off + ln]
            if not multi:
                out[name] = bytes(blob) if typ == "bytes" else blob.decode("utf-8")
            else:
                n, j = valcodec.read_uvarint(blob, 0)
                elems = []
                for _ in range(n):
                    el, j = valcodec.read_uvarint(blob, j)
                    elems.append(blob[j:j + el].decode("utf-8"))
                    j += el
                out[name] = elems
        return out

    def encode_value(self, key, value):
        if value is None:
            return b""
        if len(key) == 5 and key[0] == "i":
            raise ValueError(f"nebula codec: index value must be None, got {value!r}")
        if not isinstance(value, dict):
            raise ValueError(f"nebula codec: only dict rows are stored, got {type(value).__name__}")
        return self._rowv2_encode(self._row_columns(key), value)

    def decode_value(self, key, raw):
        if not raw:
            return None
        return self._rowv2_decode(self._row_columns(key), raw)

    # ----------------------
    # --- STORAGE WRITES ---
    # ----------------------

    # --- labels on rows (the C++ format's Value::label, exec/label_tags.h), mirrored ------------------------------
    # C++ carries a vertex's tag on the row once a READ has named it: the tag that held a probe's hit, a per-tag
    # index or label scan, and a hop whose far end the schema's edge declaration gives ONE label (our schema's
    # `between`; real NebulaGraph's CREATE EDGE names no end tags, so this one is what the query's author knows, as
    # an nGQL pattern names the tag). Never the plan's own label on a by-id read: nothing was read to back it. A
    # later read of a step whose input carries the vid's tag tries that tag only; a vid that reaches a read only
    # through an open-ended hop still pays its misses. Here the vid such a read emits is a _Tagged str: it equals and
    # hashes as the plain vid, and the plan operators pass it along by reference, as the C++ ones pass the Value.
    # Not followed by a derived format's own props read. The engine twin turns it off (_ROW_LABELS).
    _ROW_LABELS = True
    _wrote = False                 # a vertex write ran in this plan: a tag no longer says the vertex is there

    def begin_plan(self):
        self._wrote = False

    def insert_vertex(self, store, vid, label, props):
        self._wrote = True                 # set here, not in _w_*: the subclasses override those
        return super().insert_vertex(store, vid, label, props)

    def delete_vertex(self, store, vid, label, with_edges=True):
        self._wrote = True
        return super().delete_vertex(store, vid, label, with_edges)

    def _note(self, v, tag):
        """`v` as a read that named its tag emits it"""
        return _Tagged(v, tag) if self._ROW_LABELS else v

    @staticmethod
    def _first_tags(vids):
        """{vid: the tag its first TAGGED occurrence in one step's input carries} (label_tags.h first_tags)"""
        tags = {}
        for v in vids:
            k = getattr(v, "label", None)
            if k is not None:
                tags.setdefault(v, k)
        return tags

    def _far_label(self, etype, sign):
        """the ONE label the schema gives the far end of `etype` in this direction, else None (an open far end)"""
        far = {(d if sign == "+" else s) for s, d in self.schema.edge_endpoints.get(etype, ())}
        return next(iter(far)) if len(far) == 1 else None

    def _v(self, vid):
        """Identity now: the encoded vid already carries its GROUP's tag (Post & Comment
        share the ONE Message tag -- real nebula's single msg_ vid space, so an
        unknown-type message anchor scans ONE prefix; the type lives only in the tag key,
        recovered from storage). Grouping happens at construction (vids.vid via group_of),
        so no per-format translation remains. Kept as a seam in case a format ever needs
        to re-map a vid. A _Tagged vid goes out plain: every write path names its keys through here, so a tag
        never reaches the store."""
        return str(vid) if type(vid) is _Tagged else vid

    def add_vertex(self, store, vid, label, props):
        store.put((self._v(vid), label), without_key_id(props))   # <vid, tag> -> the props (id is the key)
        # The empty tag index (type-enumeration) is materialized ONLY when the label has no
        # required property index to reuse (_prop_indexed above). A label WITH one (Person, Post,
        # ...) enumerates through that index instead -- no redundant structure, even when the
        # schema declares a per-type membership index (canonical does for every label). Its keys
        # ("i", label, "", "", vid) sort before every value-index key ("i", label, prop, ...), so
        # read_vertices_by_label reads exactly them.
        if label not in self._prop_indexed:
            store.put(("i", label, "", "", self._v(vid)), None)   # empty tag index
        for p, val in props.items():
            if self.schema.is_indexed(label, p):
                store.put(("i", label, p, str(val), self._v(vid)), None)  # value in the key
            elif (label, p) in self.schema.date_cols:
                # sorted date index; fixed-width int so key order == date order
                store.put(("i", label, p, f"{val:013d}", self._v(vid)), None)

    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        # Real nebula writes every directed edge under BOTH key orientations.
        src, dst = self._v(src), self._v(dst)
        store.put((src, "+" + etype, "0", dst), dict(props or {}))
        store.put((dst, "-" + etype, "0", src), dict(props or {}))

    # --- WRITE OPERATORS (incremental; see base.Format) --------------------------------
    #
    # The row-store shape: a vertex is ONE key plus one entry per index the LABEL declares, and an
    # edge is two keys. So write cost tracks the SCHEMA, not the payload -- an insert costs the same
    # whether it carries 2 properties or 9 (mb2's MB83 == MB84 == 4 PUT). The price of that
    # condensed row is paid on UPDATE instead: one property cannot be written alone, so the whole
    # row is read back and rewritten, plus the touched column's index entry when it is indexed.
    # (Real nebula re-maintains every index of the label on any update -- mb2: MB82 == MB81 -- an
    # engine choice that nebula_engine models, not a cost of the layout.)

    def _index_key(self, label, prop, val, v):
        """One index key, encoded exactly as add_vertex encodes it -- value index (raw string) or
        sorted date index (zero-padded so key order == date order). A column with no value yet
        still gets an entry: a row-store index holds one entry per ROW per index, which is what
        keeps the insert cost flat in property count."""
        if (label, prop) in self.schema.date_cols and not self.schema.is_indexed(label, prop):
            return ("i", label, prop, f"{val:013d}" if val is not None else "", v)
        return ("i", label, prop, str(val) if val is not None else "", v)

    def _vertex_index_pairs(self, label, props, v):
        pairs = []
        if label not in self._prop_indexed:
            pairs.append((("i", label, "", "", v), None))    # empty tag index (type enumeration)
        for prop in self.schema.index_cols_of(label):
            pairs.append((self._index_key(label, prop, props.get(prop), v), None))
        return pairs

    def _n_index_keys(self, label):
        """FORMULA side of the oracle: how many index keys a row of `label` owns. Counted from the
        schema, never from the emitted list, so a bug in either shows up as predicted != actual."""
        return len(self.schema.index_cols_of(label)) + (0 if label in self._prop_indexed else 1)

    def _w_insert_vertex(self, store, vid, label, props):
        v = self._v(vid)
        puts = [((v, label), without_key_id(props))] + self._vertex_index_pairs(label, props, v)
        return puts, [], KVCost(P=1 + self._n_index_keys(label))

    def _w_insert_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        src, dst = self._v(src), self._v(dst)
        # Both orientations, each carrying a full copy of the edge props -- the same thing add_edge
        # writes. Two keys per edge whatever the endpoints' degree: an edge insert never touches a
        # neighbour's data, which is the split-adjacency layout's write-side advantage.
        puts = [((src, "+" + etype, "0", dst), dict(props)),
                ((dst, "-" + etype, "0", src), dict(props))]
        return puts, [], KVCost(P=2)

    def _w_update_props(self, store, vid, label, props):
        v = self._v(vid)
        row = store.get((v, label)) or {}                    # READ-MODIFY-WRITE: 1 GET, charged as a read
        new = {**row, **props}                               # every changed column, ONE row write
        puts = [((v, label), new)]
        dels = []
        # Only the changed columns' index entries move (real nebula rewrites them all -- mb2 MB82 ==
        # MB81 -- an engine choice nebula_engine keeps; see _update_index_cols).
        cols = self._update_index_cols(label, props)
        for p in cols:
            old_key = self._index_key(label, p, row.get(p), v)
            new_key = self._index_key(label, p, new.get(p), v)
            dels.append(old_key)
            puts.append((new_key, None))
        return puts, dels, KVCost(G=1, P=1 + len(cols), D=len(cols))

    def _w_update_edge_prop(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        src, dst = self._v(src), self._v(dst)
        fk, rk = (src, "+" + etype, "0", dst), (dst, "-" + etype, "0", src)
        # The edge value is one row per orientation, so the untouched props must be read back
        # first: 1 GET of the forward copy (the reverse copy holds the same value), then both
        # copies PUT whole. An absent edge: the GET misses and nothing is written.
        old = store.get(fk)
        if old is None:
            return [], [], KVCost(G=1)
        new = {**old, **props}
        return [(fk, new), (rk, dict(new))], [], KVCost(G=1, P=2)

    def _w_delete_vertex(self, store, vid, label, with_edges=True):
        v = self._v(vid)
        row = store.get((v, label)) or {}                    # need the values to name the index keys
        dels = [(v, label)] + [k for k, _ in self._vertex_index_pairs(label, row, v)]
        pred = KVCost(G=1, D=1 + self._n_index_keys(label))
        if with_edges:
            for etype, dir in self.schema.incident_etypes(label):
                _p2, d2, c2 = self._w_delete_incident(store, vid, label, etype, dir)
                dels += d2
                _acc(pred, c2)
        return [], dels, pred

    def _w_delete_incident(self, store, vid, label, etype, dir):
        """The edge cleanup a vertex delete owes ONE incident (etype, dir): both orientations of
        every edge found, addressed directly. Split out of _w_delete_vertex: one piece per incident
        (etype, dir)."""
        v = self._v(vid)
        nbrs, cost = self.read_neighbors(store, [vid], etype, dir)
        sign = "+" if dir == "out" else "-"
        dels = []
        for nbr in nbrs:
            dels.append((v, sign + etype, "0", self._v(nbr)))                     # our side
            dels.append((self._v(nbr), ("-" if sign == "+" else "+") + etype, "0", v))
            cost.D += 2
        return [], dels, cost

    def _w_delete_edge(self, store, etype, src, dst, src_label=None, dst_label=None):
        src, dst = self._v(src), self._v(dst)
        # Both orientations, addressed directly -- no search: the edge key IS <src, etype, rank, dst>,
        # so nebula knows where it lives without reading anything. (Contrast arango, whose edge doc
        # id is not derivable from the endpoints, so it must be looked up first.)
        return [], [(src, "+" + etype, "0", dst), (dst, "-" + etype, "0", src)], KVCost(D=2)

    # xxx STORAGE WRITES END xxx

    # ---------------------
    # --- STORAGE READS ---
    # ---------------------

    @counted
    def read_vertex_by_id(self, store, vid, label=None):
        if isinstance(vid, str):
            # vid is the key. The plan's label is not carried on: nothing was read to back it (see _note's sources)
            return [self._v(vid)], None
        # Candidate message anchor: the vids collapse to ONE msg_ vid, but the TAG is
        # still unknown, so try each candidate TAG on that vid until one exists -- real
        # GETs, misses included (nGQL prop access is tag-qualified). Loop the tags for the
        # single vid (NOT zip(vids, tags) -- with unified msg_ there is one vid, many tags).
        labels = (label,) if isinstance(label, str) else tuple(label)
        for cand in vid:
            v = self._v(cand)
            for tag in labels:
                if store.get((v, tag)) is not None:
                    return [self._note(v, tag)], None
        return [], None

    @counted
    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False, first_only=False):
        # One prefix scan per direction: SEEK <src, ±et>, one NEXT per edge, neighbor
        # vid read from the KEY (key-only, byte-cheap). The undirected LDBC pattern
        # (dir="both") pays 2 SEEKs per source -- verified vs real /kvstats.
        # srcs read as a SET (base docstring); candidate message vids collapse to
        # one msg_ vid (see _v) and are scanned -- and emitted -- once.
        if etype not in self.schema.etypes:
            raise ValueError(f"nebula read_neighbors: unknown etype {etype!r}")
        signs = {"out": ("+",), "in": ("-",), "both": ("+", "-")}[dir]
        tags = self._first_tags(srcs)
        distinct = [self._note(s, tags[s]) if s in tags else s           # a src keeps its tag on the pair rows
                    for s in dict.fromkeys(self._v(s) for s in srcs)]
        out = []
        for s in distinct:
            for sign in signs:
                prefix = (str(s), sign + etype)
                far = self._far_label(etype, sign)           # one declared far label: the neighbours carry it
                cur = store.seek(prefix)                     # 1 SEEK per direction
                while cur.valid() and cur.key()[:2] == prefix:
                    other = cur.key()[3]                     # neighbor vid from the KEY
                    # DECISION: charge the edge VALUE bytes on every edge, even when the
                    # query doesn't project them. The value is inline with the key in the same
                    # RocksDB block, so a scan cannot step past it without reading it (it
                    # rides up in the block, and iter.bytes.read counts key+value per
                    # position). Not projecting only saves the copy to the caller's buffer,
                    # not the read. So value() runs unconditionally; val is nulled after
                    # when unprojected so the returned row is unchanged. Contrast arango,
                    # whose edge doc is a SEPARATE key -> correctly untouched on this walk.
                    val = cur.value()
                    if not edge_props:
                        val = None

                    if far is not None:
                        other = self._note(other, far)
                    out.append(neighbor_item(s, other, val, edge_props, pairs))
                    if first_only:                           # the chase: no NEXT past the first edge
                        break
                    cur.next()                               # 1 NEXT per edge
                else:
                    cur.mark_miss()                          # the landing that ended the scan: an absence miss
        return out, None

    @counted
    def read_vertex_props(self, store, vids, props, label=None):
        # 1 GET per (vid, tag) -- the whole tag row. When the logical type is a
        # Message, `label` is the candidate list ["Post", "Comment"]: tags are tried
        # in order, so a comment costs 2 GETs (1 not-found + 1 found). Misses are
        # real GETs the engine performs (nGQL prop access is tag-qualified).
        # `id` is KEY-DERIVED: the nebula vid IS the external id (person_933,
        # msg_42), so projecting it costs nothing -- real queries return id(v).
        # A pure-id projection therefore reads NOTHING. Distinct vids read once.
        labels = (label,) if isinstance(label, str) else tuple(label or ())
        if not labels:
            raise ValueError("nebula read_vertex_props needs label (a tag name or candidate list)")
        row_props = [p for p in props if p != "id"]
        # What a MISS costs, and where that cost belongs. The LAYOUT forces the probe: a
        # message vid does not reveal Post-vs-Comment and nGQL prop access is tag-qualified,
        # so reading an unknown-type message may look under the wrong tag first. That failed
        # lookup is 1 GET, charged below -- a real format property.
        # What the layout does NOT dictate is what happens NEXT. Real NebulaGraph storaged
        # follows a miss with a PREFIX SCAN of the vid to find which tags exist (measured
        # against live storaged). That is an ENGINE implementation decision,
        # not a consequence of the key shape -- a minimal engine over this same layout would
        # stop at the failed GET. So it lives in formats/nebula_engine.py, not here:
        # kvformat prices the cheapest plan the LAYOUT permits, and charging the optimal
        # path for an engine's fallback would fold engine waste into the very optimum the
        # engines are meant to be a ceiling over.
        cache = {}
        tags = self._first_tags(vids)                        # each distinct vid as the input carries it
        for v in dict.fromkeys(self._v(x) for x in vids):
            known = tags.get(v)
            row = {"_vid": self._note(v, known) if known else v}
            if "id" in props:
                row["id"] = key_id(v)
            if row_props:
                blob = None
                # a carried tag: that tag only (none when the plan does not ask for it); else every tag in order
                for tag in (labels if known is None else (known,) if known in labels else ()):
                    blob = store.get((v, tag))               # 1 GET (found or not-found)
                    if blob is not None:
                        row["_vid"] = self._note(v, tag)     # the row carries the tag on
                        break
                for p in row_props:
                    row[p] = (blob or {}).get(p)
            cache[v] = row
        return ([dict(cache[self._v(x)]) for x in vids],
                None)

    def read_anchor_props(self, store, cands, props, labels=None):
        # The anchor try IS the read: a found (vid, tag) GET returns the whole row, so
        # resolving + reading = 1-2 GETs total (misses paid, no re-read). Loop the candidate
        # TAGS on the single unified msg_ vid (NOT zip(vids, tags) -- one vid, many tags).
        labels = (labels,) if isinstance(labels, str) else tuple(labels or ())
        if not labels:
            raise ValueError("nebula read_anchor_props needs labels (a tag name or candidate list)")
        G = 0
        for cand in cands:
            v = self._v(cand)
            for tag in labels:
                G += 1
                blob = store.get((v, tag))
                if blob is not None:
                    return [{"_vid": self._note(v, tag), **{p: key_id(v) if p == "id" else blob.get(p) for p in props}}], KVCost(G=G)   # id: the key
        return [], KVCost(G=G)

    @counted
    def read_index_eq(self, store, label, prop, value):
        # Value-sorted native index: prefix SEEK + one NEXT per match, vid read
        # from the KEY (0 GETs). R = 1 partition (see the module docstring).
        # A sorted (date) column's entries are written zero-padded (add_vertex), so the probe must
        # use the same token -- str(value) would never match. Value indexes are keyed by str(value).
        tok = f"{value:013d}" if (label, prop) in self.schema.date_cols and not self.schema.is_indexed(label, prop) else str(value)
        prefix = ("i", label, prop, tok)
        vids = []
        cur = store.seek(prefix)
        while cur.valid() and cur.key()[:4] == prefix:
            vids.append(self._note(cur.key()[4], label))     # a tag's index holds that tag's vertices
            cur.next()
        cur.mark_miss()                            # the landing that ended the scan: an absence miss
        return vids, None

    @counted
    def read_index_range(self, store, labels, prop, lo, hi):
        # Per-tag value-sorted date index: seek lo, 1 NEXT per entry until >= hi;
        # vid read from the KEY (0 GETs). Fixed-width (013d) keys make key order ==
        # ms order. Scans each label's index (Post+Comment for messages) and unions.
        lo_s, hi_s = f"{lo:013d}", f"{hi:013d}"
        out = []
        for label in labels:
            cur = store.seek(("i", label, prop, lo_s))
            while (cur.valid() and cur.key()[:3] == ("i", label, prop)
                   and cur.key()[3] < hi_s):
                out.append(self._note(cur.key()[4], label))
                cur.next()
            cur.mark_miss()                        # the landing past the window: an absence miss
        return out, None

    @counted
    def read_vertices_by_label(self, store, label):
        # Type-enumeration entry. TWO structures can serve it, with the SAME cost
        # shape (1 SEEK + 1 NEXT per vertex, vid in the KEY, 0 GETs -- the same
        # walk as read_index_eq):
        #   1. a REQUIRED PROPERTY index on the label -- real nebula's LOOKUP ON <tag>
        #      accepts ANY index on the tag as its entry point (mb2 NS2: Person
        #      enumerates via person_creation_date_idx), and every entry carries the
        #      vid in its key. Used for every prop-indexed label (Person/Post/...);
        #   2. else the EMPTY tag index ("i", label, "", "", vid) -- built only for a
        #      label with no required property index (Forum, Organisation; every label
        #      on the label_only store, which has no property indexes).
        # These MIRROR add_vertex, which builds the empty-tag index under the exact same
        # `label not in _prop_indexed` condition -- so this always seeks a structure that exists.
        # The property chosen is the alphabetically first REQUIRED (non-nullable) index on the
        # label -- deterministic, and cost-identical to any other choice (one index entry per
        # vertex; vid in the key). REQUIRED matters for correctness: a nullable indexed prop has
        # no index entry for vertices that lack it, so enumerating through it would under-count the
        # type -- which is why such a label is NOT in _prop_indexed and takes the empty-tag branch.
        if label in self._prop_indexed:
            props = sorted(p for l, p in (self.schema.indexed_cols | self.schema.date_cols)
                           if l == label and (label, p) not in self.schema.nullable_cols)
            prefix = ("i", label, props[0])
        else:
            prefix = ("i", label, "", "")
        klen = len(prefix)
        vids = []
        cur = store.seek(prefix)
        while cur.valid() and cur.key()[:klen] == prefix:
            vids.append(self._note(cur.key()[4], label))
            cur.next()
        cur.mark_miss()                            # the landing that ended the scan: an absence miss
        return vids, None

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """both directions of every edge type are stored, each edge its own key: in reads valid, point GETs (no lists)"""
        return {"reverse_adjacency"}

    @classmethod
    def capabilities(cls, schema):
        # Per-vertex type index: enumerating a type is a range scan (never
        # type_index_single_posting), so a type FILTER lowers to the per-candidate probe.
        return {"sorted_range_index", "per_type_index", "type_index", "whole_row_read", "label_scoped_props"}

    def read_filter_by_label_index(self, store, vids, label):
        # Enter by the type index and intersect. Unlike dgraph's single posting, nebula's is
        # per-vertex, so this is a RANGE SCAN via read_vertices_by_label: 1 SEEK + M NEXT
        # (M = |type|). Which of the two ops is cheaper is a NEXT-weight question: the scan
        # beats the probe (N GET) under S+G and loses under S+G+N when M >> N -- the study
        # experiments/type_index/ reports both currencies. The resolver never lowers to this op
        # for nebula (no type_index_single_posting); a plan may name it explicitly.
        if not vids:
            return [], KVCost()
        # every candidate's tag is noted: decided by them, the index is not walked (one unknown: the whole walk)
        tags = {} if self._wrote else self._first_tags(vids)
        if tags and all(v in tags for v in vids):
            return [self._note(self._v(v), label) for v in vids if tags[v] == label], KVCost()
        members, cost = self.read_vertices_by_label(store, label)   # 1 SEEK + M NEXT
        keep = set(members)
        return [self._note(self._v(v), label) for v in vids if self._v(v) in keep], cost

    @counted
    def read_filter_by_label_probe(self, store, vids, label):
        # The per-candidate probe: one existence GET on the (vid, tag) key per DISTINCT vid -> N GET; a repeated vid
        # is kept at every occurrence.
        if not isinstance(label, str):
            raise ValueError(f"nebula read_filter_by_label_probe needs one label, got {label!r} "
                             f"(list-label filtering is unimplemented)")
        if not vids:
            return [], KVCost()
        keep = {}
        tags = {} if self._wrote else self._first_tags(vids)
        for v in dict.fromkeys(vids):
            k = tags.get(v)
            if k is not None:                                # a noted tag decides it: 0 GETs
                keep[v] = k == label
                continue
            keep[v] = store.get((self._v(v), label)) is not None
        out = [self._note(self._v(v), label) for v in vids if keep[v]]
        return out, None

    def read_top_k_by_index(self, store, vids, prop, k, order="desc", hi=None,
                       labels=None, props=None, id_order="asc"):
        """Ranked top-k via the per-tag SORTED date indexes (value in the KEY):
        walk each candidate tag's index newest-first, merging the cursors in
        date order; a hit = the entry's vid (also in the key) is in the
        candidate set. Ranking reads NOTHING -- date and id are both
        key-derived -- so the walk costs 1 SEEK per tag + 1 NEXT per entry
        crossed; winner rows are then read once each (+1 GET, and the matching
        index names the tag, so no try-each-tag miss). The `hi` (exclusive ms)
        bound is free: seek_for_prev on the 4-tuple ("i", tag, prop, hi) lands
        on the last entry strictly BELOW it (an entry AT hi extends the tuple
        and sorts after). After the k-th hit the walk drains remaining
        same-date entries so the (date desc, id) tie-break cut is complete."""
        assert order == "desc", "the LDBC shape; asc would walk forward"
        labels = (labels,) if isinstance(labels, str) else tuple(labels)
        # vids=None = NO candidate restriction (a GLOBAL top-k): the per-tag index
        # IS the type restriction, so every entry crossed is a hit -- real nebula's
        # LOOKUP ON <tag> ... ORDER BY | LIMIT with limit pushdown (mb2 NS7
        # patched: RS + RK N + RK G). dgraph has no analogue (its date index is
        # per-PREDICATE, all types mixed) -- see the microbench topk override.
        cand = None if vids is None else set(self._v(v) for v in vids)
        bound = "~" if hi is None else f"{hi:013d}"      # "~" sorts after digits
        cursors, S, N = [], 0, 0
        for tag in labels:
            cursors.append((tag, store.seek_for_prev(("i", tag, prop, bound))))
            S += 1

        def current(tc):                                  # key if still in this index
            tag, cur = tc
            if cur.valid() and cur.key()[:3] == ("i", tag, prop):
                return cur.key()                          # ("i", tag, prop, ms, vid)
            return None

        hits = []
        while True:
            best = None
            for tc in cursors:
                key = current(tc)
                if key is not None and (best is None or key[3] > best[1][3]):
                    best = (tc, key)
            if best is None:
                break
            if len(hits) >= k and best[1][3] != hits[-1][prop + "_s"]:
                break                                     # past the k-th date: complete
            (tag, cur), key = best
            vid = key[4]
            if cand is None or vid in cand:
                hits.append({"_vid": self._note(vid, tag), prop: int(key[3]), prop + "_s": key[3],
                             "id": key_id(vid), "_tag": tag})
            cur.prev()                                    # 1 NEXT per entry crossed
            N += 1
        for _, cur in cursors:                            # every cursor rests on an unconsumed entry
            cur.mark_miss()                               # (or off its index): an absence miss
        hits.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        hits.sort(key=lambda r: r[prop], reverse=True)
        rows, G = hits[:k], 0
        for r in rows:
            del r[prop + "_s"]
            tag = r.pop("_tag")
            if props:
                blob = store.get((r["_vid"], tag)) or {}  # 1 GET, tag known: no miss
                G += 1
                for p in props:
                    r[p] = key_id(r["_vid"]) if p == "id" else blob.get(p)   # id: the key
        return rows, KVCost(S=S, G=G, N=N)

    def _edge_read(self, store, run, anchors, todo, etype):
        # Point-GET the exact edge key of the read's family per (candidate, anchor) pair until a hit -- a
        # FETCH-by-edge-key, not a prefix scan. Each candidate once per read; misses are real GETs. A dated
        # etype's in key carries the date: the probe's date-0 key never hits there (as the C++ side).
        anchors = [self._v(a) for a in anchors]
        G, found = 0, set()
        for r in run:
            sign = "+" if r["dir"] == "out" else "-"
            for c in todo:
                b = self._v(c)
                for a in anchors:
                    owner, other = (a, b) if r["at"] == "anchor" else (b, a)
                    G += 1
                    if store.get((owner, sign + etype, "0", other)) is not None:
                        found.add(c)
                        break
        return found, KVCost(G=G)

    # xxx STORAGE READS END xxx