"""pertype_csr: the per-type chunked layout, column chunks for props and CSR chunks for adjacency (the Python twin).

This twin is a rows-and-order oracle, not a count oracle. Its counts differ from the C++ format's in three
ways: it takes a vertex's label from the id's group or the plan's list (C++ reads the label a row carries or an entry
stores); its adjacency chunks hold no far-label block; its index keeps one key per entry (C++: one posting per value).
oracle.runner refuses the counts of every step one of these can touch (take_count_caveat). Where counts are kept,
S / G / N and the value bytes are the format's; the key-byte lanes are this Python key model's, not the C++ key's.

Keys (one KV pair per chunk, not per entity):
  ("t", label, "p", first_uid)              a props chunk: the rows of one type, uid-sorted, columnar
  ("t", label, "a", etype, dir, first_src)  an adjacency chunk: CSR lists for one (type, edge type, direction);
                                            both directions stored, topology only
  ("t", src_label, "ep", etype, first_ord)  an edge-props chunk: one edge type's props, stored once, by edge ordinal
  ("i", ...)                                the indexes: nebula's keys and per-entry values
A chunk closes at the chunk target (formats/pertype_chunk.py); the target is in the store dir name, not fingerprinted.
Each edge of a prop-carrying edge type has an ordinal (load order; an insert takes max+1), stored in both adjacency
directions so either can find its props. Chunk bodies are plain struct / varint bytes; compression is the block
codec's job, and encode/decode_value are the identity.

Reads price the cheapest plan the layout allows: a lookup is 1 SEEK (seek_for_prev on the chunk key) plus the whole
chunk value; a type scan is 1 SEEK + 1 NEXT per chunk; a chunk already read in the same operator call is free. Writes
are a chunk read-modify-write; a vertex delete does one per chunk that holds a neighbor's reverse entry. Decode time
goes to `decode_us` (a diagnostic, not KVCost).
"""
import os
import struct
import time
from functools import wraps

from kvevaluator.formats.base import Format, neighbor_item, _acc
from kvevaluator.formats.nebula import NebulaFormat
from kvevaluator.cost import KVCost
from kvevaluator.kvstore import valcodec
from kvevaluator.formats.pertype_chunk import pertype_chunk
from kvevaluator.schema import vids as vids_mod
from kvevaluator.schema.vids import uid_of, key_id, without_key_id

def _delta_uvarints(sorted_ints):
    """Ascending ints -> concatenated delta uvarints (first delta is from 0)."""
    out = bytearray()
    prev = 0
    for u in sorted_ints:
        out += valcodec.uvarint(u - prev)
        prev = u
    return bytes(out)

def _str_column(cells):
    """Present str cells -> u32 END-offset array + concatenated UTF-8 (a bytes cell: its raw bytes)."""
    offs, blob = bytearray(), bytearray()
    for s in cells:
        blob += s.encode("utf-8") if isinstance(s, str) else bytes(s)
        offs += struct.pack(">I", len(blob))
    return bytes(offs) + bytes(blob)

def _multi_column(cells):
    """Present list-of-str cells -> u32 END-offset array + per-list blobs
    (uvarint count + len-prefixed elems)."""
    offs, blob = bytearray(), bytearray()
    for lst in cells:
        b = valcodec.uvarint(len(lst))
        for e in lst:
            u = e.encode("utf-8")
            b += valcodec.uvarint(len(u)) + u
        blob += b
        offs += struct.pack(">I", len(blob))
    return bytes(offs) + bytes(blob)

