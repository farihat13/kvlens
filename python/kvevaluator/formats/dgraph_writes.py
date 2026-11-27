"""Dgraph's write path, as the class DgraphFormat extends: ingest (add_vertex, add_edge, finalize_write) and the
incremental write operators (read-modify-write of every posting a write dirties). The keys and values it writes
come from DgraphFormat (self._data_pred, self._adj, self._idx, ...)."""
from kvevaluator.cost import KVCost
from kvevaluator.formats.base import _acc


class DgraphWrites:
    """DgraphFormat's write methods (a mixin: it holds no state of its own)."""

    def add_vertex(self, store, vid, label, props):
        # dgraph.type IS stored (every node has it) but a plain projection query does
        # NOT read it (verified vs real /kvstats: seek=1 for 1..250 friends; it fires
        # only for expand()/type-filter/GraphQL queries, which our plans avoid).
        store.put(("dgraph.type", "D", vid), [label])
        # the dgraph.type ENUMERATION index (what the real type() function reads): one posting
        # list per type with every uid of that type. SCHEMA-DRIVEN -- built iff the design declares
        # a per-type membership index for this label. The canonical schema DOES declare it for every
        # label (dgraph mandates dgraph.type; ldbc.schema's index block), so this is on in canonical;
        # a schema that omits `index <Label>` (a "no type-enum" variant) would drop it and force the
        # probe path. The ("dgraph.type","D",vid) row above is UNCONDITIONAL -- it is the structural
        # per-vertex type record the probe path reads, present whatever the index design.
        if self.schema.has_type_index(label):
            self._idx.setdefault(("dgraph.type", label), []).append(vid)
        # SIM (_id_as_uid): the vid IS the uid, so no fqid/external-id index -- the anchor is a
        # direct address and `id` is derived. The engine models `eq(id,N)`, so it builds the fqid
        # per-vid index (one entry per vertex) its anchor SEEK reads, and the engine's `id @index(int)`:
        # one posting per id VALUE, shared by every type with that id, in value order -- what an
        # `orderasc: id` walks (sort_topk).
        if not self._id_as_uid:
            store.put(("fqid", "D", vid), vid)
            self._idx.setdefault(("fqid", vid), []).append(vid)
            if props.get("id") is not None:
                self._idx.setdefault(("id", self._id_token(props["id"])), []).append(vid)
        for p, val in props.items():
            if p == "id" and self._id_as_uid:
                continue                         # SIM: id IS the vid, derived not stored (engine stores it)
            if val is None:
                continue                         # an absent value has no posting and no index entry
            store.put((self._data_pred(label, p), "D", vid), val)
            if not self._index_entry(label, p):
                continue
            if p in self._indexed:
                self._idx.setdefault((self._index_pred(label, p), str(val)), []).append(vid)
            elif p in self._date_preds:
                # @index(day): one posting list per DAY token. Predicate-global by default (shared
                # by every dated type -- faithful to the real per-predicate @index(day)); keyed per
                # type (f"{label}.{prop}") when this column is type-scoped. Zero-padded so key order
                # == date order; read by read_top_k_by_index's walk.
                pred = self._index_pred(label, p)
                if (label, p) in self._key_sorted:
                    # dim-2 Option B: ms IN THE KEY. One index entry per message, keyed by an
                    # order-preserving ms+vid composite token, EMPTY value (the vid is in the key).
                    # A top-k KEY range scan reads exactly K key-only entries and STOPS -- scan_key
                    # proportional to K, scan_value ~ 0, like nebula. The key is UNIQUE per message,
                    # so put it DIRECT (no day-bucket accumulation -> no finalize_write pass).
                    store.put((pred, "I", self._key_sorted_token(val, vid)), None)
                    continue
                key = (pred, f"{val // 86400000:07d}")
                if (label, p) in self._value_sorted:
                    # dim-2 Option A: keep the raw ms so finalize_write can date-sort the bucket and
                    # the read can rank + apply the maxDate filter without a per-candidate ms GET.
                    # Stored as (ms, vid); finalize turns it into date-descending [vid, ms] pairs.
                    self._idx.setdefault(key, []).append((val, vid))
                else:
                    self._idx.setdefault(key, []).append(vid)

    @staticmethod
    def _id_token(val):
        """The engine's `id @index(int)` token: zero-padded, so key order is numeric order (LDBC ids >= 0)."""
        return f"{int(val):020d}"

    def add_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        self._adj.setdefault((etype, "D", src), []).append([dst, props or {}])
        if etype in self.schema.reverse_etypes:
            self._adj.setdefault((etype, "R", dst), []).append([src, props or {}])

    def finalize_write(self, store):
        # Postings are stored uid-ASCENDING -- real dgraph's invariant (codec/codec.go;
        # uidpack_bytes prices exactly that order), and what lets the UidPack value codec
        # round-trip the stored list exactly. The ingest stream arrives in CSV order, so
        # sort here.
        for key, postings in self._adj.items():
            postings.sort(key=lambda e: e[0])   # e = [dst, facets]; facets travel with their uid
            store.put(key, postings)            # one blob per (predicate, kind, vertex)
        for (pred, token), entries in self._idx.items():
            if pred in self._value_sorted_preds:
                # dim-2: a date-ordered posting. entries are (ms, vid); store them as
                # date-DESCENDING [vid, ms] pairs (newest first, matching the newest-first read
                # walk). id (= key_id(vid)) is the ms tie-break, ascending, so the top-k's
                # (date desc, id asc) order is reproducible; the read re-sorts anyway, so the
                # stored tie order is not load-bearing -- sorting by (-ms, vid) just keeps it
                # deterministic across ingests.
                entries = sorted(entries, key=lambda mv: (-mv[0], mv[1]))
                store.put((pred, "I", token), [[vid, ms] for ms, vid in entries])
            else:
                store.put((pred, "I", token), sorted(entries))  # index: uids in the VALUE, uid-ascending
        self._adj.clear()
        self._idx.clear()

    # --- WRITE OPERATORS (incremental; see base.Format) --------------------------------
    #
    # THE POSTING-STORE SHAPE. Everything dgraph stores is a posting list: a scalar property, an
    # adjacency, an index entry. So an incremental write is "which posting keys did this dirty?",
    # and the answer follows the PREDICATES touched -- which is why a 9-property insert costs
    # roughly twice a 5-property one here while a row store charges the same for both (mb2's
    # MB83/MB84).
    #
    # WHAT THIS FORMAT CHARGES: the LAYOUT's own write cost, read-modify-write. One PUT per dirtied
    # posting key, and its bytes are the WHOLE posting -- appending one edge to a hub rewrites the
    # hub's entire adjacency, so write bytes are O(degree).
    #
    # WHY NOT delta+rollup HERE, WHEN THAT IS WHAT REAL DGRAPH DOES. Because delta+rollup is not a
    # property of this layout -- it is a WRITE STRATEGY on top of it, the same way a plan is a
    # strategy on top of a read layout. A posting store admits at least two (rewrite-in-place;
    # append a delta and merge later), and baking one in would put an engine mechanism inside the
    # format the way copying an engine's planner would put a planner inside the read path -- the
    # confound this experiment exists to remove (the evaluator prices the cheapest plan the layout
    # allows). It also silently mis-states a finding: "dgraph's edge insert is flat in degree" is
    # a fact about the ROLLUP, not about the layout, and stating it here would hide what the
    # strategy buys.
    #
    # So the strategy lives in DgraphRollupWrites (formats/dgraph_engine.py), the ENGINE twin's base,
    # because delta+rollup IS what the real engine does and mb2's measured numbers are of that engine.

    # Every read a write does is charged: True for the layout formats. The ENGINE twin
    # sets False -- mb2 measured no user-visible read-modify-write reads on real dgraph's writes.
    _rmw_reads_counted = True

    def _rmw_s(self, n):
        """FORMULA side: the SEEKs that `n` read-modify-write reads cost under this format's rule."""
        return n if self._rmw_reads_counted else 0

    def _current(self, store, key):
        """The posting list at `key` today: the read half of a read-modify-write, a counted point
        SEEK -- dgraph's own read idiom (every read a write does is charged; an absent posting is
        the absence lane). Real dgraph
        writes a delta and merges at rollup instead -- that strategy is DgraphRollupWrites' (the engine twin)."""
        v = store.seek_exact(key) if self._rmw_reads_counted else store.backend.get(key)
        return list(v) if isinstance(v, list) else ([] if v is None else v)

    def reset_write_state(self):
        """Forget any per-cell write state. Nothing to forget in the plain layout (each write is
        self-contained); DgraphRollupWrites overrides it to drop accumulated deltas. Drivers call it
        between measurement cells unconditionally."""
        pass

    _READ = object()

    def _apply_writes(self, store, puts, dels, predicted, op, target):
        """A value-sorted design's (uid, ms) day buckets are not maintained on a write: refuse, before anything lands
        (a bare uid appended to a [[vid, ms], ...] bucket would corrupt it). Mirrored by C++ Dgraph::run_write_op."""
        if self._value_sorted:
            raise ValueError(f"dgraph {op}: writes on a value-sorted date-index design are not modelled")
        return super()._apply_writes(store, puts, dels, predicted, op, target)

    def _dirty(self, store, key, posting, remove=False, base=_READ):
        """One dirtied posting key -> the PUTs it costs. Read-modify-write: ONE put, carrying the
        whole merged posting; the read is a counted point SEEK (_current). Every removal is a PUT (dgraph
        has no DELETE -- its D column is identically 0, mb2 L4); `remove` names WHICH removal, see
        _merge. A BLIND write reads nothing: the caller passes the known `base` (a new vertex's own
        keys: [] -- nothing there yet; a scalar overwrite: None -- last write wins), and emptying the
        vertex's OWN key (remove=True) needs no read either. Each op's formula-side G counts exactly
        the non-blind calls."""
        if base is self._READ:
            base = None if remove is True else self._current(store, key)
        return [(key, self._merge(base, [posting], remove))]

    def _merge(self, base, postings, remove):
        """The new posting value. `remove` is the mode, NAMED BY THE CALLER -- own-vs-shared
        cannot be inferred from the value's shape (the removed vertex's own adjacency posting is
        also a list, and must empty):
          False    append (insert); a scalar predicate is a one-posting list, so last write wins.
          "entry"  remove ONE entry from a posting OTHER vertices share -- a neighbour's
                   adjacency, an index token. postings[-1] is the vid whose entry goes; the
                   co-tenants survive, and the put's value bytes are the honest
                   posting-minus-one rewrite.
          "props"  not a removal: replace the props of ONE entry (update_edge_prop). postings[-1]
                   is [vid, {col: value}]; the entry keeps its untouched props.
          True     empty the key whole -- the removed vertex's OWN posting. The tombstone VALUE
                   is None, not [] (the C++ side writes null the same way): a read of the
                   tombstoned key sees "absent", matching a real tombstone, and _current maps it
                   back to [] for any later RMW."""
        assert remove in (False, True, "entry", "props"), f"unknown _merge mode {remove!r}"
        if remove == "props":
            # not a removal: an edge-prop update. postings[-1] is [vid, {col: value}]; every
            # entry with that vid keeps its other props and takes the new values
            ent, new = postings[-1]
            if not isinstance(base, list):
                return base
            return [[e[0], {**(e[1] or {}), **new}] if isinstance(e, list) and e[0] == ent
                    else ([e, dict(new)] if e == ent else e) for e in base]
        if remove == "entry":
            if not isinstance(base, list):
                return None                      # scalar: no co-tenants to preserve
            ent = postings[-1]
            # every entry with this vid goes -- parallel duplicate edges carry no discriminator
            return [e for e in base if (e[0] if isinstance(e, list) else e) != ent]
        if remove:
            return None
        if not isinstance(base, list):
            return postings[-1]                              # scalar predicate: last write wins
        # DEDUP on append: a posting is a keyed SET -- a real
        # engine cannot hold a duplicate uid, and our realistic codec refuses one. The one
        # reachable duplicate was a SAME-VALUE indexed update (old-token remove and new-token
        # append hit ONE key; both read the pre-put base, the append won carrying the dup).
        # Both puts are still issued -- only the winning CONTENT dedups -- so MB82 = 6 PUT
        # and every count stay untouched.
        return base + [x for x in postings if x not in base]

    def _n_puts(self, nkeys):
        """FORMULA side of the oracle: PUTs for `nkeys` dirtied posting keys -- one each, computed
        from the key count alone, so a bug in the emit path shows up as predicted != actual."""
        return nkeys

    def _vertex_posting_keys(self, vid, label, props):
        """Every posting key a vertex's own record occupies: the type record, one per property, and
        one index posting per indexed/dated value plus the type-enumeration index. This is the list
        an insert dirties and a delete tombstones."""
        self._no_per_label("_vertex_posting_keys")
        keys = [("dgraph.type", "D", vid)]
        if not self._id_as_uid:
            keys.append(("fqid", "D", vid))
        for p, val in props.items():
            if p == "id" and self._id_as_uid:
                continue                                     # SIM: id IS the vid, derived not stored
            if val is None:
                continue                                     # absent: no posting (add_vertex)
            keys.append((p, "D", vid))
        index_keys = []
        if self.schema.has_type_index(label):
            index_keys.append(("dgraph.type", "I", label))
        if not self._id_as_uid:
            index_keys.append(("fqid", "I", vid))
            if props.get("id") is not None:
                index_keys.append(("id", "I", self._id_token(props["id"])))   # shared by every type: RMW
        for p, val in props.items():
            if p == "id" and self._id_as_uid or val is None or not self._index_entry(label, p):
                continue
            if p in self._indexed:
                index_keys.append((self._index_pred(label, p), "I", str(val)))
            elif p in self._date_preds:
                if (label, p) in self._key_sorted:      # the vertex's OWN key-only entry (see _w_insert_vertex)
                    index_keys.append((self._index_pred(label, p), "I", self._key_sorted_token(val, vid)))
                else:
                    index_keys.append((self._index_pred(label, p), "I", f"{val // 86400000:07d}"))
        return keys, index_keys

    def _vertex_data_value(self, attr, vid, label, props):
        """The value stored at one of a vertex's DATA posting keys. Split out from _w_insert_vertex
        because a derived layout can re-key the properties (group co-read predicates into one key),
        and then the key no longer names a single property."""
        if attr == "dgraph.type":
            return [label]
        if attr == "fqid":
            return vid
        return props[attr]

    def _w_insert_vertex(self, store, vid, label, props):
        self._no_per_label("_w_insert_vertex")
        data_keys, index_keys = self._vertex_posting_keys(vid, label, props)
        puts = []
        for key in data_keys:                                # a new vertex's own keys: blind
            puts += self._dirty(store, key, self._vertex_data_value(key[0], vid, label, props), base=[])
        n_rmw = 0
        for key in index_keys:
            if key[0] in self._key_sorted_preds:
                puts.append((key, None))                     # a key-only entry, written blind (no RMW)
            elif key[0] == "fqid":
                puts += self._dirty(store, key, vid, base=[])   # the engine's per-vid entry: OWN key, blind
            else:
                puts += self._dirty(store, key, vid)         # an index posting holds uids: RMW
                n_rmw += 1
        n = len(data_keys) + len(index_keys)
        return puts, [], KVCost(S=self._rmw_s(n_rmw), P=self._n_puts(n))

    def _w_insert_edge(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        self._no_per_label("_w_insert_edge")
        # One posting key per direction the schema stores -- flat in degree on the OP count, and
        # that flatness is the posting layout's write-side advantage. The bytes are not flat: at
        # k=1 the rollup rewrites the whole adjacency (see the section header).
        keys = [(etype, "D", src)]
        entry = [[dst, dict(props)]]
        if etype in self.schema.reverse_etypes:
            keys.append((etype, "R", dst))
            entry.append([src, dict(props)])
        puts = []
        for key, posting in zip(keys, entry):
            puts += self._dirty(store, key, posting)         # a shared adjacency: RMW
        return puts, [], KVCost(S=self._rmw_s(len(keys)), P=self._n_puts(len(keys)))

    def _w_update_props(self, store, vid, label, props):
        self._no_per_label("_w_update_props")
        # Split props: every column is its own posting, so a multi-column update is the
        # single-column update once per column (blind put; one GET per INDEXED column only).
        puts, dels, total = [], [], KVCost()
        for prop, value in props.items():
            p1, d1, c1 = self._w_update_one(store, vid, label, prop, value)
            puts += p1
            dels += d1
            _acc(total, c1)
        return puts, dels, total

    def _w_update_one(self, store, vid, label, prop, value):
        self._no_per_label("_w_update_one")
        keys = [(prop, "D", vid)]
        puts, dels = self._dirty(store, (prop, "D", vid), value, base=None), []   # scalar: blind
        S, G = 0, 0
        if (label, prop) in self._key_sorted:
            # the key-sorted entry is the vertex's OWN key (ms + vid): the old value locates it (1 GET),
            # it goes by one DELETE (nebula's rule for a key-only index entry), the new one is
            # written blind -- no shared token, no read-modify-write
            old = store.get((prop, "D", vid))
            G = 1
            pred = self._index_pred(label, prop)
            if old is not None:
                dels.append((pred, "I", self._key_sorted_token(old, vid)))
            keys.append((pred, "I", self._key_sorted_token(value, vid)))
            puts.append((keys[-1], None))
        elif self._index_entry(label, prop):
            # An indexed update must drop the OLD token's posting and add the new one, so it has to
            # know the old value -- one GET, charged. A NON-indexed update reads nothing at all
            # (mb2 MB81: 2 PUT, 0 read-modify-write), because a predicate posting is written blind.
            old = store.get((prop, "D", vid))
            G = 1
            pred = self._index_pred(label, prop)             # per type when type-scoped
            tok = (lambda v: str(v) if prop in self._indexed else f"{v // 86400000:07d}")
            if old is not None:
                keys.append((pred, "I", tok(old)))
                # the old token is SHARED with every other vertex holding that value
                puts += self._dirty(store, (pred, "I", tok(old)), vid, remove="entry")
                S += self._rmw_s(1)                          # its RMW read
            # Update-to-the-SAME-value: both puts hit one key; the append wins and _merge
            # DEDUPS it (a posting is a set), so the winning posting holds the uid once
            # while both puts still count (MB82 = 6 PUT intact).
            keys.append((pred, "I", tok(value)))
            puts += self._dirty(store, (pred, "I", tok(value)), vid)
            S += self._rmw_s(1)                              # the new token's RMW read
        return puts, dels, KVCost(S=S, G=G, P=self._n_puts(len(keys)), D=len(dels))

    def _w_delete_vertex(self, store, vid, label, with_edges=True):
        self._no_per_label("_w_delete_vertex")
        # The vertex's own postings are known from the SCHEMA, no lookup needed. The index tokens
        # are not -- they are keyed by VALUE -- so each indexed property costs one GET to learn
        # which token to remove this uid from.
        props = {}
        pred = KVCost()
        for p in self.schema.index_cols_of(label):
            v = store.get((p, "D", vid))
            pred.G += 1
            if v is not None:
                props[p] = v
        # One posting per DECLARED column of the label, plus the type record (and the engine's
        # fqid). Read off the schema, not off the props the caller passed -- the row on disk has
        # every column whether or not the caller knows them, and a `uid(v)` delete tombstones them
        # all. This per-predicate fan-out is why a predicate store's delete is expensive before
        # connectivity even enters the picture.
        data_keys = [("dgraph.type", "D", vid)]
        if not self._id_as_uid:
            data_keys.append(("fqid", "D", vid))
            # the engine's per-vid fqid INDEX entry is keyed by the vid, so it is the vertex's
            # OWN key (never shared) -- without this line a deleted vertex stays
            # id-resolvable through the fqid anchor path
            data_keys.append(("fqid", "I", vid))
        for p in self.schema.props_of(label):
            data_keys.append((p, "D", vid))
        if not self._id_as_uid:
            data_keys.append(("id", "D", vid))   # the engine STORES id; the SIM derives it from the vid
        index_keys, dels = [], []
        if self.schema.has_type_index(label):
            index_keys.append(("dgraph.type", "I", label))
        if not self._id_as_uid:
            idv = store.get(("id", "D", vid))                # the engine's id @index(int) token
            pred.G += 1
            if idv is not None:
                index_keys.append(("id", "I", self._id_token(idv)))
        for p, val in props.items():
            if p in self._indexed:
                index_keys.append((self._index_pred(label, p), "I", str(val)))
            elif p in self._date_preds:
                if (label, p) in self._key_sorted:      # OWN key-only entry: one DELETE, no co-tenant rewrite
                    dels.append((self._index_pred(label, p), "I", self._key_sorted_token(val, vid)))
                else:
                    index_keys.append((self._index_pred(label, p), "I", f"{val // 86400000:07d}"))
        puts = []
        for key in data_keys:
            puts += self._dirty(store, key, vid, remove=True)       # OWN keys: empty whole
        for key in index_keys:
            puts += self._dirty(store, key, vid, remove="entry")    # SHARED tokens: this uid only (RMW)
        pred.S += self._rmw_s(len(index_keys))
        pred.P = self._n_puts(len(data_keys) + len(index_keys))
        pred.D = len(dels)
        if with_edges:
            for etype, dir in self.schema.incident_etypes(label):
                p2, _d2, c2 = self._w_delete_incident(store, vid, label, etype, dir)
                puts += p2
                _acc(pred, c2)
        return puts, dels, pred          # D = 0 unless key-sorted: dgraph deletes are tombstone PUTs (mb2 L4)

    def _w_delete_incident(self, store, vid, label, etype, dir):
        """The edge cleanup a vertex delete owes ONE incident (etype, dir): tombstone our own
        posting, and remove our ENTRY from the posting keyed at each NEIGHBOUR (theirs is
        shared; their other edges survive) -- the connectivity scaling mb2 measured
        (MB87: 61 tombstone puts at a leaf, 765 at a hub; the other half of every incident edge
        lives in a posting keyed by the neighbour, not by us). Split out of _w_delete_vertex: one
        piece per incident etype.
        Returns (puts, dels, cost) with P formula'd per piece via _n_puts -- _n_puts must stay
        LINEAR for the pieces to sum to the monolith's total."""
        self._no_per_label("_w_delete_incident")
        if not self._can_traverse(etype, dir):
            return [], [], KVCost()      # no stored reverse: dangling edges survive, uncharged
        nbrs, cost = self.read_neighbors(store, [vid], etype, dir)
        puts, nkeys = [], 0
        if nbrs:
            own = (etype, "D" if dir == "out" else "R", vid)   # our own side: ONE key
            puts += self._dirty(store, own, vid, remove=True)
            nkeys += 1
            for nbr in nbrs:
                # The other side exists only where the predicate is @reverse: without it there
                # is no reverse posting to fix up, and a put there would materialize a phantom
                # empty posting ingest never wrote and no read queries (the C++ side's guard).
                if etype not in self.schema.reverse_etypes:
                    continue
                # The NEIGHBOUR's posting is shared -> remove only our entry. A self-loop is the
                # exception: that "neighbour" key is OUR OWN posting, and puts apply only at op
                # end, so an "entry" put here would overwrite the own-key tombstone (last put
                # wins) and resurrect the posting.
                puts += self._dirty(store, (etype, "R" if dir == "out" else "D", nbr),
                                    vid, remove="entry" if nbr != vid else True)
                nkeys += 1
                cost.S += self._rmw_s(nbr != vid)            # a shared neighbour posting: RMW read
        cost.P += self._n_puts(nkeys)
        return puts, [], cost

    def _w_delete_edge(self, store, etype, src, dst, src_label=None, dst_label=None):
        self._no_per_label("_w_delete_edge")
        # Both postings are SHARED (src's other edges of this etype; dst's other reverse
        # entries): remove only THIS edge's entry from each -- the far endpoint's vid.
        keys = [((etype, "D", src), dst)]
        if etype in self.schema.reverse_etypes:
            keys.append(((etype, "R", dst), src))
        puts = []
        for key, other in keys:
            puts += self._dirty(store, key, other, remove="entry")   # shared postings: RMW
        return puts, [], KVCost(S=self._rmw_s(len(keys)), P=self._n_puts(len(keys)))

    def _w_update_edge_prop(self, store, etype, src, dst, props, src_label=None, dst_label=None):
        self._no_per_label("_w_update_edge_prop")
        # The edge props ride INSIDE the posting entry, so each stored side is one RMW of its whole
        # posting: read once (a counted point SEEK, as every read a write does), then rewritten. A side
        # whose posting does not hold the entry is not written: an absent edge costs its reads only.
        sides = [((etype, "D", src), dst)]
        if etype in self.schema.reverse_etypes:
            sides.append(((etype, "R", dst), src))
        puts, nkeys = [], 0
        for key, other in sides:
            current = self._posting_now(store, key)            # the RMW's read, charged once
            if not self._has_entry(current, other):
                continue
            puts += self._dirty(store, key, [other, dict(props)], remove="props", base=current)
            nkeys += 1
        return puts, [], KVCost(S=self._rmw_s(len(sides)), P=self._n_puts(nkeys))

    def _posting_now(self, store, key):
        """The posting's current LOGICAL value (for an existence check). Plain dgraph: the stored
        value; the rollup strategy folds its pending deltas on top (DgraphRollupWrites)."""
        return self._current(store, key)

    @staticmethod
    def _has_entry(posting, vid):
        return isinstance(posting, list) and any(
            (e[0] if isinstance(e, list) else e) == vid for e in posting)

    def _can_traverse(self, etype, dir):
        """dgraph can only walk an edge backward when the predicate is @reverse. Without one the
        incident-edge sweep of a delete cannot even FIND those edges, so they survive as dangling
        references (_w_delete_incident returns empty for that direction, uncharged; the cost row
        does not currently surface which directions dangled)."""
        return dir == "out" or etype in self.schema.reverse_etypes

    # xxx STORAGE WRITES END xxx
