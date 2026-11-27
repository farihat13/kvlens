"""Arango format: documents, with separate column families. A document is keyed <collection, docid>; the primary index
maps (collection, _key) -> <collection, docid>; each edge collection has from / to edge indexes.

  (Person, d1)                  -> {_key: person_933, firstName: ..., ...}
  (pk, Person, person_933)      -> (Person, d1)
  (knows, e1)                   -> {_from: "Person/person_933", _to: "Person/person_1", creationDate: ...}
  (f, knows, person_933, e1)    -> "Person/person_1"   # from-edge index: covering
  (t, knows, person_1, e1)      -> "Person/person_933" # to-edge index: covering
  (i, Person, firstName, Chau, d1) -> None             # persistent index: value in key

The edge index is covering (its value is the other vertex's _id; ArangoDB v3.12.8, RocksDBValue::EdgeIndexValue), so a
pure-id expansion reads index entries only. Edge props need the edge doc, a direct GET by the docId in the index key.
The pk index is per collection: a traversal learns the collection from the edge's _from / _to for free (the `_coll`
map stands in for it, since vids no longer carry the type); a bare-id anchor has no edge, so it probes each candidate
collection. The persistent indexes are the shared design's (schema.indexed_cols + date_cols); a collection is its own
key range, so type enumeration needs no extra structure.

Op costs: read_vertex_by_id = 1 GET (pk); read_neighbors = SEEK + NEXT per edge, + 1 GET per edge only for edge props;
read_vertex_props = 2 GET per vid (pk + doc). Ingest streams with no buffering.
"""
from dataclasses import dataclass

from kvevaluator.formats.base import Format, counted, neighbor_item
from kvevaluator.cost import KVCost
from kvevaluator.formats.props_codec import props_codec
from kvevaluator.schema.graph import content_bytes
from kvevaluator.kvstore import valcodec
from kvevaluator.schema.vids import key_id, without_key_id

@dataclass(frozen=True)
class ArangoSizing:
    """Tunable byte widths for arango (key framing + value leaf widths). RocksDB key = [objectId][...]: document =
    objectId + docid; primary index = objectId + _key (real length); persistent / edge index = objectId + column value or
    vid (real length) + docid. Value leaf widths default to the neutral model (int 8, bool 1).
    `pack` (off by default) models VelocyPack: a head byte per value, field names in every document (only the five system
    fields get 1-byte codes), a frame and index table per object; it grows narrow documents. See vpack_bytes.
    """
    object_id: int = 8          # per-collection / per-index 8B prefix
    doc_id: int = 8             # LocalDocumentId, fixed uint64
    int_bytes: int = 8          # value leaf: an int property's stored width
    bool_bytes: int = 1         # value leaf: a bool property's stored width
    pack: bool = False          # kind-1 VelocyPack document framing (off = neutral)

# VelocyPack (VPack) size model. Self-describing, so a
# document's byte size is computable from the document alone (no external schema).
_VPACK_SYS = frozenset({"_key", "_rev", "_id", "_from", "_to"})   # 1-byte dictionary codes

def _int_byte_width(x):
    """Little-endian payload bytes for a signed int outside the small-int range (1..8)."""
    n = 1
    while x < -(1 << (8 * n - 1)) or x >= (1 << (8 * n - 1)):
        n += 1
    return n

def _vpack_string(s):
    """A VPack string: 1-byte head + bytes for len <= 126; else head + 8-byte length + bytes."""
    n = len(s)
    return (1 + n) if n <= 126 else (1 + 8 + n)

def _bytes_wcode(n):
    """the smallest length width (1/2/4/8 B) that holds n, as an object's frame does"""
    return next(k for k, w in enumerate(_OBJ_W) if w == 8 or n < 1 << (8 * w))

def _vpack_value(x):
    """Bytes for one VPack value (head byte + payload). Small ints ride in the head byte."""
    if x is None:
        return 1                                    # null: head only
    if isinstance(x, bool):
        return 1                                    # true/false: head only
    if isinstance(x, int):
        return 1 if -6 <= x <= 9 else 1 + _int_byte_width(x)
    if isinstance(x, float):
        return 1 + 8                                # double: head + 8
    if isinstance(x, str):
        return _vpack_string(x)
    if isinstance(x, (bytes, bytearray)):
        return 1 + _OBJ_W[_bytes_wcode(len(x))] + len(x)
    if isinstance(x, dict):
        return vpack_bytes(x)                        # nested object
    if isinstance(x, (list, tuple)):                 # array: frame + elements (rare in LDBC docs)
        body = sum(_vpack_value(e) for e in x)
        return 1 + 1 + len(x) + body
    return 1

def _cell_bytes(x):
    """One present RowV2 cell (mirrors cpp/kvevaluator/src/codec/rowv2.h): an 8-byte slot, + a string's bytes, + a
    list's uvarint count and per element uvarint length + bytes (multi columns: LDBC email / speaks)."""
    if isinstance(x, str):
        return 8 + len(x.encode("utf-8"))            # UTF-8 bytes, as stored (not characters)
    if isinstance(x, (bytes, bytearray)):
        return 8 + len(x)
    if isinstance(x, (list, tuple)):
        els = [str(e).encode("utf-8") for e in x]
        return 8 + len(valcodec.uvarint(len(els))) + sum(len(valcodec.uvarint(len(e))) + len(e) for e in els)
    return 8

def _doc_row_bytes(props, ncols):
    """A schema document's RowV2 row, after its system-member object: header 1 + an absent bitmap over the declared
    columns + one cell per present column. No row at all when nothing is declared (a prop-less etype)."""
    if not ncols:
        return 0
    return 1 + (ncols + 7) // 8 + sum(_cell_bytes(x) for x in props.values() if x is not None)

