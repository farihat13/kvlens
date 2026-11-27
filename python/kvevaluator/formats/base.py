"""The format base class: a format writes the graph into a store as KV pairs and, per operator, runs the KV ops and
returns a KVCost beside its rows (a write's predicted from the schema, a read's counted over its call).

Ingest streams one record at a time through add_vertex / add_edge / finalize_write (dgraph buffers its posting lists
until finalize_write); materialize() drives them over an in-memory Graph for small data.

Operator conventions:
- read_neighbors `dir` is "out" | "in" | "both"; a format pays for each direction it scans.
- read_neighbors reads srcs as a set: each distinct vid is scanned once and emitted once, in first-occurrence order, as
  the engines do with their frontiers. Plans must not rely on duplicated srcs giving duplicated output.
  read_vertex_props keeps one row per input but also reads each distinct vid once.
- read_neighbors returns a bare dst list; with pairs=True and/or edge_props=[...] it returns row dicts {"src", "dst",
  <edge props>}. Covered-read rule: dgraph and nebula serve edge props from what read_neighbors already read (0 extra
  ops, only bytes); arango's edge index holds only the neighbor id, so an edge prop costs 1 GET per edge.
- A bare `props` always means vertex props; edge props come only through read_neighbors' edge_props (`edge_*`).
- `label` is the vertex label or, for a Message, the candidate list ["Post", "Comment"]; formats that key by label use
  it, dgraph ignores it. read_vertex_by_id also takes a candidate vid list and pays for every miss.
- read_vertex_props rows carry "_vid", charged 0 bytes (bookkeeping, not a read).
- Free ops (filter, dedup, ..., top_k) are 0-KV in-memory steps implemented once here; they stay in the plan so
  later cardinalities are real.
"""
import functools
from abc import ABC, abstractmethod

from kvevaluator.cost import KVCost

def counted(read):
    """A read whose cost is what the store counted over the call (its counter's change), not a prediction. A write that runs
    a read adds this cost to its own prediction. A store without a counter: zero.
    """
    @functools.wraps(read)
    def run(self, store, *args, **kwargs):
        before = store.cost.copy() if hasattr(store, "cost") else None
        rows, _ = read(self, store, *args, **kwargs)
        if before is None:
            return rows, KVCost()
        after = store.cost
        return rows, KVCost(**{k: getattr(after, k) - v for k, v in before.__dict__.items()})
    return run

def _acc(total, c):
    """Accumulate one KVCost into another (read_chase/read_closure sum their inner calls)."""
    total.S += c.S
    total.G += c.G
    total.N += c.N
    total.get_value_bytes += c.get_value_bytes
    total.scan_value_bytes += c.scan_value_bytes
    total.scan_key_bytes += c.scan_key_bytes
    total.get_key_bytes += c.get_key_bytes
    total.P += c.P
    total.D += c.D
    total.write_key_bytes += c.write_key_bytes
    total.write_value_bytes += c.write_value_bytes

def _neutral_key(v):
    """A total order over plan values, the same on every evaluator (C++ value.cc
    neutral_compare): null < bool < int / float < str < bytes < list/tuple, containers element-wise."""
    if v is None:
        return (0,)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):                 # a float (the grammar's div) ranks with int, by value
        return (2, v)
    if isinstance(v, str):
        return (3, v)
    if isinstance(v, (bytes, bytearray)):           # by unsigned bytes, as C++
        return (4, bytes(v))
    if isinstance(v, (list, tuple)):
        return (5, tuple(_neutral_key(x) for x in v))
    return (6, repr(v))

def neighbor_item(src, dst, props, edge_props, pairs):
    """Shape one read_neighbors() output element: bare dst vid by default, a row dict
    when pairs and/or edge_props ask for more (see the module docstring)."""
    if not (pairs or edge_props):
        return dst
    row = {"src": src, "dst": dst} if pairs else {"dst": dst}
    for p in edge_props or ():
        row[p] = (props or {}).get(p)
    return row

def edge_reads(reads):
    """the reads of a read_edge_exists step, checked: [{"dir", "at", "unless_found"}], at least one, the first not
    unless_found (the C++ edge_reads, formats/common.cc)"""
    if not reads:
        raise ValueError("read_edge_exists: the step names no reads (want reads: [{dir: out|in, at: anchor|cand, "
                         "unless_found: bool}, ...])")
    out = []
    for r in reads:
        if (not isinstance(r, dict) or r.get("dir") not in ("out", "in") or r.get("at") not in ("anchor", "cand")
                or set(r) - {"dir", "at", "unless_found"} or not isinstance(r.get("unless_found", False), bool)):
            raise ValueError(f"read_edge_exists: bad read {r!r}")
        out.append({"dir": r["dir"], "at": r["at"], "unless_found": bool(r.get("unless_found", False))})
    if out[0]["unless_found"]:
        raise ValueError("read_edge_exists: the first read cannot be unless_found (nothing was read before it)")
    return out

