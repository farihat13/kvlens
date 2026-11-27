"""Dgraph engine-faithful format: the plan the real engine runs, not the cheapest one.

It reads the same store as DgraphFormat (a Format holds only the schema; every operator takes the store), so a
difference between the two is the plan, never the layout. It is checked against a live engine's /kvstats at the pinned
operating point (cache off, deterministic sort on, eq / type filter paths = index; experiments/engine_fidelity/).
What the engine does differently:
  1. Filter then sort: a date filter reads the date of every neighbour before `first: k` picks the winners.
  2. Type filters go through the type index: one posting-list SEEK, not one per candidate.
  3. A multi-key sort re-reads the second key for every uid of the first sort's result (sort_topk).
Not modelled: the anchor goes through the fqid index, not eq(id, N) (both one posting-list SEEK: the cost is right,
the key is not); and stock dgraph's racing sort paths, whose cost is nondeterministic (the deterministic sort is the
operating point).
"""
from kvevaluator.formats.dgraph import DgraphFormat
from kvevaluator.cost import KVCost

# Which operators carry engine mechanics, and which are inherited BECAUSE the engine really
# does the same thing. A gate in tests/ asserts every operator lands in exactly one
# bucket -- otherwise a later tweak to the optimal-path DgraphFormat would silently rebase
# this engine baseline, and the comparison would drift without anything looking wrong.
OVERRIDES = frozenset({
    "read_filter_by_label_index",   # engine: ONE type-index SEEK whatever the schema (below)
    "sort_topk",         # NEW op: the multi-key sort's index walk + second-key fetch
    "has_scan",          # NEW op: `func: has(pred)` -- the predicate scan (1 SEEK + 1 NEXT/subject)
    "ineq_root_scan",    # NEW op: root `lt`/`between` on a day index -- key walk, bucket reads, edge re-check
    "sort_without_index",  # NEW op: `orderasc: <unindexed pred>, first: k` -- 1 value read per uid
    "first_k_by_uid",    # NEW op (in memory, 0 KV): `first: k` over a uid list, in the live uid order
    "sort_without_index_per_src",  # NEW op: a nested block's `(orderasc: <unindexed pred>)` -- per parent
})
INHERITS = frozenset({
    # add_vertex / the anchors / read_vertex_props diverge only through the base's `_id_as_uid`
    # flag (engine=False): the engine builds the fqid/id index, anchors via a SEEK, and GETs
    # `id`; the SIM base treats the vid AS the uid. Same METHOD, so still INHERITS -- the flag,
    # not a redefinition, carries the engine's `eq(id,N)` anchor.
    "add_vertex", "add_edge",     # same store + the engine's fqid index (via _id_as_uid)
    "read_vertex_by_id",                  # engine: 1 fqid SEEK; SIM: 0 direct (see the anchor note)
    "read_neighbors",                  # engine expands the whole posting list, same as optimal
    "read_vertex_props",                  # 1 GET per (predicate, vertex); engine GETs `id`, SIM derives
    "read_anchor_props", "read_index_eq", "read_index_range", "read_vertices_by_label",
    "read_filter_by_label_probe",            # the per-candidate probe: NOT used by engine plans (they name _index)
    "read_top_k_by_index",             # NOT used by engine plans; they use sort_topk instead
    "read_chase", "read_closure", "read_edge_exists",   # composed from read_neighbors; engine does the same
})