def vpack_bytes(doc):
    """Modeled VelocyPack size of one document dict. Each member = its key (a 1-byte
    dictionary code for a system field, else a full VPack string) + its VPack value. The
    object is wrapped: type(1) + byteLength + index-table(one entry per member) + nrItems,
    whose widths step up with total size (<256: 1B, <64KB: 2B, <4GB: 4B). Worked example
    {"length":3,"content":"yes"} -> 25 bytes."""
    body = 0
    for k, val in doc.items():
        body += 1 if k in _VPACK_SYS else _vpack_string(k)
        body += _vpack_value(val)
    n = len(doc)
    for w, thresh in ((1, 256), (2, 65536), (4, 4294967296), (8, float("inf"))):
        frame = 1 + w + n * w + w                    # type + byteLength + index table + nrItems
        total = body + frame
        if total < thresh:
            return total
    return total

# --- physical VPack codec: the real bytes of the structure vpack_bytes prices ----------
# Self-describing (member names ride in the bytes), with our own 1-byte head registry --
# the COST structure mirrors vpack_bytes member for member, the head byte VALUES are ours,
# not real VelocyPack's. A deliberate divergence: a string that IS a vid (16-hex) or a
# collection-qualified id ("Label/16-hex") is stored as a packed int (1+8, or
# 1+len(label)+1+8) instead of the engine's full string -- reversible (the rendering is
# canonical), and why val_phybytes < model_value on arango docs BY DESIGN.
_VP_NULL, _VP_FALSE, _VP_TRUE, _VP_DOUBLE = 0x00, 0x01, 0x02, 0x03
_VP_VID, _VP_REF = 0x04, 0x05              # packed vid / packed "Label/vid" string
_VP_SMALL = 0x10                           # + (x+6) in the low nibble: ints -6..9, head-only
_VP_INT = 0x20                             # + width 1..8: signed little-endian payload
_VP_ARRAY = 0x30                           # + count(1) + per-element 1B length + bodies
_VP_OBJ = 0x40                             # + wcode 0..3 (w = 1/2/4/8): the object frame
_VP_BYTES = 0x50                           # + wcode 0..3: a w-byte big-endian length + the raw bytes (a bytes value)
_VP_STR = 0x80                             # | len 0..126: utf-8 bytes follow
_VP_STRLONG = 0xFF                         # + 8B length + utf-8 bytes
_OBJ_W = (1, 2, 4, 8)
# 0x06-0x0A: BELOW every other head, so a long member key's string head (0x80|len,
# len 112-116 lands on 0xF0-0xF4) can never be mistaken for a system-field code
# (syscodes at 0xF0 would collide).
_VPACK_SYSCODE = {n: 0x06 + i for i, n in enumerate(("_key", "_rev", "_id", "_from", "_to"))}
_VPACK_SYSNAME = {c: n for n, c in _VPACK_SYSCODE.items()}

_HEX = frozenset("0123456789abcdef")

def _canonical_vid(s):
    """EXACTLY the 16-lowercase-hex rendering vids.vid() emits. Stricter than uid_of on
    purpose: int(s, 16) also accepts '0x..', '+', '_', whitespace and UPPERCASE, all of
    which would re-render to a DIFFERENT string -- the packed representation must be
    injective."""
    return len(s) == 16 and all(c in _HEX for c in s)

def _vp_encode_str(s):
    """Encode a string value. If it is exactly a canonical vid or ref rendering, store the
    packed form (it decodes back to the same string). Otherwise store the model's
    short/long string form."""
    if _canonical_vid(s):
        return bytes([_VP_VID]) + int(s, 16).to_bytes(8, "big")
    if "/" in s:
        label, _, v = s.partition("/")
        if _canonical_vid(v) and 0 < len(label) < 256:
            lb = label.encode("utf-8")
            if len(lb) < 256:
                return bytes([_VP_REF, len(lb)]) + lb + int(v, 16).to_bytes(8, "big")
    b = s.encode("utf-8")
    if len(b) <= 126:
        return bytes([_VP_STR | len(b)]) + b
    return bytes([_VP_STRLONG]) + len(b).to_bytes(8, "big") + b

def _vp_encode_val(x):
    if x is None:
        return bytes([_VP_NULL])
    if isinstance(x, bool):
        return bytes([_VP_TRUE if x else _VP_FALSE])
    if isinstance(x, int):
        if -6 <= x <= 9:
            return bytes([_VP_SMALL + (x + 6)])
        w = _int_byte_width(x)
        if w > 8:
            # refuse at ENCODE: a 9-byte head would be undecodable;
            # write-succeeds/read-fails is the worse failure. No LDBC value reaches this.
            raise ValueError(f"vpack codec: int {x} exceeds 8 bytes")
        return bytes([_VP_INT + w]) + x.to_bytes(w, "little", signed=True)
    if isinstance(x, float):
        import struct
        return bytes([_VP_DOUBLE]) + struct.pack(">d", x)
    if isinstance(x, str):
        return _vp_encode_str(x)
    if isinstance(x, (bytes, bytearray)):
        wc = _bytes_wcode(len(x))
        return bytes([_VP_BYTES + wc]) + len(x).to_bytes(_OBJ_W[wc], "big") + bytes(x)
    if isinstance(x, dict):
        return _vp_encode_obj(x)
    if isinstance(x, (list, tuple)):
        bodies = [_vp_encode_val(e) for e in x]
        if len(x) > 255 or any(len(b) > 255 for b in bodies):
            raise ValueError("vpack codec: array too large for the 1-byte element table")
        return (bytes([_VP_ARRAY, len(x)]) + bytes(len(b) for b in bodies)
                + b"".join(bodies))
    raise TypeError(f"vpack codec: unsupported type {type(x).__name__}: {x!r}")

def _vp_encode_obj(doc):
    """Mirror of vpack_bytes: members (key + value), then the frame whose widths step up
    with total size -- head(1) + byteLength(w) + index table (one offset per member) +
    nrItems(w), laid out head, byteLength, members, offsets, nrItems."""
    members, offsets = bytearray(), []
    for k, val in doc.items():
        offsets.append(len(members))
        if k in _VPACK_SYSCODE:
            members.append(_VPACK_SYSCODE[k])
        else:
            members += _vp_encode_str(k)
        members += _vp_encode_val(val)
    n = len(doc)
    for wcode, (w, thresh) in enumerate(zip(_OBJ_W, (256, 65536, 4294967296, float("inf")))):
        total = len(members) + 1 + w + n * w + w
        if total < thresh:
            head_off = 1 + w                       # members start after head + byteLength
            out = bytearray([_VP_OBJ + wcode])
            out += total.to_bytes(w, "big")
            out += members
            for o in offsets:
                out += (head_off + o).to_bytes(w, "big")
            out += n.to_bytes(w, "big")
            return bytes(out)
    raise AssertionError("unreachable")

