"""Microbench entry access-path shapes."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param, let, eq, and_, le, lt, field, in_, tuple_, not_, sub, floordiv, lit
from kvevaluator.workloads.microbench._shared import DAY, DATE_WINDOW_PARAM, DATE_WINDOW_LET


# ------------------------------------------------------- entry-point selection

def _entry_label(label):
    """NS2 'find start vids by type': the type-enumeration structure, whole-term.
    dgraph reads ONE dgraph.type posting (1 SEEK, all uids in the value); nebula
    walks a per-row index (1 SEEK + M NEXT); arango range-scans its collection's
    pk (1 SEEK + M NEXT). Final = count (message vids don't agree raw, and an
    id projection would charge dgraph M GETs of pure output materialization)."""
    def build(args):
        return Plan([
            Step("v",   "read_vertices_by_label", label=label),
            Step("d",   "dedup", items=ref("v")),
            Step("n",   "count", items=ref("d")),
            Step("out", "as_row", value=ref("n"), field="count"),
        ])
    return build


_FIRSTNAME_PARAM = {"firstName": {}}


def entry_eq(args):
    """NS3 'find start vids by indexed value' + the id projection. read_index_eq is
    the split: dgraph 1 SEEK (uids in the value); nebula 1 SEEK + v NEXT (vid in
    the key); arango 1 SEEK + v NEXT + v GET (docid in the key -> doc read to
    recover the _key). The id fetch then costs dgraph v GETs, nebula/arango 0
    (key-derived)."""
    return Plan([
        Step("v",   "read_index_eq", label="Person", prop="firstName", value=param("firstName")),
        Step("d",   "dedup", items=ref("v")),
        Step("pr",  "read_vertex_props", vids=ref("d"), props=["id"], label="Person"),
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id"]),
    ], params=_FIRSTNAME_PARAM)


def entry_eq_scan(args):
    """NS4: the same lookup with NO value index -- enter by type, read the prop
    off every vertex, filter. The per-vertex prop read IS the price of the
    missing index (same block the LDBC label-only plans use). Where a whole-label read takes one scan of the
    label's rows by default (whole_label_scan_by_default: pertype_csr, meta) the enumerate + read is fused into it;
    ArangoDB's scan is built and held as the default here (queries/__init__.py FUSED_SCAN_REQUIRES)."""
    return Plan([
        Step("v",   "read_vertices_by_label", label="Person"),
        Step("pp",  "read_vertex_props", vids=ref("v"), props=["firstName"], label="Person"),
        Step("fx",  "filter", rows=ref("pp"), pred=eq(field("firstName"), param("firstName"))),
        Step("vv",  "pluck", rows=ref("fx"), field="_vid"),
        Step("d",   "dedup", items=ref("vv")),
        Step("pr",  "read_vertex_props", vids=ref("d"), props=["id"], label="Person"),
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id"]),
    ], params=_FIRSTNAME_PARAM)