class DgraphRollupWrites(DgraphFormat):
    """dgraph's delta+rollup write strategy over the posting layout: the engine twin's (real Dgraph writes this way).

    Per dirtied posting key: 1 delta PUT + 1 rolled-base PUT per k-write window (writes_per_rollup = k; k=1, the
    measured point, leaves the store fully merged). The layout format dgraph rewrites the whole posting in place
    instead. Engine sites: dgraph posting/mvcc.go, oracle.go."""

    # k: how many write OPERATIONS pass before a rollup sweep. Stands in for real dgraph's ~10 s
    # per-key cooldown, so k = write-throughput x 10 s (at 1000 writes/s, k = 10_000). k=1 rolls
    # after every operation, which is the point mb2 measured at.
    writes_per_rollup = 1

    def __init__(self, schema, sizing=None):
        super().__init__(schema, sizing)
        self._pending = {}       # key -> (base snapshotted before its first delta, [deltas])
        self._since_rollup = 0   # write OPERATIONS since the last sweep
        self._reads_saved = 0    # this op's RMW reads the formula counted but the window already did

    def reset_write_state(self):
        """Forget pending deltas and the window position. Drivers call this between measurement
        cells so a cell's cost never depends on which cell ran before it."""
        self._pending.clear()
        self._since_rollup = 0
        self._reads_saved = 0

    def _base_of(self, store, key):
        """The posting's TRUE base -- the value it held before any of this window's deltas landed on
        it -- snapshotted the first time the key is dirtied and kept until the sweep.

        Load-bearing. Real dgraph keeps a delta as its own versioned key, so the base is still there
        when the rollup reads it. Our store has no versions: the delta PUT lands on the same key and
        overwrites the base. Without this snapshot the sweep would merge the delta with itself and
        report a tiny write, making amortized bytes look ~3000x cheaper than they are -- turning the
        whole amortization result into an artifact of the store's lack of MVCC."""
        if key not in self._pending:
            self._pending[key] = (self._current(store, key), [])
        return self._pending[key]

    # The rollup's read: real dgraph reads a key's base once per rollup, when it merges the window's
    # deltas. It is charged here, the first time the window dirties the key (a counted point SEEK via
    # _current: this snapshot IS that base, see _base_of) -- at k=1, the measured point, that is the
    # same operation. A blind write (a known `base`, or emptying an OWN key) reads nothing.

    def _dirty(self, store, key, posting, remove=False, base=DgraphFormat._READ):
        """One dirtied posting key -> its DELTA put. The rolled base is not written here: it is
        written by the sweep in _apply_writes, once per key per window, which is what makes a hot
        key's many deltas share one rollup."""
        blind = base is not DgraphFormat._READ or remove is True
        if key in self._pending:
            if not blind and self._rmw_reads_counted:
                self._reads_saved += 1                       # the window already read this key
        elif blind:
            self._pending[key] = ([] if base is DgraphFormat._READ else base, [])
        _base, deltas = self._base_of(store, key)
        deltas.append((posting, remove))
        # removal delta = the tombstone marker; an append or an edge-prop update ("props") is a
        # value delta
        return [(key, [posting] if remove in (False, "props") else None)]

    def _posting_now(self, store, key):
        """With deltas pending (k > 1), the stored value is only the LAST delta: fold the window's
        deltas onto the snapshotted base, as the sweep will."""
        if key not in self._pending:
            return self._current(store, key)
        val, deltas = self._pending[key]
        for posting, r in deltas:
            val = self._merge(val, [posting], r)
        return val

    def _n_puts(self, nkeys):
        """FORMULA side of the oracle for the op's own puts: one delta per dirtied posting key. The
        sweep's puts are counted separately in _apply_writes, so both halves stay independent of the
        emit path."""
        return nkeys

    def _rollup_sweep(self):
        """Close the window: one rolled-base PUT per key holding deltas, then clear.

        Sweeping EVERY pending key (not just heavily-written ones) is the point of this shape --
        real dgraph enqueues every dirtied key on commit and the delta count only sets priority
        (posting/mvcc.go). So a cold key pays its rollup here too."""
        puts = []
        for key, (base, deltas) in self._pending.items():
            # Fold the window's deltas onto the snapshotted base IN ORDER, each with its own
            # mode: an own-key tombstone (True) empties, a shared-posting "entry" removal takes
            # only that entry out, an append appends. (Checking only the LAST
            # delta's remove flag would empty a shared posting on an "entry" removal.)
            val = base
            for posting, r in deltas:
                val = self._merge(val, [posting], r)
            puts.append((key, val))
        self._pending.clear()
        return puts

    def _apply_writes(self, store, puts, dels, predicted, op, target):
        """The op's own puts, plus the rollup sweep when this operation closes the window.

        _apply_writes is the single funnel every write operation passes through, which is what makes
        an OPERATION-counted window possible without threading state through each _w_* method."""
        # the formula side counts one read per non-blind dirty; a key the window had already read
        # (k>1, or dirtied twice in one op) was not read again
        predicted.S -= self._reads_saved
        self._reads_saved = 0
        self._since_rollup += 1
        if self._since_rollup >= self.writes_per_rollup:
            self._since_rollup = 0
            sweep = self._rollup_sweep()
            puts = list(puts) + sweep
            predicted.P += len(sweep)
        return super()._apply_writes(store, puts, dels, predicted, op, target)