def _vp_decode_val(b, i):
    import struct
    h = b[i]
    i += 1
    if h == _VP_NULL:
        return None, i
    if h == _VP_FALSE:
        return False, i
    if h == _VP_TRUE:
        return True, i
    if h == _VP_DOUBLE:
        return struct.unpack_from(">d", b, i)[0], i + 8
    if h == _VP_VID:
        return format(int.from_bytes(b[i:i + 8], "big"), "016x"), i + 8
    if h == _VP_REF:
        ln = b[i]
        label = b[i + 1:i + 1 + ln].decode("utf-8")
        u = int.from_bytes(b[i + 1 + ln:i + 9 + ln], "big")
        return f"{label}/{format(u, '016x')}", i + 9 + ln
    if _VP_SMALL <= h < _VP_SMALL + 16:
        return (h - _VP_SMALL) - 6, i
    if _VP_INT < h <= _VP_INT + 8:
        w = h - _VP_INT
        return int.from_bytes(b[i:i + w], "little", signed=True), i + w
    if h == _VP_ARRAY:
        n = b[i]
        lens = b[i + 1:i + 1 + n]
        j = i + 1 + n
        out = []
        for ln in lens:
            v, j2 = _vp_decode_val(b, j)
            if j2 - j != ln:
                raise ValueError("vpack codec: array element length mismatch")
            out.append(v)
            j = j2
        return out, j
    if _VP_OBJ <= h < _VP_OBJ + 4:
        w = _OBJ_W[h - _VP_OBJ]
        total = int.from_bytes(b[i:i + w], "big")
        end = i - 1 + total
        n = int.from_bytes(b[end - w:end], "big")
        j = i + w
        stop = end - w * (n + 1)                   # members end where the offset table starts
        out = {}
        while j < stop:
            if b[j] in _VPACK_SYSNAME:
                k = _VPACK_SYSNAME[b[j]]
                j += 1
            else:
                k, j = _vp_decode_val(b, j)
            out[k], j = _vp_decode_val(b, j)
        if len(out) != n:
            raise ValueError(f"vpack codec: decoded {len(out)} members, frame says {n}")
        return out, end
    if h == _VP_STRLONG:
        ln = int.from_bytes(b[i:i + 8], "big")
        return b[i + 8:i + 8 + ln].decode("utf-8"), i + 8 + ln
    if _VP_BYTES <= h < _VP_BYTES + 4:
        w = _OBJ_W[h - _VP_BYTES]
        ln = int.from_bytes(b[i:i + w], "big")
        return bytes(b[i + w:i + w + ln]), i + w + ln
    if h & 0x80:
        ln = h & 0x7F
        return b[i:i + ln].decode("utf-8"), i + ln
    raise ValueError(f"vpack codec: unknown head 0x{h:02x} at {i - 1}")

def _edge_key(eid):
    """An edge document's _key member: the C++ store's hex16(docid) -- bulk "e%011d" -> n, an incremental "w%011d" ->
    the top-bit write space | n (keys.h kWriteDoc) -- so both sides encode the same 16-char string."""
    n = int(eid[1:])
    return format(((1 << 63) | n) if eid[0] == "w" else n, "016x")

