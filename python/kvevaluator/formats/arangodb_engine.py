"""ArangoDB engine-faithful format: the plan the real engine runs, not the cheapest one.

ArangodbFormat's operators already charge what the engine does per access (covering edge index, pk + doc per vertex
read), so they are inherited (INHERITS); the engine's own plan shapes are new ops (OVERRIDES: path enumeration, a
filtered traversal, a document fetch per path step). The engine plan does not use a date index (the optimizer does not
pick it even where the design has one), pays for whole documents where a traversal node has no vertex projection, and
charges nothing for a FilterNode whose predicate was pushed into the traversal.
"""
from kvevaluator.formats.arangodb import ArangodbFormat
from kvevaluator.cost import KVCost
from kvevaluator.schema.vids import key_id

# `label` value meaning "resolve the collection from the vid itself". docs_per_step's
# _collection() takes any non-str label that way, which is what the engine does when it
# learned the collection from an edge document's _from/_to.
BY_VID = ()

OVERRIDES = frozenset({
    "traverse_paths",   # NEW op: path enumeration, not distinct-vertex frontiers
    "traverse_filtered",# NEW op: a traversal with a vertex postFilter (the double read)
    "docs_per_step",    # NEW op: a document fetch per path step, no dedup
    "traversal_open",   # NEW op: the one fixed SEEK arango's first traversal pays
    "traverse_filtered_limit",  # NEW op: a 1-hop filtered traversal cut short by a LIMIT
})
INHERITS = frozenset({
    "add_vertex", "add_edge",
    "read_vertex_by_id",        # pk GET -- the engine's document lookup
    "read_neighbors",        # TraversalNode: edge-index SEEK + 1 NEXT per edge, index is covering
    "read_vertex_props",        # pk + doc per distinct vid, the id-indirection the engine pays
    "read_anchor_props", "read_index_eq", "read_index_range", "read_vertices_by_label", "read_filter_by_label_probe",
    "read_filter_by_label_index",
    "read_top_k_by_index",   # deliberately NOT used: the optimizer ignores the date index
    "read_chase", "read_closure", "read_edge_exists",
})