def entry_range_scan(args):
    """NS5 with NO date index -- entry_range's twin, the way entry_eq_scan is entry_eq's. Enter by type, read
    creationDate off every person, keep the window.

    It is a DENOMINATOR, so it is kept although its block is entry_eq_scan's: entry_eq's indexed cost is 1 SEEK in
    every format, while entry_range's is a per-tag sorted walk (nebula/arango) or a day-token walk + type
    restriction + boundary trim (dgraph), so "what does the DATE index buy?" cannot be read off entry_eq_scan.

    No per-format override: the answer is a count (nothing is re-read), and the plan reads no index, so dgraph's day
    granularity cannot leak in."""
    return Plan([
        Step("v",   "read_vertices_by_label", label="Person"),
        Step("pp",  "read_vertex_props", vids=ref("v"), props=["creationDate"], label="Person"),
        Step("fx",  "filter", rows=ref("pp"),
             pred=and_(le(param("startDate"), field("creationDate")),
                       lt(field("creationDate"), let("end")))),
        Step("n",   "count", items=ref("fx")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM, let=DATE_WINDOW_LET)


def entry_range(args):
    """NS5 'find start vids by indexed value range' (Person.creationDate window,
    [start, start+days)). nebula/arango read exactly the window off their
    value-sorted per-tag index. Final = count row."""
    return Plan([
        Step("w",   "read_index_range", labels=["Person"], prop="creationDate",
             lo=param("startDate"), hi=let("end")),
        Step("d",   "dedup", items=ref("w")),
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM, let=DATE_WINDOW_LET)


def entry_range_dgraph(args):
    """entry_range on the day-token index: dgraph's date index is DAY-granular and per-PREDICATE (all dated types share
    it), so the exact window costs three extra things the per-tag sorted indexes don't pay:
      1. boundary-day trim -- only the candidates on the lo/hi day tokens pay an exact-ms GET;
      2. type restriction -- one dgraph.type posting read (1 SEEK) + a free intersect keeps the persons;
      3. the walk itself unions whole day postings (uids in the value).
    Intersect BEFORE the trim so only person boundary candidates pay the GET.

    ONE cursor: the day tokens are adjacent keys, so index_range_buckets walks the span once and tags each candidate
    with its day, and the boundary/interior split is two free filters. 2 SEEKs is the floor: the type restriction
    reads another key family, and a dgraph uid is opaque, so the type cannot be skipped.
    """
    # _bucket is the day NUMBER now (index_range_buckets), so the boundary days are plain
    # floordiv, no key-string formatting -- which is what lets this dgraph plan be neutral IR.
    return Plan([
        Step("per",  "read_vertices_by_label", label="Person"),
        # end_excl = end-1: the last day that can hold an in-window value, since end is exclusive
        Step("day",  "index_range_buckets", labels=["Person"], prop="creationDate",
             lo=param("startDate"), hi=let("end_excl")),
        # boundary days straddle the window edges -> their members still need the exact-ms check
        Step("bndr", "filter", rows=ref("day"),
             pred=in_(field("_bucket"), tuple_(let("lo_day"), let("hi_day")))),
        Step("bnd",  "pluck", rows=ref("bndr"), field="_vid"),
        # interior days are wholly inside the window -> no re-check possible to fail
        Step("winr", "filter", rows=ref("day"),
             pred=not_(in_(field("_bucket"), tuple_(let("lo_day"), let("hi_day"))))),
        Step("win",  "pluck", rows=ref("winr"), field="_vid"),
        Step("bp",  "intersect", items=ref("bnd"), other=ref("per")),
        Step("bd",  "dedup", items=ref("bp")),
        Step("br",  "read_vertex_props", vids=ref("bd"), props=["creationDate"], label="Person"),
        Step("bf",  "filter", rows=ref("br"),
             pred=and_(le(param("startDate"), field("creationDate")),
                       lt(field("creationDate"), let("end")))),
        Step("bv",  "pluck", rows=ref("bf"), field="_vid"),
        Step("wp",  "intersect", items=ref("win"), other=ref("per")),
        Step("all", "concat", a=ref("wp"), b=ref("bv")),
        Step("d",   "dedup", items=ref("all")),
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM,
        let={**DATE_WINDOW_LET,
             "end_excl": sub(let("end"), 1),
             "lo_day": floordiv(param("startDate"), lit(DAY)),
             "hi_day": floordiv(let("end_excl"), lit(DAY))})



# ------------------------------------------------------------ logical plans (the optimizer's)

def entry_eq_logical(args):
    """entry_eq with the lookup as a choice point: the value index (the hand plan) or a scan of
    the label with a per-vertex property read (what entry_eq_scan pins)."""
    return Plan([
        Step("v",   "entry_eq", label="Person", prop="firstName", value=param("firstName")),
        Step("d",   "dedup", items=ref("v")),
        Step("pr",  "read_vertex_props", vids=ref("d"), props=["id"], label="Person"),
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id"]),
    ], params=_FIRSTNAME_PARAM)


LOGICAL = {"entry_eq": entry_eq_logical}


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

def _entrylabel_dgraph_engine(label):
    """From dgraph/entrylabel_<label>/p1/plan.txt. Block 0: `type(<label>)` at the root reads
    the one dgraph.type posting (1 SEEK). `uid` [vals=n] is the uid itself: no read."""
    def build(args):
        return Plan([
            Step("v",   "read_vertices_by_label", label=label),          # block 0: dgraph.type eq(<label>)
            Step("d",   "dedup", items=ref("v")),
            Step("n",   "count", items=ref("d")),
            Step("out", "as_row", value=ref("n"), field="count"),
        ])
    return build


def entry_eq_dgraph_engine(args):
    """From dgraph/entry_eq/p2/plan.txt. Block 0: `firstName eq(...)` at the root is one hash-index
    posting read (1 SEEK); then `id` [vals=n]: 1 GET per match. No type filter in the query.
    The engine returns uid order; the sort is the optimal plan's in-memory order, kept so the
    row check compares like with like (it reads nothing)."""
    return Plan([
        Step("v",   "read_index_eq", label="Person", prop="firstName",
             value=param("firstName")),                                # block 0: firstName eq
        Step("pr",  "read_vertex_props", vids=ref("v"), props=["id"]), # block 0: id
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id"]),
    ], params=_FIRSTNAME_PARAM)


def entry_range_dgraph_engine(args):
    """From dgraph/entry_range/p2/plan.txt. Block 0: `creationDate between(start, end)` at the
    root, then `@filter(type(person))` [src=all dated uids in the window].

    The root is ineq_root_scan (dgraph_engine.py): a key-only walk over the window's day tokens
    (1 SEEK + 1 NEXT per token), one posting read per token (1 SEEK each), and an exact-value
    re-check of EVERY uid in the first and last day bucket, of every type (1 SEEK each). The
    type filter then runs over the whole window (1 type-index SEEK). `uid` is free.
    between() is inclusive at both ends; the optimal window is [start, end)."""
    return Plan([
        Step("w",   "ineq_root_scan", prop="creationDate", fname="between",
             lo=param("startDate"), hi=let("end")),                    # block 0: between (root)
        Step("p",   "read_filter_by_label_index", vids=ref("w"), label="Person"),  # block 0: type(person)
        Step("n",   "count", items=ref("p")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM, let=DATE_WINDOW_LET)


ENGINE_DGRAPH = {"entrylabel_person": _entrylabel_dgraph_engine("Person"),
                 "entrylabel_forum": _entrylabel_dgraph_engine("Forum"),
                 "entry_eq": entry_eq_dgraph_engine,
                 "entry_range": entry_range_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

# All four are LOOKUP / MATCH entries. A LOOKUP index scan runs once per partition (100 SEEKs)
# and, because the query yields id(vertex), reads the base tag row per entry: see
# NebulaEngineFormat.tag_index_scan.

def entrylabel_person_nebula_engine(args):
    """From nebula/entrylabel_person/p1/plan.txt. Node 3 TagIndexFullScan: index_id 32 (the
    Person creationDate index -- entry_range's plan names the same id on that column), no
    column hints, no limit; `id(vertex)` makes it read each entry's base row. Node 2 Project is
    in memory. No dedup: one index entry per vertex."""
    return Plan([
        Step("v",   "tag_index_scan", label="Person", prop="creationDate",
             base_row=True),                                   # node 3 TagIndexFullScan
        Step("n",   "count", items=ref("v")),                  # node 2 Project (one row each)
        Step("out", "as_row", value=ref("n"), field="count"),
    ])


def entrylabel_forum_nebula_engine(args):
    """From nebula/entrylabel_forum/p1/plan.txt. Node 11 ScanVertices: props [_tag] of tagId 5
    (Forum), filter Forum._tag IS NOT EMPTY -- a scan of every tag row in the space, keeping the
    Forums (the live store has no Forum tag index). Node 10 AppendVertices lists no props, so
    under --optimize_appendvertices=true it returns without a storage call
    (AppendVerticesExecutor.cpp:33). Node 6 Project is in
    memory. See NebulaEngineFormat.scan_vertices for the layout gap: S and N are the engine's,
    bytes are not."""
    return Plan([
        Step("v",   "scan_vertices", label="Forum"),           # node 11 ScanVertices
        Step("n",   "count", items=ref("v")),                  # nodes 10 AppendVertices, 6 Project
        Step("out", "as_row", value=ref("n"), field="count"),
    ])


def entry_eq_nebula_engine(args):
    """From nebula/entry_eq/p2/plan.txt. Node 4 TagIndexPrefixScan: index_id 25, column hint
    PREFIX firstName == $firstName; `id(vertex)` makes it read each entry's base row. Node 3
    Project is in memory. The live query has no ORDER BY; the in-memory sort only lines the rows
    up with the optimal plan's order for the row check."""
    return Plan([
        Step("v",   "tag_index_scan", label="Person", prop="firstName",
             value=param("firstName"), base_row=True),         # node 4 TagIndexPrefixScan
        Step("srt", "top_k", rows=ref("v"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id"]),   # node 3 Project
    ], params=_FIRSTNAME_PARAM)


def entry_range_nebula_engine(args):
    """From nebula/entry_range/p2/plan.txt. Node 4 TagIndexRangeScan: index_id 32, column hint
    RANGE creationDate [startDate, endDate) (includeBegin true, includeEnd false); `id(vertex)`
    makes it read each entry's base row. Node 3 Project is in memory."""
    return Plan([
        Step("w",   "tag_index_scan", label="Person", prop="creationDate",
             lo=param("startDate"), hi=let("end"), base_row=True),   # node 4 TagIndexRangeScan
        Step("n",   "count", items=ref("w")),                  # node 3 Project
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM, let=DATE_WINDOW_LET)


ENGINE_NEBULA = {"entrylabel_person": entrylabel_person_nebula_engine,
                 "entrylabel_forum": entrylabel_forum_nebula_engine,
                 "entry_eq": entry_eq_nebula_engine,
                 "entry_range": entry_range_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

from kvevaluator.plans.ir.authoring import key_id as _key_id  # (arangodb section only)


def _entrylabel_arangodb_engine(label):
    """From arangodb/entrylabel_<label>/p1/plan.txt. Node [5] IndexNode primary(_key) on the
    collection, projections [_key], indexCoversProjections=True, limit 0: a walk of the
    collection's primary index, the _key read off the index key, no document read
    (read_vertices_by_label: 1 SEEK + 1 NEXT per document). The primary index is unique, so
    nothing dedups; node [4] returns the keys and the harness counts them."""
    def build(args):
        return Plan([
            Step("v",   "read_vertices_by_label", label=label),        # node 5: primary index walk
            Step("n",   "count", items=ref("v")),                      # node 4: count of the keys
            Step("out", "as_row", value=ref("n"), field="count"),
        ])
    return build


def entry_eq_arangodb_engine(args):
    """From arangodb/entry_eq/p2/plan.txt. Node [9] IndexNode persistent(firstName) on Person,
    projections [_key] but NOT indexCoversProjections: the index key holds the document id, so
    each match costs 1 NEXT + 1 document GET to get its _key (read_index_eq). Nodes [5]-[8]
    (TO_NUMBER(_key), SORT, the {id} object) are in memory."""
    return Plan([
        Step("v",   "read_index_eq", label="Person", prop="firstName",
             value=param("firstName")),                                # node 9: IndexNode
        Step("r",   "annotate", items=ref("v"), field="match", value=True),
        Step("pr",  "derive", rows=ref("r"), into="id", fn=_key_id(field("_vid"))),  # node 5
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),          # node 6
        Step("out", "project", rows=ref("srt"), fields=["id"]),                      # node 7
    ], params=_FIRSTNAME_PARAM)


def entry_range_arangodb_engine(args):
    """From arangodb/entry_range/p2/plan.txt. Node [7] IndexNode persistent(creationDate) on
    Person, range [startDate, end): 1 SEEK + 1 NEXT per entry in the window. Node [8]
    MaterializeNode projections [_key]: the index key holds the document id, so 1 document GET
    per match. read_index_range charges exactly that pair. Node [6] returns the keys; the
    harness counts them."""
    return Plan([
        Step("w",   "read_index_range", labels=["Person"], prop="creationDate",
             lo=param("startDate"), hi=let("end")),                    # nodes 7 + 8
        Step("n",   "count", items=ref("w")),                          # node 6: count of the keys
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=DATE_WINDOW_PARAM, let=DATE_WINDOW_LET)


ENGINE_ARANGODB = {"entrylabel_person": _entrylabel_arangodb_engine("Person"),
                   "entrylabel_forum": _entrylabel_arangodb_engine("Forum"),
                   "entry_eq": entry_eq_arangodb_engine,
                   "entry_range": entry_range_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}