class Format(ABC):
    name = "base"

    def take_count_caveat(self):
        """Why the step that just ran has counts that are NOT this format's (one clause, for the error text), else
        None; clears it. A Python twin whose reads differ from the C++ format's on some steps records it at the read
        that differs; runner.run_plan then refuses that step's counts (cost.TwinCountsRefused). Default: never."""
        return None

    @classmethod
    def capabilities(cls, schema):
        """The access-path CAPABILITIES this format offers on `schema` (the vocabulary in
        planir/capabilities.py): the code selector a plan bundle's variants are chosen by.
        A list may depend on the physical design (dgraph's type index). Empty = only a
        bundle's universal fallback variant runs."""
        return set()

    @classmethod
    def edge_capabilities_for(cls, schema, etype):
        """The caps of ONE EDGE TYPE's adjacency (planir/capabilities.py: reverse_adjacency, adjacency_lists) -- what a
        variant's `on` naming an edge type is checked against. Mirrors C++ Format::edge_capabilities_for."""
        return set()

    @classmethod
    def capabilities_for(cls, schema, label):
        """The capabilities this format offers on one LABEL (kvevaluator.formats.caps.caps_resolver; a variant's `on`
        checks its caps here). Default: the store-wide set; a format whose caps differ per label overrides it."""
        return cls.capabilities(schema)

    @classmethod
    def pack_target(cls, schema, chunk=None):
        """The bytes one landing on a vertex row decodes when this layout PACKS many rows into a chunk (the store's
        chunk target where it is a pack size), else None: rows are not packed. dgraph's chunk target is a
        SPLIT threshold (only a unit larger than it splits), not a pack, so they keep None. `chunk` = the store's
        chunk target (bytes). Mirrored in C++ Format::pack_target; kvevaluator.formats.caps.store_caps derives from it."""
        return None

    def __init__(self, schema):
        # The dataset's declared facts (kvevaluator.schema.schema), injected so format
        # code never hardcodes a dataset. Which facts a format consults is its
        # own business (dgraph reads most; nebula/arango only indexes + groups).
        self.schema = schema

    # --- key-byte model (the KEY analogue of graph.value_bytes) ---
    def key_bytes(self, key):
        """Byte cost of one logical key tuple, charged by CountingKV on every key a scan lands on and every GET lookup key.
        This default is the order-preserving codec's encoded length; the shipped formats override it with their own sizing
        model (each format's *Sizing dataclass).
        """
        from kvevaluator.kvstore.key import key_nbytes
        return key_nbytes(key)

    def value_bytes(self, v, key=None):
        """Byte cost of one stored value, charged by CountingKV whenever a value is materialized (a GET, a cursor's value()).
        This default is the neutral content size; the shipped formats override it with their own leaf widths.
        """
        from kvevaluator.schema.graph import value_bytes
        return value_bytes(v)

    # --- physical value codec (models serialized bytes; AbstractKV holds live objects) ---
    def encode_value(self, key, value):
        """`value` -> the bytes a disk store holds at `key`: the format's engine layout (RowV2 / UidPack / VPack), always (the
        `pack` flag sizes value_bytes only). Contract: None <-> b"", any other value encodes to >= 1 byte, and
        decode_value(key, encode_value(key, v)) returns an identical object. No default: each format says what its bytes are.
        """
        raise NotImplementedError(f"{type(self).__name__} has no value codec (encode_value)")

    def decode_value(self, key, raw):
        """Inverse of encode_value: `raw` (bytes) -> the stored Python value. A size-only
        format (writes bytes, never reads them back) may leave this unimplemented."""
        raise NotImplementedError(f"{type(self).__name__} has no value codec (decode_value)")

    def warm(self, store):
        """One-time SETUP after a store is opened, BEFORE any query runs. Default no-op.
        A format overrides this to rebuild in-memory state it needs but that a CACHED
        store (opened without re-ingesting) leaves empty -- arango's vid->collection map.
        Drivers call it right after building stores so its reads land OUTSIDE the per-query
        measurement window: on rocksdb a read here still moves the engine's own tickers, so
        doing it lazily mid-query would wrongly attribute a full setup scan to that query."""
        pass

    # --- streaming ingest (the per-format physical insert) ---
    
    # ----------------------
    # --- STORAGE WRITES ---
    # ----------------------

    @abstractmethod
    def add_vertex(self, store, vid, label, props): ...

    @abstractmethod
    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None): ...

    def finalize_write(self, store):
        pass        # default: nothing buffered (dgraph overrides for its blobs + indexes)

    def materialize(self, graph, store):
        """Small-data convenience: stream an in-memory Graph through the per-record
        ingest methods. At scale, drive add_vertex/add_edge/finalize_write from a CSV
        stream instead (see ldbc.stream / runner.ingest_stream)."""
        for v in graph.vertices.values():
            self.add_vertex(store, v.vid, v.label, v.props)
        for e in graph.edges:
            self.add_edge(store, e.etype, e.src, e.dst, e.props, e.src_label, e.dst_label)
        self.finalize_write(store)

    # --- WRITE OPERATORS: the incremental write into an EXISTING store -----------------
    #
    # Distinct from the ingest methods above, and the distinction is the whole point. add_vertex /
    # add_edge / finalize_write are the BULK LOAD: they build the store from nothing, so they may
    # buffer, sort, and blind-put (dgraph accumulates a whole posting list in memory and writes it
    # once). An incremental insert has no such freedom -- the posting list is already out there --
    # and that is exactly where the formats diverge on writes, the way they diverge on reads.
    #
    # Each op is (execute, predict): the format's `_w_*` method does the reads it needs, returns the
    # (key, value) PUTs and the deleted keys it will perform, plus a KVCost predicted from the schema.
    # This class applies the writes; CountingKV tallies the actual P/D. predicted == actual is
    # therefore the prediction vs EXECUTION, not arithmetic on one list.
    #
    # Reads a write performs (a read-modify-write of a packed row, the incident-edge walk of a
    # delete) are charged to the READ columns, because that is what they are. A write cell reports
    # both currencies; the asymmetry mb2 measured -- dgraph updating a scalar with ZERO read-back,
    # a row/document store reading the whole record first -- is a read-column difference.
    #
    # Result rows are the LOGICAL outcome ({"op", "target"}), identical across formats on purpose,
    # so the cross-format `agree` check still means "all three did the same thing" while their
    # costs differ.

    def insert_vertex(self, store, vid, label, props):
        puts, dels, pred = self._w_insert_vertex(store, vid, label, dict(props))
        return self._apply_writes(store, puts, dels, pred, "insert_vertex", vid)

    def insert_edge(self, store, etype, src, dst, props=None, src_label=None, dst_label=None):
        puts, dels, pred = self._w_insert_edge(store, etype, src, dst, props or {},
                                               src_label, dst_label)
        return self._apply_writes(store, puts, dels, pred, "insert_edge", f"{src}-{etype}->{dst}")

    def update_prop(self, store, vid, label, prop=None, value=None, props=None):
        """Two forms. The single form (`prop`, `value`) rewrites one column. The multi form
        (`props={col: value, ...}`) rewrites several columns in ONE row write: a condensed row is
        read and rewritten once, a split layout writes one record per changed column. Both run
        the same per-format code (_w_update_props); the single form is a one-column dict."""
        if props is None:
            if prop is None:
                raise ValueError("update_prop: give either prop+value or a non-empty props dict")
            puts, dels, pred = self._w_update_props(store, vid, label, {prop: value})
            return self._apply_writes(store, puts, dels, pred, "update_prop", f"{vid}.{prop}")
        if prop is not None or not props:
            raise ValueError("update_prop: give either prop+value or a non-empty props dict")
        puts, dels, pred = self._w_update_props(store, vid, label, dict(props))
        return self._apply_writes(store, puts, dels, pred, "update_prop",
                                  f"{vid}.{','.join(sorted(props))}")

    def update_edge_prop(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        """Rewrite properties of ONE existing edge src -[etype]-> dst, in every stored copy (the
        forward record and, where the layout keeps one, the reverse copy). An absent edge writes
        nothing -- the result row is the same, like delete_edge on a missing edge."""
        if not props:
            raise ValueError("update_edge_prop: props must name at least one column")
        declared = {p for p, _t in self.schema.edge_prop_types.get(etype, ())}
        unknown = sorted(set(props) - declared)
        if unknown:        # C++ refuses the same: a column the edge value has no slot for
            raise ValueError(f"update_edge_prop: {etype!r} declares no edge property {unknown}")
        puts, dels, pred = self._w_update_edge_prop(store, etype, src, dst, dict(props),
                                                    src_label, dst_label)
        return self._apply_writes(store, puts, dels, pred, "update_edge_prop",
                                  f"{src}-{etype}->{dst}")

    def delete_vertex(self, store, vid, label, with_edges=True):
        """Remove a vertex. `with_edges` decides which logical operation is priced:
          True  -- the vertex and every incident edge: the fair cross-format comparison, and the one that shows connectivity
                   scaling (in dgraph each incident edge's reverse posting is keyed by the neighbor).
          False -- the vertex record and its index entries only, leaving the edges dangling: what nebula's DELETE VERTEX
                   (without WITH EDGE) and arango's document removal do.
        """
        puts, dels, pred = self._w_delete_vertex(store, vid, label, with_edges)
        return self._apply_writes(store, puts, dels, pred, "delete_vertex", vid)

    def delete_edge(self, store, etype, src, dst, src_label=None, dst_label=None):
        puts, dels, pred = self._w_delete_edge(store, etype, src, dst, src_label, dst_label)
        return self._apply_writes(store, puts, dels, pred, "delete_edge", f"{src}-{etype}->{dst}")

    def _apply_writes(self, store, puts, dels, predicted, op, target):
        # DELS BEFORE PUTS: a real engine's delete+put of the SAME key in one batch
        # applies in order, so the put wins. The reverse order would make an update's
        # re-put of an UNCHANGED index entry lose to its own del -- destroying entries
        # the update never meant to touch -- a class the formula gate cannot see
        # (predicted==actual with both wrong). Counts are untouched: the engine really
        # issues both ops (mb2 MB81 == MB82).
        for key in dels:
            store.delete(key)
        for key, value in puts:
            store.put(key, value)
        # the one exception: a key this write first put and then left (a chunk rewritten, then
        # moved or emptied). Its delete follows the puts. Only chunk formats set it (_rmw).
        for key in getattr(dels, "late", ()):
            store.delete(key)
        return [{"op": op, "target": target}], predicted

    # --- shared write helpers ---------------------------------------------------------
    def _can_traverse(self, etype, dir):
        """May this format follow `etype` in `dir`? Every format can go "out"; going "in" needs a
        stored reverse, which dgraph has only for @reverse predicates. Overridden where it differs."""
        return True

    def _update_index_cols(self, label, props):
        """The index columns an update of the columns `props` re-maintains, for the formats whose
        index key embeds the column value (nebula, arango). The cheapest plan moves only the changed
        columns' entries: every other entry's key holds an unchanged value, so it is already right.
        Real nebula and arango rewrite every index on any update (mb2 MB82 == MB81); that is an
        engine choice, so the engine formats override this."""
        return [p for p in self.schema.index_cols_of(label) if p in props]

    def _incident_edges(self, store, vid, label):
        """([(etype, dir, [neighbor vids])], KVCost, [unreachable (etype, dir)]) -- the edges a
        vertex delete has to clean up, found through the format's OWN access paths, so the search
        is charged at that format's real read cost.

        `unreachable` names the directions this layout cannot follow (dgraph without @reverse). Those
        edges survive the delete as dangling references. That is a real property of the layout, not a
        modelling shortcut, so it is REPORTED rather than silently skipped -- a format that cannot
        find an edge cannot be charged for removing it, and should not get credit for it either."""
        total, found, unreachable = KVCost(), [], []
        for etype, dir in self.schema.incident_etypes(label):
            if not self._can_traverse(etype, dir):
                unreachable.append((etype, dir))
                continue
            nbrs, c = self.read_neighbors(store, [vid], etype, dir)
            _acc(total, c)
            if nbrs:
                found.append((etype, dir, list(nbrs)))
        return found, total, unreachable

    # xxx STORAGE WRITES END xxx

    # --- query operators (return (result, predicted KVCost)) ---

    # ---------------------
    # --- STORAGE READS ---
    # ---------------------

    @abstractmethod
    def read_vertex_by_id(self, store, vid, label=None): ...

    @abstractmethod
    def read_neighbors(self, store, srcs, etype, dir="out", edge_props=None, pairs=False): ...

    @abstractmethod
    def read_vertex_props(self, store, vids, props, label=None): ...

    @abstractmethod
    def read_anchor_props(self, store, cands, props, labels=None): ...
    # Message anchor + props FUSED: resolve which candidate vid exists AND read its
    # props, at the format's true optimum. A read_vertex_by_id + read_vertex_props split would
    # double-charge the formats whose anchor try already reads the whole row
    # (nebula tag GET, arango pk+doc) — the real engines don't pay that seam, and
    # kvformat totals must stay <= theirs. Returns [row] (with _vid) or [].

    @abstractmethod
    def read_index_eq(self, store, label, prop, value): ...
    # Entry by indexed property value -> matching vids. The formats' index layouts
    # split here: dgraph = point SEEK, uids in the VALUE; nebula =
    # sorted prefix scan, vid in the KEY (S + t·N, 0 GETs); arango = sorted prefix
    # scan of docids + a doc GET per match to recover the _key (S + t·N + t·G).

    @abstractmethod
    def read_index_range(self, store, labels, prop, lo, hi): ...
    # Entry by an indexed prop's VALUE RANGE [lo, hi) -> matching vids. The forward
    # cousin of read_index_eq, for a date window (IC3/IC4): when the candidate set is far
    # LARGER than the window, enumerate the window here + intersect beats reading the
    # prop off every candidate. EXACT for the value-sorted indexes (nebula/arango:
    # seek lo, 1 NEXT per entry until >= hi); DAY-GRANULAR (a superset at the two
    # boundary days) for dgraph's day-token index -- callers re-check the exact ms on
    # the small post-intersection survivors. `labels` lists the tag/collection indexes
    # to scan (Post+Comment for messages); dgraph ignores it (per-predicate index).

    @abstractmethod
    def read_vertices_by_label(self, store, label): ...
    # Enumerate EVERY vid of a type through the vertex-label (type-enumeration)
    # index -- the entry point when no value index exists (the label-only design).
    # Each format walks the structure
    # it already has: dgraph reads the dgraph.type posting list (1 SEEK, uids in the
    # value); nebula prefix-scans the empty tag index (1 SEEK + 1 NEXT/vertex, vid in
    # the key); arango prefix-scans the per-collection pk index (same). A value lookup
    # with no index is then read_vertices_by_label + read_vertex_props(prop) + filter(==value) -- the
    # per-vertex prop read IS the price of the missing value index.

    @abstractmethod
    def read_filter_by_label_probe(self, store, vids, label): ...
    # Keep only the vids of the given type by PROBING each candidate. The cost is a
    # format property: dgraph reads the dgraph.type list per vid (a SEEK each);
    # nebula probes the (vid, tag) key per vid (a GET each, misses included);
    # arango pays NOTHING (ids are collection-qualified — _from/_to name the type).

    @abstractmethod
    def read_filter_by_label_index(self, store, vids, label): ...
    # The same filter entered by the TYPE INDEX: enumerate the type once, intersect in
    # memory. 1 SEEK for dgraph (one posting), 1 SEEK + one NEXT per member for a
    # per-vertex index (nebula, pertype_csr). The access path is the op NAME: the query
    # builders emit the logical `filter_by_label` and the resolver lowers it to one of
    # the two by the format's capabilities (type_index_single_posting -> index).

    # --------------------------------
    # --- STORAGE READS (Composed) ---
    # --------------------------------

    CHASE_READS_ONCE = True   # read_chase reads a node once per call; an engine twin sets False
    # a chase hop stops on the first edge (read_neighbors(first_only=True): 1 SEEK, no NEXT past it); a format whose
    # scan has no such stop, or an engine twin (the engine scans the whole list), leaves it False
    CHASE_FIRST_EDGE_ONLY = False

    # --- iterated reads (composed from read_neighbors, so their cost is the sum of
    #     oracle-true read_neighbors calls -- no per-format code needed) ---
    def read_chase(self, store, srcs, etype, dir="out", max_hops=None):
        """Follow etype from each src until no further edge (or `max_hops` hops,
        if set); return the LAST vertex per src, parallel to srcs.
        Unbounded (max_hops=None): the real replyOf-to-root walk -- a post roots
        itself, and the final empty read that proves 'no more edges' is paid.
        Bounded (max_hops=K): the engines' capped approximation (dgraph's K-level
        unroll / nebula's REPLY_OF*0..K) -- stops AT hop K without the terminal
        read, so a chain deeper than K returns a NON-root vertex (an incomplete
        answer: the real engines' 3-hop replyOf cap). Used only by the *_capped recorded losers.
        A node is read once per call: chains that meet, and repeated srcs, share its read
        (every format; the C++ chase_over reads it once per level batch).
        An engine twin (CHASE_READS_ONCE False) reads it again per chain: the engine runs the
        chase as a per-row subquery.
        A node's first neighbour is its first direction's, out then in: a later direction is read
        only when the earlier ones have none (every format; C++ chase_over). An engine
        twin reads every direction at once (the engine's plan)."""
        total = KVCost()
        out = []
        next_of = {}   # node -> its first neighbour (None: none), read once per call
        for s in srcs:
            cur = s
            hops = 0
            while max_hops is None or hops < max_hops:
                if cur not in next_of or not self.CHASE_READS_ONCE:
                    next_of[cur] = self._chase_first(store, cur, etype, dir, total)
                if next_of[cur] is None:
                    break
                cur = next_of[cur]
                hops += 1
            out.append(cur)
        return out, total

    def _chase_first(self, store, node, etype, dir, total):
        """`node`'s first neighbour for read_chase (None: none); its reads are added to `total`"""
        dirs = (dir,) if dir != "both" or not self.CHASE_READS_ONCE else ("out", "in")
        first = {"first_only": True} if self.CHASE_FIRST_EDGE_ONLY else {}
        for d in dirs:
            nxt, c = self.read_neighbors(store, [node], etype, d, **first)
            _acc(total, c)
            if nxt:
                return nxt[0]
        return None

    def read_closure(self, store, srcs, etype, dir="out", max_levels=None):
        """Transitive read_closure by level-BFS (srcs included), e.g. tagclass
        subtree via isSubclassOf reversed. Each level is one read_neighbors call.
        Unbounded (max_levels=None): the full subtree. Bounded (max_levels=K):
        the engines' capped approximation (dgraph's K-level isSubclassOf unroll)
        -- stops after K expansions, MISSING any descendant deeper than K."""
        total = KVCost()
        seen = list(dict.fromkeys(srcs))
        frontier = seen
        levels = 0
        while frontier and (max_levels is None or levels < max_levels):
            nxt, c = self.read_neighbors(store, frontier, etype, dir)
            _acc(total, c)
            frontier = [x for x in dict.fromkeys(nxt) if x not in seen]
            seen += frontier
            levels += 1
        return seen, total

    def read_edge_exists(self, store, rows, field, etype, into, others=None,
                  via_srcs=None, via_etype=None, reads=None):
        """Flag each row by whether an edge row[field] <-etype-> anchor exists
        (IS7's knows flag). LAZY: with no rows nothing is read at all — including
        the anchor resolution when given via (via_srcs --via_etype--> anchors), so
        a no-replies IS7 costs zero (the ≤-engine bound caught the eager version).
        The PLAN names the reads that answer it, in order (`reads`, the C++ EdgeRead):
        each {"dir": "out"|"in", "at": "anchor"|"cand", "unless_found": bool} reads ONE
        (etype, dir) family -- the anchors' keys of it or the candidates' -- and a format
        does that read its own way (_edge_read): dgraph the owner's posting once; nebula an
        exact edge-key GET per pair; arango the owner's from / to edge list. `unless_found`
        = only for the candidates no earlier read found (the same set in every format). A
        run of anchor-side reads that are not unless_found is one _edge_read call."""
        if not rows:
            return [], KVCost()
        total = KVCost()
        if others is None:
            others, c = self.read_neighbors(store, via_srcs, via_etype, "out")
            _acc(total, c)
        distinct_others = list(dict.fromkeys(others))
        reads = edge_reads(reads)
        cands = list(dict.fromkeys(r[field] for r in rows))
        found = set()
        i = 0
        while i < len(reads):
            r = reads[i]
            todo = [c for c in cands if c not in found] if r["unless_found"] else cands
            run = [r]
            if r["at"] == "anchor" and not r["unless_found"]:
                while i + 1 < len(reads) and reads[i + 1]["at"] == "anchor" and not reads[i + 1]["unless_found"]:
                    i += 1
                    run.append(reads[i])
            i += 1
            if not todo:        # every candidate found: this read reads nothing
                continue
            hits, c = self._edge_read(store, run, distinct_others, todo, etype)
            found |= set(hits)
            _acc(total, c)
        return [{**r, into: r[field] in found} for r in rows], total

    # xxx STORAGE READS END xxx

    def _edge_read(self, store, run, anchors, todo, etype):
        """One read of read_edge_exists (or a run of anchor-side reads): the candidates of `todo` it finds, and its
        cost. Each read of `run` names one (etype, dir) family and whose keys: the anchors' or the candidates'."""
        raise NotImplementedError

    # --- free ops (0 KV): in-memory steps, shared by every format ---
    def attach(self, store, rows, values, into):
        """Attach a PARALLEL scalar list as a field (e.g. each message's chased
        root vid) so later merges can join on it instead of relying on position."""
        return [{**r, into: v} for r, v in zip(rows, values, strict=True)], KVCost()
    def filter(self, store, rows, pred):
        return [r for r in rows if pred(r)], KVCost()

    def dedup(self, store, items):
        return list(dict.fromkeys(items)), KVCost()

    def as_row(self, store, value, field):
        """Wrap a scalar step output (e.g. a count) into a single-row list, so a
        plan's final output is always a list of rows -- the shape the cross-format
        checks and the CSV row count expect. Free."""
        return [{field: value}], KVCost()

    def gate(self, store, items, when):
        """`items`, but only when `when` is non-empty. Free.

        Models a subquery that sits INSIDE a loop and therefore does not run when the loop
        body never executes -- arango's `LET knowsOriginal = (...)` inside `FOR reply IN ...`,
        nebula's node fed by an Argument off the replies branch. Without it a message with no
        replies is charged for work the engine skipped, which is invisible at a param whose
        message happens to have replies and wrong at every other."""
        return (list(items) if when else []), KVCost()

    def dedup_rows(self, store, rows, field="_vid"):
        """Keep the FIRST row per distinct `field`. `dedup` only works on hashable items;
        row dicts need this. Engine plans need it because real traversals emit a vertex once
        per path (arango) or once per frontier level (nebula), so a match can repeat."""
        seen, out = set(), []
        for r in rows:
            if r[field] not in seen:
                seen.add(r[field])
                out.append(r)
        return out, KVCost()

    def exclude(self, store, items, remove):
        rm = set(remove)
        return [x for x in items if x not in rm], KVCost()

    def count(self, store, items):
        return len(items), KVCost()

    def concat(self, store, a, b):
        return list(a) + list(b), KVCost()

    def intersect(self, store, items, other):
        keep = set(other)
        return [x for x in items if x in keep], KVCost()

    def annotate(self, store, items, field, value):
        """Lift a vid list into rows carrying a constant (e.g. BFS distance)."""
        return [{"_vid": x, field: value} for x in items], KVCost()

    def group_count(self, store, items):
        """-> [{'item': x, 'count': n}], first-seen order."""
        c = {}
        for x in items:
            c[x] = c.get(x, 0) + 1
        return [{"item": k, "count": n} for k, n in c.items()], KVCost()

    def filter_in(self, store, rows, field, allowed):
        """Keep rows whose `field` is in `allowed` (a step output, unlike filter's
        baked-in predicate)."""
        keep = set(allowed)
        return [r for r in rows if r[field] in keep], KVCost()

    def flag_in(self, store, rows, field, allowed, into):
        """row[into] = whether row[field] is in `allowed` (IC7 isNew, IS7 knows)."""
        keep = set(allowed)
        return [{**r, into: r[field] in keep} for r in rows], KVCost()

    def derive(self, store, rows, into, fn):
        """row[into] = fn(row) — computed columns (score, latency, pair keys)."""
        return [{**r, into: fn(r)} for r in rows], KVCost()

    def group_top(self, store, rows, by, keys):
        """One row per `by` group: that group's top row under `keys`
        (top_k with k=1 per group; IC7's latest-like-per-liker)."""
        srt, _ = self.top_k(store, rows, keys, None)
        seen = {}
        for r in srt:
            seen.setdefault(r[by], r)
        return list(seen.values()), KVCost()

    def group_sum(self, store, rows, by, field):
        """-> [{'item': g, 'sum': s}], first-seen order (IC10 score)."""
        s = {}
        for r in rows:
            s[r[by]] = s.get(r[by], 0) + r[field]
        return [{"item": k, "sum": v} for k, v in s.items()], KVCost()

    def join_multi(self, store, left, right, left_on, right_on, where=None, where_key=None, where_in=None,
                   where_not_in=None):
        """Like join but keeps EVERY matching right row (one output row per
        matching pair) — for many-to-many merges like post→tags. Optional tests on each merged pair (C++ ops/flow.cc
        PairTest): `where(row)`, then `where_key(row)` in `where_in` or not in `where_not_in`; a failing pair is
        not kept."""
        if where_in is not None and where_not_in is not None:
            raise ValueError("join_multi: where_in and where_not_in together")
        if (where_key is None) != (where_in is None and where_not_in is None):
            raise ValueError("join_multi: where_key goes with one of where_in, where_not_in")
        members = set(where_in if where_in is not None else where_not_in or ())
        member = where_in is not None
        by = {}
        for rrow in right:
            by.setdefault(rrow[right_on], []).append(rrow)
        out = []
        for lrow in left:
            for rrow in by.get(lrow[left_on], ()):
                row = {**lrow, **{k: v for k, v in rrow.items() if k != right_on}}
                if where is not None and not where(row):
                    continue
                if where_key is not None and (where_key(row) in members) != member:
                    continue
                out.append(row)
        return out, KVCost()

    def pluck(self, store, rows, field):
        return [r[field] for r in rows], KVCost()

    def project(self, store, rows, fields):
        return [{f: r.get(f) for f in fields} for r in rows], KVCost()

    def zip_rows(self, store, left, right):
        """Merge two PARALLEL row lists element-wise (e.g. props of a 1:1 hop)."""
        return [{**lrow, **rrow} for lrow, rrow in zip(left, right, strict=True)], KVCost()

    def join(self, store, left, right, left_on, right_on="_vid"):
        """Merge each right row's fields into the left rows they match (hash join
        on left[left_on] == right[right_on]); right_on itself isn't copied."""
        by = {rrow[right_on]: rrow for rrow in right}
        return [{**lrow, **{k: v for k, v in by[lrow[left_on]].items() if k != right_on}}
                for lrow in left], KVCost()

    def left_join(self, store, left, right, left_on, right_on="_vid", default=None):
        """`join` that KEEPS an unmatched left row: it gets `default`'s fields ({name: value}) instead of a
        right row's (BI's OPTIONAL MATCH + count: a person with no likes keeps likeCount 0). A matched row
        merges the right row's fields as `join` does; right_on itself isn't copied. Free."""
        by = {rrow[right_on]: rrow for rrow in right}
        dflt = dict(default or {})
        out = []
        for lrow in left:
            rrow = by.get(lrow[left_on])
            out.append({**lrow, **({k: v for k, v in rrow.items() if k != right_on} if rrow is not None else dflt)})
        return out, KVCost()

    def group_agg(self, store, rows, by, aggs):
        """GROUP BY several fields with several aggregates in one step: `by` = [field, ...], `aggs` =
        {out_name: [kind, field]} with kind count / sum / min / max (count ignores its field; avg is a later
        `derive` div(sum, count), so the float stays Python's on both backends). One row per distinct `by`
        tuple, first-seen order, carrying the `by` fields and the aggregates. Free (0 KV)."""
        groups = {}
        for r in rows:
            key = tuple(r[f] for f in by)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {f: r[f] for f in by}
                for name, (kind, field) in aggs.items():
                    g[name] = 0 if kind in ("count", "sum") else None
            for name, (kind, field) in aggs.items():
                if kind == "count":
                    g[name] += 1
                elif kind == "sum":
                    g[name] += r[field]
                elif kind == "min":
                    g[name] = r[field] if g[name] is None or r[field] < g[name] else g[name]
                elif kind == "max":
                    g[name] = r[field] if g[name] is None or r[field] > g[name] else g[name]
                else:
                    raise ValueError(f"group_agg: unknown aggregate kind {kind!r}")
        return list(groups.values()), KVCost()

    def rename(self, store, rows, names):
        """Rename row fields ({old: new}) so later merges don't collide (e.g. a
        reply's `id` vs its author's `id`)."""
        return [{names.get(k, k): v for k, v in r.items()} for r in rows], KVCost()

    def nest(self, store, rows, children, on, child_on, into, fields):
        """Attach to each row a SORTED, DEDUPLICATED list of (fields...) tuples
        from the child rows whose `child_on` matches the row's `on` — LDBC nested
        results (IC1 universities, IC12 tagNames), deterministic for the
        cross-format diff. Sorted by the NEUTRAL value order (_neutral_key) that the
        C++ evaluator implements too: null < bool < int < str < list/tuple, element-wise
        (repr order would sort ints as digit strings -- not a stable neutral order)."""
        by = {}
        for c in children:
            by.setdefault(c[child_on], set()).add(tuple(c.get(f) for f in fields))
        return [{**r, into: sorted(by.get(r[on], ()), key=_neutral_key)} for r in rows], KVCost()

    def top_k(self, store, rows, keys, k, keep_ties=False):
        """Order + limit over an in-memory set. keys = [(field, "asc"|"desc"), ...]
        in priority order (LDBC tie-breaks), applied as stable sorts. k=None keeps
        everything (a plain LDBC sort with no limit). keep_ties=True also keeps rows
        beyond k that tie the k-th row on the PRIMARY (first) key -- used when a later
        step reads a secondary tie-break key for the winners only (IC7 defers personId),
        so the boundary rows the re-sort might promote aren't dropped early. 0 KVcost."""
        out = list(rows)
        for field, order in reversed(keys):
            out.sort(key=lambda r: r[field], reverse=(order == "desc"))
        if k is None or len(out) <= k:
            return out, KVCost()
        if keep_ties:
            pf = keys[0][0]
            boundary = out[k - 1][pf]
            i = k
            while i < len(out) and out[i][pf] == boundary:
                i += 1
            return out[:i], KVCost()
        return out[:k], KVCost()
