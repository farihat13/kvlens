"""Nebula engine-faithful format: what the real engine does where it differs from NebulaFormat's cheapest plan.

Most of the engine-vs-optimal difference is the plan, which NebulaFormat already charges. The mechanics that are the
engine's own live here, not in nebula.py, so they never enter the optimum the engines are compared against:
  - storaged follows a failed tag lookup with a prefix scan (read_vertex_props)
  - an update rewrites every index of the label (the _update_index_cols hook)
Every other operator is inherited because the engine does the same thing, and the manifest says so: that is a finding,
not an omission. A future engine mechanic lands here as an override, and the manifest records it.
"""
from kvevaluator.formats.nebula import NebulaFormat
from kvevaluator.cost import KVCost
from kvevaluator.schema.vids import key_id

# The manifest: every operator is declared overridden or inherited, and the gate in
# tests/ enforces the partition, so an operator added to the ABC later cannot slip
# through unexamined.
OVERRIDES = frozenset({
    "tag_probe",        # NEW op: a Traverse first-step tag check -- 1 GET, NO prefix scan
    "read_vertex_props",        # storaged's prefix-scan-after-a-miss (see below)
    "all_tag_props",    # NEW op: the heterogeneous traverse's 8-tags-per-vertex read
    "tag_index_scan",   # NEW op: LOOKUP's TagIndex*Scan -- one scan per partition + base-row GETs
    "scan_vertices",    # NEW op: MATCH's ScanVertices -- every vertex row of every partition
    "get_edge",         # NEW op: FETCH PROP ON <edge>'s GetEdges -- 1 GET per edge key, no scan
})
INHERITS = frozenset({
    "add_vertex", "add_edge",
    "read_vertex_by_id",        # vid IS the key -- free, engine and optimal alike
    "read_neighbors",        # Traverse: 1 SEEK per (src, direction) + 1 NEXT per edge
    "read_anchor_props", "read_index_eq", "read_index_range", "read_vertices_by_label", "read_filter_by_label_probe",
    "read_filter_by_label_index",
    "read_top_k_by_index",   # NOT used by engine plans -- the engine sorts after materializing
    "read_chase", "read_closure", "read_edge_exists",
})


# Every tag in the LDBC space. A heterogeneous traverse reads ALL of them per visited vertex
# (see all_tag_props), so the count matters: 8 GETs per vertex, not 1.
ALL_TAGS = ("Person", "Post", "Comment", "Forum", "Organisation", "Place", "Tag", "TagClass")

# Partitions in the live LDBC space (nebulagraph/scripts/ldbc/load.sh:62, `PARTITION_NUM = 100`).
# Our nebula layout does not model partitions (nebula.py: R = 1), which is right for point reads
# and per-vertex scans: a vid lives in one partition. A whole-space read -- an index LOOKUP or a
# ScanVertices -- is sent to EVERY partition, and each one opens its own prefix scan. That is
# 1 SEEK per partition, found rows or not.
NEBULA_PARTS = 100