def _prop_columns(cols, rows):
    """Columnar body for `rows` (list of prop dicts) under the declared `cols`
    [(name, typ, multi)]: per column an absent-bitmap then the packed cells."""
    out = bytearray()
    n = len(rows)
    for name, typ, multi in cols:
        bitmap = bytearray((n + 7) // 8)
        cells = []
        for i, r in enumerate(rows):
            v = r.get(name)
            if v is None:                       # absent or explicit None: not stored
                bitmap[i // 8] |= 1 << (i % 8)
            else:
                cells.append(v)
        out += bytes(bitmap)
        if multi:
            out += _multi_column(cells)
        elif typ in ("str", "bytes"):
            if any(isinstance(c, (bytes, bytearray)) != (typ == "bytes") for c in cells):
                raise ValueError(f"{name}: a {typ} column holds another kind (an input written before the bytes type?)")
            out += _str_column(cells)
        else:                                   # int / date
            for v in cells:
                out += struct.pack(">q", v)
    return bytes(out)

def _est_props(props):
    """Rough serialized size of one prop row, for chunk targeting only (the real
    size is whatever the chunk serializer produces)."""
    n = 8                                       # the vid / row overhead ballpark
    for v in props.values():
        if isinstance(v, str):                   # a string's UTF-8 bytes, as stored (not its characters; as C++ est_props)
            n += 4 + len(v.encode("utf-8"))
        elif isinstance(v, bytes):               # a blob's raw length
            n += 4 + len(v)
        elif isinstance(v, list):
            n += 2 + sum(1 + len(e.encode("utf-8")) for e in v)
        else:
            n += 8
    return n

BULK_MIN = 16           # rows requested from one chunk before a lazy view bulk-decodes it

def _hex(u):
    return format(u, "016x")

def _read_deltas(b, i, n):
    vals, cur = [], 0
    for _ in range(n):
        d, i = valcodec.read_uvarint(b, i)
        cur += d
        vals.append(cur)
    return vals, i

def _decode_columns(cols, n, b, i):
    """Decoder twin of _prop_columns -> (rows, next index)."""
    rows = [{} for _ in range(n)]
    for name, typ, multi in cols:
        nb = (n + 7) // 8
        bm = b[i:i + nb]; i += nb
        present = [r for r in range(n) if not (bm[r // 8] >> (r % 8)) & 1]
        k = len(present)
        if multi or typ in ("str", "bytes"):
            offs = [struct.unpack(">I", b[i + 4 * j:i + 4 * j + 4])[0] for j in range(k)]
            i += 4 * k
            end = i + (offs[-1] if offs else 0)
            start = 0
            for j, r in enumerate(present):
                sub = b[i + start:i + offs[j]]
                if multi:
                    cnt, p = valcodec.read_uvarint(sub, 0)
                    lst = []
                    for _ in range(cnt):
                        L, p = valcodec.read_uvarint(sub, p)
                        lst.append(sub[p:p + L].decode("utf-8")); p += L
                    rows[r][name] = lst
                else:
                    rows[r][name] = bytes(sub) if typ == "bytes" else sub.decode("utf-8")
                start = offs[j]
            i = end
        else:
            for r in present:
                rows[r][name] = struct.unpack(">q", b[i:i + 8])[0]; i += 8
    return rows, i

def _timed(fn):
    """Add the decoder's wall time to fmt.decode_us."""
    @wraps(fn)
    def wrapper(self, *a, **k):
        t = time.perf_counter()
        try:
            return fn(self, *a, **k)
        finally:
            self.decode_us += (time.perf_counter() - t) * 1e6
    return wrapper

class _Dels(list):
    """A write's deletes, plus `late`: the keys base._apply_writes deletes AFTER the puts (see _rmw)."""
    late = ()

class _ChunkReader:
    """Per-operator-call chunk access: seek_for_prev to the chunk whose key <= the
    id, read + decode it ONCE, remember its [first, last] id range. A later id inside
    a decoded range costs 0 KV ops; an id past the last chunk seeks (1 SEEK) but does
    not re-read the value. `decode(raw) -> (payload, first_id, last_id)`."""

    def __init__(self, store):
        self.store, self.cache, self.S = store, {}, 0
        self.put_keys, self.late_dels = set(), []      # a write's chunk puts so far; see _rmw

    on_chunk = None     # set by the format: called with the family prefix of every chunk value read (count caveat)

    def find(self, prefix, hexid, decode):
        return self.locate(prefix, hexid, decode)[1]

    def locate(self, prefix, hexid, decode):
        """(chunk key, payload) -- (None, None) when no chunk of `prefix` is at or before hexid."""
        n = len(prefix)
        for key, (lo, hi, payload) in self.cache.items():
            if key[:n] == prefix and lo <= hexid <= hi:
                return key, payload
        cur = self.store.seek_for_prev(prefix + (hexid,))
        self.S += 1
        if not cur.valid() or cur.key()[:n] != prefix:
            # a miss: the SEEK, no chunk; the off-family landing key (if any) is charged to the
            # miss lane (an absence miss), never to read
            cur.mark_miss()
            return None, None
        key = cur.key()
        if key in self.cache:                              # re-landing: the SEEK, no re-read
            return key, self.cache[key][2]
        raw = cur.value()
        if self.on_chunk is not None:
            self.on_chunk(prefix)
        payload, lo, hi = decode(raw)                      # the whole chunk, charged
        self.cache[key] = (lo, hi, payload)
        return key, payload

class PerTypeCsrFormat(Format):
    name = "pertype_csr"

    def __init__(self, schema, sizing=None, pack=False):
        super().__init__(schema)
        self.chunk_target = pertype_chunk()   # the C++ store's default (64K) unless the env names another
        # the C++ store keeps each adjacency entry's far-end label (Config::far_label, on by default; the arm
        # KVEVAL_PERTYPE_FAR_LABEL=off). It sizes the label block (_far_bits); this twin's chunks hold no label block
        self.stores_far_label = pertype_far_label() == "on"
        # Index KEYS are nebula's, byte-for-byte -- composition, so the two can never drift on
        # that axis (fingerprint: _LAYOUT_SOURCES lists nebula.py alongside this file). Index
        # VALUES are a chunked PostingIndex in C++ (dgraph's posting shape, not
        # nebula's per-entry one); this twin keeps nebula's per-entry index values.
        self._neb = NebulaFormat(schema)
        # ingest buffers, emitted + cleared by finalize_write
        self._verts = {}        # label -> [(uid, props)]
        self._adj = {}          # (label, etype, dir) -> {src_uid: [dst_uid]}
        self._eprops = {}       # (label, etype) -> {(src_uid, dst_uid): props}
        # entity/edge tallies for the driver's cross-arm count check (chunk keys
        # can't be counted per entity, so the format reports what it ingested)
        self.ingest_counts = {"vertices": 0, "edges": 0}
        self.decode_us = 0.0    # time inside the chunk decoders; a caller resets it per cell
        self._count_caveat = None   # set by a read whose counts are not the C++ format's (take_count_caveat)
        # vid tag -> the labels whose rows live under that tag (Message = Post | Comment),
        # for reads that get no label: the tag is in the vid, the type only in storage.
        self._tag_labels = {}
        for label in schema.vertex_prop_types:
            self._tag_labels.setdefault(vids_mod._TAG[vids_mod.group_of(label)], []).append(label)

    # --- byte model: the modeled byte count is the physical byte count here -------------
    def value_bytes(self, v, key=None):
        if v is None:
            return 0
        if isinstance(v, (bytes, bytearray)):
            return len(v)
        raise ValueError(f"pertype_csr value_bytes: only chunk bytes / None are stored, "
                         f"got {type(v).__name__}")

    def key_bytes(self, key):
        # The delegated index keys use nebula's key model (same tuples, same modeled
        # bytes); chunk keys use the base default, there is no structural model for them.
        if key and key[0] == "i":
            return self._neb.key_bytes(key)
        return super().key_bytes(key)

    def encode_value(self, key, value):
        if value is None:
            return b""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise ValueError(f"pertype_csr codec: only chunk bytes are stored, got {type(value).__name__}")

    def decode_value(self, key, raw):
        # Identity, not NotImplementedError: kvsize/store_size read values through the
        # cursor, and the decoded form of a size-only chunk IS its bytes.
        return bytes(raw) if raw else None

    def _far_bits(self, etype, dir):
        """the label block's width for a list of (etype, dir), as the C++ store writes it (format.h far_bits):
        ceil(log2 k) when the schema gives the far end k > 1 labels and the store keeps far labels, else 0"""
        if not self.stores_far_label:
            return 0
        k = len({e[1] if dir == "out" else e[0] for e in self.schema.edge_endpoints.get(etype, ())})
        return (k - 1).bit_length() if k > 1 else 0

    def _owners(self, etype, dir):
        """the labels that can hold a list of (etype, dir), in the schema's vertex order: what a vid with no label on
        its row probes, in order (C++ owners_of)"""
        ends = set(self._end_labels(etype, dir))
        return [l for l in self.schema.vertex_prop_types if l in ends]

    # ----------------------
    # --- STORAGE WRITES ---
    # ----------------------

    def add_vertex(self, store, vid, label, props):
        u = uid_of(vid)
        if u is None:
            raise ValueError(f"pertype_csr: vertex vid {vid!r} is not an encoded uid")
        self._verts.setdefault(label, []).append((u, without_key_id(props)))
        self.ingest_counts["vertices"] += 1
        # indexes: exactly nebula's writes (shape AND trigger), so the index
        # component is identical across formats
        if label not in self._neb._prop_indexed:
            store.put(("i", label, "", "", vid), None)
        for p, val in props.items():
            if self.schema.is_indexed(label, p):
                store.put(("i", label, p, str(val), vid), None)
            elif (label, p) in self.schema.date_cols:
                store.put(("i", label, p, f"{val:013d}", vid), None)

    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        su, du = uid_of(src), uid_of(dst)
        if su is None or du is None:
            raise ValueError(f"pertype_csr: edge endpoint {src!r}/{dst!r} is not an encoded uid")
        if src_label is None or dst_label is None:
            raise ValueError("pertype_csr: add_edge needs src_label and dst_label (chunks are per type)")
        self._adj.setdefault((src_label, etype, "out"), {}).setdefault(su, []).append(du)
        self._adj.setdefault((dst_label, etype, "in"), {}).setdefault(du, []).append(su)
        self.ingest_counts["edges"] += 1
        if props and self.schema.edge_prop_types.get(etype):
            self._eprops.setdefault((src_label, etype), {})[(su, du)] = dict(props)

    def _emit_chunks(self, store, key_of, items, est, serialize):
        """Greedy chunker: append `items` (already sorted) until the estimate
        reaches the target, then serialize + put under key_of(first item)."""
        buf, size = [], 0
        for it in items:
            buf.append(it)
            size += est(it)
            if size >= self.chunk_target:
                store.put(key_of(buf[0]), serialize(buf))
                buf, size = [], 0
        if buf:
            store.put(key_of(buf[0]), serialize(buf))

    def _serialize_props(self, cols, rows):
        """[(uid, props)] (uid-sorted) -> one props chunk."""
        body = valcodec.uvarint(len(rows))
        body += _delta_uvarints([u for u, _ in rows])
        return body + _prop_columns(cols, [p for _, p in rows])

    def _serialize_adj(self, groups, with_ordinals):
        """[(src_uid, sorted [dst_uid], [ordinal per dst] | None)] (src-sorted) -> one
        CSR chunk; ordinals are written iff with_ordinals (a prop-carrying etype)."""
        body = valcodec.uvarint(len(groups))
        body += _delta_uvarints([s for s, _, _ in groups])
        for _, dsts, _ in groups:
            body += valcodec.uvarint(len(dsts))
        offs, nbr = bytearray(), bytearray()
        for _, dsts, _ in groups:
            nbr += _delta_uvarints(dsts)
            offs += struct.pack(">I", len(nbr))
        tail = bytearray()
        if with_ordinals:
            for _, _, ords in groups:
                for o in ords:
                    tail += valcodec.uvarint(o)
        return body + bytes(offs) + bytes(nbr) + bytes(tail)

    def _serialize_eprops(self, cols, rows):
        """[(ordinal, props)] (ordinal-sorted) -> one edge-props chunk."""
        body = valcodec.uvarint(len(rows)) + _delta_uvarints([o for o, _ in rows])
        return body + _prop_columns(cols, [p for _, p in rows])

    def _vcols(self, label):
        return [(p, t, (label, p) in self.schema.multival_cols)     # declared props only: id is key-derived
                for p, t in self.schema.vertex_prop_types[label]]

    def _ecols(self, etype):
        return [(p, t, False) for p, t in self.schema.edge_prop_types.get(etype, ())]

    def _has_eprops(self, etype):
        return bool(self.schema.edge_prop_types.get(etype))

    def finalize_write(self, store):
        for label in sorted(self._verts):
            rows = sorted(self._verts[label], key=lambda r: r[0])
            cols = self._vcols(label)
            self._emit_chunks(
                store, lambda first, l=label: ("t", l, "p", _hex(first[0])), rows,
                lambda r: 8 + _est_props(r[1]),
                lambda buf: self._serialize_props(cols, buf))
        # Out-edge ordinals per (label, etype): position in the src-sorted, dst-sorted
        # out order. Numbered before any chunk is emitted so reverse chunks and ep
        # chunks can refer to them.
        ordinal = {}                       # (label, etype) -> {(src, dst): ordinal}
        for (label, etype, dir) in sorted(self._adj):
            if dir != "out":
                continue
            m, k = {}, 0
            for s, ds in sorted(self._adj[(label, etype, dir)].items()):
                for d in sorted(ds):
                    m[(s, d)] = k; k += 1
            ordinal[(label, etype)] = m
        for (label, etype, dir) in sorted(self._adj):
            with_ords = self._has_eprops(etype)
            groups = []
            for s, ds in sorted(self._adj[(label, etype, dir)].items()):
                ds = sorted(ds)
                ords = None
                if with_ords:
                    # out: this label's numbering; in: the out-edge (d -> s) lives under
                    # d's label, and d's tag names its group
                    ords = ([ordinal[(label, etype)][(s, d)] for d in ds] if dir == "out"
                            else [self._out_ordinal(ordinal, d, etype, s) for d in ds])
                groups.append((s, ds, ords))
            self._emit_chunks(
                store, lambda first, l=label, e=etype, d=dir: ("t", l, "a", e, d, _hex(first[0])),
                groups, lambda g: 9 + 5 * len(g[1]) + (3 * len(g[1]) if with_ords else 0),
                lambda buf, w=with_ords: self._serialize_adj(buf, w))
        for (label, etype) in sorted(self._eprops):
            by_edge = self._eprops[(label, etype)]
            rows = sorted((ordinal[(label, etype)][k], v) for k, v in by_edge.items())
            cols = self._ecols(etype)
            self._emit_chunks(
                store, lambda first, l=label, e=etype: ("t", l, "ep", e, f"{first[0]:012d}"), rows,
                lambda r: 2 + _est_props(r[1]),
                lambda buf: self._serialize_eprops(cols, buf))
        self._verts, self._adj, self._eprops = {}, {}, {}

    def _out_ordinal(self, ordinal, src, etype, dst):
        for lab in self._tag_labels[src >> vids_mod.SHIFT]:
            m = ordinal.get((lab, etype))
            if m is not None and (src, dst) in m:
                return m[(src, dst)]
        raise ValueError(f"pertype_csr: no out-edge {src:x} -{etype}-> {dst:x} to number")

    # --- decoders (each timed into decode_us) ---

    @_timed
    def _props_decode(self, cols, raw):
        n, i = valcodec.read_uvarint(raw, 0)
        uids, i = _read_deltas(raw, i, n)
        rows, _ = _decode_columns(cols, n, raw, i)
        return uids, rows

    def _props_payload(self, cols, raw):
        """props chunk -> ({uid_hex: row}, first_hex, last_hex). The delta format has no
        random access, so the whole chunk is decoded once per operator call. Only the
        decode is timed; turning ids into vid strings is the transport's business (the
        baselines read vids off keys untimed)."""
        uids, rows = self._props_decode(cols, raw)
        keys = [_hex(u) for u in uids]
        return dict(zip(keys, rows)), keys[0], keys[-1]

    @_timed
    def _adj_decode(self, raw, with_ordinals):
        """adj chunk -> [(src_int, [dst_int], [ordinal] | None)] in chunk order."""
        n, i = valcodec.read_uvarint(raw, 0)
        srcs, i = _read_deltas(raw, i, n)
        degs = []
        for _ in range(n):
            d, i = valcodec.read_uvarint(raw, i); degs.append(d)
        offs = [struct.unpack(">I", raw[i + 4 * j:i + 4 * j + 4])[0] for j in range(n)]
        i += 4 * n
        area, start, lists = i, 0, []
        for d, o in zip(degs, offs):
            ds, _ = _read_deltas(raw, area + start, d)
            lists.append(ds); start = o
        i = area + start
        groups = []
        for s, ds in zip(srcs, lists):
            ords = None
            if with_ordinals:
                ords = []
                for _ in ds:
                    o, i = valcodec.read_uvarint(raw, i); ords.append(o)
            groups.append((s, ds, ords))
        return groups

    def _adj_payload(self, raw, with_ordinals):
        """adj chunk -> ({src_hex: (dst_hex list, ordinal list | None)}, first_hex, last_hex)."""
        groups = self._adj_decode(raw, with_ordinals)
        payload = {_hex(s): ([_hex(d) for d in ds], ords) for s, ds, ords in groups}
        return payload, _hex(groups[0][0]), _hex(groups[-1][0])

    # --- RMW decoders: the chunk as the mutable structure its serializer takes ---
    @_timed
    def _props_rows(self, cols, raw):
        n, i = valcodec.read_uvarint(raw, 0)
        uids, i = _read_deltas(raw, i, n)
        rows, _ = _decode_columns(cols, n, raw, i)
        return list(zip(uids, rows)), _hex(uids[0]), _hex(uids[-1])

    @_timed
    def _adj_groups(self, raw, with_ordinals):
        groups = self._adj_decode(raw, with_ordinals)
        return groups, _hex(groups[0][0]), _hex(groups[-1][0])

    @_timed
    def _ep_rows(self, cols, raw):
        n, i = valcodec.read_uvarint(raw, 0)
        ords, i = _read_deltas(raw, i, n)
        rows, _ = _decode_columns(cols, n, raw, i)
        return list(zip(ords, rows)), f"{ords[0]:012d}", f"{ords[-1]:012d}"

    @_timed
    def _ep_decode(self, cols, raw):
        n, i = valcodec.read_uvarint(raw, 0)
        ords, i = _read_deltas(raw, i, n)
        rows, _ = _decode_columns(cols, n, raw, i)
        return ords, rows

    def _ep_payload(self, cols, raw):
        """ep chunk -> ({ordinal_12d: row}, first_12d, last_12d)."""
        ords, rows = self._ep_decode(cols, raw)
        keys = [f"{o:012d}" for o in ords]
        return dict(zip(keys, rows)), keys[0], keys[-1]

    # ---------------------
    # --- STORAGE READS ---
    # ---------------------
    # Every operator: the cheapest plan the LAYOUT allows. A chunk lookup is
    # seek_for_prev on the chunk key (1 SEEK) and the whole chunk value (charged by
    # CountingKV as scan value bytes); a chunk decoded earlier in the same call is free.

    def _one_owner(self, etype, dir):
        """the ONE owner label of a list of (etype, dir), when that label is also alone in its id group; else None.
        For a source of that group the id's group and the C++ rule (a carried label, or the schema's owners) open the
        same single family"""
        own = self._owners(etype, dir)
        if len(own) == 1 and len(self._tag_labels[vids_mod._TAG[vids_mod.group_of(own[0])]]) == 1:
            return own[0]
        return None

    def _labels_for(self, vid, label, one_owner=None):
        """the labels this twin tries for vid, in order. Marks the step (count caveat) unless the C++ read is sure
        to open the same families: ONE label named and vid's id group holds only that kind of vertex (a row of
        another label, which C++ would not read at all, cannot be in the set), or a hop whose list has one owner
        (`one_owner`) and vid is of that owner's group"""
        u = uid_of(vid)
        group = tuple(self._tag_labels.get(u >> vids_mod.SHIFT, ())) if u is not None else ()   # (): no such id group here
        named = (label,) if isinstance(label, str) else (tuple(label) if label is not None else None)
        if named is not None and len(named) == 1:
            if group != named:                 # another kind of vertex can be in the set (or the group is unknown)
                self._caveat(self._CAVEAT_LABEL)
            return named
        if not (named is None and one_owner is not None and group == (one_owner,)):
            self._caveat(self._CAVEAT_LABEL)
        return named if named is not None else group

    def _row_lookup(self, rd, v, label):
        """(row or None) for vid v under the candidate label(s): one chunk lookup per label
        tried, the first hit wins (a message of unknown type may cost a miss first)."""
        for lab in self._labels_for(v, label):
            cols = self._vcols(lab)
            chunk = rd.find(("t", lab, "p"), v, lambda b, c=cols: self._props_payload(c, b))
            if chunk is not None:
                row = chunk.get(v)
                if row is not None:
                    return row
        return None

    def read_vertex_by_id(self, store, vid, label=None):
        # The vid IS the id (same convention as dgraph's SIM anchor): 0 ops. Existence
        # and the concrete type are resolved by the read that follows.
        cands = (vid,) if isinstance(vid, str) else tuple(vid)
        return list(cands), KVCost()

    def read_vertex_props(self, store, vids, props, label=None):
        rd = self._reader(store)
        row_props = [p for p in props if p != "id"]
        distinct = list(dict.fromkeys(vids))
        if row_props:
            # Locate every vid's chunk first (the SEEKs), then let a chunk that serves many
            # of them switch to a bulk decode (a decoder with element access would decode a few rows
            # one by one and hundreds in bulk).
            hits = {}
            for v in distinct:
                for lab in self._labels_for(v, label):
                    cols = self._vcols(lab)
                    key, chunk = rd.locate(("t", lab, "p"), v, lambda b, c=cols: self._props_payload(c, b))
                    if chunk is not None and v in chunk:
                        hits[v] = (key, chunk)
                        break
            per_chunk = {}
            for key, chunk in hits.values():
                per_chunk[key] = per_chunk.get(key, 0) + 1
            for key, chunk in hits.values():
                if per_chunk[key] >= BULK_MIN and hasattr(chunk, "bulk"):
                    chunk.bulk()
        cache = {}
        for v in distinct:
            row = {"_vid": v}
            if "id" in props:
                row["id"] = key_id(v)                        # key-derived, free
            if row_props:
                found = hits[v][1].get(v) if v in hits else {}
                for p in row_props:
                    row[p] = (found or {}).get(p)
            cache[v] = row
        return [dict(cache[v]) for v in vids], KVCost(S=rd.S)

    def read_anchor_props(self, store, cands, props, labels=None):
        rd = self._reader(store)
        for cand in cands:
            row = self._row_lookup(rd, cand, labels)
            if row is not None:
                return [{"_vid": cand, **{p: key_id(cand) if p == "id" else row.get(p) for p in props}}], KVCost(S=rd.S)   # id: the key
        return [], KVCost(S=rd.S)

    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False, _rd=None):
        # `_rd`: the caller's chunk reader, when several reads of one step share their chunks (read_edge_exists);
        # the cost returned is then this call's own SEEKs
        rd = _rd if _rd is not None else self._reader(store)
        s0 = rd.S
        dirs = {"out": ("out",), "in": ("in",), "both": ("out", "in")}[dir]
        has_ep = self._has_eprops(etype)
        want_ep = bool(edge_props) and has_ep
        ecols = self._ecols(etype)
        out = []
        for s in dict.fromkeys(srcs):
            for d in dirs:
                ends = self._end_labels(etype, d)
                for lab in self._labels_for(s, None, self._one_owner(etype, d)):
                    if lab not in ends:
                        continue                             # schema: no such structure, no probe
                    chunk = rd.find(("t", lab, "a", etype, d), s,
                                    lambda b: self._adj_payload(b, has_ep))
                    if chunk is None or s not in chunk:
                        continue
                    dsts, ords = chunk[s]
                    for j, dst in enumerate(dsts):
                        props = None
                        if want_ep:
                            props = self._edge_props(rd, s if d == "out" else dst, etype,
                                                     ords[j], ecols)
                        out.append(neighbor_item(s, dst, props, edge_props, pairs))
                    break                                    # found under this label
        return out, KVCost(S=rd.S - s0)

    def _edge_props(self, rd, out_src, etype, ordinal, ecols):
        """The props of out-edge #ordinal of (out_src's label, etype): one ep-chunk lookup."""
        key = f"{ordinal:012d}"
        srcs = self._end_labels(etype, "out")
        for lab in self._labels_for(out_src, None, self._one_owner(etype, "out")):
            if lab not in srcs:
                continue                                     # this label never sources the edge
            ep = rd.find(("t", lab, "ep", etype), key, lambda b, c=ecols: self._ep_payload(c, b))
            if ep is not None and key in ep:
                return ep[key]
        return {}

    def read_index_eq(self, store, label, prop, value):
        # CONCRETE data path only: this Python twin's store still holds nebula's physical
        # per-entry index value (the posting VALUE encoding is C++-only, "the Python twin
        # keeps the old index by design"), so the read and its RETURNED rows are correct via
        # delegation; its counts are not the C++ format's (the caveat refuses them).
        self._caveat(self._CAVEAT_INDEX)
        return self._neb.read_index_eq(store, label, prop, value)

    def read_index_range(self, store, labels, prop, lo, hi):
        self._caveat(self._CAVEAT_INDEX)
        # CONCRETE data path only -- see read_index_eq. The measured sf3 counts stay CLOSE to
        # nebula's per-entry walk even under the posting index: microbench's entry_range (near-unique
        # values) is N == rows on all 3 params; ic3's date-window walk measures N 0.3-2.6% BELOW rows
        # (duplicate ms timestamps collapse into one posting term). A dense-value range
        # (e.g. a day-granular date index) would collapse much further under the posting index.
        return self._neb.read_index_range(store, labels, prop, lo, hi)

    def read_vertices_by_label(self, store, label, props=None, keep=None):
        # The layout's NATIVE type enumeration: the label's chunks are one contiguous key
        # range, so walking them is 1 SEEK + 1 NEXT per CHUNK (not per vertex), and the vids
        # come out of each chunk's id column -- at the price of every chunk's bytes. pertype_csr
        # also carries nebula's type/covering index (identical bytes),
        # which enumerates at 1 NEXT per vertex over ~10x fewer bytes; the chunk scan is the
        # op-cheapest plan the layout allows, so it is the one priced here (the byte-cheaper
        # index path is what read_filter_by_label_probe's "index" via still uses).
        # `props` fuses the projection into the same pass (the chunk is already in hand; a
        # scan-then-read plan would re-read every chunk). Returns rows when props is given,
        # else vids. `keep` (a vid set, with props): rows of other vertices are not built; the set
        # is tested in memory at no cost, every chunk is still crossed and counted.
        if keep is not None and props is None:
            raise ValueError("pertype_csr read_vertices_by_label: keep needs props (the scan of the label's rows)")
        kept = None if keep is None else set(keep)
        prefix = ("t", label, "p")
        cols = self._vcols(label)
        out, n = [], 0
        row_props = [p for p in (props or ()) if p != "id"]
        cur = store.seek(prefix)
        while cur.valid() and cur.key()[:3] == prefix:
            raw = cur.value()
            if props is None:
                out += self._chunk_ids(cols, raw)
            else:
                for v, r in self._chunk_rows_all(cols, raw):
                    if kept is not None and v not in kept:
                        continue
                    row = {"_vid": v}
                    if "id" in props:
                        row["id"] = key_id(v)
                    for p in row_props:
                        row[p] = r.get(p)
                    out.append(row)
            cur.next()
            n += 1
        cur.mark_miss()                                # the landing that ended the scan: an absence miss
        return out, KVCost(S=1, N=n)

    @_timed
    def _chunk_ids_int(self, cols, raw):
        n, i = valcodec.read_uvarint(raw, 0)
        uids, _ = _read_deltas(raw, i, n)
        return uids

    def _chunk_ids(self, cols, raw):
        """The id column of a props chunk, as hex vids (pertype_csr: the delta column)."""
        return [_hex(u) for u in self._chunk_ids_int(cols, raw)]

    def _chunk_rows_all(self, cols, raw):
        """Every row of a props chunk, bulk-decoded -> [(uid_hex, row)] (the scan path)."""
        uids, rows = self._props_decode(cols, raw)
        return [(_hex(u), r) for u, r in zip(uids, rows)]

    @classmethod
    def pack_target(cls, schema, chunk=None):
        # rows are packed into chunks that close at ~the store's chunk target (pertype_csr/format.cc finalize). None
        # here means the caller passed no chunk (a chunk-blind pick), NOT "unpacked": C++ always knows its store's.
        return chunk

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """both directions of every edge type, each a chunked list"""
        return {"reverse_adjacency", "adjacency_lists"}

    @classmethod
    def capabilities(cls, schema):
        # The LAYOUT's contract (what the C++ store of record grants; kvevaluator
        # formats/pertype_csr): a per-type index, a sorted date index, whole rows in one chunk
        # read, a label scan that projects props in the same pass (the fused
        # read_vertices_by_label); and the type membership as ONE posting when every label declares the type
        # index (dgraph's rule). This Python twin still runs the sorted-keys index, so under a
        # single-posting variant it enumerates the whole type: same rows, its own counts (the
        # accepted count divergence; tests/unit/check_caps_parity.py pins the caps to the C++ side).
        caps = {"sorted_range_index", "per_type_index", "type_index", "whole_row_read",
                "label_scan_carries_props", "label_rows_one_run", "whole_label_scan_by_default", "label_scoped_props"}
        if schema.type_indexed_labels and schema.type_indexed_labels >= set(schema.vertex_props):
            caps.add("type_index_single_posting")
        return caps

    def read_filter_by_label_index(self, store, vids, label):
        # CONCRETE data path only -- see read_index_eq's note. The RESULT (which vids survive)
        # is correct via nebula's physical scan; the C++ posting-index shape reads 1 SEEK, not
        # 1 SEEK + M NEXT.
        if not vids:
            return [], KVCost()
        # the C++ read walks no index when every candidate carries its label; and the index layouts differ
        self._caveat(self._CAVEAT_LABEL)
        members, cost = self._neb.read_vertices_by_label(store, label)
        keep = set(members)
        return [v for v in vids if v in keep], cost

    def read_filter_by_label_probe(self, store, vids, label):
        # The per-candidate probe: one chunk lookup per vid (1 SEEK per DISTINCT chunk touched,
        # the chunk's bytes once) -- the layout's own membership test.
        if not vids:
            return [], KVCost()
        self._caveat(self._CAVEAT_LABEL)       # the C++ read looks nothing up for a candidate that carries its label
        rd = self._reader(store)
        labels = (label,) if isinstance(label, str) else tuple(label)
        keep = {v: self._row_lookup(rd, v, labels) is not None for v in dict.fromkeys(vids)}
        return [v for v in vids if keep[v]], KVCost(S=rd.S)

    def read_top_k_by_index(self, store, vids, prop, k, order="desc", hi=None,
                            labels=None, props=None, id_order="asc"):
        """The delegated per-tag SORTED date index walk (nebula's, verbatim: the index
        is identical), then the winners' rows read through the chunks (1 chunk lookup
        per winner's tag/chunk) instead of nebula's 1 GET per row."""
        assert order == "desc", "the LDBC shape; asc would walk forward"
        self._caveat(self._CAVEAT_INDEX)
        labels = (labels,) if isinstance(labels, str) else tuple(labels)
        cand = None if vids is None else set(vids)
        bound = "~" if hi is None else f"{hi:013d}"
        cursors, S, N = [], 0, 0
        for tag in labels:
            cursors.append((tag, store.seek_for_prev(("i", tag, prop, bound))))
            S += 1

        def current(tc):
            tag, cur = tc
            if cur.valid() and cur.key()[:3] == ("i", tag, prop):
                return cur.key()
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
                break
            (tag, cur), key = best
            vid = key[4]
            if cand is None or vid in cand:
                hits.append({"_vid": vid, prop: int(key[3]), prop + "_s": key[3],
                             "id": key_id(vid), "_tag": tag})
            cur.prev()
            N += 1
        for _, cur in cursors:                            # every cursor rests on an unconsumed entry
            cur.mark_miss()                               # (or off its index): an absence miss
        hits.sort(key=lambda r: r["id"], reverse=(id_order == "desc"))
        hits.sort(key=lambda r: r[prop], reverse=True)
        rows = hits[:k]
        rd = self._reader(store)
        for r in rows:
            del r[prop + "_s"]
            tag = r.pop("_tag")
            if props:
                found = self._row_lookup(rd, r["_vid"], tag) or {}
                for p in props:
                    r[p] = key_id(r["_vid"]) if p == "id" else found.get(p)   # id: the key
        return rows, KVCost(S=S + rd.S, N=N)

    def read_edge_exists(self, store, rows, field, etype, into, others=None,
                         via_srcs=None, via_etype=None, reads=None):
        # every read of the step shares ONE chunk reader: a chunk two reads touch (the anchor's list and a
        # candidate's list of one family in one chunk) is read and charged once, as the C++ side's one cache
        # and one landing group. The via walk (another edge type) keeps its own.
        self._step_rd = self._reader(store)
        try:
            return Format.read_edge_exists(self, store, rows, field, etype, into, others=others, via_srcs=via_srcs,
                                           via_etype=via_etype, reads=reads)
        finally:
            self._step_rd = None

    def _edge_read(self, store, run, anchors, todo, etype):
        # One read = the named direction's adjacency of each owner once -- the chunk lookups -- and membership in
        # memory, through the step's shared reader (read_edge_exists above).
        dirs = {r["dir"] for r in run}
        d = "both" if len(dirs) == 2 else dirs.pop()
        # a read marked unless_found reads only what no earlier read found: its keys depend on them, so it is a
        # batch of its own, with its own reader (the once-per-batch rule; the C++ side's own cache)
        rd = self._reader(store) if run[0]["unless_found"] else getattr(self, "_step_rd", None)
        if run[0]["at"] == "anchor":
            nbrs, cost = self.read_neighbors(store, anchors, etype, d, _rd=rd)
            have = set(nbrs)
            return {x for x in todo if x in have}, cost
        nbrs, cost = self.read_neighbors(store, todo, etype, d, pairs=True, _rd=rd)
        aset = set(anchors)
        return {p["src"] for p in nbrs if p["dst"] in aset}, cost

    def _reader(self, store):
        rd = _ChunkReader(store)
        rd.on_chunk = self._chunk_read
        return rd

    # --- the count caveat (base.Format.take_count_caveat): where this twin's counts are not the C++ format's ---
    _CAVEAT_LABEL = ("the Python twin takes a vertex's label from the id's group (or tries the plan's labels in "
                     "order), while the C++ format reads the label the row carries or the entry stores "
                     "(stamp realistic-v5)")
    _CAVEAT_BLOCK = ("the Python twin's adjacency chunk holds no far-label block, while the C++ store's does "
                     "(stamp realistic-v5): the chunk's bytes differ")
    _CAVEAT_INDEX = ("the Python twin's index keeps one key per entry, while the C++ store keeps one posting per "
                     "value (index_layout posting)")
    _CAVEAT_DELETE = ("the Python twin's delete_vertex reads its own lists twice and probes its neighbours' labels, "
                      "while the C++ format reads them once and takes the labels from the stored entries")

    def _caveat(self, why):
        if self._count_caveat is None:
            self._count_caveat = why

    def take_count_caveat(self):
        why, self._count_caveat = self._count_caveat, None
        return why

    def _chunk_read(self, prefix):
        # an adjacency chunk whose family stores far labels in C++: this twin read other bytes
        if prefix[2] == "a" and self._far_bits(prefix[3], prefix[4]):
            self._caveat(self._CAVEAT_BLOCK)

    def _end_labels(self, etype, dir):
        """The labels whose adjacency chunks a `dir` walk over etype probes: the probing side
        of the declared endpoints (out: sources; in: destinations)."""
        ends = self.schema.edge_endpoints.get(etype, ())
        return sorted({e[0] if dir == "out" else e[1] for e in ends})

    _LABEL_RULE_OPS = ("read_vertex_props", "read_filter_by_label_probe", "read_neighbors", "read_edge_exists",
                       "read_chase", "read_closure")

    # ----------------------
    # --- STORAGE WRITES (incremental): chunk read-modify-write ---
    # ----------------------

    def _rmw(self, rd, prefix, hexid, decode, mutate, serialize, keyfn):
        """One chunk RMW: locate (1 SEEK unless already decoded in this call), mutate the
        decoded list in place, re-serialize. Returns (puts, dels): a PUT of the whole
        chunk under the key of its (new) first item, a DELETE of the old key when the
        first item moved or the chunk emptied. No chunk at/before hexid -> a new chunk.
        A dropped key this SAME write already put goes to rd.late_dels (applied after the puts),
        or that earlier put would bring the old chunk back; a later put of the key takes it back
        to the normal deletes. The C++ twin: ChunkWrites in exec/chunk_write.h."""
        key, rows = rd.locate(prefix, hexid, decode)
        if rows is None:
            rows = []
        mutate(rows)
        rows.sort(key=lambda r: r[0])
        puts, dels = [], []
        if rows:
            newkey = prefix + (keyfn(rows[0][0]),)
            if newkey in rd.late_dels:
                rd.late_dels.remove(newkey); dels.append(newkey)
            puts.append((newkey, serialize(rows)))
            if key is not None and key != newkey:
                (rd.late_dels if key in rd.put_keys else dels).append(key)
            rd.put_keys.add(newkey)
        elif key is not None:
            (rd.late_dels if key in rd.put_keys else dels).append(key)
        if key is not None:                         # keep the reader's view current
            rd.cache.pop(key, None)
        if rows:
            rd.cache[prefix + (keyfn(rows[0][0]),)] = (keyfn(rows[0][0]), keyfn(rows[-1][0]), rows)
        return puts, dels

    @staticmethod
    def _written(rd, puts, dels, pred=None):
        """A _w_* result: (puts, dels with the write's late deletes attached, cost with S / P / D)."""
        dels = _Dels(dels)
        dels.late = list(rd.late_dels)
        pred = pred if pred is not None else KVCost()
        pred.S += rd.S; pred.P = len(puts); pred.D = len(dels) + len(dels.late)
        return puts, dels, pred

    def _resolve_label(self, rd, vid, label, etype=None, dir=None):
        """The concrete label of an EXISTING vertex, as the C++ writes resolve it (writes.cc): the hint; else the
        labels the schema gives that side of the edge type -- `dir` = the direction of the vertex's OWN list ("out":
        it is the edge's source) -- one = it, several = the first whose props chunk holds the row (1 SEEK per label
        tried, in the schema's order). Never the id's group."""
        if isinstance(label, str):
            return label
        labels = tuple(label) if label is not None else tuple(self._owners(etype, dir))
        if len(labels) == 1:
            return labels[0]
        for lab in labels:
            cols = self._vcols(lab)
            chunk = rd.find(("t", lab, "p"), vid, lambda b, c=cols: self._props_rows(c, b))
            if chunk is not None and any(_hex(u) == vid for u, _ in chunk):
                return lab
        return labels[0]

    def _next_ordinal(self, rd, label, etype):
        """max ordinal + 1 of (label, etype): the last ep chunk's last row (1 SEEK)."""
        cols = self._ecols(etype)
        last = rd.find(("t", label, "ep", etype), "9" * 12, lambda b, c=cols: self._ep_rows(c, b))
        return (max(o for o, _ in last) + 1) if last else 0

    def _vertex_rmw(self, rd, label, vid, mutate):
        cols = self._vcols(label)
        return self._rmw(rd, ("t", label, "p"), vid, lambda b: self._props_rows(cols, b), mutate,
                         lambda rows: self._serialize_props(cols, rows), _hex)

    def _adj_rmw(self, rd, label, etype, dir, hexid, mutate):
        w = self._has_eprops(etype)
        if self._far_bits(etype, dir):         # the C++ chunk written here also holds the entries' label codes
            self._caveat(self._CAVEAT_BLOCK)
        return self._rmw(rd, ("t", label, "a", etype, dir), hexid, lambda b: self._adj_groups(b, w),
                         mutate, lambda groups: self._serialize_adj(groups, w), _hex)

    def _ep_rmw(self, rd, label, etype, ordinal, mutate):
        cols = self._ecols(etype)
        return self._rmw(rd, ("t", label, "ep", etype), f"{ordinal:012d}",
                         lambda b: self._ep_rows(cols, b), mutate,
                         lambda rows: self._serialize_eprops(cols, rows), lambda o: f"{o:012d}")

    @staticmethod
    def _upsert(rows, key, value):
        for k, r in enumerate(rows):
            if r[0] == key:
                rows[k] = (key, value); return
        rows.append((key, value))

    @staticmethod
    def _adj_add(groups, src, dst, ordinal):
        for k, (s, ds, ords) in enumerate(groups):
            if s == src:
                if dst in ds:
                    return
                pos = sum(1 for d in ds if d < dst)
                ds.insert(pos, dst)
                if ords is not None:
                    ords.insert(pos, ordinal)
                return
        groups.append((src, [dst], [ordinal] if ordinal is not None else None))

    @staticmethod
    def _drop_row(rows, key):
        rows[:] = [r for r in rows if r[0] != key]

    @staticmethod
    def _adj_remove(groups, src, dst):
        """Remove dst from src's list; returns the removed ordinal (or None)."""
        for k, (s, ds, ords) in enumerate(groups):
            if s == src and dst in ds:
                pos = ds.index(dst)
                ds.pop(pos)
                o = ords.pop(pos) if ords is not None else None
                if not ds:
                    groups.pop(k)
                return o
        return None

    def _w_insert_vertex(self, store, vid, label, props):
        if self._neb._vertex_index_pairs(label, props, vid):
            self._caveat(self._CAVEAT_INDEX)   # index entries written: one key each here, a posting rewrite in C++
        rd = self._reader(store)
        u = uid_of(vid)
        puts, dels = self._vertex_rmw(rd, label, vid, lambda rows: self._upsert(rows, u, without_key_id(props)))
        puts += self._neb._vertex_index_pairs(label, props, vid)
        return self._written(rd, puts, dels)

    def _w_update_props(self, store, vid, label, props):
        if self._update_index_cols(label, props):
            self._caveat(self._CAVEAT_INDEX)   # an indexed column moves: one key each here, a posting rewrite in C++
        rd = self._reader(store)
        u, old = uid_of(vid), {}

        def mutate(rows):
            nonlocal old
            for k, r in enumerate(rows):
                if r[0] == u:
                    old = dict(r[1]); rows[k] = (u, {**old, **props}); return
            rows.append((u, dict(props)))
        puts, dels = self._vertex_rmw(rd, label, vid, mutate)   # every changed column, ONE chunk RMW
        # only the changed columns' index entries move, as in nebula (see _update_index_cols)
        new = {**old, **props}
        for p in self._update_index_cols(label, props):
            dels.append(self._neb._index_key(label, p, old.get(p), vid))
            puts.append((self._neb._index_key(label, p, new.get(p), vid), None))
        return self._written(rd, puts, dels)

    def _w_insert_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        rd = self._reader(store)
        sl, dl = self._resolve_label(rd, src, src_label, etype, "out"), self._resolve_label(rd, dst, dst_label, etype, "in")
        su, du = uid_of(src), uid_of(dst)
        ordinal = self._next_ordinal(rd, sl, etype) if self._has_eprops(etype) else None
        puts, dels = self._adj_rmw(rd, sl, etype, "out", src, lambda g: self._adj_add(g, su, du, ordinal))
        p2, d2 = self._adj_rmw(rd, dl, etype, "in", dst, lambda g: self._adj_add(g, du, su, ordinal))
        puts += p2; dels += d2
        if ordinal is not None:
            p3, d3 = self._ep_rmw(rd, sl, etype, ordinal, lambda rows: self._upsert(rows, ordinal, dict(props)))
            puts += p3; dels += d3
        return self._written(rd, puts, dels)

    def _w_delete_edge(self, store, etype, src, dst, src_label=None, dst_label=None):
        rd = self._reader(store)
        sl, dl = self._resolve_label(rd, src, src_label, etype, "out"), self._resolve_label(rd, dst, dst_label, etype, "in")
        su, du = uid_of(src), uid_of(dst)
        removed = []
        puts, dels = self._adj_rmw(rd, sl, etype, "out", src, lambda g: removed.append(self._adj_remove(g, su, du)))
        p2, d2 = self._adj_rmw(rd, dl, etype, "in", dst, lambda g: self._adj_remove(g, du, su))
        puts += p2; dels += d2
        if self._has_eprops(etype) and removed and removed[0] is not None:
            o = removed[0]
            p3, d3 = self._ep_rmw(rd, sl, etype, o, lambda rows: self._drop_row(rows, o))
            puts += p3; dels += d3
        return self._written(rd, puts, dels)

    def _w_update_edge_prop(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        # The edge props live in the edge-prop chunk at the edge's ORDINAL; the ordinal lives in
        # src's out-adjacency chunk. So: locate the adjacency chunk (a counted read, as insert/delete
        # do), find the ordinal, and RMW the edge-prop chunk at it. The adjacency chunks do not change.
        if not self._has_eprops(etype):
            raise ValueError(f"update_edge_prop: {etype!r} declares no edge properties")
        rd = self._reader(store)
        sl = self._resolve_label(rd, src, src_label, etype, "out")
        su, du = uid_of(src), uid_of(dst)
        _key, groups = rd.locate(("t", sl, "a", etype, "out"), src, lambda b: self._adj_groups(b, True))
        ordinal = None
        for s, ds, ords in groups or []:
            if s == su and du in ds:
                ordinal = ords[ds.index(du)]
                break
        if ordinal is None:
            return [], [], KVCost(S=rd.S)                   # absent edge: the locate is the whole cost

        def mutate(rows):
            for k, r in enumerate(rows):
                if r[0] == ordinal:
                    rows[k] = (ordinal, {**r[1], **props}); return
            rows.append((ordinal, dict(props)))
        puts, dels = self._ep_rmw(rd, sl, etype, ordinal, mutate)
        return self._written(rd, puts, dels)

    def _w_delete_vertex(self, store, vid, label, with_edges=True):
        self._caveat(self._CAVEAT_DELETE)      # the whole step, until this twin reads its own lists once
        rd = self._reader(store)
        u, old = uid_of(vid), {}

        def take(rows):
            nonlocal old
            for k, r in enumerate(rows):
                if r[0] == u:
                    old = dict(r[1]); rows.pop(k); return
        puts, dels = self._vertex_rmw(rd, label, vid, take)
        dels += [k for k, _ in self._neb._vertex_index_pairs(label, old, vid)]
        pred = KVCost()
        if with_edges:
            # the edges through the layout's own access path (read_neighbors charges the
            # chunk lookups), then ONE RMW per touched chunk on both sides: neighbors are
            # grouped by the chunk holding their reverse entry before anything is rewritten
            edges, cost, _unreachable = self._incident_edges(store, vid, label)
            _acc(pred, cost)
            for etype, dir, nbrs in edges:
                other = "in" if dir == "out" else "out"
                ords = []
                p1, d1 = self._adj_rmw(rd, label, etype, dir, vid,
                                       lambda g: ords.extend(self._adj_remove(g, u, uid_of(n)) for n in nbrs))
                puts += p1; dels += d1
                w = self._has_eprops(etype)
                by_chunk = {}                       # (label, chunk key) -> [neighbor uid]
                for n in nbrs:
                    nl = self._resolve_label(rd, n, None, etype, other)
                    key, _ = rd.locate(("t", nl, "a", etype, other), n, lambda b: self._adj_groups(b, w))
                    group = (nl, key) if key is not None else (nl, None, n)   # no chunk: on its own
                    by_chunk.setdefault(group, []).append(uid_of(n))
                for (nl, *_rest), nus in by_chunk.items():
                    anchor = _hex(nus[0])
                    p2, d2 = self._adj_rmw(rd, nl, etype, other, anchor,
                                           lambda g, nus=nus: [self._adj_remove(g, nu, u) for nu in nus])
                    puts += p2; dels += d2
                if w:
                    # the ep rows live under the OUT side's label: ours for dir=out, the
                    # neighbor's for dir=in; grouped by ep chunk the same way
                    by_ep = {}
                    for n, o in zip(nbrs, ords):
                        if o is None:
                            continue
                        ol = label if dir == "out" else self._resolve_label(rd, n, None, etype, "out")
                        cols = self._ecols(etype)
                        key, _ = rd.locate(("t", ol, "ep", etype), f"{o:012d}", lambda b, c=cols: self._ep_rows(c, b))
                        by_ep.setdefault((ol, key), []).append(o)
                    for (ol, _key), os_ in by_ep.items():
                        p3, d3 = self._ep_rmw(rd, ol, etype, os_[0],
                                              lambda rows, os_=os_: [self._drop_row(rows, o) for o in os_])
                        puts += p3; dels += d3
        return self._written(rd, puts, dels, pred)


# Per-type CSR's stored FAR-END LABEL (cpp/kvevaluator/src/formats/pertype_csr/format.h Config::far_label): on = each
# adjacency entry stores its far end's label where the schema gives that direction more than one (a block per
# chunk), the default; off = the arm pertype_csr-nofarlabel
# (KVEVAL_PERTYPE_FAR_LABEL=off).
PERTYPE_FAR_LABEL_DEFAULT = "on"


def pertype_far_label():
    v = os.environ.get("KVEVAL_PERTYPE_FAR_LABEL", PERTYPE_FAR_LABEL_DEFAULT)
    if v not in ("on", "off"):
        raise SystemExit(f"KVEVAL_PERTYPE_FAR_LABEL={v!r}: expected on or off")
    return v