class ArangodbEngineFormat(ArangodbFormat):
    CHASE_READS_ONCE = False   # the engine runs the chase per row: a node read again per chain
    CHASE_FIRST_EDGE_ONLY = False   # and scans each hop's whole edge list
    name = "arangodb_engine"
    PROPS_CODEC = "named"   # the real engine stores every edge-prop name (the knob is the evaluator's fairness setting)
    _ROW_LABELS = False     # the engine's own reads: a bare-id read under one collection pays its miss (IS4)

    @classmethod
    def capabilities(cls, schema):
        # the engine twin runs the engine's own plans: it does not advertise the fair format's scan of a collection's
        # documents, so a capability-selected bundle run on its store keeps the body it had (the op itself is inherited)
        return super().capabilities(schema) - {"label_rows_one_run"}

    def _update_index_cols(self, label, props):
        """Real arango rewrites EVERY index of the label on any update, touched or not (mb2 measured
        MB82 == MB81). The layout does not force it, so the base format moves only the changed
        columns' entries; the engine's choice is kept here."""
        return list(self.schema.index_cols_of(label))

    # ------------------------
    # --- CUSTOM OPERATORS ---
    # ------------------------

    def traversal_open(self, store, items, etype):
        """The ONE extra SEEK arango's first traversal pays, on top of its edge-index seeks.

        Measured directly rather than inferred, because the captured plans do not show it --
        one probe per construct, /kvstats delta as the answer:

            RETURN DOCUMENT('Post/3')                                S=0   (no traversal)
            1 traversal  (1 edge index, 1 start vertex)              S=2
            2 traversals (1 edge index each, same start)             S=3
            3 traversals (1 edge index each, same start)             S=4
            1 traversal  (2 edge indexes: ANY knows)                 S=3

        So it is not 2 seeks per index, and not 1 fixed seek per query: it is +1 on the FIRST
        traversal only, whatever its index count. A query with no traversal at all never pays
        it. That reading is what makes the IS cells land exactly -- IS6 runs three traversals
        and measures S=4 (2+1+1), which neither of the other two readings produces.

        It is an engine cost, not a layout cost: nothing about the document/edge-index key
        shape requires it, so it lives here rather than in ArangodbFormat.

        Declare it once, before the first `read_neighbors`, and pass the anchor through unchanged.
        A plan that forgets it under-reports S by exactly 1, which the small IS cells catch
        immediately.

        The seek below is REAL, not a bookkeeping constant: it positions at the head of
        `etype`'s edge index, which is what opening a traversal cursor before any start
        vertex is known amounts to. Charging S=1 without performing it would leave the
        per-step oracle red, and a phantom cost nothing in the store ever performs is
        exactly the kind of fudge the oracle exists to catch. What is MEASURED here is that
        the seek happens; that it lands on the edge index is inferred from where the
        traversal is about to read.
        """
        store.seek(("f", etype))
        return items, KVCost(S=1)

    def traverse_paths(self, store, srcs, etype, dir="both", depth=3, pairs=False):
        """A variable-depth `TraversalNode`: enumerate PATHS, not distinct vertices.

        This is the one place arango's engine differs structurally from the other two on IC1.
        Nebula's traverse dedups its frontier (`dedup: 1`) and visits 1682 distinct vertices;
        arango enumerates every path of length 1..depth and emits one row per path step, so
        the same vertex reached by ten paths is emitted ten times. The captured plan says so:
        node 3 is `depth=1..3` with items=42 filtered=22408, i.e. **22450 path steps**, and
        live NEXT is 22458.

        Uniqueness: arango's default is `uniqueEdges: "path"` -- an EDGE cannot repeat within
        a path, but a VERTEX can, reached by a different edge. That is modelled literally
        here, using the edge id the covering index carries in its KEY.

        On the LOADED LDBC data the two rules happen to coincide for a 2-hop `knows` walk:
        the live DB has exactly one edge document per pair (10 edge docs for a person with 10
        neighbours), so a back-hop reuses the same edge and is blocked either way -- measured,
        `FOR v IN 1..2 ANY p knows FILTER v._id == p RETURN 1` returns 0 rows. The edge rule
        is still what is implemented, because it is what arango documents and what a
        differently-loaded dataset would follow.

        This is what cost_eval had to approximate with a sampled path-explosion factor; here
        it is just enumerated. The known IC1 arango residual (22450 engine path steps against
        21905 enumerated) is NOT this rule -- see traverse_filtered.

        Cost: one edge-index SEEK per (expanded vertex, direction) plus one NEXT per edge
        crossed -- the same mechanics as `read_neighbors`, applied per path rather than per
        distinct vertex. `pairs` (as in read_neighbors) returns {src, dst} rows, src = the vertex
        the step was reached from; it changes the output only, never the cost.
        """
        sources, steps, cost, _, _ = self._walk(store, srcs, etype, dir, depth)
        if pairs:
            return [{"src": src, "dst": dst} for src, _, dst in steps], cost
        return [dst for _, _, dst in steps], cost

    def _walk(self, store, srcs, etype, dir, depth):
        """The path walk itself, shared by `traverse_paths` and `traverse_filtered`.

        Returns (expansion_sources, emitted_steps, cost, depths, paths). `expansion_sources` is every vertex
        the traversal expanded FROM -- the start vertices plus each step emitted at a depth
        below the last. They are listed separately because a traversal's vertex postFilter is
        evaluated on them too (see traverse_filtered), while only the steps are emitted.

        Each emitted step is `(src, eid, dst)`: the vertex it was reached from, the edge id
        (which the covering index carries in its KEY), and the vertex itself. The edge id is
        what lets a caller price an EDGE-property filter -- the index value covers only the
        opposite vertex's _id, so reading a property like `workFrom` costs a GET of the edge
        document.

        `depths[i]` is the path length of `emitted_steps[i]` (1..depth). The walk goes level by
        level, so the depth is known for free -- no extra read. IC1 sorts by it (the AQL's
        `MIN(LENGTH(p.edges))` per friend).

        `paths[i]` = (vertices, edge ids) of the path that ends in `emitted_steps[i]`, start
        vertex included -- what a traversal's path variable holds (traverse_filtered path_out).
        """
        sides = {"out": ("f",), "in": ("t",), "both": ("f", "t")}[dir]
        sources, steps, depths, paths, S, N = [], [], [], [], 0, 0
        # A path is identified by the EDGES it used, not the vertices it touched -- the edge
        # id rides in the covering index's key (see ArangodbFormat.add_edge).
        # NOT deduped: a correlated inner traversal expands once per driving ROW, and IC11
        # measures it -- its isLocatedIn node seeks 215 times over 177 distinct companies.
        # Callers whose plan really did dedup (a COLLECT, a WITH DISTINCT) pass a deduped
        # list; this is not the place to decide that for them.
        frontier = [(s, (), (s,)) for s in srcs]
        for level in range(1, depth + 1):
            nxt = []
            for vid, path, pverts in frontier:
                sources.append(vid)
                for idx in sides:
                    prefix = (idx, etype, vid)
                    cur = store.seek(prefix)
                    S += 1
                    while cur.valid() and cur.key()[:3] == prefix:
                        eid = cur.key()[3]
                        other = cur.value().split("/", 1)[1]
                        cur.next()
                        N += 1
                        if eid in path:              # uniqueEdges: "path"
                            continue
                        steps.append((vid, eid, other))   # one emission PER PATH, no dedup
                        depths.append(level)
                        paths.append((pverts + (other,), path + (eid,)))
                        nxt.append((other, path + (eid,), pverts + (other,)))
            frontier = nxt
        return sources, steps, KVCost(S=S, N=N), depths, paths

    def traverse_filtered(self, store, srcs, etype, dir, depth, pred,
                          props=None, label=None, edge_props=None, allowed=None,
                          excluded=None, path_out=False):
        """A TraversalNode carrying a postFilter -- the shape that dominates arango's cost on
        IC9, and the reason its G is 2x nebula's on the same walk.

        THE RULE (/kvstats deltas on cumulative AQL prefixes):

            it reads every vertex it EXPANDS FROM plus every step it EMITS -- that is the
            filter evaluation -- and then reads every SURVIVING step AGAIN, as it emits it.

        What "reads" means depends on what the filter and the body actually touch, and both
        halves are priced separately:

          `props`      the destination VERTEX is materialized: pk + doc = 2 GETs.
          `edge_props` the EDGE document is read: 1 GET (its id is already in the index key,
                       so there is no pk hop). Expansion sources have no edge, so the edge
                       term covers steps and survivors only.

        Measured on three nodes with three different shapes, no free parameter:

          IC9  [3]  knows 1..2, vertex filter      E=11  T=658    V=658
                    2 x (11 + 658) + 2 x 658                        = 2654   live 2654
          IC11 [7]  workAt, EDGE filter, vertex    E=346 T=744    V=215
                    never referenced -> props=None
                    0 + 744 + 215                                   =  959   live  959
          IC11 [10] isLocatedIn, vertex filter     E=215 T=215    V=0
                    2 x (215 + 215) + 0                             =  860   live  860

        The IC9 depth sweep is what identifies the E term as per-EXPANSION rather than
        per-step: the same walk costs 2 x (11 + 658 + 658) at depth 1..2 and 2 x (1 + 10 + 10)
        at depth 1..1, and 11 = 1 start + 10 depth-1 vertices.

        The second read is not the projection: adding a query's real `RETURN {...}` on top of
        a filtered traversal costs zero more, and the doubling appears even when the body is
        `RETURN 1`.

        NOT covered here, and deliberately left to the plan: some traversals also materialize
        their START vertex once per expansion even with no filter and nothing referenced.
        IC11 [10] does (215 GETs x 2 for `company`); IC9 [7] does not (0 for `f`). The
        difference tracks whether the start was already a materialized document -- `f` is a
        COLLECT group key, `company` is a previous traversal's output -- but that is a
        hypothesis, not something these measurements settle, so the plan charges it as an
        explicit `docs_per_step` where it is measured rather than hiding it in this operator.

        `allowed` (optional, a step output): the postFilter also requires the reached vertex
        to be in this set -- `v._id IN <list computed earlier in the query>` (IC12's
        `tc._id IN allClasses`, `tag._id IN relevantTagIds`). A lambda `pred` cannot see a
        step output, so the set comes in as an argument. In memory: no extra read.
        `excluded` is the mirror: `v._id != <vertex found earlier>` (IC6's
        `otherTag._id != targetTag._id`).

        `path_out`: the node also emits its PATH variable (`pathOutVariable` in the captured
        plan; IC1's `COLLECT ... INTO paths = p` keeps it). For each surviving path,
        TraversalExecutor::doOutput -> SingleProviderPathResult::toVelocyPack builds the path
        object, reading every vertex on it (start included: pk + doc = 2 GETs) and every edge
        (1 GET): 3d + 2 GETs for a path of length d. IC1 [3], sf0.3: this is the whole G gap
        the operator would leave at all 6 params without it (p19: 3 x 3960 + 2 x 1332 = 14544). Assumes
        the node's `producePathsVertices` and `producePathsEdges` are both true (IC1 [3]: both);
        a plan with either off would read only that half.
        """
        sources, steps, cost, depths, paths = self._walk(store, srcs, etype, dir, depth)
        dsts = [d for _, _, d in steps]
        G = 0

        # --- the filter evaluation: expansion sources and emitted steps ---
        # `_depth` = the path length of the step (in memory, the walk is level by level)
        rows = [{"_vid": d, "_src": s, "id": key_id(d), "_depth": k}
                for (s, _, d), k in zip(steps, depths)]
        if props:
            # The SOURCES are not necessarily in `label`'s collection -- IC11's isLocatedIn
            # node expands from Organisations and lands on Places. The engine knows each
            # source's collection from the edge doc's _from/_to it already read, so resolve
            # it from the vid (BY_VID) rather than assuming the destination's.
            _, c0 = self.docs_per_step(store, sources, props, BY_VID)
            checked, c = self.docs_per_step(store, dsts, props, label)
            G += c0.G + c.G
            for row, got in zip(rows, checked):
                row.update({k: v for k, v in got.items() if k != "_vid"})
        if edge_props:
            for row, (_, eid, _) in zip(rows, steps):
                doc = store.get((etype, eid)) or {}      # edge doc: 1 GET, id is in the key
                G += 1
                row.update({p: doc.get(p) for p in edge_props})

        keep = None if allowed is None else set(allowed)
        drop = set(excluded or ())
        kept_i = [i for i, r in enumerate(rows) if pred(r) and (keep is None or r["_vid"] in keep)
                  and r["_vid"] not in drop]
        kept = [rows[i] for i in kept_i]

        # --- the emission: every survivor, read a second time ---
        if props:
            _, c = self.docs_per_step(store, [r["_vid"] for r in kept], props, label)
            G += c.G
        if edge_props:
            by_vid = {d: eid for _, eid, d in steps}
            for r in kept:
                store.get((etype, by_vid[r["_vid"]]))    # the same edge docs, again
                G += 1

        # --- the path variable: each surviving path's vertices and edges, read to build it ---
        if path_out:
            for i in kept_i:
                pverts, peids = paths[i]
                _, c = self.docs_per_step(store, list(pverts), props or ["_key"], BY_VID)
                G += c.G
                for eid in peids:
                    store.get((etype, eid))              # edge doc: 1 GET, id is in the key
                    G += 1
        return kept, KVCost(S=cost.S, G=G, N=cost.N)

    def traverse_filtered_limit(self, store, srcs, etype, dir, pred, limit, props=None,
                                label=None, edge_props=None, order="dfs", allowed=None):
        """A 1..1 TraversalNode with a postFilter whose LimitNode cuts it short -- the
        `FOR v IN 1..1 ANY src knows FILTER v._id == dst LIMIT 1` probe (microbench edge_probe,
        LDBC IC7's isFriend). One independent traversal per start vertex (per driving row).

        `allowed` (optional, a step output): the postFilter also requires the reached vertex to
        be in this set, as in traverse_filtered -- `v._id IN <list computed earlier>` (IC10's
        `tag._id IN myTags`). In memory: no extra read.

        Same read rule as `traverse_filtered` (expansion source + each evaluated step, then
        each survivor again), with one difference: the EDGES are read in full, the VERTICES
        only until `limit` survivors are found. The reason is in the engine (3.12.8):

          - Graph/Queues/CursorLifoQueue.h pop(): popping a vertex's neighbour cursor pulls
            a whole batch (`cursor.next()`, Graph/Cursors/SingleServerEdgeCursor.cpp readNext,
            up to ExecutionBlock DefaultBatchSize = 1000 edges per edge index) and pushes
            every step at once. So every edge of the start vertex is read (SEEK per index +
            1 NEXT per edge), whatever the LIMIT.
          - Graph/Enumerators/OneSidedEnumerator.cpp computeNeighbourhoodOfNextVertex(): the
            steps are then popped and validated (the vertex postFilter, which fetches the
            vertex document) ONE AT A TIME, and getNextPath() returns as soon as one passes.
            The LimitNode stops asking after `limit` rows, so the rest are never fetched.

        WHEN THE CUT HAPPENS -- read it off the captured plan: between the TraversalNode and
        the LimitNode there may only be nodes that forward the row limit upstream
        (CalculationNode does: CalculationExecutor.cpp returns output.getClientCall()). A
        FilterNode breaks it; so would a Sort or Collect. Top level or inside a per-row
        subquery makes no difference. Precondition: no `fullCount` cursor option -- with it
        the LIMIT fast-forwards through TraversalExecutor::skipRowsRange, which evaluates
        every remaining path (no runner here sets it). The mechanism:

          - Aql/Executor/LimitExecutor.cpp calculateUpstreamCall(): the LIMIT asks upstream
            for at most `limit` rows (hardLimit). Aql/Executor/TraversalExecutor.cpp
            produceRows() loops `while (!output.isFull())`, and OutputAqlItemRow.h
            numRowsLeft() is min(block rows, the call's limit) -- so the traversal stops
            after `limit` rows.
          - Aql/Executor/FilterExecutor.cpp produceRows() returns `AqlCall{}` upstream ("Just
            fetch everything from above, allow overfetching"): a FilterNode drops the limit,
            so a traversal under one fills a whole block and evaluates every step.
          - Whether the FilterNode survives is the optimizer rule
            `remove-filter-covered-by-traversal` (Aql/OptimizerRules.cpp
            removeFiltersCoveredByTraversal): it unlinks the FILTER when the condition was
            fully pushed into the traversal. It fires for a literal / bind-var / DOCUMENT()
            target and did NOT fire for a `FIRST(subquery)._id` target (IS7's origAuthor).

        Measured, /kvstats + profile on sf0.3 (Person/102, 41 knows edges; one
        run per AQL shape, repeated twice, identical):

            target / placement                      FilterNode  filtered  G    cut
            literal, subquery, match popped first   removed     1         6    yes
            literal, subquery, match popped last    removed     41        86   (nothing to cut)
            DOCUMENT(x)._id, subquery (IC7 shape)   removed     1         8    yes
            FIRST(subquery)._id, subquery (IS7)     kept        41        90   NO
            literal, top level                      removed     1         6    yes
            FIRST(subquery)._id, top level          kept        41        90   NO
            3 rows, per-row subquery (IC7 shape)    removed     94        194  yes, per row

        (`filtered` counts the start vertex too when it fails: the postFilter also runs on
        depth 0.) G = 2 (start) + 2 x evaluated steps + 2 x survivors, e.g. 2 + 2 + 2 = 6 and
        2 + 82 + 2 = 86; the other rows add resolving the target (DOCUMENT +2, FIRST(subquery)
        +4). The 3-row case: starts of degree 41/47/44, the target is row 1's first-popped
        neighbour, so 3 x 2 + 2 x (1 + 47 + 44) + 2 x 1 survivor + 2 (DOCUMENT) = 194.
        So LDBC IC7 [22] (no FilterNode) cuts, and LDBC IS7 [13] (FilterNode
        [15] kept) evaluates every reached vertex -- IS7 keeps `traverse_filtered`.

        Pop order: AQL's default traversal order is DFS (TraverserOptions.cpp, mode(Order::DFS)),
        whose CursorLifoQueue push_front()s each step of the batch -- the LAST edge read is
        evaluated FIRST. BFS (CursorFifoQueue) evaluates in read order. Within a batch the
        edge indexes are read in the plan's `base:index` order, each in its key order
        (insertion order: the index key ends in the LocalDocumentId, as ours ends in the
        edge id). So how many vertices are fetched depends on where the match sits in that
        order -- data-dependent, and reproduced only if our load order matches the engine's.

        Limits (not modelled, say so rather than guess): depth 1 only; a start vertex with
        > 1000 edges per index would need more than one batch, which this charges as one.

        Load-order dependency: our edge ids are a global load counter (`e%011d`, arangodb.py),
        so our per-vertex edge order equals arango's only because both loads inserted edges in
        the same order. A re-ingest in another order moves the match position and the count.
        """
        if order not in ("dfs", "bfs"):
            raise ValueError(f"order must be dfs or bfs, got {order!r}")
        keep = None if allowed is None else set(allowed)
        out, S, G, N = [], 0, 0, 0
        for src in srcs:                       # one subquery run per driving row: its own LIMIT
            sources, steps, cost, _, _ = self._walk(store, [src], etype, dir, 1)   # every edge: S + N
            S, N = S + cost.S, N + cost.N
            if order == "dfs":
                steps = steps[::-1]                      # LIFO: last edge read pops first
            if props:                                    # the start vertex, validated first
                _, c0 = self.docs_per_step(store, sources, props, BY_VID)
                G += c0.G
            kept = []
            for s, eid, d in steps:
                if len(kept) >= limit:
                    break                                # LimitNode satisfied: no more pops
                row = {"_vid": d, "_src": s, "id": key_id(d), "_depth": 1}
                if props:
                    got, c = self.docs_per_step(store, [d], props, label)
                    G += c.G
                    row.update({k: v for k, v in got[0].items() if k != "_vid"})
                if edge_props:
                    doc = store.get((etype, eid)) or {}  # edge doc: 1 GET, id is in the key
                    G += 1
                    row.update({p: doc.get(p) for p in edge_props})
                if pred(row) and (keep is None or d in keep):
                    kept.append((row, eid))
            # the emission: every survivor, read a second time (traverse_filtered's rule)
            for row, eid in kept:
                if props:
                    _, c = self.docs_per_step(store, [row["_vid"]], props, label)
                    G += c.G
                if edge_props:
                    store.get((etype, eid))
                    G += 1
            out.extend(row for row, _ in kept)
        return out, KVCost(S=S, G=G, N=N)

    def docs_per_step(self, store, vids, props, label=None):
        """Materialize the document behind EVERY traversal output, with no dedup.

        This is how a TraversalNode reads its destination vertex, and it differs from
        `ArangodbFormat.read_vertex_props` in two ways that both cost GETs:

        1. **No dedup.** read_vertex_props reads each distinct vid once -- correct for a plan that
           knows what it wants. A traversal has no such view: it fetches the document at each
           path step, re-reading a vertex it already saw on another path (IC1's explosion).
        2. **It fetches even when only the key is wanted.** Measured:
           `FOR v IN 1..1 OUTBOUND <p> isLocatedIn RETURN v._key` costs 2 GETs, the same as
           `RETURN v.name` -- although the `_key` is already in the edge index's value. So
           requesting `props=["id"]` here is NOT free, unlike read_vertex_props where `id` is
           key-derived. That gap is real engine waste and reproducing it is the point.

        It is demand-driven, not unconditional: a traversal whose vertex is never referenced
        costs nothing (`RETURN 1` measures G=0). Use `read_neighbors` alone for those.

        Cost: pk + doc = 2 GETs per STEP, or 1 when the pk misses (nothing to address).
        """
        if label is None:
            raise ValueError("arango docs_per_step needs label (collection or candidates)")
        # message vids (candidate list) recover their collection from _coll -- rebuild it
        # from the store first if this format was opened without ingest (cached-open path).
        if not isinstance(label, str):
            self._ensure_coll(store)
        rows, G = [], 0
        for v in vids:                                # NOT dict.fromkeys -- repeats are real
            loc = store.get(("pk", self._collection(store, v, label), v))
            G += 1
            doc = {}
            if loc:                       # charge the doc read ONLY when it happens: a vid
                doc = store.get(loc)      # outside `label`'s collection stops at the pk miss
                G += 1
            # _label = concrete collection (free: the traversal held the collection-qualified
            # _id), so an IS_SAME_COLLECTION filter reads the type off the row, not the vid.
            row = {"_vid": v, "_label": label if isinstance(label, str) else self._coll.get(v),
                   "id": key_id(v)}
            row.update({p: (doc or {}).get(p) for p in props if p != "id"})
            rows.append(row)
        return rows, KVCost(G=G)