class DgraphEngineFormat(DgraphRollupWrites):
    CHASE_READS_ONCE = False   # the engine runs the chase per row: a node read again per chain
    name = "dgraph_engine"

    def __init__(self, schema, sizing=None):
        super().__init__(schema, sizing)
        # the engine plans' sort_topk / ineq_root_scan walk the predicate-global date band (real dgraph's @index(day)):
        # a type-scoped date design moves Post / Comment out of it -- refuse rather than miss them silently
        if schema.type_scoped_index & schema.date_cols:
            raise ValueError("dgraph_engine: the engine twin reads dgraph's predicate-global date index; "
                             "a type-scoped date design is not the engine's")

    # The live Dgraph's uid per vid, or None. Dgraph keeps every uid list sorted by uid, so `first: k`
    # over a uid variable keeps the k SMALLEST uids; the loader assigns uids (blank nodes), so that
    # order is not the vid order. Captured from the live engine with its counts (measure.py), set by
    # validate.py. A plan that needs it and finds None fails loudly (first_k_by_uid).
    uid_of = None

    # Real dgraph's write path shows no user-visible read-modify-write read (mb2's write sweep: the
    # reads it sees are external-id resolution; the merge happens in the storage layer / at
    # rollup). The layout formats charge that read; the engine twin does not.
    _rmw_reads_counted = False

    # The engine models the real `eq(id,N)` anchor: keep the fqid/external-id index (build +
    # SEEK anchor + id GET), which the SIM base drops (it treats the vid AS the uid). This
    # pins the engine baseline via the base's `_id_as_uid` branches -- no operator override,
    # so add_vertex/read_vertex_by_id/read_anchor_props/read_vertex_props stay in INHERITS.
    _id_as_uid = False

    # ----------------------------
    # --- OVERRIDDEN OPERATORS ---
    # ----------------------------

    def read_filter_by_label_index(self, store, vids, label):
        """The engine's type filter at typefilterpath=index: ONE SEEK on the dgraph.type
        index posting list, then a free in-memory membership test. The engine plans name this
        op directly (typefilterpath=index whatever the schema), so this override is unconditional.
        The SIM plans instead emit the logical `filter_by_label`, which the resolver lowers by the
        format's capabilities: on a type-indexed schema (canonical declares one for every label)
        DgraphFormat holds type_index_single_posting and gets this same 1-SEEK op; with no type
        index it gets the per-candidate probe (N SEEK, cheaper for the tiny candidate sets SIM
        plans build, ruinous for the thousands an engine plan carries).

        Measured: IC2 puts 3811 candidates through two type filters and the engine's TOTAL
        SEEK count is 124 -- so these cannot be per-candidate.

        An EMPTY input costs NOTHING: dgraph does not read the type index to filter no rows.
        Visible twice -- IS3, where the empty `knows` direction is free, and IS6, whose static
        3-level unroll prunes whole branches by depth (a depth-1 comment never enters the
        c1/p2/c2/p3 subtree, and those branches cost 0, not 1 SEEK each). Charging the seek
        unconditionally over-counts IS6 by up to 5 SEEKs at shallow depths."""
        if not vids:
            return [], KVCost()
        members, cost = self.read_vertices_by_label(store, label)
        keep = set(members)
        return [v for v in vids if v in keep], cost

    # ------------------------
    # --- CUSTOM OPERATORS ---
    # ------------------------

    def sort_topk(self, store, rows, prop, k, second_key=None, id_order="asc",
                  index=None, desc=True):
        """The engine's `orderdesc: <prop>, orderasc: <second_key>, first: k`.

        Transcribed from `worker/sort.go` (dgraph-25.0.0) rather than fitted, and then
        checked against six measured points -- see THE EVIDENCE below.

        `sortWithIndex` opens ONE reverse badger iterator over the `prop` index, bounded to
        the tokenizer's prefix, and walks buckets newest-first:

            for itr.Seek(seekKey); itr.Valid(); itr.Next() {   <- 1 SEEK, then 1 NEXT/bucket
                intersectBucket(ts, token, out)                <- see below
            }

        and `intersectBucket` does, per bucket:

            pl := posting.GetNoStore(x.IndexKey(attr, token))  <- 1 SEEK: the bucket's list
            result := pl.Uids(Intersect: candidates)           <- in memory, free
            sortByValue(ts, result, scalar)                    <- 1 SEEK PER INTERSECTED UID
            ...
            if len(ulist) >= count { return errDone }          <- stop after this bucket

        `sortByValue` is the term that is easy to miss and dominates on a dense candidate
        set: it calls `fetchValue` -> `posting.GetNoStore(x.DataKey(attr, uid))` for EVERY
        uid the bucket contributed, not for the k that will survive. So a query whose first
        candidate-bearing bucket holds 232 messages pays 232 reads to return 20.

        The SECOND key is a different mechanism entirely. `processSort` calls `multiSort`
        only when `len(ts.Order) > 1`, and that fetches the second attribute over the whole
        input UidMatrix -- every candidate, not just the accumulated ones.

        THE EVIDENCE (IC9 p1, maxDate swept over 6 values so the walk depth and
        the first hit bucket's size move independently):

            maxDate       buckets   hit-bucket    predicted S      measured S   measured N
            2013-12-31          1          101   1+  1+101=103            103            0
            2013-01-01          1          101   1+  1+101=103            103            0
            2012-08-28         18          232   1+ 18+232=251            251           17
            2012-01-01        258           96   1+258+ 96=355            355          257
            2011-06-01        472          101   1+472+101=574            574          471
            2010-06-01        838           25   1+838+ 25=864            864          836

        Six for six, with no free parameter, over a relation that is NOT monotone in the
        bucket count (251 seeks for 18 buckets, 355 for 258). A curve fitted to the bucket
        walk alone cannot produce that column; reading sortByValue does.

        The same sweep settles the GET side. A SINGLE-key sort measures **zero** extra GETs
        at every one of the six dates, while the two-key sort measures exactly the candidate
        count (119970 / 119970 / 117061 / 69533 / 36977 / 3408). So every GET here is
        multiSort's second-key fetch, and the first key is paid for in SEEKs. The first key's exact values are read by
        sortByValue, so the engine's ordering is exact, and the second key's reads are a separate operator.
        """
        cand = {r["_vid"] for r in rows}
        by_vid = {r["_vid"]: r for r in rows}
        S = N = 0

        # Which index the walk crosses: `prop`'s own index by default. For `id` that is the engine
        # store's `id @index(int)` (dgraph.py add_vertex): one bucket per id VALUE, shared by every
        # type with that id, in value order -- so an `orderasc: id` walk until the k-th match crosses
        # every id below it, whatever its type (IC11: ~893K buckets before the 20th person at sf0.3).
        # Not the per-vid `fqid` index: that one is type-major, and would stop after the Persons alone.
        idx = index or prop
        if desc:
            cur = store.seek_for_prev((idx, "I", "~"))    # "~" sorts after every token
        else:
            cur = store.seek((idx, "I", ""))             # "" sorts before every token
        S += 1                                        # itr.Seek(seekKey)
        acc, vals, seen = [], {}, set()
        while cur.valid() and cur.key()[:2] == (idx, "I"):
            uids = self._read_list(store, (idx, "I", cur.key()[2])) or []
            S += 1                                    # GetNoStore(IndexKey): the bucket
            # removeDuplicates(result.Uids, il.uset) -- a uid can only be in one day bucket,
            # so this only matters for multi-language predicates. Kept for faithfulness.
            inter = [u for u in uids if u in cand and u not in seen]
            seen.update(inter)
            for u in inter:
                vals[u] = self._read_list(store, (prop, "D", u))
                S += 1                                # fetchValue: GetNoStore(DataKey)
            inter.sort(key=lambda u: vals[u] or 0, reverse=desc)   # sortByValue's types.Sort
            acc.extend(inter)
            if len(acc) >= k:
                break                                 # errDone, BEFORE the iterator advances
            cur.prev() if desc else cur.next()
            N += 1                                    # itr.Next()

        # multiSort: the SECOND attribute, fetched over `destUids(r.reply.UidMatrix)`.
        # Absent entirely for a one-key sort -- which is what the six-date sweep measures
        # (1-key G delta = 0 at every date).
        #
        # How big that matrix is comes down to an OVERFLOW in sortWithIndex's tail:
        #
        #     remainingCount := int(ts.Count) - len(r.UidMatrix[i].Uids)
        #     canAppend := x.Min(uint64(remainingCount), uint64(len(nullNodes)))
        #
        # `remainingCount` is an int and goes NEGATIVE whenever the walk overshoots k --
        # which it usually does, since a bucket is a whole day and is appended entire for a
        # multi-key sort. `uint64(-2)` is 18446744073709551614, so x.Min returns
        # len(nullNodes) and EVERY candidate the walk never reached is appended to the
        # result. The second-key fetch then covers the whole candidate set.
        #
        # Measured on IC8 dgraph, which separates the two branches cleanly
        # because its three params straddle the boundary:
        #
        #     param  candidates  buckets  accumulated  ->  matrix  measured 2-key G
        #     p1           2114        8           22      2114                2114
        #     p2           1945       24           20        20                  20
        #     p3            666       34           20        20                  20
        #
        # Landing exactly on k is the ONLY case where the overflow does not fire, and both
        # params that land there measure exactly k. Recording that pattern as a tally
        # ("8 of 10 cells charge per candidate, 2 charge exactly k") would have no mechanism. This is the
        # mechanism, and it is a dgraph bug rather than a design choice: the query asks for
        # 20 rows and pays to read a second sort key for 2114 of them.
        G = 0
        if second_key:
            in_acc = set(acc)
            nulls = [v for v in dict.fromkeys(r["_vid"] for r in rows) if v not in in_acc]
            remaining = k - len(acc)
            can = len(nulls) if remaining < 0 else min(remaining, len(nulls))
            srows, cost = self.read_vertex_props(store, acc + nulls[:can], [second_key])
            S += cost.S
            G += cost.G
            second = {r["_vid"]: r[second_key] for r in srows}
        else:
            second = {}

        out = [dict(by_vid[u], **{prop: vals[u]},
                    **({second_key: second.get(u)} if second_key else {})) for u in acc]
        # The ordering is exact on both keys: sortByValue read the real `prop` values for
        # everything in `acc`, and multiSort read the real second key.
        out.sort(key=lambda r: (r.get(second_key) is None, r.get(second_key)),
                 reverse=(id_order == "desc"))
        out.sort(key=lambda r: (r[prop] or 0), reverse=desc)
        return out[:k], KVCost(S=S, G=G, N=N)

    def sort_without_index(self, store, vids, prop, k, desc=False):
        """The engine's one-key sort on a predicate with NO sortable index:
        `func: uid(X), orderasc: <prop>, first: k` with `<prop>` unindexed (IC1's lastName,
        dgraph/data/ldbcTypes.schema:116). Returns the top k as rows {_vid, prop}.

        detsort picks sortWithoutIndex when the order column has no sortable index
        (our Dgraph patch's deterministic sort toggle). In dgraph-25.0.0 worker/sort.go:

          sortWithoutIndex (:126)  -> sortByValue over the WHOLE uid list, then paginate(first: k)
          sortByValue (:781)       -> fetchValue per uid, types.Sort in memory
          fetchValue (:811)        -> posting.GetNoStore(x.DataKey(attr, uid))   <- 1 SEEK per uid

        GetNoStore is an iterator read, so each value costs 1 SEEK, not a GET -- the same read
        sort_topk charges for sortByValue. With one order key, paginate does not widen the page
        on ties (that branch is `len(ts.Order) > 1`), so exactly k survive. Only then do the
        block's children read their predicates, for the k survivors.

        A uid with no value goes last (sortByValue's nullsList). Ties: types.Sort uses sort.Sort,
        which is not stable, so the engine's order inside a tie is not knowable here; we break
        ties by vid. Only a tie that straddles position k can change WHICH rows survive."""
        vals = {}
        for u in dict.fromkeys(vids):
            vals[u] = self._read_list(store, (prop, "D", u))     # fetchValue: GetNoStore(DataKey)
        have = sorted((u for u in vals if vals[u] is not None), key=lambda u: u)
        have.sort(key=lambda u: vals[u], reverse=desc)
        order = have + sorted(u for u in vals if vals[u] is None)
        out = [{"_vid": u, prop: vals[u]} for u in order[:k]]
        return out, KVCost(S=len(vals))

    def sort_without_index_per_src(self, store, rows, prop, src="src", dst="dst"):
        """A NESTED block's `(orderasc: <unindexed pred>)`, no `first` (IC11's
        `workAt @filter(uid(QUAL)) (orderasc: name)`). sortWithoutIndex (worker/sort.go:126) sorts
        each list of the uid matrix on its own -- one list per parent -- and sortByValue fetches the
        value of every uid of that list (fetchValue: 1 SEEK each, as in sort_without_index). So a
        child under two parents is fetched twice: 1 SEEK per (parent, child) row, no dedup across
        parents. Returns the rows ordered by (parent, value); ties by child vid (sort.Sort is not
        stable, so the engine's tie order is not knowable -- see sort_without_index)."""
        keyed = [(r, self._read_list(store, (prop, "D", r[dst]))) for r in rows]   # fetchValue
        keyed.sort(key=lambda rv: (rv[0][src], rv[1] is None, rv[1] if rv[1] is not None else "",
                                   rv[0][dst]))
        return [{**r, prop: v} for r, v in keyed], KVCost(S=len(rows))

    def has_scan(self, store, etype, dir="out"):
        """The engine's `func: has(<pred>)`: a SCAN of the predicate's key range that ENUMERATES the
        subjects that have an `etype` edge (one NEXT per subject key). It reads only the KEYS, not the
        postings -- `has()` tests existence, so a subsequent `uid_in(pred, ...)` filter does the actual
        posting reads. Returns the subject vids. Cost: 1 SEEK to enter the range + 1 NEXT per subject.

        IC12 is why this exists: `T as var(func: has(hasType)) @filter(type(tag) AND uid_in(hasType,
        uid(TCAll)))` SCANS every Tag with a hasType edge (all 16080 at sf0.3 -- a constant NEXT count,
        independent of the tagClass) to enumerate the candidates, then `uid_in(hasType, TCAll)` reads
        each tag's hasType posting (the ~16080 SEEK) to keep the ones in the closure. The OPTIMAL plan
        avoids both by entering from the tag CLASSES (reverse hasType over the small closure), so it
        charges 0 NEXT and few SEEKs. Charging this scan + the per-tag posting read is what makes IC12
        dgraph match the live engine's 16080-tag scan -- category-A engine waste (enumerate-then-filter
        where the reverse entry reads only the tags it needs)."""
        kind = {"out": "D", "in": "R"}[dir]
        out, S, N = [], 0, 0
        cur = store.seek((etype, kind, ""))
        S += 1                                        # enter the predicate's key range
        while cur.valid() and cur.key()[:2] == (etype, kind):
            out.append(cur.key()[2])                  # the subject vid (KEY only -- has() reads no posting)
            cur.next()
            N += 1                                    # one step per subject key
        cur.mark_miss()                               # the landing past the range: an absence miss
        return out, KVCost(S=S, N=N)

    def ineq_root_scan(self, store, prop, fname, lo=None, hi=None):
        """The engine's inequality function AT THE QUERY ROOT over a day-token (lossy) index:
        `func: lt(<prop>, hi)` or `func: between(<prop>, lo, hi)`. Returns the matching uids.

        Three separate passes, all in worker/ (dgraph-25.0.0):

          1. tokens.go:196-240 getInequalityTokens -- a KEY-ONLY iterator (PrefetchValues=false)
             over the index. `between` walks forward from lo's token and stops at the first token
             past hi's; `lt` walks BACKWARD from hi's token to the start of the index (a lossy
             tokenizer takes every token on that side; no early stop).
                 itr.Seek(seekKey)                  <- 1 SEEK
                 append token; itr.Next()           <- 1 NEXT per token taken
          2. task.go:826 handleUidPostings -- one `qs.cache.GetUids(IndexKey(attr, token))` per
             token: 1 SEEK per bucket posting (the walk above read keys only).
          3. task.go:1334 handleCompareFunction -- the day token is lossy, so the uids of the
             EDGE buckets are re-checked against the exact value, one
             `posting.GetNoStore(DataKey(attr, uid))` each (an iterator read: 1 SEEK, not a GET --
             the same read sortByValue does, see sort_topk). `between` re-checks the first AND the
             last bucket (filterRow(0), filterRow(last)); `lt` re-checks the first bucket only when
             it IS hi's own day (tokens[0] == ineqValueToken[0]). The re-check runs over every uid
             in the bucket, of every type: the root has no type restriction yet.

        No per-uid read anywhere else: the interior buckets' uids are taken whole."""
        day = lambda ms: f"{ms // 86400000:07d}"
        S = N = 0
        tokens = []
        if fname == "between":
            lo_tok, hi_tok = day(lo), day(hi)
            cur = store.seek((prop, "I", lo_tok))
            S += 1                                    # itr.Seek(seekKey)
            while cur.valid() and cur.key()[:2] == (prop, "I"):
                if cur.key()[2] > hi_tok:
                    break                             # break LOOP: past hi's token, no Next
                tokens.append(cur.key()[2])
                cur.next()
                N += 1                                # itr.Next()
        elif fname == "lt":
            hi_tok = day(hi)
            cur = store.seek_for_prev((prop, "I", hi_tok))
            S += 1                                    # itr.Seek(seekKey), Reverse=true
            while cur.valid() and cur.key()[:2] == (prop, "I"):
                tokens.append(cur.key()[2])
                cur.prev()
                N += 1                                # itr.Next() (reverse)
        else:
            raise ValueError(f"ineq_root_scan: unsupported function {fname!r}")
        cur.mark_miss()                               # the landing outside the walk: an absence miss

        rows = []
        for t in tokens:
            rows.append(list(self._read_list(store, (prop, "I", t)) or []))
            S += 1                                    # handleUidPostings: GetUids(IndexKey)

        def recheck(i, keep):                         # filterRow(i): one GetNoStore per uid
            nonlocal S
            out = []
            for u in rows[i]:
                v = self._read_list(store, (prop, "D", u))
                S += 1
                if v is not None and keep(v):
                    out.append(u)
            rows[i] = out

        if rows and fname == "between":
            recheck(0, lambda v: lo <= v <= hi)       # CompareBetween is inclusive at both ends
            recheck(len(rows) - 1, lambda v: lo <= v <= hi)
        elif rows and fname == "lt" and tokens[0] == hi_tok:
            recheck(0, lambda v: v < hi)
        return list(dict.fromkeys(u for r in rows for u in r)), KVCost(S=S, N=N)

    # --- uid order (in memory, 0 KV) ---

    def first_k_by_uid(self, store, items, k, uid_of=None):
        """`first: k` over a uid list: the k items with the smallest live-Dgraph uid. In memory.
        `uid_of`: the map as [vid, uid] pairs riding on the step (the form the C++ twin reads), else the
        format's own."""
        uid_of = dict(uid_of) if uid_of is not None else self.uid_of
        if uid_of is None:
            raise RuntimeError("first_k_by_uid needs the live Dgraph uid map (uid_of): measure.py "
                               "exports it next to live_kvstats.csv, validate.py loads it")
        missing = [v for v in items if v not in uid_of]
        if missing:
            raise KeyError(f"{len(missing)} vids have no uid in the Dgraph uid map, e.g. {missing[:3]}")
        return sorted(items, key=uid_of.__getitem__)[:k], KVCost()