class ArangodbFormat(Format):
    name = "arangodb"
    CHASE_FIRST_EDGE_ONLY = True   # a chase hop: 1 SEEK landing on the first edge, no NEXT (C++ arangodb chase_reads)
    # a read under one label of a vid known to be of another collection is not issued (the C++ format carries
    # the collection on the row; here the _coll map stands in). The engine twin sets it False
    _ROW_LABELS = True

    # the edge-doc codec this instance prices (python/kvevaluator/formats/props_codec.py), read ONCE: None = the knob
    PROPS_CODEC = None

    def __init__(self, schema, sizing=None):
        super().__init__(schema)
        self.sz = sizing or ArangoSizing()                  # tunable key + value byte widths
        self.props_codec = self.PROPS_CODEC or props_codec()
        self._docn = 0
        self._edgen = 0
        # {vid -> concrete collection}. Real arango knows a vertex's collection from the
        # `_id` it reads out of an edge's _from/_to (collection-qualified). Vids are now
        # unified to `msg_` and no longer carry the Post-vs-Comment type in a prefix, so we
        # keep the label the loader gave us at write time and recover it here -- a free
        # in-memory lookup, strictly more faithful than parsing the vid string. NOT used for
        # the bare-id message ANCHOR (is4/is6): there the engine genuinely does not know the
        # type and probes each candidate collection (see read_vertex_by_id/read_anchor_props).
        self._coll = {}          # vid -> collection, populated at ingest (add_vertex)
        self._coll_loaded = False  # have we rebuilt _coll from the store (cached-open path)?

    def key_bytes(self, key):
        """Arango RocksDB key = [objectId][...]. Four shapes, dispatched by arity + marker:
          document (collection, docid) / (etype, eid) -> objectId + docId
          primary  ("pk", label, vid)                 -> objectId + _key(real len)
          edge idx ("f"|"t", etype, vid, eid)         -> objectId + vid(real len) + docId
          value idx ("i", label, prop, val, docid)    -> objectId + val(real len) + docId
        The _key and the edge-index _from/_to vid are genuinely varlen (arango's only
        varlen id), so they are content-real. Known limitation: the edge-index key models
        the bare vid, not the collection-qualified _id ("Person/person_933"), a small
        under-count by the collection prefix."""
        s = self.sz
        n = len(key)
        if n == 2:                                          # a document (vertex or edge doc)
            return s.object_id + s.doc_id
        if n == 3 and key[0] == "pk":                       # ("pk", label, vid)
            return s.object_id + len(str(key[2]))
        if n == 4 and key[0] in ("f", "t"):                 # ("f"|"t", etype, vid, eid)
            return s.object_id + len(str(key[2])) + s.doc_id
        if n == 5 and key[0] == "i":                        # ("i", label, prop, val, docid)
            return s.object_id + len(str(key[3])) + s.doc_id
        raise ValueError(f"arango key_bytes: unrecognized key shape {key!r}")

    def value_bytes(self, v, key=None):
        """Value bytes under arango's model. Neutral unless sz.pack, which models VPack for
        DOCUMENTS only -- the dict values (vertex/edge docs). The store's other values (the pk
        loc tuple, the covering edge-index _id string, empty index values) are internal, not
        VPack documents, so they stay neutral. `dict` is the reliable document discriminator:
        no other arango value is a dict. Every document follows the property codec
        (python/kvevaluator/formats/props_codec.py): schema = the system-member object ({_from, _to} for an edge doc,
        {_key} for a vertex doc) + the props' RowV2 row (the C++ store's form), named = every prop a
        named member."""
        if self.sz.pack and isinstance(v, dict):
            if self.props_codec == "schema":
                # the system members stay a VPack object; the declared columns (key = (collection, docid)) set the
                # row's bitmap and whether it exists: an edge doc's etype props, a vertex doc's label props (no id: key-derived)
                edge = "_from" in v
                sys = ("_from", "_to") if edge else ("_key",)
                props = {k: x for k, x in v.items() if k not in sys}
                sys_obj = {k: v[k] for k in sys if k in v}
                if not key:
                    ncols = len(props)
                elif edge:
                    ncols = len(self.schema.edge_prop_types.get(key[0], ()))
                else:
                    ncols = len(self.schema.vertex_prop_types.get(key[0], ()))   # the declared props (no id)
                return vpack_bytes(sys_obj) + _doc_row_bytes(props, ncols)
            return vpack_bytes(v)
        return content_bytes(v, self.sz.int_bytes, self.sz.bool_bytes)

    # --- physical value codec (it lives on the format): dispatch by the same key shapes -------------
    def encode_value(self, key, value):
        if value is None:
            return b""                                     # empty index entries
        if len(key) == 2:                                  # (collection, docid): a document
            if not isinstance(value, dict):
                raise ValueError(f"arango codec: document must be a dict, got {type(value).__name__}")
            return _vp_encode_obj(value)
        if key[0] == "pk":                                 # loc tuple (collection, docid)
            return valcodec.encode_prim(value)
        if key[0] in ("f", "t"):                           # covering ref "Label/vid"
            if not isinstance(value, str):
                raise ValueError(f"arango codec: edge-index ref must be str, got {value!r}")
            return _vp_encode_str(value)
        raise ValueError(f"arango codec: unexpected value {value!r} at {key}")

    def decode_value(self, key, raw):
        if not raw:
            return None
        if len(key) == 2:
            v, i = _vp_decode_val(raw, 0)
            if i != len(raw):
                raise ValueError(f"arango codec: {len(raw) - i} trailing bytes at {key}")
            return v
        if key[0] == "pk":
            return valcodec.decode_prim(raw)
        if key[0] in ("f", "t"):
            v, i = _vp_decode_val(raw, 0)
            if i != len(raw):
                raise ValueError(f"arango codec: {len(raw) - i} trailing bytes at {key}")
            return v
        raise ValueError(f"arango codec: unexpected raw value at {key}")

    # ----------------------
    # --- STORAGE WRITES ---
    # ----------------------

    def add_vertex(self, store, vid, label, props):
        self._coll[vid] = label                              # remember the collection (type)
        self._docn += 1
        # docids are FIXED-WIDTH so the abstract store's string order equals numeric order --
        # what real arango's monotonic LocalDocumentId gives (bare "d%d" made "d10" sort before
        # "d2", an artifact the C++ backend exposed as neighbor-order divergence)
        loc = (label, "d%011d" % self._docn)                 # <collection, docid>
        store.put(loc, {"_key": vid, **without_key_id(props)})   # the vertex document (id is the _key)
        store.put(("pk", label, vid), loc)                   # per-collection primary index
        for p, val in props.items():
            if self.schema.is_indexed(label, p):
                store.put(("i", label, p, str(val), loc[1]), None)
            elif (label, p) in self.schema.date_cols:
                # sorted date index; fixed-width int so key order == date order
                store.put(("i", label, p, f"{val:013d}", loc[1]), None)

    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        # The loader always passes the concrete labels; the map is the fallback (a msg_ vid
        # no longer reveals Post-vs-Comment, so we cannot parse it out of the vid).
        src_label = src_label or self._coll[src]
        dst_label = dst_label or self._coll[dst]
        self._edgen += 1
        eid = "e%011d" % self._edgen
        loc = (etype, eid)                                   # edge doc, per-etype collection
        store.put(loc, {"_key": _edge_key(eid), "_from": f"{src_label}/{src}", "_to": f"{dst_label}/{dst}",
                        **(props or {})})
        store.put(("pk", etype, eid), loc)                   # every document has its pk entry, edges too
        # covering edge indexes: the VALUE is the opposite vertex's _id; the
        # edge doc's id rides in the KEY (an edge-prop read = direct doc GET)
        store.put(("f", etype, src, eid), f"{dst_label}/{dst}")
        store.put(("t", etype, dst, eid), f"{src_label}/{src}")

    # --- WRITE OPERATORS (incremental; see base.Format) --------------------------------
    #
    # The document-store shape, and it behaves like nebula's row store on writes even though the
    # read paths differ: a vertex is a document plus a fixed index set, so an insert is FLAT in
    # property count (mb2 MB83 == MB84 == 4 PUT), and an update reads the document back and
    # moves the touched column's index entry (the real engine re-maintains every secondary index,
    # mb2 MB82 == MB81 -- arangodb_engine models that). The one place arango is genuinely
    # costlier than nebula on writes is DELETING an edge: nebula addresses an edge key directly
    # from <src, etype, dst>, while an arango edge document's id is not derivable from its
    # endpoints, so the edge index has to be SCANNED to find it first (mb2 saw 3-243 NEXTs on
    # MB88 for exactly this reason).

    def _new_doc_id(self, prefix):
        """A document id for an INCREMENTAL insert. Deliberately a different id space ("w") from
        the bulk load's "d"/"e": on a cached store the format object is fresh, so its counters
        restart at 1 and a "d1" would collide with a loaded document. Real arango has the same
        requirement and solves it the same way -- a fresh, never-reused _key."""
        self._writen = getattr(self, "_writen", 0) + 1
        return "%s%011d" % (prefix, self._writen)

    def _index_key(self, label, prop, val, docid):
        """One index key, encoded exactly as add_vertex encodes it. A column with no value still
        gets an entry -- one entry per document per index, which is what makes the insert flat."""
        if (label, prop) in self.schema.date_cols and not self.schema.is_indexed(label, prop):
            return ("i", label, prop, f"{val:013d}" if val is not None else "", docid)
        return ("i", label, prop, str(val) if val is not None else "", docid)

    def _n_index_keys(self, label):
        """FORMULA side of the oracle -- counted from the schema, not from the emitted list."""
        return len(self.schema.index_cols_of(label))

    def _w_insert_vertex(self, store, vid, label, props):
        docid = self._new_doc_id("w")
        loc = (label, docid)
        self._coll[vid] = label                              # keep the vid->collection map live
        puts = [(loc, {"_key": vid, **without_key_id(props)}), (("pk", label, vid), loc)]
        for prop in self.schema.index_cols_of(label):
            puts.append((self._index_key(label, prop, props.get(prop), docid), None))
        # doc + pk + one entry per declared index of the label
        return puts, [], KVCost(P=2 + self._n_index_keys(label))

    def _w_insert_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        src_label = src_label or self._coll[src]
        dst_label = dst_label or self._coll[dst]
        eid = self._new_doc_id("w")
        puts = [((etype, eid), {"_key": _edge_key(eid), "_from": f"{src_label}/{src}", "_to": f"{dst_label}/{dst}",
                                **props}),
                (("pk", etype, eid), (etype, eid)),
                (("f", etype, src, eid), f"{dst_label}/{dst}"),
                (("t", etype, dst, eid), f"{src_label}/{src}")]
        # edge doc + its pk entry + both covering index entries. Four keys, flat in degree -- the
        # split-adjacency write advantage, paid for with two more keys than nebula (the separate edge document).
        return puts, [], KVCost(P=4)

    def _w_update_props(self, store, vid, label, props):
        loc = store.get(("pk", label, vid))                  # 1 GET: primary index -> document id
        doc = (store.get(loc) or {}) if loc else {}          # 1 GET: the document itself
        if loc is None:
            return [], [], KVCost(G=1)                       # no such vertex: the pk miss is the whole cost
        docid = loc[1]
        new = {**doc, **props}                               # every changed member, ONE document write
        puts = [(loc, new)]
        dels = []
        # only the changed columns' index entries move (real arango rewrites them all -- mb2: 82 ==
        # 81 -- an engine choice arangodb_engine keeps; see _update_index_cols)
        cols = self._update_index_cols(label, props)
        for p in cols:
            dels.append(self._index_key(label, p, doc.get(p), docid))
            puts.append((self._index_key(label, p, new.get(p), docid), None))
        # 2 GETs is the id-indirection this layout pays on every access, read or write.
        return puts, dels, KVCost(G=2, P=1 + len(cols), D=len(cols))

    def _w_delete_vertex(self, store, vid, label, with_edges=True):
        loc = store.get(("pk", label, vid))
        if loc is None:
            return [], [], KVCost(G=1)
        doc = store.get(loc) or {}
        docid = loc[1]
        dels = [loc, ("pk", label, vid)]
        for p in self.schema.index_cols_of(label):
            dels.append(self._index_key(label, p, doc.get(p), docid))
        pred = KVCost(G=2, D=2 + self._n_index_keys(label))
        if with_edges:
            # One covering-index scan per incident (etype, dir). Each entry names the edge doc
            # (key) and the other endpoint (value), so the doc and BOTH covering entries are
            # addressed directly. No per-edge _locate_edge: that search is delete_edge's tax,
            # paid only when the caller knows just the endpoints.
            for etype, dir in self.schema.incident_etypes(label):
                side, other_side = ("f", "t") if dir == "out" else ("t", "f")
                prefix = (side, etype, vid)
                cur = store.seek(prefix)                     # 1 SEEK (edge index)
                pred.S += 1
                while cur.valid() and cur.key()[:3] == prefix:
                    eid = cur.key()[3]
                    other = cur.value().split("/", 1)[1]     # covering: "Label/vid" -> vid
                    dels += [(etype, eid), ("pk", etype, eid), (side, etype, vid, eid),
                             (other_side, etype, other, eid)]
                    pred.D += 4
                    cur.next()                               # 1 NEXT per edge
                    pred.N += 1
                cur.mark_miss()                              # the landing that ended the scan
        return [], dels, pred

    def _locate_edge(self, store, etype, src, dst):
        """Find an edge document's id by scanning the forward covering index of `src` until the
        entry whose value names `dst`. This SEARCH is the document layout's write-side tax: the
        edge doc's id is not a function of its endpoints, so removing an edge you can already name
        still costs a scan (mb2 MB88: 3-243 NEXTs). Returns (eid | None, KVCost)."""
        cur = store.seek(("f", etype, src, ""))
        S, N = 1, 0
        while cur.valid() and cur.key()[:3] == ("f", etype, src):
            if str(cur.value()).endswith("/" + str(dst)):
                return cur.key()[3], KVCost(S=S, N=N)
            cur.next()
            N += 1
        cur.mark_miss()                              # not found: the scan ran off the prefix
        return None, KVCost(S=S, N=N)

    def _remove_edge(self, store, etype, src, dst):
        """(deleted keys, KVCost) for one edge: its document, its pk entry and both covering index entries.
        Used by delete_edge; delete_vertex addresses its edges from its own scan."""
        eid, cost = self._locate_edge(store, etype, src, dst)
        if eid is None:
            return [], cost                                  # nothing to remove; the search is still paid
        cost.D += 4
        return [(etype, eid), ("pk", etype, eid), ("f", etype, src, eid), ("t", etype, dst, eid)], cost

    def _w_delete_edge(self, store, etype, src, dst, src_label=None, dst_label=None):
        dels, cost = self._remove_edge(store, etype, src, dst)
        return [], dels, cost

    def _w_update_edge_prop(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        # The edge props live in the edge DOCUMENT, whose id is not a function of the endpoints:
        # find it through the covering index exactly as delete_edge does (the scan is paid either
        # way), then read the document back and rewrite it whole (1 GET + 1 PUT). The covering
        # index entries hold only the neighbour id, so they do not change.
        eid, cost = self._locate_edge(store, etype, src, dst)
        if eid is None:
            return [], [], cost
        doc = store.get((etype, eid)) or {}
        cost.G += 1
        cost.P += 1
        return [((etype, eid), {**doc, **props})], [], cost

    # xxx STORAGE READS END xxx

    # ---------------------
    # --- STORAGE READS ---
    # ---------------------

    def _collection(self, store, vid, label):
        """The pk collection for a vid. A str label names it; a candidate list means
        'message whose type the engine already knows from the edge _id it read' --
        recovered here from the _coll map."""
        if isinstance(label, str):
            return label
        self._ensure_coll(store)
        return self._coll[vid]

    def warm(self, store):
        """Rebuild the vid->collection map up front (see base.Format.warm). Doing it here,
        at store-open, keeps the pk sweep out of any query's rocks-ticker window."""
        self._ensure_coll(store)

    def _ensure_coll(self, store):
        """Make sure vid->collection is populated. It is filled at ingest (add_vertex),
        but a CACHED store is opened WITHOUT re-ingesting -- and even --rebuild reopens
        the just-built store with a fresh format -- so the map can be empty when a query
        runs. Rebuild it ONCE from the pk index (`("pk", collection, vid)` per vertex),
        reading the RAW uncounted store: this is SETUP, not a query op. It stays faithful
        to the cost model -- the real engine gets a vertex's collection FREE from the edge
        _from/_to it traversed; this scan only recovers what our in-memory map lost, it is
        not charged. Idempotent + memoized, so it costs one pk sweep per format at most."""
        # _coll non-empty => ingest ran in THIS format (it fills every vertex before any
        # edge/read), so the map is already complete -- skip the scan (the abstract path).
        if self._coll or self._coll_loaded:
            return
        # Read the RAW store so the scan stays uncounted. `store` is normally a CountingKV
        # (use its .backend); degrade to `store` itself if it is already a raw backend, so a
        # future caller that passes the raw store here can't turn this into an AttributeError.
        raw = getattr(store, "backend", store)
        cur = raw.seek(("pk",))                       # uncounted: raw store, setup only
        edge_colls = set(self.schema.etypes)
        while cur.valid() and cur.key()[0] == "pk":
            _, collection, vid = cur.key()
            if collection not in edge_colls:          # an edge document's own pk entry is not a vertex
                self._coll[vid] = collection
            cur.next()
        self._coll_loaded = True

    @counted
    def read_vertex_by_id(self, store, vid, label=None):
        if label is None:
            raise ValueError("arango read_vertex_by_id needs label (the collection)")
        # A candidate-COLLECTION list (message anchor of unknown type) probes each
        # collection's pk in order for the one msg_ vid -- what DOCUMENT('Post/42') then
        # DOCUMENT('Comment/42') does -- paying a GET per miss. A str label = one probe.
        cands = (vid,) if isinstance(vid, str) else tuple(vid)
        colls = [label] if isinstance(label, str) else list(label)
        for cand in cands:
            for coll in colls:
                if store.get(("pk", coll, cand)) is not None:
                    return [cand], None
        return [], None

    @counted
    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False, first_only=False):
        # Per direction: SEEK the edge index, 1 NEXT per edge. The index VALUE is
        # the opposite vertex's _id (covering), so a pure-id expansion reads NO
        # edge documents; requesting edge props adds 1 GET per edge (the edge doc,
        # whose id is in the index KEY -- a direct doc read, no pk hop).
        sides = {"out": ("f",), "in": ("t",), "both": ("f", "t")}[dir]
        distinct = list(dict.fromkeys(srcs))    # srcs read as a SET (base docstring)
        out = []
        for s in distinct:
            for idx in sides:
                prefix = (idx, etype, s)
                cur = store.seek(prefix)                     # 1 SEEK (edge index)
                while cur.valid() and cur.key()[:3] == prefix:
                    other = cur.value().split("/", 1)[1]     # covering: "Label/vid" -> vid
                    if edge_props:
                        eid = cur.key()[3]
                        doc = store.get((etype, eid)) or {}  # edge doc, only for props
                        out.append(neighbor_item(s, other, doc, edge_props, pairs))
                    else:
                        out.append(neighbor_item(s, other, None, None, pairs))
                    if first_only:                           # the chase: no NEXT past the first edge
                        break
                    cur.next()                               # 1 NEXT per edge
                else:
                    cur.mark_miss()                          # the landing that ended the scan: an absence miss
        return out, None

    def _outside(self, vid, label):
        """is the vid's collection known and outside the plan's label (a str, or the candidate list)?"""
        coll = self._coll.get(vid)
        return coll is not None and (coll != label if isinstance(label, str) else coll not in label)

    @counted
    def read_vertex_props(self, store, vids, props, label=None):
        # pk + doc = 2 GETs per DISTINCT vid (id-indirection), one row per input.
        # `id` is KEY-DERIVED: _key embeds the external id ("msg_42"), so a
        # pure-id projection reads nothing (real AQL: PARSE_IDENTIFIER/._key).
        if label is None:
            raise ValueError("arango read_vertex_props needs label (the collection)")
        row_props = [p for p in props if p != "id"]
        # A concrete str label names the collection outright; a candidate list (or None)
        # means message vids whose collection must come from _coll -- so rebuild that map
        # from the store first if it was opened without ingest (cached-open path).
        if self._ROW_LABELS or not isinstance(label, str):
            self._ensure_coll(store)    # also under a str label: the map says when a vid is of another collection
        cache = {}
        for v in dict.fromkeys(vids):
            # _label = the concrete collection (free: real arango holds the _id it traversed
            # to). Lets an IS_SAME_COLLECTION filter read the type off the row instead of the
            # vid, which no longer carries it (see engine_plans, e.g. ic2/is6/ic4). A str
            # label IS the collection; else look it up in the (now-populated) map.
            # A vid whose collection is known to be OUTSIDE the plan's label is not read at all: under one label, a vid
            # of another collection (no engine fetches Post/x for a known Comment); under a candidate list, a vid of
            # none of them (bi16 / bi17: a Tag's hasTag-in also yields Forums, and the props read of ["Post",
            # "Comment"] is the type filter). The C++ does the same from the label the row carries (Arango::
            # read_vertex_props, narrow()). The _coll map stands in for that label (Value::label); it knows every vid,
            # the row only those that passed a hop or an index.
            # So the stand-in is exact only for a vid that reached this read through a hop or an index (every
            # committed fair plan: the 417-cell differential); the engine twin turns it off (_ROW_LABELS), because
            # its bare-id reads (IS4: DOCUMENT('Post/x') then DOCUMENT('Comment/x')) do pay the miss.
            other = self._ROW_LABELS and self._outside(v, label)
            row = {"_vid": v, "_label": label if isinstance(label, str) else (None if other else self._coll.get(v))}
            if "id" in props:
                row["id"] = key_id(v)
            if row_props and other:
                for p in row_props:
                    row[p] = None
            elif row_props:
                loc = store.get(("pk", self._collection(store, v, label), v))  # 1 GET: pk
                # A pk MISS costs ONE get, not two: with no pointer there is no document to
                # read. Charging 2 unconditionally was wrong -- the layout cannot force a
                # read that has nothing to address. Visible in arango IS4, where the query
                # is literally DOCUMENT('Post/3') + DOCUMENT('Comment/3') and live G is 3
                # (pk+doc for the hit, pk-only for the miss), not 4.
                doc = {}
                if loc:
                    doc = store.get(loc)                                # 1 GET: the document
                for p in row_props:
                    row[p] = (doc or {}).get(p)
            cache[v] = row
        return ([dict(cache[v]) for v in vids],
                None)

    def read_anchor_props(self, store, cands, props, labels=None):
        # Probe each candidate COLLECTION for the msg_ vid until one exists (misses paid),
        # then ONE doc read -- what DOCUMENT('Post/42') + DOCUMENT('Comment/42') does, with
        # no pk re-lookup like a read_vertex_by_id + read_vertex_props split would pay. A str label = one
        # known collection. The bare-id anchor genuinely does not know the type, so it does
        # NOT consult _coll here -- it pays the real per-collection probe.
        G = 0
        colls = [labels] if isinstance(labels, str) else list(labels or ["?"])
        for cand in cands:
            for coll in colls:
                G += 1
                loc = store.get(("pk", coll, cand))
                if loc is not None:
                    doc = store.get(loc) or {}
                    G += 1
                    return [{"_vid": cand, "_label": coll,
                             **{p: key_id(cand) if p == "id" else doc.get(p) for p in props}}], KVCost(G=G)
        return [], KVCost(G=G)

    @counted
    def read_index_eq(self, store, label, prop, value):
        # Persistent-index prefix scan: SEEK + one NEXT per match. The index key
        # holds the DOCID, not the _key -- recovering the vid for traversal costs
        # a doc GET per match (which would also cover that doc's props).
        prefix = ("i", label, prop, str(value))
        vids = []
        cur = store.seek(prefix)
        while cur.valid() and cur.key()[:4] == prefix:
            docid = cur.key()[4]
            cur.next()
            doc = store.get((label, docid)) or {}       # 1 GET: resolve _key
            vids.append(doc.get("_key"))
        cur.mark_miss()                                # the landing that ended the scan: an absence miss
        return vids, None

    @counted
    def read_index_range(self, store, labels, prop, lo, hi):
        # Per-collection value-sorted date index: seek lo, 1 NEXT per entry until
        # >= hi. The index KEY holds the DOCID, so recovering the _key for the
        # intersect costs 1 doc GET per match (as read_index_eq). Fixed-width (013d)
        # keys make key order == ms order. Scans Post+Comment and unions.
        lo_s, hi_s = f"{lo:013d}", f"{hi:013d}"
        out = []
        for label in labels:
            cur = store.seek(("i", label, prop, lo_s))
            while (cur.valid() and cur.key()[:3] == ("i", label, prop)
                   and cur.key()[3] < hi_s):
                docid = cur.key()[4]
                cur.next()
                doc = store.get((label, docid)) or {}    # 1 GET: resolve _key
                out.append(doc.get("_key"))
            cur.mark_miss()                            # the landing past the window: an absence miss
        return out, None

    @counted
    def read_vertices_by_label(self, store, label, props=None, keep=None):
        # With `props`: ONE scan of the collection's documents ((label, docid) is one key run): 1 SEEK + 1 NEXT
        # per document, every document's key and value read; rows as read_vertex_props builds them, in docid
        # order. `keep` (a vid set, optional): rows of other vertices are not built; the set is tested in memory,
        # every document is still crossed. The capability: label_rows_one_run.
        if props is not None:
            kept = None if keep is None else set(keep)
            row_props = [p for p in props if p != "id"]
            rows = []
            cur = store.seek((label,))
            while cur.valid() and cur.key()[:1] == (label,):
                doc = cur.value()
                v = doc["_key"]
                if kept is None or v in kept:
                    row = {"_vid": v, "_label": label}
                    if "id" in props:
                        row["id"] = key_id(v)
                    for p in row_props:
                        row[p] = doc.get(p)
                    rows.append(row)
                cur.next()
            cur.mark_miss()
            return rows, None
        if keep is not None:
            raise ValueError("arangodb read_vertices_by_label: keep needs props (the scan of the collection's documents)")
        # Type-enumeration entry: prefix-scan the per-collection pk index
        # ("pk", label, vid) -> loc. The vid is in the KEY, so enumeration is
        # key-only -- 1 SEEK + 1 NEXT per doc, 0 GETs (unlike read_index_eq, whose
        # persistent-index key holds the docid and needs a doc GET to recover the
        # _key). Downstream read_vertex_props then pays the doc reads -- the missing
        # value index shows up there, not here.
        prefix = ("pk", label)
        vids = []
        cur = store.seek(prefix)
        while cur.valid() and cur.key()[:2] == prefix:
            vids.append(cur.key()[2])
            cur.next()
        cur.mark_miss()                                # the landing that ended the scan: an absence miss
        return vids, None

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """the from / to edge indexes hold both directions; no per-edge key to probe (an edge's doc id is not a
        function of its endpoints): the lists are what a membership check reads"""
        return {"reverse_adjacency", "adjacency_lists"}

    @classmethod
    def capabilities(cls, schema):
        # The type lives in the collection-qualified id, an always-on per-type structure: the
        # type filter is free by either op, so arango holds type_index_single_posting whatever
        # the schema declares (the plan's index variant costs it nothing).
        # index_entries_by_docid: its persistent index keys by the internal docid (the _key is
        # in the document), so a candidate-filtered index walk pays a document GET per crossed
        # entry -- the access-path fact behind its one-phase picks (read_top_k_by_index).
        # label_rows_one_run: a collection's documents are one key run ((label, docid)), so one sequential scan
        # of the run may read the label with its props. NOT held: whole_label_scan_by_default
        # -- the scan is a candidate here (the person-run twins, ic10's pin), not the default plan of the microbench
        # whole-label scans, until the other formats' scans are sized
        return {"sorted_range_index", "per_type_index", "type_index", "whole_row_read",
                "type_index_single_posting", "index_entries_by_docid", "label_scoped_props", "label_rows_one_run"}

    def read_filter_by_label_index(self, store, vids, label):
        return self.read_filter_by_label_probe(store, vids, label)   # free either way (below)

    @counted
    def read_filter_by_label_probe(self, store, vids, label):
        # FREE: arango ids are collection-qualified ("Comment/msg_42" in _from/_to), so
        # the type is already known -- recovered here from the load-time _coll map, standing
        # in for the collection name the engine read out of the edge doc. Ignores schema.has_type_index:
        # the type lives in the id -- an always-on per-type structure -- so arango's type
        # filter is free in BOTH study configs.
        self._ensure_coll(store)              # rebuild the map if the store was opened w/o ingest
        return ([v for v in vids if self._coll.get(v) == label],
                None)

    _DOCID_LEN = 12          # "d%011d" -- the fixed docid string this format writes

    def read_top_k_by_index(self, store, vids, prop, k, order="desc", hi=None,
                       labels=None, props=None, id_order="asc"):
        """Ranked top-k via the per-collection SORTED persistent date indexes:
        walk each candidate collection's index newest-first, merging in date
        order. The index key holds the DOCID, not the _key, so MEMBERSHIP costs
        1 doc GET per entry crossed -- the read doc then carries the winners'
        props and id for free (no re-read; same fusion argument as
        read_anchor_props). `hi` (exclusive ms) is free via seek_for_prev on the
        4-tuple (see the nebula op). Drains same-date entries past the k-th hit
        so the tie-break cut is complete."""
        assert order == "desc", "the LDBC shape; asc would walk forward"
        labels = (labels,) if isinstance(labels, str) else tuple(labels)
        # vids=None = NO candidate restriction (a GLOBAL top-k): the per-collection
        # index IS the type restriction, so every entry crossed is a hit -- AQL
        # SORT ... DESC LIMIT k over the persistent index. The membership doc GET
        # per crossed entry stays (the _key lives in the doc) -- exactly arango's
        # docid-in-key cost. dgraph has no analogue (its date index mixes all
        # types) -- see the microbench topk override.
        cand = None if vids is None else set(vids)
        bound = "~" if hi is None else f"{hi:013d}"      # "~" sorts after digits
        cursors, S, G, N = [], 0, 0, 0
        for label in labels:
            cursors.append((label, store.seek_for_prev(("i", label, prop, bound))))
            S += 1

        def current(lc):                                  # key if still in this index
            label, cur = lc
            if cur.valid() and cur.key()[:3] == ("i", label, prop):
                return cur.key()                          # ("i", label, prop, ms, docid)
            return None

        hits = []
        while True:
            best = None
            for lc in cursors:
                key = current(lc)
                if key is not None and (best is None or key[3] > best[1][3]):
                    best = (lc, key)
            if best is None:
                break
            if len(hits) >= k and best[1][3] != hits[-1][prop + "_s"]:
                break                                     # past the k-th date: complete
            (label, cur), key = best
            doc = store.get((label, key[4])) or {}        # membership: docid -> _key
            G += 1
            vid = doc.get("_key")
            if cand is None or vid in cand:
                hits.append({"_vid": vid, prop: int(key[3]), prop + "_s": key[3],
                             "id": key_id(vid), "_doc": doc})
            cur.prev()                                    # 1 NEXT per entry crossed
            N += 1
        for _, cur in cursors:                            # every cursor rests on an unconsumed entry
            cur.mark_miss()                               # (or off its index): an absence miss
        hits.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        hits.sort(key=lambda r: r[prop], reverse=True)
        rows = hits[:k]
        for r in rows:
            del r[prop + "_s"]
            doc = r.pop("_doc")
            for p in props or ():
                r[p] = key_id(r["_vid"]) if p == "id" else doc.get(p)   # free: doc already read; id: the key
        return rows, KVCost(S=S, G=G, N=N)

    def _edge_read(self, store, run, anchors, todo, etype):
        # No composite (from, to) index exists, so one read scans the named edge list (from for out, to for in)
        # of each owner once (index scan + edge-doc GETs) and checks membership.
        dirs = {r["dir"] for r in run}
        d = "both" if len(dirs) == 2 else dirs.pop()
        if run[0]["at"] == "anchor":
            nbrs, c = self.read_neighbors(store, anchors, etype, d)
            keep = set(nbrs)
            found = {x for x in todo if x in keep}
        else:
            nbrs, c = self.read_neighbors(store, todo, etype, d, pairs=True)
            aset = set(anchors)
            found = {p["src"] for p in nbrs if p["dst"] in aset}
        return found, KVCost(S=c.S, G=c.G, N=c.N)
