"""Microbench topk access-path shapes."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param


# -------------------------------------------------------------- ranked (top-k)

_MAXDATE_PARAM = {"maxDate": {"type": "int"}}


def _topk_rowwalk(k):
    """NS7 for the per-row formats: the date index is per-tag/collection and
    value-sorted, so a global 'k newest messages before maxDate' walks it
    newest-first and STOPS at k -- no candidate set needed (the index IS the
    type restriction). nebula reads ~k+ties entries (vid in the key) + k winner
    rows; arango pays 1 doc GET per entry crossed (docid in the key)."""
    def build(args):
        return Plan([
            Step("top", "read_top_k_by_index", vids=None, prop="creationDate", k=k,
                 hi=param("maxDate"), labels=["Post", "Comment"], props=["content"]),
            Step("out", "project", rows=ref("top"),
                 fields=["id", "creationDate", "content"]),
        ], params=_MAXDATE_PARAM)
    return build


def _topk_dgraph(k):
    """NS7 for dgraph: its date index is per-PREDICATE (all dated types in one
    token space), so a typed top-k MUST carry a candidate set -- two
    dgraph.type posting reads (2 SEEK) supply it. The walk then reads WHOLE
    day-bucket postings and pays exact-ms + id GETs for every candidate in the
    buckets it crosses -- it cannot stop at k inside a bucket. That
    read-the-whole-bucket vs stop-at-k gap is the flagship read_top_k_by_index
    differentiator."""
    def build(args):
        return Plan([
            Step("cp",  "read_vertices_by_label", label="Post"),
            Step("cc",  "read_vertices_by_label", label="Comment"),
            Step("cm",  "concat", a=ref("cp"), b=ref("cc")),
            # `labels` is passed even though dgraph's read_top_k_by_index IGNORES it (its
            # date index is per-predicate, all types in one token space). nebula/arango DO
            # read it: they walk per-tag / per-collection date indexes and must know which
            # ones the ranked set spans. Free for dgraph: it never reads the argument.
            Step("top", "read_top_k_by_index", vids=ref("cm"), prop="creationDate", k=k,
                 hi=param("maxDate"), labels=["Post", "Comment"],
                 props=["content"]),
            Step("out", "project", rows=ref("top"),
                 fields=["id", "creationDate", "content"]),
        ], params=_MAXDATE_PARAM)
    return build


def _topk_scan_steps(args, one_phase):
    """NS8: top-10 newest messages with NO date index -- enter by type, read the sort key off every message, sort in
    memory. Two-phase (dgraph: rank on id+creationDate GETs, fetch content for the 10 winners) vs one-phase
    (nebula/arango: the rank read already returned the whole row). Cheaper scans (arango's collection scan, built and
    held: queries/__init__.py FUSED_SCAN_REQUIRES; nebula's whole-vertex-CF scan, not built) are not modeled here: this
    is mb2's NS8 shape, kept identical across the three engine formats until both are sized. No params.
    """
    rank = ["id", "creationDate"] + (["content"] if one_phase else [])
    steps = [
        Step("cp",  "read_vertices_by_label", label="Post"),
        Step("rp",  "read_vertex_props", vids=ref("cp"), props=rank, label="Post"),
        Step("cc",  "read_vertices_by_label", label="Comment"),
        Step("rc",  "read_vertex_props", vids=ref("cc"), props=rank, label="Comment"),
        Step("rw",  "concat", a=ref("rp"), b=ref("rc")),
        Step("top", "top_k", rows=ref("rw"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=10),
    ]
    if one_phase:
        steps.append(Step("out", "project", rows=ref("top"),
                          fields=["id", "creationDate", "content"]))
    else:
        steps += [
            Step("tv",  "pluck", rows=ref("top"), field="_vid"),
            Step("bd",  "read_vertex_props", vids=ref("tv"), props=["content"],
                 label=["Post", "Comment"]),
            Step("z",   "join", left=ref("top"), right=ref("bd"),
                 left_on="_vid", right_on="_vid"),
            Step("out", "project", rows=ref("z"),
                 fields=["id", "creationDate", "content"]),
        ]
    return Plan(steps)


def topk10_scan(args):
    return _topk_scan_steps(args, one_phase=False)        # dgraph two-phase


def topk10_scan_onephase(args):
    return _topk_scan_steps(args, one_phase=True)         # nebula/arango


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

def topk1_dgraph_engine(args):
    """From dgraph/topk1/p2/plan.txt. Block 0, one root with order + filter:

      `creationDate lt(maxDate)` at the root -- ineq_root_scan (dgraph_engine.py): a key-only
          walk from maxDate's day token BACK to the start of the index (1 SEEK + 1 NEXT per
          token), one posting read per token (1 SEEK each), and an exact re-check of every uid in
          maxDate's own day bucket (1 SEEK each). Every dated type: persons and forums too.
      `@filter(type(post) OR type(comment))` -- one type-index SEEK per branch.
      `order=creationDate desc,id first=1` -- sort_topk: the second walk of the same index,
          from its newest token down, with the second key `id` read for the candidates
          (multiSort; see sort_topk for why that is every candidate).
      `id`, `creationDate`, `content` [vals=1] -- 1 GET each on the winner.

    At the training param S and N are exact; G is -2, the split Comment type posting (see
    nhop.chain_dgraph_engine). It fits only because G is ~424k, not because it is modelled."""
    return Plan([
        Step("w",   "ineq_root_scan", prop="creationDate", fname="lt",
             hi=param("maxDate")),                                     # block 0: lt (root)
        Step("tp",  "read_filter_by_label_index", vids=ref("w"), label="Post"),     # type(post)
        Step("tc",  "read_filter_by_label_index", vids=ref("w"), label="Comment"),  # type(comment)
        Step("cm",  "concat", a=ref("tp"), b=ref("tc")),
        Step("cr",  "annotate", items=ref("cm"), field="creationDate", value=0),
        Step("top", "sort_topk", rows=ref("cr"), prop="creationDate", k=1,
             second_key="id", id_order="asc"),                         # block 0: order + first
        Step("tv",  "pluck", rows=ref("top"), field="_vid"),
        Step("pr",  "read_vertex_props", vids=ref("tv"),
             props=["id", "creationDate", "content"]),                 # block 0: id, creationDate, content
        Step("out", "project", rows=ref("pr"), fields=["id", "creationDate", "content"]),
    ], params=_MAXDATE_PARAM)


ENGINE_DGRAPH = {"topk1": topk1_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

def topk1_nebula_engine(args):
    """From nebula/topk1/p2/plan.txt. Nodes 11 and 12 TagIndexRangeScan: index_id 28 (Post) and
    29 (Comment), RANGE creationDate [__EMPTY__, maxDate), no limit -- the TopN is NOT pushed into
    the scans, so each returns EVERY entry below maxDate, and reads each entry's base row
    (content is not an index column; NebulaEngineFormat.tag_index_scan). Node 7 Union, node 13
    TopN (count 1) and the Projects are in memory. Ties sort by id here (the live query sorts
    by the string vid; only the row count is compared live)."""
    return Plan([
        Step("po",  "tag_index_scan", label="Post", prop="creationDate", hi=param("maxDate"),
             base_row=True, props=["creationDate", "content"]),      # node 11 TagIndexRangeScan
        Step("co",  "tag_index_scan", label="Comment", prop="creationDate", hi=param("maxDate"),
             base_row=True, props=["creationDate", "content"]),      # node 12 TagIndexRangeScan
        Step("un",  "concat", a=ref("po"), b=ref("co")),             # node 7 Union
        Step("top", "top_k", rows=ref("un"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=1),   # node 13 TopN
        Step("out", "project", rows=ref("top"),
             fields=["id", "creationDate", "content"]),              # nodes 3/6 Project
    ], params=_MAXDATE_PARAM)


ENGINE_NEBULA = {"topk1": topk1_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

def topk1_arangodb_engine(args):
    """From arangodb/topk1/p2/plan.txt. Two independent subqueries, one per collection:
    [29] IndexNode persistent(creationDate) on Post, walked newest-first below maxDate, with
    [8] LimitNode 1 and [31] MaterializeNode (_key, content, creationDate); [30]/[18]/[32] the
    same on Comment. Each is one read_top_k_by_index over ONE collection with k=1: 1 SEEK +
    1 NEXT + 1 document GET (the index key holds the document id). [22] UNION, [23]
    EnumerateList, [26] SortNode and [27] LimitNode 1 merge the two winners in memory.

    Known mismatch in the op, not the plan: read_top_k_by_index drains entries that TIE the
    k-th date, while the engine's LIMIT 1 on the index stops at one entry. On a tie at the
    newest date it would over-read here by the tie count."""
    return Plan([
        Step("tp",  "read_top_k_by_index", vids=None, prop="creationDate", k=1,
             hi=param("maxDate"), labels=["Post"], props=["content"]),     # [29] + [8] + [31]
        Step("tc",  "read_top_k_by_index", vids=None, prop="creationDate", k=1,
             hi=param("maxDate"), labels=["Comment"], props=["content"]),  # [30] + [18] + [32]
        Step("u",   "concat", a=ref("tp"), b=ref("tc")),                   # [22] UNION, [23]
        Step("top", "top_k", rows=ref("u"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=1),         # [26] Sort, [27] Limit
        Step("out", "project", rows=ref("top"), fields=["id", "creationDate", "content"]),
    ], params=_MAXDATE_PARAM)


ENGINE_ARANGODB = {"topk1": topk1_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}