class NebulaEngineFormat(NebulaFormat):
    CHASE_READS_ONCE = False   # the engine runs the chase per row: a node read again per chain
    CHASE_FIRST_EDGE_ONLY = False   # and scans each hop's whole edge list
    name = "nebula_engine"
    _ROW_LABELS = False     # the engine's plan is replayed as recorded: no tag is carried from one read to the next

    # ----------------------------
    # --- OVERRIDDEN OPERATORS ---
    # ----------------------------

    def _update_index_cols(self, label, props):
        """Real nebula rewrites EVERY index of the label on any update, touched or not (mb2 measured
        MB82 == MB81). The layout does not force it, so the base format moves only the changed
        columns' entries; the engine's choice is kept here."""
        return list(self.schema.index_cols_of(label))

    def read_vertex_props(self, store, vids, props, label=None):
        """AppendVertices, with storaged's miss behaviour.

        `NebulaFormat.read_vertex_props` charges 1 GET per tag probe, found or not -- the layout
        forces the probe (a message vid does not reveal its type, prop access is
        tag-qualified), so that GET is a format property.

        The extra cost modelled here is NOT a format property. When the row at (vid, tag) is
        absent, real storaged follows the failed lookup with a PREFIX SCAN of the vid to
        discover which tags the vertex actually has. Nothing in the key shape requires that
        -- a minimal engine over the same layout would stop at the failed GET -- so it is
        engine waste and belongs on this side of the split. Source: GetTagPropNode checks
        whether the vertex exists when none of its requested tags was found, by a tag-prefix
        scan of the vid (nebula storage/exec/GetPropNode.h:47-68, use_vertex_key=false).

        Measured against live storaged /kvstats:

            FETCH PROP ON Person <200 vids>                      prefix=0     get=200
            MATCH (p)<-[:HAS_CREATOR]-(m) RETURN id(m)           prefix=1     get=1
            MATCH (p)<-[:HAS_CREATOR]-(m:Post) RETURN m.Post...  prefix=2180  get=2653
                                                (2652 messages traversed, 473 of them Posts)

        2180 == 2652 - 473 == exactly the vertices whose Post tag was NOT found, while GET
        is one per vertex regardless of type. An explicit-vid read of the RIGHT tag costs no
        prefix scan at all, so the cost is specifically the miss. This is also why nebula's
        instrumentation splits get_found_latency from get_notfound_latency.
        """
        # Reimplemented rather than wrapped: calling super() and then re-probing to find the
        # misses would issue a SECOND GET per vid, inflating G by the miss count. The engine
        # reads each (vid, tag) once.
        if label is None:
            raise ValueError("nebula read_vertex_props needs label (a tag name or candidate list)")
        labels = (label,) if isinstance(label, str) else tuple(label)
        row_props = [p for p in props if p != "id"]
        cache, G, S = {}, 0, 0
        for v in dict.fromkeys(self._v(x) for x in vids):
            row = {"_vid": v}
            if "id" in props:
                row["id"] = key_id(v)
            if row_props:
                blob = None
                for tag in labels:
                    blob = store.get((v, tag))     # 1 GET per probe, found or not
                    G += 1
                    if blob is not None:
                        break
                    store.seek((v,))               # the miss: storaged's tag-discovery scan
                    S += 1
                for p in row_props:
                    row[p] = (blob or {}).get(p)
            cache[v] = row
        return [dict(cache[self._v(x)]) for x in vids], KVCost(S=S, G=G)

    # ------------------------
    # --- CUSTOM OPERATORS ---
    # ------------------------

    def all_tag_props(self, store, vids, props=None, tags=ALL_TAGS, dedup=True):
        """Read EVERY tag a plan node LISTS on each visited vertex, with no short-circuit.

        Two shapes of plan node do this, and they differ only in how long the tag list is:

        - A heterogeneous `Traverse`. When a traversal is variable-length or direction-BOTH,
          graphd cannot know what type each visited vertex is, so it asks storaged for all
          tags at once. The captured IC1 plan shows this literally -- node 34 (`steps: 1..3`,
          `edgeDirection: BOTH`) carries a `vertexProps` list with EIGHT tagId entries, one
          per tag in the space. Confirmed on IC1 p1, sf0.1: distinct vertices within 3 hops =
          1 + 9 + 345 + 1327 = 1682, and live G = 13455 = 8 x 1682 - 1.

        - An `AppendVertices` over a union type. IC9 node 19 lists tagId 4 AND tagId 3
          (`props: [{creationDate,content},{imageFile,creationDate,content}]`) because a
          `message` is a Post or a Comment. It reads BOTH rows for every message, even though
          exactly one of them can exist. Confirmed on IC9 p1: 119970 messages and live G =
          240392 = 2 x 119970 + 452, the 452 being the friend rows and the anchor.

        So the mechanic is one rule -- charge the LISTED tags, not the resolved one -- and
        `tags` is what the plan node listed. Cost: len(tags) GETs per DISTINCT visited vertex
        (graphd dedups the frontier -- `dedup: 1` on the node -- so a vertex reached by
        several paths is read once).

        **No prefix scan here.** `NebulaEngineFormat.read_vertex_props` charges storaged's
        tag-discovery scan after a failed lookup; this operator never triggers it, because it
        only fires when at least one listed tag is found. IC9 is the discrimination: 77354 of
        its 119970 messages are Comments, so a "scan whenever the Post probe misses" rule
        would add 77354 seeks to a query that measures 473 in total. The unified reading is
        that storaged scans only when NONE of the requested tags is present -- true of IS4's
        single-tag probe, false here and on IC1 where every vertex has one of the 8.

        This is engine behaviour, not a layout property -- the key shape `(vid, tag)` permits
        reading a single tag, which is what `NebulaFormat.read_vertex_props` does. graphd chooses the
        broad read because it will not commit to a type before reading.

        **`dedup`** (default True) is the frontier dedup above -- one read per DISTINCT vid, the
        `dedup: 1` an `AppendVertices`/`Traverse` node carries. Pass `dedup=False` when the plan node
        does NOT dedup, so the whole vertex is read once PER INPUT ROW: the case where nebula fails
        to push a bound-vertex filter into a Traverse and materializes every expansion (IC7's isNew
        `not((liker)-[:KNOWS]-(person))`, the documented ic10 limitation) -- there the materialization
        is per knows-EDGE, and the count is what makes IC7 nebula's GETs match live.
        """
        seq = dict.fromkeys(self._v(x) for x in vids) if dedup else [self._v(x) for x in vids]
        rows, G = [], 0
        for v in seq:
            row = {"_vid": v, "id": key_id(v)}
            for tag in tags:
                blob = store.get((v, tag))     # 1 GET per LISTED tag, no short-circuit
                G += 1
                if blob:
                    row.update(blob)
                    row["_tag"] = tag
            rows.append(row)
        return rows, KVCost(G=G)

    def tag_probe(self, store, vids, label, props=None):
        """A `Traverse`'s `first step filter: X._tag IS NOT EMPTY` -- 1 GET per source, and
        NO prefix scan on a miss.

        This is the one place the missed-tag rule (a missed tag GET is followed by a prefix scan of the vid) does NOT apply, and
        both halves are measured:

          IS4  a missed tag read inside an `AppendVertices` costs 1 GET **+ 1 SEEK**
          IS6  a missed tag read inside a Traverse's first-step filter costs 1 GET **+ 0**

        The mechanism fits: AppendVertices wants the property ROW, so when the tag it guessed
        is absent storaged prefix-scans the vid to discover which tags do exist. A first-step
        filter only asks "is this vertex a Post?", which a point lookup answers with `absent`
        -- there is nothing further to discover.

        Getting this wrong is worth a SEEK per reached vertex: modelling IS6's probe with the
        engine `read_vertex_props` over-charged nebula by exactly the reply depth (S 5/7/8 measured
        against 6/9/11 predicted).

        The probe does NOT prune the READS: `containerOf` is then tried from every reached vertex,
        including the ones that just failed this check -- which the S arithmetic confirms at
        every depth (d=1 measures 2 containerOf seeks over 2 reached vertices, not 1).

        What the filter DOES drop is the ROWS: the edges of a source that fails the check are read
        but not output (storage/exec GetNeighborsProcessor builds a FilterNode with the tag filter;
        FilterNode::checkTagAndEdge skips the failing rows). IC12 node 29 reads 30,477 hasTag edges
        and outputs 17,384; a plan must take the next step's inputs from the surviving rows only.

        A Traverse that ALSO lists `vertexProps` for the same tag collapses the two into this
        one read (IS1's rule), so pass `props` to get the values out of the same GET rather
        than charging a second one -- nebula IC4 node 30 is exactly that shape:
        `vertexProps: [{"props":["creationDate"],"tagId":3}]` next to
        `first step filter: Post._tag IS NOT EMPTY`.
        """
        rows, out, G = [], [], 0
        for v in dict.fromkeys(self._v(x) for x in vids):
            blob = store.get((v, label))             # point lookup: found or not-found
            G += 1
            if blob is not None:
                out.append(v)
                rows.append({"_vid": v, **{p: key_id(v) if p == "id" else blob.get(p) for p in (props or ())}})   # id: the key
        return (rows if props else out), KVCost(G=G)

    def _other_partition_seeks(self, store, key):
        """The prefix seeks of the partitions our unpartitioned store folds into the one walk
        the caller already did: NEBULA_PARTS - 1 more SEEKs, each landing and stopping. The NEXTs
        are unchanged -- every entry sits in exactly one partition, so the per-partition walks
        add up to the one walk. Returns the SEEK count."""
        for _ in range(NEBULA_PARTS - 1):
            store.seek(key).mark_miss()
        return NEBULA_PARTS - 1

    def tag_index_scan(self, store, label, prop, value=None, lo=None, hi=None,
                       base_row=False, props=()):
        """LOOKUP ON <tag>: `TagIndexPrefixScan` (value=), `TagIndexRangeScan` (lo/hi, hi
        exclusive, lo=None is the plan's `beginValue: __EMPTY__`) or `TagIndexFullScan` (neither)
        over the index on (label, prop).

        Two engine mechanics on top of the layout's index walk (1 SEEK + 1 NEXT per entry):

        - **One scan per partition.** storaged runs the index plan once per partition
          (storage/index/LookupProcessor.cpp:225-227, `plan->execute(part)` for each part), and
          each run opens its own prefix iterator (storage/exec/IndexScanNode.cpp:422 resetIter).
          So S = NEBULA_PARTS, whatever the match count. Measured: entry_eq p2 S=100 for 8 rows.

        - **A base-row GET per entry when the query needs a column the index lacks**
          (`base_row=True`). IndexScanNode.cpp:392-415 sets needAccessBase_ when a required
          column is not an index field, and doNext (line 443) then reads the tag row per entry
          (IndexVertexScanNode.cpp:55-62, a kvstore get of the tag key). `YIELD id(vertex)` counts:
          graphd asks for every prop of the vertex (the query-file notes), so an id-only LOOKUP
          still pays 1 GET per entry -- entry_eq p2 measures G=8 for 8 rows.

        Returns one row per index entry: `_vid`, `id` (the vid is the key) and `props` off the
        base row (empty when base_row is False). Index entries are unique per vertex, so there
        is no dedup; the row always exists (the index is on its own tag), so no miss."""
        if value is not None:
            vids, c = self.read_index_eq(store, label, prop, value)
        elif lo is not None or hi is not None:
            vids, c = self.read_index_range(store, [label], prop, 0 if lo is None else lo, hi)
        else:
            prefix = ("i", label, prop)
            vids, cur = [], store.seek(prefix)
            while cur.valid() and cur.key()[:3] == prefix:
                vids.append(cur.key()[4])
                cur.next()
            cur.mark_miss()
            c = KVCost(S=1, N=len(vids))
        S, G = c.S + self._other_partition_seeks(store, ("i", label, prop)), 0
        rows = []
        for v in vids:
            row = {"_vid": v, "id": key_id(v)}
            if base_row:
                blob = store.get((v, label))           # getBaseData: 1 GET per entry
                G += 1
                row.update({p: (blob or {}).get(p) for p in props if p != "id"})
            rows.append(row)
        return rows, KVCost(S=S, G=G, N=c.N)

    def scan_vertices(self, store, label):
        """MATCH (v:<label>) with no index on the label: `ScanVertices`. storaged scans every
        TAG ROW of every partition -- `NebulaKeyUtils::tagPrefix(partId)`, all tags, not just the
        label's -- and drops the rows of other tags (storage/exec/ScanNode.h:58-104; the skip is
        the `tagNodesIndex_.find(tagId)` miss at line 87). Tag rows sit in their own key family
        (key type kTag_, common/utils/Types.h:15), so the scan never touches an edge key.
        Cost: 1 SEEK per partition + 1 NEXT per vertex row in the space, 0 GET (the value rides
        with the key).

        LAYOUT GAP. Our nebula layout has no key-type byte: a vertex's tag row and its edge keys
        share the vid prefix, so "every tag row" is not one contiguous range here. The walk that
        visits each tag row once is the per-tag type-enumeration structure (read_vertices_by_label:
        one entry per vertex of the tag), done for each of the 8 tags. The SEEK and NEXT counts
        match the engine's scan (8 tag walks + the other partitions' seeks = NEBULA_PARTS; one NEXT
        per vertex); the BYTES do not (index keys are walked, not rows), so read no byte result off
        this op."""
        out, S, N = [], 0, 0
        for tag in ALL_TAGS:
            vids, c = self.read_vertices_by_label(store, tag)
            S, N = S + c.S, N + c.N
            if tag == label:
                out = vids
        for _ in range(NEBULA_PARTS - S):              # the partitions the 8 walks did not stand for
            store.seek(("i", label)).mark_miss()
        return out, KVCost(S=NEBULA_PARTS, N=N)

    def get_edge(self, store, src, dst, etype):
        """FETCH PROP ON <etype> src -> dst: `GetEdges`. 1 GET of the forward edge key
        (src, +etype, rank 0, dst), found or not (storage/exec/EdgeNode.h:136). Unlike a missed TAG
        read there is no follow-up scan: GetEdgePropNode just emits an empty row
        (storage/exec/GetPropNode.h:188-210) and graphd's Filter drops it -- edge_probe p3
        measures G=2, S=0 with one of its two probes missing.

        Differs from `read_edge_exists` (the layout's cheapest probe) only in having no
        short-circuit: each GetEdges node is its own branch of a UNION ALL, so both directions
        are always fetched. Returns [{"src", "dst"}] for a found edge, [] otherwise."""
        found = store.get((self._v(src), "+" + etype, "0", self._v(dst))) is not None
        return ([{"src": self._v(src), "dst": self._v(dst)}] if found else []), KVCost(G=1)
